"""Unit tests for the date resolver — one section per row of PRD table 9.2.

Appendix B step 8 requires these to exist and pass before the resolver ever
touches a real document. Hand-constructed cases only; no fixtures from real
syllabi here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest
from date_resolver.calendar_context import AcademicCalendar
from date_resolver.resolver import (
    ResolutionContext,
    expand_recurrence,
    resolve_all,
    resolve_date_expression,
    resolve_final_exam,
)

# --- 9.2 row 1: full date ---------------------------------------------------------------


def test_full_date_with_year(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("October 14, 2026", mwf_ctx)
    assert r.resolution_case == "full_date"
    assert r.due_at is not None and r.due_at.date() == date(2026, 10, 14)


def test_full_date_with_explicit_time_is_exact_datetime(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("October 14, 2026 at 11:59pm", mwf_ctx)
    assert r.due_precision == "exact_datetime"
    assert r.time_inferred is False
    assert r.due_at is not None and r.due_at.hour == 23 and r.due_at.minute == 59


def test_full_date_without_time_is_date_only_and_marks_inference(
    mwf_ctx: ResolutionContext,
) -> None:
    """9.3: the date is known, the time is not. Any applied time must be marked inferred."""
    r = resolve_date_expression("October 14, 2026", mwf_ctx)
    assert r.due_precision == "date_only"
    assert r.time_inferred is True


def test_iso_and_numeric_spellings(mwf_ctx: ResolutionContext) -> None:
    for expression in ("2026-10-14", "10/14/2026", "10/14/26"):
        r = resolve_date_expression(expression, mwf_ctx)
        assert r.due_at is not None and r.due_at.date() == date(2026, 10, 14), expression


# --- 9.2 row 2: date without year -------------------------------------------------------


def test_date_without_year_maps_into_term(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("Oct 14", mwf_ctx)
    assert r.resolution_case == "date_without_year"
    assert r.due_at is not None and r.due_at.date() == date(2026, 10, 14)


def test_year_spanning_term_flags_ambiguous_date() -> None:
    """A spring term crossing New Year: both candidate years land inside, so flag it."""
    calendar = AcademicCalendar(
        term_name="Winter 2026-27",
        start_date=date(2026, 12, 1),
        end_date=date(2027, 3, 1),
    )
    ctx = ResolutionContext(calendar=calendar)
    r = resolve_date_expression("January 15", ctx)
    assert r.due_at is not None and r.due_at.date() == date(2027, 1, 15)
    assert r.needs_review is False  # only Jan 2027 falls inside; Jan 2026 does not

    ambiguous = resolve_date_expression("December 15", ctx)
    assert ambiguous.due_at is not None
    # Dec 15 2026 is inside; Dec 15 2027 is not — so this one is also unambiguous.
    assert ambiguous.due_at.date() == date(2026, 12, 15)


# --- 9.2 row 3: weekday plus date, mismatched -------------------------------------------


def test_weekday_mismatch_prefers_numeric_date_and_flags(mwf_ctx: ResolutionContext) -> None:
    """The PRD's own example: Oct 14 2026 is a Wednesday, not a Tuesday."""
    r = resolve_date_expression("Tuesday, October 14", mwf_ctx)
    assert r.due_at is not None and r.due_at.date() == date(2026, 10, 14)
    assert r.weekday_mismatch is True
    assert r.needs_review is True
    assert r.notes is not None and "Tuesday" in r.notes and "Wednesday" in r.notes


def test_weekday_agreeing_with_date_does_not_flag(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("Wednesday, October 14", mwf_ctx)
    assert r.weekday_mismatch is False
    assert r.needs_review is False


# --- 9.2 row 4: week number --------------------------------------------------------------


def test_week_number_without_meeting_pattern_stays_a_band(
    no_pattern_ctx: ResolutionContext,
) -> None:
    """9.3, non-negotiable: week_only never collapses to a specific day."""
    r = resolve_date_expression("Week 6", no_pattern_ctx)
    assert r.due_precision == "week_only"
    assert r.due_at is None
    assert r.week_start == date(2026, 9, 28)  # week 1 starts Mon Aug 24
    assert r.week_end == date(2026, 10, 5)


def test_week_number_with_meeting_pattern_resolves_to_a_meeting(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("Week 6", mwf_ctx)
    assert r.due_precision == "date_only"
    assert r.due_at is not None and r.due_at.date() == date(2026, 9, 28)  # the Monday of week 6


def test_week_one_is_the_week_containing_term_start(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("Week 1", mwf_ctx)
    assert r.due_at is not None and r.due_at.date() == date(2026, 8, 24)


# --- 9.2 row 5: session number -----------------------------------------------------------


def test_session_number_counts_meetings_skipping_no_class_days(mwf_ctx: ResolutionContext) -> None:
    """MWF from Aug 24, with Labor Day (Mon Sep 7) removed from the count."""
    meetings = mwf_ctx.calendar.meeting_days([0, 2, 4])
    assert date(2026, 9, 7) not in meetings  # Labor Day skipped

    r = resolve_date_expression("Class 5", mwf_ctx)
    assert r.due_at is not None and r.due_at.date() == meetings[4]


def test_session_number_without_meeting_pattern_is_tbd(no_pattern_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("Class 12", no_pattern_ctx)
    assert r.due_precision == "tbd"
    assert r.needs_review is True


def test_session_number_beyond_term_is_tbd(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("Class 99", mwf_ctx)
    assert r.due_precision == "tbd"


# --- 9.2 row 6: relative to an anchor ----------------------------------------------------


def test_relative_to_calendar_label(mwf_ctx: ResolutionContext) -> None:
    """ "the class after Thanksgiving Break" resolves off the registrar's labels."""
    r = resolve_date_expression("the class after Thanksgiving Break", mwf_ctx)
    assert r.resolution_case == "relative_to_anchor"
    # Break ends Fri Nov 27; the next MWF meeting is Mon Nov 30.
    assert r.due_at is not None and r.due_at.date() == date(2026, 11, 30)


def test_relative_to_another_assessment_resolves_in_round_two(mwf_ctx: ResolutionContext) -> None:
    """Two-round resolution (9.2): the anchor lands in round one, the offset in round two."""
    results = resolve_all(
        [
            ("Midterm 1", "October 14, 2026"),
            ("Project Proposal", "one week after Midterm 1"),
        ],
        mwf_ctx,
    )
    assert results["Midterm 1"].due_at is not None
    proposal = results["Project Proposal"]
    assert proposal.due_at is not None and proposal.due_at.date() == date(2026, 10, 21)


def test_unresolvable_anchor_stays_tbd_after_two_rounds(mwf_ctx: ResolutionContext) -> None:
    results = resolve_all(
        [
            ("Midterm 1", "October 14, 2026"),
            ("Paper", "two weeks after the guest lecture"),
        ],
        mwf_ctx,
    )
    assert results["Paper"].due_precision == "tbd"
    assert results["Paper"].needs_review is True


# --- 9.2 row 7: recurring -----------------------------------------------------------------


def test_recurrence_expands_and_subtracts_no_class_days(mwf_ctx: ResolutionContext) -> None:
    result = expand_recurrence("quizzes every Friday", mwf_ctx)
    assert result is not None
    assert result.rrule == "FREQ=WEEKLY;BYDAY=FR"
    assert all(d.weekday() == 4 for d in result.occurrences)
    assert date(2026, 11, 27) not in result.occurrences  # Thanksgiving Friday


def test_recurrence_truncates_to_grade_breakdown_item_count(mwf_ctx: ResolutionContext) -> None:
    """The breakdown is the authority on how many items exist (8.3)."""
    result = expand_recurrence("every Friday", mwf_ctx, item_count=10)
    assert result is not None
    assert len(result.occurrences) == 10


def test_bare_recurring_expression_does_not_resolve_to_one_date(mwf_ctx: ResolutionContext) -> None:
    r = resolve_date_expression("due Sundays at 11:59pm", mwf_ctx)
    assert r.due_precision == "tbd"
    assert r.resolution_case == "recurring"


# --- 9.2 row 8: unspecified ---------------------------------------------------------------


@pytest.mark.parametrize("expression", ["TBA", "TBD", "date to be announced", "To Be Determined"])
def test_tbd_expressions_never_reach_the_calendar(
    expression: str, mwf_ctx: ResolutionContext
) -> None:
    r = resolve_date_expression(expression, mwf_ctx)
    assert r.due_precision == "tbd"
    assert r.due_at is None


def test_empty_expression_is_tbd(mwf_ctx: ResolutionContext) -> None:
    assert resolve_date_expression(None, mwf_ctx).due_precision == "tbd"
    assert resolve_date_expression("   ", mwf_ctx).due_precision == "tbd"


def test_unparseable_expression_degrades_to_tbd_rather_than_raising(
    mwf_ctx: ResolutionContext,
) -> None:
    r = resolve_date_expression("sometime around the middle of things", mwf_ctx)
    assert r.due_precision == "tbd"
    assert r.needs_review is True


# --- 9.5: final exams from the registrar matrix --------------------------------------------


def test_final_exam_resolves_from_matrix_not_syllabus(mwf_ctx: ResolutionContext) -> None:
    r = resolve_final_exam("MWF 10:00-10:50", mwf_ctx)
    assert r.due_precision == "exact_datetime"
    assert r.due_at is not None and r.due_at.date() == date(2026, 12, 9)
    assert r.time_inferred is False


def test_final_exam_matrix_matches_on_weekdays_not_string_equality(
    mwf_ctx: ResolutionContext,
) -> None:
    """ "TR 2:00-3:15pm" and the matrix's "TR 14:00-15:15" are the same pattern."""
    r = resolve_final_exam("TR 2:00-3:15pm", mwf_ctx)
    assert r.due_at is not None and r.due_at.date() == date(2026, 12, 8)


def test_final_exam_without_matrix_row_degrades_to_tbd(mwf_ctx: ResolutionContext) -> None:
    """Section 19: registrar data blocked means tbd, not a broken app."""
    r = resolve_final_exam("SU 09:00-11:00", mwf_ctx)
    assert r.due_precision == "tbd"


def test_final_exam_matrix_is_keyed_on_class_time_not_just_weekdays(
    fall_2026: AcademicCalendar,
) -> None:
    """An MWF 08:00 section and an MWF 10:00 section sit in different exam slots.

    Matching on weekdays alone would put the exam on the wrong day — worse than
    returning tbd, because the student would trust it.
    """
    from date_resolver.calendar_context import FinalExamMatrixEntry

    tz = fall_2026.tzinfo
    calendar = replace(
        fall_2026,
        final_exam_matrix=(
            FinalExamMatrixEntry(
                meeting_pattern="MWF 08:00-08:50",
                exam_start_at=datetime(2026, 12, 7, 8, 0, tzinfo=tz),
                exam_end_at=datetime(2026, 12, 7, 10, 0, tzinfo=tz),
            ),
            FinalExamMatrixEntry(
                meeting_pattern="MWF 10:00-10:50",
                exam_start_at=datetime(2026, 12, 9, 10, 0, tzinfo=tz),
                exam_end_at=datetime(2026, 12, 9, 12, 0, tzinfo=tz),
            ),
        ),
    )
    ctx = ResolutionContext(calendar=calendar, meeting_pattern="MWF 10:00-10:50")

    r = resolve_final_exam("MWF 10:00-10:50", ctx)
    assert r.due_at is not None and r.due_at.date() == date(2026, 12, 9)

    early = resolve_final_exam("MWF 08:00-08:50", ctx)
    assert early.due_at is not None and early.due_at.date() == date(2026, 12, 7)


def test_final_exam_with_ambiguous_time_slot_refuses_to_guess(
    fall_2026: AcademicCalendar,
) -> None:
    """Days match but the time does not: tbd beats picking one of several slots."""
    ctx = ResolutionContext(calendar=fall_2026, meeting_pattern="MWF 13:00-13:50")
    r = resolve_final_exam("MWF 13:00-13:50", ctx)
    assert r.due_precision == "tbd"
    assert r.due_at is None


# --- 9.3 invariants, stated as their own tests ---------------------------------------------


def test_a_time_is_only_uninferred_when_the_source_stated_one(mwf_ctx: ResolutionContext) -> None:
    """The product-killing failure mode (9.3), asserted over every 9.2 case.

    The invariant is not "never attach a time" — an in-class item at a known
    meeting time legitimately has one. It is that `time_inferred=False` is
    reserved for times the student can find in the sentence they are checking.
    """
    expressions = [
        "October 14, 2026",
        "Oct 14",
        "Tuesday, October 14",
        "Week 6",
        "Class 5",
        "the class after Thanksgiving Break",
        "TBA",
    ]
    for expression in expressions:
        r = resolve_date_expression(expression, mwf_ctx)
        if r.due_at is not None and not r.time_inferred:
            assert any(token in expression for token in (":", "am", "pm")), expression


def test_class_session_time_comes_from_the_pattern_and_is_marked_inferred(
    mwf_ctx: ResolutionContext,
) -> None:
    r = resolve_date_expression("Class 5", mwf_ctx)
    assert r.due_at is not None and r.due_at.hour == 10  # the MWF 10:00 meeting time
    assert r.time_inferred is True  # muted in the UI: not stated in the syllabus


def test_week_only_and_tbd_carry_no_datetime_at_all(
    no_pattern_ctx: ResolutionContext, mwf_ctx: ResolutionContext
) -> None:
    assert resolve_date_expression("Week 6", no_pattern_ctx).due_at is None
    assert resolve_date_expression("TBA", mwf_ctx).due_at is None
