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
    """Each candidate's base/bull/bear narrative is ~700 tokens. 8 candidates
    × ~700 = ~5,600 plus JSON overhead, headroom for verbose narratives.
    16384 was chosen as the safe doubling; anything <12k is unsafe."""
    cfg = stages.scenarios()
    assert cfg.max_tokens >= 12_000, (
        f"scenarios max_tokens={cfg.max_tokens} risks the regression where "
        "the LLM truncates mid-JSON on 8-candidate runs"
    )


def test_constructor_max_tokens_above_8192_guard():
    """Constructor emits 3-12 positions each with entry_thesis,
    kill_conditions, sizing math, plus envelope rationales. Same defensive
    bump as scenarios so this stage doesn't become the next bottleneck."""
    cfg = stages.constructor()
    assert cfg.max_tokens >= 12_000, (
        f"constructor max_tokens={cfg.max_tokens} risks truncation on full "
        "3-12 position portfolios with verbose theses"
    )
