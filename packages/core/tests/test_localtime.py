"""Tests for the stored-instant to calendar-day conversion.

Small module, high blast radius: `due_at` is `timestamptz`, so every display
surface that thinks in days depends on this being right. The failure mode is
silent and systematic — it moves deadlines exactly one day, only for the
late-evening ones, which is to say almost all of them (9.3's default is 11:59pm).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from db.localtime import local_date, local_datetime

EASTERN = "America/New_York"


def test_late_evening_local_time_keeps_its_own_day() -> None:
    """11:59pm Eastern is 03:59 UTC the next morning."""
    deadline = datetime(2026, 10, 14, 23, 59, tzinfo=ZoneInfo(EASTERN))
    stored = deadline.astimezone(UTC)  # what Postgres hands back

    assert stored.date() == date(2026, 10, 15)  # the naive, wrong reading
    assert local_date(stored, EASTERN) == date(2026, 10, 14)  # the student's day


def test_midday_times_are_unaffected() -> None:
    """The bug only bites near the boundary; midday must not move."""
    noon = datetime(2026, 10, 14, 12, 0, tzinfo=ZoneInfo(EASTERN)).astimezone(UTC)
    assert local_date(noon, EASTERN) == date(2026, 10, 14)


def test_conversion_respects_daylight_saving() -> None:
    """A term spans a DST transition; the offset is not constant."""
    summer = datetime(2026, 9, 1, 23, 59, tzinfo=ZoneInfo(EASTERN))  # EDT, UTC-4
    winter = datetime(2026, 12, 1, 23, 59, tzinfo=ZoneInfo(EASTERN))  # EST, UTC-5

    assert local_date(summer.astimezone(UTC), EASTERN) == date(2026, 9, 1)
    assert local_date(winter.astimezone(UTC), EASTERN) == date(2026, 12, 1)


def test_other_timezones_work() -> None:
    pacific = datetime(2026, 10, 14, 23, 59, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert local_date(pacific.astimezone(UTC), "America/Los_Angeles") == date(2026, 10, 14)


def test_none_passes_through() -> None:
    assert local_date(None) is None
    assert local_datetime(None) is None


def test_naive_datetimes_are_treated_as_already_local() -> None:
    """Assuming UTC for a naive value would shift the day — the exact bug."""
    naive = datetime(2026, 10, 14, 23, 59)
    assert local_date(naive, EASTERN) == date(2026, 10, 14)
    assert local_datetime(naive, EASTERN) == naive


def test_local_datetime_preserves_the_instant() -> None:
    deadline = datetime(2026, 10, 14, 23, 59, tzinfo=ZoneInfo(EASTERN))
    stored = deadline.astimezone(UTC)
    converted = local_datetime(stored, EASTERN)

    assert converted == stored  # same moment
    assert converted.hour == 23  # expressed locally
    assert converted.date() == date(2026, 10, 14)


@pytest.mark.parametrize("hour", [0, 1, 22, 23])
def test_boundary_hours_round_trip(hour: int) -> None:
    """Both ends of the day are where an offset error shows up."""
    original = datetime(2026, 10, 14, hour, 30, tzinfo=ZoneInfo(EASTERN))
    assert local_date(original.astimezone(UTC), EASTERN) == date(2026, 10, 14)
