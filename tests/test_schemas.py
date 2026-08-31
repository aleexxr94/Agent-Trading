"""Schema tests — validate fixtures against schemas/*.schema.json.

v2 covers: position, portfolio, signals, view, sanity, decision_log.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry()
    for schema_file in SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(schema_file.read_text())
        reg = reg.with_resource(schema["$id"], Resource.from_contents(schema))
    return reg


def _validator(schema_name: str, registry: Registry) -> Draft202012Validator:
    return Draft202012Validator(_load_schema(schema_name), registry=registry)


# ---------- fixture builders ----------


def _etf_position(**overrides) -> dict:
    base = {
        "kind": "etf",
        "symbol": "TQQQ",
        "shares": 5,
        "avg_cost": 70.0,
        "leverage_factor": 3.0,
        "entry_thesis": "Trend-following long Nasdaq into supportive macro window.",
        "kill_conditions": {
            "max_loss_pct": 25,
            "underlying_price_below": None,
            "underlying_price_above": None,
            "time_stop_utc": None,
            "notes": "",
        },
        "position_pct": 12.0,
    }
    base.update(overrides)
    return base


def _portfolio(positions: list[dict], **overrides) -> dict:
    base = {
        "run_id": "20260510T120000Z-abc123",
        "generated_at": "2026-05-10T12:00:00Z",
        "nav_usd": 2500.0,
        "cash_usd": 250.0,
        "cash_buffer_pct": 10.0,
        "all_cash": False,
        "all_cash_rationale": None,
        "positions": positions,
        "construction_rationale": "Diversified across 3 leverage families with one long-vol hedge.",
    }
    base.update(overrides)
    return base


# ---------- position.schema ----------


def test_etf_position_valid(registry):
    _validator("position.schema.json", registry).validate(_etf_position())


def test_inverse_etf_position_valid(registry):
    """Inverse ETFs are held long; the position stores the leverage
    magnitude (positive). BITI is 1x, the floor."""
    _validator("position.schema.json", registry).validate(
        _etf_position(symbol="SQQQ", leverage_factor=3.0)
    )
    _validator("position.schema.json", registry).validate(
        _etf_position(symbol="BITI", leverage_factor=1.0)
    )


def test_position_kind_rejects_option(registry):
    """Options are not a supported instrument class — kind:'option' (and
    any non-etf kind) must be rejected."""
    for kind in ("option", "future"):
        bad = _etf_position(kind=kind)
        with pytest.raises(Exception):
            _validator("position.schema.json", registry).validate(bad)


def test_position_pct_cap_25(registry):
    """Schema bounds position_pct at the 25% hold ceiling. The 15% entry/add
    cap is enforced at the sanity layer (entry_cap_on_adds), not the schema —
    so a held winner that drifted to 18–25% is a valid position object."""
    # Above the hold ceiling → rejected.
    bad = _etf_position(position_pct=25.01)
    with pytest.raises(Exception):
        _validator("position.schema.json", registry).validate(bad)
    # At/under the hold ceiling → valid (drifted winner representable).
    _validator("position.schema.json", registry).validate(_etf_position(position_pct=25.0))
    _validator("position.schema.json", registry).validate(_etf_position(position_pct=18.0))


def test_etf_rejects_option_only_field(registry):
    """additionalProperties:false rejects any option-only field leaking in."""
    for field in ("strike", "expiry", "contracts", "premium_paid", "greeks", "underlying"):
        bad = _etf_position()
        bad[field] = 100
        with pytest.raises(Exception):
            _validator("position.schema.json", registry).validate(bad)


# ---------- portfolio.schema ----------


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8, 10, 12])
def test_portfolio_position_band_valid(registry, count):
    """1–12 positions allowed; concentration bounded by 15%/position."""
    positions = [_etf_position(symbol=f"AAA{i}", position_pct=5.0) for i in range(count)]
    _validator("portfolio.schema.json", registry).validate(_portfolio(positions))


@pytest.mark.parametrize("count", [0, 13])
def test_portfolio_position_band_rejects_out_of_range(registry, count):
    positions = [_etf_position(symbol=f"AAA{i}", position_pct=2.0) for i in range(count)]
    with pytest.raises(Exception):
        _validator("portfolio.schema.json", registry).validate(_portfolio(positions))


def test_portfolio_all_cash_valid(registry):
    p = _portfolio(
        [],
        all_cash=True,
        all_cash_rationale="Conviction below threshold; capital preservation > forcing 10 slots.",
    )
    _validator("portfolio.schema.json", registry).validate(p)


def test_portfolio_all_cash_requires_rationale(registry):
    p = _portfolio([], all_cash=True, all_cash_rationale=None)
    with pytest.raises(Exception):
        _validator("portfolio.schema.json", registry).validate(p)


def test_portfolio_all_cash_rejects_positions(registry):
    p = _portfolio(
        [_etf_position()] * 8,
        all_cash=True,
        all_cash_rationale="should not coexist with positions",
    )
    with pytest.raises(Exception):
        _validator("portfolio.schema.json", registry).validate(p)


def test_portfolio_all_etf_positions(registry):
    positions = [_etf_position(symbol=f"E{i}") for i in range(9)]
    _validator("portfolio.schema.json", registry).validate(_portfolio(positions))


def test_portfolio_rejects_option_position(registry):
    """A portfolio carrying an option-shaped position must fail validation."""
    bad_option = {
        "kind": "option", "underlying": "SPY", "type": "call", "strike": 530.0,
        "expiry": "2026-06-19", "dte": 40, "contracts": 1, "premium_paid": 6.5,
        "entry_thesis": "x" * 40, "position_pct": 5.0,
        "kill_conditions": {"max_loss_pct": 100},
    }
    with pytest.raises(Exception):
        _validator("portfolio.schema.json", registry).validate(
            _portfolio([_etf_position(), bad_option])
        )


# ---------- signals.schema (v2) ----------


def _signals_row(**overrides) -> dict:
    base = {
        "symbol": "TQQQ",
        "kind": "etf",
        "factor": "nasdaq",
        "leverage_factor": 3.0,
        "family": "Nasdaq 3x long",
        "last_close": 72.45,
        "adv_30d": 85_000_000,
        "momentum_30d_pct": 8.4,
        "momentum_60d_pct": 12.1,
        "hv_30d_annualised": 0.42,
        "hv_90d_annualised": 0.38,
        "dist_from_50d_ma_pct": 4.2,
        "dist_from_200d_ma_pct": 15.7,
    }
    base.update(overrides)
    return base


def _signals(rows: list[dict] | None = None) -> dict:
    if rows is None:
        rows = [_signals_row()]
    return {
        "run_id": "rid",
        "generated_at": "2026-05-13T14:00:00Z",
        "tickers": rows,
    }


def test_signals_valid(registry):
    _validator("signals.schema.json", registry).validate(_signals())


def test_signals_accepts_null_metrics(registry):
    """yfinance failures leave numeric fields as null; the schema must
    accept that (the row also carries an error string so downstream
    stages can decide whether to skip)."""
    row = _signals_row(
        last_close=None, adv_30d=None,
        momentum_30d_pct=None, momentum_60d_pct=None,
        hv_30d_annualised=None, hv_90d_annualised=None,
        dist_from_50d_ma_pct=None, dist_from_200d_ma_pct=None,
        error="yfinance: 404 not found",
    )
    _validator("signals.schema.json", registry).validate(_signals([row]))


def test_signals_rejects_unknown_kind(registry):
    bad = _signals([_signals_row(kind="future")])
    with pytest.raises(Exception):
        _validator("signals.schema.json", registry).validate(bad)


# ---------- view.schema (v2) ----------


def _view_candidate(**overrides) -> dict:
    base = {
        "symbol": "TQQQ",
        "instrument_kind": "etf",
        "thesis": "Strong momentum_30d_pct=8.4 confirms uptrend.",
        "confidence": 0.7,
    }
    base.update(overrides)
    return base


def _view(candidates: list[dict] | None = None, **overrides) -> dict:
    base = {
        "run_id": "rid",
        "generated_at": "2026-05-13T14:00:30Z",
        "regime": "trending_up",
        "regime_rationale": "Broad equity uptrend across multiple factors.",
        "candidates": candidates if candidates is not None else [_view_candidate()],
    }
    base.update(overrides)
    return base


def test_view_valid(registry):
    _validator("view.schema.json", registry).validate(_view())


def test_view_zero_candidates_allowed(registry):
    """Flash-crash regime: strategist may return empty candidate list
    with regime_rationale explaining the abstain."""
    v = _view(candidates=[], regime="vol_elevated",
              regime_rationale="UVXY +40% in session; abstain.")
    _validator("view.schema.json", registry).validate(v)


def test_view_rejects_more_than_6_candidates(registry):
    v = _view(candidates=[_view_candidate(symbol=f"X{i}") for i in range(7)])
    with pytest.raises(Exception):
        _validator("view.schema.json", registry).validate(v)


def test_view_rejects_unknown_regime(registry):
    v = _view(regime="bull_run_2.0")
    with pytest.raises(Exception):
        _validator("view.schema.json", registry).validate(v)


def test_view_rejects_unknown_instrument_kind(registry):
    v = _view(candidates=[_view_candidate(instrument_kind="future")])
    with pytest.raises(Exception):
        _validator("view.schema.json", registry).validate(v)


def test_view_rejects_option_instrument_kinds(registry):
    """option_call / option_put are no longer valid instrument kinds —
    the strategist can only propose ETFs."""
    for kind in ("option_call", "option_put"):
        v = _view(candidates=[_view_candidate(instrument_kind=kind)])
        with pytest.raises(Exception):
            _validator("view.schema.json", registry).validate(v)


def test_view_confidence_in_range(registry):
    v = _view(candidates=[_view_candidate(confidence=1.5)])
    with pytest.raises(Exception):
        _validator("view.schema.json", registry).validate(v)


# ---------- decision_log.schema ----------


def _decision():
    return {
        "run_id": "rid",
        "stage": "signals",
        "model": "local-deterministic",
        "inputs_hash": "deadbeefcafebabe",
        "output_ref": "signals.json",
        "prompt_cache_hit_pct": 0.0,
        "cost_usd": 0.0,
        "started_at": "2026-05-13T14:00:00Z",
        "ended_at": "2026-05-13T14:00:05Z",
        "status": "ok",
        "risk_warning": "PAPER TRADING — leveraged & inverse ETFs are high-risk.",
    }


def test_decision_log_valid(registry):
    _validator("decision_log.schema.json", registry).validate(_decision())


def test_decision_log_accepts_v2_stages(registry):
    for stage in ("market_gate", "signals", "strategist", "construct", "execute", "monitor"):
        d = _decision()
        d["stage"] = stage
        _validator("decision_log.schema.json", registry).validate(d)


def test_decision_log_accepts_skipped_market_closed(registry):
    """Market-gate stage logs `skipped_market_closed` when Alpaca clock
    reports the market is closed — distinct from the generic `skipped`."""
    d = _decision()
    d["stage"] = "market_gate"
    d["status"] = "skipped_market_closed"
    _validator("decision_log.schema.json", registry).validate(d)


def test_decision_log_accepts_pipeline_crash_row(registry):
    """The crash handler writes a synthetic stage="pipeline" row with
    status="error" (or "aborted" for cost-cap stops) and a short error
    string — the §Promotion failure gate counts "error" rows."""
    d = _decision()
    d["stage"] = "pipeline"
    d["status"] = "error"
    d["error"] = "RuntimeError: boom"
    _validator("decision_log.schema.json", registry).validate(d)
    d["status"] = "aborted"
    _validator("decision_log.schema.json", registry).validate(d)


def test_decision_log_rejects_unknown_status(registry):
    d = _decision()
    d["status"] = "exploded"
    with pytest.raises(Exception):
        _validator("decision_log.schema.json", registry).validate(d)


def test_decision_log_rejects_v1_stage(registry):
    """v1 stages (screen, research, chains, scenarios) and the removed
    chain_lookup stage are gone from the enum — no decision rows with
    these stage values can be written."""
    for stage in ("screen", "research", "chains", "scenarios", "chain_lookup"):
        d = _decision()
        d["stage"] = stage
        with pytest.raises(Exception):
            _validator("decision_log.schema.json", registry).validate(d)


def test_decision_log_requires_risk_warning(registry):
    bad = _decision()
    del bad["risk_warning"]
    with pytest.raises(Exception):
        _validator("decision_log.schema.json", registry).validate(bad)
