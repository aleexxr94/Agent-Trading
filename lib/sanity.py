"""Deterministic post-construct sanity rules — v2.

Runs after ``stage_construct`` emits ``portfolio.json``. Applies a set
of structural checks against the portfolio (and the upstream
strategist view, where useful) and emits ``sanity.json`` alongside.

Why deterministic? The CLAUDE.md spec already has cost caps, a halt
flag, and prompt-level guardrails. What it was missing was a cheap
fast way to catch *patterns* — "the agent is concentrating >20% NAV on
one ticker" or "a position has no enforceable stop" — that no single
LLM agent has the shape to notice. Sanity rules are zero-cost (no API
calls) and surface those patterns in the Agent Logs tab.

Non-blocking by default. Each rule has a fixed ``severity`` (``warn``
or ``fail``); when fired, the rule's ``status`` matches its severity.
Overall status is the worst rule status seen. Set
``SANITY_BLOCK_ON_FAIL=true`` to escalate ``fail`` rules into a hard
runtime block (orchestrator skips ``stage_execute`` and preserves
cadence via the default heuristic — see fix(sanity) commit on PR γ).

Add a new rule by writing a ``_r_*`` function returning ``RuleResult``
and appending it to ``RULES``. Each rule receives ``(portfolio,
view)`` and decides for itself how much of either to consume.

v1 → v2: the ``expected_value_positive`` rule (which read
scenarios.json) was replaced by ``position_backed_by_strategist``
which reads the v2 view.json — same intent (don't take positions
upstream agents didn't endorse) but the upstream signal is now
``confidence`` instead of ``expected_value_pct``.

Rule list (see docstrings on each ``_r_*`` for full rationale):

  - ``per_underlying_pct_cap_30``        (warn)
  - ``kill_conditions_complete``         (fail)
  - ``position_backed_by_strategist``    (fail) — v2
  - ``construction_rationale_meaningful`` (fail)
  - ``symbol_in_universe``               (fail) — defense-in-depth mirror of
    the hard order-layer universe guard in lib/orders.py
  - ``position_within_adaptive_cap``     (fail) — hold ceiling (adaptive 25%),
    the per-position bound (25% hard via schema; the drawdown-tightened
    value is advisory by default)
  - ``entry_cap_on_adds``                (fail) — entry/add cap (adaptive 15%);
    held winners may drift past it, new opens/adds may not
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

Severity = Literal["warn", "fail"]
Status = Literal["pass", "warn", "fail", "skip"]


# Overall sanity status is the worst rule status seen. "skip" doesn't
# affect overall (rule didn't apply to this portfolio — e.g. the
# re-entry-cooldown rule when no symbols are in cooldown).
_STATUS_RANK: dict[Status, int] = {"skip": 0, "pass": 1, "warn": 2, "fail": 3}


@dataclass
class RuleResult:
    name: str
    severity: Severity
    status: Status
    detail: str = ""
    meta: dict = field(default_factory=dict)


def _position_underlying(p: dict) -> str | None:
    """The ticker a position is exposed to. ETF-only system, so this is
    simply the position ``symbol``."""
    return p.get("symbol")


def _r_per_underlying_pct_cap_30(portfolio: dict, view: dict | None) -> RuleResult:
    """Σ position_pct per ticker ≤ 30%.

    The per-position caps (entry 15%, hold ceiling 25%) don't stop the same
    ticker appearing twice from concentrating NAV. 30% is a soft warning
    threshold — not a hard block — chosen to leave headroom above a single
    25% holding sitting at the hold ceiling while still catching genuine
    double-loading of one ticker.
    """
    name = "per_underlying_pct_cap_30"
    severity: Severity = "warn"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")

    pct_by_underlying: dict[str, float] = {}
    for p in positions:
        und = _position_underlying(p)
        if not und:
            continue
        pct_by_underlying[und] = pct_by_underlying.get(und, 0.0) + float(p.get("position_pct") or 0.0)

    breaches = {s: round(v, 2) for s, v in pct_by_underlying.items() if v > 30.0}
    if breaches:
        return RuleResult(
            name, severity, severity,
            detail=f"sum(position_pct) > 30% on: {breaches}",
            meta={"pct_by_underlying": {s: round(v, 4) for s, v in pct_by_underlying.items()}},
        )
    return RuleResult(
        name, severity, "pass",
        meta={"pct_by_underlying": {s: round(v, 4) for s, v in pct_by_underlying.items()}},
    )


def _r_kill_conditions_complete(portfolio: dict, view: dict | None) -> RuleResult:
    """Every position needs an enforceable kill condition.

    The schema requires ``max_loss_pct`` to be present, but doesn't
    require any of the price-stop / time-stop fields. monitor.py
    needs at least one OF those alongside ``max_loss_pct`` to actually
    act before the loss is realised — ``max_loss_pct`` alone fires only
    after the position has bled the full amount.

    Pass criteria:
      - ``max_loss_pct`` is a number in (0, 100]
      - AND ≥1 of {underlying_price_below, underlying_price_above,
        trailing_stop_pct, time_stop_utc} is non-null — a trailing stop
        is a (dynamic) price stop, so it satisfies the requirement.
    """
    name = "kill_conditions_complete"
    severity: Severity = "fail"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")

    bad: list[dict] = []
    for p in positions:
        sym = _position_underlying(p) or "<unknown>"
        kc = p.get("kill_conditions") or {}
        max_loss = kc.get("max_loss_pct")
        if not isinstance(max_loss, (int, float)) or not (0 < max_loss <= 100):
            bad.append({"sym": sym, "kind": p.get("kind"), "issue": f"max_loss_pct={max_loss!r} not in (0, 100]"})
            continue
        has_stop = any(
            kc.get(k) is not None
            for k in ("underlying_price_below", "underlying_price_above",
                      "trailing_stop_pct", "time_stop_utc")
        )
        if not has_stop:
            bad.append({
                "sym": sym, "kind": p.get("kind"),
                "issue": "no price-stop or time-stop set (max_loss_pct only)",
            })

    if bad:
        return RuleResult(
            name, severity, severity,
            detail=f"{len(bad)} position(s) failed kill-condition completeness",
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_position_backed_by_strategist(portfolio: dict, view: dict | None) -> RuleResult:
    """Every traded position must be endorsed by the strategist.

    v2 successor to the v1 ``expected_value_positive`` rule. Reads the
    strategist's view.json instead of scenarios.json. The strategist's
    ``confidence`` field is the v2 analogue of expected_value_pct —
    confidence ≥ 0.5 means "endorsed enough to surface this idea."

    Constructor.md says it should be taking strategist picks, not
    inventing positions out of band. This rule asserts that every ETF
    position's symbol appears in view.candidates with confidence ≥ 0.5.

    Skip if view payload isn't available (e.g. dry-run path without
    fixture view).
    """
    name = "position_backed_by_strategist"
    severity: Severity = "fail"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")
    if not view or not view.get("candidates"):
        return RuleResult(name, severity, "skip", "view payload unavailable")

    # Build endorsement index keyed by (symbol, instrument_kind). ETF-only,
    # so the kind tag is always "etf".
    endorsed: dict[tuple[str, str], float] = {}
    for c in view["candidates"]:
        sym = c.get("symbol")
        kind = c.get("instrument_kind")
        conf = c.get("confidence")
        if sym and kind and isinstance(conf, (int, float)):
            endorsed[(sym, kind)] = float(conf)

    bad: list[dict] = []
    for p in positions:
        if p.get("kind") != "etf":
            continue
        key = (p.get("symbol", ""), "etf")
        conf = endorsed.get(key)
        if conf is None:
            bad.append({
                "symbol": key[0], "instrument_kind": key[1],
                "issue": "not in strategist candidates",
            })
        elif conf < 0.5:
            bad.append({
                "symbol": key[0], "instrument_kind": key[1],
                "confidence": conf,
                "issue": "strategist confidence < 0.5",
            })

    if bad:
        return RuleResult(
            name, severity, severity,
            detail=f"{len(bad)} position(s) not endorsed by strategist with confidence ≥ 0.5",
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_symbol_in_universe(portfolio: dict, view: dict | None) -> RuleResult:
    """Every position symbol must be in the 29-ticker ETF universe.

    Defense-in-depth, advisory by default. The hard gate is in
    lib/orders.py (diff_portfolio + submit_plan refuse non-universe symbols
    at the broker boundary); this rule surfaces a non-universe symbol in the
    Agent Logs tab so the operator can see it even before order time. Like
    every sanity rule it only blocks stage_execute when
    SANITY_BLOCK_ON_FAIL=true.
    """
    from . import universe

    name = "symbol_in_universe"
    severity: Severity = "fail"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")
    allowed = set(universe.all_symbols())
    bad = [
        {"symbol": p.get("symbol"), "kind": p.get("kind")}
        for p in positions
        if p.get("symbol") not in allowed
    ]
    if bad:
        return RuleResult(
            name, severity, severity,
            detail=f"{len(bad)} position(s) outside the 29-ticker ETF universe",
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_construction_rationale_meaningful(portfolio: dict, view: dict | None) -> RuleResult:
    """``construction_rationale`` non-empty and ≥ 80 characters.

    The schema requires it to be non-empty (minLength 1). 80 chars is
    the floor for the field actually carrying explanatory content
    rather than a placeholder. The constructor prompt asks for "why
    this set of positions, why this count, why now" — that doesn't
    fit in 30 characters.
    """
    name = "construction_rationale_meaningful"
    severity: Severity = "fail"
    text = (portfolio.get("construction_rationale") or "").strip()
    if len(text) < 80:
        return RuleResult(
            name, severity, severity,
            detail=f"construction_rationale length={len(text)} < 80",
            meta={"length": len(text)},
        )
    return RuleResult(name, severity, "pass", meta={"length": len(text)})


# ----- v2 win-rate rules (added on top of the v2 base) -----


def _r_position_size_matches_confidence(portfolio: dict, view: dict | None) -> RuleResult:
    """Position size should be proportional to strategist confidence.

    Heuristic ceiling: ``position_pct <= confidence × 15``. A 0.6-
    confidence pick should size ≤ 9% NAV; a 0.95-confidence pick can
    use up to 14.25%. This is a *warn* rule — the schema's hard 15%
    cap still applies — but flagging mis-sizing surfaces the case where
    the constructor uniformly sizes every position to 12% regardless
    of how strong the upstream signal was.
    """
    name = "position_size_matches_confidence"
    severity: Severity = "warn"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")
    if not view or not view.get("candidates"):
        return RuleResult(name, severity, "skip", "view payload unavailable")

    confidence_by_key: dict[tuple[str, str], float] = {}
    for c in view["candidates"]:
        sym = c.get("symbol")
        kind = c.get("instrument_kind")
        conf = c.get("confidence")
        if sym and kind and isinstance(conf, (int, float)):
            confidence_by_key[(sym, kind)] = float(conf)

    held: dict = portfolio.get("_current_positions") or {}
    bad: list[dict] = []
    for p in positions:
        if p.get("kind") != "etf":
            continue
        key = (p.get("symbol", ""), "etf")
        conf = confidence_by_key.get(key)
        if conf is None:
            continue  # `position_backed_by_strategist` covers this case
        ceiling = round(conf * 15.0, 2)
        pct = float(p.get("position_pct") or 0.0)
        # A held winner that merely drifted above the confidence-weighted
        # ceiling isn't a sizing mistake — don't spam the warning. Only flag
        # when the position is sized above the ceiling AND above what we
        # already held (i.e. a fresh open / deliberate add was oversized).
        held_weight = float(held.get(key[0], 0.0) or 0.0)
        if pct <= held_weight + 0.01:
            continue
        if pct > ceiling + 0.01:  # tiny float-tolerance
            bad.append({
                "symbol": key[0], "instrument_kind": key[1],
                "confidence": conf, "ceiling": ceiling, "position_pct": pct,
            })
    if bad:
        return RuleResult(
            name, severity, severity,
            detail=(
                f"{len(bad)} position(s) sized above the confidence-weighted "
                f"ceiling (position_pct > confidence × 15)"
            ),
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_position_within_adaptive_cap(portfolio: dict, view: dict | None) -> RuleResult:
    """Per-position % enforcement against the drawdown-adaptive HOLD CEILING
    computed by risk.adaptive_hold_ceiling_pct.

    The upper bound on ANY position's weight, held or new: a winner may drift
    up to the hold ceiling (25% at peak, tightening toward 12.5% in drawdown)
    but no further. Opening/adding above the lower *entry* cap is a separate
    rule (``_r_entry_cap_on_adds``).

    Two enforcement tiers, by design (see CLAUDE.md §System scope):
      - The 25% BASE ceiling is a HARD bound — ``position.schema.json`` caps
        ``position_pct`` at 25, so a portfolio above it fails validation on
        write and the run aborts. This rule is the readable mirror of that.
      - The DRAWDOWN-TIGHTENED ceiling (down to 12.5%) is ADVISORY by default:
        it is fed to the constructor as soft guidance and surfaces here as a
        ``fail`` that only hard-skips execute when ``SANITY_BLOCK_ON_FAIL`` is
        set. A held winner that drifted above the tightened ceiling is not
        force-trimmed on the default path — the 25% schema cap, the 25%
        per-position loss-kill, and the 8% daily circuit breaker remain the
        hard backstops. (Unlike ``entry_cap_on_adds``, this rule is NOT
        promoted to an unconditional block.)

    Reads ``_adaptive_hold_ceiling_pct`` injected by run_sanity_checks. If
    absent, falls back to the base 25.0 ceiling so the rule still bounds
    concentration when the ceiling wasn't supplied.
    """
    name = "position_within_adaptive_cap"
    severity: Severity = "fail"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")
    ceiling = float(portfolio.get("_adaptive_hold_ceiling_pct", 25.0))
    bad = [
        {"sym": p.get("symbol") or p.get("underlying"), "position_pct": p.get("position_pct"),
         "hold_ceiling_pct": ceiling}
        for p in positions
        if float(p.get("position_pct") or 0.0) > ceiling + 0.01
    ]
    if bad:
        return RuleResult(
            name, severity, severity,
            detail=(
                f"{len(bad)} position(s) exceed the hold ceiling "
                f"{ceiling:.2f}% (the absolute per-position bound)"
            ),
            meta={"offenders": bad, "hold_ceiling_pct": ceiling},
        )
    return RuleResult(name, severity, "pass", meta={"hold_ceiling_pct": ceiling})


def _r_entry_cap_on_adds(portfolio: dict, view: dict | None) -> RuleResult:
    """The 15% entry/add cap, enforced deterministically.

    A position may sit above the (drawdown-adaptive) entry cap ONLY as drift
    of an already-open holding — i.e. it appreciated there, we didn't buy it
    there. So a position is an offender when BOTH:

      - ``position_pct`` exceeds the adaptive entry cap (15% at peak,
        tightening in drawdown), AND
      - it exceeds the weight we already held coming into this cycle
        (a fresh open, or an *add* that pushes weight past the entry cap).

    A held winner that drifted above the entry cap but is being kept at/below
    its current weight is allowed (it's the hold-ceiling rule's job to bound
    that). ``_current_positions`` is an injected ``{symbol: held_weight_pct}``
    map; a symbol absent from it is treated as a fresh open (held_weight 0).

    Drift is detected by BOTH weight and SHARES. The order layer
    (``diff_portfolio``) buys/sells by ``shares``, not ``position_pct``, and the
    schema doesn't tie the two together — so a held winner left at the same
    ``position_pct`` while its ``shares`` increase is an *add* (a buy above the
    entry cap) that a weight-only check would miss. ``_current_position_shares``
    is an injected ``{symbol: held_shares}`` map; when present, an increase in
    target shares over held shares (while over the entry cap) also fails.
    """
    name = "entry_cap_on_adds"
    severity: Severity = "fail"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")
    entry_cap = float(portfolio.get("_adaptive_cap_pct", 15.0))
    held: dict = portfolio.get("_current_positions") or {}
    held_shares: dict = portfolio.get("_current_position_shares") or {}
    tol = 0.01
    bad: list[dict] = []
    for p in positions:
        sym = p.get("symbol") or p.get("underlying")
        pct = float(p.get("position_pct") or 0.0)
        held_weight = float(held.get(sym, 0.0) or 0.0)
        # Within the entry cap → always fine.
        if pct <= entry_cap + tol:
            continue
        # Above the entry cap — only allowed as pure drift of an existing
        # holding: neither the weight nor the share count may increase.
        weight_increased = pct > held_weight + tol
        shares_increased = False
        if sym in held_shares and p.get("shares") is not None:
            shares_increased = float(p.get("shares")) > float(held_shares[sym]) + tol
        if not weight_increased and not shares_increased:
            continue
        bad.append({
            "sym": sym, "position_pct": pct,
            "entry_cap_pct": entry_cap, "held_weight_pct": held_weight,
            "issue": (
                "opened/added above the entry cap"
                if held_weight <= tol else
                "added beyond the entry cap "
                + ("(share count increased past held drift)"
                   if shares_increased and not weight_increased
                   else "(weight increased past held drift)")
            ),
        })
    if bad:
        return RuleResult(
            name, severity, severity,
            detail=(
                f"{len(bad)} position(s) opened or added above the entry cap "
                f"{entry_cap:.2f}% (held winners may drift past it, but new "
                f"entries/adds may not)"
            ),
            meta={"offenders": bad, "entry_cap_pct": entry_cap},
        )
    return RuleResult(name, severity, "pass", meta={"entry_cap_pct": entry_cap})


def _r_position_notional_above_floor(portfolio: dict, view: dict | None) -> RuleResult:
    """Per-position notional value ≥ $50. On a $2,500 paper account,
    tiny positions (e.g. 1% NAV = $25) get eaten by spread + fees
    before they can deliver edge. The constructor is asked via prompt
    to consolidate small picks; this rule warns when it didn't.

    Notional = position_pct × nav. Requires nav from
    ``portfolio['_nav_usd']`` (injected by run_sanity_checks).
    """
    name = "position_notional_above_floor"
    severity: Severity = "warn"
    positions = portfolio.get("positions") or []
    nav = float(portfolio.get("_nav_usd") or portfolio.get("nav_usd") or 0.0)
    if not positions or nav <= 0:
        return RuleResult(name, severity, "skip", "all-cash portfolio or NAV unknown")
    bad = []
    for p in positions:
        pct = float(p.get("position_pct") or 0.0)
        notional = pct / 100.0 * nav
        if notional < 50.0:
            bad.append({
                "sym": p.get("symbol") or p.get("underlying"),
                "position_pct": pct, "notional_usd": round(notional, 2),
            })
    if bad:
        return RuleResult(
            name, severity, severity,
            detail=(
                f"{len(bad)} position(s) below the $50 notional floor "
                "(spread + fees will dominate expected return)"
            ),
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_position_adv_liquidity(portfolio: dict, view: dict | None) -> RuleResult:
    """Per-position order size should be ≤ 1% of 30d ADV on the
    underlying. On a $2,500 account this almost never bites, but the
    rail prevents the constructor from picking a tiny illiquid ticker
    where a $200 position is a noticeable fraction of daily volume.

    Reads adv_30d per symbol from ``portfolio['_signals_adv']`` map
    (injected by run_sanity_checks from signals.json).
    """
    name = "position_adv_liquidity"
    severity: Severity = "warn"
    positions = portfolio.get("positions") or []
    adv_map: dict = portfolio.get("_signals_adv") or {}
    nav = float(portfolio.get("_nav_usd") or portfolio.get("nav_usd") or 0.0)
    if not positions or not adv_map or nav <= 0:
        return RuleResult(name, severity, "skip", "no positions / no signals / no NAV")
    bad = []
    for p in positions:
        sym = p.get("symbol") if p.get("kind") == "etf" else p.get("underlying")
        if not sym:
            continue
        adv_dollars = adv_map.get(sym)
        if not adv_dollars or adv_dollars <= 0:
            continue
        notional = float(p.get("position_pct") or 0.0) / 100.0 * nav
        # 1% of ADV-in-dollars is the soft ceiling.
        if notional > 0.01 * adv_dollars:
            bad.append({
                "sym": sym, "notional_usd": round(notional, 2),
                "adv_dollars": round(adv_dollars, 2),
                "frac_of_adv": round(notional / adv_dollars, 5),
            })
    if bad:
        return RuleResult(
            name, severity, severity,
            detail=(
                f"{len(bad)} position(s) exceed 1% of underlying's "
                "30d ADV (slippage risk on entry/exit)"
            ),
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_reentry_cooldown(portfolio: dict, view: dict | None) -> RuleResult:
    """Discourage re-entering a symbol just fully exited (re-entry cooldown).

    After a full exit, the system avoids re-buying the same symbol for
    ``risk.REENTRY_COOLDOWN_DAYS`` (see lib/risk.py) — unless the strategist's
    re-entry confidence on that name clears
    ``risk.REENTRY_COOLDOWN_OVERRIDE_CONFIDENCE``, in which case the re-entry
    is an explicit, logged override rather than a violation.

    Reads ``portfolio['_cooldown_symbols']`` (a ``{symbol: last_exit_iso}``
    map injected by run_sanity_checks) and the strategist confidence per
    ``(symbol, instrument_kind)`` from view.candidates. ``warn`` severity:
    this is a soft, non-blocking guardrail — the LLM remains the decider
    (consistent with the prompt-driven harvest/exit logic). The override
    confidences and any offenders are recorded in ``meta`` so sanity.json
    documents the decision.
    """
    from . import risk

    name = "reentry_cooldown"
    severity: Severity = "warn"
    positions = portfolio.get("positions") or []
    cooldown: dict = portfolio.get("_cooldown_symbols") or {}
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")
    if not cooldown:
        return RuleResult(name, severity, "skip", "no symbols in re-entry cooldown")

    # Strategist confidence index keyed by (symbol, instrument_kind),
    # mirroring _r_position_backed_by_strategist.
    endorsed: dict[tuple[str, str], float] = {}
    for c in (view or {}).get("candidates", []) or []:
        sym = c.get("symbol")
        kind = c.get("instrument_kind")
        conf = c.get("confidence")
        if sym and kind and isinstance(conf, (int, float)):
            endorsed[(sym, kind)] = float(conf)

    threshold = risk.REENTRY_COOLDOWN_OVERRIDE_CONFIDENCE
    offenders: list[dict] = []
    overrides: list[dict] = []
    for p in positions:
        # The cooldown map is keyed by the broker symbol exactly as
        # trades.jsonl stores it: the ETF ticker. `match_key` is what we
        # test for cooldown membership; `conf_key` is how view.candidates
        # indexes confidence.
        if p.get("kind") != "etf":
            continue
        match_key = p.get("symbol", "")
        conf_key = (match_key, "etf")
        if match_key not in cooldown:
            continue
        sym = conf_key[0]
        conf = endorsed.get(conf_key)
        exited_at = cooldown.get(match_key)
        if conf is not None and conf > threshold:
            overrides.append({
                "symbol": sym, "confidence": conf,
                "exited_at": exited_at,
                "reason": f"re-entry confidence {conf} > {threshold} override",
            })
        else:
            offenders.append({
                "symbol": sym, "confidence": conf, "exited_at": exited_at,
                "issue": (
                    f"re-entered within {risk.REENTRY_COOLDOWN_DAYS}d of full "
                    f"exit without confidence > {threshold}"
                ),
            })

    meta = {"overrides": overrides} if overrides else {}
    if offenders:
        meta["offenders"] = offenders
        return RuleResult(
            name, severity, severity,
            detail=(
                f"{len(offenders)} position(s) re-entered a symbol within the "
                f"{risk.REENTRY_COOLDOWN_DAYS}-day cooldown without a "
                f"confidence > {threshold} override"
            ),
            meta=meta,
        )
    return RuleResult(name, severity, "pass", meta=meta)


# Registration order: list rules in roughly increasing severity so
# dashboards rendering top-to-bottom show structural issues before
# detail-level warnings.
RULES: list[Callable[[dict, dict | None], RuleResult]] = [
    _r_construction_rationale_meaningful,
    _r_symbol_in_universe,
    _r_kill_conditions_complete,
    _r_position_backed_by_strategist,
    _r_position_within_adaptive_cap,
    _r_entry_cap_on_adds,
    _r_per_underlying_pct_cap_30,
    _r_position_size_matches_confidence,
    _r_position_notional_above_floor,
    _r_position_adv_liquidity,
    _r_reentry_cooldown,
]


def run_sanity_checks(
    portfolio: dict,
    view: dict | None = None,
    *,
    signals: dict | None = None,
    nav_usd: float | None = None,
    adaptive_cap_pct: float | None = None,
    hold_ceiling_pct: float | None = None,
    cooldown_symbols: dict | None = None,
    current_positions: list[dict] | None = None,
) -> dict:
    """Run all registered rules. Return a sanity-report dict ready to
    write to ``sanity.json`` (run_id + generated_at are filled in by
    the orchestrator caller, not here, so this function stays pure).

    Overall status is the worst per-rule status seen. Skips don't
    degrade the overall status.

    The keyword args (signals, nav_usd, adaptive_cap_pct, hold_ceiling_pct,
    cooldown_symbols, current_positions) are injected into the portfolio dict
    on under-prefixed keys so the rule functions can access them without
    changing the (portfolio, view) signature. ``cooldown_symbols`` is a
    ``{symbol: last_exit_iso}`` map of recently fully-exited symbols (see
    ``trades.symbols_in_cooldown``) consumed by ``_r_reentry_cooldown``.
    ``adaptive_cap_pct`` is the entry/add cap and ``hold_ceiling_pct`` the
    drift ceiling (both drawdown-adaptive); ``current_positions`` is the
    broker-position summary list (rows carry ``symbol`` + ``position_pct`` or
    ``market_value``) used to tell drift of a held winner apart from a fresh
    open. ``portfolio`` itself is NOT mutated — we operate on a shallow copy.
    """
    enriched = dict(portfolio)
    if nav_usd is not None:
        enriched["_nav_usd"] = nav_usd
    if adaptive_cap_pct is not None:
        enriched["_adaptive_cap_pct"] = adaptive_cap_pct
    if hold_ceiling_pct is not None:
        enriched["_adaptive_hold_ceiling_pct"] = hold_ceiling_pct
    if cooldown_symbols is not None:
        enriched["_cooldown_symbols"] = cooldown_symbols
    if current_positions is not None:
        # Build a {symbol: held_weight_pct} map so the entry-cap and
        # confidence rules can tell drift of an existing holding apart from a
        # fresh open/add. Prefer an explicit ``position_pct`` on the row;
        # otherwise derive weight from market_value / NAV when NAV is known.
        held_weights: dict[str, float] = {}
        held_shares: dict[str, float] = {}
        for row in current_positions:
            sym = row.get("symbol")
            if not sym:
                continue
            pct = row.get("position_pct")
            if pct is None and nav_usd:
                mv = row.get("market_value")
                if mv is not None and float(nav_usd) > 0:
                    pct = abs(float(mv)) / float(nav_usd) * 100.0
            if pct is not None:
                held_weights[sym] = float(pct)
            # Share count drives the order layer (diff_portfolio), so the
            # entry-cap rule also compares shares to catch an add that leaves
            # position_pct unchanged. Rows use `qty` (broker) or `shares`.
            qty = row.get("shares", row.get("qty"))
            if qty is not None:
                held_shares[sym] = abs(float(qty))
        enriched["_current_positions"] = held_weights
        enriched["_current_position_shares"] = held_shares
    if signals is not None:
        # adv_30d expressed in dollars (volume × last_close). The rule
        # consumes ADV-in-dollars so it compares directly to position
        # notional in dollars.
        adv_map = {}
        for t in signals.get("tickers", []):
            sym = t.get("symbol")
            adv = t.get("adv_30d")
            px = t.get("last_close")
            if sym and adv and px:
                adv_map[sym] = float(adv) * float(px)
        enriched["_signals_adv"] = adv_map
    results = [r(enriched, view) for r in RULES]
    summary: dict[Status, int] = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    worst_rank = 0
    worst_status: Status = "pass"
    for r in results:
        summary[r.status] += 1
        rank = _STATUS_RANK[r.status]
        # "skip" alone shouldn't make a clean run report "skip" overall —
        # only escalate worst_status to skip if nothing else fired.
        if rank > worst_rank and r.status != "skip":
            worst_rank = rank
            worst_status = r.status
    return {
        "status": worst_status,
        "summary": summary,
        "rules": [asdict(r) for r in results],
    }


def block_on_fail_enabled() -> bool:
    """Whether ``SANITY_BLOCK_ON_FAIL=true`` is set. The orchestrator
    reads this and, if true, skips ``stage_execute`` when the sanity
    report's overall status is ``fail``. Defaults to false so the rule
    set can land and be observed in production without immediately
    gating order submission.
    """
    return os.environ.get("SANITY_BLOCK_ON_FAIL", "false").strip().lower() in ("1", "true", "yes")


class SanityBlock(RuntimeError):
    """Raised by the orchestrator when sanity status is ``fail`` AND
    ``SANITY_BLOCK_ON_FAIL=true``. stage_execute is skipped for the
    current cycle; next_run.json carries the block reason for the
    dashboard to surface.
    """
