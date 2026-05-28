"""Guardrail tests on the LLM-facing rule text.

These assert that the harvest / early-exit / high-conviction-hold /
cooldown guidance in the prompts stays in sync with the numbers
centralised in lib/risk.py. The prompts are markdown (the LLM reads them
verbatim), so the numbers can't be interpolated — this test prevents the
prompt text and the code constants from silently drifting apart.
"""
from __future__ import annotations

from pathlib import Path

from lib import risk

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def _read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def test_constructor_harvest_threshold_is_30_not_20():
    text = _read("constructor.md")
    assert f"{int(risk.HARVEST_MIN_GAIN_PCT)}%" in text  # "30%"
    # The old 20% harvest trigger must be gone from the harvest section.
    assert "≥ 20% of cost basis" not in text
    assert "+30%" in text  # the [-25%, +30%] band


def test_constructor_allows_partial_harvest_and_hold_judgment():
    text = _read("constructor.md").lower()
    assert "partial" in text  # partial harvest / trim is explicitly allowed
    assert "trim" in text
    assert "let it run" in text  # high-conviction hold carve-out


def test_constructor_early_exit_and_high_conviction_thresholds():
    text = _read("constructor.md")
    # Early-exit discretion below 0.6 confidence.
    assert "below 0.6" in text
    # High-conviction hold above 0.75.
    assert "0.75" in text


def test_constructor_documents_reentry_cooldown_and_override():
    text = _read("constructor.md").lower()
    assert "cooldown" in text
    assert "0.8" in text  # override threshold


def test_strategist_mentions_confidence_drop_signal():
    text = _read("strategist.md")
    assert "below 0.6" in text
