"""5-stage pipeline entrypoint.

Stages:
  1. screen      → state/runs/{run_id}/screen.json
  2. research    → state/runs/{run_id}/research.json   (bull + bear in parallel)
  3. scenarios   → state/runs/{run_id}/scenarios.json
  4. construct   → state/runs/{run_id}/portfolio.json   (8–12 or all-cash)
  5. execute     → submit paper orders, write state/next_run.json

This module is the **skeleton** — stage runners are stubs that read fixtures
and emit schema-valid JSON. LLM prompts arrive in Phase 4. The pipeline shape,
halt-flag check, decision logging, and dry-run mode are all wired here.

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
from typing import Callable

from lib import llm, risk, state
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


@dataclass
class StageContext:
    run_id: str
    dry_run: bool
    broker: Broker | None


# ----- per-stage stubs (replaced with LLM calls in Phase 4) -----


def stage_screen(ctx: StageContext) -> dict:
    return _load_fixture("screen.json")


def stage_research(ctx: StageContext, screen: dict) -> dict:
    out = _load_fixture("research.json")
    out["run_id"] = ctx.run_id
    return out


def stage_scenarios(ctx: StageContext, research: dict) -> dict:
    out = _load_fixture("scenarios.json")
    out["run_id"] = ctx.run_id
    return out


def stage_construct(ctx: StageContext, scenarios: dict) -> dict:
    out = _load_fixture("portfolio.json")
    out["run_id"] = ctx.run_id
    return out


def stage_execute(ctx: StageContext, portfolio: dict) -> dict:
    """Stub — Phase 4/5 will translate portfolio.json into Broker.submit_order calls.

    Always paper-only at this stage. Returns the next-run plan.
    """
    next_run = {
        "run_id": ctx.run_id,
        "next_run_at": state.utcnow_iso(),
        "rationale": "skeleton stub: real next-run scheduling lands with the orchestrator prompt.",
    }
    if not ctx.dry_run:
        state.write_json(state.NEXT_RUN, next_run)
    return next_run


# ----- pipeline driver -----


STAGE_MAP: list[tuple[str, str, str]] = [
    # (stage_id, schema_filename, output_filename)
    ("screen",    "",                          "screen.json"),
    ("research",  "research.schema.json",      "research.json"),
    ("scenarios", "scenarios.schema.json",     "scenarios.json"),
    ("construct", "portfolio.schema.json",     "portfolio.json"),
    ("execute",   "",                          "next_run.json"),
]


def _run_stage(
    *,
    ctx: StageContext,
    stage_id: str,
    schema: str,
    output_filename: str,
    runner: Callable[[], dict],
    inputs_hash_parts: tuple[str, ...],
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
        "model": "stub",  # Phase 4 will plumb the real model id
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

    screen = _run_stage(
        ctx=ctx, stage_id="screen", schema="", output_filename="screen.json",
        runner=lambda: stage_screen(ctx),
        inputs_hash_parts=(rid,),
    )
    research = _run_stage(
        ctx=ctx, stage_id="research", schema="research.schema.json",
        output_filename="research.json",
        runner=lambda: stage_research(ctx, screen),
        inputs_hash_parts=(rid, json.dumps(screen, sort_keys=True)),
    )
    scenarios = _run_stage(
        ctx=ctx, stage_id="scenarios", schema="scenarios.schema.json",
        output_filename="scenarios.json",
        runner=lambda: stage_scenarios(ctx, research),
        inputs_hash_parts=(rid, json.dumps(research, sort_keys=True)),
    )
    portfolio = _run_stage(
        ctx=ctx, stage_id="construct", schema="portfolio.schema.json",
        output_filename="portfolio.json",
        runner=lambda: stage_construct(ctx, scenarios),
        inputs_hash_parts=(rid, json.dumps(scenarios, sort_keys=True)),
    )
    # Position-band sanity (defence in depth — schema also enforces this)
    if not risk.position_band_ok(len(portfolio["positions"]), portfolio["all_cash"]):
        raise RuntimeError("portfolio violates 8–12 band / all-cash invariant")

    next_run = _run_stage(
        ctx=ctx, stage_id="execute", schema="", output_filename="next_run.json",
        runner=lambda: stage_execute(ctx, portfolio),
        inputs_hash_parts=(rid, json.dumps(portfolio, sort_keys=True)),
    )

    if not dry_run:
        state.write_json(state.CURRENT_PORTFOLIO, portfolio)

    return {
        "run_id": rid,
        "screen": screen,
        "research": research,
        "scenarios": scenarios,
        "portfolio": portfolio,
        "next_run": next_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-agent paper-trading orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="No orders, no state mutation outside per-run dir")
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
