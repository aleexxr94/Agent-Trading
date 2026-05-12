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
    📜 Decisions — chronological stage decisions with full agent reasoning
    📈 Performance — equity curve, cost-over-time, monthly cost breakdown
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
        --bg-0: #0a0e17;
        --bg-1: #111827;
        --bg-2: #1f2937;
        --border: #1f2937;
        --text-0: #e5e7eb;
        --text-1: #9ca3af;
        --text-2: #6b7280;
        --green: #10b981;
        --green-soft: #064e3b;
        --red: #ef4444;
        --red-soft: #7f1d1d;
        --amber: #f59e0b;
        --amber-soft: #78350f;
        --blue: #3b82f6;
        --purple: #a855f7;
      }

      /* tighten the main container */
      .block-container { padding-top: 1.2rem !important; padding-bottom: 4rem !important; max-width: 1400px; }

      /* hero card */
      .at-hero {
        background: linear-gradient(135deg, #111827 0%, #0a0e17 100%);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 1.25rem 1.5rem;
        margin-bottom: 0.75rem;
      }
      .at-hero-row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
      .at-hero-label { color: var(--text-1); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em; }
      .at-hero-nav { font-size: 2.4rem; font-weight: 600; color: var(--text-0); letter-spacing: -0.02em; line-height: 1.1; margin-top: 0.1rem; }
      .at-hero-sub { color: var(--text-1); font-size: 0.85rem; margin-top: 0.25rem; }

      /* status pills */
      .at-pills { display: flex; gap: 0.5rem; flex-wrap: wrap; }
      .at-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.25rem 0.7rem;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: 0.02em;
        border: 1px solid transparent;
      }
      .at-pill.paper      { background: #064e3b; color: #6ee7b7; border-color: #065f46; }
      .at-pill.live       { background: #7f1d1d; color: #fecaca; border-color: #991b1b; }
      .at-pill.orders-on  { background: #1e3a8a; color: #bfdbfe; border-color: #1e40af; }
      .at-pill.orders-off { background: var(--bg-2); color: var(--text-1); border-color: var(--border); }
      .at-pill.halted     { background: #b91c1c; color: #fff5f5; border-color: #dc2626;
                            animation: at-pulse 1.5s ease-in-out infinite; }
      .at-pill.allcash    { background: #78350f; color: #fde68a; border-color: #92400e; }
      .at-pill.active     { background: #064e3b; color: #6ee7b7; border-color: #065f46; }

      @keyframes at-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }

      /* slim risk strip — replaces the chunky old banner */
      .at-risk-strip {
        background: #7f1d1d; color: #fecaca;
        padding: 0.35rem 0.85rem; border-radius: 6px;
        font-size: 0.78rem; text-align: center;
        margin: 0 0 0.75rem 0;
        border: 1px solid #991b1b;
      }

      /* halted banner — sticks to top, pulses */
      .at-halt-banner {
        position: sticky; top: 0; z-index: 999;
        background: #b91c1c; color: #fff5f5;
        padding: 0.6rem 1rem; border-radius: 6px; margin-bottom: 0.75rem;
        font-weight: 700; text-align: center;
        box-shadow: 0 4px 12px rgba(185, 28, 28, 0.4);
        animation: at-pulse 1.5s ease-in-out infinite;
      }

      /* compact metric card grid */
      .at-stat {
        background: var(--bg-1);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 0.85rem 1rem;
      }
      .at-stat-label { color: var(--text-1); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; }
      .at-stat-value { color: var(--text-0); font-size: 1.35rem; font-weight: 600; margin-top: 0.15rem; }
      .at-stat-sub { color: var(--text-2); font-size: 0.72rem; margin-top: 0.15rem; }
      .at-stat-value.pos { color: var(--green); }
      .at-stat-value.neg { color: var(--red); }
      .at-stat-value.warn { color: var(--amber); }

      /* cost meter bar */
      .at-meter { height: 6px; background: var(--bg-2); border-radius: 999px; overflow: hidden; margin-top: 0.4rem; }
      .at-meter-fill { height: 100%; background: var(--green); border-radius: 999px; transition: width 0.4s; }
      .at-meter-fill.warn { background: var(--amber); }
      .at-meter-fill.danger { background: var(--red); }

      /* tighter tab look */
      .stTabs [data-baseweb="tab-list"] { gap: 0.25rem; border-bottom: 1px solid var(--border); }
      .stTabs [data-baseweb="tab"] { padding: 0.5rem 1rem; }

      /* dataframe row hover */
      [data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 8px; }

      /* subtle subheaders */
      h2, h3 { color: var(--text-0); font-weight: 600; letter-spacing: -0.01em; }
      .at-section-label { color: var(--text-1); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; margin: 0.5rem 0 0.4rem 0; }

      /* small-muted text */
      .small-muted { color: var(--text-2); font-size: 0.78rem; }
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
broker_marks = dd.try_load_broker_marks()

# Useful pre-computed values
cost_today = dd.cost_today_usd()
cost_today_pct = min(100.0, 100.0 * cost_today / max(DAILY_CAP_USD, 0.0001))
cost_this_run = dd.cost_for_run_usd(latest_rid) if latest_rid else 0.0
totals = dd.total_token_cost()
n_positions = len(portfolio.get("positions", []))
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
sg[0].markdown(_meter_card("Cost today", cost_today, DAILY_CAP_USD), unsafe_allow_html=True)
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
    "📜 Decisions",
    "📈 Performance",
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

    rows = dd.position_table_rows(portfolio, marks=broker_marks or None)

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
            if v is None or (isinstance(v, float) and v != v):  # NaN check
                return "color: var(--text-2)"
            if isinstance(v, (int, float)):
                if v > 0: return "color: #10b981; font-weight: 600"
                if v < 0: return "color: #ef4444; font-weight: 600"
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
                marker=dict(line=dict(color="#0a0e17", width=2)),
            ))
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                height=360,
                margin=dict(l=10, r=10, t=40, b=10),
                title=dict(text="Allocation — % NAV", font=dict(size=14, color="#e5e7eb")),
                legend=dict(font=dict(color="#e5e7eb", size=11)),
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
                        "#10b981" if r["Net P&L"] >= 0 else "#ef4444" for r in bar_rows
                    ],
                ))
                fig_bar.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    height=360,
                    margin=dict(l=10, r=10, t=40, b=10),
                    title=dict(text="Net P&L per position (USD)",
                               font=dict(size=14, color="#e5e7eb")),
                    yaxis=dict(title="USD", gridcolor="#1f2937"),
                    xaxis=dict(tickfont=dict(size=10)),
                )
                st.plotly_chart(fig_bar, width="stretch")
            else:
                st.info("Per-position P&L bar populates once marks are available.")
    elif not is_all_cash:
        st.write("No open positions.")


# ===== Tab 2: Decisions =====
with tabs[1]:
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
with tabs[2]:
    pnl_break = pnl_lib.compute_portfolio_pnl(
        portfolio=portfolio, marks=broker_marks or None,
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
            line=dict(width=2, color="#10b981"),
            marker=dict(size=4, color="#10b981"),
        ))
        if "net_pnl_usd" in nav_df.columns:
            fig_nav.add_trace(go.Scatter(
                x=nav_df["at"], y=nav_df["net_pnl_usd"], mode="lines",
                name="Cumulative Net P&L (USD)",
                yaxis="y2", line=dict(dash="dot", color="#a855f7"),
            ))
            fig_nav.update_layout(
                yaxis2=dict(title="Net P&L (USD)", overlaying="y", side="right",
                            showgrid=False, color="#a855f7"),
            )
        fig_nav.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=340, yaxis_title="NAV (USD)",
            yaxis=dict(gridcolor="#1f2937", color="#10b981"),
            xaxis=dict(gridcolor="#1f2937"),
            margin=dict(l=10, r=10, t=20, b=10),
            legend=dict(orientation="h", y=1.1),
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
            line=dict(color="#3b82f6", width=2),
            fill="tozeroy",
            fillcolor="rgba(59, 130, 246, 0.1)",
        ))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=300, yaxis_title="USD",
            yaxis=dict(gridcolor="#1f2937"),
            xaxis=dict(gridcolor="#1f2937"),
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False,
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No LLM cost history yet — run the orchestrator (live mode) to populate.")

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


# ===== Tab 4: Agent Logs =====
with tabs[3]:
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


# ===== Tab 5: Settings =====
with tabs[4]:
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
