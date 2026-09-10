"""Timeline and heatmap — PRD sections 23, 6.3 (US-5, US-6).

Read-only projections over the student's enrollments. All the arithmetic lives
in `packages/core/workload_model`; this router merges user overrides onto the
shared assessment rows and hands the result to those pure functions.
"""

from __future__ import annotations

import uuid
from datetime import date

from db.localtime import DEFAULT_TIMEZONE, local_date, local_datetime
from db.models import (
    Assessment,
    Course,
    Enrollment,
    Institution,
    Section,
    Term,
    User,
    UserOverride,
)
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from workload_model.flags import compute_flags
from workload_model.heatmap import ScheduledItem, build_heatmap

from services.api.deps import current_user, get_db

router = APIRouter(tags=["plan"])


class TimelineItem(BaseModel):
    assessment_id: str
    title: str
    course: str
    type: str
    weight_pct: float | None
    due_at: str | None
    due_precision: str
    time_inferred: bool
    is_group: bool
    source_span: str
    page_ref: int | None
    edited: bool
    #: Section 10.1's composite score and its bucket. The review screen orders
    #: by this and decides which items may auto-accept, so it has to come from
    #: the server. A client that re-derives confidence from precision alone
    #: cannot see a weekday mismatch or a failed validation rule, which are
    #: exactly the cases worth surfacing first.
    confidence: float | None
    confidence_bucket: str


class HeatmapCell(BaseModel):
    week_start: str
    effort_hours: float
    flags: list[dict]


def _institution_timezone(db: Session, user: User) -> str:
    institution = db.scalar(select(Institution).where(Institution.id == user.institution_id))
    return institution.timezone if institution else DEFAULT_TIMEZONE


def _enrolled_rows(db: Session, user: User, term_id: uuid.UUID | None):
    """Every assessment across the user's enrollments, with overrides applied."""
    query = (
        select(Assessment, Course, Section, UserOverride)
        .join(Section, Assessment.section_id == Section.id)
        .join(Course, Section.course_id == Course.id)
        .join(
            Enrollment,
            (Enrollment.section_id == Section.id) & (Enrollment.user_id == user.id),
        )
        .outerjoin(
            UserOverride,
            (UserOverride.assessment_id == Assessment.id) & (UserOverride.user_id == user.id),
        )
    )
    if term_id is not None:
        query = query.where(Section.term_id == term_id)
    return db.execute(query).all()


@router.get("/timeline", response_model=list[TimelineItem])
def timeline(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    course_id: uuid.UUID | None = Query(default=None),
    term_id: uuid.UUID | None = Query(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[TimelineItem]:
    """US-5: all courses on one timeline, merged across enrollments."""
    timezone = _institution_timezone(db, user)
    out: list[TimelineItem] = []
    for assessment, course, _section, override in _enrolled_rows(db, user, term_id):
        if course_id is not None and course.id != course_id:
            continue
        if override is not None and override.dismissed:
            continue

        due_at = override.due_at if override and override.due_at else assessment.due_at
        # The stored value is an instant in UTC; the day the student means is the
        # one in the institution's timezone (see db.localtime).
        due_day = local_date(due_at, timezone)
        if from_ and due_day and due_day < from_:
            continue
        if to and due_day and due_day > to:
            continue

        local_due = local_datetime(due_at, timezone)
        out.append(
            TimelineItem(
                assessment_id=str(assessment.id),
                title=(override.title if override and override.title else assessment.title),
                course=f"{course.subject_code} {course.catalog_number}",
                type=assessment.type,
                weight_pct=assessment.weight_pct,
                due_at=(local_due.isoformat() if local_due else None),
                due_precision=assessment.due_precision,
                time_inferred=assessment.time_inferred,
                is_group=assessment.is_group,
                source_span=assessment.source_span,
                page_ref=assessment.page_ref,
                edited=override is not None and override.due_at is not None,
                confidence=assessment.confidence,
                confidence_bucket=_bucket(assessment.confidence),
            )
        )
    return sorted(out, key=lambda i: (i.due_at is None, i.due_at or ""))


def _bucket(score: float | None) -> str:
    """Section 10.1's thresholds. Kept identical to the worker's own bucketing
    in `validate.confidence_bucket`, because a row that auto-accepts in the UI
    but reads as low-confidence in the pipeline is a contradiction the student
    would eventually notice."""
    if score is None:
        return "low"
    if score >= 0.85:
        return "high"
    if score >= 0.60:
        return "medium"
    return "low"


@router.get("/heatmap", response_model=list[HeatmapCell])
def heatmap(
    term_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[HeatmapCell]:
    """US-6: one cell per week, colored by projected effort, with collision flags."""
    term = db.scalar(select(Term).where(Term.id == term_id))
    if term is None:
        return []

    timezone = term.institution.timezone if term.institution else DEFAULT_TIMEZONE
    rows = _enrolled_rows(db, user, term_id)
    total_credits = sum({c.id: c.credits for _, c, _, _ in rows}.values())

    items: list[ScheduledItem] = []
    for assessment, course, _section, override in rows:
        if override is not None and override.dismissed:
            continue
        # tbd items are never scheduled (9.3).
        if assessment.due_precision == "tbd":
            continue
        due_at = override.due_at if override and override.due_at else assessment.due_at
        due_on = local_date(due_at, timezone)
        if due_on is None:
            continue

        items.append(
            ScheduledItem(
                assessment_id=str(assessment.id),
                title=assessment.title,
                course_label=f"{course.subject_code} {course.catalog_number}",
                assessment_type=assessment.type,
                due_on=due_on,
                credits=course.credits,
                weight_pct=assessment.weight_pct,
                is_group=assessment.is_group,
                user_calibration=user.calibration_factor,
            )
        )

    term_end = term.finals_end or term.end_date
    cells = build_heatmap(items, term.start_date, term_end, total_credits or 15)
    # Flags are recomputed here rather than cached: median weekly load shifts
    # whenever a course is added, so they must be recomputed on any enrollment
    # change (11.4).
    flags = compute_flags(items, cells, term.start_date, term_end, total_credits or 15)

    return [
        HeatmapCell(
            week_start=cell.week_start.isoformat(),
            effort_hours=round(cell.effort_hours, 1),
            flags=[
                {
                    "kind": f.kind,
                    "severity": f.severity,
                    "explanation": f.explanation,
                    "assessment_ids": f.assessment_ids,
                }
                for f in flags.get(cell.week_start, [])
            ],
        )
        for cell in cells
    ]
