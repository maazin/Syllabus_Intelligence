"""API tests — PRD sections 23, 15.1, 15.2.

Runs against a real Postgres (the docker-compose one), because the things most
worth testing here are ownership boundaries and constraint behavior, and a
sqlite stand-in would not exercise either faithfully.

Skips cleanly when the database is unreachable so `pytest` still passes on a
machine with no docker running.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5533/syllint"
)
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-for-hmac-sha256-min")
os.environ.setdefault("INSTITUTION_EMAIL_DOMAIN", "test.edu")
os.environ.setdefault("ENVIRONMENT", "local")

from db.models import Enrollment, Institution, Section, Term, User  # noqa: E402
from db.session import SessionLocal, engine  # noqa: E402

from services.api.deps import issue_access_token  # noqa: E402
from services.api.main import app  # noqa: E402


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
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def institution(db) -> Institution:
    inst = db.scalar(select(Institution).where(Institution.domain == "test.edu"))
    if inst is None:
        pytest.skip("run scripts/seed_dev_data.py first")
    return inst


def purge_user(db, user_id: uuid.UUID) -> None:
    """Delete a user and everything referencing them.

    This is the same ordering account deletion needs (15.2: documents,
    enrollments, overrides, calendar events, tokens). There is no ON DELETE
    CASCADE on these foreign keys, and deliberately so — a cascade would make
    it easy to remove a student's data by accident. The order below is the
    dependency order the constraints enforce.
    """
    from db.models import (
        Assessment,
        CalendarConnection,
        CalendarEventMap,
        CorrectionLog,
        ExtractionRun,
        GradeCategory,
        SyllabusDocument,
        UserOverride,
    )

    documents = db.scalars(
        select(SyllabusDocument).where(SyllabusDocument.uploader_user_id == user_id)
    ).all()
    document_ids = [d.id for d in documents]

    connections = db.scalars(
        select(CalendarConnection).where(CalendarConnection.user_id == user_id)
    ).all()
    for connection in connections:
        db.query(CalendarEventMap).filter(CalendarEventMap.connection_id == connection.id).delete()
    db.query(CalendarConnection).filter(CalendarConnection.user_id == user_id).delete()

    db.query(UserOverride).filter(UserOverride.user_id == user_id).delete()
    # 15.2 retains anonymized corrections data; the test fixture drops it outright
    # because these rows are synthetic.
    db.query(CorrectionLog).filter(CorrectionLog.user_id == user_id).delete()

    if document_ids:
        db.query(Assessment).filter(Assessment.document_id.in_(document_ids)).delete(
            synchronize_session=False
        )
        db.query(GradeCategory).filter(GradeCategory.document_id.in_(document_ids)).delete(
            synchronize_session=False
        )
        # With the compose worker running, an upload in a test gets parsed for
        # real before teardown, and a parse writes extraction_runs. Cleaning
        # them here is what lets the suite run beside a live stack.
        db.query(ExtractionRun).filter(ExtractionRun.document_id.in_(document_ids)).delete(
            synchronize_session=False
        )
        db.query(SyllabusDocument).filter(SyllabusDocument.id.in_(document_ids)).delete(
            synchronize_session=False
        )

    db.query(Enrollment).filter(Enrollment.user_id == user_id).delete()
    db.query(User).filter(User.id == user_id).delete()
    db.commit()


@pytest.fixture
def user(db, institution) -> User:
    """A throwaway user, removed afterwards so tests do not accumulate rows."""
    email = f"pytest-{uuid.uuid4().hex[:8]}@test.edu"
    u = User(email=email, institution_id=institution.id)
    db.add(u)
    db.commit()
    db.refresh(u)
    user_id = u.id
    yield u
    purge_user(db, user_id)


@pytest.fixture
def auth(user) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(user.id)}"}


# --- health / contract -----------------------------------------------------------------


def test_health(client) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_openapi_exposes_the_section_23_surface(client) -> None:
    """23: generate the spec from the app and treat it as the source of truth."""
    paths = client.get("/openapi.json").json()["paths"]
    for expected in (
        "/api/v1/auth/magic-link",
        "/api/v1/auth/verify",
        "/api/v1/documents",
        "/api/v1/timeline",
        "/api/v1/heatmap",
    ):
        assert expected in paths, expected


# --- auth (15.2) -----------------------------------------------------------------------


def test_signup_is_restricted_to_the_institution_domain(client) -> None:
    """15.2: .edu verification at signup."""
    response = client.post("/api/v1/auth/magic-link", json={"email": "someone@gmail.com"})
    assert response.status_code == 400
    assert "test.edu" in response.json()["detail"]


def test_protected_routes_reject_anonymous_callers(client) -> None:
    for path in ("/api/v1/courses", "/api/v1/timeline", "/api/v1/calendar/ics-url"):
        assert client.get(path).status_code == 401, path


def test_a_magic_link_token_is_not_an_access_token(client) -> None:
    """Purpose is checked on decode, so a 15-minute login token cannot become a
    30-day session token."""
    from services.api.deps import issue_magic_link_token

    token = issue_magic_link_token("someone@test.edu")
    response = client.get("/api/v1/courses", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_garbage_token_is_rejected(client) -> None:
    response = client.get("/api/v1/courses", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


# --- catalog ---------------------------------------------------------------------------


def test_catalog_search_matches_code_and_title(client, auth) -> None:
    by_code = client.get("/api/v1/courses?q=COP", headers=auth).json()
    assert by_code and all(c["subject_code"] == "COP" for c in by_code)

    by_title = client.get("/api/v1/courses?q=Statistics", headers=auth).json()
    assert any("Statistics" in c["title"] for c in by_title)


def test_manual_course_entry_enrolls_the_student(client, auth, db, institution) -> None:
    """US-2: a student with no syllabus still gets a timeline."""
    term = db.scalar(select(Term).where(Term.institution_id == institution.id))
    payload = {
        "subject_code": "ART",
        "catalog_number": "1301",
        "title": "Drawing I",
        "credits": 3,
        "term_id": str(term.id),
        "section_code": "001",
        "meeting_pattern": "TR 09:30-10:45",
    }
    response = client.post("/api/v1/courses/manual", json=payload, headers=auth)
    assert response.status_code == 201
    section_id = uuid.UUID(response.json()["section_id"])

    enrollment = db.scalar(select(Enrollment).where(Enrollment.section_id == section_id))
    assert enrollment is not None and enrollment.confirmed is True


def test_manual_course_entry_is_idempotent(client, auth, db, institution) -> None:
    term = db.scalar(select(Term).where(Term.institution_id == institution.id))
    payload = {
        "subject_code": "ART",
        "catalog_number": "1302",
        "title": "Drawing II",
        "credits": 3,
        "term_id": str(term.id),
    }
    first = client.post("/api/v1/courses/manual", json=payload, headers=auth).json()
    second = client.post("/api/v1/courses/manual", json=payload, headers=auth).json()
    assert first["section_id"] == second["section_id"]


def test_manual_course_rejects_absurd_credits(client, auth, db, institution) -> None:
    term = db.scalar(select(Term).where(Term.institution_id == institution.id))
    response = client.post(
        "/api/v1/courses/manual",
        json={
            "subject_code": "ART",
            "catalog_number": "9999",
            "title": "Everything",
            "credits": 500,
            "term_id": str(term.id),
        },
        headers=auth,
    )
    assert response.status_code == 422


# --- uploads (US-1, 15.1) ----------------------------------------------------------------


def test_upload_rejects_more_than_ten_files(client, auth) -> None:
    files = [("files", (f"f{i}.txt", b"hello", "text/plain")) for i in range(11)]
    response = client.post("/api/v1/documents", files=files, headers=auth)
    assert response.status_code == 400


def test_one_bad_file_does_not_block_the_others(client, auth) -> None:
    """US-1: each file's parse status is independent."""
    files = [
        ("files", ("good.txt", b"COP 4530 syllabus text", "text/plain")),
        ("files", ("empty.txt", b"", "text/plain")),
    ]
    results = client.post("/api/v1/documents", files=files, headers=auth).json()
    assert len(results) == 2
    statuses = {r["filename"]: r["status"] for r in results}
    assert statuses["empty.txt"] == "failed"
    assert statuses["good.txt"] in ("queued", "ready")


def test_identical_content_dedupes_to_one_parse(client, auth) -> None:
    """18.3: dedup is load-bearing, not an optimization."""
    payload = b"a unique syllabus body " + uuid.uuid4().hex.encode()
    first = client.post(
        "/api/v1/documents",
        files=[("files", ("a.txt", payload, "text/plain"))],
        headers=auth,
    ).json()[0]
    second = client.post(
        "/api/v1/documents",
        files=[("files", ("a-again.txt", payload, "text/plain"))],
        headers=auth,
    ).json()[0]

    assert first["deduped"] is False
    assert second["deduped"] is True
    assert second["document_id"] == first["document_id"]
    assert second["status"] == "ready"


def test_a_document_is_not_visible_to_another_student(client, auth, db, institution) -> None:
    """15.1: original uploaded files stay private to the uploader."""
    uploaded = client.post(
        "/api/v1/documents",
        files=[("files", ("private.txt", b"private syllabus", "text/plain"))],
        headers=auth,
    ).json()[0]

    other = User(email=f"other-{uuid.uuid4().hex[:8]}@test.edu", institution_id=institution.id)
    db.add(other)
    db.commit()
    try:
        headers = {"Authorization": f"Bearer {issue_access_token(other.id)}"}
        response = client.get(f"/api/v1/documents/{uploaded['document_id']}", headers=headers)
        # 404, not 403 — a wrong id must be indistinguishable from someone else's.
        assert response.status_code == 404
    finally:
        purge_user(db, other.id)


def test_review_complete_on_a_document_with_no_items_is_a_conflict(client, auth) -> None:
    uploaded = client.post(
        "/api/v1/documents",
        files=[("files", ("x.txt", f"x {uuid.uuid4().hex}".encode(), "text/plain"))],
        headers=auth,
    ).json()[0]
    response = client.post(
        f"/api/v1/documents/{uploaded['document_id']}/review-complete", headers=auth
    )
    assert response.status_code == 409


# --- calendar (13) --------------------------------------------------------------------------


def test_ics_feed_url_is_per_user_and_unguessable(client, auth, user) -> None:
    url = client.get("/api/v1/calendar/ics-url", headers=auth).json()["url"]
    assert str(user.id) not in url  # the raw user id must not leak into the URL
    token = url.rsplit("/", 1)[-1]
    assert len(token) == 32


def test_ics_feed_serves_calendar_content(client, auth) -> None:
    url = client.get("/api/v1/calendar/ics-url", headers=auth).json()["url"]
    response = client.get("/api/v1/calendar/ics/" + url.rsplit("/", 1)[-1])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.text.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in response.text


def test_a_bogus_feed_token_is_not_served(client) -> None:
    assert client.get("/api/v1/calendar/ics/" + "0" * 32).status_code == 404


# --- timeline / heatmap ----------------------------------------------------------------------


def test_timeline_is_empty_for_a_new_user(client, auth) -> None:
    assert client.get("/api/v1/timeline", headers=auth).json() == []


def test_heatmap_requires_a_term(client, auth) -> None:
    assert client.get("/api/v1/heatmap", headers=auth).status_code == 422


def test_heatmap_returns_a_cell_per_week(client, auth, db, institution) -> None:
    """A student with no items still sees the baseline floor (11.3)."""
    term = db.scalar(select(Term).where(Term.institution_id == institution.id))
    cells = client.get(f"/api/v1/heatmap?term_id={term.id}", headers=auth).json()
    assert cells
    assert all("week_start" in c and "effort_hours" in c for c in cells)


# --- ICS formatting (13) ---------------------------------------------------------------


def test_ics_escaping_follows_rfc5545() -> None:
    """Commas and semicolons inside a title must not split the property value."""
    from services.api.routers.calendar import _escape

    assert _escape("Essay, part 1; final") == "Essay\\, part 1\\; final"
    assert _escape("line\nbreak") == "line\\nbreak"
    # Backslash is escaped first, so an existing backslash does not swallow the
    # escapes introduced after it.
    assert _escape("a\\b") == "a\\\\b"


def test_ics_long_lines_are_folded_to_75_octets() -> None:
    from services.api.routers.calendar import _fold

    folded = _fold("SUMMARY:" + "x" * 200)
    for line in folded.split("\r\n"):
        assert len(line) <= 75
    # Continuation lines must begin with a space.
    assert all(line.startswith(" ") for line in folded.split("\r\n")[1:])


# --- upload implies enrollment (US-1 -> US-5) ---------------------------------------


def test_uploading_for_a_section_enrolls_the_student(client, auth, db, user) -> None:
    """Without this the timeline is empty after a successful parse.

    Timeline, heatmap, and the ICS feed all join through `enrollments`, so an
    upload that does not enroll reads to the student as the product having
    silently lost their syllabus.
    """
    section = db.scalar(select(Section).limit(1))
    uploaded = client.post(
        f"/api/v1/documents?section_id={section.id}",
        files=[("files", ("s.txt", f"syllabus {uuid.uuid4().hex}".encode(), "text/plain"))],
        headers=auth,
    ).json()[0]
    assert uploaded["status"] in ("queued", "ready")

    enrollment = db.scalar(
        select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.section_id == section.id)
    )
    assert enrollment is not None
    # Inferred from an upload, not confirmed against the registrar.
    assert enrollment.confirmed is False


def test_enrolling_twice_for_the_same_section_is_a_no_op(client, auth, db, user) -> None:
    section = db.scalar(select(Section).limit(1))
    for _ in range(2):
        client.post(
            f"/api/v1/documents?section_id={section.id}",
            files=[("files", ("s.txt", f"body {uuid.uuid4().hex}".encode(), "text/plain"))],
            headers=auth,
        )
    count = len(
        db.scalars(
            select(Enrollment).where(
                Enrollment.user_id == user.id, Enrollment.section_id == section.id
            )
        ).all()
    )
    assert count == 1


def test_a_student_sees_one_copy_of_each_item_when_a_section_has_several_uploads(
    client, auth, db, user
) -> None:
    """Assessments hang off documents, and a section accumulates several.

    Joining assessments straight to the section showed every enrolled student
    every upload's items, so three uploads in a section tripled the timeline
    and the heatmap. The student sees their own upload, otherwise the
    section's canonical one, otherwise the newest.
    """
    import uuid as _uuid

    from db.models import Assessment, Enrollment, Section, SyllabusDocument, User
    from sqlalchemy import select

    me = user
    section = db.scalar(select(Section).limit(1))
    assert section is not None

    def upload(owner: User, title: str) -> SyllabusDocument:
        document = SyllabusDocument(
            uploader_user_id=owner.id,
            section_id=section.id,
            storage_key=f"documents/xx/{_uuid.uuid4().hex}",
            sha256=_uuid.uuid4().hex,
            mime="application/pdf",
            visibility="private",
        )
        db.add(document)
        db.flush()
        db.add(
            Assessment(
                document_id=document.id,
                section_id=section.id,
                title=title,
                type="homework",
                weight_pct=5.0,
                due_precision="tbd",
                confidence=0.9,
                source_span=f"{title} sentence",
            )
        )
        return document

    classmate = User(
        email=f"mate-{_uuid.uuid4().hex[:6]}@test.edu", institution_id=me.institution_id
    )
    db.add(classmate)
    db.flush()
    if not db.scalar(
        select(Enrollment).where(Enrollment.user_id == me.id, Enrollment.section_id == section.id)
    ):
        db.add(Enrollment(user_id=me.id, section_id=section.id, term_id=section.term_id))

    theirs = upload(classmate, "Classmate's copy")
    mine = upload(me, "My copy")
    db.commit()

    titles = [i["title"] for i in client.get("/api/v1/timeline", headers=auth).json()]
    assert "My copy" in titles
    assert "Classmate's copy" not in titles, "a classmate's upload is not this student's timeline"

    for document in (mine, theirs):
        db.query(Assessment).filter(Assessment.document_id == document.id).delete()
        db.query(SyllabusDocument).filter(SyllabusDocument.id == document.id).delete()
    db.query(User).filter(User.id == classmate.id).delete()
    db.commit()
