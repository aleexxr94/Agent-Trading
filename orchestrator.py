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

from lib import llm, market_gate, risk, sanity, signals, stages, state
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


def stage_strategist(ctx: StageContext, signals_out: dict) -> dict:
    """One LLM call — Sonnet 4.6, ~$0.05. Reads signals.json, emits
    view.json with regime classification + 0-6 ranked candidates."""
    if ctx.dry_run:
        out = _load_fixture("view.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.strategist()
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{
            "role": "user",
            "content": (
                f"Signals: {json.dumps(signals_out, sort_keys=True)}\n"
                f"Run id: {ctx.run_id}\n"
                "Return JSON conforming to view.schema.json."
            ),
        }],
        schema_filename=cfg.schema_filename,
        max_tokens=cfg.max_tokens,
        thinking=cfg.thinking,
        output_config=_output_config(cfg),
    ))
    return res.payload


def stage_construct(ctx: StageContext, signals_out: dict, view: dict) -> dict:
    """One LLM call — Opus 4.7, ~$0.20. Reads signals + view; emits the
    final portfolio.json with positions, sizing, kill conditions."""
    if ctx.dry_run:
        out = _load_fixture("portfolio.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.constructor()
    nav = _account_nav(ctx)
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{
            "role": "user",
            "content": (
                f"Signals: {json.dumps(signals_out, sort_keys=True)}\n"
                f"Strategist view: {json.dumps(view, sort_keys=True)}\n"
                f"NAV (USD): {nav:.2f}\n"
                f"Run id: {ctx.run_id}\n"
                "Return JSON conforming to portfolio.schema.json."
            ),
        }],
        schema_filename=cfg.schema_filename,
        max_tokens=cfg.max_tokens,
        thinking=cfg.thinking,
        output_config=_output_config(cfg),
    ))
    return res.payload


def _account_nav(ctx: StageContext) -> float:
    """$2.5k notional override unless VIRTUAL_NAV_USD set or broker
    reports a different equity figure. Same as v1."""
    override = os.environ.get("VIRTUAL_NAV_USD")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    if ctx.broker is not None:
        try:
            return ctx.broker.get_account().equity_usd
        except Exception:
            pass
    return 2500.0  # $2.5k paper baseline


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
) -> tuple[str, str]:
    """Ask the orchestrator-meta agent for a regime-adaptive cadence.

    Returns (next_run_at_iso, rationale). On any failure falls back to
    `_default_next_run_at(portfolio)` with an explanatory rationale.
    """
    if ctx.dry_run:
        return _default_next_run_at(portfolio), "dry-run: heuristic only"

    from datetime import datetime, timezone
    cfg = stages.orchestrator_meta()
    now = state.utcnow()
    nav_history = state.read_nav_history(limit=3)

    user_msg = {
        "role": "user",
        "content": (
            f"Current UTC: {state.utcnow_iso()}\n"
            f"Portfolio summary:\n"
            f"  positions: {len(portfolio.get('positions', []))}\n"
            f"  all_cash: {portfolio.get('all_cash', False)}\n"
            f"  nav_usd: {portfolio.get('nav_usd', 0.0):.2f}\n"
            f"  cash_buffer_pct: {portfolio.get('cash_buffer_pct', 0.0):.1f}\n"
            f"Strategist regime: {view.get('regime', 'unknown')}\n"
            f"Recent NAV history (last {len(nav_history)} rows):\n"
            f"  {json.dumps(nav_history, separators=(',', ':'))}\n\n"
            "Choose the next-run window. Return JSON only."
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
        return _default_next_run_at(portfolio), f"meta call failed ({type(e).__name__}); using heuristic"

    try:
        at = datetime.strptime(payload["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return _default_next_run_at(portfolio), "meta returned malformed next_run_at; using heuristic"

    delta_seconds = (at - now).total_seconds()
    min_seconds = META_MIN_HOURS * 3600 - META_BOUND_TOLERANCE_SECONDS
    max_seconds = META_MAX_HOURS * 3600 + META_BOUND_TOLERANCE_SECONDS
    if delta_seconds < min_seconds or delta_seconds > max_seconds:
        return _default_next_run_at(portfolio), (
            f"meta returned out-of-bounds cadence ({delta_seconds/3600:.2f}h, "
            f"allowed {META_MIN_HOURS}-{META_MAX_HOURS}h ±{META_BOUND_TOLERANCE_SECONDS:g}s); "
            f"using heuristic"
        )

    rationale = (payload.get("rationale") or "")[:300]
    return payload["next_run_at"], f"orchestrator-meta: {rationale}"


def stage_execute(ctx: StageContext, portfolio: dict, view: dict | None = None) -> dict:
    """Submit paper orders to converge actual positions on `portfolio`,
    then plan the next run. Order submission is a no-op when broker is
    None or ORDERS_ENABLED is false.

    The order-delta computation in lib.orders enforces the v2 safety
    invariant: orders never cross zero (no long→short flips via a
    single sell). Closes are submitted before opens to free up cash.
    """
    view = view or {"candidates": []}
    next_at, meta_rationale = _compute_next_run_at(
        ctx=ctx, portfolio=portfolio, view=view,
    )
    next_run = {
        "run_id": ctx.run_id,
        "next_run_at": next_at,
        "rationale": meta_rationale,
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
        from lib import pnl as pnl_lib
        breakdown = pnl_lib.compute_portfolio_pnl(portfolio=portfolio, marks=None)
        state.append_nav({
            "run_id": ctx.run_id,
            "at": state.utcnow_iso(),
            "nav_usd": portfolio.get("nav_usd", 0.0),
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
    })
    return output


def run_pipeline(*, dry_run: bool, run_id: str | None = None, broker: Broker | None = None) -> dict:
    if state.is_halted():
        raise llm.HaltFlagSet("halt.flag is set; refusing to start orchestrator run")

    rid = run_id or state.new_run_id()
    ctx = StageContext(run_id=rid, dry_run=dry_run, broker=broker)

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

    # ----- Stage 2: strategist (1 LLM call) -----
    view = _run_stage(
        ctx=ctx, stage_id="strategist", schema="view.schema.json",
        output_filename="view.json",
        runner=lambda: stage_strategist(ctx, signals_out),
        inputs_hash_parts=(rid, json.dumps(signals_out, sort_keys=True)),
        model=strat_model,
    )

    # ----- Stage 3: construct (1 LLM call) -----
    portfolio = _run_stage(
        ctx=ctx, stage_id="construct", schema="portfolio.schema.json",
        output_filename="portfolio.json",
        runner=lambda: stage_construct(ctx, signals_out, view),
        inputs_hash_parts=(
            rid,
            json.dumps(signals_out, sort_keys=True),
            json.dumps(view, sort_keys=True),
        ),
        model=cons_model,
    )
    if not risk.position_band_ok(len(portfolio["positions"]), portfolio["all_cash"]):
        raise RuntimeError("portfolio violates 1–12 band / all-cash invariant")

    # ----- Stage 4: sanity (deterministic) -----
    sanity_report = sanity.run_sanity_checks(portfolio, view)
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
    args = parser.parse_args(argv)

    if (
        os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"
        and LIVE_VERSION == 0
    ):
        print("LIVE_TRADING_ENABLED=true but LIVE_VERSION=0 — refusing to run.", file=sys.stderr)
        return 2

    broker = None if args.dry_run else _try_load_broker()

    t0 = time.time()
    result = run_pipeline(dry_run=args.dry_run, run_id=args.run_id, broker=broker)
    dt = time.time() - t0
    stage_count = 6 if result.get("market_gate", {}).get("is_open", True) else 1
    print(f"run_id={result['run_id']} stages={stage_count} elapsed={dt:.2f}s dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
