# Agent-Trading

> **PAPER TRADING — Experimental autonomous AI agent. Leveraged ETFs and listed options on a small account are high-risk. Not financial advice.**

Autonomous multi-agent trading system that screens leveraged ETFs and listed options, runs adversarial bull/bear research, builds scenario-weighted views, constructs a **1–12 position portfolio (or all-cash)**, and executes via **Alpaca paper**. Runs unattended on a Linux VPS (or on Windows under Task Scheduler) with a Streamlit dashboard.

The canonical build spec is [CLAUDE.md](./CLAUDE.md). This README is the operator's manual.

---

## Mandatory risk warning

> Leveraged ETFs decay path-dependently in volatile markets and are not buy-and-hold instruments. Long options can expire worthless; theta works against long premium daily. A $2,500 account cannot diversify options positions meaningfully — concentration risk is structural, not a flaw to fix. This system is an experiment in autonomous AI trading agents, not a path to reliable returns. Expect losses. Do not deploy capital you cannot afford to lose entirely. None of this is financial advice.

---

## Architecture at a glance

```
systemd timer (Linux) / Task Scheduler (Windows) ──▶ orchestrator.py
                                                      ├─ Stage 1: screen      (Haiku 4.5)
                                                      ├─ Stage 2: research    (Sonnet 4.6, bull+bear in parallel)
                                                      ├─ Stage 3: scenarios   (Sonnet 4.6)
                                                      ├─ Stage 4: construct   (Sonnet 4.6, 1–12 or all-cash)
                                                      └─ Stage 5: execute     (Alpaca paper) + write next_run.json
                                                            │
                                                            ▼
                                                   state/ (JSON, append-only)
                                                            │
                                                            ▼
                                                   dashboard.py (Streamlit, dark mode)
```

`monitor.py` runs more frequently and only checks kill conditions; it can flatten a position but cannot open new ones.

### Model tiering rationale

Per-stage model assignment matches the cost/quality demand of each decision. All defaults overridable via `.env` (`MODEL_*` vars).

| Stage | Model | Why |
|---|---|---|
| **screen** | `claude-haiku-4-5` | Structured liquidity-filter step. Cheap, fast, sufficient. |
| **research** (bull + bear, parallel) | `claude-sonnet-4-6` | Fans out 2× per candidate (~16 calls/run). Sonnet is the right cost/quality sweet spot at this fan-out. |
| **scenarios** | `claude-sonnet-4-6` | Probability-weighted base/bull/bear case modelling. Well-defined output shape. |
| **construct** | `claude-sonnet-4-6` | **The actual trade decision** — picks positions, sizing, kill conditions. Output is heavily constrained by `portfolio.schema.json`, so the structured-output server-side validator already does much of the work the Opus tier used to. Sonnet 4.6 here ≈ 40% cheaper per run vs. Opus 4.6 with no observed quality regression in paper. Override with `MODEL_CONSTRUCTOR=claude-opus-4-6` if needed. |
| **orchestrator-meta** *(timing only)* | `claude-sonnet-4-6` | Decides next-run window from regime + portfolio state. Bounded 1–24h, falls back to a 4h/6h heuristic if the LLM output is unusable. Small payload, no schema — Sonnet is right-sized; downgraded from Opus on 2026-05-13. |

Typical cost per orchestrator run on a real universe: **~$0.10–$0.50** (most of it in `construct`). Per-run cap defaults to $2; daily cap defaults to $10.

---

## What we scan & trade

**Universe: 33 instruments** across 13 factor groups. Curated, not exhaustive — entries that fail runtime liquidity filters (ADV, spread, options OI) are rejected at screen time even though they're listed here.

### Leveraged ETFs (28)

Direct positions in 2x / 3x daily-rebalancing ETFs. Subject to per-position **15% NAV cap** and **25% loss kill condition**.

| Factor | Bull | Bear |
|---|---|---|
| Nasdaq-100 (3x) | TQQQ | SQQQ |
| S&P 500 (3x) | UPRO | SPXU |
| Russell 2000 (3x) | TNA, URTY | TZA, SRTY |
| Semiconductors (3x) | SOXL | SOXS |
| Financials, broad (3x) | FAS | FAZ |
| Financials, regional banks (3x) | DPST | — |
| Biotech (3x) | LABU | LABD |
| Healthcare (3x) | CURE | — |
| China large-cap (3x) | YINN | YANG |
| Energy (2x) | ERX | ERY |
| Gold miners (2x) | NUGT | DUST |
| VIX front-month (1.5x) | UVXY | — |
| Natural Gas (2x) | BOIL | — |
| Bitcoin futures (2x) | BITX, BITU | SBIT |
| Ether futures (2x) | ETHU | — |

### Option underlyings (5)

The agent doesn't hold these as positions — only **listed calls / puts on them**. Long options only (no writes, no spreads). Subject to **100% premium kill condition**.

| Symbol | Why |
|---|---|
| SPY | Most liquid options chain in the world; broad-market exposure |
| QQQ | Tech-heavy options chain, very liquid |
| IWM | Small-cap options chain |
| DIA | Dow Jones — large-cap value tilt, different factor from SPY/QQQ |
| TLT | 20+ Year Treasury — rates exposure, anti-correlated to equity-long |

### What we explicitly don't trade

- **Spot single-name equities** (no AAPL, NVDA, etc.) — too much idiosyncratic risk on a $2,500 account
- **Unleveraged broad-market ETFs as positions** — SPY/QQQ/IWM/DIA/TLT are only entered via their options
- **Direct spot crypto** (BTC/USD on Alpaca) — exposure is via the leveraged-ETF wrappers above instead, fits the existing schema
- **Multi-leg option combos** — single-leg only for now (no spreads, condors, straddles)
- **Live trading** — gate hard-disabled until §"Promotion to live" in [CLAUDE.md](./CLAUDE.md) is met

---

## How the agents work

Each cycle, five agents run in sequence with **schema-validated JSON outputs**. Each agent has a role-specific system prompt under [`prompts/`](./prompts/). Anthropic prompt caching keeps the static system block + universe block cheap across calls.

### 1. Screener — Haiku 4.5
Receives the full 33-symbol universe with live **ADV**, **30-day historical volatility**, **last close** (fetched via `yfinance`). Applies liquidity filters and emits a `passed` candidate list. Cheap and fast — Haiku is right-sized for structured filtering. Output: `screen.json`.

### 2. Adversarial research — Sonnet 4.6, parallel bull + bear
For each of the **top 8 screened candidates**, two parallel LLM calls — one bull, one bear. **Bear must steel-man the bull case** before disagreeing (and vice versa); both sides are required by schema to list counterarguments. Each side returns a thesis, key drivers, counterarguments, and a confidence ∈ [0, 1]. Output: `research.json` with one row per candidate carrying `confidence_delta = bull.confidence − bear.confidence` and an `abstain` flag for genuinely-unclear cases.

### 3. Scenario modelling — Sonnet 4.6
For **every** researched candidate (including likely-negative-EV ones — *the constructor decides what to trade*), produces probability-weighted **base / bull / bear** cases:
- 3 probabilities summing to 1.0 (±0.01)
- Expected return per case (signed; bear typically negative)
- Horizon in days (agent's choice — no hard-coded calendar rules)
- `expected_value_pct` = probability-weighted return across cases
- For options: explicit DTE rationale + strike rationale

This is a **data-producing** stage, not a gating one. Output: `scenarios.json`.

### 4. Portfolio construction — Sonnet 4.6 (the actual trade decision)
Reads all scenarios, filters by:
- Positive `expected_value_pct` (drops negative-EV candidates here)
- Correlation across surviving positions (bull/bear pairs of the same factor count as one)
- Stomach for the bear case

Builds a **1–12 position portfolio** with sizing math:
- ETFs: integer shares from `position_pct × NAV / share_price`, floored
- Options: integer contracts at `position_pct × NAV / (premium × 100)`, floored
- Hard 15% per-position NAV cap, sum of `position_pct` ≤ 100 (residual = cash buffer)
- Explicit kill conditions per leg

If every candidate is negative-EV, outputs **all-cash** with rationale. **A single strong positive-EV thesis is acceptable** — no artificial minimum-diversification rule; the 15% per-position cap is the concentration guard. Output: `portfolio.json`.

### 5. Execution + meta-scheduling
- **execute**: diffs target portfolio vs current Alpaca positions, emits **close orders first** (free cash) then **open orders**. Single-leg market orders for ETFs and options (option symbols built via OSI: `SPY260619C00530000` etc.). Gated behind `ORDERS_ENABLED=true` — default off so dry-runs are safe.
- **meta** — Sonnet 4.6: given the freshly-built portfolio + recent NAV trend + time of day, decides when the **next cycle fires** (bounded 1–24h, falls back to a 4h/6h heuristic if the LLM output is unusable). Writes `state/next_run.json` for the systemd timer to pick up.

### Risk controls (always-on)

- **8% daily-drawdown circuit breaker**: NAV down ≥8% in a UTC day → next orchestrator run halts new orders; `monitor.py` still flattens. Resets at 00:00 UTC.
- **`monitor.py`** (every 15 min during US market hours): re-evaluates kill conditions on every open position. Flattens via the broker. **Cannot open new positions** — it's a stop-loss daemon, not a trader.
- **Halt flag (`state/halt.flag`)**: presence stops both orchestrator and monitor *before any API call*. Toggled from the dashboard, or `sudo -u agent touch /opt/agent-trading/state/halt.flag`.
- **Cost caps**: per-run **$2**, daily **$10**. Cleanly aborts between stages if hit.

### Strategy in one paragraph

On a $2,500 paper account in a leveraged-ETF + listed-options universe, **the edge isn't sector picking — it's discipline**: positive-EV only, hard 15% per-position NAV cap, 25% kill for ETFs / 100% premium kill for long options, single-leg trades only, no multi-leg combos. Every idea passes through adversarial bull/bear research before scenarios; every position is sized by an Opus model that's been told *capital preservation matters, but so does deploying capital when the edge is real*. The agent picks cadence dynamically (1–24h between runs), tightening near catalysts and loosening when all-cash. **Losses are expected** — the experiment is whether the agent's prompt + schema discipline produces an honest Sharpe across many cycles, not whether it picks individual winners.

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
| `PER_RUN_COST_CAP_USD`       | Default `2.00`. Per-run hard cap.                                     |
| `DAILY_COST_CAP_USD`         | Default `10.00`. Daily hard cap (resets at 00:00 UTC).                |
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
| `state/runs/{run_id}/`           | Per-run JSON: `screen.json`, `research.json`, `scenarios.json`, `portfolio.json`, `next_run.json` |
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
