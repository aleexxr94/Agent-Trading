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
    📈 vs S&P 500 — strategy NAV vs hypothetical buy-and-hold SPY (total return) since inception
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

from lib import benchmark as bench
from lib import dashboard_data as dd
from lib import pnl as pnl_lib
from lib import state

ROOT = Path(__file__).resolve().parent


# ---------- cached views ----------
# Streamlit re-runs the whole script on every interaction (tab clicks,
# button presses, even mouse hover on Plotly charts). The JSONL readers
# below are cheap individually but together add up — and trades_pnl_view
# in particular is called from BOTH the Performance synthetic-balance
# block and the Trades tab on every render, doing FIFO matching twice
# for nothing. Wrapping them with `st.cache_data` and keying on file
# mtimes gives sub-millisecond cache hits until the orchestrator (or
# the operator via "Resync") writes a new row.
#
# `lib/dashboard_data.py` deliberately stays Streamlit-free; caching
# lives here at the call-site layer.


def _state_mtimes() -> tuple[int, int, int, int]:
    """File-mtime tuple used as a cache key. Returns 0 for missing files
    so the cache key is stable before the orchestrator has written any
    state."""
    def _m(p):
        try:
            return p.stat().st_mtime_ns
        except FileNotFoundError:
            return 0
    return (
        _m(state.TRADES_LOG),
        _m(state.COSTS_LOG),
        _m(state.NAV_HISTORY_LOG),
        _m(state.DECISIONS_LOG),
    )


def _marks_key(marks: dict | None) -> tuple:
    """Hashable cache key for a marks dict — Streamlit's cache_data
    can hash tuples of primitives reliably."""
    if not marks:
        return ()
    return tuple(sorted((str(k), float(v)) for k, v in marks.items()))


@st.cache_data(ttl=15, show_spinner=False)
def _cached_trades_pnl_view(_mtimes: tuple, marks_key: tuple) -> dict:
    marks = {k: v for k, v in marks_key} if marks_key else None
    return dd.trades_pnl_view(marks=marks)


@st.cache_data(ttl=15, show_spinner=False)
def _cached_realized_balance_series(_mtimes: tuple) -> list:
    return dd.realized_balance_series()


@st.cache_data(ttl=15, show_spinner=False)
def _cached_fees_running_total(_mtimes: tuple) -> list:
    return dd.fees_running_total()


@st.cache_data(ttl=15, show_spinner=False)
def _cached_fees_by_month(_mtimes: tuple) -> list:
    return dd.fees_by_month()


@st.cache_data(ttl=15, show_spinner=False)
def _cached_cost_by_month(_mtimes: tuple) -> list:
    return dd.cost_by_month()


@st.cache_data(ttl=15, show_spinner=False)
def _cached_cost_by_stage(_mtimes: tuple) -> list:
    return dd.cost_by_stage()


@st.cache_data(ttl=15, show_spinner=False)
def _cached_cache_hit_trend(_mtimes: tuple) -> list:
    return dd.cache_hit_trend()


@st.cache_data(ttl=15, show_spinner=False)
def _cached_nav_history(_mtimes: tuple) -> list:
    return dd.load_nav_history()


@st.cache_data(ttl=15, show_spinner=False)
def _cached_run_ids(_mtimes: tuple) -> list[str]:
    """All run_ids under state/runs/, newest first. Run-dir names start
    with a UTC timestamp so a reverse lexicographic sort is newest-first
    (same trick as dd.load_run_summaries)."""
    try:
        if not state.RUNS_DIR.exists():
            return []
        return sorted(
            (p.name for p in state.RUNS_DIR.iterdir() if p.is_dir()),
            reverse=True,
        )
    except OSError:
        return []


# ---------- chart helpers ----------
# Plotly's defaults make every chart click-to-zoom, which on touch/mobile
# fires an instant zoom on tap. NO_ZOOM_CONFIG + per-axis fixedrange=True
# disables all zoom/pan while leaving hover tooltips alive (staticPlot
# would kill hover, so it stays False).
NO_ZOOM_CONFIG = {
    "displayModeBar": False,
    "scrollZoom": False,
    "doubleClick": False,
    "staticPlot": False,
    "displaylogo": False,
}


def _fnum(v, default: float = 0.0) -> float:
    """Coerce a possibly-None / NaN / string cell to float for plotting
    and money formatting."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if pd.isna(f) else f


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _tight_yrange(ys, min_pad: float = 5.0, frac: float = 0.08):
    """Explicit y-range hugging the data with proportional padding (and a
    dollar floor so a flat line still has headroom). Replaces autorange,
    which gets dragged wide by the single live tip and flattens the line."""
    vals = [_fnum(v) for v in ys if v is not None and not (isinstance(v, float) and pd.isna(v))]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * frac, min_pad)
    return [lo - pad, hi + pad]


def _style_fig(
    fig,
    *,
    height: int = 380,
    yaxis_title: str = "",
    yrange=None,
    legend: bool = False,
    right_margin: int = 10,
) -> None:
    """Apply the shared light-theme Plotly layout in one place: white
    template on transparent backgrounds, slate gridlines, zoom disabled
    (pairs with NO_ZOOM_CONFIG), tight margins, unified hover style."""
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=height,
        yaxis_title=yaxis_title,
        xaxis_title="",
        yaxis=dict(gridcolor="#e2e8f0", fixedrange=True, range=yrange),
        xaxis=dict(gridcolor="#e2e8f0", fixedrange=True),
        margin=dict(l=10, r=right_margin, t=20 if legend else 10, b=10),
        hoverlabel=dict(bgcolor="white", bordercolor="#e2e8f0", font_size=13),
        dragmode=False,
        showlegend=legend,
    )
    if legend:
        fig.update_layout(
            legend=dict(orientation="h", y=1.1, font=dict(size=12)),
        )


def _render_balance_chart(
    *, xs, ys, hover_texts, yaxis_title: str, caption: str,
    live_transition_at: str | None = None,
) -> None:
    """CoinGecko-style balance chart: one smooth area line that flows into
    the current value at a labelled end dot. Direction-aware colour (green
    up / red down over the visible window), tight y-axis, zoom disabled.
    ``live_transition_at`` (ISO UTC) draws a dotted LIVE marker at the first
    point of the live era, segmenting paper history from real-money cycles."""
    up = len(ys) < 2 or _fnum(ys[-1]) >= _fnum(ys[0])
    col = "#059669" if up else "#dc2626"
    shape = "spline" if len(ys) > 2 else "linear"
    yrange = _tight_yrange(ys)
    ybottom = yrange[0] if yrange else 0.0
    fig = go.Figure()
    # Invisible baseline at the bottom of the visible band so the gradient
    # fill fades across the whole band. fill="tozeroy" on a non-zero axis
    # would push the fade off-screen and leave a flat block of colour.
    fig.add_trace(go.Scatter(
        x=xs, y=[ybottom] * len(xs), mode="lines",
        line=dict(width=0, color="rgba(0,0,0,0)"),
        hoverinfo="skip", showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines",
        line=dict(width=2.5, color=col, shape=shape, smoothing=0.5),
        fill="tonexty",
        fillgradient=dict(type="vertical", colorscale=[
            [0.0, _rgba(col, 0.0)],
            [1.0, _rgba(col, 0.32)],
        ]),
        customdata=[[t] for t in hover_texts],
        hovertemplate="%{customdata[0]}<extra></extra>",
        showlegend=False,
    ))
    # Terminal dot marking the current value (hover handled by the line).
    fig.add_trace(go.Scatter(
        x=[xs[-1]], y=[ys[-1]], mode="markers",
        marker=dict(size=8, color=col, line=dict(width=2, color="white")),
        hoverinfo="skip", showlegend=False,
    ))
    fig.add_annotation(
        x=xs[-1], y=ys[-1], text=f"${_fnum(ys[-1]):,.2f}",
        showarrow=False, xanchor="left", yanchor="middle", xshift=10,
        font=dict(size=13, color=col),
        bgcolor=_rgba(col, 0.10), bordercolor=col, borderwidth=1, borderpad=4,
    )
    if live_transition_at:
        # Anchor the marker to the first plotted point inside the live era
        # (works on both categorical and date x-axes; ISO strings compare
        # chronologically). No point in range → transition predates/postdates
        # the visible window, skip silently.
        vx = next((x for x in xs if str(x) >= live_transition_at), None)
        if vx is not None:
            fig.add_vline(x=vx, line_width=1, line_dash="dot", line_color="#f59e0b")
            fig.add_annotation(
                x=vx, y=1.0, yref="paper", text="LIVE", showarrow=False,
                yanchor="bottom", font=dict(size=10, color="#f59e0b"),
            )
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=380, yaxis_title=yaxis_title,
        yaxis=dict(gridcolor="#e2e8f0", color=col, fixedrange=True, range=yrange),
        xaxis=dict(gridcolor="#e2e8f0", fixedrange=True),
        margin=dict(l=10, r=70, t=20, b=10),
        dragmode=False, showlegend=False,
    )
    st.plotly_chart(fig, width="stretch", config=NO_ZOOM_CONFIG)
    if caption:
        st.caption(caption)


RISK_WARNING_TEXT = (
    "PAPER TRADING — experimental autonomous AI agent. Leveraged & inverse "
    "ETFs on a small account are high-risk. Not financial advice."
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

      /* Pinned TOTAL footer beneath the positions dataframe. Visually
         tied to the table above via a border-top that mirrors the
         dataframe's row separator, but rendered as a separate element
         so the dataframe's column sort can't reorder it. */
      .at-total-footer {
        display: flex; flex-wrap: wrap;
        align-items: center; gap: 0.8rem;
        padding: 0.55rem 0.85rem;
        margin-top: -0.25rem;  /* tighten the gap to the dataframe */
        border-top: 2px solid var(--border);
        background: var(--bg-2);
        font-size: 0.95rem;
        font-weight: 500;
      }
      .at-total-footer .at-total-label {
        font-weight: 800; letter-spacing: 0.04em;
        color: var(--text-0);
        padding: 0.1rem 0.5rem;
        border: 1px solid var(--border);
        border-radius: 4px;
        background: var(--bg-1);
        font-size: 0.85rem;
      }
      .at-total-footer .at-total-cells strong { font-weight: 700; }

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
        transition: box-shadow 0.15s ease;
        min-width: 0;
        overflow-wrap: break-word;
      }
      .at-stat:hover { box-shadow: 0 2px 8px rgba(15, 23, 42, 0.08); }
      .at-stat-label { color: var(--text-1); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }
      .at-stat-value { color: var(--text-0); font-size: 1.7rem; font-weight: 700; margin-top: 0.2rem; letter-spacing: -0.01em; }
      .at-stat-sub { color: var(--text-1); font-size: 0.85rem; margin-top: 0.25rem; font-weight: 500; }
      .at-stat-value.pos { color: var(--green); }
      .at-stat-value.neg { color: var(--red); }
      .at-stat-value.warn { color: var(--amber); }
      /* small delta chip rendered beside a stat value */
      .at-stat-delta {
        display: inline-block; vertical-align: middle;
        font-size: 0.85rem; font-weight: 700;
        padding: 0.1rem 0.45rem; margin-left: 0.45rem;
        border-radius: 999px; border: 1px solid var(--border);
        background: var(--bg-2); color: var(--text-1);
      }
      .at-stat-delta.pos { background: var(--green-soft); color: var(--green-text); border-color: #a7f3d0; }
      .at-stat-delta.neg { background: var(--red-soft);   color: var(--red-text);   border-color: #fecaca; }

      /* cost meter bar */
      .at-meter { height: 7px; background: var(--bg-2); border-radius: 999px; overflow: hidden; margin-top: 0.5rem; }
      .at-meter-fill { height: 100%; background: var(--green); border-radius: 999px; transition: width 0.4s; }
      .at-meter-fill.warn { background: var(--amber); }
      .at-meter-fill.danger { background: var(--red); }

      /* tabs */
      .stTabs [data-baseweb="tab-list"] { gap: 0.3rem; border-bottom: 1px solid var(--border); }
      .stTabs [data-baseweb="tab"] { padding: 0.65rem 1.2rem; font-size: 0.95rem; font-weight: 600; }
      .stTabs [data-baseweb="tab-highlight"] { background-color: var(--green); height: 3px; border-radius: 3px 3px 0 0; }

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
        border-left: 3px solid var(--green);
        padding-left: 0.5rem;
      }

      /* small-muted text */
      .small-muted { color: var(--text-2); font-size: 0.85rem; }

      /* monthly outperformance row tint (vs S&P 500 tab) */
      .at-month-pos { background-color: var(--green-soft); }
      .at-month-neg { background-color: var(--red-soft); }
      .at-help-icon { font-size: 0.85em; opacity: 0.55; cursor: help; margin-left: 4px; }

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


def _fmt_countdown(iso: str) -> str:
    """'in 3h 12m' / 'in 14m' / 'overdue 22m' relative to now; '' on bad
    input. Accurate as of page render — Streamlit doesn't tick live."""
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return ""
    prefix, secs = ("in", secs) if secs >= 0 else ("overdue", -secs)
    mins = int(secs // 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days > 0:
        return f"{prefix} {days}d {hours}h"
    if hours > 0:
        return f"{prefix} {hours}h {mins}m"
    return f"{prefix} {mins}m"


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
_synth_live = dd.compute_synthetic_balance(
    marks=broker_marks or {},
    portfolio=portfolio,
    broker_costs=broker_costs or {},
    held_keys=broker_view.held_keys if broker_view.available else None,
)
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
    f"${_synth_live.trading_fees_total_usd + _synth_live.slippage_total_usd:,.2f} costs"
)
# Build the conditional sub-line block as a SINGLE concatenated
# string. Each line is included only when its data is non-empty.
# Critical: assemble with str concatenation, not interpolation of
# three separate variables on separate template lines. The latter
# leaves blank lines between empty-string interpolations, and
# Streamlit's markdown parser interprets ≥1 blank line inside an
# HTML block as end-of-block — every tag that follows then renders
# as literal text (the "</div>" string you may have seen in a
# screenshot was exactly this).
extra_lines_parts: list[str] = []
# Informational-only Alpaca equity row. Always labelled and rendered
# in a muted style so it's obviously NOT the source of truth.
if broker_view.available and broker_view.nav_usd is not None:
    extra_lines_parts.append(
        f'<div class="at-hero-sub" style="opacity:0.7; font-size:0.85rem;">'
        f'Alpaca account: <strong>${broker_view.nav_usd:,.2f}</strong> '
        f'<span style="color: var(--text-2);">(informational — not used '
        f'for any dashboard calculation)</span>'
        f'</div>'
    )
if _synth_live.unmarked_open_lots > 0:
    extra_lines_parts.append(
        f'<div class="at-hero-sub" style="opacity:0.7; font-size:0.85rem; color: var(--amber-text);">'
        f'{_synth_live.unmarked_open_lots} open lot(s) without live marks '
        f'— their P&L contribution treated as $0 until marks return.'
        f'</div>'
    )
extra_lines = "".join(extra_lines_parts)

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
            &nbsp;•&nbsp; Next: <strong>{_fmt_ts(next_run_at)}</strong>{
                f' <span style="color:var(--text-2);">({_fmt_countdown(next_run_at)})</span>'
                if _fmt_countdown(next_run_at) else ""
            }
            &nbsp;•&nbsp; Source: <strong>{source}</strong>
          </div>{extra_lines}
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

_recent_chips = dd.closed_trade_chips(marks=broker_marks or {}, limit=6)
_recent_chips_html = "".join(
    f'<span class="at-chip {"pos" if c["net_pnl_usd"] >= 0 else "neg"}">'
    f'{html.escape(c["symbol"])} '
    f'<strong>{"+" if c["net_pnl_usd"] >= 0 else ""}${c["net_pnl_usd"]:,.2f}</strong>'
    f'</span>'
    for c in _recent_chips
) or '<span style="color: var(--text-2); font-size: 0.9rem;">No closed trades yet — chips appear here as positions close.</span>'

_agg_chips = dd.closed_trade_chips_by_ticker(marks=broker_marks or {}, limit=12)

def _agg_chip_html(c: dict) -> str:
    tone = "pos" if c["net_pnl_usd"] >= 0 else "neg"
    count_suffix = f" ×{c['trade_count']}" if c["trade_count"] > 1 else ""
    sign = "+" if c["net_pnl_usd"] >= 0 else ""
    return (
        f'<span class="at-chip {tone}">'
        f'{html.escape(c["symbol"])}{count_suffix} '
        f'<strong>{sign}${c["net_pnl_usd"]:,.2f}</strong>'
        f'</span>'
    )

_agg_chips_html = "".join(_agg_chip_html(c) for c in _agg_chips)

_strip_label_style = (
    "font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; "
    "font-weight: 600; color: var(--text-2); margin-bottom: 0.25rem;"
)
_recent_block = (
    f'<div>'
    f'<div style="{_strip_label_style}">Recent closes</div>'
    f'<div style="display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center;">'
    f'{_recent_chips_html}'
    f'</div>'
    f'</div>'
)
_agg_block = (
    f'<div>'
    f'<div style="{_strip_label_style}">By ticker (all-time)</div>'
    f'<div style="display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center;">'
    f'{_agg_chips_html}'
    f'</div>'
    f'</div>'
) if _agg_chips else ""

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
        <div style="flex: 1; min-width: 280px; display: flex; flex-direction: column; gap: 0.6rem;">
          {_recent_block}
          {_agg_block}
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


def _stat_card(
    label: str,
    value: str,
    *,
    sub: str = "",
    tone: str = "",
    help_text: str = "",
    delta: str = "",
) -> str:
    cls = f"at-stat-value {tone}".strip()
    sub_html = f'<div class="at-stat-sub">{sub}</div>' if sub else ""
    help_html = (
        f'<span class="at-help-icon" title="{html.escape(help_text)}">ⓘ</span>'
        if help_text else ""
    )
    delta_html = ""
    if delta:
        # Signed string like "+1.2%" / "-0.8%" — arrow + tone from the sign.
        negative = delta.lstrip().startswith(("-", "−"))
        arrow = "▼" if negative else "▲"
        dcls = "neg" if negative else "pos"
        delta_html = (
            f'<span class="at-stat-delta {dcls}">{arrow} '
            f'{html.escape(delta.lstrip("+-− "))}</span>'
        )
    return (
        f'<div class="at-stat">'
        f'<div class="at-stat-label">{label}{help_html}</div>'
        f'<div class="{cls}">{value}{delta_html}</div>'
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
    "📈 vs S&P 500",
    "💱 Trades",
    "🤖 Agent Logs",
    "🎯 Calibration",
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
        closed_labels = [p["symbol"] for p in closed_positions]
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
        # columns that mix int + None ("Days held" is None when no trade
        # history covers the symbol). Map sentinels to pandas's nullable
        # Int64 so the column renders as a number where data exists and
        # blank where it doesn't.
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

        # Aggregate row: pinned to the BELOW the table so operator
        # sorts on the data rows (ascending / descending) don't move
        # it. Streamlit's st.dataframe has no frozen-row primitive, so
        # we keep the TOTAL outside the dataframe entirely — rendered
        # as a styled footer line whose columns mirror the data
        # sums the operator wants to see at a glance.
        def _sum_col(col: str) -> float | None:
            if col not in df_pos.columns:
                return None
            vals = [v for v in df_pos[col] if isinstance(v, (int, float)) and v == v]
            return sum(vals) if vals else None

        # Δ% lives in the same green/red semantic space as the P&L
        # columns — apply the same color formatter so a move-since-entry
        # of -8% reads red at a glance.
        color_subset = [c for c in ("Gross P&L", "Net P&L", "Δ%") if c in df_pos.columns]
        # na_rep="—" turns every NaN / pd.NA cell into an em-dash
        # (matches the existing "no data" sentinel used in row builds).
        # Without this, Pandas Styler renders pd.NA as the literal
        # string "None" — which is what shows up for Days held when
        # marks aren't wired. `precision=None` keeps
        # column_config's per-column NumberColumn(format=...) rules
        # in charge of numeric rendering for the non-NA cells.
        styled = (
            df_pos.style
            .format(na_rep="—", precision=None)
            .map(_color_pnl, subset=color_subset)
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
                         "Entry/add cap is 15% (→7.5% in ≥10% drawdown); an "
                         "already-open winner may drift up to the 25% hold "
                         "ceiling (→12.5% in drawdown) before a forced trim.",
                ),
                "Days held": st.column_config.NumberColumn(
                    "Days held",
                    format="%d",
                    help="Whole-days since the earliest currently-open buy "
                         "lot for this symbol (FIFO over state/trades.jsonl). "
                         "Resets after a full close and reopen.",
                ),
                "Bias":      st.column_config.Column(
                    "Bias",
                    help="Direction expressed by the position: Bull (bull "
                         "ETF), Bear (inverse ETF), Long vol (UVXY), "
                         "Long crypto (BITX), Short crypto (BITI).",
                ),
                "Kill":      st.column_config.Column(
                    "Kill",
                    help="Trigger conditions monitor.py uses to flatten "
                         "the position: max-loss %, ETF price thresholds, "
                         "and time stop (date).",
                ),
                "Fees":      st.column_config.NumberColumn(
                    "Fees",
                    format="$%,.2f",
                    help="Modelled round-trip Alpaca cost for this position "
                         "(lib/alpaca_costs.py): entry-leg slippage already "
                         "paid, plus projected close (slippage + sell-side "
                         "SEC/FINRA fees). Commission is $0. "
                         "Net P&L = Gross P&L − Fees.",
                ),
                "Gross P&L": st.column_config.NumberColumn("Gross P&L", format="$%+,.2f"),
                "Net P&L":   st.column_config.NumberColumn("Net P&L",  format="$%+,.2f"),
            },
        )

        # ---------- Pinned TOTAL footer ----------
        # Anchored below the dataframe so column sorts on the data
        # rows don't move it. Operator asked for this on May 14:
        # ascending / descending sorts were dragging the in-table
        # TOTAL row to the top or bottom, making it look like just
        # another data point. Streamlit's st.dataframe has no
        # frozen-row primitive, so we render the totals as a
        # separate compact line below the table — visually it reads
        # as a footer pinned to the table's bottom edge.
        _total_notional = _sum_col("Notional")
        _total_pct = _sum_col("% NAV")
        _total_fees = _sum_col("Fees")
        _total_gross = _sum_col("Gross P&L")
        _total_net = _sum_col("Net P&L")

        def _fmt_signed_usd(v: float | None) -> str:
            if v is None:
                return "—"
            return f"${v:+,.2f}".replace("$+", "+$").replace("$-", "−$")

        def _tone_color(v: float | None) -> str:
            if v is None or v == 0:
                return "var(--text-1)"
            return "#059669" if v > 0 else "#dc2626"

        total_cells_html = (
            f'<span style="color: var(--text-2);">Notional</span> '
            f'<strong>${_total_notional:,.0f}</strong>'
            if _total_notional is not None else ""
        )
        if _total_pct is not None:
            total_cells_html += (
                f' &nbsp;·&nbsp; <span style="color: var(--text-2);">% NAV</span> '
                f'<strong>{_total_pct:.1f}%</strong>'
            )
        if _total_fees is not None:
            total_cells_html += (
                f' &nbsp;·&nbsp; <span style="color: var(--text-2);">Fees</span> '
                f'<strong>${_total_fees:,.2f}</strong>'
            )
        if _total_gross is not None:
            total_cells_html += (
                f' &nbsp;·&nbsp; <span style="color: var(--text-2);">Gross P&L</span> '
                f'<strong style="color: {_tone_color(_total_gross)};">'
                f'{_fmt_signed_usd(_total_gross)}</strong>'
            )
        if _total_net is not None:
            total_cells_html += (
                f' &nbsp;·&nbsp; <span style="color: var(--text-2);">Net P&L</span> '
                f'<strong style="color: {_tone_color(_total_net)};">'
                f'{_fmt_signed_usd(_total_net)}</strong>'
            )
        st.markdown(
            f'<div class="at-total-footer">'
            f'<span class="at-total-label">TOTAL</span>'
            f'<span class="at-total-cells">{total_cells_html}</span>'
            f'</div>',
            unsafe_allow_html=True,
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

    # ---------- Universe reference ----------
    # Plain-English explainer for every ticker the agent may trade, below
    # the charts. Rows with an open position sort to the top and get a
    # green/red row background by Net P&L sign. Purely informational —
    # nothing above reads from it.
    st.markdown('<div class="at-section-label">Universe reference</div>', unsafe_allow_html=True)
    open_pnl_by_symbol = {r["Symbol"]: r.get("Net P&L") for r in rows}
    uni_rows = dd.universe_explainer_rows(open_pnl_by_symbol)
    df_uni = pd.DataFrame(uni_rows)
    uni_status = df_uni.pop("_status")
    # Pre-format to strings: the data grid formats NumberColumn cells from
    # the raw value (styler display text is ignored there), and a raw None
    # renders as the literal "None" — a string column renders verbatim.
    df_uni["Open P&L"] = [
        "—" if pd.isna(v) else f"${v:+,.2f}" for v in df_uni["Open P&L"]
    ]

    def _universe_row_style(row: "pd.Series") -> list[str]:
        status = uni_status.loc[row.name]
        if status == "win":
            css = "background-color: #d1fae5; color: #065f46"
        elif status == "loss":
            css = "background-color: #fee2e2; color: #991b1b"
        elif status == "open":
            css = "background-color: #e2e8f0; color: #0f172a"
        else:
            return [""] * len(row)
        return [css] * len(row)

    styled_uni = (
        df_uni.style
        .format(na_rep="—", precision=None)
        .apply(_universe_row_style, axis=1)
    )
    st.dataframe(
        styled_uni,
        width="stretch",
        hide_index=True,
        height=430,
        column_config={
            "Symbol":    st.column_config.Column("Symbol", width="small"),
            "Factor":    st.column_config.Column("Factor", width="small"),
            "Direction": st.column_config.Column(
                "Direction", width="small",
                help="Bull = rises with the factor; Bear = inverse ETF that "
                     "rises when the factor falls (the system never shorts).",
            ),
            "Leverage":  st.column_config.Column("Leverage", width="small"),
            "Pair":      st.column_config.Column(
                "Pair", width="small",
                help="Opposite-direction ETF on the same factor; — for solo "
                     "lines with no liquid counterpart.",
            ),
            "Explainer": st.column_config.Column("What it is", width="large"),
            "Open P&L":  st.column_config.Column(
                "Open P&L",
                width="small",
                help="Net P&L of the currently-open position in this ticker "
                     "(— when not held, or held but unmarked).",
            ),
        },
    )
    st.caption(
        "Green = open position currently winning · red = losing · grey = "
        "open but no live mark. Open positions sort to the top."
    )


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
            # Status pill (matches the hero-row pills). Review cycles get
            # their own pill since they're a fundamentally different kind
            # of cycle — no positions, no orders, just strategist
            # reflection. Showing "all-cash" or "no portfolio" would be
            # misleading.
            if s.get("cycle_intent") == "review":
                status_html = '<span class="at-pill orders-off">📋 review</span>'
            elif s["all_cash"] is True:
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
        # Construct→critic→reconstruct (CLAUDE.md §3.5) legitimately logs
        # two rows for the same (run_id, stage). Counting up front lets us
        # badge the second occurrence as a retry AND guarantees Streamlit
        # widget keys stay unique even before the index-suffix below.
        from collections import Counter
        _stage_counts = Counter((r["run_id"], r["stage"]) for r in decisions)
        for idx, row in enumerate(reversed(decisions)):
            stage = row['stage']
            stage_icon = {
                "market_gate": "🕰️",
                "signals": "📊",
                "strategist": "🧠",
                "construct": "🧩",
                "critic": "⚖️",
                "execute": "📤",
                "meta": "🕒",
                "monitor": "🛡️",
            }.get(stage, "•")
            retry_badge = " 🔁 retry" if _stage_counts[(row["run_id"], stage)] > 1 else ""
            with st.expander(
                f"{stage_icon}  {stage:<14}{retry_badge} • {row['model']:<28} • "
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
                        key=f"view-{idx}-{row['run_id']}-{stage}",
                    ):
                        st.json(json.loads(artifact.read_text()))


# ===== Tab 3: Performance =====
with tabs[3]:
    # Drive the entire P&L summary off the same SyntheticBalance the
    # hero card uses — no parallel computation, no risk of divergence.
    # Reuse _synth_live computed above the hero header (identical args)
    # so trades_pnl_view + FIFO matching don't run a second time per
    # render. Saves one full pass over trades.jsonl on the Performance
    # tab.
    _synth = _synth_live

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
            "Trading costs",
            f"${_synth.trading_fees_total_usd + _synth.slippage_total_usd:,.2f}",
            sub=(
                f"${_synth.trading_fees_total_usd:,.2f} fees "
                f"+ ${_synth.slippage_total_usd:,.2f} slippage (modelled Alpaca)"
            ),
        ),
        unsafe_allow_html=True,
    )
    row2[2].markdown(
        _stat_card(
            "Synthetic balance",
            f"${_synth.synthetic_balance_usd:,.2f}",
            tone=_tone_for(_synth.synthetic_balance_usd - _synth.starting_balance_usd),
            sub="= start + closed + open − LLM − fees − slippage",
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
        "by the historical attribution). Trading costs are modelled on the "
        "**Alpaca** live schedule (lib/alpaca_costs.py): sell-side SEC/FINRA "
        "fees (real on live, modelled on paper) plus per-side slippage — the "
        "dominant friction, which Alpaca never reports even live. Both are "
        "subtracted from the synthetic balance and the equity curve, so paper "
        "Sharpe is friction-honest. Commission is \\$0; margin/borrow are \\$0 "
        "(cash, long-only)."
    )

    st.markdown('<div class="at-section-label">Portfolio balance over time</div>',
                unsafe_allow_html=True)
    # Single chart with a source toggle:
    #
    # 1. **Actual NAV (per-cycle)** — `state/nav_history.jsonl`. One
    #    point per orchestrator cycle, plus a live-broker-equity tip.
    #    What the orchestrator actually saw at cycle end. This is the
    #    chart users want when they ask "what is the bot doing right
    #    now". Falls back to the synthetic trace when the file is empty.
    #
    # 2. **Synthetic balance (reconstructed)** — deterministically built
    #    from trades.jsonl + costs.jsonl. Independent of broker
    #    availability. Math: $2,500 + closed_gross(t) − LLM(t) − fees(t).
    #    Plus a live tip including open P&L + modelled fees.
    chart_controls = st.columns([2, 3])
    with chart_controls[0]:
        source_choice = st.radio(
            "Source",
            ["Actual NAV (per-cycle)", "Synthetic balance (reconstructed)"],
            index=0,
            horizontal=False,
            help="Actual NAV plots the portfolio equity the orchestrator "
                 "recorded at the end of each cycle (from "
                 "state/nav_history.jsonl). Synthetic balance is a "
                 "deterministic reconstruction from trades + costs that "
                 "works even when the broker is unreachable.",
        )
    with chart_controls[1]:
        window_choice = st.radio(
            "Window",
            ["1D", "1W", "1M", "1Y", "All"],
            index=4,
            horizontal=True,
            help="Filter the chart to the trailing window. Affects this "
                 "chart only; underlying logs are untouched. The live tip "
                 "is always shown.",
        )
    window_days = {"1D": 1, "1W": 7, "1M": 30, "1Y": 365}.get(window_choice)

    def _apply_window(df: pd.DataFrame, time_col: str = "at") -> pd.DataFrame:
        if window_days is None or df.empty or time_col not in df.columns:
            return df
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        out = df.copy()
        out["_at_dt"] = pd.to_datetime(
            out[time_col].astype(str).str.replace("Z", "+00:00", regex=False),
            utc=True, errors="coerce",
        )
        return out[out["_at_dt"] >= cutoff]

    if source_choice.startswith("Actual"):
        # Per-cycle NAV from orchestrator.py:932-943, reset-aware for any
        # nav_offset configured (legacy — currently unused in this build
        # but preserved for promotion-to-live; apply_nav_offset_to_history
        # is a no-op when offset is 0).
        raw_rows = _cached_nav_history(_state_mtimes())
        # Read the offset once so the history line AND the live broker
        # tip below use the SAME unit space. Codex P2 (PR #87): the
        # earlier version offset-corrected the history but used raw
        # broker equity for the live tip, producing a false vertical
        # jump equal to the offset whenever a NAV anchor was set.
        _nav_offset_usd = state.nav_offset_usd()
        nav_rows = dd.apply_nav_offset_to_history(
            raw_rows, nav_offset_usd=_nav_offset_usd,
        )
        if not nav_rows:
            st.info(
                "No NAV history yet — `state/nav_history.jsonl` is empty. "
                "The orchestrator writes one row per cycle that reaches "
                "stage_execute. Switch the source above to **Synthetic "
                "balance (reconstructed)** to see a chart from trades + "
                "costs in the meantime."
            )
        else:
            nav_df = pd.DataFrame(nav_rows)
            # Codex P1 (PR #87): state.append_nav only REQUIRES run_id,
            # at, nav_usd — the other hover columns are optional. Older
            # or externally-written rows that omit positions_count /
            # gross_pnl_usd / net_pnl_usd / nav_source would crash the
            # `nav_df[[...]]` slice with KeyError. Ensure every hover
            # column exists with a sensible default before the slice.
            for col, default in [
                ("run_id", "—"),
                ("positions_count", 0),
                ("gross_pnl_usd", 0.0),
                ("net_pnl_usd", 0.0),
                ("nav_source", "—"),
            ]:
                if col not in nav_df.columns:
                    nav_df[col] = default
            nav_df = _apply_window(nav_df, time_col="at")
            # Live tip: prefer real broker equity when available; fall
            # back to the synthetic balance (hero card) so the operator
            # always sees a current marker even when the broker is offline.
            # When nav_offset is configured, broker equity must be
            # offset-corrected to match the history line's units (Codex
            # P2). The synthetic fallback is already in virtual units so
            # no offset applies.
            live_at = state.utcnow_iso()
            xs = list(nav_df["at"])
            ys = [_fnum(v) for v in nav_df["nav_usd"]]
            hover_texts = [
                (
                    f"{r['at']}<br>NAV: ${_fnum(r['nav_usd']):,.2f}"
                    f"<br>Run: {r['run_id']}"
                    f"<br>Positions: {r['positions_count']}"
                    f"<br>Gross P&L: ${_fnum(r['gross_pnl_usd']):,.2f}"
                    f"<br>Net P&L: ${_fnum(r['net_pnl_usd']):,.2f}"
                    f"<br>Source: {r['nav_source']}"
                )
                for _, r in nav_df.iterrows()
            ]
            # Once the live era has begun, the persisted rows are in
            # live/capped-allocation units — appending the paper-scale
            # synthetic tip would end the line with a false jump/drop
            # (Codex P2 on PR #112); the latest live row IS the current
            # marker. On paper, the rows are synthetic units so the tip
            # must be the mark-aware synthetic balance, the same figure as
            # the hero card — not the broker's ~$100k equity (Codex P2 on
            # PR #98). Fold the tip into one continuous line so the curve
            # flows into "now" and the y-axis fits the whole series tightly.
            _latest_is_live = bool(nav_rows) and state.record_mode(nav_rows[-1]) == "live"
            if _latest_is_live:
                tip_phrase = "the latest live cycle"
                if not xs:
                    # The trailing window filtered out every row (e.g. 1D
                    # with the last live cycle older than a day). Anchor the
                    # chart with the latest live row so _render_balance_chart
                    # has a point to draw — never the paper-scale synthetic
                    # tip (Codex P2 on PR #112).
                    last_row = nav_rows[-1]
                    xs.append(last_row.get("at") or live_at)
                    ys.append(_fnum(last_row.get("nav_usd")))
                    hover_texts.append(
                        f"Latest live cycle<br>${_fnum(last_row.get('nav_usd')):,.2f}"
                    )
            else:
                synth_tip = float(_synth_live.synthetic_balance_usd)
                xs.append(live_at)
                ys.append(_fnum(synth_tip))
                hover_texts.append(
                    f"Live (synthetic balance)<br>${_fnum(synth_tip):,.2f}"
                )
                tip_phrase = "the current live (synthetic balance)"
            _render_balance_chart(
                xs=xs, ys=ys, hover_texts=hover_texts,
                yaxis_title="Portfolio NAV (USD)",
                live_transition_at=(state.read_live_transition() or {}).get("at"),
                caption=(
                    f"Line = portfolio NAV at the end of each orchestrator "
                    f"cycle from `state/nav_history.jsonl` ({len(nav_df)} of "
                    f"{len(nav_rows)} cycles shown), flowing into "
                    f"{tip_phrase} at the labelled end point. Colour "
                    f"is green when up over the window, red when down. Toggle "
                    f"Source → *Synthetic balance* for a broker-independent "
                    f"reconstruction from trades.jsonl + costs.jsonl."
                ),
            )
    else:
        # Synthetic balance — preserved from the previous implementation
        # so the deterministic "broker-independent" view stays available.
        series = _cached_realized_balance_series(_state_mtimes())
        live_tip = dd.live_balance_tip(synthetic_balance=_synth)
        nav_df = pd.DataFrame(series) if series else pd.DataFrame(columns=[
            "at", "synthetic_realized_balance_usd",
            "closed_gross_pnl_usd", "llm_cost_total_usd",
            "trading_fees_total_usd",
        ])
        nav_df = _apply_window(nav_df, time_col="at")
        if nav_df.empty and live_tip["synthetic_balance_usd"] == _synth.starting_balance_usd and not _synth.open_gross_pnl_usd:
            st.info(
                "No realized events and no open positions yet — the "
                "synthetic line populates with the first closed trade, "
                "LLM cost row, or open position with marks."
            )
        else:
            # Fold the live snapshot into one continuous line (see Actual
            # branch) — drops the floating diamond + dashed gap.
            xs = list(nav_df["at"]) + [live_tip["at"]]
            ys = (
                [_fnum(v) for v in nav_df["synthetic_realized_balance_usd"]]
                + [_fnum(live_tip["synthetic_balance_usd"])]
            )
            hover_texts = [
                (
                    f"{r['at']}<br>Balance: "
                    f"${_fnum(r['synthetic_realized_balance_usd']):,.2f}"
                    f"<br>Closed gross: ${_fnum(r['closed_gross_pnl_usd']):,.2f}"
                    f"<br>LLM: −${_fnum(r['llm_cost_total_usd']):,.2f}"
                    f"<br>Fees: −${_fnum(r['trading_fees_total_usd']):,.2f}"
                    f"<br>Slippage: −${_fnum(r.get('slippage_total_usd', 0.0)):,.2f}"
                )
                for _, r in nav_df.iterrows()
            ]
            hover_texts.append(
                f"Live: ${_fnum(live_tip['synthetic_balance_usd']):,.2f}"
                f"<br>Closed gross: ${_fnum(live_tip['closed_gross_pnl_usd']):,.2f}"
                f"<br>Open gross: ${_fnum(live_tip['open_gross_pnl_usd']):,.2f}"
                f"<br>LLM: −${_fnum(live_tip['llm_cost_total_usd']):,.4f}"
                f"<br>Fees (real + modelled): −"
                f"${_fnum(live_tip['trading_fees_total_usd']):,.2f}"
                f"<br>Slippage (modelled): −"
                f"${_fnum(live_tip.get('slippage_total_usd', 0.0)):,.2f}"
            )
            _render_balance_chart(
                xs=xs, ys=ys, hover_texts=hover_texts,
                yaxis_title="Synthetic balance (USD)",
                live_transition_at=(state.read_live_transition() or {}).get("at"),
                caption=(
                    "Line = historical realized balance reconstructed from "
                    "`state/trades.jsonl` + `state/costs.jsonl` (closed gross "
                    "− fees − slippage − LLM, exact), flowing into the live "
                    "snapshot (adds open P&L + modelled open costs) at the "
                    "labelled end point — matches the hero card."
                ),
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
            fillgradient=dict(type="vertical", colorscale=[
                [0.0, _rgba("#2563eb", 0.0)],
                [1.0, _rgba("#2563eb", 0.22)],
            ]),
        ))
        _style_fig(fig, height=320, yaxis_title="USD")
        st.plotly_chart(fig, width="stretch", config=NO_ZOOM_CONFIG)
    else:
        st.info("No LLM cost history yet — run the orchestrator (live mode) to populate.")

    st.markdown(
        '<div class="at-section-label">Trading fees over time (real, from Alpaca fills)</div>',
        unsafe_allow_html=True,
    )
    fees_cum = _cached_fees_running_total(_state_mtimes())
    if fees_cum:
        df_fees = pd.DataFrame(fees_cum)
        fig_fees = go.Figure()
        fig_fees.add_trace(go.Scatter(
            x=df_fees["at"], y=df_fees["cum_fees_usd"], mode="lines",
            name="Cumulative trading fees",
            line=dict(color="#d97706", width=2.5),  # amber to distinguish from blue LLM line
            fill="tozeroy",
            fillgradient=dict(type="vertical", colorscale=[
                [0.0, _rgba("#d97706", 0.0)],
                [1.0, _rgba("#d97706", 0.22)],
            ]),
        ))
        _style_fig(fig_fees, height=320, yaxis_title="USD (fees)")
        st.plotly_chart(fig_fees, width="stretch", config=NO_ZOOM_CONFIG)
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
    fees_m = _cached_fees_by_month(_state_mtimes())
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
    by_month = _cached_cost_by_month(_state_mtimes())
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

    st.markdown(
        '<div class="at-section-label">Cost by pipeline stage</div>',
        unsafe_allow_html=True,
    )
    by_stage = _cached_cost_by_stage(_state_mtimes())
    if by_stage:
        st.dataframe(
            pd.DataFrame(by_stage).rename(columns={
                "stage": "Stage",
                "calls": "Calls",
                "cost_usd": "Cost (USD)",
                "total_tokens": "Tokens",
                "cache_hit_pct": "Cache hit",
            }),
            width="stretch",
            hide_index=True,
            column_config={
                "Cost (USD)": st.column_config.NumberColumn(format="$%.4f"),
                "Tokens": st.column_config.NumberColumn(format="%d"),
                "Cache hit": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        st.caption(
            "Where the LLM budget actually goes — calls, spend, and "
            "prompt-cache efficiency per pipeline stage, all time "
            "(reset-aware)."
        )
    else:
        st.info("No per-stage cost data yet.")

    st.markdown(
        '<div class="at-section-label">Prompt-cache hit rate by run</div>',
        unsafe_allow_html=True,
    )
    cache_trend = _cached_cache_hit_trend(_state_mtimes())
    if cache_trend:
        df_ct = pd.DataFrame(cache_trend)
        fig_ct = go.Figure()
        fig_ct.add_trace(go.Scatter(
            x=df_ct["at"], y=df_ct["cache_hit_pct"],
            mode="lines+markers", name="Cache hit %",
            line=dict(color="#2563eb", width=2),
            marker=dict(size=5),
            customdata=[[r] for r in df_ct["run_id"]],
            hovertemplate="%{customdata[0]}<br>%{y:.1f}%<extra></extra>",
        ))
        _style_fig(fig_ct, height=240, yaxis_title="Cache hit (%)",
                   yrange=[0, 105])
        st.plotly_chart(fig_ct, width="stretch", config=NO_ZOOM_CONFIG)
        st.caption(
            "Token-weighted prompt-cache hit rate per orchestrator run, "
            "from costs.jsonl cache counters. Sustained high values mean "
            "the static system prompts are caching as designed; a sudden "
            "drop usually means a prompt was edited."
        )
    else:
        st.info("No cache-hit history yet.")


# ===== Tab 5: vs S&P 500 =====
def _benchmark_mtimes() -> tuple:
    """Cache key for the benchmark tab. Extends `_state_mtimes()` with
    the all-time cost-reset flag mtime so hitting the operator's
    "Reset ALL LLM costs" button invalidates the cached bundle —
    realized_balance_series is reset-aware, but cost_all_time_reset.json
    isn't part of _state_mtimes() (regression for codex P2)."""
    try:
        reset_mtime = state.ALL_TIME_COST_RESET_FLAG.stat().st_mtime_ns
    except FileNotFoundError:
        reset_mtime = 0
    return _state_mtimes() + (reset_mtime,)


@st.cache_data(ttl=3600, show_spinner=False)
def _benchmark_cached(starting: float, live_nav: float | None, mtimes: tuple):
    return dd.benchmark_view(starting, live_nav_usd=live_nav)


def _month_row_tone(row: "pd.Series") -> list[str]:
    delta = float(row.get("delta_pct", 0.0) or 0.0)
    if delta > 0:
        bg = "background-color: #d1fae5"
    elif delta < 0:
        bg = "background-color: #fee2e2"
    else:
        bg = ""
    return [bg] * len(row)


with tabs[4]:
    st.markdown(
        '<div class="at-section-label">Strategy vs S&amp;P 500 (SPY total return)</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Compares the live strategy P&L against an equivalent buy-and-hold "
        "position in SPY (dividends reinvested) since the first orchestrator "
        "cycle. Strategy curve is the realised synthetic balance "
        "(= $2,500 + closed gross P&L − LLM cost − trading fees), the same "
        "series the Performance tab's 'Synthetic balance' toggle plots. "
        "Flat segments mean nothing closed that day. The live diamond "
        "includes open-position MTM. Deposits/withdrawals are not tracked."
    )

    # Live tip is always the synthetic balance (= $2,500 + closed +
    # open − LLM − fees). The historical strategy curve is built from
    # realized_balance_series which is in the same synthetic-unit
    # space, so we MUST stay in those units — using raw broker equity
    # here would inject a ~$100k point onto a ~$2,500 history in the
    # default VIRTUAL_NAV_USD=2500 setup with Alpaca reachable
    # (regression for codex P2). The synthetic balance includes
    # open-position MTM via broker marks when available and falls back
    # gracefully when the broker is offline.
    _live_nav_bench = float(_synth_live.synthetic_balance_usd)
    _live_label_bench = (
        "Live synthetic balance (open MTM included)"
        if broker_view.available else
        "Live synthetic balance (broker offline — closed-only)"
    )

    try:
        bundle = _benchmark_cached(2500.0, _live_nav_bench, _benchmark_mtimes())
        _bench_error = None
    except Exception as exc:  # noqa: BLE001 — yfinance/network surface area is wide
        bundle = None
        _bench_error = f"{type(exc).__name__}: {exc}"

    if _bench_error is not None:
        st.warning(
            "SPY benchmark data is temporarily unavailable — "
            f"`{_bench_error}`. The tab will retry automatically on the "
            "next dashboard refresh; the error is not cached."
        )
    elif bundle is None:
        st.info(
            "Not enough data yet — need at least 2 cycles of NAV history "
            "(spanning ≥1 trading day) before the benchmark comparison is "
            "meaningful. Run `python orchestrator.py` a few more times."
        )
    else:
        # ---- (b) Headline 3-column stat cards ----
        # _fnum keeps a stray NaN (e.g. an un-scrubbed SPY tip) from
        # rendering as a literal "$nan" in the cards.
        strat_end = _fnum(bundle.strategy_curve["nav"].iloc[-1])
        spy_end = _fnum(bundle.spy_curve["nav"].iloc[-1])
        delta_tone = "pos" if _fnum(bundle.delta_usd) >= 0 else "neg"
        cols = st.columns(3)
        cols[0].markdown(
            _stat_card(
                "Strategy NAV",
                f"${strat_end:,.2f}",
                sub=f"SPY-equivalent ${spy_end:,.2f}",
            ),
            unsafe_allow_html=True,
        )
        cols[1].markdown(
            _stat_card(
                "Delta vs SPY",
                f"${_fnum(bundle.delta_usd):+,.2f}",
                sub=f"{_fnum(bundle.delta_pct):+.2f} pp on total return",
                tone=delta_tone,
                help_text=(
                    "Dollar and percentage-point gap between the strategy "
                    "and the SPY-equivalent NAV today. Positive = ahead."
                ),
            ),
            unsafe_allow_html=True,
        )
        beating = _fnum(bundle.strategy_total_return_pct) >= _fnum(
            bundle.spy_total_return_pct
        )
        cols[2].markdown(
            _stat_card(
                "Total return",
                f"{_fnum(bundle.strategy_total_return_pct):+.2f}%",
                sub=f"SPY {_fnum(bundle.spy_total_return_pct):+.2f}%",
                tone="pos" if beating else "neg",
            ),
            unsafe_allow_html=True,
        )

        # ---- (c) Headline equity-curve chart ----
        strat_df = bundle.strategy_curve.reset_index()
        spy_df = bundle.spy_curve.reset_index()
        fig_bench = go.Figure()
        fig_bench.add_trace(go.Scatter(
            x=strat_df["date"],
            y=strat_df["nav"],
            mode="lines",
            name="Strategy",
            line=dict(width=2.5, color="#059669"),
            hovertemplate="%{x|%Y-%m-%d}<br>Strategy: $%{y:,.2f}<extra></extra>",
        ))
        fig_bench.add_trace(go.Scatter(
            x=spy_df["date"],
            y=spy_df["nav"],
            mode="lines",
            name="SPY-equivalent",
            line=dict(width=2.5, color="#d97706"),
            hovertemplate="%{x|%Y-%m-%d}<br>SPY-eqv: $%{y:,.2f}<extra></extra>",
        ))
        # Diamond live tip for the strategy's current value.
        fig_bench.add_trace(go.Scatter(
            x=[strat_df["date"].iloc[-1]],
            y=[strat_end],
            mode="markers",
            name=_live_label_bench,
            marker=dict(
                size=12, color="#d97706", symbol="diamond",
                line=dict(width=1.5, color="#0f172a"),
            ),
            hovertemplate=f"{_live_label_bench}: $%{{y:,.2f}}<extra></extra>",
        ))
        # End-of-line value annotations.
        fig_bench.add_annotation(
            x=strat_df["date"].iloc[-1], y=strat_end,
            text=f"  ${strat_end:,.0f}",
            showarrow=False, xanchor="left",
            font=dict(color="#059669", size=12, family="monospace"),
        )
        fig_bench.add_annotation(
            x=spy_df["date"].iloc[-1], y=spy_end,
            text=f"  ${spy_end:,.0f}",
            showarrow=False, xanchor="left",
            font=dict(color="#d97706", size=12, family="monospace"),
        )
        # Tight y-range hugging both curves — autorange gets dragged to
        # zero by stray annotation anchors and flattens the lines
        # against the top of the chart.
        _bench_yrange = _tight_yrange(
            list(strat_df["nav"]) + list(spy_df["nav"]), min_pad=10.0
        )
        _style_fig(
            fig_bench,
            height=380,
            yaxis_title="Portfolio value (USD)",
            yrange=_bench_yrange,
            legend=True,
            right_margin=80,
        )
        st.plotly_chart(fig_bench, width="stretch", config=NO_ZOOM_CONFIG)
        st.caption(
            f"Inception {bundle.inception.isoformat()} → as of "
            f"{bundle.as_of.isoformat()} · {len(strat_df)} trading-day points · "
            "Sharpe risk-free rate = 0%."
        )

        # ---- (c2) Underwater (drawdown) chart ----
        st.markdown(
            '<div class="at-section-label">Underwater (drawdown from peak)</div>',
            unsafe_allow_html=True,
        )
        _dd_strat = bench.drawdown_series(bundle.strategy_curve["nav"]) * 100.0
        _dd_spy = bench.drawdown_series(bundle.spy_curve["nav"]) * 100.0
        fig_dd = go.Figure()
        fig_dd.add_trace(go.Scatter(
            x=list(_dd_strat.index), y=list(_dd_strat.values),
            mode="lines", name="Strategy",
            line=dict(width=2, color="#059669"),
            fill="tozeroy", fillcolor=_rgba("#059669", 0.18),
            hovertemplate="%{x|%Y-%m-%d}<br>Strategy: %{y:.2f}%<extra></extra>",
        ))
        fig_dd.add_trace(go.Scatter(
            x=list(_dd_spy.index), y=list(_dd_spy.values),
            mode="lines", name="SPY-equivalent",
            line=dict(width=2, color="#d97706"),
            fill="tozeroy", fillcolor=_rgba("#d97706", 0.14),
            hovertemplate="%{x|%Y-%m-%d}<br>SPY-eqv: %{y:.2f}%<extra></extra>",
        ))
        _style_fig(fig_dd, height=220, yaxis_title="Drawdown (%)", legend=True)
        st.plotly_chart(fig_dd, width="stretch", config=NO_ZOOM_CONFIG)
        st.caption(
            "Percent below the running peak at each point — 0% means a "
            "fresh equity high; the depth of each dip is how far the "
            "curve sat under water before recovering."
        )

        # ---- (d) Risk-adjusted comparison cards ----
        st.markdown(
            '<div class="at-section-label">Risk-adjusted comparison</div>',
            unsafe_allow_html=True,
        )
        rcols = st.columns(4)
        rcols[0].markdown(
            _stat_card(
                "Sharpe (ann.)",
                f"{bundle.sharpe_strategy:.2f}",
                sub=f"SPY {bundle.sharpe_spy:.2f}",
                tone="pos" if bundle.sharpe_strategy >= bundle.sharpe_spy else "neg",
                help_text=(
                    "Return earned per unit of risk. Higher is better; >1 "
                    "good, >2 excellent. Annualised, risk-free rate = 0%."
                ),
            ),
            unsafe_allow_html=True,
        )
        dd_strat_pct, _, _ = bundle.max_dd_strategy
        dd_spy_pct, _, _ = bundle.max_dd_spy
        rcols[1].markdown(
            _stat_card(
                "Max drawdown",
                f"{dd_strat_pct * 100:.2f}%",
                sub=f"SPY {dd_spy_pct * 100:.2f}%",
                tone="neg" if dd_strat_pct < dd_spy_pct else "",
                help_text=(
                    "Largest peak-to-trough decline so far. Closer to 0% "
                    "is better. Negative values indicate the drawdown size."
                ),
            ),
            unsafe_allow_html=True,
        )
        rcols[2].markdown(
            _stat_card(
                "Volatility (ann.)",
                f"{bundle.vol_strategy_ann * 100:.2f}%",
                sub=f"SPY {bundle.vol_spy_ann * 100:.2f}%",
                help_text=(
                    "Annualised standard deviation of daily returns. Higher "
                    "means the strategy's day-to-day swings are larger."
                ),
            ),
            unsafe_allow_html=True,
        )
        if bundle.pct_months_strategy_beat is None:
            beat_value = "—"
            beat_sub = "Available after 1 full month"
            beat_tone = ""
        else:
            beat_value = f"{bundle.pct_months_strategy_beat:.0f}%"
            beat_sub = "of completed months"
            beat_tone = "pos" if bundle.pct_months_strategy_beat >= 50.0 else "neg"
        rcols[3].markdown(
            _stat_card(
                "% months beat SPY",
                beat_value,
                sub=beat_sub,
                tone=beat_tone,
                help_text=(
                    "Share of completed calendar months where the strategy "
                    "outperformed SPY. Partial current month is excluded."
                ),
            ),
            unsafe_allow_html=True,
        )

        # ---- (e) Secondary stats: CAGR + correlation ----
        scols = st.columns(2)
        if bundle.cagr_strategy is None:
            cagr_value = "—"
            cagr_sub = "Available after 90 days"
            cagr_tone = ""
        else:
            cagr_value = f"{bundle.cagr_strategy * 100:+.2f}%"
            cagr_sub = (
                f"SPY {bundle.cagr_spy * 100:+.2f}%"
                if bundle.cagr_spy is not None else ""
            )
            cagr_tone = (
                "pos" if (bundle.cagr_spy is None or
                          bundle.cagr_strategy >= bundle.cagr_spy) else "neg"
            )
        scols[0].markdown(
            _stat_card(
                "CAGR (annualised)",
                cagr_value,
                sub=cagr_sub,
                tone=cagr_tone,
                help_text=(
                    "Compound annual growth rate — what the strategy would "
                    "earn per year if it kept compounding at this pace. "
                    "Available after 90 days of data."
                ),
            ),
            unsafe_allow_html=True,
        )
        scols[1].markdown(
            _stat_card(
                "Correlation with SPY",
                f"{bundle.correlation:+.2f}",
                sub=f"{bundle.correlation_label_text} co-movement",
                help_text=(
                    "Pearson correlation of daily returns. 1.0 = lockstep, "
                    "0 = unrelated, −1 = opposite. Low/Moderate/High buckets "
                    "use thresholds 0.3 and 0.7."
                ),
            ),
            unsafe_allow_html=True,
        )

        # ---- (f) Collapsible monthly breakdown ----
        if bundle.months_table is None or bundle.months_table.empty:
            st.caption("Monthly breakdown available after one full calendar month.")
        else:
            with st.expander("Monthly breakdown", expanded=False):
                mt = bundle.months_table.copy()
                mt["month_display"] = [
                    f"{m} *" if partial else m
                    for m, partial in zip(mt["month"], mt["is_partial"])
                ]
                display_df = mt[[
                    "month_display", "strat_ret_pct", "spy_ret_pct",
                    "delta_pct", "strat_eom", "spy_eom",
                ]].rename(columns={"month_display": "month"})
                styled = display_df.style.apply(_month_row_tone, axis=1)
                st.dataframe(
                    styled,
                    column_config={
                        "month": st.column_config.TextColumn("Month"),
                        "strat_ret_pct": st.column_config.NumberColumn(
                            "Strategy %", format="%.2f%%",
                            help="Month-over-month return of the strategy.",
                        ),
                        "spy_ret_pct": st.column_config.NumberColumn(
                            "SPY %", format="%.2f%%",
                            help="Month-over-month return of SPY total return.",
                        ),
                        "delta_pct": st.column_config.NumberColumn(
                            "Δ vs SPY (pp)", format="%+.2f",
                            help="Strategy − SPY. Green tint = beat, red = lagged.",
                        ),
                        "strat_eom": st.column_config.NumberColumn(
                            "Strategy EoM $", format="$%d",
                        ),
                        "spy_eom": st.column_config.NumberColumn(
                            "SPY EoM $", format="$%d",
                        ),
                    },
                    hide_index=True,
                    width="stretch",
                )
                if mt["is_partial"].any():
                    st.caption("`*` partial month — current month not yet ended.")


# ===== Tab 6: Trades =====
with tabs[5]:
    st.markdown(
        '<div class="at-section-label">Per-trade PnL — gross − fees − attributed LLM cost</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Each row pairs a buy fill with the sell that closed it (FIFO). "
        "Fees + slippage are modelled on the Alpaca live schedule "
        "(lib/alpaca_costs.py; real fees on live, modelled on paper). "
        "LLM cost is the opening run's total split evenly across the positions "
        "it opened (per the locked methodology). "
        "Net = gross − fees − slippage − LLM."
    )

    # Surface the orchestrator's last activities-sync error. The sync is
    # wrapped in try/except at orchestrator.py:898-911 and the failure
    # string lands in state/next_run.json["trades_sync_error"] — without
    # this banner a silently-failing sync looks identical to "no fills
    # yet", which is the symptom that first hid closed trades from the
    # operator. The Settings tab has a one-click "Resync" button to
    # retry without restarting the orchestrator.
    _sync_err = ""
    if state.NEXT_RUN.exists():
        try:
            _sync_err = state.read_json(state.NEXT_RUN).get("trades_sync_error", "") or ""
        except Exception:
            _sync_err = ""
    if _sync_err:
        st.warning(
            f"⚠️ Last activities sync failed — closed trades may be stale. "
            f"`{_sync_err}`. Use **Settings → Resync from Alpaca activities** "
            f"to retry, or check Alpaca API keys."
        )

    view = _cached_trades_pnl_view(_state_mtimes(), _marks_key(broker_marks))
    # Local name MUST NOT shadow the module-level `totals` which the
    # Settings tab below reads as a dd.total_token_cost() dict (keys
    # `cost_usd`, `calls`, `total_tokens`, …). Streamlit re-runs every
    # `with tabs[N]:` block on each render, so a name collision here
    # leaks into the Settings tab and triggers
    # `KeyError: 'cost_usd'` on the lifetime-cost stat card.
    trade_totals = view["totals"]

    tcols = st.columns(5)
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
        (
            tcols[4],
            "Realised slippage",
            trade_totals.get("realised_slippage_usd", 0.0),
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

    # ---- Trade statistics (closed trades, net P&L methodology) ----
    _tstats = dd.trade_stats(view["closed"])
    if _tstats is not None:
        st.markdown(
            '<div class="at-section-label">Trade statistics (closed trades)</div>',
            unsafe_allow_html=True,
        )

        def _fmt_hold(hours: float | None) -> str:
            if hours is None:
                return "—"
            return f"{hours / 24.0:.1f} d" if hours >= 48.0 else f"{hours:.1f} h"

        _pf = _tstats["profit_factor"]
        _aw, _al = _tstats["avg_win_usd"], _tstats["avg_loss_usd"]
        srow1 = st.columns(3)
        srow1[0].markdown(
            _stat_card(
                "Win rate (net)",
                f"{_tstats['win_rate_pct']:.0f}%",
                sub=f"{_tstats['wins']} wins · {_tstats['losses']} losses",
                tone="pos" if _tstats["win_rate_pct"] >= 50.0 else "neg",
                help_text=(
                    "Share of closed trades with positive NET P&L "
                    "(gross − fees − attributed LLM cost). $0 nets "
                    "count as non-wins."
                ),
            ),
            unsafe_allow_html=True,
        )
        srow1[1].markdown(
            _stat_card(
                "Profit factor",
                f"{_pf:.2f}" if _pf is not None else "—",
                sub="win $ / loss $" if _pf is not None else "no losing trades yet",
                tone=("pos" if _pf >= 1.0 else "neg") if _pf is not None else "",
                help_text=(
                    "Sum of winning trades' net P&L divided by the "
                    "absolute sum of losing trades'. >1 means winners "
                    "outweigh losers."
                ),
            ),
            unsafe_allow_html=True,
        )
        srow1[2].markdown(
            _stat_card(
                "Avg win / avg loss",
                (f"${_aw:,.2f}" if _aw is not None else "—")
                + " / "
                + (f"${_al:,.2f}" if _al is not None else "—"),
                sub="per closed trade, net",
            ),
            unsafe_allow_html=True,
        )
        srow2 = st.columns(3)
        srow2[0].markdown(
            _stat_card(
                "Avg hold time",
                _fmt_hold(_tstats["avg_hold_hours"]),
                sub="open fill → closing fill",
            ),
            unsafe_allow_html=True,
        )
        srow2[1].markdown(
            _stat_card(
                "Best trade",
                f"${_tstats['best']['net_pnl_usd']:+,.2f}",
                sub=_tstats["best"]["symbol"],
                tone="pos" if _tstats["best"]["net_pnl_usd"] > 0 else "neg",
            ),
            unsafe_allow_html=True,
        )
        srow2[2].markdown(
            _stat_card(
                "Worst trade",
                f"${_tstats['worst']['net_pnl_usd']:+,.2f}",
                sub=_tstats["worst"]["symbol"],
                tone="pos" if _tstats["worst"]["net_pnl_usd"] > 0 else "neg",
            ),
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
            "slippage_usd": "Slippage",
            "llm_cost_usd": "LLM",
            "net_pnl_usd": "Net",
            "buy_run_id": "Run",
        })

        sty = df_closed.style.format({
            "Entry": "${:,.4f}",
            "Exit": "${:,.4f}",
            "Gross": "${:,.2f}",
            "Fees": "${:,.2f}",
            "Slippage": "${:,.2f}",
            "LLM": "${:,.4f}",
            "Net": "${:,.2f}",
        }).map(_pnl_color, subset=["Gross", "Net"])
        st.dataframe(sty, width="stretch", hide_index=True)
    else:
        st.info(
            "No closed trades yet. Closed-trade rows appear once a position "
            "is fully sold and the activities sync picks up the close."
        )

    # Surface sells that arrived in trades.jsonl with no matching prior
    # buy lot — FIFO drops them on the floor (they never appear in the
    # closed table) but the operator needs to see them to diagnose
    # missing-history situations: out-of-order activities sync, manual
    # broker close before orchestrator opened the lot locally, or a wipe
    # that cleared buys but not sells.
    _unmatched = view.get("unmatched_sells", [])
    if _unmatched:
        with st.expander(
            f"⚠️ Unmatched sells ({len(_unmatched)}) — sells with no prior buy in trades.jsonl",
            expanded=False,
        ):
            st.caption(
                "These sells couldn't be paired against an open buy lot by "
                "FIFO. Common causes: history was wiped while positions were "
                "still open, the activities sync ran out of order, or the "
                "position was opened outside the orchestrator. They are NOT "
                "included in Realised P&L above."
            )
            df_unm = pd.DataFrame(_unmatched).rename(columns={
                "symbol": "Symbol",
                "kind": "Kind",
                "qty": "Qty",
                "fill_price": "Fill price",
                "filled_at": "Filled at",
                "activity_id": "Activity ID",
            })
            sty_unm = df_unm.style.format({"Fill price": "${:,.4f}"})
            st.dataframe(sty_unm, width="stretch", hide_index=True)

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
            "slippage_usd": "Slippage",
            "llm_cost_usd": "LLM",
            "net_pnl_usd": "Net",
            "buy_run_id": "Run",
        })
        sty_o = df_open.style.format({
            "Entry": "${:,.4f}",
            "Mark": lambda v: "—" if v is None else f"${v:,.4f}",
            "Gross": lambda v: "—" if v is None else f"${v:,.2f}",
            "Fees": "${:,.2f}",
            "Slippage": "${:,.2f}",
            "LLM": "${:,.4f}",
            "Net": lambda v: "—" if v is None else f"${v:,.2f}",
        }).map(_pnl_color, subset=["Gross", "Net"])
        st.dataframe(sty_o, width="stretch", hide_index=True)
    else:
        st.info(
            "No open lots. Open lots populate once the activities sync writes "
            "fills into state/trades.jsonl."
        )


# ===== Tab 7: Agent Logs =====
with tabs[6]:
    _run_ids = _cached_run_ids(_state_mtimes())
    if not _run_ids:
        st.info("No runs yet.")
    else:
        def _fmt_rid(rid: str) -> str:
            # Run-dir names start "YYYYMMDDTHHMMSSZ…" (same shape
            # dd.load_run_summaries parses) — pretty-print that prefix.
            if len(rid) >= 16 and rid[8] == "T" and rid[15] == "Z":
                pretty = (
                    f"{rid[0:4]}-{rid[4:6]}-{rid[6:8]} "
                    f"{rid[9:11]}:{rid[11:13]}:{rid[13:15]} UTC"
                )
            else:
                pretty = rid
            return f"{pretty} · latest" if rid == _run_ids[0] else pretty

        selected_rid = st.selectbox(
            "Run archive",
            _run_ids,
            index=0,
            key="agent_logs_run",
            format_func=_fmt_rid,
            help=(
                f"{len(_run_ids)} archived runs under state/runs/ — "
                "pick any past cycle to inspect its full artifact trail."
            ),
        )
        _latest_suffix = " · latest" if selected_rid == _run_ids[0] else ""
        st.markdown(
            f'<div class="at-section-label">Run · '
            f'<code style="color:var(--text-0);">{selected_rid}</code>'
            f'{_latest_suffix}</div>',
            unsafe_allow_html=True,
        )
        run_dir = state.RUNS_DIR / selected_rid

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
                    # Old/aborted runs can leave truncated artifacts —
                    # surface the parse error instead of crashing the tab.
                    try:
                        st.json(json.loads(f.read_text()))
                    except (json.JSONDecodeError, OSError) as exc:
                        st.warning(f"Unreadable artifact: `{exc}`")

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


# ===== Tab 8: Calibration =====
with tabs[7]:
    st.caption(
        "The agent's own track record — exactly the evidence the LLM stages "
        "are fed each cycle — plus activity health and the promotion scorecard."
    )

    # --- trade-sync staleness alert (silent blackout detector) ---
    try:
        sync_check = dd.trade_sync_gaps()
    except Exception:
        sync_check = {"stale": False, "gaps": []}
    if sync_check.get("stale"):
        st.error(
            f"⚠️ Trade-sync staleness: {len(sync_check['gaps'])} run(s) in the "
            f"last {sync_check.get('lookback_days', 7)} days submitted accepted "
            "orders but have NO matching fills in trades.jsonl. Cooldown, P&L "
            "and the Sharpe gate are degraded until fills sync. Check Alpaca "
            "credentials / trades_sync logs. Affected runs: "
            + ", ".join(g["run_id"] for g in sync_check["gaps"][:5])
        )

    cal = dd.calibration_view()
    memo = cal["memo"]

    if not memo.get("closed_trades"):
        st.info(
            "No closed trades yet — the calibration views populate once the "
            "first position is opened and closed."
        )
    else:
        overall = memo.get("overall") or {}
        oc = st.columns(4)
        oc[0].markdown(_stat_card(
            "Closed trades", str(memo["closed_trades"]),
            sub=f"avg hold {overall.get('avg_hold_hours') or '—'}h",
        ), unsafe_allow_html=True)
        oc[1].markdown(_stat_card(
            "Win rate",
            f"{overall.get('win_rate_pct'):.0f}%" if overall.get("win_rate_pct") is not None else "—",
            sub=f"{overall.get('wins', 0)}W / {overall.get('losses', 0)}L",
        ), unsafe_allow_html=True)
        oc[2].markdown(_stat_card(
            "Profit factor",
            f"{overall.get('profit_factor'):.2f}" if overall.get("profit_factor") is not None else "—",
            sub="gross wins / |losses|",
        ), unsafe_allow_html=True)
        oc[3].markdown(_stat_card(
            "Realized net",
            f"${overall.get('net_pnl_usd', 0.0):+.2f}",
            sub="after fees + LLM cost",
        ), unsafe_allow_html=True)

        cal_cols = st.columns(2)
        with cal_cols[0]:
            st.markdown('<div class="at-section-label">Win rate by confidence bucket</div>', unsafe_allow_html=True)
            st.caption("Are the agent's confidence scores honest? 0.8s should win more than 0.5s.")
            st.dataframe(
                memo.get("confidence_calibration") or [],
                use_container_width=True, hide_index=True,
            )
            st.markdown('<div class="at-section-label">By regime (at entry)</div>', unsafe_allow_html=True)
            st.dataframe(
                memo.get("by_regime") or [],
                use_container_width=True, hide_index=True,
            )
        with cal_cols[1]:
            st.markdown('<div class="at-section-label">By factor</div>', unsafe_allow_html=True)
            st.dataframe(
                memo.get("by_factor") or [],
                use_container_width=True, hide_index=True,
            )
            st.markdown('<div class="at-section-label">Recent exits (what killed them)</div>', unsafe_allow_html=True)
            st.dataframe(
                memo.get("recent_exits") or [],
                use_container_width=True, hide_index=True,
            )

    # --- activity health ---
    st.markdown('<div class="at-section-label">Activity health</div>', unsafe_allow_html=True)
    st.caption(
        "The system was once over-gated into chronic all-cash. These should "
        "show real deployment; a slide toward 0% activity is a regression."
    )
    try:
        act = dd.activity_metrics()
    except Exception:
        act = {}
    ac = st.columns(4)
    ac[0].markdown(_stat_card(
        "Cycles placing orders",
        f"{act.get('pct_cycles_with_orders')}%" if act.get("pct_cycles_with_orders") is not None else "—",
        sub=f"{act.get('cycles_with_orders', 0)} of {act.get('runs_seen', 0)} runs "
            f"(+{act.get('dedup_skipped', 0)} dedup-skips)",
    ), unsafe_allow_html=True)
    ac[1].markdown(_stat_card(
        "Time in market",
        f"{act.get('time_in_market_pct')}%" if act.get("time_in_market_pct") is not None else "—",
        sub="cycles holding ≥1 position",
    ), unsafe_allow_html=True)
    ac[2].markdown(_stat_card(
        "Avg open positions", str(act.get("avg_positions") if act.get("avg_positions") is not None else "—"),
    ), unsafe_allow_html=True)
    ac[3].markdown(_stat_card(
        "Avg cash buffer",
        f"{act.get('avg_cash_pct')}%" if act.get("avg_cash_pct") is not None else "—",
        sub="high = capital idle",
    ), unsafe_allow_html=True)

    # --- critic record ---
    st.markdown('<div class="at-section-label">Critic record</div>', unsafe_allow_html=True)
    try:
        crit = dd.critic_history(limit=100)
    except Exception:
        crit = {"rows": [], "accepted": 0, "rejected": 0}
    if crit["rows"]:
        st.caption(
            f"{crit['accepted']} accepted / {crit['rejected']} rejected over the "
            f"last {len(crit['rows'])} critiqued cycles."
        )
        st.dataframe(crit["rows"], use_container_width=True, hide_index=True)
    else:
        st.info("No critique artifacts yet.")

    # --- kill-event audit ---
    st.markdown('<div class="at-section-label">Kill-event audit</div>', unsafe_allow_html=True)
    if cal["kill_events"]:
        st.dataframe(cal["kill_events"], use_container_width=True, hide_index=True)
    else:
        st.info("No monitor-driven flattens recorded yet (state/kill_events.jsonl).")

    # --- live-readiness scorecard ---
    st.markdown('<div class="at-section-label">Promotion scorecard (informational)</div>', unsafe_allow_html=True)
    st.caption(
        "Auto-tracked CLAUDE.md promotion criteria. Going live remains a "
        "manual, triple-locked decision — this just shows distance to the bar."
    )
    try:
        score = dd.readiness_scorecard()
    except Exception:
        score = []
    score_rows = [
        {
            "": ("✅" if r["met"] else ("❌" if r["met"] is False else "⏳")),
            "Criterion": r["criterion"],
            "Target": r["target"],
            "Current": r["value"],
        }
        for r in score
    ]
    if score_rows:
        st.dataframe(score_rows, use_container_width=True, hide_index=True)


# ===== Tab 9: Settings =====
with tabs[8]:
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

    st.markdown(
        '<div class="at-section-label">Resync trades from Alpaca</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Pull the full activities history from Alpaca (FILLS + fees) and "
        "append any rows not already in `state/trades.jsonl`. Idempotent — "
        "rows are keyed by Alpaca's `activity_id`, so re-running this is "
        "safe. Use after a transient API failure (the Trades tab banner "
        "will say so), after wiping history, or any time closed trades "
        "look stale."
    )
    if st.button(
        "🔁 Resync from Alpaca activities",
        help="Runs lib.trades_sync.sync_fills_from_alpaca with no since-cursor. "
             "Requires ALPACA_API_KEY / ALPACA_API_SECRET in env.",
    ):
        try:
            from lib import trades_sync
            res = trades_sync.sync_fills_from_alpaca(
                order_id_to_run_id=trades_sync.order_id_to_run_id_from_runs(),
            )
            st.success(
                f"Sync complete — wrote {res.new_fills_written} new fill(s), "
                f"saw {res.fills_seen} activities, matched fees on "
                f"{res.fees_matched} fill(s). Refresh to see the Trades tab."
            )
        except Exception as e:
            st.error(
                f"Sync failed: `{type(e).__name__}: {e}`. Check ALPACA_API_KEY "
                "/ ALPACA_API_SECRET and that the broker is reachable."
            )

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
