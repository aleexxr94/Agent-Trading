"""Per-stage LLM configuration.

Reads model IDs from env (Opus 4.7 / Sonnet 4.6 / Haiku 4.5 by default) and
loads system prompts from prompts/.

Effort/thinking notes:
  - Haiku 4.5 does NOT accept output_config.effort and will 400 if passed.
  - Sonnet 4.6 supports low/medium/high; Opus 4.6/4.7 supports the full set
    incl. "max" (Opus-tier) and "xhigh" (Opus 4.7-only).
  - Adaptive thinking is recommended for the 4.6/4.7 family. For Haiku we
    leave thinking unset (the screener doesn't need it).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"


def _load(name: str) -> str:
    path = PROMPT_DIR / name
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class StageConfig:
    stage: str
    model: str
    system_prompt: str
    schema_filename: str | None
    max_tokens: int
    thinking: dict | None
    output_config_extras: dict  # merged with structured-output format if present


def _model(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default)


# Lazy loaders so test runs that mock stages don't need every prompt file.
def screener() -> StageConfig:
    return StageConfig(
        stage="screen",
        model=_model("MODEL_SCREENER", "claude-haiku-4-5"),
        system_prompt=_load("screener.md"),
        schema_filename=None,  # screen.json is free-form universe summary
        max_tokens=4096,
        thinking=None,                # Haiku: no thinking
        output_config_extras={},      # Haiku: NO effort (would 400)
    )


def bull() -> StageConfig:
    return StageConfig(
        stage="research_bull",
        model=_model("MODEL_RESEARCH", "claude-sonnet-4-6"),
        system_prompt=_load("bull.md"),
        schema_filename=None,  # bull/bear merged into research.json by orchestrator
        max_tokens=4096,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )


def bear() -> StageConfig:
    return StageConfig(
        stage="research_bear",
        model=_model("MODEL_RESEARCH", "claude-sonnet-4-6"),
        system_prompt=_load("bear.md"),
        schema_filename=None,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )


def scenarios() -> StageConfig:
    return StageConfig(
        stage="scenarios",
        model=_model("MODEL_SCENARIOS", "claude-sonnet-4-6"),
        system_prompt=_load("scenarios.md"),
        schema_filename="scenarios.schema.json",
        # 32768 (was 16384, originally 8192): the relaxed scenarios.md from
        # PR #26 made the stage emit a row for every researched candidate
        # (instead of dropping low-EV ones). Combined with `thinking: adaptive`
        # — which charges thinking tokens against the output budget — 16384
        # wasn't enough: the first paper run after PR #26 truncated mid-string
        # at ~7843 chars (~2000 output tokens after thinking consumed the rest)
        # and both retry attempts failed the same way, crashing the orchestrator
        # before construct could even run. 32k gives thinking ample room while
        # still leaving 4-8k output tokens for 8 candidates of scenarios JSON.
        max_tokens=32768,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )


def constructor() -> StageConfig:
    return StageConfig(
        stage="construct",
        model=_model("MODEL_CONSTRUCTOR", "claude-opus-4-6"),
        system_prompt=_load("constructor.md"),
        schema_filename="portfolio.schema.json",
        # 16384 (was 8192): same headroom rationale as scenarios — constructor
        # outputs 1-12 positions each with entry_thesis, kill_conditions,
        # sizing math, plus a portfolio-level construction_rationale and
        # all_cash_rationale envelope. Defensive bump to match scenarios so the
        # construct stage doesn't become the next bottleneck.
        max_tokens=16384,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )


def orchestrator_meta() -> StageConfig:
    """Used for the next-run scheduling / meta-decision call (Opus tier)."""
    return StageConfig(
        stage="meta",
        model=_model("MODEL_ORCHESTRATOR", "claude-opus-4-6"),
        system_prompt=_load("orchestrator.md"),
        schema_filename=None,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )
