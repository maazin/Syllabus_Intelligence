"""Upload, parse status, and review — PRD sections 23, 6.1, 6.2.

The upload endpoint does three things and stops: store the bytes, dedupe by
content hash, enqueue the parse. Everything after that is the worker's job
(section 21). Notably, one file failing must never block the other four
(US-1), so per-file errors are captured into the response rather than raised.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from db.models import Assessment, Enrollment, Section, SyllabusDocument, User
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.deps import current_user, get_db
from services.api.queue import enqueue_parse
from services.api.storage import store_document

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])

#: US-1's stated limits.
MAX_FILES = 10
MAX_FILE_BYTES = 25 * 1024 * 1024

ACCEPTED_SUFFIXES = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".heic", ".txt", ".html", ".htm"}


class DocumentAccepted(BaseModel):
    document_id: str | None
    filename: str
    status: str
    deduped: bool = False
    error: str | None = None


class CourseMatch(BaseModel):
    confidence: float
    candidates: list[dict[str, Any]] = []


class DocumentStatus(BaseModel):
    document_id: str
    status: str
    course_match: CourseMatch | None = None
    error: str | None = None


@router.post(
    "/documents",
    response_model=list[DocumentAccepted],
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_documents(
    files: list[UploadFile] = File(...),
    section_id: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[DocumentAccepted]:
    if len(files) > MAX_FILES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Upload at most {MAX_FILES} files at a time."
        )

    results: list[DocumentAccepted] = []
    for upload in files:
        # Per-file try/except, not a batch abort: US-1 requires one failure to
        # leave the other four parsing.
        try:
            data = await upload.read()
            if len(data) > MAX_FILE_BYTES:
                raise ValueError(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
            if not data:
                raise ValueError("File is empty")

            document, deduped = store_document(
                db,
                user=user,
                filename=upload.filename or "upload",
                data=data,
                section_id=section_id,
            )
            # A deduped upload already has a parse (18.3), so it skips the queue
            # entirely — which is why later uploaders in a section get instant
            # results. Only a genuinely new document costs tokens.
            # Uploading a syllabus for a section is an implicit statement of
            # enrollment; without it the timeline joins through `enrollments`
            # and comes back empty (US-1 -> US-5).
            if document.section_id is not None:
                ensure_enrollment(db, user, document.section_id)

            if not deduped:
                db.flush()
                enqueue_parse(document.id)

            results.append(
                DocumentAccepted(
                    document_id=str(document.id),
                    filename=upload.filename or "upload",
                    status="ready" if deduped else "queued",
                    deduped=deduped,
                )
            )
        except Exception as exc:
            logger.exception("Upload failed for %s", upload.filename)
            results.append(
                DocumentAccepted(
                    document_id=None,
                    filename=upload.filename or "upload",
                    status="failed",
                    error=str(exc),
                )
            )

    db.commit()
    return results


@router.get("/documents/{document_id}", response_model=DocumentStatus)
def get_document_status(
    document_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> DocumentStatus:
    """Parse status for one document.

    Section 23.1 specifies SSE for this rather than client polling — five
    documents parsing in parallel with independent state is exactly the shape
    SSE fits. This polling form is the documented fallback; the SSE variant
    belongs here once the worker publishes progress events.
    """
    document = _owned_document(db, document_id, user)
    latest = sorted(document.extraction_runs, key=lambda r: r.created_at)[-1:] or None
    state = latest[0].status if latest else "queued"
    return DocumentStatus(document_id=str(document.id), status=state)


@router.post("/documents/{document_id}/confirm-course", status_code=status.HTTP_200_OK)
def confirm_course(
    document_id: uuid.UUID,
    section_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """US-1: match confidence below 0.8 prompts the user to confirm from a dropdown."""
    document = _owned_document(db, document_id, user)
    document.section_id = section_id
    ensure_enrollment(db, user, section_id)
    db.commit()
    return {"status": "confirmed"}


def ensure_enrollment(db: Session, user: User, section_id: uuid.UUID) -> bool:
    """Enroll the uploader in the section this document belongs to.

    Uploading a syllabus *is* the statement "I am taking this course" — US-1 says
    a student uploads five files and US-5 says they then see five courses on one
    timeline. Without this, the timeline, heatmap, and calendar feed all join
    through `enrollments` and come back empty after a successful parse, which
    reads as the product silently losing the upload.

    Returns True if a new enrollment was created.
    """
    section = db.scalar(select(Section).where(Section.id == section_id))
    if section is None:
        return False

    existing = db.scalar(
        select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.section_id == section_id)
    )
    if existing is not None:
        return False

    db.add(
        Enrollment(
            user_id=user.id,
            section_id=section_id,
            term_id=section.term_id,
            # Inferred from the upload rather than confirmed by the registrar.
            # The student can remove it; `confirmed` distinguishes the two.
            confirmed=False,
        )
    )
    return True


@router.post("/documents/{document_id}/review-complete", status_code=status.HTTP_200_OK)
def complete_review(
    document_id: uuid.UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Unlock calendar writes for this document's items.

    US-3 is explicit that nothing writes to the student's real calendar until
    review is completed for that document. This endpoint is that gate.
    """
    from datetime import UTC, datetime

    document = _owned_document(db, document_id, user)
    assessments = db.scalars(select(Assessment).where(Assessment.document_id == document.id)).all()
    if not assessments:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This document has no extracted items to review yet.",
        )

    now = datetime.now(UTC)
    for assessment in assessments:
        if assessment.verified_at is None:
            assessment.verified_by_user_id = user.id
            assessment.verified_at = now
    db.commit()
    return {"status": "review_complete", "verified": len(assessments)}


def _owned_document(db: Session, document_id: uuid.UUID, user: User) -> SyllabusDocument:
    """Fetch a document, refusing to leak another student's upload.

    15.1: original uploaded files stay private to the uploader and are never
    served to other users.
    """
    document = db.scalar(select(SyllabusDocument).where(SyllabusDocument.id == document_id))
    if document is None or document.uploader_user_id != user.id:
        # Same 404 either way, so a wrong id cannot be distinguished from
        # someone else's document.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return document
