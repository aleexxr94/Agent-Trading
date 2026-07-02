# Agent-Trading

> **PAPER TRADING — Experimental autonomous AI agent. Leveraged & inverse ETFs on a small account are high-risk. Not financial advice.**

Autonomous multi-agent trading system that runs a deterministic-signals scan over a 71-ticker **leveraged/inverse ETF** universe, asks a strategist agent for a regime call + ranked candidate ideas, and asks a constructor agent to build a **1–12 position portfolio (or all-cash)** that's then executed via **Alpaca paper**. The LLM stages also read the system's **own realized track record** (win rates by factor, confidence calibration, what killed each exit) as evidence for sizing and conviction. Bullish theses are expressed by holding bull ETFs; bearish theses by holding inverse ETFs — never short selling, never options. Runs unattended on a Linux VPS under systemd, with a Streamlit dashboard.

The canonical build spec is [CLAUDE.md](./CLAUDE.md). This README is the operator's manual.

---

## Mandatory risk warning

> Leveraged and inverse ETFs decay path-dependently in volatile markets and are not buy-and-hold instruments — a 3x ETF held through chop bleeds value even when the underlying ends flat. A $2,500 account cannot diversify meaningfully across many such positions — concentration risk is structural, not a flaw to fix. This system is an experiment in autonomous AI trading agents, not a path to reliable returns. Expect losses. Do not deploy capital you cannot afford to lose entirely. None of this is financial advice.

---

## Architecture at a glance

```
systemd timer (Linux VPS) ──▶ orchestrator.py
                                                      ├─ Stage 0: market_gate   (Alpaca clock, $0)
                                                      ├─ Stage 1: signals       (deterministic Python, $0)
                                                      ├─       state context     (broker positions + PnL history)
                                                      ├─ Stage 2: strategist    (Sonnet 4.6, ~$0.05)
                                                      ├─ Stage 3: construct     (Opus 4.7, ~$0.20, 1–12 or all-cash)
                                                      ├─ Stage 3.5: critic      (Sonnet 4.6 low, ~$0.03, +1 retry on reject)
                                                      ├─ Stage 4: sanity        (deterministic Python, $0, 10 rules)
                                                      └─ Stage 5: execute       (Alpaca paper) + meta-scheduler writes next_run.json
                                                            │
                                                            ▼
                                                   state/ (JSON, append-only)
                                                            │
                                                            ▼
                                                   dashboard.py (Streamlit, dark mode)
```

A cycle is a sequence of **schema-validated stages**. Four are LLM calls — the strategist, the constructor, the critic, and the meta-scheduler that picks the next-run window; the rest are deterministic Python. A critic rejection triggers one constructor rerun, so a cycle is 4 LLM calls in the steady state, 5 on a reject. Typical per-cycle LLM cost is **~$0.30**.

`monitor.py` runs more frequently than the orchestrator and only checks kill conditions; it can flatten a position but cannot open new ones.

### Model tiering

Per-stage model assignment matches the cost/quality demand of each decision. All defaults are overridable via `.env` (`MODEL_*` vars).

| Stage | Model | Why |
|---|---|---|
| **market_gate** | n/a (deterministic) | Alpaca clock query — free, zero LLM call. |
| **signals** | n/a (deterministic) | yfinance + universe metadata. 71 ticker rows out (the LLM stages read a compact factor-grouped rendering), zero LLM cost. |
| **strategist** | `claude-sonnet-4-6` (effort `medium`) | Reads the signals table → emits a regime call + 0–6 candidate ideas with thesis + confidence. Medium effort caps thinking-budget allocation. |
| **construct** | `claude-opus-4-7` (effort `high`) | **The actual trade decision** — picks positions, sizing, kill conditions. On a $2,500 leveraged/inverse-ETF account, position-selection quality has direct PnL impact: multi-position correlation reasoning + sizing math under a 15%/position cap + per-row kill-condition tailoring. Override with `MODEL_CONSTRUCTOR=claude-sonnet-4-6` if cost dominates. |
| **critic** | `claude-sonnet-4-6` (effort `low`) | Adversarial second pass over the portfolio. Accept/reject with suggested changes; a reject reruns the constructor once. |
| **orchestrator-meta** *(timing only)* | `claude-sonnet-4-6` | Decides the next-run window from regime + portfolio state. Bounded 1–24h, falls back to a 4h/6h heuristic if the LLM output is unusable. Small payload, no schema — Sonnet is right-sized. |

Per-run cost cap defaults to $3; daily cap defaults to $12.

---

## What we scan & trade

**Universe: 71 leveraged/inverse ETFs** across 41 factor groups — 25 bull/bear
pairs plus 5 asymmetric +2x bull / -1x inverse lines (BITX/BITI crypto,
PLTU/PLTD, AMZU/AMZD, GGLL/GGLS, METU/METD) and 11 solo bulls (widened from
29 tickers on 2026-06-10 and from 57 on 2026-07-02). Curated; entries that
fail liquidity filters at signals time still appear in the table with their
numeric features, but the strategist learns to ignore low-ADV rows.

Every position is a **long holding of a leveraged or inverse ETF**. A bullish
thesis holds the bull ETF; a bearish thesis holds the inverse ETF. Bearish
views are expressed by **buying inverse ETFs** (SQQQ, SPXU, TZA, …) — never by
short selling and never by puts. Subject to a per-position **15% NAV entry cap**
and **25% loss kill condition**.

| Factor | Bull | Inverse (bear) |
|---|---|---|
| Nasdaq-100 (3x) | TQQQ | SQQQ |
| S&P 500 (3x) | UPRO | SPXU |
| Dow Jones (3x) | UDOW | SDOW |
| Russell 2000 small-caps (3x) | TNA | TZA |
| S&P 500 high beta (3x) | HIBL | HIBS |
| Semiconductors (3x) | SOXL | SOXS |
| Technology sector (3x) | TECL | TECS |
| Internet (3x) | WEBL | WEBS |
| Biotech (3x) | LABU | LABD |
| FTSE China (3x) | YINN | YANG |
| Emerging markets (3x) | EDC | EDZ |
| Financials, broad (3x) | FAS | FAZ |
| Energy sector (2x) | ERX | ERY |
| Oil & gas E&P (2x) | GUSH | DRIP |
| Natural gas (2x) | BOIL | KOLD |
| WTI crude futures (2x) | UCO | SCO |
| 20+yr Treasuries / rates (3x) | TMF | TMV |
| Gold miners (2x) | NUGT | DUST |
| Gold bullion (2x) | UGL | GLL |
| Silver (2x) | AGQ | ZSL |
| VIX front-month | UVXY (1.5x) | SVIX (1x inverse) |
| Bitcoin futures | BITX (2x) | BITI (1x inverse) |
| Ether futures (2x) | ETHU | ETHD |
| NVIDIA (2x, single-stock) | NVDL | NVD |
| Tesla (2x, single-stock) | TSLL | TSLZ |
| MicroStrategy (2x, single-stock) | MSTU | MSTZ |
| Palantir (single-stock) | PLTU (2x) | PLTD (1x inverse) |
| Amazon (single-stock) | AMZU (2x) | AMZD (1x inverse) |
| Alphabet (single-stock) | GGLL (2x) | GGLS (1x inverse) |
| Meta (single-stock) | METU (2x) | METD (1x inverse) |
| Homebuilders (3x) | NAIL | — |
| Aerospace & defense (3x) | DFEN | — |
| Healthcare (3x) | CURE | — |
| Regional banks (3x) | DPST | — |
| Coinbase (2x, single-stock) | CONL | — |
| Utilities (3x) | UTSL | — |
| Retail (3x) | RETL | — |
| MSCI Brazil (2x) | BRZU | — |
| MSCI India (2x) | INDL | — |
| FTSE Europe (3x) | EURL | — |
| MSCI South Korea (3x) | KORU | — |

### What we explicitly don't trade

- **Options** — no calls, puts, spreads, or straddles
- **Spot single-name equities** (no buying AAPL or NVDA stock directly). *Leveraged single-stock ETFs* (NVDL/NVD, TSLL/TSLZ, MSTU/MSTZ, CONL as of 2026-06-10; PLTU/PLTD, AMZU/AMZD, GGLL/GGLS, METU/METD as of 2026-07-02) **are** in the universe — they're listed ETFs riding the same caps and kill rails, but they carry company event risk (earnings, guidance) outside the macro calendar, which the strategist prompt explicitly flags.
- **Unleveraged broad-market ETFs as core positions** — exposure is always via a leveraged/inverse ETF
- **Actual short-selling** — bearish theses are expressed as long inverse ETFs (SQQQ, SPXU, etc.). Cash account, no margin.
- **Live trading** — gate hard-disabled until §"Promotion to live" in [CLAUDE.md](./CLAUDE.md) is met

### Factor coverage

The 29 paired factors give the strategist a directional expression on both
sides of each theme; the 11 solo bulls are long-only expressions (bearish view
= don't hold them). Several equity factors (Nasdaq / S&P / Dow / tech / semis /
high-beta / internet) are correlated risk-on beta — the constructor
de-duplicates by `factor`, and the signals stage now emits a live
**`factor_correlations`** block (30d bull-ETF return correlations ≥ |0.7|) so
the agents see which factors are *currently* the same bet instead of relying
on a static rule. Rates (TMF/TMV), gold (NUGT/DUST, UGL/GLL), silver and
crude are the main diversifiers against equity beta; UVXY is the long-vol
tail hedge and SVIX the calm-regime short-vol expression. MSTR and COIN trade
as crypto beta — the prompts warn against stacking them with BITX/ETHU.

---

## How the agents work

Each cycle runs as the sequence of stages below. The LLM agents read role-specific system prompts under [`prompts/`](./prompts/). Anthropic prompt caching keeps the static system block cheap across calls. Each stage emits a schema-validated JSON artifact under `state/runs/{run_id}/`; a schema-failed agent output is retried once with the validation error fed back, and a second failure aborts the run.

### 0. Market gate — Alpaca clock, $0
Queries `/v2/clock`. If markets are closed (weekend, holiday, after-hours), writes `market_gate.json` + a closed-market `next_run.json` pointing at the broker-reported next open, and exits. No LLM calls billed on a closed-market **trade** cycle. (A **review** cycle deliberately skips this stage — it's designed to run after close — so an after-hours review still runs signals + strategist + meta and bills ~$0.05; it just never opens orders. See "Cycle intents" below.)

### 1. Signals — deterministic Python, $0
For each of the 71 universe tickers, computes from yfinance daily history:
- `last_close`, `adv_30d` (liquidity)
- `momentum_30d_pct` / `momentum_60d_pct` (trailing returns)
- `hv_30d_annualised` / `hv_90d_annualised` (close-to-close vol)
- `dist_from_50d_ma_pct` / `dist_from_200d_ma_pct` (trend position)
- `rsi_14` (overbought/oversold), `rel_strength_spy_30d` (leading vs lagging the tape)
- `trend_r2` (trend-vs-chop quality — chop is where daily-rebalanced leveraged ETFs decay)
- `upcoming_macro_events_7d` (per-ticker FOMC/CPI/NFP/PCE events within 7 days)

Plus a universe-level `factor_correlations` block (30d bull-ETF return
correlations ≥ |0.7|). Output: `signals.json`. The LLM stages read a
**compact factor-grouped rendering** (`lib.signals.compact_for_llm`) — one
row per factor with both legs inlined, nulls stripped — which keeps the
expensive constructor call's input at roughly the old 29-ticker size despite
the wider universe.

### 1b. Cycle dedup — Python, $0
If the signals fingerprint AND the broker-position fingerprint both match the prior cycle's, the orchestrator skips strategist + construct + execute and reuses the cached portfolio. Stored in `state/last_cycle_hash.json`.

### 2. Strategist — Sonnet 4.6, ~$0.05
One LLM call. Reads the compact signals + current broker positions (with unrealized P&L %) + recent PnL history + the **performance memo** (`lib/feedback.py`, $0: the agent's own realized win/loss record by factor, confidence-bucket calibration — "your 0.70–0.84 picks won X%" — and recent exits tagged with what killed them). The memo is framed as calibration evidence for conviction scores, explicitly **not** an instruction to trade less. Emits a **regime classification** (one of `risk_on`, `risk_off`, `neutral`, `vol_elevated`, `trending_up`, `trending_down`, `choppy`) + up to **6 candidate ideas** with `instrument_kind` (always `etf`), `thesis` (signal-citing), and `confidence` ∈ [0, 1].

Bullish theses name the bull ETF; bearish theses name the inverse ETF (SQQQ, SPXU, etc.). The system never goes broker-short and never trades options.

Output: `view.json`.

### 3. Portfolio construction — Opus 4.7, ~$0.20
Reads `signals.json` + `view.json`. Picks 1–12 positions from the strategist's candidate list — or all-cash if the strategist returned zero candidates and the regime is genuinely uninvestable.

Builds the portfolio with sizing math:
- Integer shares from `position_pct × NAV / share_price`, floored
- Refuses a position where even 1 share exceeds the per-position cap
- Hard 15% per-position NAV entry cap, sum of `position_pct` ≤ 100 (residual = cash buffer)
- Explicit kill conditions per leg: max_loss_pct = 25 + at least one of ETF price stop, **trailing stop** (`trailing_stop_pct` — a %-from-peak ratchet the constructor may choose per position; monitor tracks peaks and enforces it), or time stop

Bias: take a position when the strategist surfaces a candidate with sufficient confidence — abstaining cycle after cycle is not the goal. The constructor receives:
- Compact signals + view (from the upstream stages)
- Current broker positions with unrealized P&L % (state awareness — bias to hold winners vs churn)
- Recent PnL history (last 5 cycles' regime + realized 4h PnL — self-correct drift)
- The performance memo (its own factor-level record + confidence calibration as sizing evidence)
- Adaptive per-position cap (lower in drawdown via `risk.adaptive_position_cap_pct`) + universe-median HV30 context for vol-aware sizing judgment

Output: `portfolio.json`.

### 3.5 Critic — Sonnet 4.6 (low effort), ~$0.03
Adversarial second-pair-of-eyes pass. Reads `view.json` + `portfolio.json` **plus** current positions, PnL history, the performance memo and a free pre-computed sanity preview (it previously reviewed blind), returns either `{accept: true}` or `{accept: false, critique, suggested_changes}`. On reject, the orchestrator re-runs the constructor ONCE with the critique fed back. **Skipped entirely ($0, deterministic auto-accept artifact) when the constructed portfolio is a no-op against current holdings** — zero orders means nothing new to critique. The prompt pins the critic's job as *better trades, not fewer trades*. Worst case: ~$0.30 extra (one critic + one retry construct); typical case: ~$0.03 (accept on first pass). Output: `critique.json`.

### 4. Sanity — deterministic Python, $0
Post-construct rules (see [`lib/sanity.py`](./lib/sanity.py)). Each rule has a fixed severity (`warn` or `fail`); the overall sanity status is the worst per-rule status. Non-blocking by default — `SANITY_BLOCK_ON_FAIL=true` escalates `fail` into a hard skip of `stage_execute`.

Rules (10 total):
- `construction_rationale_meaningful` — ≥ 80 chars
- `kill_conditions_complete` — max_loss_pct ∈ (0,100] + at least one price / trailing / time stop
- `position_backed_by_strategist` — every position's symbol is endorsed at confidence ≥ 0.5 in `view.json`
- `position_within_adaptive_cap` — position_pct ≤ the drawdown-adaptive **hold ceiling** (25% at-peak, 12.5% at ≥10% drawdown, linear between). The 25% base is a hard bound (the schema rejects any position above 25%); the drawdown-tightened value is advisory by default (constructor-guided + non-blocking unless `SANITY_BLOCK_ON_FAIL`), so a held winner is not force-trimmed mid-drawdown — the 25% loss-kill and 8% daily breaker remain the hard backstops
- `entry_cap_on_adds` — a position above the drawdown-adaptive **entry cap** (15% at-peak, 7.5% at ≥10% drawdown) is allowed only as drift of an existing holding; opening or adding above the entry cap fails. This rule always hard-skips `stage_execute` on `fail`, independent of `SANITY_BLOCK_ON_FAIL`. (Entry-cap discipline + hold-ceiling drift = winners may run without being force-trimmed back to entry weight every cycle.)
- `per_underlying_pct_cap_30` — Σ position_pct per ticker ≤ 30%
- `position_size_matches_confidence` — position_pct ≤ strategist confidence × 15 (skipped for a held winner that merely drifted above the ceiling)
- `position_notional_above_floor` — position notional ≥ $50 (spread + fees dominate below this on a $2,500 account)
- `position_adv_liquidity` — position notional ≤ 1% of ticker's 30-day dollar ADV
- `reentry_cooldown` — a symbol fully exited within the last 7 days isn't re-entered unless the strategist confidence clears the override threshold (≥ 0.8)

Output: `sanity.json`.

### 5. Execution + meta-scheduling
- **execute**: diffs target portfolio vs current Alpaca positions, emits **close orders first** (free cash) then **open orders**. Integer-share market orders for ETFs only; any option-shaped payload is defensively rejected before it can reach the broker. Gated behind `ORDERS_ENABLED=true`. Order safety invariant: orders never cross zero in a single ticket — a flip from long to short (or vice versa) is split into a close + an open.
- **meta** — Sonnet 4.6: given the freshly-built portfolio + recent NAV trend + time of day + strategist regime, decides when the **next cycle fires** (bounded 1–24h, falls back to a 4h/6h heuristic if the LLM output is unusable) and which intent it runs (trade vs review — see below). Writes `state/next_run.json`.

### Cycle intents — trade vs review
Most cycles run the full pipeline (a **trade** cycle). The meta-scheduler can instead schedule a lightweight **review** cycle — signals + strategist + meta only, no construct/critic/execute and no orders — to re-check the regime cheaply between trade cycles. Autonomous review cycles are capped by `MAX_REVIEW_CYCLES_PER_DAY` (default 2). The intent is read from the prior `next_run.json` by default; `--intent {trade,review}` forces it for a manual run.

### Risk controls (always-on)

- **8% daily-drawdown circuit breaker**: NAV down ≥8% in a UTC day → next orchestrator run halts new orders; `monitor.py` still flattens. Resets at 00:00 UTC.
- **`monitor.py`** (every 15 min during US market hours): re-evaluates kill conditions on every open position — the hard 25% loss cap, fixed price/time stops, and any constructor-chosen **trailing stops** (peak marks ratcheted in `state/position_peaks.json`). Flattens via the broker and logs every broker-accepted flatten's cause to `state/kill_events.jsonl` (loss cap / price stop / time stop / trailing stop / orphan) so the performance memo and the Calibration tab can show *why* positions died. Also warns in the journal on **trade-sync staleness** (orders accepted recently but no fills synced — a failure that once went unnoticed for 5 weeks). **Cannot open new positions** — it's a stop-loss daemon, not a trader.
- **Halt flag (`state/halt.flag`)**: presence stops both orchestrator and monitor *before any API call*. Toggled from the dashboard, or `sudo -u agent touch /opt/agent-trading/state/halt.flag`.
- **Cost caps**: per-run **$3**, daily **$12**. Cleanly aborts between stages if hit.
- **Market gate**: a **trade** cycle queries Alpaca's clock before any LLM work. Closed market → no LLM calls billed, no orders submitted, exits cleanly with a `next_run_at` pointing at the broker-reported next open. (A **review** cycle deliberately skips the gate — it's meant to run after close — so it still bills ~$0.05 for signals + strategist + meta, but never submits orders.)
- **Post-construct sanity rules** (`lib/sanity.py`, runs after every cycle, zero LLM cost). **Non-blocking by default** — set `SANITY_BLOCK_ON_FAIL=true` to hard-skip `stage_execute` on `fail` status (the `entry_cap_on_adds` rule hard-skips regardless).
- **Modelled trading costs** (`lib/alpaca_costs.py`, wrapped by `lib/pnl.py`): an Alpaca-calibrated cost model — per-side slippage (the dominant live friction) + sell-side SEC/FINRA TAF; commission is $0 on Alpaca. With `PAPER_COST_MODEL=true` (default) every synced fill is stamped with modelled `fees_usd` (paper only; live uses real fees) and `slippage_usd`, so realized P&L, the equity curve, **and the SPY/Sharpe comparison are NET of cost** — `realized_balance_series()` (`lib/dashboard_data.py`) subtracts both. Paper Sharpe is therefore friction-honest for the promote-to-live gate. Margin interest and borrow fees are $0 (cash account, long-only). Rates are env-configurable (they reset periodically). Set `PAPER_COST_MODEL=false` to fall back to gross fills.
- **Order safety invariant** (`lib/orders._plan_for_symbol`): orders never cross zero in a single ticket. Going from long to short on a symbol is split into close + open. The long-only schema never triggers it today, but the invariant is defensive against future short-enabling changes.

### Strategy in one paragraph

On a $2,500 paper account in a leveraged/inverse-ETF universe, **the edge isn't sector picking — it's discipline**: positive-EV only, hard 15% per-position NAV entry cap, 25% loss kill condition, no options, no broker shorts (bearish views go via inverse ETFs).

The pipeline reads deterministic signals (momentum, vol, MA distance, RSI, relative strength, trend quality, live factor correlations) for 71 curated tickers and feeds them — together with the system's own realized track record — to a Sonnet strategist that picks ≤6 high-conviction ideas; an Opus constructor then sizes 1–12 positions and chooses each position's stop style (fixed, trailing, or time). The meta-scheduler sets the next-run window per cycle (1–24h); the market gate skips weekends and holidays automatically.

**Losses are expected** — the experiment is whether prompt + schema discipline produces an honest Sharpe across many cycles, not whether it picks individual winners. Per-cycle LLM cost is ~$0.30, which keeps experimentation affordable.

---

## Supported runtime: Linux VPS only

**Linux VPS + systemd is the sole supported production runtime.** The system is designed to run unattended for weeks on a box that doesn't sleep, with systemd timers driving the orchestrator and monitor and a `127.0.0.1`-bound Streamlit dashboard reached over Tailscale or an SSH tunnel. Windows is not a supported runtime.

Setup below: [Linux VPS setup](#linux-vps-setup). The full operator playbook is [`deploy/README.md`](./deploy/README.md).

---

# Linux VPS setup

### 1. Provision a VPS

| Provider | Spec | Cost |
|---|---|---|
| **Hetzner CX23** | 2 vCPU / 4 GB / 40 GB SSD, Ubuntu 24.04 LTS, primary IPv4, Helsinki | €4.49/mo |
| Other | Anything 1+ vCPU, 2+ GB RAM, Ubuntu 24.04 LTS | varies |

Upload your SSH **public key** (from `ssh-keygen -t ed25519` on your laptop) when creating the server. Disable password auth.

### 2. Bootstrap on the VPS

```bash
ssh root@<your-server-ip>
apt-get update && apt-get install -y git
git clone https://github.com/aleexxr94/agent-trading.git /opt/agent-trading
bash /opt/agent-trading/deploy/install.sh
```

The installer is idempotent — re-running it pulls the latest commit, refreshes the venv, and preserves `.env` and `state/`.

What it does:
- Installs `python3` (3.12 on 24.04), `python3-venv`, `git`, `jq`, `build-essential`.
- Creates a non-root system user `agent` (no shell, no sudo).
- Clones the repo to `/opt/agent-trading`, owned by `agent:agent`.
- Provisions `.venv` and installs `requirements.txt`.
- Seeds `.env` from `.env.example` at mode 600 (preserves existing).
- Installs the systemd units (orchestrator + monitor service+timer, dashboard service, scheduler service).
- Drops a logrotate config for `state/*.log`.

### 3. Configure secrets

```bash
sudo -u agent nano /opt/agent-trading/.env
```

Fill in:

| Variable                     | Notes                                                                 |
| ---------------------------- | --------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`          | From [console.anthropic.com](https://console.anthropic.com).           |
| `ALPACA_API_KEY` / `_SECRET` | **Paper** keys from [alpaca.markets](https://alpaca.markets) — never live. |
| `ALPACA_BASE_URL`            | `https://paper-api.alpaca.markets` (default).                         |
| `ALPACA_DATA_URL`            | `https://data.alpaca.markets` (default) — market-data feed.           |
| `MODEL_*`                    | Override the per-stage model IDs if needed (defaults are Claude 4.X — strategist/critic/meta on Sonnet 4.6, constructor on Opus 4.7). |
| `VIRTUAL_NAV_USD`            | Default `2500`. Sizing baseline (this value + realized P&L), used instead of Alpaca paper's $100k equity. If unset/malformed it falls back to a hard-coded `2500` — it does **not** read broker equity. |
| `PER_RUN_COST_CAP_USD`       | Default `3.00`. Per-run hard cap.                                     |
| `DAILY_COST_CAP_USD`         | Default `12.00`. Daily hard cap (resets at 00:00 UTC).                |
| `ORDERS_ENABLED`             | Default `false`. When `false`, the pipeline writes `portfolio.json` but never touches the broker. Flip to `true` once decisions look right. |
| `SANITY_BLOCK_ON_FAIL`       | Default `false`. When `true`, any sanity rule with status `fail` hard-skips `stage_execute`. |
| `LIVE_TRADING_ENABLED`       | **Leave `false`.** See [Promotion to live](./CLAUDE.md#promotion-to-live-documented-only--do-not-enable-in-code). |

### 4. Manual smoke (do this BEFORE enabling timers)

```bash
# --dry-run: no orders, no LLM calls — runs the pipeline against tests/fixtures/*
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py --dry-run
# A real cycle (calls the LLMs; writes orders only if ORDERS_ENABLED=true)
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py
sudo -u agent tail -n 5 /opt/agent-trading/state/decisions.jsonl
sudo -u agent tail -n 5 /opt/agent-trading/state/costs.jsonl
```

A real run typically costs ~$0.03–$0.50 depending on candidate count, well under the $3 per-run cap.

Other orchestrator flags: `--intent {trade,review}` forces the cycle type, `--run-id` overrides the generated run id, and `--ignore-cap` bypasses the daily review-frequency cap (only honoured alongside `--intent review`).

### 5. Start the dashboard

```bash
systemctl enable --now agent-dashboard.service
systemctl status agent-dashboard.service
```

Bound to `127.0.0.1:8501` only — **never** exposed to the public internet. Two ways to reach it from your laptop:

**Tailscale Serve** (cleanest, also works from your phone). After installing Tailscale on both the VPS and your laptop:
```bash
# On the VPS
tailscale serve --bg http://localhost:8501
tailscale serve status   # prints the https://<host>.<tailnet>.ts.net URL
```
Open that URL in your laptop browser. See [`deploy/tailscale.md`](./deploy/tailscale.md) for the full setup.

**SSH local port forward** (no Tailscale needed). On your laptop:
```bash
ssh -L 8501:127.0.0.1:8501 root@<your-server-ip>
```
Then `http://localhost:8501`.

The dashboard has nine tabs: **Portfolio** (NAV, cash, open positions, P&L), **Cycles** (per-run summaries), **Decisions** (stage-by-stage decision log), **Performance** (equity curve, drawdown, LLM cost), **vs S&P 500** (SPY benchmark), **Trades** (per-trade realized P&L), **Agent Logs** (latest stage artifacts + sanity report), **Calibration** (the agent's own track record exactly as the LLM stages see it — win rate by confidence bucket / factor / regime, critic accept-reject record, kill-event audit, activity health, trade-sync staleness alert, and the auto-tracked promotion scorecard), and **Settings** (emergency-stop toggle, paper/live indicator, cost today).

### 6. Enable autonomous timers

Once the smoke run looks clean and the dashboard renders:

```bash
systemctl enable --now agent-orchestrator.timer agent-monitor.timer
systemctl enable --now agent-scheduler.service
systemctl list-timers --all 'agent-*'
journalctl -u agent-orchestrator.service -f
```

Enable `agent-scheduler.service` alongside the timers — it's the dynamic-cadence daemon that reads `state/next_run.json` and fires the orchestrator at the meta-scheduler-chosen time. Without it, the only trigger is the orchestrator timer's daily 13:30 UTC safety-net fallback and the 1–24h cadence is ignored. (`install.sh` auto-enables the scheduler only if `agent-orchestrator.timer` was already enabled at install time, so on a fresh setup you must enable it explicitly here.) The monitor fires every 15 minutes during US market hours.

### Update the deployment

Changes land via PR: open a PR, get it reviewed, and merge to `main`. Merging to `main` triggers `.github/workflows/deploy.yml`, which SSHes into the VPS, runs `install.sh`, and restarts the dashboard — no manual deploy needed.

Manual fallback (operator-only, if the Action is disabled or failing):
```bash
ssh root@<your-server-ip>
bash /opt/agent-trading/deploy/install.sh
systemctl restart agent-dashboard.service
# Timers pick up changes automatically on the next fire.
```

### Uninstall

```bash
systemctl disable --now agent-orchestrator.timer agent-monitor.timer agent-dashboard.service agent-scheduler.service
rm /etc/systemd/system/agent-{orchestrator,monitor,dashboard}.{service,timer}
rm /etc/systemd/system/agent-scheduler.service
systemctl daemon-reload
rm /etc/logrotate.d/agent-trading
# Keep /opt/agent-trading/state — that's your run history.
```

Note the scheduler is a bare `.service` (no `.timer`); disabling it is what stops the dynamic cadence from continuing to poll `state/next_run.json` and trying to start the orchestrator after the rest of the system is removed.

---

## Halt procedure

The halt flag is checked before every LLM API call and before any order. The orchestrator and monitor wrappers (`deploy/run_orchestrator.sh`, `deploy/run_monitor.sh`) short-circuit before activating the venv when it's present, and the systemd units refuse to start while the flag exists.

```bash
sudo -u agent touch /opt/agent-trading/state/halt.flag    # halt
sudo -u agent rm    /opt/agent-trading/state/halt.flag    # resume
```

Or click **Emergency stop** on the dashboard's Settings tab.

---

## Logs and state

| Location                         | Contents                                                             |
| -------------------------------- | -------------------------------------------------------------------- |
| `state/runs/{run_id}/`           | Per-run JSON: `market_gate.json`, `signals.json`, `view.json`, `portfolio.json`, `critique.json`, `sanity.json`, `orders.json`, `next_run.json` |
| `state/decisions.jsonl`          | Append-only decision log (one row per stage, schema-validated)        |
| `state/costs.jsonl`              | Per-LLM-call cost ledger; daily and per-run caps enforced from this file |
| `state/current_portfolio.json`   | Latest known portfolio (consumed by `monitor.py` and the dashboard)   |
| `state/next_run.json`            | Meta-scheduler's next-run timestamp + intent                          |
| `state/halt.flag`                | Presence = stop                                                       |

Quick inspection:

```bash
sudo -u agent tail -n 20 /opt/agent-trading/state/decisions.jsonl
sudo -u agent tail -n 20 /opt/agent-trading/state/costs.jsonl
journalctl -u agent-orchestrator.service -n 200 --no-pager
journalctl -u agent-dashboard.service    -n 200 --no-pager
```

The `bin/analyze_runs.py` helper joins the per-run artifacts into a cycle-by-cycle table (`python -m bin.analyze_runs [--limit N] [--csv path]`). The dashboard's Performance and Settings tabs surface the same numbers without the JSON wrangling.

If you have paper history that predates the modelled-cost layer (fills written before `PAPER_COST_MODEL` existed), run `python -m bin.backfill_costs --dry-run` then `python -m bin.backfill_costs` once to net modelled slippage + SEC/TAF onto those legacy rows (idempotent; backs up `trades.jsonl` first). New fills are stamped automatically at sync time, so this is a one-off for pre-upgrade history.

---

## Repo hygiene

- `.env` is gitignored. **Never** commit secrets.
- `state/` is gitignored — runtime artifacts only.
- All schemas under `schemas/` are validated on every write. Schema-failed agent outputs retry once with the validation error fed back; second failure aborts the run.
- Conventional commits. Open a PR against `main`; do not push to `main` directly.
- `lib/broker.py` is the only abstraction layer over the broker — the live broker is Alpaca via `lib/alpaca_client.py`; the interface is retained so an alternative broker could be added behind it later if ever needed.

---

## Promotion to live (do **not** attempt)

Live trading is gated by a triple lock and is intentionally not buildable from this checkout:

1. `LIVE_TRADING_ENABLED=true` env var — disabled by default.
2. `LIVE_VERSION = 0` constant in `lib/live_gate.py` — must be bumped in code. The shared `assert_live_gate()` guard (called by both `orchestrator.py` and `monitor.py`) refuses to run if the env var is set while the version is still 0.
3. `lib/alpaca_client.py` refuses to construct against a non-paper base URL unless both gates are satisfied.

The full set of pre-conditions (≥ 4 weeks paper, Sharpe ≥ 0.5, max DD ≤ 25%, Alpaca live eligibility confirmed for UK ETF trading — no new broker client required) is documented in [CLAUDE.md §Promotion to live](./CLAUDE.md#promotion-to-live-documented-only--do-not-enable-in-code).

---

## Status

The full pipeline runs end-to-end on paper data; the dashboard renders against fixtures or live state. See [CLAUDE.md](./CLAUDE.md) for the full build spec, [`deploy/README.md`](./deploy/README.md) for the Linux operator playbook, and the `state/` directory (when populated) for current behaviour.
