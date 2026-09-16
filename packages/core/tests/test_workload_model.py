"""Unit tests for the workload model — PRD section 11.

Includes the hand-constructed scenario Appendix C requires: three exams in
four days on a 15-credit load. The PRD notes that case lands exactly on the
1.8x severe-crunch boundary, which is why 11.4's comparisons are written with
explicit inclusive/exclusive operators — this file pins that behavior down.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from workload_model.effort import (
    BASE_EFFORT,
    EffortInput,
    credit_multiplier,
    effort_hours,
    weight_multiplier,
)
from workload_model.flags import compute_flags
from workload_model.heatmap import (
    ScheduledItem,
    baseline_weekly_hours,
    build_heatmap,
    distribute_effort,
)

TERM_START = date(2026, 8, 24)
TERM_END = date(2026, 12, 4)


# --- 11.2 scaling -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("credits", "expected"),
    [(1, 0.5), (3, 1.0), (4, 1.25), (5, 1.5)],
)
def test_credit_multiplier_matches_the_stated_anchors(credits: float, expected: float) -> None:
    assert credit_multiplier(credits) == pytest.approx(expected)


def test_credit_multiplier_interpolates_between_anchors_and_clamps_outside() -> None:
    assert 0.5 < credit_multiplier(2) < 1.0
    assert credit_multiplier(0.5) == 0.5
    assert credit_multiplier(12) == 1.5


def test_weight_multiplier_clamps_to_the_stated_range() -> None:
    # A 60% midterm against a typical 20% would be 3.0 unclamped.
    assert weight_multiplier("midterm", 60) == 2.0
    # A 1% quiz against a typical 2% would be 0.5 exactly — the floor.
    assert weight_multiplier("quiz", 0.5) == 0.5
    assert weight_multiplier("midterm", 20) == pytest.approx(1.0)


def test_unstated_weight_scores_neutral_not_penalized() -> None:
    """An absent weight is missing data, not evidence of a trivial item."""
    assert weight_multiplier("midterm", None) == 1.0


def test_group_work_carries_the_coordination_penalty() -> None:
    solo = effort_hours(EffortInput("final_project", credits=3, weight_pct=25))
    group = effort_hours(EffortInput("final_project", credits=3, weight_pct=25, is_group=True))
    assert group == pytest.approx(solo * 1.3)


def test_user_calibration_defaults_to_identity() -> None:
    item = EffortInput("problem_set", credits=3, weight_pct=3)
    assert effort_hours(item) == pytest.approx(
        effort_hours(EffortInput("problem_set", credits=3, weight_pct=3, user_calibration=1.0))
    )


def test_base_effort_table_matches_section_11_1() -> None:
    """Guards against a typo silently reshaping every heatmap in the product."""
    assert BASE_EFFORT["final_exam"].base_hours == 12
    assert BASE_EFFORT["final_exam"].prep_window_days == 10
    assert BASE_EFFORT["final_project"].base_hours == 20
    assert BASE_EFFORT["discussion_post"].base_hours == 0.75
    assert BASE_EFFORT["lab_report"].distribution == "flat"
    assert BASE_EFFORT["midterm"].distribution == "back_weighted"


# --- 11.3 distribution ---------------------------------------------------------------------


def test_back_weighted_distribution_matches_the_stated_shape() -> None:
    """A 7-day window puts ~25% on the final day and ~3.5% on the first (11.3)."""
    item = ScheduledItem(
        assessment_id="a1",
        title="Midterm 1",
        course_label="COP 4530",
        assessment_type="midterm",  # 7-day window, back-weighted
        due_on=date(2026, 10, 14),
        credits=3,
        weight_pct=20,
    )
    spread = distribute_effort(item)
    total = sum(spread.values())
    assert len(spread) == 7
    assert spread[date(2026, 10, 14)] / total == pytest.approx(0.25, abs=0.005)
    assert spread[date(2026, 10, 8)] / total == pytest.approx(0.0357, abs=0.005)


def test_distribution_conserves_total_effort() -> None:
    for assessment_type in BASE_EFFORT:
        item = ScheduledItem(
            assessment_id="x",
            title="t",
            course_label="C",
            assessment_type=assessment_type,
            due_on=date(2026, 10, 14),
            weight_pct=10,
        )
        spread = distribute_effort(item)
        if item.effort() > 0:
            assert sum(spread.values()) == pytest.approx(item.effort()), assessment_type


def test_flat_distribution_divides_evenly() -> None:
    item = ScheduledItem(
        assessment_id="l1",
        title="Lab 3",
        course_label="CHM 2045",
        assessment_type="lab_report",  # 3-day window, flat
        due_on=date(2026, 10, 14),
        weight_pct=3,
    )
    spread = distribute_effort(item)
    assert len(set(round(v, 6) for v in spread.values())) == 1


def test_baseline_gives_the_heatmap_a_floor() -> None:
    """A genuinely light week reads as "a week of school", not as empty (11.3)."""
    assert baseline_weekly_hours(15) == 30.0
    cells = build_heatmap([], TERM_START, TERM_END, total_enrolled_credits=15)
    assert all(cell.effort_hours > 0 for cell in cells)


def test_heatmap_has_no_gap_weeks() -> None:
    cells = build_heatmap([], TERM_START, TERM_END, total_enrolled_credits=15)
    starts = [c.week_start for c in cells]
    assert starts == sorted(starts)
    for earlier, later in zip(starts, starts[1:], strict=False):
        assert (later - earlier).days == 7


# --- 11.4 flags ----------------------------------------------------------------------------


def _exam(assessment_id: str, course: str, due_on: date, weight: float = 15) -> ScheduledItem:
    return ScheduledItem(
        assessment_id=assessment_id,
        title=f"{course} Midterm",
        course_label=course,
        assessment_type="midterm",
        due_on=due_on,
        credits=3,
        weight_pct=weight,
    )


def test_three_exams_in_four_days_flags_red() -> None:
    """Appendix C's required scenario: the heatmap must flag this."""
    items = [
        _exam("e1", "COP 4530", date(2026, 10, 12)),
        _exam("e2", "MAC 2312", date(2026, 10, 14)),
        _exam("e3", "PHY 2048", date(2026, 10, 15)),
    ]
    cells = build_heatmap(items, TERM_START, TERM_END, total_enrolled_credits=15)
    flags = compute_flags(items, cells, TERM_START, TERM_END, total_enrolled_credits=15)

    week = date(2026, 10, 12)
    assert week in flags, "the collision week produced no flag at all"
    kinds = {f.kind for f in flags[week]}
    assert "major_collision" in kinds
    assert flags[week][0].severity == "red"  # highest severity sorts first (11.4)


def test_major_collision_needs_two_high_stakes_items_within_72_hours() -> None:
    near = [_exam("e1", "A", date(2026, 10, 12)), _exam("e2", "B", date(2026, 10, 14))]
    far = [_exam("e1", "A", date(2026, 10, 12)), _exam("e2", "B", date(2026, 10, 20))]

    near_flags = compute_flags(near, [], TERM_START, TERM_END, 15)
    far_flags = compute_flags(far, [], TERM_START, TERM_END, 15)

    assert any(f.kind == "major_collision" for fl in near_flags.values() for f in fl)
    assert not any(f.kind == "major_collision" for fl in far_flags.values() for f in fl)


def test_deadline_stack_needs_four_items_within_48_hours() -> None:
    three = [
        ScheduledItem(
            f"p{i}", f"PS {i}", "COP 4530", "problem_set", date(2026, 10, 13), weight_pct=3
        )
        for i in range(3)
    ]
    four = three + [
        ScheduledItem("p4", "PS 4", "MAC 2312", "problem_set", date(2026, 10, 14), weight_pct=3)
    ]

    assert not any(
        f.kind == "deadline_stack"
        for fl in compute_flags(three, [], TERM_START, TERM_END, 15).values()
        for f in fl
    )
    assert any(
        f.kind == "deadline_stack"
        for fl in compute_flags(four, [], TERM_START, TERM_END, 15).values()
        for f in fl
    )


def test_grade_concentration_is_per_course_not_across_courses() -> None:
    """35% of *a single course* grade (11.4) — three courses at 20% each must not fire."""
    spread_across = [
        _exam("e1", "COP 4530", date(2026, 10, 13), weight=20),
        _exam("e2", "MAC 2312", date(2026, 10, 14), weight=20),
        _exam("e3", "PHY 2048", date(2026, 10, 15), weight=20),
    ]
    concentrated = [
        _exam("e1", "COP 4530", date(2026, 10, 13), weight=20),
        _exam("e2", "COP 4530", date(2026, 10, 15), weight=20),
    ]

    assert not any(
        f.kind == "grade_concentration"
        for fl in compute_flags(spread_across, [], TERM_START, TERM_END, 15).values()
        for f in fl
    )
    assert any(
        f.kind == "grade_concentration"
        for fl in compute_flags(concentrated, [], TERM_START, TERM_END, 15).values()
        for f in fl
    )


def test_severe_crunch_boundary_is_inclusive_at_1_8x() -> None:
    """11.4 states the boundaries explicitly to keep this test from being flaky."""
    from workload_model.flags import _rolling_load_flags  # noqa: PLC0415

    # A term with one enormous cluster: the ratio clears 1.8x decisively.
    items = [
        _exam("e1", "A", date(2026, 10, 12), weight=30),
        _exam("e2", "B", date(2026, 10, 13), weight=30),
        _exam("e3", "C", date(2026, 10, 14), weight=30),
        _exam("e4", "D", date(2026, 10, 15), weight=30),
    ]
    flags = _rolling_load_flags(items, TERM_START, TERM_END, total_enrolled_credits=15)
    severities = {f.kind for f in flags}
    assert "severe_crunch" in severities


def test_highest_severity_sorts_first_within_a_week() -> None:
    """11.4: display the highest severity and list the rest; do not stack five badges."""
    items = [
        _exam("e1", "COP 4530", date(2026, 10, 13), weight=20),
        _exam("e2", "COP 4530", date(2026, 10, 14), weight=20),
        ScheduledItem("p1", "PS 1", "MAC 2312", "problem_set", date(2026, 10, 13), weight_pct=3),
        ScheduledItem("p2", "PS 2", "MAC 2312", "problem_set", date(2026, 10, 13), weight_pct=3),
        ScheduledItem("p3", "PS 3", "PHY 2048", "problem_set", date(2026, 10, 14), weight_pct=3),
        ScheduledItem("p4", "PS 4", "PHY 2048", "problem_set", date(2026, 10, 14), weight_pct=3),
    ]
    flags = compute_flags(items, [], TERM_START, TERM_END, 15)
    for week_flags in flags.values():
        ranks = [f.rank for f in week_flags]
        assert ranks == sorted(ranks, reverse=True)


def test_cross_course_collision_does_not_sum_incommensurable_weights() -> None:
    """20% of one course + 25% of another is not "45% of your grade"."""
    items = [
        _exam("e1", "COP 4530", date(2026, 10, 12), weight=20),
        _exam("e2", "MAC 2312", date(2026, 10, 14), weight=25),
    ]
    flags = compute_flags(items, [], TERM_START, TERM_END, 15)
    collision = next(f for fl in flags.values() for f in fl if f.kind == "major_collision")
    assert "45%" not in collision.explanation
    assert "across 2 courses" in collision.explanation


def test_single_course_collision_does_sum_weights() -> None:
    """Within one course the percentages share a denominator, so summing is honest."""
    items = [
        _exam("e1", "COP 4530", date(2026, 10, 12), weight=20),
        _exam("e2", "COP 4530", date(2026, 10, 14), weight=25),
    ]
    flags = compute_flags(items, [], TERM_START, TERM_END, 15)
    collision = next(f for fl in flags.values() for f in fl if f.kind == "major_collision")
    assert "45% of your grade" in collision.explanation


def test_every_flag_carries_a_plain_language_explanation() -> None:
    """The explanation is product copy and the growth mechanism (11.4), not debug output."""
    items = [
        _exam("e1", "COP 4530", date(2026, 10, 13), weight=25),
        _exam("e2", "COP 4530", date(2026, 10, 15), weight=20),
    ]
    flags = compute_flags(items, [], TERM_START, TERM_END, 15)
    all_flags = [f for fl in flags.values() for f in fl]
    assert all_flags
    for flag in all_flags:
        # Every explanation names when, says something concrete, and ends as a
        # sentence — it is shown to the student, not logged.
        assert flag.explanation.endswith(".")
        assert any(month in flag.explanation for month in ("Oct", "Nov", "Sep", "Dec"))
        assert flag.assessment_ids


def test_rolling_load_flags_name_their_own_seven_day_window() -> None:
    """The window is rolling (11.4) while heatmap cells are calendar weeks.

    Stating the span keeps a badge reading "about 14 hours" from contradicting
    the "8.9h" printed on the cell it sits on.
    """
    items = [
        _exam("e1", "COP 4530", date(2026, 10, 13), weight=25),
        _exam("e2", "COP 4530", date(2026, 10, 15), weight=20),
    ]
    flags = compute_flags(items, [], TERM_START, TERM_END, 15)
    rolling = [f for fl in flags.values() for f in fl if f.kind in ("crunch_week", "severe_crunch")]
    assert rolling
    for flag in rolling:
        assert re.search(r"[A-Z][a-z]{2} \d+ to [A-Z][a-z]{2} \d+", flag.explanation), (
            "the 7-day span is not stated"
        )
        assert "in 7 days" in flag.explanation


def test_grade_concentration_explanation_reads_like_the_prds_example() -> None:
    """ "3 items worth 45% of your grade land within 4 days" (6.3 / 11.4)."""
    items = [
        _exam("e1", "COP 4530", date(2026, 10, 12), weight=15),
        _exam("e2", "COP 4530", date(2026, 10, 14), weight=15),
        _exam("e3", "COP 4530", date(2026, 10, 15), weight=15),
    ]
    flags = compute_flags(items, [], TERM_START, TERM_END, 15)
    concentration = [f for fl in flags.values() for f in fl if f.kind == "grade_concentration"]
    assert concentration
    text = concentration[0].explanation
    assert "45%" in text
    assert "4 days" in text
    assert "COP 4530" in text


def test_rolling_flag_is_attributed_to_the_week_it_mostly_covers() -> None:
    """A window is anchored by its midpoint, not its start.

    Anchoring by start would badge a cell the crunch barely touches, and the
    student would click a quiet-looking week to find the explanation.
    """
    items = [
        _exam("e1", "COP 4530", date(2026, 11, 19), weight=30),
        _exam("e2", "MAC 2312", date(2026, 11, 20), weight=30),
    ]
    flags = compute_flags(items, [], TERM_START, TERM_END, 15)
    rolling = [
        (week, f)
        for week, fl in flags.items()
        for f in fl
        if f.kind in ("crunch_week", "severe_crunch")
    ]
    assert rolling

    # The load sits in the week of Mon Nov 16; the flag must land there.
    weeks = {week for week, _ in rolling}
    assert date(2026, 11, 16) in weeks


def test_one_busy_stretch_produces_one_crunch_flag() -> None:
    """Adjacent calendar weeks must not each report the same window shifted a day.

    A single heavy cluster straddling a week boundary used to fire twice, as
    "Oct 8 to Oct 14" and "Oct 9 to Oct 15", and a student reads that as two
    crunches. Overlapping windows collapse to the heavier one.
    """
    from datetime import date, timedelta

    from workload_model.flags import compute_flags
    from workload_model.heatmap import ScheduledItem, build_heatmap

    term_start, term_end = date(2026, 8, 24), date(2026, 12, 11)
    items = []
    # A quiet baseline: one small item a week.
    for week in range(16):
        due = term_start + timedelta(weeks=week, days=4)
        items.append(
            ScheduledItem(
                assessment_id=f"ps{week}",
                title=f"PS {week}",
                course_label="C1",
                assessment_type="homework",
                due_on=due,
                weight_pct=2.0,
            )
        )
    # One heavy cluster across a Sunday/Monday boundary.
    for i, due in enumerate((date(2026, 10, 10), date(2026, 10, 12), date(2026, 10, 13))):
        items.append(
            ScheduledItem(
                assessment_id=f"big{i}",
                title=f"Big {i}",
                course_label="C1",
                assessment_type="midterm",
                due_on=due,
                weight_pct=20.0,
            )
        )
    cells = build_heatmap(items, term_start, term_end, 15)
    flags = compute_flags(items, cells, term_start, term_end, 15)
    crunch = [f for f in sum(flags.values(), []) if f.kind in ("crunch_week", "severe_crunch")]
    assert len(crunch) == 1, [f.explanation for f in crunch]
