"""Pin StageConfig max_tokens values for the high-cardinality stages.

scenarios and constructor both fan over the 8-research-candidate output set,
emitting per-candidate JSON. The first live paper run hit the original 8192
output-token cap on scenarios mid-JSON, the retry-with-error-feedback
shrank itself to 2 candidates to fit, and the constructor correctly
abstained to all-cash — but for a fake reason (truncated input, not a real
zero-conviction signal). These tests guard against silently dropping back
to the old cap.
"""
from __future__ import annotations

from lib import stages


def test_scenarios_max_tokens_within_provider_caps():
    """The scenarios stage uses `thinking: adaptive`, which charges thinking
    tokens against the output budget. Each iteration of the cardinality rule
    has pushed scenarios output bigger:
      - PR #26 made it emit one row per candidate (was: drop low-EV)
      - PR #27 bumped 16k → 32k after observing truncation at ~7843 chars
      - PR #39-41 made option underlyings emit BOTH call+put rows + full
        option_rationale per direction, pushing output past 32k again
        (observed truncation at ~8697 chars on the regime-aware run)

    Two-sided guard now:
      - Floor 48k so future "trim tokens" edits can't regress us into the
        same truncation crash.
      - Ceiling 64k because that's Sonnet 4.6's hard provider cap (Codex
        P1 on PR #42: an earlier 65536 value would 400-fail the API call
        at request validation BEFORE retry logic could run — turning the
        stage into a hard-failure path instead of fixing truncation).

    If MODEL_SCENARIOS is changed to a model with a different cap, this
    guard should be made model-aware rather than relaxed."""
    cfg = stages.scenarios()
    assert 48_000 <= cfg.max_tokens <= 64_000, (
        f"scenarios max_tokens={cfg.max_tokens} is outside the safe range "
        f"[48000, 64000]. Below 48k risks truncation; above 64k fails "
        f"Sonnet 4.6's provider cap with a 400 before retry logic runs."
    )


def test_constructor_max_tokens_within_provider_caps():
    """Constructor emits 1-12 positions each with entry_thesis,
    kill_conditions, sizing math, plus envelope rationales. After PRs
    #39-41 the constructor consumes 14-row scenarios inputs (8 ETFs + up
    to 6 option-direction rows per scenarios.md's cardinality rule); the
    older 16k cap left the model with ~340 output tokens after adaptive
    thinking consumed the budget, observed truncating at chars 1341 and
    2296 on a real paper run.

    Two-sided guard:
      - Floor 24k so future "trim tokens" edits can't regress us below
        the level where adaptive thinking + the verbose portfolio
        payload reliably fits.
      - Ceiling 32k. Originally chosen because that's Opus 4.6's
        default max_tokens cap (without the interleaved-thinking beta
        header). 32k stays well inside Sonnet 4.6's 64k provider cap
        after PR β (2026-05-13) downgraded the default model. If
        MODEL_CONSTRUCTOR is overridden back to Opus the 32k ceiling
        still applies cleanly.
    """
    cfg = stages.constructor()
    assert 24_000 <= cfg.max_tokens <= 32_000, (
        f"constructor max_tokens={cfg.max_tokens} is outside the safe range "
        f"[24000, 32000]. Below 24k risks truncation; above 32k exceeds "
        f"Opus 4.6's default max_tokens cap (Sonnet 4.6's 64k cap also "
        f"comfortably above 32k)."
    )


def test_constructor_default_model_is_opus_4_7():
    """Lock the PR-δ promotion: constructor default is Opus 4.7.

    Recap of decisions:
      - Pre-2026-05-13: constructor was claude-opus-4-6 (initial spec
        bias toward the highest-stakes call's reasoning quality).
      - PR β (2026-05-13): downgraded to claude-sonnet-4-6 to land
        with the cost-caps bump; rationale was that structured output
        enforces shape so Sonnet is sufficient.
      - PR δ (2026-05-13, hours later): promoted back to Opus, but
        to 4.7 specifically (same price as 4.6, newer model, supports
        xhigh effort). Honest re-think: cost delta is ~$60/month at
        4 cycles/day; PnL impact of better position-picking on a
        $2,500 account dominates. Construct is where dollars are
        decided — pay for the best reasoning there.

    Override is preserved via MODEL_CONSTRUCTOR env var (flip back
    to Sonnet if cost ever dominates).
    """
    import os
    prev = os.environ.pop("MODEL_CONSTRUCTOR", None)
    try:
        assert stages.constructor().model == "claude-opus-4-7"
    finally:
        if prev is not None:
            os.environ["MODEL_CONSTRUCTOR"] = prev


def test_orchestrator_meta_default_model_is_sonnet():
    """Lock the PR-β downgrade for the meta scheduling call (kept on
    Sonnet through PR δ — meta has a deterministic 4h/6h fallback
    that covers any LLM output failure, so Opus is overkill for a
    2k-token scheduling decision). Override preserved via
    MODEL_ORCHESTRATOR env var.
    """
    import os
    prev = os.environ.pop("MODEL_ORCHESTRATOR", None)
    try:
        assert stages.orchestrator_meta().model == "claude-sonnet-4-6"
    finally:
        if prev is not None:
            os.environ["MODEL_ORCHESTRATOR"] = prev
