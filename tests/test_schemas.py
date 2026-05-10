"""Schema tests — validate fixtures against schemas/*.schema.json.

Covers the 8–12 position band, all-cash empty case, the 15% per-position cap,
the option/ETF discriminator, scenario probability shape, and the decision-log
required fields.
"""
from __future__ import annotations

import copy
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
            "iv_collapse_pct": None,
            "time_stop_utc": None,
            "notes": "",
        },
        "position_pct": 12.0,
    }
    base.update(overrides)
    return base


def _option_position(**overrides) -> dict:
    base = {
        "kind": "option",
        "underlying": "SPY",
        "type": "call",
        "strike": 530.0,
        "expiry": "2026-06-19",
        "dte": 40,
        "contracts": 1,
        "premium_paid": 6.50,
        "greeks": {
            "delta": 0.45,
            "gamma": 0.02,
            "theta": -0.04,
            "vega": 0.18,
            "iv": 0.18,
            "iv_percentile": 35,
        },
        "entry_thesis": "Defined-risk long delta on broad-market trend continuation.",
        "kill_conditions": {
            "max_loss_pct": 100,
            "underlying_price_below": 510,
            "underlying_price_above": None,
            "iv_collapse_pct": 30,
            "time_stop_utc": None,
            "notes": "Premium-defined risk; max loss = 100% of premium.",
        },
        "position_pct": 5.0,
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


def test_option_position_valid(registry):
    _validator("position.schema.json", registry).validate(_option_position())


def test_position_kind_discriminator_rejects_unknown(registry):
    bad = _etf_position(kind="future")
    with pytest.raises(Exception):
        _validator("position.schema.json", registry).validate(bad)


def test_position_pct_cap_15(registry):
    bad = _etf_position(position_pct=15.01)
    with pytest.raises(Exception):
        _validator("position.schema.json", registry).validate(bad)


def test_etf_rejects_option_only_field(registry):
    bad = _etf_position()
    bad["strike"] = 100  # unexpected for ETF
    with pytest.raises(Exception):
        _validator("position.schema.json", registry).validate(bad)


def test_option_requires_greeks(registry):
    bad = _option_position()
    del bad["greeks"]
    with pytest.raises(Exception):
        _validator("position.schema.json", registry).validate(bad)


# ---------- portfolio.schema ----------


@pytest.mark.parametrize("count", [8, 10, 12])
def test_portfolio_position_band_valid(registry, count):
    positions = [_etf_position(symbol=f"AAA{i}", position_pct=5.0) for i in range(count)]
    _validator("portfolio.schema.json", registry).validate(_portfolio(positions))


@pytest.mark.parametrize("count", [0, 7, 13])
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


def test_portfolio_mixed_etf_and_option(registry):
    positions = [_etf_position(symbol=f"E{i}") for i in range(6)] + [
        _option_position(underlying=f"O{i}") for i in range(3)
    ]
    _validator("portfolio.schema.json", registry).validate(_portfolio(positions))


# ---------- research.schema ----------


def _research(candidate_overrides=None):
    side = {
        "thesis": "x",
        "key_drivers": ["macro tailwind"],
        "counterarguments": ["rate shock risk"],
        "confidence": 0.6,
    }
    candidate = {
        "symbol": "TQQQ",
        "instrument_kind": "etf",
        "bull": copy.deepcopy(side),
        "bear": {**copy.deepcopy(side), "confidence": 0.3, "thesis": "y"},
        "confidence_delta": 0.3,
        "abstain": False,
    }
    if candidate_overrides:
        candidate.update(candidate_overrides)
    return {
        "run_id": "rid",
        "generated_at": "2026-05-10T12:00:00Z",
        "candidates": [candidate],
    }


def test_research_valid(registry):
    _validator("research.schema.json", registry).validate(_research())


def test_research_requires_counterarguments(registry):
    bad = _research()
    bad["candidates"][0]["bull"]["counterarguments"] = []
    with pytest.raises(Exception):
        _validator("research.schema.json", registry).validate(bad)


# ---------- scenarios.schema ----------


def _scenarios(option=False):
    cases = [
        {"label": "base", "probability": 0.5, "expected_return_pct": 4.0, "narrative": "n"},
        {"label": "bull", "probability": 0.3, "expected_return_pct": 12.0, "narrative": "n"},
        {"label": "bear", "probability": 0.2, "expected_return_pct": -8.0, "narrative": "n"},
    ]
    candidate = {
        "symbol": "SPY" if option else "TQQQ",
        "instrument_kind": "option" if option else "etf",
        "horizon_days": 30,
        "cases": cases,
        "expected_value_pct": 2.5,
        "option_rationale": None,
    }
    if option:
        candidate["option_rationale"] = {
            "type": "call",
            "strike": 530.0,
            "expiry": "2026-06-19",
            "dte": 40,
            "dte_rationale": "Through next CPI + FOMC.",
            "strike_rationale": "ATM-ish for delta exposure with manageable theta.",
        }
    return {
        "run_id": "rid",
        "generated_at": "2026-05-10T12:00:00Z",
        "candidates": [candidate],
    }


def test_scenarios_etf_valid(registry):
    _validator("scenarios.schema.json", registry).validate(_scenarios())


def test_scenarios_option_requires_rationale(registry):
    bad = _scenarios(option=True)
    bad["candidates"][0]["option_rationale"] = None
    with pytest.raises(Exception):
        _validator("scenarios.schema.json", registry).validate(bad)


def test_scenarios_requires_three_cases(registry):
    bad = _scenarios()
    bad["candidates"][0]["cases"] = bad["candidates"][0]["cases"][:2]
    with pytest.raises(Exception):
        _validator("scenarios.schema.json", registry).validate(bad)


# ---------- decision_log.schema ----------


def _decision():
    return {
        "run_id": "rid",
        "stage": "screen",
        "model": "claude-haiku-4-5-20251001",
        "inputs_hash": "deadbeefcafebabe",
        "output_ref": "screen.json",
        "prompt_cache_hit_pct": 80.0,
        "cost_usd": 0.04,
        "started_at": "2026-05-10T12:00:00Z",
        "ended_at": "2026-05-10T12:00:05Z",
        "status": "ok",
        "risk_warning": "PAPER TRADING — leveraged ETFs and options are high-risk.",
    }


def test_decision_log_valid(registry):
    _validator("decision_log.schema.json", registry).validate(_decision())


def test_decision_log_rejects_unknown_stage(registry):
    bad = _decision()
    bad["stage"] = "magic"
    with pytest.raises(Exception):
        _validator("decision_log.schema.json", registry).validate(bad)


def test_decision_log_requires_risk_warning(registry):
    bad = _decision()
    del bad["risk_warning"]
    with pytest.raises(Exception):
        _validator("decision_log.schema.json", registry).validate(bad)
