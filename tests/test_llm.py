"""Cost-cap and schema-retry tests for lib.llm — covers acceptance criterion #5.

The Anthropic SDK is not imported in these tests; we inject a fake client
factory that returns canned responses + usage.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from lib import llm, state


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class FakeMessages:
    def __init__(self, payloads, usages=None):
        self._payloads = list(payloads)
        self._usages = list(usages) if usages else [FakeUsage() for _ in payloads]
        self.calls = 0
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        i = self.calls
        self.calls += 1
        self.last_kwargs = kwargs
        text = self._payloads[i]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=self._usages[i],
        )


def _fake_client(messages: FakeMessages):
    return SimpleNamespace(messages=messages)


_DECISION_PAYLOAD = {
    "run_id": "rid",
    "stage": "screen",
    "model": "claude-haiku-4-5",
    "inputs_hash": "deadbeef" * 2,
    "output_ref": "screen.json",
    "prompt_cache_hit_pct": 80.0,
    "cost_usd": 0.04,
    "started_at": "2026-05-10T12:00:00Z",
    "ended_at": "2026-05-10T12:00:05Z",
    "status": "ok",
    "risk_warning": "PAPER TRADING.",
}


def _call(**overrides) -> llm.StageCall:
    base = dict(
        run_id="rid",
        stage="screen",
        model="claude-haiku-4-5",
        system_blocks=[{"type": "text", "text": "you are an agent"}],
        user_messages=[{"role": "user", "content": "do work"}],
        schema_filename="decision_log.schema.json",
    )
    base.update(overrides)
    return llm.StageCall(**base)


def test_structured_call_happy_path(tmp_state):
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)],
                       [FakeUsage(input_tokens=1000, output_tokens=200)])
    res = llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert res.payload["stage"] == "screen"
    assert res.cost_usd > 0
    assert state.read_costs_for_run("rid")


def test_thinking_and_output_config_passthrough(tmp_state):
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)])
    llm.structured_call(
        _call(
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": {}}},
        ),
        client_factory=lambda: _fake_client(fm),
    )
    assert fm.last_kwargs["thinking"] == {"type": "adaptive"}
    assert fm.last_kwargs["output_config"]["effort"] == "high"


def test_schema_retry_then_success(tmp_state):
    bad = json.dumps({"run_id": "rid", "stage": "magic"})
    good = json.dumps(_DECISION_PAYLOAD)
    fm = FakeMessages([bad, good])
    res = llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert res.payload["stage"] == "screen"
    assert fm.calls == 2


def test_schema_retry_failure_raises(tmp_state):
    bad = json.dumps({"run_id": "rid", "stage": "magic"})
    fm = FakeMessages([bad, bad])
    with pytest.raises(llm.SchemaRetryFailed):
        llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))


def test_per_run_cap_aborts_before_call(tmp_state, monkeypatch):
    monkeypatch.setenv("PER_RUN_COST_CAP_USD", "1.00")
    state.append_cost({
        "run_id": "rid", "stage": "prev", "model": "m",
        "cost_usd": 1.50, "at": state.utcnow_iso(),
    })
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)])
    with pytest.raises(llm.CostCapExceeded) as ei:
        llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert "per-run" in str(ei.value)
    assert fm.calls == 0


def test_daily_cap_aborts_across_runs(tmp_state, monkeypatch):
    monkeypatch.setenv("DAILY_COST_CAP_USD", "5.00")
    state.append_cost({
        "run_id": "earlier", "stage": "x", "model": "m",
        "cost_usd": 5.50, "at": state.utcnow_iso(),
    })
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)])
    with pytest.raises(llm.CostCapExceeded) as ei:
        llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert "daily" in str(ei.value)


def test_halt_flag_blocks_call(tmp_state):
    state.set_halt("test")
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)])
    with pytest.raises(llm.HaltFlagSet):
        llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))


def test_cost_estimate_uses_pricing_table():
    usage = llm.CallUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    # Opus 4.7 at $5/$25 — much cheaper than the deprecated $15/$75 numbers.
    assert llm.estimate_cost_usd("claude-opus-4-7", usage) == pytest.approx(5.00)
    assert llm.estimate_cost_usd("claude-sonnet-4-6", usage) == pytest.approx(3.00)
    assert llm.estimate_cost_usd("claude-haiku-4-5", usage) == pytest.approx(1.00)


def test_cache_read_pricing():
    usage = llm.CallUsage(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1_000_000,
    )
    # ~0.1x base input on Opus 4.7: $5 → $0.50
    assert llm.estimate_cost_usd("claude-opus-4-7", usage) == pytest.approx(0.50)


# ---------- schema sanitizer for Anthropic structured outputs ----------


def test_sanitize_strips_numeric_constraints():
    schema = {
        "type": "object",
        "properties": {
            "p": {"type": "number", "minimum": 0, "maximum": 1, "multipleOf": 0.01},
        },
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["properties"]["p"] == {"type": "number"}


def test_sanitize_strips_string_array_constraints():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 50, "pattern": "^[A-Z]+$"},
            "tags": {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True, "items": {"type": "string"}},
        },
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["properties"]["name"] == {"type": "string"}
    assert out["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}


def test_sanitize_strips_conditional_if_then_else():
    schema = {
        "type": "object",
        "if": {"properties": {"x": {"const": True}}},
        "then": {"required": ["y"]},
        "else": {"required": ["z"]},
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "if" not in out and "then" not in out and "else" not in out
    assert out["type"] == "object"


def test_sanitize_preserves_supported_keywords():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"enum": ["etf", "option"]},
            "ref": {"$ref": "#/$defs/x"},
            "fmt": {"type": "string", "format": "date-time"},
        },
        "required": ["kind"],
        "additionalProperties": False,
        "$defs": {"x": {"type": "string"}},
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["properties"]["kind"] == {"enum": ["etf", "option"]}
    assert out["properties"]["ref"] == {"$ref": "#/$defs/x"}
    assert out["properties"]["fmt"]["format"] == "date-time"
    assert out["additionalProperties"] is False
    assert out["required"] == ["kind"]


def test_sanitize_recurses_into_oneof_anyof_allof():
    schema = {
        "oneOf": [
            {"type": "number", "minimum": 0},
            {"type": "string", "minLength": 1},
        ],
        "allOf": [{"properties": {"n": {"type": "integer", "maximum": 10}}}],
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["oneOf"][0] == {"type": "number"}
    assert out["oneOf"][1] == {"type": "string"}
    assert out["allOf"][0]["properties"]["n"] == {"type": "integer"}


def test_sanitize_does_not_mutate_input():
    schema = {"type": "number", "minimum": 0, "maximum": 1}
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "minimum" in schema  # original untouched
    assert "minimum" not in out


def test_sanitize_on_real_portfolio_schema():
    """The actual portfolio.schema.json must produce a sanitized version with
    no banned keywords anywhere in the tree."""
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).parent.parent / "schemas" / "portfolio.schema.json").read_text())
    out = llm.sanitize_schema_for_structured_output(schema)
    banned = llm._STRUCTURED_OUTPUT_UNSUPPORTED_KEYS

    def walk(node):
        if isinstance(node, dict):
            for k in node:
                assert k not in banned, f"banned key {k!r} survived in sanitized schema"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out)


# ---------- $ref inlining ----------


def test_internal_ref_passes_through_unchanged():
    schema = {"properties": {"x": {"$ref": "#/$defs/x"}}, "$defs": {"x": {"type": "string"}}}
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["properties"]["x"] == {"$ref": "#/$defs/x"}


def test_external_ref_inlined_from_registry():
    target = {"$id": "https://x.local/foo.json", "type": "string", "minLength": 3}
    schema = {"type": "object", "properties": {"name": {"$ref": "https://x.local/foo.json"}}}
    out = llm.sanitize_schema_for_structured_output(
        schema, ref_registry={target["$id"]: target}
    )
    # Inlined and sanitized (minLength + $id stripped):
    assert out["properties"]["name"] == {"type": "string"}


def test_external_ref_not_in_registry_left_intact():
    schema = {"properties": {"x": {"$ref": "https://x.local/missing.json"}}}
    out = llm.sanitize_schema_for_structured_output(schema, ref_registry={})
    # We pass it through unchanged — Anthropic will 400, but we don't silently
    # corrupt by dropping the ref.
    assert out["properties"]["x"] == {"$ref": "https://x.local/missing.json"}


def test_metadata_id_and_schema_stripped():
    schema = {"$schema": "https://...", "$id": "https://x.local/y.json", "type": "object"}
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "$id" not in out and "$schema" not in out
    assert out["type"] == "object"


def test_real_portfolio_schema_inlined_no_external_refs():
    """The actual portfolio + position schemas must, after sanitisation with
    the schema registry, contain NO external $refs anywhere in the tree."""
    import json
    from pathlib import Path
    schema_dir = Path(__file__).parent.parent / "schemas"
    registry = {}
    for f in schema_dir.glob("*.schema.json"):
        s = json.loads(f.read_text())
        if "$id" in s:
            registry[s["$id"]] = s
    portfolio = json.loads((schema_dir / "portfolio.schema.json").read_text())
    out = llm.sanitize_schema_for_structured_output(portfolio, ref_registry=registry)

    def find_external_refs(node, found):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                found.append(ref)
            for v in node.values():
                find_external_refs(v, found)
        elif isinstance(node, list):
            for v in node:
                find_external_refs(v, found)
        return found

    assert find_external_refs(out, []) == []


# ---------- markdown fence stripping ----------


def test_strip_fences_with_json_lang_tag():
    assert llm._strip_markdown_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_without_lang_tag():
    assert llm._strip_markdown_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_no_op_when_no_fence():
    assert llm._strip_markdown_fences('{"a": 1}') == '{"a": 1}'
    assert llm._strip_markdown_fences('  {"a": 1}  ') == '{"a": 1}'


def test_structured_call_parses_fenced_response(tmp_state):
    """Even if the model wraps JSON in markdown fences, structured_call should
    parse it cleanly instead of triggering the schema-retry path."""
    fenced = '```json\n' + json.dumps(_DECISION_PAYLOAD) + '\n```'
    fm = FakeMessages([fenced])
    res = llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert res.payload["stage"] == "screen"
    assert fm.calls == 1  # no retry needed
