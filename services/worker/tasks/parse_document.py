"""The Celery task that turns an uploaded file into reviewable rows.

    fetch from R2 -> ingest (§7) -> extract A+B (§8) -> resolve dates (§9)
                  -> validate (§9.4) -> score confidence (§10.1)
                  -> estimate effort (§11) -> persist (§12)

This is the seam between the HTTP surface and everything that thinks. The API
enqueues this and returns immediately; US-1 wants the UI usable while parsing
runs, with median end-to-end under 45 seconds and p95 under 3 minutes.

Failure policy: a document that cannot be parsed is marked failed with a reason
the student can act on, and the other four in the batch are unaffected (US-1).
A hard failure routes to manual entry with an apology (§9.4) rather than
showing an empty result.
"""

from __future__ import annotations

import logging
import uuid

from db import objectstore
from db.course_match import current_term, describe, match_section
from db.models import Enrollment, Section, SyllabusDocument, User
from db.session import SessionLocal
from schemas.extraction import ExtractionOutput
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.worker.celery_app import app
from services.worker.tasks.extract import extract_document
from services.worker.tasks.ingest import IngestionError, ingest
from services.worker.tasks.persist import (
    load_calendar,
    persist_extraction,
    record_extraction_run,
)
from services.worker.tasks.pipeline import resolve_assessments
from services.worker.tasks.replay import ReplayError
from services.worker.tasks.validate import composite_confidence, validate_extraction

logger = logging.getLogger(__name__)


class ParseFailure(Exception):
    """A document could not be turned into reviewable rows."""


@app.task(name="documents.parse", bind=True, max_retries=2)
def parse_document(self, document_id: str) -> dict:
    """Parse one uploaded document end to end.

    Retries only on transient failures. An unparseable document is a permanent
    outcome and retrying it just burns tokens — so `IngestionError` and a hard
    validation failure are recorded and returned, never re-queued.
    """
    with SessionLocal() as db:
        document = db.scalar(
            select(SyllabusDocument).where(SyllabusDocument.id == uuid.UUID(document_id))
        )
        if document is None:
            raise ParseFailure(f"No document {document_id}")

        try:
            return _run(db, document)
        except (IngestionError, ReplayError) as exc:
            # Both are permanent for this document. Retrying an unparseable file
            # burns tokens; retrying a replay with no matching recording just
            # delays the same answer by a minute.
            logger.warning("Parse cannot proceed for %s: %s", document_id, exc)
            record_extraction_run(
                db,
                document=document,
                model="-",
                prompt_version="-",
                pass_name="ingest" if isinstance(exc, IngestionError) else "replay",
                status="failed",
                raw_output={"error": str(exc)},
            )
            db.commit()
            return {"document_id": document_id, "status": "failed", "error": str(exc)}
        except Exception as exc:
            logger.exception("Parse failed for %s", document_id)
            db.rollback()
            if self.request.retries >= self.max_retries:
                # Out of attempts. Without a terminal row the status endpoint
                # reports "queued" forever and the review screen spins with it.
                record_extraction_run(
                    db,
                    document=document,
                    model="-",
                    prompt_version="-",
                    pass_name="cascade",
                    status="failed",
                    raw_output={"error": f"Gave up after {self.max_retries + 1} attempts: {exc}"},
                )
                db.commit()
                return {"document_id": document_id, "status": "failed", "error": str(exc)}
            # Transient (network, rate limit, DB blip) — worth one more attempt.
            raise self.retry(exc=exc, countdown=30) from exc


def _run(db: Session, document: SyllabusDocument) -> dict:
    data = objectstore.get(document.storage_key)
    ingested = ingest(document.storage_key.rsplit("/", 1)[-1], data)

    document.page_count = ingested.page_count
    document.is_scanned = ingested.is_scanned

    # A document that came back from "which course is this?" already has its
    # extraction on the previous run. Reusing it is not an optimisation: a
    # second model call on the same text can produce a different item list,
    # and the student would be confirming a course for one extraction and
    # then reviewing another.
    result = _retained_extraction(document) or extract_document(ingested)

    # One row per model call, carrying the prompt version that produced it (§16).
    for run in result.runs:
        record_extraction_run(
            db,
            document=document,
            model=run.model,
            prompt_version=result.prompt_version,
            pass_name="cascade",
            status="succeeded",
            cost_cents=run.cost_cents,
            latency_ms=run.latency_ms,
        )

    # US-1: the section comes from the document, not from the upload form.
    # Below the threshold the extraction is kept and the student is asked.
    section = _section_for(db, document, result)
    if section is None:
        return {
            "document_id": str(document.id),
            "status": "needs_course",
            "assessments": len(result.output.assessments),
            "model": result.model,
        }

    calendar = load_calendar(db, section.term_id)
    from date_resolver.resolver import ResolutionContext

    ctx = ResolutionContext(
        calendar=calendar,
        meeting_pattern=section.meeting_pattern or result.output.course.meeting_pattern_raw,
    )
    resolutions = resolve_assessments(result.output, ctx)

    source_text = ingested.full_text
    flags = validate_extraction(
        result.output,
        page_count=ingested.page_count,
        source_text=source_text,
        resolved=resolutions,
        term_start=calendar.start_date,
        term_end=calendar.finals_end or calendar.end_date,
    )
    flagged = {title for f in flags for title in f.assessment_ids}

    confidences = {
        item.title: composite_confidence(
            item,
            source_text=source_text,
            resolution=resolutions.get(item.title),
            survived_validation=item.title not in flagged,
        )
        for item in result.output.assessments
    }
    efforts = _effort_by_title(result.output, section)

    persist_extraction(
        db,
        document=document,
        section=section,
        extraction=result.output,
        resolutions=resolutions,
        confidences=confidences,
        effort_hours=efforts,
    )
    hard_failure = any(f.severity == "hard_failure" for f in flags)
    # The terminal row is what the status endpoint reads. A replayed or
    # retained extraction records no model calls above, so without this a
    # parsed document would report "queued" forever.
    record_extraction_run(
        db,
        document=document,
        model=result.model,
        prompt_version=result.prompt_version,
        pass_name="persist",
        status="needs_manual_entry" if hard_failure else "succeeded",
        cost_cents=result.cost_cents,
        latency_ms=result.latency_ms,
    )
    db.commit()

    return {
        "document_id": str(document.id),
        "status": "needs_manual_entry" if hard_failure else "parsed",
        "assessments": len(result.output.assessments),
        "flags": [f.check for f in flags],
        "model": result.model,
        "escalated": result.escalated,
        "cost_cents": result.cost_cents,
    }


def _effort_by_title(extraction, section: Section) -> dict[str, float]:
    """Precompute §11.2 effort so the heatmap does not recompute it per request."""
    from workload_model.effort import EffortInput, effort_hours

    credits = section.course.credits if section.course else 3.0
    return {
        item.title: effort_hours(
            EffortInput(
                assessment_type=item.type,
                credits=credits,
                weight_pct=item.weight_pct,
                is_group=item.is_group,
            )
        )
        for item in extraction.assessments
    }


def _retained_extraction(document: SyllabusDocument):
    """The extraction kept on a `needs_course` run, if this is the second pass."""
    from services.worker.tasks.extract import ExtractionResult

    runs = sorted(document.extraction_runs, key=lambda r: r.created_at)
    if not runs or runs[-1].status != "needs_course":
        return None
    retained = runs[-1]
    if retained.raw_output is None:
        return None
    payload = dict(retained.raw_output)
    payload.pop("_match", None)
    logger.info("Reusing the extraction retained on run %s for %s", retained.id, document.id)
    return ExtractionResult(
        output=ExtractionOutput.model_validate(payload),
        prompt_version=retained.prompt_version,
        model=retained.model,
        runs=[],
    )


def _section_for(db: Session, document: SyllabusDocument, result) -> Section | None:
    """Resolve the section, asking the student when the evidence is thin.

    Returns None after recording a `needs_course` run that retains the
    extraction and the candidate list, so the API can present the choice and
    the second pass can skip the model.
    """
    if document.section_id is not None:
        section = db.scalar(select(Section).where(Section.id == document.section_id))
        if section is None:
            raise ParseFailure(f"Section {document.section_id} no longer exists")
        return section

    uploader = db.scalar(select(User).where(User.id == document.uploader_user_id))
    if uploader is None:
        raise ParseFailure(f"Uploader of {document.id} no longer exists")
    term = current_term(db, uploader.institution_id)
    if term is None:
        raise ParseFailure("No term is configured for this institution (section 9.1)")

    course = result.output.course
    match = match_section(
        db,
        term=term,
        subject_code=course.subject_code,
        catalog_number=course.catalog_number,
        section_code=course.section_code,
        instructor_name=course.instructor.name if course.instructor else None,
    )
    if match.decided and match.section is not None:
        document.section_id = match.section.id
        _enrol(db, uploader, match.section)
        logger.info("Matched %s to %s at %.2f", document.id, match.section.id, match.confidence)
        return match.section

    # Retain the extraction with the run so the second pass does not pay for
    # it again, and the candidates so the API can present them without
    # re-deriving the match.
    record_extraction_run(
        db,
        document=document,
        model=result.model,
        prompt_version=result.prompt_version,
        pass_name="course_match",
        status="needs_course",
        cost_cents=result.cost_cents,
        latency_ms=result.latency_ms,
        raw_output={
            **result.output.model_dump(mode="json"),
            "_match": {
                "confidence": match.confidence,
                "guess": " ".join(p for p in (course.subject_code, course.catalog_number) if p),
                "candidates": [describe(s) for s in match.candidates],
            },
        },
    )
    db.commit()
    logger.info(
        "Document %s needs course confirmation (confidence %.2f, %d candidates)",
        document.id,
        match.confidence,
        len(match.candidates),
    )
    return None


def _enrol(db: Session, user: User, section: Section) -> None:
    """Uploading a syllabus is the statement "I am taking this course" (US-1)."""
    existing = db.scalar(
        select(Enrollment).where(Enrollment.user_id == user.id, Enrollment.section_id == section.id)
    )
    if existing is None:
        db.add(
            Enrollment(
                user_id=user.id, section_id=section.id, term_id=section.term_id, confirmed=False
            )
        )
