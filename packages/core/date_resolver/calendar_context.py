"""The institution's academic calendar, as the resolver needs it (PRD section 9.1).

This is a plain value object, not a DB query. `date_resolver` is pure — the
worker loads a term's rows and hands one of these in. That is what makes
section 9.2's whole resolution table unit-testable against hand-constructed
cases before it ever touches a real document (Appendix B, step 8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FinalExamMatrixEntry:
    """One row of the registrar's final exam matrix (section 9.5)."""

    meeting_pattern: str
    exam_start_at: datetime
    exam_end_at: datetime


@dataclass(frozen=True)
class CalendarExceptionEntry:
    """A labeled no-class day. The label matters: anchor expressions like
    "the class after spring break" (9.2) resolve against it."""

    date: date
    label: str
    no_class: bool = True


@dataclass(frozen=True)
class AcademicCalendar:
    """One term's dates. ~20 rows of manually seeded data that unblocks everything."""

    term_name: str
    start_date: date
    end_date: date
    timezone: str = "America/New_York"
    default_due_time: time = time(23, 59)
    exceptions: tuple[CalendarExceptionEntry, ...] = ()
    add_drop_date: date | None = None
    withdrawal_date: date | None = None
    finals_start: date | None = None
    finals_end: date | None = None
    final_exam_matrix: tuple[FinalExamMatrixEntry, ...] = ()

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def no_class_days(self) -> frozenset[date]:
        return frozenset(e.date for e in self.exceptions if e.no_class)

    def dates_for_label(self, label: str) -> list[date]:
        """Every date carrying a label matching `label` (case-insensitive substring).

        Returns a list because breaks span multiple days — anchor resolution
        wants the last day of "spring break", not the first.
        """
        needle = label.lower().strip()
        if not needle:
            return []
        return sorted(
            e.date
            for e in self.exceptions
            if needle in e.label.lower() or e.label.lower() in needle
        )

    def contains(self, d: date) -> bool:
        """Is this date inside the instructional term? Finals week counts (section 9.5)."""
        end = self.finals_end or self.end_date
        return self.start_date <= d <= end

    def is_class_day(self, d: date) -> bool:
        return self.contains(d) and d not in self.no_class_days

    def week_one_monday(self) -> date:
        """Week 1 is the week containing the term start date (section 9.2)."""
        return self.start_date - timedelta(days=self.start_date.weekday())

    def week_bounds(self, week_number: int) -> tuple[date, date]:
        """[start, end) bounds of the given 1-indexed term week."""
        if week_number < 1:
            raise ValueError("week_number is 1-indexed")
        start = self.week_one_monday() + timedelta(weeks=week_number - 1)
        return start, start + timedelta(days=7)

    def week_number_for(self, d: date) -> int:
        """Inverse of `week_bounds` — which term week does this date fall in?"""
        delta_days = (d - self.week_one_monday()).days
        return delta_days // 7 + 1

    def meeting_days(self, weekdays: list[int]) -> list[date]:
        """Every class-meeting date in the term for the given weekday indices.

        No-class days are subtracted, which is what makes "Class 12" (session
        counting) and recurrence expansion agree with the real calendar.
        """
        if not weekdays:
            return []
        wanted = set(weekdays)
        out: list[date] = []
        cursor = self.start_date
        last = self.end_date
        while cursor <= last:
            if cursor.weekday() in wanted and cursor not in self.no_class_days:
                out.append(cursor)
            cursor += timedelta(days=1)
        return out

    def localize(self, d: date, t: time | None = None) -> datetime:
        """Attach the institution's timezone. `t=None` applies the LMS default due time."""
        return datetime.combine(
            d, t if t is not None else self.default_due_time, tzinfo=self.tzinfo
        )
