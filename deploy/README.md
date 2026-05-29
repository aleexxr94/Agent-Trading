# Linux VPS operator playbook (Hetzner / any Ubuntu 24.04 VPS)

> **Paper trading only.** Live trading is gated by `LIVE_TRADING_ENABLED` and a hard-coded `LIVE_VERSION` constant in `orchestrator.py`. See [CLAUDE.md §Promotion to live](../CLAUDE.md#promotion-to-live-documented-only--do-not-enable-in-code).

This is the canonical playbook for running Agent-Trading. **Linux VPS + systemd is the sole supported production runtime.** The pieces:

| Layer | Component |
|---|---|
| Schedule | systemd `*.timer` units + `agent-scheduler.service` (dynamic cadence from `state/next_run.json`) |
| Wrapper | `deploy/run_orchestrator.sh`, `deploy/run_monitor.sh`, `deploy/run_scheduler.sh` |
| Halt | Touch `state/halt.flag` |
| Dashboard | `agent-dashboard.service` (auto-restart, binds `127.0.0.1` only) |

---

## One-time bootstrap

SSH into the VPS:

```bash
ssh root@<your-ip>
```

On the server:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/aleexxr94/agent-trading.git /opt/agent-trading
bash /opt/agent-trading/deploy/install.sh
```

The installer is idempotent — re-running it pulls the latest commit and re-creates the venv without touching `.env` or `state/`.

What it does:

1. Installs `python3` (3.12 on 24.04), `python3-venv`, `git`, `jq`, `build-essential`.
2. Creates a non-root system user `agent` (no shell, no sudo).
3. Clones the repo to `/opt/agent-trading`, owned by `agent:agent`.
4. Provisions `.venv` and installs `requirements.txt` as `agent`.
5. Seeds `.env` from `.env.example` (mode 600, owned by `agent`) — preserves any existing `.env`.
6. Installs five systemd units: orchestrator service + timer, monitor service + timer, dashboard service.
7. Drops a logrotate config that rotates `state/*.log` daily, 14 days retention.

---

## Configure secrets

```bash
sudo -u agent nano /opt/agent-trading/.env
```

Fill in:

| Key | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | **Paper** keys from alpaca.markets |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` (default) |
| `MODEL_*` | Per-stage Claude 4.X model IDs (defaults work) |
| `PER_RUN_COST_CAP_USD` / `DAILY_COST_CAP_USD` | $2 / $10 defaults |
| `LIVE_TRADING_ENABLED` | **Leave `false`.** |

The file is mode 600 owned by `agent` — only root and `agent` can read it.

---

## Manual smoke (do this FIRST)

Before enabling any timer, run the orchestrator interactively as the `agent` user:

```bash
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py --dry-run
sudo -u agent /opt/agent-trading/.venv/bin/python /opt/agent-trading/orchestrator.py
```

Inspect:

```bash
sudo -u agent tail -n 5 /opt/agent-trading/state/decisions.jsonl
sudo -u agent tail -n 5 /opt/agent-trading/state/costs.jsonl
sudo -u agent ls /opt/agent-trading/state/runs/
```

Send the contents of `decisions.jsonl` + `costs.jsonl` back if you hit anything unexpected — likely outcomes are prompt-tuning issues that a one-line PR fixes.

---

## Start the dashboard

```bash
systemctl enable --now agent-dashboard.service
systemctl status         agent-dashboard.service
journalctl -u            agent-dashboard.service -f
```

The dashboard binds to `127.0.0.1:8501` only — **deliberately not reachable from the public internet**. To view it:

- **From your laptop**: SSH tunnel `ssh -L 8501:127.0.0.1:8501 root@<ip>`, then open `http://localhost:8501` in your browser.
- **From your phone**: Tailscale (preferred). See [`tailscale.md`](./tailscale.md) — 5-minute setup, no public ports.

---

## Enable the autonomous timers

Once the smoke run looks clean:

```bash
systemctl enable --now agent-orchestrator.timer agent-monitor.timer
```

Inspect:

```bash
systemctl list-timers --all 'agent-*'
systemctl status agent-orchestrator.timer agent-monitor.timer
journalctl -u agent-orchestrator.service -f      # orchestrator log
journalctl -u agent-monitor.service      -f      # monitor log
```

### How rescheduling works

`agent-orchestrator.timer` has a daily fallback (13:30 UTC). After each successful run, `deploy/run_orchestrator.sh` reads `state/next_run.json` and registers a **transient one-shot timer** via `systemd-run --on-calendar=…`. So:

- Normal cadence comes from what the orchestrator itself decides each run.
- If the wrapper fails to schedule, the daily fallback still fires.
- Transient timers are auto-cleaned by systemd once they run.

Inspect transient timers with:

```bash
systemctl list-timers --all | grep agent-orchestrator-next
```

---

## Halt + resume

```bash
# Halt — every wrapper short-circuits before activating the venv;
# every systemd unit refuses to start (ConditionPathExists=!.../halt.flag).
sudo -u agent touch /opt/agent-trading/state/halt.flag

# Resume
sudo -u agent rm /opt/agent-trading/state/halt.flag
```

Or click **Emergency stop** on the dashboard's Settings tab.

---

## Logs

| What | Where |
|---|---|
| systemd journal | `journalctl -u agent-orchestrator.service` (and `agent-monitor`, `agent-dashboard`) |
| Decision log | `/opt/agent-trading/state/decisions.jsonl` |
| Cost ledger | `/opt/agent-trading/state/costs.jsonl` |
| Per-run artifacts | `/opt/agent-trading/state/runs/{run_id}/` |
| Current portfolio | `/opt/agent-trading/state/current_portfolio.json` |
| Next-run plan | `/opt/agent-trading/state/next_run.json` |

---

## Update the deployment

**Automated:** merging to `main` triggers `.github/workflows/deploy.yml`, which SSHes into the VPS, runs `install.sh`, and restarts the dashboard. Check the Actions tab on GitHub for status. One-time setup of the three repo secrets (`VPS_HOST`, `VPS_SSH_KEY`, `VPS_HOST_KEY`) is documented in the workflow file's header comment.

**Manual fallback** (if the Action is disabled or failing):

```bash
ssh root@<your-ip>
bash /opt/agent-trading/deploy/install.sh
systemctl restart agent-dashboard.service
# Timers pick up changes automatically on the next fire.
```

The installer pulls + resets `main`, re-installs requirements, and re-renders unit files. `.env` and `state/` are preserved.

---

## Uninstall

```bash
systemctl disable --now agent-orchestrator.timer agent-monitor.timer agent-dashboard.service
rm /etc/systemd/system/agent-{orchestrator,monitor,dashboard}.{service,timer}
systemctl daemon-reload
rm /etc/logrotate.d/agent-trading
# Keep /opt/agent-trading/state — that's your run history. Delete only if you're done.
```

---

## Security model

- API keys live in `/opt/agent-trading/.env` (mode 600, owned by `agent`). Never echoed to logs.
- `agent` user is non-root, no shell, no sudo. systemd units `NoNewPrivileges=true`.
- All units have `ProtectSystem=strict` + `ReadOnlyPaths={{REPO_DIR}}` + `ReadWritePaths={{REPO_DIR}}/state` — the orchestrator can write to `state/` but cannot touch `lib/` or `prompts/`.
- Dashboard binds to `127.0.0.1` only. Public exposure requires explicit action (Tailscale or an SSH tunnel).
- No public port is opened by `install.sh`. The Hetzner firewall (Settings → Cloud → Firewalls) can be left default-deny inbound apart from SSH (port 22).

If you want belt-and-suspenders:

```bash
ufw allow OpenSSH
ufw enable
```
