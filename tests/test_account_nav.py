"""Tests for orchestrator._account_nav — synthetic-NAV sizing (Phase 3).

Alpaca paper accounts ship with $100k of fake equity. CLAUDE.md sizes for a
$2,500 experimental account, so the agent sizes against the SYNTHETIC balance
($2,500 baseline, VIRTUAL_NAV_USD-overridable, + realized P&L) and NEVER the
broker's ~$100k equity. Phase 3 removed the broker-equity fallback entirely so
a missing/garbled env var can't make the agent size ~40× too large.

These tests use tmp_state so the synthetic balance reads empty logs (→ baseline).
"""
from __future__ import annotations

import orchestrator
from lib import state
from lib.broker import Account


class _FakeBroker:
    def __init__(self, equity_usd: float):
        self._equity = equity_usd

    @property
    def name(self): return "fake"

    def get_account(self):
        return Account(
            cash_usd=self._equity, equity_usd=self._equity,
            buying_power_usd=self._equity * 2, is_paper=True,
        )

    def get_positions(self): return []
    def submit_order(self, *a, **kw): raise NotImplementedError
    def cancel_all(self): return 0
    def flatten(self, sym): return None


def _ctx(broker=None) -> orchestrator.StageContext:
    return orchestrator.StageContext(run_id="t", dry_run=True, broker=broker)


def test_account_nav_uses_virtual_override_as_baseline(tmp_state, monkeypatch):
    """VIRTUAL_NAV_USD is the synthetic baseline; with no trades, NAV == it,
    regardless of broker-reported equity."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 2500.0


def test_account_nav_override_baseline_even_for_low_value(tmp_state, monkeypatch):
    monkeypatch.setenv("VIRTUAL_NAV_USD", "1500.50")
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 1500.50


def test_account_nav_never_uses_broker_equity(tmp_state, monkeypatch):
    """Phase 3: broker equity ($100k paper) must NEVER leak into sizing, even
    with no VIRTUAL_NAV_USD set. Falls to the $2,500 synthetic baseline."""
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    broker = _FakeBroker(equity_usd=100_000)
    assert orchestrator._account_nav(_ctx(broker)) == 2500.0


def test_account_nav_falls_back_to_baseline_when_no_broker(tmp_state, monkeypatch):
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    assert orchestrator._account_nav(_ctx(None)) == 2500.0


def test_account_nav_ignores_unparseable_override(tmp_state, monkeypatch):
    """Garbage override must not crash — fall through to the $2,500 baseline."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "not-a-number")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    assert orchestrator._account_nav(_ctx(None)) == 2500.0


def test_account_nav_tracks_realized_pnl(tmp_state, monkeypatch):
    """Phase 3: a banked round-trip lifts the synthetic NAV the agent sizes
    against. Buy TQQQ ×4 @ $70, sell @ $80 → +$40 realized → NAV $2,540."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")
    state.append_trade({
        "activity_id": "a1", "alpaca_order_id": "o1", "symbol": "TQQQ",
        "kind": "etf", "side": "buy", "qty": 4, "fill_price": 70.0,
        "fees_usd": 0.0, "filled_at": "2026-05-28T14:00:00Z",
    })
    state.append_trade({
        "activity_id": "a2", "alpaca_order_id": "o2", "symbol": "TQQQ",
        "kind": "etf", "side": "sell", "qty": 4, "fill_price": 80.0,
        "fees_usd": 0.0, "filled_at": "2026-05-28T15:00:00Z",
    })
    assert orchestrator._account_nav(_ctx(None)) == 2540.0


def test_account_nav_uses_raw_llm_cost_ignoring_display_reset(tmp_state, monkeypatch):
    """Codex P2 (#98): sizing NAV must use the RAW cost audit log, so clicking
    'Reset all LLM costs' (a display-only marker) does not inflate the capital
    the agent sizes against."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500")
    state.append_cost({
        "run_id": "r", "stage": "construct", "model": "m",
        "cost_usd": 50.0, "at": state.utcnow_iso(),
    })
    assert orchestrator._account_nav(_ctx(None)) == 2450.0  # 2500 − 50 raw cost
    state.set_all_time_cost_reset()  # hide costs in the dashboard display
    assert orchestrator._account_nav(_ctx(None)) == 2450.0  # unchanged — raw, not display


def test_parsed_virtual_nav_override_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    assert orchestrator._parsed_virtual_nav_override() is None


def test_parsed_virtual_nav_override_returns_none_when_garbage(monkeypatch):
    """Codex P1 on PR #76: misconfigured env var ('not-a-number',
    empty string after parsing) must return None so the orchestrator
    stamps nav_source='broker' on the row rather than mis-flagging
    a broker-units row as virtual."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "not-a-number")
    assert orchestrator._parsed_virtual_nav_override() is None


def test_parsed_virtual_nav_override_returns_float_when_valid(monkeypatch):
    monkeypatch.setenv("VIRTUAL_NAV_USD", "2500.0")
    assert orchestrator._parsed_virtual_nav_override() == 2500.0


def test_parsed_virtual_nav_override_handles_whitespace_negative(monkeypatch):
    """Edge cases: leading/trailing whitespace parses fine, negative
    numbers also parse (operator might want to stress-test a negative
    NAV scenario). The helper just reports parseability."""
    monkeypatch.setenv("VIRTUAL_NAV_USD", "  -500  ")
    assert orchestrator._parsed_virtual_nav_override() == -500.0


# ---------- live sizing path (fail closed; inert on paper) ----------


import pytest  # noqa: E402


class _LiveBroker:
    """Stub of a genuinely live broker (triple lock notionally raised)."""

    is_paper = False

    def __init__(self, equity_usd=12345.0, fail=False, account_is_paper=False):
        self._equity = equity_usd
        self._fail = fail
        self._account_is_paper = account_is_paper
        self.get_account_calls = 0

    @property
    def name(self): return "fake-live"

    def get_account(self):
        self.get_account_calls += 1
        if self._fail:
            raise RuntimeError("alpaca down")
        return Account(
            cash_usd=self._equity, equity_usd=self._equity,
            buying_power_usd=self._equity, is_paper=self._account_is_paper,
        )

    def get_positions(self): return []
    def submit_order(self, *a, **kw): raise NotImplementedError
    def cancel_all(self): return 0
    def flatten(self, sym): return None


def _live_ctx(broker) -> orchestrator.StageContext:
    return orchestrator.StageContext(run_id="t", dry_run=False, broker=broker)


def test_live_nav_returns_real_equity(tmp_state, monkeypatch):
    monkeypatch.delenv("LIVE_NAV_CAP_USD", raising=False)
    broker = _LiveBroker(equity_usd=12345.0)
    assert orchestrator._account_nav(_live_ctx(broker)) == 12345.0


def test_live_nav_capped_by_env(tmp_state, monkeypatch):
    monkeypatch.setenv("LIVE_NAV_CAP_USD", "2500")
    broker = _LiveBroker(equity_usd=12345.0)
    assert orchestrator._account_nav(_live_ctx(broker)) == 2500.0


def test_live_nav_cap_above_equity_is_noop(tmp_state, monkeypatch):
    monkeypatch.setenv("LIVE_NAV_CAP_USD", "50000")
    broker = _LiveBroker(equity_usd=12345.0)
    assert orchestrator._account_nav(_live_ctx(broker)) == 12345.0


def test_live_nav_read_failure_raises_never_2500(tmp_state, monkeypatch):
    """The live path has NO fallback: a failed equity read must raise, not
    silently size against the paper baseline."""
    monkeypatch.delenv("LIVE_NAV_CAP_USD", raising=False)
    broker = _LiveBroker(fail=True)
    with pytest.raises(orchestrator.LiveNavUnavailable):
        orchestrator._account_nav(_live_ctx(broker))


def test_live_nav_malformed_cap_fails_closed(tmp_state, monkeypatch):
    monkeypatch.setenv("LIVE_NAV_CAP_USD", "not-a-number")
    broker = _LiveBroker(equity_usd=12345.0)
    with pytest.raises(orchestrator.LiveNavUnavailable):
        orchestrator._account_nav(_live_ctx(broker))
    monkeypatch.setenv("LIVE_NAV_CAP_USD", "-100")
    with pytest.raises(orchestrator.LiveNavUnavailable):
        orchestrator._account_nav(_live_ctx(broker))


def test_live_nav_invalid_equity_fails_closed(tmp_state, monkeypatch):
    monkeypatch.delenv("LIVE_NAV_CAP_USD", raising=False)
    broker = _LiveBroker(equity_usd=0.0)
    with pytest.raises(orchestrator.LiveNavUnavailable):
        orchestrator._account_nav(_live_ctx(broker))


def test_live_nav_paper_account_on_live_path_fails_closed(tmp_state, monkeypatch):
    """Belt and braces: a broker claiming live whose account says paper is a
    misconfiguration, not something to size against."""
    monkeypatch.delenv("LIVE_NAV_CAP_USD", raising=False)
    broker = _LiveBroker(account_is_paper=True)
    with pytest.raises(orchestrator.LiveNavUnavailable):
        orchestrator._account_nav(_live_ctx(broker))


def test_live_nav_writes_transition_marker_once(tmp_state, monkeypatch):
    monkeypatch.setenv("LIVE_NAV_CAP_USD", "2500")
    broker = _LiveBroker(equity_usd=2612.34)
    orchestrator._account_nav(_live_ctx(broker))
    marker = state.read_live_transition()
    assert marker["live_starting_equity_usd"] == 2612.34
    assert marker["nav_cap_usd"] == 2500.0
    # A later, richer account must not overwrite the recorded start.
    orchestrator._account_nav(_live_ctx(_LiveBroker(equity_usd=9999.0)))
    assert state.read_live_transition() == marker


def test_live_nav_cached_on_ctx_single_broker_call(tmp_state, monkeypatch):
    monkeypatch.delenv("LIVE_NAV_CAP_USD", raising=False)
    broker = _LiveBroker(equity_usd=12345.0)
    ctx = _live_ctx(broker)
    assert orchestrator._account_nav(ctx) == 12345.0
    assert orchestrator._account_nav(ctx) == 12345.0
    assert broker.get_account_calls == 1


def test_paper_broker_never_hits_live_path(tmp_state, monkeypatch):
    """A paper broker (is_paper=True) on a non-dry-run ctx stays on the
    synthetic path and never calls get_account."""
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)

    class _PaperNeverCalled:
        is_paper = True

        def get_account(self):
            raise AssertionError("paper path must not read broker equity")

    assert orchestrator._account_nav(_live_ctx(_PaperNeverCalled())) == 2500.0


def test_dry_run_never_hits_live_path(tmp_state, monkeypatch):
    """Dry-run with a live-shaped broker still sizes synthetically."""
    monkeypatch.delenv("VIRTUAL_NAV_USD", raising=False)
    broker = _LiveBroker(equity_usd=12345.0)
    ctx = orchestrator.StageContext(run_id="t", dry_run=True, broker=broker)
    assert orchestrator._account_nav(ctx) == 2500.0
    assert broker.get_account_calls == 0


def test_run_pipeline_skips_cycle_when_live_nav_unavailable(tmp_state, monkeypatch):
    """Fail-closed prefetch: a live broker whose equity read fails skips the
    ENTIRE cycle — no orders, no LLM calls — and leaves a retry-soon
    next_run.json plus an audited decision row."""
    broker = _LiveBroker(fail=True)
    out = orchestrator.run_pipeline(dry_run=False, run_id="livefail", broker=broker)
    assert out["live_nav_unavailable"] is True
    nr = out["next_run"]
    assert nr["live_nav_unavailable"] is True
    assert "live NAV unavailable" in nr["rationale"]
    rows = [
        __import__("json").loads(line)
        for line in state.DECISIONS_LOG.read_text().splitlines() if line.strip()
    ]
    assert rows[-1]["status"] == "skipped_live_nav_unavailable"
    assert rows[-1]["stage"] == "live_nav_prefetch"
    assert rows[-1]["cost_usd"] == 0.0


def test_run_pipeline_fails_closed_when_env_live_but_broker_missing(tmp_state, monkeypatch):
    """Codex P1 (PR #112): full env lock raised (env flag + LIVE_VERSION +
    live base URL) but broker construction failed (broker=None) must be
    treated as live-NAV-unavailable — never the no-broker paper path."""
    from lib import live_gate
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setattr(live_gate, "LIVE_VERSION", 1)
    out = orchestrator.run_pipeline(dry_run=False, run_id="nobroker", broker=None)
    assert out["live_nav_unavailable"] is True


def test_peak_nav_30d_scoped_to_mode(tmp_state, monkeypatch):
    """Codex P1 (PR #112): the 30d peak must not mix paper-scale and
    live-scale rows — each era sees only its own peak (live cold-starts
    at 0.0 → full base cap)."""
    from datetime import datetime, timezone
    monkeypatch.setattr(
        state, "utcnow",
        lambda: datetime(2026, 7, 2, 14, 0, 0, tzinfo=timezone.utc),
    )
    state.append_nav({"run_id": "p", "at": "2026-07-01T14:00:00Z",
                      "nav_usd": 2800.0, "mode": "paper"})
    state.append_nav({"run_id": "l", "at": "2026-07-02T10:00:00Z",
                      "nav_usd": 5000.0, "mode": "live"})
    assert orchestrator._peak_nav_30d(mode="paper") == 2800.0
    assert orchestrator._peak_nav_30d(mode="live") == 5000.0
    assert orchestrator._peak_nav_30d() == 2800.0  # default = paper


def test_recent_pnl_history_scoped_to_mode(tmp_state):
    """Codex P1 (PR #112): cycle-over-cycle PnL % must never pair a paper
    row with a live row (a $2.5k→$5k boundary pair would read as +100%)."""
    state.append_nav({"run_id": "p1", "at": "2026-07-01T14:00:00Z",
                      "nav_usd": 2500.0, "mode": "paper"})
    state.append_nav({"run_id": "p2", "at": "2026-07-01T18:00:00Z",
                      "nav_usd": 2550.0, "mode": "paper"})
    state.append_nav({"run_id": "l1", "at": "2026-07-02T14:00:00Z",
                      "nav_usd": 5000.0, "mode": "live"})
    paper = orchestrator._recent_pnl_history(limit=5, mode="paper")
    assert len(paper) == 1 and paper[0]["realized_pnl_pct"] == 2.0
    # One live row → no pair yet → empty, NOT a +96% paper→live jump.
    assert orchestrator._recent_pnl_history(limit=5, mode="live") == []


def test_live_nav_cap_debits_losses_since_transition(tmp_state, monkeypatch):
    """Codex P1 (PR #112): with the account funded above the cap, losses
    must debit the allocation immediately — min(equity, cap) would stay
    pinned at the cap until total equity fell below it."""
    monkeypatch.setenv("LIVE_NAV_CAP_USD", "2500")
    state.write_live_transition_once(
        live_starting_equity_usd=5000.0, nav_cap_usd=2500.0,
        run_id="r0", live_version=1,
    )
    # $1k loss on a $5k account: allocation = 2500 − 1000 = 1500, NOT 2500.
    assert orchestrator._account_nav(_live_ctx(_LiveBroker(equity_usd=4000.0))) == 1500.0
    # Profits compound the allocation (like the paper synthetic balance).
    assert orchestrator._account_nav(_live_ctx(_LiveBroker(equity_usd=6000.0))) == 3500.0


def test_live_nav_cap_exhausted_allocation_fails_closed(tmp_state, monkeypatch):
    monkeypatch.setenv("LIVE_NAV_CAP_USD", "2500")
    state.write_live_transition_once(
        live_starting_equity_usd=5000.0, nav_cap_usd=2500.0,
        run_id="r0", live_version=1,
    )
    # Equity down to the deposit-minus-allocation floor: allocation ≤ 0.
    with pytest.raises(orchestrator.LiveNavUnavailable):
        orchestrator._account_nav(_live_ctx(_LiveBroker(equity_usd=2500.0)))
