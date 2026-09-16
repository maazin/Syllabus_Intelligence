"""The worker registers the task the API sends.

The API enqueues by name (section 21) and never imports the worker, so a
mismatch between the two is invisible to every unit test on either side: the
upload returns 202, the worker logs a KeyError, and the student's review
screen waits forever. This is the one place the two names are compared.
"""

from __future__ import annotations

from services.api.queue import PARSE_TASK
from services.worker.celery_app import app


def test_the_parse_task_the_api_enqueues_is_registered() -> None:
    app.loader.import_default_modules()
    assert PARSE_TASK in app.tasks, sorted(app.tasks)
