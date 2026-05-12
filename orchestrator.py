"""5-stage pipeline entrypoint.

Stages:
  1. screen      → state/runs/{run_id}/screen.json   (Haiku)
  2. research    → state/runs/{run_id}/research.json (Sonnet — bull+bear in parallel per candidate)
  3. chains      → state/runs/{run_id}/chains.json   (Alpaca data — real bid/ask/IV/delta per option underlying)
  4. scenarios   → state/runs/{run_id}/scenarios.json (Sonnet)
  5. construct   → state/runs/{run_id}/portfolio.json (Opus, 1–12 or all-cash)
  6. execute     → submit paper orders, write state/next_run.json

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
    # Best-effort JSON parse (Haiku, no schema). Strip any ```json fences the
    # model added despite the "JSON only — no markdown fences" prompt: without
    # this, the bare json.loads fails and every candidate gets dumped into a
    # silent `raw` envelope — downstream stages see {"passed": []} and the
    # agent abstains to all-cash even though the screener found candidates.
    try:
        return json.loads(llm.strip_markdown_fences(res.raw_text))
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
        # Same defensive fence-stripping as stage_screen — Sonnet too will
        # occasionally wrap its JSON in ```json fences despite the prompt.
        try:
            return json.loads(llm.strip_markdown_fences(r.raw_text))
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


RESEARCH_CANDIDATE_CAP = 10            # total candidates that fan into bull+bear research
RESEARCH_OPTION_UNDERLYING_CAP = 5     # max option underlyings (full SPY/QQQ/IWM/DIA/TLT set)


def _select_research_candidates(passed: list[dict]) -> list[dict]:
    """Pick which of the screener's `passed` rows go into bull+bear research.

    Prioritises option underlyings (SPY/QQQ/IWM/DIA/TLT) so they never get
    cut by a "first 8" slice that the screener happened to fill with ETFs.
    In high-vol / extended-market regimes, defined-risk long puts on those
    underlyings are frequently the only positive-EV plays — the constructor
    can only consider candidates that survive this stage's cap.
    """
    options = [c for c in passed if c.get("kind") == "option_underlying"]
    etfs    = [c for c in passed if c.get("kind") != "option_underlying"]
    options = options[:RESEARCH_OPTION_UNDERLYING_CAP]
    etfs    = etfs[: max(0, RESEARCH_CANDIDATE_CAP - len(options))]
    return options + etfs


def stage_research(ctx: StageContext, screen: dict) -> dict:
    if ctx.dry_run:
        out = _load_fixture("research.json")
        out["run_id"] = ctx.run_id
        return out
    candidates = _select_research_candidates(screen.get("passed", []))

    async def _gather():
        return await asyncio.gather(*[_research_one(ctx, c) for c in candidates])

    rows = asyncio.run(_gather()) if candidates else []
    return {
        "run_id": ctx.run_id,
        "generated_at": state.utcnow_iso(),
        "candidates": rows,
    }


def _option_underlyings_from_research(research: dict) -> list[str]:
    """Pick the candidates marked as option plays. The research stage emits
    ``instrument_kind: "option"`` for option underlyings (mirroring the
    screener's ``kind: "option_underlying"`` flag); ETFs use
    ``instrument_kind: "etf"``."""
    out: list[str] = []
    for c in research.get("candidates") or []:
        if c.get("abstain"):
            continue
        if c.get("instrument_kind") == "option":
            sym = c.get("symbol") or c.get("underlying")
            if sym:
                out.append(sym)
    return out


def _spot_lookup_from_screen(screen: dict) -> dict[str, float]:
    """Build {symbol: last_close} from screen output so the chain stage can
    set its ATM band without re-hitting yfinance. Falls back to None for
    symbols with missing/error rows; the chain stage skips those."""
    out: dict[str, float] = {}
    for c in (screen.get("passed") or []) + (screen.get("failed") or []):
        sym = c.get("symbol")
        lc = c.get("last_close")
        if sym and isinstance(lc, (int, float)) and lc > 0:
            out[sym] = float(lc)
    return out


def stage_chains(
    ctx: StageContext, research: dict, screen: dict | None = None,
) -> dict:
    """Fetch live Alpaca option chains for every option-underlying candidate.

    Phase 9b fix for the May 11 2026 SPY-565P incident: the scenarios
    agent was pricing premiums and IVs from training-data priors, which
    on a small paper account ran 5-10x off real market (agent said
    $3.50; fill was $0.61). This stage runs BEFORE scenarios so the
    next prompt can include real bid/ask/IV/greeks per strike.

    Per-underlying failures are isolated — we record the error in the
    artifact and continue. If every underlying fails we still emit a
    chains.json with empty ``underlyings`` so the scenarios stage knows
    no live chain context is available and can fall back gracefully.

    Dry-run: load tests/fixtures/chains.json if present; otherwise emit
    an empty stub. The fixture is regenerated when the chain shape
    changes — keep tests/fixtures/chains.json in sync with PR #50's
    ``summarise_chain`` shape.
    """
    if ctx.dry_run:
        # Fixture is optional — older fixtures predate this stage. Fall back
        # to an empty stub rather than crashing dry-runs that don't include it.
        try:
            out = _load_fixture("chains.json")
            out["run_id"] = ctx.run_id
            return out
        except FileNotFoundError:
            return {
                "run_id": ctx.run_id,
                "generated_at": state.utcnow_iso(),
                "underlyings": {},
            }

    from datetime import date as _date
    from lib import options_chain

    underlyings = _option_underlyings_from_research(research)
    spots = _spot_lookup_from_screen(screen or {})
    today = _date.today()

    out: dict = {
        "run_id": ctx.run_id,
        "generated_at": state.utcnow_iso(),
        "underlyings": {},
    }
    if not underlyings:
        return out

    # Lazy fetcher construction so an all-spots-missing run doesn't burn an
    # AlpacaBroker init (which requires API keys + a successful SDK import).
    fetcher: options_chain.ChainFetcher | None = None

    for sym in underlyings:
        spot = spots.get(sym)
        if spot is None:
            out["underlyings"][sym] = {
                "error": "no spot price available from screen — chain skip",
            }
            continue
        if fetcher is None:
            fetcher = options_chain.ChainFetcher()
        try:
            summary = fetcher.fetch(sym, spot=spot, today=today)
            out["underlyings"][sym] = summary
        except options_chain.ChainFetchError as e:
            out["underlyings"][sym] = {"error": str(e)}
        except Exception as e:
            # Defensive — fetcher already wraps known failure modes as
            # ChainFetchError. Unexpected exceptions get the same soft-fail
            # treatment so a misbehaving SDK can't take down the whole cycle.
            out["underlyings"][sym] = {
                "error": f"unexpected {type(e).__name__}: {e}"
            }
    return out


def stage_scenarios(ctx: StageContext, research: dict, chains: dict | None = None) -> dict:
    if ctx.dry_run:
        out = _load_fixture("scenarios.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.scenarios()
    chains_block = ""
    if chains and chains.get("underlyings"):
        chains_block = (
            "\n\nLive option chains (Alpaca, ATM band ±25%, DTE 14–75, "
            "spread ≤25%). Use THESE bid/ask/iv/delta/dte values as the "
            "ground truth for option picks — NOT training-data priors:\n"
            f"{json.dumps(chains.get('underlyings'), sort_keys=True)}\n"
            "When picking strike + expiry for an option_rationale, pick "
            "from this chain. premium_paid should equal the mid (or ask "
            "for a market buy) of the selected OSI row."
        )
    res = llm.structured_call(llm.StageCall(
        run_id=ctx.run_id,
        stage=cfg.stage,
        model=cfg.model,
        system_blocks=_system_blocks(cfg),
        user_messages=[{
            "role": "user",
            "content": (
                f"Research summary: {json.dumps(research, sort_keys=True)}\n"
                f"Run id: {ctx.run_id}"
                f"{chains_block}\n"
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
    # Alpaca paper accounts ship with $100k by default. The $2.5k experimental
    # notional in CLAUDE.md is what sizing must respect — VIRTUAL_NAV_USD lets
    # the operator pin the agent to a smaller notional than the broker reports.
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
        payload = json.loads(llm.strip_markdown_fences(res.raw_text))
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
                {
                    "symbol": r.symbol,
                    "qty": r.qty,
                    "side": r.side,
                    "status": r.status,
                    # Phase 2 per-trade PnL: surface broker_order_id so the
                    # activities sync (lib/trades_sync.sync_fills_from_alpaca)
                    # can attribute each fill to this run via the
                    # order_id_to_run_id map built by
                    # lib/trades_sync.order_id_to_run_id_from_runs.
                    "broker_order_id": r.broker_order_id,
                }
                for r in results
            ],
        }
        # Per-run orders index — read by lib/trades_sync to build the
        # order_id_to_run_id map without re-parsing the full decisions log.
        # Atomic JSON write so concurrent dashboard reads always see a
        # complete file (state.write_json uses a tmp+rename).
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
        # activity. Run EVERY cycle that reaches stage_execute with a broker
        # connection — not just cycles that submitted new orders. Codex P1
        # caught the earlier `if accepted_order_ids` gate: it would miss
        # fills from prior cycles' orders that filled late (partials, slow
        # routing, out-of-hours fills) and leave trades.jsonl stale until
        # another new order happened to fire. The sync is idempotent (PR
        # #52: known_ids dedupe) so re-running every cycle is cheap.
        try:
            from lib import trades_sync
            trades_sync.sync_fills_from_alpaca(
                trading_client=getattr(ctx.broker, "_client", None),
                order_id_to_run_id=trades_sync.order_id_to_run_id_from_runs(),
            )
        except Exception as e:
            # Failures are soft — sync is best-effort and the next cycle's
            # sync will pick up missed fills. Label lands on next_run.json
            # so the dashboard Agent Logs tab can surface persistent errors.
            next_run["trades_sync_error"] = (
                f"sync_fills_from_alpaca: {type(e).__name__}: {e}"
            )
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
    # Phase 9b: real option chains fetched from Alpaca BEFORE scenarios so
    # the scenarios agent prices off ground-truth bid/ask/IV/delta, not
    # training-data priors that ran 5-10× off real market (May 11 2026
    # SPY-565P regression). Per-underlying failures don't abort; scenarios
    # degrades to "no chain context for X" and falls back to priors only
    # where chain data is missing.
    chains = _run_stage(
        ctx=ctx, stage_id="chains", schema="",
        output_filename="chains.json",
        runner=lambda: stage_chains(ctx, research, screen=screen),
        inputs_hash_parts=(rid, json.dumps(research, sort_keys=True)),
        model="alpaca-data",
    )
    scenarios_out = _run_stage(
        ctx=ctx, stage_id="scenarios", schema="scenarios.schema.json",
        output_filename="scenarios.json",
        runner=lambda: stage_scenarios(ctx, research, chains=chains),
        inputs_hash_parts=(
            rid,
            json.dumps(research, sort_keys=True),
            json.dumps(chains, sort_keys=True),
        ),
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
        raise RuntimeError("portfolio violates 1–12 band / all-cash invariant")

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
        "chains": chains,
        "scenarios": scenarios_out,
        "portfolio": portfolio,
        "next_run": next_run,
    }


def _try_load_broker() -> Broker | None:
    """Best-effort AlpacaBroker construction. Returns None if creds are
    missing or the SDK isn't installed — orchestrator still runs (writes
    portfolio.json, decision_log, next_run.json) but stage_execute can't
    submit orders without a broker. Same shape as monitor.py's helper —
    deliberately duplicated so the two entrypoints can fail independently.
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

    # Load the broker for live (non-dry-run) cycles so stage_execute can
    # actually submit orders. Dry runs skip broker entirely — they use
    # fixtures end-to-end and shouldn't open a network connection.
    # Previously this was never instantiated in main(); stage_execute saw
    # ctx.broker=None on every cycle, silently skipped submission, and the
    # operator had to run orders.submit_plan by hand to produce trades.
    broker = None if args.dry_run else _try_load_broker()

    t0 = time.time()
    result = run_pipeline(dry_run=args.dry_run, run_id=args.run_id, broker=broker)
    dt = time.time() - t0
    print(f"run_id={result['run_id']} stages=5 elapsed={dt:.2f}s dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
