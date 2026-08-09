# R4 Provisioning Workflow & Dependency Contract Survey: Detailed Analysis

**Explorer Agent:** `teamwork_preview_explorer_m1_2`  
**Date:** 2026-08-06  
**Target Repository:** `/Users/raelldottin/Documents/Personal/tachikoma`  
**Scope:** `.github/workflows/provision-pss-secrets.yml`, `scripts/provision_account_secrets.py`, `requirements.txt`, `Makefile`, `sdk/`, `tests/`

---

## 1. Inspection of `.github/workflows/provision-pss-secrets.yml` and `scripts/provision_account_secrets.py`

### 1.1 `.github/workflows/provision-pss-secrets.yml`
- **Purpose:** Workflow designed to run daily or on-demand (`workflow_dispatch`) to provision and rotate PSS account refresh tokens for up to 5 accounts (`PSS_ACCOUNT_1_*` through `PSS_ACCOUNT_5_*`).
- **Dependencies Step (Lines 43-46):**
  ```yaml
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests xmltodict
  ```
  - **Observation:** Hardcodes explicit package installation for `requests` and `xmltodict` only.
  - **Defect:** Does NOT install from `requirements.txt` (`pip install -r requirements.txt`).
- **Execution Step (Lines 48-51):**
  ```yaml
      - name: Provision account secrets
        id: provision
        run: |
          python scripts/provision_account_secrets.py
  ```
- **Post-Execution Output Step (Lines 53-57):**
  ```yaml
      - name: Output new refresh tokens
        run: |
          echo "New refresh tokens generated successfully"
          echo "To update GitHub secrets, use: gh secret set PSS_ACCOUNT_1_REFRESH_TOKEN -b \"<token>\" --repo <owner>/<repo>"
  ```
  - **Observation:** Comments state tokens can be captured by downstream workflows, but step prints instructions.

### 1.2 `scripts/provision_account_secrets.py`
- **Imports (Lines 6-20):**
  ```python
  import os
  import sys
  import subprocess
  import json
  import re

  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

  from sdk.device import Device
  from sdk.client import Client
  from sdk.redaction import redact_secrets
  ```
- **Account Extraction Logic (Lines 49-65):**
  ```python
  def main():
      accounts = []
      for i in range(1, 6):
          email = os.environ.get(f"PSS_ACCOUNT_{i}_EMAIL")
          password = os.environ.get(f"PSS_ACCOUNT_{i}_PASSWORD")
          refresh_token = os.environ.get(f"PSS_ACCOUNT_{i}_REFRESH_TOKEN")
          
          if not all([email, password, refresh_token]):
              print(f"Account {i}: Missing secrets, skipping", file=sys.stderr)
              continue
          
          accounts.append((f"account_{i}", email, password, refresh_token))
      
      if not accounts:
          print("No accounts configured", file=sys.stderr)
          sys.exit(1)
  ```
- **Provisioning Execution Loop (Lines 67-76):**
  ```python
      new_tokens = {}
      for name, email, password, refresh_token in accounts:
          try:
              new_refresh = provision_account(name, email, password, refresh_token)
              new_tokens[name] = new_refresh
              print(f"{name}: OK", file=sys.stderr)
          except Exception as e:
              print(f"{name}: FAILED - {redact_secrets(str(e))}", file=sys.stderr)
              sys.exit(1)
  ```
- **Token Output (Line 80):**
  ```python
      print(json.dumps(new_tokens))
  ```

---

## 2. Dependency Management Survey (`requirements.txt`, `Makefile`, Existing Tests)

### 2.1 Authoritative Dependencies in `requirements.txt`
`requirements.txt` defines 7 dependencies:
```text
certifi==2022.12.7
charset-normalizer==3.0.1
idna==3.4
ratelimit==2.2.1
requests==2.28.2
urllib3==1.26.14
xmltodict==0.13.0
```
Key finding: `ratelimit==2.2.1` is listed in `requirements.txt`.

### 2.2 Indirect Imports via SDK
- `sdk/client.py` line 17 explicitly imports:
  ```python
  from ratelimit import limits, sleep_and_retry
  ```
- `scripts/provision_account_secrets.py` line 19 imports:
  ```python
  from sdk.client import Client
  ```
- Consequently, executing `scripts/provision_account_secrets.py` transitively loads `sdk/client.py`, which requires `ratelimit`.

### 2.3 Dependency Contracts in Workflows & `Makefile`
- `.github/workflows/daily-run.yml` (Line 26): Uses `pip install -r requirements.txt`.
- `.github/workflows/provision-pss-secrets.yml` (Line 46): Uses `pip install requests xmltodict` (violates dependency contract by omitting `ratelimit` and installing directly instead of using `requirements.txt`).
- `Makefile`: Targets `test`, `test-security`, `syntax-check`, `automation-check`, `lint` run Python invocations, assuming requirements are installed.

---

## 3. Missing-Dependency Failure Mode Analysis

### 3.1 Mechanism of Failure
1. `provision-pss-secrets.yml` runs `pip install requests xmltodict`.
2. Python environment has `requests` and `xmltodict` installed, but lacks `ratelimit`.
3. Workflow runs `python scripts/provision_account_secrets.py`.
4. Script executes `from sdk.client import Client`.
5. `sdk/client.py` executes line 17: `from ratelimit import limits, sleep_and_retry`.
6. Python raises `ModuleNotFoundError: No module named 'ratelimit'`.
7. Script fails immediately during module load before any account logic or main() execution occurs.

### 3.2 Required Remediation for R4
- **Regression Test:** Create a red test (e.g. in `tests/test_provision_account_secrets.py`) that simulates the missing dependency state (or tests import/script execution without `ratelimit`) proving it fails with `ModuleNotFoundError`.
- **Workflow Fix:** Update `.github/workflows/provision-pss-secrets.yml` step `Install dependencies` to:
  ```yaml
  - name: Install dependencies
    run: |
      python -m pip install --upgrade pip
      pip install -r requirements.txt
  ```

---

## 4. Analysis of Account Configuration Requirements

The prompt requires supporting 4 distinct account configuration states cleanly:

### 4.1 Zero Configured Accounts (Safe No-Op)
- **Requirement:**
  - Must exit successfully (`exit code 0`).
  - Perform zero network requests.
  - Output a clear, sanitized summary message stating 0 accounts were configured.
  - Must NOT treat missing accounts as a failure/crash.
- **Current Defect:**
  - `scripts/provision_account_secrets.py` lines 63-65 print `"No accounts configured"` to stderr and execute `sys.exit(1)`.
  - Violates zero-account contract by returning exit code 1.

### 4.2 One Fully Configured Account
- **Requirement:**
  - Attempt only the configured account slot.
  - In unit/integration tests, all Pixel Starships traffic MUST be mocked.
  - Report a structured success/failure outcome for that account.
  - Secrets and tokens must NEVER be printed.
- **Current Implementation:**
  - Attempts account 1 if `email`, `password`, `refresh_token` are set.
  - But leaks new token via stdout JSON (`print(json.dumps(new_tokens))`).

### 4.3 Five Fully Configured Accounts
- **Requirement:**
  - Process all 5 accounts independently.
  - A failure on Account $k$ must NOT stop or erase outcomes for remaining accounts $k+1..5$.
  - Collect structured per-account outcomes.
  - Return exit code `nonzero` if ANY account fails; exit code `0` only if ALL 5 accounts succeed.
- **Current Defect:**
  - Lines 74-76 in current script:
    ```python
    except Exception as e:
        print(f"{name}: FAILED - {redact_secrets(str(e))}", file=sys.stderr)
        sys.exit(1)
    ```
  - If Account 1 fails, `sys.exit(1)` immediately aborts execution. Accounts 2, 3, 4, 5 are skipped, hiding their status!

### 4.4 Partially Configured Account (e.g. Email without Password)
- **Requirement:**
  - If any slot $i$ has a partial credential set (e.g., email present but password missing, or password present but refresh token missing):
  - Fail BEFORE any PSS client initialization or network requests.
  - Identify the account slot and missing credential category without printing secret values.
  - Exit `nonzero`.
  - Must NOT skip or continue as though the account were unconfigured.
- **Current Defect:**
  - Lines 57-59 in current script:
    ```python
    if not all([email, password, refresh_token]):
        print(f"Account {i}: Missing secrets, skipping", file=sys.stderr)
        continue
    ```
  - Treats partial credentials as unconfigured, skips the slot, and proceeds to remaining accounts. If another account succeeds, it can result in exit code 0!

---

## 5. Token and Output Safety Analysis

### 5.1 Required Safety Standards
- Refresh tokens, access tokens, passwords, and emails must NEVER be printed in stdout, stderr, logs, exceptions, workflow summaries, or artifacts.
- Returned tokens from mocked rotation must not leak into stdout/stderr.
- Workflow summaries must use safe account labels (e.g. `account_1`) and status flags (`OK`, `FAILED`).
- Script must NOT rely on stdout/stderr logs to transfer newly generated secrets.

### 5.2 Current Violations
1. **stdout secret leakage:** Line 80 of `provision_account_secrets.py`:
   `print(json.dumps(new_tokens))`
   Outputs raw refresh tokens as JSON to standard output.
2. **Insecure secret transfer mechanism:** The workflow relies on stdout log output for capturing secrets rather than a secure mechanism (or explicitly documenting token persistence limitations as a residual risk if outside slice scope).
3. **Log Sanitization:** While `redact_secrets()` is called on stderr exception strings, printing raw tokens to stdout bypasses redaction.

---

## 6. Exit Code Behavior Analysis

| Scenario | Required Exit Code | Current Exit Code | Current Status |
|----------|--------------------|-------------------|----------------|
| 0 accounts configured | `0` (Success/No-op) | `1` | **FAIL** |
| All configured accounts succeed | `0` (Success) | `0` | **PASS** (but leaks tokens) |
| 1+ accounts fail | `nonzero` (e.g. 1) | `1` | **PASS** (but halts early) |
| Partial account configuration | `nonzero` (e.g. 1) | `0` or `1` (skips slot) | **FAIL** |
| Missing dependency (`ratelimit`) | `nonzero` (e.g. 1) | `1` (ModuleNotFoundError) | **PASS** (unhandled crash) |

---

## 7. Summary of Required Code Changes for Implementer

1. **`.github/workflows/provision-pss-secrets.yml`**:
   - Change `pip install requests xmltodict` to `pip install -r requirements.txt`.
   - Remove or sanitize steps that echo secrets.

2. **`scripts/provision_account_secrets.py`**:
   - Update main logic to validate slot configurations:
     - All 5 empty -> print safe no-op summary, exit 0.
     - Slot has 1 or 2 vars set (partial) -> log slot error (e.g. `account_1: Invalid configuration - missing required credential fields`), mark invalid, exit nonzero.
     - Slot has all 3 vars set -> add to active provisioning list.
   - Loop through all active accounts, recording status for every account without early `sys.exit(1)`.
   - Remove `print(json.dumps(new_tokens))` from stdout. Never print token strings.
   - Compute final aggregate exit code: 0 if all active accounts succeeded and zero partial account errors occurred; nonzero otherwise.

3. **`tests/test_provision_account_secrets.py` (New test suite)**:
   - Test zero accounts configuration (assert exit 0, no network calls).
   - Test 1 account success & failure (mocked PSS traffic).
   - Test 5 accounts independence (mock 1 failing, 4 succeeding; verify all 5 run and exit code is nonzero).
   - Test partial account config (assert fails before client init, exit code nonzero).
   - Test missing dependency regression.
   - Test secret-safe stdout/stderr output (assert no tokens/passwords/emails appear in captured stdout/stderr).
