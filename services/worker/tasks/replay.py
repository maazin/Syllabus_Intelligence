"""Replay recorded extractions instead of calling the model.

For development and the end-to-end smoke test. The model call is the one part
of the pipeline that costs money and needs a credential, and everything
downstream of it (resolution, validation, persistence, review, the timeline,
the heatmap, calendar sync) is exactly what an end-to-end test needs to
exercise against a real API, a real worker and a real database.

Matching is by content, not by filename. Every recorded extraction carries
source spans that are verbatim-findable in the document it was recorded from,
and `validate.py` already enforces that property. A document "matches" a
recording when every span in the recording is findable in the document's
text, which means the recording is a faithful extraction of it. A document
that matches nothing fails with a clear message rather than being handed a
plausible-looking extraction of some other syllabus.

Hard rule: this never runs in production. `ENVIRONMENT=production` with
`LLM_MODE=replay` is refused at import of the first document, not logged and
carried on with.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from schemas.extraction import ExtractionOutput

try:
    from services.worker.tasks.validate import source_span_is_findable
except ImportError:  # running from inside services/worker/tasks
    from validate import source_span_is_findable  # type: ignore[no-redef,import-not-found]

logger = logging.getLogger(__name__)

#: `tests/fixtures/recorded_extractions`, resolved from this file so it works
#: from a checkout and from inside the worker image, where the tests tree is
#: copied to the same relative place.
RECORDED_DIR = Path(
    os.environ.get(
        "LLM_REPLAY_DIR",
        str(Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "recorded_extractions"),
    )
)


class ReplayError(RuntimeError):
    """No recording matches, or replay is not allowed here."""


def is_enabled() -> bool:
    return os.environ.get("LLM_MODE", "live").lower() == "replay"


def guard() -> None:
    if os.environ.get("ENVIRONMENT", "local") == "production":
        raise ReplayError(
            "LLM_MODE=replay is set in production. Refusing: replayed extractions "
            "are test fixtures, and a student would be shown another course's deadlines."
        )


def _recordings() -> list[tuple[str, ExtractionOutput]]:
    if not RECORDED_DIR.is_dir():
        raise ReplayError(f"No recorded extractions directory at {RECORDED_DIR}")
    found: list[tuple[str, ExtractionOutput]] = []
    for path in sorted(RECORDED_DIR.glob("*.json")):
        payload = json.loads(path.read_text())
        payload.pop("_comment", None)
        found.append((path.stem, ExtractionOutput.model_validate(payload)))
    return found


def _matches(recording: ExtractionOutput, text: str) -> bool:
    spans = [a.source_span for a in recording.assessments if a.source_span]
    return bool(spans) and all(source_span_is_findable(span, text) for span in spans)


def replay(text: str) -> tuple[str, ExtractionOutput]:
    """The recording whose every source span is findable in `text`."""
    guard()
    recordings = _recordings()
    for name, recording in recordings:
        if _matches(recording, text):
            logger.info("Replaying recorded extraction %r", name)
            return name, recording
    names = ", ".join(name for name, _ in recordings) or "(none)"
    raise ReplayError(
        "LLM_MODE=replay, and no recorded extraction matches this document. "
        f"Recordings available: {names}. Upload one of the fixtures in "
        "tests/fixtures/syllabi, or unset LLM_MODE to call the model."
    )
