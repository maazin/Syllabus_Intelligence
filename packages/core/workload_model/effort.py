"""Effort estimation — PRD section 11.1 and 11.2. Pure functions.

Every constant in this file is a guess until the corpus calibrates it (11.5).
Two consequences the UI must honor: label the heatmap as an estimate, and ship
the "took way more/less time than estimated" control in P1. Do not present
these numbers as measurements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Distribution = Literal["flat", "back_weighted"]


@dataclass(frozen=True)
class EffortProfile:
    base_hours: float
    prep_window_days: int
    distribution: Distribution


#: Section 11.1, verbatim.
BASE_EFFORT: dict[str, EffortProfile] = {
    "final_exam": EffortProfile(12, 10, "back_weighted"),
    "midterm": EffortProfile(8, 7, "back_weighted"),
    "final_project": EffortProfile(20, 21, "back_weighted"),
    "paper_long": EffortProfile(14, 14, "back_weighted"),
    "presentation": EffortProfile(5, 7, "back_weighted"),
    "paper_short": EffortProfile(6, 7, "back_weighted"),
    "project_milestone": EffortProfile(6, 10, "flat"),
    "problem_set": EffortProfile(4, 4, "back_weighted"),
    "lab_report": EffortProfile(3, 3, "flat"),
    "quiz": EffortProfile(1.5, 2, "flat"),
    "reading_response": EffortProfile(1, 2, "flat"),
    "discussion_post": EffortProfile(0.75, 1, "flat"),
    # Participation is continuous, not an event; it contributes via the baseline
    # load in 11.3 rather than as a dated item with a prep window.
    "participation": EffortProfile(0, 0, "flat"),
    "other": EffortProfile(2, 3, "flat"),
}

#: Seed values for 11.2's `weight_multiplier`. Replaced by corpus-derived
#: values once Layer 2 has enough verified profiles to compute them.
TYPICAL_WEIGHT: dict[str, float] = {
    "final_exam": 25,
    "midterm": 20,
    "final_project": 25,
    "paper_long": 20,
    "presentation": 10,
    "paper_short": 10,
    "project_milestone": 8,
    "problem_set": 3,
    "lab_report": 3,
    "quiz": 2,
    "reading_response": 2,
    "discussion_post": 1,
    "participation": 5,
    "other": 5,
}


@dataclass(frozen=True)
class EffortInput:
    assessment_type: str
    credits: float = 3.0
    weight_pct: float | None = None
    is_group: bool = False
    user_calibration: float = 1.0


def credit_multiplier(credits: float) -> float:
    """1cr 0.5 | 3cr 1.0 | 4cr 1.25 | 5cr 1.5 (11.2).

    Interpolates between the stated anchors so a 2-credit course does not fall
    off the table, and clamps outside the stated range.
    """
    anchors = [(1.0, 0.5), (3.0, 1.0), (4.0, 1.25), (5.0, 1.5)]
    if credits <= anchors[0][0]:
        return anchors[0][1]
    if credits >= anchors[-1][0]:
        return anchors[-1][1]
    for (c0, m0), (c1, m1) in zip(anchors, anchors[1:], strict=False):
        if c0 <= credits <= c1:
            span = c1 - c0
            return m0 + (m1 - m0) * ((credits - c0) / span)
    return 1.0


def weight_multiplier(assessment_type: str, weight_pct: float | None) -> float:
    """clamp(weight_pct / typical_weight[type], 0.5, 2.0) (11.2).

    An unstated weight is not evidence of a small item, so it scores neutral (1.0)
    rather than being penalized down to the 0.5 floor.
    """
    if weight_pct is None:
        return 1.0
    typical = TYPICAL_WEIGHT.get(assessment_type, TYPICAL_WEIGHT["other"])
    if typical <= 0:
        return 1.0
    return max(0.5, min(2.0, weight_pct / typical))


def effort_hours(item: EffortInput) -> float:
    """The full 11.2 scaling chain."""
    profile = BASE_EFFORT.get(item.assessment_type, BASE_EFFORT["other"])
    return (
        profile.base_hours
        * credit_multiplier(item.credits)
        * weight_multiplier(item.assessment_type, item.weight_pct)
        * (1.3 if item.is_group else 1.0)
        * item.user_calibration
    )


def prep_window_days(assessment_type: str) -> int:
    return BASE_EFFORT.get(assessment_type, BASE_EFFORT["other"]).prep_window_days


def distribution_for(assessment_type: str) -> Distribution:
    return BASE_EFFORT.get(assessment_type, BASE_EFFORT["other"]).distribution
