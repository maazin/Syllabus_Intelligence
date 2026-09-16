"""The full worker task, end to end — PRD sections 7 through 12.

Exercises the real chain: fetch from object storage, ingest a real PDF, resolve
dates against the seeded registrar calendar, validate, score confidence, and
persist. Only the model call is stubbed, because it is the one step that costs
money and is non-deterministic; everything around it is the part that breaks
silently and so is worth testing for real.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5533/syllint"
)
os.environ.setdefault("R2_ENDPOINT", "http://localhost:9100")

from db.localtime import local_date  # noqa: E402
from db.models import (  # noqa: E402
    Assessment,
    Enrollment,
    ExtractionRun,
    GradeCategory,
    Institution,
    Section,
    SyllabusDocument,
    User,
)
from db.session import SessionLocal, engine  # noqa: E402
from schemas.extraction import ExtractionOutput  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "syllabi" / "cs_lecture_native_pdf.pdf"
RECORDED = ROOT / "tests" / "fixtures" / "recorded_extractions" / "cs_lecture_native_pdf.json"

#: The seeded institution's timezone (scripts/seed_dev_data.py).
TZ = "America/New_York"


def _ready() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except Exception:
        return False
    try:
        from db import objectstore

        objectstore.client().head_bucket(Bucket=objectstore.bucket())
    except Exception:
        return False
    return FIXTURE.is_file() and RECORDED.is_file()


pytestmark = pytest.mark.skipif(
    not _ready(),
    reason="needs Postgres, MinIO, and generated fixtures; run docker compose first",
)


@pytest.fixture
def uploaded():
    """A real file in object storage, with a document row pointing at it."""
    from services.api.storage import store_document

    with SessionLocal() as db:
        institution = db.scalar(select(Institution).where(Institution.domain == "test.edu"))
        if institution is None:
            pytest.skip("run scripts/seed_dev_data.py first")

        # An MWF 10:00 section, so the final exam matrix lookup has a row (9.5).
        section = db.scalar(select(Section).where(Section.meeting_pattern == "MWF 10:00-10:50"))
        if section is None:
            pytest.skip("run scripts/seed_dev_data.py first")

        user = User(email=f"task-{uuid.uuid4().hex[:8]}@test.edu", institution_id=institution.id)
        db.add(user)
        db.flush()

        # Unique bytes so this test never collides with another run's dedup.
        data = FIXTURE.read_bytes() + f"\n% {uuid.uuid4().hex}".encode()
        document, _ = store_document(
            db, user=user, filename=FIXTURE.name, data=data, section_id=section.id
        )
        db.commit()
        ids = {"document_id": document.id, "user_id": user.id, "section_id": section.id}

    yield ids

    with SessionLocal() as db:
        assessment_ids = [
            a.id
            for a in db.scalars(
                select(Assessment).where(Assessment.document_id == ids["document_id"])
            ).all()
        ]
        if assessment_ids:
            db.query(Assessment).filter(Assessment.id.in_(assessment_ids)).delete(
                synchronize_session=False
            )
        db.query(GradeCategory).filter(GradeCategory.document_id == ids["document_id"]).delete()
        db.query(ExtractionRun).filter(ExtractionRun.document_id == ids["document_id"]).delete()
        db.query(SyllabusDocument).filter(SyllabusDocument.id == ids["document_id"]).delete()
        db.query(User).filter(User.id == ids["user_id"]).delete()
        db.commit()


@pytest.fixture
def stub_model(monkeypatch):
    """Replace only the model call, leaving the rest of the chain real."""
    from services.worker.tasks import extract as extract_module
    from services.worker.tasks import parse_document as task_module

    payload = ExtractionOutput.model_validate_json(RECORDED.read_text())

    class _Result:
        output = payload
        flags: list = []
        prompt_version = "v1"
        model = "stub-model"
        escalated = False
        cost_cents = 0.0
        latency_ms = 0
        runs: list = []

    monkeypatch.setattr(task_module, "extract_document", lambda document: _Result())
    monkeypatch.setattr(extract_module, "extract_document", lambda document, **kw: _Result())
    return payload


def test_task_parses_a_real_document_end_to_end(uploaded, stub_model) -> None:
    from services.worker.tasks.parse_document import _run

    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uploaded["document_id"])
        )
        result = _run(db, document)

    assert result["status"] == "parsed"
    assert result["assessments"] == 9

    with SessionLocal() as db:
        stored = db.scalars(
            select(Assessment).where(Assessment.document_id == uploaded["document_id"])
        ).all()
        assert len(stored) == 9

        by_title = {a.title: a for a in stored}

        # Resolution ran against the seeded registrar calendar. Read the day in
        # the institution's timezone, never off the stored UTC instant.
        assert by_title["Midterm Exam 1"].due_at is not None
        assert local_date(by_title["Midterm Exam 1"].due_at, TZ).isoformat() == "2026-10-14"

        # 9.5: the final exam came from the matrix, not the syllabus text, and
        # carries a real stated time rather than an inferred one.
        final = by_title["Final Exam"]
        assert final.due_precision == "exact_datetime"
        assert local_date(final.due_at, TZ).isoformat() == "2026-12-09"
        assert final.time_inferred is False

        # Every item traces to a verbatim quote (§16).
        assert all(a.source_span.strip() for a in stored)

        # Effort was precomputed so the heatmap does not recompute per request.
        assert by_title["Midterm Exam 1"].effort_hours is not None


def test_task_records_page_count_and_scan_status(uploaded, stub_model) -> None:
    from services.worker.tasks.parse_document import _run

    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uploaded["document_id"])
        )
        _run(db, document)

    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uploaded["document_id"])
        )
        assert document.page_count == 2
        assert document.is_scanned is False


def test_task_lowers_confidence_on_the_weekday_mismatch(uploaded, stub_model) -> None:
    """The fixture says "Tuesday, October 14"; Oct 14 2026 is a Wednesday."""
    from services.worker.tasks.parse_document import _run

    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uploaded["document_id"])
        )
        result = _run(db, document)

    assert "weekday_mismatch" in result["flags"]

    with SessionLocal() as db:
        midterm = db.scalar(
            select(Assessment).where(
                Assessment.document_id == uploaded["document_id"],
                Assessment.title == "Midterm Exam 1",
            )
        )
        clean = db.scalar(
            select(Assessment).where(
                Assessment.document_id == uploaded["document_id"],
                Assessment.title == "Midterm Exam 2",
            )
        )
        assert midterm.confidence < clean.confidence
        assert midterm.confidence < 0.6, "a mismatched date must not land in an auto-accept bucket"


def test_a_document_with_no_section_matches_its_own_course(uploaded, stub_model) -> None:
    """US-1: the section comes from the syllabus, not from the upload form.

    The recorded extraction names COP 4530 section 003, which the seed has
    exactly once, so the match is unambiguous and the parse proceeds. The
    uploader is enrolled as a side effect, because uploading a syllabus is the
    statement "I am taking this course" and the timeline joins through
    enrollments.
    """
    from services.worker.tasks.parse_document import _run

    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uploaded["document_id"])
        )
        document.section_id = None
        db.query(Enrollment).filter(Enrollment.user_id == uploaded["user_id"]).delete()
        db.flush()

        outcome = _run(db, document)

        assert outcome["status"] == "parsed"
        assert document.section_id == uploaded["section_id"]
        enrolled = db.scalar(
            select(Enrollment).where(
                Enrollment.user_id == uploaded["user_id"],
                Enrollment.section_id == uploaded["section_id"],
            )
        )
        assert enrolled is not None and enrolled.confirmed is False
        db.query(Enrollment).filter(Enrollment.user_id == uploaded["user_id"]).delete()
        db.commit()


def test_an_ambiguous_course_asks_and_keeps_the_extraction(
    uploaded, stub_model, monkeypatch
) -> None:
    """Below the 0.8 line nothing is persisted, and the model is not called twice.

    Simulated by having the extraction name a course the seed does not carry.
    The run retains the extraction and the candidate list; confirming the
    section and running again resumes from that retained extraction, which
    the test proves by making a second model call impossible.
    """
    from services.worker.tasks import parse_document as task_module
    from services.worker.tasks.parse_document import _run

    stub_model.course.subject_code = "ZZZ"
    stub_model.course.catalog_number = "9999"

    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uploaded["document_id"])
        )
        document.section_id = None
        db.flush()

        outcome = _run(db, document)
        assert outcome["status"] == "needs_course"
        assert document.section_id is None
        assert db.scalar(select(Assessment).where(Assessment.document_id == document.id)) is None, (
            "nothing is attributed to a section the student has not confirmed"
        )

        retained = sorted(document.extraction_runs, key=lambda r: r.created_at)[-1]
        assert retained.status == "needs_course"
        match = retained.raw_output["_match"]
        assert match["guess"] == "ZZZ 9999"
        assert match["candidates"], "the student needs something to choose from"
        assert all("section_id" in c and "label" in c for c in match["candidates"])

        # The student confirms. The second pass must not need the model.
        def no_second_call(document):
            raise AssertionError("model called again after course confirmation")

        monkeypatch.setattr(task_module, "extract_document", no_second_call)
        document.section_id = uploaded["section_id"]
        db.flush()

        outcome = _run(db, document)
        assert outcome["status"] == "parsed"
        assert outcome["assessments"] == 9
        assert db.scalar(select(Assessment).where(Assessment.document_id == document.id))
        db.commit()


def test_late_evening_deadlines_keep_their_local_day(uploaded, stub_model) -> None:
    """The bug class this whole helper exists for.

    11:59pm Eastern is 03:59 UTC the following morning. Reading the stored
    instant's `.date()` would move essentially every deadline in the product
    one day later, because 11:59pm is the LMS default due time (9.3).
    """
    from services.worker.tasks.parse_document import _run

    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uploaded["document_id"])
        )
        _run(db, document)

    with SessionLocal() as db:
        midterm = db.scalar(
            select(Assessment).where(
                Assessment.document_id == uploaded["document_id"],
                Assessment.title == "Midterm Exam 1",
            )
        )
        assert midterm.time_inferred is True  # 11:59pm default was applied

        local = local_date(midterm.due_at, TZ)
        utc_day = midterm.due_at.astimezone(__import__("datetime").UTC).date()

        assert local.isoformat() == "2026-10-14"
        # If these ever coincide the test has stopped proving anything — the
        # fixture's due time must stay late enough to cross the UTC boundary.
        assert utc_day != local, "fixture no longer exercises the UTC day-shift"
