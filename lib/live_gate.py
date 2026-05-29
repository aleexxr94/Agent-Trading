"""Single source of truth for the live-trading triple lock.

Both entrypoints that can touch the broker — ``orchestrator.py`` (submits
orders) and ``monitor.py`` (flattens / cancels) — must refuse to run when
``LIVE_TRADING_ENABLED=true`` while the hard-coded ``LIVE_VERSION`` is still
0. Keeping the constant + the assertion in one module avoids the two
entrypoints drifting apart (e.g. the orchestrator guarded but the monitor
not — which previously let the monitor flatten under a half-raised lock,
relying solely on AlpacaBroker refusing non-paper construction).

Triple lock (see CLAUDE.md §Promotion to live):
  1. ``LIVE_VERSION`` constant below — bump 0 → 1 in code (can't be set via env).
  2. ``LIVE_TRADING_ENABLED=true`` env var.
  3. ``lib/alpaca_client.py`` independently refuses a non-paper client unless
     ``LIVE_TRADING_ENABLED=true``.

``assert_live_gate`` fails closed: if the env says live but the version is
still 0, the entrypoint exits without doing anything.
"""
from __future__ import annotations

import os

# Hard-coded gate per spec §Critical preconditions #1. Bump only when promoted
# to live; combined with the LIVE_TRADING_ENABLED env var (the triple lock).
LIVE_VERSION = 0


def live_trading_env_enabled() -> bool:
    """True iff the LIVE_TRADING_ENABLED env var is set to true."""
    return os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"


def live_gate_blocked() -> bool:
    """True iff the env requests live trading but ``LIVE_VERSION`` is still 0.

    When True, the caller must refuse to run (fail closed). The two must be
    raised together: env=true alone, with the version still 0, is a
    misconfiguration we never honour.
    """
    return live_trading_env_enabled() and LIVE_VERSION == 0


def assert_live_gate(*, entrypoint: str) -> int | None:
    """Return a non-zero exit code if the live gate is half-raised, else None.

    ``entrypoint`` is only used for the operator-facing message. Callers
    should ``return`` the code from their ``main`` when it is not None.
    """
    if live_gate_blocked():
        print(
            f"{entrypoint}: LIVE_TRADING_ENABLED=true but LIVE_VERSION=0 "
            "— refusing to run (fail closed). See CLAUDE.md §Promotion to live.",
            file=__import__("sys").stderr,
        )
        return 2
    return None
