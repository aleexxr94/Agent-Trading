"""Streamlit dashboard — paper-trading portfolio + decision log + cost ledger.

Run locally:    streamlit run dashboard.py
Phone access:   streamlit run dashboard.py --server.address 0.0.0.0
                (no auth — only on a trusted home network; see README)

Layout:
  Hero header — big NAV, status pills (PAPER / ORDERS / HALTED), last/next cycle
  Slim risk strip — mandatory warning, dismissable in spirit but persistent
  Stats grid — cost-today/cap meter, run count, positions, model-cost split
  Tabs:
    📊 Portfolio — positions table with P&L colouring, allocation, per-position bar
    📒 Cycles — per-run summaries (rationale, candidate counts, cost)
    📜 Decisions — chronological stage decisions with full agent reasoning
    📈 Performance — equity curve, LLM-cost-over-time, trading-fees-over-time, monthly cost breakdown
    💱 Trades — per-trade PnL (gross − fees − attributed LLM cost), closed + open lots, totals
    🤖 Agent Logs — latest artifacts (market_gate/signals/view/portfolio/sanity/orders), next-run plan
    ⚙️ Settings — halt flag toggle, cost totals, README link
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import dashboard_data as dd
from lib import pnl as pnl_lib
from lib import state

ROOT = Path(__file__).resolve().parent

RISK_WARNING_TEXT = (
    "PAPER TRADING — experimental autonomous AI agent. Leveraged ETFs and "
    "options on a small account are high-risk. Not financial advice."
)

# Per-run + per-day cost caps come from .env so the meters reflect what
# orchestrator.py will actually enforce. Defaults match lib.llm._per_run_cap
# / _daily_cap and .env.example — bumped to $3 / $12 on 2026-05-13 (Codex
# P2 on PR #62: dashboard fallback was reporting stricter thresholds than
# the orchestrator actually enforced when the env vars were unset).
PER_RUN_CAP_USD = float(os.environ.get("PER_RUN_COST_CAP_USD", "3.00"))
DAILY_CAP_USD = float(os.environ.get("DAILY_COST_CAP_USD", "12.00"))


# ---------- page setup ----------


st.set_page_config(
    page_title="Agent-Trading",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# Inline CSS — Streamlit honours .streamlit/config.toml for base theme but
# we layer custom components (status pills, hero card, meters) on top.
st.markdown(
    """
    <style>
      :root {
        /* Light theme palette */
        --bg-0:   #ffffff;
        --bg-1:   #f8fafc;   /* slate-50  — card background */
        --bg-2:   #f1f5f9;   /* slate-100 — meter track    */
        --border: #e2e8f0;   /* slate-200 — card borders   */
        --text-0: #0f172a;   /* slate-900 — primary text   */
        --text-1: #475569;   /* slate-600 — secondary text */
        --text-2: #94a3b8;   /* slate-400 — muted text     */
        --green:       #059669;  /* emerald-600 */
        --green-soft:  #d1fae5;  /* emerald-100 */
        --green-text:  #065f46;  /* emerald-800 */
        --red:         #dc2626;  /* red-600     */
        --red-soft:    #fee2e2;  /* red-100     */
        --red-text:    #991b1b;  /* red-800     */
        --amber:       #d97706;  /* amber-600   */
        --amber-soft:  #fef3c7;  /* amber-100   */
        --amber-text:  #92400e;  /* amber-800   */
        --blue:        #2563eb;  /* blue-600    */
        --blue-soft:   #dbeafe;  /* blue-100    */
        --blue-text:   #1e40af;  /* blue-800    */
        --purple:      #7c3aed;  /* violet-600  */
        --plot-grid:   #e2e8f0;  /* slate-200   */
      }

      /* widen the main container — was bottle-necked at 1400px; use the full
         viewport so a wide monitor doesn't leave empty bands on either side */
      .block-container {
        padding-top: 1.4rem !important;
        padding-bottom: 4rem !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
        max-width: none !important;
      }

      /* hero card */
      .at-hero {
        background: linear-gradient(135deg, var(--bg-1) 0%, var(--bg-0) 100%);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 1.4rem 1.75rem;
        margin-bottom: 0.9rem;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
      }
      .at-hero-row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
      .at-hero-label { color: var(--text-1); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 600; }
      .at-hero-nav { font-size: 3.1rem; font-weight: 800; color: var(--text-0); letter-spacing: -0.02em; line-height: 1.05; margin-top: 0.25rem; }
      .at-hero-sub { color: var(--text-1); font-size: 0.95rem; margin-top: 0.4rem; font-weight: 500; }

      /* status pills */
      .at-pills { display: flex; gap: 0.5rem; flex-wrap: wrap; }
      .at-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.3rem 0.8rem;
        border-radius: 999px;
        font-size: 0.85rem;
        font-weight: 700;
        letter-spacing: 0.02em;
        border: 1px solid transparent;
      }
      .at-pill.paper      { background: var(--green-soft); color: var(--green-text); border-color: #a7f3d0; }
      .at-pill.live       { background: var(--red-soft);   color: var(--red-text);   border-color: #fecaca; }
      .at-pill.orders-on  { background: var(--blue-soft);  color: var(--blue-text);  border-color: #bfdbfe; }
      .at-pill.orders-off { background: var(--bg-2);       color: var(--text-1);     border-color: var(--border); }
      .at-pill.halted     { background: #fef2f2; color: var(--red-text); border-color: var(--red);
                            animation: at-pulse 1.5s ease-in-out infinite; }
      .at-pill.allcash    { background: var(--amber-soft); color: var(--amber-text); border-color: #fde68a; }
      .at-pill.active     { background: var(--green-soft); color: var(--green-text); border-color: #a7f3d0; }

      /* Closed-trade P&L chips beside the Settled balance card */
      .at-chip {
        display: inline-flex; align-items: center; gap: 0.3rem;
        padding: 0.25rem 0.6rem; border-radius: 999px;
        font-size: 0.82rem; font-weight: 500;
        border: 1px solid var(--border);
        background: var(--bg-2);
      }
      .at-chip.pos { background: var(--green-soft); color: var(--green-text); border-color: #a7f3d0; }
      .at-chip.neg { background: var(--red-soft);   color: var(--red-text);   border-color: #fecaca; }
      .at-chip strong { font-weight: 700; }

      @keyframes at-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.55; } }

      /* slim risk strip — replaces the chunky old banner */
      .at-risk-strip {
        background: var(--red); color: #fff5f5;
        padding: 0.45rem 0.95rem; border-radius: 8px;
        font-size: 0.9rem; font-weight: 600; text-align: center;
        margin: 0 0 0.9rem 0;
        border: 1px solid var(--red-text);
        box-shadow: 0 1px 3px rgba(220, 38, 38, 0.18);
      }

      /* halted banner — sticks to top, pulses */
      .at-halt-banner {
        position: sticky; top: 0; z-index: 999;
        background: var(--red); color: #ffffff;
        padding: 0.7rem 1rem; border-radius: 8px; margin-bottom: 0.85rem;
        font-weight: 800; font-size: 1rem; text-align: center;
        box-shadow: 0 6px 18px rgba(220, 38, 38, 0.35);
        animation: at-pulse 1.5s ease-in-out infinite;
      }

      /* compact metric card grid */
      .at-stat {
        background: var(--bg-1);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 1rem 1.15rem;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
      }
      .at-stat-label { color: var(--text-1); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }
      .at-stat-value { color: var(--text-0); font-size: 1.7rem; font-weight: 700; margin-top: 0.2rem; letter-spacing: -0.01em; }
      .at-stat-sub { color: var(--text-1); font-size: 0.85rem; margin-top: 0.25rem; font-weight: 500; }
      .at-stat-value.pos { color: var(--green); }
      .at-stat-value.neg { color: var(--red); }
      .at-stat-value.warn { color: var(--amber); }

      /* cost meter bar */
      .at-meter { height: 7px; background: var(--bg-2); border-radius: 999px; overflow: hidden; margin-top: 0.5rem; }
      .at-meter-fill { height: 100%; background: var(--green); border-radius: 999px; transition: width 0.4s; }
      .at-meter-fill.warn { background: var(--amber); }
      .at-meter-fill.danger { background: var(--red); }

      /* tabs */
      .stTabs [data-baseweb="tab-list"] { gap: 0.3rem; border-bottom: 1px solid var(--border); }
      .stTabs [data-baseweb="tab"] { padding: 0.65rem 1.2rem; font-size: 0.95rem; font-weight: 600; }

      /* dataframe */
      [data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
      /* Bump dataframe cell font-size from Streamlit's default (~0.875rem)
         to a more readable 1rem for operator readability. Targets both the
         <th> header row and <td> body rows. Scoped to dataframes only —
         doesn't touch the rest of the page chrome. */
      [data-testid="stDataFrame"] th,
      [data-testid="stDataFrame"] td {
        font-size: 1rem !important;
        padding: 0.5rem 0.75rem !important;
      }
      [data-testid="stDataFrame"] th {
        font-weight: 700 !important;
      }

      /* subtle subheaders */
      h1, h2, h3 { color: var(--text-0); font-weight: 700; letter-spacing: -0.01em; }
      .at-section-label {
        color: var(--text-1); font-size: 0.88rem; text-transform: uppercase;
        letter-spacing: 0.08em; font-weight: 700;
        margin: 1rem 0 0.5rem 0;
      }

      /* small-muted text */
      .small-muted { color: var(--text-2); font-size: 0.85rem; }

      /* slightly larger body type for readability */
      .stMarkdown, .stCaption, p { font-size: 0.95rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- data ----------


portfolio, source = dd.load_portfolio()
decisions = dd.load_decisions(limit=200)
costs = dd.load_costs(limit=2000)
latest_rid = dd.latest_run_id()
halted = state.is_halted()
broker_view = dd.try_load_broker_view()
broker_marks, broker_costs = broker_view.marks, broker_view.costs

# Useful pre-computed values
cost_today = dd.cost_today_usd()
cost_today_pct = min(100.0, 100.0 * cost_today / max(DAILY_CAP_USD, 0.0001))
cost_this_run = dd.cost_for_run_usd(latest_rid) if latest_rid else 0.0
totals = dd.total_token_cost()
_open_for_count, _closed_for_count = dd.split_positions_by_broker_holdings(
    portfolio,
    held_keys=broker_view.held_keys if broker_view.available else None,
)
# n_positions reflects what's actually open at the broker (or everything in
# portfolio.json when the broker is unreachable). Avoids the hero/status
# pills disagreeing with the table after a position closes.
n_positions = len(_open_for_count)
is_all_cash = portfolio.get("all_cash", False)
orders_enabled = os.environ.get("ORDERS_ENABLED", "false").lower() == "true"
live_trading = os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"

# Last + next cycle timestamps
last_run_at = portfolio.get("generated_at", "")
next_run_at = ""
if state.NEXT_RUN.exists():
    try:
        next_run_at = state.read_json(state.NEXT_RUN).get("next_run_at", "")
    except Exception:
        pass


def _fmt_ts(iso: str) -> str:
    """Render an ISO UTC timestamp as 'May 12 14:00 UTC' for at-a-glance reading."""
    if not iso:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%M UTC")
    except Exception:
        return iso[:16].replace("T", " ")


# ---------- halt banner (sticky, top) ----------


if halted:
    st.markdown(
        f'<div class="at-halt-banner">🛑 ORCHESTRATOR HALTED — '
        f'<code style="background:transparent;color:#fff">{state.HALT_FLAG.name}</code> is set. '
        f'New orders disabled. Clear via Settings tab.</div>',
        unsafe_allow_html=True,
    )


# ---------- hero header ----------


def _pills_html() -> str:
    pills = []
    pills.append('<span class="at-pill paper">● PAPER</span>' if not live_trading
                 else '<span class="at-pill live">● LIVE</span>')
    pills.append('<span class="at-pill orders-on">⚡ ORDERS ON</span>' if orders_enabled
                 else '<span class="at-pill orders-off">○ orders off</span>')
    if halted:
        pills.append('<span class="at-pill halted">🛑 HALTED</span>')
    elif is_all_cash:
        pills.append('<span class="at-pill allcash">💰 all-cash</span>')
    else:
        pills.append(f'<span class="at-pill active">▶ {n_positions} open</span>')
    return '<div class="at-pills">' + "".join(pills) + "</div>"


# Synthetic balance is the single source of truth for the dashboard's
# headline number. It's derived entirely from logs we control
# (state/trades.jsonl + state/costs.jsonl) and intentionally ignores
# Alpaca's account equity — that's exposed as a separate informational
# sub-line below. See lib/dashboard_data.py:SyntheticBalance and the
# refactor rationale at plans/federated-greeting-sphinx.md.
_synth_live = dd.compute_synthetic_balance(marks=broker_marks or {})
hero_nav_usd = _synth_live.synthetic_balance_usd
_starting = _synth_live.starting_balance_usd
_hero_tone = (
    "pos" if hero_nav_usd > _starting
    else "neg" if hero_nav_usd < _starting
    else ""
)
# Breakdown sub-line: shows the five formula components so any drift
# between the headline and the per-position table is auditable in one
# glance. Numbers carry signs (closed/open can be either) and we drop
# "−$0.00" terms only when ALL costs/fees are genuinely zero.
def _signed(usd: float) -> str:
    return f"${usd:+,.2f}".replace("$+", "+$").replace("$-", "−$")

breakdown_html = (
    f"${_starting:,.0f} "
    f"<span style='color: var(--text-2);'>+</span> "
    f"{_signed(_synth_live.closed_gross_pnl_usd)} closed "
    f"<span style='color: var(--text-2);'>+</span> "
    f"{_signed(_synth_live.open_gross_pnl_usd)} open "
    f"<span style='color: var(--text-2);'>−</span> "
    f"${_synth_live.llm_cost_total_usd:,.2f} LLM "
    f"<span style='color: var(--text-2);'>−</span> "
    f"${_synth_live.trading_fees_total_usd:,.2f} fees"
)
# Informational-only Alpaca equity row. Always labelled and rendered
# in a muted style so it's obviously NOT the source of truth.
alpaca_line = ""
if broker_view.available and broker_view.nav_usd is not None:
    alpaca_line = (
        f'<div class="at-hero-sub" style="opacity:0.7; font-size:0.85rem;">'
        f'Alpaca account: <strong>${broker_view.nav_usd:,.2f}</strong> '
        f'<span style="color: var(--text-2);">(informational — not used '
        f'for any dashboard calculation)</span>'
        f'</div>'
    )
unmarked_line = ""
if _synth_live.unmarked_open_lots > 0:
    unmarked_line = (
        f'<div class="at-hero-sub" style="opacity:0.7; font-size:0.85rem; color: var(--amber-text);">'
        f'{_synth_live.unmarked_open_lots} open lot(s) without live marks '
        f'— their P&L contribution treated as $0 until marks return.'
        f'</div>'
    )
# Data-integrity warning: unmatched sell fills in trades.jsonl signal
# the synthetic balance is missing some P&L. Healthy operation never
# triggers this (Codex P1 on PR #79). Surface loudly rather than let
# the headline lie about a number the operator trusts.
integrity_line = ""
if _synth_live.is_integrity_warning:
    integrity_line = (
        f'<div class="at-hero-sub" style="font-size:0.9rem; '
        f'color: var(--red-text); font-weight: 600; margin-top: 0.4rem; '
        f'padding: 0.4rem 0.6rem; background: var(--red-soft); '
        f'border-radius: 6px;">'
        f'⚠ {_synth_live.unmatched_sell_count} unmatched sell fill(s) '
        f'in state/trades.jsonl — synthetic balance may be inaccurate. '
        f'Inspect the file for sells without matching buys (out-of-order '
        f'sync, legacy fills, or manual edits).'
        f'</div>'
    )

st.markdown(
    f"""
    <div class="at-hero">
      <div class="at-hero-row">
        <div>
          <div class="at-hero-label">Synthetic balance (USD)</div>
          <div class="at-hero-nav {_hero_tone}">${hero_nav_usd:,.2f}</div>
          <div class="at-hero-sub">
            {breakdown_html}
          </div>
          <div class="at-hero-sub" style="opacity:0.75; font-size:0.85rem;">
            Last cycle: <strong>{_fmt_ts(last_run_at)}</strong>
            &nbsp;•&nbsp; Next: <strong>{_fmt_ts(next_run_at)}</strong>
            &nbsp;•&nbsp; Source: <strong>{source}</strong>
          </div>
          {alpaca_line}
          {unmarked_line}
          {integrity_line}
        </div>
        <div style="text-align:right">
          {_pills_html()}
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------- Realized balance (closed trades only) ----------
# Complementary view to the hero. Frozen between closes so it doesn't
# move with intraday marks — only changes when a real fill lands in
# trades.jsonl or a cost row lands in costs.jsonl. The chips strip
# beside it shows the per-trade contributions that built it.
_synth_realized = dd.compute_synthetic_balance(marks={})
_realized_balance = _synth_realized.synthetic_balance_usd
_chips = dd.closed_trade_chips(marks=broker_marks or {}, limit=12)
_chips_html = "".join(
    f'<span class="at-chip {"pos" if c["net_pnl_usd"] >= 0 else "neg"}">'
    f'{html.escape(c["symbol"])} '
    f'<strong>{"+" if c["net_pnl_usd"] >= 0 else ""}${c["net_pnl_usd"]:,.2f}</strong>'
    f'</span>'
    for c in _chips
) or '<span style="color: var(--text-2); font-size: 0.9rem;">No closed trades yet — chips appear here as positions close.</span>'
_realized_tone = (
    "pos" if _realized_balance > _synth_realized.starting_balance_usd
    else "neg" if _realized_balance < _synth_realized.starting_balance_usd
    else ""
)
st.markdown(
    f"""
    <div class="at-hero" style="margin-top: 0.8rem; background: var(--card-2, #f8fafc);">
      <div style="display: flex; align-items: center; gap: 1.25rem; flex-wrap: wrap;">
        <div style="min-width: 220px;">
          <div class="at-hero-label">Realized balance (closed trades only)</div>
          <div class="at-hero-nav {_realized_tone}" style="font-size: 1.85rem;">${_realized_balance:,.2f}</div>
          <div class="at-hero-sub" style="font-size: 0.8rem;">
            Frozen at last close. Live mark P&L is in the hero above.
          </div>
        </div>
        <div style="flex: 1; min-width: 280px; display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center;">
          {_chips_html}
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# Slim, persistent risk strip
st.markdown(
    f'<div class="at-risk-strip">⚠ {RISK_WARNING_TEXT}</div>',
    unsafe_allow_html=True,
)


# ---------- stats grid ----------


def _stat_card(label: str, value: str, *, sub: str = "", tone: str = "") -> str:
    cls = f"at-stat-value {tone}".strip()
    sub_html = f'<div class="at-stat-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="at-stat">'
        f'<div class="at-stat-label">{label}</div>'
        f'<div class="{cls}">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


def _meter_card(label: str, current: float, cap: float, *, fmt: str = "${:.2f}") -> str:
    pct = min(100.0, 100.0 * current / max(cap, 0.0001))
    tone = "danger" if pct >= 90 else ("warn" if pct >= 70 else "")
    return (
        f'<div class="at-stat">'
        f'<div class="at-stat-label">{label}</div>'
        f'<div class="at-stat-value">{fmt.format(current)}</div>'
        f'<div class="at-stat-sub">of {fmt.format(cap)} cap &nbsp;• {pct:.0f}%</div>'
        f'<div class="at-meter"><div class="at-meter-fill {tone}" style="width: {pct}%"></div></div>'
        f'</div>'
    )


sg = st.columns(4)
cost_today_label = "Cost today"
if state.read_cost_reset_at():
    cost_today_label = "Cost today · since reset"
sg[0].markdown(_meter_card(cost_today_label, cost_today, DAILY_CAP_USD), unsafe_allow_html=True)
sg[1].markdown(_meter_card("Cost this run", cost_this_run, PER_RUN_CAP_USD), unsafe_allow_html=True)
runs_total = dd.runs_count()
sg[2].markdown(
    _stat_card(
        "Runs all time",
        f"{runs_total:,}",
        sub=f"{totals['calls']:,} LLM calls · ${totals['cost_usd']:.2f} total",
    ),
    unsafe_allow_html=True,
)
sg[3].markdown(
    _stat_card(
        "Cache hit rate",
        f"{100.0 * totals['cache_read_input_tokens'] / max(1, totals['total_tokens']):.1f}%"
        if totals['total_tokens'] else "—",
        sub=f"{totals['total_tokens']:,} tokens lifetime",
    ),
    unsafe_allow_html=True,
)


# ---------- tabs ----------


tabs = st.tabs([
    "📊 Portfolio",
    "📒 Cycles",
    "📜 Decisions",
    "📈 Performance",
    "💱 Trades",
    "🤖 Agent Logs",
    "⚙️ Settings",
])


# ===== Tab 1: Portfolio =====
with tabs[0]:
    if source != "live":
        st.info(
            f"Showing **{source}** data — no live portfolio yet. "
            "Run `python orchestrator.py` to populate."
        )

    # When the broker is reachable, hide portfolio.json positions the broker
    # no longer carries (closed manually / expired / killed). When the broker
    # is unreachable we render everything — better than blanking the dashboard.
    filter_keys = broker_view.held_keys if broker_view.available else None
    # "Days held" is derived from the earliest buy-side fill per symbol in
    # trades.jsonl — pass that map into the row builder so it can populate
    # the new column without each row re-reading the file.
    opened_at_by_symbol = dd._opened_at_map_from_trades(dd.load_trades())
    rows = dd.position_table_rows(
        portfolio,
        marks=broker_marks or None,
        costs=broker_costs or None,
        held_keys=filter_keys,
        opened_at_by_symbol=opened_at_by_symbol,
    )

    open_positions, closed_positions = dd.split_positions_by_broker_holdings(
        portfolio, held_keys=filter_keys,
    )
    if closed_positions:
        closed_labels = []
        for p in closed_positions:
            if p["kind"] == "etf":
                closed_labels.append(p["symbol"])
            else:
                closed_labels.append(
                    f"{p['underlying']} {p['type'].upper()} {p['strike']:g} {p['expiry']}"
                )
        st.warning(
            "Closed since last orchestrator run — "
            "broker no longer reports: **"
            + ", ".join(closed_labels)
            + "**. These were in the agent's last portfolio.json but are no "
            "longer open at Alpaca."
        )

    if is_all_cash:
        # Escape the rationale — it's model-generated text and may contain
        # angle brackets or other HTML-looking content that would otherwise
        # render as markup inside this unsafe_allow_html block.
        rationale = html.escape(portfolio.get("all_cash_rationale") or "—")
        st.markdown(
            f'<div class="at-stat" style="border-color: var(--amber-soft);">'
            f'<div class="at-stat-label" style="color:var(--amber);">💰 ALL-CASH PORTFOLIO</div>'
            f'<div class="at-stat-sub" style="margin-top:0.4rem; line-height:1.5; color:var(--text-1);">'
            f'{rationale}</div></div>',
            unsafe_allow_html=True,
        )

    if rows:
        st.markdown('<div class="at-section-label">Positions</div>', unsafe_allow_html=True)

        df_pos = pd.DataFrame(rows)

        # Coerce mixed-type columns to consistent dtypes — Streamlit
        # serialises dataframes through pyarrow, which rejects "object"
        # columns that mix int + str ("DTE" is int for options / "—"
        # for ETFs) or int + None ("Days held" is None when no trade
        # history covers the symbol). Map sentinels to pandas's
        # nullable Int64 so the column renders as a number where data
        # exists and blank where it doesn't.
        if "DTE" in df_pos.columns:
            df_pos["DTE"] = (
                df_pos["DTE"]
                .apply(lambda v: v if isinstance(v, (int, float)) else None)
                .astype("Int64")
            )
        if "Days held" in df_pos.columns:
            df_pos["Days held"] = df_pos["Days held"].astype("Int64")

        # Aggregate totals across the open-positions table. Surfaces the
        # whole-portfolio P&L at a glance, separate from the per-row
        # breakdown below. None values (rows without a live mark) are
        # skipped — sum operates on populated cells only.
        def _sum_or_none(col_name: str) -> float | None:
            if col_name not in df_pos.columns:
                return None
            vals = [v for v in df_pos[col_name] if isinstance(v, (int, float)) and v == v]
            return sum(vals) if vals else None

        total_gross = _sum_or_none("Gross P&L")
        total_net = _sum_or_none("Net P&L")
        total_notional = _sum_or_none("Notional")

        tot = st.columns(4)
        tot[0].markdown(
            _stat_card(
                "Positions open",
                str(len(df_pos)),
                sub="across the universe",
            ),
            unsafe_allow_html=True,
        )
        tot[1].markdown(
            _stat_card(
                "Total notional",
                f"${total_notional:,.0f}" if total_notional is not None else "—",
                sub="sum of position USD value",
            ),
            unsafe_allow_html=True,
        )
        tot[2].markdown(
            _stat_card(
                "Aggregate Gross P&L",
                f"${total_gross:+,.2f}" if total_gross is not None else "—",
                sub="pre-fees, pre-LLM",
                tone=("pos" if total_gross and total_gross > 0 else
                      "neg" if total_gross and total_gross < 0 else ""),
            ),
            unsafe_allow_html=True,
        )
        tot[3].markdown(
            _stat_card(
                "Aggregate Net P&L",
                f"${total_net:+,.2f}" if total_net is not None else "—",
                sub="net of modelled costs",
                tone=("pos" if total_net and total_net > 0 else
                      "neg" if total_net and total_net < 0 else ""),
            ),
            unsafe_allow_html=True,
        )

        # Color-code Gross/Net P&L: green positive, red negative, neutral when blank.
        def _color_pnl(v):
            # On a light background the brighter Tailwind-emerald-500 / red-500
            # are washed-out; the -600 variants below carry better contrast.
            if v is None or (isinstance(v, float) and v != v):  # NaN check
                return "color: #94a3b8"
            if isinstance(v, (int, float)):
                if v > 0: return "color: #059669; font-weight: 700"
                if v < 0: return "color: #dc2626; font-weight: 700"
            return ""

        # Append a TOTAL row at the bottom of the dataframe — readers
        # asked for column sums alongside the per-row breakdown. The
        # symbol column gets the literal string "TOTAL" so the row is
        # visually obvious; the leftover non-numeric columns get blanks
        # to avoid bogus "averages." Pandas Styler bolds the row via a
        # row-level apply.
        def _sum_col(col: str) -> float | None:
            if col not in df_pos.columns:
                return None
            vals = [v for v in df_pos[col] if isinstance(v, (int, float)) and v == v]
            return sum(vals) if vals else None

        # Any column pandas inferred as numeric must use pd.NA for the
        # blank TOTAL cell — "" demotes the whole column back to object
        # and pyarrow refuses to serialise it for Streamlit. Text
        # columns ("Symbol"/"Kind"/"Bias"/"Greeks"/"Kill"/"Leverage")
        # take "" cleanly.
        _numeric_dtypes = {"int64", "Int64", "float64", "Float64"}
        _is_numeric = {
            col: str(df_pos[col].dtype) in _numeric_dtypes
            for col in df_pos.columns
        }

        total_row: dict = {}
        for col in df_pos.columns:
            if col in ("Notional", "Fees", "Gross P&L", "Net P&L"):
                total_row[col] = _sum_col(col)
            elif col == "% NAV":
                # % NAV sums to the portfolio's invested share (cash is
                # the remainder). Show the sum, not a fictitious average.
                total_row[col] = _sum_col(col)
            elif col == "Symbol":
                total_row[col] = "TOTAL"
            elif _is_numeric.get(col, False):
                # Numeric columns we don't sum (Qty, Entry, Mark, DTE,
                # Days held, Δ%): averaging across heterogenous
                # instruments would mislead, so leave blank — pd.NA
                # preserves the column dtype.
                total_row[col] = pd.NA
            else:
                total_row[col] = ""
        df_pos_with_total = pd.concat(
            [df_pos, pd.DataFrame([total_row])],
            ignore_index=True,
        )
        total_row_idx = len(df_pos_with_total) - 1

        def _bold_total(row):
            if row.name == total_row_idx:
                return ["font-weight: 800; border-top: 2px solid var(--border)"] * len(row)
            return [""] * len(row)

        # Δ% lives in the same green/red semantic space as the P&L
        # columns — apply the same color formatter so a move-since-entry
        # of -8% reads red at a glance.
        color_subset = [c for c in ("Gross P&L", "Net P&L", "Δ%") if c in df_pos_with_total.columns]
        # na_rep="—" turns every NaN / pd.NA cell into an em-dash
        # (matches the existing "no data" sentinel used in row builds).
        # Without this, Pandas Styler renders pd.NA as the literal
        # string "None" — which is what shows up on ETF rows for DTE /
        # Days held and on every cell of the TOTAL row for columns we
        # don't sum (Qty, Entry, Mark, etc.). `precision=None` keeps
        # column_config's per-column NumberColumn(format=...) rules
        # in charge of numeric rendering for the non-NA cells.
        styled = (
            df_pos_with_total.style
            .format(na_rep="—", precision=None)
            .map(_color_pnl, subset=color_subset)
            .apply(_bold_total, axis=1)
        )
        st.dataframe(
            styled,
            width="stretch",
            hide_index=True,
            column_config={
                "Entry":     st.column_config.NumberColumn(
                    "Entry",
                    format="$%.2f",
                    help="Per-unit cost basis at entry — broker-reported "
                         "avg_cost when available, otherwise the agent's "
                         "intended fill price from portfolio.json.",
                ),
                "Mark":      st.column_config.NumberColumn(
                    "Mark",
                    format="$%.2f",
                    help="Current per-unit price (last trade / mid quote) "
                         "from the broker. Blank when marks aren't wired.",
                ),
                "Δ%":        st.column_config.NumberColumn(
                    "Δ%",
                    format="%+.1f%%",
                    help="Gross percent move since entry, per unit. "
                         "Independent of position size — complements the "
                         "$-denominated Gross / Net P&L columns.",
                ),
                "Notional":  st.column_config.NumberColumn("Notional", format="$%,.0f"),
                "% NAV":     st.column_config.NumberColumn(
                    "% NAV",
                    format="%.1f%%",
                    help="Position notional as a share of total NAV. "
                         "Entry cap is 15%; cap drops to 7.5% in ≥10% "
                         "drawdown. Drift past the cap after entry is OK.",
                ),
                "DTE":       st.column_config.Column(
                    "DTE",
                    help="Days to expiry for options. '—' on ETF rows.",
                ),
                "Days held": st.column_config.NumberColumn(
                    "Days held",
                    format="%d",
                    help="Whole-days since the earliest buy fill for this "
                         "symbol per state/trades.jsonl.",
                ),
                "Bias":      st.column_config.Column(
                    "Bias",
                    help="Direction expressed by the position: Bull (bull "
                         "ETF or long call), Bear (inverse ETF or long "
                         "put), Long vol (UVXY), Long crypto (BITX).",
                ),
                "Kill":      st.column_config.Column(
                    "Kill",
                    help="Trigger conditions monitor.py uses to flatten "
                         "the position: max-loss %, underlying price "
                         "thresholds, and time stop (date).",
                ),
                "Fees":      st.column_config.NumberColumn(
                    "Fees",
                    format="$%,.2f",
                    help="Modelled round-trip broker costs for this "
                         "position (IBKR Pro retail): entry-leg spread + "
                         "commission already paid, plus projected close. "
                         "Net P&L = Gross P&L − Fees.",
                ),
                "Gross P&L": st.column_config.NumberColumn("Gross P&L", format="$%+,.2f"),
                "Net P&L":   st.column_config.NumberColumn("Net P&L",  format="$%+,.2f"),
            },
        )
        if not broker_marks:
            st.caption(
                "Mark / P&L columns stay blank until Alpaca paper keys are "
                "configured — see README §Setup."
            )

        col_pie, col_bar = st.columns([1, 1])
        with col_pie:
            pie_data = dd.allocation_pie(portfolio)
            fig = go.Figure(go.Pie(
                labels=[r["label"] for r in pie_data],
                values=[r["value"] for r in pie_data],
                hole=0.55,
                marker=dict(line=dict(color="#ffffff", width=2)),
            ))
            fig.update_layout(
                template="plotly_white",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=360,
                margin=dict(l=10, r=10, t=40, b=10),
                title=dict(text="Allocation — % NAV", font=dict(size=15, color="#0f172a")),
                legend=dict(font=dict(color="#0f172a", size=12)),
            )
            st.plotly_chart(fig, width="stretch")

        with col_bar:
            bar_rows = [r for r in rows if r.get("Net P&L") is not None]
            if bar_rows:
                bar_rows = sorted(bar_rows, key=lambda r: r["Net P&L"])
                # Two-trace bar: per-position bars (green/red by sign) plus
                # an aggregate TOTAL bar on the far right in gold so it's
                # visually distinct from the individual positions. The
                # operator asked for the combined number alongside the
                # per-position breakdown.
                total_net = sum(r["Net P&L"] for r in bar_rows)
                fig_bar = go.Figure()
                fig_bar.add_trace(go.Bar(
                    x=[r["Symbol"] for r in bar_rows],
                    y=[r["Net P&L"] for r in bar_rows],
                    marker_color=[
                        "#059669" if r["Net P&L"] >= 0 else "#dc2626"
                        for r in bar_rows
                    ],
                    name="Per position",
                    hovertemplate="%{x}<br>Net: $%{y:+,.2f}<extra></extra>",
                ))
                fig_bar.add_trace(go.Bar(
                    x=["TOTAL"],
                    y=[total_net],
                    marker_color=["#d97706"],
                    marker_line=dict(color="#0f172a", width=1.5),
                    name="Total",
                    text=[f"${total_net:+,.2f}"],
                    textposition="outside",
                    hovertemplate="All positions combined<br>Net: $%{y:+,.2f}<extra></extra>",
                ))
                fig_bar.update_layout(
                    template="plotly_white",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    height=360,
                    margin=dict(l=10, r=10, t=40, b=10),
                    title=dict(text="Net P&L per position + combined (USD)",
                               font=dict(size=15, color="#0f172a")),
                    yaxis=dict(title="USD", gridcolor="#e2e8f0"),
                    xaxis=dict(tickfont=dict(size=11)),
                    showlegend=False,
                )
                st.plotly_chart(fig_bar, width="stretch")
            else:
                st.info("Per-position P&L bar populates once marks are available.")
    elif not is_all_cash:
        st.write("No open positions.")


# ===== Tab 2: Cycles =====
with tabs[1]:
    summaries = dd.load_run_summaries(limit=20)
    if not summaries:
        st.info("No runs yet — fire the orchestrator (manually or via the timer) to populate this view.")
    else:
        st.markdown(
            f'<div class="at-section-label">Last {len(summaries)} cycles — newest first, all expanded</div>',
            unsafe_allow_html=True,
        )
        for s in summaries:
            # Status pill (matches the hero-row pills): orange for all-cash,
            # green for positions, neutral for in-flight runs missing data.
            if s["all_cash"] is True:
                status_html = '<span class="at-pill allcash">💰 all-cash</span>'
            elif s["all_cash"] is False and s["positions_count"] > 0:
                status_html = f'<span class="at-pill active">▶ {s["positions_count"]} positions</span>'
            else:
                status_html = '<span class="at-pill orders-off">○ no portfolio</span>'

            # Funnel: universe signals → strategist candidates → portfolio positions.
            funnel = (
                f'{s["signals_count"]} signals → '
                f'{s["candidates_count"]} candidates → '
                f'{s["positions_count"]} {"positions" if s["positions_count"] != 1 else "position"}'
            )

            # Escape the rationales since they're model-generated text and we're
            # injecting them into an unsafe_allow_html block.
            construction = html.escape(s["construction_rationale"] or "—")
            all_cash_rat = html.escape(s["all_cash_rationale"] or "")
            next_rat     = html.escape(s["next_run_rationale"] or "")
            run_id_safe  = html.escape(s["run_id"])

            # Pick the most informative rationale to lead with: all-cash takes
            # priority because it answers "why no trade" — exactly what an
            # operator wants to see at a glance.
            primary_rationale = all_cash_rat if (s["all_cash"] and all_cash_rat) else construction

            # Pre-compute the meta-scheduler footer so the outer f-string
            # stays clean (Python 3.11 doesn't allow nested f-strings with
            # quote-mismatched expressions; pre-computing avoids the trap
            # entirely AND makes the markup easier to read).
            meta_footer = ""
            if next_rat:
                meta_footer = (
                    '<div style="margin-top: 0.9rem; padding-top: 0.9rem; '
                    'border-top: 1px solid var(--border);">'
                    '<div class="at-stat-label" style="font-size: 0.85rem;">'
                    f'Meta-scheduler — next at {_fmt_ts(s["next_run_at"])}'
                    '</div>'
                    '<div style="color: var(--text-0); font-size: 1.0rem; '
                    'line-height: 1.55; margin-top: 0.4rem; font-weight: 500;">'
                    f'{next_rat}'
                    '</div></div>'
                )

            st.markdown(
                f'''
                <div class="at-stat" style="margin-bottom: 1rem; padding: 1.2rem 1.4rem;">
                  <div style="display:flex; align-items:baseline; justify-content:space-between; gap:1rem; flex-wrap:wrap; margin-bottom: 0.85rem;">
                    <div>
                      <div class="at-stat-label" style="margin-bottom: 0.2rem; font-size: 0.95rem;">
                        {_fmt_ts(s["generated_at"]) if s["generated_at"] else "in flight"}
                      </div>
                      <div style="font-family: ui-monospace, monospace; font-size: 0.9rem; color: var(--text-2); font-weight: 500;">
                        {run_id_safe}
                      </div>
                    </div>
                    <div style="text-align:right;">
                      {status_html}
                      <div style="color: var(--text-1); font-size: 0.95rem; margin-top: 0.4rem; font-weight: 600;">
                        ${s["cost_usd"]:.4f} spent · {funnel}
                      </div>
                    </div>
                  </div>
                  <div style="color: var(--text-0); font-size: 1.15rem; line-height: 1.65; font-weight: 500;">
                    {primary_rationale}
                  </div>
                  {meta_footer}
                </div>
                ''',
                unsafe_allow_html=True,
            )


# ===== Tab 3: Decisions =====
with tabs[2]:
    if not decisions:
        st.info("No decisions logged yet.")
    else:
        st.markdown(
            f'<div class="at-section-label">{len(decisions)} stages logged — newest first</div>',
            unsafe_allow_html=True,
        )
        for row in reversed(decisions):
            stage = row['stage']
            stage_icon = {
                "market_gate": "🕰️",
                "signals": "📊",
                "strategist": "🧠",
                "chain_lookup": "🔗",
                "construct": "🧩",
                "critic": "⚖️",
                "execute": "📤",
                "meta": "🕒",
                "monitor": "🛡️",
            }.get(stage, "•")
            with st.expander(
                f"{stage_icon}  {stage:<14} • {row['model']:<28} • "
                f"{_fmt_ts(row.get('started_at',''))} • ${row.get('cost_usd', 0):.4f}",
                expanded=False,
            ):
                st.json(row, expanded=False)
                run_dir = state.RUNS_DIR / row["run_id"]
                artifact = run_dir / row["output_ref"]
                if artifact.exists():
                    st.markdown(f"**Artifact:** `{artifact.relative_to(ROOT)}`")
                    if st.button(
                        f"View {stage} artifact",
                        key=f"view-{row['run_id']}-{stage}",
                    ):
                        st.json(json.loads(artifact.read_text()))


# ===== Tab 3: Performance =====
with tabs[3]:
    # Drive the entire P&L summary off the same SyntheticBalance the
    # hero card uses — no parallel computation, no risk of divergence.
    _synth = dd.compute_synthetic_balance(marks=broker_marks or {})
    # Modelled trading costs are kept as a separate sanity-floor
    # estimate — they're NOT in the synthetic balance formula
    # (real Alpaca fees from trades.jsonl are used instead).
    _modelled = pnl_lib.compute_portfolio_pnl(
        portfolio=portfolio,
        marks=broker_marks or None,
        costs=broker_costs or None,
    )

    st.markdown('<div class="at-section-label">Synthetic balance breakdown</div>',
                unsafe_allow_html=True)

    def _tone_for(v: float) -> str:
        return "pos" if v > 0 else "neg" if v < 0 else ""

    row1 = st.columns(3)
    row1[0].markdown(
        _stat_card(
            "Starting balance",
            f"${_synth.starting_balance_usd:,.2f}",
            sub="virtual baseline (CLAUDE.md spec)",
        ),
        unsafe_allow_html=True,
    )
    row1[1].markdown(
        _stat_card(
            "Closed gross P&L",
            f"${_synth.closed_gross_pnl_usd:+,.2f}",
            tone=_tone_for(_synth.closed_gross_pnl_usd),
            sub="Σ (sell − buy) × qty across closed trades",
        ),
        unsafe_allow_html=True,
    )
    row1[2].markdown(
        _stat_card(
            "Open gross P&L",
            f"${_synth.open_gross_pnl_usd:+,.2f}",
            tone=_tone_for(_synth.open_gross_pnl_usd),
            sub=(
                f"{_synth.unmarked_open_lots} open lot(s) unmarked"
                if _synth.unmarked_open_lots else
                "Σ (mark − buy) × qty across open lots"
            ),
        ),
        unsafe_allow_html=True,
    )

    row2 = st.columns(3)
    row2[0].markdown(
        _stat_card(
            "LLM cost (since reset)",
            f"${_synth.llm_cost_total_usd:,.4f}",
            sub="all-time Anthropic spend, reset-aware",
        ),
        unsafe_allow_html=True,
    )
    row2[1].markdown(
        _stat_card(
            "Trading fees",
            f"${_synth.trading_fees_total_usd:,.2f}",
            sub="real broker fees, never reset",
        ),
        unsafe_allow_html=True,
    )
    row2[2].markdown(
        _stat_card(
            "Synthetic balance",
            f"${_synth.synthetic_balance_usd:,.2f}",
            tone=_tone_for(_synth.synthetic_balance_usd - _synth.starting_balance_usd),
            sub="= start + closed + open − LLM − fees",
        ),
        unsafe_allow_html=True,
    )

    # Smaller, separate sanity-floor card. Explicitly labelled as
    # modelled (not used by the headline balance) so the operator
    # doesn't conflate it with the real-fees number above.
    st.markdown(
        '<div class="at-section-label" style="margin-top:0.6rem;">'
        'Modelled trading costs (sanity floor)</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        _stat_card(
            "Modelled round-trip cost",
            f"${_modelled.modelled_costs_usd:,.2f}",
            sub=(
                "IBKR-Pro-retail estimate for currently-open positions. "
                "NOT used in the synthetic balance — real Alpaca fees "
                "from trades.jsonl are deducted instead."
            ),
        ),
        unsafe_allow_html=True,
    )

    marks_status = (
        f"Live marks from Alpaca paper ({len(broker_marks)} positions matched)."
        if broker_marks else
        "No live marks yet — connect Alpaca paper keys to populate Open gross P&L."
    )
    st.caption(
        marks_status + " LLM cost is reset-aware (the Settings tab "
        "'Reset ALL LLM costs' button bumps the synthetic balance upward "
        "by the historical attribution). Trading fees are real and never "
        "reset — they reduce the balance permanently."
    )

    st.markdown('<div class="at-section-label">Realized balance curve</div>',
                unsafe_allow_html=True)
    # Time series of the realized synthetic balance — reconstructed
    # deterministically from trades.jsonl + costs.jsonl (no marks
    # history needed). Each step in the line is a real close or a
    # real cost row landing; open-position mark drift lives in the
    # hero card, not here.
    series = dd.realized_balance_series()
    if series:
        nav_df = pd.DataFrame(series)
        window_choice = st.radio(
            "Window",
            ["1D", "1W", "1M", "1Y", "All"],
            index=4,
            horizontal=True,
            label_visibility="collapsed",
            help="Filter the curve to the trailing window. Affects "
                 "this chart only; underlying trades/costs logs are "
                 "untouched.",
        )
        window_days = {"1D": 1, "1W": 7, "1M": 30, "1Y": 365}.get(window_choice)
        if window_days is not None and "at" in nav_df.columns:
            from datetime import datetime, timezone, timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
            nav_df = nav_df.copy()
            nav_df["_at_dt"] = pd.to_datetime(
                nav_df["at"].str.replace("Z", "+00:00", regex=False),
                utc=True,
                errors="coerce",
            )
            nav_df = nav_df[nav_df["_at_dt"] >= cutoff]
        if nav_df.empty:
            st.info(
                f"No realized events in the trailing {window_choice} window. "
                "Try a wider window — closes and LLM costs only land "
                "intermittently."
            )
        else:
            fig_nav = go.Figure()
            fig_nav.add_trace(go.Scatter(
                x=nav_df["at"],
                y=nav_df["synthetic_realized_balance_usd"],
                mode="lines+markers",
                name="Realized synthetic balance (USD)",
                line=dict(width=2.5, color="#059669"),
                marker=dict(size=5, color="#059669"),
                hovertemplate=(
                    "%{x}<br>Balance: $%{y:,.2f}"
                    "<br>Closed gross: $%{customdata[0]:,.2f}"
                    "<br>LLM: −$%{customdata[1]:,.2f}"
                    "<br>Fees: −$%{customdata[2]:,.2f}"
                    "<extra></extra>"
                ),
                customdata=nav_df[[
                    "closed_gross_pnl_usd",
                    "llm_cost_total_usd",
                    "trading_fees_total_usd",
                ]].values,
            ))
            fig_nav.update_layout(
                template="plotly_white",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=360, yaxis_title="Synthetic balance (USD)",
                yaxis=dict(gridcolor="#e2e8f0", color="#059669"),
                xaxis=dict(gridcolor="#e2e8f0"),
                margin=dict(l=10, r=10, t=20, b=10),
                legend=dict(orientation="h", y=1.1, font=dict(size=12)),
            )
            st.plotly_chart(fig_nav, width="stretch")
            if len(nav_df) == 1:
                st.caption(
                    "Single realized event in this window — the curve "
                    "renders as one marker rather than a line. Subsequent "
                    "closes or cost rows will extend it."
                )
            st.caption(
                "Reconstructed from `state/trades.jsonl` + `state/costs.jsonl`. "
                "Open-position mark drift is NOT plotted here — it lives "
                "on the hero card so this curve stays stable. LLM-cost "
                "resets visibly redraw the curve upward."
            )
    else:
        st.info(
            "No realized events yet — the curve populates with the first "
            "closed trade or LLM cost row."
        )

    st.markdown('<div class="at-section-label">LLM cost over time</div>', unsafe_allow_html=True)
    if costs:
        df = pd.DataFrame([
            {"at": r.get("at", ""), "cost_usd": r.get("cost_usd", 0.0)} for r in costs
        ])
        df["cum_cost"] = df["cost_usd"].cumsum()
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df["at"], y=df["cum_cost"], mode="lines",
            name="Cumulative LLM cost",
            line=dict(color="#2563eb", width=2.5),
            fill="tozeroy",
            fillcolor="rgba(37, 99, 235, 0.12)",
        ))
        fig.update_layout(
            template="plotly_white",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=320, yaxis_title="USD",
            yaxis=dict(gridcolor="#e2e8f0"),
            xaxis=dict(gridcolor="#e2e8f0"),
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False,
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No LLM cost history yet — run the orchestrator (live mode) to populate.")

    st.markdown(
        '<div class="at-section-label">Trading fees over time (real, from Alpaca fills)</div>',
        unsafe_allow_html=True,
    )
    fees_cum = dd.fees_running_total()
    if fees_cum:
        df_fees = pd.DataFrame(fees_cum)
        fig_fees = go.Figure()
        fig_fees.add_trace(go.Scatter(
            x=df_fees["at"], y=df_fees["cum_fees_usd"], mode="lines",
            name="Cumulative trading fees",
            line=dict(color="#d97706", width=2.5),  # amber to distinguish from blue LLM line
            fill="tozeroy",
            fillcolor="rgba(217, 119, 6, 0.12)",
        ))
        fig_fees.update_layout(
            template="plotly_white",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=320, yaxis_title="USD (fees)",
            yaxis=dict(gridcolor="#e2e8f0"),
            xaxis=dict(gridcolor="#e2e8f0"),
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False,
        )
        st.plotly_chart(fig_fees, width="stretch")
        total_fees = dd.total_trading_fees_usd()
        st.caption(
            f"All-time trading fees: **${total_fees:,.2f}** across "
            f"**{len(fees_cum)}** fills. Pulled per-fill from Alpaca activities — "
            f"unaffected by the LLM-cost reset (these are real paid fees)."
        )
    else:
        st.info(
            "No trading fees yet. trades.jsonl is empty — fills are populated "
            "once the orchestrator starts submitting paper orders and the "
            "Alpaca activities sync runs."
        )

    st.markdown('<div class="at-section-label">Trading fees by month</div>', unsafe_allow_html=True)
    fees_m = dd.fees_by_month()
    if fees_m:
        df_fm = pd.DataFrame(fees_m)
        df_fm["fees_usd"] = df_fm["fees_usd"].map(lambda v: f"${v:,.2f}")
        st.dataframe(
            df_fm.rename(columns={
                "month": "Month",
                "fills": "Fills",
                "fees_usd": "Fees (USD)",
            }),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No monthly trading-fee data yet.")

    st.markdown('<div class="at-section-label">Cost & tokens by month (this project only)</div>',
                unsafe_allow_html=True)
    by_month = dd.cost_by_month()
    if by_month:
        df_m = pd.DataFrame(by_month)
        df_m["cost_usd"] = df_m["cost_usd"].map(lambda v: f"${v:,.4f}")
        df_m["total_tokens"] = df_m["total_tokens"].map(lambda v: f"{v:,}")
        st.dataframe(
            df_m.rename(columns={
                "month": "Month",
                "calls": "Calls",
                "total_tokens": "Tokens",
                "cost_usd": "Cost (USD)",
            }),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No monthly cost data yet.")


# ===== Tab 4: Trades =====
with tabs[4]:
    st.markdown(
        '<div class="at-section-label">Per-trade PnL — gross − fees − attributed LLM cost</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Each row pairs a buy fill with the sell that closed it (FIFO). "
        "Fees are real, pulled per-fill from Alpaca activities. "
        "LLM cost is the opening run's total split evenly across the positions "
        "it opened (per the locked methodology). Net = gross − fees − LLM."
    )
    view = dd.trades_pnl_view(marks=broker_marks)
    # Local name MUST NOT shadow the module-level `totals` which the
    # Settings tab below reads as a dd.total_token_cost() dict (keys
    # `cost_usd`, `calls`, `total_tokens`, …). Streamlit re-runs every
    # `with tabs[N]:` block on each render, so a name collision here
    # leaks into the Settings tab and triggers
    # `KeyError: 'cost_usd'` on the lifetime-cost stat card.
    trade_totals = view["totals"]

    tcols = st.columns(4)
    for col, label, value, fmt in [
        (tcols[0], "Closed trades", trade_totals["closed_count"], "{}"),
        (tcols[1], "Open lots", trade_totals["open_count"], "{}"),
        (
            tcols[2],
            "Realised net",
            trade_totals["realised_net_usd"],
            "${:,.2f}",
        ),
        (
            tcols[3],
            "Realised fees",
            trade_totals["realised_fees_usd"],
            "${:,.2f}",
        ),
    ]:
        with col:
            cls = "pos" if (isinstance(value, (int, float)) and value > 0) else (
                "neg" if (isinstance(value, (int, float)) and value < 0) else ""
            )
            st.markdown(
                f'<div class="at-stat">'
                f'<div class="at-stat-label">{label}</div>'
                f'<div class="at-stat-value {cls}">{fmt.format(value)}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # Shared color formatter for Gross / Net columns on both the closed
    # and open tables. Codex P1 caught a previous version that defined
    # this inside the `if view["closed"]:` branch — when the closed list
    # was empty but open lots existed, the open-table block raised
    # UnboundLocalError. Hoisting to the outer scope keeps both branches
    # independently renderable.
    def _pnl_color(v):
        if v is None or (isinstance(v, float) and v != v):
            return "color: #94a3b8"
        return "color: #059669; font-weight: 600" if v >= 0 else "color: #dc2626; font-weight: 600"

    st.markdown(
        '<div class="at-section-label" style="margin-top:1.2rem;">'
        'Closed trades</div>',
        unsafe_allow_html=True,
    )
    if view["closed"]:
        df_closed = pd.DataFrame(view["closed"])
        df_closed = df_closed.rename(columns={
            "symbol": "Symbol",
            "kind": "Kind",
            "qty": "Qty",
            "buy_price": "Entry",
            "sell_price": "Exit",
            "opened_at": "Opened",
            "closed_at": "Closed",
            "gross_pnl_usd": "Gross",
            "fees_usd": "Fees",
            "llm_cost_usd": "LLM",
            "net_pnl_usd": "Net",
            "buy_run_id": "Run",
        })

        sty = df_closed.style.format({
            "Entry": "${:,.4f}",
            "Exit": "${:,.4f}",
            "Gross": "${:,.2f}",
            "Fees": "${:,.2f}",
            "LLM": "${:,.4f}",
            "Net": "${:,.2f}",
        }).map(_pnl_color, subset=["Gross", "Net"])
        st.dataframe(sty, width="stretch", hide_index=True)
    else:
        st.info(
            "No closed trades yet. Closed-trade rows appear once a position "
            "is fully sold and the activities sync picks up the close."
        )

    st.markdown(
        '<div class="at-section-label" style="margin-top:1.2rem;">'
        'Open lots (unrealised)</div>',
        unsafe_allow_html=True,
    )
    if view["open"]:
        df_open = pd.DataFrame(view["open"])
        df_open = df_open.rename(columns={
            "symbol": "Symbol",
            "kind": "Kind",
            "qty": "Qty",
            "buy_price": "Entry",
            "mark": "Mark",
            "opened_at": "Opened",
            "gross_pnl_usd": "Gross",
            "fees_usd": "Fees",
            "llm_cost_usd": "LLM",
            "net_pnl_usd": "Net",
            "buy_run_id": "Run",
        })
        sty_o = df_open.style.format({
            "Entry": "${:,.4f}",
            "Mark": lambda v: "—" if v is None else f"${v:,.4f}",
            "Gross": lambda v: "—" if v is None else f"${v:,.2f}",
            "Fees": "${:,.2f}",
            "LLM": "${:,.4f}",
            "Net": lambda v: "—" if v is None else f"${v:,.2f}",
        }).map(_pnl_color, subset=["Gross", "Net"])
        st.dataframe(sty_o, width="stretch", hide_index=True)
    else:
        st.info(
            "No open lots. Open lots populate once the activities sync writes "
            "fills into state/trades.jsonl."
        )


# ===== Tab 5: Agent Logs =====
with tabs[5]:
    if latest_rid is None:
        st.info("No runs yet.")
    else:
        st.markdown(
            f'<div class="at-section-label">Latest run · '
            f'<code style="color:var(--text-0);">{latest_rid}</code></div>',
            unsafe_allow_html=True,
        )
        run_dir = state.RUNS_DIR / latest_rid

        # Sanity report — surface as a structured panel above the JSON
        # dumps so the operator sees rule status at a glance, not buried
        # inside a "click to expand" envelope. Deterministic post-construct
        # rules; no LLM cost. See lib/sanity.py for rule list. The panel
        # only renders when sanity.json exists (runs predating PR γ won't
        # have one).
        sanity_path = run_dir / "sanity.json"
        if sanity_path.exists():
            try:
                sanity_doc = json.loads(sanity_path.read_text())
            except (json.JSONDecodeError, OSError):
                sanity_doc = None
            if sanity_doc:
                overall = sanity_doc.get("status", "pass")
                summary = sanity_doc.get("summary", {})
                badge_color = {
                    "pass": "var(--green)",
                    "warn": "var(--amber)",
                    "fail": "var(--red)",
                }.get(overall, "var(--text-1)")
                st.markdown(
                    f'<div class="at-section-label">🛡️  Sanity '
                    f'<span style="color:{badge_color}; font-weight:700;">'
                    f'{overall.upper()}</span> · '
                    f'pass={summary.get("pass", 0)} '
                    f'warn={summary.get("warn", 0)} '
                    f'fail={summary.get("fail", 0)} '
                    f'skip={summary.get("skip", 0)}</div>',
                    unsafe_allow_html=True,
                )
                rules = sanity_doc.get("rules", [])
                if rules:
                    rule_rows = []
                    for r in rules:
                        rule_rows.append({
                            "Rule": r.get("name", ""),
                            "Severity": r.get("severity", ""),
                            "Status": r.get("status", ""),
                            "Detail": r.get("detail", "") or "—",
                        })
                    st.dataframe(
                        pd.DataFrame(rule_rows),
                        width="stretch",
                        hide_index=True,
                    )

        artifact_icons = {
            "market_gate.json":   "🕰️",
            "signals.json":       "📊",
            "view.json":          "🧠",
            "chain_lookups.json": "🔗",
            "portfolio.json":     "🧩",
            "critique.json":      "⚖️",
            "sanity.json":        "🛡️",
            "orders.json":        "💱",
            "next_run.json":      "🕒",
        }
        for name, icon in artifact_icons.items():
            f = run_dir / name
            if f.exists():
                with st.expander(f"{icon}  {name} — {f.stat().st_size:,} bytes"):
                    st.json(json.loads(f.read_text()))

    st.markdown('<div class="at-section-label">Last 20 decisions</div>', unsafe_allow_html=True)
    if decisions:
        st.dataframe(
            pd.DataFrame(decisions[-20:])[
                ["run_id", "stage", "model", "cost_usd", "prompt_cache_hit_pct", "status"]
            ],
            width="stretch",
            hide_index=True,
            column_config={
                "cost_usd": st.column_config.NumberColumn("Cost", format="$%.4f"),
                "prompt_cache_hit_pct": st.column_config.NumberColumn("Cache hit", format="%.1f%%"),
            },
        )

    st.markdown('<div class="at-section-label">Next-run plan</div>', unsafe_allow_html=True)
    if state.NEXT_RUN.exists():
        st.json(state.read_json(state.NEXT_RUN))
    else:
        st.info("No next-run plan written yet.")


# ===== Tab 6: Settings =====
with tabs[6]:
    st.markdown('<div class="at-section-label">Mode</div>', unsafe_allow_html=True)
    mode_pills = []
    mode_pills.append('<span class="at-pill paper">● PAPER</span>' if not live_trading
                      else '<span class="at-pill live">● LIVE</span>')
    mode_pills.append('<span class="at-pill orders-on">⚡ ORDERS ON</span>' if orders_enabled
                      else '<span class="at-pill orders-off">○ orders off</span>')
    st.markdown('<div class="at-pills">' + "".join(mode_pills) + "</div>", unsafe_allow_html=True)
    st.caption(
        "Live trading is gated behind `LIVE_TRADING_ENABLED` and a hard-coded "
        "`LIVE_VERSION` constant. See CLAUDE.md §Promotion to live."
    )

    st.markdown('<div class="at-section-label">Cost ledger</div>', unsafe_allow_html=True)
    today = cost_today
    this_run = cost_this_run
    cc = st.columns(3)
    cc[0].markdown(_stat_card("Cost today", f"${today:.4f}",
                                sub=f"of ${DAILY_CAP_USD:.2f} daily cap"),
                    unsafe_allow_html=True)
    cc[1].markdown(_stat_card("Cost this run", f"${this_run:.4f}",
                                sub=f"of ${PER_RUN_CAP_USD:.2f} run cap"),
                    unsafe_allow_html=True)
    cc[2].markdown(_stat_card("Cost all time", f"${totals['cost_usd']:.4f}",
                                sub=f"{totals['calls']:,} LLM calls"),
                    unsafe_allow_html=True)

    cc2 = st.columns(3)
    cc2[0].markdown(_stat_card("Tokens lifetime", f"{totals['total_tokens']:,}"),
                     unsafe_allow_html=True)
    cc2[1].markdown(_stat_card("Cache reads", f"{totals['cache_read_input_tokens']:,}"),
                     unsafe_allow_html=True)
    cc2[2].markdown(_stat_card(
        "Cache hit rate",
        (f"{100.0 * totals['cache_read_input_tokens'] / max(1, totals['total_tokens']):.1f}%"
         if totals['total_tokens'] else "—"),
    ), unsafe_allow_html=True)
    st.caption(
        "All-time totals scoped to **this project** — aggregates `state/costs.jsonl` only."
    )

    st.markdown('<div class="at-section-label">Cost meter</div>', unsafe_allow_html=True)
    reset_at = state.read_cost_reset_at()
    if reset_at:
        st.info(
            f"Daily cost meter is currently filtered — only counting LLM spend "
            f"after **{_fmt_ts(reset_at)}**. Underlying `state/costs.jsonl` is "
            f"untouched (audit log is complete)."
        )
        cm = st.columns(2)
        with cm[0]:
            if st.button("🔄 Reset meter to now (zero it again)", help="Records a new reset marker at the current time."):
                state.set_cost_reset("dashboard")
                st.rerun()
        with cm[1]:
            if st.button("↩ Clear the reset (show full UTC-day total)"):
                state.clear_cost_reset()
                st.rerun()
    else:
        st.caption(
            "The daily-cost meter reads spend since 00:00 UTC by default. "
            "Press the button below to zero the meter at the current moment "
            "(e.g. discount today's testing churn). The `state/costs.jsonl` "
            "audit log is never mutated."
        )
        if st.button("🔄 Reset daily cost meter to $0", help="Records a reset marker. Audit log remains intact."):
            state.set_cost_reset("dashboard")
            st.rerun()

    st.markdown(
        '<div class="at-section-label">All-time LLM cost</div>',
        unsafe_allow_html=True,
    )
    all_time_reset_at = state.read_all_time_cost_reset_at()
    if all_time_reset_at:
        st.info(
            f"LLM-cost reset active — every dashboard surface counts only "
            f"spend after **{_fmt_ts(all_time_reset_at)}**. This bumps the "
            f"Performance tab Net P&L, the equity-curve cumulative-net line "
            f"and the Trades tab Realised net + per-row Net columns upward "
            f"by whatever was previously attributed. Underlying "
            f"`state/costs.jsonl` is intact; per-run / per-day cap "
            f"enforcement still uses the raw log."
        )
        at_cols = st.columns(2)
        with at_cols[0]:
            if st.button(
                "🔄 Re-reset all costs to now",
                help="Records a new all-time reset marker at the current time.",
            ):
                state.set_all_time_cost_reset("dashboard")
                st.rerun()
        with at_cols[1]:
            if st.button("↩ Clear all-time reset (show full history)"):
                state.clear_all_time_cost_reset()
                st.rerun()
    else:
        st.caption(
            "Pressing the button below stamps an all-time reset marker. From "
            "that moment, LLM cost is treated as $0 across **every** dashboard "
            "surface that subtracts it from P&L — the Performance tab Net P&L "
            "card, the equity-curve Cumulative Net P&L line, and the Trades "
            "tab Realised net + per-row Net columns all bump upward by the "
            "previously-attributed amount. Useful after a testing burn or "
            "model-config change you want to draw a line under. The underlying "
            "`state/costs.jsonl` audit log is preserved (never mutated) and "
            "per-run / per-day cap enforcement continues to use the raw log."
        )
        if st.button(
            "🧹 Reset ALL LLM costs to $0",
            help="Records an all-time reset marker. costs.jsonl audit log is "
                 "preserved; per-run and per-day caps continue to use the raw "
                 "log. Net P&L surfaces (Performance, Trades, equity curve) "
                 "all reflect the reset.",
        ):
            state.set_all_time_cost_reset("dashboard")
            st.rerun()

    # NAV anchor + manual baseline UI removed in the synthetic-balance
    # refactor — the dashboard no longer derives its headline from
    # Alpaca account equity, so there's nothing to "anchor". See
    # lib/dashboard_data.SyntheticBalance for the new source of truth.
    # Stale state files (state/nav_offset.json,
    # state/nav_manual_baseline.json) are harmlessly left on disk and
    # wiped by the "Wipe history" button below.

    st.markdown('<div class="at-section-label">Wipe history (start fresh)</div>', unsafe_allow_html=True)
    st.caption(
        "Clears `state/decisions.jsonl`, `state/trades.jsonl`, "
        "`state/nav_history.jsonl`, `state/runs/*`, and the portfolio / "
        "next-run / dedup-hash snapshots. By default also clears "
        "`state/costs.jsonl` so per-run + daily caps reset to $0. "
        "`state/halt.flag` is preserved (this button doesn't override your stop intent). "
        "A timestamped backup is dropped under `state/backup_<utc>/` "
        "before anything is deleted — you can restore from there if "
        "needed."
    )

    include_costs = st.checkbox(
        "Also clear `state/costs.jsonl` (audit log + cap enforcement reset to $0)",
        value=True,
        help="Uncheck this if you want to keep the cost audit log untouched. "
             "Caps continue to enforce against historical spend.",
    )

    if not st.session_state.get("wipe_confirm_pending"):
        if st.button(
            "🧹 Wipe history & runs",
            help="Removes all per-cycle artifacts and decision/trade/NAV logs. "
                 "Two-step confirmation; backup created first.",
        ):
            st.session_state["wipe_confirm_pending"] = True
            st.rerun()
    else:
        st.warning(
            "⚠️ Press **Confirm wipe** to delete all run history "
            f"({'including' if include_costs else 'excluding'} the cost "
            "audit log). The backup will land in `state/backup_<utc>/`."
        )
        wc_cols = st.columns(2)
        with wc_cols[0]:
            if st.button("✅ Confirm wipe", type="primary"):
                result = state.wipe_run_history(include_costs=include_costs)
                st.session_state["wipe_confirm_pending"] = False
                st.success(
                    f"Wiped {result['runs_dirs_removed']} run dirs, "
                    f"truncated {len(result['jsonl_truncated'])} log files, "
                    f"removed {len(result['snapshots_removed'])} snapshots. "
                    f"Backup: `{result['backup_dir'] or '<failed>'}`"
                )
                st.rerun()
        with wc_cols[1]:
            if st.button("↩ Cancel"):
                st.session_state["wipe_confirm_pending"] = False
                st.rerun()

    st.markdown('<div class="at-section-label">Halt flag</div>', unsafe_allow_html=True)
    if halted:
        st.error(f"Orchestrator is HALTED. Flag file: `{state.HALT_FLAG}`")
        if st.button("Clear halt flag", type="primary"):
            state.clear_halt()
            st.rerun()
    else:
        st.success("Orchestrator is not halted.")
        if st.button("🛑 Emergency stop (write halt.flag)"):
            state.set_halt("dashboard")
            st.rerun()

    st.markdown('<div class="at-section-label">Auto-refresh</div>', unsafe_allow_html=True)
    st.caption(
        "Streamlit dashboards don't refresh on their own — the hero NAV, "
        "positions table, and equity curve only update when you reload the "
        "page (or hit the manual button below). Toggle this on to insert "
        "a `meta http-equiv=\"refresh\"` tag into the page so the browser "
        "reloads every N seconds. Off by default to avoid surprise re-runs "
        "interrupting your reading."
    )
    # Codex P1 on PR #73: the meta refresh starts a NEW Streamlit
    # session each tick, which would reset st.checkbox(value=False) to
    # default and drop the meta tag — so auto-refresh would fire exactly
    # ONCE then die. Persist the toggle + interval in URL query params
    # because the meta refresh preserves the URL (no target specified =
    # reloads current URL including query string). New sessions then
    # read the params and rebuild the same widget state, keeping the
    # loop alive across arbitrarily many reloads.
    params = st.query_params
    autorefresh_param = params.get("autorefresh", "0") == "1"
    try:
        interval_param = int(params.get("interval", "60"))
    except (TypeError, ValueError):
        interval_param = 60
    interval_param = max(15, min(300, interval_param))

    auto_refresh_on = st.checkbox(
        "Auto-refresh enabled", value=autorefresh_param,
        help="Reload the whole page every N seconds. Live broker NAV / "
             "positions / fills will tick forward without a manual refresh. "
             "State persists across reloads via URL query params, so a once-"
             "enabled toggle keeps refreshing until you uncheck it.",
    )
    refresh_seconds = st.slider(
        "Refresh interval (seconds)",
        min_value=15, max_value=300, value=interval_param, step=15,
        disabled=not auto_refresh_on,
        help="60s matches the scheduler's poll cadence; 30s is fine if you "
             "want tighter live-mark updates during a position you're watching.",
    )

    # Sync widget state → URL query params. Only mutates params when
    # the desired state diverges from the URL to avoid a redundant
    # rerun loop on every render.
    desired_autorefresh = "1" if auto_refresh_on else "0"
    desired_interval = str(refresh_seconds) if auto_refresh_on else None
    if params.get("autorefresh", "0") != desired_autorefresh:
        params["autorefresh"] = desired_autorefresh
    if auto_refresh_on and params.get("interval") != desired_interval:
        params["interval"] = desired_interval
    elif not auto_refresh_on and "interval" in params:
        del params["interval"]

    if auto_refresh_on:
        st.markdown(
            f'<meta http-equiv="refresh" content="{refresh_seconds}">',
            unsafe_allow_html=True,
        )
        st.success(
            f"Page will reload every {refresh_seconds}s. "
            "Toggle state lives in the URL (`?autorefresh=1&interval=…`) "
            "so the loop survives the reload."
        )

    st.markdown('<div class="at-section-label">Manual actions</div>', unsafe_allow_html=True)
    if st.button("🔄 Refresh data"):
        st.rerun()
    st.markdown(
        f"[📖 README]({(ROOT / 'README.md').as_uri()}) &nbsp;·&nbsp; "
        f"[📋 CLAUDE.md]({(ROOT / 'CLAUDE.md').as_uri()})"
    )
