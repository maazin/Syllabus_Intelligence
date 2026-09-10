"""Persistence tests — PRD sections 12, 9.3.

Runs against real Postgres. The invariants worth testing here are all about a
*shared* parse: several students in a section point at one extraction, and a
re-parse must never destroy anyone's personal edits. That behavior depends on
foreign keys and constraints, so a sqlite stand-in would not test it.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5533/syllint"
)

from db.models import (  # noqa: E402
    Assessment,
    CorrectionLog,
    GradeCategory,
    Institution,
    Section,
    SectionPolicies,
    SyllabusDocument,
    Term,
    User,
    UserOverride,
)
from db.session import SessionLocal, engine  # noqa: E402
from schemas.extraction import ExtractionOutput  # noqa: E402
from schemas.resolution import ResolvedDate  # noqa: E402

from services.worker.tasks.persist import (  # noqa: E402
    load_calendar,
    persist_extraction,
    record_extraction_run,
)


def _db_available() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres not reachable; start docker compose first"
)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def fixture_env(db):
    """A user, a section, and a document to hang a parse off."""
    institution = db.scalar(select(Institution).where(Institution.domain == "test.edu"))
    if institution is None:
        pytest.skip("run scripts/seed_dev_data.py first")

    section = db.scalar(select(Section).limit(1))
    if section is None:
        pytest.skip("run scripts/seed_dev_data.py first")

    user = User(email=f"persist-{uuid.uuid4().hex[:8]}@test.edu", institution_id=institution.id)
    db.add(user)
    db.flush()

    document = SyllabusDocument(
        uploader_user_id=user.id,
        section_id=section.id,
        storage_key=f"documents/test/{uuid.uuid4().hex}",
        sha256=uuid.uuid4().hex * 2,
        mime="application/pdf",
        source="upload",
    )
    db.add(document)
    db.commit()

    yield {"user": user, "section": section, "document": document}

    ids = [
        a.id
        for a in db.scalars(select(Assessment).where(Assessment.document_id == document.id)).all()
    ]
    if ids:
        db.query(UserOverride).filter(UserOverride.assessment_id.in_(ids)).delete(
            synchronize_session=False
        )
        db.query(CorrectionLog).filter(CorrectionLog.assessment_id.in_(ids)).delete(
            synchronize_session=False
        )
        db.query(Assessment).filter(Assessment.id.in_(ids)).delete(synchronize_session=False)
    db.query(CorrectionLog).filter(CorrectionLog.user_id == user.id).delete()
    db.query(GradeCategory).filter(GradeCategory.document_id == document.id).delete()
    from db.models import ExtractionRun

    db.query(ExtractionRun).filter(ExtractionRun.document_id == document.id).delete()
    db.query(SyllabusDocument).filter(SyllabusDocument.id == document.id).delete()
    db.query(User).filter(User.id == user.id).delete()
    db.commit()


def _extraction() -> ExtractionOutput:
    return ExtractionOutput.model_validate(
        {
            "course": {"subject_code": "COP", "catalog_number": "4530"},
            "grade_breakdown": [
                {"category": "Exams", "weight_pct": 60, "item_count": 2},
                {"category": "Homework", "weight_pct": 40, "item_count": 1},
            ],
            "assessments": [
                {
                    "title": "Midterm 1",
                    "type": "midterm",
                    "category_ref": "Exams",
                    "weight_pct": 30,
                    "date_expression_raw": "October 14, 2026",
                    "source_span": "Midterm 1 is on October 14, 2026.",
                    "confidence": 0.95,
                    "page_ref": 2,
                },
                {
                    "title": "Reading Response",
                    "type": "reading_response",
                    "category_ref": "Homework",
                    "weight_pct": 40,
                    "date_expression_raw": "Week 5",
                    "source_span": "Reading response due in Week 5.",
                    "confidence": 0.7,
                },
                {
                    "title": "Guest Talk Writeup",
                    "type": "other",
                    "date_expression_raw": "TBA",
                    "source_span": "Guest talk writeup: TBA.",
                    "confidence": 0.6,
                },
            ],
            "policies": {
                "attendance_graded": True,
                "attendance_weight_pct": 5,
                "late_policy_class": "per_day_penalty",
                "group_work_present": False,
                "final_exam_type": "cumulative",
                "ai_policy_class": "permitted_with_disclosure",
                "required_materials": [
                    {"title": "The Textbook", "isbn": "123", "required": True, "est_cost_usd": 90}
                ],
            },
        }
    )


def _resolutions() -> dict[str, ResolvedDate]:
    from datetime import date
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    return {
        "Midterm 1": ResolvedDate(
            due_at=datetime(2026, 10, 14, 23, 59, tzinfo=tz),
            due_precision="date_only",
            time_inferred=True,
            resolution_case="full_date",
        ),
        "Reading Response": ResolvedDate(
            due_precision="week_only",
            resolution_case="week_number",
            week_start=date(2026, 9, 21),
            week_end=date(2026, 9, 28),
        ),
        "Guest Talk Writeup": ResolvedDate(
            due_precision="tbd", resolution_case="unspecified", needs_review=True
        ),
    }


# --- writing a parse -----------------------------------------------------------------------


def test_persist_writes_assessments_categories_and_policies(db, fixture_env) -> None:
    written = persist_extraction(
        db,
        document=fixture_env["document"],
        section=fixture_env["section"],
        extraction=_extraction(),
        resolutions=_resolutions(),
        confidences={"Midterm 1": 0.93},
    )
    db.commit()

    assert len(written) == 3
    stored = db.scalars(
        select(Assessment).where(Assessment.document_id == fixture_env["document"].id)
    ).all()
    assert {a.title for a in stored} == {"Midterm 1", "Reading Response", "Guest Talk Writeup"}

    categories = db.scalars(
        select(GradeCategory).where(GradeCategory.document_id == fixture_env["document"].id)
    ).all()
    assert {c.name for c in categories} == {"Exams", "Homework"}

    policies = db.scalar(
        select(SectionPolicies).where(SectionPolicies.section_id == fixture_env["section"].id)
    )
    assert policies is not None
    assert policies.ai_policy_class == "permitted_with_disclosure"
    assert policies.est_materials_cost_usd == 90


def test_precision_columns_preserve_the_9_3_invariants(db, fixture_env) -> None:
    persist_extraction(
        db,
        document=fixture_env["document"],
        section=fixture_env["section"],
        extraction=_extraction(),
        resolutions=_resolutions(),
        confidences={},
    )
    db.commit()

    by_title = {
        a.title: a
        for a in db.scalars(
            select(Assessment).where(Assessment.document_id == fixture_env["document"].id)
        ).all()
    }

    # date_only with an applied default time must carry the inference marker.
    assert by_title["Midterm 1"].due_precision == "date_only"
    assert by_title["Midterm 1"].time_inferred is True

    # week_only keeps its precision so no renderer can present it as a day.
    assert by_title["Reading Response"].due_precision == "week_only"
    assert by_title["Reading Response"].time_inferred is True

    # tbd never carries a date at all.
    assert by_title["Guest Talk Writeup"].due_precision == "tbd"
    assert by_title["Guest Talk Writeup"].due_at is None


def test_confidence_falls_back_to_the_models_own_score(db, fixture_env) -> None:
    persist_extraction(
        db,
        document=fixture_env["document"],
        section=fixture_env["section"],
        extraction=_extraction(),
        resolutions=_resolutions(),
        confidences={"Midterm 1": 0.42},
    )
    db.commit()

    stored = {
        a.title: a.confidence
        for a in db.scalars(
            select(Assessment).where(Assessment.document_id == fixture_env["document"].id)
        ).all()
    }
    assert stored["Midterm 1"] == pytest.approx(0.42)  # composite score wins
    assert stored["Reading Response"] == pytest.approx(0.7)  # falls back to the model's


# --- re-parsing a shared document -------------------------------------------------------------


def test_reparse_replaces_rows_rather_than_duplicating(db, fixture_env) -> None:
    for _ in range(2):
        persist_extraction(
            db,
            document=fixture_env["document"],
            section=fixture_env["section"],
            extraction=_extraction(),
            resolutions=_resolutions(),
            confidences={},
        )
        db.commit()

    stored = db.scalars(
        select(Assessment).where(Assessment.document_id == fixture_env["document"].id)
    ).all()
    assert len(stored) == 3, "a re-parse duplicated rows instead of replacing them"


def test_reparse_refuses_to_discard_user_overrides(db, fixture_env) -> None:
    """Section 12: a shared parse must be re-runnable without destroying edits.

    The safe behavior when that is impossible is to refuse loudly, not to
    silently delete the student's correction.
    """
    persist_extraction(
        db,
        document=fixture_env["document"],
        section=fixture_env["section"],
        extraction=_extraction(),
        resolutions=_resolutions(),
        confidences={},
    )
    db.commit()

    midterm = db.scalar(
        select(Assessment).where(
            Assessment.document_id == fixture_env["document"].id,
            Assessment.title == "Midterm 1",
        )
    )
    db.add(
        UserOverride(
            user_id=fixture_env["user"].id,
            assessment_id=midterm.id,
            due_at=datetime(2026, 10, 21, 23, 59, tzinfo=UTC),
        )
    )
    db.commit()

    with pytest.raises(ValueError, match="user overrides"):
        persist_extraction(
            db,
            document=fixture_env["document"],
            section=fixture_env["section"],
            extraction=_extraction(),
            resolutions=_resolutions(),
            confidences={},
        )
    db.rollback()

    # The override, and the assessment it points at, both survived.
    assert db.scalar(select(UserOverride).where(UserOverride.assessment_id == midterm.id))


def test_corrections_log_survives_a_reparse(db, fixture_env) -> None:
    """Section 12: append-only, never garbage-collected — it is the eval dataset."""
    persist_extraction(
        db,
        document=fixture_env["document"],
        section=fixture_env["section"],
        extraction=_extraction(),
        resolutions=_resolutions(),
        confidences={},
    )
    db.commit()

    midterm = db.scalar(
        select(Assessment).where(
            Assessment.document_id == fixture_env["document"].id,
            Assessment.title == "Midterm 1",
        )
    )
    db.add(
        CorrectionLog(
            user_id=fixture_env["user"].id,
            assessment_id=midterm.id,
            field="due_at",
            old_value="2026-10-14",
            new_value="2026-10-21",
            source_span="Midterm 1 is on October 14, 2026.",
            prompt_version="v1",
        )
    )
    db.commit()

    persist_extraction(
        db,
        document=fixture_env["document"],
        section=fixture_env["section"],
        extraction=_extraction(),
        resolutions=_resolutions(),
        confidences={},
    )
    db.commit()

    surviving = db.scalars(
        select(CorrectionLog).where(CorrectionLog.user_id == fixture_env["user"].id)
    ).all()
    assert len(surviving) == 1
    # The assessment it referenced is gone, but everything that makes the
    # correction useful for eval is on the row itself.
    assert surviving[0].assessment_id is None
    assert surviving[0].old_value == "2026-10-14"
    assert surviving[0].new_value == "2026-10-21"
    assert surviving[0].prompt_version == "v1"


# --- extraction runs / calendar loading --------------------------------------------------------


def test_extraction_run_records_the_prompt_version(db, fixture_env) -> None:
    """§16: production corrections must be attributable to a specific prompt."""
    run = record_extraction_run(
        db,
        document=fixture_env["document"],
        model="claude-sonnet-5",
        prompt_version="v1",
        pass_name="cascade",
        status="succeeded",
        cost_cents=0.42,
        latency_ms=1234,
    )
    db.commit()
    assert run.prompt_version == "v1"
    assert run.cost_cents == pytest.approx(0.42)


def test_load_calendar_reconstructs_the_resolver_value_object(db, fixture_env) -> None:
    term = db.scalar(select(Term).where(Term.id == fixture_env["section"].term_id))
    calendar = load_calendar(db, term.id)

    assert calendar.term_name == term.name
    assert calendar.start_date == term.start_date
    assert calendar.no_class_days, "seeded no-class days did not survive the round trip"
    assert calendar.final_exam_matrix, "seeded exam matrix did not survive the round trip"
    # The labels matter: anchor expressions resolve against them (9.2).
    assert calendar.dates_for_label("Thanksgiving")
