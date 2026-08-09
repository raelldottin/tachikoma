## 2026-08-06T06:00:18Z
You are teamwork_preview_critic_m4_1.
Your working directory is: /Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_critic_m4_1

MANDATORY CONTEXT ISOLATION:
You are acting as the Independent Critic per ORIGINAL_REQUEST.md §R5.
You MUST NOT rely on builder self-reviews, builder reasoning, or builder claims. You MUST evaluate actual codebase diffs, test outputs, and validation execution results against the Quality Bar.

MANDATORY ASSIGNMENT:
Read /Users/raelldottin/Documents/Personal/tachikoma/ORIGINAL_REQUEST.md and AGENTS.md before starting.

Input Files to Inspect:
- `automation/gauntlet/quality-bar.md` (12-point reliability quality bar)
- `automation/gauntlet/slice_definition.json` (pilot slice configuration & budget)
- `automation/schemas/critic_review.schema.json` (JSON schema for your review verdict)
- `.github/workflows/provision-pss-secrets.yml`
- `scripts/provision_account_secrets.py`
- `tests/test_provision_account_secrets.py`
- `automation/gauntlet/workbench.md`

Validation Commands to Execute and Verify:
- `make automation-check`
- `make syntax-check`
- `make test`
- `make test-security`
- `make lint`
- `git diff --check`

Instructions:
1. Run all 6 required validation commands. Verify each returns exit code 0 cleanly.
2. Inspect `scripts/provision_account_secrets.py`, `.github/workflows/provision-pss-secrets.yml`, and `tests/test_provision_account_secrets.py` against all 12 criteria in `automation/gauntlet/quality-bar.md`:
   - Criterion 1: Unit, security, automation-harness tests pass.
   - Criterion 2: All PSS traffic in tests is mocked.
   - Criterion 3: Credentials/tokens/passwords/account IDs never appear in source, fixtures, logs, exceptions, summaries, artifacts.
   - Criterion 4: Every configured account receives explicit structured outcome.
   - Criterion 5: GHA workflow fails truthfully when required provisioning fails.
   - Criterion 6: Bounded handling for transient failures & explicit terminal state.
   - Criterion 7: N/A for provisioning pilot.
   - Criterion 8: Idempotency tested for repeated execution.
   - Criterion 9: Existing gameplay and resource-spending behavior unchanged.
   - Criterion 10: README.template updated before generated README.md (if docs updated).
   - Criterion 11: Changes stay inside allowed paths and max_files_changed budget (<= 10).
   - Criterion 12: No unresolved critical or high-severity defect remains.

3. Produce strict JSON output matching `automation/schemas/critic_review.schema.json` and save it to:
   `/Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_critic_m4_1/critic_review.json`

Required JSON shape:
```json
{
  "verdict": "pass",
  "largest_remaining_gap": "",
  "severity": "none",
  "evidence": [
    ...
  ],
  "quality_bar_failures": [],
  "required_next_action": ""
}
```

4. Write a self-contained handoff report to:
   `/Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_critic_m4_1/handoff.md`

When complete, send a message to parent with your verdict and handoff path.
