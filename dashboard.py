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

    rows = dd.position_table_rows(portfolio)
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        pie_data = dd.allocation_pie(portfolio)
        fig = go.Figure(go.Pie(
            labels=[r["label"] for r in pie_data],
            values=[r["value"] for r in pie_data],
            hole=0.45,
        ))
        fig.update_layout(
            template="plotly_dark",
            height=420,
            margin=dict(l=10, r=10, t=20, b=10),
        )
        st.plotly_chart(fig, width="stretch")
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
    st.subheader("Performance")
    st.caption(
        "Equity curve and drawdown. Populated once orchestrator runs accumulate; "
        "SPY benchmark overlay TBD when broker history wires in."
    )
    nav_series = []
    for r in costs:
        nav_series.append({"at": r.get("at", ""), "cost_usd": r.get("cost_usd", 0)})
    if nav_series:
        df = pd.DataFrame(nav_series)
        df["cum_cost"] = df["cost_usd"].cumsum()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["at"], y=df["cum_cost"], mode="lines", name="Cumulative LLM cost"))
        fig.update_layout(template="plotly_dark", height=420, yaxis_title="USD")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No cost or NAV history yet.")

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
    cc1, cc2 = st.columns(2)
    cc1.metric("Cost today (USD)", f"${today:.4f}", delta=None)
    cc2.metric("Cost this run (USD)", f"${this_run:.4f}", delta=None)

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
