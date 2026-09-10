"""Validation and confidence tests — PRD sections 9.4, 10.1, and 16.

`source_span_is_findable` gets the most attention here because it is the only
cheap, exact test for the one metric section 16 gives a hard ceiling: a
fabricated deadline is a product-killing bug, and everything else in the
confidence stack is downstream of catching it.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from schemas.extraction import (
    AssessmentExtraction,
    CourseMetadata,
    ExtractionOutput,
    GradeBreakdownItem,
)
from schemas.resolution import ResolvedDate
from validate import (
    composite_confidence,
    confidence_bucket,
    hallucination_rate,
    source_span_is_findable,
    validate_extraction,
)

TZ = ZoneInfo("America/New_York")

SOURCE = """
[page 1]
COP 4530 Data Structures, Fall 2026
Midterm 1 will be held in class on Tuesday, October 14.
Problem sets are due every Friday at 11:59pm.
The final exam is cumulative — see the university final exam schedule.
"""


def _assessment(**kwargs) -> AssessmentExtraction:
    defaults = dict(
        title="Midterm 1",
        type="midterm",
        category_ref="Exams",
        weight_pct=20.0,
        date_expression_raw="Tuesday, October 14",
        source_span="Midterm 1 will be held in class on Tuesday, October 14.",
        confidence=0.94,
    )
    defaults.update(kwargs)
    return AssessmentExtraction(**defaults)


def _output(assessments=None, breakdown=None) -> ExtractionOutput:
    return ExtractionOutput(
        course=CourseMetadata(subject_code="COP", catalog_number="4530"),
        grade_breakdown=breakdown or [],
        assessments=assessments or [],
    )


# --- hallucination detection (section 16's hard gate) ---------------------------------


def test_verbatim_span_is_findable() -> None:
    assert source_span_is_findable("Midterm 1 will be held in class", SOURCE)


def test_invented_span_is_not_findable() -> None:
    """The whole point: if the model cannot quote it, the model made it up."""
    assert not source_span_is_findable("Midterm 2 will be held on November 3.", SOURCE)


def test_findability_tolerates_formatting_noise_but_not_invention() -> None:
    """Curly quotes and en dashes are PDF artifacts, not hallucination."""
    assert source_span_is_findable("The final exam is cumulative — see the", SOURCE)
    assert source_span_is_findable("The final exam is cumulative - see the", SOURCE)
    assert source_span_is_findable("Problem  sets   are due every Friday", SOURCE)
    # ...but a real content change still fails.
    assert not source_span_is_findable("Problem sets are due every Monday", SOURCE)


def test_empty_span_is_never_findable() -> None:
    assert not source_span_is_findable("", SOURCE)
    assert not source_span_is_findable("   ", SOURCE)


def test_hallucination_rate_is_a_share_of_items() -> None:
    good = _assessment()
    bad = _assessment(title="Ghost Exam", source_span="Ghost Exam is on December 1.")
    assert hallucination_rate([good, good], SOURCE) == 0.0
    assert hallucination_rate([good, bad], SOURCE) == 0.5
    assert hallucination_rate([], SOURCE) == 0.0


def test_unfindable_span_forces_low_confidence_regardless_of_self_report() -> None:
    """A confidently-wrong item is exactly the case the hard gate exists for."""
    fabricated = _assessment(
        title="Ghost Exam", source_span="Ghost Exam is on December 1.", confidence=0.99
    )
    score = composite_confidence(fabricated, source_text=SOURCE)
    assert confidence_bucket(score) == "low"


# --- 9.4 validation rules ----------------------------------------------------------------


def test_weights_summing_to_100_pass() -> None:
    breakdown = [
        GradeBreakdownItem(category="Exams", weight_pct=40),
        GradeBreakdownItem(category="Problem Sets", weight_pct=20),
        GradeBreakdownItem(category="Projects", weight_pct=25),
        GradeBreakdownItem(category="Final", weight_pct=15),
    ]
    flags = validate_extraction(
        _output([_assessment()], breakdown), page_count=2, source_text=SOURCE
    )
    assert not any(f.check == "weights_out_of_range" for f in flags)


@pytest.mark.parametrize("total", [85, 97.9, 102.1, 115])
def test_weights_outside_tolerance_are_flagged(total: float) -> None:
    """The strongest single quality signal available (9.4)."""
    breakdown = [GradeBreakdownItem(category="Everything", weight_pct=total)]
    flags = validate_extraction(
        _output([_assessment()], breakdown), page_count=2, source_text=SOURCE
    )
    assert any(f.check == "weights_out_of_range" for f in flags)


@pytest.mark.parametrize("total", [98, 100, 102])
def test_weight_tolerance_boundaries_are_inclusive(total: float) -> None:
    breakdown = [GradeBreakdownItem(category="Everything", weight_pct=total)]
    flags = validate_extraction(
        _output([_assessment()], breakdown), page_count=2, source_text=SOURCE
    )
    assert not any(f.check == "weights_out_of_range" for f in flags)


def test_weights_are_reported_as_is_not_normalized() -> None:
    """8.3: report as-is, flag it, never silently normalize."""
    breakdown = [
        GradeBreakdownItem(category="Exams", weight_pct=50),
        GradeBreakdownItem(category="Papers", weight_pct=40),
    ]
    output = _output([_assessment()], breakdown)
    validate_extraction(output, page_count=2, source_text=SOURCE)
    assert [c.weight_pct for c in output.grade_breakdown] == [50, 40]


def test_item_count_mismatch_is_flagged() -> None:
    breakdown = [
        GradeBreakdownItem(category="Problem Sets", weight_pct=100, item_count=10),
    ]
    assessments = [
        _assessment(title=f"PS {i}", type="problem_set", category_ref="Problem Sets")
        for i in range(3)
    ]
    flags = validate_extraction(_output(assessments, breakdown), page_count=2, source_text=SOURCE)
    mismatch = [f for f in flags if f.check == "item_count_mismatch"]
    assert mismatch and mismatch[0].category == "Problem Sets"


def test_zero_assessments_from_multipage_document_is_a_hard_failure() -> None:
    flags = validate_extraction(_output([]), page_count=5, source_text=SOURCE)
    hard = [f for f in flags if f.check == "zero_assessments_extracted"]
    assert hard and hard[0].severity == "hard_failure"


def test_zero_assessments_from_a_one_page_document_is_not_a_hard_failure() -> None:
    """A one-page cover sheet legitimately has no schedule."""
    flags = validate_extraction(_output([]), page_count=1, source_text=SOURCE)
    assert not any(f.check == "zero_assessments_extracted" for f in flags)


def test_overweight_non_final_item_is_flagged() -> None:
    heavy = _assessment(title="Quiz 1", type="quiz", weight_pct=75)
    flags = validate_extraction(_output([heavy]), page_count=2, source_text=SOURCE)
    assert any(f.check == "weight_over_threshold" for f in flags)


def test_a_heavy_final_is_not_flagged() -> None:
    """A 70% final project is unusual but legitimate; a 70% quiz is a misparse."""
    final = _assessment(title="Final Project", type="final_project", weight_pct=70)
    flags = validate_extraction(_output([final]), page_count=2, source_text=SOURCE)
    assert not any(f.check == "weight_over_threshold" for f in flags)


def test_date_outside_term_is_flagged_but_the_item_is_kept() -> None:
    """9.4 says flag the item, do not drop it."""
    output = _output([_assessment()])
    resolved = {
        "Midterm 1": ResolvedDate(
            due_at=datetime(2027, 3, 1, 23, 59, tzinfo=TZ),
            due_precision="date_only",
            resolution_case="full_date",
        )
    }
    flags = validate_extraction(
        output,
        page_count=2,
        source_text=SOURCE,
        resolved=resolved,
        term_start=date(2026, 8, 24),
        term_end=date(2026, 12, 11),
    )
    assert any(f.check == "date_outside_term" for f in flags)
    assert len(output.assessments) == 1  # kept


def test_weekday_mismatch_surfaces_as_a_flag() -> None:
    output = _output([_assessment()])
    resolved = {
        "Midterm 1": ResolvedDate(
            due_at=datetime(2026, 10, 14, 23, 59, tzinfo=TZ),
            due_precision="date_only",
            resolution_case="weekday_plus_date",
            weekday_mismatch=True,
            notes="Source says Tuesday, but 2026-10-14 is a Wednesday.",
        )
    }
    flags = validate_extraction(output, page_count=2, source_text=SOURCE, resolved=resolved)
    mismatch = [f for f in flags if f.check == "weekday_mismatch"]
    assert mismatch
    assert "Tuesday" in mismatch[0].message and "Wednesday" in mismatch[0].message


def test_two_exams_within_five_days_flags_a_likely_misparse() -> None:
    output = _output(
        [
            _assessment(title="Midterm 1"),
            _assessment(title="Midterm 2"),
        ]
    )
    resolved = {
        "Midterm 1": ResolvedDate(
            due_at=datetime(2026, 10, 14, 23, 59, tzinfo=TZ),
            due_precision="date_only",
            resolution_case="full_date",
        ),
        "Midterm 2": ResolvedDate(
            due_at=datetime(2026, 10, 16, 23, 59, tzinfo=TZ),
            due_precision="date_only",
            resolution_case="full_date",
        ),
    }
    flags = validate_extraction(output, page_count=2, source_text=SOURCE, resolved=resolved)
    assert any(f.check == "exams_too_close" for f in flags)


# --- 10.1 composite confidence ---------------------------------------------------------------


def test_imprecise_resolution_scores_lower_than_exact() -> None:
    """10.1: a week_only resolution scores lower than an exact_datetime."""
    item = _assessment()
    exact = composite_confidence(
        item,
        source_text=SOURCE,
        resolution=ResolvedDate(
            due_at=datetime(2026, 10, 14, 14, 0, tzinfo=TZ),
            due_precision="exact_datetime",
            resolution_case="full_date",
        ),
    )
    week = composite_confidence(
        item,
        source_text=SOURCE,
        resolution=ResolvedDate(
            due_precision="week_only",
            resolution_case="week_number",
            week_start=date(2026, 9, 28),
            week_end=date(2026, 10, 5),
        ),
    )
    assert week < exact


def test_failing_validation_lowers_confidence() -> None:
    item = _assessment()
    clean = composite_confidence(item, source_text=SOURCE, survived_validation=True)
    flagged = composite_confidence(item, source_text=SOURCE, survived_validation=False)
    assert flagged < clean


def test_peer_agreement_pulls_the_score() -> None:
    """The only signal grounded outside this one document (10.1)."""
    item = _assessment(confidence=0.7)
    agreeing = composite_confidence(item, source_text=SOURCE, peer_agreement=1.0)
    disagreeing = composite_confidence(item, source_text=SOURCE, peer_agreement=0.0)
    assert disagreeing < 0.7 < agreeing


def test_confidence_stays_in_range() -> None:
    item = _assessment(confidence=1.0)
    assert 0.0 <= composite_confidence(item, source_text=SOURCE, peer_agreement=1.0) <= 1.0


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.99, "high"),
        (0.85, "high"),
        (0.84, "medium"),
        (0.60, "medium"),
        (0.59, "low"),
        (0.1, "low"),
    ],
)
def test_confidence_buckets(score: float, expected: str) -> None:
    assert confidence_bucket(score) == expected
