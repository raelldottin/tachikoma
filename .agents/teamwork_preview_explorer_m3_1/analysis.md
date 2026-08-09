# Milestone 3 Implementation Design: R4 Provisioning Workflow Repairs

**Author:** teamwork_preview_explorer_m3_1  
**Date:** 2026-08-06  
**Target Milestone:** Milestone 3 (R4 Provisioning Workflow Repairs)  
**Working Directory:** `/Users/raelldottin/Documents/Personal/tachikoma/.agents/teamwork_preview_explorer_m3_1`  

---

## 1. Executive Summary & Scope

This document details the exact technical specification and design for repairing and proving the `Provision PSS Account Secrets` GitHub Actions workflow (`.github/workflows/provision-pss-secrets.yml`) and its backing script (`scripts/provision_account_secrets.py`), as well as adding regression coverage in `tests/test_provision_account_secrets.py`.

### Scope & Allowed Paths Budget
- **Allowed Paths**: `.github/workflows/provision-pss-secrets.yml`, `scripts/provision_account_secrets.py`, `tests/`, `README.template`, `README.md`.
- **File Budget Limit**: $\le 10$ changed files.
- **Planned File Modifications**:
  1. `.github/workflows/provision-pss-secrets.yml` (Workflow dependency fix)
  2. `scripts/provision_account_secrets.py` (Script logic, output safety, contract implementation)
  3. `tests/test_provision_account_secrets.py` (New regression & unit test suite)
  4. `README.template` (Updated documentation if needed)
  5. `README.md` (Generated from template if updated)
  Total planned changed files: 3 to 5 files (well within budget).

---

## 2. Technical Requirements & Problem Analysis

### 2.1 Dependency Contract Repair
- **Defect Observation**: In `.github/workflows/provision-pss-secrets.yml` (line 46), dependencies are installed via:
  ```yaml
  pip install requests xmltodict
  ```
  However, `scripts/provision_account_secrets.py` imports `Client` from `sdk.client`, which transitively requires `ratelimit` (line 17 of `sdk/client.py`: `from ratelimit import limits, sleep_and_retry`). `ratelimit==2.2.1` is defined in `requirements.txt` (line 4). When executed in CI without `ratelimit`, the workflow fails with `ModuleNotFoundError: No module named 'ratelimit'`.
- **Specification Fix**: Replace incomplete inline package list with Tachikoma's authoritative dependency file `requirements.txt`:
  ```yaml
  pip install -r requirements.txt
  ```
- **Regression Test Specification**: In `tests/test_provision_account_secrets.py`, create `test_missing_ratelimit_dependency()` using `unittest.mock.patch.dict('sys.modules', {'ratelimit': None})` to prove that attempting to import `sdk.client` without `ratelimit` fails with `ModuleNotFoundError` / `ImportError`.

---

### 2.2 Configuration Contract Repairs in `scripts/provision_account_secrets.py`

#### 1. Zero Configured Accounts
- **Specification**: Scan all 5 potential account slots (`PSS_ACCOUNT_1_*` through `PSS_ACCOUNT_5_*`). If no account credentials are provided across all slots:
  - Print to `stdout`: `"No accounts configured. Exit code 0 safe no-op."`
  - Exit with status `0` (`sys.exit(0)`).
  - Perform **zero** network requests and skip `Client` / `Device` initialization.

#### 2. One Configured Account
- **Specification**: When exactly 1 account is configured:
  - Attempt provisioning for only that slot.
  - Print sanitized output status to `stderr` (`Account 1: SUCCESS` or `Account 1: FAILED`).
  - Exit with `0` on success, or `1` on failure.

#### 3. Five Configured Accounts (Independent Loop)
- **Specification**: Loop through all 5 account slots independently:
  - Maintain a results dictionary capturing per-slot status (`True` for success, `False` for failure).
  - **Do NOT** break or call `sys.exit(1)` immediately when account 1 fails. Continue to attempt all configured slots.
  - Return exit code `0` if all attempted accounts succeeded. Return exit code `1` (nonzero) if any account failed.

#### 4. Partially Configured Accounts (Pre-flight Validation)
- **Specification**: Check each slot prior to `Client` initialization or network activity:
  - An account slot is **partial** if 1 or 2 of `EMAIL`, `PASSWORD`, `REFRESH_TOKEN` are present, but not all 3.
  - Fail fast before network/Client creation.
  - Identify slot label and missing fields (e.g. `Account 1: Partial configuration - missing password`).
  - Log sanitized error to `stderr` and record a `FAILED` outcome for that slot.
  - Ensure aggregate script exit status is nonzero (`1`).

---

### 2.3 Token & Output Safety Specification

1. **Remove Stdout Token Dump**:
   - Delete `print(json.dumps(new_tokens))` from `scripts/provision_account_secrets.py`. Secrets must never be dumped to stdout/stderr.
2. **Exception Sanitization**:
   - All caught exception strings must be formatted using `redact_secrets(str(e))` before logging or printing.
3. **Sanitized Summary Formatting**:
   - Stdout/stderr summaries must use safe slot labels (`Account 1`, `Account 2`, ... `Account 5`) and safe status strings (`SUCCESS`, `FAILED`).
4. **No Log Token Transport**:
   - Scripts and workflows must not rely on logs for secret transfer.

---

## 3. Detailed Implementation Design

### 3.1 Workflow Repair (`.github/workflows/provision-pss-secrets.yml`)

Modify lines 43–47:
```yaml
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
```

---

### 3.2 Script Repair (`scripts/provision_account_secrets.py`)

```python
#!/usr/bin/env python3
"""
Provision GitHub secrets for 5 accounts using captured refresh tokens as bootstrap,
then rotate via email/password. Never prints credential values.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.device import Device
from sdk.client import Client
from sdk.redaction import redact_secrets


def provision_account(account_name: str, email: str, password: str, refresh_token: str) -> str:
    """Bootstrap with refresh_token, rotate via email/password, return new refresh_token."""
    device = Device(language="en")
    device.key = "CC3C7642-E6FE-4737-88C1-130395760B52"  # iOS device key
    device.refreshToken = refresh_token
    
    client = Client(device=device, settings={
        "checksum_key": "5343",
        "savy_checksum": "Savvy!s0d@",
    })
    
    # Stage 1: DeviceLogin17 with refresh token → get accessToken
    if not client.create_device_session():
        raise RuntimeError(f"{account_name}: DeviceLogin17 failed")
    
    if not client.accessToken:
        raise RuntimeError(f"{account_name}: No accessToken from DeviceLogin17")
    
    # Stage 2: UserEmailPasswordAuthorize4 with email/password → get NEW refresh token
    if not client.authorize_email_password(email, password):
        raise RuntimeError(f"{account_name}: Email/password authorize failed")
    
    if not device.refreshToken:
        raise RuntimeError(f"{account_name}: No new refreshToken after rotation")
    
    return device.refreshToken


def main():
    configured_accounts = []
    partial_slots = []
    
    for i in range(1, 6):
        email = (os.environ.get(f"PSS_ACCOUNT_{i}_EMAIL") or "").strip()
        password = (os.environ.get(f"PSS_ACCOUNT_{i}_PASSWORD") or "").strip()
        refresh_token = (os.environ.get(f"PSS_ACCOUNT_{i}_REFRESH_TOKEN") or "").strip()
        
        has_any = bool(email or password or refresh_token)
        has_all = bool(email and password and refresh_token)
        
        if not has_any:
            continue
            
        label = f"Account {i}"
        if not has_all:
            missing = []
            if not email:
                missing.append("email")
            if not password:
                missing.append("password")
            if not refresh_token:
                missing.append("refresh_token")
            missing_str = ", ".join(missing)
            partial_slots.append((label, missing_str))
        else:
            configured_accounts.append((label, email, password, refresh_token))
            
    # Zero accounts configured case
    if not configured_accounts and not partial_slots:
        print("No accounts configured. Exit code 0 safe no-op.")
        sys.exit(0)
        
    results = {}
    
    # Process partial slots first (fails fast before network/Client creation)
    for label, missing_str in partial_slots:
        print(f"{label}: Partial configuration - missing {missing_str}", file=sys.stderr)
        print(f"{label}: FAILED", file=sys.stderr)
        results[label] = False
        
    # Process fully configured accounts independently
    for label, email, password, refresh_token in configured_accounts:
        try:
            provision_account(label, email, password, refresh_token)
            print(f"{label}: SUCCESS", file=sys.stderr)
            results[label] = True
        except Exception as e:
            redacted_err = redact_secrets(str(e))
            print(f"{label}: FAILED - {redacted_err}", file=sys.stderr)
            results[label] = False
            
    # Aggregate result check
    if not all(results.values()):
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
```

---

### 3.3 Test Suite Design (`tests/test_provision_account_secrets.py`)

The test suite will contain 10 unit tests in class `TestProvisionAccountSecrets`:

1. `test_missing_ratelimit_dependency`:
   - Simulates `ratelimit` absence via `sys.modules['ratelimit'] = None`.
   - Asserts importing `sdk.client` raises `ImportError` / `ModuleNotFoundError`.
2. `test_zero_configured_accounts`:
   - Clears all `PSS_ACCOUNT_*` env vars.
   - Runs `main()`, asserts `SystemExit` with code `0`.
   - Asserts `stdout` contains `"No accounts configured. Exit code 0 safe no-op."`.
   - Asserts zero network activity.
3. `test_one_account_success`:
   - Mocks `provision_account` returning a new refresh token.
   - Asserts `main()` exits with `0`, `stderr` contains `Account 1: SUCCESS`.
4. `test_one_account_failure`:
   - Mocks `provision_account` raising `RuntimeError("Account 1: DeviceLogin17 failed")`.
   - Asserts `main()` exits with `1`, `stderr` contains `Account 1: FAILED`.
5. `test_five_accounts_all_success`:
   - Sets env vars for accounts 1–5.
   - Mocks `provision_account` success for all 5.
   - Asserts `main()` exits with `0`, all 5 slots reported `SUCCESS`.
6. `test_five_accounts_independent_processing`:
   - Sets env vars for accounts 1–5.
   - Mocks account 1 failure and accounts 2–5 success.
   - Asserts all 5 accounts were attempted (independent loop).
   - Asserts overall exit code is `1` (nonzero aggregate failure).
7. `test_partial_account_configuration`:
   - Sets `PSS_ACCOUNT_1_EMAIL` without password.
   - Asserts `main()` logs `Account 1: Partial configuration - missing password`.
   - Asserts `provision_account` is NOT called for slot 1.
   - Asserts aggregate exit code is `1`.
8. `test_token_rotation_success_no_token_leak`:
   - Tests `provision_account()` with mocked `Client.create_device_session()` and `Client.authorize_email_password()`.
   - Asserts `stdout` and `stderr` contain no raw tokens, JWTs, or passwords.
9. `test_token_rotation_failure_sanitized_error`:
   - Tests stage 1 and stage 2 failures in `provision_account()`.
   - Asserts expected `RuntimeError` is raised with safe sanitized text.
10. `test_redact_secrets_in_provisioning_exceptions`:
    - Verifies that exceptions containing sensitive credentials (e.g. `refreshToken=xyz789&email=test@example.com`) are redacted when formatted in `main()`.

---

## 4. Quality Bar Compliance Matrix

| Criterion | Quality Bar Requirement | Implementation Compliance Strategy |
|---|---|---|
| 1 | Unit, security, and harness tests pass | All new tests in `test_provision_account_secrets.py` pass; `make test` & `make test-security` pass. |
| 2 | Mocked Pixel Starships traffic | All network calls in unit tests use `unittest.mock.patch` on `Client` / `requests.Session`. |
| 3 | Secrets/Tokens safety | stdout secret dump removed; safe slot labels used; exception messages passed through `redact_secrets()`. |
| 4 | Explicit outcome per account | Each configured slot logs `Account N: SUCCESS` or `Account N: FAILED`. |
| 5 | Truthful workflow failure | Aggregate exit code is `1` if any slot (partial or full) fails provisioning. |
| 6 | Bounded transient failure handling | Each account failure is captured independently without unhandled crashes. |
| 7 | State verification (N/A) | Provisioning pilot does not perform in-game ship state mutations. |
| 8 | Idempotency tested | Repeated executions of `main()` with same mocked responses produce identical outcomes. |
| 9 | Gameplay unchanged | Gameplay strategy code untouched; provisioning script only manages token rotation. |
| 10 | Documentation hierarchy | `README.template` updated before `README.md` if documentation changes are made. |
| 11 | Scope & file budget | Allowed paths respected; total changed files $\le 5$ (budget limit $\le 10$). |
| 12 | Independent critic pass | Code structure satisfies strict schema and critic review standards. |

---

## 5. Verification Commands

The following commands must pass upon implementation:

```bash
make automation-check
make syntax-check
make test
make test-security
make lint
git diff --check
```
