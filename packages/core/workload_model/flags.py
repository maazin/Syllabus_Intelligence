"""Collision flags — PRD section 11.4. Pure functions.

Boundaries are written with explicit inclusive/exclusive comparisons because the
PRD says so and gives the reason: a hand-constructed test case (three midterms in
four days on a 15-credit load) lands exactly on 1.8x, and an ambiguous comparison
there produces a flaky test. `>=` and `<` here are load-bearing, not stylistic.

Every flag carries a plain-language explanation. Per the PRD that sentence is the
growth mechanism — "Week of Oct 12: three items worth 45 percent of your grade,
all within four days" is what gets screenshotted into a group chat — so the
`explanation` field is product copy, not debug output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Literal

from workload_model.heatmap import ScheduledItem, WeeklyCell, daily_loads

FlagSeverity = Literal["yellow", "red"]

FlagKind = Literal[
    "crunch_week",
    "severe_crunch",
    "major_collision",
    "deadline_stack",
    "grade_concentration",
]

#: Severity ordering for "display the highest severity and list the rest" (11.4).
_SEVERITY_RANK: dict[FlagSeverity, int] = {"yellow": 1, "red": 2}

_HIGH_STAKES_TYPES = frozenset({"midterm", "final_exam", "final_project"})


@dataclass
class Flag:
    kind: FlagKind
    severity: FlagSeverity
    week_start: date
    explanation: str
    assessment_ids: list[str] = field(default_factory=list)

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self.severity]


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f"{singular}s")


def _fmt_week(week_start: date) -> str:
    return week_start.strftime("%b %-d") if hasattr(week_start, "strftime") else str(week_start)


def _span_days(days: list[date]) -> int:
    """Inclusive day span of a cluster, e.g. Mon..Thu -> 4."""
    return (max(days) - min(days)).days + 1


def compute_flags(
    items: list[ScheduledItem],
    weekly_cells: list[WeeklyCell],
    term_start: date,
    term_end: date,
    total_enrolled_credits: float,
) -> dict[date, list[Flag]]:
    """All flags for the term, keyed by week start and sorted highest-severity first.

    `median weekly load` is computed across the whole term (11.4), so it shifts
    when a course is added — callers must recompute on any enrollment change
    rather than caching this per document.
    """
    flags: list[Flag] = []
    flags.extend(_rolling_load_flags(items, term_start, term_end, total_enrolled_credits))
    flags.extend(_major_collision_flags(items))
    flags.extend(_deadline_stack_flags(items))
    flags.extend(_grade_concentration_flags(items))

    by_week: dict[date, list[Flag]] = {}
    for flag in flags:
        by_week.setdefault(flag.week_start, []).append(flag)
    for week_flags in by_week.values():
        week_flags.sort(key=lambda f: (-f.rank, f.kind))
    return by_week


def _week_start_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _rolling_load_flags(
    items: list[ScheduledItem],
    term_start: date,
    term_end: date,
    total_enrolled_credits: float,
) -> list[Flag]:
    """Crunch week (>=1.4x, <1.8x median) and severe crunch (>=1.8x median)."""
    loads = daily_loads(items, term_start, term_end, total_enrolled_credits)
    if not loads:
        return []

    hours_by_day = {load.day: load.hours for load in loads}
    ordered_days = sorted(hours_by_day)

    # Rolling 7-day totals, one per window start.
    rolling: dict[date, float] = {}
    contributions: dict[date, list[str]] = {}
    for i, day in enumerate(ordered_days):
        window = ordered_days[i : i + 7]
        if len(window) < 7:
            break  # a partial tail window is not a 7-day load; do not flag on it
        rolling[day] = sum(hours_by_day[d] for d in window)
        ids: list[str] = []
        for d in window:
            for assessment_id in loads[ordered_days.index(d)].contributions:
                if assessment_id not in ids:
                    ids.append(assessment_id)
        contributions[day] = ids

    if not rolling:
        return []

    # The comparison baseline is the student's median *weekly* load. Using the
    # median of rolling windows rather than of calendar weeks keeps it stable
    # against where the term happens to start in the week.
    baseline = median(rolling.values())
    if baseline <= 0:
        return []

    # One flag per calendar week: take that week's worst rolling window.
    #
    # A window is attributed to the week containing its *midpoint*, not its start.
    # A window running Nov 14-20 sits mostly inside the week of Nov 16, and
    # anchoring it to the week of Nov 9 would put the badge on a cell the crunch
    # barely touches.
    worst_by_week: dict[date, tuple[float, date]] = {}
    for window_start, total in rolling.items():
        week_start = _week_start_of(window_start + timedelta(days=3))
        current = worst_by_week.get(week_start)
        if current is None or total > current[0]:
            worst_by_week[week_start] = (total, window_start)

    out: list[Flag] = []
    for week_start, (total, window_start) in sorted(worst_by_week.items()):
        ratio = total / baseline
        if ratio < 1.4:
            continue

        # Name the actual 7-day window, not the calendar week. These flags are
        # defined on a *rolling* window (11.4) while heatmap cells are calendar
        # weeks, so the two numbers legitimately differ — and a badge reading
        # "about 14 hours" on a cell labelled "8.9h" reads as a contradiction
        # unless the span it refers to is stated.
        window_end = window_start + timedelta(days=6)
        span = f"{_fmt_week(window_start)}–{_fmt_week(window_end)}"

        severe = ratio >= 1.8
        out.append(
            Flag(
                kind="severe_crunch" if severe else "crunch_week",
                severity="red" if severe else "yellow",
                week_start=week_start,
                explanation=(
                    f"{span}: about {total:.0f} hours of work in 7 days, "
                    f"{ratio:.1f}x your typical week."
                ),
                assessment_ids=contributions.get(window_start, []),
            )
        )
    return out


def _clusters_within(items: list[ScheduledItem], hours: int) -> list[list[ScheduledItem]]:
    """Maximal groups of items whose due dates all fall within `hours` of each other."""
    if not items:
        return []
    window = timedelta(hours=hours)
    ordered = sorted(items, key=lambda i: i.due_on)
    out: list[list[ScheduledItem]] = []
    for i, anchor in enumerate(ordered):
        cluster = [
            other
            for other in ordered[i:]
            if timedelta(days=(other.due_on - anchor.due_on).days) <= window
        ]
        if len(cluster) > 1:
            out.append(cluster)
    return out


def _dedupe_clusters(clusters: list[list[ScheduledItem]]) -> list[list[ScheduledItem]]:
    """Drop clusters wholly contained in a larger one, so one crunch fires one flag."""
    kept: list[list[ScheduledItem]] = []
    as_sets = [({i.assessment_id for i in c}, c) for c in clusters]
    as_sets.sort(key=lambda pair: len(pair[0]), reverse=True)
    seen: list[set[str]] = []
    for ids, cluster in as_sets:
        if any(ids <= existing for existing in seen):
            continue
        seen.append(ids)
        kept.append(cluster)
    return kept


def _major_collision_flags(items: list[ScheduledItem]) -> list[Flag]:
    """2+ high-stakes items (weight >= 15, or midterm/final/final project) within 72 hours."""
    high_stakes = [
        i
        for i in items
        if (i.weight_pct is not None and i.weight_pct >= 15)
        or i.assessment_type in _HIGH_STAKES_TYPES
    ]
    out: list[Flag] = []
    for cluster in _dedupe_clusters(_clusters_within(high_stakes, hours=72)):
        if len(cluster) < 2:
            continue
        span = _span_days([i.due_on for i in cluster])
        week_start = _week_start_of(min(i.due_on for i in cluster))
        courses = {i.course_label for i in cluster}
        total_weight = sum(i.weight_pct or 0 for i in cluster)

        # Percentages from different courses are not commensurable — 20% of one
        # course plus 25% of another is not "45% of your grade". Only sum them
        # when the cluster sits inside a single course; otherwise count courses.
        if len(courses) == 1 and total_weight > 0:
            weight_clause = f" worth {total_weight:.0f}% of your grade"
        elif len(courses) > 1:
            weight_clause = f" across {len(courses)} courses"
        else:
            weight_clause = ""

        out.append(
            Flag(
                kind="major_collision",
                severity="red",
                week_start=week_start,
                explanation=(
                    f"Week of {_fmt_week(week_start)}: {len(cluster)} major "
                    f"{_plural(len(cluster), 'item')}{weight_clause}, all within "
                    f"{span} {_plural(span, 'day')}."
                ),
                assessment_ids=[i.assessment_id for i in cluster],
            )
        )
    return out


def _deadline_stack_flags(items: list[ScheduledItem]) -> list[Flag]:
    """4+ items of any type due within 48 hours."""
    out: list[Flag] = []
    for cluster in _dedupe_clusters(_clusters_within(items, hours=48)):
        if len(cluster) < 4:
            continue
        span = _span_days([i.due_on for i in cluster])
        week_start = _week_start_of(min(i.due_on for i in cluster))
        out.append(
            Flag(
                kind="deadline_stack",
                severity="yellow",
                week_start=week_start,
                explanation=(
                    f"Week of {_fmt_week(week_start)}: {len(cluster)} things due within "
                    f"{span} {_plural(span, 'day')}."
                ),
                assessment_ids=[i.assessment_id for i in cluster],
            )
        )
    return out


def _grade_concentration_flags(items: list[ScheduledItem]) -> list[Flag]:
    """Any 7-day window holding >= 35% of a *single* course's grade."""
    out: list[Flag] = []
    by_course: dict[str, list[ScheduledItem]] = {}
    for item in items:
        by_course.setdefault(item.course_label, []).append(item)

    for course_label, course_items in by_course.items():
        ordered = sorted(course_items, key=lambda i: i.due_on)
        best: tuple[float, list[ScheduledItem]] | None = None
        for i, anchor in enumerate(ordered):
            window = [other for other in ordered[i:] if (other.due_on - anchor.due_on).days <= 6]
            total = sum(x.weight_pct or 0 for x in window)
            if total >= 35 and (best is None or total > best[0]):
                best = (total, window)
        if best is None:
            continue
        total, window = best
        week_start = _week_start_of(min(i.due_on for i in window))
        span = _span_days([i.due_on for i in window])
        out.append(
            Flag(
                kind="grade_concentration",
                severity="red",
                week_start=week_start,
                explanation=(
                    f"Week of {_fmt_week(week_start)}: {len(window)} "
                    f"{_plural(len(window), 'item')} worth {total:.0f}% of your "
                    f"{course_label} grade, all within {span} {_plural(span, 'day')}."
                ),
                assessment_ids=[i.assessment_id for i in window],
            )
        )
    return out
