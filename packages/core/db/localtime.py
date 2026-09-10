"""Converting stored instants back to the institution's calendar day.

`assessments.due_at` is `timestamptz`, which is right: a deadline is an instant,
and Postgres normalizes it to UTC on write. But every *display* surface in this
product — the timeline, the heatmap, the ICS feed, the collision flags — works
in calendar days, and the calendar day a student means is the one in the
institution's own timezone.

Calling `.date()` on the value read back from Postgres gives the UTC day, which
is wrong for exactly the deadlines that matter most: the LMS default due time is
11:59pm local (section 9.3), and 11:59pm Eastern is 03:59 UTC the *following*
day. Left unconverted, essentially every deadline in the product would render
one day late.

Always route a stored `due_at` through `local_date` before treating it as a day.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

#: Fallback only. Real callers pass the institution's timezone.
DEFAULT_TIMEZONE = "America/New_York"


def local_date(moment: datetime | None, timezone: str = DEFAULT_TIMEZONE) -> date | None:
    """The calendar day this instant falls on, in the institution's timezone."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        # A naive datetime came from somewhere that lost its offset. Treat it as
        # already local rather than silently assuming UTC and shifting the day.
        return moment.date()
    return moment.astimezone(ZoneInfo(timezone)).date()


def local_datetime(moment: datetime | None, timezone: str = DEFAULT_TIMEZONE) -> datetime | None:
    """The same instant expressed in the institution's timezone."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(ZoneInfo(timezone))
