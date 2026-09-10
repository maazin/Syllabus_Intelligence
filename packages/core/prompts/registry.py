"""MLflow prompt registry and tracing — PRD sections 16 and 18.1.

Section 18.1 picks MLflow because it replaces three separate tools: tracing
(2.14, OpenTelemetry-compatible), the prompt registry (2.21, immutable versions
and aliases), and `mlflow.genai.evaluate()` with built-in scorers (3.0).

The registry is what makes section 16's central claim operational:

    "Version prompts explicitly and record `prompt_version` on every run so
     that production corrections can be attributed to a specific prompt."

The files in `prompts/v1/` remain the source of truth, because a prompt that
only exists in a tracking server cannot be code-reviewed or rolled back with
the code that depends on it. This module registers those files so that a run,
a trace, and a correction can all be joined on one version, and so a regression
can be pinned to the exact text that caused it.

Every function here degrades to a no-op when no tracking server is configured.
Extraction must not fail because an observability sidecar is unreachable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from prompts import CURRENT_PROMPT_VERSION, available_versions, load_prompt

logger = logging.getLogger(__name__)

#: Prompts registered under these names. The registry name is stable across
#: versions; the version is what moves.
PASS_A_PROMPT = "syllabus_pass_a_structure"
PASS_B_PROMPT = "syllabus_pass_b_schedule"

_PROMPT_FILES = {
    PASS_A_PROMPT: "pass_a_structure",
    PASS_B_PROMPT: "pass_b_schedule",
}

#: The alias production reads. Moving this is how a prompt is promoted, and it
#: means a rollback is an alias change rather than a redeploy.
PRODUCTION_ALIAS = "production"


def tracking_uri() -> str | None:
    """The MLflow server from `MLFLOW_TRACKING_URI` (section 24)."""
    return os.environ.get("MLFLOW_TRACKING_URI") or None


def is_enabled() -> bool:
    return tracking_uri() is not None


def _mlflow() -> Any:
    """Import lazily so the worker starts without MLflow installed.

    Typed as Any because mlflow ships no stubs, and pinning a partial protocol
    here would be a fiction that has to be maintained. Callers are guarded by
    `is_enabled()`.
    """
    import mlflow

    uri = tracking_uri()
    if uri is None:
        raise RuntimeError("MLFLOW_TRACKING_URI is unset; call is_enabled() first")

    mlflow.set_tracking_uri(uri)
    return mlflow


def register_prompts(version: str = CURRENT_PROMPT_VERSION) -> dict[str, str]:
    """Push the prompt files for one version into the registry.

    Idempotent by nature: MLflow versions are immutable, so registering
    unchanged text returns the existing version rather than creating a
    duplicate. Safe to run on every deploy.
    """
    if not is_enabled():
        logger.info("MLFLOW_TRACKING_URI is unset; skipping prompt registration")
        return {}

    mlflow = _mlflow()
    registered: dict[str, str] = {}

    for registry_name, filename in _PROMPT_FILES.items():
        template = load_prompt(filename, version=version)
        prompt = mlflow.genai.register_prompt(
            name=registry_name,
            template=template,
            commit_message=f"Prompt file version {version}",
            tags={
                # The folder name is the `prompt_version` on every
                # `extraction_runs` row, so this tag is the join key between
                # the registry and production data.
                "prompt_version": version,
                "source_file": f"packages/core/prompts/{version}/{filename}.txt",
            },
        )
        registered[registry_name] = str(prompt.version)
        logger.info("Registered %s as version %s", registry_name, prompt.version)

    return registered


def promote_to_production(registry_name: str, version: str) -> None:
    """Point the production alias at a specific registry version.

    Section 27.2 makes a worse prompt exactly as blocking as a failing test.
    Promotion is therefore a deliberate step after the eval gate passes, never
    a side effect of registering.
    """
    if not is_enabled():
        return
    mlflow = _mlflow()
    mlflow.genai.set_prompt_alias(name=registry_name, alias=PRODUCTION_ALIAS, version=version)
    logger.info("Promoted %s version %s to %s", registry_name, version, PRODUCTION_ALIAS)


@contextmanager
def extraction_run(
    document_id: str, prompt_version: str, model: str
) -> Iterator[Callable[..., None]]:
    """Wrap one document's extraction in a tracked run.

    Records the inputs that make a later correction interpretable: which
    prompt version, which model tier, which document. Yields a logger callable
    so the caller records outcomes without importing MLflow itself.

    A tracking failure is logged and swallowed. Losing a trace is a nuisance;
    losing a student's parse because the tracking server is down is not.
    """
    if not is_enabled():
        yield lambda **_: None
        return

    try:
        mlflow = _mlflow()
        mlflow.set_experiment("syllabus-extraction")
    except Exception:
        logger.exception("Could not reach MLflow; continuing without tracing")
        yield lambda **_: None
        return

    try:
        with mlflow.start_run(run_name=f"extract-{document_id[:8]}"):
            mlflow.set_tags(
                {
                    "document_id": document_id,
                    "prompt_version": prompt_version,
                    "model": model,
                }
            )

            def record(**metrics: Any) -> None:
                numeric = {k: v for k, v in metrics.items() if isinstance(v, int | float)}
                other = {k: str(v) for k, v in metrics.items() if k not in numeric}
                if numeric:
                    mlflow.log_metrics(numeric)
                if other:
                    mlflow.set_tags(other)

            yield record
    except Exception:
        logger.exception("MLflow run failed; extraction result is unaffected")
        yield lambda **_: None


def log_eval_report(report: Any, version: str = CURRENT_PROMPT_VERSION) -> None:
    """Record a golden-set run so ship gates are tracked over time (section 16).

    A single pass or fail says nothing about direction. Logging every run
    against its prompt version is what turns the gate into a trend, and what
    lets "the correction rate on this field is spiking" be traced back to the
    prompt change that caused it.
    """
    if not is_enabled():
        return

    try:
        mlflow = _mlflow()
        mlflow.set_experiment("syllabus-eval")
        with mlflow.start_run(run_name=f"golden-set-{version}"):
            mlflow.set_tags({"prompt_version": version, "documents": report.document_count})
            mlflow.log_metrics({score.name: score.value for score in report.scores})
            mlflow.log_metrics({f"{score.name}__gate": score.gate for score in report.scores})
            mlflow.log_metric("passed", 1 if report.passed else 0)
    except Exception:
        logger.exception("Could not log the eval report to MLflow")


def registered_versions() -> list[str]:
    """Prompt versions present on disk, which the registry mirrors."""
    return available_versions()
