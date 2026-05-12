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


def test_scenarios_max_tokens_above_48k_guard():
    """The scenarios stage uses `thinking: adaptive`, and thinking tokens
    are charged against the output budget. Each iteration of the cardinality
    rule has pushed scenarios output bigger:
      - PR #26 made it emit one row per candidate (was: drop low-EV)
      - PR #27 bumped 16k → 32k after observing truncation at ~7843 chars
      - PR #39-41 made option underlyings emit BOTH call+put rows + full
        option_rationale per direction, pushing output past 32k again
        (observed truncation at ~8697 chars on the regime-aware run)
    Floor is now 48k so any future "trim tokens" edit can't take us back
    below the level where Sonnet 4.6's adaptive thinking + the full
    cardinality output reliably fits. Sonnet 4.6's max output is 64k."""
    cfg = stages.scenarios()
    assert cfg.max_tokens >= 48_000, (
        f"scenarios max_tokens={cfg.max_tokens} risks truncation when "
        "adaptive thinking eats the output budget on full-cardinality runs"
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
