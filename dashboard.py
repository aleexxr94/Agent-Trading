"""Streamlit dashboard — paper-trading portfolio + decision log + cost ledger.

Run locally:    streamlit run dashboard.py
Phone access:   streamlit run dashboard.py --server.address 0.0.0.0
                (no auth — only on a trusted home network; see README)

Tabs:
  1. Portfolio — NAV, cash, the 8–12 positions, allocation pie, P&L stub.
  2. Trades & Rationales — chronological decisions with full agent reasoning.
  3. Performance — equity curve + drawdown + SPY benchmark (Plotly).
  4. Agent Logs — bull/bear debates, scenarios, last 20 decision-log rows,
     orchestrator-set next-run time.
  5. Settings — emergency-stop button (writes state/halt.flag), paper/live
     indicator (live disabled), cost today / cost this run, README link.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import dashboard_data as dd
from lib import pnl as pnl_lib
from lib import state

ROOT = Path(__file__).resolve().parent

RISK_BANNER = (
    "**PAPER TRADING — Experimental autonomous AI agent. Leveraged ETFs and "
    "options on a small account are high-risk. Not financial advice.**"
)


# ---------- page setup ----------


st.set_page_config(
    page_title="Agent-Trading",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Dark mode + red banner styling. Streamlit honours the theme via .streamlit/config.toml,
# but we also inline the banner colour so it survives theme switches.
st.markdown(
    """
    <style>
    .risk-banner {
        background: #7f1d1d;
        color: #fee2e2;
        padding: 0.6rem 1rem;
        border-radius: 6px;
        margin-bottom: 1rem;
        font-weight: 500;
        text-align: center;
    }
    .halt-banner {
        position: sticky;
        top: 0;
        z-index: 999;
        background: #b91c1c;
        color: #fff5f5;
        padding: 0.6rem 1rem;
        border-radius: 6px;
        margin-bottom: 1rem;
        font-weight: 700;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    }
    .small-muted { color: #9ca3af; font-size: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _banner() -> None:
    st.markdown(f'<div class="risk-banner">{RISK_BANNER}</div>', unsafe_allow_html=True)


# ---------- data ----------


portfolio, source = dd.load_portfolio()
decisions = dd.load_decisions(limit=200)
costs = dd.load_costs(limit=2000)
latest_rid = dd.latest_run_id()
halted = state.is_halted()
# Pull live marks once at the top so the Portfolio tab can show per-position
# P&L and the Performance tab can compute aggregate Gross/Net P&L from the
# same source — keeps the two tabs internally consistent.
broker_marks = dd.try_load_broker_marks()


# Sticky halt banner — visible across every tab when the orchestrator is
# halted, so an operator on any tab sees it without scrolling back.
if halted:
    st.markdown(
        '<div class="halt-banner">🛑 ORCHESTRATOR HALTED — '
        f'halt.flag is set ({state.HALT_FLAG.name}). New orders disabled. '
        'Clear via Settings tab.</div>',
        unsafe_allow_html=True,
    )


# ---------- tabs ----------


tabs = st.tabs(["Portfolio", "Trades & Rationales", "Performance", "Agent Logs", "Settings"])

# ----- Tab 1: Portfolio -----
with tabs[0]:
    _banner()
    st.subheader("Portfolio")

    if source != "live":
        st.info(f"Showing **{source}** data — no live portfolio yet. Run `python orchestrator.py` to populate.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("NAV (USD)", f"${portfolio['nav_usd']:,.2f}")
    c2.metric("Cash", f"${portfolio['cash_usd']:,.2f}")
    c3.metric("Cash buffer", f"{portfolio['cash_buffer_pct']:.1f}%")
    c4.metric("Positions", len(portfolio["positions"]) or "0 (all-cash)")

    if portfolio.get("all_cash"):
        st.warning("All-cash portfolio. Rationale: " + (portfolio.get("all_cash_rationale") or "—"))

    rows = dd.position_table_rows(portfolio, marks=broker_marks or None)
    if rows:
        df_pos = pd.DataFrame(rows)
        st.dataframe(
            df_pos,
            width="stretch",
            hide_index=True,
            column_config={
                "Cost": st.column_config.NumberColumn("Cost", format="$%.2f"),
                "Mark": st.column_config.NumberColumn("Mark", format="$%.2f"),
                "Notional": st.column_config.NumberColumn("Notional", format="$%,.0f"),
                "% NAV": st.column_config.NumberColumn("% NAV", format="%.1f%%"),
                "Gross P&L": st.column_config.NumberColumn("Gross P&L", format="$%+,.2f"),
                "Net P&L": st.column_config.NumberColumn("Net P&L", format="$%+,.2f"),
            },
        )
        if not broker_marks:
            st.caption(
                "Mark / Gross P&L / Net P&L columns stay blank until Alpaca paper "
                "keys are configured — see README §Setup."
            )

        col_pie, col_bar = st.columns([1, 1])
        with col_pie:
            pie_data = dd.allocation_pie(portfolio)
            fig = go.Figure(go.Pie(
                labels=[r["label"] for r in pie_data],
                values=[r["value"] for r in pie_data],
                hole=0.45,
            ))
            fig.update_layout(
                template="plotly_dark",
                height=380,
                margin=dict(l=10, r=10, t=30, b=10),
                title="Allocation (% NAV)",
            )
            st.plotly_chart(fig, width="stretch")
        with col_bar:
            # Per-position Net P&L bar — green/red colouring so the worst
            # position is visible at a glance without reading the table.
            bar_rows = [r for r in rows if r.get("Net P&L") is not None]
            if bar_rows:
                bar_rows = sorted(bar_rows, key=lambda r: r["Net P&L"])
                fig_bar = go.Figure(go.Bar(
                    x=[r["Symbol"] for r in bar_rows],
                    y=[r["Net P&L"] for r in bar_rows],
                    marker_color=[
                        "#16a34a" if r["Net P&L"] >= 0 else "#dc2626" for r in bar_rows
                    ],
                ))
                fig_bar.update_layout(
                    template="plotly_dark",
                    height=380,
                    margin=dict(l=10, r=10, t=30, b=10),
                    title="Net P&L per position (USD)",
                    yaxis_title="Net P&L (USD)",
                )
                st.plotly_chart(fig_bar, width="stretch")
            else:
                st.info("Per-position P&L bar populates once marks are available.")
    else:
        st.write("No open positions.")

# ----- Tab 2: Trades & Rationales -----
with tabs[1]:
    _banner()
    st.subheader("Decisions (chronological)")
    if not decisions:
        st.info("No decisions logged yet.")
    else:
        for row in reversed(decisions):
            with st.expander(
                f"{row['stage']:<10} • {row['model']:<28} • {row.get('started_at','')} • "
                f"${row.get('cost_usd', 0):.4f}",
                expanded=False,
            ):
                st.json(row, expanded=False)
                run_dir = state.RUNS_DIR / row["run_id"]
                artifact = run_dir / row["output_ref"]
                if artifact.exists():
                    st.markdown(f"**Artifact:** `{artifact.relative_to(ROOT)}`")
                    if st.button(f"View {row['stage']} artifact", key=f"view-{row['run_id']}-{row['stage']}"):
                        st.json(json.loads(artifact.read_text()))

# ----- Tab 3: Performance -----
with tabs[2]:
    _banner()
    st.subheader("P&L (gross / modelled costs / net)")

    # Marks were fetched at page top so the Portfolio tab and Performance
    # tab stay consistent. Falls back to empty dict on any broker error
    # (alpaca-py missing, no creds, network blip) — the dashboard always renders.
    pnl_break = pnl_lib.compute_portfolio_pnl(portfolio=portfolio, marks=broker_marks or None)

    p1, p2, p3 = st.columns(3)
    has_gross = broker_marks and pnl_break.gross_pnl_usd != 0
    p1.metric("Gross P&L (USD)", f"${pnl_break.gross_pnl_usd:+,.2f}" if has_gross else "—")
    p2.metric("Modelled trading costs", f"${pnl_break.modelled_costs_usd:,.2f}")
    p3.metric("Net P&L (USD)", f"${pnl_break.net_pnl_usd:+,.2f}" if has_gross else "—")

    marks_status = (
        f"Live marks from Alpaca paper ({len(broker_marks)} positions matched)."
        if broker_marks else
        "No live marks yet — connect Alpaca paper keys to populate Gross / Net P&L. "
        "Cards stay '—' until then."
    )
    st.caption(
        marks_status + " Trading costs are modelled to mirror **IBKR Pro retail** "
        "(USD account): \\$1 min commission + \\$0.005/share on ETFs (capped 0.5%), "
        "\\$0.65/contract + \\$0.04 OCC on options, ~5 bps / 25 bps half-spread, "
        "plus SEC + FINRA TAF on sell side. So Net P&L tracks what live would "
        "actually cost — capital preservation honesty pre-promotion."
    )

    st.subheader("Equity curve")
    nav_history = dd.load_nav_history()
    if nav_history:
        nav_df = pd.DataFrame(nav_history)
        fig_nav = go.Figure()
        fig_nav.add_trace(go.Scatter(
            x=nav_df["at"], y=nav_df["nav_usd"], mode="lines+markers",
            name="NAV (USD)", line=dict(width=2),
        ))
        if "net_pnl_usd" in nav_df.columns:
            fig_nav.add_trace(go.Scatter(
                x=nav_df["at"], y=nav_df["net_pnl_usd"], mode="lines",
                name="Cumulative Net P&L (USD)", yaxis="y2", line=dict(dash="dot"),
            ))
            fig_nav.update_layout(
                yaxis2=dict(title="Net P&L (USD)", overlaying="y", side="right", showgrid=False),
            )
        fig_nav.update_layout(
            template="plotly_dark", height=360, yaxis_title="NAV (USD)",
            margin=dict(l=10, r=10, t=20, b=10), legend=dict(orientation="h"),
        )
        st.plotly_chart(fig_nav, width="stretch")
    else:
        st.info("No NAV history yet — the equity curve populates once orchestrator runs accumulate.")

    st.subheader("LLM cost over time")
    if costs:
        df = pd.DataFrame([
            {"at": r.get("at", ""), "cost_usd": r.get("cost_usd", 0.0)} for r in costs
        ])
        df["cum_cost"] = df["cost_usd"].cumsum()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["at"], y=df["cum_cost"], mode="lines", name="Cumulative LLM cost"))
        fig.update_layout(template="plotly_dark", height=360, yaxis_title="USD",
                          margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No LLM cost history yet — run the orchestrator (live mode) to populate.")

    st.subheader("Cost & tokens by month (this project only)")
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

# ----- Tab 4: Agent Logs -----
with tabs[3]:
    _banner()
    st.subheader("Latest agent artifacts")
    if latest_rid is None:
        st.info("No runs yet.")
    else:
        run_dir = state.RUNS_DIR / latest_rid
        st.markdown(f"**Latest run:** `{latest_rid}`")
        for name in ("research.json", "scenarios.json", "portfolio.json", "next_run.json"):
            f = run_dir / name
            if f.exists():
                with st.expander(f"{name} — {f.stat().st_size:,} bytes"):
                    st.json(json.loads(f.read_text()))

    st.subheader("Last 20 decisions")
    if decisions:
        st.dataframe(
            pd.DataFrame(decisions[-20:])[
                ["run_id", "stage", "model", "cost_usd", "prompt_cache_hit_pct", "status"]
            ],
            width="stretch",
            hide_index=True,
        )

    st.subheader("Next-run plan")
    if state.NEXT_RUN.exists():
        st.json(state.read_json(state.NEXT_RUN))
    else:
        st.info("No next-run plan written yet.")

# ----- Tab 5: Settings -----
with tabs[4]:
    _banner()
    st.subheader("Mode")
    st.write("Paper trading. **Live trading is disabled in this build** (see CLAUDE.md §Promotion to live).")

    st.subheader("Cost")
    today = dd.cost_today_usd()
    this_run = dd.cost_for_run_usd(latest_rid) if latest_rid else 0.0
    totals = dd.total_token_cost()
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Cost today (USD)", f"${today:.4f}")
    cc2.metric("Cost this run (USD)", f"${this_run:.4f}")
    cc3.metric("Cost all time (USD)", f"${totals['cost_usd']:.4f}")

    cc4, cc5, cc6 = st.columns(3)
    cc4.metric("Tokens all time", f"{totals['total_tokens']:,}")
    cc5.metric("LLM calls all time", f"{totals['calls']:,}")
    cc6.metric(
        "Cache hit rate",
        (
            f"{100.0 * totals['cache_read_input_tokens'] / max(1, totals['total_tokens']):.1f}%"
            if totals["total_tokens"] else "—"
        ),
    )
    st.caption(
        "All-time totals are scoped to **this project** — they aggregate "
        "`state/costs.jsonl` only (not your Anthropic console total)."
    )

    st.subheader("Halt flag")
    if halted:
        st.error(f"Orchestrator is HALTED. Flag: {state.HALT_FLAG}")
        if st.button("Clear halt flag"):
            state.clear_halt()
            st.rerun()
    else:
        st.success("Orchestrator is not halted.")
        if st.button("🛑 Emergency stop (write halt.flag)"):
            state.set_halt("dashboard")
            st.rerun()

    st.subheader("Manual actions")
    if st.button("Refresh data"):
        st.rerun()
    st.markdown(f"[README]({(ROOT / 'README.md').as_uri()}) · [CLAUDE.md]({(ROOT / 'CLAUDE.md').as_uri()})")
