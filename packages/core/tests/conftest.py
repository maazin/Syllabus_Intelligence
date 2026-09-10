"""Shared fixtures for the core package tests.

The calendar here is the Fall 2026 term used throughout the PRD's examples
(section 9.2's "Tuesday, October 14" case is a real mismatch against it —
Oct 14 2026 is a Wednesday, which is exactly why the PRD picked it).
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from date_resolver.calendar_context import (
    AcademicCalendar,
    CalendarExceptionEntry,
    FinalExamMatrixEntry,
)
from date_resolver.resolver import ResolutionContext

TZ = ZoneInfo("America/New_York")


@pytest.fixture
def fall_2026() -> AcademicCalendar:
    """Fall 2026: Mon Aug 24 through Fri Dec 4, finals Dec 7-11."""
    return AcademicCalendar(
        term_name="Fall 2026",
        start_date=date(2026, 8, 24),
        end_date=date(2026, 12, 4),
        timezone="America/New_York",
        default_due_time=time(23, 59),
        exceptions=(
            CalendarExceptionEntry(date(2026, 9, 7), "Labor Day"),
            CalendarExceptionEntry(date(2026, 11, 25), "Thanksgiving Break"),
            CalendarExceptionEntry(date(2026, 11, 26), "Thanksgiving Break"),
            CalendarExceptionEntry(date(2026, 11, 27), "Thanksgiving Break"),
        ),
        add_drop_date=date(2026, 8, 28),
        withdrawal_date=date(2026, 10, 30),
        finals_start=date(2026, 12, 7),
        finals_end=date(2026, 12, 11),
        final_exam_matrix=(
            FinalExamMatrixEntry(
                meeting_pattern="MWF 10:00-10:50",
                exam_start_at=datetime(2026, 12, 9, 10, 0, tzinfo=TZ),
                exam_end_at=datetime(2026, 12, 9, 12, 0, tzinfo=TZ),
            ),
            FinalExamMatrixEntry(
                meeting_pattern="TR 14:00-15:15",
                exam_start_at=datetime(2026, 12, 8, 14, 0, tzinfo=TZ),
                exam_end_at=datetime(2026, 12, 8, 16, 0, tzinfo=TZ),
            ),
        ),
    )


@pytest.fixture
def mwf_ctx(fall_2026: AcademicCalendar) -> ResolutionContext:
    return ResolutionContext(calendar=fall_2026, meeting_pattern="MWF 10:00-10:50")


@pytest.fixture
def no_pattern_ctx(fall_2026: AcademicCalendar) -> ResolutionContext:
    """A section whose meeting pattern is unknown — the week_only path (9.3)."""
    return ResolutionContext(calendar=fall_2026, meeting_pattern=None)
