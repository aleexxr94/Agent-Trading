# Autonomous Multi-Agent Leveraged Trading System — Claude Code Build Spec

> This file is the persistent build spec for this repo. Re-read it at the start of every Claude Code session and before any architectural change. If anything in this file conflicts with a casual instruction in chat, this file wins unless I explicitly say "override CLAUDE.md".

## Role
You are a senior quant systems engineer. Build a complete, **paper-trading-only**, privately-hosted multi-agent trading system in the GitHub repo I will provide. Work iteratively: design → implement → test → document. After each major milestone, stop, summarise what changed, and wait for me to confirm before continuing.

## Critical preconditions (read and confirm before writing any code)
1. **Paper trading only** until I explicitly promote it via the criteria in §11. Live mode must be gated behind both an env var and a hard-coded version flag. Do not build a UI button that toggles live.
2. **I am UK-based.** Alpaca **paper** is fine. Alpaca **live** brokerage is not available to UK retail. Do not assume live USD funding via Alpaca will ever happen. Add a TODO and an interface seam in `lib/broker.py` so the broker can be swapped to IBKR later without rewriting the orchestrator.
3. **Account size:** $2,500 paper (all sizing in USD). Every sizing calculation must respect this. The orchestrator must be willing to hold cash if conviction is insufficient — do not force-fill 10 slots.
4. **Environment / runtime:** **Linux VPS + systemd is the sole supported production runtime.** Schedule the orchestrator and monitor with the systemd services/timers under `deploy/` (idempotent `install.sh`, service+timer units, the `agent-scheduler.service` dynamic-cadence daemon, Tailscale phone access) on an Ubuntu 24.04 box that doesn't sleep. Use a project-local `.venv` (Python 3.11+; 3.12 on Ubuntu 24.04). Do not use Claude Code Routines for production runtime. **Windows 10/11 + Windows Task Scheduler is no longer a supported runtime** — do not add or maintain Windows Task Scheduler XML, PowerShell wrappers, or Windows runtime docs unless I explicitly request Windows support in the future. Any lingering Windows references are obsolete history, not setup guidance. (Historical note: the original build targeted Windows Task Scheduler; that path was removed in favour of the Linux VPS path.)
5. **Runtime architecture:** local Python service on the VPS calling the **Anthropic API directly** with prompt caching. **Claude Code is for development only, not runtime execution.** Pro-plan usage limits make routine-driven production execution unreliable.
6. If anything below is ambiguous, bundle all clarifying questions into one message before starting. Do not guess on capital allocation, position counts, kill switches, or broker behaviour.

## System scope

> **ETF-only migration (2026-05-29):** Listed options were removed entirely.
> The $2,500 account made them non-viable (the 15% per-position cap rarely
> cleared a single contract; six months of paper trading opened zero option
> positions). The system now trades **only leveraged/inverse ETFs**. Bearish
> views are expressed by buying inverse ETFs — never short selling, never
> puts. The factors the option underlyings covered (rates, energy) now have
> real leveraged ETF pairs (TMF/TMV, ERX/ERY). This section supersedes the
> earlier options-bearing spec.

- Universe (ETF-only, 29 tickers): 13 bull/bear leveraged-ETF pairs —
  TQQQ/SQQQ (nasdaq), UPRO/SPXU (sp500), TNA/TZA (small-caps), SOXL/SOXS
  (semis), TECL/TECS (technology), LABU/LABD (biotech), YINN/YANG (china),
  FAS/FAZ (financials-broad), ERX/ERY (energy), GUSH/DRIP (oil-gas-ep),
  BOIL/KOLD (natural-gas), TMF/TMV (rates), NUGT/DUST (gold-miners) — plus
  UVXY (solo long-vol) and BITX (2x bull) / BITI (1x inverse) on crypto-btc.
- **No options. No spot single-name equities. No unleveraged broad-market
  ETFs as core positions.** Every position is a long leveraged/inverse ETF.
- **No broker shorts.** Bear theses are expressed as long inverse ETFs
  (SQQQ, SPXU, etc.). Cash account only.
- Portfolio target: **1–12 open positions** at the end of each cycle (or all-cash if conviction is genuinely absent). The 1-position floor lets a single strong-conviction thesis fire even when broader diversification isn't available. Concentration risk is bounded by the per-position 15% NAV cap and kill conditions.
- Per-position cap at entry: **≤15% of portfolio NAV**.
- Per-position kill condition: **≤25% loss of position NAV**. Each position must also carry at least one of `underlying_price_below`, `underlying_price_above`, or `time_stop_utc` (sanity rule fail otherwise). The price thresholds reference the ETF's own price.
- Daily portfolio drawdown circuit breaker: **≥8% in a single UTC day** halts new orders and triggers monitor-only mode until next manual review.
- Cycle cadence: **every 4 hours during market hours, weekdays only**. The market_gate stage queries Alpaca's clock and short-circuits weekends/holidays/after-hours without LLM cost. Within market hours, the orchestrator-meta agent picks the actual next-run timestamp (bounded 1–24h).

## Architecture (locked)

```
┌─────────────────────┐    ┌──────────────────────────────┐
│ systemd timer/svc   │───▶│ orchestrator.py              │
│ (next-run set by AI)│    │  Stage 0: Market Gate        │
└─────────────────────┘    │  Stage 1: Signals (Python)   │
                           │  Stage 2: Strategist (LLM)   │
                           │  Stage 3: Portfolio Construct│
                           │  Stage 4: Sanity (Python)    │
                           │  Stage 5: Execute + Schedule │
                           └────────┬─────────────────────┘
                                    │ Anthropic API (prompt cached, 4 calls/cycle)
                                    │ Alpaca paper API
                                    │ yfinance
                                    ▼
                           ┌──────────────────────────────┐
                           │ state/  (JSON, append-only   │
                           │ decision logs, run artifacts)│
                           └────────┬─────────────────────┘
                                    │
                                    ▼
                           ┌──────────────────────────────┐
                           │ dashboard.py  (Streamlit,    │
                           │ localhost:8501, dark mode)   │
                           └──────────────────────────────┘
```

The v2 pipeline (2026-05-13) replaced 5 LLM-bearing stages with 2: deterministic signals + a single strategist LLM call, plus the construct LLM call that remained. A critic LLM call (stage 3.5) was layered on afterwards as a winrate add-on; combined with the meta-scheduler call that always closes a trade cycle, the steady state is 4 LLM calls/cycle (strategist + construct + critic + meta), or 5 when the critic rejects and the constructor reruns once. Per-cycle LLM cost dropped from ~$1.50–2.50 to ~$0.30.

Sub-agents are separate Anthropic API calls with role-specific system prompts and structured output schemas — not Claude Code's sub-agent feature.

## v2 pipeline (with winrate add-ons)
Each stage emits a validated JSON artifact under `state/runs/{run_id}/`. Schema-failed LLM outputs are retried once with the validation error fed back; second failure aborts the run and logs.

0. **Market Gate** (Python, $0) — Alpaca `/v2/clock` query. If markets are closed → write `market_gate.json` + closed-market `next_run.json` and exit. No LLM calls billed on closed-market cycles.
1. **Signals** (Python, $0) — For each of the 29 universe tickers, compute deterministic features from yfinance daily history: momentum (30/60d), HV (30/90d), distance from 50/200d MAs, ADV, last close. Includes per-ticker `upcoming_macro_events_7d` (FOMC/CPI/NFP/PCE within 7 days). Output: `signals.json`. Replaces the v1 screener + bull/bear research + scenarios chain entirely.
1b. **Cycle dedup** (Python, $0) — If the signals fingerprint AND broker-position fingerprint both match the prior cycle's, skip strategist + construct + execute and reuse the cached portfolio. Stored in `state/last_cycle_hash.json`.
2. **Strategist** (Sonnet 4.6 medium effort, ~$0.05) — Reads `signals.json` + current broker positions + recent PnL history (last 5 cycles); emits a regime classification + up to 6 candidate ideas with `instrument_kind` (always `etf`), `thesis` (signal-citing), `confidence` ∈ [0, 1]. Bullish theses name the bull ETF; bearish theses name the inverse ETF. Output: `view.json`.
3. **Portfolio Construction** (Opus 4.7 high effort, ~$0.20) — Converge on 1–12 positions (or all-cash). Reads signals + view + current positions + PnL history + adaptive per-position cap. Output: `portfolio.json`.
3.5. **Critic** (Sonnet 4.6 low effort, ~$0.03) — Adversarial review. Returns `{accept, critique, suggested_changes}`. On reject, the constructor reruns ONCE with the critique fed back. Output: `critique.json`.
4. **Sanity** (Python, $0) — 9 deterministic post-construct rules covering concentration, kill_conditions, strategist endorsement, adaptive-cap enforcement (lower in drawdown), confidence-weighted sizing, notional floor ($50), ADV liquidity (≤1% of dollar ADV), and re-entry cooldown (no re-entry of a symbol exited within 7 days unless strategist confidence ≥ 0.8). Non-blocking by default; `SANITY_BLOCK_ON_FAIL=true` escalates `fail` to a hard skip of stage_execute. Output: `sanity.json`.
5. **Execution + Monitoring** — submit paper orders via Alpaca (close before open; no-cross-zero invariant); orchestrator-meta picks next-run window in 1–24h and writes `next_run.json`. A lightweight `monitor.py` runs more frequently and only checks kill conditions; it can flatten a position but cannot open new ones.

## Repo structure

```
.
├── README.md
├── .env.example                # commit; real .env is gitignored
├── .gitignore                  # excludes .env, state/, __pycache__, .venv/, *.pyc, Thumbs.db
├── requirements.txt
├── pyproject.toml
├── orchestrator.py             # main pipeline entrypoint
├── monitor.py                  # kill-condition checker
├── dashboard.py                # Streamlit app
├── prompts/
│   ├── orchestrator.md         # meta-scheduler prompt
│   ├── strategist.md           # v2 stage 2 — single LLM call producing the view
│   ├── constructor.md          # v2 stage 3 — single LLM call producing the portfolio
│   └── critic.md               # v2 stage 3.5 — adversarial review of the portfolio
├── schemas/                    # all validated on write
│   ├── position.schema.json    # single ETF position object (options removed)
│   ├── portfolio.schema.json
│   ├── signals.schema.json     # v2 stage 1 output
│   ├── view.schema.json        # v2 stage 2 output
│   ├── critique.schema.json    # v2 stage 3.5 output
│   ├── sanity.schema.json
│   └── decision_log.schema.json
├── lib/
│   ├── broker.py               # interface; Alpaca impl behind it (IBKR TODO)
│   ├── alpaca_client.py        # AlpacaBroker — implements get_clock for market_gate
│   ├── market_gate.py          # v2 stage 0 — Alpaca clock short-circuit
│   ├── signals.py              # v2 stage 1 — deterministic feature generator
│   ├── market_data.py          # yfinance wrappers (history, ADV, HV)
│   ├── events.py               # macro calendar (FOMC/CPI/NFP/PCE) for signals
│   ├── orders.py               # diff_portfolio + no-cross-zero invariant (ETF-only; rejects option payloads)
│   ├── sanity.py               # v2 stage 4 — deterministic post-construct rules
│   ├── stages.py               # StageConfig per LLM stage
│   ├── llm.py                  # Anthropic client + prompt caching + cost tracking
│   ├── risk.py                 # sizing, caps, kill checks, circuit breakers, cooldown
│   ├── universe.py             # 29-ticker leveraged/inverse ETF universe metadata
│   ├── marks.py                # mark-price helpers (ETF symbols)
│   ├── pnl.py                  # portfolio P&L computation
│   ├── trades.py               # trade-log reader + re-entry cooldown state
│   ├── trades_sync.py          # pulls filled orders from Alpaca into state
│   ├── benchmark.py            # SPY benchmark + backtest/Monte-Carlo helpers
│   ├── dashboard_data.py       # dashboard aggregation (synthetic NAV, timelines)
│   └── state.py                # JSON read/write, run_id, halt-flag
├── deploy/                     # Linux VPS runtime (the only supported path) — see deploy/README.md
│   ├── install.sh              # idempotent installer (venv, user, systemd units)
│   ├── run_orchestrator.sh / run_monitor.sh / run_scheduler.sh
│   ├── systemd/                # orchestrator/monitor service+timer, dashboard, scheduler
│   └── tailscale.md            # phone access without public ports
├── bin/
│   └── analyze_runs.py         # offline run-log analysis
├── tests/                      # ~30 files, ~800 tests (representative below)
│   ├── test_risk.py            # + test_risk_adaptive_cap.py
│   ├── test_sanity.py
│   ├── test_orders.py
│   ├── test_monitor.py
│   ├── test_state.py
│   ├── test_schemas.py
│   ├── test_orchestrator_dryrun.py
│   ├── test_trades.py / test_trades_sync.py
│   ├── test_dashboard_data.py / test_benchmark.py / test_pnl.py
│   └── ...                      # market_gate, meta_scheduler, review_cycle, universe, …
└── state/                      # gitignored runtime
    ├── runs/
    ├── decisions.jsonl
    ├── costs.jsonl
    ├── halt.flag               # presence = stop
    └── current_portfolio.json
```

## Schemas (write these first; validate every agent output)
- `position.schema.json`: a single ETF position object (no option branch).
  - `{ kind: "etf", symbol, shares, avg_cost, leverage_factor, entry_thesis, kill_conditions, position_pct }`
  - `kill_conditions`: `{ max_loss_pct, underlying_price_below?, underlying_price_above?, time_stop_utc?, notes? }` — price thresholds reference the ETF's own price.
- `portfolio.schema.json`: array of 1–12 positions (or all-cash); sum of `position_pct` ≤ 100; cash buffer field; total NAV at write time.
- `decision_log.schema.json`: `run_id`, stage, model, inputs_hash, output_ref, prompt_cache_hit_pct, cost_usd, started_at, ended_at.

## Cost controls (mandatory)
- Anthropic prompt caching on every static system prompt and on the screening universe block.
- **Per-run hard cap: $3.00 USD.** If exceeded mid-run, finish current stage, abort the rest, log. Raised from $2.00 on 2026-05-13 after a paper run overshot to $2.22 mid-stage; the new headroom lets cycles complete through `stage_execute` even when the construct stage runs on the Opus tier.
- **Daily hard cap: $12.00 USD.** Beyond this, orchestrator refuses to run until next UTC day.
- Append cost per run to `state/costs.jsonl`. Surface in dashboard.

## Dashboard (Streamlit, dark mode, mobile-friendly)
Top banner, red, every tab:
> **PAPER TRADING — Experimental autonomous AI agent. Leveraged & inverse ETFs on a small account are high-risk. Not financial advice.**

Tabs:
1. **Portfolio** — NAV, cash, up to 12 positions. ETF rows show leverage factor + shares, bull/bear direction, factor, notional, entry cost, current mark, P&L, kill conditions. Allocation pie. Day P&L, total P&L.
2. **Trades & Rationales** — chronological decisions: full agent reasoning, "why this instrument", "why now", kill conditions, horizon chosen.
3. **Performance** — equity curve, drawdown, vs SPY benchmark (Plotly).
4. **Agent Logs** — sanity-report panel + latest stage artifacts (market_gate, signals, view, portfolio, sanity, orders, next_run), current orchestrator-set next-run time, last 20 decision-log entries.
5. **Settings** — emergency stop button (writes `state/halt.flag`; orchestrator checks this **before any API call**), paper/live indicator (live disabled), API cost today and this run, manual refresh, link to README.

Phone access on local Wi-Fi: `streamlit run dashboard.py --server.address 0.0.0.0`. Do not enable ngrok or any tunnel by default. Leave a commented `tunnel.sh.example` with a warning that I have not added auth.

## Sub-agent prompt design
Each prompt under `prompts/` must include:
- Role and scope.
- Reference to the JSON output schema it must conform to.
- Explicit bias-mitigation: bull must list its strongest counterarguments; bear must steel-man bull first.
- Risk reminders specific to leveraged/inverse ETFs (daily-rebalance decay, path dependence in chop, gap risk, liquidity/ADV on the smaller sector ETFs).
- "If uncertain, abstain" rule — outputs may be empty if conviction is low.

Orchestrator prompt must state explicitly:
> You manage a $2,500 experimental paper account. Capital preservation outweighs upside chasing. If conviction is insufficient, output an all-cash portfolio with rationale rather than forcing 10 positions.

## Acceptance criteria (all must pass before I run live cycles)
1. `pytest` green. All schemas validate against representative fixtures.
2. `python orchestrator.py --dry-run` completes a full pipeline run on paper data, sends no orders, writes a full decision log and run artifacts.
3. `streamlit run dashboard.py` launches and renders every tab against a `state/seed_portfolio.json` fixture without errors.
4. Emergency stop flag halts the orchestrator within one cycle; dashboard reflects halted state.
5. Per-run and daily cost caps are enforced — include a test that stubs a high-cost LLM call and asserts the abort path.
6. README is Linux VPS-focused and covers: Ubuntu 24.04 VPS setup (`deploy/install.sh`, project-local `.venv`), env vars, manual smoke runs, systemd timer/service install + uninstall, dashboard launch (bound to `127.0.0.1`, reached via Tailscale or an SSH tunnel), halt procedure, log inspection (`journalctl` + `state/*.jsonl`), and repo hygiene (no committed secrets). The dashboard must never bind `0.0.0.0`; phone access goes through Tailscale or an SSH tunnel, not a public port.

## Promotion to live (documented only — do not enable in code)
All of:
- ≥ 4 weeks continuous paper running with ≥ 80% of scheduled cycles completed.
- Sharpe ≥ 0.5 on paper after modelled costs (sanity floor, not a target).
- Max drawdown ≤ 25% on paper.
- No unresolved schema-validation failures or unhandled exceptions in last 7 days.
- I confirm a UK-suitable broker (likely IBKR) and a new `lib/ibkr_client.py` is implemented behind the existing broker interface.
- `LIVE_TRADING_ENABLED=true` env var **and** a hard-coded `LIVE_VERSION` constant bumped in code.

### Paper → live code change map (reference only — keep the triple lock engaged)
When (and only when) the criteria above are met, this is the exact, file-level set of
changes required to move from paper to real money. Nothing here is enabled in the current
checkout — the triple lock is designed to fail closed at every layer.

1. **Triple-lock release (the two deliberate edits).**
   - `lib/live_gate.py` — bump `LIVE_VERSION = 0` → `1` (in code; can't be set via env).
     This is the single source of truth, shared by both `orchestrator.py` (which
     re-exports it) and `monitor.py`.
   - `.env` — set `LIVE_TRADING_ENABLED=true`.
   - `lib/live_gate.py:assert_live_gate()` refuses to run if env is `true` while
     `LIVE_VERSION == 0` (exit code 2), and is called from BOTH `orchestrator.main`
     and `monitor.main`, so the two must be raised together at either entrypoint.
     `lib/alpaca_client.py:61` independently refuses to construct a non-paper
     client unless `LIVE_TRADING_ENABLED=true`.

2. **Broker.** UK retail **cannot** trade Alpaca live (precondition §2), so "going live" in
   practice means a new **`lib/ibkr_client.py:IBKRBroker(Broker)`** behind the existing seam
   (`lib/broker.py`), not flipping `ALPACA_BASE_URL` to `https://api.alpaca.markets`.
   - Implement the 6 abstract members: the `name` property plus `get_account`, `get_positions`,
     `submit_order`, `cancel_all`, `flatten` — omitting any one (incl. `name`) leaves the class
     abstract and un-instantiable behind `_try_load_broker()`.
   - Override the optional `get_clock` method used by market_gate (default returns `None`, which
     silently degrades the market gate — must be real). (The former option-data methods —
     `option_contract_tradable`, `get_option_quote`, `get_underlying_price` — were removed with
     options; ETF price stops use the ETF's own mark, so no underlying-price fetch is needed.)
   - Map IBKR responses onto the existing dataclasses (`Account`, `BrokerPosition`,
     `OrderRequest`, `OrderResult`, `MarketClock`); keep `is_paper=False`.
   - Point `_try_load_broker()` at `IBKRBroker` (a ~5-line glue change), and add the live
     credential env vars to `.env.example`.

3. **Sizing — replace the synthetic NAV pin (code change, not config).** Paper sizing is pinned to
   a synthetic balance (`VIRTUAL_NAV_USD=2500` + realized P&L, never the broker's $100k paper
   equity) via `orchestrator.py:_account_nav` (≈ 481-499) → `lib.dashboard_data.realized_synthetic_nav`
   / `synthetic_base_usd`. **Note the fallback: if `VIRTUAL_NAV_USD` is unset or malformed, the code
   falls back to a hard-coded `2500.0`, not broker equity** (Phase 3 deliberately removed the
   broker-equity path so a missing var couldn't size ~40× too large). So going live means reworking
   `_account_nav` / `synthetic_base_usd` to read the live broker's `Account.equity_usd`, then
   re-verifying the 15% per-position cap and adaptive-cap math against real equity.

4. **Costs / fills (also an Alpaca-only seam).** `lib/trades_sync.py:sync_fills_from_alpaca` is
   called at `orchestrator.py:363-364` (pre-cooldown) and `1021-1022` (post-execute) with
   `getattr(ctx.broker, "_client", None)`. With an `IBKRBroker` that attribute is `None`, so it
   constructs an **Alpaca** client instead; the caught failure means **live fills/fees never reach
   `trades.jsonl`**, silently breaking cooldown, P&L, and the Sharpe gate. A live swap must add an
   IBKR fill-sync path here. Live also needs the broker's real commission / SEC / regulatory fee
   schedule, or P&L and Sharpe will be overstated.

5. **Order gate.** `ORDERS_ENABLED=true` must already have been validated on paper (it is the
   same switch live uses to actually submit). Confirm it has run cleanly on paper first.

6. **Pre-flight before the first live cycle.** Re-run the §Acceptance criteria; confirm the
   dashboard PAPER→LIVE pill flips as expected (`dashboard.py`); dry-run; then one supervised
   live cycle with the smallest possible sizing and the halt flag within reach.

## Mandatory risk warnings (in README, dashboard banner, and every decision log)
> Leveraged and inverse ETFs decay path-dependently in volatile markets and are not buy-and-hold instruments — a 3x ETF held through chop bleeds value even when the underlying ends flat. A $2,500 account cannot diversify meaningfully across many such positions — concentration risk is structural, not a flaw to fix. This system is an experiment in autonomous AI trading agents, not a path to reliable returns. Expect losses. Do not deploy capital you cannot afford to lose entirely. None of this is financial advice.

## Working style
- Conventional commits. Open a draft PR against `main`; do not push to `main` directly.
- After scaffolding §1–§5 of the pipeline (skeleton + schemas + tests, no prompts yet), **stop and summarise the architecture for me** before writing the LLM prompts.
- Dependency-light Python: `httpx`, `pydantic`, `pandas`, `yfinance`, `alpaca-py`, `anthropic`, `streamlit`, `plotly`, `pytest`, `python-dotenv`.
- All times stored in UTC; dashboard renders in user-local.
- Never commit `.env`, `state/`, or any API key.

## What to ask me before starting
Bundle all clarifying questions into one message. Likely topics:
- Repo URL and whether it is empty or has any starter content.
- Confirmed location of Alpaca paper keys (env var names you should expect).
- (Resolved 2026-05-29: ETF-only — options were removed entirely.)
- Are overnight and weekend positions allowed, or must the agent flatten before close?
- Preferred Anthropic model for the orchestrator vs sub-agents (default: sonnet for orchestrator, haiku for screening, sonnet for adversarial research).

Begin with the architecture confirmation and your single bundled clarifying-question message. Do not write code until I reply.
