"""Replay mode: recorded extractions in place of the model call."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.worker.tasks import replay
from services.worker.tasks.ingest import ingest

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "syllabi"


@pytest.fixture(autouse=True)
def _replay_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.setenv("ENVIRONMENT", "local")


def test_the_recorded_fixture_matches_its_own_document() -> None:
    path = FIXTURES / "cs_lecture_native_pdf.pdf"
    if not path.is_file():
        pytest.skip("run scripts/generate_fixture_syllabi.py first")
    doc = ingest(path.name, path.read_bytes())
    name, output = replay.replay(doc.full_text)
    assert name == "cs_lecture_native_pdf"
    assert len(output.assessments) == 9


def test_a_document_with_no_recording_is_refused_with_the_list() -> None:
    """Never hand a student another course's deadlines."""
    with pytest.raises(replay.ReplayError, match="cs_lecture_native_pdf"):
        replay.replay("Syllabus for a course nobody recorded. Midterm on the 14th.")


def test_production_refuses_replay_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(replay.ReplayError, match="production"):
        replay.replay("anything")


def test_extract_document_routes_through_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cascade never touches the model when replay is on."""
    from services.worker.tasks import extract as extract_module

    def explode(**_: object) -> None:
        raise AssertionError("model was called in replay mode")

    monkeypatch.setattr(extract_module, "call_structured", explode)
    path = FIXTURES / "cs_lecture_native_pdf.pdf"
    if not path.is_file():
        pytest.skip("run scripts/generate_fixture_syllabi.py first")
    result = extract_module.extract_document(ingest(path.name, path.read_bytes()))
    assert result.model == "replay:cs_lecture_native_pdf"
    assert result.cost_cents == 0
    assert result.runs == []
    # Validation still ran on the replayed output.
    assert isinstance(result.flags, list)
