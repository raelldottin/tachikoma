## 2026-08-06T01:46:01Z
MANDATORY ASSIGNMENT:
Read /Users/raelldottin/Documents/Personal/tachikoma/ORIGINAL_REQUEST.md and AGENTS.md before starting.

Task: Milestone 3 Implementation (R4 Provisioning Workflow Repairs)
Authorized allowed paths to edit/create:
- `.github/workflows/provision-pss-secrets.yml`
- `scripts/provision_account_secrets.py`
- `tests/test_provision_account_secrets.py`
- `README.template`
- `README.md`

Instructions:
1. Update `.github/workflows/provision-pss-secrets.yml`:
   In the `Install dependencies` step (line 46), change:
   `pip install requests xmltodict`
   to:
   `pip install -r requirements.txt`

2. Refactor `scripts/provision_account_secrets.py`:
   - Implement **Zero Accounts Contract**: If no accounts are configured in environment variables (`PSS_EMAIL_1`..`5`), print a clear, sanitized summary to stdout (`No accounts configured. Safe exit 0.`), perform ZERO network activity, and exit cleanly with status `0`.
   - Implement **Partial Account Contract**: Perform a pre-flight validation on all 5 slots before initializing `Client` or calling network APIs. If an account slot has a partial configuration (e.g. email set without password, or password set without email), log an error identifying the slot (e.g. `Account 1: Partial configuration - missing password`), skip client initialization for that slot, record failure for the slot, and ensure final script exit code is `1` (nonzero).
   - Implement **Five Accounts Independent Processing**: Loop through all 5 account slots independently. Process each configured account without aborting immediately (`sys.exit(1)`) on an individual failure. Collect individual outcomes (`SUCCESS`, `FAILED`, `PARTIAL_CONFIG_FAILED`) for each slot.
   - Implement **Token and Output Safety**: REMOVE `print(json.dumps(new_tokens))` from stdout. Ensure refresh tokens, access tokens, passwords, and emails are NEVER printed to stdout or stderr. Sanitize all exception messages using `redact_secrets()`. Stdout summaries must report only safe slot labels (e.g. `Account 1: SUCCESS`, `Account 2: FAILED`) without credentials.
   - Implement **Deterministic Exit Semantics**: Exit `0` if zero accounts configured OR if every configured account succeeded. Exit `1` (nonzero) if any account failed or had partial configuration or dependency bootstrap failure.

3. Create `tests/test_provision_account_secrets.py`:
   Write unit tests with full `unittest.mock` for all PSS network calls:
   - `test_missing_ratelimit_dependency`: Verify regression behavior when `ratelimit` module is absent.
   - `test_zero_accounts_configured`: Verify exit status 0, stdout summary message, and zero network calls when no env vars are set.
   - `test_one_account_success`: Verify 1 configured account succeeds with exit 0 and produces sanitized stdout.
   - `test_one_account_failure`: Verify 1 configured account failure yields exit 1 and sanitized stderr without token leak.
   - `test_five_accounts_all_success`: Verify 5 configured accounts are all processed independently and return exit 0.
   - `test_five_accounts_partial_failure`: Verify account 1 failure does not abort remaining 4 accounts; all 5 are evaluated, and aggregate exit code is 1.
   - `test_partial_account_email_no_password`: Verify fast fail before network activity, slot error message, and exit status 1.
   - `test_partial_account_password_no_email`: Verify fast fail before network activity, slot error message, and exit status 1.
   - `test_token_safety_stdout_stderr`: Assert refresh tokens and access tokens NEVER appear in captured stdout or stderr during success or failure runs.
   - `test_mocked_failed_token_rotation_sanitized`: Verify useful redacted error message when token rotation raises an exception.

4. Run all validation targets and record exact command outputs:
   - `make automation-check`
   - `make syntax-check`
   - `make test`
   - `make test-security`
   - `make lint`
   - `git diff --check`

5. Write your handoff report to:
   `/Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_worker_m3_1/handoff.md`
   including all modified files, exact build/test outputs, and verification methods.

When complete, send a message to parent with your handoff summary and path.
