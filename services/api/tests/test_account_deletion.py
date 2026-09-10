"""Account deletion tests — PRD section 15.2, Appendix C.

The two claims worth proving are in tension, which is why they get their own
file: everything belonging to the student goes, *and* the corrections dataset
survives. Getting either half wrong is a real problem — one is a privacy
failure, the other silently destroys the eval data section 16 depends on.
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
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-for-hmac-sha256-min")
os.environ.setdefault("INSTITUTION_EMAIL_DOMAIN", "test.edu")

from db.models import (  # noqa: E402
    Assessment,
    CalendarConnection,
    CalendarEventMap,
    CorrectionLog,
    Enrollment,
    Institution,
    Section,
    SyllabusDocument,
    User,
    UserOverride,
)
from db.session import SessionLocal, engine  # noqa: E402

from services.api.account import delete_account  # noqa: E402


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


def _make_user(db, institution, section, *, email_prefix="del") -> dict:
    user = User(
        email=f"{email_prefix}-{uuid.uuid4().hex[:8]}@test.edu", institution_id=institution.id
    )
    db.add(user)
    db.flush()

    db.add(
        Enrollment(user_id=user.id, section_id=section.id, term_id=section.term_id, confirmed=True)
    )
    document = SyllabusDocument(
        uploader_user_id=user.id,
        section_id=section.id,
        storage_key=f"documents/test/{uuid.uuid4().hex}",
        sha256=uuid.uuid4().hex * 2,
        mime="application/pdf",
        source="upload",
    )
    db.add(document)
    db.flush()

    assessment = Assessment(
        document_id=document.id,
        section_id=section.id,
        title="Midterm",
        type="midterm",
        due_at=datetime(2026, 10, 14, 23, 59, tzinfo=UTC),
        due_precision="date_only",
        weight_pct=20,
        source_span="Midterm is on October 14.",
        confidence=0.9,
        verified_at=datetime.now(UTC),
    )
    db.add(assessment)
    db.flush()

    db.add(
        UserOverride(
            user_id=user.id,
            assessment_id=assessment.id,
            due_at=datetime(2026, 10, 21, 23, 59, tzinfo=UTC),
        )
    )
    db.add(
        CorrectionLog(
            user_id=user.id,
            assessment_id=assessment.id,
            field="due_at",
            old_value="2026-10-14",
            new_value="2026-10-21",
            source_span="Midterm is on October 14.",
            prompt_version="v1",
        )
    )
    connection = CalendarConnection(
        user_id=user.id,
        provider="google",
        refresh_token_enc="fake-token",
        target_calendar_id="cal-abc",
    )
    db.add(connection)
    db.flush()
    db.add(
        CalendarEventMap(
            connection_id=connection.id,
            assessment_id=assessment.id,
            external_event_id="evt-1",
            etag="etag-1",
        )
    )
    db.commit()
    return {
        "user": user,
        "document": document,
        "assessment": assessment,
        "connection": connection,
    }


@pytest.fixture
def subject(db):
    institution = db.scalar(select(Institution).where(Institution.domain == "test.edu"))
    section = db.scalar(select(Section).limit(1))
    if institution is None or section is None:
        pytest.skip("run scripts/seed_dev_data.py first")
    made = _make_user(db, institution, section)
    user_id = made["user"].id
    yield {**made, "institution": institution, "section": section, "user_id": user_id}
    # Best-effort cleanup if a test did not delete. Uses the captured id because
    # a deleted ORM object cannot be refreshed.
    db.rollback()
    db.query(CorrectionLog).filter(CorrectionLog.user_id == user_id).delete()
    db.commit()


# --- Appendix C: account deletion removes all documents, events, and tokens -----------


def test_deletion_removes_everything_belonging_to_the_user(db, subject) -> None:
    user_id = subject["user_id"]
    document_id = subject["document"].id
    assessment_id = subject["assessment"].id
    connection_id = subject["connection"].id

    report = delete_account(db, subject["user_id"], remove_remote_events=False)

    assert report.documents == 1
    assert report.enrollments == 1
    assert report.overrides == 1
    assert report.calendar_events == 1

    assert db.scalar(select(User).where(User.id == user_id)) is None
    assert db.scalar(select(SyllabusDocument).where(SyllabusDocument.id == document_id)) is None
    assert db.scalar(select(Assessment).where(Assessment.id == assessment_id)) is None
    assert db.scalar(select(Enrollment).where(Enrollment.user_id == user_id)) is None
    assert db.scalar(select(UserOverride).where(UserOverride.user_id == user_id)) is None
    assert (
        db.scalar(select(CalendarConnection).where(CalendarConnection.id == connection_id)) is None
    )
    assert (
        db.scalar(select(CalendarEventMap).where(CalendarEventMap.connection_id == connection_id))
        is None
    )


def test_corrections_survive_but_are_anonymized(db, subject) -> None:
    """15.2: "Retain only anonymized corrections data." Section 12: never
    garbage-collect the log. Both hold at once."""
    user_id = subject["user_id"]
    correction_id = db.scalar(select(CorrectionLog.id).where(CorrectionLog.user_id == user_id))
    assert correction_id is not None

    report = delete_account(db, subject["user_id"], remove_remote_events=False)
    assert report.corrections_anonymized == 1

    surviving = db.scalar(select(CorrectionLog).where(CorrectionLog.id == correction_id))
    assert surviving is not None, "the eval dataset was destroyed"
    assert surviving.user_id is None
    assert surviving.assessment_id is None
    # Everything that makes the row useful for eval is intact.
    assert surviving.field == "due_at"
    assert surviving.old_value == "2026-10-14"
    assert surviving.new_value == "2026-10-21"
    assert surviving.prompt_version == "v1"


def test_deletion_does_not_touch_another_students_data(db, subject) -> None:
    """Two students, one section. Deleting one must not disturb the other."""
    other = _make_user(db, subject["institution"], subject["section"], email_prefix="keep")
    other_ids = {
        "user": other["user"].id,
        "document": other["document"].id,
        "assessment": other["assessment"].id,
    }

    delete_account(db, subject["user_id"], remove_remote_events=False)

    assert db.scalar(select(User).where(User.id == other_ids["user"])) is not None
    assert (
        db.scalar(select(SyllabusDocument).where(SyllabusDocument.id == other_ids["document"]))
        is not None
    )
    assert db.scalar(select(Assessment).where(Assessment.id == other_ids["assessment"])) is not None
    assert (
        db.scalar(select(UserOverride).where(UserOverride.user_id == other_ids["user"])) is not None
    )

    delete_account(db, other["user"].id, remove_remote_events=False)


def test_deletion_is_idempotent_enough_to_be_safe(db, subject) -> None:
    """A retried delete must not raise — the student already asked once."""
    delete_account(db, subject["user_id"], remove_remote_events=False)
    # The ORM object is detached now; a second call on a vanished user is a no-op.
    report = delete_account(db, subject["user_id"], remove_remote_events=False)
    assert report.documents == 0


def test_a_remote_cleanup_failure_does_not_block_deletion(db, subject, monkeypatch) -> None:
    """A student who asked to be deleted must not be blocked by Google being down."""
    import services.api.google_calendar as gcal

    def _boom(*args, **kwargs):
        raise RuntimeError("Google is unreachable")

    monkeypatch.setattr(gcal.GoogleCalendarClient, "from_connection", _boom)

    user_id = subject["user_id"]
    report = delete_account(db, user_id, remove_remote_events=True)

    assert db.scalar(select(User).where(User.id == user_id)) is None
    assert report.errors, "the failure should be reported, not silently swallowed"


# --- the HTTP surface ------------------------------------------------------------------


def test_delete_endpoint_requires_matching_email_confirmation(db, subject) -> None:
    from fastapi.testclient import TestClient

    from services.api.deps import issue_access_token
    from services.api.main import app

    client = TestClient(app)
    email = subject["user"].email
    headers = {"Authorization": f"Bearer {issue_access_token(subject['user_id'])}"}

    wrong = client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirm_email": "someone-else@test.edu"},
        headers=headers,
    )
    assert wrong.status_code == 400

    # The account is untouched after a failed confirmation.
    assert db.scalar(select(User).where(User.id == subject["user_id"])) is not None

    right = client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirm_email": email},
        headers=headers,
    )
    assert right.status_code == 200
    assert right.json()["status"] == "deleted"


def test_deleted_users_token_stops_working(db, subject) -> None:
    from fastapi.testclient import TestClient

    from services.api.deps import issue_access_token
    from services.api.main import app

    client = TestClient(app)
    token = issue_access_token(subject["user_id"])
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/account/me", headers=headers).status_code == 200
    delete_account(db, subject["user_id"], remove_remote_events=False)
    db.commit()
    # The JWT is still cryptographically valid; the user lookup is what fails.
    assert client.get("/api/v1/account/me", headers=headers).status_code == 401
