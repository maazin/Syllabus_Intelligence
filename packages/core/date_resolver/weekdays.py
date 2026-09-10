"""Parse a meeting-pattern string ("MWF 10:00-10:50", "TR 14:00-15:15", "Mon/Wed 6:00pm")
into weekday indices (Monday=0 .. Sunday=6) and, where present, a start time.

Pure, no I/O. This is the piece "Week N" and "Class N" resolution (section 9.2) depend on.
"""

from __future__ import annotations

import re
from datetime import time

_DAY_CODES: dict[str, int] = {
    "mo": 0, "mon": 0, "monday": 0, "m": 0,
    "tu": 1, "tue": 1, "tues": 1, "tuesday": 1, "t": 1,
    "we": 2, "wed": 2, "wednesday": 2, "w": 2,
    "th": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "r": 3,
    "fr": 4, "fri": 4, "friday": 4, "f": 4,
    "sa": 5, "sat": 5, "saturday": 5, "s": 5,
    "su": 6, "sun": 6, "sunday": 6, "u": 6,
}  # fmt: skip

# Longest codes first so "Th" matches before falling back to "T" + "h".
_COMPACT_ALTERNATION = "|".join(
    sorted((k for k in _DAY_CODES if k.isalpha()), key=len, reverse=True)
)
_COMPACT_RE = re.compile(_COMPACT_ALTERNATION, re.IGNORECASE)

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?", re.IGNORECASE)

#: A time range: "10:00-10:50", "2:00-3:15pm", "9:30 am - 10:45 am".
#: The trailing meridian governs both endpoints when the first lacks one —
#: "2:00-3:15pm" is a 2pm class, not a 2am one.
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*[-–—]\s*(\d{1,2}):(\d{2})\s*(am|pm)?",
    re.IGNORECASE,
)

_SEPARATORS_RE = re.compile(r"[\/,&]|(?:\s+and\s+)", re.IGNORECASE)


#: Full and abbreviated day *names*, matched with word boundaries. Distinct from
#: the compact single-letter codes, which are only safe on an all-code token.
_NAMED_DAYS = {k: v for k, v in _DAY_CODES.items() if len(k) >= 2}
_NAMED_DAY_RE = re.compile(
    r"\b(" + "|".join(sorted(_NAMED_DAYS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)

#: A token made up entirely of compact day-code letters, e.g. "MWF", "TR", "TTh".
#: Anchored end-to-end so a word like "Asynchronous" cannot match letter by letter.
_COMPACT_TOKEN_RE = re.compile(rf"^(?:{_COMPACT_ALTERNATION})+$", re.IGNORECASE)


def parse_meeting_weekdays(meeting_pattern: str | None) -> list[int]:
    """Extract the set of meeting weekdays from a raw meeting-pattern string.

    Handles compact letter codes ("MWF", "TR", "TTh") and separated names
    ("Mon/Wed/Fri", "Tuesday, Thursday"). Returns [] if nothing recognizable
    is found — callers must treat that as "unknown pattern", not "no class."

    Day *names* are tried before compact codes, and compact parsing only runs on
    a token composed entirely of code letters. Without that guard, prose like
    "Asynchronous online" parses as Saturday/Thursday/Sunday, which would put
    deadlines on days the class never meets.
    """
    if not meeting_pattern:
        return []

    # Strip the time portion so "Tuesday, Thursday 2:00pm" does not feed digits
    # and meridian letters into day matching.
    days_part = re.split(r"\d", meeting_pattern, maxsplit=1)[0].strip()
    if not days_part:
        return []

    found: list[int] = []
    for match in _NAMED_DAY_RE.finditer(days_part):
        day = _NAMED_DAYS[match.group(1).lower()]
        if day not in found:
            found.append(day)
    if found:
        return sorted(found)

    # No day names. Fall back to compact codes, but only for a token that is
    # entirely day-code letters.
    for token in _SEPARATORS_RE.split(days_part):
        token = token.strip()
        if not token or not _COMPACT_TOKEN_RE.match(token):
            continue
        for match in _COMPACT_RE.finditer(token):
            day = _DAY_CODES[match.group(0).lower()]
            if day not in found:
                found.append(day)
    return sorted(found)


def _to_24h(hour: int, minute: int, meridian: str) -> time | None:
    meridian = meridian.lower()
    if meridian == "pm" and hour != 12:
        hour += 12
    elif meridian == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour=hour, minute=minute)


def parse_meeting_time(meeting_pattern: str | None) -> time | None:
    """Extract the class start time, e.g. 10:00 from "MWF 10:00-10:50".

    Ranges are parsed as ranges. In "TR 2:00-3:15pm" the `pm` governs the whole
    range, so the start time is 14:00 — reading it as 02:00 would place a final
    exam in the small hours of the morning.
    """
    if not meeting_pattern:
        return None

    range_match = _TIME_RANGE_RE.search(meeting_pattern)
    if range_match:
        start_hour, start_minute = int(range_match.group(1)), int(range_match.group(2))
        start_meridian = range_match.group(3) or ""
        end_meridian = range_match.group(6) or ""

        if not start_meridian and end_meridian:
            end_hour = int(range_match.group(4))
            # Borrow the trailing meridian, unless doing so would make the class
            # end before it starts (e.g. "11:00-12:30pm" is 11am to 12:30pm).
            candidate = _to_24h(start_hour, start_minute, end_meridian)
            end_time = _to_24h(end_hour, int(range_match.group(5)), end_meridian)
            if candidate and end_time and candidate < end_time:
                return candidate
            return _to_24h(start_hour, start_minute, "")
        return _to_24h(start_hour, start_minute, start_meridian)

    match = _TIME_RE.search(meeting_pattern)
    if not match:
        return None
    return _to_24h(int(match.group(1)), int(match.group(2)), match.group(3) or "")
