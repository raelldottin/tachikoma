# Context Summary

## Project Objective
Make Tachikoma’s scheduled account automation reliable, secure, truthful, and diagnosable by applying a supervised Gauntlet Loop with independent builder and critic runs for the `Provision PSS Account Secrets` workflow.

## Key Files & Directories
- `ORIGINAL_REQUEST.md`: System requirements and prompt
- `AGENTS.md`: Repository instructions and rules
- `automation/gauntlet/workbench.md`: Baseline and execution records
- `automation/gauntlet/quality-bar.md`: 12-point reliability quality bar
- `.github/workflows/provision-pss-secrets.yml`: Provisioning workflow
- `scripts/provision_account_secrets.py`: Provisioning script
- `requirements.txt`: Master dependency list
- `Makefile`: Build & test automation targets

## Hard Constraints
- DISPATCH-ONLY Orchestrator: Delegate all investigation, code changes, build/test execution, and critic reviews.
- Strict isolation of credential material (mocked PSS traffic, zero real tokens/secrets).
- Non-interfering slice budget and allowed paths.
