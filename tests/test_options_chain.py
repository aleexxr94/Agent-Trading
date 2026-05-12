"""Tests for the option-chain fetcher introduced in Phase 9b.

Pure functions (parse_osi, snapshots_from_alpaca_chain, filter_chain,
summarise_chain) are exercised directly with hand-built fake Alpaca
response objects. ChainFetcher is tested with an injected client.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pytest

from lib import options_chain as oc


# ---------- parse_osi ----------


@pytest.mark.parametrize("osi,expected", [
    ("SPY260619C00530000", ("SPY", "2026-06-19", "call", 530.0)),
    ("TLT260619P00088000", ("TLT", "2026-06-19", "put", 88.0)),
    ("QQQ260918P00400500", ("QQQ", "2026-09-18", "put", 400.5)),
    ("GOOGL260619C00150000", ("GOOGL", "2026-06-19", "call", 150.0)),
])
def test_parse_osi_decodes_real_osis(osi, expected):
    assert oc.parse_osi(osi) == expected


@pytest.mark.parametrize("bad", [
    "SPY", "spy260619C00530000", "SPY26C00530000",
    "SPY260619X00530000", "",
])
def test_parse_osi_rejects_garbage(bad):
    with pytest.raises(ValueError):
        oc.parse_osi(bad)


# ---------- snapshots_from_alpaca_chain ----------


def _quote(*, bid, ask):
    return SimpleNamespace(bid_price=bid, ask_price=ask)


def _greeks(*, delta=0.30, gamma=0.02, theta=-0.04, vega=0.18):
    return SimpleNamespace(delta=delta, gamma=gamma, theta=theta, vega=vega)


def _snap(*, latest_quote=None, greeks=None, iv=None):
    return SimpleNamespace(latest_quote=latest_quote, greeks=greeks, implied_volatility=iv)


def test_snapshots_normalise_basic_chain():
    today = date(2026, 5, 12)
    raw = {
        "SPY260619C00530000": _snap(
            latest_quote=_quote(bid=8.10, ask=8.30),
            greeks=_greeks(delta=0.52),
            iv=0.18,
        ),
        "SPY260619P00510000": _snap(
            latest_quote=_quote(bid=4.20, ask=4.40),
            greeks=_greeks(delta=-0.32),
            iv=0.21,
        ),
    }
    out = oc.snapshots_from_alpaca_chain(raw, underlying="SPY", today=today)
    assert len(out) == 2
    call = next(s for s in out if s.type == "call")
    put = next(s for s in out if s.type == "put")
    assert call.osi == "SPY260619C00530000"
    assert call.strike == 530.0 and call.expiry == "2026-06-19" and call.dte == 38
    assert call.bid == 8.10 and call.ask == 8.30 and call.mid == pytest.approx(8.20)
    assert call.spread_pct == pytest.approx((0.20 / 8.20) * 100, rel=1e-3)
    assert call.delta == 0.52 and call.iv == 0.18
    assert put.delta == -0.32 and put.iv == 0.21


def test_snapshots_drop_other_underlyings():
    """Alpaca occasionally mixes adjusted-symbol contracts into the response;
    we keep only the requested underlying."""
    today = date(2026, 5, 12)
    raw = {
        "SPY260619C00530000": _snap(latest_quote=_quote(bid=1, ask=2)),
        # SPY1 = adjusted root (post-split); not the same instrument.
        "SPY1260619C00530000": _snap(latest_quote=_quote(bid=1, ask=2)),
    }
    out = oc.snapshots_from_alpaca_chain(raw, underlying="SPY", today=today)
    assert len(out) == 1
    assert out[0].underlying == "SPY"


def test_snapshots_drop_unparseable_osis():
    today = date(2026, 5, 12)
    raw = {
        "SPY260619C00530000": _snap(latest_quote=_quote(bid=1, ask=2)),
        "garbage": _snap(latest_quote=_quote(bid=1, ask=2)),
    }
    out = oc.snapshots_from_alpaca_chain(raw, underlying="SPY", today=today)
    assert len(out) == 1


def test_snapshots_drop_rows_with_no_usable_quote():
    today = date(2026, 5, 12)
    raw = {
        "SPY260619C00530000": _snap(latest_quote=None),                  # no quote
        "SPY260619C00540000": _snap(latest_quote=_quote(bid=0, ask=0)),  # zero quote
        "SPY260619C00550000": _snap(latest_quote=_quote(bid=8.30, ask=8.10)),  # crossed
        "SPY260619C00560000": _snap(latest_quote=_quote(bid=8.10, ask=8.30)),  # OK
    }
    out = oc.snapshots_from_alpaca_chain(raw, underlying="SPY", today=today)
    assert [s.osi for s in out] == ["SPY260619C00560000"]


def test_snapshots_drop_expired_contracts():
    """Contracts whose expiry is before today have dte < 0 → dropped."""
    today = date(2026, 5, 12)
    raw = {
        "SPY260501C00530000": _snap(latest_quote=_quote(bid=1, ask=2)),  # 11 days ago
        "SPY260619C00530000": _snap(latest_quote=_quote(bid=1, ask=2)),  # 38 days fwd
    }
    out = oc.snapshots_from_alpaca_chain(raw, underlying="SPY", today=today)
    assert [s.osi for s in out] == ["SPY260619C00530000"]


def test_snapshots_preserve_null_greeks_and_iv():
    """When Alpaca returns null greeks/IV for a row we keep the snapshot
    (bid/ask is still useful) but preserve None so the prompt can render
    'n/a' instead of a misleading zero."""
    today = date(2026, 5, 12)
    raw = {
        "SPY260619C00530000": _snap(
            latest_quote=_quote(bid=8.10, ask=8.30),
            greeks=None,
            iv=None,
        ),
    }
    out = oc.snapshots_from_alpaca_chain(raw, underlying="SPY", today=today)
    assert len(out) == 1
    s = out[0]
    assert s.iv is None and s.delta is None and s.gamma is None
    assert s.theta is None and s.vega is None


# ---------- filter_chain ----------


def _cs(*, type="call", strike=530.0, dte=38, bid=8.10, ask=8.30, **rest):
    """Build a ChainSnapshot with sensible defaults for filter tests."""
    mid = 0.5 * (bid + ask)
    spread = (ask - bid) / mid * 100 if mid > 0 else float("inf")
    return oc.ChainSnapshot(
        osi=f"SPY{260619 if dte > 0 else 260501}{'C' if type == 'call' else 'P'}{int(strike * 1000):08d}",
        underlying="SPY",
        type=type,
        strike=strike,
        expiry="2026-06-19",
        dte=dte,
        bid=bid,
        ask=ask,
        mid=mid,
        spread_pct=spread,
        iv=rest.get("iv", 0.18),
        delta=rest.get("delta", 0.5),
        gamma=rest.get("gamma", 0.02),
        theta=rest.get("theta", -0.04),
        vega=rest.get("vega", 0.18),
    )


def test_filter_keeps_atm_within_dte():
    snaps = [_cs(strike=530, dte=38)]
    kept = oc.filter_chain(snaps, spot=530)
    assert len(kept) == 1


def test_filter_drops_short_and_long_dte():
    snaps = [
        _cs(strike=530, dte=5),   # too short
        _cs(strike=530, dte=120), # too long
        _cs(strike=530, dte=38),  # OK
    ]
    kept = oc.filter_chain(snaps, spot=530)
    assert [s.dte for s in kept] == [38]


def test_filter_drops_deep_itm_and_otm_outside_atm_band():
    snaps = [
        _cs(strike=300, dte=38),   # 43% below spot — outside ±25%
        _cs(strike=700, dte=38),   # 32% above spot — outside ±25%
        _cs(strike=530, dte=38),   # ATM
        _cs(strike=560, dte=38),   # ~5.7% OTM call — inside ±25%
    ]
    kept = oc.filter_chain(snaps, spot=530)
    assert sorted(s.strike for s in kept) == [530, 560]


def test_filter_drops_wide_spread():
    snaps = [
        _cs(strike=530, dte=38, bid=8.10, ask=8.30),    # 2.4% spread — ok
        _cs(strike=540, dte=38, bid=0.05, ask=0.50),    # 164% spread — drop
    ]
    kept = oc.filter_chain(snaps, spot=530, max_spread_pct=25.0)
    assert [s.strike for s in kept] == [530]


# ---------- summarise_chain ----------


def test_summarise_splits_calls_and_puts_and_sorts():
    snaps = [
        _cs(type="call", strike=560, dte=45),
        _cs(type="put",  strike=510, dte=38),
        _cs(type="call", strike=530, dte=38),
        _cs(type="put",  strike=520, dte=45),
    ]
    summary = oc.summarise_chain(snaps, spot=530)
    assert summary["underlying"] == "SPY"
    assert summary["spot"] == 530
    assert [(c["strike"], c["dte"]) for c in summary["calls"]] == [(530, 38), (560, 45)]
    assert [(p["strike"], p["dte"]) for p in summary["puts"]] == [(510, 38), (520, 45)]


def test_summarise_empty_chain():
    summary = oc.summarise_chain([], spot=530)
    assert summary["underlying"] is None
    assert summary["calls"] == [] and summary["puts"] == []


# ---------- ChainFetcher ----------


class _FakeOptionDataClient:
    """In-memory stand-in for alpaca-py's OptionHistoricalDataClient."""

    def __init__(self, *, raw=None, raise_exc=None):
        self._raw = raw or {}
        self._raise = raise_exc
        self.last_req = None

    def get_option_chain(self, req):
        self.last_req = req
        if self._raise:
            raise self._raise
        return self._raw


def test_chain_fetcher_returns_summary_with_real_alpaca_shape():
    today = date(2026, 5, 12)
    raw = {
        "SPY260619C00530000": _snap(
            latest_quote=_quote(bid=8.10, ask=8.30),
            greeks=_greeks(delta=0.52), iv=0.18,
        ),
        "SPY260619P00510000": _snap(
            latest_quote=_quote(bid=4.20, ask=4.40),
            greeks=_greeks(delta=-0.32), iv=0.21,
        ),
    }
    client = _FakeOptionDataClient(raw=raw)
    f = oc.ChainFetcher(client=client)
    summary = f.fetch("SPY", spot=530, today=today)
    assert summary["underlying"] == "SPY"
    assert len(summary["calls"]) == 1
    assert len(summary["puts"]) == 1


def test_chain_fetcher_sends_pre_filter_bounds_to_alpaca():
    """The fetcher must narrow the server-side response — passing
    strike_price_gte/lte and expiration_date_gte/lte saves bandwidth and
    cuts the chance Alpaca returns thousands of irrelevant strikes."""
    today = date(2026, 5, 12)
    client = _FakeOptionDataClient(raw={
        "SPY260619C00530000": _snap(
            latest_quote=_quote(bid=8.10, ask=8.30),  # tight spread → survives filter
        ),
    })
    f = oc.ChainFetcher(client=client)
    f.fetch("SPY", spot=500, today=today, min_dte=14, max_dte=60, atm_band_pct=20)
    req = client.last_req
    # 20% band off $500 = $100 → [400, 600]
    assert req.strike_price_gte == pytest.approx(400.0)
    assert req.strike_price_lte == pytest.approx(600.0)
    # DTE 14-60 from May 12 → May 26 to July 11
    assert req.expiration_date_gte == date(2026, 5, 26)
    assert req.expiration_date_lte == date(2026, 7, 11)


def test_chain_fetcher_raises_chainfetcherror_on_alpaca_failure():
    client = _FakeOptionDataClient(raise_exc=RuntimeError("subscription denied"))
    f = oc.ChainFetcher(client=client)
    with pytest.raises(oc.ChainFetchError) as ei:
        f.fetch("SPY", spot=530)
    assert "subscription denied" in str(ei.value)


def test_chain_fetcher_raises_chainfetcherror_on_empty_chain():
    client = _FakeOptionDataClient(raw={})
    f = oc.ChainFetcher(client=client)
    with pytest.raises(oc.ChainFetchError):
        f.fetch("SPY", spot=530)


def test_chain_fetcher_raises_chainfetcherror_when_alpaca_sdk_missing(monkeypatch):
    """Codex P1 (PR #50): the SDK import for OptionChainRequest must live
    inside the try block so a missing alpaca-py / alpaca submodule raises
    ChainFetchError (the documented soft-failure contract) instead of
    leaking ModuleNotFoundError up the call stack and aborting the run."""
    import sys
    client = _FakeOptionDataClient(raw={})  # never reached
    f = oc.ChainFetcher(client=client)

    # Force the import inside fetch() to fail. Setting the module entry
    # to None makes Python raise ModuleNotFoundError on the next import
    # of that name (PEP 328 behaviour). monkeypatch restores on teardown.
    monkeypatch.setitem(sys.modules, "alpaca.data.requests", None)

    with pytest.raises(oc.ChainFetchError) as ei:
        f.fetch("SPY", spot=530)
    assert "alpaca get_option_chain failed" in str(ei.value)


def test_chain_fetcher_raises_when_filter_drops_everything():
    """Liquid strikes but all outside DTE band → ChainFetchError so the
    caller can degrade rather than send the agent an empty chain."""
    today = date(2026, 5, 12)
    raw = {
        # 1 day to expiry — outside default 14 DTE floor
        "SPY260513C00530000": _snap(
            latest_quote=_quote(bid=0.50, ask=0.55),
            greeks=_greeks(), iv=0.5,
        ),
    }
    client = _FakeOptionDataClient(raw=raw)
    f = oc.ChainFetcher(client=client)
    with pytest.raises(oc.ChainFetchError) as ei:
        f.fetch("SPY", spot=530, today=today)
    assert "no liquid strikes" in str(ei.value)
