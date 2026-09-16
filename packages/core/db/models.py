"""SQLAlchemy models mirroring PRD section 12.

The DDL in the PRD is deliberately abbreviated ("omit obvious timestamps and
indexes"). This file fills those back in via `TimestampMixin` and adds a
handful of indexes on foreign keys and lookup columns that the PRD's prose
implies (content-hash dedup on `sha256`, section-scoped queries on
`assessments`, etc). It does not add columns beyond what section 12 and the
surrounding sections (7-14) require — if a column is here, trace it back to
a section before assuming it's needed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- Institution / term / calendar (section 9.1, 12) --------------------------------------


class Institution(TimestampMixin, Base):
    __tablename__ = "institutions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="America/New_York")
    lms_type: Mapped[str | None] = mapped_column(String)
    default_due_time: Mapped[str] = mapped_column(String, nullable=False, default="23:59")

    terms: Mapped[list[Term]] = relationship(back_populates="institution")
    courses: Mapped[list[Course]] = relationship(back_populates="institution")
    instructors: Mapped[list[Instructor]] = relationship(back_populates="institution")


class Term(TimestampMixin, Base):
    __tablename__ = "terms"

    id: Mapped[uuid.UUID] = _uuid_pk()
    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    add_drop_date: Mapped[date | None] = mapped_column(Date)
    withdrawal_date: Mapped[date | None] = mapped_column(Date)
    finals_start: Mapped[date | None] = mapped_column(Date)
    finals_end: Mapped[date | None] = mapped_column(Date)

    institution: Mapped[Institution] = relationship(back_populates="terms")
    calendar_exceptions: Mapped[list[CalendarException]] = relationship(back_populates="term")
    final_exam_matrix: Mapped[list[FinalExamMatrixRow]] = relationship(back_populates="term")


class CalendarException(Base):
    __tablename__ = "calendar_exceptions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id"), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    no_class: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    term: Mapped[Term] = relationship(back_populates="calendar_exceptions")


class FinalExamMatrixRow(Base):
    """Section 9.5 — resolves finals from the registrar matrix, not the syllabus."""

    __tablename__ = "final_exam_matrix"

    id: Mapped[uuid.UUID] = _uuid_pk()
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id"), nullable=False, index=True)
    meeting_pattern: Mapped[str] = mapped_column(String, nullable=False)
    exam_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exam_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    term: Mapped[Term] = relationship(back_populates="final_exam_matrix")


# --- Catalog: instructors / courses / sections --------------------------------------------


class Instructor(TimestampMixin, Base):
    __tablename__ = "instructors"

    id: Mapped[uuid.UUID] = _uuid_pk()
    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String)
    canonical_name: Mapped[str | None] = mapped_column(String, index=True)

    institution: Mapped[Institution] = relationship(back_populates="instructors")


class Course(TimestampMixin, Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = _uuid_pk()
    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), nullable=False, index=True
    )
    subject_code: Mapped[str] = mapped_column(String, nullable=False)
    catalog_number: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    credits: Mapped[float] = mapped_column(Float, nullable=False)
    gened_attributes: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)

    __table_args__ = (
        UniqueConstraint("institution_id", "subject_code", "catalog_number", name="uq_course_code"),
    )

    institution: Mapped[Institution] = relationship(back_populates="courses")
    sections: Mapped[list[Section]] = relationship(back_populates="course")


class Section(TimestampMixin, Base):
    __tablename__ = "sections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id"), nullable=False, index=True
    )
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id"), nullable=False, index=True)
    section_code: Mapped[str] = mapped_column(String, nullable=False)
    instructor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("instructors.id"))
    meeting_pattern: Mapped[str | None] = mapped_column(String)
    modality: Mapped[str | None] = mapped_column(String)
    seats_total: Mapped[int | None] = mapped_column(Integer)
    seats_open: Mapped[int | None] = mapped_column(Integer)

    course: Mapped[Course] = relationship(back_populates="sections")
    instructor: Mapped[Instructor | None] = relationship()
    documents: Mapped[list[SyllabusDocument]] = relationship(back_populates="section")
    assessments: Mapped[list[Assessment]] = relationship(back_populates="section")
    policies: Mapped[SectionPolicies | None] = relationship(back_populates="section", uselist=False)


# --- Users / enrollments -------------------------------------------------------------------


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), nullable=False, index=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calibration_factor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class Enrollment(TimestampMixin, Base):
    __tablename__ = "enrollments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sections.id"), nullable=False, index=True
    )
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id"), nullable=False, index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (UniqueConstraint("user_id", "section_id", name="uq_enrollment"),)


# --- Documents / extraction (section 7, 8) --------------------------------------------------


class SyllabusDocument(TimestampMixin, Base):
    __tablename__ = "syllabus_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    uploader_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sections.id"), index=True)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False, index=True)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String, nullable=False, default="upload")
    is_scanned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visibility: Mapped[str] = mapped_column(String, nullable=False, default="private")
    canonical_for_section: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    section: Mapped[Section | None] = relationship(back_populates="documents")
    extraction_runs: Mapped[list[ExtractionRun]] = relationship(back_populates="document")
    assessments: Mapped[list[Assessment]] = relationship(back_populates="document")
    grade_categories: Mapped[list[GradeCategory]] = relationship(back_populates="document")


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("syllabus_documents.id"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    pass_: Mapped[str] = mapped_column("pass", String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    cost_cents: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Note (18.3): the free Neon tier caps at 0.5 GB. Raw model output belongs in
    # R2 keyed by this row's id, not inline here — this column is the local-dev
    # convenience path; production code should write a pointer instead.
    raw_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[SyllabusDocument] = relationship(back_populates="extraction_runs")


# --- Assessments / grade categories / policies (section 8, 9, 12) --------------------------


class Assessment(TimestampMixin, Base):
    __tablename__ = "assessments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("syllabus_documents.id"), nullable=False, index=True
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sections.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    category_ref: Mapped[str | None] = mapped_column(String)
    weight_pct: Mapped[float | None] = mapped_column(Float)
    date_expression_raw: Mapped[str | None] = mapped_column(String)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_precision: Mapped[str] = mapped_column(String, nullable=False, default="tbd")
    time_inferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recurrence_rule: Mapped[str | None] = mapped_column(String)
    is_group: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effort_hours: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    page_ref: Mapped[int | None] = mapped_column(Integer)
    source_span: Mapped[str] = mapped_column(Text, nullable=False)
    verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[SyllabusDocument] = relationship(back_populates="assessments")
    section: Mapped[Section] = relationship(back_populates="assessments")
    overrides: Mapped[list[UserOverride]] = relationship(back_populates="assessment")


class GradeCategory(Base):
    __tablename__ = "grade_categories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("syllabus_documents.id"), nullable=False, index=True
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sections.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    weight_pct: Mapped[float] = mapped_column(Float, nullable=False)
    item_count: Mapped[int | None] = mapped_column(Integer)
    drop_lowest: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    document: Mapped[SyllabusDocument] = relationship(back_populates="grade_categories")


class SectionPolicies(Base):
    __tablename__ = "section_policies"

    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id"), primary_key=True)
    attendance_graded: Mapped[bool | None] = mapped_column(Boolean)
    attendance_weight_pct: Mapped[float | None] = mapped_column(Float)
    late_policy_class: Mapped[str] = mapped_column(String, nullable=False, default="unspecified")
    group_work_present: Mapped[bool | None] = mapped_column(Boolean)
    group_work_weight_pct: Mapped[float | None] = mapped_column(Float)
    final_exam_type: Mapped[str] = mapped_column(String, nullable=False, default="unspecified")
    curve_mentioned: Mapped[bool | None] = mapped_column(Boolean)
    participation_weight_pct: Mapped[float | None] = mapped_column(Float)
    ai_policy_class: Mapped[str] = mapped_column(String, nullable=False, default="unspecified")
    materials: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    est_materials_cost_usd: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    section: Mapped[Section] = relationship(back_populates="policies")


# --- Overrides / corrections (section 10, 12) -----------------------------------------------


class UserOverride(TimestampMixin, Base):
    """Kept separate from `assessments` so a section-level re-parse never clobbers a user's edit."""

    __tablename__ = "user_overrides"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id"), nullable=False, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effort_hours: Mapped[float | None] = mapped_column(Float)
    dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    title: Mapped[str | None] = mapped_column(String)

    __table_args__ = (UniqueConstraint("user_id", "assessment_id", name="uq_user_override"),)

    assessment: Mapped[Assessment] = relationship(back_populates="overrides")


class CorrectionLog(Base):
    """Append-only. Never garbage-collect (section 12) — this is the eval/improvement dataset."""

    __tablename__ = "corrections_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Both foreign keys are nullable, for two different reasons that land in the
    # same place: this row must outlive whatever it points at.
    #
    # `user_id` is cleared on account deletion — section 15.2 says retain only
    # *anonymized* corrections, and the row's eval value (field, old/new value,
    # source span, prompt version) identifies nobody.
    #
    # `assessment_id` is cleared on re-parse — assessments are derived data,
    # replaced wholesale whenever a document is parsed again.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessments.id"), nullable=True, index=True
    )
    field: Mapped[str] = mapped_column(String, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source_span: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Calendar sync (section 13) --------------------------------------------------------------


class CalendarConnection(TimestampMixin, Base):
    __tablename__ = "calendar_connections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    target_calendar_id: Mapped[str | None] = mapped_column(String)
    sync_token: Mapped[str | None] = mapped_column(String)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarEventMap(Base):
    __tablename__ = "calendar_event_map"

    id: Mapped[uuid.UUID] = _uuid_pk()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calendar_connections.id"), nullable=False, index=True
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessments.id"), nullable=False, index=True
    )
    external_event_id: Mapped[str] = mapped_column(String, nullable=False)
    etag: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        UniqueConstraint("connection_id", "assessment_id", name="uq_calendar_event_map"),
    )


# --- Layer 2: corpus (section 14) -------------------------------------------------------------


class CourseProfile(Base):
    __tablename__ = "course_profiles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id"), nullable=False, index=True
    )
    instructor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instructors.id"), nullable=False, index=True
    )
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terms.id"), nullable=False, index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    assessment_mix: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    est_weekly_hours: Mapped[float | None] = mapped_column(Float)
    has_group_project: Mapped[bool | None] = mapped_column(Boolean)
    attendance_graded: Mapped[bool | None] = mapped_column(Boolean)
    exam_count: Mapped[int | None] = mapped_column(Integer)
    final_exam_type: Mapped[str | None] = mapped_column(String)
    graded_item_count: Mapped[int | None] = mapped_column(Integer)
    est_materials_cost_usd: Mapped[float | None] = mapped_column(Float)
    source_document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list
    )
    verification_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("course_id", "instructor_id", "term_id", name="uq_course_profile"),
    )
