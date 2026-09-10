"""Output contract for the validation rules in PRD section 9.4."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ValidationSeverity = Literal["info", "warning", "hard_failure"]

ValidationCheck = Literal[
    "weights_out_of_range",
    "item_count_mismatch",
    "date_outside_term",
    "weekday_mismatch",
    "exams_too_close",
    "weight_over_threshold",
    "zero_assessments_extracted",
]


class ValidationFlag(BaseModel):
    check: ValidationCheck
    severity: ValidationSeverity
    message: str
    assessment_ids: list[str] = []
    category: str | None = None
