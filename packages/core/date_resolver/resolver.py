"""Date resolution — PRD section 9. Pure functions, no I/O.

The governing rule, from 9.3: an ambiguity is *displayed* as an ambiguity.
Nothing in this module may invent specificity the source text did not carry.
A "Week 6" expression resolves to a week band, not to a Wednesday; a "TBA"
resolves to `tbd` and never reaches a calendar. Every escape hatch here sets
`needs_review` rather than guessing.

Case coverage maps one-to-one onto the table in 9.2:

    full_date | date_without_year | weekday_plus_date | week_number
    session_number | relative_to_anchor | recurring | unspecified
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

from schemas.resolution import RecurrenceResolution, ResolvedDate

from date_resolver.calendar_context import AcademicCalendar
from date_resolver.weekdays import parse_meeting_time, parse_meeting_weekdays

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip

_WEEKDAY_NAMES: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}  # fmt: skip

_WEEKDAY_FULL = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAY_NAMES, key=len, reverse=True))

# "October 14, 2026" / "Oct 14 2026" / "Oct. 14"
_MONTH_DAY_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:\s*(?:,\s*)?(\d{{4}}))?\b", re.IGNORECASE
)
# "10/14/2026", "10/14", "2026-10-14"
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_WEEKDAY_RE = re.compile(rf"\b({_WEEKDAY_ALT})\b", re.IGNORECASE)
_WEEK_NUM_RE = re.compile(r"\bweek\s+(\d{1,2})\b", re.IGNORECASE)
_SESSION_NUM_RE = re.compile(r"\b(?:class|session|lecture|meeting)\s+(\d{1,2})\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", re.IGNORECASE)
_TIME_24H_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")

_TBD_TOKENS = (
    "tba", "tbd", "to be announced", "to be determined", "to be scheduled",
    "see schedule", "date tba", "announced in class",
)  # fmt: skip

_RELATIVE_RE = re.compile(
    r"\b(?:the\s+)?(?:(?P<count>\d+|one|two|three|a)\s+)?"
    r"(?P<unit>class(?:es)?|day|days|week|weeks|session|sessions)?\s*"
    r"(?P<direction>after|before|following|preceding|prior\s+to)\s+"
    r"(?P<anchor>.+)",
    re.IGNORECASE,
)

_WORD_NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

_RECURRENCE_RE = re.compile(
    rf"\b(?:every|each|weekly\s+on|due)\s+(?P<day>{_WEEKDAY_ALT})s?\b", re.IGNORECASE
)
_RECURRENCE_PLURAL_RE = re.compile(rf"\b(?P<day>{_WEEKDAY_ALT})s\b", re.IGNORECASE)

_RRULE_BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@dataclass(frozen=True)
class ResolutionContext:
    """Everything resolution needs beyond the expression itself."""

    calendar: AcademicCalendar
    meeting_pattern: str | None = None
    #: Titles of already-resolved assessments, for anchor lookup (9.2 "relative to an anchor").
    #: Populated between round one and round two by `resolve_all`.
    anchors: dict[str, date] | None = None

    @property
    def meeting_weekdays(self) -> list[int]:
        return parse_meeting_weekdays(self.meeting_pattern)

    @property
    def meeting_time(self) -> time | None:
        return parse_meeting_time(self.meeting_pattern)


def _tbd(note: str, case: str = "unspecified") -> ResolvedDate:
    return ResolvedDate(
        due_precision="tbd",
        resolution_case=case,  # type: ignore[arg-type]
        needs_review=True,
        notes=note,
    )


def _extract_time(text: str) -> time | None:
    """Pull an explicit clock time out of the expression, if the source stated one.

    Only an explicitly written time counts. Absence here is what drives
    `date_only` + `time_inferred` downstream, per 9.3.
    """
    match = _TIME_RE.search(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridian = match.group(3).lower().replace(".", "")
        if meridian.startswith("p") and hour != 12:
            hour += 12
        if meridian.startswith("a") and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    match24 = _TIME_24H_RE.search(text)
    if match24:
        hour, minute = int(match24.group(1)), int(match24.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    return None


def _named_weekday(text: str) -> int | None:
    match = _WEEKDAY_RE.search(text)
    return _WEEKDAY_NAMES[match.group(1).lower()] if match else None


def _parse_month_day(text: str) -> tuple[int, int, int | None] | None:
    """Return (month, day, year|None) from any supported date spelling."""
    iso = _ISO_DATE_RE.search(text)
    if iso:
        return int(iso.group(2)), int(iso.group(3)), int(iso.group(1))

    md = _MONTH_DAY_RE.search(text)
    if md:
        month = _MONTHS[md.group(1).lower()]
        day = int(md.group(2))
        year = int(md.group(3)) if md.group(3) else None
        return month, day, year

    num = _NUMERIC_DATE_RE.search(text)
    if num:
        month, day = int(num.group(1)), int(num.group(2))
        year_raw = num.group(3)
        year = None
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day, year
    return None


def _place_in_term(month: int, day: int, calendar: AcademicCalendar) -> tuple[date | None, bool]:
    """Map a year-less month/day into the term range (9.2, "Date without year").

    Returns (resolved_date, ambiguous). A term spanning a year boundary can make
    both candidate years land inside the term; that case is flagged, not guessed.
    """
    candidates: list[date] = []
    for year in {calendar.start_date.year, calendar.end_date.year}:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # e.g. Feb 30, or Feb 29 in a non-leap year
        candidates.append(candidate)

    inside = [c for c in candidates if calendar.contains(c)]
    if len(inside) == 1:
        return inside[0], False
    if len(inside) > 1:
        return inside[0], True  # ambiguous: both fall inside a year-spanning term
    if candidates:
        # Outside the term entirely. Keep it (9.4 flags, never drops) — nearest wins.
        nearest = min(
            candidates,
            key=lambda c: min(
                abs((c - calendar.start_date).days), abs((c - calendar.end_date).days)
            ),
        )
        return nearest, False
    return None, False


def _first_meeting_in_week(week_start: date, week_end: date, ctx: ResolutionContext) -> date | None:
    """The first class meeting inside [week_start, week_end), or None if unknown."""
    weekdays = ctx.meeting_weekdays
    if not weekdays:
        return None
    for meeting in ctx.calendar.meeting_days(weekdays):
        if week_start <= meeting < week_end:
            return meeting
    return None


def _build_dated(
    resolved: date,
    stated_time: time | None,
    ctx: ResolutionContext,
    case: str,
    *,
    inferred_time: time | None = None,
    weekday_mismatch: bool = False,
    needs_review: bool = False,
    notes: str | None = None,
) -> ResolvedDate:
    """Assemble a ResolvedDate, applying 9.3's precision + time-inference rules.

    Three distinct situations, and the distinction is the whole point of 9.3:

    - `stated_time` set: the source text named a time. `exact_datetime`, not inferred.
    - `inferred_time` set: we know a real clock time from elsewhere (the section's
      meeting pattern, for an in-class item). Still `exact_datetime` — the time is
      genuine — but `time_inferred=True` so the UI renders it muted with a tooltip.
      It did not come from the sentence the student is checking against.
    - Neither: the date is known and the time is not. `date_only`, all-day, and any
      LMS default applied downstream for calendar sync is likewise marked inferred.
    """
    if stated_time is not None:
        return ResolvedDate(
            due_at=ctx.calendar.localize(resolved, stated_time),
            due_precision="exact_datetime",
            time_inferred=False,
            weekday_mismatch=weekday_mismatch,
            needs_review=needs_review,
            resolution_case=case,  # type: ignore[arg-type]
            notes=notes,
        )
    if inferred_time is not None:
        return ResolvedDate(
            due_at=ctx.calendar.localize(resolved, inferred_time),
            due_precision="exact_datetime",
            time_inferred=True,
            weekday_mismatch=weekday_mismatch,
            needs_review=needs_review,
            resolution_case=case,  # type: ignore[arg-type]
            notes=notes,
        )
    return ResolvedDate(
        due_at=ctx.calendar.localize(resolved, None),
        due_precision="date_only",
        time_inferred=True,
        weekday_mismatch=weekday_mismatch,
        needs_review=needs_review,
        resolution_case=case,  # type: ignore[arg-type]
        notes=notes,
    )


def resolve_date_expression(
    expression: str | None,
    ctx: ResolutionContext,
) -> ResolvedDate:
    """Resolve one `date_expression_raw` against the academic calendar (9.2).

    Never raises on unparseable input — an unrecognized expression becomes
    `tbd` with `needs_review=True`, which surfaces in review rather than
    silently producing a wrong calendar entry.
    """
    if expression is None or not expression.strip():
        return _tbd("No date expression in the source text.")

    text = expression.strip()
    lowered = text.lower()

    if any(token in lowered for token in _TBD_TOKENS):
        return _tbd(f"Source says {text!r}; shown in the unscheduled tray, not the calendar.")

    explicit_time = _extract_time(text)

    # --- Case: relative to an anchor ("the class after spring break") -----------------
    # Checked before absolute parsing: "one week after Midterm 1" may contain no date
    # at all, and an anchor phrase that *does* contain one still resolves via offset.
    relative = _match_relative(text, ctx)
    if relative is not None:
        return relative

    # --- Case: week number ("Week 6") -------------------------------------------------
    week_match = _WEEK_NUM_RE.search(text)
    if week_match:
        return _resolve_week_number(int(week_match.group(1)), text, explicit_time, ctx)

    # --- Case: session number ("Class 12") --------------------------------------------
    session_match = _SESSION_NUM_RE.search(text)
    if session_match:
        return _resolve_session_number(int(session_match.group(1)), explicit_time, ctx)

    # --- Cases: full date / date without year / weekday+date --------------------------
    parsed = _parse_month_day(text)
    if parsed is not None:
        month, day, year = parsed
        ambiguous = False
        if year is not None:
            try:
                resolved = date(year, month, day)
            except ValueError:
                return _tbd(f"Invalid calendar date in {text!r}.")
            case = "full_date"
        else:
            maybe, ambiguous = _place_in_term(month, day, ctx.calendar)
            if maybe is None:
                return _tbd(f"Invalid calendar date in {text!r}.")
            resolved = maybe
            case = "date_without_year"

        notes = None
        needs_review = ambiguous
        if ambiguous:
            notes = (
                "Term spans a year boundary and both candidate years fall inside it; "
                "confirm the year."
            )

        # Weekday cross-check (9.2): instructors reuse last year's syllabus and update
        # one half of "Tuesday, October 14" but not the other. Prefer the numeric date,
        # flag the mismatch, never resolve it silently.
        named = _named_weekday(text)
        mismatch = named is not None and named != resolved.weekday()
        if named is not None:
            case = "weekday_plus_date"
            if mismatch:
                notes = (
                    f"Source says {_WEEKDAY_FULL[named].capitalize()}, but "
                    f"{resolved.isoformat()} is a "
                    f"{_WEEKDAY_FULL[resolved.weekday()].capitalize()}. Using the numeric date."
                )
                needs_review = True

        return _build_dated(
            resolved,
            explicit_time,
            ctx,
            case,
            weekday_mismatch=mismatch,
            needs_review=needs_review,
            notes=notes,
        )

    # --- Case: bare weekday, no date ("due Fridays") ----------------------------------
    if _RECURRENCE_RE.search(text) or _RECURRENCE_PLURAL_RE.search(text):
        return _tbd(
            f"{text!r} looks recurring; expand it with expand_recurrence() rather than "
            "resolving a single date.",
            case="recurring",
        )

    return _tbd(f"Could not resolve {text!r} against the {ctx.calendar.term_name} calendar.")


def _resolve_week_number(
    week_number: int,
    text: str,
    explicit_time: time | None,
    ctx: ResolutionContext,
) -> ResolvedDate:
    """ "Week 6" — resolve to a meeting day if we know the pattern, else a week band."""
    try:
        week_start, week_end = ctx.calendar.week_bounds(week_number)
    except ValueError:
        return _tbd(f"Invalid week number in {text!r}.")

    if not ctx.calendar.contains(week_start) and not ctx.calendar.contains(
        week_end - timedelta(days=1)
    ):
        return _tbd(f"{text!r} falls outside the {ctx.calendar.term_name} term.")

    meeting = _first_meeting_in_week(week_start, week_end, ctx)
    if meeting is not None:
        return _build_dated(
            meeting,
            explicit_time,
            ctx,
            "week_number",
            notes=f"Week {week_number} resolved to the first class meeting that week.",
        )

    # No meeting pattern: precision is week_only. It renders as a band and never
    # collapses to a specific day (9.3) — this branch is the rule's whole point.
    return ResolvedDate(
        due_precision="week_only",
        resolution_case="week_number",
        week_start=week_start,
        week_end=week_end,
        notes=f"Week {week_number} of the term; no meeting pattern known, so shown as a band.",
    )


def _resolve_session_number(
    session_number: int,
    explicit_time: time | None,
    ctx: ResolutionContext,
) -> ResolvedDate:
    """ "Class 12" — count meeting-pattern occurrences from term start, skipping no-class days."""
    weekdays = ctx.meeting_weekdays
    if not weekdays:
        return _tbd(
            f"Class {session_number} needs a meeting pattern to resolve; "
            "none is known for this section."
        )
    meetings = ctx.calendar.meeting_days(weekdays)
    if session_number < 1 or session_number > len(meetings):
        return _tbd(
            f"Class {session_number} is outside the {len(meetings)} meetings in "
            f"{ctx.calendar.term_name}."
        )
    resolved = meetings[session_number - 1]
    # A class-session item happens at the class meeting time. That time is real, but it
    # comes from the registrar's meeting pattern rather than the syllabus sentence, so
    # it is marked inferred and renders muted (9.3).
    return _build_dated(
        resolved,
        explicit_time,
        ctx,
        "session_number",
        inferred_time=ctx.meeting_time,
        notes=f"Class {session_number} counted from term start, skipping no-class days.",
    )


def _match_relative(text: str, ctx: ResolutionContext) -> ResolvedDate | None:
    """ "the class after spring break", "one week after Midterm 1" (9.2).

    Returns None when the expression is not relative at all, so the caller
    falls through to absolute parsing.
    """
    match = _RELATIVE_RE.search(text)
    if not match:
        return None
    anchor_text = match.group("anchor").strip(" .,")
    if not anchor_text:
        return None

    direction = match.group("direction").lower()
    backwards = direction in ("before", "preceding") or direction.startswith("prior")
    unit = (match.group("unit") or "class").lower()
    count_raw = (match.group("count") or "1").lower()
    count = _WORD_NUMBERS.get(count_raw, None)
    if count is None:
        count = int(count_raw) if count_raw.isdigit() else 1

    anchor_date = _lookup_anchor(anchor_text, ctx)
    if anchor_date is None:
        # Round one could not resolve the anchor. `resolve_all` runs a second
        # round after other items land; anything still unresolved goes to review as tbd.
        return _tbd(
            f"Anchor {anchor_text!r} not resolved yet; retry after other items resolve.",
            case="relative_to_anchor",
        )

    sign = -1 if backwards else 1
    if unit.startswith("week"):
        resolved = anchor_date + timedelta(weeks=sign * count)
    elif unit.startswith("day"):
        resolved = anchor_date + timedelta(days=sign * count)
    else:
        # Counting meetings can fail where counting days cannot, so the result
        # is held separately until it is known to be a date. Assigning the
        # optional straight back into `resolved` makes the two cases the same
        # variable with different guarantees.
        by_meetings = _offset_by_meetings(anchor_date, sign * count, ctx)
        if by_meetings is None:
            return _tbd(
                f"{text!r} counts class meetings, but this section has no known meeting pattern.",
                case="relative_to_anchor",
            )
        resolved = by_meetings

    return _build_dated(
        resolved,
        _extract_time(text),
        ctx,
        "relative_to_anchor",
        notes=f"Resolved relative to {anchor_text!r} ({anchor_date.isoformat()}).",
    )


def _lookup_anchor(anchor_text: str, ctx: ResolutionContext) -> date | None:
    """Resolve an anchor phrase to a date: a calendar exception label, or another assessment."""
    needle = anchor_text.lower().strip()

    # Calendar labels first ("spring break", "reading day"). These come from the
    # registrar and are more reliable than a fuzzy assessment-title match. A break
    # spans several days, and "after spring break" means after the *last* of them.
    labeled = ctx.calendar.dates_for_label(needle)
    if labeled:
        return labeled[-1]

    if ctx.anchors:
        # Prefer an exact title match before falling back to substring containment,
        # so "Midterm 1" does not accidentally anchor to "Midterm 1 Review".
        for title, when in ctx.anchors.items():
            if title.lower() == needle:
                return when
        for title, when in ctx.anchors.items():
            t = title.lower()
            if needle in t or t in needle:
                return when
    return None


def _offset_by_meetings(anchor: date, offset: int, ctx: ResolutionContext) -> date | None:
    """Move N class meetings forward/back from a date (offset may be negative)."""
    weekdays = ctx.meeting_weekdays
    if not weekdays:
        return None
    meetings = ctx.calendar.meeting_days(weekdays)
    if not meetings:
        return None

    if offset >= 0:
        later = [m for m in meetings if m > anchor]
        if len(later) < offset:
            return None
        return later[offset - 1] if offset > 0 else anchor
    earlier = [m for m in meetings if m < anchor]
    steps = -offset
    if len(earlier) < steps:
        return None
    return earlier[-steps]


def expand_recurrence(
    recurrence_expression: str,
    ctx: ResolutionContext,
    *,
    item_count: int | None = None,
    drop_lowest: int = 0,
) -> RecurrenceResolution | None:
    """ "quizzes every Friday" -> RRULE + expanded dates, no-class days subtracted (9.2).

    `item_count` from the grade breakdown truncates the expansion when the
    schedule enumerates fewer occurrences than the term has weeks — the
    breakdown is the authority on how many items actually exist.
    """
    match = _RECURRENCE_RE.search(recurrence_expression) or _RECURRENCE_PLURAL_RE.search(
        recurrence_expression
    )
    if not match:
        return None
    weekday = _WEEKDAY_NAMES[match.group("day").lower()]

    occurrences = [d for d in ctx.calendar.meeting_days([weekday]) if ctx.calendar.contains(d)]
    if item_count is not None and item_count > 0:
        occurrences = occurrences[:item_count]

    return RecurrenceResolution(
        rrule=f"FREQ=WEEKLY;BYDAY={_RRULE_BYDAY[weekday]}",
        occurrences=occurrences,
        due_time_inferred=_extract_time(recurrence_expression) is None,
    )


def resolve_final_exam(
    meeting_pattern: str | None,
    ctx: ResolutionContext,
) -> ResolvedDate:
    """Resolve a final exam from the registrar matrix, not the syllabus text (9.5).

    Roughly half of syllabi say "see the university final exam schedule". Finals
    week is also the densest collision period of the term, so a heatmap that
    can't place these is missing the part the student most needs to see.
    """
    pattern = meeting_pattern or ctx.meeting_pattern
    if not pattern:
        return _tbd("Final exam needs a meeting pattern to look up in the registrar matrix.")

    wanted_days = set(parse_meeting_weekdays(pattern))
    if not wanted_days:
        return _tbd(f"Could not parse meeting pattern {pattern!r} for final exam lookup.")
    wanted_time = parse_meeting_time(pattern)

    # The matrix is keyed by day pattern *and* class time: an MWF 08:00 section and
    # an MWF 10:00 section sit in different exam slots. Matching on weekdays alone
    # would silently put the exam on the wrong day, which is worse than a `tbd`.
    day_matches = [
        entry
        for entry in ctx.calendar.final_exam_matrix
        if set(parse_meeting_weekdays(entry.meeting_pattern)) == wanted_days
    ]
    if not day_matches:
        return _tbd(
            f"No final exam matrix row matches meeting pattern {pattern!r}; "
            "degrade to tbd rather than guessing."
        )

    for entry in day_matches:
        if parse_meeting_time(entry.meeting_pattern) == wanted_time:
            return ResolvedDate(
                due_at=entry.exam_start_at,
                due_precision="exact_datetime",
                time_inferred=False,
                resolution_case="full_date",
                notes=f"From the registrar final exam matrix ({entry.meeting_pattern}).",
            )

    # Days match but no row carries this class time. Picking one of several
    # candidate slots would be a guess about which exam the student sits.
    if wanted_time is None:
        return _tbd(
            f"Meeting pattern {pattern!r} has no class time, and the matrix keys "
            f"{len(day_matches)} different slots on that day pattern."
        )
    return _tbd(
        f"No final exam matrix row for {pattern!r} at {wanted_time.strftime('%H:%M')}; "
        "confirm the section's meeting time."
    )


def resolve_all(
    expressions: list[tuple[str, str | None]],
    ctx: ResolutionContext,
) -> dict[str, ResolvedDate]:
    """Resolve a document's expressions in two rounds (9.2, "relative to an anchor").

    Round one resolves everything that stands alone. Round two retries the
    anchor-relative items against titles that round one placed. Anything still
    unresolved stays `tbd` and goes to review — there is no round three.
    """
    results: dict[str, ResolvedDate] = {}
    for title, expression in expressions:
        results[title] = resolve_date_expression(expression, ctx)

    anchors: dict[str, date] = {
        title or "": r.due_at.date() for title, r in results.items() if r.due_at is not None
    }
    if not anchors:
        return results

    round_two_ctx = ResolutionContext(
        calendar=ctx.calendar,
        meeting_pattern=ctx.meeting_pattern,
        anchors={**(ctx.anchors or {}), **anchors},
    )
    for title, expression in expressions:
        current = results[title]
        if current.due_precision == "tbd" and current.resolution_case == "relative_to_anchor":
            results[title] = resolve_date_expression(expression, round_two_ctx)
    return results
