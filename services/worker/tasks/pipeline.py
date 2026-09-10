"""End-to-end orchestration: one uploaded file to a reviewable, schedulable result.

This is section 7's pipeline with sections 8-11 chained onto it:

    ingest -> extract (A + B) -> resolve dates -> validate -> score confidence
           -> schedule items -> heatmap + flags

Kept separate from the individual task modules so the ordering is stated in one
place. The ordering matters: validation runs *after* resolution (9.4 says so),
and confidence depends on both, so nothing here can be reordered casually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from date_resolver.calendar_context import AcademicCalendar
from date_resolver.resolver import (
    ResolutionContext,
    resolve_date_expression,
    resolve_final_exam,
)
from schemas.extraction import AssessmentExtraction, ExtractionOutput
from schemas.resolution import ResolvedDate
from schemas.validation import ValidationFlag
from workload_model.flags import Flag, compute_flags
from workload_model.heatmap import ScheduledItem, WeeklyCell, build_heatmap

try:
    from services.worker.tasks.ingest import IngestedDocument, ingest
    from services.worker.tasks.validate import (
        composite_confidence,
        confidence_bucket,
        hallucination_rate,
        validate_extraction,
    )
except ImportError:  # running from inside services/worker/tasks
    # Unresolvable to a type checker on purpose: these names only exist on
    # sys.path when a script is run from this directory, which is how the
    # pipeline script invokes them. The package-qualified imports above are
    # the ones mypy checks.
    from ingest import IngestedDocument, ingest  # type: ignore[no-redef,import-not-found]
    from validate import (  # type: ignore[no-redef,import-not-found]
        composite_confidence,
        confidence_bucket,
        hallucination_rate,
        validate_extraction,
    )

logger = logging.getLogger(__name__)


@dataclass
class ReviewItem:
    """One assessment as the review screen (US-3) needs it.

    Carries the source span and page ref alongside the resolved date so the
    student verifies by glancing rather than re-reading the syllabus.
    """

    assessment: AssessmentExtraction
    resolution: ResolvedDate
    confidence: float
    bucket: str

    @property
    def title(self) -> str:
        return self.assessment.title

    @property
    def needs_explicit_action(self) -> bool:
        """10.1: `low` items are expanded and require an explicit action."""
        return self.bucket == "low"


@dataclass
class PipelineResult:
    document: IngestedDocument
    extraction: ExtractionOutput
    review_items: list[ReviewItem] = field(default_factory=list)
    flags: list[ValidationFlag] = field(default_factory=list)
    heatmap: list[WeeklyCell] = field(default_factory=list)
    collision_flags: dict[date, list[Flag]] = field(default_factory=dict)
    hallucination_rate: float = 0.0

    @property
    def review_order(self) -> list[ReviewItem]:
        """10.2: order by confidence ascending so the riskiest items get attention first."""
        return sorted(self.review_items, key=lambda i: i.confidence)

    @property
    def auto_accepted(self) -> list[ReviewItem]:
        return [i for i in self.review_items if i.bucket == "high"]

    @property
    def unscheduled(self) -> list[ReviewItem]:
        """9.3: `tbd` items live in a term-level tray, never on the calendar."""
        return [i for i in self.review_items if i.resolution.due_precision == "tbd"]


def resolve_assessments(
    extraction: ExtractionOutput,
    ctx: ResolutionContext,
) -> dict[str, ResolvedDate]:
    """Resolve every assessment's date expression, in the two rounds 9.2 requires."""
    results: dict[str, ResolvedDate] = {}

    for assessment in extraction.assessments:
        # A final exam deferred to the registrar resolves from the matrix, not the
        # syllabus text (9.5) — roughly half of syllabi defer it this way, and
        # finals week is the densest collision period of the term.
        if assessment.type == "final_exam" and _defers_to_registrar(assessment.date_expression_raw):
            results[assessment.title] = resolve_final_exam(ctx.meeting_pattern, ctx)
            continue
        results[assessment.title] = resolve_date_expression(assessment.date_expression_raw, ctx)

    # Round two: anchors that other items provided are now available.
    anchors = {title: r.due_at.date() for title, r in results.items() if r.due_at is not None}
    if anchors:
        round_two = ResolutionContext(
            calendar=ctx.calendar,
            meeting_pattern=ctx.meeting_pattern,
            anchors={**(ctx.anchors or {}), **anchors},
        )
        for assessment in extraction.assessments:
            current = results[assessment.title]
            if current.due_precision == "tbd" and current.resolution_case == "relative_to_anchor":
                results[assessment.title] = resolve_date_expression(
                    assessment.date_expression_raw, round_two
                )
    return results


_REGISTRAR_PHRASES = (
    "university final exam schedule",
    "final exam schedule",
    "registrar",
    "university exam schedule",
    "see the university",
)


def _defers_to_registrar(expression: str | None) -> bool:
    if not expression:
        return True  # a final exam with no date at all is a registrar lookup
    lowered = expression.lower()
    return any(phrase in lowered for phrase in _REGISTRAR_PHRASES)


def to_scheduled_items(
    review_items: list[ReviewItem],
    *,
    course_label: str,
    credits: float,
    user_calibration: float = 1.0,
) -> list[ScheduledItem]:
    """Convert reviewed items into heatmap inputs, dropping what must not be scheduled.

    `tbd` items are excluded outright (9.3). `week_only` items are included with
    their band so effort spreads across the week rather than pinning to a day
    the syllabus never named.
    """
    out: list[ScheduledItem] = []
    for item in review_items:
        resolution = item.resolution
        if resolution.due_precision == "tbd":
            continue

        if resolution.due_precision == "week_only":
            assert resolution.week_start and resolution.week_end
            due_on = resolution.week_end
            band = (resolution.week_start, resolution.week_end)
        else:
            assert resolution.due_at is not None
            due_on = resolution.due_at.date()
            band = None

        out.append(
            ScheduledItem(
                assessment_id=item.title,
                title=item.title,
                course_label=course_label,
                assessment_type=item.assessment.type,
                due_on=due_on,
                credits=credits,
                weight_pct=item.assessment.weight_pct,
                is_group=item.assessment.is_group,
                user_calibration=user_calibration,
                week_band=band,
            )
        )
    return out


def run_pipeline(
    filename: str,
    data: bytes,
    calendar: AcademicCalendar,
    *,
    extractor,
    meeting_pattern: str | None = None,
    course_label: str = "Course",
    credits: float = 3.0,
    total_enrolled_credits: float = 15.0,
    allow_ocr: bool = True,
) -> PipelineResult:
    """Run one document all the way through.

    `extractor` is injected rather than imported so the pipeline can be exercised
    end to end without a live model — the offline path passes a recorded
    extraction, and production passes `extract_document`.
    """
    document = ingest(filename, data, allow_ocr=allow_ocr)
    logger.info(
        "ingested %s: %d page(s), scanned=%s, ~%d tokens",
        filename,
        document.page_count,
        document.is_scanned,
        document.estimated_tokens,
    )

    extraction = extractor(document)
    source_text = document.full_text

    ctx = ResolutionContext(
        calendar=calendar,
        meeting_pattern=meeting_pattern or extraction.course.meeting_pattern_raw,
    )
    resolved = resolve_assessments(extraction, ctx)

    flags = validate_extraction(
        extraction,
        page_count=document.page_count,
        source_text=source_text,
        resolved=resolved,
        term_start=calendar.start_date,
        term_end=calendar.finals_end or calendar.end_date,
    )
    flagged_titles = {t for f in flags for t in f.assessment_ids}

    review_items = []
    for assessment in extraction.assessments:
        resolution = resolved[assessment.title]
        score = composite_confidence(
            assessment,
            source_text=source_text,
            resolution=resolution,
            survived_validation=assessment.title not in flagged_titles,
        )
        review_items.append(
            ReviewItem(
                assessment=assessment,
                resolution=resolution,
                confidence=score,
                bucket=confidence_bucket(score),
            )
        )

    scheduled = to_scheduled_items(review_items, course_label=course_label, credits=credits)
    term_end = calendar.finals_end or calendar.end_date
    heatmap = build_heatmap(scheduled, calendar.start_date, term_end, total_enrolled_credits)
    collisions = compute_flags(
        scheduled, heatmap, calendar.start_date, term_end, total_enrolled_credits
    )

    return PipelineResult(
        document=document,
        extraction=extraction,
        review_items=review_items,
        flags=flags,
        heatmap=heatmap,
        collision_flags=collisions,
        hallucination_rate=hallucination_rate(extraction.assessments, source_text),
    )
