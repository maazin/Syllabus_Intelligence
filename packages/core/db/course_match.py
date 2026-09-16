"""Match an extracted syllabus to a registrar section (PRD section 6.1, US-1).

US-1: "the system matches the syllabus to a course; match confidence below
0.8 prompts the student to confirm from a dropdown." A student uploads a file,
not a section id, so the section has to come from the document itself, and
it has to come *after* extraction, because the subject code and catalog
number are in the text.

Shared by the worker, which matches after extracting, and the API, which
lists candidates when the worker could not decide. Pure over ORM rows, so it
is testable without a queue.

Getting this wrong is worse than not deciding: a document matched to the
wrong section resolves "the final exam" through the wrong slot in the exam
matrix and puts a real-looking wrong date on a student's calendar. So the
thresholds below lean toward asking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from db.models import Course, Instructor, Section, Term

#: US-1's line. At or above this the match is applied without asking.
CONFIRM_THRESHOLD = 0.8


@dataclass(frozen=True)
class MatchResult:
    section: Section | None
    confidence: float
    candidates: list[Section] = field(default_factory=list)

    @property
    def decided(self) -> bool:
        return self.section is not None and self.confidence >= CONFIRM_THRESHOLD


def current_term(db: Session, institution_id: object) -> Term | None:
    """The term in progress, or the next to start.

    Between terms there is no current term, and the week before classes is the
    product's busiest moment, so the next term is the right answer then.
    """
    today = datetime.now(UTC).date()
    terms = db.scalars(
        select(Term).where(Term.institution_id == institution_id).order_by(Term.start_date.asc())
    ).all()
    for term in terms:
        last_day = term.finals_end or term.end_date
        if term.start_date <= today <= last_day:
            return term
    upcoming = [t for t in terms if t.start_date > today]
    if upcoming:
        return upcoming[0]
    return terms[-1] if terms else None


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _last_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\b(dr|prof|professor|mr|ms|mrs|phd)\b\.?", "", name, flags=re.I)
    parts = [p for p in re.split(r"[\s,]+", cleaned) if p]
    return _norm(parts[-1]) if parts else ""


def sections_in_term(db: Session, term: Term) -> list[Section]:
    return list(
        db.scalars(
            select(Section)
            .where(Section.term_id == term.id)
            .options(selectinload(Section.course), selectinload(Section.instructor))
            .order_by(Section.section_code)
        ).all()
    )


def match_section(
    db: Session,
    *,
    term: Term,
    subject_code: str | None,
    catalog_number: str | None,
    section_code: str | None = None,
    instructor_name: str | None = None,
) -> MatchResult:
    """Score every section in the term against what the syllabus said.

    Subject and catalog number are the identity; without both there is no
    match, only a list to choose from. Section code and instructor surname
    break ties between sections of one course. Confidence is discrete on
    purpose: it is a statement of what evidence agreed, not a probability, and
    presenting it as one would overstate it.
    """
    everything = sections_in_term(db, term)
    if not subject_code or not catalog_number:
        return MatchResult(None, 0.0, everything)

    same_course = [
        s
        for s in everything
        if _norm(s.course.subject_code) == _norm(subject_code)
        and _norm(s.course.catalog_number) == _norm(catalog_number)
    ]
    if not same_course:
        return MatchResult(None, 0.0, everything)

    if len(same_course) == 1:
        # One section of this course this term. The code and instructor can
        # only agree or be absent; a disagreement is worth surfacing.
        only = same_course[0]
        if section_code and _norm(only.section_code) != _norm(section_code):
            return MatchResult(only, 0.6, same_course)
        return MatchResult(only, 0.9, same_course)

    by_code = [
        s for s in same_course if section_code and _norm(s.section_code) == _norm(section_code)
    ]
    if len(by_code) == 1:
        return MatchResult(by_code[0], 0.95, same_course)

    surname = _last_name(instructor_name)
    by_instructor = [
        s
        for s in same_course
        if surname and s.instructor is not None and _last_name(s.instructor.name) == surname
    ]
    if len(by_instructor) == 1:
        return MatchResult(by_instructor[0], 0.85, same_course)

    # Several sections, nothing to pick between them. Ask.
    return MatchResult(None, 0.5, same_course)


def describe(section: Section) -> dict[str, object]:
    """The shape the confirmation dropdown renders."""
    instructor: Instructor | None = section.instructor
    course: Course = section.course
    return {
        "section_id": str(section.id),
        "label": f"{course.subject_code} {course.catalog_number} section {section.section_code}",
        "title": course.title,
        "instructor": instructor.name if instructor else None,
        "meeting_pattern": section.meeting_pattern,
    }
