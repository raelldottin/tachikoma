# Handoff Report — explorer_m1_3

**Slice**: Gauntlet Slice 2 (`runtime-response-shape-guards`)  
**Scope**: R6 (SMTP Pre-Validation), R7 (Runtime Exit Semantics), R8 (Deterministic Test Coverage), and interaction with R3/R4/R5 in `run.py` & `tests/`

---

## 1. Observation

### 1.1 Baseline Repository & Validation State
- **Branch**: `main`
- **Validation Suite**: Executed `make automation-check`, `make syntax-check`, `make test`, `make test-security`, `make lint`, `git diff --check`.
  - `make automation-check`: PASSED (0 errors)
  - `make syntax-check`: PASSED (0 errors)
  - `make test`: PASSED (34 tests run, 0 failures)
  - `make test-security`: PASSED (11 tests run, 0 failures)
  - `make lint`: PASSED (`ruff` check passed; `ty` check passed with exit-zero flag)
  - `git diff --check`: PASSED (0 formatting errors)

### 1.2 Inspection of `run.py` (lines 1 to 192)
- **Argument Parsing** (lines 84-124): Defines `--smtp-email`, `--smtp-password-file`, `-r`/`--recipient`.
- **Device & Client Instantiation** (lines 131-149): `Device` and `Client` objects are initialized *before* any SMTP validation is performed.
- **Authentication** (lines 151-160): Calls `client.login()`. If login returns `False`, logs warning and exits with code `1`.
- **Gameplay Step Execution** (lines 161-177):
  ```python
  while client:
      client.grabFlyingStarbux()
      if client.freeStarbuxToday >= client.freeStarbuxMax:
          client.collectTaskReward()
          client.getCrewInfo()
          client.upgradeResearches()
          client.upgradeRooms()
          client.collectDailyReward()
          client.listActiveMarketplaceMessages()
          client.getMessages()
          client.infoBux()
          client.manageTraining()
          client.getResourceTotals()
          client.upgradeCharacters()
          logging.info(f'[{client.info["@Name"]}] Finished...')
          break
  ```
  *Observation*: Gameplay steps (`upgradeResearches()`, `upgradeRooms()`, `manageTraining()`) are called sequentially without capturing their `bool` return values. A failure in `upgradeRooms()` does not affect subsequent calls, but the process currently exits implicitly with code `0` (or end-of-script) rather than aggregating runtime failure states.
- **SMTP Log Delivery Post-Processing** (lines 178-189):
  ```python
  smtp_email = args.smtp_email
  smtp_password = None
  if args.smtp_password_file:
      smtp_password = Path(args.smtp_password_file).read_text().strip()
  recipient = args.recipient

  if smtp_email and smtp_password and recipient:
      email_logfile(logfilepath, client, smtp_email, smtp_password, recipient)
  else:
      email_logfile(logfilepath, client)
  ```
  *Observation*: If SMTP arguments are absent or partial, `run.py` falls back to `email_logfile(logfilepath, client)`, which attempts to read `./config.secrets`. It does not perform pre-gameplay validation, does not log `Email log delivery is disabled.` when absent, and does not exit `2` when partial.

### 1.3 Inspection of `sdk/client.py` Key Sections
- **`upgradeRooms()` Exception Logging** (line 1832):
  ```python
  except:
      logging.exception("Unable to upgrade research.", exc_info=True)
      return False
  ```
  *Observation*: Exception log message erroneously says `"Unable to upgrade research."` instead of `"Unable to upgrade rooms."`.
- **`listUpgradingRooms()` Room Design Access** (line 1846):
  ```python
  for roomDesignData in roomDesigns["RoomDesign"]:
  ```
  *Observation*: Assumes `roomDesigns["RoomDesign"]` is always present and iterable as a list.
- **Research Lab Upgrade Skip Classification** (lines 1862-1868): `addResearch()` currently returns `False` when `errorMessage` is present, without distinguishing expected game-state rejection `"Please upgrade your lab room."`.
- **Training Design Parsing** (lines 838-842): `manageTraining()` logs `TrainingDesign data not available.` and returns `False` if `"TrainingDesign"` key is missing from response dictionary.

### 1.4 Inspection of Existing Tests (`tests/`)
- Current test modules: `test_crew_leveling.py`, `test_live_auth.py`, `test_provision_account_secrets.py`, `test_security.py`, `test_tui.py`, `synthetic_fixtures.py`.
- No existing tests cover CLI SMTP pre-validation, CLI exit code aggregation, or response shape variations (single dict vs list vs missing) for room designs, research, and training.

---

## 2. Logic Chain

### 2.1 SMTP Pre-Validation Logic (R6)
1. **Observation**: Command-line arguments `args.smtp_email`, `args.smtp_password_file`, and `args.recipient` are evaluated after `parser.parse_args()`.
2. **Step 1**: Count how many of the 3 SMTP arguments are provided (`smtp_count`).
3. **Step 2**: If `smtp_count == 0`:
   - Log: `Email log delivery is disabled.`
   - Set `smtp_enabled = False`.
   - Do NOT create password handle or attempt email delivery.
   - Continue to gameplay.
4. **Step 3**: If `smtp_count == 3`:
   - Inspect `Path(args.smtp_password_file)`.
   - If the file exists, is readable, and `content.strip()` is non-empty:
     - Read password into `smtp_password` variable (never logged or printed).
     - Set `smtp_enabled = True`.
     - Continue to gameplay.
   - If file is missing, unreadable, or empty after stripping:
     - Log: `Incomplete SMTP configuration; email delivery was not attempted.`
     - Do NOT create `Device` or `Client`, do NOT authenticate or initiate network activity.
     - Call `sys.exit(2)`.
5. **Step 4**: If `smtp_count` is 1 or 2 (partial configuration):
   - Log: `Incomplete SMTP configuration; email delivery was not attempted.`
   - Do NOT create `Device` or `Client`, do NOT authenticate or initiate network activity.
   - Call `sys.exit(2)`.

### 2.2 Truthful Runtime Exit Semantics & Aggregation (R7)
1. **Observation**: `upgradeResearches()`, `upgradeRooms()`, and `manageTraining()` return `True` on success/expected skips, and `False` on unexpected endpoint/schema/runtime failures.
2. **Step 1**: Maintain a local boolean `runtime_failed = False` in `run.py`.
3. **Step 2**: Execute gameplay methods sequentially without short-circuiting:
   ```python
   if not client.upgradeResearches():
       runtime_failed = True
   if not client.upgradeRooms():
       runtime_failed = True
   ...
   if not client.manageTraining():
       runtime_failed = True
   ```
4. **Step 3**: Because statements are executed sequentially without `and` short-circuiting, a failure in `upgradeResearches()` or `upgradeRooms()` does NOT prevent subsequent independent gameplay actions (such as `manageTraining()`) from executing.
5. **Step 4**: After gameplay completes and optional SMTP email is sent (if `smtp_enabled` is True), terminate `run.py`:
   - If `runtime_failed` is True: call `sys.exit(1)`.
   - If `runtime_failed` is False: call `sys.exit(0)`.
6. **Step 5**: Authentication failures (`not client.login()`) continue to immediately call `sys.exit(1)`. SMTP pre-validation failures call `sys.exit(2)`.

### 2.3 Deterministic Test Coverage Strategy (R8)
1. **Observation**: Tests must run deterministically with synthetic fixtures and zero live network traffic.
2. **Step 1**: Create a dedicated test module `tests/test_runtime_guards.py`.
3. **Step 2**: Use `unittest.mock.patch` on `sys.argv`, `sys.stdout`, `sys.stderr`, `Device`, `Client`, and `smtplib.SMTP`.
4. **Step 3**: Construct test suites covering:
   - **SMTP Validation**: Test all 0-argument, 1-argument, 2-argument, 3-argument (valid file, missing file, empty file) combinations. Assert exact log messages, exit codes (`0`, `1`, `2`), and ensure `Device`/`Client` constructors are NOT called on exit `2`.
   - **Exit Aggregation**: Test exit `0` when actions succeed or return expected skips; test exit `1` when `upgradeRooms()`, `upgradeResearches()`, or `manageTraining()` returns `False`; assert later independent actions still run before exit `1`.
   - **Response Shape Guards**: Test missing key, empty collection, single dictionary, list of dictionaries, service error responses, and invalid XML schemas for room designs, research, and training.
   - **Research Rejection Classification**: Assert `"Please upgrade your lab room."` logs `Skipped research design <design_id>: lab upgrade required.`, does not log error, and returns `True` (exit code `0`).

---

## 3. Caveats

1. **Read-Only Explorer Scope**: This investigation is strictly read-only. No edits were made to project source files (`run.py`, `sdk/client.py`, `tests/`). Proposed code changes are provided as exact diff specifications for the implementer agent.
2. **Lint Diagnostics in Baseline**: Pre-existing `ty check` diagnostics exist in `sdk/commands.py` and `tests/test_crew_leveling.py`, but `Makefile` runs `ty check --exit-zero`, so `make lint` exits 0.
3. **Mocking Integrity**: All test specs use synthetic UUIDs, synthetic timestamps, and mocked network handlers, strictly complying with the quality bar (no real credentials or network traffic).

---

## 4. Conclusion & Proposed Implementation Plan

### 4.1 Proposed Changes to `run.py`

```python
# Proposed diff for run.py:

def main():
    parser = argparse.ArgumentParser(...)
    ...
    args = parser.parse_args()

    # R6: Validate SMTP Configuration before Device/Client creation or network activity
    smtp_email = args.smtp_email
    smtp_password_file = args.smtp_password_file
    recipient = args.recipient

    smtp_args = [smtp_email, smtp_password_file, recipient]
    smtp_count = sum(1 for a in smtp_args if a is not None)

    smtp_password = None
    smtp_enabled = False

    if smtp_count == 0:
        logging.info("Email log delivery is disabled.")
        smtp_enabled = False
    elif smtp_count == 3:
        pw_path = Path(smtp_password_file)
        if pw_path.is_file():
            try:
                pw_content = pw_path.read_text().strip()
                if pw_content:
                    smtp_password = pw_content
                    smtp_enabled = True
                else:
                    logging.error("Incomplete SMTP configuration; email delivery was not attempted.")
                    sys.exit(2)
            except Exception:
                logging.error("Incomplete SMTP configuration; email delivery was not attempted.")
                sys.exit(2)
        else:
            logging.error("Incomplete SMTP configuration; email delivery was not attempted.")
            sys.exit(2)
    else:
        logging.error("Incomplete SMTP configuration; email delivery was not attempted.")
        sys.exit(2)

    # Initialize Device and Client only after SMTP pre-validation passes
    auth_string = None
    if args.auth_file:
        auth_string = read_auth_file(args.auth_file)

    if auth_string:
        device = Device(language="en", authentication_string=auth_string)
    else:
        device = Device(language="en")
        if args.device_key:
            device.set_device_key(args.device_key.upper())
        elif args.login_email:
            try:
                os.unlink(device.DB)
            except FileNotFoundError:
                pass

    settings = {}
    if args.login_email:
        settings["allow_email_password_login"] = True

    client = Client(device=device, settings=settings)

    if args.login_email:
        password = getpass.getpass("Game password: ")
        if not client.login(email=args.login_email, password=password):
            logging.warning("[authenticate] failed to login")
            sys.exit(1)
    else:
        if not client.login():
            logging.warning("[authenticate] failed to login")
            sys.exit(1)

    # R7: Track runtime failure states across independent gameplay steps
    runtime_failed = False

    while client:
        client.grabFlyingStarbux()
        if client.freeStarbuxToday >= client.freeStarbuxMax:
            client.collectTaskReward()
            client.getCrewInfo()
            
            if not client.upgradeResearches():
                runtime_failed = True
            if not client.upgradeRooms():
                runtime_failed = True
                
            client.collectDailyReward()
            client.listActiveMarketplaceMessages()
            client.getMessages()
            client.infoBux()
            
            if not client.manageTraining():
                runtime_failed = True
                
            client.getResourceTotals()
            client.upgradeCharacters()
            logging.info(f'[{client.info["@Name"]}] Finished...')
            break

    # Send log file via SMTP only if SMTP is enabled
    if smtp_enabled:
        email_logfile(logfilepath, client, smtp_email, smtp_password, recipient)

    # Truthful exit aggregation
    if runtime_failed:
        sys.exit(1)
    else:
        sys.exit(0)
```

### 4.2 Proposed Test Suite: `tests/test_runtime_guards.py`

Create `tests/test_runtime_guards.py` containing:
1. `TestSMTPPreValidation`:
   - `test_smtp_disabled_when_no_flags`: Verify log `"Email log delivery is disabled."`, no `email_logfile` call, exit code 0.
   - `test_smtp_partial_one_flag_exits_2`: Test each single flag (`--smtp-email`, `--smtp-password-file`, `-r`), verify log `"Incomplete SMTP configuration; email delivery was not attempted."`, `sys.exit(2)`, and assert `Device`/`Client` never called.
   - `test_smtp_partial_two_flags_exits_2`: Test each two-flag pair, verify exit `2` before `Device`/`Client`.
   - `test_smtp_missing_password_file_exits_2`: All 3 flags supplied, password file path nonexistent -> exit `2`.
   - `test_smtp_empty_password_file_exits_2`: All 3 flags supplied, password file empty -> exit `2`.
   - `test_smtp_complete_valid_configuration`: All 3 flags supplied, valid file -> runs gameplay, calls `email_logfile()` with credentials after gameplay, exits `0`.
2. `TestExitCodeAggregation`:
   - `test_exit_0_on_success_and_expected_skips`: Mock `upgradeResearches()=True`, `upgradeRooms()=True`, `manageTraining()=True` -> exit code 0.
   - `test_exit_1_on_unexpected_room_upgrade_failure`: Mock `upgradeRooms()=False` -> exit code 1; assert `manageTraining()` still runs.
   - `test_exit_1_on_unexpected_training_failure`: Mock `manageTraining()=False` -> exit code 1.
   - `test_independent_actions_continue_after_failure`: Mock `upgradeResearches()=False` -> verify `upgradeRooms()` and `manageTraining()` are both invoked, and final exit code is 1.
3. `TestRoomDesignShapeGuards`:
   - Test dictionary shape, list shape, empty shape, missing shape, service error shape for room designs.
   - Verify `"Unable to upgrade rooms."` is logged on unexpected exception inside `upgradeRooms()`.
4. `TestResearchOutcomeClassification`:
   - Test expected lab rejection response (`"Please upgrade your lab room."`): verify log `Skipped research design <design_id>: lab upgrade required.`, return value `True`, and exit code `0`.
   - Test unexpected endpoint error: verify error log, return value `False`, and exit code `1`.
5. `TestTrainingShapeGuards`:
   - Test missing, empty, dictionary, list, and error response shapes for training designs.

---

## 5. Verification Method

1. Run standard validation suite:
   ```bash
   make automation-check
   make syntax-check
   make test
   make test-security
   make lint
   git diff --check
   ```
2. Run focused runtime guard unit tests:
   ```bash
   python3 -m unittest tests/test_runtime_guards.py
   ```
3. Invalidation conditions:
   - Any test failure in `tests/test_runtime_guards.py` or existing tests.
   - `run.py` creating `Device` or `Client` when partial SMTP configuration is passed.
   - Exiting `1` when research is skipped due to lab upgrade requirement.
   - Short-circuiting gameplay execution so `manageTraining()` is skipped if `upgradeRooms()` fails.
