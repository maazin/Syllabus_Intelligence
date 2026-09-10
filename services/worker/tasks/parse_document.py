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

from db.models import Section, SyllabusDocument
from db.session import SessionLocal
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
        except IngestionError as exc:
            logger.warning("Ingestion failed for %s: %s", document_id, exc)
            record_extraction_run(
                db,
                document=document,
                model="-",
                prompt_version="-",
                pass_name="ingest",
                status="failed",
            )
            db.commit()
            return {"document_id": document_id, "status": "failed", "error": str(exc)}
        except Exception as exc:
            logger.exception("Parse failed for %s", document_id)
            db.rollback()
            # Transient (network, rate limit, DB blip) — worth one more attempt.
            raise self.retry(exc=exc, countdown=30) from exc


def _run(db: Session, document: SyllabusDocument) -> dict:
    from services.api.storage import fetch_document

    if document.section_id is None:
        raise ParseFailure(
            f"Document {document.id} has no section; confirm the course match first (US-1)."
        )
    section = db.scalar(select(Section).where(Section.id == document.section_id))
    if section is None:
        raise ParseFailure(f"Section {document.section_id} no longer exists")

    data = fetch_document(document.storage_key)
    ingested = ingest(document.storage_key.rsplit("/", 1)[-1], data)

    document.page_count = ingested.page_count
    document.is_scanned = ingested.is_scanned

    result = extract_document(ingested)

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
    db.commit()

    hard_failure = any(f.severity == "hard_failure" for f in flags)
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
