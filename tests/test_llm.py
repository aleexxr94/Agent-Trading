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

    def _next_message(self, kwargs):
        i = self.calls
        self.calls += 1
        self.last_kwargs = kwargs
        text = self._payloads[i]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=self._usages[i],
        )

    def create(self, **kwargs):
        # Retained for backwards compat with callers that may still use the
        # non-streaming path. Production code (lib.llm._one_call) now uses
        # stream() to sidestep the SDK's 10-minute non-streaming timeout.
        return self._next_message(kwargs)

    def stream(self, **kwargs):
        return _FakeStreamCtx(self._next_message(kwargs))


class _FakeStreamCtx:
    """Mimics the context-manager returned by anthropic SDK's messages.stream()."""

    def __init__(self, final_message):
        self._final = final_message

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get_final_message(self):
        return self._final


def _fake_client(messages: FakeMessages):
    return SimpleNamespace(messages=messages)


def _make_apistatus_error(anthropic_mod, *, status_code: int, body: dict, message: str = "err"):
    """Build a real anthropic.APIStatusError with a working response object.

    The SDK's constructor reads ``response.request`` so we can't pass None.
    Construct a minimal httpx.Response + Request pair.
    """
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status_code=status_code, request=req, json=body)
    return anthropic_mod.APIStatusError(message=message, response=resp, body=body)


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


def test_structured_call_uses_streaming_api(tmp_state):
    """Regression: lib.llm must call messages.stream(), not messages.create().
    The Anthropic SDK refuses non-streaming requests when max_tokens is high
    enough to risk a >10-minute response — observed crash on the first paper
    run after scenarios was bumped to max_tokens=32768."""
    create_calls = {"n": 0}
    stream_calls = {"n": 0}

    class _SpyMessages(FakeMessages):
        def create(self, **kw):
            create_calls["n"] += 1
            return super().create(**kw)

        def stream(self, **kw):
            stream_calls["n"] += 1
            return super().stream(**kw)

    fm = _SpyMessages([json.dumps(_DECISION_PAYLOAD)])
    llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert stream_calls["n"] == 1, "expected exactly one stream() call"
    assert create_calls["n"] == 0, "non-streaming create() must not be used"


def test_structured_call_retries_on_transient_stream_error(tmp_state, monkeypatch):
    """Regression for an observed live failure: Anthropic's streaming edge
    dropped a connection mid-response on a long construct call, raising
    httpx.RemoteProtocolError. lib.llm previously crashed the whole
    orchestrator on the first such blip. Now we retry with backoff and
    only escalate after exhausting STREAM_RETRY_ATTEMPTS.
    """
    httpx = pytest.importorskip("httpx")  # only installed alongside anthropic

    monkeypatch.setattr(llm, "STREAM_RETRY_BACKOFF_SECONDS", (0.0, 0.0))

    attempts = {"n": 0}

    class _FlakyStreamCtx:
        def __init__(self, fail_count: int, then_payload: str):
            self._fail_count = fail_count
            self._payload = then_payload

        def __enter__(self):
            attempts["n"] += 1
            if attempts["n"] <= self._fail_count:
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body"
                )
            return self

        def __exit__(self, *_a):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self._payload)],
                usage=FakeUsage(),
            )

    class _FlakyMessages:
        def __init__(self, fail_count: int, payload: str):
            self._fail_count = fail_count
            self._payload = payload

        def stream(self, **kwargs):
            return _FlakyStreamCtx(self._fail_count, self._payload)

    # 2 failures, then succeed → expect 3 attempts total
    fm = _FlakyMessages(fail_count=2, payload=json.dumps(_DECISION_PAYLOAD))
    res = llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert attempts["n"] == 3
    assert res.payload["stage"] == "screen"


def test_structured_call_gives_up_after_max_retries(tmp_state, monkeypatch):
    """If every attempt fails with a transient error, eventually re-raise
    the underlying exception rather than retrying forever."""
    httpx = pytest.importorskip("httpx")

    monkeypatch.setattr(llm, "STREAM_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(llm, "STREAM_RETRY_ATTEMPTS", 3)

    class _AlwaysFailMessages:
        def stream(self, **kwargs):
            class _Ctx:
                def __enter__(self_):
                    raise httpx.RemoteProtocolError("perma-broken")
                def __exit__(self_, *_a):
                    return False
            return _Ctx()

    fm = _AlwaysFailMessages()
    with pytest.raises(httpx.RemoteProtocolError):
        llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))


def test_structured_call_retries_on_anthropic_overloaded(tmp_state, monkeypatch):
    """Regression: live observation May 12 2026 had a scenarios call die on
    anthropic.APIStatusError with type='overloaded_error' (HTTP 529). The
    typed subclass is anthropic.InternalServerError. Previously this wasn't
    in the retry tuple — one 529 ate the whole cycle. After this PR it's
    retryable and one transient overload should not crash the orchestrator."""
    anthropic = pytest.importorskip("anthropic")

    monkeypatch.setattr(llm, "STREAM_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(llm, "STREAM_RETRY_ATTEMPTS", 5)

    attempts = {"n": 0}

    class _OverloadedThenOkCtx:
        def __init__(self, payload: str):
            self._payload = payload

        def __enter__(self):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                # Simulate the typed 529 InternalServerError subclass.
                import httpx
                req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                resp = httpx.Response(status_code=529, request=req, json={"type": "overloaded_error"})
                raise anthropic.InternalServerError(
                    message="Overloaded",
                    response=resp,
                    body={"type": "overloaded_error"},
                )
            return self

        def __exit__(self, *_a):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self._payload)],
                usage=FakeUsage(),
            )

    class _OverloadedMessages:
        def stream(self, **kwargs):
            return _OverloadedThenOkCtx(json.dumps(_DECISION_PAYLOAD))

    res = llm.structured_call(_call(), client_factory=lambda: _fake_client(_OverloadedMessages()))
    assert attempts["n"] == 3, "should retry past 2 overloaded errors and succeed on the 3rd"
    assert res.payload["stage"] == "screen"


def test_structured_call_retries_on_base_apistatuserror_overloaded(tmp_state, monkeypatch):
    """Regression for PR #48: the Anthropic SDK raises the BASE
    ``anthropic.APIStatusError`` class (not the typed ``InternalServerError``
    subclass) when an in-stream SSE error event carries
    ``error.type == 'overloaded_error'``. PR #47 only retried the typed
    subclass, so a single overload mid-stream still crashed the cycle.
    The predicate-based check must catch the base class when the body
    indicates a transient condition."""
    anthropic = pytest.importorskip("anthropic")

    monkeypatch.setattr(llm, "STREAM_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(llm, "STREAM_RETRY_ATTEMPTS", 5)

    attempts = {"n": 0}

    class _BaseAPIStatusErrorCtx:
        def __init__(self, payload: str):
            self._payload = payload

        def __enter__(self):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                # Raise the BASE class directly — this is what the SDK does
                # for in-stream SSE error events that don't map to a typed
                # subclass.
                raise _make_apistatus_error(
                    anthropic,
                    status_code=529,
                    body={
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Overloaded"},
                    },
                    message="Overloaded",
                )
            return self

        def __exit__(self, *_a):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self._payload)],
                usage=FakeUsage(),
            )

    class _OverloadedMessages:
        def stream(self, **kwargs):
            return _BaseAPIStatusErrorCtx(json.dumps(_DECISION_PAYLOAD))

    res = llm.structured_call(_call(), client_factory=lambda: _fake_client(_OverloadedMessages()))
    assert attempts["n"] == 3, "should retry past 2 base-APIStatusError overloads and succeed on the 3rd"
    assert res.payload["stage"] == "screen"


def test_structured_call_does_not_retry_base_apistatuserror_4xx(tmp_state, monkeypatch):
    """Sister regression: a base APIStatusError without a transient body
    (e.g. 400 bad-request surfaced via the base class) must NOT trigger
    retries — those are request bugs, not outages."""
    anthropic = pytest.importorskip("anthropic")

    monkeypatch.setattr(llm, "STREAM_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(llm, "STREAM_RETRY_ATTEMPTS", 5)

    attempts = {"n": 0}

    class _BadRequestCtx:
        def __enter__(self):
            attempts["n"] += 1
            raise _make_apistatus_error(
                anthropic,
                status_code=400,
                body={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "bad schema"},
                },
                message="invalid_request",
            )

        def __exit__(self, *_a):
            return False

    class _BadMessages:
        def stream(self, **kwargs):
            return _BadRequestCtx()

    with pytest.raises(anthropic.APIStatusError):
        llm.structured_call(_call(), client_factory=lambda: _fake_client(_BadMessages()))
    assert attempts["n"] == 1, "4xx-style request bugs must not be retried"


def test_is_transient_anthropic_error_predicate():
    """Direct unit test for the predicate to lock down the exact body
    shapes and status codes that count as transient."""
    anthropic = pytest.importorskip("anthropic")

    # In-stream overloaded SSE event (the May 12 2026 production case).
    overloaded = _make_apistatus_error(
        anthropic,
        status_code=529,
        body={"type": "error", "error": {"type": "overloaded_error"}},
        message="Overloaded",
    )
    assert llm._is_transient_anthropic_error(overloaded)

    # Bad-request body — must NOT be transient.
    bad = _make_apistatus_error(
        anthropic,
        status_code=400,
        body={"type": "error", "error": {"type": "invalid_request_error"}},
        message="bad",
    )
    assert not llm._is_transient_anthropic_error(bad)

    # 500-class status with an unfamiliar body still counts as transient
    # via the status-code fallback.
    server_500 = _make_apistatus_error(
        anthropic,
        status_code=503,
        body={"some": "other shape"},
        message="service unavailable",
    )
    assert llm._is_transient_anthropic_error(server_500)

    # Non-anthropic exception — must be False (not True).
    assert not llm._is_transient_anthropic_error(ValueError("nope"))


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
    # Internal $ref now inlined (Anthropic rejects $defs alongside anyOf/etc.)
    assert out["properties"]["ref"] == {"type": "string"}
    assert "$defs" not in out
    assert out["properties"]["fmt"]["format"] == "date-time"
    assert out["additionalProperties"] is False
    assert out["required"] == ["kind"]


def test_sanitize_recurses_into_anyof_oneof_allof():
    """Recursion is applied even when oneOf is rewritten to anyOf."""
    schema = {
        "oneOf": [
            {"type": "number", "minimum": 0},
            {"type": "string", "minLength": 1},
        ],
        "allOf": [{"properties": {"n": {"type": "integer", "maximum": 10}}}],
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "oneOf" not in out
    assert out["anyOf"][0] == {"type": "number"}
    assert out["anyOf"][1] == {"type": "string"}
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


def test_internal_ref_resolved_when_defs_present():
    """Internal #/$defs/foo refs get inlined; the $defs block is stripped."""
    schema = {"properties": {"x": {"$ref": "#/$defs/x"}}, "$defs": {"x": {"type": "string"}}}
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["properties"]["x"] == {"type": "string"}
    assert "$defs" not in out


def test_internal_ref_left_alone_when_target_missing():
    """An internal $ref pointing nowhere stays as-is — we don't silently
    corrupt the schema by dropping it. Anthropic will reject, but at least
    the failure mode is obvious."""
    schema = {"properties": {"x": {"$ref": "#/$defs/missing"}}}
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["properties"]["x"] == {"$ref": "#/$defs/missing"}


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


def test_oneof_converted_to_anyof():
    """Anthropic structured outputs supports anyOf/allOf but NOT oneOf.
    Discriminated unions get rewritten to anyOf — local validation keeps oneOf."""
    schema = {
        "oneOf": [
            {"type": "object", "properties": {"kind": {"const": "etf"}}},
            {"type": "object", "properties": {"kind": {"const": "option"}}},
        ]
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "oneOf" not in out
    assert "anyOf" in out
    assert len(out["anyOf"]) == 2
    assert out["anyOf"][0]["properties"]["kind"] == {"const": "etf"}


def test_oneof_inside_array_items_converted():
    schema = {
        "type": "array",
        "items": {
            "oneOf": [
                {"$ref": "#/$defs/etf"},
                {"$ref": "#/$defs/option"},
            ]
        },
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "anyOf" in out["items"]
    assert "oneOf" not in out["items"]


def test_internal_ref_inlined_and_defs_stripped():
    """#/$defs/foo refs get fully inlined; the $defs block is dropped."""
    schema = {
        "anyOf": [{"$ref": "#/$defs/etf"}, {"$ref": "#/$defs/option"}],
        "$defs": {
            "etf":    {"type": "object", "properties": {"kind": {"const": "etf"}}},
            "option": {"type": "object", "properties": {"kind": {"const": "option"}}},
        },
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "$defs" not in out
    assert out["anyOf"][0]["properties"]["kind"] == {"const": "etf"}
    assert out["anyOf"][1]["properties"]["kind"] == {"const": "option"}
    # No $ref values anywhere
    def has_ref(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return True
            return any(has_ref(v) for v in node.values())
        if isinstance(node, list):
            return any(has_ref(v) for v in node)
        return False
    assert not has_ref(out)


def test_internal_ref_resolves_transitively():
    """A $defs entry that itself uses $ref to another $defs entry inlines through."""
    schema = {
        "properties": {"a": {"$ref": "#/$defs/etf"}},
        "$defs": {
            "etf": {
                "type": "object",
                "properties": {"kill": {"$ref": "#/$defs/killConditions"}},
            },
            "killConditions": {"type": "object", "properties": {"max": {"type": "number"}}},
        },
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert "$defs" not in out
    assert out["properties"]["a"]["properties"]["kill"]["properties"]["max"] == {"type": "number"}


def test_internal_ref_resolves_forward_in_tree_order():
    """A ref appearing before its $defs entry in document order still resolves."""
    schema = {
        "anyOf": [{"$ref": "#/$defs/x"}],
        "$defs": {"x": {"type": "string"}},
    }
    out = llm.sanitize_schema_for_structured_output(schema)
    assert out["anyOf"][0] == {"type": "string"}


def test_real_portfolio_schema_fully_inlined():
    """The actual portfolio + position schemas must, after sanitisation,
    contain NO external $refs, NO internal $refs, NO $defs, NO oneOf
    anywhere — the structured-outputs-acceptable subset."""
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

    def find_violations(node, found):
        if isinstance(node, dict):
            if "$ref" in node:
                found.append(("ref", node["$ref"]))
            if "$defs" in node:
                found.append(("$defs", "present"))
            if "oneOf" in node:
                found.append(("oneOf", "present"))
            for k in llm._STRUCTURED_OUTPUT_UNSUPPORTED_KEYS:
                if k in node:
                    found.append((k, "present"))
            for v in node.values():
                find_violations(v, found)
        elif isinstance(node, list):
            for v in node:
                find_violations(v, found)
        return found

    assert find_violations(out, []) == []


# ---------- markdown fence stripping ----------


def test_strip_fences_with_json_lang_tag():
    assert llm.strip_markdown_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_without_lang_tag():
    assert llm.strip_markdown_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_no_op_when_no_fence():
    assert llm.strip_markdown_fences('{"a": 1}') == '{"a": 1}'
    assert llm.strip_markdown_fences('  {"a": 1}  ') == '{"a": 1}'


def test_structured_call_parses_fenced_response(tmp_state):
    """Even if the model wraps JSON in markdown fences, structured_call should
    parse it cleanly instead of triggering the schema-retry path."""
    fenced = '```json\n' + json.dumps(_DECISION_PAYLOAD) + '\n```'
    fm = FakeMessages([fenced])
    res = llm.structured_call(_call(), client_factory=lambda: _fake_client(fm))
    assert res.payload["stage"] == "screen"
    assert fm.calls == 1  # no retry needed
