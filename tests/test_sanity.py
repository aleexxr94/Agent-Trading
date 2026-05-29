"""Tests for lib/sanity.py — deterministic post-construct rules (ETF-only).

Each rule has its own focused test pair (passes on good input, fires on
the targeted failure mode). The full-run integration test confirms the
rules compose into a single sanity.json with the worst-status rollup.
"""
from __future__ import annotations

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
            "Default fixture rationale: balanced exposure across leveraged and "
            "inverse ETFs within the spec's 1-12 position band and 15% "
            "per-position cap."
        ),
    }


def _view_for(symbols_confidences: dict[str, float]) -> dict:
    """Build a minimal v2 strategist view payload from {symbol: confidence}.
    All candidates are ETFs (the only instrument_kind)."""
    return {
        "run_id": "test",
        "generated_at": "2026-05-13T00:00:00Z",
        "regime": "trending_up",
        "regime_rationale": "fixture",
        "candidates": [
            {
                "symbol": sym,
                "instrument_kind": "etf",
                "thesis": "fixture thesis citing signals",
                "confidence": conf,
            }
            for sym, conf in symbols_confidences.items()
        ],
    }


# ---- per-underlying concentration ----


def test_per_underlying_warn_fires_above_20():
    # Two TQQQ legs at 13% + 14% = 27% on one ticker.
    p = _portfolio([
        _etf("TQQQ", position_pct=13.0),
        _etf("TQQQ", position_pct=14.0),
    ])
    r = sanity._r_per_underlying_pct_cap_20(p, None)
    assert r.status == "warn"
    assert r.severity == "warn"
    assert "TQQQ" in r.detail


def test_per_underlying_passes_when_split_across_tickers():
    p = _portfolio([
        _etf("TQQQ", position_pct=14.0),
        _etf("SOXL", position_pct=14.0),
    ])
    r = sanity._r_per_underlying_pct_cap_20(p, None)
    assert r.status == "pass"


def test_per_underlying_skips_on_all_cash():
    p = _portfolio([])
    r = sanity._r_per_underlying_pct_cap_20(p, None)
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
        _etf("SOXL", position_pct=10.0, kill_conditions=_kc_complete(
            underlying_price_below=None, time_stop_utc="2026-06-15T00:00:00Z",
        )),
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
        _etf("SOXL", position_pct=10.0),
    ])
    v = _view_for({"TQQQ": 0.7, "SOXL": 0.6})
    r = sanity._r_position_backed_by_strategist(p, v)
    assert r.status == "pass"


def test_position_backed_skip_when_no_view_payload():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    r = sanity._r_position_backed_by_strategist(p, None)
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
    # SOXL has incomplete kill_conditions → kill_conditions_complete fails;
    # two TQQQ legs at 13%+14% → per-underlying warn. Strategist endorses
    # both; rationale long → meaningful passes. Worst status = fail.
    p = _portfolio([
        _etf("TQQQ", position_pct=13.0),
        _etf("TQQQ", position_pct=14.0),
        _etf("SOXL", position_pct=10.0, kill_conditions={"max_loss_pct": 25.0}),
    ])
    v = _view_for({"TQQQ": 0.95, "SOXL": 0.95})
    report = sanity.run_sanity_checks(p, v)
    assert report["status"] == "fail"
    assert report["summary"]["fail"] >= 1
    assert report["summary"]["warn"] >= 1


def test_overall_status_warn_when_no_fails():
    # Single warn (per-underlying 27% on TQQQ), everything else passes.
    p = _portfolio([
        _etf("TQQQ", position_pct=13.0),
        _etf("TQQQ", position_pct=14.0),
    ])
    v = _view_for({"TQQQ": 0.95})  # high enough that size-vs-confidence passes
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
    # Most rules skip on zero positions; only the rationale-length rule runs.
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


# ---- v2 winrate rules: confidence-weighted sizing ----


def test_size_matches_confidence_warns_when_oversized_for_confidence():
    """Position sized larger than confidence × 15."""
    p = _portfolio([_etf("TQQQ", position_pct=12.0)])
    v = _view_for({"TQQQ": 0.6})  # ceiling = 9%, actual = 12%
    r = sanity._r_position_size_matches_confidence(p, v)
    assert r.status == "warn"
    assert r.meta["offenders"][0]["symbol"] == "TQQQ"


def test_size_matches_confidence_passes_when_sized_under_ceiling():
    p = _portfolio([_etf("TQQQ", position_pct=8.0)])
    v = _view_for({"TQQQ": 0.6})
    r = sanity._r_position_size_matches_confidence(p, v)
    assert r.status == "pass"


# ---- v2 winrate rules: adaptive cap ----


def test_adaptive_cap_skips_when_no_drawdown():
    p = _portfolio([_etf("TQQQ", position_pct=14.0)])
    p["_adaptive_cap_pct"] = 15.0
    r = sanity._r_position_within_adaptive_cap(p, None)
    assert r.status == "skip"


def test_adaptive_cap_fails_when_position_exceeds_dd_cap():
    """In drawdown the cap is 7.5%; a 12% position should fail."""
    p = _portfolio([_etf("TQQQ", position_pct=12.0)])
    p["_adaptive_cap_pct"] = 7.5
    r = sanity._r_position_within_adaptive_cap(p, None)
    assert r.status == "fail"


def test_adaptive_cap_passes_when_position_within_dd_cap():
    p = _portfolio([_etf("TQQQ", position_pct=7.0)])
    p["_adaptive_cap_pct"] = 7.5
    r = sanity._r_position_within_adaptive_cap(p, None)
    assert r.status == "pass"


# ---- v2 winrate rules: notional floor ----


def test_notional_floor_warns_when_below_50_usd():
    """1% NAV × $2500 = $25 → below $50 floor."""
    p = _portfolio([_etf("TQQQ", position_pct=1.0)])
    p["_nav_usd"] = 2500.0
    r = sanity._r_position_notional_above_floor(p, None)
    assert r.status == "warn"
    assert r.meta["offenders"][0]["notional_usd"] < 50.0


def test_notional_floor_passes_when_above_50_usd():
    p = _portfolio([_etf("TQQQ", position_pct=5.0)])
    p["_nav_usd"] = 2500.0
    r = sanity._r_position_notional_above_floor(p, None)
    assert r.status == "pass"


# ---- v2 winrate rules: ADV liquidity ----


def test_adv_liquidity_warns_when_notional_exceeds_1pct_of_adv():
    """Position notional > 1% of ticker's dollar ADV."""
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    p["_nav_usd"] = 2500.0  # → $250 notional
    p["_signals_adv"] = {"TQQQ": 20000.0}  # 1% = $200, notional = $250 → fires
    r = sanity._r_position_adv_liquidity(p, None)
    assert r.status == "warn"


def test_adv_liquidity_passes_when_notional_within_1pct_of_adv():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    p["_nav_usd"] = 2500.0  # → $250 notional
    p["_signals_adv"] = {"TQQQ": 100_000.0}  # 1% = $1000 — plenty
    r = sanity._r_position_adv_liquidity(p, None)
    assert r.status == "pass"


def test_adv_liquidity_skips_when_no_signals():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    p["_nav_usd"] = 2500.0
    # _signals_adv NOT set
    r = sanity._r_position_adv_liquidity(p, None)
    assert r.status == "skip"


# ---- run_sanity_checks injection of extra context ----


def test_run_sanity_checks_injects_extra_context_into_rules():
    """nav_usd + adaptive_cap_pct + signals → flow through to the rules
    that consume them."""
    p = _portfolio([_etf("TQQQ", position_pct=12.0)])
    v = _view_for({"TQQQ": 0.7})
    signals = {"tickers": [{"symbol": "TQQQ", "adv_30d": 1_000_000, "last_close": 70.0}]}
    report = sanity.run_sanity_checks(
        p, v,
        signals=signals,
        nav_usd=2500.0,
        adaptive_cap_pct=7.5,
    )
    # adaptive_cap rule should fail (12% > 7.5%); notional rule should
    # pass (12% × $2500 = $300 > $50).
    rules_by_name = {r["name"]: r for r in report["rules"]}
    assert rules_by_name["position_within_adaptive_cap"]["status"] == "fail"
    assert rules_by_name["position_notional_above_floor"]["status"] == "pass"


# ---- reentry cooldown ----


def _with_cooldown(portfolio: dict, cooldown: dict) -> dict:
    """Inject the _cooldown_symbols key the way run_sanity_checks does so the
    rule can be exercised directly."""
    enriched = dict(portfolio)
    enriched["_cooldown_symbols"] = cooldown
    return enriched


def test_reentry_cooldown_skip_when_no_cooldown_symbols():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    v = _view_for({"TQQQ": 0.7})
    r = sanity._r_reentry_cooldown(_with_cooldown(p, {}), v)
    assert r.status == "skip"


def test_reentry_cooldown_warns_on_reentry_below_override_confidence():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    v = _view_for({"TQQQ": 0.7})  # endorsed, but ≤ 0.8 override floor
    cooldown = {"TQQQ": "2026-05-26T15:00:00Z"}
    r = sanity._r_reentry_cooldown(_with_cooldown(p, cooldown), v)
    assert r.status == "warn"
    assert r.meta["offenders"][0]["symbol"] == "TQQQ"


def test_reentry_cooldown_passes_with_high_confidence_override():
    p = _portfolio([_etf("TQQQ", position_pct=10.0)])
    v = _view_for({"TQQQ": 0.85})  # > 0.8 override floor
    cooldown = {"TQQQ": "2026-05-26T15:00:00Z"}
    r = sanity._r_reentry_cooldown(_with_cooldown(p, cooldown), v)
    assert r.status == "pass"
    assert r.meta["overrides"][0]["symbol"] == "TQQQ"
    assert r.meta["overrides"][0]["confidence"] == 0.85


def test_reentry_cooldown_ignores_symbols_not_in_cooldown():
    """An unrelated held name must not be blocked by another name's cooldown."""
    p = _portfolio([_etf("SOXL", position_pct=10.0)])
    v = _view_for({"SOXL": 0.6})
    cooldown = {"TQQQ": "2026-05-26T15:00:00Z"}  # different symbol
    r = sanity._r_reentry_cooldown(_with_cooldown(p, cooldown), v)
    assert r.status == "pass"
    assert not r.meta.get("offenders")


def test_reentry_cooldown_matches_inverse_etf_by_symbol():
    """Inverse ETFs are matched against the cooldown map by their plain
    symbol, the same as any other ETF."""
    p = _portfolio([_etf("SQQQ", position_pct=10.0)])
    v = _view_for({"SQQQ": 0.65})  # ≤ 0.8 override floor
    r = sanity._r_reentry_cooldown(
        _with_cooldown(p, {"SQQQ": "2026-05-26T15:00:00Z"}), v
    )
    assert r.status == "warn"
    assert r.meta["offenders"][0]["symbol"] == "SQQQ"


def test_reentry_cooldown_skip_on_all_cash():
    p = _portfolio([])
    p["all_cash"] = True
    r = sanity._r_reentry_cooldown(_with_cooldown(p, {"TQQQ": "2026-05-26T15:00:00Z"}), None)
    assert r.status == "skip"
