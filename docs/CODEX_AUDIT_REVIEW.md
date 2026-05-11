# Codex Audit Review Log

This file is the canonical audit handoff between Codex (review/audit) and Claude (implementation).

## Related Documents
- Audit index: `docs/CODEX_AUDIT_INDEX.md`

## How to use
- Ask Codex to append new audit entries to this file.
- Ask Claude to read this file first before planning or implementing changes.
- Treat the latest entry as the current working audit context unless stated otherwise.

---

## Entry Template

### Audit Entry
- **Entry ID:** `YYYY-MM-DD-<short-topic>`
- **Timestamp (UTC):** `YYYY-MM-DDTHH:MM:SSZ`
- **Auditor:** `Codex`
- **Mode:** `Read-only audit` / `Code review` / `Post-change verification`
- **Scope Reviewed:**
  - `path/to/file_or_module`
  - `...`

#### Executive Summary
- 3–6 bullets summarizing the most important outcomes.

#### Findings
- **Finding 1 (Severity: High/Medium/Low):**
  - Observation
  - Why it matters
  - Evidence (file references, behavior, assumptions)
- **Finding 2 ...**

#### Strategic Risks
- Concise bullets of systemic risks (e.g., data decay, schema mismatch, orchestration fragility).

#### Recommended Tasks
- Rank by:
  - Impact on Alpha (1–5)
  - Impact on Stability (1–5)
  - Complexity (1–5)
  - Priority (P0/P1/P2)

#### Implementation Notes for Claude
- Practical guidance to convert findings into phased tickets and safe rollout steps.

#### Validation / Evidence
- Commands executed:
  - `command 1`
  - `command 2`
- Key artifacts inspected:
  - `file paths`

#### Open Questions
- Unresolved assumptions, missing telemetry, or decisions required.

---

## Entries

### Audit Entry
- **Entry ID:** `2026-05-11-pipeline-handoff-risk-audit`
- **Timestamp (UTC):** `2026-05-11T15:47:27Z`
- **Auditor:** `Codex`
- **Mode:** `Read-only audit`
- **Scope Reviewed:**
  - `orchestrator.py`
  - `lib/llm.py`
  - `lib/risk.py`
  - `monitor.py`
  - `schemas/research.schema.json`
  - `schemas/scenarios.schema.json`
  - `schemas/portfolio.schema.json`
  - `prompts/constructor.md`

#### Executive Summary
- Pipeline is structurally sound but semantic fidelity decays across Screen → Research → Scenarios → Construct handoffs.
- Highest risk is narrative compression where Opus receives summarized abstractions rather than preserved adversarial evidence.
- Schema enforcement is strong locally, but provider-side sanitized schema can diverge and introduce retry/abort friction.

#### Findings
- **Finding 1 (Severity: High): Information decay across stage handoffs.**
  - Observation: Entire upstream payloads are serialized into text prompts each stage, with compressive narrative transfer.
  - Why it matters: Final sizing/allocation may overweight compressed summaries instead of raw bull/bear nuance.
  - Evidence: `stage_scenarios()` and `stage_construct()` pass prior-stage JSON via user text blocks.
- **Finding 2 (Severity: Medium): Candidate truncation before deep research.**
  - Observation: Research fan-out is capped using `screen.passed[:8]`.
  - Why it matters: Ordering bias can drop valid opportunities before adversarial analysis.
- **Finding 3 (Severity: Medium): Schema mismatch risk between provider and local validation.**
  - Observation: Structured-output schema is sanitized/weakened for provider acceptance while local validation remains stricter.
  - Why it matters: Can increase retry/abort frequency and reduce reliability under malformed outputs.
- **Finding 4 (Severity: Medium): Contradictory bull/bear confidence is under-adjudicated.**
  - Observation: `confidence_delta` exists but no explicit conflict-resolution contract before Construct.
  - Why it matters: High-confidence disagreement may be averaged narratively without deterministic treatment.
- **Finding 5 (Severity: Medium): Kill-monitor blind spots under missing marks.**
  - Observation: Positions with missing marks are skipped in monitor evaluation.
  - Why it matters: Risk controls may fail silently during data gaps/stale feeds.

#### Strategic Risks
- Narrative compression and context clipping at stage boundaries.
- Reliability drift from schema duality (sanitized remote vs strict local).
- Operational fragility under stale/missing market data and parse fallbacks.

#### Recommended Tasks
- Add explicit contradiction-resolution policy between bull/bear outputs before scenarios/construct.
- Insert a critic gate post-scenarios to challenge probabilities, EV, and tail risk assumptions.
- Add regime classification to modulate risk budget/exposure dynamically.
- Build feedback memory from paper outcomes into construct inputs (bounded, structured).
- Harden mark/staleness handling in monitor to avoid silent kill-condition blind spots.

#### Implementation Notes for Claude
- Prioritize P0: (1) schema consistency hardening, (2) anti-decay handoff integrity into Opus.
- Keep rollout reversible: feature flags, additive schema fields, and staged validation gates.
- Add observability for conflict states, missing-mark coverage, and schema retry/abort counts.

#### Validation / Evidence
- Commands executed:
  - `sed -n '1,560p' orchestrator.py`
  - `sed -n '1,320p' lib/llm.py`
  - `sed -n '1,320p' monitor.py`
  - `sed -n '1,260p' lib/risk.py`
  - `sed -n '1,260p' schemas/research.schema.json`
  - `sed -n '1,260p' schemas/scenarios.schema.json`
  - `sed -n '1,260p' schemas/portfolio.schema.json`
  - `sed -n '1,260p' prompts/constructor.md`
- Key artifacts inspected:
  - `orchestrator.py`, `lib/llm.py`, `monitor.py`, `lib/risk.py`
  - schema files under `schemas/` and constructor prompt in `prompts/`

#### Open Questions
- Should research fan-out remain fixed at 8 or become regime/cost adaptive?
- Should unresolved bull/bear conflicts force abstain/all-cash paths by policy?
- What telemetry threshold should trigger automatic halt on schema retry failures?


> Add newest entries at the top of this section.
