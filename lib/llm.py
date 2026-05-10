"""Anthropic client wrapper — prompt caching, schema-validated outputs, cost caps.

Spec invariants:
  - Per-run hard cap: $2.00 USD. Mid-run breach → finish current call, abort
    cleanly between stages, log.
  - Daily hard cap: $10.00 USD. Beyond this, orchestrator refuses to run
    until next UTC day (caller checks before invoking).
  - Schema-failed agent outputs retry once with the validation error fed
    back; second failure aborts the run.
  - Halt flag (state/halt.flag) checked before every API call.

Claude 4.X feature support:
  - Adaptive thinking via thinking={"type": "adaptive"} (Opus 4.7, Sonnet 4.6)
  - Effort via output_config={"effort": "..."} (low/medium/high/xhigh/max).
    xhigh is Opus 4.7 only; max is Opus-tier only. Sonnet 4.6 supports
    low/medium/high. Haiku 4.5 does NOT accept effort and will 400.
  - Structured outputs via output_config={"format": {"type": "json_schema",
    "schema": {...}}} — server-validated, replaces assistant-prefill prompts
    (which 400 on 4.6/4.7).
  - Sampling params (temperature/top_p/top_k) are removed on Opus 4.7. Don't
    pass them.

This is the only file that imports `anthropic` and the only file that touches
state/costs.jsonl directly via lib.state.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import ValidationError

from . import state

# USD per million tokens — Claude 4.X published list prices.
# Cache writes at 1.25x base input (5min TTL); cache reads at ~0.1x base input.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":            {"in": 5.00, "in_cache_write": 6.25, "in_cache_read": 0.50, "out": 25.00},
    "claude-opus-4-6":            {"in": 5.00, "in_cache_write": 6.25, "in_cache_read": 0.50, "out": 25.00},
    "claude-sonnet-4-6":          {"in": 3.00, "in_cache_write": 3.75, "in_cache_read": 0.30, "out": 15.00},
    "claude-haiku-4-5":           {"in": 1.00, "in_cache_write": 1.25, "in_cache_read": 0.10, "out":  5.00},
    "claude-haiku-4-5-20251001":  {"in": 1.00, "in_cache_write": 1.25, "in_cache_read": 0.10, "out":  5.00},
}


# JSON-Schema keywords that Anthropic's structured-outputs feature rejects
# with a 400 ("not supported"). Local validation still uses the full schema;
# only the network-bound copy passed via output_config.format is sanitized.
# Source: Anthropic structured-outputs docs (2026-04 snapshot).
_STRUCTURED_OUTPUT_UNSUPPORTED_KEYS: frozenset[str] = frozenset({
    # Numerical
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    # String
    "minLength", "maxLength", "pattern",
    # Array
    "minItems", "maxItems", "uniqueItems",
    # Conditional
    "if", "then", "else",
    # Property-count
    "minProperties", "maxProperties",
})

# Root-level metadata that confuses the structured-outputs validator after
# inlining (they only make sense on the outermost schema). Strip everywhere
# to be safe — they carry no validation semantics.
_STRUCTURED_OUTPUT_METADATA_TO_STRIP: frozenset[str] = frozenset({
    "$id", "$schema",
})


def sanitize_schema_for_structured_output(
    schema,
    *,
    ref_registry: dict[str, dict] | None = None,
):
    """Return a new schema with unsupported keywords recursively removed.

    Anthropic's structured-outputs feature rejects:
      - Standard JSON Schema constraints (minimum/maximum/minLength/...).
      - External `$ref` URIs — only internal `#/$defs/...` refs are allowed.

    Pass `ref_registry` to inline external refs by their `$id`:
        registry = {schema["$id"]: schema for schema in sibling_schemas}
        sanitize_schema_for_structured_output(root, ref_registry=registry)

    Internal `$ref` values (anything starting with `#`) pass through unchanged.

    Local validation still uses the unmodified original via lib.state.validate(),
    so removing these does not weaken anything structural.
    """
    registry = ref_registry or {}

    def _walk(node):
        if isinstance(node, dict):
            # External $ref → inline from registry, then walk the inlined content.
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#") and ref in registry:
                return _walk(registry[ref])
            return {
                k: _walk(v)
                for k, v in node.items()
                if k not in _STRUCTURED_OUTPUT_UNSUPPORTED_KEYS
                and k not in _STRUCTURED_OUTPUT_METADATA_TO_STRIP
            }
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    return _walk(schema)


def _strip_markdown_fences(text: str) -> str:
    """Sonnet sometimes wraps JSON in ```json … ``` fences despite explicit
    instructions to the contrary. Strip them defensively before json.loads."""
    t = text.strip()
    if t.startswith("```"):
        # Drop the opening fence (with optional language tag) and the closing fence.
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith("```"):
            t = t[: -3]
        t = t.strip()
    return t


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
        # Unknown model — fall back to Opus pricing so we don't undercount.
        p = PRICING["claude-opus-4-7"]
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
    """Call before every API call. Raises CostCapExceeded if either cap is at
    or beyond limit (mid-call enforcement happens between stages, per spec)."""
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


@dataclass
class StageCall:
    """All inputs needed for one schema-validated LLM call."""
    run_id: str
    stage: str
    model: str
    system_blocks: list[dict]
    user_messages: list[dict]
    schema_filename: str | None = None
    max_tokens: int = 4096
    thinking: dict | None = None         # e.g. {"type": "adaptive"}
    output_config: dict | None = None    # e.g. {"effort": "high", "format": {...}}
    extra: dict = field(default_factory=dict)


def structured_call(
    call: StageCall,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> StructuredCallResult:
    """One LLM call with prompt caching, schema validation + one retry, cost record.

    `call.system_blocks` is the Anthropic-format list — caller marks static
    blocks with `cache_control: {"type": "ephemeral"}` for prompt caching.
    """
    if state.is_halted():
        raise HaltFlagSet("halt.flag present; refusing to call LLM")
    check_caps_or_raise(call.run_id)

    cli = (client_factory or _client)()

    def _one_call(messages: list[dict]) -> StructuredCallResult:
        kwargs: dict[str, Any] = {
            "model": call.model,
            "max_tokens": call.max_tokens,
            "system": call.system_blocks,
            "messages": messages,
        }
        if call.thinking is not None:
            kwargs["thinking"] = call.thinking
        if call.output_config is not None:
            kwargs["output_config"] = call.output_config
        kwargs.update(call.extra)

        resp = cli.messages.create(**kwargs)
        usage = CallUsage(
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
            cache_creation_input_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0),
            cache_read_input_tokens=getattr(resp.usage, "cache_read_input_tokens", 0),
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )
        cost = estimate_cost_usd(call.model, usage)
        state.append_cost({
            "run_id": call.run_id,
            "stage": call.stage,
            "model": call.model,
            "cost_usd": cost,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "at": state.utcnow_iso(),
        })

        if call.schema_filename is not None:
            cleaned = _strip_markdown_fences(text)
            try:
                payload = json.loads(cleaned)
            except json.JSONDecodeError as e:
                raise ValidationError(f"non-JSON response: {e}") from e
            state.validate(payload, call.schema_filename)
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
        return _one_call(call.user_messages)
    except (ValidationError, json.JSONDecodeError) as first_err:
        retry_messages = list(call.user_messages) + [
            {
                "role": "user",
                "content": (
                    f"Your previous response failed schema validation against "
                    f"{call.schema_filename}: {first_err}. Re-emit ONLY valid "
                    f"JSON that conforms to the schema. No prose."
                ),
            }
        ]
        check_caps_or_raise(call.run_id)
        try:
            return _one_call(retry_messages)
        except (ValidationError, json.JSONDecodeError) as second_err:
            raise SchemaRetryFailed(
                f"two consecutive schema failures for {call.schema_filename}: {second_err}"
            ) from second_err
