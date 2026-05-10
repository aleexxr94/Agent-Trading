#!/usr/bin/env bash
# Idempotent installer for the Agent-Trading Linux deployment.
#
# Tested on Ubuntu 24.04 LTS (Hetzner Cloud, default image).
# Run as root:    sudo bash deploy/install.sh
#
# Re-running is safe — every step checks current state before mutating.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/agent-trading}"
AGENT_USER="${AGENT_USER:-agent}"
GIT_REMOTE="${GIT_REMOTE:-https://github.com/aleexxr94/agent-trading.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log()  { printf '\033[0;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Run as root (sudo bash $0)."

# ---------- 1. system packages ----------
log "Installing OS packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    git jq curl ca-certificates tzdata \
    build-essential >/dev/null

# ---------- 2. agent user ----------
if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
    log "Creating system user '$AGENT_USER'..."
    useradd --system --create-home --home-dir "/home/$AGENT_USER" \
            --shell /usr/sbin/nologin "$AGENT_USER"
else
    log "User '$AGENT_USER' already exists."
fi

# ---------- 3. repo ----------
if [ ! -d "$REPO_DIR/.git" ]; then
    log "Cloning $GIT_REMOTE into $REPO_DIR..."
    install -d -o "$AGENT_USER" -g "$AGENT_USER" "$REPO_DIR"
    sudo -u "$AGENT_USER" git clone --branch "$GIT_BRANCH" "$GIT_REMOTE" "$REPO_DIR"
else
    log "Repo present — pulling latest on $GIT_BRANCH..."
    sudo -u "$AGENT_USER" git -C "$REPO_DIR" fetch --quiet origin
    sudo -u "$AGENT_USER" git -C "$REPO_DIR" checkout --quiet "$GIT_BRANCH"
    sudo -u "$AGENT_USER" git -C "$REPO_DIR" reset --hard --quiet "origin/$GIT_BRANCH"
fi

# ---------- 4. venv + deps ----------
log "Provisioning .venv..."
sudo -u "$AGENT_USER" "$PYTHON_BIN" -m venv "$REPO_DIR/.venv"
sudo -u "$AGENT_USER" "$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$AGENT_USER" "$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# ---------- 5. .env ----------
ENV_FILE="$REPO_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    log "Seeding .env from .env.example — fill in the keys before starting timers."
    install -m 600 -o "$AGENT_USER" -g "$AGENT_USER" \
        "$REPO_DIR/.env.example" "$ENV_FILE"
else
    log "Existing .env preserved (mode=$(stat -c '%a' "$ENV_FILE"))."
    chmod 600 "$ENV_FILE"
    chown "$AGENT_USER:$AGENT_USER" "$ENV_FILE"
fi

# ---------- 6. state dir ----------
install -d -m 750 -o "$AGENT_USER" -g "$AGENT_USER" "$REPO_DIR/state"
install -d -m 750 -o "$AGENT_USER" -g "$AGENT_USER" "$REPO_DIR/state/runs"

# ---------- 7. systemd units ----------
log "Installing systemd units..."
for unit in agent-orchestrator.service agent-orchestrator.timer \
            agent-monitor.service      agent-monitor.timer \
            agent-dashboard.service; do
    src="$REPO_DIR/deploy/systemd/$unit"
    [ -f "$src" ] || fail "Missing unit file: $src"
    # Substitute placeholders so the units track REPO_DIR / AGENT_USER.
    sed -e "s|{{REPO_DIR}}|$REPO_DIR|g" \
        -e "s|{{AGENT_USER}}|$AGENT_USER|g" \
        "$src" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload

# ---------- 8. log rotation ----------
cat >/etc/logrotate.d/agent-trading <<'EOF'
/opt/agent-trading/state/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
    su agent agent
}
EOF

# ---------- 9. summary ----------
cat <<EOF

==================================================================
 Agent-Trading installed at: $REPO_DIR
 Running as system user:     $AGENT_USER
==================================================================

NEXT STEPS (in order):

  1. Edit secrets:
       sudo -u $AGENT_USER nano $ENV_FILE
     Fill in ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_API_SECRET.

  2. Manual smoke (recommended before enabling timers):
       sudo -u $AGENT_USER $REPO_DIR/.venv/bin/python $REPO_DIR/orchestrator.py --dry-run
       sudo -u $AGENT_USER $REPO_DIR/.venv/bin/python $REPO_DIR/orchestrator.py
     Inspect:
       sudo -u $AGENT_USER tail -n 5 $REPO_DIR/state/decisions.jsonl
       sudo -u $AGENT_USER tail -n 5 $REPO_DIR/state/costs.jsonl

  3. Start the dashboard (binds to 127.0.0.1:8501 — reach via Tailscale or SSH tunnel):
       systemctl enable --now agent-dashboard.service

  4. When the smoke looks clean, enable the timers:
       systemctl enable --now agent-orchestrator.timer agent-monitor.timer

  5. Inspect everything:
       systemctl status   agent-orchestrator.timer agent-monitor.timer agent-dashboard.service
       systemctl list-timers
       journalctl -u agent-orchestrator.service -f

HALT (stops all timers within one cycle):
       sudo -u $AGENT_USER touch $REPO_DIR/state/halt.flag

RESUME:
       sudo -u $AGENT_USER rm $REPO_DIR/state/halt.flag

See deploy/README.md and deploy/tailscale.md for the full operator playbook.
EOF
