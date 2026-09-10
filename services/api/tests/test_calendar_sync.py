"""Google Calendar sync tests — PRD section 13, Appendix C.

Uses a fake client rather than hitting Google: the behaviors worth pinning down
are all *ours* — what gets written, what does not, and what happens when the
student deletes an event. None of that needs a network round trip to verify, and
the two Appendix C items here ("creates, updates, and removes events" and
"deleting an event in Google does not resurrect it") are exactly the kind of
rule that regresses silently.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5533/syllint"
)
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-for-hmac-sha256-min")

from db.models import (  # noqa: E402
    Assessment,
    CalendarConnection,
    CalendarEventMap,
    Enrollment,
    Institution,
    Section,
    SyllabusDocument,
    User,
    UserOverride,
)
from db.session import SessionLocal, engine  # noqa: E402

from services.api.calendar_sync import build_event, sync_user  # noqa: E402
from services.api.google_calendar import CalendarEvent, EventDeletedByUser  # noqa: E402

TZ = "America/New_York"
EASTERN = ZoneInfo(TZ)


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


class FakeGoogle:
    """Records what sync asked Google to do.

    `deleted_externally` simulates the student removing an event from their own
    calendar — the case section 13 singles out.
    """

    def __init__(self, deleted_externally: set[str] | None = None) -> None:
        self.inserted: list[tuple[str, CalendarEvent]] = []
        self.updated: list[tuple[str, CalendarEvent]] = []
        self.deleted: list[str] = []
        self.deleted_externally = deleted_externally or set()
        self._counter = 0

    def insert_event(self, calendar_id: str, event: CalendarEvent):
        self._counter += 1
        event_id = f"evt-{self._counter}"
        self.inserted.append((event_id, event))
        return event_id, f"etag-{self._counter}"

    def update_event(self, calendar_id: str, external_event_id: str, event: CalendarEvent):
        if external_event_id in self.deleted_externally:
            raise EventDeletedByUser(external_event_id)
        self.updated.append((external_event_id, event))
        return external_event_id, f"etag-updated-{external_event_id}"

    def delete_event(self, calendar_id: str, external_event_id: str) -> None:
        self.deleted.append(external_event_id)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def env(db):
    """A connected user with three reviewed assessments of differing precision."""
    institution = db.scalar(select(Institution).where(Institution.domain == "test.edu"))
    template = db.scalar(select(Section).limit(1))
    if institution is None or template is None:
        pytest.skip("run scripts/seed_dev_data.py first")

    # A dedicated section, because assessments are section-scoped: a shared
    # parse is visible to everyone enrolled in that section (which is the point,
    # see 18.3), so reusing a seeded section would let another test's fixture
    # data show up in this user's sync.
    section = Section(
        course_id=template.course_id,
        term_id=template.term_id,
        section_code=f"T{uuid.uuid4().hex[:4]}",
        meeting_pattern=template.meeting_pattern,
        modality=template.modality,
    )
    db.add(section)
    db.flush()

    user = User(email=f"cal-{uuid.uuid4().hex[:8]}@test.edu", institution_id=institution.id)
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

    now = datetime.now(UTC)
    made = {}
    specs = [
        (
            "Midterm",
            "midterm",
            "exact_datetime",
            False,
            datetime(2026, 10, 14, 14, 0, tzinfo=EASTERN),
        ),
        (
            "Problem Set",
            "problem_set",
            "date_only",
            True,
            datetime(2026, 10, 20, 23, 59, tzinfo=EASTERN),
        ),
        (
            "Reading",
            "reading_response",
            "week_only",
            True,
            datetime(2026, 9, 21, 0, 0, tzinfo=EASTERN),
        ),
        ("Mystery Task", "other", "tbd", False, None),
    ]
    for title, type_, precision, inferred, due in specs:
        a = Assessment(
            document_id=document.id,
            section_id=section.id,
            title=title,
            type=type_,
            due_at=due,
            due_precision=precision,
            time_inferred=inferred,
            weight_pct=20,
            source_span=f"{title} appears in the syllabus.",
            confidence=0.9,
            verified_at=now,  # review complete, so eligible for the calendar
        )
        db.add(a)
        db.flush()
        made[title] = a

    connection = CalendarConnection(
        user_id=user.id,
        provider="google",
        refresh_token_enc="fake-refresh-token",
        target_calendar_id="cal-123",
    )
    db.add(connection)
    db.commit()

    yield {
        "user": user,
        "assessments": made,
        "connection": connection,
        "document": document,
        "section": section,
    }

    ids = list(a.id for a in made.values())
    db.query(CalendarEventMap).filter(CalendarEventMap.assessment_id.in_(ids)).delete(
        synchronize_session=False
    )
    db.query(CalendarEventMap).filter(CalendarEventMap.connection_id == connection.id).delete(
        synchronize_session=False
    )
    db.query(UserOverride).filter(UserOverride.assessment_id.in_(ids)).delete(
        synchronize_session=False
    )
    db.query(Assessment).filter(Assessment.id.in_(ids)).delete(synchronize_session=False)
    db.query(CalendarConnection).filter(CalendarConnection.user_id == user.id).delete()
    db.query(SyllabusDocument).filter(SyllabusDocument.id == document.id).delete()
    db.query(Enrollment).filter(Enrollment.user_id == user.id).delete()
    db.query(User).filter(User.id == user.id).delete()
    from db.models import SectionPolicies

    db.query(SectionPolicies).filter(SectionPolicies.section_id == section.id).delete()
    db.query(Section).filter(Section.id == section.id).delete()
    db.commit()


# --- Appendix C: creates, updates, and removes events -------------------------------


def test_sync_creates_events_for_eligible_items(db, env) -> None:
    google = FakeGoogle()
    report = sync_user(db, env["user"], client=google, timezone=TZ)

    # Three eligible items; the tbd one is never written (9.3).
    assert report.created == 3
    summaries = {event.summary for _, event in google.inserted}
    assert any("Midterm" in s for s in summaries)
    assert not any("Mystery Task" in s for s in summaries)


def test_second_sync_updates_rather_than_duplicating(db, env) -> None:
    google = FakeGoogle()
    sync_user(db, env["user"], client=google, timezone=TZ)

    google2 = FakeGoogle()
    report = sync_user(db, env["user"], client=google2, timezone=TZ)

    assert report.created == 0, "a second sync created duplicate events"
    assert report.updated == 3


def test_dismissing_an_item_removes_its_event(db, env) -> None:
    google = FakeGoogle()
    sync_user(db, env["user"], client=google, timezone=TZ)

    midterm = env["assessments"]["Midterm"]
    db.add(UserOverride(user_id=env["user"].id, assessment_id=midterm.id, dismissed=True))
    db.commit()

    google2 = FakeGoogle()
    report = sync_user(db, env["user"], client=google2, timezone=TZ)
    assert report.deleted == 1
    assert len(google2.deleted) == 1


def test_unreviewed_items_never_reach_the_calendar(db, env) -> None:
    """US-3: nothing writes to a real calendar until review is complete."""
    for assessment in env["assessments"].values():
        assessment.verified_at = None
    db.commit()

    google = FakeGoogle()
    report = sync_user(db, env["user"], client=google, timezone=TZ)
    assert report.created == 0
    assert google.inserted == []


# --- Appendix C: deleting in Google does not resurrect ---------------------------------


def test_event_deleted_in_google_is_dismissed_not_recreated(db, env) -> None:
    """Section 13's sharpest rule, stated as a test.

    "Recreating an event the user deleted is the fastest way to get uninstalled."
    """
    google = FakeGoogle()
    sync_user(db, env["user"], client=google, timezone=TZ)

    # The student deletes the first event from their own calendar.
    gone = google.inserted[0][0]
    second = FakeGoogle(deleted_externally={gone})
    report = sync_user(db, env["user"], client=second, timezone=TZ)

    assert report.dismissed_by_user == 1
    assert all(event_id != gone for event_id, _ in second.inserted), "the event was recreated"

    # A third sync must also leave it alone — the dismissal is durable.
    third = FakeGoogle()
    report3 = sync_user(db, env["user"], client=third, timezone=TZ)
    assert report3.dismissed_by_user == 0
    assert report3.created == 0


def test_a_google_deletion_is_recorded_as_a_user_override(db, env) -> None:
    """The dismissal lives where every other personal edit does, so a re-parse
    of the shared section-level extraction cannot undo it (section 12)."""
    google = FakeGoogle()
    sync_user(db, env["user"], client=google, timezone=TZ)
    gone = google.inserted[0][0]

    mapping_before = db.scalar(
        select(CalendarEventMap).where(CalendarEventMap.external_event_id == gone)
    )
    assessment_id = mapping_before.assessment_id

    sync_user(db, env["user"], client=FakeGoogle(deleted_externally={gone}), timezone=TZ)

    override = db.scalar(
        select(UserOverride).where(
            UserOverride.user_id == env["user"].id,
            UserOverride.assessment_id == assessment_id,
        )
    )
    assert override is not None and override.dismissed is True
    # The stale mapping is gone too, so nothing tries to update it again.
    assert (
        db.scalar(select(CalendarEventMap).where(CalendarEventMap.external_event_id == gone))
        is None
    )


# --- event shaping (9.3 carried into the calendar) --------------------------------------


def test_exact_datetime_becomes_a_timed_event(db, env) -> None:
    from db.models import Course

    midterm = env["assessments"]["Midterm"]
    course = db.scalar(select(Course).where(Course.id == env["section"].course_id))
    event = build_event(midterm, None, course, TZ)

    assert event.start_at is not None
    assert event.all_day_on is None and event.all_day_span is None
    payload = event.to_payload()
    assert "dateTime" in payload["start"]


def test_date_only_becomes_an_all_day_event(db, env) -> None:
    from db.models import Course

    problem_set = env["assessments"]["Problem Set"]
    course = db.scalar(select(Course).where(Course.id == env["section"].course_id))
    event = build_event(problem_set, None, course, TZ)

    payload = event.to_payload()
    assert "date" in payload["start"], "an inferred time must not become a timed event"
    # And crucially the local day, not the UTC one — 11:59pm Eastern is Oct 21 in UTC.
    assert payload["start"]["date"] == "2026-10-20"


def test_week_only_becomes_a_seven_day_span(db, env) -> None:
    """9.3: a week band never collapses to a specific day."""
    from db.models import Course

    reading = env["assessments"]["Reading"]
    course = db.scalar(select(Course).where(Course.id == env["section"].course_id))
    event = build_event(reading, None, course, TZ)

    assert event.all_day_span is not None
    start, end = event.all_day_span
    assert (end - start) == timedelta(days=7)


def test_tbd_is_never_shaped_into_an_event(db, env) -> None:
    from db.models import Course

    mystery = env["assessments"]["Mystery Task"]
    course = db.scalar(select(Course).where(Course.id == env["section"].course_id))
    assert build_event(mystery, None, course, TZ) is None


def test_event_description_carries_provenance(db, env) -> None:
    """13: course, type, weight, and a link back to the source snippet."""
    from db.models import Course

    midterm = env["assessments"]["Midterm"]
    course = db.scalar(select(Course).where(Course.id == env["section"].course_id))
    event = build_event(midterm, None, course, TZ)

    assert "20% of grade" in event.description
    assert "midterm" in event.description
    assert midterm.source_span in event.description


def test_inferred_time_is_disclosed_in_the_event_body(db, env) -> None:
    from db.models import Course

    problem_set = env["assessments"]["Problem Set"]
    course = db.scalar(select(Course).where(Course.id == env["section"].course_id))
    event = build_event(problem_set, None, course, TZ)
    assert "did not state a time" in event.description


def test_an_event_with_no_date_refuses_to_serialize() -> None:
    """A last-resort guard: a payload with no start would be a fabricated event."""
    event = CalendarEvent(assessment_id="x", summary="s", description="d")
    with pytest.raises(ValueError, match="tbd"):
        event.to_payload()
