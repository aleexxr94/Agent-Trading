"""Static macro event calendar — fed into signals.json so the strategist
sees upcoming Fed / inflation / payrolls events that affect the universe.

Bootstrap version (PR v2-winrate): hard-coded calendar of FOMC + CPI +
NFP dates through end-2026. No API dependency; refresh annually via
PR. Future iteration: replace with a Federal Reserve or BLS API pull.

Each event has:
  - date: YYYY-MM-DD
  - type: FOMC | CPI | NFP | PCE
  - description: human-friendly label
  - broad_market: True (affects SPY/QQQ/TLT and equity ETFs broadly)

The signals stage attaches a per-ticker
``upcoming_macro_events_7d: list[dict]`` field listing any events
within the next 7 calendar days. The strategist sees this and biases
its regime call (e.g. "FOMC in 3 days → reduce gross exposure").

Cost: $0 — pure data lookup.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


# Static 2026 macro calendar. Update annually.
# Sources: Fed FOMC schedule, BLS NFP/CPI release calendar, BEA PCE.
_EVENTS_2026: tuple[dict, ...] = (
    # FOMC meetings (rate decisions)
    {"date": "2026-01-28", "type": "FOMC", "description": "FOMC rate decision (Jan)"},
    {"date": "2026-03-18", "type": "FOMC", "description": "FOMC rate decision (Mar) + SEP"},
    {"date": "2026-04-29", "type": "FOMC", "description": "FOMC rate decision (Apr/May)"},
    {"date": "2026-06-17", "type": "FOMC", "description": "FOMC rate decision (Jun) + SEP"},
    {"date": "2026-07-29", "type": "FOMC", "description": "FOMC rate decision (Jul)"},
    {"date": "2026-09-16", "type": "FOMC", "description": "FOMC rate decision (Sep) + SEP"},
    {"date": "2026-11-04", "type": "FOMC", "description": "FOMC rate decision (Nov)"},
    {"date": "2026-12-16", "type": "FOMC", "description": "FOMC rate decision (Dec) + SEP"},

    # CPI (monthly, mid-month)
    {"date": "2026-01-14", "type": "CPI", "description": "CPI release (Dec 2025 data)"},
    {"date": "2026-02-11", "type": "CPI", "description": "CPI release (Jan)"},
    {"date": "2026-03-11", "type": "CPI", "description": "CPI release (Feb)"},
    {"date": "2026-04-15", "type": "CPI", "description": "CPI release (Mar)"},
    {"date": "2026-05-13", "type": "CPI", "description": "CPI release (Apr)"},
    {"date": "2026-06-10", "type": "CPI", "description": "CPI release (May)"},
    {"date": "2026-07-15", "type": "CPI", "description": "CPI release (Jun)"},
    {"date": "2026-08-12", "type": "CPI", "description": "CPI release (Jul)"},
    {"date": "2026-09-09", "type": "CPI", "description": "CPI release (Aug)"},
    {"date": "2026-10-14", "type": "CPI", "description": "CPI release (Sep)"},
    {"date": "2026-11-12", "type": "CPI", "description": "CPI release (Oct)"},
    {"date": "2026-12-10", "type": "CPI", "description": "CPI release (Nov)"},

    # NFP (Non-Farm Payrolls — first Friday of each month)
    {"date": "2026-01-02", "type": "NFP", "description": "NFP release (Dec 2025)"},
    {"date": "2026-02-06", "type": "NFP", "description": "NFP release (Jan)"},
    {"date": "2026-03-06", "type": "NFP", "description": "NFP release (Feb)"},
    {"date": "2026-04-03", "type": "NFP", "description": "NFP release (Mar)"},
    {"date": "2026-05-01", "type": "NFP", "description": "NFP release (Apr)"},
    {"date": "2026-06-05", "type": "NFP", "description": "NFP release (May)"},
    {"date": "2026-07-03", "type": "NFP", "description": "NFP release (Jun)"},
    {"date": "2026-08-07", "type": "NFP", "description": "NFP release (Jul)"},
    {"date": "2026-09-04", "type": "NFP", "description": "NFP release (Aug)"},
    {"date": "2026-10-02", "type": "NFP", "description": "NFP release (Sep)"},
    {"date": "2026-11-06", "type": "NFP", "description": "NFP release (Oct)"},
    {"date": "2026-12-04", "type": "NFP", "description": "NFP release (Nov)"},
)


def upcoming_events(
    *,
    within_days: int = 7,
    from_date: date | None = None,
) -> list[dict]:
    """Return macro events within `within_days` of `from_date` (default
    today). Returned list is sorted ascending by date.

    The strategist reads this per-cycle. Events older than today are
    excluded (they're already priced).
    """
    today = from_date or date.today()
    horizon = today + timedelta(days=within_days)
    out: list[dict] = []
    for e in _EVENTS_2026:
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if today <= d <= horizon:
            out.append({**e, "days_away": (d - today).days})
    return sorted(out, key=lambda r: r["date"])


# Tickers in the v2 universe that are sensitive to broad-macro events.
# Currently: SPY, QQQ, TLT directly; everything else gets the events
# attached too because v2's universe is mostly broad-equity proxies.
_BROAD_MACRO_SYMBOLS: frozenset[str] = frozenset({
    "SPY", "QQQ", "TLT",
    "TQQQ", "SQQQ", "UPRO", "SPXU", "TNA", "TZA",
    "SOXL", "SOXS", "FAS", "FAZ",
    "UVXY",  # vol reacts to all macro
})


def events_for_symbol(symbol: str, *, within_days: int = 7,
                      from_date: date | None = None) -> list[dict]:
    """Per-symbol view: returns events that materially affect this
    ticker. For now, all broad-equity / rates tickers get the full
    macro calendar; crypto (BITX) is exempt.
    """
    if symbol not in _BROAD_MACRO_SYMBOLS:
        return []
    return upcoming_events(within_days=within_days, from_date=from_date)
