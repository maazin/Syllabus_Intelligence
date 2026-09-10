"""End-to-end pipeline tests — a real file in, a reviewable schedule out.

Uses the recorded extraction rather than a live model call, so the whole chain
downstream of the model is verified deterministically and offline. The model
call itself is covered by the golden-set eval harness (section 16), which is a
different kind of test with a different failure mode.

Several assertions here are Appendix C's definition-of-done items stated as
executable checks.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))
sys.path.insert(0, str(ROOT / "services" / "worker" / "tasks"))

from date_resolver.calendar_context import (  # noqa: E402
    AcademicCalendar,
    CalendarExceptionEntry,
    FinalExamMatrixEntry,
)
from pipeline import run_pipeline, to_scheduled_items  # noqa: E402
from schemas.extraction import ExtractionOutput  # noqa: E402
from workload_model.flags import compute_flags  # noqa: E402
from workload_model.heatmap import ScheduledItem, build_heatmap  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SYLLABI = FIXTURES / "syllabi"
RECORDED = FIXTURES / "recorded_extractions"


@pytest.fixture
def calendar() -> AcademicCalendar:
    payload = json.loads((FIXTURES / "academic_calendar.json").read_text())
    matrix = json.loads((FIXTURES / "final_exam_matrix.json").read_text())
    tz = ZoneInfo(payload["timezone"])
    return AcademicCalendar(
        term_name=payload["term_name"],
        start_date=date.fromisoformat(payload["start_date"]),
        end_date=date.fromisoformat(payload["end_date"]),
        timezone=payload["timezone"],
        default_due_time=time.fromisoformat(payload["default_due_time"]),
        exceptions=tuple(
            CalendarExceptionEntry(date.fromisoformat(e["date"]), e["label"], e["no_class"])
            for e in payload["exceptions"]
        ),
        finals_start=date.fromisoformat(payload["finals_start"]),
        finals_end=date.fromisoformat(payload["finals_end"]),
        final_exam_matrix=tuple(
            FinalExamMatrixEntry(
                r["meeting_pattern"],
                datetime.fromisoformat(r["exam_start_at"]).replace(tzinfo=tz),
                datetime.fromisoformat(r["exam_end_at"]).replace(tzinfo=tz),
            )
            for r in matrix["rows"]
        ),
    )


def _recorded(name: str):
    path = RECORDED / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"no recorded extraction for {name}")
    payload = ExtractionOutput.model_validate_json(path.read_text())
    return lambda document: payload


@pytest.fixture
def cs_result(calendar: AcademicCalendar):
    path = SYLLABI / "cs_lecture_native_pdf.pdf"
    if not path.is_file():
        pytest.skip("fixtures not generated; run scripts/generate_fixture_syllabi.py")
    return run_pipeline(
        path.name,
        path.read_bytes(),
        calendar,
        extractor=_recorded("cs_lecture_native_pdf"),
        meeting_pattern="MWF 10:00-10:50",
        course_label="COP 4530",
        credits=3.0,
        total_enrolled_credits=15.0,
        allow_ocr=False,
    )


# --- Appendix C: definition of done -------------------------------------------------------


def test_every_extracted_deadline_traces_to_a_verbatim_source_span(cs_result) -> None:
    """Appendix C item 2, and section 16's hard ceiling."""
    assert cs_result.hallucination_rate == 0.0
    for item in cs_result.review_items:
        assert item.assessment.source_span.strip()


def test_no_imprecise_item_shows_a_time_without_an_inference_marker(cs_result) -> None:
    """Appendix C item 3 — the rule that keeps the product trustworthy."""
    for item in cs_result.review_items:
        resolution = item.resolution
        if resolution.due_precision != "exact_datetime" and resolution.due_at is not None:
            assert resolution.time_inferred, f"{item.title} shows a time with no marker"


def test_final_exam_resolves_from_the_registrar_matrix(cs_result) -> None:
    """Appendix C item 4: the syllabus only says "see the university schedule"."""
    final = next(i for i in cs_result.review_items if i.assessment.type == "final_exam")
    assert final.resolution.due_precision == "exact_datetime"
    assert final.resolution.due_at is not None
    assert final.resolution.due_at.date() == date(2026, 12, 9)  # the MWF 10:00 slot
    assert final.resolution.notes is not None
    assert "matrix" in final.resolution.notes


def test_weekday_mismatch_is_surfaced_not_silently_resolved(cs_result) -> None:
    """The fixture says "Tuesday, October 14"; Oct 14 2026 is a Wednesday."""
    midterm = next(i for i in cs_result.review_items if i.title == "Midterm Exam 1")
    assert midterm.resolution.weekday_mismatch is True
    assert midterm.needs_explicit_action, "a mismatched date must not auto-accept"
    assert any(f.check == "weekday_mismatch" for f in cs_result.flags)


# --- pipeline mechanics --------------------------------------------------------------------


def test_pipeline_extracts_tables_and_the_full_grade_breakdown(cs_result) -> None:
    tables = [t for page in cs_result.document.pages for t in page.tables_markdown]
    assert len(tables) >= 2
    total = sum(c.weight_pct for c in cs_result.extraction.grade_breakdown)
    assert total == pytest.approx(100.0)


def test_review_queue_is_ordered_lowest_confidence_first(cs_result) -> None:
    """10.2: the riskiest items get attention first."""
    scores = [i.confidence for i in cs_result.review_order]
    assert scores == sorted(scores)


def test_high_confidence_items_are_the_collapsible_majority(cs_result) -> None:
    """10.2's progress indicator only reads as almost-done if most items auto-accept."""
    assert len(cs_result.auto_accepted) >= len(cs_result.review_items) // 2


def test_item_count_mismatch_is_flagged(cs_result) -> None:
    """The breakdown says 10 problem sets; the schedule enumerates 3."""
    assert any(f.check == "item_count_mismatch" for f in cs_result.flags)


def test_heatmap_covers_the_full_term_including_finals(cs_result, calendar) -> None:
    """9.5: a heatmap that stops at week 14 misses the densest period of the term."""
    assert cs_result.heatmap
    last_week = cs_result.heatmap[-1].week_start
    assert last_week >= calendar.finals_start - __import__("datetime").timedelta(days=7)


def test_tbd_items_never_become_scheduled_items(calendar) -> None:
    """9.3: tbd never appears on the calendar at all."""
    from pipeline import ReviewItem
    from schemas.extraction import AssessmentExtraction
    from schemas.resolution import ResolvedDate

    tbd = ReviewItem(
        assessment=AssessmentExtraction(
            title="Guest Lecture Response",
            type="reading_response",
            source_span="Date TBA.",
            confidence=0.8,
        ),
        resolution=ResolvedDate(due_precision="tbd", resolution_case="unspecified"),
        confidence=0.5,
        bucket="low",
    )
    assert to_scheduled_items([tbd], course_label="X", credits=3) == []


def test_week_only_items_schedule_as_a_band_not_a_day(calendar) -> None:
    from pipeline import ReviewItem
    from schemas.extraction import AssessmentExtraction
    from schemas.resolution import ResolvedDate

    item = ReviewItem(
        assessment=AssessmentExtraction(
            title="Reading Response 2",
            type="reading_response",
            source_span="| Week 5 | Hemingway | Reading Response 2 |",
            confidence=0.8,
        ),
        resolution=ResolvedDate(
            due_precision="week_only",
            resolution_case="week_number",
            week_start=date(2026, 9, 21),
            week_end=date(2026, 9, 28),
        ),
        confidence=0.7,
        bucket="medium",
    )
    scheduled = to_scheduled_items([item], course_label="ENG 3014", credits=3)
    assert len(scheduled) == 1
    assert scheduled[0].week_band == (date(2026, 9, 21), date(2026, 9, 28))


# --- Appendix C item 5: the hand-constructed collision scenario ----------------------------


def test_three_exams_in_four_days_across_courses_flags_red(calendar) -> None:
    """Appendix C item 5, at the multi-course scale the product actually runs at."""
    items = [
        ScheduledItem("m1", "Midterm", "COP 4530", "midterm", date(2026, 10, 12), 3, 20),
        ScheduledItem("m2", "Midterm", "MAC 2312", "midterm", date(2026, 10, 14), 4, 25),
        ScheduledItem("m3", "Midterm", "PHY 2048", "midterm", date(2026, 10, 15), 4, 20),
        ScheduledItem("p1", "Essay", "ENG 3014", "paper_short", date(2026, 10, 16), 3, 15),
    ]
    term_end = calendar.finals_end or calendar.end_date
    heatmap = build_heatmap(items, calendar.start_date, term_end, 15)
    flags = compute_flags(items, heatmap, calendar.start_date, term_end, 15)

    week = date(2026, 10, 12)
    assert week in flags
    assert flags[week][0].severity == "red"
    kinds = {f.kind for f in flags[week]}
    assert "major_collision" in kinds

    collision = next(f for f in flags[week] if f.kind == "major_collision")
    assert collision.assessment_ids
    # Weights from different courses are not summable into one "% of your grade",
    # so a cross-course collision counts courses instead.
    assert "across 3 courses" in collision.explanation
    assert "%" not in collision.explanation
