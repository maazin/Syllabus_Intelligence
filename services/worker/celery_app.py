"""Celery application — PRD section 18.2.

The PRD is explicit about why this runs on a small always-on VM rather than a
serverless container platform: a Celery worker long-polls its broker, so it is
doing work between requests, and Cloud Run throttles CPU outside of requests.
Treat platform-level scale-to-zero for Celery as unavailable. The ~$6/mo VM
also hosts Redis and the MLflow tracking server; the API still scales to zero.
"""

from __future__ import annotations

import os

from celery import Celery

broker_url = os.environ.get("REDIS_URL", "redis://localhost:6479/0")

app = Celery("syllabus_intelligence", broker=broker_url, backend=broker_url)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # US-1 targets p95 under 3 minutes end to end; a task still running at 10
    # minutes is stuck, not slow.
    task_time_limit=600,
    task_soft_time_limit=540,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
)

# Named explicitly rather than autodiscovered. `autodiscover_tasks(["pkg"])`
# looks for a module called `pkg.tasks`, so pointed at `services.worker.tasks`
# it searched for `services.worker.tasks.tasks`, found nothing, and the worker
# started cleanly with only `health.ping` registered. Every upload was then
# received and dropped with a KeyError, which the API never sees.
app.conf.imports = ("services.worker.tasks.parse_document",)


@app.task(name="health.ping")
def ping() -> str:
    return "pong"
