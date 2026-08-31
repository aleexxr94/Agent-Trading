"""Anthropic client wrapper — prompt caching, schema-validated outputs, cost caps.

Spec invariants:
  - Per-run hard cap: $3.00 USD. Mid-run breach → finish current call, abort
    cleanly between stages, log.
  - Daily hard cap: $12.00 USD. Beyond this, orchestrator refuses to run
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
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import ValidationError

from . import state


# Transient HTTP/SDK errors that warrant a retry rather than a hard crash.
# Anthropic's streaming edge drops connections mid-response on long-running
# Opus calls (httpx.RemoteProtocolError), times out on slow networks
# (httpx.ReadTimeout / anthropic.APITimeoutError), AND occasionally returns
# 5xx / 529 Overloaded when traffic is heavy (anthropic.InternalServerError
# raised with type='overloaded_error'). Rate-limit (429) is also worth
# retrying — Anthropic recommends exponential backoff for those.
#
# 4xx errors like 400/401/403/404/422 should NOT be retried — they signal a
# request bug (auth, schema, malformed contract), not a transient outage.
# anthropic's typed exceptions split them out cleanly: BadRequestError /
# AuthenticationError / PermissionDeniedError / NotFoundError /
# UnprocessableEntityError are all separate subclasses of APIStatusError
# and stay outside our retry tuple.
#
# Imported lazily inside _retryable_stream_errors() so a missing optional
# dependency (httpx is a transitive dep of the anthropic SDK) can't break
# import of this module.
def _retryable_stream_errors() -> tuple[type[BaseException], ...]:
    errs: list[type[BaseException]] = []
    try:
        import httpx
        errs.extend([
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ReadError,
        ])
    except ImportError:
        pass
    try:
        import anthropic
        errs.extend([
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.InternalServerError,  # 5xx incl. 529 Overloaded
            anthropic.RateLimitError,       # 429 — backoff handles it
        ])
    except ImportError:
        pass
    return tuple(errs) or (ConnectionError,)


# Some Anthropic in-stream SSE error events surface as the BASE
# `anthropic.APIStatusError`, not its typed `InternalServerError` /
# `RateLimitError` subclasses (observed May 12 2026: an overloaded_error
# raised as base APIStatusError mid-stream, bypassing PR #47's typed retry
# tuple). We can't put the base class in `_retryable_stream_errors()` —
# that would also retry 4xx auth / bad-request / schema errors, which are
# bugs not outages. Instead, inspect the exception body + status code at
# catch time and retry only when it represents a transient condition.
def _is_transient_anthropic_error(exc: BaseException) -> bool:
    try:
        import anthropic
    except ImportError:
        return False
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    # Body-based check: in-stream SSE error events carry a dict body with
    # the upstream error type. `overloaded_error` is the 529 we keep
    # seeing on heavy Sonnet/Opus traffic and is always worth a retry.
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            etype = err.get("type")
            if etype in ("overloaded_error", "api_error", "timeout_error"):
                return True
    # Status-code fallback for cases where body isn't a dict (e.g. raw
    # response surfaced through the base class). 5xx and 429 are
    # transient; 4xx (except 429) is a request bug — don't retry.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status >= 500 or status == 429):
        return True
    return False


# Retry tuning. Five attempts total (first try + 4 retries). Exponential
# backoff 2s, 5s, 10s, 20s — total worst-case wait ~37s. Heavier than the
# original 1s/4s because Anthropic's "overloaded_error" responses often
# need 10-30s to clear on the server side. Short enough not to hide a
# sustained outage; long enough to ride out the brief 529 bursts we saw
# on May 12 2026 during a live scenarios call.
STREAM_RETRY_ATTEMPTS = 5
STREAM_RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 20.0)

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
    """Return a new schema acceptable to Anthropic's structured-outputs feature.

    Discovered constraints (each surfaced by a separate 400 from the API):
      - Numerical / string / array constraints not allowed (PR #5).
      - External `$ref` URIs not allowed (PR #6).
      - `oneOf` not allowed — rewrite to `anyOf` (PR #7).
      - `$defs` blocks not allowed alongside `anyOf` — fully inline internal
        `#/$defs/foo` refs and strip every `$defs` block (this PR).

    Pass `ref_registry` to resolve external refs by their `$id`:
        registry = {schema["$id"]: schema for schema in sibling_schemas}
        sanitize_schema_for_structured_output(root, ref_registry=registry)

    Local validation still uses the unmodified original via lib.state.validate(),
    so the schema sent to Anthropic is intentionally weaker than what we
    enforce locally — just enough for server-side structural validation while
    keeping our richer constraints on the client side.
    """
    registry = ref_registry or {}

    # --- Phase 1: inline external refs into a working copy.
    def _inline_external(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#") and ref in registry:
                return _inline_external(registry[ref])
            return {k: _inline_external(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_inline_external(v) for v in node]
        return node

    inlined = _inline_external(schema)

    # --- Phase 2: harvest every $defs entry across the inlined tree into a
    # single map. Forward refs (anyOf [{$ref:#/$defs/x}] appearing before
    # $defs.x in tree order) are handled correctly because collection is
    # complete before any substitution happens.
    defs: dict[str, dict] = {}

    def _collect_defs(node):
        if isinstance(node, dict):
            for k, v in node.get("$defs", {}).items():
                defs[k] = v
            for v in node.values():
                _collect_defs(v)
        elif isinstance(node, list):
            for v in node:
                _collect_defs(v)

    _collect_defs(inlined)

    # --- Phase 3: sanitize + rewrite oneOf→anyOf + inline internal $refs +
    # strip every $defs block.
    def _walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
                if name in defs:
                    return _walk(defs[name])
            out: dict = {}
            for k, v in node.items():
                if k in _STRUCTURED_OUTPUT_UNSUPPORTED_KEYS:
                    continue
                if k in _STRUCTURED_OUTPUT_METADATA_TO_STRIP:
                    continue
                if k == "$defs":
                    continue
                if k == "oneOf":
                    out["anyOf"] = _walk(v)
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    return _walk(inlined)


_CLOSING_FENCE_RE = re.compile(r"\n```")


def strip_markdown_fences(text: str) -> str:
    """Extract the first JSON object from LLM output, tolerating prose
    preamble, prose epilogue, and markdown fences in any combination.

    Three observed failure modes in production:
      (a) ```json … ``` fence with nothing after the closer (Haiku, 2025).
      (b) ```json … ``` fence AND a "Regime flags & rationale:" prose
          epilogue outside the closing fence (live paper run 2026-05-12;
          screener produced 12 valid candidates but json.loads choked on
          the trailing prose).
      (c) No fence at all — Sonnet 4.6 with adaptive thinking writes
          a reasoning preamble + raw JSON + a trailing summary, so the
          string is "Here is the next-run plan: {…} This schedules…".
          Observed on the orchestrator-meta stage producing
          "meta call failed (JSONDecodeError); using heuristic" on
          ~100% of cycles on the 2026-05-22 VPS deploy.

    Resolution strategy:
      1. If the text starts with ``` strip the fence + trailing prose
         (handles (a) and (b)). The closing fence is matched as
         ``\\n``` to avoid truncating triple-backticks legitimately
         embedded inside a JSON string value.
      2. Otherwise (handles (c)): find the first balanced ``{…}`` OR
         ``[…]`` block — whichever opening character appears first.
         Scan from there, track depth on the matching delimiter pair
         honouring quoted strings + backslash escapes, return the slice
         when depth hits 0. Top-level arrays are valid JSON
         (``[{"a":1},{"b":2}]``) and must survive intact, not be
         truncated to their first element (Codex P2 on PR #89).
      3. If neither yields a result, return the stripped input unchanged
         and let the caller's `json.loads` raise — the original behaviour.

    The balanced-delimiter extraction is necessary because regex can't
    correctly handle nested objects in JSON values (e.g. a portfolio
    array with nested kill_conditions dicts).
    """
    t = text.strip()
    if not t:
        return t
    # --- (a) / (b): leading markdown fence ---
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            body = t[nl + 1:]
            m = _CLOSING_FENCE_RE.search(body)
            if m is not None:
                body = body[:m.start()]
            stripped = body.strip()
            # The brace-balanced extractor below also handles a body
            # that the closing-fence cut left with trailing junk; fall
            # through to it instead of returning here.
            t = stripped or t
    # --- (c): prose around raw JSON — pull the first balanced {…} or […] ---
    # Top-level JSON can legitimately be an array (`[{"a":1},{"b":2}]`),
    # not just an object. Codex P2 (PR #89): the original brace-only
    # scan truncated such arrays to their first element. Detect the
    # first JSON-opening character and balance against the matching
    # close so both shapes survive intact.
    first_obj = t.find("{")
    first_arr = t.find("[")
    if first_obj == -1 and first_arr == -1:
        return t
    if first_obj == -1:
        first, open_ch, close_ch = first_arr, "[", "]"
    elif first_arr == -1:
        first, open_ch, close_ch = first_obj, "{", "}"
    else:
        # Whichever opening character comes first wins.
        if first_obj < first_arr:
            first, open_ch, close_ch = first_obj, "{", "}"
        else:
            first, open_ch, close_ch = first_arr, "[", "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(first, len(t)):
        ch = t[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return t[first:i + 1]
    # Unbalanced — return the stripped input so the caller's json.loads
    # raises with the original context. Pre-existing behaviour.
    return t


class CostCapExceeded(RuntimeError):
    """Raised between calls when per-run or daily caps would be breached.

    ``cap`` is machine-readable ("per_run" | "daily") so the pipeline crash
    handler can pick the right reschedule (fresh run soon vs next UTC day)
    without parsing the human-readable message.
    """

    def __init__(self, message: str, *, cap: str = "per_run"):
        super().__init__(message)
        self.cap = cap


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
    return float(os.environ.get("PER_RUN_COST_CAP_USD", "3.00"))


def _daily_cap() -> float:
    return float(os.environ.get("DAILY_COST_CAP_USD", "12.00"))


def check_caps_or_raise(run_id: str) -> None:
    """Call before every API call. Raises CostCapExceeded if either cap is at
    or beyond limit (mid-call enforcement happens between stages, per spec)."""
    run_total = sum(r["cost_usd"] for r in state.read_costs_for_run(run_id))
    if run_total >= _per_run_cap():
        raise CostCapExceeded(
            f"per-run cap ${_per_run_cap():.2f} reached (run_total=${run_total:.4f})",
            cap="per_run",
        )
    day_total = sum(r["cost_usd"] for r in state.read_costs_today())
    if day_total >= _daily_cap():
        raise CostCapExceeded(
            f"daily cap ${_daily_cap():.2f} reached (day_total=${day_total:.4f})",
            cap="daily",
        )


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

        # The Anthropic SDK refuses non-streaming requests once max_tokens
        # is high enough that the worst-case latency could exceed 10 minutes
        # (raises ValueError: "Streaming is required for operations that may
        # take longer than 10 minutes"). The scenarios stage uses
        # max_tokens=32768 + adaptive thinking, which trips that guard.
        # Streaming sidesteps the timeout — we still wait for the final
        # message, so the rest of the call site (cost recording, schema
        # validation, retry) is unchanged.
        #
        # Transient HTTP-level retry: Anthropic's edge occasionally closes
        # the streaming connection mid-response on long Opus calls
        # (observed RemoteProtocolError on a construct call with 32k
        # max_tokens + adaptive thinking). One blip used to take down the
        # whole orchestrator cycle — now we retry with backoff. The
        # downstream schema-retry path is separate; this loop only
        # handles network/protocol failures.
        retryable = _retryable_stream_errors()
        last_exc: BaseException | None = None
        for attempt in range(STREAM_RETRY_ATTEMPTS):
            try:
                with cli.messages.stream(**kwargs) as stream:
                    resp = stream.get_final_message()
                break  # success
            except BaseException as exc:
                # Retry only if the error matches one of the typed transient
                # classes OR the predicate detects a transient overloaded /
                # 5xx / 429 condition surfaced through base APIStatusError.
                if not (
                    isinstance(exc, retryable)
                    or _is_transient_anthropic_error(exc)
                ):
                    raise
                last_exc = exc
                if attempt == STREAM_RETRY_ATTEMPTS - 1:
                    raise
                backoff = STREAM_RETRY_BACKOFF_SECONDS[
                    min(attempt, len(STREAM_RETRY_BACKOFF_SECONDS) - 1)
                ]
                print(
                    f"llm: stream attempt {attempt + 1}/{STREAM_RETRY_ATTEMPTS} "
                    f"failed ({type(exc).__name__}: {exc}); retrying in {backoff}s",
                    flush=True,
                )
                time.sleep(backoff)
        else:
            # Loop exhausted without break — re-raise the last exception.
            assert last_exc is not None
            raise last_exc
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
            cleaned = strip_markdown_fences(text)
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
