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
    🤖 Agent Logs — latest artifacts (research/scenarios/portfolio), next-run plan
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
# orchestrator.py will actually enforce. Defaults match .env.example.
PER_RUN_CAP_USD = float(os.environ.get("PER_RUN_COST_CAP_USD", "2.00"))
DAILY_CAP_USD = float(os.environ.get("DAILY_COST_CAP_USD", "10.00"))


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


st.markdown(
    f"""
    <div class="at-hero">
      <div class="at-hero-row">
        <div>
          <div class="at-hero-label">Net Asset Value (USD)</div>
          <div class="at-hero-nav">${portfolio['nav_usd']:,.2f}</div>
          <div class="at-hero-sub">
            Last cycle: <strong>{_fmt_ts(last_run_at)}</strong>
            &nbsp;•&nbsp; Next: <strong>{_fmt_ts(next_run_at)}</strong>
            &nbsp;•&nbsp; Source: <strong>{source}</strong>
          </div>
        </div>
        <div style="text-align:right">
          {_pills_html()}
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
    rows = dd.position_table_rows(
        portfolio,
        marks=broker_marks or None,
        costs=broker_costs or None,
        held_keys=filter_keys,
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

        styled = df_pos.style.map(_color_pnl, subset=["Gross P&L", "Net P&L"])
        st.dataframe(
            styled,
            width="stretch",
            hide_index=True,
            column_config={
                "Cost":      st.column_config.NumberColumn("Cost",     format="$%.2f"),
                "Mark":      st.column_config.NumberColumn("Mark",     format="$%.2f"),
                "Notional":  st.column_config.NumberColumn("Notional", format="$%,.0f"),
                "% NAV":     st.column_config.NumberColumn("% NAV",    format="%.1f%%"),
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
                fig_bar = go.Figure(go.Bar(
                    x=[r["Symbol"] for r in bar_rows],
                    y=[r["Net P&L"] for r in bar_rows],
                    marker_color=[
                        "#059669" if r["Net P&L"] >= 0 else "#dc2626" for r in bar_rows
                    ],
                ))
                fig_bar.update_layout(
                    template="plotly_white",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    height=360,
                    margin=dict(l=10, r=10, t=40, b=10),
                    title=dict(text="Net P&L per position (USD)",
                               font=dict(size=15, color="#0f172a")),
                    yaxis=dict(title="USD", gridcolor="#e2e8f0"),
                    xaxis=dict(tickfont=dict(size=11)),
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

            # Funnel: screened → researched → scenarios → final
            funnel = (
                f'{s["screened_count"]} screened → '
                f'{s["researched_count"]} researched → '
                f'{s["scenarios_count"]} scenarios → '
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
                "screen": "🔎",
                "research": "⚖️",
                "research_bull": "📈",
                "research_bear": "📉",
                "scenarios": "🎲",
                "construct": "🧩",
                "execute": "📤",
                "meta": "🕒",
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
    pnl_break = pnl_lib.compute_portfolio_pnl(
        portfolio=portfolio,
        marks=broker_marks or None,
        costs=broker_costs or None,
    )
    has_gross = broker_marks and pnl_break.gross_pnl_usd != 0

    st.markdown('<div class="at-section-label">P&L summary</div>', unsafe_allow_html=True)
    p = st.columns(3)
    gross_tone = "pos" if pnl_break.gross_pnl_usd > 0 else ("neg" if pnl_break.gross_pnl_usd < 0 else "")
    net_tone = "pos" if pnl_break.net_pnl_usd > 0 else ("neg" if pnl_break.net_pnl_usd < 0 else "")
    p[0].markdown(
        _stat_card(
            "Gross P&L",
            f"${pnl_break.gross_pnl_usd:+,.2f}" if has_gross else "—",
            tone=gross_tone if has_gross else "",
        ),
        unsafe_allow_html=True,
    )
    p[1].markdown(
        _stat_card("Modelled trading costs", f"${pnl_break.modelled_costs_usd:,.2f}"),
        unsafe_allow_html=True,
    )
    p[2].markdown(
        _stat_card(
            "Net P&L",
            f"${pnl_break.net_pnl_usd:+,.2f}" if has_gross else "—",
            tone=net_tone if has_gross else "",
        ),
        unsafe_allow_html=True,
    )

    marks_status = (
        f"Live marks from Alpaca paper ({len(broker_marks)} positions matched)."
        if broker_marks else
        "No live marks yet — connect Alpaca paper keys to populate Gross / Net P&L."
    )
    st.caption(
        marks_status + " Trading costs are modelled to mirror **IBKR Pro retail** "
        "(USD account): $1 min commission + $0.005/share on ETFs (capped 0.5%), "
        "$0.65/contract + $0.04 OCC on options, ~5 bps / 25 bps half-spread, "
        "plus SEC + FINRA TAF on sell side."
    )

    st.markdown('<div class="at-section-label">Equity curve</div>', unsafe_allow_html=True)
    nav_history = dd.load_nav_history()
    if nav_history:
        nav_df = pd.DataFrame(nav_history)
        fig_nav = go.Figure()
        fig_nav.add_trace(go.Scatter(
            x=nav_df["at"], y=nav_df["nav_usd"], mode="lines+markers",
            name="NAV (USD)",
            line=dict(width=2.5, color="#059669"),
            marker=dict(size=5, color="#059669"),
        ))
        if "net_pnl_usd" in nav_df.columns:
            fig_nav.add_trace(go.Scatter(
                x=nav_df["at"], y=nav_df["net_pnl_usd"], mode="lines",
                name="Cumulative Net P&L (USD)",
                yaxis="y2", line=dict(dash="dot", color="#7c3aed", width=2),
            ))
            fig_nav.update_layout(
                yaxis2=dict(title="Net P&L (USD)", overlaying="y", side="right",
                            showgrid=False, color="#7c3aed"),
            )
        fig_nav.update_layout(
            template="plotly_white",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=360, yaxis_title="NAV (USD)",
            yaxis=dict(gridcolor="#e2e8f0", color="#059669"),
            xaxis=dict(gridcolor="#e2e8f0"),
            margin=dict(l=10, r=10, t=20, b=10),
            legend=dict(orientation="h", y=1.1, font=dict(size=12)),
        )
        st.plotly_chart(fig_nav, width="stretch")
    else:
        st.info("No NAV history yet — the equity curve populates once orchestrator runs accumulate.")

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
    totals = view["totals"]

    tcols = st.columns(4)
    for col, label, value, fmt in [
        (tcols[0], "Closed trades", totals["closed_count"], "{}"),
        (tcols[1], "Open lots", totals["open_count"], "{}"),
        (
            tcols[2],
            "Realised net",
            totals["realised_net_usd"],
            "${:,.2f}",
        ),
        (
            tcols[3],
            "Realised fees",
            totals["realised_fees_usd"],
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
        artifact_icons = {
            "research.json":  "⚖️",
            "scenarios.json": "🎲",
            "portfolio.json": "🧩",
            "next_run.json":  "🕒",
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
            f"All dashboard cost totals are currently filtered — only counting "
            f"LLM spend after **{_fmt_ts(all_time_reset_at)}**. Underlying "
            f"`state/costs.jsonl` is intact; cap enforcement still uses the raw "
            f"log so per-run / per-day safety rails remain in force."
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
            "Dashboard totals (today, this run, monthly, all-time) read from the "
            "full `state/costs.jsonl` by default. Pressing the button below zeroes "
            "ALL displayed totals at the current moment — useful after a testing "
            "burn or model-config change you want to draw a line under. Audit log "
            "and cap enforcement are unaffected."
        )
        if st.button(
            "🧹 Reset ALL LLM costs to $0 (display only)",
            help="Records an all-time reset marker. costs.jsonl audit log is preserved; "
                 "per-run and per-day caps continue to use the raw log.",
        ):
            state.set_all_time_cost_reset("dashboard")
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

    st.markdown('<div class="at-section-label">Manual actions</div>', unsafe_allow_html=True)
    if st.button("🔄 Refresh data"):
        st.rerun()
    st.markdown(
        f"[📖 README]({(ROOT / 'README.md').as_uri()}) &nbsp;·&nbsp; "
        f"[📋 CLAUDE.md]({(ROOT / 'CLAUDE.md').as_uri()})"
    )
