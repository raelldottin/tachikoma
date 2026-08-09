# Tachikoma Gauntlet Pilot Execution Plan

## Plan Overview
Target: Supervised Gauntlet Loop pilot for `Provision PSS Account Secrets` workflow.

### Step 1: Baseline & Quality Bar (M1)
- Dispatch Explorers (`teamwork_preview_explorer`) to inspect git branch, commit SHA, upstream status, dirt, slice state, known failures, gameplay settings, and run baseline validation checks.
- Dispatch Worker (`teamwork_preview_worker`) to create `automation/gauntlet/workbench.md` with baseline records and `automation/gauntlet/quality-bar.md` with 12-point reliability criteria.

### Step 2: Pilot Slice & Critic Infra (M2)
- Define `gauntlet-provisioning-dependency-and-secret-contract` slice in queue/schema infrastructure.
- Establish strict JSON schema for Independent Critic and evidence formatting.

### Step 3: Repair Provisioning Workflow (M3)
- Add regression test for missing dependency failure.
- Fix dependency contract via `requirements.txt` / Makefile / workflow.
- Implement configuration contract (0 accounts, 1 account, 5 accounts, partial account).
- Implement token & output safety (zero secret leaking, sanitized stdout/stderr).
- Ensure deterministic exit semantics (0 for success/no-op, nonzero for failure).

### Step 4: Critic Loop, Validation & Evidence (M4)
- Execute independent fresh Critic review (`teamwork_preview_reviewer` / `teamwork_preview_critic`).
- Run required validation command suite.
- Run provisioning-specific test suite.
- Update `automation/gauntlet/workbench.md` with builder/critic round history and final state.

### Step 5: Final Victory Claim (M5)
- Verify clean forensic audit and Critic PASS verdict.
- Send completion message to parent/Sentinel.
