"""Layer 2 corpus and search — PRD section 14, US-8, US-9.

The centerpiece is `test_the_prds_named_query_works_end_to_end`. Section 14.3
names one specific query and says to verify it works before calling P2 done:

    "3-credit humanities elective, no group project, no attendance policy,
     papers instead of exams"

Everything else here defends the properties that make that answer trustworthy —
that profiles are never averaged across instructors, that unvalidated
extractions never get published, and that provenance is always stated.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5533/syllint"
)
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-for-hmac-sha256-min")

from db.models import (  # noqa: E402
    Assessment,
    Course,
    CourseProfile,
    GradeCategory,
    Institution,
    Instructor,
    Section,
    SectionPolicies,
    SyllabusDocument,
    Term,
    User,
)
from db.session import SessionLocal, engine  # noqa: E402

from services.api.deps import issue_access_token  # noqa: E402
from services.api.main import app  # noqa: E402
from services.pipeline.aggregate import (  # noqa: E402
    aggregate_term,
    assessment_mix,
    estimated_weekly_hours,
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
def client() -> TestClient:
    return TestClient(app)


def _course(db, institution, subject, number, title, credits, attributes) -> Course:
    course = Course(
        institution_id=institution.id,
        subject_code=subject,
        catalog_number=number,
        title=title,
        credits=credits,
        gened_attributes=attributes,
    )
    db.add(course)
    db.flush()
    return course


def _build_section(
    db,
    *,
    course: Course,
    term: Term,
    instructor: Instructor,
    uploader: User,
    assessments: list[tuple[str, str, float, bool]],
    categories: list[tuple[str, float]],
    attendance_graded: bool,
    group_work: bool,
    final_exam_type: str,
    materials_cost: float | None = None,
    verified: bool = True,
    visibility: str = "private",
) -> Section:
    section = Section(
        course_id=course.id,
        term_id=term.id,
        section_code=f"S{uuid.uuid4().hex[:4]}",
        instructor_id=instructor.id,
        meeting_pattern="TR 14:00-15:15",
        modality="in_person",
        seats_total=60,
        seats_open=7,
    )
    db.add(section)
    db.flush()

    document = SyllabusDocument(
        uploader_user_id=uploader.id,
        section_id=section.id,
        storage_key=f"documents/test/{uuid.uuid4().hex}",
        sha256=uuid.uuid4().hex * 2,
        mime="application/pdf",
        source="upload",
        visibility=visibility,
    )
    db.add(document)
    db.flush()

    now = datetime.now(UTC)
    for title, type_, weight, is_group in assessments:
        db.add(
            Assessment(
                document_id=document.id,
                section_id=section.id,
                title=title,
                type=type_,
                weight_pct=weight,
                is_group=is_group,
                due_precision="date_only",
                due_at=datetime(2026, 10, 14, 23, 59, tzinfo=UTC),
                source_span=f"{title} is described in the syllabus.",
                confidence=0.9,
                verified_at=now if verified else None,
            )
        )
    for name, weight in categories:
        db.add(
            GradeCategory(
                document_id=document.id,
                section_id=section.id,
                name=name,
                weight_pct=weight,
            )
        )
    db.add(
        SectionPolicies(
            section_id=section.id,
            attendance_graded=attendance_graded,
            group_work_present=group_work,
            final_exam_type=final_exam_type,
            est_materials_cost_usd=materials_cost,
        )
    )
    db.flush()
    return section


@pytest.fixture
def corpus(db):
    """A small catalog with contrasting courses, aggregated into profiles."""
    institution = db.scalar(select(Institution).where(Institution.domain == "test.edu"))
    term = db.scalar(select(Term).limit(1))
    if institution is None or term is None:
        pytest.skip("run scripts/seed_dev_data.py first")

    uploader = User(email=f"corpus-{uuid.uuid4().hex[:8]}@test.edu", institution_id=institution.id)
    db.add(uploader)
    db.flush()

    whitfield = Instructor(
        institution_id=institution.id,
        name="Prof. Whitfield",
        email=f"w{uuid.uuid4().hex[:6]}@x.edu",
    )
    okonkwo = Instructor(
        institution_id=institution.id, name="Dr. Okonkwo", email=f"o{uuid.uuid4().hex[:6]}@x.edu"
    )
    db.add_all([whitfield, okonkwo])
    db.flush()

    made = {}

    # The course the PRD's query is looking for: 3-credit humanities, papers,
    # no group project, no graded attendance.
    made["lit"] = _course(
        db, institution, "ENG", f"3{uuid.uuid4().hex[:3]}", "Modern Literature", 3, ["HUM"]
    )
    _build_section(
        db,
        course=made["lit"],
        term=term,
        instructor=whitfield,
        uploader=uploader,
        assessments=[
            ("Short Paper", "paper_short", 25, False),
            ("Final Paper", "paper_long", 45, False),
            ("Reading Response 1", "reading_response", 15, False),
            ("Reading Response 2", "reading_response", 15, False),
        ],
        categories=[("Papers", 70), ("Responses", 30)],
        attendance_graded=False,
        group_work=False,
        final_exam_type="none",
        materials_cost=45,
    )

    # Same department, but everything the query excludes.
    made["seminar"] = _course(
        db, institution, "HIS", f"3{uuid.uuid4().hex[:3]}", "Group Seminar", 3, ["HUM"]
    )
    _build_section(
        db,
        course=made["seminar"],
        term=term,
        instructor=okonkwo,
        uploader=uploader,
        assessments=[
            ("Group Project", "final_project", 40, True),
            ("Midterm", "midterm", 30, False),
            ("Final Exam", "final_exam", 30, False),
        ],
        categories=[("Project", 40), ("Exams", 60)],
        attendance_graded=True,
        group_work=True,
        final_exam_type="cumulative",
        materials_cost=200,
    )

    # A 4-credit STEM course, to prove the credits facet bites.
    made["stem"] = _course(
        db, institution, "PHY", f"2{uuid.uuid4().hex[:3]}", "Physics A", 4, ["PHYS"]
    )
    _build_section(
        db,
        course=made["stem"],
        term=term,
        instructor=okonkwo,
        uploader=uploader,
        assessments=[
            ("Midterm 1", "midterm", 25, False),
            ("Midterm 2", "midterm", 25, False),
            ("Final Exam", "final_exam", 50, False),
        ],
        categories=[("Exams", 100)],
        attendance_graded=True,
        group_work=False,
        final_exam_type="cumulative",
    )
    db.commit()

    aggregate_term(db, term.id)

    # Tests that build extra courses append them here so teardown can find them;
    # otherwise their sections keep the instructors alive and the delete fails.
    extra: list[Course] = []
    yield {
        "institution": institution,
        "term": term,
        "uploader": uploader,
        "courses": made,
        "extra": extra,
        "instructors": {"whitfield": whitfield, "okonkwo": okonkwo},
    }

    db.rollback()
    course_ids = [c.id for c in made.values()] + [c.id for c in extra]
    db.query(CourseProfile).filter(CourseProfile.course_id.in_(course_ids)).delete(
        synchronize_session=False
    )
    section_ids = [
        s.id for s in db.scalars(select(Section).where(Section.course_id.in_(course_ids))).all()
    ]
    document_ids = [
        d.id
        for d in db.scalars(
            select(SyllabusDocument).where(SyllabusDocument.section_id.in_(section_ids))
        ).all()
    ]
    db.query(Assessment).filter(Assessment.section_id.in_(section_ids)).delete(
        synchronize_session=False
    )
    db.query(GradeCategory).filter(GradeCategory.section_id.in_(section_ids)).delete(
        synchronize_session=False
    )
    db.query(SectionPolicies).filter(SectionPolicies.section_id.in_(section_ids)).delete(
        synchronize_session=False
    )
    db.query(SyllabusDocument).filter(SyllabusDocument.id.in_(document_ids)).delete(
        synchronize_session=False
    )
    db.query(Section).filter(Section.id.in_(section_ids)).delete(synchronize_session=False)
    db.query(Course).filter(Course.id.in_(course_ids)).delete(synchronize_session=False)
    db.query(Instructor).filter(Instructor.id.in_([whitfield.id, okonkwo.id])).delete(
        synchronize_session=False
    )
    db.query(User).filter(User.id == uploader.id).delete()
    db.commit()


@pytest.fixture
def auth(corpus, db):
    user = db.scalar(select(User).where(User.email == corpus["uploader"].email))
    return {"Authorization": f"Bearer {issue_access_token(user.id)}"}


# --- 14.3's named acceptance query --------------------------------------------------------


def test_the_prds_named_query_works_end_to_end(client, auth, corpus) -> None:
    """Section 14.3: verify this specific query before calling P2 done.

    "3-credit humanities elective, no group project, no attendance policy,
     papers instead of exams"
    """
    response = client.get(
        "/api/v1/courses/search",
        params={
            "credits": 3,
            "gened_attribute": "HUM",
            "has_group_project": False,
            "attendance_graded": False,
            "assessment_mix": "paper_heavy",
        },
        headers=auth,
    )
    assert response.status_code == 200
    results = response.json()["results"]

    titles = {r["title"] for r in results}
    assert "Modern Literature" in titles, "the matching course was not returned"
    assert "Group Seminar" not in titles, "a course with a group project slipped through"
    assert "Physics A" not in titles, "a 4-credit exam-heavy course slipped through"

    match = next(r for r in results if r["title"] == "Modern Literature")
    assert match["has_group_project"] is False
    assert match["attendance_graded"] is False
    assert match["assessment_mix"] == "paper_heavy"
    assert match["credits"] == 3


# --- aggregation (14.1, 14.2) -----------------------------------------------------------


def test_assessment_mix_labels_by_dominant_weight() -> None:
    def _fake(type_: str, weight: float):
        return Assessment(title="x", type=type_, weight_pct=weight, source_span="s")

    papers = assessment_mix([_fake("paper_long", 60), _fake("reading_response", 40)])
    assert papers["label"] == "paper_heavy"

    exams = assessment_mix([_fake("midterm", 40), _fake("final_exam", 60)])
    assert exams["label"] == "exam_heavy"


def test_a_course_with_no_dominant_family_reads_as_continuous() -> None:
    """ "Constant small deadlines" is a real answer, not a failure to classify."""

    def _fake(type_: str, weight: float):
        return Assessment(title="x", type=type_, weight_pct=weight, source_span="s")

    spread = assessment_mix(
        [
            _fake("midterm", 25),
            _fake("paper_short", 25),
            _fake("final_project", 25),
            _fake("problem_set", 25),
        ]
    )
    assert spread["label"] == "continuous"


def test_weekly_hours_includes_the_baseline_floor() -> None:
    """Must match how the personal heatmap computes it, or the product
    contradicts itself between search and timeline (11.3)."""
    hours = estimated_weekly_hours([], credits=3, term_weeks=15)
    assert hours == pytest.approx(6.0)  # 3 credits x 2, no assessments


def test_profiles_are_not_averaged_across_instructors(client, auth, corpus, db) -> None:
    """14.1: instructor variance is the entire point of the feature."""
    profiles = db.scalars(
        select(CourseProfile).where(
            CourseProfile.course_id.in_([c.id for c in corpus["courses"].values()])
        )
    ).all()
    keys = {(p.course_id, p.instructor_id, p.term_id) for p in profiles}
    assert len(keys) == len(profiles), "profiles collapsed across instructors"


def test_unreviewed_extractions_are_not_published(db, corpus) -> None:
    """14.2: at least one document must have completed human review."""
    institution, term = corpus["institution"], corpus["term"]
    course = _course(db, institution, "ART", f"1{uuid.uuid4().hex[:3]}", "Unreviewed Course", 3, [])
    corpus["extra"].append(course)
    _build_section(
        db,
        course=course,
        term=term,
        instructor=corpus["instructors"]["whitfield"],
        uploader=corpus["uploader"],
        assessments=[("Paper", "paper_long", 100, False)],
        categories=[("Papers", 100)],
        attendance_graded=False,
        group_work=False,
        final_exam_type="none",
        verified=False,  # nobody reviewed it
    )
    db.commit()

    report = aggregate_term(db, term.id)
    assert report.reasons["no_human_review"] >= 1
    assert db.scalar(select(CourseProfile).where(CourseProfile.course_id == course.id)) is None


def test_invalid_grade_weights_block_publication(db, corpus) -> None:
    """14.2: the grade weights must validate. An unvalidated profile is worse
    than no profile — the search product's credibility rests on the numbers."""
    institution, term = corpus["institution"], corpus["term"]
    course = _course(db, institution, "ART", f"2{uuid.uuid4().hex[:3]}", "Bad Weights", 3, [])
    corpus["extra"].append(course)
    _build_section(
        db,
        course=course,
        term=term,
        instructor=corpus["instructors"]["whitfield"],
        uploader=corpus["uploader"],
        assessments=[("Paper", "paper_long", 60, False)],
        categories=[("Papers", 60)],  # sums to 60, not 100
        attendance_graded=False,
        group_work=False,
        final_exam_type="none",
    )
    db.commit()

    report = aggregate_term(db, term.id)
    assert report.reasons["weights_do_not_validate"] >= 1
    assert db.scalar(select(CourseProfile).where(CourseProfile.course_id == course.id)) is None


def test_instructor_opt_out_withdraws_the_profile(db, corpus) -> None:
    """15.1: one click, honored within 24 hours, no argument."""
    institution, term = corpus["institution"], corpus["term"]
    course = _course(db, institution, "ART", f"3{uuid.uuid4().hex[:3]}", "Opted Out", 3, [])
    corpus["extra"].append(course)
    _build_section(
        db,
        course=course,
        term=term,
        instructor=corpus["instructors"]["whitfield"],
        uploader=corpus["uploader"],
        assessments=[("Paper", "paper_long", 100, False)],
        categories=[("Papers", 100)],
        attendance_graded=False,
        group_work=False,
        final_exam_type="none",
        visibility="opted_out",
    )
    db.commit()

    aggregate_term(db, term.id)
    assert db.scalar(select(CourseProfile).where(CourseProfile.course_id == course.id)) is None


# --- provenance and the empty state (14.2, US-8) --------------------------------------------


def test_every_profile_states_its_provenance(client, auth) -> None:
    """14.2: "Based on the Fall 2025 syllabus, taught by X." """
    results = client.get("/api/v1/courses/search", headers=auth).json()["results"]
    assert results
    for entry in results:
        assert entry["provenance"]
        if entry["known"]:
            assert "syllabus" in entry["provenance"]
            assert entry["term"] and entry["instructor"]


def test_unprofiled_courses_still_appear(client, auth) -> None:
    """US-8: do not hide them; the empty state is the acquisition surface."""
    results = client.get("/api/v1/courses/search", headers=auth).json()["results"]
    unknown = [r for r in results if not r["known"]]
    assert unknown, "no unprofiled courses surfaced at all"
    assert "upload one" in unknown[0]["provenance"]


def test_profile_only_filters_exclude_unprofiled_courses(client, auth) -> None:
    """A course with no data has not been *shown* to have no group project.

    Returning it for `has_group_project=false` would be a claim the corpus
    cannot support.
    """
    results = client.get(
        "/api/v1/courses/search", params={"has_group_project": False}, headers=auth
    ).json()["results"]
    assert all(r["known"] for r in results)


# --- US-9: instructor comparison ---------------------------------------------------------


def test_course_profiles_endpoint_lists_instructors_most_recent_first(client, auth, corpus) -> None:
    course_id = corpus["courses"]["lit"].id
    response = client.get(f"/api/v1/courses/{course_id}/profiles", headers=auth)
    assert response.status_code == 200
    profiles = response.json()
    assert profiles and profiles[0]["known"]
    assert profiles[0]["instructor"] == "Prof. Whitfield"
    # Registrar detail is attached where a section exists (14.3).
    assert profiles[0]["meeting_pattern"] == "TR 14:00-15:15"
    assert profiles[0]["seats_open"] == 7


def test_a_course_with_no_profile_returns_an_unknown_placeholder(client, auth, db, corpus) -> None:
    course = _course(
        db, corpus["institution"], "ART", f"4{uuid.uuid4().hex[:3]}", "Never Uploaded", 3, []
    )
    db.commit()
    try:
        profiles = client.get(f"/api/v1/courses/{course.id}/profiles", headers=auth).json()
        assert len(profiles) == 1
        assert profiles[0]["known"] is False
    finally:
        db.query(Course).filter(Course.id == course.id).delete()
        db.commit()


# --- other facets --------------------------------------------------------------------------


def test_weekly_hours_facet_filters(client, auth) -> None:
    generous = client.get(
        "/api/v1/courses/search", params={"max_weekly_hours": 200}, headers=auth
    ).json()["results"]
    strict = client.get(
        "/api/v1/courses/search", params={"max_weekly_hours": 0.1}, headers=auth
    ).json()["results"]
    assert len(strict) < len(generous)


def test_materials_cost_facet_filters(client, auth) -> None:
    results = client.get(
        "/api/v1/courses/search", params={"max_materials_cost": 50}, headers=auth
    ).json()["results"]
    titles = {r["title"] for r in results}
    assert "Modern Literature" in titles  # $45
    assert "Group Seminar" not in titles  # $200


def test_final_exam_type_facet_filters(client, auth) -> None:
    results = client.get(
        "/api/v1/courses/search", params={"final_exam_type": "none"}, headers=auth
    ).json()["results"]
    assert results
    assert all(r["final_exam_type"] == "none" for r in results)
