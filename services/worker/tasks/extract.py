"""Two-pass extraction — PRD sections 8 and 25.

Pass A (structure) runs first and its grade breakdown is fed into Pass B
(schedule), so the model can link "Problem Set 4" in the schedule to
"Problem Sets, 20%" in the breakdown. One giant call is explicitly rejected
by the PRD: Pass A materially improves Pass B, and separating them lets each
be evaluated and iterated independently.

Escalation to the stronger model (25.3) is decided *after* validation, not
from the model's own self-report — a confidently wrong extraction is exactly
the case that needs the stronger tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from prompts import CURRENT_PROMPT_VERSION, load_prompt
from prompts.registry import extraction_run
from schemas.extraction import ExtractionOutput, PassAOutput, PassBOutput
from schemas.validation import ValidationFlag

try:
    from services.worker.tasks import replay
    from services.worker.tasks.ingest import IngestedDocument
    from services.worker.tasks.llm import (
        CHEAP_TIER,
        STRONG_TIER,
        LLMResult,
        call_structured,
    )
    from services.worker.tasks.validate import validate_extraction
except ImportError:  # running from inside services/worker/tasks
    # Unresolvable to a type checker on purpose: these names only exist on
    # sys.path when a script is run from this directory, which is how the
    # pipeline script invokes them. The package-qualified imports above are
    # the ones mypy checks.
    import replay  # type: ignore[no-redef,import-not-found]
    from ingest import IngestedDocument  # type: ignore[no-redef,import-not-found]
    from llm import (  # type: ignore[no-redef,import-not-found]
        CHEAP_TIER,
        STRONG_TIER,
        LLMResult,
        call_structured,
    )
    from validate import validate_extraction  # type: ignore[no-redef,import-not-found]

logger = logging.getLogger(__name__)

#: 25.3: escalate when more than this share of items land in the `low` bucket.
LOW_CONFIDENCE_ESCALATION_SHARE = 0.20

#: 10.1's buckets.
HIGH_CONFIDENCE = 0.85
MEDIUM_CONFIDENCE = 0.60


@dataclass
class ExtractionResult:
    """Everything one document's extraction produced, ready to persist."""

    output: ExtractionOutput
    flags: list[ValidationFlag] = field(default_factory=list)
    prompt_version: str = CURRENT_PROMPT_VERSION
    model: str = CHEAP_TIER
    escalated: bool = False
    cost_cents: float = 0.0
    latency_ms: int = 0
    runs: list[LLMResult] = field(default_factory=list)

    @property
    def hard_failure(self) -> bool:
        """9.4's hard failure: route to manual entry with an apology, not an empty screen."""
        return any(f.severity == "hard_failure" for f in self.flags)


def _pass_a(text: str, model: str, *, escalated: bool) -> LLMResult:
    return call_structured(
        system_prompt=load_prompt("pass_a_structure"),
        user_content=(
            "Extract the course metadata, the full grading breakdown table, and all course "
            "policies (attendance, late work, group work, final exam type, AI use, "
            "required materials).\n\n"
            f"Syllabus text:\n{text}"
        ),
        output_model=PassAOutput,
        model=model,
        escalated=escalated,
    )


def _pass_b(text: str, pass_a: PassAOutput, model: str, *, escalated: bool) -> LLMResult:
    breakdown_json = pass_a.model_dump_json(include={"grade_breakdown"}, indent=2)
    return call_structured(
        system_prompt=load_prompt("pass_b_schedule"),
        user_content=(
            "Grade breakdown from the prior pass:\n"
            f"{breakdown_json}\n\n"
            f"Syllabus text:\n{text}\n\n"
            "Extract every graded item with its date expression exactly as written, its "
            "type, its weight, and its category_ref."
        ),
        output_model=PassBOutput,
        model=model,
        escalated=escalated,
    )


def _should_escalate(
    output: ExtractionOutput, flags: list[ValidationFlag], page_count: int
) -> bool:
    """Section 25.3's three escalation triggers, in order of cost to check."""
    if any(f.severity in ("warning", "hard_failure") for f in flags):
        return True
    if page_count > 1 and not output.assessments:
        return True
    if output.assessments:
        low = sum(1 for a in output.assessments if a.confidence < MEDIUM_CONFIDENCE)
        if low / len(output.assessments) > LOW_CONFIDENCE_ESCALATION_SHARE:
            return True
    return False


def extract_document(
    document: IngestedDocument,
    *,
    allow_escalation: bool = True,
) -> ExtractionResult:
    """Run both passes over an ingested document, escalating tiers if needed.

    The whole extraction is wrapped in one MLflow run tagged with the prompt
    version (section 16), so a production correction rate can later be
    attributed to the exact prompt that produced the item.
    """
    with extraction_run(
        document_id=document.sha256, prompt_version=CURRENT_PROMPT_VERSION, model=CHEAP_TIER
    ) as record:
        result = _extract(document, allow_escalation=allow_escalation)
        record(
            assessments=len(result.output.assessments),
            escalated=result.escalated,
            cost_cents=result.cost_cents,
            latency_ms=result.latency_ms,
            flags=",".join(f.check for f in result.flags) or "none",
            pages=document.page_count,
            is_scanned=document.is_scanned,
        )
        return result


def _extract(document: IngestedDocument, *, allow_escalation: bool) -> ExtractionResult:
    text = document.full_text
    runs: list[LLMResult] = []

    if replay.is_enabled():
        return _replayed(document, text)

    def run_tier(model: str, escalated: bool) -> tuple[ExtractionOutput, list[LLMResult]]:
        a = _pass_a(text, model, escalated=escalated)
        b = _pass_b(text, a.parsed, model, escalated=escalated)  # type: ignore[arg-type]
        merged = ExtractionOutput.from_passes(a.parsed, b.parsed)  # type: ignore[arg-type]
        return merged, [a, b]

    output, tier_runs = run_tier(CHEAP_TIER, escalated=False)
    runs.extend(tier_runs)
    flags = validate_extraction(output, page_count=document.page_count, source_text=text)

    escalated = False
    if allow_escalation and _should_escalate(output, flags, document.page_count):
        logger.info("Escalating document to %s: %s", STRONG_TIER, [f.check for f in flags])
        try:
            strong_output, strong_runs = run_tier(STRONG_TIER, escalated=True)
        except Exception:
            # The cheap-tier result is still usable and already validated. Losing
            # it because the escalation failed would be strictly worse for the user.
            logger.exception("Escalation to %s failed; keeping cheap-tier result", STRONG_TIER)
        else:
            runs.extend(strong_runs)
            strong_flags = validate_extraction(
                strong_output, page_count=document.page_count, source_text=text
            )
            # Keep the escalated result unless it is *worse* by flag count — the
            # stronger model is not unconditionally better on every document.
            if len(strong_flags) <= len(flags):
                output, flags, escalated = strong_output, strong_flags, True

    return ExtractionResult(
        output=output,
        flags=flags,
        prompt_version=CURRENT_PROMPT_VERSION,
        model=STRONG_TIER if escalated else CHEAP_TIER,
        escalated=escalated,
        cost_cents=round(sum(r.cost_cents for r in runs), 4),
        latency_ms=sum(r.latency_ms for r in runs),
        runs=runs,
    )


def _replayed(document: IngestedDocument, text: str) -> ExtractionResult:
    """A recorded extraction in place of the model call. See `replay.py`.

    Validation still runs on the replayed output, so the flags a reviewer sees
    are the real ones for this document, and the `model` field names the
    recording so an extraction_runs row never claims a model that was not
    called.
    """
    # ReplayError is left to propagate. It is a permanent outcome for this
    # document, not a transient one, and `parse_document` records it as such
    # rather than retrying it the way it retries a rate limit.
    name, output = replay.replay(text)

    flags = validate_extraction(output, page_count=document.page_count, source_text=text)
    return ExtractionResult(
        output=output,
        flags=flags,
        prompt_version=CURRENT_PROMPT_VERSION,
        model=f"replay:{name}",
        escalated=False,
        cost_cents=0.0,
        latency_ms=0,
        runs=[],
    )
