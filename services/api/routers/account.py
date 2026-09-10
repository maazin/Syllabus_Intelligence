"""Account management — PRD section 15.2 and Appendix C.

Deletion is the whole of this router for now, and it is here rather than folded
into `auth` because it is a destructive, irreversible operation that deserves
its own reviewable surface.
"""

from __future__ import annotations

from db.models import User
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.api.account import delete_account
from services.api.deps import current_user, get_db

router = APIRouter(prefix="/account", tags=["account"])


class DeleteRequest(BaseModel):
    #: The client must echo the account's own email back. Deletion is
    #: irreversible and removes the student's whole term, so a stray DELETE
    #: should not be able to do it.
    confirm_email: str


class DeleteResponse(BaseModel):
    status: str
    documents_removed: int
    stored_files_removed: int
    enrollments_removed: int
    overrides_removed: int
    calendar_events_removed: int
    corrections_anonymized: int
    warnings: list[str]


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    """15.2: store the minimum — email, institution, enrollments, overrides."""
    return {
        "id": str(user.id),
        "email": user.email,
        "institution_id": str(user.institution_id),
        "verified": user.verified_at is not None,
        "calibration_factor": user.calibration_factor,
    }


@router.delete("", response_model=DeleteResponse)
def delete_me(
    payload: DeleteRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> DeleteResponse:
    """Remove documents, enrollments, overrides, and calendar events; revoke tokens.

    Corrections are retained in anonymized form (15.2): the row survives as eval
    data, the link to the person does not.
    """
    if payload.confirm_email.strip().lower() != user.email.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confirmation email does not match this account.",
        )

    report = delete_account(db, user.id)
    return DeleteResponse(
        status="deleted",
        documents_removed=report.documents,
        stored_files_removed=report.stored_files,
        enrollments_removed=report.enrollments,
        overrides_removed=report.overrides,
        calendar_events_removed=report.calendar_events,
        corrections_anonymized=report.corrections_anonymized,
        warnings=report.errors,
    )
