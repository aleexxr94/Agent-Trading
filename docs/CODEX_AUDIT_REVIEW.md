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

> Add newest entries at the top of this section.
