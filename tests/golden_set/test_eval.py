"""The eval gate as a test — PRD sections 16 and 27.2.

    pytest tests/golden_set -m eval

Section 27.2 makes this the one CI gate allowed to fail a PR for a reason other
than a bug: a worse prompt is exactly as blocking as a failing test. It runs on
any PR touching `packages/core/prompts/`, `extract.py`, or `resolve_dates.py`.

The eval marker keeps it out of the default run, because it costs money — the
full 100 documents are two model calls each.
"""

from __future__ import annotations

import pytest

from tests.golden_set.harness import (
    MAX_HALLUCINATION_RATE,
    aggregate,
    load_labels,
    score_document,
)


def test_harness_scores_a_perfect_extraction() -> None:
    """The harness itself must be correct before its verdicts mean anything."""
    from schemas.extraction import ExtractionOutput

    source = "Midterm 1 is on October 14. Final paper due December 1."
    predicted = ExtractionOutput.model_validate(
        {
            "course": {},
            "assessments": [
                {
                    "title": "Midterm 1",
                    "type": "midterm",
                    "weight_pct": 20,
                    "date_expression_raw": "October 14",
                    "source_span": "Midterm 1 is on October 14.",
                    "confidence": 0.9,
                },
                {
                    "title": "Final paper",
                    "type": "paper_long",
                    "weight_pct": 30,
                    "date_expression_raw": "December 1",
                    "source_span": "Final paper due December 1.",
                    "confidence": 0.9,
                },
            ],
        }
    )
    label = {
        "assessments": [
            {
                "title": "Midterm 1",
                "type": "midterm",
                "weight_pct": 20,
                "date_expression_raw": "October 14",
            },
            {
                "title": "Final paper",
                "type": "paper_long",
                "weight_pct": 30,
                "date_expression_raw": "December 1",
            },
        ]
    }

    score = score_document(predicted, label, source)
    assert score["f1"] == pytest.approx(1.0)
    assert score["type_correct"] == 2
    assert score["weight_correct"] == 2
    assert score["date_correct"] == 2
    assert score["unfindable"] == 0


def test_harness_catches_a_missed_item_and_an_invented_one() -> None:
    from schemas.extraction import ExtractionOutput

    source = "Midterm 1 is on October 14. Final paper due December 1."
    predicted = ExtractionOutput.model_validate(
        {
            "course": {},
            "assessments": [
                {
                    "title": "Midterm 1",
                    "type": "midterm",
                    "source_span": "Midterm 1 is on October 14.",
                    "confidence": 0.9,
                },
                {
                    "title": "Pop Quiz",
                    "type": "quiz",
                    "source_span": "There will be a pop quiz on November 3.",
                    "confidence": 0.9,
                },
            ],
        }
    )
    label = {
        "assessments": [
            {"title": "Midterm 1", "type": "midterm"},
            {"title": "Final paper", "type": "paper_long"},
        ]
    }

    score = score_document(predicted, label, source)
    assert score["precision"] == pytest.approx(0.5)  # Pop Quiz is a false positive
    assert score["recall"] == pytest.approx(0.5)  # Final paper was missed
    assert score["unfindable"] == 1  # and Pop Quiz is unquotable — a hallucination


def test_harness_matches_items_regardless_of_order() -> None:
    """A correct extraction in a different order is still correct."""
    from schemas.extraction import ExtractionOutput

    source = "A is due Monday. B is due Friday."
    predicted = ExtractionOutput.model_validate(
        {
            "course": {},
            "assessments": [
                {
                    "title": "B",
                    "type": "quiz",
                    "source_span": "B is due Friday.",
                    "confidence": 0.9,
                },
                {
                    "title": "A",
                    "type": "quiz",
                    "source_span": "A is due Monday.",
                    "confidence": 0.9,
                },
            ],
        }
    )
    label = {"assessments": [{"title": "A", "type": "quiz"}, {"title": "B", "type": "quiz"}]}
    assert score_document(predicted, label, source)["f1"] == pytest.approx(1.0)


def test_empty_golden_set_reports_zero_rather_than_passing_vacuously() -> None:
    """A green check on zero documents would be worse than a red one."""
    report = aggregate({})
    assert report.document_count == 0
    assert report.scores == []


def test_hallucination_ceiling_is_the_documented_value() -> None:
    assert MAX_HALLUCINATION_RATE == 0.005


@pytest.mark.eval
def test_golden_set_meets_every_ship_gate() -> None:
    """The real gate. Requires labeled documents and a live model.

    Skips — loudly — until the golden set exists. Section 16 requires 100
    hand-labeled real syllabi before this number means anything, and the
    section 26 fixtures explicitly are not that.
    """
    labels = load_labels()
    if not labels:
        pytest.skip(
            "Golden set is empty. Add hand-labeled syllabi to tests/golden_set/labels/ "
            "(section 16 requires 100, stratified). Until then this gate cannot run."
        )

    from pathlib import Path

    from extract import extract_document
    from ingest import ingest

    per_document = {}
    for name, label in labels:
        source_paths = [p for p in label.get("source_paths", []) if p]
        if not source_paths:
            pytest.fail(f"{name}: label has no source_paths")

        source = Path(source_paths[0])
        document = ingest(source.name, source.read_bytes())
        result = extract_document(document)
        per_document[name] = score_document(result.output, label, document.full_text)

    report = aggregate(per_document)
    print("\n" + report.render())

    # Section 16: record the run against its prompt version so a regression can
    # be traced to the change that caused it rather than merely noticed.
    from prompts.registry import log_eval_report

    log_eval_report(report)

    assert report.passed, "one or more ship gates regressed:\n" + report.render()
