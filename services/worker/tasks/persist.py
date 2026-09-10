"""Write extraction output into the database — PRD sections 12, 8, 9.

The subtle requirement, and the reason this is its own module: a section-level
parse is *shared*. Several students upload the same syllabus, dedup collapses
them onto one parse, and a re-parse must not destroy anyone's personal edits.
That is why `user_overrides` is a separate table (section 12) and why nothing
here ever touches it.

Re-running extraction for a document replaces that document's extracted rows and
leaves every override intact.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from date_resolver.calendar_context import (
    AcademicCalendar,
    CalendarExceptionEntry,
    FinalExamMatrixEntry,
)
from db.models import (
    Assessment,
    CalendarException,
    ExtractionRun,
    FinalExamMatrixRow,
    GradeCategory,
    Section,
    SectionPolicies,
    SyllabusDocument,
    Term,
)
from schemas.resolution import ResolvedDate
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def load_calendar(db: Session, term_id: uuid.UUID) -> AcademicCalendar:
    """Build the resolver's calendar from the seeded registrar rows (section 9.1).

    The resolver is pure and takes a value object; this is the only place that
    turns database rows into one.
    """
    term = db.scalar(select(Term).where(Term.id == term_id))
    if term is None:
        raise ValueError(f"No term {term_id}")

    institution = term.institution
    exceptions = db.scalars(
        select(CalendarException).where(CalendarException.term_id == term.id)
    ).all()
    matrix = db.scalars(
        select(FinalExamMatrixRow).where(FinalExamMatrixRow.term_id == term.id)
    ).all()

    from datetime import time

    hour, _, minute = institution.default_due_time.partition(":")
    return AcademicCalendar(
        term_name=term.name,
        start_date=term.start_date,
        end_date=term.end_date,
        timezone=institution.timezone,
        default_due_time=time(int(hour), int(minute or 0)),
        exceptions=tuple(
            CalendarExceptionEntry(date=e.date, label=e.label, no_class=e.no_class)
            for e in exceptions
        ),
        add_drop_date=term.add_drop_date,
        withdrawal_date=term.withdrawal_date,
        finals_start=term.finals_start,
        finals_end=term.finals_end,
        final_exam_matrix=tuple(
            FinalExamMatrixEntry(
                meeting_pattern=row.meeting_pattern,
                exam_start_at=row.exam_start_at,
                exam_end_at=row.exam_end_at,
            )
            for row in matrix
        ),
    )


def record_extraction_run(
    db: Session,
    *,
    document: SyllabusDocument,
    model: str,
    prompt_version: str,
    pass_name: str,
    status: str,
    cost_cents: float | None = None,
    latency_ms: int | None = None,
    raw_output: dict | None = None,
) -> ExtractionRun:
    """Append one row per model call.

    `prompt_version` on every row is what lets a production correction rate be
    attributed back to a specific prompt (section 16). Raw output belongs in R2
    keyed by this row's id rather than inline (18.3 — Neon's free tier is 0.5 GB
    and this column would dominate it); `raw_output` here is the local-dev path.
    """
    run = ExtractionRun(
        document_id=document.id,
        model=model,
        prompt_version=prompt_version,
        pass_=pass_name,
        status=status,
        cost_cents=cost_cents,
        latency_ms=latency_ms,
        raw_output=raw_output,
    )
    db.add(run)
    db.flush()
    return run


def persist_extraction(
    db: Session,
    *,
    document: SyllabusDocument,
    section: Section,
    extraction,
    resolutions: dict[str, ResolvedDate],
    confidences: dict[str, float],
    effort_hours: dict[str, float] | None = None,
) -> list[Assessment]:
    """Replace this document's extracted rows with a fresh parse.

    Deletes and reinserts rather than merging: extraction output is derived data
    with no independent identity, and a partial merge would leave stale items
    from a previous prompt version silently mixed into a new parse. Personal
    edits live in `user_overrides` and are untouched by this.
    """
    _clear_previous_rows(db, document)

    for category in extraction.grade_breakdown:
        db.add(
            GradeCategory(
                document_id=document.id,
                section_id=section.id,
                name=category.category,
                weight_pct=category.weight_pct,
                item_count=category.item_count,
                drop_lowest=category.drop_lowest,
            )
        )

    written: list[Assessment] = []
    for item in extraction.assessments:
        resolution = resolutions.get(item.title)
        due_at, precision, time_inferred = _due_columns(resolution)

        assessment = Assessment(
            document_id=document.id,
            section_id=section.id,
            title=item.title,
            type=item.type,
            category_ref=item.category_ref,
            weight_pct=item.weight_pct,
            date_expression_raw=item.date_expression_raw,
            due_at=due_at,
            due_precision=precision,
            time_inferred=time_inferred,
            recurrence_rule=item.recurrence_raw,
            is_group=item.is_group,
            effort_hours=(effort_hours or {}).get(item.title),
            confidence=confidences.get(item.title, item.confidence),
            page_ref=item.page_ref,
            source_span=item.source_span,
        )
        db.add(assessment)
        written.append(assessment)

    _persist_policies(db, section=section, policies=extraction.policies)
    db.flush()
    logger.info(
        "Persisted %d assessment(s) and %d grade category(ies) for document %s",
        len(written),
        len(extraction.grade_breakdown),
        document.id,
    )
    return written


def _due_columns(resolution: ResolvedDate | None) -> tuple[datetime | None, str, bool]:
    """Map a ResolvedDate onto the three DB columns, preserving 9.3's invariants.

    A `week_only` item stores the band's start so the timeline can place it, but
    keeps `due_precision = week_only` so every renderer knows it is a band and
    must not present it as a specific day.
    """
    if resolution is None:
        return None, "tbd", False
    if resolution.due_precision == "week_only":
        assert resolution.week_start is not None
        from datetime import time

        return (
            datetime.combine(resolution.week_start, time(0, 0)),
            "week_only",
            True,
        )
    return resolution.due_at, resolution.due_precision, resolution.time_inferred


def _persist_policies(db: Session, *, section: Section, policies) -> None:
    row = db.scalar(select(SectionPolicies).where(SectionPolicies.section_id == section.id))
    if row is None:
        row = SectionPolicies(section_id=section.id)
        db.add(row)

    row.attendance_graded = policies.attendance_graded
    row.attendance_weight_pct = policies.attendance_weight_pct
    row.late_policy_class = policies.late_policy_class
    row.group_work_present = policies.group_work_present
    row.group_work_weight_pct = policies.group_work_weight_pct
    row.final_exam_type = policies.final_exam_type
    row.curve_mentioned = policies.curve_mentioned
    row.participation_weight_pct = policies.participation_weight_pct
    row.ai_policy_class = policies.ai_policy_class
    row.materials = [m.model_dump() for m in policies.required_materials]
    row.est_materials_cost_usd = (
        sum(m.est_cost_usd or 0 for m in policies.required_materials) or None
    )
    row.raw = policies.model_dump(mode="json")


def _clear_previous_rows(db: Session, document: SyllabusDocument) -> None:
    """Remove a prior parse of this document.

    Assessments are referenced by `user_overrides`, `corrections_log`, and
    `calendar_event_map`. Those rows carry the student's own edits and history,
    so a re-parse must not cascade into them — which is why re-parsing a
    document that already has verified, overridden items is refused here rather
    than silently destroying that work.
    """
    from db.models import CalendarEventMap, CorrectionLog, UserOverride

    existing = db.scalars(select(Assessment).where(Assessment.document_id == document.id)).all()
    if not existing:
        return

    ids = [a.id for a in existing]
    referenced = db.scalar(select(UserOverride).where(UserOverride.assessment_id.in_(ids)).limit(1))
    if referenced is not None:
        raise ValueError(
            f"Document {document.id} has user overrides; re-parsing would discard them. "
            "Create a new document row instead of re-parsing this one."
        )

    db.query(CalendarEventMap).filter(CalendarEventMap.assessment_id.in_(ids)).delete(
        synchronize_session=False
    )
    # corrections_log is append-only and never garbage-collected (section 12), so
    # its assessment_id is detached rather than deleted.
    db.query(CorrectionLog).filter(CorrectionLog.assessment_id.in_(ids)).update(
        {CorrectionLog.assessment_id: None}, synchronize_session=False
    )
    db.query(Assessment).filter(Assessment.id.in_(ids)).delete(synchronize_session=False)
    db.query(GradeCategory).filter(GradeCategory.document_id == document.id).delete(
        synchronize_session=False
    )
    db.flush()
