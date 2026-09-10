"""Course catalog, manual entry, and the Layer 2 surface — PRD sections 23, 14.

The search and profile endpoints are P2 and intentionally minimal here: the
corpus does not exist until Layer 1 has run for a term (section 1 is emphatic
that building Layer 2 first is the failure mode). What is implemented now is
the catalog lookup Layer 1 needs, manual course entry (US-2), and the
instructor opt-out that section 15.1 requires to exist before the public layer
goes live.
"""

from __future__ import annotations

import uuid

from db.models import Course, Enrollment, Section, Term, User
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from services.api.deps import current_user, get_db

router = APIRouter(tags=["courses"])


class CourseOut(BaseModel):
    id: str
    subject_code: str
    catalog_number: str
    title: str
    credits: float
    gened_attributes: list[str]


class ManualCourseIn(BaseModel):
    subject_code: str = Field(..., max_length=8)
    catalog_number: str = Field(..., max_length=8)
    title: str
    credits: float = Field(..., gt=0, le=12)
    term_id: uuid.UUID
    section_code: str = "001"
    meeting_pattern: str | None = None


@router.get("/courses", response_model=list[CourseOut])
def search_catalog(
    q: str = Query(default="", description="Substring of code or title"),
    limit: int = Query(default=25, le=100),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[CourseOut]:
    """Catalog lookup for the course-match dropdown in US-1."""
    query = select(Course).where(Course.institution_id == user.institution_id)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                Course.title.ilike(pattern),
                Course.subject_code.ilike(pattern),
                Course.catalog_number.ilike(pattern),
            )
        )
    courses = db.scalars(query.limit(limit)).all()
    return [
        CourseOut(
            id=str(c.id),
            subject_code=c.subject_code,
            catalog_number=c.catalog_number,
            title=c.title,
            credits=c.credits,
            gened_attributes=list(c.gened_attributes or []),
        )
        for c in courses
    ]


@router.post("/courses/manual", status_code=status.HTTP_201_CREATED)
def create_manual_course(
    payload: ManualCourseIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """US-2: a student whose professor never posted a syllabus still gets a timeline.

    Manually entered courses are excluded from the Layer 2 corpus (US-2) — they
    carry no verified document, so `canonical_for_section` never points at them.
    """
    term = db.scalar(select(Term).where(Term.id == payload.term_id))
    if term is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Term not found")

    course = db.scalar(
        select(Course).where(
            Course.institution_id == user.institution_id,
            Course.subject_code == payload.subject_code.upper(),
            Course.catalog_number == payload.catalog_number,
        )
    )
    if course is None:
        course = Course(
            institution_id=user.institution_id,
            subject_code=payload.subject_code.upper(),
            catalog_number=payload.catalog_number,
            title=payload.title,
            credits=payload.credits,
            gened_attributes=[],
        )
        db.add(course)
        db.flush()

    section = db.scalar(
        select(Section).where(
            Section.course_id == course.id,
            Section.term_id == term.id,
            Section.section_code == payload.section_code,
        )
    )
    if section is None:
        section = Section(
            course_id=course.id,
            term_id=term.id,
            section_code=payload.section_code,
            meeting_pattern=payload.meeting_pattern,
        )
        db.add(section)
        db.flush()

    existing = db.scalar(
        select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.section_id == section.id)
    )
    if existing is None:
        db.add(Enrollment(user_id=user.id, section_id=section.id, term_id=term.id, confirmed=True))
    db.commit()

    return {"course_id": str(course.id), "section_id": str(section.id)}


@router.post("/courses/{course_id}/opt-out", status_code=status.HTTP_200_OK)
def instructor_opt_out(course_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """15.1: one click from any profile page, honored within 24 hours, no argument.

    Appendix C requires this flow to exist and work even though the public layer
    is not live yet — which is exactly why it is here before the search endpoints.
    """
    course = db.scalar(select(Course).where(Course.id == course_id))
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")

    # Marks every section's documents non-public. The extraction stays for the
    # uploader's own timeline; only the corpus contribution is withdrawn.
    sections = db.scalars(select(Section).where(Section.course_id == course.id)).all()
    affected = 0
    for section in sections:
        for document in section.documents:
            document.visibility = "opted_out"
            document.canonical_for_section = False
            affected += 1
    db.commit()
    return {"status": "opted_out", "documents_withdrawn": affected}
