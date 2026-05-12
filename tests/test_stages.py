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


def test_constructor_max_tokens_above_8192_guard():
    """Constructor emits 1-12 positions each with entry_thesis,
    kill_conditions, sizing math, plus envelope rationales. Same defensive
    bump as scenarios so this stage doesn't become the next bottleneck."""
    cfg = stages.constructor()
    assert cfg.max_tokens >= 12_000, (
        f"constructor max_tokens={cfg.max_tokens} risks truncation on full "
        "1-12 position portfolios with verbose theses"
    )
