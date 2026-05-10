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

    def create(self, **kwargs):
        i = self.calls
        self.calls += 1
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
    "model": "claude-haiku-4-5-20251001",
    "inputs_hash": "deadbeef" * 2,
    "output_ref": "screen.json",
    "prompt_cache_hit_pct": 80.0,
    "cost_usd": 0.04,
    "started_at": "2026-05-10T12:00:00Z",
    "ended_at": "2026-05-10T12:00:05Z",
    "status": "ok",
    "risk_warning": "PAPER TRADING.",
}


def test_structured_call_happy_path(tmp_state):
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)],
                       [FakeUsage(input_tokens=1000, output_tokens=200)])
    res = llm.structured_call(
        run_id="rid",
        stage="screen",
        model="claude-haiku-4-5-20251001",
        system_blocks=[{"type": "text", "text": "you are an agent"}],
        user_messages=[{"role": "user", "content": "do work"}],
        schema_filename="decision_log.schema.json",
        client_factory=lambda: _fake_client(fm),
    )
    assert res.payload["stage"] == "screen"
    assert res.cost_usd > 0
    rows = state.read_costs_for_run("rid")
    assert len(rows) == 1


def test_schema_retry_then_success(tmp_state):
    bad = json.dumps({"run_id": "rid", "stage": "magic"})
    good = json.dumps(_DECISION_PAYLOAD)
    fm = FakeMessages([bad, good])
    res = llm.structured_call(
        run_id="rid",
        stage="screen",
        model="claude-haiku-4-5-20251001",
        system_blocks=[{"type": "text", "text": "x"}],
        user_messages=[{"role": "user", "content": "y"}],
        schema_filename="decision_log.schema.json",
        client_factory=lambda: _fake_client(fm),
    )
    assert res.payload["stage"] == "screen"
    assert fm.calls == 2


def test_schema_retry_failure_raises(tmp_state):
    bad = json.dumps({"run_id": "rid", "stage": "magic"})
    fm = FakeMessages([bad, bad])
    with pytest.raises(llm.SchemaRetryFailed):
        llm.structured_call(
            run_id="rid",
            stage="screen",
            model="claude-haiku-4-5-20251001",
            system_blocks=[{"type": "text", "text": "x"}],
            user_messages=[{"role": "user", "content": "y"}],
            schema_filename="decision_log.schema.json",
            client_factory=lambda: _fake_client(fm),
        )


def test_per_run_cap_aborts_before_call(tmp_state, monkeypatch):
    monkeypatch.setenv("PER_RUN_COST_CAP_USD", "1.00")
    state.append_cost({
        "run_id": "rid", "stage": "prev", "model": "m",
        "cost_usd": 1.50, "at": state.utcnow_iso(),
    })
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)])
    with pytest.raises(llm.CostCapExceeded) as ei:
        llm.structured_call(
            run_id="rid",
            stage="screen",
            model="claude-haiku-4-5-20251001",
            system_blocks=[{"type": "text", "text": "x"}],
            user_messages=[{"role": "user", "content": "y"}],
            schema_filename="decision_log.schema.json",
            client_factory=lambda: _fake_client(fm),
        )
    assert "per-run" in str(ei.value)
    assert fm.calls == 0  # never invoked


def test_daily_cap_aborts_across_runs(tmp_state, monkeypatch):
    monkeypatch.setenv("DAILY_COST_CAP_USD", "5.00")
    state.append_cost({
        "run_id": "earlier", "stage": "x", "model": "m",
        "cost_usd": 5.50, "at": state.utcnow_iso(),
    })
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)])
    with pytest.raises(llm.CostCapExceeded) as ei:
        llm.structured_call(
            run_id="rid",
            stage="screen",
            model="claude-haiku-4-5-20251001",
            system_blocks=[{"type": "text", "text": "x"}],
            user_messages=[{"role": "user", "content": "y"}],
            schema_filename="decision_log.schema.json",
            client_factory=lambda: _fake_client(fm),
        )
    assert "daily" in str(ei.value)


def test_halt_flag_blocks_call(tmp_state):
    state.set_halt("test")
    fm = FakeMessages([json.dumps(_DECISION_PAYLOAD)])
    with pytest.raises(llm.HaltFlagSet):
        llm.structured_call(
            run_id="rid",
            stage="screen",
            model="claude-haiku-4-5-20251001",
            system_blocks=[{"type": "text", "text": "x"}],
            user_messages=[{"role": "user", "content": "y"}],
            schema_filename="decision_log.schema.json",
            client_factory=lambda: _fake_client(fm),
        )


def test_cost_estimate_uses_pricing_table():
    usage = llm.CallUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    assert llm.estimate_cost_usd("claude-sonnet-4-6", usage) == pytest.approx(3.00)
    assert llm.estimate_cost_usd("claude-haiku-4-5-20251001", usage) == pytest.approx(1.00)
