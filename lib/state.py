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
# Cost-reset marker: when an operator manually resets the daily-cost meter
# from the dashboard, this file is written with the UTC timestamp. Cost
# helpers (read_costs_today / cost_today_usd) only sum rows whose `at` is
# AFTER this timestamp (when present and same-day). The underlying
# costs.jsonl audit log is never mutated — the reset is purely a display
# offset.
COST_RESET_FLAG = STATE_DIR / "cost_reset.json"
# All-time cost reset marker. Set from the dashboard when the operator
# wants to zero ALL displayed LLM-cost totals (today, month, all-time),
# not just the daily meter. Helpers in lib.dashboard_data filter every
# costs.jsonl row whose `at` is ≤ this timestamp out of the displayed
# totals — the underlying audit log is never mutated. Cap enforcement
# in lib.llm.check_caps_or_raise stays unfiltered: it's a per-run /
# per-day safety rail and isn't subject to the operator's display preference.
ALL_TIME_COST_RESET_FLAG = STATE_DIR / "cost_all_time_reset.json"
DECISIONS_LOG = STATE_DIR / "decisions.jsonl"
COSTS_LOG = STATE_DIR / "costs.jsonl"
# Per-fill audit log for per-trade PnL accounting. One row per Alpaca
# activity (fill or partial fill). Idempotent by activity_id so repeated
# syncs from /v2/account/activities don't duplicate rows. See lib/trades.py
# for the writer and the FIFO matcher that turns this log into closed-trade
# realised PnL with real fees and equal-split attributed LLM cost.
TRADES_LOG = STATE_DIR / "trades.jsonl"
CURRENT_PORTFOLIO = STATE_DIR / "current_portfolio.json"
NEXT_RUN = STATE_DIR / "next_run.json"
NAV_HISTORY_LOG = STATE_DIR / "nav_history.jsonl"
# Cycle-dedup: signals-hash + portfolio-pointer from last completed
# cycle. If the next cycle's signals hash matches AND the broker
# positions are unchanged, the orchestrator skips strategist +
# constructor + execute and just re-writes next_run.json. Saves ~$0.25
# on quiet markets.
LAST_CYCLE_HASH = STATE_DIR / "last_cycle_hash.json"

# NAV display anchor (PR following #73). Alpaca paper accounts default
# to $100,000 USD and can't always be reset to a lower target. The
# anchor lets the operator pin the displayed NAV to the spec target
# ($2,500 per CLAUDE.md) while letting it track broker P&L 1:1 from
# that moment. The file stores both the broker baseline and the
# virtual baseline so the offset is recoverable and re-anchoring later
# stays coherent.
#
# Schema:
#   {
#     "broker_baseline_usd": <float>,
#     "virtual_baseline_usd": <float>,
#     "set_at": <ISO-UTC>,
#     "note": <optional string>
#   }
#
# Offset applied to displayed NAV = broker_baseline_usd - virtual_baseline_usd.
# Dashboards subtract this from raw broker equity (and from nav_history
# rows at render time). Constructor sizing is unaffected — it reads
# VIRTUAL_NAV_USD from the environment as a separate setting.
NAV_OFFSET_FLAG = STATE_DIR / "nav_offset.json"

# Remembered manual broker baseline — separate from the active anchor.
# The Settings tab's "manual broker baseline" input defaults to this
# value so an operator can re-anchor without re-typing the exact
# pre-trades equity each time. Survives re-anchor / clear-anchor;
# only changes when the operator explicitly enters a new value in the
# manual input and clicks Set.
#
# Schema: {"broker_baseline_usd": <float>, "set_at": <ISO-UTC>}
NAV_MANUAL_BASELINE_FLAG = STATE_DIR / "nav_manual_baseline.json"


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


def read_cost_reset_at() -> str | None:
    """Return the ISO UTC timestamp when the dashboard last reset the
    daily cost meter (or None if no reset on file). Helpers compare each
    log row's `at` against this to decide whether to count it toward
    the displayed "today's cost"."""
    if not COST_RESET_FLAG.exists():
        return None
    try:
        return json.loads(COST_RESET_FLAG.read_text(encoding="utf-8")).get("at")
    except (json.JSONDecodeError, OSError):
        return None


def set_cost_reset(reason: str = "dashboard") -> str:
    """Mark NOW as the cost-meter reset point. Returns the ISO timestamp written.
    Subsequent calls to read_costs_today only return rows with `at` > this."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    at = utcnow_iso()
    COST_RESET_FLAG.write_text(
        json.dumps({"at": at, "reason": reason}, sort_keys=True),
        encoding="utf-8",
    )
    return at


def clear_cost_reset() -> None:
    """Discard any reset marker — the daily meter reverts to summing the
    full UTC day. Useful when the operator wants to see the unfiltered
    daily total again."""
    if COST_RESET_FLAG.exists():
        COST_RESET_FLAG.unlink()


def read_all_time_cost_reset_at() -> str | None:
    """Return the ISO UTC timestamp of the all-time cost-reset marker,
    or None if no reset on file. Display helpers filter every cost row
    whose `at` is ≤ this timestamp out of dashboard totals."""
    if not ALL_TIME_COST_RESET_FLAG.exists():
        return None
    try:
        return json.loads(
            ALL_TIME_COST_RESET_FLAG.read_text(encoding="utf-8")
        ).get("at")
    except (json.JSONDecodeError, OSError):
        return None


def set_all_time_cost_reset(reason: str = "dashboard") -> str:
    """Mark NOW as the all-time display-reset point for LLM cost totals.
    Returns the ISO timestamp written.

    The underlying costs.jsonl audit log is preserved untouched — only
    the dashboard's displayed totals (today, this run, monthly, all-time)
    will skip rows at or before this marker. Cap enforcement in
    lib.llm.check_caps_or_raise is NOT affected; per-run and per-day
    caps remain in force based on the raw log.

    Also stamps the same timestamp into the daily reset marker so that
    "today's cost" meter zeros at the same moment — the operator asked
    for "reset all costs up to date", which includes the current UTC day.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    at = utcnow_iso()
    ALL_TIME_COST_RESET_FLAG.write_text(
        json.dumps({"at": at, "reason": reason}, sort_keys=True),
        encoding="utf-8",
    )
    # Stamp the same timestamp into the daily-reset marker so today's
    # display clears synchronously. The two markers serve different
    # display surfaces but the "reset all" action should affect both.
    COST_RESET_FLAG.write_text(
        json.dumps({"at": at, "reason": reason}, sort_keys=True),
        encoding="utf-8",
    )
    return at


def clear_all_time_cost_reset() -> None:
    """Discard the all-time reset marker; dashboard totals revert to the
    full audit log.

    Codex P2 (PR #53): set_all_time_cost_reset writes BOTH markers
    (all-time + daily) to the same timestamp so today's meter zeros
    synchronously. The clear path must do the symmetric thing — drop
    both — otherwise today's meter stays filtered after the operator
    clicks "Clear all-time reset (show full history)" and the dashboard
    is left in a partially-reset state. If the operator wants a daily-
    only reset they can re-apply it via the dedicated daily-reset button.
    """
    if ALL_TIME_COST_RESET_FLAG.exists():
        ALL_TIME_COST_RESET_FLAG.unlink()
    # Symmetric to set_all_time_cost_reset which writes both markers.
    if COST_RESET_FLAG.exists():
        COST_RESET_FLAG.unlink()


def filter_costs_post_reset(rows: list[dict]) -> list[dict]:
    """Drop cost rows whose `at` is ≤ the all-time reset marker.

    Used by every dashboard helper that surfaces an LLM-cost total
    (lib.dashboard_data.total_token_cost / cost_by_month / load_costs /
    runs_count). When no reset marker exists, returns ``rows`` unchanged.

    Per-day display (cost_today / read_costs_today) already filters on
    the same-day reset marker; this is a stricter superset that also
    blanks history.
    """
    reset_at = read_all_time_cost_reset_at()
    if not reset_at:
        return rows
    return [r for r in rows if (r.get("at") or "") > reset_at]


# --------- NAV display anchor (DEPRECATED) ---------
#
# The synthetic-balance refactor (see lib/dashboard_data.SyntheticBalance)
# made these helpers unused: the dashboard no longer derives its
# headline from Alpaca account equity, so there's no offset to apply.
# The functions below are kept for one release cycle so that:
#   - If any external tooling or snapshot reader still references them,
#     they don't ImportError on upgrade.
#   - State files on disk (state/nav_offset.json,
#     state/nav_manual_baseline.json) can still be read for forensic
#     debugging if needed.
# They'll be removed in a follow-up cleanup PR.


def read_nav_offset() -> dict | None:
    """Return the anchor dict (broker_baseline_usd, virtual_baseline_usd,
    set_at, note) or None when no anchor is set.

    Display-only: this does not influence cap enforcement, sizing,
    sanity rules, or broker orders. Pure rendering offset.
    """
    if not NAV_OFFSET_FLAG.exists():
        return None
    try:
        data = json.loads(NAV_OFFSET_FLAG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "broker_baseline_usd" not in data or "virtual_baseline_usd" not in data:
        return None
    return data


def nav_offset_usd() -> float:
    """Convenience: the dollar offset to subtract from broker equity
    when displaying NAV. Returns 0.0 when no anchor is set so calls
    are no-op safe."""
    data = read_nav_offset()
    if data is None:
        return 0.0
    try:
        return float(data["broker_baseline_usd"]) - float(data["virtual_baseline_usd"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def set_nav_offset(
    *,
    broker_baseline_usd: float,
    virtual_baseline_usd: float = 2500.0,
    note: str = "",
) -> str:
    """Stamp a new anchor. `broker_baseline_usd` is the broker's current
    equity at anchor time; `virtual_baseline_usd` is what the operator
    wants the dashboard to show at that moment (defaults to $2,500 per
    the CLAUDE.md spec).

    Returns the ISO-UTC timestamp written into the file.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    at = utcnow_iso()
    NAV_OFFSET_FLAG.write_text(
        json.dumps({
            "broker_baseline_usd": float(broker_baseline_usd),
            "virtual_baseline_usd": float(virtual_baseline_usd),
            "set_at": at,
            "note": note,
        }, sort_keys=True),
        encoding="utf-8",
    )
    return at


def clear_nav_offset() -> None:
    """Remove the anchor. Dashboard reverts to raw broker equity."""
    if NAV_OFFSET_FLAG.exists():
        NAV_OFFSET_FLAG.unlink()


def read_manual_nav_baseline_usd() -> float | None:
    """Return the operator's last-entered manual broker baseline, or
    None when unset. The dashboard's manual-baseline input defaults to
    this value so re-anchor / clear-anchor flows don't wipe the
    operator's known pre-trades equity figure."""
    if not NAV_MANUAL_BASELINE_FLAG.exists():
        return None
    try:
        data = json.loads(NAV_MANUAL_BASELINE_FLAG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return float(data["broker_baseline_usd"])
    except (KeyError, TypeError, ValueError):
        return None


def set_manual_nav_baseline_usd(broker_baseline_usd: float) -> str:
    """Stamp a new remembered manual baseline. Returns the ISO-UTC
    timestamp written into the file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    at = utcnow_iso()
    NAV_MANUAL_BASELINE_FLAG.write_text(
        json.dumps({
            "broker_baseline_usd": float(broker_baseline_usd),
            "set_at": at,
        }, sort_keys=True),
        encoding="utf-8",
    )
    return at


def clear_manual_nav_baseline() -> None:
    """Drop the remembered manual baseline. Input falls back to its
    hardcoded default."""
    if NAV_MANUAL_BASELINE_FLAG.exists():
        NAV_MANUAL_BASELINE_FLAG.unlink()


def read_costs_today() -> list[dict]:
    """Return cost rows from the current UTC day. Used by daily-cap enforcement.

    Honours `cost_reset.json` if present and same-UTC-day: rows are filtered
    to those AFTER the reset marker. This lets an operator zero the meter
    from the dashboard without losing audit-log entries.
    """
    if not COSTS_LOG.exists():
        return []
    today = utcnow().date().isoformat()
    reset_at = read_cost_reset_at()
    # Only apply the reset filter when it's from today — yesterday's reset
    # shouldn't affect today's accounting.
    reset_today = reset_at if (reset_at and reset_at.startswith(today)) else None
    out = []
    for line in COSTS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not row.get("at", "").startswith(today):
            continue
        if reset_today and row.get("at", "") <= reset_today:
            continue
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


def append_trade(entry: dict) -> None:
    """Append one row to state/trades.jsonl.

    Required keys (raises ValueError if missing):
      - activity_id (str) — Alpaca activities-API ID; the idempotency key.
        Callers must check existing rows before calling this; the function
        does NOT dedupe internally to keep the writer fast.
      - alpaca_order_id (str)
      - symbol (str) — ETF symbol or OSI option symbol
      - kind ("etf" | "option")
      - side ("buy" | "sell")
      - qty (number)
      - fill_price (number) — per-share for ETFs, per-share-premium for options
      - fees_usd (number) — sum of OCC + SEC + any other fees from the fill
      - filled_at (ISO UTC)
      - run_id (str | None) — the orchestrator run that triggered this fill,
        when known. None is allowed for manual / out-of-band trades that the
        operator placed without the agent's involvement.
    """
    required = {
        "activity_id", "alpaca_order_id", "symbol", "kind", "side",
        "qty", "fill_price", "fees_usd", "filled_at",
    }
    missing = required - entry.keys()
    if missing:
        raise ValueError(f"trade entry missing keys: {sorted(missing)}")
    if "run_id" not in entry:
        entry["run_id"] = None
    TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TRADES_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=False) + "\n")


def read_trades() -> list[dict]:
    """Return all rows from state/trades.jsonl, in append order.

    Returns [] when the file doesn't exist yet — same convention as the
    other JSONL readers in this module.
    """
    if not TRADES_LOG.exists():
        return []
    out = []
    for line in TRADES_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_trade_activity_ids() -> set[str]:
    """Return the set of activity_ids already in trades.jsonl. Use this
    for idempotent sync against /v2/account/activities — only insert rows
    whose activity_id isn't already in the set."""
    return {r.get("activity_id") for r in read_trades() if r.get("activity_id")}


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


# --------- wipe history (dashboard "start fresh" button) ---------


def wipe_run_history(*, include_costs: bool = True, backup: bool = True) -> dict:
    """Clear all per-cycle history so the dashboard renders from a blank
    slate. Triggered by the Settings tab "Wipe history" button.

    What this removes:
      - state/runs/* (every per-cycle artifact dir)
      - state/decisions.jsonl (decision log)
      - state/nav_history.jsonl (NAV-per-cycle history)
      - state/trades.jsonl (Alpaca fill log)
      - state/current_portfolio.json (last portfolio snapshot)
      - state/next_run.json (last scheduler target)
      - state/last_cycle_hash.json (cycle-dedup fingerprint)
      - state/scheduler_last_fired.txt (scheduler dedup)
      - state/cost_reset.json + cost_all_time_reset.json (display
        markers — meaningless without an audit log behind them)
      - state/costs.jsonl ONLY if include_costs=True (the audit log
        backing per-run / daily cap enforcement)

    What this PRESERVES:
      - state/halt.flag (operator's emergency-stop intent — not for
        this button to override)
      - The state/ directory itself

    Backup: when backup=True (default), copies every file we're about
    to clobber into state/backup_<utc_iso>/ first. ~30s of disk; if
    something goes wrong the operator restores by cp-ing files back.
    """
    import shutil

    cleared: dict = {
        "runs_dirs_removed": 0,
        "jsonl_truncated": [],
        "snapshots_removed": [],
        "backup_dir": None,
    }

    # Backup first — fail-safe rope-and-pulley.
    #
    # Naming: microsecond precision in the timestamp covers human-pace
    # double-clicks; a numeric suffix retry loop covers the pathological
    # case where two wipes land in the same microsecond (e.g. concurrent
    # automation, test calls). Without this, a same-second collision
    # would have OSError-raised inside the existing try/except, blanked
    # backup_dir to None, and let the wipe proceed without a safety net —
    # exactly the scenario where the operator needs rollback (Codex P2
    # on PR #70).
    if backup:
        base_stamp = utcnow().strftime('%Y%m%dT%H%M%S%fZ')
        backup_dir = STATE_DIR / f"backup_{base_stamp}"
        suffix = 1
        try:
            while backup_dir.exists():
                suffix += 1
                backup_dir = STATE_DIR / f"backup_{base_stamp}_{suffix}"
                if suffix > 1000:
                    # Pathological — bail out and proceed without backup
                    # rather than spinning forever.
                    raise OSError("backup dir naming exhausted suffix space")
            backup_dir.mkdir(parents=True, exist_ok=False)
            for f in STATE_DIR.iterdir():
                if f.name.startswith("backup_"):
                    continue
                if f.name == "halt.flag":
                    continue
                if f.is_file():
                    shutil.copy2(f, backup_dir / f.name)
                elif f.is_dir() and f.name == "runs":
                    # Recursive copy — small but per-run cache files
                    # add up. Skip on permission/disk errors so the
                    # main wipe still proceeds.
                    try:
                        shutil.copytree(f, backup_dir / "runs")
                    except (OSError, shutil.Error):
                        pass
            cleared["backup_dir"] = str(backup_dir)
        except OSError:
            cleared["backup_dir"] = None  # fail soft — proceed without backup

    # Truncate (don't unlink) the JSONL audit logs so anything that holds
    # a file handle keeps writing without recreating mode/owner issues.
    jsonl_files = [DECISIONS_LOG, TRADES_LOG, NAV_HISTORY_LOG]
    if include_costs:
        jsonl_files.append(COSTS_LOG)
    for f in jsonl_files:
        if f.exists():
            try:
                f.write_text("")
                cleared["jsonl_truncated"].append(f.name)
            except OSError:
                pass

    # Remove snapshot files entirely (orchestrator regenerates them).
    # Includes deprecated NAV anchor files (NAV_OFFSET_FLAG,
    # NAV_MANUAL_BASELINE_FLAG) so a fresh-start wipe truly is fresh
    # — the synthetic-balance refactor doesn't read them, but stale
    # files lingering on disk are noise the operator probably doesn't
    # want carried into the next experiment.
    snapshots = [
        CURRENT_PORTFOLIO, NEXT_RUN, LAST_CYCLE_HASH,
        STATE_DIR / "scheduler_last_fired.txt",
        COST_RESET_FLAG, ALL_TIME_COST_RESET_FLAG,
        NAV_OFFSET_FLAG, NAV_MANUAL_BASELINE_FLAG,
    ]
    for f in snapshots:
        if f.exists():
            try:
                f.unlink()
                cleared["snapshots_removed"].append(f.name)
            except OSError:
                pass

    # Wipe per-cycle run dirs.
    if RUNS_DIR.exists():
        for run_dir in RUNS_DIR.iterdir():
            if run_dir.is_dir():
                try:
                    shutil.rmtree(run_dir)
                    cleared["runs_dirs_removed"] += 1
                except OSError:
                    pass

    return cleared
