"""Effort distribution and the weekly heatmap — PRD sections 11.3 and 6.3. Pure functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from workload_model.effort import (
    EffortInput,
    distribution_for,
    effort_hours,
    prep_window_days,
)


@dataclass(frozen=True)
class ScheduledItem:
    """One dated assessment, as the heatmap needs it.

    `due_on` is a plain date: the heatmap works in days, and an item's clock
    time never changes which week it lands in. Items with `due_precision`
    of `tbd` are not scheduled and must be filtered out before reaching here.
    """

    assessment_id: str
    title: str
    course_label: str
    assessment_type: str
    due_on: date
    credits: float = 3.0
    weight_pct: float | None = None
    is_group: bool = False
    user_calibration: float = 1.0
    #: For `week_only` items — spread across the band rather than pinned to a day.
    week_band: tuple[date, date] | None = None

    def effort(self) -> float:
        return effort_hours(
            EffortInput(
                assessment_type=self.assessment_type,
                credits=self.credits,
                weight_pct=self.weight_pct,
                is_group=self.is_group,
                user_calibration=self.user_calibration,
            )
        )


@dataclass
class DailyLoad:
    day: date
    hours: float = 0.0
    contributions: dict[str, float] = field(default_factory=dict)

    def add(self, assessment_id: str, hours: float) -> None:
        self.hours += hours
        self.contributions[assessment_id] = self.contributions.get(assessment_id, 0.0) + hours


@dataclass
class WeeklyCell:
    week_start: date
    effort_hours: float
    assessment_ids: list[str]


def distribute_effort(item: ScheduledItem) -> dict[date, float]:
    """Spread an item's hours across its prep window (11.3).

    Back-weighted: `weight(day_i) = (i + 1) / sum(1..n)`, so a 7-day window puts
    about 25% of the effort on the final day and about 3.5% on the first. Flat
    divides evenly.
    """
    total = item.effort()
    if total <= 0:
        return {}

    window = prep_window_days(item.assessment_type)
    if window <= 0:
        return {item.due_on: total}

    # A week_only item has no specific due day, so its window ends at the band's
    # end rather than at a fabricated date (9.3 carried into the workload model).
    end = item.week_band[1] - timedelta(days=1) if item.week_band else item.due_on
    days = [end - timedelta(days=offset) for offset in range(window - 1, -1, -1)]
    n = len(days)

    if distribution_for(item.assessment_type) == "flat":
        share = total / n
        return {d: share for d in days}

    denominator = n * (n + 1) / 2
    return {d: total * ((i + 1) / denominator) for i, d in enumerate(days)}


def baseline_weekly_hours(total_enrolled_credits: float) -> float:
    """`total_enrolled_credits * 2` (11.3).

    Reading, lecture attendance, routine coursework. This is what keeps a light
    week reading as "a week of school" rather than as empty.
    """
    return total_enrolled_credits * 2.0


def daily_loads(
    items: list[ScheduledItem],
    term_start: date,
    term_end: date,
    total_enrolled_credits: float,
) -> list[DailyLoad]:
    """Per-day effort across the term, assessments plus the baseline floor."""
    days: dict[date, DailyLoad] = {}
    cursor = term_start
    while cursor <= term_end:
        days[cursor] = DailyLoad(day=cursor)
        cursor += timedelta(days=1)

    baseline_per_day = baseline_weekly_hours(total_enrolled_credits) / 7.0
    for load in days.values():
        load.hours += baseline_per_day

    for item in items:
        for day, hours in distribute_effort(item).items():
            # Prep windows can start before the term does; clamp rather than drop,
            # so the hours still land on the first day the student could begin.
            target = min(max(day, term_start), term_end)
            if target in days:
                days[target].add(item.assessment_id, hours)

    return [days[d] for d in sorted(days)]


def build_heatmap(
    items: list[ScheduledItem],
    term_start: date,
    term_end: date,
    total_enrolled_credits: float,
) -> list[WeeklyCell]:
    """One cell per term week, colored by projected effort hours (US-6)."""
    loads = daily_loads(items, term_start, term_end, total_enrolled_credits)
    week_one_monday = term_start - timedelta(days=term_start.weekday())

    buckets: dict[date, WeeklyCell] = {}
    for load in loads:
        week_start = load.day - timedelta(days=load.day.weekday())
        cell = buckets.setdefault(
            week_start, WeeklyCell(week_start=week_start, effort_hours=0.0, assessment_ids=[])
        )
        cell.effort_hours += load.hours
        for assessment_id in load.contributions:
            if assessment_id not in cell.assessment_ids:
                cell.assessment_ids.append(assessment_id)

    # Guarantee an unbroken run of weeks even if a week has no load at all,
    # so the heatmap never renders with a hole in the middle of the term.
    cursor = week_one_monday
    while cursor <= term_end:
        buckets.setdefault(
            cursor, WeeklyCell(week_start=cursor, effort_hours=0.0, assessment_ids=[])
        )
        cursor += timedelta(days=7)

    return [buckets[k] for k in sorted(buckets)]
