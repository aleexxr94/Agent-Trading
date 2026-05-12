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


def test_scenarios_max_tokens_above_8192_guard():
    """The scenarios stage uses `thinking: adaptive`, and thinking tokens
    are charged against the output budget. Combined with the PR #26 prompt
    that emits a row for every researched candidate (including negative-EV
    ones), 16384 was insufficient — observed truncation at ~7843 chars on
    the first paper run after the prompt relaxation. 24k is the safe floor."""
    cfg = stages.scenarios()
    assert cfg.max_tokens >= 24_000, (
        f"scenarios max_tokens={cfg.max_tokens} risks truncation when "
        "adaptive thinking eats the output budget on 8-candidate runs"
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
