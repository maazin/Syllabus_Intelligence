#!/usr/bin/env python3
"""Load the section 26 fixtures into the local database.

    python scripts/seed_dev_data.py

Seeds the institution, the Fall 2026 academic calendar, the final exam matrix,
and a small course catalog. Section 9.1 is explicit that the academic calendar
is a hard data dependency to be seeded before any resolver code runs — it is
~20 rows and it unblocks everything.

Idempotent: re-running updates in place rather than duplicating, so it is safe
to run after every `alembic upgrade head`.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))

from db.models import (  # noqa: E402
    CalendarException,
    Course,
    FinalExamMatrixRow,
    Institution,
    Instructor,
    Section,
    Term,
)
from db.session import SessionLocal, engine  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

COURSE_CATALOG = [
    ("COP", "4530", "Data Structures, Algorithms and Generic Programming", 3, []),
    ("COP", "3502", "Programming Fundamentals I", 3, []),
    ("MAC", "2312", "Calculus with Analytic Geometry II", 4, ["QUANT"]),
    ("PHY", "2048", "General Physics A", 4, ["PHYS"]),
    ("ENG", "3014", "Modern American Literature", 3, ["HUM", "WRIT"]),
    ("HIS", "2020", "American History Since 1877", 3, ["HIST", "DIV"]),
    ("PSY", "2012", "General Psychology", 3, ["SOC"]),
    ("CHM", "2045", "General Chemistry I", 3, ["PHYS"]),
    ("STA", "2023", "Introduction to Statistics", 3, ["QUANT"]),
    ("PHI", "2010", "Introduction to Philosophy", 3, ["HUM"]),
]

INSTRUCTORS = [
    ("Dr. Alice Nakamura", "a.nakamura@example.edu"),
    ("Prof. Daniel Whitfield", "d.whitfield@example.edu"),
    ("Dr. Maria Okonkwo", "m.okonkwo@example.edu"),
    ("Dr. Samuel Reyes", "s.reyes@example.edu"),
]

#: (subject, catalog, section_code, instructor index, meeting pattern, modality)
SECTIONS = [
    ("COP", "4530", "003", 0, "MWF 10:00-10:50", "in_person"),
    ("COP", "3502", "001", 0, "MWF 09:00-09:50", "in_person"),
    ("MAC", "2312", "012", 2, "MWF 11:00-11:50", "in_person"),
    ("PHY", "2048", "004", 3, "TR 09:30-10:45", "in_person"),
    ("ENG", "3014", "001", 1, "TR 14:00-15:15", "in_person"),
    ("STA", "2023", "007", 2, "TR 15:30-16:45", "hybrid"),
]


def seed(session: Session) -> None:
    calendar_data = json.loads((FIXTURES / "academic_calendar.json").read_text())
    matrix_data = json.loads((FIXTURES / "final_exam_matrix.json").read_text())
    tz = ZoneInfo(calendar_data["timezone"])

    institution = session.scalar(select(Institution).where(Institution.domain == "test.edu"))
    if institution is None:
        institution = Institution(
            name=calendar_data["institution"],
            domain="test.edu",
            timezone=calendar_data["timezone"],
            lms_type="canvas",
            default_due_time=calendar_data["default_due_time"],
        )
        session.add(institution)
        session.flush()
    print(f"  institution   {institution.name}")

    term = session.scalar(
        select(Term).where(
            Term.institution_id == institution.id, Term.name == calendar_data["term_name"]
        )
    )
    if term is None:
        term = Term(institution_id=institution.id, name=calendar_data["term_name"])
        session.add(term)
    term.start_date = date.fromisoformat(calendar_data["start_date"])
    term.end_date = date.fromisoformat(calendar_data["end_date"])
    term.add_drop_date = date.fromisoformat(calendar_data["add_drop_date"])
    term.withdrawal_date = date.fromisoformat(calendar_data["withdrawal_date"])
    term.finals_start = date.fromisoformat(calendar_data["finals_start"])
    term.finals_end = date.fromisoformat(calendar_data["finals_end"])
    session.flush()
    print(f"  term          {term.name}  {term.start_date} .. {term.finals_end}")

    # Calendar exceptions and the exam matrix are replaced wholesale rather than
    # merged: they come from the registrar as a unit, and a stale row that
    # survives a re-seed would silently corrupt every date resolution downstream.
    session.query(CalendarException).filter(CalendarException.term_id == term.id).delete()
    for entry in calendar_data["exceptions"]:
        session.add(
            CalendarException(
                term_id=term.id,
                date=date.fromisoformat(entry["date"]),
                label=entry["label"],
                no_class=entry["no_class"],
            )
        )
    print(f"  no-class days {len(calendar_data['exceptions'])}")

    session.query(FinalExamMatrixRow).filter(FinalExamMatrixRow.term_id == term.id).delete()
    for row in matrix_data["rows"]:
        session.add(
            FinalExamMatrixRow(
                term_id=term.id,
                meeting_pattern=row["meeting_pattern"],
                exam_start_at=datetime.fromisoformat(row["exam_start_at"]).replace(tzinfo=tz),
                exam_end_at=datetime.fromisoformat(row["exam_end_at"]).replace(tzinfo=tz),
            )
        )
    print(f"  exam matrix   {len(matrix_data['rows'])} rows")

    instructors = []
    for name, email in INSTRUCTORS:
        existing = session.scalar(select(Instructor).where(Instructor.email == email))
        if existing is None:
            existing = Instructor(
                institution_id=institution.id,
                name=name,
                email=email,
                canonical_name=name.split()[-1].lower(),
            )
            session.add(existing)
            session.flush()
        instructors.append(existing)
    print(f"  instructors   {len(instructors)}")

    courses: dict[tuple[str, str], Course] = {}
    for subject, catalog, title, credits, attributes in COURSE_CATALOG:
        existing = session.scalar(
            select(Course).where(
                Course.institution_id == institution.id,
                Course.subject_code == subject,
                Course.catalog_number == catalog,
            )
        )
        if existing is None:
            existing = Course(
                institution_id=institution.id,
                subject_code=subject,
                catalog_number=catalog,
                title=title,
                credits=credits,
                gened_attributes=attributes,
            )
            session.add(existing)
            session.flush()
        courses[(subject, catalog)] = existing
    print(f"  courses       {len(courses)}")

    section_count = 0
    for subject, catalog, code, instructor_index, pattern, modality in SECTIONS:
        course = courses[(subject, catalog)]
        existing = session.scalar(
            select(Section).where(
                Section.course_id == course.id,
                Section.term_id == term.id,
                Section.section_code == code,
            )
        )
        if existing is None:
            existing = Section(
                course_id=course.id,
                term_id=term.id,
                section_code=code,
            )
            session.add(existing)
        existing.instructor_id = instructors[instructor_index].id
        existing.meeting_pattern = pattern
        existing.modality = modality
        existing.seats_total = 120
        existing.seats_open = 14
        section_count += 1
    print(f"  sections      {section_count}")


def main() -> None:
    print(f"Seeding {engine.url.render_as_string(hide_password=True)}\n")
    with SessionLocal() as session:
        seed(session)
        session.commit()
    print("\nSeed complete.")


if __name__ == "__main__":
    main()
