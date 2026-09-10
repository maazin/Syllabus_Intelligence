"""LLM client wrapper — PRD sections 8, 25.3, and 18.1.

Isolates every model call behind one function so that the model cascade
(25.3), cost/latency accounting, and prompt versioning happen in exactly one
place. `extract.py` never talks to the SDK directly.

Structured output is enforced with `client.messages.parse()` against the
Pydantic models in `packages/core/schemas` — the extraction schema and the
output constraint are the same object, so they cannot drift apart.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import TypeVar

import anthropic
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Section 25.3's cascade. Both passes run on the cheap tier first; a document
#: only escalates when a validation rule fires, confidence is broadly low, or
#: extraction came back empty. Which tier served a request is logged on every
#: run so the hit rate is measurable from day one.
CHEAP_TIER = os.environ.get("LLM_MODEL_SYNC", "claude-sonnet-5")
STRONG_TIER = os.environ.get("LLM_MODEL_STRONG", "claude-opus-5")

#: Corpus seeding has no latency requirement, so it runs on the batch endpoint
#: at half price (18.1). Same model, different endpoint.
BATCH_TIER = os.environ.get("LLM_MODEL_BATCH", CHEAP_TIER)

#: Per-million-token rates for the cost estimate written to `extraction_runs`.
#: Approximate and for internal cost tracking only — never shown to users.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class ExtractionCallError(Exception):
    """A model call failed in a way the caller should surface, not retry blindly."""


@dataclass
class LLMResult:
    """One model call's output plus the accounting `extraction_runs` needs."""

    parsed: BaseModel
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    escalated: bool = False

    @property
    def cost_cents(self) -> float:
        input_rate, output_rate = _PRICING.get(self.model, (0.0, 0.0))
        dollars = (
            self.input_tokens / 1_000_000 * input_rate
            + self.output_tokens / 1_000_000 * output_rate
        )
        return round(dollars * 100, 4)


def _client() -> anthropic.Anthropic:
    """Build a client from the ambient credentials.

    No api_key argument: the SDK resolves `LLM_API_KEY`-equivalent credentials
    from `ANTHROPIC_API_KEY` or a logged-in profile, which keeps the key out of
    this process's code path entirely.
    """
    api_key = os.environ.get("LLM_API_KEY")
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def call_structured(
    *,
    system_prompt: str,
    user_content: str,
    output_model: type[T],
    model: str = CHEAP_TIER,
    max_tokens: int = 16000,
    escalated: bool = False,
) -> LLMResult:
    """One schema-constrained extraction call.

    Raises `ExtractionCallError` on anything the caller cannot use, so a single
    failed document is marked failed without taking down the batch (US-1).
    """
    client = _client()
    started = time.monotonic()

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            output_format=output_model,
        )
    except anthropic.RateLimitError as exc:
        raise ExtractionCallError(f"Rate limited calling {model}: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionCallError(f"{model} returned {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionCallError(f"Could not reach the model API: {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)

    # A safety decline is not a parse failure and must not be retried as one.
    if response.stop_reason == "refusal":
        raise ExtractionCallError(
            f"{model} declined to process this document "
            f"({getattr(response.stop_details, 'category', 'unspecified')})."
        )

    parsed = response.parsed_output
    if parsed is None:
        raise ExtractionCallError(f"{model} returned no parseable output")

    logger.info(
        "extraction call model=%s tier=%s latency_ms=%d in=%d out=%d",
        model,
        "strong" if escalated else "cheap",
        latency_ms,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    return LLMResult(
        parsed=parsed,
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
        escalated=escalated,
    )
