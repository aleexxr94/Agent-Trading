"""v2 pipeline entrypoint.

Stages:
  0. market_gate → state/runs/{run_id}/market_gate.json (Alpaca clock, $0)
  1. signals     → state/runs/{run_id}/signals.json     (deterministic Python, $0)
  2. strategist  → state/runs/{run_id}/view.json        (Sonnet 4.6, ~$0.05)
  3. construct   → state/runs/{run_id}/portfolio.json   (Opus 4.7, ~$0.20)
  4. sanity      → state/runs/{run_id}/sanity.json      (deterministic, $0)
  5. execute     → state/runs/{run_id}/orders.json + next_run.json (Alpaca paper, $0)

v1 → v2 migration: the bull/bear research stages, the chains stage, and
the scenarios stage were collapsed into a single deterministic signals
table + a single strategist LLM call. Per-cycle LLM cost dropped from
~$1.50–2.50 to ~$0.25. The construct stage still owns position selection
+ sizing + kill-condition tailoring on Opus 4.7.

``--dry-run`` reads from tests/fixtures/* (no LLM, no orders, no broker).
Live mode loads AlpacaBroker, calls the market gate, then runs the
pipeline.

Live trading is gated by LIVE_VERSION + LIVE_TRADING_ENABLED — see spec.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Load .env so manual `python orchestrator.py` invocations pick up API keys.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass
from typing import Callable

from lib import llm, market_gate, options_chain, risk, sanity, signals, stages, state
from lib.broker import Broker

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "tests" / "fixtures"

# Hard-coded gate per spec §Critical preconditions #1.
LIVE_VERSION = 0  # bump only when promoted; combined with LIVE_TRADING_ENABLED env var

RISK_WARNING = (
    "PAPER TRADING. Leveraged ETFs decay path-dependently; long options can "
    "expire worthless. Capital preservation outweighs upside chasing on a "
    "$2.5k experimental account. Not financial advice."
)


# ----- helpers -----


def _hash_inputs(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()[:32]


def _load_fixture(name: str) -> dict:
    p = FIXTURE_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"Missing fixture: {p}")
    return json.loads(p.read_text())


def _system_blocks(cfg: stages.StageConfig) -> list[dict]:
    """Cache the (large, stable) prompt as the cached prefix; let the
    run-specific user message be the volatile suffix. One ephemeral
    breakpoint per stage."""
    return [
        {
            "type": "text",
            "text": cfg.system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _schema_ref_registry() -> dict[str, dict]:
    """Build a $id → schema map from schemas/*.schema.json so external
    $refs can be inlined by lib.llm.sanitize_schema_for_structured_output."""
    registry: dict[str, dict] = {}
    for path in (ROOT / "schemas").glob("*.schema.json"):
        s = json.loads(path.read_text())
        if isinstance(s.get("$id"), str):
            registry[s["$id"]] = s
    return registry


def _output_config(cfg: stages.StageConfig) -> dict | None:
    """Merge stage effort with structured-output format (if schema set)."""
    out: dict = dict(cfg.output_config_extras)
    if cfg.schema_filename:
        full = json.loads((ROOT / "schemas" / cfg.schema_filename).read_text())
        out["format"] = {
            "type": "json_schema",
            "schema": llm.sanitize_schema_for_structured_output(
                full, ref_registry=_schema_ref_registry()
            ),
        }
    return out or None


@dataclass
class StageContext:
    run_id: str
    dry_run: bool
    broker: Broker | None
    # Which pipeline branch is running. "trade" = full pipeline (default,
    # back-compat for legacy callers that don't pass this field). "review"
    # = after-hours reflection: signals + strategist + meta-scheduler
    # only, no construct / sanity / execute. Defense in depth — the
    # run_pipeline branch is the primary safety boundary; this field
    # rides on every decision row so audit can prove what ran.
    cycle_intent: str = "trade"
    # Where the cycle_intent was loaded from. Affects the daily review
    # cap: only `intent_source="file"` rows count toward the cap, so an
    # operator's `--intent=review` doesn't burn the autonomous-review
    # budget.
    intent_source: str = "default"


# Daily-review-frequency cap defaults. Operator can override via env.
DEFAULT_MAX_REVIEW_CYCLES_PER_DAY = 2


def _max_review_cycles_per_day() -> int:
    """Env-driven daily cap on autonomous review cycles. CLI overrides
    (--ignore-cap) bypass this entirely; only `intent_source="file"`
    cycles are subject to it."""
    try:
        return int(os.environ.get("MAX_REVIEW_CYCLES_PER_DAY", DEFAULT_MAX_REVIEW_CYCLES_PER_DAY))
    except ValueError:
        return DEFAULT_MAX_REVIEW_CYCLES_PER_DAY


def _load_cycle_intent(
    *, cli_intent: str | None, ignore_cap: bool,
) -> tuple[str, str]:
    """Resolve the cycle intent for THIS run with precedence:
      CLI > env > prior next_run.json > "trade".

    Returns (intent, source). `source` tells the cap-enforcement path
    whether the intent was operator-driven (cli/env, exempt from the
    daily review cap) or autonomous (file/default, subject to the cap).

    Anything we can't parse cleanly falls back to "trade" — review is
    the cheaper but more opinionated path, so when in doubt run the
    full pipeline.
    """
    if cli_intent in ("trade", "review"):
        return cli_intent, "cli"
    env_intent = os.environ.get("CYCLE_INTENT")
    if env_intent in ("trade", "review"):
        return env_intent, "env"
    if state.NEXT_RUN.exists():
        try:
            nr = json.loads(state.NEXT_RUN.read_text(encoding="utf-8"))
            file_intent = nr.get("cycle_intent")
            if file_intent in ("trade", "review"):
                return file_intent, "file"
        except (json.JSONDecodeError, OSError):
            pass
    return "trade", "default"


def _count_autonomous_reviews_today() -> int:
    """Count review cycles run today (UTC) whose intent came from
    next_run.json. Only `intent_source="file"` rows count — manual
    `--intent=review` and `CYCLE_INTENT=review` cycles are operator-
    driven and don't burn the cap. We look at the synthetic
    ``review_complete`` decision row (one per review cycle) so each
    cycle is counted exactly once.

    Cap-skip rows (status="skipped_review_cap") are excluded so a
    blocked attempt doesn't itself burn budget — otherwise raising
    MAX_REVIEW_CYCLES_PER_DAY mid-day would still find the meter
    inflated by the failed attempts (Codex P2 on PR #83).
    """
    if not state.DECISIONS_LOG.exists():
        return 0
    today = state.utcnow().date().isoformat()
    n = 0
    for line in state.DECISIONS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("stage") != "review_complete":
            continue
        if row.get("status") != "ok":
            continue
        if row.get("cycle_intent") != "review":
            continue
        if row.get("intent_source") != "file":
            continue
        if not (row.get("started_at") or "").startswith(today):
            continue
        n += 1
    return n


def _next_run_at_after_review_cap(broker: Broker | None) -> str:
    """When the daily review cap blocks an autonomous review pick,
    advance next_run to the broker-reported next market open if we have
    it, else 6 hours out — the cheapest fallback that keeps the
    scheduler firing without burning a slot on another review attempt.
    """
    from datetime import timedelta
    if broker is not None:
        try:
            clock = broker.get_clock()
            if clock is not None and clock.next_open:
                return clock.next_open
        except Exception:
            pass
    return (state.utcnow() + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- per-stage runners -----


def stage_signals(ctx: StageContext) -> dict:
    """Deterministic feature table for the v2 universe. $0 cost.

    In dry-run mode loads the test fixture; otherwise calls
    lib.signals.compute_signals which hits yfinance for each ticker.
    """
    if ctx.dry_run:
        out = _load_fixture("signals.json")
        out["run_id"] = ctx.run_id
        return out
    return signals.compute_signals(run_id=ctx.run_id)


def stage_strategist(
    ctx: StageContext,
    signals_out: dict,
    current_positions: list[dict] | None = None,
    pnl_history: list[dict] | None = None,
) -> dict:
    """One LLM call — Sonnet 4.6, ~$0.05. Reads signals + current
    portfolio + recent PnL feedback; emits view.json with regime
    classification + 0-6 ranked candidates.

    current_positions: broker-reported holdings (passed through from
    orchestrator). Lets the strategist bias toward "keep this winner"
    vs churning to fresh ideas.

    pnl_history: last 5 cycles' {regime, positions_summary,
    realized_4h_pnl_pct} so the strategist can self-correct drift.
    """
    if ctx.dry_run:
        out = _load_fixture("view.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.strategist()
    current_positions = current_positions or []
    pnl_history = pnl_history or []
    content = (
        f"Signals: {json.dumps(signals_out, sort_keys=True)}\n"
        f"Current broker positions: {json.dumps(current_positions, sort_keys=True)}\n"
        f"Recent PnL history (last cycles, oldest first): "
        f"{json.dumps(pnl_history, sort_keys=True)}\n"
        f"Run id: {ctx.run_id}\n"
        "Return JSON conforming to view.schema.json. When current "
        "positions already align with your regime call, prefer keeping "
        "them (no churn). When recent PnL on a regime has been "
        "consistently negative, weight your new regime call lower."
    )
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{"role": "user", "content": content}],
        schema_filename=cfg.schema_filename,
        max_tokens=cfg.max_tokens,
        thinking=cfg.thinking,
        output_config=_output_config(cfg),
    ))
    return res.payload


def stage_chain_lookup(ctx: StageContext, signals_out: dict, view: dict) -> dict:
    """Resolve option candidates → real tradable contracts. $0 cost.

    The strategist names option candidates like ``{symbol: "SPY",
    instrument_kind: "option_call"}``. Without this stage the
    constructor invents OSI symbols (strike+expiry combos) from thin
    air, and orders.py rejects them at submission time when Alpaca
    paper doesn't list those exact contracts.

    For each option candidate, query Alpaca's option-contracts
    endpoint for the nearest-OTM contract at target DTE 37 (±14d).
    Output ``chain_lookups.json`` keyed by candidate, so the
    constructor can read the real strike + expiry per option position.
    """
    return options_chain.lookup_for_view(view, signals_out, broker=ctx.broker)


def stage_construct(
    ctx: StageContext,
    signals_out: dict,
    view: dict,
    chain_lookups: dict | None = None,
    current_positions: list[dict] | None = None,
    pnl_history: list[dict] | None = None,
    adaptive_cap_pct: float = 15.0,
) -> dict:
    """One LLM call — Opus 4.7, ~$0.20. Reads signals + view + chain
    lookups + current positions + recent PnL feedback; emits the final
    portfolio.json with positions, sizing, kill conditions."""
    if ctx.dry_run:
        out = _load_fixture("portfolio.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.constructor()
    nav = _account_nav(ctx)
    chain_lookups = chain_lookups or {"lookups": []}
    current_positions = current_positions or []
    pnl_history = pnl_history or []
    content = (
        f"Signals: {json.dumps(signals_out, sort_keys=True)}\n"
        f"Strategist view: {json.dumps(view, sort_keys=True)}\n"
        f"Chain lookups (real Alpaca contracts for option candidates): "
        f"{json.dumps(chain_lookups, sort_keys=True)}\n"
        f"Current broker positions: {json.dumps(current_positions, sort_keys=True)}\n"
        f"Recent PnL history (last cycles): {json.dumps(pnl_history, sort_keys=True)}\n"
        f"NAV (USD): {nav:.2f}\n"
        f"Adaptive per-position cap %: {adaptive_cap_pct:.2f} "
        f"(reduced from 15.0% when NAV is in drawdown)\n"
        f"Run id: {ctx.run_id}\n"
        "Return JSON conforming to portfolio.schema.json. Use the OSI "
        "symbols + strikes from chain_lookups for option positions; do "
        "NOT invent strikes. Prefer keeping current positions when the "
        "strategist's view is consistent with them."
    )
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{"role": "user", "content": content}],
        schema_filename=cfg.schema_filename,
        max_tokens=cfg.max_tokens,
        thinking=cfg.thinking,
        output_config=_output_config(cfg),
    ))
    return res.payload


def stage_critic(
    ctx: StageContext,
    view: dict,
    portfolio: dict,
) -> dict:
    """One LLM call — Sonnet 4.6 low effort, ~$0.03. Reads view +
    portfolio; returns {accept, critique, suggested_changes}.

    Dry-run returns a default-accept fixture so the pipeline doesn't
    require an LLM call in tests.
    """
    if ctx.dry_run:
        return {
            "accept": True,
            "critique": "dry-run: critic auto-accepts",
            "suggested_changes": [],
        }
    cfg = stages.critic()
    content = (
        f"View: {json.dumps(view, sort_keys=True)}\n"
        f"Portfolio: {json.dumps(portfolio, sort_keys=True)}\n"
        f"Run id: {ctx.run_id}\n"
        "Return JSON conforming to critique.schema.json."
    )
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{"role": "user", "content": content}],
        schema_filename=cfg.schema_filename,
        max_tokens=cfg.max_tokens,
        thinking=cfg.thinking,
        output_config=_output_config(cfg),
    ))
    return res.payload


def _parsed_virtual_nav_override() -> float | None:
    """Return VIRTUAL_NAV_USD as a float if the env var exists AND
    parses cleanly. Returns None if the var is absent OR malformed —
    callers use that to decide whether the resulting NAV is in
    virtual or broker units (Codex P1 on PR #76: stamping
    nav_source from env presence alone misclassified rows whenever
    the var was set but unparseable).
    """
    override = os.environ.get("VIRTUAL_NAV_USD")
    if not override:
        return None
    try:
        return float(override)
    except ValueError:
        return None


def _account_nav(ctx: StageContext) -> float:
    """$2.5k notional override unless VIRTUAL_NAV_USD set or broker
    reports a different equity figure. Same as v1."""
    parsed = _parsed_virtual_nav_override()
    if parsed is not None:
        return parsed
    if ctx.broker is not None:
        try:
            return ctx.broker.get_account().equity_usd
        except Exception:
            pass
    return 2500.0  # $2.5k paper baseline


def _broker_portfolio_summary_for_meta(ctx: StageContext) -> dict:
    """Compact account summary fed to the meta-scheduler when the cycle
    didn't produce a constructed portfolio (review path).

    Reads real broker holdings + equity + cash so the cadence + intent
    decision after a review reflects what we actually hold. Passing a
    flat placeholder ({positions: [], all_cash: True, nav: 0}) would
    push meta into the "all-cash, calm" 6-12h bucket exactly when an
    open option or risk-sensitive position actually needs near-term
    monitoring (Codex P1 on PR #83).

    Returns the shape `_compute_next_run_at` reads:
      {positions, all_cash, nav_usd, cash_buffer_pct}

    Defensive: broker errors fall back to all-cash + spec NAV. Dry-run
    is short-circuited upstream so this isn't exercised there.
    """
    positions = _current_positions_summary(ctx)
    nav = _account_nav(ctx)
    cash_usd = 0.0
    if ctx.broker is not None and not ctx.dry_run:
        try:
            cash_usd = ctx.broker.get_account().cash_usd
        except Exception:
            cash_usd = 0.0
    cash_pct = (cash_usd / nav * 100.0) if nav > 0 else 100.0
    return {
        "positions": positions,
        "all_cash": len(positions) == 0,
        "nav_usd": nav,
        "cash_buffer_pct": cash_pct,
    }


def _current_positions_summary(ctx: StageContext) -> list[dict]:
    """Compact broker-position summary, fed to strategist + constructor
    so they can reason about current state vs target.

    Each row: {symbol, qty, avg_cost, market_value, unrealized_pl_usd,
    asset_class}. Returns empty list on dry-run or broker error.
    """
    if ctx.broker is None or ctx.dry_run:
        return []
    try:
        positions = ctx.broker.get_positions()
    except Exception:
        return []
    rows = []
    for p in positions:
        rows.append({
            "symbol": p.symbol,
            "qty": p.qty,
            "avg_cost": p.avg_cost,
            "market_value": p.market_value,
            "unrealized_pl_usd": p.unrealized_pl_usd,
            "asset_class": p.asset_class,
        })
    return rows


def _recent_pnl_history(*, limit: int = 5) -> list[dict]:
    """Last N cycles' regime + portfolio + realized 4h PnL.

    Reads state/nav_history.jsonl in pairs to compute realized
    cycle-over-cycle NAV % change. Joins with the matching view.json
    in the run dir to get the regime classification per cycle.

    Returns oldest-first so the LLM reads a chronological tape.
    """
    rows = state.read_nav_history(limit=limit + 1)
    if len(rows) < 2:
        return []
    out: list[dict] = []
    for prev, curr in zip(rows, rows[1:]):
        prev_nav = prev.get("nav_usd") or 0.0
        curr_nav = curr.get("nav_usd") or 0.0
        if prev_nav <= 0:
            realized_pct = None
        else:
            realized_pct = round((curr_nav / prev_nav - 1.0) * 100.0, 4)
        rid = curr.get("run_id", "")
        regime = None
        view_path = state.RUNS_DIR / rid / "view.json"
        if view_path.exists():
            try:
                regime = json.loads(view_path.read_text()).get("regime")
            except Exception:
                regime = None
        out.append({
            "run_id": rid,
            "at": curr.get("at"),
            "regime": regime,
            "positions_count": curr.get("positions_count", 0),
            "all_cash": curr.get("all_cash", False),
            "realized_pnl_pct": realized_pct,
        })
    return out[-limit:]


def _signals_fingerprint(signals_out: dict) -> str:
    """Hash of the per-ticker feature payload used for cycle dedup.

    Fingerprints (a) numeric price/vol/MA features rounded to 4dp so
    yfinance recompute noise doesn't make every cycle look unique,
    AND (b) the set of upcoming macro events per ticker — Codex P1
    on PR #68 caught that fingerprinting only the numerics meant a
    new FOMC/CPI/NFP/PCE moving into the 7-day window wouldn't bump
    the hash, so dedup would skip strategist + construct *exactly*
    when new event risk appeared. Now any change in the event set
    (new event added, event date changed, event count shifted)
    invalidates the dedup and forces a fresh cycle.

    Excludes generated_at + run_id so the same data on two different
    cycles produces the same hash.
    """
    rows = []
    for t in signals_out.get("tickers", []):
        # Compact-but-stable representation of the macro event list:
        # sort by date so reordering doesn't spuriously change the hash.
        events_summary = sorted(
            ((e.get("date"), e.get("type")) for e in (t.get("upcoming_macro_events_7d") or [])),
            key=lambda p: (p[0] or "", p[1] or ""),
        )
        rows.append({
            "sym": t.get("symbol"),
            "last_close": round(t.get("last_close") or 0.0, 4),
            "mom30": round(t.get("momentum_30d_pct") or 0.0, 2),
            "mom60": round(t.get("momentum_60d_pct") or 0.0, 2),
            "hv30": round(t.get("hv_30d_annualised") or 0.0, 4),
            "hv90": round(t.get("hv_90d_annualised") or 0.0, 4),
            "d50": round(t.get("dist_from_50d_ma_pct") or 0.0, 2),
            "d200": round(t.get("dist_from_200d_ma_pct") or 0.0, 2),
            "events": events_summary,
        })
    rows.sort(key=lambda r: r["sym"] or "")
    return _hash_inputs(json.dumps(rows, sort_keys=True))


def _positions_fingerprint(positions: list[dict]) -> str:
    """Stable hash of the current-positions set, used as the secondary
    dedup key. If positions changed (manual close, kill_condition
    flatten, prior fill), the dedup must NOT skip — the agent should
    re-evaluate."""
    rows = sorted(
        ({"sym": p.get("symbol"), "qty": p.get("qty")} for p in positions),
        key=lambda r: r["sym"] or "",
    )
    return _hash_inputs(json.dumps(rows, sort_keys=True))


def _check_cycle_dedup(signals_out: dict, current_positions: list[dict]) -> dict | None:
    """Return the cached portfolio dict if dedup applies; None otherwise.

    Dedup applies when:
      - state/last_cycle_hash.json exists
      - signals_fingerprint matches the prior cycle's
      - positions_fingerprint matches the prior cycle's
      - state/current_portfolio.json exists (the cached portfolio
        to reuse)
    """
    if not state.LAST_CYCLE_HASH.exists() or not state.CURRENT_PORTFOLIO.exists():
        return None
    try:
        last = json.loads(state.LAST_CYCLE_HASH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    signals_fp = _signals_fingerprint(signals_out)
    positions_fp = _positions_fingerprint(current_positions)
    if (
        last.get("signals_fingerprint") != signals_fp
        or last.get("positions_fingerprint") != positions_fp
    ):
        return None
    try:
        portfolio = json.loads(state.CURRENT_PORTFOLIO.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return {"portfolio": portfolio}


def _write_dedup_next_run(rid: str, *, portfolio: dict, ctx: StageContext) -> dict:
    """Build + persist a minimal next_run.json on a dedup-skipped cycle.

    Dedup only fires on trade cycles (review path skips dedup entirely),
    so cycle_intent on the persisted next_run is "trade".
    """
    next_at = _default_next_run_at(portfolio)
    next_run = {
        "run_id": rid,
        "next_run_at": next_at,
        "rationale": (
            "cycle dedup: signals fingerprint and broker positions both "
            "unchanged from prior cycle. Skipped strategist + "
            "construct + execute; cached portfolio retained."
        ),
        "dedup_skipped": True,
        "cycle_intent": "trade",
    }
    state.write_json(state.run_dir(rid) / "next_run.json", next_run)
    state.write_json(state.NEXT_RUN, next_run)
    # Log a decision row so the dashboard timeline shows the skip.
    state.append_decision({
        "run_id": rid,
        "stage": "signals",
        "model": "local-deterministic",
        "inputs_hash": _hash_inputs(rid),
        "output_ref": "signals.json",
        "prompt_cache_hit_pct": 0.0,
        "cost_usd": 0.0,
        "started_at": state.utcnow_iso(),
        "ended_at": state.utcnow_iso(),
        "status": "skipped",
        "risk_warning": RISK_WARNING,
        "cycle_intent": ctx.cycle_intent,
        "intent_source": ctx.intent_source,
    })
    return next_run


def _update_cycle_dedup_hash(signals_out: dict, current_positions: list[dict]) -> None:
    """Called at the END of a successful cycle to record the fingerprints
    for the NEXT cycle's dedup check. Failures here are non-fatal."""
    try:
        state.write_json(state.LAST_CYCLE_HASH, {
            "signals_fingerprint": _signals_fingerprint(signals_out),
            "positions_fingerprint": _positions_fingerprint(current_positions),
            "updated_at": state.utcnow_iso(),
        })
    except Exception:
        pass


def _peak_nav_30d() -> float:
    """Highest NAV observed in the last 30 calendar days from
    state.read_nav_history. Used by risk.adaptive_position_cap_pct
    to dial size down in drawdown.

    Codex P2 on PR #69: an earlier version used ``limit=180`` (≈30d
    at 6 cycles/day) as a proxy for "last 30 days." But the orchestrator
    cadence floor is 1h (META_MIN_HOURS), so in faster regimes 180
    rows could cover only ~7.5 days. The drawdown-adaptive cap would
    then "forget" earlier peaks and lift size limits too soon — the
    opposite of what the protection was meant to do.

    Fix: filter rows by the ``at`` timestamp against a 30-day-ago cutoff
    rather than relying on row count. Pull a generous slab (1000 rows)
    to absorb sub-1h cadence experiments; the math still operates on
    a clean 30-day window.
    """
    from datetime import datetime, timedelta, timezone

    rows = state.read_nav_history(limit=1000)
    if not rows:
        return 0.0
    cutoff = utcnow_aware = state.utcnow()
    cutoff = cutoff - timedelta(days=30)

    peak = 0.0
    for r in rows:
        nav = r.get("nav_usd") or 0.0
        if nav <= 0:
            continue
        at_str = r.get("at") or ""
        try:
            at = datetime.strptime(at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Malformed timestamp — include the row defensively so a
            # bad write doesn't accidentally lift the cap.
            peak = max(peak, nav)
            continue
        if at >= cutoff:
            peak = max(peak, nav)
    return peak


def _default_next_run_at(portfolio: dict) -> str:
    """Heuristic fallback cadence — used when the meta LLM output is
    unusable or the path is dry-run.
      - all-cash: 6 hours (no urgency — just sample the universe again)
      - positions held: 4 hours (faster, to monitor kill conditions)

    Market-gate handles weekend/holiday skipping upstream; this default
    is only the "open and operating normally" floor.
    """
    from datetime import timedelta
    hours = 6 if portfolio.get("all_cash") else 4
    return (state.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Orchestrator-meta returns a next-run timestamp; bounds enforced here.
META_MIN_HOURS = 1.0
META_MAX_HOURS = 24.0
# Tolerance absorbs second-precision rounding + LLM round-trip latency.
META_BOUND_TOLERANCE_SECONDS = 30.0


def _compute_next_run_at(
    *, ctx: StageContext, portfolio: dict, view: dict,
) -> tuple[str, str, str]:
    """Ask the orchestrator-meta agent for a regime-adaptive cadence.

    Returns (next_run_at_iso, rationale, cycle_intent_for_next). On any
    failure falls back to `_default_next_run_at(portfolio)` with an
    explanatory rationale and `cycle_intent="trade"` so safety defaults
    to the full pipeline when the meta call can't be trusted.

    `cycle_intent_for_next` ∈ {"trade","review"} tells the NEXT cycle
    which branch to take. The meta call gets market-clock state +
    today's review count + per-day cap as context so it can pick
    "review" only at sensible times (post-close reflection) and only
    when budget remains.
    """
    if ctx.dry_run:
        return _default_next_run_at(portfolio), "dry-run: heuristic only", "trade"

    from datetime import datetime, timezone
    cfg = stages.orchestrator_meta()
    now = state.utcnow()
    nav_history = state.read_nav_history(limit=3)

    # Market-clock + review-budget context for the meta-scheduler.
    market_is_open = True
    next_open = None
    if ctx.broker is not None:
        try:
            clock = ctx.broker.get_clock()
            if clock is not None:
                market_is_open = bool(clock.is_open)
                next_open = clock.next_open or None
        except Exception:
            pass
    reviews_today = _count_autonomous_reviews_today()
    review_cap = _max_review_cycles_per_day()
    review_budget_remaining = max(0, review_cap - reviews_today)

    user_msg = {
        "role": "user",
        "content": (
            f"Current UTC: {state.utcnow_iso()}\n"
            f"Market clock: is_open={market_is_open}, next_open={next_open}\n"
            f"Today's autonomous review cycles: {reviews_today}/{review_cap} "
            f"(remaining budget: {review_budget_remaining})\n"
            f"Portfolio summary:\n"
            f"  positions: {len(portfolio.get('positions', []))}\n"
            f"  all_cash: {portfolio.get('all_cash', False)}\n"
            f"  nav_usd: {portfolio.get('nav_usd', 0.0):.2f}\n"
            f"  cash_buffer_pct: {portfolio.get('cash_buffer_pct', 0.0):.1f}\n"
            f"Strategist regime: {view.get('regime', 'unknown')}\n"
            f"Recent NAV history (last {len(nav_history)} rows):\n"
            f"  {json.dumps(nav_history, separators=(',', ':'))}\n\n"
            "Choose the next-run window AND the cycle_intent for it. "
            "review = signals + strategist only, no orders, ~$0.05 cost; "
            "use for post-close reflection. trade = full pipeline. "
            "Return JSON only."
        ),
    }
    try:
        res = llm.structured_call(llm.StageCall(
            run_id=ctx.run_id,
            stage=cfg.stage,
            model=cfg.model,
            system_blocks=_system_blocks(cfg),
            user_messages=[user_msg],
            schema_filename=None,
            max_tokens=cfg.max_tokens,
            thinking=cfg.thinking,
            output_config=_output_config(cfg),
        ))
        payload = json.loads(llm.strip_markdown_fences(res.raw_text))
    except Exception as e:
        return _default_next_run_at(portfolio), f"meta call failed ({type(e).__name__}); using heuristic", "trade"

    try:
        at = datetime.strptime(payload["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return _default_next_run_at(portfolio), "meta returned malformed next_run_at; using heuristic", "trade"

    delta_seconds = (at - now).total_seconds()
    min_seconds = META_MIN_HOURS * 3600 - META_BOUND_TOLERANCE_SECONDS
    max_seconds = META_MAX_HOURS * 3600 + META_BOUND_TOLERANCE_SECONDS
    if delta_seconds < min_seconds or delta_seconds > max_seconds:
        return _default_next_run_at(portfolio), (
            f"meta returned out-of-bounds cadence ({delta_seconds/3600:.2f}h, "
            f"allowed {META_MIN_HOURS}-{META_MAX_HOURS}h ±{META_BOUND_TOLERANCE_SECONDS:g}s); "
            f"using heuristic"
        ), "trade"

    # Pull cycle_intent from the meta output. Defaults to "trade" when
    # missing or invalid — safer to run the full pipeline than to
    # accidentally skip orders. If the meta would burn the cap, fall
    # back to "trade" with an explanatory note in the rationale.
    next_intent = payload.get("cycle_intent")
    if next_intent not in ("trade", "review"):
        next_intent = "trade"
    if next_intent == "review" and review_budget_remaining <= 0:
        next_intent = "trade"
        rationale_suffix = " (meta picked review but daily cap exhausted; downgraded to trade)"
    else:
        rationale_suffix = ""
    rationale = (payload.get("rationale") or "")[:300] + rationale_suffix
    return payload["next_run_at"], f"orchestrator-meta: {rationale}", next_intent


def stage_execute(ctx: StageContext, portfolio: dict, view: dict | None = None) -> dict:
    """Submit paper orders to converge actual positions on `portfolio`,
    then plan the next run. Order submission is a no-op when broker is
    None or ORDERS_ENABLED is false.

    The order-delta computation in lib.orders enforces the v2 safety
    invariant: orders never cross zero (no long→short flips via a
    single sell). Closes are submitted before opens to free up cash.
    """
    view = view or {"candidates": []}
    next_at, meta_rationale, next_intent = _compute_next_run_at(
        ctx=ctx, portfolio=portfolio, view=view,
    )
    next_run = {
        "run_id": ctx.run_id,
        "next_run_at": next_at,
        "rationale": meta_rationale,
        "cycle_intent": next_intent,
    }
    from lib import orders
    next_run["orders_enabled"] = orders.is_enabled()
    if (
        ctx.broker is not None
        and not ctx.dry_run
        and orders.is_enabled()
    ):
        try:
            current = ctx.broker.get_positions()
        except Exception as e:
            next_run["order_plan_error"] = f"get_positions: {type(e).__name__}: {e}"
            current = []
        plan = orders.diff_portfolio(portfolio, current)
        results = orders.submit_plan(plan, broker=ctx.broker)
        next_run["order_plan"] = {
            "total_legs": plan.total_legs,
            "closes": len(plan.closes),
            "opens": len(plan.requests),
            "options_skipped": len(plan.skipped),
            "results": [
                {
                    "symbol": r.symbol,
                    "qty": r.qty,
                    "side": r.side,
                    "status": r.status,
                    "broker_order_id": r.broker_order_id,
                }
                for r in results
            ],
        }
        accepted_order_ids = [
            r.broker_order_id for r in results
            if r.broker_order_id and not r.status.startswith(("error", "skipped"))
        ]
        state.write_json(
            state.run_dir(ctx.run_id) / "orders.json",
            {
                "run_id": ctx.run_id,
                "submitted_at": state.utcnow_iso(),
                "order_ids": accepted_order_ids,
            },
        )

        # Pull fills + fees back from Alpaca and append to trades.jsonl so
        # the dashboard's per-trade PnL + fees chart reflect actual broker
        # activity. Run EVERY cycle that reaches stage_execute (idempotent
        # via known_ids dedupe).
        try:
            from lib import trades_sync
            trades_sync.sync_fills_from_alpaca(
                trading_client=getattr(ctx.broker, "_client", None),
                order_id_to_run_id=trades_sync.order_id_to_run_id_from_runs(),
            )
        except Exception as e:
            next_run["trades_sync_error"] = (
                f"sync_fills_from_alpaca: {type(e).__name__}: {e}"
            )
    if not ctx.dry_run:
        state.write_json(state.NEXT_RUN, next_run)
        # NAV history: one row per cycle for the dashboard equity curve.
        # Marks aren't wired here — gross/net P&L includes the modelled-
        # cost entry-leg estimate only. Real marks come through the
        # broker-position path in lib/marks.py.
        #
        # `nav_source` records whether nav_usd is in raw broker units
        # or virtual (VIRTUAL_NAV_USD-overridden) units. The dashboard's
        # NAV anchor offset only applies to broker-unit rows; without
        # this tag a row written under VIRTUAL_NAV_USD=2500 would get
        # the broker offset subtracted again and land at ~-$95k.
        #
        # Derive the tag from the SAME parsing logic _account_nav uses
        # (Codex P1 on PR #76): a malformed env var like
        # VIRTUAL_NAV_USD="not-a-number" falls through to broker
        # equity, so the row is broker-units despite the var existing.
        from lib import pnl as pnl_lib
        breakdown = pnl_lib.compute_portfolio_pnl(portfolio=portfolio, marks=None)
        nav_source = "virtual" if _parsed_virtual_nav_override() is not None else "broker"
        state.append_nav({
            "run_id": ctx.run_id,
            "at": state.utcnow_iso(),
            "nav_usd": portfolio.get("nav_usd", 0.0),
            "nav_source": nav_source,
            "cash_usd": portfolio.get("cash_usd", 0.0),
            "positions_count": len(portfolio.get("positions", [])),
            "all_cash": portfolio.get("all_cash", False),
            "gross_pnl_usd": breakdown.gross_pnl_usd,
            "modelled_costs_usd": breakdown.modelled_costs_usd,
            "net_pnl_usd": breakdown.net_pnl_usd,
        })
    return next_run


# ----- pipeline driver -----


def _run_stage(
    *,
    ctx: StageContext,
    stage_id: str,
    schema: str,
    output_filename: str,
    runner: Callable[[], dict],
    inputs_hash_parts: tuple[str, ...],
    model: str = "stub",
) -> dict:
    if state.is_halted():
        raise llm.HaltFlagSet(f"halt.flag set before stage={stage_id}")

    started_at = state.utcnow_iso()
    output = runner()
    if schema:
        state.validate(output, schema)

    out_path = state.run_dir(ctx.run_id) / output_filename
    state.write_json(out_path, output)

    state.append_decision({
        "run_id": ctx.run_id,
        "stage": stage_id,
        "model": model,
        "inputs_hash": _hash_inputs(*inputs_hash_parts),
        "output_ref": output_filename,
        "prompt_cache_hit_pct": 0.0,
        "cost_usd": 0.0,
        "started_at": started_at,
        "ended_at": state.utcnow_iso(),
        "status": "ok",
        "risk_warning": RISK_WARNING,
        "cycle_intent": ctx.cycle_intent,
        "intent_source": ctx.intent_source,
    })
    return output


def _run_pipeline_review(*, ctx: StageContext, dry_run: bool) -> dict:
    """Review-only pipeline branch: signals + strategist + meta only.

    No market gate (the whole point: review runs after close). No
    construct, no critic, no sanity, no execute → no orders, ever. The
    strategist's output goes to ``review.json`` (not view.json) so it
    doesn't pollute ``_recent_pnl_history``'s trade-cycle drift loop.

    Dedup hash + NAV history + current_portfolio.json are NOT updated
    on a review cycle — only trade cycles record cycle-over-cycle
    state. This keeps a review followed by an unchanged-signals trade
    cycle from dedup-skipping into a stale portfolio.

    Cost: ~$0.05 (strategist + meta).
    """
    rid = ctx.run_id

    # ----- Stage 1: signals (deterministic, $0) -----
    signals_out = _run_stage(
        ctx=ctx, stage_id="signals", schema="signals.schema.json",
        output_filename="signals.json",
        runner=lambda: stage_signals(ctx),
        inputs_hash_parts=(rid,),
        model="local-deterministic",
    )

    # Pull current positions + PnL history so the strategist's regime
    # commentary is informed by what we currently hold.
    current_positions = _current_positions_summary(ctx)
    pnl_history = _recent_pnl_history(limit=5)

    # ----- Stage 2: strategist → review.json (not view.json) -----
    # Same prompt + schema as trade-cycle strategist; the artifact
    # filename is what differs. Reading `_recent_pnl_history` walks
    # view.json only, so writing review.json keeps the trade-cycle
    # drift-correction loop free of after-hours reflection regimes.
    strat_model = "fixture" if dry_run else stages.strategist().model
    review_payload = _run_stage(
        ctx=ctx, stage_id="strategist", schema="view.schema.json",
        output_filename="review.json",
        runner=lambda: stage_strategist(
            ctx, signals_out,
            current_positions=current_positions,
            pnl_history=pnl_history,
        ),
        inputs_hash_parts=(rid, json.dumps(signals_out, sort_keys=True)),
        model=strat_model,
    )

    # ----- Meta-scheduler: pick next-run window + intent for next cycle.
    # Feed real broker holdings (not a flat placeholder) so meta's
    # cadence + intent decision reflects what we actually hold. A
    # placeholder would push meta into the "all-cash" bucket exactly
    # when a held option / risk-sensitive position needs near-term
    # monitoring after the reflection cycle.
    next_at, meta_rationale, next_intent = _compute_next_run_at(
        ctx=ctx,
        portfolio=_broker_portfolio_summary_for_meta(ctx),
        view=review_payload,
    )
    next_run = {
        "run_id": rid,
        "next_run_at": next_at,
        "rationale": meta_rationale,
        "cycle_intent": next_intent,
        "review_completed": True,
    }
    state.write_json(state.run_dir(rid) / "next_run.json", next_run)
    if not dry_run:
        state.write_json(state.NEXT_RUN, next_run)

    # Synthetic ``review_complete`` decision row marks the end of a
    # review cycle. The frequency-cap counter looks for this stage so
    # each cycle is counted exactly once regardless of how many sub-
    # stage rows the review wrote.
    state.append_decision({
        "run_id": rid,
        "stage": "review_complete",
        "model": "local-deterministic",
        "inputs_hash": _hash_inputs(rid),
        "output_ref": "review.json",
        "prompt_cache_hit_pct": 0.0,
        "cost_usd": 0.0,
        "started_at": state.utcnow_iso(),
        "ended_at": state.utcnow_iso(),
        "status": "ok",
        "risk_warning": RISK_WARNING,
        "cycle_intent": ctx.cycle_intent,
        "intent_source": ctx.intent_source,
    })

    return {
        "run_id": rid,
        "cycle_intent": "review",
        "intent_source": ctx.intent_source,
        "signals": signals_out,
        "review": review_payload,
        "next_run": next_run,
    }


def run_pipeline(
    *,
    dry_run: bool,
    run_id: str | None = None,
    broker: Broker | None = None,
    cli_intent: str | None = None,
    ignore_cap: bool = False,
) -> dict:
    """Run one orchestrator cycle.

    cli_intent: explicit operator override. None falls back to env
    (CYCLE_INTENT) → prior next_run.json → "trade".
    ignore_cap: bypass the daily review-cap check. Only honoured when
    cli_intent is set (CLI is the only path that can grant this).
    """
    if state.is_halted():
        raise llm.HaltFlagSet("halt.flag is set; refusing to start orchestrator run")

    rid = run_id or state.new_run_id()
    cycle_intent, intent_source = _load_cycle_intent(
        cli_intent=cli_intent, ignore_cap=ignore_cap,
    )

    # ignore_cap is an operator-only override. The docstring promises it
    # only applies to CLI-driven reviews; tighten the gate here so that
    # `--ignore-cap` without `--intent=review` cannot bypass the cap on
    # a file-driven (autonomous) review pick (Codex P2 on PR #83).
    effective_ignore_cap = ignore_cap and intent_source == "cli"

    # Frequency cap: if meta-scheduler picked review but the daily cap
    # is hit, skip + advance next_run. Only intent_source="file" rows
    # count (autonomous cycles); operator-driven cli/env intents bypass.
    if cycle_intent == "review" and intent_source == "file" and not effective_ignore_cap:
        cap = _max_review_cycles_per_day()
        today_count = _count_autonomous_reviews_today()
        if today_count >= cap:
            next_at = _next_run_at_after_review_cap(broker)
            next_run = {
                "run_id": rid,
                "next_run_at": next_at,
                "rationale": (
                    f"review cycle skipped: daily cap reached "
                    f"({today_count}/{cap}). Next cycle defaults to trade. "
                    "Operator override: rerun with --intent=review --ignore-cap."
                ),
                "cycle_intent": "trade",
                "review_cap_skipped": True,
            }
            state.write_json(state.run_dir(rid) / "next_run.json", next_run)
            state.write_json(state.NEXT_RUN, next_run)
            state.append_decision({
                "run_id": rid,
                "stage": "review_complete",
                "model": "local-deterministic",
                "inputs_hash": _hash_inputs(rid),
                "output_ref": "next_run.json",
                "prompt_cache_hit_pct": 0.0,
                "cost_usd": 0.0,
                "started_at": state.utcnow_iso(),
                "ended_at": state.utcnow_iso(),
                "status": "skipped_review_cap",
                "risk_warning": RISK_WARNING,
                "cycle_intent": cycle_intent,
                "intent_source": intent_source,
            })
            return {
                "run_id": rid,
                "cycle_intent": "review",
                "review_cap_skipped": True,
                "next_run": next_run,
            }

    ctx = StageContext(
        run_id=rid,
        dry_run=dry_run,
        broker=broker,
        cycle_intent=cycle_intent,
        intent_source=intent_source,
    )

    if cycle_intent == "review":
        return _run_pipeline_review(ctx=ctx, dry_run=dry_run)

    # ----- Stage 0: market gate -----
    # Dry-run skips the gate (no broker); live mode calls Alpaca's clock.
    # If markets are closed we write market_gate.json + a closed-market
    # next_run.json pointing at the broker-reported next-open, then exit.
    # Zero LLM cost on a closed-market cycle.
    if not dry_run:
        ms = market_gate.check(broker)
        if not ms.is_open:
            nr = market_gate.write_closed_artifacts(rid, ms)
            state.write_json(state.NEXT_RUN, nr)
            state.append_decision({
                "run_id": rid,
                "stage": "market_gate",
                "model": "local-deterministic",
                "inputs_hash": _hash_inputs(rid),
                "output_ref": "market_gate.json",
                "prompt_cache_hit_pct": 0.0,
                "cost_usd": 0.0,
                "started_at": state.utcnow_iso(),
                "ended_at": state.utcnow_iso(),
                "status": "skipped_market_closed",
                "risk_warning": RISK_WARNING,
                "cycle_intent": ctx.cycle_intent,
                "intent_source": ctx.intent_source,
            })
            return {
                "run_id": rid,
                "market_gate": {"is_open": False, "next_open": ms.next_open},
                "next_run": nr,
            }
        # Market is open: persist the gate result for the dashboard.
        state.write_json(state.run_dir(rid) / "market_gate.json", {
            "run_id": rid,
            "generated_at": state.utcnow_iso(),
            "is_open": True,
            "next_open": None,
            "rationale": ms.rationale,
        })

    strat_model = "fixture" if dry_run else stages.strategist().model
    cons_model = "fixture" if dry_run else stages.constructor().model

    # ----- Stage 1: signals (deterministic) -----
    signals_out = _run_stage(
        ctx=ctx, stage_id="signals", schema="signals.schema.json",
        output_filename="signals.json",
        runner=lambda: stage_signals(ctx),
        inputs_hash_parts=(rid,),
        model="local-deterministic",
    )

    # Current broker positions — passed to strategist + constructor as
    # state context so they can bias toward holding existing winners
    # vs churning the portfolio. Live only; dry-run uses empty list.
    current_positions = _current_positions_summary(ctx)

    # ----- Cycle dedup -----
    # If the signals fingerprint matches the prior cycle AND the broker
    # position set is unchanged, skip strategist + construct + execute
    # and reuse the last portfolio. Saves ~$0.25 on a quiet 4h window
    # where the market hasn't moved meaningfully.
    if not dry_run:
        dedup = _check_cycle_dedup(signals_out, current_positions)
        if dedup is not None:
            next_run = _write_dedup_next_run(rid, portfolio=dedup["portfolio"], ctx=ctx)
            return {
                "run_id": rid,
                "signals": signals_out,
                "dedup_skipped": True,
                "next_run": next_run,
            }

    # Recent PnL feedback — last 5 cycles' regime + realized 4h PnL
    # so the strategist can self-correct drift across cycles.
    pnl_history = _recent_pnl_history(limit=5)

    # ----- Stage 2: strategist (1 LLM call) -----
    view = _run_stage(
        ctx=ctx, stage_id="strategist", schema="view.schema.json",
        output_filename="view.json",
        runner=lambda: stage_strategist(
            ctx, signals_out,
            current_positions=current_positions,
            pnl_history=pnl_history,
        ),
        inputs_hash_parts=(rid, json.dumps(signals_out, sort_keys=True)),
        model=strat_model,
    )

    # ----- Stage 2.5: chain lookup (deterministic, $0) -----
    # Resolve every option candidate to a real tradable Alpaca contract
    # so the constructor doesn't invent OSI symbols.
    chain_lookups = _run_stage(
        ctx=ctx, stage_id="chain_lookup", schema="chain_lookups.schema.json",
        output_filename="chain_lookups.json",
        runner=lambda: stage_chain_lookup(ctx, signals_out, view),
        inputs_hash_parts=(rid, json.dumps(view, sort_keys=True)),
        model="local-deterministic",
    )

    # Adaptive position-pct cap from current drawdown — feeds the
    # constructor prompt as a soft ceiling. Real enforcement is in
    # sanity (rule: position_within_adaptive_cap).
    adaptive_cap = risk.adaptive_position_cap_pct(
        current_nav=_account_nav(ctx),
        peak_nav_30d=_peak_nav_30d(),
    )

    # ----- Stage 3: construct (1 LLM call) -----
    portfolio = _run_stage(
        ctx=ctx, stage_id="construct", schema="portfolio.schema.json",
        output_filename="portfolio.json",
        runner=lambda: stage_construct(
            ctx, signals_out, view,
            chain_lookups=chain_lookups,
            current_positions=current_positions,
            pnl_history=pnl_history,
            adaptive_cap_pct=adaptive_cap,
        ),
        inputs_hash_parts=(
            rid,
            json.dumps(signals_out, sort_keys=True),
            json.dumps(view, sort_keys=True),
            json.dumps(chain_lookups, sort_keys=True),
        ),
        model=cons_model,
    )
    if not risk.position_band_ok(len(portfolio["positions"]), portfolio["all_cash"]):
        raise RuntimeError("portfolio violates 1–12 band / all-cash invariant")

    # ----- Stage 3.5: critic (1 LLM call, ~$0.03) -----
    # Adversarial review of the constructor's portfolio. If accept=true
    # proceed to sanity. If accept=false, retry the constructor ONCE
    # with the critique fed back; the retry's portfolio is then used.
    critique = _run_stage(
        ctx=ctx, stage_id="critic", schema="critique.schema.json",
        output_filename="critique.json",
        runner=lambda: stage_critic(ctx, view, portfolio),
        inputs_hash_parts=(
            rid,
            json.dumps(view, sort_keys=True),
            json.dumps(portfolio, sort_keys=True),
        ),
        model="fixture" if dry_run else stages.critic().model,
    )
    if not critique.get("accept", True) and not dry_run:
        # Retry constructor with the critique appended as user context.
        def _retry_with_critique() -> dict:
            cfg = stages.constructor()
            content = (
                f"Signals: {json.dumps(signals_out, sort_keys=True)}\n"
                f"Strategist view: {json.dumps(view, sort_keys=True)}\n"
                f"Chain lookups: {json.dumps(chain_lookups, sort_keys=True)}\n"
                f"Current broker positions: {json.dumps(current_positions, sort_keys=True)}\n"
                f"Recent PnL history: {json.dumps(pnl_history, sort_keys=True)}\n"
                f"NAV (USD): {_account_nav(ctx):.2f}\n"
                f"Adaptive per-position cap %: {adaptive_cap:.2f}\n"
                f"Run id: {ctx.run_id}\n"
                f"Critic rejected your first attempt: {critique.get('critique')}. "
                f"Suggested changes: {json.dumps(critique.get('suggested_changes', []))}. "
                "Address the critique and return a revised portfolio. "
                "JSON only, portfolio.schema.json."
            )
            res = llm.structured_call(llm.StageCall(
                run_id=ctx.run_id,
                stage=cfg.stage,
                model=cfg.model,
                system_blocks=_system_blocks(cfg),
                user_messages=[{"role": "user", "content": content}],
                schema_filename=cfg.schema_filename,
                max_tokens=cfg.max_tokens,
                thinking=cfg.thinking,
                output_config=_output_config(cfg),
            ))
            return res.payload

        portfolio = _run_stage(
            ctx=ctx, stage_id="construct", schema="portfolio.schema.json",
            output_filename="portfolio.json",
            runner=_retry_with_critique,
            inputs_hash_parts=(
                rid,
                json.dumps(view, sort_keys=True),
                json.dumps(critique, sort_keys=True),
            ),
            model=cons_model,
        )
        if not risk.position_band_ok(len(portfolio["positions"]), portfolio["all_cash"]):
            raise RuntimeError("retry-portfolio violates 1–12 band / all-cash invariant")

    # ----- Stage 4: sanity (deterministic) -----
    sanity_report = sanity.run_sanity_checks(
        portfolio, view,
        signals=signals_out,
        nav_usd=_account_nav(ctx),
        adaptive_cap_pct=adaptive_cap,
    )
    sanity_report["run_id"] = rid
    sanity_report["generated_at"] = state.utcnow_iso()
    state.write_json(
        state.run_dir(rid) / "sanity.json", sanity_report, schema="sanity.schema.json",
    )
    sanity_blocked = (
        sanity.block_on_fail_enabled() and sanity_report["status"] == "fail"
    )

    if sanity_blocked:
        next_run = {
            "run_id": rid,
            "next_run_at": _default_next_run_at(portfolio),
            "rationale": (
                "stage_execute skipped: SANITY_BLOCK_ON_FAIL=true and sanity "
                f"report status=fail ({sanity_report['summary']['fail']} rule "
                "failure(s)). Cadence preserved via heuristic so the scheduler "
                "keeps firing; see sanity.json for offender details."
            ),
            "sanity_block": {
                "status": sanity_report["status"],
                "failed_rules": [
                    r["name"] for r in sanity_report["rules"] if r["status"] == "fail"
                ],
            },
            "cycle_intent": "trade",
        }
        state.write_json(state.run_dir(rid) / "next_run.json", next_run)
        if not dry_run:
            state.write_json(state.NEXT_RUN, next_run)
    else:
        # ----- Stage 5: execute (broker submission + meta scheduling) -----
        next_run = _run_stage(
            ctx=ctx, stage_id="execute", schema="", output_filename="next_run.json",
            runner=lambda: stage_execute(ctx, portfolio, view),
            inputs_hash_parts=(rid, json.dumps(portfolio, sort_keys=True)),
            model="local",
        )
        next_run["sanity"] = {
            "status": sanity_report["status"],
            "summary": sanity_report["summary"],
        }
        state.write_json(state.run_dir(rid) / "next_run.json", next_run)
        if not dry_run:
            state.write_json(state.NEXT_RUN, next_run)

    if not dry_run:
        state.write_json(state.CURRENT_PORTFOLIO, portfolio)
        # Update the cycle-dedup fingerprints so the next cycle can
        # short-circuit cleanly if nothing material changed.
        _update_cycle_dedup_hash(signals_out, current_positions)

    return {
        "run_id": rid,
        "signals": signals_out,
        "view": view,
        "portfolio": portfolio,
        "sanity": sanity_report,
        "next_run": next_run,
    }


def _try_load_broker() -> Broker | None:
    """Best-effort AlpacaBroker construction. Returns None if creds are
    missing or the SDK isn't installed — orchestrator still runs (writes
    portfolio.json, decision_log, next_run.json) but stage_execute can't
    submit orders without a broker.
    """
    try:
        from lib.alpaca_client import AlpacaBroker
        return AlpacaBroker()
    except Exception as e:
        print(
            f"broker unavailable ({type(e).__name__}: {e}); "
            f"stage_execute will skip order submission",
            file=sys.stderr,
        )
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v2 paper-trading orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="No orders, no LLM calls — fixture mode")
    parser.add_argument("--run-id", default=None, help="Override generated run_id")
    parser.add_argument(
        "--intent", choices=["trade", "review"], default=None,
        help=(
            "Force cycle intent. trade = full pipeline (default). "
            "review = signals + strategist + meta only, no orders. "
            "Without this flag, intent is read from prior next_run.json "
            "(which the meta-scheduler set on the previous cycle)."
        ),
    )
    parser.add_argument(
        "--ignore-cap", action="store_true",
        help=(
            "Bypass the daily review-frequency cap. Honoured only when "
            "--intent=review is also passed; never available to "
            "autonomous (next_run.json-driven) cycles."
        ),
    )
    args = parser.parse_args(argv)

    if (
        os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"
        and LIVE_VERSION == 0
    ):
        print("LIVE_TRADING_ENABLED=true but LIVE_VERSION=0 — refusing to run.", file=sys.stderr)
        return 2

    broker = None if args.dry_run else _try_load_broker()

    t0 = time.time()
    result = run_pipeline(
        dry_run=args.dry_run,
        run_id=args.run_id,
        broker=broker,
        cli_intent=args.intent,
        ignore_cap=args.ignore_cap,
    )
    dt = time.time() - t0
    stage_count = 6 if result.get("market_gate", {}).get("is_open", True) else 1
    intent_tag = f" intent={result.get('cycle_intent', 'trade')}" if result.get("cycle_intent") else ""
    print(f"run_id={result['run_id']} stages={stage_count} elapsed={dt:.2f}s dry_run={args.dry_run}{intent_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
