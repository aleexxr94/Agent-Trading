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


def test_constructor_default_model_is_sonnet():
    """Lock the PR-β downgrade: constructor default is Sonnet 4.6.

    Lifted from chat conversation 2026-05-13: original spec said "sonnet
    for orchestrator, haiku for screening, sonnet for adversarial
    research" — i.e. Sonnet was always the default tier and Opus was an
    interim choice for the highest-stakes call. After observing typical
    runs cost $0.80-2.20 with Opus at construct + meta, the team
    downgraded both to Sonnet to keep cycles comfortably inside the
    $3/run cap (PR α bumped from $2). Override is preserved via
    MODEL_CONSTRUCTOR env var.
    """
    import os
    # Ensure no test-env override is shadowing the default we're pinning.
    prev = os.environ.pop("MODEL_CONSTRUCTOR", None)
    try:
        assert stages.constructor().model == "claude-sonnet-4-6"
    finally:
        if prev is not None:
            os.environ["MODEL_CONSTRUCTOR"] = prev


def test_orchestrator_meta_default_model_is_sonnet():
    """Lock the PR-β downgrade for the meta scheduling call. Same
    rationale as constructor — small payload, no schema, Sonnet
    sufficient. Override preserved via MODEL_ORCHESTRATOR env var.
    """
    import os
    prev = os.environ.pop("MODEL_ORCHESTRATOR", None)
    try:
        assert stages.orchestrator_meta().model == "claude-sonnet-4-6"
    finally:
        if prev is not None:
            os.environ["MODEL_ORCHESTRATOR"] = prev
