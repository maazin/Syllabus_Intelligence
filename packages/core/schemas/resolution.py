"""Output contract for `date_resolver` (PRD section 9).

Kept separate from `schemas.extraction` because resolution is a distinct
stage: it consumes `date_expression_raw` plus the academic calendar and
produces a calendar-safe date. Section 9.3's rule is load-bearing here —
`week_only` and `tbd` must never carry a fabricated specific time.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

DuePrecision = Literal["exact_datetime", "date_only", "week_only", "tbd"]

ResolutionCase = Literal[
    "full_date",
    "date_without_year",
    "weekday_plus_date",
    "week_number",
    "session_number",
    "relative_to_anchor",
    "recurring",
    "unspecified",
]


class ResolvedDate(BaseModel):
    """Result of resolving one `date_expression_raw` (section 9.2)."""

    due_at: datetime | None = Field(
        default=None, description="None whenever due_precision is week_only or tbd."
    )
    due_precision: DuePrecision
    time_inferred: bool = Field(
        default=False, description="True when a default LMS due time (9.3) was applied."
    )
    weekday_mismatch: bool = Field(
        default=False, description="Weekday named does not match the resolved numeric date (9.2)."
    )
    needs_review: bool = Field(
        default=False,
        description="Resolution was ambiguous (e.g. year-boundary date, unresolved anchor) "
        "and should be surfaced in the review screen rather than trusted silently.",
    )
    resolution_case: ResolutionCase
    week_start: date | None = Field(
        default=None, description="Set for week_only precision — the band's start (inclusive)."
    )
    week_end: date | None = Field(
        default=None, description="Set for week_only precision — the band's end (exclusive)."
    )
    anchor_assessment_id: str | None = Field(
        default=None, description="Set when resolution_case == relative_to_anchor."
    )
    notes: str | None = None

    @model_validator(mode="after")
    def enforce_precision_invariants(self) -> ResolvedDate:
        if self.due_precision == "tbd" and self.due_at is not None:
            raise ValueError("tbd items must never carry a due_at (section 9.3)")
        if self.due_precision == "week_only":
            if self.due_at is not None:
                raise ValueError("week_only items must never carry a specific due_at (section 9.3)")
            if self.week_start is None or self.week_end is None:
                raise ValueError("week_only items must carry a week_start/week_end band")
        if self.due_precision == "exact_datetime" and self.due_at is None:
            raise ValueError("exact_datetime requires a due_at")
        return self


class RecurrenceResolution(BaseModel):
    """Result of expanding a `recurrence_raw` expression (section 9.2, "Recurring" row)."""

    rrule: str = Field(..., description="iCal RRULE string, e.g. 'FREQ=WEEKLY;BYDAY=FR'")
    occurrences: list[date] = Field(
        default_factory=list,
        description="Expanded across the term, no-class days already subtracted.",
    )
    due_time_inferred: bool = True
