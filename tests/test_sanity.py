"""Tests for lib/sanity.py — deterministic post-construct rules.

Each rule has its own focused test pair (passes on good input, fires on
the targeted failure mode). The full-run integration test confirms the
rules compose into a single sanity.json with the worst-status rollup.
"""
from __future__ import annotations

import os

import pytest

from lib import sanity


# ---- minimal portfolio builders ----


def _kc_complete(**overrides) -> dict:
    """Kill-conditions block that satisfies the kill_conditions_complete
    rule: max_loss_pct in (0, 100] AND ≥1 of the stop fields set."""
    base = {
        "max_loss_pct": 25.0,
        "underlying_price_below": 50.0,
        "underlying_price_above": None,
        "iv_collapse_pct": None,
        "time_stop_utc": None,
        "notes": "",
    }
    base.update(overrides)
    return base


def _etf(symbol: str, position_pct: float, **overrides) -> dict:
    base = {
        "kind": "etf",
        "symbol": symbol,
        "shares": 5,
        "avg_cost": 50.0,
        "leverage_factor": 3.0,
        "entry_thesis": "fixture",
        "kill_conditions": _kc_complete(),
        "position_pct": position_pct,
    }
    base.update(overrides)
    return base


def _option(
    underlying: str,
    *,
    type_: str = "call",
    position_pct: float = 10.0,
    iv_percentile: float = 30.0,
    premium_paid: float = 1.10,
    strike: float = 85.0,
    **overrides,
) -> dict:
    base = {
        "kind": "option",
        "underlying": underlying,
        "type": type_,
        "strike": strike,
        "expiry": "2026-06-19",
        "dte": 37,
        "contracts": 3,
        "premium_paid": premium_paid,
        "greeks": {
            "delta": 0.40 if type_ == "call" else -0.40,
            "gamma": 0.05,
            "theta": -0.05,
            "vega": 0.10,
            "iv": 0.30,
            "iv_percentile": iv_percentile,
        },
        "entry_thesis": "fixture",
        "kill_conditions": _kc_complete(),
        "position_pct": position_pct,
    }
    base.update(overrides)
    return base


def _portfolio(positions: list[dict], rationale: str | None = None) -> dict:
    return {
        "run_id": "test",
        "generated_at": "2026-05-13T00:00:00Z",
        "nav_usd": 2500.0,
        "cash_usd": 250.0,
        "cash_buffer_pct": 10.0,
        "all_cash": False,
        "all_cash_rationale": None,
        "positions": positions,
        "construction_rationale": rationale or (
            "Default fixture rationale: balanced exposure across leveraged "
            "ETFs and listed options within the spec's 1-12 position band "
            "and 15% per-position cap."
        ),
    }


def _view_for(symbols_confidences: dict[str, float], *, default_kind: str = "etf") -> dict:
    """Build a minimal v2 strategist view payload from
    {symbol: confidence}. v1's `_scenarios_for` analogue —
    expected_value_pct → confidence."""
    return {
        "run_id": "test",
        "generated_at": "2026-05-13T00:00:00Z",
        "regime": "trending_up",
        "regime_rationale": "fixture",
        "candidates": [
            {
                "symbol": sym,
                "instrument_kind": default_kind,
                "thesis": "fixture thesis citing signals",
                "confidence": conf,
            }
            for sym, conf in symbols_confidences.items()
        ],
    }


# ---- per-underlying concentration ----


def test_per_underlying_warn_fires_above_20():
    # TLT call + TLT put at 13% + 14% = 27% on one underlying.
    p = _portfolio([
        _option("TLT", type_="call", position_pct=13.0),
        _option("TLT", type_="put", position_pct=14.0),
    ])
    r = sanity._r_per_underlying_pct_cap_20(p, None)
    assert r.status == "warn"
    assert r.severity == "warn"
    assert "TLT" in r.detail


def test_per_underlying_passes_when_split_across_underlyings():
    p = _portfolio([
        _option("TLT", type_="call", position_pct=14.0),
        _option("SPY", type_="put", position_pct=14.0),
    ])
    r = sanity._r_per_underlying_pct_cap_20(p, None)
    assert r.status == "pass"


def test_per_underlying_skips_on_all_cash():
    p = _portfolio([])
    r = sanity._r_per_underlying_pct_cap_20(p, None)
    assert r.status == "skip"


# ---- straddle requires low IV ----


def test_straddle_fails_when_iv_percentile_above_threshold():
    # TLT long call + long put — the 2026-05-13 paper-run pattern.
    p = _portfolio([
        _option("TLT", type_="call", iv_percentile=65.0),
        _option("TLT", type_="put", iv_percentile=62.0),
    ])
    r = sanity._r_straddle_requires_low_iv(p, None)
    assert r.status == "fail"
    assert r.severity == "fail"
    assert "iv_percentile" in r.detail


def test_straddle_passes_when_iv_percentile_below_threshold():
    p = _portfolio([
        _option("TLT", type_="call", iv_percentile=22.0),
        _option("TLT", type_="put", iv_percentile=20.0),
    ])
    r = sanity._r_straddle_requires_low_iv(p, None)
    assert r.status == "pass"


def test_straddle_fails_when_iv_percentile_missing():
    p = _portfolio([
        _option("TLT", type_="call"),
        _option("TLT", type_="put"),
    ])
    # Drop iv_percentile to exercise the "missing" branch.
    del p["positions"][0]["greeks"]["iv_percentile"]
    r = sanity._r_straddle_requires_low_iv(p, None)
    assert r.status == "fail"
    assert any(o.get("issue") == "iv_percentile missing" for o in r.meta["offenders"])


def test_straddle_skip_when_no_pair():
    p = _portfolio([_option("TLT", type_="call")])
    r = sanity._r_straddle_requires_low_iv(p, None)
    assert r.status == "skip"


def test_straddle_skip_for_etf_only_portfolio():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    r = sanity._r_straddle_requires_low_iv(p, None)
    assert r.status == "skip"


# ---- kill conditions complete ----


def test_kill_conditions_fail_when_only_max_loss_set():
    p = _portfolio([
        _etf("TQQQ", position_pct=10.0, kill_conditions={"max_loss_pct": 25.0}),
    ])
    r = sanity._r_kill_conditions_complete(p, None)
    assert r.status == "fail"


def test_kill_conditions_fail_when_max_loss_zero_or_negative():
    p = _portfolio([
        _etf("TQQQ", position_pct=10.0, kill_conditions={
            "max_loss_pct": 0,
            "underlying_price_below": 40.0,
        }),
    ])
    r = sanity._r_kill_conditions_complete(p, None)
    assert r.status == "fail"


def test_kill_conditions_pass_with_price_or_time_stop():
    p = _portfolio([
        _etf("TQQQ", position_pct=10.0, kill_conditions=_kc_complete(underlying_price_below=40.0)),
        _option(
            "TLT", type_="call", position_pct=10.0,
            kill_conditions=_kc_complete(
                underlying_price_below=None, time_stop_utc="2026-06-15T00:00:00Z",
            ),
        ),
    ])
    r = sanity._r_kill_conditions_complete(p, None)
    assert r.status == "pass"


# ---- position-backed-by-strategist (v2 replacement for ev-positive) ----


def test_position_backed_fail_when_strategist_confidence_below_threshold():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    v = _view_for({"TQQQ": 0.3})
    r = sanity._r_position_backed_by_strategist(p, v)
    assert r.status == "fail"
    assert r.meta["offenders"][0]["issue"] == "strategist confidence < 0.5"


def test_position_backed_fail_when_no_strategist_entry():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    v = _view_for({"SOXL": 0.8})
    r = sanity._r_position_backed_by_strategist(p, v)
    assert r.status == "fail"
    assert r.meta["offenders"][0]["issue"] == "not in strategist candidates"


def test_position_backed_pass_when_all_endorsed():
    p = _portfolio([
        _etf("TQQQ", position_pct=10.0),
        _option("TLT", type_="call", position_pct=10.0),
    ])
    v = {
        "run_id": "test", "generated_at": "2026-05-13T00:00:00Z",
        "regime": "trending_up", "regime_rationale": "fixture",
        "candidates": [
            {"symbol": "TQQQ", "instrument_kind": "etf", "thesis": "x", "confidence": 0.7},
            {"symbol": "TLT", "instrument_kind": "option_call", "thesis": "x", "confidence": 0.6},
        ],
    }
    r = sanity._r_position_backed_by_strategist(p, v)
    assert r.status == "pass"


def test_position_backed_skip_when_no_view_payload():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    r = sanity._r_position_backed_by_strategist(p, None)
    assert r.status == "skip"


def test_position_backed_distinguishes_call_and_put():
    """A long call on SPY is NOT endorsed by an option_put SPY view
    candidate — the strategist's direction is opposite."""
    p = _portfolio([_option("SPY", type_="call", position_pct=10.0)])
    v = {
        "run_id": "test", "generated_at": "2026-05-13T00:00:00Z",
        "regime": "trending_down", "regime_rationale": "fixture",
        "candidates": [
            {"symbol": "SPY", "instrument_kind": "option_put", "thesis": "x", "confidence": 0.8},
        ],
    }
    r = sanity._r_position_backed_by_strategist(p, v)
    assert r.status == "fail"


# ---- option premium floor ----


def test_premium_warn_below_005():
    p = _portfolio([_option("TLT", type_="call", premium_paid=0.03)])
    r = sanity._r_option_premium_above_floor(p, None)
    assert r.status == "warn"


def test_premium_pass_above_005():
    p = _portfolio([_option("TLT", type_="call", premium_paid=0.50)])
    r = sanity._r_option_premium_above_floor(p, None)
    assert r.status == "pass"


def test_premium_skip_for_etf_only_portfolio():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    r = sanity._r_option_premium_above_floor(p, None)
    assert r.status == "skip"


# ---- construction rationale meaningful ----


def test_rationale_fail_when_too_short():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)], rationale="ok.")
    r = sanity._r_construction_rationale_meaningful(p, None)
    assert r.status == "fail"


def test_rationale_pass_at_threshold():
    text = "x" * 81
    p = _portfolio([_etf("TQQQ", position_pct=10.0)], rationale=text)
    r = sanity._r_construction_rationale_meaningful(p, None)
    assert r.status == "pass"


# ---- composition / overall status ----


def test_overall_status_is_worst_per_rule():
    # TLT call+put with iv=80 → straddle_requires_low_iv fails;
    # premium=0.03 → premium_warn; strategist endorses TLT calls+puts so
    # position_backed passes; rationale long so meaningful passes.
    p = _portfolio([
        _option("TLT", type_="call", iv_percentile=80.0, premium_paid=0.03),
        _option("TLT", type_="put", iv_percentile=80.0, premium_paid=0.04),
    ])
    v = {
        "run_id": "test", "generated_at": "2026-05-13T00:00:00Z",
        "regime": "vol_elevated", "regime_rationale": "fixture",
        "candidates": [
            {"symbol": "TLT", "instrument_kind": "option_call", "thesis": "x", "confidence": 0.6},
            {"symbol": "TLT", "instrument_kind": "option_put",  "thesis": "x", "confidence": 0.6},
        ],
    }
    report = sanity.run_sanity_checks(p, v)
    assert report["status"] == "fail"
    assert report["summary"]["fail"] >= 1
    assert report["summary"]["warn"] >= 1


def test_overall_status_warn_when_no_fails():
    # Single warn (per-underlying 27% on TLT), everything else passes.
    p = _portfolio([
        _option("TLT", type_="call", position_pct=13.0, iv_percentile=20.0),
        _option("TLT", type_="put",  position_pct=14.0, iv_percentile=20.0),
    ])
    v = {
        "run_id": "test", "generated_at": "2026-05-13T00:00:00Z",
        "regime": "vol_elevated", "regime_rationale": "fixture",
        "candidates": [
            {"symbol": "TLT", "instrument_kind": "option_call", "thesis": "x", "confidence": 0.6},
            {"symbol": "TLT", "instrument_kind": "option_put",  "thesis": "x", "confidence": 0.6},
        ],
    }
    report = sanity.run_sanity_checks(p, v)
    assert report["status"] == "warn"


def test_overall_status_pass_when_no_rules_fire():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    v = _view_for({"TQQQ": 0.7})
    report = sanity.run_sanity_checks(p, v)
    assert report["status"] == "pass"


def test_all_cash_portfolio_passes_sanity():
    """Empty portfolio (all-cash) is not a sanity violation — most rules
    skip cleanly. Confirms the overall rollup doesn't fall over on
    portfolios with zero positions."""
    p = _portfolio([])
    p["all_cash"] = True
    p["all_cash_rationale"] = "Conviction insufficient; capital preservation."
    p["positions"] = []
    report = sanity.run_sanity_checks(p, None)
    assert report["status"] == "pass"
    # construction_rationale meaningful + premium-floor + ev all skip on
    # zero positions; per-underlying skips on zero positions; only the
    # rationale-length rule still actually runs.
    assert report["summary"]["skip"] >= 4


# ---- block_on_fail env switch ----


def test_block_on_fail_defaults_to_false(monkeypatch):
    monkeypatch.delenv("SANITY_BLOCK_ON_FAIL", raising=False)
    assert sanity.block_on_fail_enabled() is False


@pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "YES"])
def test_block_on_fail_truthy_values_enable(monkeypatch, val):
    monkeypatch.setenv("SANITY_BLOCK_ON_FAIL", val)
    assert sanity.block_on_fail_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "", "anything-else"])
def test_block_on_fail_falsy_values_disable(monkeypatch, val):
    monkeypatch.setenv("SANITY_BLOCK_ON_FAIL", val)
    assert sanity.block_on_fail_enabled() is False
