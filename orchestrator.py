"""5-stage pipeline entrypoint.

Stages:
  1. screen      → state/runs/{run_id}/screen.json   (Haiku)
  2. research    → state/runs/{run_id}/research.json (Sonnet — bull+bear in parallel per candidate)
  3. scenarios   → state/runs/{run_id}/scenarios.json (Sonnet)
  4. construct   → state/runs/{run_id}/portfolio.json (Sonnet, 8–12 or all-cash)
  5. execute     → submit paper orders, write state/next_run.json

`--dry-run` reads from tests/fixtures/* (no LLM, no orders). Live mode calls
lib.llm.structured_call per stage with prompt-cached system blocks.

Live trading is gated by LIVE_VERSION + LIVE_TRADING_ENABLED — see spec.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Load .env so manual `python orchestrator.py` invocations pick up API keys.
# systemd services use EnvironmentFile= and don't strictly need this, but it
# makes operator smoke runs from the shell work without a separate `source`.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass
from typing import Callable

from lib import llm, market_data, risk, stages, state, universe
from lib.broker import Broker

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "tests" / "fixtures"

# Hard-coded gate per spec §Critical preconditions #1.
LIVE_VERSION = 0  # bump only when promoted; combined with LIVE_TRADING_ENABLED env var

RISK_WARNING = (
    "PAPER TRADING. Leveraged ETFs decay path-dependently; long options can "
    "expire worthless. Capital preservation outweighs upside chasing on a "
    "£2k experimental account. Not financial advice."
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
    """Cache the (large, stable) prompt as the cached prefix; let the run-specific
    user message be the volatile suffix. One ephemeral breakpoint per stage."""
    return [
        {
            "type": "text",
            "text": cfg.system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _schema_ref_registry() -> dict[str, dict]:
    """Build a $id → schema map from schemas/*.schema.json so external $refs
    can be inlined by lib.llm.sanitize_schema_for_structured_output."""
    registry: dict[str, dict] = {}
    for path in (ROOT / "schemas").glob("*.schema.json"):
        s = json.loads(path.read_text())
        if isinstance(s.get("$id"), str):
            registry[s["$id"]] = s
    return registry


def _output_config(cfg: stages.StageConfig) -> dict | None:
    """Merge stage effort (if any) with structured-output format (if schema set).

    Anthropic's structured-outputs feature only accepts a restricted JSON
    Schema subset — numerical / string / array constraints, conditional
    if/then/else, and external $ref URIs are rejected with a 400. The full
    schema still drives local validation via lib.state.validate(); only the
    network-bound copy is sanitized + inlined. See
    lib.llm.sanitize_schema_for_structured_output.
    """
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


def stage_screen(ctx: StageContext) -> dict:
    if ctx.dry_run:
        return _load_fixture("screen.json")
    cfg = stages.screener()
    # Fetch live ADV / HV / price for every entry in the static universe.
    # Per-symbol failures surface as {error: ...} rows rather than crashing.
    snapshot = market_data.universe_snapshot(
        universe.all_symbols(), run_id=ctx.run_id
    )
    universe_block = json.dumps(snapshot, separators=(",", ":"))
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{
            "role": "user",
            "content": (
                f"Screen this universe for the current session. "
                f"UTC: {state.utcnow_iso()}\n\n"
                f"Universe data (last_close in USD, adv_30d in shares, "
                f"hv_30d_annualised as decimal e.g. 0.45 = 45%):\n"
                f"{universe_block}\n\n"
                f"Apply liquidity filters strictly against these numbers, "
                f"not training-data priors. Return JSON only."
            ),
        }],
        schema_filename=None,
        max_tokens=cfg.max_tokens,
        thinking=cfg.thinking,
        output_config=_output_config(cfg),
    ))
    # Best-effort JSON parse (Haiku, no schema). Free-form fallback to raw text.
    try:
        return json.loads(res.raw_text)
    except json.JSONDecodeError:
        return {"generated_at": state.utcnow_iso(), "raw": res.raw_text, "passed": [], "rejected": []}


async def _research_one(ctx: StageContext, candidate: dict) -> dict:
    """One candidate: bull and bear in parallel, merged into a research candidate row."""
    bull_cfg = stages.bull()
    bear_cfg = stages.bear()

    user_msg = {
        "role": "user",
        "content": (
            f"Candidate: {json.dumps(candidate, sort_keys=True)}\n"
            f"Run id: {ctx.run_id}\n"
            "Return JSON only matching {thesis, key_drivers, counterarguments, confidence}."
        ),
    }

    async def _call(cfg: stages.StageConfig):
        # Anthropic SDK sync call — wrap in to_thread for parallel execution.
        return await asyncio.to_thread(
            llm.structured_call,
            llm.StageCall(
                run_id=ctx.run_id,
                stage=cfg.stage,
                model=cfg.model,
                system_blocks=_system_blocks(cfg),
                user_messages=[user_msg],
                schema_filename=None,
                max_tokens=cfg.max_tokens,
                thinking=cfg.thinking,
                output_config=_output_config(cfg),
            ),
        )

    bull_res, bear_res = await asyncio.gather(_call(bull_cfg), _call(bear_cfg))

    def _parse(r) -> dict:
        try:
            return json.loads(r.raw_text)
        except json.JSONDecodeError:
            return {"thesis": r.raw_text[:200], "key_drivers": ["[parse_failed]"],
                    "counterarguments": ["[parse_failed]"], "confidence": 0.0}

    bull = _parse(bull_res)
    bear = _parse(bear_res)
    return {
        "symbol": candidate.get("symbol", "?"),
        "instrument_kind": (
            "option" if candidate.get("kind") == "option_underlying" else "etf"
        ),
        "bull": bull,
        "bear": bear,
        "confidence_delta": float(bull.get("confidence", 0)) - float(bear.get("confidence", 0)),
        "abstain": bull.get("confidence", 0) < 0.3 and bear.get("confidence", 0) < 0.3,
    }


def stage_research(ctx: StageContext, screen: dict) -> dict:
    if ctx.dry_run:
        out = _load_fixture("research.json")
        out["run_id"] = ctx.run_id
        return out
    candidates = screen.get("passed", [])[:8]  # cap fan-out for cost discipline

    async def _gather():
        return await asyncio.gather(*[_research_one(ctx, c) for c in candidates])

    rows = asyncio.run(_gather()) if candidates else []
    return {
        "run_id": ctx.run_id,
        "generated_at": state.utcnow_iso(),
        "candidates": rows,
    }


def stage_scenarios(ctx: StageContext, research: dict) -> dict:
    if ctx.dry_run:
        out = _load_fixture("scenarios.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.scenarios()
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{
            "role": "user",
            "content": (
                f"Research summary: {json.dumps(research, sort_keys=True)}\n"
                f"Run id: {ctx.run_id}\n"
                "Return JSON conforming to scenarios.schema.json."
            ),
        }],
        schema_filename=cfg.schema_filename,
        max_tokens=cfg.max_tokens,
        thinking=cfg.thinking,
        output_config=_output_config(cfg),
    ))
    return res.payload


def stage_construct(ctx: StageContext, scenarios_out: dict) -> dict:
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
                f"Scenarios: {json.dumps(scenarios_out, sort_keys=True)}\n"
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
    if ctx.broker is not None:
        try:
            return ctx.broker.get_account().equity_usd
        except Exception:
            pass
    return 2500.0  # £2k paper baseline


def _default_next_run_at(portfolio: dict) -> str:
    """Heuristic next-run cadence — used as the fallback when the
    orchestrator-meta LLM call doesn't return a usable timestamp:
       - all-cash: 6 hours (no urgency — just sample the universe again)
       - positions held: 4 hours (faster, to monitor kill conditions)
    Both well above rate-limit windows, both produce a strictly future
    timestamp so systemd-run will accept the schedule."""
    from datetime import timedelta
    hours = 6 if portfolio.get("all_cash") else 4
    return (state.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Orchestrator-meta runs after construct and decides cadence. Hard bounds
# match the prompt — any returned timestamp outside this window falls back
# to the heuristic.
META_MIN_HOURS = 1.0
META_MAX_HOURS = 24.0

# Tolerance applied to both bounds, accounting for:
#   - second-precision ISO output ('YYYY-MM-DDTHH:MM:SSZ') vs microsecond `now`
#     (model's "1 hour from now" rounds down to whole seconds, making the
#     delta come in at 0.9998h)
#   - LLM round-trip latency between captured `now` and the response
#   - small clock drift between this host and the model's reference time
#
# 30 seconds is invisible at hour-scale cadence but absorbs all three.
# Without it, the documented minimum (1h) is systematically rejected — see
# Codex review on PR #19.
META_BOUND_TOLERANCE_SECONDS = 30.0


def _compute_next_run_at(
    *, ctx: StageContext, portfolio: dict, scenarios_out: dict,
) -> tuple[str, str]:
    """Ask the orchestrator-meta agent for a regime-adaptive cadence.

    Returns (next_run_at_iso, rationale). On any failure — schema retry
    blown, JSON malformed, timestamp out of bounds, or `ctx.dry_run=True`
    — falls back to `_default_next_run_at(portfolio)` with an explanatory
    rationale. Never propagates exceptions to the caller.
    """
    if ctx.dry_run:
        return _default_next_run_at(portfolio), "dry-run: heuristic only"

    from datetime import datetime, timedelta, timezone
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
            f"Recent NAV history (last {len(nav_history)} rows):\n"
            f"  {json.dumps(nav_history, separators=(',', ':'))}\n"
            f"Scenarios horizon hints:\n"
            f"  {json.dumps([{'symbol': c.get('symbol'), 'horizon_days': c.get('horizon_days')} for c in scenarios_out.get('candidates', [])], separators=(',', ':'))}\n\n"
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
        payload = json.loads(res.raw_text)
    except Exception as e:
        return _default_next_run_at(portfolio), f"meta call failed ({type(e).__name__}); using heuristic"

    # Sanity-check the returned timestamp before we trust it.
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

    rationale = (payload.get("rationale") or "")[:300]  # cap length so it stays log-friendly
    return payload["next_run_at"], f"orchestrator-meta: {rationale}"


def stage_execute(ctx: StageContext, portfolio: dict, scenarios_out: dict | None = None) -> dict:
    """Submit paper orders to converge actual positions on `portfolio`, then plan
    the next run. Order submission is a no-op when broker is None."""
    scenarios_out = scenarios_out or {"candidates": []}
    next_at, meta_rationale = _compute_next_run_at(
        ctx=ctx, portfolio=portfolio, scenarios_out=scenarios_out,
    )
    next_run = {
        "run_id": ctx.run_id,
        "next_run_at": next_at,
        "rationale": meta_rationale,
    }
    # Order submission — gated behind ORDERS_ENABLED=true env var. Default
    # OFF so this code can ride to main behind a flag and the operator opts
    # in explicitly (the spec mandates paper-only and every iteration before
    # promotion has to be deliberate).
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
                {"symbol": r.symbol, "qty": r.qty, "side": r.side, "status": r.status}
                for r in results
            ],
        }
    if not ctx.dry_run:
        state.write_json(state.NEXT_RUN, next_run)
        # NAV history: one row per run for the dashboard equity curve.
        # Marks aren't wired in yet, so gross/net P&L only includes the
        # modelled-cost entry-leg estimate. Real marks land in Phase 10a.
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

    screen_model = "fixture" if dry_run else stages.screener().model
    research_model = "fixture" if dry_run else stages.bull().model
    scen_model = "fixture" if dry_run else stages.scenarios().model
    cons_model = "fixture" if dry_run else stages.constructor().model

    screen = _run_stage(
        ctx=ctx, stage_id="screen", schema="", output_filename="screen.json",
        runner=lambda: stage_screen(ctx),
        inputs_hash_parts=(rid,), model=screen_model,
    )
    research = _run_stage(
        ctx=ctx, stage_id="research", schema="research.schema.json",
        output_filename="research.json",
        runner=lambda: stage_research(ctx, screen),
        inputs_hash_parts=(rid, json.dumps(screen, sort_keys=True)),
        model=research_model,
    )
    scenarios_out = _run_stage(
        ctx=ctx, stage_id="scenarios", schema="scenarios.schema.json",
        output_filename="scenarios.json",
        runner=lambda: stage_scenarios(ctx, research),
        inputs_hash_parts=(rid, json.dumps(research, sort_keys=True)),
        model=scen_model,
    )
    portfolio = _run_stage(
        ctx=ctx, stage_id="construct", schema="portfolio.schema.json",
        output_filename="portfolio.json",
        runner=lambda: stage_construct(ctx, scenarios_out),
        inputs_hash_parts=(rid, json.dumps(scenarios_out, sort_keys=True)),
        model=cons_model,
    )
    if not risk.position_band_ok(len(portfolio["positions"]), portfolio["all_cash"]):
        raise RuntimeError("portfolio violates 8–12 band / all-cash invariant")

    next_run = _run_stage(
        ctx=ctx, stage_id="execute", schema="", output_filename="next_run.json",
        runner=lambda: stage_execute(ctx, portfolio, scenarios_out),
        inputs_hash_parts=(rid, json.dumps(portfolio, sort_keys=True)),
        model="local",
    )

    if not dry_run:
        state.write_json(state.CURRENT_PORTFOLIO, portfolio)

    return {
        "run_id": rid,
        "screen": screen,
        "research": research,
        "scenarios": scenarios_out,
        "portfolio": portfolio,
        "next_run": next_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-agent paper-trading orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="No orders, no LLM calls — fixture mode")
    parser.add_argument("--run-id", default=None, help="Override generated run_id")
    args = parser.parse_args(argv)

    if (
        os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"
        and LIVE_VERSION == 0
    ):
        print("LIVE_TRADING_ENABLED=true but LIVE_VERSION=0 — refusing to run.", file=sys.stderr)
        return 2

    t0 = time.time()
    result = run_pipeline(dry_run=args.dry_run, run_id=args.run_id)
    dt = time.time() - t0
    print(f"run_id={result['run_id']} stages=5 elapsed={dt:.2f}s dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
