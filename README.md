# Agent-Trading

> **PAPER TRADING — Experimental autonomous AI agent. Leveraged & inverse ETFs on a small account are high-risk. Not financial advice.**

Autonomous multi-agent trading system that runs a deterministic-signals scan over a 29-ticker **leveraged/inverse ETF** universe, asks a strategist agent for a regime call + ranked candidate ideas, and asks a constructor agent to build a **1–12 position portfolio (or all-cash)** that's then executed via **Alpaca paper**. Bullish theses are expressed by holding bull ETFs; bearish theses by holding inverse ETFs — never short selling, never options. Runs unattended on a Linux VPS under systemd, with a Streamlit dashboard.

The canonical build spec is [CLAUDE.md](./CLAUDE.md). This README is the operator's manual.

---

## Why ETF-only (migration note)

The system originally also traded listed options (long calls/puts on a set of
underlyings). Options were **removed entirely** on 2026-05-29: the $2,500
account size made them non-viable — the 15%-per-position cap (~$375) rarely
cleared a single contract, and after six months of paper trading the system
opened **zero** option positions. The strategy is now purely leveraged and
inverse ETFs. Bearish views are expressed by **buying inverse ETFs** (SQQQ,
SPXU, TZA, …), not by short selling and not by puts. The factors the option
underlyings used to cover (rates, energy) now have real leveraged ETF pairs
(TMF/TMV, ERX/ERY).

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
                                                      ├─ Stage 4: sanity        (deterministic Python, $0, 9 rules)
                                                      └─ Stage 5: execute       (Alpaca paper) + write next_run.json
                                                            │
                                                            ▼
                                                   state/ (JSON, append-only)
                                                            │
                                                            ▼
                                                   dashboard.py (Streamlit, dark mode)
```

`monitor.py` runs more frequently and only checks kill conditions; it can flatten a position but cannot open new ones.

### v2 pipeline (2026-05-13)

The v1 pipeline had 5 LLM-bearing stages (screen, bull/bear research × 8 candidates, scenarios, construct, meta) costing ~$1.50–2.50/cycle. The bull/bear/scenarios stages produced qualitative text that ultimately compressed to a few numbers anyway — and they were the heaviest, least reliable parts of the system (Sonnet+adaptive-thinking empty-output failures, cost-cap overruns, recurring TLT straddles).

v2 collapses screen + bull/bear + scenarios into a single **deterministic signals stage** (zero LLM cost, just yfinance features per ticker) plus a **single strategist LLM call** that produces a regime + ranked candidate list. The construct stage still owns position selection on Opus 4.7 because that IS the soft-judgement call. The v2-winrate add-on (PR after #67) layered a critic agent, PnL feedback, cycle dedup, and a drawdown-adaptive cap on top.

- LLM calls per full trade cycle: **20 → 4** (strategist + construct + critic + meta-scheduler; ~80% reduction, +1 for the critic on rejection retry = 5)
- Typical per-cycle cost: **~$0.30** (was ~$1.50–2.50; critic adds ~$0.03, the meta-scheduler call ~$0.02)
- At 2 cycles/weekday: **~$12/month** (was ~$300/month)

The market_gate stage is the cheapest reliability win: skip the entire pipeline cleanly when markets are closed instead of producing a portfolio that can't trade. Cycle-dedup further skips strategist + construct + execute when the signals fingerprint and broker positions are both unchanged from the prior cycle.

### Model tiering rationale

Per-stage model assignment matches the cost/quality demand of each decision. All defaults overridable via `.env` (`MODEL_*` vars).

| Stage | Model | Why |
|---|---|---|
| **market_gate** | n/a (deterministic) | Alpaca clock query — free, zero LLM call. |
| **signals** | n/a (deterministic) | yfinance + universe metadata. 29 ticker rows out, zero LLM cost. Replaces the v1 screener + bull/bear + scenarios chain. |
| **strategist** | `claude-sonnet-4-6` (effort `medium`) | Reads the signals table → emits a regime call + 0–6 candidate ideas with thesis + confidence. Sonnet + medium effort caps thinking-budget allocation (PR ε lesson on the v1 scenarios stage). |
| **construct** | `claude-opus-4-7` (effort `high`) | **The actual trade decision** — picks positions, sizing, kill conditions. On a $2,500 leveraged/inverse-ETF account, position-selection quality has direct PnL impact: multi-position correlation reasoning + sizing math under a 15%/position cap + per-row kill-condition tailoring. Override with `MODEL_CONSTRUCTOR=claude-sonnet-4-6` if cost dominates. |
| **orchestrator-meta** *(timing only)* | `claude-sonnet-4-6` | Decides next-run window from regime + portfolio state. Bounded 1–24h, falls back to a 4h/6h heuristic if the LLM output is unusable. Small payload, no schema — Sonnet is right-sized. |

Per-run cap defaults to $3; daily cap defaults to $12.

---

## What we scan & trade

**Universe: 29 leveraged/inverse ETFs** across 15 factor groups — 13 bull/bear
pairs plus UVXY (solo long-vol) and BITX/BITI (crypto). Curated; entries that
fail liquidity filters at signals time still appear in the table with their
numeric features, but the strategist learns to ignore low-ADV rows.

Every position is a **long holding of a leveraged or inverse ETF**. A bullish
thesis holds the bull ETF; a bearish thesis holds the inverse ETF. Subject to
per-position **15% NAV cap** and **25% loss kill condition**.

| Factor | Bull | Inverse (bear) |
|---|---|---|
| Nasdaq-100 (3x) | TQQQ | SQQQ |
| S&P 500 (3x) | UPRO | SPXU |
| Russell 2000 small-caps (3x) | TNA | TZA |
| Semiconductors (3x) | SOXL | SOXS |
| Technology sector (3x) | TECL | TECS |
| Biotech (3x) | LABU | LABD |
| FTSE China (3x) | YINN | YANG |
| Financials, broad (3x) | FAS | FAZ |
| Energy sector (2x) | ERX | ERY |
| Oil & gas E&P (2x) | GUSH | DRIP |
| Natural gas (2x) | BOIL | KOLD |
| 20+yr Treasuries / rates (3x) | TMF | TMV |
| Gold miners (2x) | NUGT | DUST |
| VIX front-month (1.5x) | UVXY | — |
| Bitcoin futures | BITX (2x) | BITI (1x inverse) |

### What we explicitly don't trade

- **Options** — removed entirely (see the migration note above); no calls, puts, spreads, or straddles
- **Spot single-name equities** (no AAPL, NVDA, etc.) — too much idiosyncratic risk on a $2,500 account
- **Unleveraged broad-market ETFs as core positions** — exposure is always via a leveraged/inverse ETF
- **Actual short-selling** — bearish theses are expressed as long inverse ETFs (SQQQ, SPXU, etc.). Cash account, no margin.
- **Live trading** — gate hard-disabled until §"Promotion to live" in [CLAUDE.md](./CLAUDE.md) is met

### Factor coverage

The 13 paired factors give the strategist a directional expression on both
sides of each theme. Several equity factors (Nasdaq / S&P / tech / semis) are
correlated risk-on beta — the constructor de-duplicates by `factor` so it
doesn't load the same bet twice under different tickers. Rates (TMF/TMV) and
gold (NUGT/DUST) are the main diversifiers against equity beta; UVXY is the
long-vol tail hedge. Excluded as redundant/illiquid: URTY/SRTY (TNA/TZA cover
small-caps), DPST, CURE, BITU/SBIT/ETHU, DIA.

---

## How the agents work

Each cycle runs as a sequence of **schema-validated stages**. Four are LLM calls (strategist, construct, critic, and the meta-scheduler that picks the next-run window); the rest are deterministic Python. The LLM agents read role-specific system prompts under [`prompts/`](./prompts/). Anthropic prompt caching keeps the static system block cheap across calls.

### 0. Market gate — Alpaca clock, $0
Queries `/v2/clock`. If markets are closed (weekend, holiday, after-hours), writes `market_gate.json` + a closed-market `next_run.json` pointing at the broker-reported next open, and exits. No LLM calls billed on a closed-market cycle.

### 1. Signals — deterministic Python, $0
For each of the 29 universe tickers, computes from yfinance daily history:
- `last_close`, `adv_30d` (liquidity)
- `momentum_30d_pct` / `momentum_60d_pct` (trailing returns)
- `hv_30d_annualised` / `hv_90d_annualised` (close-to-close vol)
- `dist_from_50d_ma_pct` / `dist_from_200d_ma_pct` (trend position)
- `upcoming_macro_events_7d` (per-ticker FOMC/CPI/NFP/PCE events within 7 days)

Replaces the v1 screener + bull/bear research + scenarios chain entirely. Output: `signals.json`.

### 2. Strategist — Sonnet 4.6, ~$0.05
One LLM call. Reads `signals.json`, emits a **regime classification** (one of `risk_on`, `risk_off`, `neutral`, `vol_elevated`, `trending_up`, `trending_down`, `choppy`) + up to **6 candidate ideas** with `instrument_kind` (always `etf`), `thesis` (signal-citing), and `confidence` ∈ [0, 1].

Bullish theses name the bull ETF; bearish theses name the inverse ETF (SQQQ, SPXU, etc.). The system never goes broker-short and never trades options.

Output: `view.json`.

### 3. Portfolio construction — Opus 4.7, ~$0.20
Reads `signals.json` + `view.json`. Picks 1–12 positions from the strategist's candidate list — or all-cash if the strategist returned zero candidates and the regime is genuinely uninvestable.

Builds the portfolio with sizing math:
- Integer shares from `position_pct × NAV / share_price`, floored
- Refuses a position where even 1 share exceeds the per-position cap
- Hard 15% per-position NAV cap, sum of `position_pct` ≤ 100 (residual = cash buffer)
- Explicit kill conditions per leg (max_loss_pct = 25 + at least one of ETF price / time stop)

Bias: take a position if the strategist surfaces a candidate with confidence ≥ 0.6. **Abstaining cycle after cycle is not the goal** — the v1 system collapsed to that pattern and the v2 prompt explicitly counters it. The constructor receives:
- Signals + view (from the upstream stages)
- Current broker positions (state awareness — bias to hold winners vs churn)
- Recent PnL history (last 5 cycles' regime + realized 4h PnL — self-correct drift)
- Adaptive per-position cap (lower in drawdown via `risk.adaptive_position_cap_pct`)

Output: `portfolio.json`.

### 3.5 Critic — Sonnet 4.6 (low effort), ~$0.03
Adversarial second-pair-of-eyes pass. Reads `view.json` + `portfolio.json`, returns either `{accept: true}` or `{accept: false, critique, suggested_changes}`. On reject, the orchestrator re-runs the constructor ONCE with the critique fed back. Worst case: ~$0.30 extra (one critic + one retry construct); typical case: ~$0.03 (accept on first pass). Output: `critique.json`.

### 4. Sanity — deterministic Python, $0
Post-construct rules (see [`lib/sanity.py`](./lib/sanity.py)). Each rule has a fixed severity (`warn` or `fail`); the overall sanity status is the worst per-rule status. Non-blocking by default — `SANITY_BLOCK_ON_FAIL=true` escalates `fail` into a hard skip of `stage_execute`.

Rules (9 total):
- `construction_rationale_meaningful` — ≥ 80 chars
- `kill_conditions_complete` — max_loss_pct ∈ (0,100] + at least one price/time stop
- `position_backed_by_strategist` — every position's symbol is endorsed at confidence ≥ 0.5 in `view.json`
- `position_within_adaptive_cap` — position_pct ≤ drawdown-adaptive cap (15% at-peak, 7.5% at ≥10% drawdown, linear between)
- `per_underlying_pct_cap_20` — Σ position_pct per ticker ≤ 20%
- `position_size_matches_confidence` — position_pct ≤ strategist confidence × 15
- `position_notional_above_floor` — position notional ≥ $50 (spread + fees dominate below this on a $2,500 account)
- `position_adv_liquidity` — position notional ≤ 1% of ticker's 30-day dollar ADV
- `reentry_cooldown` — a symbol fully exited within the last 7 days isn't re-entered unless the strategist confidence clears the override threshold (≥ 0.8); pairs with the 30%-gain harvest rule to stop churn-and-re-buy

Output: `sanity.json`.

### 5. Execution + meta-scheduling
- **execute**: diffs target portfolio vs current Alpaca positions, emits **close orders first** (free cash) then **open orders**. Integer-share market orders for ETFs only; any option-shaped payload is defensively rejected before it can reach the broker. Gated behind `ORDERS_ENABLED=true`. Order safety invariant: orders never cross zero in a single ticket — a flip from long to short (or vice versa) is split into a close + an open.
- **meta** — Sonnet 4.6: given the freshly-built portfolio + recent NAV trend + time of day + strategist regime, decides when the **next cycle fires** (bounded 1–24h, falls back to a 4h/6h heuristic if the LLM output is unusable). Writes `state/next_run.json`.

### Risk controls (always-on)

- **8% daily-drawdown circuit breaker**: NAV down ≥8% in a UTC day → next orchestrator run halts new orders; `monitor.py` still flattens. Resets at 00:00 UTC.
- **`monitor.py`** (every 15 min during US market hours): re-evaluates kill conditions on every open position. Flattens via the broker. **Cannot open new positions** — it's a stop-loss daemon, not a trader.
- **Halt flag (`state/halt.flag`)**: presence stops both orchestrator and monitor *before any API call*. Toggled from the dashboard, or `sudo -u agent touch /opt/agent-trading/state/halt.flag`.
- **Cost caps**: per-run **$3**, daily **$12**. Cleanly aborts between stages if hit.
- **Market gate** (v2 stage 0): the orchestrator queries Alpaca's clock at the start of every cycle. Closed market → no LLM calls billed, no orders submitted, exits cleanly with a `next_run_at` pointing at the broker-reported next open.
- **Post-construct sanity rules** (`lib/sanity.py`, runs after every cycle, zero LLM cost). **Non-blocking by default** — set `SANITY_BLOCK_ON_FAIL=true` to hard-skip `stage_execute` on `fail` status.
- **Order safety invariant** (`lib/orders._plan_for_symbol`): orders never cross zero in a single ticket. Going from long to short on a symbol is split into close + open. For v2's long-only schema this never triggers, but the invariant is defensive against future short-enabling changes.

### Strategy in one paragraph

On a $2,500 paper account in a leveraged/inverse-ETF universe, **the edge isn't sector picking — it's discipline**: positive-EV only, hard 15% per-position NAV cap, 25% loss kill condition, no options, no broker shorts (bearish views go via inverse ETFs).

The v2 pipeline reads deterministic signals (momentum, vol, MA distance) for 29 curated tickers and feeds them to a Sonnet strategist that picks ≤6 high-conviction ideas; an Opus constructor then sizes 1–12 positions. Cycles run every 4 hours during market hours; the cadence skips weekends/holidays automatically.

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
- Installs five systemd units (orchestrator + monitor service+timer, dashboard service).
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
| `MODEL_*`                    | Override the per-stage model IDs if needed (defaults are Claude 4.X). |
| `VIRTUAL_NAV_USD`            | Default `2500`. Sizing baseline (this value + realized P&L), used instead of Alpaca paper's $100k equity. If unset/malformed it falls back to a hard-coded `2500` — it does **not** read broker equity (Phase 3 removed that path so a missing var can't size ~40× too large). |
| `PER_RUN_COST_CAP_USD`       | Default `3.00`. Per-run hard cap.                                     |
| `DAILY_COST_CAP_USD`         | Default `12.00`. Daily hard cap (resets at 00:00 UTC).                |
| `ORDERS_ENABLED`             | Default `false`. When `false`, the pipeline writes `portfolio.json` but never touches the broker. Flip to `true` once decisions look right. |
| `SANITY_BLOCK_ON_FAIL`       | Default `false`. When `true`, any sanity rule with status `fail` hard-skips `stage_execute`. |
| `LIVE_TRADING_ENABLED`       | **Leave `false`.** See [Promotion to live](./CLAUDE.md#promotion-to-live-documented-only--do-not-enable-in-code). |

### 4. Manual smoke (do this BEFORE enabling timers)

```bash
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py --dry-run
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py
sudo -u agent tail -n 5 /opt/agent-trading/state/decisions.jsonl
sudo -u agent tail -n 5 /opt/agent-trading/state/costs.jsonl
```

A live run typically costs ~$0.03–$0.50 depending on candidate count, well under the $3 per-run cap.

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

### 6. Enable autonomous timers

Once the smoke run looks clean and the dashboard renders:

```bash
systemctl enable --now agent-orchestrator.timer agent-monitor.timer
systemctl list-timers --all 'agent-*'
journalctl -u agent-orchestrator.service -f
```

The orchestrator timer has a daily 13:30 UTC fallback and self-reschedules from `state/next_run.json` after each run. The monitor fires every 15 minutes during US market hours.

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
systemctl disable --now agent-orchestrator.timer agent-monitor.timer agent-dashboard.service
rm /etc/systemd/system/agent-{orchestrator,monitor,dashboard}.{service,timer}
systemctl daemon-reload
rm /etc/logrotate.d/agent-trading
# Keep /opt/agent-trading/state — that's your run history.
```

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
| `state/next_run.json`            | Orchestrator-chosen next-run timestamp                                |
| `state/halt.flag`                | Presence = stop                                                       |

Quick inspection:

```bash
sudo -u agent tail -n 20 /opt/agent-trading/state/decisions.jsonl
sudo -u agent tail -n 20 /opt/agent-trading/state/costs.jsonl
journalctl -u agent-orchestrator.service -n 200 --no-pager
journalctl -u agent-dashboard.service    -n 200 --no-pager
```

The dashboard's Performance and Settings tabs surface the same numbers without the JSON wrangling.

---

## Repo hygiene

- `.env` is gitignored. **Never** commit secrets.
- `state/` is gitignored — runtime artifacts only.
- All schemas under `schemas/` are validated on every write. Schema-failed agent outputs retry once with the validation error fed back; second failure aborts the run.
- Conventional commits. Open a PR against `main`; do not push to `main` directly.
- `lib/broker.py` is the only abstraction layer over the broker — swapping to IBKR is a one-file change behind that interface.

---

## Promotion to live (do **not** attempt)

Live trading is gated by a triple lock and is intentionally not buildable from this checkout:

1. `LIVE_TRADING_ENABLED=true` env var — disabled by default.
2. `LIVE_VERSION = 0` constant in `lib/live_gate.py` — must be bumped in code. The shared `assert_live_gate()` guard (called by both `orchestrator.py` and `monitor.py`) refuses to run if the env var is set while the version is still 0.
3. `lib/alpaca_client.py` refuses to construct against a non-paper base URL unless both gates are satisfied.

The full set of pre-conditions (≥ 4 weeks paper, Sharpe ≥ 0.5, max DD ≤ 25%, IBKR client implemented for UK suitability) is documented in [CLAUDE.md §Promotion to live](./CLAUDE.md#promotion-to-live-documented-only--do-not-enable-in-code).

---

## Status

Active development. The full pipeline runs end-to-end on paper data; the dashboard renders against fixtures or live state. See [CLAUDE.md](./CLAUDE.md) for the full spec, [`deploy/README.md`](./deploy/README.md) for the Linux operator playbook, and the `state/` directory (when populated) for current behaviour.
