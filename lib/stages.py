"""Per-stage LLM configuration — v2 pipeline.

The v2 pipeline has TWO LLM-bearing stages (was 5 in v1: screen,
research, scenarios, construct, meta). v1's screener was replaced by
the deterministic ``lib.signals`` module; bull/bear research and
scenarios were collapsed into a single strategist call that reads the
signals table directly.

Stages:
  - screener   — DELETED (replaced by lib.signals)
  - bull/bear  — DELETED (replaced by strategist)
  - scenarios  — DELETED (replaced by strategist)
  - strategist — Sonnet 4.6, ~$0.05/call. Reads signals.json, emits
    a regime classification + up to 6 candidate ideas with thesis +
    confidence per row.
  - construct  — Opus 4.7, ~$0.20/call. Reads signals + view, emits
    portfolio.json.
  - meta       — Sonnet 4.6, ~$0.02/call. Picks the next-run cadence.

Reads model IDs from env (Opus 4.7 / Sonnet 4.6 / Haiku 4.5 by default)
and loads system prompts from prompts/.

Effort/thinking notes:
  - Haiku 4.5 does NOT accept output_config.effort and will 400 if
    passed (not used by v2 — kept here for completeness).
  - Sonnet 4.6 supports low/medium/high; Opus 4.6/4.7 supports the
    full set incl. "max" (Opus-tier) and "xhigh" (Opus 4.7-only).
  - Adaptive thinking is recommended for the 4.6/4.7 family.
  - Strategist effort is "medium" (same rationale as v1 scenarios in
    PR ε: high+adaptive can starve output on multi-candidate inputs).
  - Constructor effort is "high" — it IS the soft-judgement call.
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


def strategist() -> StageConfig:
    """v2 stage 2 — reads signals.json, emits view.json.

    Sonnet 4.6 + effort=medium + 32k max_tokens. Output is a regime
    classification (one of 7 enum values) plus 0–6 ranked candidate
    ideas. Schema-bounded; no markdown fences allowed.

    Why medium effort: same lesson as PR ε on the v1 scenarios stage —
    Sonnet+adaptive+effort=high can consume the entire token budget on
    thinking and return an empty body. Strategist input is ~15 rows
    (vs v1 scenarios' ~14), so the same failure mode applies. Medium
    caps thinking-budget allocation, leaves headroom for the structured
    JSON output.
    """
    return StageConfig(
        stage="strategist",
        model=_model("MODEL_STRATEGIST", "claude-sonnet-4-6"),
        system_prompt=_load("strategist.md"),
        schema_filename="view.schema.json",
        max_tokens=32_000,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "medium"},
    )


def constructor() -> StageConfig:
    """v2 stage 3 — reads signals + view, emits portfolio.json.

    Opus 4.7 + effort=high + 32k max_tokens. This IS the soft-judgement
    call — multi-position trade-off reasoning under correlation and
    the 15%/position cap, kill-condition tailoring, EV thresholding.

    Why Opus 4.7 specifically: same family pricing as 4.6 ($5/M in,
    $25/M out) but newer reasoning + supports xhigh effort. Cost
    differential vs Sonnet on construct is ~$0.50/cycle (~$10/month
    at 2 cycles/weekday) — well inside the $3/run cap and the
    quality-vs-cost asymmetry favours Opus on the actual trade
    decision. Override via MODEL_CONSTRUCTOR if cost dominates.
    """
    return StageConfig(
        stage="construct",
        model=_model("MODEL_CONSTRUCTOR", "claude-opus-4-7"),
        system_prompt=_load("constructor.md"),
        schema_filename="portfolio.schema.json",
        max_tokens=32_000,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )


def orchestrator_meta() -> StageConfig:
    """v2 stage 5 — chooses next-run cadence within market hours.

    Sonnet 4.6, 2k max_tokens, no schema (free-form ISO timestamp +
    rationale). Has a deterministic 4h/6h fallback heuristic in
    orchestrator.py so an unusable LLM output never breaks cadence.
    """
    return StageConfig(
        stage="meta",
        model=_model("MODEL_ORCHESTRATOR", "claude-sonnet-4-6"),
        system_prompt=_load("orchestrator.md"),
        schema_filename=None,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )
