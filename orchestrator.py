"""v2 pipeline entrypoint.

Leveraged/inverse ETF-only autonomous paper-trading system. Bullish theses
hold bull ETFs; bearish theses hold inverse ETFs (no shorts, no options).

Stages:
  0. market_gate → state/runs/{run_id}/market_gate.json (Alpaca clock, $0)
  1. signals     → state/runs/{run_id}/signals.json     (deterministic Python, $0)
  2. strategist  → state/runs/{run_id}/view.json        (Sonnet 4.6, ~$0.05)
  3. construct   → state/runs/{run_id}/portfolio.json   (Opus 4.7, ~$0.20)
  3.5 critic     → state/runs/{run_id}/critique.json    (Sonnet 4.6, ~$0.03)
  4. sanity      → state/runs/{run_id}/sanity.json      (deterministic, $0)
  5. execute     → state/runs/{run_id}/orders.json + next_run.json (Alpaca paper, $0)

v1 → v2 migration: the bull/bear research stages and the scenarios stage
were collapsed into a single deterministic signals table + a single
strategist LLM call. Per-cycle LLM cost dropped from ~$1.50–2.50 to ~$0.25.
The construct stage still owns position selection + sizing + kill-condition
tailoring on Opus 4.7.

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

from lib import feedback, live_gate, live_nav, llm, market_gate, risk, sanity, signals, stages, state, trades
from lib.broker import Broker

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "tests" / "fixtures"

# Hard-coded gate per spec §Critical preconditions #1. The canonical constant
# now lives in lib/live_gate.py (single source of truth shared with monitor.py);
# re-exported here so existing references keep working.
LIVE_VERSION = live_gate.LIVE_VERSION  # bump in lib/live_gate.py when promoted

RISK_WARNING = (
    "PAPER TRADING. Leveraged & inverse ETFs decay path-dependently and are "
    "not buy-and-hold instruments. Capital preservation outweighs upside "
    "chasing on a $2.5k experimental account. Not financial advice."
)

# Reschedule delay after an unhandled pipeline crash. Mirrors
# market_gate.CLOCK_ERROR_RETRY_MINUTES: without a fresh next_run.json the
# scheduler pins on the stale timestamp (LAST_FIRED == NEXT_AT) and nothing
# fires until the daily 13:30 UTC fallback timer.
CRASH_RETRY_MINUTES = 30


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
    # Live-mode sizing NAV, prefetched once per cycle by run_pipeline when
    # the broker is genuinely live (triple lock fully raised). None on
    # paper. Caching avoids re-hitting broker.get_account at every
    # _account_nav call site within a cycle.
    live_nav_usd: float | None = None


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


def _performance_memo_block(performance_memo: dict | None) -> str:
    """Render the track-record memo for a stage's user message.

    The framing sentence matters: the memo is calibration EVIDENCE for the
    agent's judgment on conviction and sizing. It must never read as "be
    more cautious" — over-gating into chronic all-cash is the failure mode
    this system already escaped once.
    """
    if not performance_memo:
        return ""
    return (
        f"Performance memo — your own realized track record: "
        f"{json.dumps(performance_memo, sort_keys=True)}\n"
        "Use the memo as calibration evidence: where your high-confidence "
        "picks on a factor have repeatedly won, trust similar setups; where "
        "they have repeatedly lost, demand a cleaner signal or express the "
        "view through a different factor. The memo is NOT an instruction to "
        "trade less — staying active within the risk rails is expected.\n"
    )


def stage_strategist(
    ctx: StageContext,
    signals_out: dict,
    current_positions: list[dict] | None = None,
    pnl_history: list[dict] | None = None,
    performance_memo: dict | None = None,
    user_notes: list[dict] | None = None,
    manual_closes: list[dict] | None = None,
) -> dict:
    """One LLM call — Sonnet 4.6, ~$0.05. Reads signals + current
    portfolio + recent PnL feedback + its own realized track record;
    emits view.json with regime classification + 0-6 ranked candidates.

    current_positions: broker-reported holdings (passed through from
    orchestrator). Lets the strategist bias toward "keep this winner"
    vs churning to fresh ideas.

    pnl_history: last 5 cycles' {regime, positions_summary,
    realized_4h_pnl_pct} so the strategist can self-correct drift.

    performance_memo: lib.feedback.build_performance_memo output — the
    factor-level win/loss record + confidence calibration the agent uses
    as evidence when scoring new candidates.
    """
    if ctx.dry_run:
        out = _load_fixture("view.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.strategist()
    current_positions = current_positions or []
    pnl_history = pnl_history or []
    content = (
        f"{_live_mode_context_line(ctx)}"
        f"Signals: {json.dumps(signals.compact_for_llm(signals_out), sort_keys=True)}\n"
        f"Current broker positions: {json.dumps(current_positions, sort_keys=True)}\n"
        f"Recent PnL history (last cycles, oldest first): "
        f"{json.dumps(pnl_history, sort_keys=True)}\n"
        f"{_performance_memo_block(performance_memo)}"
        f"{_user_notes_block(user_notes)}"
        f"{_manual_close_prompt_line(manual_closes or [])}"
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


def _cooldown_prompt_line(cooldown_symbols: dict) -> str:
    """One constructor-prompt line listing symbols in re-entry cooldown.

    Empty string when nothing is in cooldown so the prompt stays clean.
    The override threshold quoted here matches the deterministic sanity
    rule (risk.REENTRY_COOLDOWN_OVERRIDE_CONFIDENCE)."""
    if not cooldown_symbols:
        return ""
    syms = ", ".join(sorted(cooldown_symbols))
    return (
        f"Symbols in re-entry cooldown (fully exited within "
        f"{risk.REENTRY_COOLDOWN_DAYS} days): {syms}. Do NOT re-open these "
        f"unless your re-entry confidence exceeds "
        f"{risk.REENTRY_COOLDOWN_OVERRIDE_CONFIDENCE}; if you override the "
        f"cooldown, say so explicitly in construction_rationale.\n"
    )


def _user_notes_block(user_notes: list[dict] | None) -> str:
    """Labeled block of pending operator notes for the strategist +
    constructor user messages. Verbatim text, oldest-first, one bullet per
    note. Empty string when there are none so the prompt stays clean.
    Framed as guidance, not an order — the agents keep their judgment."""
    if not user_notes:
        return ""
    bullets = "".join(
        f"- [{n.get('at', '?')}] {n.get('text', '')}\n" for n in user_notes
    )
    return (
        "USER NOTES (operator guidance typed into the dashboard — weigh it, "
        "but use your judgment; it is context, not an order):\n"
        f"{bullets}"
    )


def _recent_manual_closes(
    *, now=None, window_days: float = 7.0, mode: str = "paper",
) -> list[dict]:
    """Positions the operator manually closed from the dashboard within the
    window, most-recent close per symbol: [{"symbol", "at"}, ...].

    Reads kill_events.jsonl rows stamped source="dashboard" by
    lib.manual_actions.close_position_manually, era-scoped like every other
    memo/prompt input. Failures are non-fatal — an empty list just means no
    manual-close context this cycle."""
    from datetime import datetime
    now = now or state.utcnow()
    latest: dict[str, str] = {}
    try:
        for ev in state.read_kill_events(limit=200):
            if ev.get("source") != "dashboard":
                continue
            if state.record_mode(ev) != mode:
                continue
            at_raw = ev.get("at") or ""
            try:
                at = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if (now - at).total_seconds() > window_days * 86400.0:
                continue
            sym = ev.get("symbol")
            if sym and at_raw > latest.get(sym, ""):
                latest[sym] = at_raw
    except Exception:
        return []
    return [{"symbol": s, "at": a} for s, a in sorted(latest.items())]


def _manual_close_prompt_line(manual_closes: list[dict]) -> str:
    """One prompt line flagging recent operator-initiated closes. The 7-day
    re-entry cooldown already discourages re-opening; this line adds the
    WHY — the operator deliberately exited — so an override is a conscious,
    justified act rather than a signal-chasing reflex."""
    if not manual_closes:
        return ""
    entries = ", ".join(
        f"{mc['symbol']} ({mc['at'][:10]})" for mc in manual_closes
    )
    return (
        f"Operator manually closed from the dashboard within the last 7 "
        f"days: {entries}. The operator deliberately exited these — re-open "
        f"one only with a strong NEW justification, and say so explicitly "
        f"in your rationale.\n"
    )


def _cooldown_symbols_now() -> dict:
    """Symbols in re-entry cooldown as of now, from the trade log.

    Thin wrapper over trades.symbols_in_cooldown using the repo-wide
    REENTRY_COOLDOWN_DAYS window. Failures are non-fatal — an empty map
    just means no cooldown is applied this cycle.

    Accuracy depends on state/trades.jsonl being current; the trade cycle
    calls _sync_fills_before_cooldown() before this so a symbol closed
    between cycles (manual broker close, monitor flatten) is reflected."""
    try:
        return trades.symbols_in_cooldown(
            state.read_trades(),
            now=state.utcnow(),
            window_days=risk.REENTRY_COOLDOWN_DAYS,
            mode=live_gate.trading_mode(),
        )
    except Exception:
        return {}


def _sync_fills_before_cooldown(ctx: StageContext) -> str | None:
    """Pull Alpaca fills into state/trades.jsonl BEFORE the cooldown map is
    derived, so a symbol fully exited between cycles outside the orchestrator
    (manual broker close, monitor-driven flatten) is on the log when the
    re-entry cooldown + dedup fingerprint are computed. Idempotent (dedupe
    via known activity ids), mirroring the post-execute sync. Returns an
    error string on failure (non-fatal — the cycle continues with whatever
    the log already has, degrading the guardrail rather than aborting)."""
    if ctx.dry_run:
        return None
    try:
        from lib import trades_sync
        trades_sync.sync_fills_from_alpaca(
            trading_client=getattr(ctx.broker, "_client", None),
            order_id_to_run_id=trades_sync.order_id_to_run_id_from_runs(),
            mode=live_gate.trading_mode(ctx.broker),
        )
        return None
    except Exception as e:
        return f"sync_fills_from_alpaca(pre-cooldown): {type(e).__name__}: {e}"


def _live_mode_context_line(ctx: StageContext) -> str:
    """One user-message line that overrides the paper framing baked into the
    static system prompts once the account is genuinely live (Codex P2 on
    PR #112: prompts/*.md all describe a '$2,500 paper account', and the
    cached system blocks deliberately stay byte-stable across promotion —
    this per-cycle line is the mode-aware correction). Empty on paper so
    pre-promotion inputs stay byte-identical for the prompt cache + dedup.
    """
    if live_gate.trading_mode(ctx.broker) != "live":
        return ""
    nav = ctx.live_nav_usd
    nav_txt = (
        f"${nav:,.2f}" if isinstance(nav, (int, float))
        else "the NAV figure provided in this message"
    )
    return (
        "LIVE TRADING OVERRIDE: this account now trades REAL MONEY on Alpaca "
        f"live. The capital you size against is {nav_txt} — the operator's "
        "allocated live NAV (real equity, possibly capped) — NOT the $2,500 "
        "paper account described in your system prompt; read every 'paper' "
        "reference there as historical framing. Losses are real. Capital "
        "preservation outweighs upside chasing even more strictly than on "
        "paper: when conviction is marginal, hold cash.\n"
    )


def _vol_context_line(signals_out: dict) -> str:
    """One context line giving the constructor the universe-median HV30 so
    it can judge how choppy each candidate is relative to peers. Pure
    information for sizing judgment — no formula, no rule."""
    hvs = sorted(
        t["hv_30d_annualised"] for t in signals_out.get("tickers", [])
        if isinstance(t.get("hv_30d_annualised"), (int, float))
    )
    if not hvs:
        return ""
    median = hvs[len(hvs) // 2] if len(hvs) % 2 else (hvs[len(hvs) // 2 - 1] + hvs[len(hvs) // 2]) / 2
    return (
        f"Universe median HV30: {median:.2f}. Candidates well above it are "
        "choppier than peers (more leveraged-ETF decay in chop, wider stops "
        "needed) — weigh that in your sizing judgment.\n"
    )


def stage_construct(
    ctx: StageContext,
    signals_out: dict,
    view: dict,
    current_positions: list[dict] | None = None,
    pnl_history: list[dict] | None = None,
    adaptive_cap_pct: float = 15.0,
    hold_ceiling_pct: float = 25.0,
    cooldown_symbols: dict | None = None,
    performance_memo: dict | None = None,
    user_notes: list[dict] | None = None,
    manual_closes: list[dict] | None = None,
) -> dict:
    """One LLM call — Opus 4.7, ~$0.20. Reads signals + view + current
    positions + recent PnL feedback + the agent's own track record; emits
    the final portfolio.json with ETF positions, sizing, kill conditions."""
    if ctx.dry_run:
        out = _load_fixture("portfolio.json")
        out["run_id"] = ctx.run_id
        return out
    cfg = stages.constructor()
    nav = _account_nav(ctx)
    current_positions = current_positions or []
    pnl_history = pnl_history or []
    cooldown_symbols = cooldown_symbols or {}
    content = (
        f"{_live_mode_context_line(ctx)}"
        f"Signals: {json.dumps(signals.compact_for_llm(signals_out), sort_keys=True)}\n"
        f"Strategist view: {json.dumps(view, sort_keys=True)}\n"
        f"Current broker positions: {json.dumps(current_positions, sort_keys=True)}\n"
        f"Recent PnL history (last cycles): {json.dumps(pnl_history, sort_keys=True)}\n"
        f"{_performance_memo_block(performance_memo)}"
        f"NAV (USD): {nav:.2f}\n"
        f"Entry/add cap %: {adaptive_cap_pct:.2f} (max weight to OPEN or ADD to "
        f"a position; reduced from 15.0% when NAV is in drawdown)\n"
        f"Hold ceiling %: {hold_ceiling_pct:.2f} (an already-open position that "
        f"has appreciated may be KEPT up to this; reduced from 25.0% in "
        f"drawdown). Do NOT trim a winner back to the entry cap just because it "
        f"drifted above it — only trim weight above the hold ceiling. Never open "
        f"or add a position above the entry cap.\n"
        f"{_vol_context_line(signals_out)}"
        f"{_cooldown_prompt_line(cooldown_symbols)}"
        f"{_user_notes_block(user_notes)}"
        f"{_manual_close_prompt_line(manual_closes or [])}"
        f"Run id: {ctx.run_id}\n"
        "Return JSON conforming to portfolio.schema.json. All positions are "
        "leveraged/inverse ETFs (bullish → bull ETF, bearish → inverse ETF). "
        "Prefer keeping current positions when the strategist's view is "
        "consistent with them."
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


def _sanity_preview_for_critic(sanity_report: dict | None) -> str:
    """Compact non-pass sanity rules for the critic's user message. The
    deterministic rules run for free, so previewing them pre-critic gives
    the adversarial review concrete material instead of a blind read."""
    if not sanity_report:
        return ""
    flagged = [
        {"rule": r["name"], "status": r["status"], "detail": (r.get("detail") or "")[:200]}
        for r in sanity_report.get("rules", [])
        if r.get("status") in ("fail", "warn")
    ]
    if not flagged:
        return "Sanity preview: all deterministic rules pass.\n"
    return f"Sanity preview (deterministic rules): {json.dumps(flagged, sort_keys=True)}\n"


def stage_critic(
    ctx: StageContext,
    view: dict,
    portfolio: dict,
    current_positions: list[dict] | None = None,
    pnl_history: list[dict] | None = None,
    performance_memo: dict | None = None,
    sanity_preview: dict | None = None,
) -> dict:
    """One LLM call — Sonnet 4.6 low effort, ~$0.03. Reads view +
    portfolio + the state context the constructor saw (positions, PnL
    history, track record, sanity preview); returns {accept, critique,
    suggested_changes}. Previously the critic reviewed blind — no
    positions, no PnL, no sanity — which limited its objections to
    internal consistency only.

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
        f"{_live_mode_context_line(ctx)}"
        f"View: {json.dumps(view, sort_keys=True)}\n"
        f"Portfolio: {json.dumps(portfolio, sort_keys=True)}\n"
        f"Current broker positions: {json.dumps(current_positions or [], sort_keys=True)}\n"
        f"Recent PnL history (last cycles): {json.dumps(pnl_history or [], sort_keys=True)}\n"
        f"{_performance_memo_block(performance_memo)}"
        f"{_sanity_preview_for_critic(sanity_preview)}"
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


# Re-exported so call sites and tests have one import path; the shared
# implementation lives in lib/live_nav.py because the monitor's DD breaker
# must denominate in the SAME allocated-NAV scale as sizing (Codex P1s on
# PR #112).
LiveNavUnavailable = live_nav.LiveNavUnavailable
_broker_is_live = live_nav.broker_is_live


def _live_account_nav(ctx: StageContext) -> float:
    """Live sizing NAV via lib.live_nav.live_allocated_nav: real equity, or
    — when LIVE_NAV_CAP_USD is set — the capped starting allocation plus
    live P&L since the transition. Raises LiveNavUnavailable on ANY problem;
    this path has no fallback by design."""
    return live_nav.live_allocated_nav(ctx.broker, run_id=ctx.run_id)


def _account_nav(ctx: StageContext) -> float:
    """NAV the agent sizes against.

    Paper (today's steady state): the synthetic $2,500 baseline
    (VIRTUAL_NAV_USD-overridable) + realized P&L to date. NEVER the broker's
    ~$100k paper equity — Phase 3 removed that fallback so a missing/garbled
    env var can't make the agent size ~40× too large. Sourced from the same
    trades.jsonl-derived balance the dashboard shows, so sizing and the
    headline agree. Realized (settled) basis is intentional: it tracks
    closed P&L — compounds as wins are banked, de-risks after realized
    losses — without whipsawing on open-position mark-to-market. Falls back
    to the baseline if the synthetic computation errors.

    Live (triple lock fully raised, non-paper broker): real broker equity,
    optionally capped by LIVE_NAV_CAP_USD, prefetched once per cycle into
    ctx.live_nav_usd by run_pipeline. This branch sits OUTSIDE the paper
    try/except and never falls back to 2500/synthetic — a failed live
    equity read raises LiveNavUnavailable and the cycle skips (fail closed).
    """
    if ctx.live_nav_usd is not None:
        return ctx.live_nav_usd
    if not ctx.dry_run and _broker_is_live(ctx.broker):
        nav = _live_account_nav(ctx)
        ctx.live_nav_usd = nav
        return nav
    try:
        from lib import dashboard_data
        return dashboard_data.realized_synthetic_nav()
    except Exception:
        parsed = _parsed_virtual_nav_override()
        return parsed if parsed is not None else 2500.0


def _broker_portfolio_summary_for_meta(ctx: StageContext) -> dict:
    """Compact account summary fed to the meta-scheduler when the cycle
    didn't produce a constructed portfolio (review path).

    Reads real broker holdings + equity + cash so the cadence + intent
    decision after a review reflects what we actually hold. Passing a
    flat placeholder ({positions: [], all_cash: True, nav: 0}) would
    push meta into the "all-cash, calm" 6-12h bucket exactly when an
    open leveraged position actually needs near-term monitoring
    (Codex P1 on PR #83).

    Returns the shape `_compute_next_run_at` reads:
      {positions, all_cash, nav_usd, cash_buffer_pct}

    Defensive: broker errors fall back to all-cash + spec NAV. Dry-run
    is short-circuited upstream so this isn't exercised there.
    """
    positions = _current_positions_summary(ctx)
    nav = _account_nav(ctx)
    # Cash buffer in SYNTHETIC units, not raw broker cash (Codex P2 on PR #98):
    # the agent sizes against synthetic NAV (~$2.5k), so positions hold ~real
    # small dollars while broker cash is ~$100k — dividing raw broker cash by
    # synthetic NAV produced nonsensical thousands-of-percent buffers. Derive
    # cash as the synthetic NAV not currently deployed into open positions
    # (their market_value is on the same small-dollar scale as the sizing).
    invested = sum(abs(float(p.get("market_value") or 0.0)) for p in positions)
    cash_usd = max(0.0, nav - invested)
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
    unrealized_pl_pct, asset_class}. unrealized_pl_pct (vs cost basis) is
    precomputed so the constructor's harvest judgment ("+30% opens a
    decision") reads a direct number instead of deriving it. Returns empty
    list on dry-run or broker error.
    """
    if ctx.broker is None or ctx.dry_run:
        return []
    try:
        positions = ctx.broker.get_positions()
    except Exception:
        return []
    rows = []
    for p in positions:
        basis = abs(p.avg_cost * p.qty)
        rows.append({
            "symbol": p.symbol,
            "qty": p.qty,
            "avg_cost": p.avg_cost,
            "market_value": p.market_value,
            "unrealized_pl_usd": p.unrealized_pl_usd,
            "unrealized_pl_pct": (
                round(p.unrealized_pl_usd / basis * 100.0, 2) if basis > 0 else None
            ),
            "asset_class": p.asset_class,
        })
    return rows


def _recent_pnl_history(*, limit: int = 5, mode: str = "paper") -> list[dict]:
    """Last N cycles' regime + portfolio + realized 4h PnL.

    Reads state/nav_history.jsonl in pairs to compute realized
    cycle-over-cycle NAV % change. Joins with the matching view.json
    in the run dir to get the regime classification per cycle.

    Only rows of the given ``mode`` are paired (Codex P1 on PR #112):
    paper rows are synthetic-scale (~$2.5k) and live rows are real-equity
    scale, so a pair spanning the promotion boundary would report a
    nonsense cycle-over-cycle % (e.g. +100% on a $5k deposit) to the
    strategist for `limit` cycles.

    Returns oldest-first so the LLM reads a chronological tape.
    """
    rows = [
        r for r in state.read_nav_history(limit=1000)
        if state.record_mode(r) == mode
    ][-(limit + 1):]
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

    Codex P2 on PR #109: every feature the LLM stages can see via
    ``compact_for_llm`` must be fingerprinted, or dedup will reuse a
    cached portfolio exactly when the unhashed evidence (RSI, relative
    strength, trend quality, factor correlations) is what changed.
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
            "rsi14": round(t.get("rsi_14") or 0.0, 1),
            "rs_spy30": round(t.get("rel_strength_spy_30d") or 0.0, 2),
            "trend_r2": round(t.get("trend_r2") or 0.0, 3),
            "events": events_summary,
        })
    rows.sort(key=lambda r: r["sym"] or "")
    corr_summary = sorted(
        (
            (c.get("factor_a"), c.get("factor_b"), round(c.get("corr_30d") or 0.0, 2))
            for c in (signals_out.get("factor_correlations") or [])
        ),
        key=lambda p: (p[0] or "", p[1] or ""),
    )
    payload = {"rows": rows, "factor_correlations": corr_summary}
    return _hash_inputs(json.dumps(payload, sort_keys=True))


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


def _portfolio_is_noop(
    portfolio: dict,
    current_positions: list[dict],
    prior_portfolio: dict | None = None,
) -> bool:
    """True when the target portfolio exactly matches current broker
    holdings (same symbols, same share counts) — i.e. the execute stage
    would produce zero orders — AND every position's kill_conditions
    match the previously published portfolio's. Used to skip the LLM
    critic on hold-steady cycles.

    The kill-condition check (Codex P2, PR #109): zero orders is not
    "nothing new to critique" if the constructor rewired the stops the
    monitor enforces — widening a trailing stop or dropping a price stop
    changes risk behavior the moment current_portfolio.json is rewritten,
    so that cycle still deserves an adversarial review. Conservative:
    any difference — extra/missing symbol, fractional share drift
    > 1e-6, changed/unknown kill_conditions, or no prior portfolio to
    compare against — returns False so the critic runs."""
    target = {
        p.get("symbol"): float(p.get("shares") or 0.0)
        for p in (portfolio.get("positions") or [])
    }
    held = {
        p.get("symbol"): float(p.get("qty") or 0.0)
        for p in current_positions
        if float(p.get("qty") or 0.0) != 0.0
    }
    if set(target) != set(held):
        return False
    if not all(abs(target[s] - held[s]) <= 1e-6 for s in target):
        return False
    if not target:
        return True  # all-cash vs empty account — no stops in play
    prior_kc = {
        p.get("symbol"): json.dumps(p.get("kill_conditions") or {}, sort_keys=True)
        for p in ((prior_portfolio or {}).get("positions") or [])
    }
    return all(
        prior_kc.get(p.get("symbol"))
        == json.dumps(p.get("kill_conditions") or {}, sort_keys=True)
        for p in (portfolio.get("positions") or [])
    )


def _cooldown_fingerprint(cooldown_symbols: dict | None) -> str:
    """Stable hash of the re-entry-cooldown symbol set, used as a dedup key.

    Codex P2 on PR #99: cooldown membership shrinks purely with the passage
    of time (a symbol drops out once its 7-day window expires) even when
    signals + broker positions are unchanged. Without folding it into the
    dedup fingerprint, a portfolio kept flat *because* a symbol was in
    cooldown could be reused indefinitely, suppressing a now-valid re-entry
    until some unrelated signal/position change invalidated dedup. Including
    the sorted cooldown symbol set means cooldown expiry bumps the hash and
    forces a fresh cycle so the constructor reconsiders the re-entry.
    """
    syms = sorted((cooldown_symbols or {}).keys())
    return _hash_inputs(json.dumps(syms, sort_keys=True))


def _memo_fingerprint(performance_memo: dict | None) -> str:
    """Stable hash of the performance memo, used as a dedup key.

    Codex P2 on PR #109: the memo is LLM-visible evidence that can change
    while signals, broker positions, and cooldown membership all stay
    fixed — e.g. trade-sync backfills a fill/fee, or a monitor kill event
    re-tags a recent exit. Without this key, dedup would reuse the cached
    portfolio exactly when the agent's track record gained new
    information. The memo is cheap to build ($0, pure Python over state),
    so it is computed before the dedup check.

    Deliberately NOT included: pnl_history (_recent_pnl_history). Every
    completed cycle appends its own row, so fingerprinting it would make
    consecutive cycles always differ and disable dedup outright; its
    inputs (fills, marks, regime) are already covered by the signals,
    positions, and memo fingerprints.
    """
    return _hash_inputs(json.dumps(performance_memo, sort_keys=True, default=str))


def _notes_fingerprint(pending_notes: list[dict] | None) -> str:
    """Stable hash of the pending user-note id set, used as a dedup key.

    A note typed into the dashboard between two otherwise-identical cycles
    is new LLM-visible context — dedup must NOT reuse the cached portfolio
    over it. The publish block stores the EMPTY-set fingerprint (the cached
    portfolio's reference state is "every injected note consumed, none
    pending"), so an unchanged-market cycle can dedup again once a note has
    been injected, while any pending note — even one typed mid-cycle after
    pending_notes was captured — mismatches and forces a real run; contrast
    with memo_fp, which is deliberately stored pre-dedup."""
    ids = sorted(n.get("id") or "" for n in (pending_notes or []))
    return _hash_inputs(json.dumps(ids, sort_keys=True))


def _manual_closes_fingerprint(manual_closes: list[dict] | None) -> str:
    """Stable hash of the recent-manual-close set fed to the prompts.

    Membership decays with time alone (a close ages out of the 7-day window
    with no signal/position/fill change) — the same bug class
    _cooldown_fingerprint fixed (Codex P2, PR #99). Without this key, a
    portfolio held flat *because* the operator closed a symbol could be
    dedup-reused past the window's expiry, suppressing a now-fair re-entry."""
    rows = sorted(
        (mc.get("symbol") or "", (mc.get("at") or "")[:10])
        for mc in (manual_closes or [])
    )
    return _hash_inputs(json.dumps(rows, sort_keys=True))


def _check_cycle_dedup(
    signals_out: dict,
    current_positions: list[dict],
    cooldown_symbols: dict | None = None,
    memo_fp: str | None = None,
    notes_fp: str | None = None,
    manual_closes_fp: str | None = None,
) -> dict | None:
    """Return the cached portfolio dict if dedup applies; None otherwise.

    Dedup applies when:
      - state/last_cycle_hash.json exists
      - signals_fingerprint matches the prior cycle's
      - positions_fingerprint matches the prior cycle's
      - cooldown_fingerprint matches the prior cycle's
      - memo_fingerprint matches the prior cycle's (when provided; a hash
        file written before this key existed fails the match, which just
        costs one fresh cycle)
      - notes_fingerprint + manual_closes_fingerprint match the prior
        cycle's (same migration semantics: a legacy hash file without the
        keys fails the match once, then self-heals)
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
    cooldown_fp = _cooldown_fingerprint(cooldown_symbols)
    if (
        last.get("signals_fingerprint") != signals_fp
        or last.get("positions_fingerprint") != positions_fp
        or last.get("cooldown_fingerprint") != cooldown_fp
        or last.get("memo_fingerprint") != memo_fp
        or last.get("notes_fingerprint") != notes_fp
        or last.get("manual_closes_fingerprint") != manual_closes_fp
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
            "cycle dedup: signals, broker positions, cooldown set, and "
            "performance memo all unchanged from prior cycle. Skipped "
            "strategist + construct + execute; cached portfolio retained."
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


def _update_cycle_dedup_hash(
    signals_out: dict,
    current_positions: list[dict],
    cooldown_symbols: dict | None = None,
    memo_fp: str | None = None,
    notes_fp: str | None = None,
    manual_closes_fp: str | None = None,
) -> None:
    """Called at the END of a successful cycle to record the fingerprints
    for the NEXT cycle's dedup check. ``memo_fp`` is the fingerprint of
    the memo THIS cycle's LLM stages actually read (computed pre-dedup),
    so the next cycle's freshly built memo is compared against the
    evidence that produced the cached portfolio. ``notes_fp`` is the
    opposite: recomputed by the caller AFTER marking this cycle's notes
    consumed (normally an empty-set hash), so an unchanged-market cycle
    right after a consumed note can still dedup. Failures are non-fatal."""
    try:
        state.write_json(state.LAST_CYCLE_HASH, {
            "signals_fingerprint": _signals_fingerprint(signals_out),
            "positions_fingerprint": _positions_fingerprint(current_positions),
            "cooldown_fingerprint": _cooldown_fingerprint(cooldown_symbols),
            "memo_fingerprint": memo_fp,
            "notes_fingerprint": notes_fp,
            "manual_closes_fingerprint": manual_closes_fp,
            "updated_at": state.utcnow_iso(),
        })
    except Exception:
        pass


def _peak_nav_30d(*, mode: str = "paper") -> float:
    """Highest NAV observed in the last 30 calendar days from
    state.read_nav_history. Used by risk.adaptive_position_cap_pct
    to dial size down in drawdown.

    Only rows of the given ``mode`` count (Codex P1 on PR #112): paper
    rows are synthetic-scale and live rows are real-equity scale, so a
    cross-era peak makes the drawdown-adaptive caps either never tighten
    (live equity above the paper peak) or tighten spuriously for 30 days
    (live funding below it). The live era cold-starts at peak 0.0, which
    risk.adaptive_position_cap_pct already treats as "no history → full
    base cap".

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
        if state.record_mode(r) != mode:
            continue
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


def _publishable_portfolio(portfolio: dict) -> dict:
    """Return a copy of ``portfolio`` with any position the order layer would
    refuse (option-shaped or non-universe) stripped out.

    current_portfolio.json must never record a symbol that can't actually be
    held: the order layer drops such targets into ``skipped`` and submits no
    order, so publishing them would make the monitor treat them as holdings
    (leaving real holdings as orphans, dropping their kill conditions) and let
    a later dedup-skip reuse an invalid/unfilled portfolio. Strip them and
    recompute ``all_cash`` when nothing tradable remains. Returns the original
    object unchanged when every position is tradable (the common case).
    """
    from lib import orders
    positions = portfolio.get("positions") or []
    kept = [p for p in positions if orders.is_tradable_target(p)]
    if len(kept) == len(positions):
        return portfolio
    cleaned = dict(portfolio)
    cleaned["positions"] = kept
    # The stripped positions were never submitted, so their intended allocation
    # reverts to cash (NAV is unchanged — it's total account value). Add the
    # freed notional back so cash_usd / cash_buffer_pct match what was actually
    # held, keeping the published portfolio, NAV row, and dedup cache honest.
    nav = float(portfolio.get("nav_usd") or 0.0)
    dropped = [p for p in positions if not orders.is_tradable_target(p)]
    freed = sum(float(p.get("position_pct") or 0.0) / 100.0 * nav for p in dropped)
    if freed:
        cleaned["cash_usd"] = float(portfolio.get("cash_usd") or 0.0) + freed
        if nav > 0:
            cleaned["cash_buffer_pct"] = round(cleaned["cash_usd"] / nav * 100.0, 4)
    if not kept:
        cleaned["all_cash"] = True
    return cleaned


# Orchestrator-meta returns a next-run timestamp; bounds enforced here.
META_MIN_HOURS = 1.0
META_MAX_HOURS = 24.0
# Tolerance absorbs second-precision rounding + LLM round-trip latency.
META_BOUND_TOLERANCE_SECONDS = 30.0
# A review fired more than this far before the next market open is
# analysing a regime nothing can act on until the open — e.g. a Saturday
# review ~48h before Monday's bell. Such picks are downgraded to "trade"
# so the market gate $0-skips them and rolls next_run to the open,
# instead of billing ~$0.13 for stale reflection. Weekday-evening
# reviews (next open ~12-16h away) are unaffected. (2026-08-31 cost
# lever, user-authorized.)
REVIEW_STALENESS_MAX_HOURS = 24.0


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
            f"{_live_mode_context_line(ctx)}"
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
            "review = signals + strategist only, no orders, ~$0.13 cost; "
            "use for post-close reflection. trade = full pipeline. "
            "Note: a review scheduled more than 24h before the next market "
            "open (e.g. on a weekend) is auto-downgraded to trade, so don't "
            "pick review when the market won't open within a day of the "
            "chosen time. Return JSON only."
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
    # Weekend/holiday staleness guard: a review that would fire more than
    # REVIEW_STALENESS_MAX_HOURS before the next open can't inform any
    # trade — downgrade so the gate $0-skips it at fire time.
    if next_intent == "review" and not market_is_open and next_open:
        try:
            open_dt = datetime.fromisoformat(
                str(next_open).replace("Z", "+00:00")
            )
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
            gap_hours = (open_dt - at).total_seconds() / 3600.0
            if gap_hours > REVIEW_STALENESS_MAX_HOURS:
                next_intent = "trade"
                rationale_suffix += (
                    f" (review pick was {gap_hours:.0f}h before next open; "
                    "downgraded to trade — the market gate will roll it to the open)"
                )
        except (ValueError, TypeError):
            pass
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
        positions_ok = True
        current: list = []
        try:
            current = ctx.broker.get_positions()
        except Exception as e:
            # Fail closed: if we can't read current broker holdings we cannot
            # safely diff against the target — planning opens against an
            # assumed-empty account would double up exposure on top of
            # whatever is actually held. Skip planning/submission entirely
            # this cycle; still write an (empty) orders.json + next_run + NAV
            # below so the next cycle is scheduled and the operator can see
            # the skip. No orders are submitted, so no trades_sync is needed.
            positions_ok = False
            next_run["order_plan_error"] = f"get_positions: {type(e).__name__}: {e}"
            next_run["orders_skipped_reason"] = "get_positions failed — failing closed"
            # Signal to run_pipeline that the target was NOT reconciled against
            # the broker account. The caller must NOT publish this unexecuted
            # target as current_portfolio.json nor advance the dedup hash —
            # doing so would make monitor/dashboard/dedup treat unfilled targets
            # as held positions and real prior holdings as orphans (dropping
            # their configured kill conditions). Preserve the prior state.
            next_run["current_portfolio_unreconciled"] = True
            state.write_json(
                state.run_dir(ctx.run_id) / "orders.json",
                {
                    "run_id": ctx.run_id,
                    "submitted_at": state.utcnow_iso(),
                    "order_ids": [],
                },
            )

    if (
        ctx.broker is not None
        and not ctx.dry_run
        and orders.is_enabled()
        and positions_ok
    ):
        plan = orders.diff_portfolio(portfolio, current)
        # Phase 2: when the 8% daily-drawdown breaker is active, allow
        # de-risking (full closes AND same-sign reductions — both are SELLs on
        # a long-only book) but skip BUYs that add/open exposure for the rest
        # of the UTC day. The flag auto-expires next day. Codex P1 (PR #98):
        # _plan_for_symbol puts reductions in `requests`, so filter by side
        # rather than dropping every request.
        if risk.dd_breaker_enabled() and state.dd_halt_active(
            mode=live_gate.trading_mode(ctx.broker)
        ):
            derisking = [r for r in plan.requests if r.side == "sell"]
            skipped_opens = len(plan.requests) - len(derisking)
            plan = orders.OrderPlan(
                requests=derisking, closes=plan.closes, skipped=plan.skipped,
            )
            next_run["dd_halt"] = {
                "active": True,
                "detail": state.read_dd_halt() or {},
                "opens_skipped": skipped_opens,
                "note": "8% daily drawdown breaker active — buys skipped; closes + reductions allowed",
            }
        results = orders.submit_plan(plan, broker=ctx.broker)
        next_run["order_plan"] = {
            "total_legs": plan.total_legs,
            "closes": len(plan.closes),
            "opens": len(plan.requests),
            "skipped": len(plan.skipped),
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
                mode=live_gate.trading_mode(ctx.broker),
            )
        except Exception as e:
            next_run["trades_sync_error"] = (
                f"sync_fills_from_alpaca: {type(e).__name__}: {e}"
            )
    if not ctx.dry_run:
        state.write_json(state.NEXT_RUN, next_run)
    # NAV history: one row per cycle for the dashboard equity curve.
    # Phase 3: nav_usd is stamped DETERMINISTICALLY from the synthetic
    # balance (trades.jsonl-derived realized P&L over the $2,500 baseline),
    # NOT the constructor's self-reported portfolio.nav_usd. trades_sync
    # ran just above, so this reflects ACTUAL fills — even if some orders
    # were skipped/failed this cycle, the row is honest (only filled trades
    # count). This is the same number _account_nav sizes against and the
    # dashboard shows. nav_source is always "virtual" now (synthetic units,
    # never raw broker equity), so the dashboard applies no broker offset.
    #
    # Skip the row entirely when the cycle is unreconciled (broker positions
    # couldn't be read, so the target was never executed): the positions_count
    # / cash_usd / modelled PnL fields are derived from the unexecuted target
    # and would otherwise record it as if held, corrupting the equity curve and
    # the strategist's PnL feedback. A missing row during an outage is honest.
    if not ctx.dry_run and not next_run.get("current_portfolio_unreconciled"):
        from lib import dashboard_data as _dd
        from lib import pnl as pnl_lib
        # Base the row on the publishable subset — positions the order layer
        # refused (option-shaped / non-universe) were never submitted or held,
        # so their positions_count / cash / modelled PnL must not be recorded
        # as if held (matches what _publishable_portfolio writes to
        # current_portfolio.json).
        nav_portfolio = _publishable_portfolio(portfolio)
        breakdown = pnl_lib.compute_portfolio_pnl(portfolio=nav_portfolio, marks=None)
        try:
            synthetic_nav = _dd.realized_synthetic_nav()
        except Exception:
            synthetic_nav = nav_portfolio.get("nav_usd", 0.0)
        # Live rows must record the SAME live/capped NAV the agent sizes
        # against (Codex P1 on PR #112): _peak_nav_30d + _recent_pnl_history
        # compare these rows to _account_nav, so a live row written in
        # paper-synthetic units would leave the drawdown-adaptive caps blind
        # to live drawdowns. nav_source="live" keeps the legacy NAV-offset
        # display path from touching them.
        _nav_mode = live_gate.trading_mode(ctx.broker)
        if _nav_mode == "live" and ctx.live_nav_usd is not None:
            _nav_value, _nav_source = ctx.live_nav_usd, "live"
        else:
            _nav_value, _nav_source = synthetic_nav, "virtual"
        state.append_nav({
            "run_id": ctx.run_id,
            "at": state.utcnow_iso(),
            "nav_usd": _nav_value,
            "nav_source": _nav_source,
            "mode": _nav_mode,
            "cash_usd": nav_portfolio.get("cash_usd", 0.0),
            "positions_count": len(nav_portfolio.get("positions", [])),
            "all_cash": nav_portfolio.get("all_cash", False),
            "gross_pnl_usd": breakdown.gross_pnl_usd,
            "modelled_costs_usd": breakdown.modelled_costs_usd,
            "net_pnl_usd": breakdown.net_pnl_usd,
        })
    return next_run


# ----- pipeline driver -----


def _stage_cost_from_delta(run_id: str, n_before: int) -> tuple[float, float]:
    """Real (cost_usd, prompt_cache_hit_pct) for the cost rows a stage just
    appended. lib/llm.py records every call to costs.jsonl synchronously
    (schema retries included), so the rows past ``n_before`` are exactly
    this stage's spend. Hit% is the input-side ratio — identical semantics
    to llm.CallUsage.cache_hit_pct. Reporting must never break the
    pipeline: any read error degrades to (0.0, 0.0), the values every
    decision row carried before this existed. Deterministic and dry-run
    stages append nothing → (0.0, 0.0) exactly as before.
    """
    try:
        delta = state.read_costs_for_run(run_id)[n_before:]
        cost = sum(float(r.get("cost_usd") or 0.0) for r in delta)
        inp = sum(int(r.get("input_tokens") or 0) for r in delta)
        creation = sum(int(r.get("cache_creation_input_tokens") or 0) for r in delta)
        read = sum(int(r.get("cache_read_input_tokens") or 0) for r in delta)
        denom = inp + creation + read
        hit = (100.0 * read / denom) if denom > 0 else 0.0
        return max(0.0, round(cost, 6)), min(100.0, max(0.0, hit))
    except Exception:
        return 0.0, 0.0


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
    # Snapshot the cost ledger so the decision row can carry this stage's
    # REAL spend + cache hit rather than the historical hard-coded 0.0.
    # Note: stage_execute's runner also fires the nested meta call, whose
    # cost rows land in this stage's delta — deliberately attributed to
    # the "execute" decision row (costs.jsonl keeps the per-call truth).
    try:
        n_costs_before = len(state.read_costs_for_run(ctx.run_id))
    except Exception:
        n_costs_before = 0
    output = runner()
    if schema:
        state.validate(output, schema)

    out_path = state.run_dir(ctx.run_id) / output_filename
    state.write_json(out_path, output)

    cost_usd, cache_hit_pct = _stage_cost_from_delta(ctx.run_id, n_costs_before)
    state.append_decision({
        "run_id": ctx.run_id,
        "stage": stage_id,
        "model": model,
        "inputs_hash": _hash_inputs(*inputs_hash_parts),
        "output_ref": output_filename,
        "prompt_cache_hit_pct": cache_hit_pct,
        "cost_usd": cost_usd,
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

    Cost: ~$0.13 (strategist + meta; ~85% of it output/thinking tokens).
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

    # Pull current positions + PnL history + track record so the
    # strategist's regime commentary is informed by what we currently
    # hold and how past calls actually performed.
    current_positions = _current_positions_summary(ctx)
    pnl_history = _recent_pnl_history(limit=5, mode=live_gate.trading_mode(ctx.broker))
    performance_memo = feedback.build_performance_memo_safe()

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
            performance_memo=performance_memo,
        ),
        inputs_hash_parts=(rid, json.dumps(signals_out, sort_keys=True)),
        model=strat_model,
    )

    # ----- Meta-scheduler: pick next-run window + intent for next cycle.
    # Feed real broker holdings (not a flat placeholder) so meta's
    # cadence + intent decision reflects what we actually hold. A
    # placeholder would push meta into the "all-cash" bucket exactly
    # when a held leveraged position needs near-term monitoring after
    # the reflection cycle.
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

    # Live sizing prefetch (fail closed) — BEFORE the review branch, the
    # market gate, and any LLM spend. On a live broker the cycle must know
    # the real equity it sizes against; if that read fails there is no safe
    # fallback (never 2500/synthetic on live), so skip the whole cycle and
    # retry soon. The env-only check catches the broker-construction-failed
    # case (Codex P1 on PR #112): with the full triple lock raised but
    # broker=None, the pipeline would otherwise run the no-broker paper path
    # (market gate open, synthetic sizing, LLM spend) — that state is
    # live-NAV-unavailable, not paper. Paper and dry-run are untouched:
    # live_nav_usd stays None and _account_nav sizes synthetically as before.
    if not dry_run and (_broker_is_live(broker) or live_gate.trading_mode() == "live"):
        try:
            ctx.live_nav_usd = _live_account_nav(ctx)
        except LiveNavUnavailable as e:
            from datetime import timedelta
            next_at = (state.utcnow() + timedelta(minutes=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            next_run = {
                "run_id": rid,
                "next_run_at": next_at,
                "rationale": (
                    f"cycle skipped: live NAV unavailable ({e}). No orders "
                    "and no LLM calls were made; retrying in 30 minutes. "
                    "Check LIVE_NAV_CAP_USD and broker connectivity."
                ),
                # Preserve the scheduled intent (Codex P2 on PR #112): a
                # transient equity-read failure during an after-hours review
                # must retry as a review, not convert into a trade cycle
                # that the market gate will then skip until next open.
                "cycle_intent": cycle_intent,
                "live_nav_unavailable": True,
            }
            state.write_json(state.run_dir(rid) / "next_run.json", next_run)
            state.write_json(state.NEXT_RUN, next_run)
            state.append_decision({
                "run_id": rid,
                "stage": "live_nav_prefetch",
                "model": "local-deterministic",
                "inputs_hash": _hash_inputs(rid),
                "output_ref": "next_run.json",
                "prompt_cache_hit_pct": 0.0,
                "cost_usd": 0.0,
                "started_at": state.utcnow_iso(),
                "ended_at": state.utcnow_iso(),
                "status": "skipped_live_nav_unavailable",
                "risk_warning": RISK_WARNING,
                "cycle_intent": cycle_intent,
                "intent_source": intent_source,
            })
            return {
                "run_id": rid,
                "live_nav_unavailable": True,
                "next_run": next_run,
            }

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
            # Distinguish a clean closed-market skip from a fail-closed skip
            # caused by an unreachable/missing broker clock — the operator and
            # dashboard need to tell a transient broker outage apart from a
            # normal weekend/holiday.
            gate_status = "skipped_clock_error" if ms.clock_error else "skipped_market_closed"
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
                "status": gate_status,
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

    # Refresh the fill log from Alpaca before deriving cooldown state, so a
    # symbol exited between cycles outside this orchestrator (manual close,
    # monitor flatten) is reflected in the re-entry cooldown + dedup hash.
    pre_cooldown_sync_error = _sync_fills_before_cooldown(ctx)

    # Symbols fully exited within the re-entry cooldown window. Computed
    # before dedup so it can participate in the dedup fingerprint (cooldown
    # membership changes with time alone — see _cooldown_fingerprint) and
    # then reused for the constructor prompt + sanity guardrail below.
    cooldown_symbols = _cooldown_symbols_now()

    # Track-record memo (factor win/loss record, confidence calibration,
    # recent exits) — fed to strategist + constructor + critic as
    # calibration evidence. $0; guarded so corrupt state can't kill a run.
    # Built BEFORE dedup (after the fill sync above) so its fingerprint
    # participates in the dedup check: a backfilled fill or fresh kill
    # event changes the evidence the LLM stages would read even when
    # signals/positions/cooldown are unchanged.
    performance_memo = feedback.build_performance_memo_safe()
    memo_fp = _memo_fingerprint(performance_memo)

    # Operator context: pending dashboard notes (injected verbatim into the
    # strategist + constructor, consumed at end-of-cycle) and recent manual
    # closes (source=dashboard kill events → explicit prompt line). Both are
    # $0 and computed pre-dedup so they participate in the fingerprint — a
    # fresh note or an expiring manual-close entry must force a real cycle.
    pending_notes = [] if dry_run else state.pending_user_notes(
        mode=live_gate.trading_mode(ctx.broker),
    )
    manual_closes = [] if dry_run else _recent_manual_closes(
        mode=live_gate.trading_mode(ctx.broker),
    )
    notes_fp = _notes_fingerprint(pending_notes)
    manual_closes_fp = _manual_closes_fingerprint(manual_closes)

    # ----- Cycle dedup -----
    # Skip strategist + construct + execute and reuse the last portfolio
    # when the signals fingerprint, broker position set, cooldown set, AND
    # performance memo all match the prior cycle. Saves ~$0.25 on a quiet
    # 4h window where nothing has moved meaningfully.
    if not dry_run:
        dedup = _check_cycle_dedup(
            signals_out, current_positions, cooldown_symbols, memo_fp,
            notes_fp=notes_fp, manual_closes_fp=manual_closes_fp,
        )
        if dedup is not None:
            next_run = _write_dedup_next_run(rid, portfolio=dedup["portfolio"], ctx=ctx)
            return {
                "run_id": rid,
                "signals": signals_out,
                "dedup_skipped": True,
                "next_run": next_run,
            }

    # Recent PnL feedback — last 5 cycles' regime + realized 4h PnL
    # so the strategist can self-correct drift across cycles. NOT part of
    # the dedup fingerprint — see _memo_fingerprint for why.
    pnl_history = _recent_pnl_history(limit=5, mode=live_gate.trading_mode(ctx.broker))

    # ----- Stage 2: strategist (1 LLM call) -----
    view = _run_stage(
        ctx=ctx, stage_id="strategist", schema="view.schema.json",
        output_filename="view.json",
        runner=lambda: stage_strategist(
            ctx, signals_out,
            current_positions=current_positions,
            pnl_history=pnl_history,
            performance_memo=performance_memo,
            user_notes=pending_notes,
            manual_closes=manual_closes,
        ),
        inputs_hash_parts=(rid, json.dumps(signals_out, sort_keys=True)),
        model=strat_model,
    )

    # Two-tier per-position bounds from current drawdown, fed to the
    # constructor as soft guidance. Real enforcement is in sanity:
    #   entry/add cap  → rule: entry_cap_on_adds
    #   hold ceiling   → rule: position_within_adaptive_cap
    _nav_now = _account_nav(ctx)
    _peak_now = _peak_nav_30d(mode=live_gate.trading_mode(ctx.broker))
    adaptive_cap = risk.adaptive_position_cap_pct(
        current_nav=_nav_now, peak_nav_30d=_peak_now,
    )
    hold_ceiling = risk.adaptive_hold_ceiling_pct(
        current_nav=_nav_now, peak_nav_30d=_peak_now,
    )

    # ----- Stage 3: construct (1 LLM call) -----
    # cooldown_symbols (computed above, pre-dedup) is fed to the constructor
    # as soft prompt guidance (overridable at confidence >
    # REENTRY_COOLDOWN_OVERRIDE_CONFIDENCE) and to the sanity guardrail.
    portfolio = _run_stage(
        ctx=ctx, stage_id="construct", schema="portfolio.schema.json",
        output_filename="portfolio.json",
        runner=lambda: stage_construct(
            ctx, signals_out, view,
            current_positions=current_positions,
            pnl_history=pnl_history,
            adaptive_cap_pct=adaptive_cap,
            hold_ceiling_pct=hold_ceiling,
            cooldown_symbols=cooldown_symbols,
            performance_memo=performance_memo,
            user_notes=pending_notes,
            manual_closes=manual_closes,
        ),
        inputs_hash_parts=(
            rid,
            json.dumps(signals_out, sort_keys=True),
            json.dumps(view, sort_keys=True),
        ),
        model=cons_model,
    )
    if not risk.position_band_ok(len(portfolio["positions"]), portfolio["all_cash"]):
        raise RuntimeError("portfolio violates 1–12 band / all-cash invariant")

    # ----- Stage 3.5: critic (1 LLM call, ~$0.03) -----
    # Adversarial review of the constructor's portfolio. If accept=true
    # proceed to sanity. If accept=false, retry the constructor ONCE
    # with the critique fed back; the retry's portfolio is then used.
    #
    # Cost guard: when the constructed portfolio is a NO-OP against current
    # broker holdings (same symbols, same share counts — zero orders would
    # be produced) AND its kill_conditions match the previously published
    # portfolio's, skip the LLM critic. There is nothing for an adversarial
    # review to veto on a true hold-steady cycle; the holdings were already
    # critiqued when they were opened. Saves ~$0.03 on quiet cycles.
    #
    # The free deterministic sanity rules also run here as a PREVIEW so the
    # critic argues against concrete flags instead of reviewing blind. The
    # authoritative sanity report still runs after the critique loop on the
    # final (possibly retried) portfolio.
    sanity_preview = None
    if not dry_run:
        try:
            sanity_preview = sanity.run_sanity_checks(
                portfolio, view,
                signals=signals_out,
                nav_usd=_account_nav(ctx),
                adaptive_cap_pct=adaptive_cap,
                hold_ceiling_pct=hold_ceiling,
                cooldown_symbols=cooldown_symbols,
                current_positions=current_positions,
            )
        except Exception:
            sanity_preview = None

    prior_published = (
        state.read_json(state.CURRENT_PORTFOLIO)
        if state.CURRENT_PORTFOLIO.exists() else None
    )
    if not dry_run and _portfolio_is_noop(portfolio, current_positions, prior_published):
        critique = _run_stage(
            ctx=ctx, stage_id="critic", schema="critique.schema.json",
            output_filename="critique.json",
            runner=lambda: {
                "accept": True,
                "critique": (
                    "skipped: portfolio is a no-op against current broker "
                    "holdings (zero orders) — nothing new to critique"
                ),
                "suggested_changes": [],
            },
            inputs_hash_parts=(rid, json.dumps(portfolio, sort_keys=True)),
            model="local-deterministic",
        )
    else:
        critique = _run_stage(
            ctx=ctx, stage_id="critic", schema="critique.schema.json",
            output_filename="critique.json",
            runner=lambda: stage_critic(
                ctx, view, portfolio,
                current_positions=current_positions,
                pnl_history=pnl_history,
                performance_memo=performance_memo,
                sanity_preview=sanity_preview,
            ),
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
                f"{_live_mode_context_line(ctx)}"
                f"Signals: {json.dumps(signals.compact_for_llm(signals_out), sort_keys=True)}\n"
                f"Strategist view: {json.dumps(view, sort_keys=True)}\n"
                f"Current broker positions: {json.dumps(current_positions, sort_keys=True)}\n"
                f"Recent PnL history: {json.dumps(pnl_history, sort_keys=True)}\n"
                f"{_performance_memo_block(performance_memo)}"
                f"NAV (USD): {_account_nav(ctx):.2f}\n"
                f"Entry/add cap %: {adaptive_cap:.2f} (open/add limit); "
                f"hold ceiling %: {hold_ceiling:.2f} (a drifted winner may be "
                f"kept up to this; don't trim back to the entry cap)\n"
                f"{_cooldown_prompt_line(cooldown_symbols)}"
                f"{_user_notes_block(pending_notes)}"
                f"{_manual_close_prompt_line(manual_closes)}"
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
        hold_ceiling_pct=hold_ceiling,
        cooldown_symbols=cooldown_symbols,
        current_positions=current_positions,
    )
    sanity_report["run_id"] = rid
    sanity_report["generated_at"] = state.utcnow_iso()
    state.write_json(
        state.run_dir(rid) / "sanity.json", sanity_report, schema="sanity.schema.json",
    )
    sanity_blocked = (
        sanity.block_on_fail_enabled() and sanity_report["status"] == "fail"
    )
    # The entry/add cap is a HARD risk invariant, NOT an advisory check. It used
    # to be enforced by the schema's flat 15% `position_pct` maximum; we raised
    # that to 25 so an already-open winner can be represented at its drifted
    # weight, which means the schema no longer rejects a fresh 16–25% open. A
    # failed `entry_cap_on_adds` means the constructor tried to OPEN or ADD above
    # the entry cap — never submit that, regardless of SANITY_BLOCK_ON_FAIL.
    entry_cap_breached = any(
        r["name"] == "entry_cap_on_adds" and r["status"] == "fail"
        for r in sanity_report["rules"]
    )
    sanity_blocked = sanity_blocked or entry_cap_breached

    if sanity_blocked:
        if entry_cap_breached:
            block_rationale = (
                "stage_execute skipped: entry_cap_on_adds failed — the "
                "constructor opened or added to a position above the entry cap, "
                "a hard risk invariant enforced independently of "
                "SANITY_BLOCK_ON_FAIL. Cadence preserved via heuristic so the "
                "scheduler keeps firing; see sanity.json for offender details."
            )
        else:
            block_rationale = (
                "stage_execute skipped: SANITY_BLOCK_ON_FAIL=true and sanity "
                f"report status=fail ({sanity_report['summary']['fail']} rule "
                "failure(s)). Cadence preserved via heuristic so the scheduler "
                "keeps firing; see sanity.json for offender details."
            )
        next_run = {
            "run_id": rid,
            "next_run_at": _default_next_run_at(portfolio),
            "rationale": block_rationale,
            "sanity_block": {
                "status": sanity_report["status"],
                "failed_rules": [
                    r["name"] for r in sanity_report["rules"] if r["status"] == "fail"
                ],
            },
            "cycle_intent": "trade",
        }
        if pre_cooldown_sync_error:
            next_run["pre_cooldown_sync_error"] = pre_cooldown_sync_error
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
        if pre_cooldown_sync_error:
            next_run["pre_cooldown_sync_error"] = pre_cooldown_sync_error
        state.write_json(state.run_dir(rid) / "next_run.json", next_run)
        if not dry_run:
            state.write_json(state.NEXT_RUN, next_run)

    # Fail-closed: if stage_execute couldn't read broker positions, the target
    # was never reconciled against the account (no orders submitted). Do NOT
    # publish the unexecuted target as current_portfolio.json nor advance the
    # dedup fingerprint — that would make the monitor/dashboard/dedup treat
    # unfilled targets as current holdings and real prior holdings as orphans,
    # dropping their tailored kill conditions. Preserve the previous state so
    # the next cycle reconciles cleanly once positions can be read again.
    #
    # The same applies when sanity_blocked: stage_execute was skipped entirely,
    # so the target was never submitted. Publishing it would record an
    # unexecuted target as a live holding, and advancing the dedup fingerprint
    # would let the next identical cycle short-circuit against that false state
    # and never re-attempt construction. Skip publish + dedup on the blocked
    # path so the next cycle reconstructs from real broker state.
    if (
        not dry_run
        and not sanity_blocked
        and not next_run.get("current_portfolio_unreconciled")
    ):
        # Publish only the tradable subset — never record an option-shaped or
        # non-universe position the order layer refused (it was never held).
        state.write_json(state.CURRENT_PORTFOLIO, _publishable_portfolio(portfolio))
        # Mark this cycle's injected notes consumed ONLY on the published
        # path — a failed/blocked cycle leaves them pending so they
        # re-inject next cycle (worst case the agent reads a note twice).
        if pending_notes:
            try:
                state.append_user_notes_consumed(
                    [n["id"] for n in pending_notes if n.get("id")], run_id=rid,
                )
            except Exception:
                pass
        # Update the cycle-dedup fingerprints so the next cycle can
        # short-circuit cleanly if nothing material changed. memo_fp is the
        # pre-dedup fingerprint — the memo this cycle's LLM stages read; a
        # fill or kill event recorded since (e.g. by this cycle's execute)
        # changes the next cycle's freshly built memo and invalidates dedup.
        # notes_fp is recomputed POST-consumption (normally an empty-set
        # hash) so the next quiet cycle can dedup once the note is injected.
        _update_cycle_dedup_hash(
            signals_out, current_positions, cooldown_symbols, memo_fp,
            # Empty-set fingerprint, NOT a re-read of pending notes from
            # disk (Codex P2, PR #114): the cached portfolio reflects
            # "every injected note consumed, none pending", so ANY pending
            # note at the next cycle — including one typed mid-cycle after
            # this cycle captured pending_notes — must mismatch and force
            # a real run. A disk re-read here would absorb the mid-cycle
            # note into the stored hash and dedup would skip right past it.
            notes_fp=_notes_fingerprint([]),
            manual_closes_fp=manual_closes_fp,
        )

    return {
        "run_id": rid,
        "signals": signals_out,
        "view": view,
        "portfolio": portfolio,
        "sanity": sanity_report,
        "next_run": next_run,
    }


def _record_pipeline_crash(
    *, run_id: str, exc: Exception, dry_run: bool, cli_intent: str | None,
) -> None:
    """Best-effort crash bookkeeping for an unhandled pipeline exception.

    Three independent steps — error.json artifact, decision-log row,
    conservative reschedule — each in its own try/except so a broken
    state/ dir can never mask the original traceback (main() re-raises
    it after this returns). Never called on a healthy cycle.
    """
    import traceback as _traceback
    from datetime import timedelta

    short = f"{type(exc).__name__}: {exc}"[:500]

    # 1) error.json — even a crash before signals.json leaves an artifact,
    #    so incomplete-cycle detection has something to key on.
    try:
        state.write_json(state.run_dir(run_id) / "error.json", {
            "run_id": run_id,
            "failed_at": state.utcnow_iso(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": _traceback.format_exc(),
        })
    except Exception:
        pass

    # 2) Decision row. A cost-cap stop is a designed guardrail, not an
    #    unresolved failure — it reuses the schema's "aborted" status,
    #    which the §Promotion failure gate deliberately does not count.
    #    Everything else is "error" and does count.
    try:
        is_cap = isinstance(exc, llm.CostCapExceeded)
        cycle_intent, intent_source = _load_cycle_intent(
            cli_intent=cli_intent, ignore_cap=False,
        )
        now = state.utcnow_iso()
        state.append_decision({
            "run_id": run_id,
            "stage": "pipeline",
            "model": "local-deterministic",
            "inputs_hash": _hash_inputs(run_id),
            "output_ref": "error.json",
            "prompt_cache_hit_pct": 0.0,
            "cost_usd": 0.0,  # real spend is authoritative in costs.jsonl
            "started_at": now,
            "ended_at": now,
            "status": "aborted" if is_cap else "error",
            "error": short,
            "risk_warning": RISK_WARNING,
            "cycle_intent": cycle_intent,
            "intent_source": intent_source,
        })
    except Exception:
        pass

    # 3) Conservative reschedule so the scheduler doesn't pin on a stale
    #    next_run.json until the daily fallback timer. Skipped when the
    #    operator has halted, and when this run already scheduled itself
    #    (crash after stage_execute wrote NEXT_RUN — never override the
    #    meta-scheduler's pick).
    try:
        if state.is_halted():
            return
        try:
            existing = json.loads(state.NEXT_RUN.read_text(encoding="utf-8"))
            if existing.get("run_id") == run_id:
                return
        except (OSError, json.JSONDecodeError):
            pass

        if isinstance(exc, llm.CostCapExceeded) and getattr(exc, "cap", "") == "daily":
            # Mirror the documented daily-cap behaviour: refuse until next UTC day.
            next_day = (state.utcnow() + timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0,
            )
            next_at = next_day.strftime("%Y-%m-%dT%H:%M:%SZ")
            why = "daily cost cap reached; next cycle after UTC midnight"
        elif isinstance(exc, llm.CostCapExceeded):
            # A fresh run gets a fresh per-run budget; 4h avoids hot-looping
            # spend while the daily cap still bounds the day.
            next_at = (state.utcnow() + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
            why = "per-run cost cap reached; retry with a fresh run budget"
        else:
            next_at = (
                state.utcnow() + timedelta(minutes=CRASH_RETRY_MINUTES)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            why = f"pipeline crashed ({type(exc).__name__}); near-future retry"

        next_run = {
            "run_id": run_id,
            "next_run_at": next_at,
            "rationale": f"crash handler: {why}",
            "cycle_intent": "trade",
            "crash": True,
            "error": short,
        }
        state.write_json(state.run_dir(run_id) / "next_run.json", next_run)
        if not dry_run:
            state.write_json(state.NEXT_RUN, next_run)
    except Exception:
        pass


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

    gate_exit = live_gate.assert_live_gate(entrypoint="orchestrator")
    if gate_exit is not None:
        return gate_exit

    broker = None if args.dry_run else _try_load_broker()

    # run_id is resolved here (not inside run_pipeline) so the crash
    # handler can attribute artifacts to the run that actually raised.
    rid = args.run_id or state.new_run_id()

    t0 = time.time()
    try:
        result = run_pipeline(
            dry_run=args.dry_run,
            run_id=rid,
            broker=broker,
            cli_intent=args.intent,
            ignore_cap=args.ignore_cap,
        )
    except llm.HaltFlagSet:
        # Operator stop: no error row, no reschedule — halt means halt.
        raise
    except Exception as exc:  # KeyboardInterrupt/SystemExit are BaseException
        _record_pipeline_crash(
            run_id=rid, exc=exc, dry_run=args.dry_run, cli_intent=args.intent,
        )
        raise  # non-zero exit for systemd; original traceback preserved
    dt = time.time() - t0
    stage_count = 6 if result.get("market_gate", {}).get("is_open", True) else 1
    intent_tag = f" intent={result.get('cycle_intent', 'trade')}" if result.get("cycle_intent") else ""
    print(f"run_id={result['run_id']} stages={stage_count} elapsed={dt:.2f}s dry_run={args.dry_run}{intent_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
