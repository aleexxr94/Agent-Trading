"""StageConfig invariants — v2 pipeline.

v2 stages: strategist + constructor + orchestrator_meta (was screener +
bull + bear + scenarios + constructor + orchestrator_meta in v1).
"""
from __future__ import annotations

import os

from lib import stages


def test_strategist_max_tokens_within_provider_caps():
    """Strategist replaces the v1 scenarios stage. Same family of failure
    mode (adaptive thinking can eat output budget on multi-candidate
    inputs) but smaller payload — 15 ticker rows in → 6 candidate rows
    out is far less than v1 scenarios' ~14 output rows × ~1000 tokens.
    32k is comfortably above what the actual output needs (~3-5k tokens)
    AND inside Sonnet 4.6's 64k provider cap.
    """
    cfg = stages.strategist()
    assert 16_000 <= cfg.max_tokens <= 64_000, (
        f"strategist max_tokens={cfg.max_tokens} is outside [16000, 64000]"
    )


def test_strategist_effort_is_medium():
    """v2 inherits the PR ε lesson: Sonnet + adaptive + effort=high can
    starve output. Strategist defaults to medium for the same reason."""
    cfg = stages.strategist()
    assert cfg.output_config_extras.get("effort") == "medium", (
        f"strategist effort={cfg.output_config_extras.get('effort')!r}; "
        'expected "medium" (PR ε lesson — high+adaptive empty-output failure mode).'
    )


def test_constructor_max_tokens_within_provider_caps():
    """Constructor consumes signals + view (~30 rows total) and emits
    1-12 positions each with entry_thesis, kill_conditions, sizing.
    32k is the Opus 4.6 default cap (without interleaved-thinking beta)
    and well inside Opus 4.7's caps too.
    """
    cfg = stages.constructor()
    assert 24_000 <= cfg.max_tokens <= 32_000, (
        f"constructor max_tokens={cfg.max_tokens} is outside [24000, 32000]"
    )


def test_constructor_default_model_is_opus_4_7():
    """Lock the constructor model default. Override via MODEL_CONSTRUCTOR."""
    prev = os.environ.pop("MODEL_CONSTRUCTOR", None)
    try:
        assert stages.constructor().model == "claude-opus-4-7"
    finally:
        if prev is not None:
            os.environ["MODEL_CONSTRUCTOR"] = prev


def test_strategist_default_model_is_sonnet():
    """Lock the strategist model default. Sonnet 4.6 + medium effort is
    sufficient for the 15-row signals → 6-candidate view task. Override
    via MODEL_STRATEGIST if a quality regression shows up."""
    prev = os.environ.pop("MODEL_STRATEGIST", None)
    try:
        assert stages.strategist().model == "claude-sonnet-4-6"
    finally:
        if prev is not None:
            os.environ["MODEL_STRATEGIST"] = prev


def test_orchestrator_meta_default_model_is_sonnet():
    """Meta scheduler stays on Sonnet — small payload, no schema,
    deterministic fallback heuristic covers any LLM failure."""
    prev = os.environ.pop("MODEL_ORCHESTRATOR", None)
    try:
        assert stages.orchestrator_meta().model == "claude-sonnet-4-6"
    finally:
        if prev is not None:
            os.environ["MODEL_ORCHESTRATOR"] = prev
