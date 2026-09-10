"""Assessment edits — PRD sections 23, 6.2 (US-4).

Two invariants make this endpoint more than a PATCH:

1. **User overrides persist and are never overwritten by a re-parse.** They go
   to `user_overrides`, a separate table, so a shared section-level parse can be
   re-run without destroying anyone's personal edits (section 12).
2. **Every correction is logged.** `corrections_log` is append-only and is the
   eval and improvement dataset — the PRD calls it a first-class product output,
   not debug telemetry, and says never to garbage-collect it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from db.models import Assessment, CorrectionLog, Enrollment, User, UserOverride
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.deps import current_user, get_db

router = APIRouter(tags=["assessments"])

#: Only fields a student can meaningfully correct in review. `source_span`,
#: `confidence`, and `page_ref` are provenance and are deliberately not editable.
EDITABLE_FIELDS = {"due_at", "title", "effort_hours", "dismissed"}


class AssessmentPatch(BaseModel):
    field: str
    value: Any

    @field_validator("field")
    @classmethod
    def field_is_editable(cls, v: str) -> str:
        if v not in EDITABLE_FIELDS:
            raise ValueError(f"{v!r} is not editable. Editable fields: {sorted(EDITABLE_FIELDS)}")
        return v


@router.patch("/assessments/{assessment_id}", status_code=status.HTTP_200_OK)
def patch_assessment(
    assessment_id: uuid.UUID,
    patch: AssessmentPatch,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    assessment = db.scalar(select(Assessment).where(Assessment.id == assessment_id))
    if assessment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment not found")

    # A student may only edit items in a section they are enrolled in — the
    # assessment row itself is shared across everyone in that section.
    enrolled = db.scalar(
        select(Enrollment).where(
            Enrollment.user_id == user.id,
            Enrollment.section_id == assessment.section_id,
        )
    )
    if enrolled is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not enrolled in this section")

    override = db.scalar(
        select(UserOverride).where(
            UserOverride.user_id == user.id,
            UserOverride.assessment_id == assessment.id,
        )
    )
    if override is None:
        override = UserOverride(user_id=user.id, assessment_id=assessment.id)
        db.add(override)

    old_value = getattr(override, patch.field, None)
    if old_value is None:
        # Nothing overridden yet, so the "old" value is what extraction produced.
        old_value = getattr(assessment, patch.field, None)

    new_value = patch.value
    if patch.field == "due_at" and isinstance(new_value, str):
        try:
            new_value = datetime.fromisoformat(new_value)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Invalid datetime: {patch.value!r}"
            ) from exc

    setattr(override, patch.field, new_value)

    prompt_version = None
    if assessment.document and assessment.document.extraction_runs:
        prompt_version = assessment.document.extraction_runs[-1].prompt_version

    db.add(
        CorrectionLog(
            user_id=user.id,
            assessment_id=assessment.id,
            field=patch.field,
            old_value=None if old_value is None else str(old_value),
            new_value=None if new_value is None else str(new_value),
            source_span=assessment.source_span,
            prompt_version=prompt_version,
        )
    )
    db.commit()

    return {
        "status": "updated",
        "field": patch.field,
        "old_value": None if old_value is None else str(old_value),
        "new_value": None if new_value is None else str(new_value),
    }
