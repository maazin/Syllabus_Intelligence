"""Enqueueing work from the API — PRD section 21.

The API must not import the worker. `services/worker` pulls in pdfplumber,
Docling, and torch; dragging that into the API image would wreck the cold start
that makes Cloud Run's scale-to-zero worthwhile (18.1), and it would blur the
service boundary section 21 draws.

So this sends a task by *name* over the broker. The API knows the task's name
and its argument, nothing else.
"""

from __future__ import annotations

import logging
import os
import uuid

from celery import Celery

logger = logging.getLogger(__name__)

PARSE_TASK = "documents.parse"

_app: Celery | None = None


def _broker() -> Celery:
    global _app
    if _app is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6479/0")
        _app = Celery("syllabus_intelligence_api", broker=url)
    return _app


def enqueue_parse(document_id: uuid.UUID) -> str | None:
    """Queue a document for parsing. Returns the task id, or None if queueing failed.

    A broker that is down must not fail the upload: the file is already stored
    and the row already exists, so the parse can be retried. Losing the student's
    upload because Redis blinked would be a much worse outcome than a delayed
    parse, and US-1 wants the UI usable regardless.
    """
    try:
        result = _broker().send_task(PARSE_TASK, args=[str(document_id)])
    except Exception:
        logger.exception("Could not enqueue parse for %s", document_id)
        return None
    return result.id
