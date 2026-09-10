"""Meeting-pattern parsing tests.

Small surface, outsized consequences: "Week N" resolution, "Class N" counting,
and the final exam matrix lookup (9.5) all key off these two functions. A
misparse here silently relocates an exam.
"""

from __future__ import annotations

from datetime import time

import pytest
from date_resolver.weekdays import parse_meeting_time, parse_meeting_weekdays


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("MWF 10:00-10:50", [0, 2, 4]),
        ("TR 14:00-15:15", [1, 3]),
        ("TTh 9:30-10:45", [1, 3]),
        ("MW 16:00-17:15", [0, 2]),
        ("F 13:00-15:50", [4]),
        ("Mon/Wed/Fri 10:00", [0, 2, 4]),
        ("Tuesday, Thursday 2:00pm", [1, 3]),
        ("Monday and Wednesday 9:00", [0, 2]),
    ],
)
def test_weekday_parsing(pattern: str, expected: list[int]) -> None:
    assert parse_meeting_weekdays(pattern) == expected


def test_r_and_th_both_mean_thursday() -> None:
    assert parse_meeting_weekdays("TR 14:00") == parse_meeting_weekdays("TTh 14:00")


def test_unparseable_pattern_returns_empty_not_a_guess() -> None:
    """Callers must treat [] as "unknown pattern", never as "no class"."""
    assert parse_meeting_weekdays(None) == []
    assert parse_meeting_weekdays("") == []
    assert parse_meeting_weekdays("Asynchronous online") == []


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("MWF 10:00-10:50", time(10, 0)),
        ("TR 14:00-15:15", time(14, 0)),
        ("TR 2:00-3:15pm", time(14, 0)),
        ("MW 9:30am-10:45am", time(9, 30)),
        ("F 1:00-3:50pm", time(13, 0)),
        ("MWF 8:00-8:50am", time(8, 0)),
    ],
)
def test_meeting_time_parsing(pattern: str, expected: time) -> None:
    assert parse_meeting_time(pattern) == expected


def test_trailing_meridian_governs_the_whole_range() -> None:
    """ "2:00-3:15pm" is a 2pm class. Reading it as 02:00 misplaces the final exam."""
    assert parse_meeting_time("TR 2:00-3:15pm") == time(14, 0)


def test_meridian_is_not_borrowed_when_it_would_invert_the_range() -> None:
    """ "11:00-12:30pm" starts at 11am — borrowing pm would end the class before it starts."""
    assert parse_meeting_time("MWF 11:00-12:30pm") == time(11, 0)


def test_missing_time_returns_none() -> None:
    assert parse_meeting_time("MWF") is None
    assert parse_meeting_time(None) is None
