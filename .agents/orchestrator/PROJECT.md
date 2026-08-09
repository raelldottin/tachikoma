# Project: Tachikoma Gauntlet Slice 3 — End-to-End Live Validation & Fixes

## Architecture
- Target slice: e2e-live-validation-and-fixes
- Key workflows: .github/workflows/daily-run.yml, .github/workflows/provision-pss-secrets.yml
- Key modules: sdk/client.py, run.py, tests/
- Workbench: automation/gauntlet/workbench.md

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Baseline & Live Log Analysis | Inspect repo baseline, run gh CLI workflow execution / log fetch, discover runtime exceptions | M1 | R1 |
| 2 | Runtime Exception Fixes | Fix unhandled runtime exceptions and parsing errors in sdk/client.py, run.py, etc. | M2 | R2 |
| 3 | Mocked Test Coverage | Add deterministic tests in tests/ reproducing and verifying fix for every failure mode | M3 | R3 |
| 4 | Independent Critic & Audit | Run Quality Bar critic review, forensic integrity audit, and full validation suite | M4 | R4 |
| 5 | Workbench & Quality Bar Update | Update workbench.md with evidence, test counts, critic verdict, and residual risks | M4 | R5 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Live Workflow Survey & Log Analysis | Repo baseline, gh CLI workflow trigger/fetch, log traceback analysis | none | DONE |
| 2 | Codebase Fixes | Fix discovered runtime errors in sdk/client.py and run.py | M1 | DONE |
| 3 | Mocked Test Suite | Add synthetic/mocked tests for all failure modes in tests/ | M2 | DONE |
| 4 | Critic Loop, Audit & Workbench | Make validations, critic pass verdict, audit clean verdict, workbench update | M3 | DONE |

## Code Layout
- Target modules: sdk/client.py, run.py, scripts/provision_account_secrets.py, .github/workflows/daily-run.yml, tests/test_e2e_live_fixes.py, automation/gauntlet/workbench.md
- Allowed paths: sdk/, run.py, scripts/, .github/workflows/, tests/, automation/gauntlet/workbench.md, README.template, README.md, docs/
- Budget max_files_changed: 10 (actual changed/created: 6 files)
