"""Golden-set eval harness — PRD section 16.

The PRD is emphatic about the ordering: build this in week 1, *before* the
extraction prompt is any good, not after. So the harness exists and runs now,
against whatever labeled documents are present. With an empty golden set it
reports that and skips rather than passing vacuously — a green check on zero
documents would be worse than a red one.

Metrics and ship gates come straight from section 16's table. Hallucination
rate is the one with a hard ceiling: everything else may improve over time, a
fabricated deadline is a product-killing bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from schemas.extraction import AssessmentExtraction, ExtractionOutput
from validate import source_span_is_findable

GOLDEN_DIR = Path(__file__).parent / "labels"

#: Section 16's ship gates, verbatim.
SHIP_GATES: dict[str, float] = {
    "assessment_detection_f1": 0.90,
    "assessment_type_accuracy": 0.85,
    "weights_exact_match": 0.92,
    "due_date_match": 0.88,
    "policy_boolean_accuracy": 0.90,
}

#: The one hard ceiling (a maximum, not a minimum).
MAX_HALLUCINATION_RATE = 0.005


@dataclass
class FieldGroupScore:
    name: str
    value: float
    gate: float
    higher_is_better: bool = True

    @property
    def passed(self) -> bool:
        return self.value >= self.gate if self.higher_is_better else self.value <= self.gate


@dataclass
class EvalReport:
    document_count: int
    scores: list[FieldGroupScore] = field(default_factory=list)
    per_document: dict[str, dict] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.scores)

    def render(self) -> str:
        lines = [
            f"Golden set: {self.document_count} document(s)",
            "",
            f"  {'field group':<28} {'score':>8} {'gate':>8}  result",
            "  " + "-" * 60,
        ]
        for score in self.scores:
            comparator = ">=" if score.higher_is_better else "<="
            lines.append(
                f"  {score.name:<28} {score.value:>8.3f} {comparator}{score.gate:>7.3f}  "
                f"{'PASS' if score.passed else 'FAIL'}"
            )
        lines.append("")
        lines.append(f"  overall: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def _normalize_title(title: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _match_items(
    predicted: list[AssessmentExtraction], expected: list[dict]
) -> list[tuple[AssessmentExtraction | None, dict | None]]:
    """Pair predicted items to labeled ones by normalized title.

    Title matching rather than index matching, because a model that emits the
    right items in a different order is correct, and an eval that punishes it
    for ordering would send the prompt in the wrong direction.
    """
    remaining = {_normalize_title(e["title"]): e for e in expected}
    pairs: list[tuple[AssessmentExtraction | None, dict | None]] = []

    for item in predicted:
        key = _normalize_title(item.title)
        match = remaining.pop(key, None)
        if match is None:
            # Fall back to containment so "Midterm 1" matches "Midterm Exam 1".
            for candidate_key in list(remaining):
                if key in candidate_key or candidate_key in key:
                    match = remaining.pop(candidate_key)
                    break
        pairs.append((item, match))

    pairs.extend((None, leftover) for leftover in remaining.values())
    return pairs


def score_document(predicted: ExtractionOutput, label: dict, source_text: str) -> dict:
    """Per-document metrics, later aggregated across the set."""
    expected_items = label.get("assessments", [])
    pairs = _match_items(predicted.assessments, expected_items)

    true_positives = sum(1 for p, e in pairs if p is not None and e is not None)
    false_positives = sum(1 for p, e in pairs if p is not None and e is None)
    false_negatives = sum(1 for p, e in pairs if p is None and e is not None)

    precision = (
        true_positives / (true_positives + false_positives) if predicted.assessments else 0.0
    )
    recall = true_positives / (true_positives + false_negatives) if expected_items else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    matched = [(p, e) for p, e in pairs if p is not None and e is not None]
    type_correct = sum(1 for p, e in matched if p.type == e.get("type"))
    weight_correct = sum(
        1
        for p, e in matched
        if e.get("weight_pct") is None
        or (p.weight_pct is not None and abs(p.weight_pct - e["weight_pct"]) < 0.01)
    )
    date_correct = sum(
        1
        for p, e in matched
        if e.get("date_expression_raw") is None
        or (p.date_expression_raw or "").strip().lower()
        == str(e["date_expression_raw"]).strip().lower()
    )

    expected_policies = label.get("policies", {})
    policy_fields = [
        f
        for f in ("attendance_graded", "group_work_present", "curve_mentioned")
        if f in expected_policies
    ]
    policy_correct = sum(
        1 for f in policy_fields if getattr(predicted.policies, f) == expected_policies[f]
    )

    unfindable = sum(
        1
        for item in predicted.assessments
        if not source_span_is_findable(item.source_span, source_text)
    )

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "matched": len(matched),
        "type_correct": type_correct,
        "weight_correct": weight_correct,
        "date_correct": date_correct,
        "policy_fields": len(policy_fields),
        "policy_correct": policy_correct,
        "items": len(predicted.assessments),
        "unfindable": unfindable,
    }


def aggregate(per_document: dict[str, dict]) -> EvalReport:
    if not per_document:
        return EvalReport(document_count=0)

    n = len(per_document)
    total_matched = sum(d["matched"] for d in per_document.values()) or 1
    total_items = sum(d["items"] for d in per_document.values()) or 1
    total_policy = sum(d["policy_fields"] for d in per_document.values()) or 1

    scores = [
        FieldGroupScore(
            "assessment_detection_f1",
            sum(d["f1"] for d in per_document.values()) / n,
            SHIP_GATES["assessment_detection_f1"],
        ),
        FieldGroupScore(
            "assessment_type_accuracy",
            sum(d["type_correct"] for d in per_document.values()) / total_matched,
            SHIP_GATES["assessment_type_accuracy"],
        ),
        FieldGroupScore(
            "weights_exact_match",
            sum(d["weight_correct"] for d in per_document.values()) / total_matched,
            SHIP_GATES["weights_exact_match"],
        ),
        FieldGroupScore(
            "due_date_match",
            sum(d["date_correct"] for d in per_document.values()) / total_matched,
            SHIP_GATES["due_date_match"],
        ),
        FieldGroupScore(
            "policy_boolean_accuracy",
            sum(d["policy_correct"] for d in per_document.values()) / total_policy,
            SHIP_GATES["policy_boolean_accuracy"],
        ),
        FieldGroupScore(
            "hallucination_rate",
            sum(d["unfindable"] for d in per_document.values()) / total_items,
            MAX_HALLUCINATION_RATE,
            higher_is_better=False,
        ),
    ]
    return EvalReport(document_count=n, scores=scores, per_document=per_document)


def load_labels() -> list[tuple[str, dict]]:
    """Load hand-labeled documents from `tests/golden_set/labels/`.

    Section 16 wants 100 real syllabi stratified across STEM/humanities/lab/
    studio/online, modern PDF/LMS export/scan/DOCX, and short/long. The fixtures
    in section 26 are a starting seed, not the golden set.
    """
    if not GOLDEN_DIR.is_dir():
        return []
    return [(path.stem, json.loads(path.read_text())) for path in sorted(GOLDEN_DIR.glob("*.json"))]
