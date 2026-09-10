"""Validation rules and confidence scoring — PRD sections 9.4 and 10.1.

The single most important function here is `source_span_is_findable`. Section
16 gives hallucination rate a hard ceiling of 0.5% while every other metric is
allowed to improve over time, and a verbatim-findability check is a cheap,
exact test for it: if the model cannot quote the document, the model made it up.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from schemas.extraction import AssessmentExtraction, ExtractionOutput
from schemas.resolution import ResolvedDate
from schemas.validation import ValidationFlag

#: 9.4: weights outside this band are the strongest single quality signal available.
WEIGHT_SUM_MIN = 98.0
WEIGHT_SUM_MAX = 102.0

#: 9.4: two exams in one course inside this many days is a likely misparse.
EXAM_PROXIMITY_DAYS = 5

#: 9.4: a non-final item carrying more than this share of the grade is suspicious.
MAX_NON_FINAL_WEIGHT = 60.0

_EXAM_TYPES = frozenset({"midterm", "final_exam"})
_FINAL_TYPES = frozenset({"final_exam", "final_project"})


def _normalize(text: str) -> str:
    """Fold whitespace, quotes, and dashes so formatting noise is not a mismatch.

    PDF extraction routinely turns a straight quote into a curly one and a
    hyphen into an en dash. Those differences are not hallucination, and
    counting them as such would make the 0.5% ceiling meaningless.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def source_span_is_findable(span: str, source_text: str) -> bool:
    """Is this `source_span` a verbatim quote from the document? (10.1)

    Exact-after-normalization only. A fuzzy match here would let a
    plausible-sounding invention pass, which is the failure this check exists
    to catch.
    """
    if not span or not span.strip():
        return False
    return _normalize(span) in _normalize(source_text)


def hallucination_rate(assessments: list[AssessmentExtraction], source_text: str) -> float:
    """Share of items whose `source_span` is not findable (section 16's hard gate)."""
    if not assessments:
        return 0.0
    unfindable = sum(
        1 for a in assessments if not source_span_is_findable(a.source_span, source_text)
    )
    return unfindable / len(assessments)


def validate_extraction(
    output: ExtractionOutput,
    *,
    page_count: int,
    source_text: str,
    resolved: dict[str, ResolvedDate] | None = None,
    term_start: date | None = None,
    term_end: date | None = None,
) -> list[ValidationFlag]:
    """Every rule in 9.4's table. Runs after resolution, before review.

    Nothing here drops data. A failing item is flagged and shown, because a
    silently dropped deadline is indistinguishable to the student from a
    deadline that never existed.
    """
    flags: list[ValidationFlag] = []

    # Zero assessments from a multi-page document is a hard failure (9.4).
    if page_count > 1 and not output.assessments:
        flags.append(
            ValidationFlag(
                check="zero_assessments_extracted",
                severity="hard_failure",
                message=(
                    "We couldn't find any graded items in this syllabus. "
                    "You can add them by hand, and we'll keep the rest of the course details."
                ),
            )
        )

    # Grade weights must sum to ~100 (9.4) — the strongest single quality signal.
    if output.grade_breakdown:
        total = sum(item.weight_pct for item in output.grade_breakdown)
        if not (WEIGHT_SUM_MIN <= total <= WEIGHT_SUM_MAX):
            flags.append(
                ValidationFlag(
                    check="weights_out_of_range",
                    severity="warning",
                    message=(
                        f"The grade categories add up to {total:g}%, not 100%. Worth a quick check."
                    ),
                )
            )

    # A category's stated item_count should match how many items were enumerated.
    for category in output.grade_breakdown:
        if category.item_count is None:
            continue
        enumerated = sum(1 for a in output.assessments if a.category_ref == category.category)
        if enumerated and enumerated != category.item_count:
            flags.append(
                ValidationFlag(
                    check="item_count_mismatch",
                    severity="warning",
                    message=(
                        f"{category.category} says {category.item_count} items, but "
                        f"{enumerated} were found in the schedule."
                    ),
                    category=category.category,
                )
            )

    # A non-final item worth more than 60% is almost always a misparse (9.4).
    for assessment in output.assessments:
        if (
            assessment.weight_pct is not None
            and assessment.weight_pct > MAX_NON_FINAL_WEIGHT
            and assessment.type not in _FINAL_TYPES
        ):
            flags.append(
                ValidationFlag(
                    check="weight_over_threshold",
                    severity="warning",
                    message=(
                        f"{assessment.title} is listed at {assessment.weight_pct:g}% "
                        "of the grade, which is unusually high for this kind of item."
                    ),
                    assessment_ids=[assessment.title],
                )
            )

    flags.extend(_date_flags(output, resolved, term_start, term_end))
    return flags


def _date_flags(
    output: ExtractionOutput,
    resolved: dict[str, ResolvedDate] | None,
    term_start: date | None,
    term_end: date | None,
) -> list[ValidationFlag]:
    """Rules that need resolved dates: term range, weekday mismatch, exam proximity."""
    if not resolved:
        return []

    flags: list[ValidationFlag] = []

    for title, resolution in resolved.items():
        if resolution.weekday_mismatch:
            flags.append(
                ValidationFlag(
                    check="weekday_mismatch",
                    severity="warning",
                    message=resolution.notes
                    or f"{title}: the weekday and the date in the syllabus disagree.",
                    assessment_ids=[title],
                )
            )

        if resolution.due_at and term_start and term_end:
            due = resolution.due_at.date()
            if due < term_start or due > term_end:
                flags.append(
                    ValidationFlag(
                        check="date_outside_term",
                        severity="warning",
                        message=(
                            f"{title} resolves to {due.isoformat()}, which is outside the term."
                        ),
                        assessment_ids=[title],
                    )
                )

    # Two exams in the same course within 5 days (9.4). Note this is a *misparse*
    # signal, distinct from 11.4's `major_collision`, which is a real scheduling
    # warning across courses. Same shape, opposite meaning.
    exam_dates = []
    for a in output.assessments:
        if a.type not in _EXAM_TYPES or a.title not in resolved:
            continue
        due_at = resolved[a.title].due_at
        if due_at is None:
            continue
        exam_dates.append((a.title, due_at.date()))
    for i, (title_a, date_a) in enumerate(exam_dates):
        for title_b, date_b in exam_dates[i + 1 :]:
            if abs((date_b - date_a).days) <= EXAM_PROXIMITY_DAYS:
                flags.append(
                    ValidationFlag(
                        check="exams_too_close",
                        severity="warning",
                        message=(
                            f"{title_a} and {title_b} are both within "
                            f"{EXAM_PROXIMITY_DAYS} days. One of these dates may be misread."
                        ),
                        assessment_ids=[title_a, title_b],
                    )
                )
    return flags


def composite_confidence(
    assessment: AssessmentExtraction,
    *,
    source_text: str,
    resolution: ResolvedDate | None = None,
    survived_validation: bool = True,
    peer_agreement: float | None = None,
) -> float:
    """Section 10.1's composite score.

    Components, in the PRD's order: the model's own confidence, verbatim
    findability of the source span, survival of the 9.4 rules, whether date
    resolution required inference, and agreement with other students' verified
    extraction of the same section.
    """
    score = assessment.confidence

    # Findability is a hard gate, not a weighted term. An unquotable item is a
    # suspected hallucination and must land in the `low` bucket regardless of
    # how confident the model claimed to be.
    if not source_span_is_findable(assessment.source_span, source_text):
        return min(score, 0.25)

    if not survived_validation:
        score *= 0.7

    if resolution is not None:
        precision_factor = {
            "exact_datetime": 1.0,
            "date_only": 0.95,
            "week_only": 0.75,
            "tbd": 0.5,
        }[resolution.due_precision]
        score *= precision_factor
        if resolution.weekday_mismatch:
            score *= 0.8
        if resolution.needs_review:
            score *= 0.85

    if peer_agreement is not None:
        # Agreement with verified peer extractions pulls toward certainty in
        # either direction; it is the only signal here grounded outside this document.
        score = score * 0.7 + peer_agreement * 0.3

    return max(0.0, min(1.0, score))


def confidence_bucket(score: float) -> str:
    """10.1's buckets: high auto-accepts and collapses, low requires explicit action."""
    if score >= 0.85:
        return "high"
    if score >= 0.60:
        return "medium"
    return "low"
