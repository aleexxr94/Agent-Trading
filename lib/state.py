"""State management — run IDs, atomic JSON I/O, halt flag, append-only logs.

All schemas under schemas/ are validated here on write. No ad-hoc validation
elsewhere in the codebase per the build plan.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
RUNS_DIR = STATE_DIR / "runs"
SCHEMA_DIR = ROOT / "schemas"

HALT_FLAG = STATE_DIR / "halt.flag"
DECISIONS_LOG = STATE_DIR / "decisions.jsonl"
COSTS_LOG = STATE_DIR / "costs.jsonl"
CURRENT_PORTFOLIO = STATE_DIR / "current_portfolio.json"
NEXT_RUN = STATE_DIR / "next_run.json"
NAV_HISTORY_LOG = STATE_DIR / "nav_history.jsonl"


# --------- run_id ---------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """UTC timestamp + 6-char random suffix. Sortable, unique, human-greppable."""
    ts = utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(3)}"


def run_dir(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------- halt flag ---------


def is_halted() -> bool:
    """Spec: orchestrator must check this BEFORE any API call and any order."""
    return HALT_FLAG.exists()


def set_halt(reason: str = "manual") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HALT_FLAG.write_text(f"{utcnow_iso()} {reason}\n", encoding="utf-8")


def clear_halt() -> None:
    if HALT_FLAG.exists():
        HALT_FLAG.unlink()


# --------- schema registry ---------


@lru_cache(maxsize=1)
def _registry() -> Registry:
    reg = Registry()
    for sf in SCHEMA_DIR.glob("*.schema.json"):
        s = json.loads(sf.read_text())
        reg = reg.with_resource(s["$id"], Resource.from_contents(s))
    return reg


@lru_cache(maxsize=None)
def _validator(schema_filename: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / schema_filename).read_text())
    return Draft202012Validator(schema, registry=_registry())


def validate(payload: Any, schema_filename: str) -> None:
    """Raise jsonschema.ValidationError on failure (caller catches and retries)."""
    _validator(schema_filename).validate(payload)


# --------- atomic JSON IO ---------


def write_json(path: Path, payload: Any, schema: str | None = None) -> None:
    if schema is not None:
        validate(payload, schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------- append-only logs ---------


def append_decision(entry: dict) -> None:
    """Validates against decision_log.schema.json then appends one JSON line."""
    validate(entry, "decision_log.schema.json")
    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=False) + "\n")


def append_cost(entry: dict) -> None:
    """No schema (free-form ledger). Required keys checked here for ergonomics."""
    required = {"run_id", "stage", "model", "cost_usd", "at"}
    missing = required - entry.keys()
    if missing:
        raise ValueError(f"cost entry missing keys: {sorted(missing)}")
    COSTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with COSTS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=False) + "\n")


def read_costs_today() -> list[dict]:
    """Return cost rows from the current UTC day. Used by daily-cap enforcement."""
    if not COSTS_LOG.exists():
        return []
    today = utcnow().date().isoformat()
    out = []
    for line in COSTS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("at", "").startswith(today):
            out.append(row)
    return out


def read_costs_for_run(run_id: str) -> list[dict]:
    if not COSTS_LOG.exists():
        return []
    out = []
    for line in COSTS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("run_id") == run_id:
            out.append(row)
    return out


def append_nav(entry: dict) -> None:
    """Append one row to state/nav_history.jsonl for the equity curve.

    Required keys: run_id, at, nav_usd. Optional but recommended: cash_usd,
    gross_pnl_usd, modelled_costs_usd, net_pnl_usd, positions_count, all_cash.
    """
    required = {"run_id", "at", "nav_usd"}
    missing = required - entry.keys()
    if missing:
        raise ValueError(f"nav entry missing keys: {sorted(missing)}")
    NAV_HISTORY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with NAV_HISTORY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=False) + "\n")


def read_nav_history(limit: int | None = None) -> list[dict]:
    """Return all NAV history rows (oldest first). Pass `limit` to cap."""
    if not NAV_HISTORY_LOG.exists():
        return []
    rows = []
    for line in NAV_HISTORY_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None:
        return rows[-limit:]
    return rows
