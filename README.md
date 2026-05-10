# Agent-Trading

> **PAPER TRADING — Experimental autonomous AI agent. Leveraged ETFs and listed options on a small account are high-risk. Not financial advice.**

Autonomous multi-agent trading system that screens leveraged ETFs and listed options, runs adversarial bull/bear research, builds scenario-weighted views, constructs an 8–12 position portfolio (or holds all-cash), and executes via **Alpaca paper**. Runs on Windows under Task Scheduler with a Streamlit dashboard.

The canonical build spec is [CLAUDE.md](./CLAUDE.md). This README is the operator's manual.

---

## Mandatory risk warning

> Leveraged ETFs decay path-dependently in volatile markets and are not buy-and-hold instruments. Long options can expire worthless; theta works against long premium daily. A £2k account cannot diversify options positions meaningfully — concentration risk is structural, not a flaw to fix. This system is an experiment in autonomous AI trading agents, not a path to reliable returns. Expect losses. Do not deploy capital you cannot afford to lose entirely. None of this is financial advice.

---

## Setup (Windows, PowerShell)

> Section will be expanded in Phase 6 with full Task Scheduler walkthrough. The commands below are the steady-state install path.

```powershell
# 1. Clone
git clone <repo-url> Agent-Trading
cd Agent-Trading

# 2. Create + activate venv (Python 3.11+; uses the py launcher)
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure secrets
Copy-Item .env.example .env
notepad .env   # fill in ANTHROPIC_API_KEY and ALPACA_API_KEY/SECRET (paper)
```

---

## Manual run

```powershell
# Dry run — no orders, exercises the full 5-stage pipeline against fixtures
python orchestrator.py --dry-run

# Live paper run
python orchestrator.py
```

The orchestrator chooses its own next-run time and writes it to `state/next_run.json`.

---

## Dashboard

```powershell
# Local only
streamlit run dashboard.py

# Phone access on the same Wi-Fi (requires a Windows Defender Firewall inbound
# rule for TCP 8501 — set up in Phase 6's docs)
streamlit run dashboard.py --server.address 0.0.0.0
```

> The `0.0.0.0` mode has **no authentication**. Only enable it on a trusted home network.

---

## Halt procedure

To stop the orchestrator immediately:

```powershell
# Creates state/halt.flag — checked before any API call and before any order
New-Item -Path state\halt.flag -ItemType File -Force
```

Or click **Emergency Stop** on the dashboard's Settings tab. The orchestrator will refuse to run while the flag exists. Delete the file to resume.

---

## Logs

| Location | Contents |
| --- | --- |
| `state/runs/{run_id}/` | Per-run JSON artifacts: `screen.json`, `research.json`, `scenarios.json`, `portfolio.json` |
| `state/decisions.jsonl` | Append-only decision log (one row per stage, schema-validated) |
| `state/costs.jsonl` | Per-LLM-call cost ledger; daily and per-run caps enforced from this file |
| `state/current_portfolio.json` | Latest known portfolio (consumed by `monitor.py` and the dashboard) |
| `state/next_run.json` | Orchestrator-chosen next-run timestamp |
| `state/halt.flag` | Presence = stop |

---

## Repo hygiene

- `.env` is gitignored. Never commit secrets.
- `state/` is gitignored.
- All schemas under `schemas/` are validated on every write — schema-failed agent outputs are retried once and then abort the run.
- Conventional commits. Open a draft PR against `main`; do not push to `main` directly.

---

## Status

Build is in-progress. See [CLAUDE.md](./CLAUDE.md) for the full spec and `state/` (when populated) for current behaviour.
