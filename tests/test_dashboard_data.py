"""Pure-data-layer tests for the dashboard. No streamlit dependency required."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import dashboard_data as dd
from lib import state


FIXTURE = Path(__file__).parent / "fixtures" / "portfolio.json"


def test_load_portfolio_falls_back_to_fixture(tmp_state):
    p, src = dd.load_portfolio()
    assert src == "fixture"
    assert 1 <= len(p["positions"]) <= 12


def test_load_portfolio_prefers_live(tmp_state):
    state.write_json(state.CURRENT_PORTFOLIO, json.loads(FIXTURE.read_text()))
    p, src = dd.load_portfolio()
    assert src == "live"


def test_load_portfolio_seed_overrides_fixture(tmp_state):
    seed = state.STATE_DIR / "seed_portfolio.json"
    state.write_json(seed, json.loads(FIXTURE.read_text()))
    _, src = dd.load_portfolio()
    assert src == "seed"


# ---------- mark_key_for_position ----------


def test_mark_key_for_position_etf():
    assert dd.mark_key_for_position({"kind": "etf", "symbol": "TQQQ"}) == "TQQQ"


def test_mark_key_for_position_option():
    pos = {
        "kind": "option", "underlying": "SPY", "strike": 530.0,
        "expiry": "2026-06-19", "type": "call",
    }
    assert dd.mark_key_for_position(pos) == "SPY|530.0|2026-06-19|call"


# ---------- split_positions_by_broker_holdings ----------


def test_split_returns_all_open_when_held_keys_none():
    """held_keys=None signals broker unreachable; don't filter anything."""
    portfolio = json.loads(FIXTURE.read_text())
    open_, closed = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=None,
    )
    assert len(open_) == len(portfolio["positions"])
    assert closed == []


def test_split_treats_empty_held_keys_as_all_closed():
    """held_keys=set() = broker reachable, says zero positions → every
    portfolio entry is treated as closed."""
    portfolio = json.loads(FIXTURE.read_text())
    open_, closed = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=frozenset(),
    )
    assert open_ == []
    assert len(closed) == len(portfolio["positions"])


def test_split_partitions_etf_and_option_correctly():
    """Mixed portfolio: TQQQ + SPY 540C still open at broker; rest closed."""
    portfolio = json.loads(FIXTURE.read_text())
    held = frozenset({"TQQQ", "SPY|540.0|2026-06-19|call"})
    open_, closed = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=held,
    )
    assert len(open_) == 2
    assert {dd.mark_key_for_position(p) for p in open_} == set(held)
    # Everything else closed.
    assert len(closed) == len(portfolio["positions"]) - 2


# ---------- position_table_rows held_keys filter ----------


def test_position_table_rows_filters_stale_positions_when_broker_reachable(tmp_state):
    """Regression for May 12 2026: dashboard kept rendering a position
    after it closed on Alpaca because portfolio.json still listed it.
    Now: when held_keys is supplied, only those keys render."""
    portfolio = json.loads(FIXTURE.read_text())
    # Broker holds only TQQQ; everything else got manually closed.
    held = frozenset({"TQQQ"})
    rows = dd.position_table_rows(portfolio, held_keys=held)
    assert len(rows) == 1
    assert rows[0]["Symbol"] == "TQQQ"


def test_position_table_rows_renders_everything_when_held_keys_none(tmp_state):
    """held_keys=None = broker unreachable; don't blank the dashboard."""
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio, held_keys=None)
    assert len(rows) == len(portfolio["positions"])


def test_position_table_rows_empty_when_held_keys_empty(tmp_state):
    """held_keys=set() = broker reachable, zero positions → table empty."""
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio, held_keys=frozenset())
    assert rows == []


# ---------- BrokerView ----------


def test_broker_view_dataclass_shape():
    """BrokerView's held_keys must equal set(costs) so the dashboard's
    filter is consistent with the cost dict used for P&L."""
    view = dd.BrokerView(
        marks={"TQQQ": 80.0},
        costs={"TQQQ": 75.0, "SPY|540.0|2026-06-19|call": 8.10},
        held_keys=frozenset({"TQQQ", "SPY|540.0|2026-06-19|call"}),
        available=True,
        nav_usd=2575.5,
        captured_at="2026-05-14T13:35:00Z",
    )
    assert set(view.costs) == set(view.held_keys)
    assert view.available is True
    assert view.nav_usd == 2575.5
    assert view.captured_at == "2026-05-14T13:35:00Z"


def test_broker_view_defaults_nav_to_none_and_captured_at_to_empty():
    """Backwards-compat: existing call sites that didn't pass the new
    nav_usd / captured_at fields should still construct cleanly.
    Hero falls back to portfolio.json snapshot when nav_usd is None.
    """
    view = dd.BrokerView(
        marks={}, costs={}, held_keys=frozenset(), available=True,
    )
    assert view.nav_usd is None
    assert view.captured_at == ""


def test_try_load_broker_view_sets_available_false_when_get_positions_raises(
    tmp_state, monkeypatch
):
    """Codex P1 (PR #51 review): marks_from_broker and
    cost_basis_from_broker both swallow get_positions() exceptions and
    return {}, which is indistinguishable from a broker that legitimately
    holds zero positions. try_load_broker_view must call get_positions()
    itself so transient broker failures flip available=False — otherwise
    a flaky network blanks the entire dashboard table by treating every
    portfolio.json entry as 'closed at broker'."""

    class _FailingBroker:
        def get_positions(self):
            raise RuntimeError("simulated alpaca 500")

    import lib.alpaca_client as ac_mod
    monkeypatch.setattr(ac_mod, "AlpacaBroker", lambda *a, **kw: _FailingBroker())

    view = dd.try_load_broker_view()
    assert view.available is False, (
        "broker call failed transiently — must report unavailable, not "
        "available with empty held_keys (which would blank the dashboard)"
    )
    assert view.marks == {}
    assert view.costs == {}
    assert view.held_keys == frozenset()


def test_try_load_broker_view_distinguishes_unreachable_from_empty(
    tmp_state, monkeypatch
):
    """Sister regression: when the broker is reachable and reports an
    empty position list (a genuine all-cash account), available=True and
    held_keys=set() so the dashboard correctly hides stale portfolio.json
    rows."""

    class _EmptyBroker:
        def get_positions(self):
            return []

    import lib.alpaca_client as ac_mod
    monkeypatch.setattr(ac_mod, "AlpacaBroker", lambda *a, **kw: _EmptyBroker())

    view = dd.try_load_broker_view()
    assert view.available is True
    assert view.held_keys == frozenset()


def test_try_load_broker_view_returns_none_nav_when_get_account_fails(
    tmp_state, monkeypatch
):
    """When get_account() raises (transient Alpaca error) but
    get_positions() succeeds, BrokerView.nav_usd is None. The dashboard
    uses this only for the informational 'Alpaca account' sub-line, so
    None just means we hide that line — the synthetic balance hero is
    unaffected (it doesn't read broker equity at all)."""

    class _PartialBroker:
        def get_positions(self):
            return []

        def get_account(self):
            raise RuntimeError("simulated transient 500")

    import lib.alpaca_client as ac_mod
    monkeypatch.setattr(ac_mod, "AlpacaBroker", lambda *a, **kw: _PartialBroker())

    view = dd.try_load_broker_view()
    assert view.available is True, "positions reachable → still available"
    assert view.nav_usd is None


def test_try_load_broker_view_returns_raw_broker_equity(
    tmp_state, monkeypatch
):
    """Post-synthetic-balance refactor: BrokerView.nav_usd returns the
    broker's raw equity figure (no offset application). It's only used
    by the dashboard's informational sub-line — the headline hero
    derives from compute_synthetic_balance instead. Even when a
    legacy state/nav_offset.json sits on disk, the broker view path
    must NOT subtract from it."""

    class _StubBroker:
        def get_positions(self):
            return []

        def get_account(self):
            from lib.broker import Account
            return Account(
                cash_usd=50020.52,
                equity_usd=100020.52,
                buying_power_usd=50000.0,
                is_paper=True,
            )

    import lib.alpaca_client as ac_mod
    monkeypatch.setattr(ac_mod, "AlpacaBroker", lambda *a, **kw: _StubBroker())

    # No anchor on disk → broker_view.nav_usd is raw broker equity.
    pre = dd.try_load_broker_view()
    assert pre.available is True
    assert pre.nav_usd == pytest.approx(100020.52)

    # Legacy anchor on disk → should be IGNORED (refactor removed the
    # offset application). Same raw broker equity surfaces.
    state.set_nav_offset(
        broker_baseline_usd=100020.0,
        virtual_baseline_usd=2500.0,
        note="legacy anchor — should be ignored",
    )
    post = dd.try_load_broker_view()
    assert post.available is True
    assert post.nav_usd == pytest.approx(100020.52), (
        "raw broker equity must surface regardless of legacy "
        "state/nav_offset.json contents — the anchor file is "
        "vestigial after the synthetic-balance refactor"
    )


def test_position_table_rows_etf_and_option_columns(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio)
    kinds = {r["Kind"] for r in rows}
    assert kinds == {"ETF", "OPT"}
    opt_row = next(r for r in rows if r["Kind"] == "OPT")
    assert "Δ" in opt_row["Greeks"]
    # Kill cell now folds in any underlying price / time stops alongside
    # the max-loss %; assert on the prefix rather than full string so
    # fixture tweaks to those guards don't ripple into the test.
    assert opt_row["Kill"].startswith("≤100% loss")


def test_position_table_rows_without_marks_leaves_pnl_blank(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    rows = dd.position_table_rows(portfolio)
    for r in rows:
        assert r["Mark"] is None
        assert r["Gross P&L"] is None
        assert r["Net P&L"] is None


def test_position_table_rows_etf_pnl_with_marks(tmp_state):
    """ETF mark above cost → positive gross; net subtracts modelled costs."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio, marks={"TQQQ": 80.0})
    r = rows[0]
    assert r["Mark"] == 80.0
    # 4 shares × (80 - 70) = $40 gross
    assert r["Gross P&L"] == pytest.approx(40.0)
    # Net is below gross by some modelled round-trip cost.
    assert r["Net P&L"] < r["Gross P&L"]


def _option_pos():
    return {
        "kind": "option", "underlying": "SPY", "type": "call",
        "strike": 530.0, "expiry": "2026-06-19", "dte": 40,
        "contracts": 1, "premium_paid": 6.50,
        "greeks": {"delta": 0.45, "gamma": 0.02, "theta": -0.04, "vega": 0.18,
                    "iv": 0.18, "iv_percentile": 35},
        "entry_thesis": "x", "kill_conditions": {"max_loss_pct": 100},
        "position_pct": 5.0,
    }


def test_position_table_rows_option_pnl_uses_synthetic_key(tmp_state):
    """Option marks use 'underlying|strike|expiry|type' key, same as monitor.py."""
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},
        marks={"SPY|530.0|2026-06-19|call": 8.00},
    )
    r = rows[0]
    assert r["Mark"] == 8.00
    # 1 contract × 100 × (8.00 - 6.50) = $150 gross
    assert r["Gross P&L"] == pytest.approx(150.0)


def test_position_table_rows_option_pnl_falls_back_to_osi_key(tmp_state):
    """marks_from_broker currently keys options by OSI symbol. Until that
    aligns with the synthetic key, the dashboard must accept either so live
    option marks actually flow into the Mark / Gross / Net columns.

    Regression test for the P1 Codex flagged on PR #21: without this fallback,
    option P&L stays blank in any portfolio holding options.
    """
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},
        marks={"SPY260619C00530000": 8.00},
    )
    r = rows[0]
    assert r["Mark"] == 8.00
    assert r["Gross P&L"] == pytest.approx(150.0)


def test_allocation_pie_includes_cash(tmp_state):
    portfolio = json.loads(FIXTURE.read_text())
    pie = dd.allocation_pie(portfolio)
    assert any(r["label"] == "Cash" for r in pie)
    assert sum(r["value"] for r in pie) == pytest.approx(100.0, abs=0.5)


def test_position_pnl_uses_broker_cost_basis_when_provided(tmp_state):
    """Regression for the live observation on May 12 2026:
       Agent's portfolio.json said premium_paid=3.50 (its training-data prior)
       Alpaca actually filled at avg_cost=0.61
       Dashboard used to show -$290 fictional loss when truth was -$2.
    The `costs` dict passed into position_table_rows must override
    portfolio.json's premium_paid when computing Cost / Notional / P&L."""
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},  # premium_paid=6.50 in fixture
        marks={"SPY|530.0|2026-06-19|call": 0.59},
        costs={"SPY|530.0|2026-06-19|call": 0.61},
    )
    r = rows[0]
    # Cost column shows broker's actual fill, not the agent's premium_paid
    assert r["Entry"] == pytest.approx(0.61)
    # Notional reflects truth: 1 contract × 100 × $0.61 = $61
    assert r["Notional"] == pytest.approx(61.0)
    # Gross P&L: ($0.59 - $0.61) × 1 × 100 = -$2  (NOT the -$590 we'd see
    # if we used the fixture's premium_paid=6.50 as basis)
    assert r["Gross P&L"] == pytest.approx(-2.0)


def test_position_pnl_falls_back_to_portfolio_premium_when_no_broker_costs(tmp_state):
    """When the broker is unreachable (no costs dict), keep the old
    behaviour and use portfolio.json's premium_paid. Otherwise we'd
    silently lose P&L for offline / no-keys configurations."""
    rows = dd.position_table_rows(
        {"positions": [_option_pos()]},  # premium_paid=6.50
        marks={"SPY|530.0|2026-06-19|call": 8.00},
        costs=None,  # broker unavailable
    )
    r = rows[0]
    assert r["Entry"] == pytest.approx(6.50)
    # ($8.00 - $6.50) × 1 × 100 = $150
    assert r["Gross P&L"] == pytest.approx(150.0)


def test_position_pnl_etf_uses_broker_cost_basis_when_provided(tmp_state):
    """Same broker-truth principle for ETFs: prefer the broker's filled
    avg_cost over the portfolio's intended avg_cost."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 70.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(
        portfolio,
        marks={"TQQQ": 80.0},
        costs={"TQQQ": 75.0},  # agent thought $70, actually filled at $75
    )
    r = rows[0]
    assert r["Entry"] == pytest.approx(75.0)
    assert r["Notional"] == pytest.approx(300.0)  # 4 × $75
    # ($80 - $75) × 4 = $20 (not $40 against the agent's $70 intent)
    assert r["Gross P&L"] == pytest.approx(20.0)


def test_bias_column_etf_bull(tmp_state):
    """Bull leveraged ETFs (positive leverage_factor, e.g. TQQQ at +3x)
    surface as 'Bull' in the new Bias column."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio)
    assert rows[0]["Bias"] == "Bull"


def test_bias_column_etf_bear(tmp_state):
    """Inverse leveraged ETFs (negative leverage_factor, e.g. SQQQ at
    -3x) surface as 'Bear'. The system is long-only — a bear thesis is
    expressed by being long the inverse ETF — so the column shows the
    directional exposure, not the position side."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "SQQQ", "shares": 1, "avg_cost": 12.0,
            "leverage_factor": -3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio)
    assert rows[0]["Bias"] == "Bear"


def test_bias_column_option_put_is_bear(tmp_state):
    """Long puts express a bearish view on the underlying."""
    pos = _option_pos()
    pos["type"] = "put"
    rows = dd.position_table_rows({"positions": [pos]})
    assert rows[0]["Bias"] == "Bear"


def test_bias_column_option_call_is_bull(tmp_state):
    rows = dd.position_table_rows({"positions": [_option_pos()]})
    assert rows[0]["Bias"] == "Bull"


def test_bias_column_uvxy_is_long_vol(tmp_state):
    """UVXY has positive leverage but it's a vol instrument, not an
    equity-bull bet — labelling it 'Bull' would mislead the reader."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "UVXY", "shares": 1, "avg_cost": 20.0,
            "leverage_factor": 1.5, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio)
    assert rows[0]["Bias"] == "Long vol"


def test_delta_pct_column_computes_from_entry_and_mark(tmp_state):
    """Δ% is (mark - entry) / entry × 100, computed off per-unit prices
    so it's identical for an ETF share or an option contract premium."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio, marks={"TQQQ": 88.0})
    # (88 - 80) / 80 = +10%
    assert rows[0]["Δ%"] == pytest.approx(10.0)


def test_delta_pct_column_blank_without_mark(tmp_state):
    """No mark → no Δ%. Same convention as Gross / Net columns."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio)
    assert rows[0]["Δ%"] is None


def test_kill_column_includes_underlying_price_and_time_stop(tmp_state):
    """Kill cell folds underlying-price and time-stop guards alongside
    the max-loss %. Each guard becomes a ` · `-joined segment so the
    reader sees every trigger in one place."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {
                "max_loss_pct": 25,
                "underlying_price_below": 75.5,
                "time_stop_utc": "2026-06-12T20:00:00Z",
            },
            "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio)
    kill = rows[0]["Kill"]
    assert "≤25% loss" in kill
    assert "≤$75.5" in kill
    assert "by 2026-06-12" in kill


def test_split_positions_excludes_stale_for_prebake_pnl(tmp_state):
    """Codex P1 on PR #75: when portfolio.json carries stale rows the
    broker no longer holds (manual close, expiry, sync lag), the
    pre-bake path must NOT include them in compute_portfolio_pnl —
    each stale row with no live mark returns net_pnl = -entry_leg_cost,
    which would bake phantom losses into the NAV offset.

    Verifies the upstream contract that the anchor logic relies on:
    split_positions_by_broker_holdings keeps only broker-held rows.
    """
    portfolio = {"positions": [
        {
            "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        },
        {
            "kind": "etf", "symbol": "SQQQ", "shares": 1, "avg_cost": 12.0,
            "leverage_factor": -3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        },
    ]}
    # Broker only holds TQQQ — SQQQ is stale (e.g. manually closed).
    held = frozenset({"TQQQ"})
    open_subset, closed_subset = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=held,
    )
    assert [p["symbol"] for p in open_subset] == ["TQQQ"]
    assert [p["symbol"] for p in closed_subset] == ["SQQQ"]

    # And the pre-bake P&L computation against the filtered subset
    # must not pick up SQQQ's phantom entry-leg cost.
    from lib import pnl as pnl_lib
    pnl_all = pnl_lib.compute_portfolio_pnl(
        portfolio=portfolio, marks={"TQQQ": 88.0},
    )
    pnl_filtered = pnl_lib.compute_portfolio_pnl(
        portfolio={"positions": open_subset}, marks={"TQQQ": 88.0},
    )
    # The filtered net P&L should be strictly greater (less negative,
    # or more positive) than the unfiltered version — the stale row
    # contributes a non-zero entry-leg modelled cost on top.
    assert pnl_filtered.net_pnl_usd > pnl_all.net_pnl_usd, (
        "filtering out stale rows must improve the pre-bake P&L "
        "estimate — they contribute fictitious entry-leg losses"
    )


# ---------- SyntheticBalance ----------


def test_synthetic_balance_empty_state_equals_starting_balance(tmp_state):
    """No trades, no costs → balance = $2,500 baseline. unmarked_open_lots
    is 0 because there are no open lots at all."""
    sb = dd.compute_synthetic_balance()
    assert sb.starting_balance_usd == pytest.approx(2500.0)
    assert sb.closed_gross_pnl_usd == pytest.approx(0.0)
    assert sb.open_gross_pnl_usd == pytest.approx(0.0)
    assert sb.llm_cost_total_usd == pytest.approx(0.0)
    assert sb.trading_fees_total_usd == pytest.approx(0.0)
    assert sb.unmarked_open_lots == 0
    assert sb.synthetic_balance_usd == pytest.approx(2500.0)


def test_synthetic_balance_closed_trade_adds_gross_subtracts_fees(tmp_state):
    """One closed round-trip: +$20 gross, $0.50 in buy+sell fees. The
    balance picks up the gross via closed_gross_pnl AND subtracts the
    fees via trading_fees_total (real money paid)."""
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.25, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 1, "fill_price": 100.0,
        "fees_usd": 0.25, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance()
    assert sb.closed_gross_pnl_usd == pytest.approx(20.0)
    assert sb.trading_fees_total_usd == pytest.approx(0.50)
    # $2,500 + $20 closed gross − $0 LLM − $0.50 fees = $2,519.50
    assert sb.synthetic_balance_usd == pytest.approx(2519.50)


def test_synthetic_balance_open_lot_with_mark_adds_open_gross(tmp_state):
    """Open lot with a live mark contributes (mark − fill) × qty to
    open_gross_pnl. The fee paid on the buy is in trading_fees_total
    independently — open_gross reflects price movement only."""
    state.append_trade({
        "activity_id": "b1", "alpaca_order_id": "ob1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.30, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance(marks={"TQQQ": 90.0})
    assert sb.closed_gross_pnl_usd == pytest.approx(0.0)
    assert sb.open_gross_pnl_usd == pytest.approx(10.0)
    assert sb.unmarked_open_lots == 0
    assert sb.trading_fees_total_usd == pytest.approx(0.30)
    # $2,500 + $0 closed + $10 open − $0 LLM − $0.30 fees = $2,509.70
    assert sb.synthetic_balance_usd == pytest.approx(2509.70)


def test_synthetic_balance_open_lot_without_mark_flags_unmarked(tmp_state):
    """When marks are not available for a symbol, open_gross_pnl
    contribution is 0 (not arbitrary) and `unmarked_open_lots`
    increments so the dashboard can surface the gap to the operator."""
    state.append_trade({
        "activity_id": "c1", "alpaca_order_id": "oc1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance(marks={})  # no marks at all
    assert sb.open_gross_pnl_usd == pytest.approx(0.0)
    assert sb.unmarked_open_lots == 1
    assert sb.synthetic_balance_usd == pytest.approx(2500.0)


def test_synthetic_balance_subtracts_llm_cost(tmp_state):
    """LLM spend in costs.jsonl is subtracted in full (ALL spend,
    including runs that opened no positions). This is the truthful
    framing — every Anthropic call cost money even if it produced
    no trade."""
    state.append_cost({
        "run_id": "r1", "stage": "construct", "model": "m",
        "cost_usd": 0.35, "at": "2026-05-10T12:00:00Z",
    })
    state.append_cost({
        "run_id": "r2-all-cash", "stage": "strategist", "model": "m",
        "cost_usd": 0.05, "at": "2026-05-10T16:00:00Z",  # no trade for this run
    })
    sb = dd.compute_synthetic_balance()
    assert sb.llm_cost_total_usd == pytest.approx(0.40), (
        "ALL LLM cost rows count — including the all-cash run that "
        "produced no positions; the experiment still paid for the API call"
    )
    assert sb.synthetic_balance_usd == pytest.approx(2499.60)


def test_synthetic_balance_llm_reset_bumps_balance_upward(tmp_state):
    """state.set_all_time_cost_reset zeros the displayed LLM cost
    going forward. The synthetic balance bumps upward by exactly the
    pre-reset attribution. This is the wiring that makes 'Reset ALL
    LLM costs' a meaningful balance adjustment, not just a display
    fiddle."""
    state.append_cost({
        "run_id": "old", "stage": "x", "model": "m",
        "cost_usd": 0.40, "at": "2026-05-10T12:00:00Z",
    })
    pre = dd.compute_synthetic_balance()
    assert pre.llm_cost_total_usd == pytest.approx(0.40)
    pre_balance = pre.synthetic_balance_usd

    state.set_all_time_cost_reset("test")
    post = dd.compute_synthetic_balance()
    assert post.llm_cost_total_usd == pytest.approx(0.0)
    assert post.synthetic_balance_usd == pre_balance + 0.40, (
        "reset must visibly raise the balance by the historical LLM total"
    )


def test_synthetic_balance_trading_fees_never_reset(tmp_state):
    """Trading fees are real broker fees — actual money paid to
    Alpaca. The cost reset only affects LLM display; trading fees
    keep reducing the balance permanently."""
    state.append_trade({
        "activity_id": "f1", "alpaca_order_id": "of1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 1.50, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    pre = dd.compute_synthetic_balance()
    assert pre.trading_fees_total_usd == pytest.approx(1.50)
    state.set_all_time_cost_reset("test")
    post = dd.compute_synthetic_balance()
    assert post.trading_fees_total_usd == pytest.approx(1.50), (
        "trading fees must NOT be affected by the LLM cost reset"
    )


def test_synthetic_balance_custom_starting_balance(tmp_state):
    """starting_balance_usd is configurable so tests / future
    operators can override the CLAUDE.md $2,500 default cleanly."""
    sb = dd.compute_synthetic_balance(starting_balance_usd=5000.0)
    assert sb.starting_balance_usd == pytest.approx(5000.0)
    assert sb.synthetic_balance_usd == pytest.approx(5000.0)


def test_synthetic_balance_flags_unmatched_sells(tmp_state):
    """Codex P1 on PR #79: when trades.jsonl carries sells that
    don't FIFO-match against a buy (out-of-order sync, manual edit,
    or genuinely missing buy data), the upstream compute_trades_pnl
    silently drops them. The synthetic balance can't account for
    that P&L, so it must surface a warning to the operator rather
    than silently misrepresent the headline."""
    state.append_trade({
        "activity_id": "s1", "alpaca_order_id": "os1", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 2, "fill_price": 100.0,
        "fees_usd": 0.0, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance()
    assert sb.unmatched_sell_count == 1
    assert sb.is_integrity_warning is True


def test_synthetic_balance_unmatched_sell_not_flagged_when_matched(tmp_state):
    """A normal buy → sell round trip leaves no unmatched residue.
    is_integrity_warning is False so the dashboard renders cleanly."""
    state.append_trade({
        "activity_id": "b1", "alpaca_order_id": "ob1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 2, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "s1", "alpaca_order_id": "os1", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 2, "fill_price": 100.0,
        "fees_usd": 0.0, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance()
    assert sb.unmatched_sell_count == 0
    assert sb.is_integrity_warning is False


def test_synthetic_balance_open_gross_from_broker_positions(tmp_state):
    """Per the screenshot bug report from production: the user
    wiped trades.jsonl, then the agent opened new positions. The
    Portfolio tab's positions table correctly showed +$34 unrealized
    gross via broker marks + portfolio.json, but the hero card
    reported $0 open because compute_synthetic_balance was reading
    open lots from the (empty) trade log.

    Fix: when callers supply ``portfolio`` + ``broker_costs`` + a
    broker ``held_keys`` filter, open_gross derives from the
    broker-held subset of portfolio.json using compute_portfolio_pnl.
    Same source the positions table uses → two surfaces guaranteed
    to agree."""
    portfolio = {"positions": [
        {
            "kind": "etf", "symbol": "TQQQ", "shares": 2,
            "avg_cost": 80.0, "leverage_factor": 3.0,
            "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25},
            "position_pct": 8.0,
        },
    ]}
    # trades.jsonl is intentionally empty — the legacy open-lot path
    # would have returned $0 here. The broker positions path picks
    # up the +$20 unrealized.
    sb = dd.compute_synthetic_balance(
        marks={"TQQQ": 90.0},
        portfolio=portfolio,
        broker_costs={"TQQQ": 80.0},
        held_keys=frozenset({"TQQQ"}),
    )
    assert sb.open_gross_pnl_usd == pytest.approx(20.0)
    # Hybrid fees: real (closed) = $0 (no closed trades),
    # modelled (open) > $0 from compute_position_pnl on the open
    # TQQQ position. The synthetic balance subtracts the hybrid
    # total, so it sits BELOW the raw $2,520 by the modelled-fee
    # amount. Math: $2,500 + $20 open − modelled_open_fees.
    assert sb.modelled_open_fees_usd > 0, (
        "open ETF position should pick up a modelled round-trip cost"
    )
    assert sb.real_trading_fees_usd == pytest.approx(0.0)
    assert sb.trading_fees_total_usd == sb.modelled_open_fees_usd
    assert sb.synthetic_balance_usd == pytest.approx(
        2520.0 - sb.modelled_open_fees_usd
    )


def test_synthetic_balance_open_gross_filters_stale_portfolio_rows(tmp_state):
    """When portfolio.json carries stale rows the broker no longer
    holds, they must NOT contribute to open_gross. The held_keys
    filter (same one the positions table uses) does this."""
    portfolio = {"positions": [
        {
            "kind": "etf", "symbol": "TQQQ", "shares": 2,
            "avg_cost": 80.0, "leverage_factor": 3.0,
            "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25},
            "position_pct": 8.0,
        },
        {
            "kind": "etf", "symbol": "SQQQ", "shares": 1,
            "avg_cost": 12.0, "leverage_factor": -3.0,
            "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25},
            "position_pct": 5.0,
        },
    ]}
    # Broker only carries TQQQ. SQQQ is stale (manually closed,
    # expired, or sync lag). Must be excluded from open_gross.
    sb = dd.compute_synthetic_balance(
        marks={"TQQQ": 90.0, "SQQQ": 10.0},
        portfolio=portfolio,
        broker_costs={"TQQQ": 80.0},
        held_keys=frozenset({"TQQQ"}),
    )
    assert sb.open_gross_pnl_usd == pytest.approx(20.0), (
        "stale SQQQ row must not bleed phantom $-2 P&L into open_gross"
    )


def test_synthetic_balance_open_gross_counts_unmarked_broker_positions(tmp_state):
    """Broker holds a position but no mark is available — counted as
    unmarked, contributes $0 to open_gross. Same convention as the
    legacy trades.jsonl path."""
    portfolio = {"positions": [{
        "kind": "etf", "symbol": "TQQQ", "shares": 2,
        "avg_cost": 80.0, "leverage_factor": 3.0,
        "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 8.0,
    }]}
    sb = dd.compute_synthetic_balance(
        marks={},  # no marks at all
        portfolio=portfolio,
        broker_costs={},
        held_keys=frozenset({"TQQQ"}),
    )
    assert sb.unmarked_open_lots == 1
    assert sb.open_gross_pnl_usd == pytest.approx(0.0)


def test_synthetic_balance_open_gross_legacy_path_when_no_portfolio(tmp_state):
    """When callers don't pass portfolio, compute_synthetic_balance
    falls back to the trades.jsonl open-lot path. Backward-compat for
    existing tests + the Realized balance card (which passes
    marks={} so neither path contributes)."""
    state.append_trade({
        "activity_id": "b1", "alpaca_order_id": "ob1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance(marks={"TQQQ": 90.0})
    assert sb.open_gross_pnl_usd == pytest.approx(10.0), (
        "legacy path: open lot from trades.jsonl still works when "
        "no portfolio is supplied — tests can exercise it without "
        "broker plumbing"
    )


def test_synthetic_balance_partial_unmatched_sell(tmp_state):
    """Buy 1, sell 2 → 1 unit of the sell can FIFO-match (closing
    that round trip) and the leftover 1 unit lands in unmatched_sells.
    The closed trade reflects the 1-unit match; the leftover bumps
    the integrity counter."""
    state.append_trade({
        "activity_id": "b1", "alpaca_order_id": "ob1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "s1", "alpaca_order_id": "os1", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 2, "fill_price": 100.0,
        "fees_usd": 0.0, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance()
    # The 1-unit match still records a +$20 close.
    assert sb.closed_gross_pnl_usd == pytest.approx(20.0)
    # Leftover 1 unit on the sell side surfaces as unmatched.
    assert sb.unmatched_sell_count == 1
    assert sb.is_integrity_warning is True


# ---------- Hybrid trading fees ----------


def test_synthetic_balance_hybrid_fees_paper_etf_no_real(tmp_state):
    """On Alpaca paper ETFs, real fees are \$0. The hybrid fees model
    surfaces the modelled IBKR-Pro round-trip estimate as the
    headline fees so the synthetic balance reflects what the
    per-position table already shows. Without this fix the operator
    sees "$0.00 fees" in the breakdown despite per-row Fees showing
    a multi-dollar drag."""
    portfolio = {"positions": [{
        "kind": "etf", "symbol": "TQQQ", "shares": 2,
        "avg_cost": 80.0, "leverage_factor": 3.0,
        "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 8.0,
    }]}
    sb = dd.compute_synthetic_balance(
        marks={"TQQQ": 90.0},
        portfolio=portfolio,
        broker_costs={"TQQQ": 80.0},
        held_keys=frozenset({"TQQQ"}),
    )
    assert sb.real_trading_fees_usd == pytest.approx(0.0), (
        "no closed trades + paper ETF → real fees stay at zero"
    )
    assert sb.modelled_open_fees_usd > 0, (
        "open ETF position should carry a modelled round-trip estimate "
        "matching the positions table's per-row Fees column"
    )
    assert sb.trading_fees_total_usd == pytest.approx(
        sb.real_trading_fees_usd + sb.modelled_open_fees_usd
    )


def test_synthetic_balance_hybrid_fees_real_plus_modelled(tmp_state):
    """A closed round-trip with real fees AND an open position with a
    modelled estimate: both contribute to trading_fees_total. Codex
    should read this and understand the formula at a glance."""
    # Closed round-trip with real fees $0.50 total.
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.25, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 1, "fill_price": 100.0,
        "fees_usd": 0.25, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    # Plus a currently-open SQQQ position with a modelled estimate.
    portfolio = {"positions": [{
        "kind": "etf", "symbol": "SQQQ", "shares": 1,
        "avg_cost": 12.0, "leverage_factor": -3.0,
        "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 5.0,
    }]}
    sb = dd.compute_synthetic_balance(
        marks={"SQQQ": 13.0},
        portfolio=portfolio,
        broker_costs={"SQQQ": 12.0},
        held_keys=frozenset({"SQQQ"}),
    )
    assert sb.real_trading_fees_usd == pytest.approx(0.50)
    assert sb.modelled_open_fees_usd > 0
    assert sb.trading_fees_total_usd == pytest.approx(
        0.50 + sb.modelled_open_fees_usd
    )


def test_synthetic_balance_hybrid_fees_skips_modelled_when_broker_unreachable(tmp_state):
    """Codex P1 on PR #82: when the broker is unreachable
    (``held_keys=None``), split_positions_by_broker_holdings returns
    every portfolio.json row as "open" — including positions that
    may have already closed manually. Charging modelled fees against
    those would bias the synthetic balance downward by phantom costs
    during an outage. Require broker holdings confirmation before
    accumulating modelled fees."""
    portfolio = {"positions": [{
        "kind": "etf", "symbol": "TQQQ", "shares": 2,
        "avg_cost": 80.0, "leverage_factor": 3.0,
        "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 8.0,
    }]}
    # held_keys=None simulates a broker outage: try_load_broker_view
    # couldn't reach Alpaca, so we have a stale portfolio.json but
    # no way to verify what's actually held.
    sb = dd.compute_synthetic_balance(
        marks={"TQQQ": 90.0},
        portfolio=portfolio,
        broker_costs={"TQQQ": 80.0},
        held_keys=None,
    )
    assert sb.modelled_open_fees_usd == pytest.approx(0.0), (
        "broker outage → modelled fees must NOT be accumulated; "
        "stale portfolio.json rows could include closed positions"
    )
    # open_gross still reflects (mark - cost) * qty — the operator
    # can still see unrealized P&L from cached marks, just not
    # modelled fee drag.
    assert sb.open_gross_pnl_usd == pytest.approx(20.0)


def test_synthetic_balance_hybrid_fees_no_portfolio_skips_modelled(tmp_state):
    """When callers don't supply a portfolio (e.g. the Realized
    balance card sources closed-only), the modelled component stays
    zero — we have no broker-held positions to estimate against.
    real_trading_fees_usd still populates from trades.jsonl."""
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.25, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 1, "fill_price": 100.0,
        "fees_usd": 0.25, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    sb = dd.compute_synthetic_balance()
    assert sb.real_trading_fees_usd == pytest.approx(0.50)
    assert sb.modelled_open_fees_usd == pytest.approx(0.0)
    assert sb.trading_fees_total_usd == pytest.approx(0.50)


# ---------- live_balance_tip ----------


def test_live_balance_tip_matches_synthetic_balance(tmp_state):
    """The live tip is the chart's anchor to the hero card — by
    construction its synthetic_balance_usd value must equal the
    SyntheticBalance.synthetic_balance_usd exactly."""
    portfolio = {"positions": [{
        "kind": "etf", "symbol": "TQQQ", "shares": 2,
        "avg_cost": 80.0, "leverage_factor": 3.0,
        "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25},
        "position_pct": 8.0,
    }]}
    sb = dd.compute_synthetic_balance(
        marks={"TQQQ": 90.0},
        portfolio=portfolio,
        broker_costs={"TQQQ": 80.0},
        held_keys=frozenset({"TQQQ"}),
    )
    tip = dd.live_balance_tip(synthetic_balance=sb)
    assert tip["synthetic_balance_usd"] == pytest.approx(sb.synthetic_balance_usd)
    assert tip["closed_gross_pnl_usd"] == pytest.approx(sb.closed_gross_pnl_usd)
    assert tip["open_gross_pnl_usd"] == pytest.approx(sb.open_gross_pnl_usd)
    assert tip["llm_cost_total_usd"] == pytest.approx(sb.llm_cost_total_usd)
    assert tip["trading_fees_total_usd"] == pytest.approx(sb.trading_fees_total_usd)
    assert tip["kind"] == "live"
    assert tip["at"]  # ISO timestamp non-empty


def test_live_balance_tip_works_with_empty_series(tmp_state):
    """Even when no realized events exist yet, the helper must
    return a usable tip so the chart can still render the current
    snapshot as a single marker."""
    sb = dd.compute_synthetic_balance()
    tip = dd.live_balance_tip(synthetic_balance=sb)
    assert tip["synthetic_balance_usd"] == pytest.approx(sb.starting_balance_usd)
    assert tip["kind"] == "live"


# ---------- realized_balance_series ----------


def test_realized_balance_series_empty_state(tmp_state):
    assert dd.realized_balance_series() == []


def test_realized_balance_series_records_close_event(tmp_state):
    """A single closed round-trip emits one point at the close
    timestamp with synthetic_realized_balance = $2,500 + gross − fees."""
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 1, "fill_price": 100.0,
        "fees_usd": 0.0, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    series = dd.realized_balance_series()
    # Two events from trades.jsonl: a buy (fee=0 so skipped from
    # fees deltas) and a sell whose closed_at lands the +$20 gross.
    # Buy and sell both have fees_usd=0 so no fee events. Only the
    # close event contributes here.
    closes = [r for r in series if r["closed_gross_pnl_usd"] > 0]
    assert closes
    assert closes[-1]["synthetic_realized_balance_usd"] == pytest.approx(2520.0)


def test_realized_balance_series_interleaves_costs_and_closes(tmp_state):
    """The series walks all event types in chronological order.
    Intermediate points reflect the running totals at each step."""
    # Day 1: LLM cost lands.
    state.append_cost({
        "run_id": "r1", "stage": "construct", "model": "m",
        "cost_usd": 0.20, "at": "2026-05-10T08:00:00Z",
    })
    # Day 2: round-trip closes at +$30.
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 70.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": "r1",
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 1, "fill_price": 100.0,
        "fees_usd": 0.0, "filled_at": "2026-05-11T13:00:00Z", "run_id": "r1",
    })

    series = dd.realized_balance_series()
    # The last point should reflect the full state: +$30 closed, −$0.20 LLM.
    last = series[-1]
    assert last["synthetic_realized_balance_usd"] == pytest.approx(2529.80)
    assert last["closed_gross_pnl_usd"] == pytest.approx(30.0)
    assert last["llm_cost_total_usd"] == pytest.approx(0.20)
    # The cost-only point earlier in the series should sit below $2,500.
    cost_only = next(
        r for r in series
        if r["llm_cost_total_usd"] > 0 and r["closed_gross_pnl_usd"] == 0
    )
    assert cost_only["synthetic_realized_balance_usd"] == pytest.approx(2499.80)


def test_realized_balance_series_honors_llm_cost_reset(tmp_state):
    """After a reset, the LLM cost component of the series drops to 0
    going forward — exactly what the operator expects when clicking
    'Reset ALL LLM costs'."""
    # Pre-reset cost should disappear from the series.
    state.append_cost({
        "run_id": "old", "stage": "x", "model": "m",
        "cost_usd": 0.50, "at": "2026-05-10T12:00:00Z",
    })
    # Plant a reset marker strictly between the two cost rows so the
    # post-reset cost survives filter_costs_post_reset (which drops
    # rows with at <= reset_at).
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    state.append_cost({
        "run_id": "new", "stage": "y", "model": "m",
        "cost_usd": 0.10, "at": "2026-05-12T08:00:00Z",
    })
    series = dd.realized_balance_series()
    # The reset filter drops the pre-reset row; only the post-reset
    # cost of $0.10 appears.
    assert len(series) == 1
    last = series[-1]
    assert last["llm_cost_total_usd"] == pytest.approx(0.10)
    assert last["synthetic_realized_balance_usd"] == pytest.approx(2499.90)


# ---------- back-compat: settled_balance_usd is now a thin wrapper ----------


def test_settled_balance_no_trades_returns_virtual_baseline(tmp_state):
    """Fresh account, no closed trades → settled balance = virtual
    baseline exactly. Backward-compat wrapper around the synthetic
    balance with empty marks."""
    assert dd.settled_balance_usd(virtual_baseline_usd=2500.0) == pytest.approx(2500.0)


def test_settled_balance_adds_realised_net_pnl(tmp_state):
    """One round-trip trade closes at +$20 net → settled balance moves
    to virtual + $20. The hero NAV may show $2,500 + intra-day mark
    swing, but this card only counts closed-trade fills."""
    # Buy then sell same symbol — realised gain on the round trip.
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 1, "fill_price": 100.0,
        "fees_usd": 0.0, "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    settled = dd.settled_balance_usd(virtual_baseline_usd=2500.0)
    assert settled == pytest.approx(2520.0), (
        "ETF closed +$20 (1 share × $20 gain, $0 fees) → "
        "$2,500 baseline + $20 realised = $2,520"
    )


def test_closed_trade_chips_empty_when_no_closes(tmp_state):
    assert dd.closed_trade_chips() == []


def test_closed_trade_chips_returns_newest_first(tmp_state):
    """Chips strip is newest-first so a fresh close lands at the left
    of the row. Earlier closes drift right."""
    # Two round-trips on different days.
    for i, (sym, buy_at, sell_at, buy_p, sell_p) in enumerate([
        ("TQQQ", "2026-05-09T13:00:00Z", "2026-05-10T13:00:00Z", 80.0, 90.0),
        ("SQQQ", "2026-05-11T13:00:00Z", "2026-05-12T13:00:00Z", 12.0, 10.0),
    ]):
        state.append_trade({
            "activity_id": f"a{i}b", "alpaca_order_id": f"o{i}b",
            "symbol": sym, "kind": "etf", "side": "buy",
            "qty": 1, "fill_price": buy_p, "fees_usd": 0.0,
            "filled_at": buy_at, "run_id": None,
        })
        state.append_trade({
            "activity_id": f"a{i}s", "alpaca_order_id": f"o{i}s",
            "symbol": sym, "kind": "etf", "side": "sell",
            "qty": 1, "fill_price": sell_p, "fees_usd": 0.0,
            "filled_at": sell_at, "run_id": None,
        })
    chips = dd.closed_trade_chips()
    assert len(chips) == 2
    # SQQQ closed later → first in the chips list
    assert chips[0]["symbol"] == "SQQQ"
    assert chips[1]["symbol"] == "TQQQ"
    # SQQQ lost $2; TQQQ gained $10.
    assert chips[0]["net_pnl_usd"] == pytest.approx(-2.0)
    assert chips[1]["net_pnl_usd"] == pytest.approx(10.0)


def test_closed_trade_chips_respects_limit(tmp_state):
    """The strip caps at `limit` so a long trade history doesn't
    overflow the hero row."""
    for i in range(8):
        state.append_trade({
            "activity_id": f"b{i}", "alpaca_order_id": f"ob{i}",
            "symbol": "TQQQ", "kind": "etf", "side": "buy",
            "qty": 1, "fill_price": 80.0, "fees_usd": 0.0,
            "filled_at": f"2026-05-{10+i:02d}T13:00:00Z", "run_id": None,
        })
        state.append_trade({
            "activity_id": f"s{i}", "alpaca_order_id": f"os{i}",
            "symbol": "TQQQ", "kind": "etf", "side": "sell",
            "qty": 1, "fill_price": 81.0, "fees_usd": 0.0,
            "filled_at": f"2026-05-{10+i:02d}T14:00:00Z", "run_id": None,
        })
    chips = dd.closed_trade_chips(limit=3)
    assert len(chips) == 3


def test_closed_trade_chips_by_ticker_empty_when_no_closes(tmp_state):
    assert dd.closed_trade_chips_by_ticker() == []


def test_closed_trade_chips_by_ticker_aggregates_same_symbol(tmp_state):
    """Two round-trips on the same symbol collapse into one chip with
    ``trade_count=2`` and the summed net P&L — the whole point of the
    aggregate strip vs the per-trade recent strip."""
    for i, (buy_p, sell_p) in enumerate([(80.0, 82.0), (90.0, 93.0)]):
        state.append_trade({
            "activity_id": f"b{i}", "alpaca_order_id": f"ob{i}",
            "symbol": "UPRO", "kind": "etf", "side": "buy",
            "qty": 1, "fill_price": buy_p, "fees_usd": 0.0,
            "filled_at": f"2026-05-{10+i:02d}T13:00:00Z", "run_id": None,
        })
        state.append_trade({
            "activity_id": f"s{i}", "alpaca_order_id": f"os{i}",
            "symbol": "UPRO", "kind": "etf", "side": "sell",
            "qty": 1, "fill_price": sell_p, "fees_usd": 0.0,
            "filled_at": f"2026-05-{10+i:02d}T14:00:00Z", "run_id": None,
        })
    chips = dd.closed_trade_chips_by_ticker()
    assert len(chips) == 1
    assert chips[0]["symbol"] == "UPRO"
    assert chips[0]["trade_count"] == 2
    # +$2 + +$3 = +$5 across both round-trips (no fees in fixture).
    assert chips[0]["net_pnl_usd"] == pytest.approx(5.0)


def test_closed_trade_chips_by_ticker_orders_by_abs_pnl(tmp_state):
    """Aggregate strip leads with the biggest absolute contributor,
    positive or negative — so a -$5 ticker outranks a +$2 ticker."""
    # Symbol A: one round-trip losing $5.
    state.append_trade({
        "activity_id": "ab", "alpaca_order_id": "oab",
        "symbol": "SQQQ", "kind": "etf", "side": "buy",
        "qty": 1, "fill_price": 20.0, "fees_usd": 0.0,
        "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "as", "alpaca_order_id": "oas",
        "symbol": "SQQQ", "kind": "etf", "side": "sell",
        "qty": 1, "fill_price": 15.0, "fees_usd": 0.0,
        "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    # Symbol B: one round-trip winning $2.
    state.append_trade({
        "activity_id": "bb", "alpaca_order_id": "obb",
        "symbol": "TQQQ", "kind": "etf", "side": "buy",
        "qty": 1, "fill_price": 80.0, "fees_usd": 0.0,
        "filled_at": "2026-05-10T13:00:00Z", "run_id": None,
    })
    state.append_trade({
        "activity_id": "bs", "alpaca_order_id": "obs",
        "symbol": "TQQQ", "kind": "etf", "side": "sell",
        "qty": 1, "fill_price": 82.0, "fees_usd": 0.0,
        "filled_at": "2026-05-11T13:00:00Z", "run_id": None,
    })
    chips = dd.closed_trade_chips_by_ticker()
    assert [c["symbol"] for c in chips] == ["SQQQ", "TQQQ"]
    assert chips[0]["net_pnl_usd"] == pytest.approx(-5.0)
    assert chips[1]["net_pnl_usd"] == pytest.approx(2.0)


def test_fees_column_populates_modelled_round_trip_cost(tmp_state):
    """The Fees column surfaces the modelled round-trip broker cost
    from compute_position_pnl — same definition as the Performance
    tab's 'Modelled trading costs' aggregate. Must be > 0 for any
    real position so Net P&L = Gross − Fees is visibly correct."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 4, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio, marks={"TQQQ": 88.0})
    r = rows[0]
    assert "Fees" in r
    assert isinstance(r["Fees"], (int, float))
    assert r["Fees"] > 0, "real ETF position should carry non-zero modelled fees"
    # Sanity: Gross − Fees should equal Net (within rounding).
    assert r["Gross P&L"] - r["Fees"] == pytest.approx(r["Net P&L"])


def test_dte_column_present_for_options_dash_for_etfs(tmp_state):
    """DTE comes straight from position.schema 'dte'; ETF rows show
    '—' since they have no expiry."""
    etf = {
        "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
        "leverage_factor": 3.0, "entry_thesis": "x",
        "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
    }
    rows = dd.position_table_rows({"positions": [etf, _option_pos()]})
    etf_row = next(r for r in rows if r["Kind"] == "ETF")
    opt_row = next(r for r in rows if r["Kind"] == "OPT")
    assert etf_row["DTE"] == "—"
    assert opt_row["DTE"] == 40


def test_days_held_from_opened_at_map_for_etf(tmp_state):
    """`opened_at_by_symbol` is keyed by broker symbol (ETF ticker / OSI
    for options). Days-held is whole-days since that timestamp."""
    from datetime import datetime, timezone, timedelta
    six_days_ago = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat().replace("+00:00", "Z")
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(
        portfolio, opened_at_by_symbol={"TQQQ": six_days_ago},
    )
    assert rows[0]["Days held"] == 6


def test_days_held_none_when_no_opened_at(tmp_state):
    """Without trade history the row renders blank rather than fabricating zero."""
    portfolio = {
        "positions": [{
            "kind": "etf", "symbol": "TQQQ", "shares": 1, "avg_cost": 80.0,
            "leverage_factor": 3.0, "entry_thesis": "x",
            "kill_conditions": {"max_loss_pct": 25}, "position_pct": 5.0,
        }],
    }
    rows = dd.position_table_rows(portfolio)
    assert rows[0]["Days held"] is None


def test_opened_at_map_picks_earliest_buy_per_symbol(tmp_state):
    """When multiple buy fills exist for the same symbol (averaging-in),
    Days held should anchor on the FIRST one — that's when the position
    was opened, not when it was added to."""
    trades = [
        {"symbol": "TQQQ", "side": "buy",  "filled_at": "2026-04-10T15:00:00Z"},
        {"symbol": "TQQQ", "side": "buy",  "filled_at": "2026-05-01T15:00:00Z"},
        {"symbol": "TQQQ", "side": "sell", "filled_at": "2026-04-15T15:00:00Z"},  # noise
    ]
    out = dd._opened_at_map_from_trades(trades)
    assert out["TQQQ"] == "2026-04-10T15:00:00Z"


def test_load_decisions_empty_log(tmp_state):
    assert dd.load_decisions() == []


def test_cost_today_zero_no_log(tmp_state):
    assert dd.cost_today_usd() == 0.0


def test_load_run_summaries_returns_empty_when_no_runs(tmp_state):
    assert dd.load_run_summaries() == []


def test_load_run_summaries_pulls_rationales_and_funnel(tmp_state):
    """Each summary aggregates portfolio.json (rationales + position
    count), the v2 funnel widths from signals.json + view.json,
    sanity.json status, critique.json accept flag, next_run.json,
    and the run's total cost from the cost log. Powers the Cycles
    tab."""
    rid = "20260512T123456Z-deadbeef"
    run_dir = state.RUNS_DIR / rid
    run_dir.mkdir(parents=True)
    state.write_json(run_dir / "signals.json", {
        "tickers": [{"symbol": f"S{i}"} for i in range(15)],
    })
    state.write_json(run_dir / "view.json", {
        "regime": "trending_up",
        "candidates": [
            {"symbol": "TQQQ", "instrument_kind": "etf"},
            {"symbol": "SOXL", "instrument_kind": "etf"},
        ],
    })
    state.write_json(run_dir / "portfolio.json", {
        "run_id": rid, "generated_at": "2026-05-12T12:34:56Z",
        "all_cash": True, "positions": [],
        "all_cash_rationale": "Single positive-EV candidate below threshold.",
        "construction_rationale": "Zero positions taken.",
    })
    state.write_json(run_dir / "sanity.json", {
        "status": "pass", "summary": {"pass": 6, "warn": 0, "fail": 0, "skip": 4},
        "rules": [],
    })
    state.write_json(run_dir / "critique.json", {
        "accept": True, "critique": "ok", "suggested_changes": [],
    })
    state.write_json(run_dir / "next_run.json", {
        "next_run_at": "2026-05-12T14:00:00Z",
        "rationale": "Wait for market open.",
    })
    state.append_cost({
        "run_id": rid, "stage": "strategist", "model": "sonnet",
        "cost_usd": 0.05, "at": "2026-05-12T12:30:00Z",
    })
    state.append_cost({
        "run_id": rid, "stage": "construct", "model": "opus",
        "cost_usd": 0.20, "at": "2026-05-12T12:34:00Z",
    })

    out = dd.load_run_summaries(limit=10)
    assert len(out) == 1
    s = out[0]
    assert s["run_id"] == rid
    assert s["all_cash"] is True
    assert s["positions_count"] == 0
    assert s["signals_count"] == 15
    assert s["candidates_count"] == 2
    assert s["regime"] == "trending_up"
    assert s["sanity_status"] == "pass"
    assert s["critic_accept"] is True
    assert s["all_cash_rationale"].startswith("Single positive-EV candidate")
    assert s["next_run_rationale"] == "Wait for market open."
    assert s["cost_usd"] == pytest.approx(0.25)


def test_load_run_summaries_newest_first(tmp_state):
    """Run dirs are timestamp-prefixed; summary order must be newest first."""
    for rid in ("20260510T100000Z-old", "20260512T100000Z-new", "20260511T100000Z-mid"):
        (state.RUNS_DIR / rid).mkdir(parents=True)
    out = dd.load_run_summaries()
    assert [s["run_id"] for s in out] == [
        "20260512T100000Z-new",
        "20260511T100000Z-mid",
        "20260510T100000Z-old",
    ]


def test_load_run_summaries_tolerates_null_list_fields(tmp_state):
    """Regression for Codex P1 on PR #35: a malformed artifact like
    {"tickers": null} previously raised TypeError on len(None),
    killing the entire Cycles tab render. One bad run must not take
    down visibility for all others."""
    bad_rid = "20260512T120000Z-corrupt"
    good_rid = "20260512T130000Z-clean"

    bad_dir = state.RUNS_DIR / bad_rid
    bad_dir.mkdir(parents=True)
    state.write_json(bad_dir / "signals.json", {"tickers": None})
    state.write_json(bad_dir / "view.json", {"candidates": None})
    state.write_json(bad_dir / "portfolio.json", {"positions": None, "all_cash": None})
    state.write_json(bad_dir / "next_run.json", {"next_run_at": None, "rationale": None})

    good_dir = state.RUNS_DIR / good_rid
    good_dir.mkdir(parents=True)
    state.write_json(good_dir / "signals.json", {"tickers": [{"symbol": "TQQQ"}]})

    # Must not raise. Both summaries should come back.
    out = dd.load_run_summaries()
    assert len(out) == 2

    # Corrupt run shows zeros for the null fields, not garbage / crash
    corrupt = next(s for s in out if s["run_id"] == bad_rid)
    assert corrupt["signals_count"] == 0
    assert corrupt["candidates_count"] == 0
    assert corrupt["positions_count"] == 0
    assert corrupt["next_run_rationale"] == ""

    # Clean run still parsed correctly alongside the corrupt one
    clean = next(s for s in out if s["run_id"] == good_rid)
    assert clean["signals_count"] == 1


def test_load_run_summaries_tolerates_top_level_non_dict(tmp_state):
    """Even more degenerate: the artifact is a JSON list/string at the
    top level. Still mustn't crash."""
    rid = "20260512T140000Z-weird"
    run_dir = state.RUNS_DIR / rid
    run_dir.mkdir(parents=True)
    state.write_json(run_dir / "portfolio.json", ["not", "a", "dict"])
    state.write_json(run_dir / "next_run.json", "just a string")
    state.write_json(run_dir / "signals.json", 42)

    out = dd.load_run_summaries()
    assert len(out) == 1
    assert out[0]["run_id"] == rid
    assert out[0]["positions_count"] == 0
    assert out[0]["signals_count"] == 0


def test_load_run_summaries_defaults_cycle_intent_to_trade(tmp_state):
    """Legacy runs (decision rows lack cycle_intent) default to 'trade'
    in the summary so the Cycles tab doesn't render '📋 review' for
    pre-feature history."""
    rid = "20260510T120000Z-legacy"
    (state.RUNS_DIR / rid).mkdir(parents=True)
    out = dd.load_run_summaries()
    assert len(out) == 1
    assert out[0]["cycle_intent"] == "trade"


def test_load_run_summaries_review_cycle_timestamp_falls_back_to_run_id(tmp_state):
    """Review cycles don't write portfolio.json, so generated_at would
    otherwise be empty and the Cycles tab would render 'in flight' for
    a completed review. Fall back to the run_id timestamp prefix
    (YYYYMMDDTHHMMSSZ-xxxxxx → 2026-05-15T10:50:12Z)."""
    rid = "20260515T105012Z-1709c2"
    rdir = state.RUNS_DIR / rid
    rdir.mkdir(parents=True)
    # review.json is the completion marker for review cycles.
    state.write_json(rdir / "review.json", {"regime": "neutral", "candidates": []})
    out = dd.load_run_summaries()
    assert len(out) == 1
    assert out[0]["generated_at"] == "2026-05-15T10:50:12Z"


def test_load_run_summaries_in_flight_run_stays_in_flight(tmp_state):
    """Codex P2: a run dir with neither portfolio.json nor review.json
    is in-flight or aborted. generated_at must stay empty so the
    Cycles tab still renders 'in flight' — that's the operational
    signal an operator relies on to spot stuck cycles."""
    rid = "20260515T105012Z-stalled"
    rdir = state.RUNS_DIR / rid
    rdir.mkdir(parents=True)
    # Only signals.json written — cycle never produced a portfolio or
    # review payload (crashed / aborted / still running).
    state.write_json(rdir / "signals.json", {"tickers": []})
    out = dd.load_run_summaries()
    assert len(out) == 1
    assert out[0]["generated_at"] == "", (
        "in-flight/aborted runs must keep generated_at empty so the "
        "Cycles tab renders 'in flight'"
    )


def test_load_run_summaries_portfolio_generated_at_overrides_rid_fallback(tmp_state):
    """When portfolio.json carries its own generated_at (trade cycles),
    that wins over the rid timestamp fallback — it's more precise."""
    rid = "20260515T105012Z-1709c2"
    rdir = state.RUNS_DIR / rid
    rdir.mkdir(parents=True)
    state.write_json(rdir / "portfolio.json", {
        "run_id": rid, "generated_at": "2026-05-15T10:50:30Z",
        "all_cash": False, "positions": [],
    })
    out = dd.load_run_summaries()
    assert out[0]["generated_at"] == "2026-05-15T10:50:30Z"


def test_load_run_summaries_portfolio_missing_generated_at_keeps_rid_fallback(tmp_state):
    """Trade cycle whose portfolio.json lacks generated_at (defensive
    path for malformed LLM output) must still surface a timestamp on
    the Cycles tab via the rid fallback."""
    rid = "20260515T105012Z-1709c2"
    rdir = state.RUNS_DIR / rid
    rdir.mkdir(parents=True)
    state.write_json(rdir / "portfolio.json", {
        "run_id": rid, "all_cash": False, "positions": [],
        # no generated_at
    })
    out = dd.load_run_summaries()
    assert out[0]["generated_at"] == "2026-05-15T10:50:12Z"


def test_load_run_summaries_surfaces_review_intent(tmp_state):
    """Review cycles tag every decision row with cycle_intent='review';
    the summary reads the first matching row to pick the run's intent."""
    rid = "20260512T210000Z-review"
    rdir = state.RUNS_DIR / rid
    rdir.mkdir(parents=True)
    # Review cycles write review.json (not view.json).
    state.write_json(rdir / "signals.json", {"tickers": []})
    state.write_json(rdir / "review.json", {
        "regime": "neutral", "candidates": [],
    })
    state.append_decision({
        "run_id": rid, "stage": "review_complete",
        "model": "local-deterministic",
        "inputs_hash": "a" * 32, "output_ref": "review.json",
        "prompt_cache_hit_pct": 0.0, "cost_usd": 0.0,
        "started_at": "2026-05-12T21:00:00Z",
        "ended_at": "2026-05-12T21:00:01Z",
        "status": "ok", "risk_warning": "test",
        "cycle_intent": "review", "intent_source": "file",
    })
    out = dd.load_run_summaries()
    assert len(out) == 1
    assert out[0]["cycle_intent"] == "review"
    # And the review.json fallback populated the regime field.
    assert out[0]["regime"] == "neutral"


def test_load_run_summaries_tolerates_missing_artifacts(tmp_state):
    """A run dir that crashed mid-stage may be missing some artifacts. The
    summary should still come back populated for whatever was written."""
    rid = "20260512T120000Z-partial"
    run_dir = state.RUNS_DIR / rid
    run_dir.mkdir(parents=True)
    # Only signals.json — no portfolio.json or next_run.json
    state.write_json(run_dir / "signals.json", {"tickers": [{"symbol": "TQQQ"}]})

    out = dd.load_run_summaries()
    assert len(out) == 1
    s = out[0]
    assert s["run_id"] == rid
    assert s["signals_count"] == 1
    assert s["all_cash"] is None  # never written
    assert s["construction_rationale"] == ""
    assert s["next_run_rationale"] == ""


def test_runs_count_distinct_run_ids(tmp_state):
    """`runs_count()` must count distinct run_ids, not cost-log rows. Codex
    P2 on PR #33: total_token_cost()['calls'] was being mislabelled as
    'Runs all time' on the dashboard. One orchestrator run produces ~6-8
    cost-log rows (one per LLM stage + retries) so 'calls' over-counts
    runs by that factor."""
    # Three runs, 7 stages each → calls=21 but runs=3.
    for run_id in ("r-A", "r-B", "r-C"):
        for stage in ("screen", "research_bull", "research_bear",
                      "scenarios", "construct", "meta", "execute"):
            state.append_cost({
                "run_id": run_id, "stage": stage, "model": "claude-x",
                "cost_usd": 0.01, "at": state.utcnow_iso(),
            })
    assert dd.runs_count() == 3
    assert dd.total_token_cost()["calls"] == 21


def test_runs_count_empty(tmp_state):
    assert dd.runs_count() == 0


def test_cost_today_aggregates(tmp_state):
    state.append_cost({"run_id": "r1", "stage": "screen", "model": "m", "cost_usd": 0.10, "at": state.utcnow_iso()})
    state.append_cost({"run_id": "r2", "stage": "screen", "model": "m", "cost_usd": 0.20, "at": state.utcnow_iso()})
    assert dd.cost_today_usd() == pytest.approx(0.30)


def test_empty_portfolio_when_nothing_anywhere(tmp_state, monkeypatch):
    """If the fixture file is also missing, helper returns a coherent all-cash stub."""
    monkeypatch.setattr(dd, "SEED_PORTFOLIO_FALLBACK", tmp_state / "does-not-exist.json")
    p, src = dd.load_portfolio()
    assert src == "empty"
    assert p["all_cash"] is True
    assert p["positions"] == []


# ---------- token + cost aggregation ----------


def test_total_token_cost_empty(tmp_state):
    t = dd.total_token_cost()
    assert t["calls"] == 0
    assert t["total_tokens"] == 0
    assert t["cost_usd"] == 0.0


def test_total_token_cost_aggregates(tmp_state):
    state.append_cost({
        "run_id": "r1", "stage": "screen", "model": "claude-haiku-4-5",
        "cost_usd": 0.04, "input_tokens": 1000, "output_tokens": 200,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 800,
        "at": "2026-05-10T12:00:00Z",
    })
    state.append_cost({
        "run_id": "r2", "stage": "scenarios", "model": "claude-sonnet-4-6",
        "cost_usd": 0.18, "input_tokens": 500, "output_tokens": 1500,
        "cache_creation_input_tokens": 200, "cache_read_input_tokens": 0,
        "at": "2026-04-15T09:30:00Z",
    })
    t = dd.total_token_cost()
    assert t["calls"] == 2
    assert t["input_tokens"] == 1500
    assert t["output_tokens"] == 1700
    assert t["cache_creation_input_tokens"] == 200
    assert t["cache_read_input_tokens"] == 800
    assert t["total_tokens"] == 1500 + 1700 + 200 + 800
    assert t["cost_usd"] == pytest.approx(0.22)


def test_cost_by_month_buckets_correctly(tmp_state):
    rows = [
        ("2026-05-10T12:00:00Z", 0.10, 1000),
        ("2026-05-29T23:00:00Z", 0.20, 2000),
        ("2026-04-15T09:30:00Z", 0.30, 3000),
        ("2026-03-01T00:00:01Z", 0.05, 500),
    ]
    for at, cost, tokens in rows:
        state.append_cost({
            "run_id": "x", "stage": "s", "model": "m",
            "cost_usd": cost, "input_tokens": tokens,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "at": at,
        })
    by_month = dd.cost_by_month()
    assert [b["month"] for b in by_month] == ["2026-03", "2026-04", "2026-05"]
    may = next(b for b in by_month if b["month"] == "2026-05")
    assert may["calls"] == 2
    assert may["cost_usd"] == pytest.approx(0.30)
    assert may["total_tokens"] == 3000


def test_cost_by_month_empty(tmp_state):
    assert dd.cost_by_month() == []


# ---------- all-time cost reset display filter ----------


def test_total_token_cost_filters_post_all_time_reset(tmp_state):
    """After state.set_all_time_cost_reset, total_token_cost only counts rows
    AFTER the reset timestamp. Underlying costs.jsonl is unchanged."""
    state.append_cost({
        "run_id": "old", "stage": "x", "model": "m",
        "cost_usd": 0.50, "input_tokens": 100, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "at": "2026-05-10T12:00:00Z",
    })
    # Plant a reset marker AFTER the old row
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    state.append_cost({
        "run_id": "new", "stage": "y", "model": "m",
        "cost_usd": 0.10, "input_tokens": 50, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "at": "2026-05-12T08:00:00Z",
    })
    t = dd.total_token_cost()
    assert t["calls"] == 1, "should only count post-reset call"
    assert t["cost_usd"] == pytest.approx(0.10)
    assert t["input_tokens"] == 50
    # Audit log preserved
    assert state.COSTS_LOG.exists()
    raw_lines = state.COSTS_LOG.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 2


def test_cumulative_llm_cost_at_returns_running_sum_by_timestamp(tmp_state):
    """For each `at` queried, returns the sum of cost_usd across rows
    with `at` <= the query. Reset-aware via load_costs."""
    for at, c in [
        ("2026-05-10T12:00:00Z", 0.50),
        ("2026-05-11T12:00:00Z", 0.30),
        ("2026-05-12T12:00:00Z", 0.20),
    ]:
        state.append_cost({
            "run_id": "r", "stage": "s", "model": "m",
            "cost_usd": c, "at": at,
        })
    # Query a timestamp BEFORE all rows, BETWEEN rows, and AFTER all.
    out = dd.cumulative_llm_cost_at([
        "2026-05-10T00:00:00Z",  # before first row → 0
        "2026-05-11T13:00:00Z",  # after first two → 0.80
        "2026-05-15T00:00:00Z",  # after all three → 1.00
    ])
    assert out == [pytest.approx(0.0), pytest.approx(0.80), pytest.approx(1.00)]


def test_cumulative_llm_cost_at_honours_all_time_reset(tmp_state):
    """A reset marker zeros out pre-reset costs from the cumulative
    series so the equity-curve cumulative-net line bumps upward when
    the operator hits Reset."""
    state.append_cost({
        "run_id": "old", "stage": "s", "model": "m",
        "cost_usd": 0.50, "at": "2026-05-10T12:00:00Z",
    })
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    state.append_cost({
        "run_id": "new", "stage": "s", "model": "m",
        "cost_usd": 0.10, "at": "2026-05-12T08:00:00Z",
    })
    # Before reset: old row would have counted; with reset active, it's
    # excluded. The query timestamp here is after both rows.
    out = dd.cumulative_llm_cost_at(["2026-05-13T00:00:00Z"])
    assert out == [pytest.approx(0.10)]


def test_realised_llm_cost_attributed_to_trades_zero_when_no_trades(tmp_state):
    """No trades → no attribution. Helper returns 0.0 cleanly."""
    assert dd.realised_llm_cost_attributed_to_trades_usd() == pytest.approx(0.0)


def test_realised_llm_cost_drops_to_zero_after_all_time_reset(tmp_state):
    """Reset wiping costs.jsonl from the dashboard's view means trades
    get $0 attributed LLM cost — Net P&L surfaces that subtract this
    bump upward by exactly the reset amount. This is the core wiring
    that makes 'reset all costs' a meaningful net-P&L adjustment."""
    # One LLM cost row + one closed trade (buy then sell same symbol).
    state.append_cost({
        "run_id": "r1", "stage": "construct", "model": "m",
        "cost_usd": 0.40, "at": "2026-05-10T12:00:00Z",
    })
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 1, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-10T13:00:00Z", "run_id": "r1",
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 1, "fill_price": 82.0,
        "fees_usd": 0.0, "filled_at": "2026-05-11T13:00:00Z", "run_id": "r1",
    })
    pre = dd.realised_llm_cost_attributed_to_trades_usd()
    assert pre == pytest.approx(0.40), "trade should carry its run's LLM cost"

    state.set_all_time_cost_reset("test")
    post = dd.realised_llm_cost_attributed_to_trades_usd()
    assert post == pytest.approx(0.0), "reset zeroes the attributed cost"


def test_runs_count_filters_post_all_time_reset(tmp_state):
    """A reset wipes pre-reset run_ids from the displayed cycle count."""
    for at, rid in [
        ("2026-05-10T12:00:00Z", "old-1"),
        ("2026-05-10T14:00:00Z", "old-2"),
        ("2026-05-12T08:00:00Z", "new-1"),
    ]:
        state.append_cost({
            "run_id": rid, "stage": "s", "model": "m",
            "cost_usd": 0.10, "at": at,
        })
    assert dd.runs_count() == 3  # before reset
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    assert dd.runs_count() == 1  # only new-1 survives


def test_cost_by_month_filters_post_all_time_reset(tmp_state):
    """Monthly breakdown also honours the all-time reset."""
    state.append_cost({
        "run_id": "x", "stage": "s", "model": "m",
        "cost_usd": 0.30, "at": "2026-04-15T09:30:00Z",
    })
    state.append_cost({
        "run_id": "y", "stage": "s", "model": "m",
        "cost_usd": 0.10, "at": "2026-05-12T08:00:00Z",
    })
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    by_month = dd.cost_by_month()
    assert [b["month"] for b in by_month] == ["2026-05"]
    assert by_month[0]["cost_usd"] == pytest.approx(0.10)


def test_cost_for_run_usd_filters_post_all_time_reset(tmp_state):
    """A reset zeros an in-flight run's displayed cost so the "this run"
    meter on the dashboard matches user expectation, but the raw log
    used by cap enforcement (state.read_costs_for_run) is preserved."""
    state.append_cost({
        "run_id": "in-flight", "stage": "screen", "model": "m",
        "cost_usd": 0.40, "at": "2026-05-12T08:00:00Z",
    })
    assert dd.cost_for_run_usd("in-flight") == pytest.approx(0.40)
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-12T09:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    # Display: zero
    assert dd.cost_for_run_usd("in-flight") == pytest.approx(0.0)
    # Raw log (cap enforcement path): preserved
    rows = state.read_costs_for_run("in-flight")
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(0.40)


def test_try_load_broker_marks_no_keys_returns_empty(tmp_state, monkeypatch):
    """Without ALPACA_API_KEY / SECRET in env, AlpacaBroker init raises and
    the dashboard helper must absorb the failure so the page still renders."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert dd.try_load_broker_marks() == {}


def test_apply_nav_offset_noop_when_offset_zero(tmp_state):
    rows = [
        {"nav_usd": 100020.0, "nav_source": "broker"},
        {"nav_usd": 2500.0, "nav_source": "virtual"},
    ]
    out = dd.apply_nav_offset_to_history(rows, nav_offset_usd=0.0)
    assert out is rows or out == rows


def test_apply_nav_offset_subtracts_from_broker_rows_only(tmp_state):
    """Operator's real-world scenario: anchor offset is $97,527, the
    orchestrator wrote a broker-unit row at $100,020 AND later a
    virtual-unit row at $2,500. Subtracting the offset from the
    virtual row would land it at -$95,027 (the exact bug from the
    deployed dashboard). Helper must skip the virtual row."""
    rows = [
        {"nav_usd": 100020.0, "nav_source": "broker"},
        {"nav_usd": 2500.0, "nav_source": "virtual"},
    ]
    out = dd.apply_nav_offset_to_history(
        rows, nav_offset_usd=97527.0, virtual_baseline_usd=2500.0,
    )
    assert out[0]["nav_usd"] == pytest.approx(2493.0)
    assert out[1]["nav_usd"] == pytest.approx(2500.0)
    # And the original list is not mutated.
    assert rows[0]["nav_usd"] == 100020.0
    assert rows[1]["nav_usd"] == 2500.0


def test_apply_nav_offset_legacy_row_heuristic(tmp_state):
    """Rows without nav_source stamp fall back to value-based detection.
    Anything within 10× of the virtual baseline is treated as virtual;
    larger values look like raw broker."""
    rows = [
        {"nav_usd": 100020.0},  # no stamp → broker-units by magnitude
        {"nav_usd": 2500.0},    # no stamp → virtual by magnitude
        {"nav_usd": 12000.0},   # below 10× virtual ($25k) → virtual
        {"nav_usd": 50000.0},   # above 10× virtual → broker
    ]
    out = dd.apply_nav_offset_to_history(
        rows, nav_offset_usd=97527.0, virtual_baseline_usd=2500.0,
    )
    assert out[0]["nav_usd"] == pytest.approx(2493.0)   # broker → subtract
    assert out[1]["nav_usd"] == pytest.approx(2500.0)   # virtual → keep
    assert out[2]["nav_usd"] == pytest.approx(12000.0)  # below threshold → virtual
    assert out[3]["nav_usd"] == pytest.approx(-47527.0) # above threshold → broker (subtract; goes negative — by design, operator should re-anchor)


def test_apply_nav_offset_skips_non_numeric_nav(tmp_state):
    """Defensive: a malformed row with non-numeric nav_usd shouldn't
    crash the chart."""
    rows = [{"nav_usd": None}, {"nav_usd": "oops"}, {"nav_usd": 100020.0, "nav_source": "broker"}]
    out = dd.apply_nav_offset_to_history(
        rows, nav_offset_usd=97527.0, virtual_baseline_usd=2500.0,
    )
    assert out[0]["nav_usd"] is None
    assert out[1]["nav_usd"] == "oops"
    assert out[2]["nav_usd"] == pytest.approx(2493.0)


def test_load_nav_history_round_trip(tmp_state):
    assert dd.load_nav_history() == []
    state.append_nav({"run_id": "r1", "at": state.utcnow_iso(), "nav_usd": 2500.0})
    state.append_nav({"run_id": "r2", "at": state.utcnow_iso(), "nav_usd": 2520.0})
    hist = dd.load_nav_history()
    assert len(hist) == 2
    assert hist[1]["nav_usd"] == 2520.0


# ---------- trading fees over time ----------


def _fill(**over):
    base = dict(
        activity_id="a1",
        alpaca_order_id="o1",
        symbol="TQQQ",
        kind="etf",
        side="buy",
        qty=10,
        fill_price=70.0,
        fees_usd=0.10,
        filled_at="2026-05-12T14:30:00Z",
        run_id="run-A",
    )
    base.update(over)
    return base


def test_total_trading_fees_empty(tmp_state):
    assert dd.total_trading_fees_usd() == 0.0


def test_total_trading_fees_sums_all_fills(tmp_state):
    state.append_trade(_fill(activity_id="a1", fees_usd=0.10))
    state.append_trade(_fill(activity_id="a2", side="sell", fees_usd=0.20))
    state.append_trade(_fill(activity_id="a3", fees_usd=0.65, kind="option"))
    assert dd.total_trading_fees_usd() == pytest.approx(0.95)


def test_fees_by_month_buckets_by_filled_at(tmp_state):
    state.append_trade(_fill(activity_id="a1", fees_usd=0.10,
                              filled_at="2026-04-15T09:30:00Z"))
    state.append_trade(_fill(activity_id="a2", fees_usd=0.20,
                              filled_at="2026-05-12T14:30:00Z"))
    state.append_trade(_fill(activity_id="a3", fees_usd=0.30,
                              filled_at="2026-05-29T23:00:00Z"))
    by_month = dd.fees_by_month()
    assert [b["month"] for b in by_month] == ["2026-04", "2026-05"]
    may = next(b for b in by_month if b["month"] == "2026-05")
    assert may["fills"] == 2
    assert may["fees_usd"] == pytest.approx(0.50)


def test_fees_by_month_empty(tmp_state):
    assert dd.fees_by_month() == []


def test_fees_running_total_sorts_and_accumulates(tmp_state):
    """Powers the cumulative-fees chart. Output must be in chronological
    order regardless of append order, with monotonically increasing
    cum_fees_usd."""
    # Append OUT of chronological order to verify the sort.
    state.append_trade(_fill(activity_id="a3", fees_usd=0.30,
                              filled_at="2026-05-12T14:30:00Z"))
    state.append_trade(_fill(activity_id="a1", fees_usd=0.10,
                              filled_at="2026-05-10T09:30:00Z"))
    state.append_trade(_fill(activity_id="a2", fees_usd=0.20,
                              filled_at="2026-05-11T12:00:00Z"))
    out = dd.fees_running_total()
    assert [r["at"] for r in out] == [
        "2026-05-10T09:30:00Z",
        "2026-05-11T12:00:00Z",
        "2026-05-12T14:30:00Z",
    ]
    assert [r["cum_fees_usd"] for r in out] == pytest.approx([0.10, 0.30, 0.60])
    assert [r["fees_usd"] for r in out] == pytest.approx([0.10, 0.20, 0.30])


def test_fees_running_total_empty(tmp_state):
    assert dd.fees_running_total() == []


def test_fees_helpers_read_trades_not_costs(tmp_state):
    """Trading-fee helpers must source from trades.jsonl, not costs.jsonl.
    Sanity check: appending a cost row must NOT show up as a trading fee.
    (PR #53's all-time-cost reset filter applies to costs only; this is
    the architectural reason it cannot affect fees.)"""
    state.append_cost({
        "run_id": "r1", "stage": "screen", "model": "m",
        "cost_usd": 0.50, "at": "2026-05-12T08:00:00Z",
    })
    assert dd.total_trading_fees_usd() == 0.0  # no trades.jsonl entries
    state.append_trade(_fill(activity_id="a1", fees_usd=0.10))
    assert dd.total_trading_fees_usd() == pytest.approx(0.10)
    # The $0.50 cost row never crosses over.


# ---------- trades_pnl_view (PR 3 dashboard data) ----------


def test_trades_pnl_view_empty(tmp_state):
    """No trades on disk → empty closed/open lists, zero totals."""
    v = dd.trades_pnl_view()
    assert v["closed"] == []
    assert v["open"] == []
    assert v["totals"]["closed_count"] == 0
    assert v["totals"]["open_count"] == 0
    assert v["totals"]["realised_net_usd"] == 0.0


def test_trades_pnl_view_closed_round_trip_etf(tmp_state):
    """Buy 10 TQQQ @ $70 (fees $0.10), sell @ $75 (fees $0.10), run cost
    $0.40 on a run that opened 1 position → per-trade LLM cost $0.40.
    Gross = $50, fees = $0.20, LLM = $0.40, net = $49.40."""
    state.append_cost({
        "run_id": "r1", "stage": "screen", "model": "m",
        "cost_usd": 0.40, "at": "2026-05-12T08:00:00Z",
    })
    state.append_trade(_fill(activity_id="b", symbol="TQQQ", side="buy",
                              qty=10, fill_price=70.0, fees_usd=0.10,
                              run_id="r1"))
    state.append_trade(_fill(activity_id="s", symbol="TQQQ", side="sell",
                              qty=10, fill_price=75.0, fees_usd=0.10,
                              run_id="r2", filled_at="2026-05-13T14:00:00Z"))
    v = dd.trades_pnl_view()
    assert len(v["closed"]) == 1
    c = v["closed"][0]
    assert c["symbol"] == "TQQQ"
    assert c["gross_pnl_usd"] == pytest.approx(50.0)
    assert c["fees_usd"] == pytest.approx(0.20)
    assert c["llm_cost_usd"] == pytest.approx(0.40)
    assert c["net_pnl_usd"] == pytest.approx(49.40)
    assert c["buy_run_id"] == "r1"

    t = v["totals"]
    assert t["closed_count"] == 1
    assert t["open_count"] == 0
    assert t["realised_net_usd"] == pytest.approx(49.40)
    assert t["realised_fees_usd"] == pytest.approx(0.20)


def test_trades_pnl_view_open_lot_uses_marks(tmp_state):
    """Open lot with no sell yet: gross/net computed against passed marks dict."""
    state.append_trade(_fill(activity_id="b", symbol="TQQQ", side="buy",
                              qty=10, fill_price=70.0, fees_usd=0.10,
                              run_id="r1"))
    v = dd.trades_pnl_view(marks={"TQQQ": 80.0})
    assert v["closed"] == []
    assert len(v["open"]) == 1
    o = v["open"][0]
    assert o["mark"] == 80.0
    # ($80 - $70) * 10 = $100
    assert o["gross_pnl_usd"] == pytest.approx(100.0)


def test_trades_pnl_view_honours_all_time_cost_reset(tmp_state):
    """All-time cost reset (PR #53) → LLM column zeros even though cost
    rows still exist on disk. Fees stay as paid."""
    state.append_cost({
        "run_id": "r1", "stage": "x", "model": "m",
        "cost_usd": 0.40, "at": "2026-05-10T08:00:00Z",
    })
    state.append_trade(_fill(activity_id="b", symbol="TQQQ", side="buy",
                              qty=10, fill_price=70.0, fees_usd=0.10,
                              run_id="r1"))
    state.append_trade(_fill(activity_id="s", symbol="TQQQ", side="sell",
                              qty=10, fill_price=75.0, fees_usd=0.10,
                              run_id="r2", filled_at="2026-05-13T14:00:00Z"))
    # Plant a reset AFTER the cost row's timestamp.
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.ALL_TIME_COST_RESET_FLAG.write_text(
        '{"at": "2026-05-11T00:00:00Z", "reason": "test"}',
        encoding="utf-8",
    )
    v = dd.trades_pnl_view()
    c = v["closed"][0]
    # LLM column zeroed by the reset; fees unaffected.
    assert c["llm_cost_usd"] == 0.0
    assert c["fees_usd"] == pytest.approx(0.20)
    # Net should now equal gross - fees (no LLM cost).
    assert c["net_pnl_usd"] == pytest.approx(50.0 - 0.20)


# --------------------------------------------------------------------------- #
# Option funnel — diagnostic added 2026-05-22 after six months of paper
# trading produced zero option positions. Surfaces where in the pipeline
# option candidates die.
# --------------------------------------------------------------------------- #


def _seed_run(rid: str, *, view=None, chain=None, portfolio=None,
              sanity=None, next_run=None):
    """Write a minimal per-cycle run dir under state/runs/<rid>/.
    Any artifact arg left None is simply not written — mirrors the
    real orchestrator's behaviour when a stage is skipped or aborted.
    """
    run_dir = state.RUNS_DIR / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    if view is not None:
        state.write_json(run_dir / "view.json", view)
    if chain is not None:
        state.write_json(run_dir / "chain_lookups.json", chain)
    if portfolio is not None:
        state.write_json(run_dir / "portfolio.json", portfolio)
    if sanity is not None:
        state.write_json(run_dir / "sanity.json", sanity)
    if next_run is not None:
        state.write_json(run_dir / "next_run.json", next_run)


def test_option_funnel_empty_when_no_runs(tmp_state):
    assert dd.option_funnel() == []


def test_option_funnel_counts_surfaced_and_chain_ok(tmp_state):
    """Strategist surfaces 2 option candidates; chain resolves both."""
    _seed_run(
        "20260522T120000Z-aaaaaa",
        view={
            "regime": "trending_up",
            "candidates": [
                {"symbol": "SPY", "instrument_kind": "option_call", "confidence": 0.75,
                 "thesis": "x"},
                {"symbol": "QQQ", "instrument_kind": "option_call", "confidence": 0.70,
                 "thesis": "x"},
                {"symbol": "TQQQ", "instrument_kind": "etf", "confidence": 0.65,
                 "thesis": "x"},  # ETF, not counted
            ],
        },
        chain={
            "lookups": [
                {"candidate": {"symbol": "SPY", "instrument_kind": "option_call"},
                 "contract": {"osi_symbol": "SPY...", "strike": 540.0},
                 "error": None},
                {"candidate": {"symbol": "QQQ", "instrument_kind": "option_call"},
                 "contract": {"osi_symbol": "QQQ...", "strike": 500.0},
                 "error": None},
            ],
        },
        portfolio={"all_cash": False, "positions": [
            {"kind": "etf", "symbol": "TQQQ", "shares": 5},
        ]},
    )
    rows = dd.option_funnel()
    assert len(rows) == 1
    r = rows[0]
    assert r["surfaced"] == 2
    assert r["chain_ok"] == 2
    assert r["taken"] == 0  # constructor took the ETF, dropped both options
    assert r["regime"] == "trending_up"
    assert r["took_anything"] is True


def test_option_funnel_records_drop_when_chain_returns_null_contract(tmp_state):
    """Strategist surfaces 1 option but Alpaca returns no tradable contract."""
    _seed_run(
        "20260522T130000Z-bbbbbb",
        view={"regime": "neutral", "candidates": [
            {"symbol": "TLT", "instrument_kind": "option_put", "confidence": 0.65,
             "thesis": "x"},
        ]},
        chain={"lookups": [
            {"candidate": {"symbol": "TLT", "instrument_kind": "option_put"},
             "contract": None,
             "error": "no tradable OTM contract found"},
        ]},
        portfolio={"all_cash": True, "positions": []},
    )
    r = dd.option_funnel()[0]
    assert r["surfaced"] == 1
    assert r["chain_ok"] == 0
    assert r["taken"] == 0
    assert r["all_cash"] is True


def test_option_funnel_counts_taken_and_submitted(tmp_state):
    """Full happy path: surfaced → chain → taken → sanity → submitted."""
    _seed_run(
        "20260522T140000Z-cccccc",
        view={"candidates": [
            {"symbol": "IWM", "instrument_kind": "option_call", "confidence": 0.80,
             "thesis": "x"},
        ]},
        chain={"lookups": [
            {"candidate": {"symbol": "IWM", "instrument_kind": "option_call"},
             "contract": {"osi_symbol": "IWM260619C00220000"},
             "error": None},
        ]},
        portfolio={"all_cash": False, "positions": [
            {"kind": "option", "underlying": "IWM", "type": "call",
             "strike": 220.0, "expiry": "2026-06-19", "dte": 28, "contracts": 1,
             "premium_paid": 2.50, "position_pct": 10.0},
        ]},
        sanity={"status": "pass", "rules": []},
        next_run={
            "order_plan": {
                "results": [
                    {"symbol": "IWM260619C00220000", "qty": 1, "side": "buy",
                     "status": "accepted", "broker_order_id": "ord-x"},
                ],
            },
        },
    )
    r = dd.option_funnel()[0]
    assert r["surfaced"] == 1
    assert r["chain_ok"] == 1
    assert r["taken"] == 1
    assert r["sanity_pass"] is True
    assert r["submitted"] == 1


def test_option_funnel_does_not_count_skipped_orders(tmp_state):
    """A 'skipped: option contract not tradable at broker' result is in
    the plan but didn't actually submit — must not bump the submitted
    count or the dashboard will overstate broker activity."""
    _seed_run(
        "20260522T150000Z-dddddd",
        view={"candidates": [
            {"symbol": "GLD", "instrument_kind": "option_call", "confidence": 0.7,
             "thesis": "x"},
        ]},
        chain={"lookups": [
            {"candidate": {"symbol": "GLD", "instrument_kind": "option_call"},
             "contract": {"osi_symbol": "GLD260619C00250000"},
             "error": None},
        ]},
        portfolio={"all_cash": False, "positions": [
            {"kind": "option", "underlying": "GLD", "type": "call",
             "strike": 250.0, "expiry": "2026-06-19", "dte": 28, "contracts": 1,
             "premium_paid": 1.20, "position_pct": 5.0},
        ]},
        sanity={"status": "pass"},
        next_run={
            "order_plan": {
                "results": [
                    {"symbol": "GLD260619C00250000", "qty": 1, "side": "buy",
                     "status": "skipped: option contract not tradable at broker",
                     "broker_order_id": ""},
                ],
            },
        },
    )
    r = dd.option_funnel()[0]
    assert r["taken"] == 1
    assert r["submitted"] == 0


def test_option_funnel_orders_newest_first(tmp_state):
    """Multiple runs come back newest-first so the dashboard renders them
    in the order an operator scans (top = most recent)."""
    _seed_run("20260520T120000Z-old001",
              view={"candidates": []}, portfolio={"all_cash": True, "positions": []})
    _seed_run("20260522T120000Z-new002",
              view={"candidates": [
                  {"symbol": "SPY", "instrument_kind": "option_call",
                   "confidence": 0.8, "thesis": "x"},
              ]}, portfolio={"all_cash": True, "positions": []})
    rows = dd.option_funnel()
    assert [r["run_id"] for r in rows] == [
        "20260522T120000Z-new002", "20260520T120000Z-old001",
    ]
    assert rows[0]["surfaced"] == 1
    assert rows[1]["surfaced"] == 0
