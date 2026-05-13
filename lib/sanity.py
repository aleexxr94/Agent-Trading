"""Deterministic post-construct sanity rules.

Runs after ``stage_construct`` emits ``portfolio.json``. Applies a set of
structural checks against the portfolio (and the upstream scenarios
payload where useful) and emits ``sanity.json`` alongside the portfolio.

Why deterministic? The CLAUDE.md spec already has cost caps, a halt
flag, and prompt-level guardrails. What it was missing was a cheap fast
way to catch *patterns* — "the agent is constructing a long straddle
on TLT and the iv_percentile is 65" — that no single LLM agent has the
shape to notice (bull is bullish, bear is bearish, constructor merges
them, nobody sees the meta-pattern). Sanity rules are zero-cost
(no API calls) and surface those patterns in the Agent Logs tab.

Non-blocking by default. Each rule has a fixed ``severity`` (``warn`` or
``fail``); when fired, the rule's ``status`` matches its severity, and
the overall sanity status is the worst of any rule's status. Set
``SANITY_BLOCK_ON_FAIL=true`` to escalate ``fail`` rules into a hard
runtime block (orchestrator skips ``stage_execute`` and writes a
``next_run.json`` indicating why).

Add a new rule by writing a ``_r_*`` function returning ``RuleResult``
and appending it to ``RULES``. Each rule receives ``(portfolio,
scenarios)`` and decides for itself how much of either to consume.

Rule list (see docstrings on each ``_r_*`` for full rationale):

  - ``per_underlying_pct_cap_20``        (warn) — Σ position_pct on a
    single underlying must stay ≤ 20%. The 15%-per-position cap doesn't
    stop a call+put pair on the same underlying from concentrating
    26%+ NAV on one ticker (see TLT-straddle observation 2026-05-13).
  - ``straddle_requires_low_iv``         (fail) — long call+put on the
    same underlying must have ``greeks.iv_percentile ≤ 40`` on every
    leg. Otherwise the agent is paying for vol that isn't statistically
    cheap — the failure mode of "no directional conviction, just buy
    gamma both ways."
  - ``kill_conditions_complete``         (fail) — every position has a
    parseable ``max_loss_pct`` ∈ (0, 100] AND at least one of
    {underlying_price_below, underlying_price_above, time_stop_utc}.
    Without one of these monitor.py has nothing to flatten on.
  - ``expected_value_positive``          (fail) — every position's
    symbol/underlying has ``expected_value_pct > 0`` in
    ``scenarios.candidates``. Constructor is supposed to filter
    negative-EV candidates; this rule asserts it actually did.
  - ``option_premium_above_floor``       (warn) — option positions have
    ``premium_paid ≥ 0.05``. Penny premiums signal illiquidity that
    Alpaca paper often won't fill cleanly.
  - ``construction_rationale_meaningful`` (fail) — non-empty,
    ≥ 80 characters. Forces the constructor to actually explain itself.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

Severity = Literal["warn", "fail"]
Status = Literal["pass", "warn", "fail", "skip"]


# Overall sanity status is the worst rule status seen. "skip" doesn't
# affect overall (rule didn't apply to this portfolio — e.g. straddle
# rule on a portfolio with no option pairs).
_STATUS_RANK: dict[Status, int] = {"skip": 0, "pass": 1, "warn": 2, "fail": 3}


@dataclass
class RuleResult:
    name: str
    severity: Severity
    status: Status
    detail: str = ""
    meta: dict = field(default_factory=dict)


def _position_underlying(p: dict) -> str | None:
    """ETF positions key on ``symbol``; option positions key on
    ``underlying``. The sanity rules treat both uniformly as "the ticker
    this position is exposed to."
    """
    if p.get("kind") == "option":
        return p.get("underlying")
    return p.get("symbol")


def _r_per_underlying_pct_cap_20(portfolio: dict, scenarios: dict | None) -> RuleResult:
    """Σ position_pct per underlying ≤ 20%.

    The 15%-per-position cap (enforced by portfolio.schema.json) doesn't
    stop two same-underlying legs (call + put, or call + ETF on same
    factor) from concentrating NAV. The TLT-straddle outcome on
    2026-05-13 had 26.6% NAV on TLT despite each leg being inside the
    per-position rail. 20% is the soft warning threshold — not a hard
    block, because there's a legitimate world where the agent has
    extremely high conviction on one underlying and structures the bet
    as a directional pair. But the operator should see it.
    """
    name = "per_underlying_pct_cap_20"
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

    breaches = {s: round(v, 2) for s, v in pct_by_underlying.items() if v > 20.0}
    if breaches:
        return RuleResult(
            name, severity, severity,
            detail=f"sum(position_pct) > 20% on: {breaches}",
            meta={"pct_by_underlying": {s: round(v, 4) for s, v in pct_by_underlying.items()}},
        )
    return RuleResult(
        name, severity, "pass",
        meta={"pct_by_underlying": {s: round(v, 4) for s, v in pct_by_underlying.items()}},
    )


def _r_straddle_requires_low_iv(portfolio: dict, scenarios: dict | None) -> RuleResult:
    """Long-straddle pattern requires cheap vol on every leg.

    Pattern detection: an underlying that has BOTH a long call and a
    long put position in the portfolio. (Schema currently has no short
    options, so "long call + long put" is the only call+put combination
    that can fire — but the rule reads ``type`` defensively in case
    the schema gains shorts later.)

    A long straddle is a pure long-vol bet. Theta works against it
    daily; it only pays if realised vol exceeds implied vol over the
    holding horizon. The cheapest version of that bet is when implied
    vol is currently low relative to its own history — i.e.
    ``iv_percentile`` is in the low quartile. Anything above ~40
    means the agent is buying vol that isn't statistically cheap, and
    the position is more likely the "I don't know which direction
    so I'll buy both" failure mode than a real vol thesis.

    Threshold 40 is deliberately not 30 — leaves the agent some room
    to express moderate-vol theses on near-term catalysts without
    every straddle tripping the rule.
    """
    name = "straddle_requires_low_iv"
    severity: Severity = "fail"
    positions = portfolio.get("positions") or []

    # Group option positions by underlying, separated by type.
    by_und: dict[str, dict[str, list[dict]]] = {}
    for p in positions:
        if p.get("kind") != "option":
            continue
        und = p.get("underlying")
        if not und:
            continue
        by_und.setdefault(und, {"call": [], "put": []})
        leg_type = p.get("type")
        if leg_type in ("call", "put"):
            by_und[und][leg_type].append(p)

    straddle_underlyings = [u for u, legs in by_und.items() if legs["call"] and legs["put"]]
    if not straddle_underlyings:
        return RuleResult(name, severity, "skip", "no call+put pair on the same underlying")

    offenders: list[dict] = []
    iv_summary: dict[str, list[dict]] = {}
    for und in straddle_underlyings:
        legs = by_und[und]["call"] + by_und[und]["put"]
        for leg in legs:
            ivp = (leg.get("greeks") or {}).get("iv_percentile")
            iv_summary.setdefault(und, []).append({"type": leg.get("type"), "iv_percentile": ivp})
            if ivp is None:
                offenders.append({"underlying": und, "type": leg.get("type"), "issue": "iv_percentile missing"})
            elif ivp > 40:
                offenders.append({
                    "underlying": und,
                    "type": leg.get("type"),
                    "strike": leg.get("strike"),
                    "iv_percentile": ivp,
                })

    if offenders:
        return RuleResult(
            name, severity, severity,
            detail=(
                f"long-straddle pattern requires iv_percentile ≤ 40 on every leg; "
                f"{len(offenders)} leg(s) above threshold or missing IV data"
            ),
            meta={"straddle_underlyings": straddle_underlyings, "iv_summary": iv_summary, "offenders": offenders},
        )
    return RuleResult(
        name, severity, "pass",
        detail=f"{len(straddle_underlyings)} straddle pattern(s) on legs all at iv_percentile ≤ 40",
        meta={"straddle_underlyings": straddle_underlyings, "iv_summary": iv_summary},
    )


def _r_kill_conditions_complete(portfolio: dict, scenarios: dict | None) -> RuleResult:
    """Every position needs an enforceable kill condition.

    The schema requires ``max_loss_pct`` to be present, but doesn't
    require any of the price-stop / time-stop fields. monitor.py
    needs at least one OF those alongside ``max_loss_pct`` to actually
    act before the loss is realised — ``max_loss_pct`` alone fires only
    after the position has bled the full amount.

    Pass criteria:
      - ``max_loss_pct`` is a number in (0, 100]
      - AND ≥1 of {underlying_price_below, underlying_price_above,
        time_stop_utc} is non-null
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
            for k in ("underlying_price_below", "underlying_price_above", "time_stop_utc")
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


def _r_expected_value_positive(portfolio: dict, scenarios: dict | None) -> RuleResult:
    """Every traded position must have positive EV in scenarios.

    Constructor.md says it filters out negative-EV candidates. This
    rule asserts the filter actually ran. If scenarios.json shows
    ``expected_value_pct ≤ 0`` for a candidate that nonetheless made
    it into the portfolio, something went wrong upstream.

    Skip if scenarios payload isn't available (e.g. dry-run path
    without fixture scenarios).
    """
    name = "expected_value_positive"
    severity: Severity = "fail"
    positions = portfolio.get("positions") or []
    if not positions:
        return RuleResult(name, severity, "skip", "all-cash portfolio")
    if not scenarios or not scenarios.get("candidates"):
        return RuleResult(name, severity, "skip", "scenarios payload unavailable")

    ev_by_symbol: dict[str, float | None] = {}
    for c in scenarios["candidates"]:
        sym = c.get("symbol")
        if sym:
            ev = c.get("expected_value_pct")
            ev_by_symbol[sym] = ev if isinstance(ev, (int, float)) else None

    bad: list[dict] = []
    for p in positions:
        und = _position_underlying(p)
        if und is None:
            continue
        ev = ev_by_symbol.get(und)
        if ev is None:
            bad.append({"sym": und, "issue": "no scenarios entry"})
        elif ev <= 0:
            bad.append({"sym": und, "expected_value_pct": ev})

    if bad:
        return RuleResult(
            name, severity, severity,
            detail=f"{len(bad)} position(s) have non-positive EV (or no scenarios entry)",
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_option_premium_above_floor(portfolio: dict, scenarios: dict | None) -> RuleResult:
    """Option positions priced above the penny-illiquid floor.

    Long options below $0.05 premium tend to be deep OTM and illiquid;
    Alpaca paper often won't fill them cleanly and even if it does the
    bid-ask spread eats the position. Warn rather than fail — there
    might be a legitimate lottery-ticket trade, but the operator
    should know about it.
    """
    name = "option_premium_above_floor"
    severity: Severity = "warn"
    positions = portfolio.get("positions") or []
    options = [p for p in positions if p.get("kind") == "option"]
    if not options:
        return RuleResult(name, severity, "skip", "no option positions")

    bad: list[dict] = []
    for p in options:
        prem = p.get("premium_paid")
        if not isinstance(prem, (int, float)) or prem < 0.05:
            bad.append({
                "underlying": p.get("underlying"),
                "type": p.get("type"),
                "strike": p.get("strike"),
                "premium_paid": prem,
            })

    if bad:
        return RuleResult(
            name, severity, severity,
            detail=f"{len(bad)} option(s) priced below the $0.05 illiquid-floor",
            meta={"offenders": bad},
        )
    return RuleResult(name, severity, "pass")


def _r_construction_rationale_meaningful(portfolio: dict, scenarios: dict | None) -> RuleResult:
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


# Registration order: list rules in roughly increasing severity so
# dashboards rendering top-to-bottom show structural issues before
# detail-level warnings.
RULES: list[Callable[[dict, dict | None], RuleResult]] = [
    _r_construction_rationale_meaningful,
    _r_kill_conditions_complete,
    _r_expected_value_positive,
    _r_straddle_requires_low_iv,
    _r_per_underlying_pct_cap_20,
    _r_option_premium_above_floor,
]


def run_sanity_checks(portfolio: dict, scenarios: dict | None = None) -> dict:
    """Run all registered rules. Return a sanity-report dict ready to
    write to ``sanity.json`` (run_id + generated_at are filled in by
    the orchestrator caller, not here, so this function stays pure).

    Overall status is the worst per-rule status seen. Skips don't
    degrade the overall status.
    """
    results = [r(portfolio, scenarios) for r in RULES]
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
