# Reliability Quality Bar

This document defines the 12-point reliability quality bar required for Gauntlet evaluation. Each criterion is classified as either mandatory or non-applicable (N/A) for the current provisioning pilot.

## Criteria List

1. **Criterion 1 (Mandatory)**: Unit, security, and automation-harness tests pass.
2. **Criterion 2 (Mandatory)**: All Pixel Starships traffic used in automated tests is mocked.
3. **Criterion 3 (Mandatory)**: Credentials, passwords, refresh tokens, access tokens, device keys, and account identifiers do not appear in source, fixtures, logs, exceptions, workflow summaries, or artifacts.
4. **Criterion 4 (Mandatory)**: Every configured account receives an explicit structured outcome.
5. **Criterion 5 (Mandatory)**: GitHub Actions fails truthfully when required provisioning fails.
6. **Criterion 6 (Mandatory)**: Expected transient failures have bounded handling and an explicit terminal state.
7. **Criterion 7 (N/A for provisioning pilot)**: Mutating operations verify their resulting state when the slice and available fixtures permit it. (N/A: provisioning does not perform in-game ship state mutations, and live GitHub secret store writing is an offline fixture limitation documented as residual risk).
8. **Criterion 8 (Mandatory)**: Idempotency is tested where repeated execution is expected to be safe.
9. **Criterion 9 (Mandatory)**: Existing gameplay and resource-spending behavior remains unchanged unless a slice explicitly authorizes a change.
10. **Criterion 10 (Mandatory)**: Documentation changes update `README.template` before generated `README.md`.
11. **Criterion 11 (Mandatory)**: Changes stay inside the slice's allowed paths and file budget.
12. **Criterion 12 (Mandatory)**: No unresolved critical or high-severity defect remains in the independent critic review.
