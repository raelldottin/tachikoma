# Survey and Technical Analysis: R2 Quality Bar, R3 Pilot Slice, and R5 Critic Loop

**Agent**: `teamwork_preview_explorer_m1_3`  
**Working Directory**: `/Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_explorer_m1_3`  
**Date**: 2026-08-06  

---

## 1. Executive Summary

This survey analyzes the structural and reliability requirements for establishing a supervised Gauntlet Loop in Tachikoma, focusing on the pilot slice `gauntlet-provisioning-dependency-and-secret-contract`. 

Key findings:
1. **R2 Quality Bar Mapping**: 11 of the 12 criteria are **Mandatory** for the provisioning pilot slice, while 1 criterion (Criterion 7: In-game state verification) is **N/A** for this secret-provisioning-only slice (or bounded by GitHub Actions API/fixture limitations).
2. **Supervisor Queue Ownership Rules**: `automation/queue/slices.json` and `automation/handoffs/` are strictly owned by the supervisor. Builder/critic slice agents MUST NOT modify queue state directly.
3. **Slice Structure & File Budget**: Slice schema mandates 10 required fields. For R3 (`gauntlet-provisioning-dependency-and-secret-contract`), a strict file budget (`max_files_changed: 10`) and narrow allowed paths must be enforced.
4. **R5 Independent Critic Loop**: The critic operates with strict context isolation (receiving diffs, tests, logs, and quality bar, but NO builder reasoning/claims). A formal JSON schema (`automation/schemas/critic_review.schema.json`) is defined to enforce verdict (`pass`/`fail`), `severity`, `quality_bar_failures`, `evidence`, `largest_remaining_gap`, and `required_next_action`.

---

## 2. R2 Quality Bar Survey & Applicability Mapping

`automation/gauntlet/quality-bar.md` will codify 12 reliability criteria. The mapping for the `Provision PSS Account Secrets` pilot slice is detailed below:

| # | Criterion Summary | Status | Applicability Rationale & Enforcement |
|---|---|---|---|
| 1 | **Unit, security & harness tests pass** | **Mandatory** | Required validations (`make syntax-check`, `make test`, `make test-security`, `make automation-check`, `git diff --check`) must all pass with zero errors. |
| 2 | **Mocked PSS network traffic** | **Mandatory** | All PSS network interactions in tests must use synthetic responses/fixtures (`unittest.mock` / HTTP mocks). No live endpoints contacted. |
| 3 | **Zero credential/token leakage** | **Mandatory** | Absolute security requirement. Passwords, emails, refresh tokens, access tokens, and device keys must never appear in stdout, stderr, exceptions, logs, or artifacts. |
| 4 | **Explicit structured account outcomes** | **Mandatory** | `scripts/provision_account_secrets.py` must emit deterministic, structured status summaries per account slot (0, 1, 5, or partially configured). |
| 5 | **Truthful workflow failure** | **Mandatory** | GitHub Actions job must exit non-zero on failure. `continue-on-error` must not mask provisioning errors. |
| 6 | **Bounded transient failure handling** | **Mandatory** | Token rotation/network retry logic must have explicit timeout/retry bounds and terminate cleanly without infinite loops or unhandled crashes. |
| 7 | **Mutating operation state verification** | **N/A** | This slice does not perform in-game PSS ship state or resource mutations. GitHub secret persistence is outside fixture capabilities and documented as a residual risk. |
| 8 | **Idempotency testing** | **Mandatory** | Repeated execution across 0, 1, or 5 configured accounts must be safe and idempotent without accumulating dirty state. |
| 9 | **Gameplay & resource spending unchanged** | **Mandatory (Invariant)** | Negative constraint: code changes in workflow/scripts must not alter PSS gameplay, resource spending, or room upgrade behavior in `sdk/` or `run.py`. |
| 10 | **`README.template` updated before `README.md`** | **Mandatory** | Generated `README.md` must strictly derive from `README.template`. Any doc updates must modify `README.template` first. |
| 11 | **Scope & file budget compliance** | **Mandatory** | Changes must stay strictly inside slice `allowed_paths` and comply with `max_files_changed` (budget = 10). |
| 12 | **Zero unresolved critical/high defects** | **Mandatory** | Independent critic review must return `verdict: "pass"` with `severity: "none"` before completion. |

---

## 3. Supervisor Queue Ownership & Policy Rules

### 3.1 Supervisor Ownership Policy
From `automation/queue/slices.json` policy and `automation/supervisor/policy.py`:
- **Supervisor-Owned Paths**: `["automation/queue/slices.json", "automation/handoffs/"]`
- **Builder/Critic Prohibition**: Builder and critic agents MUST NOT edit `slices.json` or write directly to `automation/handoffs/` during slice execution. Queue status transitions (`queued` -> `in_progress` -> `done`/`failed`/`blocked`) are performed exclusively by `run_next.py`.
- **Autonomous Chaining Limit**: `consecutive_autonomous_limit: 2` (supervisor stops for review after 2 consecutive runs).
- **Handoff Timeout**: 1800 seconds.

### 3.2 Slice Schema Structure
Defined in `automation/schemas/slice.schema.json`:
- `slice_id` (string): Unique identifier (e.g. `gauntlet-provisioning-dependency-and-secret-contract`).
- `title` (string): Human-readable title.
- `status` (string enum): `queued` | `in_progress` | `blocked` | `deferred` | `done` | `failed`.
- `priority` (integer): Lower value = higher priority.
- `domain` (string): Subsystem domain (e.g. `gauntlet-pilot`).
- `allowed_paths` (array of strings): Whitelisted file/directory path prefixes.
- `required_validations` (array of strings): Verbatim shell validation command strings.
- `depends_on` (array of strings): Slice IDs that must be in status `done` before entry.
- `max_files_changed` (integer): Maximum number of changed files permitted within scope.
- `notes` (string): Slice instructions, acceptance criteria, and constraints.

### 3.3 R3 Pilot Slice Configuration
The proposed queue record for R3:
```json
{
  "slice_id": "gauntlet-provisioning-dependency-and-secret-contract",
  "title": "Repair and prove Provision PSS Account Secrets workflow dependency and secret contract",
  "status": "queued",
  "priority": 10,
  "domain": "gauntlet-pilot",
  "allowed_paths": [
    ".github/workflows/provision-pss-secrets.yml",
    "scripts/provision_account_secrets.py",
    "tests/",
    "automation/gauntlet/",
    "automation/prompts/",
    "automation/schemas/",
    "automation/tests/",
    "requirements.txt",
    "Makefile",
    "README.template",
    "README.md"
  ],
  "required_validations": [
    "make automation-check",
    "make syntax-check",
    "make test",
    "make test-security",
    "make lint",
    "git diff --check"
  ],
  "depends_on": [
    "automation-harden-portability-and-handoffs"
  ],
  "max_files_changed": 10,
  "notes": "R3 Pilot Slice: Repair dependency installation using requirements.txt; enforce configuration contracts (0, 1, 5, partial accounts); sanitize tokens/credentials; enforce non-zero exit on failure; provide deterministic unit/integration test coverage."
}
```

---

## 4. Existing Schemas, Prompts & Gauntlet Architecture

### 4.1 Existing Schemas (`automation/schemas/`)
- `slice.schema.json`: Validates `automation/queue/slices.json`.
- `handoff.schema.json`: Validates handoff artifacts produced by builder runs. Required fields: `slice_id`, `status`, `summary`, `files_touched`, `validations_passed`, `validations_failed`, `proof_level`, `missing_proof_levels`, `contract_status_changes`, `residual_risks`, `recommended_next_slice`, `recommended_next_reason`, `repo_clean_status`, `git_mirror_status`, `dirty_paths_outside_scope`, `timestamp`.

### 4.2 Existing Prompts (`automation/prompts/`)
- `base.md`: Sets agent constraints, validation expectations, proof level ladder, safety rules, clean stop requirements.
- `slice.md`: Injects current slice context (`__SLICE_ID__`, `__ALLOWED_PATHS__`, `__HANDOFF_TEMPLATE_JSON__`, etc.).
- `review.md`: Used by supervisor when autonomous execution stops.

### 4.3 Gauntlet Directory (`automation/gauntlet/`)
- Directory currently absent.
- Target artifacts to be created during gauntlet execution:
  - `automation/gauntlet/workbench.md` (R1 baseline recording, round tracking, proof level, verdict).
  - `automation/gauntlet/quality-bar.md` (R2 12-point quality bar criteria).

---

## 5. R5 Independent Critic Loop Design & Schema Specification

### 5.1 Context Isolation Rules (What Critic Sees vs Does Not See)
To guarantee strict independence, context for the critic MUST be filtered by the supervisor:
- **PROHIBITED (Critic MUST NOT receive)**:
  - Builder hidden reasoning / chain-of-thought
  - Builder self-review / self-assessment
  - Builder rationalizations or implementation justifications
  - Unsupported claims of completion
- **PERMITTED (Critic MAY receive ONLY)**:
  - Slice goal, acceptance criteria, and constraints
  - `AGENTS.md`
  - `automation/gauntlet/quality-bar.md`
  - Relevant repository source files
  - Exact `git diff` (before vs after)
  - List of changed files
  - Raw stdout/stderr from required validation commands (`make test`, `make lint`, etc.)
  - Sanitized workflow simulations and test evidence artifacts

### 5.2 Critic Verdict JSON Schema (`automation/schemas/critic_review.schema.json`)
The critic must output a single, raw JSON object matching this draft-07 JSON schema:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Gauntlet Critic Review Verdict",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "verdict",
    "largest_remaining_gap",
    "severity",
    "evidence",
    "quality_bar_failures",
    "required_next_action"
  ],
  "properties": {
    "verdict": {
      "type": "string",
      "enum": ["pass", "fail"]
    },
    "largest_remaining_gap": {
      "type": "string",
      "description": "Empty string if verdict is pass. If fail, exactly one precise, highest-impact unresolved defect."
    },
    "severity": {
      "type": "string",
      "enum": ["none", "low", "medium", "high", "critical"],
      "description": "Severity level. Must be 'none' if verdict is pass."
    },
    "evidence": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1
      },
      "description": "Concrete evidence strings (diff snippets, log quotes, test outputs)."
    },
    "quality_bar_failures": {
      "type": "array",
      "items": {
        "type": "integer",
        "minimum": 1,
        "maximum": 12
      },
      "description": "Array of quality bar rule numbers (1 to 12) that failed."
    },
    "required_next_action": {
      "type": "string",
      "description": "Smallest concrete action needed by the builder to close the gap."
    }
  }
}
```

### 5.3 Verdict Invariants & Supervisor Feedback Loop
1. **Pass Condition Requirements**:
   - `verdict == "pass"`
   - `largest_remaining_gap == ""`
   - `severity == "none"`
   - `quality_bar_failures == []`
   - `required_next_action == ""`
   - All required validations passing.
2. **Fail Condition Requirements**:
   - `verdict == "fail"`
   - `largest_remaining_gap` is non-empty string naming **one** primary defect.
   - `severity` is one of `["low", "medium", "high", "critical"]`.
   - `quality_bar_failures` contains integer indices of failed R2 rules.
   - `evidence` contains at least one concrete proof line/quote.
   - `required_next_action` specifies the minimal builder intervention.
3. **Supervisor Chaining Protocol**:
   - Upon receiving a `fail` verdict, the supervisor extracts `largest_remaining_gap`, `evidence`, and `required_next_action` and injects them into a fresh builder prompt for iteration $N+1$.
   - Cycle repeats until critic outputs `verdict: "pass"` or supervisor stopping limits are reached.

---

## 6. Recommendations & Next Steps

1. **Intake Queue Entry**: Add `gauntlet-provisioning-dependency-and-secret-contract` to `automation/queue/slices.json` with `max_files_changed: 10`.
2. **Track Critic Schema**: Create `automation/schemas/critic_review.schema.json` and `automation/prompts/critic.md`.
3. **Initialize Gauntlet Artifacts**: Create `automation/gauntlet/workbench.md` (R1) and `automation/gauntlet/quality-bar.md` (R2).
