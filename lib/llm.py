"""Anthropic client wrapper — prompt caching, schema-validated outputs, cost caps.

Spec invariants:
  - Per-run hard cap: $2.00 USD. Mid-run breach → finish current call, abort
    cleanly between stages, log.
  - Daily hard cap: $10.00 USD. Beyond this, orchestrator refuses to run
    until next UTC day (caller checks before invoking).
  - Schema-failed agent outputs retry once with the validation error fed
    back; second failure aborts the run.
  - Halt flag (state/halt.flag) checked before every API call.

This is the only file that imports `anthropic` and the only file that touches
state/costs.jsonl directly via lib.state.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import ValidationError

from . import state

# USD per million tokens — published list prices for the Claude 4.X family.
# Override via env var if Anthropic adjusts pricing.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":               {"in": 15.00, "in_cache_write": 18.75, "in_cache_read": 1.50, "out": 75.00},
    "claude-sonnet-4-6":             {"in":  3.00, "in_cache_write":  3.75, "in_cache_read": 0.30, "out": 15.00},
    "claude-haiku-4-5-20251001":     {"in":  1.00, "in_cache_write":  1.25, "in_cache_read": 0.10, "out":  5.00},
}


class CostCapExceeded(RuntimeError):
    """Raised between calls when per-run or daily caps would be breached."""


class HaltFlagSet(RuntimeError):
    """Raised when state/halt.flag is present at call time."""


class SchemaRetryFailed(RuntimeError):
    """Raised when an agent output fails schema validation twice in a row."""


@dataclass
class CallUsage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @property
    def cache_hit_pct(self) -> float:
        total_in = self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens
        return 0.0 if total_in == 0 else 100.0 * self.cache_read_input_tokens / total_in


def estimate_cost_usd(model: str, usage: CallUsage) -> float:
    p = PRICING.get(model)
    if p is None:
        # Unknown model — conservative fallback at Sonnet rates so we don't
        # silently undercount costs.
        p = PRICING["claude-sonnet-4-6"]
    return (
        usage.input_tokens / 1_000_000 * p["in"]
        + usage.cache_creation_input_tokens / 1_000_000 * p["in_cache_write"]
        + usage.cache_read_input_tokens / 1_000_000 * p["in_cache_read"]
        + usage.output_tokens / 1_000_000 * p["out"]
    )


def _per_run_cap() -> float:
    return float(os.environ.get("PER_RUN_COST_CAP_USD", "2.00"))


def _daily_cap() -> float:
    return float(os.environ.get("DAILY_COST_CAP_USD", "10.00"))


def check_caps_or_raise(run_id: str) -> None:
    """Call before every API call. Raises CostCapExceeded if either cap would
    be at or beyond limit."""
    run_total = sum(r["cost_usd"] for r in state.read_costs_for_run(run_id))
    if run_total >= _per_run_cap():
        raise CostCapExceeded(f"per-run cap ${_per_run_cap():.2f} reached (run_total=${run_total:.4f})")
    day_total = sum(r["cost_usd"] for r in state.read_costs_today())
    if day_total >= _daily_cap():
        raise CostCapExceeded(f"daily cap ${_daily_cap():.2f} reached (day_total=${day_total:.4f})")


def _client():
    import anthropic  # noqa: WPS433
    return anthropic.Anthropic()


@dataclass
class StructuredCallResult:
    payload: Any
    usage: CallUsage
    cost_usd: float
    cache_hit_pct: float
    raw_text: str


def structured_call(
    *,
    run_id: str,
    stage: str,
    model: str,
    system_blocks: list[dict],
    user_messages: list[dict],
    schema_filename: str | None,
    max_tokens: int = 2048,
    client_factory: Callable[[], Any] | None = None,
) -> StructuredCallResult:
    """One LLM call with prompt caching, schema validation + one retry, cost record.

    `system_blocks` is a list of {type:"text", text:..., cache_control?:...}
    objects in the Anthropic format — the caller is responsible for marking
    static blocks with `{"type":"ephemeral"}` cache_control.
    """
    if state.is_halted():
        raise HaltFlagSet("halt.flag present; refusing to call LLM")
    check_caps_or_raise(run_id)

    cli = (client_factory or _client)()

    def _one_call(messages: list[dict]) -> StructuredCallResult:
        resp = cli.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
        )
        usage = CallUsage(
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
            cache_creation_input_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0),
            cache_read_input_tokens=getattr(resp.usage, "cache_read_input_tokens", 0),
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        cost = estimate_cost_usd(model, usage)
        state.append_cost({
            "run_id": run_id,
            "stage": stage,
            "model": model,
            "cost_usd": cost,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "at": state.utcnow_iso(),
        })

        payload: Any = None
        if schema_filename is not None:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValidationError(f"non-JSON response: {e}") from e
            state.validate(payload, schema_filename)
        else:
            payload = text

        return StructuredCallResult(
            payload=payload,
            usage=usage,
            cost_usd=cost,
            cache_hit_pct=usage.cache_hit_pct,
            raw_text=text,
        )

    try:
        return _one_call(user_messages)
    except (ValidationError, json.JSONDecodeError) as first_err:
        # Spec: retry once, feed the error back, then abort.
        retry_messages = list(user_messages) + [
            {
                "role": "user",
                "content": (
                    f"Your previous response failed schema validation against "
                    f"{schema_filename}: {first_err}. Re-emit ONLY valid JSON "
                    f"that conforms to the schema. No prose."
                ),
            }
        ]
        check_caps_or_raise(run_id)
        try:
            return _one_call(retry_messages)
        except (ValidationError, json.JSONDecodeError) as second_err:
            raise SchemaRetryFailed(
                f"two consecutive schema failures for {schema_filename}: {second_err}"
            ) from second_err
