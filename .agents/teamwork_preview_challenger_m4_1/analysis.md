# Empirical Stress Testing & Adversarial Analysis Report

**Target Files**:
- `scripts/provision_account_secrets.py`
- `tests/test_provision_account_secrets.py`

**Agent**: `teamwork_preview_challenger_m4_1` (EMPIRICAL CHALLENGER / critic / specialist)  
**Timestamp**: 2026-08-06T05:54:00Z  

---

## Executive Summary

An exhaustive empirical stress-testing suite was constructed and executed against `scripts/provision_account_secrets.py` and `tests/test_provision_account_secrets.py`. The evaluation covered missing dependencies, zero-account configuration, 1-account and 5-account processing, all 6 partial-configuration permutations, adversarial exception token leaks, output safety, and the full project validation commands.

**Verdict**: **REQUEST_CHANGES**

---

## Detailed Empirical Findings

### Finding 1: Redaction Flaw — Un-prefixed Raw Secret Leak in Stderr (HIGH)

- **Observation**:
  `scripts/provision_account_secrets.py` lines 124–126 catch exceptions during account provisioning and print a sanitized message to `sys.stderr`:
  ```python
  except Exception as e:
      sanitized_err = redact_secrets(str(e))
      print(f"Account {i}: FAILED - {sanitized_err}", file=sys.stderr)
  ```
  `redact_secrets()` in `sdk/redaction.py` relies strictly on static regular expressions (e.g. `refreshToken=...`, `password=...`, JWT pattern `eyJ...`, or Base64 strings of length $\ge 40$).

- **Empirical Proof**:
  We configured environment variables with raw credentials:
  ```python
  os.environ["PSS_ACCOUNT_1_EMAIL"] = "user1@example.com"
  os.environ["PSS_ACCOUNT_1_PASSWORD"] = "MySecretPass99"
  os.environ["PSS_ACCOUNT_1_REFRESH_TOKEN"] = "MySecretRefreshToken88"
  ```
  When `provision_account()` raised an exception containing the un-prefixed raw secrets:
  `RuntimeError("Failed to connect using MySecretRefreshToken88 and password MySecretPass99")`

  Executing `pas.main()` resulted in the following verbatim output printed to `sys.stderr`:
  ```text
  Account 1: FAILED - Failed to connect using MySecretRefreshToken88 and password MySecretPass99
  ```

- **Root Cause**:
  1. `redact_secrets()` does not match arbitrary raw strings unless they fit pre-defined static regexes (such as `refreshToken=` or JWT/40+ char base64).
  2. `scripts/provision_account_secrets.py` does not dynamically redact the loaded values of `email`, `password`, and `refresh_token` from environment variables before printing exception text to `stderr`.

- **Existing Test Masking**:
  `tests/test_provision_account_secrets.py` line 193 passed `test_token_safety_stdout_stderr` because it artificially formatted its test exception string using `refreshToken=REFRESH_TOKEN_SECRET_9999` and `password=PASSWORD_SECRET_8888`. Because the synthetic test string used `refreshToken=` and `password=`, `sdk/redaction.py`'s static regex matched, masking the fact that un-prefixed secrets leak.

- **Mitigation Requirement**:
  1. `scripts/provision_account_secrets.py` should dynamically redact known secret strings (the values of `email`, `password`, `refresh_token` for all configured slots) from exception messages in addition to calling `redact_secrets()`.
  2. `tests/test_provision_account_secrets.py` should add a regression test asserting that un-prefixed raw secrets in exception strings are redacted.

---

### Finding 2: Full-Repository `make lint` Failure (MEDIUM)

- **Observation**:
  Running `make lint` (`uv run ruff check run.py sdk tests && uv run ty check run.py sdk tests`) fails with code 2 due to 61 pre-existing ruff lint errors across `sdk/` and `tests/` (`sdk/commands.py`, `sdk/crew_leveling.py`, `sdk/dotnet.py`, `sdk/tui.py`, `tests/test_live_auth.py`, `tests/test_security.py`, `tests/test_tui.py`).

- **Slice Verification**:
  Direct lint and type checks on the provisioning slice files:
  ```bash
  uv run ruff check scripts/provision_account_secrets.py tests/test_provision_account_secrets.py
  uv run ty check scripts/provision_account_secrets.py tests/test_provision_account_secrets.py
  ```
  both returned **All checks passed!** with 0 errors.

- **Impact**:
  While the files modified/created in the provisioning slice (`scripts/provision_account_secrets.py` and `tests/test_provision_account_secrets.py`) are 100% lint and type clean, the aggregate Makefile command `make lint` fails due to legacy files.

---

### Finding 3: Missing Dependency Handling (PASS)

- **Observation**:
  When `ratelimit` is absent (simulated via `patch.dict("sys.modules", {"ratelimit": None})`), importing `scripts.provision_account_secrets` raises `SystemExit(1)` and prints `Dependency error: ...` to `stderr`.
- **Proof**: Verified by `test_missing_ratelimit_dependency` in `tests/test_provision_account_secrets.py` and `stress_test.py`.

---

### Finding 4: Environment Variable Permutations (PASS)

- **Zero Accounts**:
  - `PSS_ACCOUNT_1..5_*` unset or whitespace-only.
  - Returns exit code 0.
  - Performs 0 network requests (`provision_account` and `Client` mock call count = 0).
  - Prints: `"No accounts configured. Safe exit 0."` to stdout.
- **One Account (Configured)**:
  - 1 slot configured with email, password, refresh token.
  - Evaluated independently; returns exit code 0 on success, 1 on failure.
- **Five Accounts (Configured)**:
  - All 5 slots configured.
  - Processed independently; failure in account 1 does not abort accounts 2..5.
  - If any account fails, final exit status is 1.
- **Partial Configurations (6 permutations)**:
  - Permutations tested:
    1. Email only (missing password, refresh_token)
    2. Password only (missing email, refresh_token)
    3. Refresh token only (missing email, password)
    4. Email + Password (missing refresh_token)
    5. Email + Refresh token (missing password)
    6. Password + Refresh token (missing email)
  - Pre-flight validation in `inspect_account_slots()` detects partial state before any network calls or `provision_account()` execution.
  - Prints sanitized slot error to stderr: `Account i: Partial configuration - missing <field>`
  - Outputs `Account i: PARTIAL_CONFIG_FAILED` to stdout summary.
  - Aggregate exit code is 1.

---

### Finding 5: Validation Suite Results Summary

| Validation Command | Status | Notes |
|-------------------|--------|-------|
| `make automation-check` | **PASS** | 37 automation harness tests passed |
| `make syntax-check` | **PASS** | `compileall -q run.py sdk` passed |
| `make test` | **PASS** | 103 unit tests passed (1 skipped) |
| `make test-security` | **PASS** | 41 security tests passed |
| `git diff --check` | **PASS** | Zero trailing whitespace / diff formatting errors |
| `make lint` | **FAIL** | 61 ruff errors in legacy `sdk/` & `tests/` (slice files are 0 errors) |
| `uv run ruff check <slice>` | **PASS** | 0 errors in `provision_account_secrets.py` & `test_provision_account_secrets.py` |
| `uv run ty check <slice>` | **PASS** | 0 type errors in `provision_account_secrets.py` & `test_provision_account_secrets.py` |

---

## Stress Test Matrix

| Scenario ID | Test Description | Expected Result | Empirical Result | Status |
|-------------|------------------|-----------------|------------------|--------|
| ST-01 | Missing `ratelimit` dependency | `SystemExit(1)` + stderr | `SystemExit(1)` + stderr | **PASS** |
| ST-02 | 0 accounts configured | `SystemExit(0)`, 0 network calls | `SystemExit(0)`, 0 network calls | **PASS** |
| ST-03 | 1 account success | `SystemExit(0)`, stdout summary | `SystemExit(0)`, stdout summary | **PASS** |
| ST-04 | 5 accounts success | `SystemExit(0)`, 5 calls | `SystemExit(0)`, 5 calls | **PASS** |
| ST-05 | 5 accounts (account 3 failing) | Accounts 1..5 run, `SystemExit(1)` | Accounts 1..5 run, `SystemExit(1)` | **PASS** |
| ST-06 | Partial config (email, no pass) | Fast fail (0 network calls), `SystemExit(1)` | Fast fail (0 network calls), `SystemExit(1)` | **PASS** |
| ST-07 | Partial config (pass, no email) | Fast fail (0 network calls), `SystemExit(1)` | Fast fail (0 network calls), `SystemExit(1)` | **PASS** |
| ST-08 | Partial config (email+pass, no ref) | Fast fail (0 network calls), `SystemExit(1)` | Fast fail (0 network calls), `SystemExit(1)` | **PASS** |
| ST-09 | Whitespace-only env vars | Treated as unconfigured, `SystemExit(0)` | Treated as unconfigured, `SystemExit(0)` | **PASS** |
| ST-10 | Prefixed exception (`refreshToken=...`) | Redacted by regex | Redacted by regex | **PASS** |
| ST-11 | Un-prefixed raw secrets in Exception | Secrets redacted | **Raw secrets leaked in stderr** | **FAIL** |
| ST-12 | Direct `provision_account()` success | Returns rotated token | Returns rotated token | **PASS** |
| ST-13 | `DeviceLogin17` failure | Raises `RuntimeError` | Raises `RuntimeError` | **PASS** |
| ST-14 | Missing `accessToken` post-login | Raises `RuntimeError` | Raises `RuntimeError` | **PASS** |
| ST-15 | `UserEmailPasswordAuthorize4` fail | Raises `RuntimeError` | Raises `RuntimeError` | **PASS** |
| ST-16 | Missing `refreshToken` post-auth | Raises `RuntimeError` | Raises `RuntimeError` | **PASS** |

---

## Required Remediation for Builder

1. **Dynamic Secret Redaction in `provision_account_secrets.py`**:
   Before printing exception strings in `main()`, replace all occurrences of `s['password']`, `s['refresh_token']`, and `s['email']` with redaction placeholders (`***REDACTED***`, `***REDACTED_EMAIL***`), in addition to calling `redact_secrets()`.

2. **Un-prefixed Secret Leak Test in `test_provision_account_secrets.py`**:
   Add a test case in `test_provision_account_secrets.py` where an exception string contains raw secret values without `refreshToken=` or `password=` prefixes, and assert that `stderr` contains zero raw secret values.

3. **Repository Lint Cleanliness**:
   Ensure `make lint` passes or document repository-wide lint scope boundaries with the supervisor.
