# Agent-Trading

> **PAPER TRADING — Experimental autonomous AI agent. Leveraged ETFs and listed options on a small account are high-risk. Not financial advice.**

Autonomous multi-agent trading system that runs a deterministic-signals scan over 15 leveraged ETFs + option underlyings, asks a strategist agent for a regime call + ranked candidate ideas, and asks a constructor agent to build a **1–12 position portfolio (or all-cash)** that's then executed via **Alpaca paper**. Runs unattended on a Linux VPS (or on Windows under Task Scheduler) with a Streamlit dashboard.

The canonical build spec is [CLAUDE.md](./CLAUDE.md). This README is the operator's manual.

---

## Mandatory risk warning

> Leveraged ETFs decay path-dependently in volatile markets and are not buy-and-hold instruments. Long options can expire worthless; theta works against long premium daily. A $2,500 account cannot diversify options positions meaningfully — concentration risk is structural, not a flaw to fix. This system is an experiment in autonomous AI trading agents, not a path to reliable returns. Expect losses. Do not deploy capital you cannot afford to lose entirely. None of this is financial advice.

---

## Architecture at a glance

```
systemd timer (Linux) / Task Scheduler (Windows) ──▶ orchestrator.py
                                                      ├─ Stage 0: market_gate   (Alpaca clock, $0)
                                                      ├─ Stage 1: signals       (deterministic Python, $0)
                                                      ├─       state context     (broker positions + PnL history)
                                                      ├─ Stage 2: strategist    (Sonnet 4.6, ~$0.05)
                                                      ├─ Stage 2.5: chain_lookup (Alpaca options, $0)
                                                      ├─ Stage 3: construct     (Opus 4.7, ~$0.20, 1–12 or all-cash)
                                                      ├─ Stage 3.5: critic      (Sonnet 4.6 low, ~$0.03, +1 retry on reject)
                                                      ├─ Stage 4: sanity        (deterministic Python, $0, 10 rules)
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

v2 collapses screen + bull/bear + scenarios into a single **deterministic signals stage** (zero LLM cost, just yfinance features per ticker) plus a **single strategist LLM call** that produces a regime + ranked candidate list. The construct stage still owns position selection on Opus 4.7 because that IS the soft-judgement call. The v2-winrate add-on (PR after #67) layered a critic agent, chain-lookups, PnL feedback, cycle dedup, and a drawdown-adaptive cap on top.

- LLM calls per cycle: **20 → 2–3** (~85% reduction, +1 for the critic on rejection retry)
- Typical per-cycle cost: **~$0.28** (was ~$1.50–2.50; critic adds ~$0.03)
- At 2 cycles/weekday: **~$12/month** (was ~$300/month)

The market_gate stage is the cheapest reliability win: skip the entire pipeline cleanly when markets are closed instead of producing a portfolio that can't trade. Cycle-dedup further skips strategist + construct + execute when the signals fingerprint and broker positions are both unchanged from the prior cycle.

### Model tiering rationale

Per-stage model assignment matches the cost/quality demand of each decision. All defaults overridable via `.env` (`MODEL_*` vars).

| Stage | Model | Why |
|---|---|---|
| **market_gate** | n/a (deterministic) | Alpaca clock query — free, zero LLM call. |
| **signals** | n/a (deterministic) | yfinance + universe metadata. 15 ticker rows out, zero LLM cost. Replaces the v1 screener + bull/bear + scenarios chain. |
| **strategist** | `claude-sonnet-4-6` (effort `medium`) | Reads the signals table → emits a regime call + 0–6 candidate ideas with thesis + confidence. Sonnet + medium effort caps thinking-budget allocation (PR ε lesson on the v1 scenarios stage). |
| **construct** | `claude-opus-4-7` (effort `high`) | **The actual trade decision** — picks positions, sizing, kill conditions. On a $2,500 leveraged-ETF + options account, position-selection quality has direct PnL impact: multi-position correlation reasoning + sizing math under a 15%/position cap + per-row kill-condition tailoring. Override with `MODEL_CONSTRUCTOR=claude-sonnet-4-6` if cost dominates. |
| **orchestrator-meta** *(timing only)* | `claude-sonnet-4-6` | Decides next-run window from regime + portfolio state. Bounded 1–24h, falls back to a 4h/6h heuristic if the LLM output is unusable. Small payload, no schema — Sonnet is right-sized. |

Per-run cap defaults to $3; daily cap defaults to $12.

---

## What we scan & trade

**Universe: 18 instruments** (trimmed from 33 in v2, expanded with gold on 2026-05-13) across 10 factor groups. Curated; entries that fail liquidity filters at signals time still appear in the table with their numeric features, but the strategist learns to ignore low-ADV rows.

### Leveraged ETFs (14)

Direct positions in 1.5x / 2x / 3x daily-rebalancing ETFs. Subject to per-position **15% NAV cap** and **25% loss kill condition**.

| Factor | Bull | Bear |
|---|---|---|
| Nasdaq-100 (3x) | TQQQ | SQQQ |
| S&P 500 (3x) | UPRO | SPXU |
| Russell 2000 (3x) | TNA | TZA |
| Semiconductors (3x) | SOXL | SOXS |
| Financials, broad (3x) | FAS | FAZ |
| Gold miners (2x) | NUGT | DUST |
| VIX front-month (1.5x) | UVXY | — |
| Bitcoin futures (2x) | BITX | — |

### Option underlyings (4)

The agent doesn't hold these as positions — only **listed calls / puts on them**. Long options only (no writes, no spreads). Subject to **100% premium kill condition**.

| Symbol | Why |
|---|---|
| SPY | Most liquid options chain in the world; broad-market exposure |
| QQQ | Tech-heavy options chain, very liquid |
| TLT | 20+ Year Treasury — rates exposure, anti-correlated to equity-long (no leveraged bond ETF in v2) |
| GLD | SPDR Gold Shares — spot-gold tracker, distinct from NUGT/DUST (which carry equity beta + operational leverage on top of gold) |

### What we explicitly don't trade

- **Spot single-name equities** (no AAPL, NVDA, etc.) — too much idiosyncratic risk on a $2,500 account
- **Unleveraged broad-market ETFs as positions** — SPY/QQQ/TLT are only entered via their options
- **Actual short-selling** — bearish theses are expressed as long bear ETFs (SQQQ, SPXU, etc.) or long puts. No margin account.
- **Multi-leg option combos** — single-leg only (no spreads, condors, straddles as a single ticket)
- **Live trading** — gate hard-disabled until §"Promotion to live" in [CLAUDE.md](./CLAUDE.md) is met

### Why the v2 trim

The v1 33-ticker universe produced the same TLT-straddle outcome cycle after cycle. Too many candidates contributed to constructor decision fatigue and convergence to a "hedge with vol" default. The v2 trim drops:

- Russell 2000 alts (URTY/SRTY) — TNA/TZA already cover the factor
- Regional banks (DPST), biotech (LABU/LABD), healthcare (CURE), China (YINN/YANG), energy (ERX/ERY), gold miners (NUGT/DUST), nat gas (BOIL), ether (ETHU), bitcoin alts (BITU/SBIT) — lower ADV; factor-redundant or speculative
- IWM, DIA as option underlyings — Russell 2000 is covered via TNA/TZA already; DIA correlates ~99% with SPY

---

## How the agents work

Each cycle runs as a sequence of **schema-validated stages**. Three are LLM calls (strategist, construct, critic); the rest are deterministic Python. The LLM agents read role-specific system prompts under [`prompts/`](./prompts/). Anthropic prompt caching keeps the static system block cheap across calls.

### 0. Market gate — Alpaca clock, $0
Queries `/v2/clock`. If markets are closed (weekend, holiday, after-hours), writes `market_gate.json` + a closed-market `next_run.json` pointing at the broker-reported next open, and exits. No LLM calls billed on a closed-market cycle.

### 1. Signals — deterministic Python, $0
For each of the 18 universe tickers, computes from yfinance daily history:
- `last_close`, `adv_30d` (liquidity)
- `momentum_30d_pct` / `momentum_60d_pct` (trailing returns)
- `hv_30d_annualised` / `hv_90d_annualised` (close-to-close vol)
- `dist_from_50d_ma_pct` / `dist_from_200d_ma_pct` (trend position)
- `is_optionable` (true for SPY/QQQ/TLT)

Replaces the v1 screener + bull/bear research + scenarios chain entirely. Output: `signals.json`.

### 2. Strategist — Sonnet 4.6, ~$0.05
One LLM call. Reads `signals.json`, emits a **regime classification** (one of `risk_on`, `risk_off`, `neutral`, `vol_elevated`, `trending_up`, `trending_down`, `choppy`) + up to **6 candidate ideas** with `instrument_kind` (`etf` / `option_call` / `option_put`), `thesis` (signal-citing), and `confidence` ∈ [0, 1].

Bear theses are expressed as long bear ETFs (SQQQ, SPXU, etc.) or long puts. The system never goes broker-short.

Output: `view.json`.

### 3. Portfolio construction — Opus 4.7, ~$0.20
Reads `signals.json` + `view.json`. Picks 1–12 positions from the strategist's candidate list — or all-cash if the strategist returned zero candidates and the regime is genuinely uninvestable.

Builds the portfolio with sizing math:
- ETFs: integer shares from `position_pct × NAV / share_price`, floored
- Options: integer contracts from `position_pct × NAV / (premium × 100)`, floored
- Strike: nearest available OTM at 30–45 DTE
- Hard 15% per-position NAV cap, sum of `position_pct` ≤ 100 (residual = cash buffer)
- Explicit kill conditions per leg (max_loss_pct + at least one of price/time stop)

Bias: take a position if the strategist surfaces a candidate with confidence ≥ 0.6. **Abstaining cycle after cycle is not the goal** — the v1 system collapsed to that pattern and the v2 prompt explicitly counters it. The constructor receives:
- Signals + view (from the upstream stages)
- `chain_lookups.json` — real Alpaca OSI symbols + DTE for each option candidate (avoids inventing untradable strikes)
- Current broker positions (state awareness — bias to hold winners vs churn)
- Recent PnL history (last 5 cycles' regime + realized 4h PnL — self-correct drift)
- Adaptive per-position cap (lower in drawdown via `risk.adaptive_position_cap_pct`)

Output: `portfolio.json`.

### 3.5 Critic — Sonnet 4.6 (low effort), ~$0.03
Adversarial second-pair-of-eyes pass. Reads `view.json` + `portfolio.json`, returns either `{accept: true}` or `{accept: false, critique, suggested_changes}`. On reject, the orchestrator re-runs the constructor ONCE with the critique fed back. Worst case: ~$0.30 extra (one critic + one retry construct); typical case: ~$0.03 (accept on first pass). Output: `critique.json`.

### 4. Sanity — deterministic Python, $0
Post-construct rules (see [`lib/sanity.py`](./lib/sanity.py)). Each rule has a fixed severity (`warn` or `fail`); the overall sanity status is the worst per-rule status. Non-blocking by default — `SANITY_BLOCK_ON_FAIL=true` escalates `fail` into a hard skip of `stage_execute`.

Rules (10 total):
- `construction_rationale_meaningful` — ≥ 80 chars
- `kill_conditions_complete` — max_loss_pct ∈ (0,100] + at least one price/time stop
- `position_backed_by_strategist` — every position has confidence ≥ 0.5 + matching instrument_kind in `view.json`
- `position_within_adaptive_cap` — position_pct ≤ drawdown-adaptive cap (15% at-peak, 7.5% at ≥10% drawdown, linear between)
- `straddle_requires_low_iv` — call+put on same underlying must have iv_percentile ≤ 40 on every leg
- `per_underlying_pct_cap_20` — Σ position_pct per underlying ≤ 20%
- `position_size_matches_confidence` — position_pct ≤ strategist confidence × 15
- `option_premium_above_floor` — premium ≥ $0.05
- `position_notional_above_floor` — position notional ≥ $50 (spread + fees dominate below this on a $2,500 account)
- `position_adv_liquidity` — position notional ≤ 1% of underlying's 30-day dollar ADV

Output: `sanity.json`.

### 5. Execution + meta-scheduling
- **execute**: diffs target portfolio vs current Alpaca positions, emits **close orders first** (free cash) then **open orders**. Single-leg market orders for ETFs and options (option symbols built via OSI: `SPY260619C00530000`). Gated behind `ORDERS_ENABLED=true`. Order safety invariant: orders never cross zero in a single ticket — a flip from long to short (or vice versa) is split into a close + an open.
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

On a $2,500 paper account in a leveraged-ETF + listed-options universe, **the edge isn't sector picking — it's discipline**: positive-EV only, hard 15% per-position NAV cap, 25% kill for ETFs / 100% premium kill for long options, single-leg trades only, no multi-leg combos, no broker shorts (bearish views go via bear ETFs or long puts).

The v2 pipeline reads deterministic signals (momentum, vol, MA distance) for 15 curated tickers and feeds them to a Sonnet strategist that picks ≤6 high-conviction ideas; an Opus constructor then sizes 1–12 positions. Cycles run every 4 hours during market hours; the cadence skips weekends/holidays automatically.

**Losses are expected** — the experiment is whether prompt + schema discipline produces an honest Sharpe across many cycles, not whether it picks individual winners. Per-cycle LLM cost is ~$0.25, which keeps experimentation affordable.

---

## Pick your deployment

| | **Linux VPS** *(recommended)* | **Windows 10/11** |
|---|---|---|
| **When to use** | Unattended autonomous running. Box doesn't sleep. Best for the spec's "let the agent run for weeks" model. | Manual / interactive use on a desktop. Laptop must stay awake when the timer fires. |
| **Cost** | ~$5/mo (Hetzner CX23, 2 vCPU / 4 GB) | Already-owned hardware |
| **Scheduler** | systemd timers | Windows Task Scheduler |
| **Dashboard reach** | Tailscale Serve (HTTPS, no public ports) or SSH tunnel | localhost or LAN with Defender Firewall rule |
| **Setup section below** | [Linux VPS setup](#linux-vps-setup-recommended) | [Windows setup](#windows-setup-alternative) |

Both paths share the same `orchestrator.py` / `monitor.py` / `dashboard.py` — only the wrapper scripts and scheduling layer differ.

---

# Linux VPS setup (recommended)

The full operator playbook for Linux is [`deploy/README.md`](./deploy/README.md), including Tailscale phone access ([`deploy/tailscale.md`](./deploy/tailscale.md)). The summary below is enough to get from a fresh Hetzner Ubuntu 24.04 box to a running dashboard in ~10 minutes.

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
| `MODEL_*`                    | Override the per-stage model IDs if needed (defaults are Claude 4.X). |
| `PER_RUN_COST_CAP_USD`       | Default `3.00`. Per-run hard cap.                                     |
| `DAILY_COST_CAP_USD`         | Default `12.00`. Daily hard cap (resets at 00:00 UTC).                |
| `LIVE_TRADING_ENABLED`       | **Leave `false`.** See [Promotion to live](./CLAUDE.md#promotion-to-live-documented-only--do-not-enable-in-code). |

### 4. Manual smoke (do this BEFORE enabling timers)

```bash
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py --dry-run
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py
sudo -u agent tail -n 5 /opt/agent-trading/state/decisions.jsonl
sudo -u agent tail -n 5 /opt/agent-trading/state/costs.jsonl
```

A live run typically costs ~$0.03–$0.50 depending on candidate count, well under the $2 per-run cap.

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
```powershell
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

When new code lands on `main`:
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

# Windows setup (alternative)

For users who prefer to run on a personal Windows 10/11 PC. Note the laptop must stay on and awake when the orchestrator timer fires (or the run is missed) — see CLAUDE.md §Critical preconditions for the spec rationale.

### 1. Clone and enter the repo

```powershell
git clone <repo-url> Agent-Trading
cd Agent-Trading
```

### 2. Create + activate the virtualenv

Python 3.11+ is required. The `py` launcher comes with the official Python installer.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation fails with an execution-policy error:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

### 4. Configure secrets

```powershell
Copy-Item .env.example .env
notepad .env
```

(Same env-var table as Linux above.)

### 5. Verify the install

```powershell
pytest
python orchestrator.py --dry-run
```

### 6. Manual run

```powershell
# Dry-run — no LLM, no orders, exercises the full 5-stage pipeline against fixtures
python orchestrator.py --dry-run

# Live paper run — calls Anthropic + Alpaca paper
python orchestrator.py
```

### 7. Dashboard

```powershell
# Local only (recommended)
streamlit run dashboard.py

# Phone access on the same Wi-Fi (no auth — see firewall note below)
streamlit run dashboard.py --server.address 0.0.0.0
```

To pin a specific known-good portfolio for dashboard development, drop it at `state\seed_portfolio.json`:
```powershell
Copy-Item tests\fixtures\portfolio.json state\seed_portfolio.json
```

#### Phone access (Windows Defender Firewall)

`--server.address 0.0.0.0` binds Streamlit to every network interface but **adds no authentication**. Only enable on a trusted home network and add a firewall rule restricted to your local subnet:

```powershell
# Run from an elevated PowerShell session
New-NetFirewallRule `
    -DisplayName "Agent-Trading Streamlit (LAN only)" `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 8501 `
    -Profile Private `
    -RemoteAddress LocalSubnet
```

Remove later with `Remove-NetFirewallRule -DisplayName "Agent-Trading Streamlit (LAN only)"`.

> Do not enable `--server.address 0.0.0.0` on public Wi-Fi or expose port 8501 outside your LAN. The dashboard has no auth and can write `state/halt.flag`.

### 8. Windows Task Scheduler (orchestrator + monitor)

```powershell
.\scheduling\register_task.ps1
```

Registers two tasks under `\Agent-Trading\`:

| Task                              | What it runs                            | Cadence                                                   |
| --------------------------------- | --------------------------------------- | --------------------------------------------------------- |
| `\Agent-Trading\Orchestrator`     | `scheduling\run_orchestrator.ps1`       | Daily 13:30 UTC fallback + login trigger; **the orchestrator overwrites the next-run trigger after each run** based on `state\next_run.json`. |
| `\Agent-Trading\Monitor`          | `scheduling\run_monitor.ps1`            | Every 15 minutes for an 8-hour window starting 13:30 UTC. |

Useful inspections:

```powershell
Get-ScheduledTask     -TaskPath '\Agent-Trading\'
Get-ScheduledTaskInfo -TaskPath '\Agent-Trading\' -TaskName 'Orchestrator'
Start-ScheduledTask   -TaskPath '\Agent-Trading\' -TaskName 'Orchestrator'   # run on demand
```

Update / uninstall / dry-run:
```powershell
.\scheduling\register_task.ps1                    # update existing registration
.\scheduling\unregister_task.ps1                  # remove
.\scheduling\register_task.ps1 -SkipMonitor       # orchestrator task only
.\scheduling\register_task.ps1 -WhatIf            # show what would change
```

> **Heads-up on laptops**: with `WakeToRun=false` (the bundled default) plus `DisallowStartIfOnBatteries=true`, a closed-lid laptop on battery will miss runs. Either change the lid behaviour to "Do nothing" while plugged in, or move to the [Linux VPS path](#linux-vps-setup-recommended) for true autonomy.

---

## Halt procedure (both platforms)

The halt flag is checked before every LLM API call and before any order. Both wrappers (`run_orchestrator.{sh,ps1}`) short-circuit before activating the venv when it's present.

**Linux:**
```bash
sudo -u agent touch /opt/agent-trading/state/halt.flag    # halt
sudo -u agent rm    /opt/agent-trading/state/halt.flag    # resume
```

**Windows:**
```powershell
New-Item -Path state\halt.flag -ItemType File -Force      # halt
Remove-Item state\halt.flag                               # resume
```

Or click **Emergency stop** on the dashboard's Settings tab.

---

## Logs and state

| Location                         | Contents                                                             |
| -------------------------------- | -------------------------------------------------------------------- |
| `state/runs/{run_id}/`           | Per-run JSON: `market_gate.json`, `signals.json`, `view.json`, `portfolio.json`, `sanity.json`, `orders.json`, `next_run.json` |
| `state/decisions.jsonl`          | Append-only decision log (one row per stage, schema-validated)        |
| `state/costs.jsonl`              | Per-LLM-call cost ledger; daily and per-run caps enforced from this file |
| `state/current_portfolio.json`   | Latest known portfolio (consumed by `monitor.py` and the dashboard)   |
| `state/next_run.json`            | Orchestrator-chosen next-run timestamp                                |
| `state/halt.flag`                | Presence = stop                                                       |

Quick inspection:

**Linux:**
```bash
sudo -u agent tail -n 20 /opt/agent-trading/state/decisions.jsonl
sudo -u agent tail -n 20 /opt/agent-trading/state/costs.jsonl
journalctl -u agent-orchestrator.service -n 200 --no-pager
journalctl -u agent-dashboard.service    -n 200 --no-pager
```

**Windows:**
```powershell
Get-Content state\decisions.jsonl -Tail 20 | ForEach-Object { ConvertFrom-Json $_ }

# Cost today
$today = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
Get-Content state\costs.jsonl |
    ForEach-Object { ConvertFrom-Json $_ } |
    Where-Object { $_.at -like "$today*" } |
    Measure-Object cost_usd -Sum |
    Select-Object -ExpandProperty Sum
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
2. `LIVE_VERSION = 0` constant in `orchestrator.py` — must be bumped in code.
3. `lib/alpaca_client.py` refuses to construct against a non-paper base URL unless both gates are satisfied.

The full set of pre-conditions (≥ 4 weeks paper, Sharpe ≥ 0.5, max DD ≤ 25%, IBKR client implemented for UK suitability) is documented in [CLAUDE.md §Promotion to live](./CLAUDE.md#promotion-to-live-documented-only--do-not-enable-in-code).

---

## Status

Active development. The 5-stage pipeline runs end-to-end on paper data; the dashboard renders against fixtures or live state. See [CLAUDE.md](./CLAUDE.md) for the full spec, [`deploy/README.md`](./deploy/README.md) for the Linux operator playbook, and the `state/` directory (when populated) for current behaviour.
