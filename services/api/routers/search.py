"""Faceted course search — PRD sections 14.3, 6.4 (US-8, US-9).

The acceptance test the PRD names explicitly:

    "3-credit humanities elective, no group project, no attendance policy,
     papers instead of exams"

That query is answerable entirely from this facet set, and `test_search.py`
verifies it end to end before P2 can be called done.

Two display rules that are not incidental:

- **Provenance is always shown.** Every profile says which term's syllabus it
  came from, which instructor taught it, and how recent that is. A three-year-old
  profile is still useful — but only if it is labeled as three years old.
- **Courses with no profile still appear**, with an `unknown` profile and a
  prompt to upload. The PRD is explicit: do not hide them, the empty state is
  the acquisition surface.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db.models import Course, CourseProfile, Instructor, Section, Term, User
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from services.api.deps import current_user, get_db

router = APIRouter(tags=["search"])


class ProfileOut(BaseModel):
    """One course + instructor + term profile. Never averaged across instructors (14.1)."""

    course_id: str
    subject_code: str
    catalog_number: str
    title: str
    credits: float
    gened_attributes: list[str]

    #: `None` throughout when `known` is False — an unprofiled course still
    #: appears in results (US-8), it just has nothing to say yet.
    known: bool
    instructor: str | None = None
    term: str | None = None
    term_start: str | None = None
    provenance: str
    est_weekly_hours: float | None = None
    assessment_mix: str | None = None
    has_group_project: bool | None = None
    attendance_graded: bool | None = None
    final_exam_type: str | None = None
    graded_item_count: int | None = None
    est_materials_cost_usd: float | None = None
    verification_count: int = 0
    meeting_pattern: str | None = None
    seats_open: int | None = None


class SearchResults(BaseModel):
    total: int
    offset: int
    limit: int
    results: list[ProfileOut]


def _provenance(term: Term | None, instructor: Instructor | None) -> str:
    """The label 14.2 requires on every profile."""
    if term is None:
        return "No verified syllabus yet — upload one to build this profile."

    who = instructor.name if instructor else "an unlisted instructor"
    age_years = (datetime.now(UTC).date() - term.start_date).days // 365
    if age_years >= 1:
        suffix = f" ({age_years} year{'s' if age_years > 1 else ''} ago)"
    else:
        suffix = ""
    return f"Based on the {term.name} syllabus, taught by {who}{suffix}."


@router.get("/courses/search", response_model=SearchResults)
def search_courses(
    q: str = Query(default="", description="Course code or title substring"),
    credits: float | None = Query(default=None),
    max_weekly_hours: float | None = Query(default=None),
    assessment_mix: str | None = Query(
        default=None, description="exam_heavy | paper_heavy | project_heavy | continuous"
    ),
    has_group_project: bool | None = Query(default=None),
    attendance_graded: bool | None = Query(default=None),
    final_exam_type: str | None = Query(default=None),
    max_graded_items: int | None = Query(default=None),
    max_materials_cost: float | None = Query(default=None),
    gened_attribute: str | None = Query(default=None),
    include_unprofiled: bool = Query(
        default=True,
        description="US-8: unprofiled courses stay in results as an acquisition surface",
    ),
    limit: int = Query(default=25, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SearchResults:
    """US-8: filter courses by how they are actually run."""
    #: The most recent profile per (course, instructor). An older term's profile
    #: is still shown when it is the only one — recency is disclosed, not required.
    latest_term = (
        select(
            CourseProfile.course_id.label("course_id"),
            CourseProfile.instructor_id.label("instructor_id"),
            func.max(Term.start_date).label("newest"),
        )
        .join(Term, CourseProfile.term_id == Term.id)
        .group_by(CourseProfile.course_id, CourseProfile.instructor_id)
        .subquery()
    )

    query = (
        select(CourseProfile, Course, Instructor, Term)
        .join(Course, CourseProfile.course_id == Course.id)
        .join(Term, CourseProfile.term_id == Term.id)
        .outerjoin(Instructor, CourseProfile.instructor_id == Instructor.id)
        .join(
            latest_term,
            (latest_term.c.course_id == CourseProfile.course_id)
            & (latest_term.c.instructor_id == CourseProfile.instructor_id)
            & (latest_term.c.newest == Term.start_date),
        )
        .where(Course.institution_id == user.institution_id)
    )

    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                Course.title.ilike(pattern),
                Course.subject_code.ilike(pattern),
                Course.catalog_number.ilike(pattern),
            )
        )
    if credits is not None:
        query = query.where(Course.credits == credits)
    if gened_attribute:
        # `contains` rather than `any`: both compile correctly, but this one
        # emits `gened_attributes @> ARRAY[...]`, which a GIN index can serve,
        # and SQLAlchemy's ORM typing resolves `.any()` on a Mapped[list[str]]
        # to the relationship comparator rather than the array one.
        query = query.where(Course.gened_attributes.contains([gened_attribute]))
    if max_weekly_hours is not None:
        query = query.where(CourseProfile.est_weekly_hours <= max_weekly_hours)
    if has_group_project is not None:
        query = query.where(CourseProfile.has_group_project.is_(has_group_project))
    if attendance_graded is not None:
        query = query.where(CourseProfile.attendance_graded.is_(attendance_graded))
    if final_exam_type:
        query = query.where(CourseProfile.final_exam_type == final_exam_type)
    if max_graded_items is not None:
        query = query.where(CourseProfile.graded_item_count <= max_graded_items)
    if max_materials_cost is not None:
        query = query.where(
            or_(
                CourseProfile.est_materials_cost_usd.is_(None),
                CourseProfile.est_materials_cost_usd <= max_materials_cost,
            )
        )
    if assessment_mix:
        query = query.where(CourseProfile.assessment_mix["label"].astext == assessment_mix)

    rows = db.execute(query).all()
    results = [
        _to_profile_out(profile, course, instructor, term)
        for profile, course, instructor, term in rows
    ]

    # US-8: courses with no verified syllabus appear with an "unknown" profile.
    # They are appended rather than interleaved, because a result with no data
    # cannot be ranked against one that has data — but hiding them would remove
    # the prompt that fills the corpus in the first place.
    if include_unprofiled and not _has_profile_only_filters(
        max_weekly_hours,
        assessment_mix,
        has_group_project,
        attendance_graded,
        final_exam_type,
        max_graded_items,
        max_materials_cost,
    ):
        results.extend(_unprofiled(db, user, q, credits, gened_attribute))

    total = len(results)
    return SearchResults(
        total=total, offset=offset, limit=limit, results=results[offset : offset + limit]
    )


def _has_profile_only_filters(*values) -> bool:
    """Did the student filter on something an unprofiled course cannot answer?

    If so, including unprofiled courses would be actively misleading: a course
    with no data has not been shown to have no group project, and returning it
    for `has_group_project=false` would be a claim the corpus cannot support.
    """
    return any(v is not None for v in values)


def _unprofiled(
    db: Session,
    user: User,
    q: str,
    credits: float | None,
    gened_attribute: str | None,
) -> list[ProfileOut]:
    profiled = select(CourseProfile.course_id).scalar_subquery()
    query = select(Course).where(
        Course.institution_id == user.institution_id, Course.id.not_in(profiled)
    )
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                Course.title.ilike(pattern),
                Course.subject_code.ilike(pattern),
                Course.catalog_number.ilike(pattern),
            )
        )
    if credits is not None:
        query = query.where(Course.credits == credits)
    if gened_attribute:
        # `contains` rather than `any`: both compile correctly, but this one
        # emits `gened_attributes @> ARRAY[...]`, which a GIN index can serve,
        # and SQLAlchemy's ORM typing resolves `.any()` on a Mapped[list[str]]
        # to the relationship comparator rather than the array one.
        query = query.where(Course.gened_attributes.contains([gened_attribute]))

    return [
        ProfileOut(
            course_id=str(course.id),
            subject_code=course.subject_code,
            catalog_number=course.catalog_number,
            title=course.title,
            credits=course.credits,
            gened_attributes=list(course.gened_attributes or []),
            known=False,
            provenance=_provenance(None, None),
        )
        for course in db.scalars(query).all()
    ]


def _to_profile_out(
    profile: CourseProfile, course: Course, instructor: Instructor | None, term: Term
) -> ProfileOut:
    mix = profile.assessment_mix or {}
    return ProfileOut(
        course_id=str(course.id),
        subject_code=course.subject_code,
        catalog_number=course.catalog_number,
        title=course.title,
        credits=course.credits,
        gened_attributes=list(course.gened_attributes or []),
        known=True,
        instructor=instructor.name if instructor else None,
        term=term.name,
        term_start=term.start_date.isoformat(),
        provenance=_provenance(term, instructor),
        est_weekly_hours=profile.est_weekly_hours,
        assessment_mix=mix.get("label") if isinstance(mix, dict) else None,
        has_group_project=profile.has_group_project,
        attendance_graded=profile.attendance_graded,
        final_exam_type=profile.final_exam_type,
        graded_item_count=profile.graded_item_count,
        est_materials_cost_usd=profile.est_materials_cost_usd,
        verification_count=profile.verification_count,
    )


@router.get("/courses/{course_id}/profiles", response_model=list[ProfileOut])
def course_profiles(
    course_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[ProfileOut]:
    """US-9: compare instructors teaching the same course.

    Most recent first (14.1), one row per instructor per term, never averaged —
    instructor variance is the entire point of the feature.
    """
    course = db.scalar(
        select(Course).where(Course.id == course_id, Course.institution_id == user.institution_id)
    )
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")

    rows = db.execute(
        select(CourseProfile, Instructor, Term)
        .join(Term, CourseProfile.term_id == Term.id)
        .outerjoin(Instructor, CourseProfile.instructor_id == Instructor.id)
        .where(CourseProfile.course_id == course_id)
        .order_by(Term.start_date.desc())
    ).all()

    if not rows:
        return [
            ProfileOut(
                course_id=str(course.id),
                subject_code=course.subject_code,
                catalog_number=course.catalog_number,
                title=course.title,
                credits=course.credits,
                gened_attributes=list(course.gened_attributes or []),
                known=False,
                provenance=_provenance(None, None),
            )
        ]

    out = []
    for profile, instructor, term in rows:
        entry = _to_profile_out(profile, course, instructor, term)
        section = db.scalar(
            select(Section).where(
                Section.course_id == course_id,
                Section.term_id == term.id,
                Section.instructor_id == profile.instructor_id,
            )
        )
        if section is not None:
            entry.meeting_pattern = section.meeting_pattern
            entry.seats_open = section.seats_open
        out.append(entry)
    return out
