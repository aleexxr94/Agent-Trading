#!/bin/bash
# SessionStart hook: install Python dependencies so tests + linters work in
# Claude Code on the web. A fresh remote container starts with almost nothing
# installed; without this, pytest can't even collect (lib/state.py imports
# jsonschema) and the Alpaca-backed option-chain tests fail on ImportError.
set -euo pipefail

# Remote (web) sessions only — local machines manage their own .venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Runtime + dev deps. requirements.txt covers the app (anthropic, alpaca-py,
# pandas, jsonschema, pytest, …); ruff is the configured linter (pyproject.toml
# [tool.ruff]) but isn't a runtime dep, so install it explicitly. pip install
# (not a venv / not `ci`) keeps the step idempotent and lets the container
# cache the result between sessions. (We don't upgrade pip itself — the
# container's pip is distro-managed and refuses self-uninstall.)
python -m pip install --quiet -r requirements.txt ruff

echo "session-start: dependencies installed"
