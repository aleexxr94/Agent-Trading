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
        # 64000 (was 32768, originally 8192): PRs #39-41 expanded scenarios
        # output (option underlyings now emit BOTH call and put rows + full
        # option_rationale with strike, expiry, dte, dte_rationale,
        # strike_rationale per side). With 8 ETF rows + up to 6 option-direction
        # rows × ~1000 tokens each, combined with `thinking: adaptive` consuming
        # most of the budget, 32k truncated at char 8697 on a real paper run
        # (Sonnet returned nothing on the first attempt — all tokens went to
        # thinking — and the retry got ~2200 output tokens of JSON before
        # running out).
        # 64000 is Sonnet 4.6's actual max_tokens cap. Setting 65536 (a power
        # of two) trips a 400 at request validation BEFORE the retry/schema
        # logic can run — Codex P1 on PR #42. Sonnet's hard ceiling is 64k,
        # so this is now the highest legal value.
        max_tokens=64_000,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )


def constructor() -> StageConfig:
    return StageConfig(
        stage="construct",
        model=_model("MODEL_CONSTRUCTOR", "claude-opus-4-7"),
        system_prompt=_load("constructor.md"),
        schema_filename="portfolio.schema.json",
        # 32000: constructor consumes a 14-row scenarios payload (8 ETFs +
        # up to 6 option-direction rows per PRs #39-41) and emits a 1–12
        # position portfolio.json with sizing math + kill conditions per
        # row. The cap was originally tuned for Opus 4.6 (interleaved-
        # thinking-free ceiling). Opus 4.7 has the same default cap, so
        # 32k is safe; if we ever bump to Sonnet (via env override) the
        # 32k value sits comfortably inside Sonnet 4.6's 64k cap too.
        #
        # Model: claude-opus-4-7. The construct stage is where actual
        # trade decisions are made on a $2,500 leveraged-ETF+options
        # account — multi-position sizing under correlation, kill-
        # condition tailoring, EV thresholding. Reasoning quality has
        # direct PnL impact and the cost difference vs Sonnet
        # (~$0.50/run on construct alone) is meaningful but well
        # within the $3/run cap. Opus 4.7 specifically (not 4.6):
        # same price, newer model, supports "xhigh" effort which 4.6
        # doesn't. The PR β downgrade to Sonnet (2026-05-13) was
        # reverted on 2026-05-13 after honestly re-thinking the
        # asymmetry: cost delta is ~$60/month at 4 cycles/day; a 5%
        # PnL improvement from better position-picking is ~$50/month
        # on $2,500 NAV, but the downside of a single bad construct
        # decision (4h of bad positions before re-evaluation) is
        # disproportionately large vs the saved cents. Override with
        # MODEL_CONSTRUCTOR=claude-sonnet-4-6 if cost dominates.
        max_tokens=32_000,
        thinking={"type": "adaptive"},
        output_config_extras={"effort": "high"},
    )


def orchestrator_meta() -> StageConfig:
    """Used for the next-run scheduling / meta-decision call.

    Sonnet 4.6 (downgraded from Opus 4.6 on 2026-05-13 PR β; kept on
    Sonnet when constructor was promoted back to Opus 4.7 on the same
    day). This call is small (2k output, schema-free free-form text)
    and the deterministic 4h/6h fallback heuristic kicks in cleanly
    if the LLM output is unusable — Sonnet is plenty for choosing a
    1–24h next-run window. Override with MODEL_ORCHESTRATOR=claude-opus-4-7
    if a quality regression on scheduling decisions ever shows up.
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
