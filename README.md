# Agent-Trading

> **PAPER TRADING — Experimental autonomous AI agent. Leveraged ETFs and listed options on a small account are high-risk. Not financial advice.**

Autonomous multi-agent trading system that screens leveraged ETFs and listed options, runs adversarial bull/bear research, builds scenario-weighted views, constructs an **8–12 position portfolio (or all-cash)**, and executes via **Alpaca paper**. Runs on Windows under Task Scheduler with a Streamlit dashboard.

The canonical build spec is [CLAUDE.md](./CLAUDE.md). This README is the operator's manual.

---

## Mandatory risk warning

> Leveraged ETFs decay path-dependently in volatile markets and are not buy-and-hold instruments. Long options can expire worthless; theta works against long premium daily. A £2k account cannot diversify options positions meaningfully — concentration risk is structural, not a flaw to fix. This system is an experiment in autonomous AI trading agents, not a path to reliable returns. Expect losses. Do not deploy capital you cannot afford to lose entirely. None of this is financial advice.

---

## Architecture at a glance

```
Windows Task Scheduler ──▶ orchestrator.py
                            ├─ Stage 1: screen      (Haiku 4.5)
                            ├─ Stage 2: research    (Sonnet 4.6, bull+bear in parallel)
                            ├─ Stage 3: scenarios   (Sonnet 4.6)
                            ├─ Stage 4: construct   (Sonnet 4.6, 8–12 or all-cash)
                            └─ Stage 5: execute     (Alpaca paper) + write next_run.json
                                  │
                                  ▼
                         state/ (JSON, append-only)
                                  │
                                  ▼
                         dashboard.py (Streamlit, dark mode)
```

`monitor.py` runs more frequently and only checks kill conditions; it can flatten a position but cannot open new ones.

---

## Setup (Windows 10/11, PowerShell)

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

### 5. Verify the install

```powershell
pytest
python orchestrator.py --dry-run
```

Both should exit 0. The dry-run writes a full set of stage artifacts under `state\runs\{run_id}\` without any LLM or order calls.

---

## Manual run

```powershell
# Dry-run — no LLM, no orders, exercises the full 5-stage pipeline against fixtures
python orchestrator.py --dry-run

# Live paper run — calls Anthropic + Alpaca paper
python orchestrator.py
```

The orchestrator picks its own next-run window and writes it to `state\next_run.json`.

---

## Dashboard

```powershell
# Local only (recommended)
streamlit run dashboard.py

# Phone access on the same Wi-Fi
streamlit run dashboard.py --server.address 0.0.0.0
```

The dashboard reads from `state/`. On a fresh checkout it falls back to the bundled fixture so every tab renders even before the first run.

### Phone access (Windows Defender Firewall)

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

To remove the rule later:

```powershell
Remove-NetFirewallRule -DisplayName "Agent-Trading Streamlit (LAN only)"
```

> Do not enable `--server.address 0.0.0.0` on public Wi-Fi or expose port 8501 outside your LAN. The dashboard has no auth and can write `state/halt.flag`.

---

## Windows Task Scheduler (orchestrator + monitor)

The orchestrator and monitor are designed to run unattended via Task Scheduler. Two scripts under `scheduling\` import (and remove) the bundled task definitions.

### Install

```powershell
.\scheduling\register_task.ps1
```

This validates that `.venv\Scripts\python.exe` exists, then registers two tasks under the `\Agent-Trading\` folder:

| Task                              | What it runs                            | Cadence                                                   |
| --------------------------------- | --------------------------------------- | --------------------------------------------------------- |
| `\Agent-Trading\Orchestrator`     | `scheduling\run_orchestrator.ps1`       | Daily 13:30 UTC fallback + login trigger; **the orchestrator overwrites the next-run trigger after each run** based on `state\next_run.json`. |
| `\Agent-Trading\Monitor`          | `scheduling\run_monitor.ps1`            | Every 15 minutes for an 8-hour window starting 13:30 UTC. |

Both run under your interactive token at LeastPrivilege (no admin required to run, only to register).

Useful inspections:

```powershell
Get-ScheduledTask     -TaskPath '\Agent-Trading\'
Get-ScheduledTaskInfo -TaskPath '\Agent-Trading\' -TaskName 'Orchestrator'
Start-ScheduledTask   -TaskPath '\Agent-Trading\' -TaskName 'Orchestrator'   # run on demand
```

### Update (re-import after pulling new task XML)

Re-run the same script — `Register-ScheduledTask -Force` updates the existing registration in place:

```powershell
.\scheduling\register_task.ps1
```

### Uninstall

```powershell
.\scheduling\unregister_task.ps1
```

### Selective install / dry-run

```powershell
.\scheduling\register_task.ps1 -SkipMonitor       # orchestrator task only
.\scheduling\register_task.ps1 -SkipOrchestrator  # monitor task only
.\scheduling\register_task.ps1 -WhatIf            # show what would change
```

---

## Halt procedure

To stop the orchestrator immediately:

```powershell
# Creates state\halt.flag — checked before any LLM API call and any order
New-Item -Path state\halt.flag -ItemType File -Force
```

Or click **Emergency stop** on the dashboard's Settings tab. Both the orchestrator and the monitor exit cleanly while the flag exists, and `run_orchestrator.ps1` / `run_monitor.ps1` short-circuit before activating the venv. Delete the file (or click "Clear halt flag" on the dashboard) to resume:

```powershell
Remove-Item state\halt.flag
```

---

## Logs and state

| Location                         | Contents                                                             |
| -------------------------------- | -------------------------------------------------------------------- |
| `state\runs\{run_id}\`           | Per-run JSON: `screen.json`, `research.json`, `scenarios.json`, `portfolio.json`, `next_run.json` |
| `state\decisions.jsonl`          | Append-only decision log (one row per stage, schema-validated)        |
| `state\costs.jsonl`              | Per-LLM-call cost ledger; daily and per-run caps enforced from this file |
| `state\current_portfolio.json`   | Latest known portfolio (consumed by `monitor.py` and the dashboard)   |
| `state\next_run.json`            | Orchestrator-chosen next-run timestamp; consumed by `run_orchestrator.ps1` |
| `state\halt.flag`                | Presence = stop                                                       |

Quick inspection:

```powershell
# Last 20 decisions
Get-Content state\decisions.jsonl -Tail 20 | ForEach-Object { ConvertFrom-Json $_ }

# Cost today
$today = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
Get-Content state\costs.jsonl |
    ForEach-Object { ConvertFrom-Json $_ } |
    Where-Object { $_.at -like "$today*" } |
    Measure-Object cost_usd -Sum |
    Select-Object -ExpandProperty Sum
```

---

## Repo hygiene

- `.env` is gitignored. **Never** commit secrets.
- `state/` is gitignored — runtime artifacts only.
- All schemas under `schemas/` are validated on every write. Schema-failed agent outputs retry once with the validation error fed back; second failure aborts the run.
- Conventional commits. Open a draft PR against `main`; do not push to `main` directly.
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

Active development. The 5-stage pipeline runs end-to-end on paper data; the dashboard renders against fixtures or live state. See [CLAUDE.md](./CLAUDE.md) for the full spec, and the `/state` directory (when populated) for current behaviour.
