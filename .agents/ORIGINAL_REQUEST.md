# Original User Request

## 2026-08-06T05:39:46Z

# Teamwork Project Prompt — Tachikoma Gauntlet Pilot

Make Tachikoma’s scheduled account automation reliable, secure, truthful, and diagnosable by applying a supervised Gauntlet Loop with independent builder and critic runs.

**Working directory:** `/Users/raelldottin/Documents/Personal/tachikoma`  
**Integrity mode:** Development  
**Initial scope:** `Provision PSS Account Secrets`

## Objective

Establish a repeatable Gauntlet Loop inside Tachikoma, then use its first pilot slice to repair and prove the `Provision PSS Account Secrets` GitHub Actions workflow.

The loop must evaluate actual repository output: diffs, tests, lint results, workflow simulations, sanitized logs and machine-readable review artifacts. Do not accept the builder’s explanation as evidence.

## Hard Requirements

Treat the following as binding:

- `AGENTS.md`
- The existing automation supervisor and queue ownership rules
- Existing gameplay and resource-spending behavior
- `README.template` ownership of generated README content
- Slice `allowed_paths`
- Slice file-change budgets
- Required validation commands
- The prohibition against real credentials and live Pixel Starships traffic during automated validation

A slice agent must not launch another agent, edit supervisor-owned queue state or silently continue into adjacent work. The existing supervisor owns builder selection, critic selection, retries, queue transitions and stopping decisions.

Do not perform broad architectural cleanup, unrelated refactoring or gameplay changes.

---

## R1. Establish the Baseline

Begin by inspecting the repository state without resetting, discarding or overwriting existing work.

1. Record:
   - Current branch
   - Commit SHA
   - Upstream status
   - `git status --short`
   - Existing queued, blocked and completed slices
   - Existing known workflow failures
   - Current enabled and disabled gameplay operations

2. If the repository contains unexpected dirt outside the approved scope, stop and report it rather than modifying or deleting it.

3. Run and record the exact results of:

```bash
make automation-check
make syntax-check
make test
make test-security
make lint
git diff --check
```

4. Create:

```text
automation/gauntlet/workbench.md
```

The workbench must record:

- Baseline commit SHA
- Branch and upstream status
- Initial repository dirt
- Validation commands and outcomes
- Test counts when available
- Known failures
- Active gauntlet slice
- Builder and critic round numbers
- Files changed
- Highest proof level reached
- Critic verdict
- Largest remaining gap
- Residual risks
- Final stopping reason

Do not describe a failing baseline as passing.

---

## R2. Create the Reliability Quality Bar

Create:

```text
automation/gauntlet/quality-bar.md
```

A change passes only when all applicable criteria are satisfied:

1. Unit, security and automation-harness tests pass.
2. All Pixel Starships traffic used in automated tests is mocked.
3. Credentials, passwords, refresh tokens, access tokens, device keys and account identifiers do not appear in source, fixtures, logs, exceptions, workflow summaries or artifacts.
4. Every configured account receives an explicit structured outcome.
5. GitHub Actions fails truthfully when required provisioning fails.
6. Expected transient failures have bounded handling and an explicit terminal state.
7. Mutating operations verify their resulting state when the slice and available fixtures permit it.
8. Idempotency is tested where repeated execution is expected to be safe.
9. Existing gameplay and resource-spending behavior remains unchanged unless a slice explicitly authorizes a change.
10. Documentation changes update `README.template` before generated `README.md`.
11. Changes stay inside the slice’s allowed paths and file budget.
12. No unresolved critical or high-severity defect remains in the independent critic review.

The quality bar must distinguish mandatory criteria from criteria that are not applicable to this provisioning-only pilot.

---

## R3. Define the Pilot Slice

Create or update the supervisor-owned queue through the proper supervisor or intake process. Do not let the builder edit queue state.

Use a narrow slice equivalent to:

```text
gauntlet-provisioning-dependency-and-secret-contract
```

The slice may modify only the minimum necessary paths, expected to include:

```text
.github/workflows/provision-pss-secrets.yml
scripts/provision_account_secrets.py
tests/
automation/gauntlet/
automation/prompts/
automation/schemas/
automation/tests/
requirements.txt
Makefile
docs/workflows/
README.template
README.md
```

Narrow this list further when files are not required. Set an explicit `max_files_changed` value before implementation.

---

## R4. Repair the Provisioning Workflow

Repair and prove the `Provision PSS Account Secrets` workflow.

### Dependency Contract

- Add a regression test that demonstrates the existing missing-dependency failure.
- The initial red test must fail for the intended reason.
- Install dependencies through Tachikoma’s authoritative dependency definition, normally `requirements.txt`, rather than maintaining an incomplete package list inside the workflow.
- Do not add a second conflicting dependency source merely to make CI pass.

### Configuration Contract

Support and test these configurations:

#### Zero configured accounts

- Exit successfully as a safe no-op.
- Perform no network requests.
- Produce a clear sanitized summary stating that no accounts were configured.
- Do not treat absent configuration as an unexpected crash.

#### One fully configured account

- Attempt only that account.
- Use mocked Pixel Starships traffic in tests.
- Report a structured success or failure outcome.
- Never print secrets or returned tokens.

#### Five fully configured accounts

- Process all five independently.
- One account’s failure must not hide or erase the outcomes of the others.
- The final workflow result must be nonzero when any required account provisioning fails.

#### Partially configured account

Examples include an email without a password, a password without an email, or an otherwise incomplete required credential set.

- Fail before Pixel Starships client initialization or network activity.
- Identify the account slot and missing field category without printing secret values.
- Produce a nonzero exit.
- Do not continue as though the account were absent.

### Token and Output Safety

Prove through tests that:

- Refresh tokens are never printed.
- Access tokens are never printed.
- Passwords and email addresses are not exposed in exceptions, summaries or artifacts.
- Mocked successful token rotation does not leak returned token values.
- Mocked failed token rotation produces a useful sanitized error.
- Workflow summaries contain only safe account labels and statuses.
- The script does not rely on logs as a mechanism for transferring newly generated secrets.

Do not implement insecure token persistence. If secure automatic secret updating is outside the slice’s permitted capabilities, document it as a residual risk and stop at a safe contract.

### Exit Semantics

The command and workflow must have deterministic exit behavior:

- `0`: no accounts configured, or every configured account succeeded.
- Nonzero: invalid partial configuration, dependency/bootstrap failure, or one or more required account operations failed.

Do not use `continue-on-error` in a way that leaves a failed provisioning run green.

---

## R5. Build the Independent Critic Loop

The supervisor must launch a fresh critic after each builder attempt.

The critic must not receive:

- The builder’s hidden reasoning
- The builder’s self-review
- Rationalizations for implementation choices
- Unsupported claims that the work is complete

The critic may receive only:

- The slice goal and constraints
- `AGENTS.md`
- The quality bar
- Relevant repository context
- Before-and-after diff
- Changed files
- Exact validation output
- Test and lint output
- Sanitized workflow simulations or logs
- Generated evidence artifacts

The critic must inspect the actual implementation and return strict JSON matching a tracked schema.

Minimum result shape:

```json
{
  "verdict": "pass",
  "largest_remaining_gap": "",
  "severity": "none",
  "evidence": [],
  "quality_bar_failures": [],
  "required_next_action": ""
}
```

For a failing review:

```json
{
  "verdict": "fail",
  "largest_remaining_gap": "One precise, highest-impact unresolved defect.",
  "severity": "critical",
  "evidence": [
    "Concrete evidence from the diff, tests or workflow output."
  ],
  "quality_bar_failures": [
    5
  ],
  "required_next_action": "The smallest action needed to close that gap."
}
```

Allowed verdicts:

```text
pass
fail
```

Allowed severities:

```text
none
low
medium
high
critical
```

A `pass` result requires:

- Empty `largest_remaining_gap`
- `severity: "none"`
- No failed applicable quality-bar criteria
- All required validations passing

A `fail` result must identify one largest meaningful gap. Lower-priority observations may be recorded separately, but they must not replace the primary gap.

The supervisor sends that gap into a fresh builder run and repeats the cycle.

---

## R6. Required Validation

Before the critic can pass the slice, run:

```bash
make automation-check
make syntax-check
make test
make test-security
make lint
git diff --check
```

Also run deterministic provisioning-specific tests proving:

- Zero configured accounts
- One configured account
- Five configured accounts
- Partially configured account
- Mocked successful refresh
- Mocked failed refresh
- Missing dependency or bootstrap failure
- No network activity during configuration rejection
- Secret-safe stdout
- Secret-safe stderr and exceptions
- Correct aggregate exit status

Never run the scheduled workflow against real accounts as validation.

---

## R7. Evidence and Completion

The workbench must record every builder and critic round.

The pilot is complete only when:

- The baseline is accurately recorded.
- The 12-point quality bar exists.
- The provisioning slice stays within approved paths and its file budget.
- All required validations pass.
- Provisioning-specific tests pass.
- No real authentication material was used or exposed.
- The workflow exits truthfully.
- A fresh critic returns `verdict: "pass"`.
- The repository has no new unexplained dirt.
- Remaining limitations are explicitly recorded.

Stop immediately when:

- Required work exceeds allowed paths or the file budget.
- Existing repository dirt makes the operation unsafe.
- A required validation cannot be run honestly.
- Live credentials or live Pixel Starships traffic would be required.
- Secure token persistence would require permissions or decisions outside the approved slice.
- The critic identifies a blocker that cannot be resolved within scope.
- The user stops the run.

Do not start the daily account-processing workflow gauntlet until this provisioning pilot has passed.

## 2026-08-06T09:53:44Z

# Teamwork Project Prompt — Tachikoma Gauntlet Slice 2: Runtime Response-Shape Guards

> Status: Approved for launch  
> Goal: Harden runtime parsing and outcome reporting through the existing supervised Gauntlet Loop

Implement robust runtime response-shape guards in Tachikoma so missing data, schema variations, expected game-state rejections, and SMTP configuration states are handled safely and truthfully.

**Working directory:** `/Users/raelldottin/Documents/Personal/tachikoma`  
**Integrity mode:** Development  
**Slice:** `runtime-response-shape-guards`

## Objective

Create the second Tachikoma Gauntlet slice and use it to harden runtime response handling.

The finished implementation must:

- Normalize API collections represented as either one dictionary or a list.
- Handle absent collections without tracebacks.
- Distinguish expected game-state skips from unexpected application failures.
- Reject incomplete SMTP configuration before authentication, client creation, or network activity.
- Continue independent gameplay actions after one runtime action fails, while returning a truthful final exit status.
- Preserve the existing gameplay strategy and resource-spending decisions.

The Gauntlet Loop must judge actual repository output: diffs, deterministic tests, lint results, sanitized logs, validation output, and machine-readable critic artifacts. Builder explanations are not evidence.

---

## Hard Requirements

Treat the following as binding:

- `AGENTS.md`
- The existing automation supervisor and queue ownership rules
- `automation/gauntlet/quality-bar.md`
- Existing gameplay and resource-spending behavior
- `README.template` ownership of generated README content
- Slice `allowed_paths`
- Slice file-change budget
- Required validation commands
- No real credentials in source, fixtures, logs, exceptions, summaries, or artifacts
- No live Pixel Starships traffic during automated validation

Slice agents must not:

- Launch other agents
- Edit supervisor-owned queue state directly
- Continue into another slice
- Reset, discard, overwrite, or conceal pre-existing user work
- Introduce a general response-parsing framework merely to clean up the architecture
- Change research, room-upgrade, training, or spending priorities

The supervisor owns builder selection, critic selection, retries, queue transitions, and stopping decisions.

---

## R1. Establish the Slice Baseline

Before changing files:

1. Record:
   - Current branch
   - Commit SHA
   - Upstream divergence
   - `git status --short`
   - Pre-existing modified and untracked files
   - Current test results
   - Current selected slice

2. Update:

```text
automation/gauntlet/workbench.md
```

Record the new slice separately from the completed provisioning pilot. Do not overwrite or rewrite the first pilot’s evidence.

3. Run:

```bash
make automation-check
make syntax-check
make test
make test-security
make lint
git diff --check
```

4. If unexpected repository dirt overlaps the slice’s allowed paths and cannot safely be preserved, stop and report the conflict.

---

## R2. Slice Scope and Change Budget

Use the supervisor’s normal queue/intake process to create a slice equivalent to:

```text
runtime-response-shape-guards
```

Expected allowed paths:

```text
sdk/client.py
run.py
tests/
automation/gauntlet/workbench.md
automation/handoffs/
automation/schemas/
```

Documentation files may be added only when user-facing behavior changes require them. When README content changes, update `README.template` before regenerating `README.md`.

Do not modify:

```text
.github/workflows/provision-pss-secrets.yml
.github/workflows/daily-run.yml
authentication protocol implementation
checksum implementation
gameplay strategy
resource-spending thresholds
```

Set:

```text
max_files_changed: 10
```

The builder must use fewer files when possible.

---

## R3. Harden `upgradeRooms()`

The current room-upgrade path assumes that `RoomDesign` always exists and is always a list.

Implement the following behavior:

### Missing or failed room-design response

When room-design data is absent, empty, malformed, or returned as an endpoint error:

- Do not index `roomDesigns["RoomDesign"]`.
- Do not call code that requires room-design entries.
- Do not produce a traceback.
- Log exactly:

```text
Room design data unavailable; skipping room upgrades.
```

- Return `False` for an unexpected endpoint or schema failure.

### Supported collection shapes

Support:

- `RoomDesign` as one dictionary
- `RoomDesign` as a list of dictionaries
- Empty or missing `RoomDesign`
- A service-level error response
- A structurally invalid response

Normalize a single dictionary into a one-element iterable locally. A small private helper is acceptable when it is also used for `TrainingDesign`, but do not create a broad parser subsystem.

### Related room functions

Any related function, including `listUpgradingRooms()`, must not independently assume:

```python
roomDesigns["RoomDesign"]
```

The normalized or validated room-design collection must be used consistently.

### Exception message

Change the incorrect exception message inside `upgradeRooms()` from:

```text
Unable to upgrade research.
```

to:

```text
Unable to upgrade rooms.
```

Unexpected exceptions must be sanitized.

---

## R4. Classify Research Outcomes Correctly

The response:

```text
Please upgrade your lab room.
```

is an expected game-state rejection. It does not indicate broken authentication or an application failure.

### Expected rejection

When `AddResearch` returns that message:

- Log:

```text
Skipped research design <design_id>: lab upgrade required.
```

- Do not log it as an error.
- Do not produce a traceback.
- Return a result that allows `upgradeResearches()` to continue considering another eligible design.
- Do not count this expected skip as a final process failure.

### Unexpected rejection

Other endpoint errors, malformed responses, missing response objects, transport failures, or unknown schemas must:

- Remain visible as sanitized application failures.
- Return `False`.
- Contribute to the final runtime failure status.
- Never be relabeled as an expected skip merely to make tests pass.

### Pre-filtering

Filter unsupported research before the mutation request only when existing authoritative response data clearly exposes the required lab capability.

Do not infer support from names, hard-code design IDs, invent new thresholds, or alter the existing research-selection strategy.

---

## R5. Harden Training Data Parsing

The training path must safely support:

- Missing `TrainingDesign`
- Empty `TrainingDesign`
- One `TrainingDesign` represented as a dictionary
- Multiple `TrainingDesign` entries represented as a list
- A service-level endpoint error
- An invalid or unexpected schema

Required behavior:

### Valid no-data condition

When the service returns a valid response with no available training designs:

- Log a clear sanitized skip message.
- Do not crash.
- Do not prevent later independent gameplay actions.
- Treat the condition as a successful no-op.

### Endpoint or schema failure

When the service itself fails or the response is malformed:

- Log a sanitized application error.
- Return `False`.
- Do not produce a traceback for a known shape variation.
- Continue later independent gameplay actions.
- Contribute to the final runtime failure status.

Do not change which characters are trained or how training selections are prioritized.

---

## R6. Validate SMTP Configuration Before Gameplay

SMTP configuration must be classified immediately after command-line parsing and before:

- Constructing `Device`
- Constructing `Client`
- Prompting for a game password
- Authenticating
- Contacting Pixel Starships
- Running any gameplay operation

The SMTP configuration consists of:

```text
--smtp-email
--smtp-password-file
--recipient
```

### No SMTP arguments

When all three arguments are absent:

- Log:

```text
Email log delivery is disabled.
```

- Continue normally.
- Do not call `email_logfile()`.
- Treat this as a valid no-op.

### Partial SMTP configuration

When one or two arguments are supplied, or when a supplied password file is missing, unreadable, or empty:

- Log:

```text
Incomplete SMTP configuration; email delivery was not attempted.
```

- Do not expose supplied values.
- Do not create a game client.
- Do not authenticate.
- Do not perform network activity.
- Exit with status `2`.

Tests must prove that `Device`, `Client`, login methods, and gameplay methods were not invoked.

### Complete SMTP configuration

When all three arguments are valid:

- Read the password from the specified file without logging it.
- Run gameplay.
- After gameplay, call:

```python
email_logfile(
    logfilepath,
    client,
    smtp_email,
    smtp_password,
    recipient,
)
```

Temporary password values must not appear in exceptions or test output.

---

## R7. Truthful Runtime Exit Semantics

Introduce the minimum necessary runtime outcome aggregation in `run.py`.

Do not stop later independent account actions merely because one response-shape-sensitive action fails.

Track the outcomes of at least:

```text
upgradeResearches()
upgradeRooms()
manageTraining()
```

Required process exits:

```text
0 — Authentication and gameplay orchestration completed with no unexpected
    runtime failures. Expected game-state skips and disabled SMTP are allowed.

1 — Authentication failed, or one or more unexpected endpoint/schema/runtime
    failures occurred.

2 — SMTP configuration was incomplete or invalid before gameplay.
```

Expected conditions must not cause exit `1`, including:

- Lab upgrade required
- Valid response containing no room upgrade opportunity
- Valid response containing no training designs
- SMTP entirely disabled

Unexpected endpoint or schema failures must not be hidden by a final `Finished...` message or a zero exit code.

The automation may complete later independent actions before exiting `1`.

Do not broaden this slice into a redesign of every existing client method’s return type.

---

## R8. Deterministic Test Coverage

All tests must use synthetic fixtures and mocked traffic.

Add tests covering at least:

### Room designs

- Missing `RoomDesign`
- Empty `RoomDesign`
- Single dictionary
- List of dictionaries
- Endpoint error response
- Invalid schema
- No traceback
- Correct `Unable to upgrade rooms.` exception message
- `listUpgradingRooms()` does not reintroduce unsafe indexing

### Research

- Lab upgrade required is logged as an expected skip
- The design ID appears in the skip message
- Expected lab rejection does not produce an error log
- Expected lab rejection does not produce final exit `1`
- Unknown endpoint error remains a failure
- Malformed response remains a failure
- No live request is made when a safe authoritative pre-filter rejects a design, when such filtering is implemented

### Training

- Missing key
- Empty collection
- Single dictionary
- List of dictionaries
- Endpoint error
- Invalid schema
- Later independent actions still run after an unexpected training failure

### SMTP

- No SMTP arguments is a successful no-op
- `email_logfile()` is not called when SMTP is disabled
- Each one-field partial combination exits `2`
- Each two-field partial combination exits `2`
- Missing password file exits `2`
- Empty password file exits `2`
- Partial SMTP validation occurs before `Device` or `Client` construction
- Complete SMTP configuration calls `email_logfile()` after gameplay
- Password content does not appear in logs or exceptions

### Exit aggregation

- Only expected skips returns `0`
- One unexpected room failure returns `1`
- One unexpected training failure returns `1`
- Remaining independent gameplay actions still execute before final exit `1`
- Partial SMTP returns `2` without gameplay

---

## R9. Quality-Bar Applicability

Apply the existing 12-point quality bar to this slice.

Add a new slice-applicability section without deleting the provisioning pilot’s applicability record.

For this slice:

- Mocked traffic is mandatory.
- Credential and token redaction is mandatory.
- Bounded error handling is mandatory.
- Gameplay preservation is mandatory.
- Allowed paths and file budget are mandatory.
- Independent critic approval is mandatory.
- Mutation verification applies only where deterministic mocked response evidence permits it.
- Provisioning-account outcome requirements that do not apply to this runtime slice may be marked N/A with an explanation.

Do not weaken a quality-bar criterion to obtain a pass.

---

## R10. Independent Critic Review

The supervisor must launch a fresh critic after every builder attempt.

The critic must not receive:

- Builder reasoning
- Builder self-evaluation
- Unsupported completion claims
- Hidden rationales

The critic may receive only:

- Slice goal
- `AGENTS.md`
- Quality bar and current-slice applicability
- Allowed paths and file budget
- Before-and-after diff
- Changed files
- Exact test output
- Exact lint output
- Sanitized mocked runtime logs
- Machine-readable artifacts

The critic must return strict JSON conforming to the existing critic-review schema and containing at minimum:

```json
{
  "verdict": "pass",
  "largest_remaining_gap": "",
  "severity": "none",
  "evidence": [],
  "quality_bar_failures": [],
  "required_next_action": ""
}
```

A failing verdict must identify exactly one largest remaining meaningful gap.

The supervisor must send that gap into a fresh builder attempt.

---

## R11. Required Validation

Before the critic can pass the slice, run:

```bash
make automation-check
make syntax-check
make test
make test-security
make lint
git diff --check
```

Also run the focused runtime-response and CLI tests independently so their test count and result are visible.

No validation may:

- Use real game credentials
- Contact live Pixel Starships endpoints
- Send real email
- Read the user’s local authentication files
- Depend on the current state of a live account

---

## Acceptance Criteria

The slice passes only when all of the following are true:

- The initial repository state is accurately recorded.
- Pre-existing user changes were preserved.
- Room-design responses support missing, dictionary, and list shapes.
- `upgradeRooms()` and `listUpgradingRooms()` do not raise shape-related tracebacks.
- The incorrect room-upgrade exception message is fixed.
- Lab-level research rejection is an expected skip.
- Unknown research failures remain application failures.
- Training responses support missing, dictionary, and list shapes.
- Valid no-training-data responses are safe no-ops.
- Unexpected training endpoint or schema failures are reported truthfully.
- No SMTP configuration is a valid no-op.
- Partial SMTP configuration exits `2` before game-client creation or network activity.
- Complete SMTP configuration still sends the log after gameplay.
- Unexpected runtime failures result in final exit `1`.
- Expected skips do not result in final exit `1`.
- Independent gameplay actions continue after a nonfatal runtime action failure.
- Gameplay selection and resource-spending behavior are unchanged.
- All tests use mocked traffic and synthetic secrets.
- No credentials, tokens, account identifiers, or passwords appear in artifacts.
- All required validation commands pass.
- A fresh independent critic returns `verdict: "pass"`.
- The final workbench records changed files, test counts, critic evidence, residual risks, and stopping reason.

Stop rather than broadening the slice when a required fix would exceed the allowed paths or file budget.

## 2026-08-08T10:03:16Z

# Teamwork Project Prompt — Tachikoma Gauntlet Slice 3: End-to-End Live Validation & Fixes

> Status: Launched
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Complete an end-to-end validation of the Tachikoma project by running the existing live GitHub Actions workflow, analyzing the real traffic logs, and fixing any remaining unhandled runtime exceptions or implementation errors.

**Working directory:** `/Users/raelldottin/Documents/Personal/tachikoma`
**Integrity mode:** Development
**Slice:** `e2e-live-validation-and-fixes`

## Objective

Create the third Tachikoma Gauntlet slice to perform a true end-to-end live run via GitHub Actions. The agent will analyze the sanitized logs from the live run to uncover any hidden unhandled exceptions, logic flaws, or parsing errors that the mocked tests missed. It will then implement fixes for all discovered issues and prove them locally using deterministic mocked tests.

## Hard Requirements

Treat the following as binding:
- `AGENTS.md`
- The existing automation supervisor and queue ownership rules
- `automation/gauntlet/quality-bar.md`
- Existing gameplay and resource-spending behavior
- Slice `allowed_paths` and file-change budget
- Required validation commands
- **Crucial Gauntlet Rule**: While the *initial discovery* is done via the live GitHub Actions workflow, all *automated validation and critic verification* of the resulting fixes must use mocked Pixel Starships traffic and synthetic tests. No live traffic is permitted during the `make test` phase.

---

## R1. Live Workflow Execution & Log Analysis

The agent must trigger the existing live GitHub Actions workflow (e.g., `daily-run.yml`) to gather real execution logs.
- Attempt to use the `gh` CLI (e.g., `gh workflow run` and `gh run view`) to trigger and monitor the run.
- If `gh` is unavailable or unauthorized, stop and prompt the user to manually trigger the workflow and provide the logs.
- Analyze the completed workflow logs for any tracebacks, unhandled exceptions, incorrect status aggregations, or unintended game-state mutations.

## R2. Fix Remaining Runtime Exceptions

For every issue identified in the live logs:
- Implement a robust fix in the codebase (e.g., `sdk/client.py`, `run.py`).
- Ensure the fix handles malformed data gracefully without crashing unrelated account actions.
- Preserve existing gameplay strategy and resource-spending rules.
- Do not introduce a broad architectural rewrite merely to fix a targeted bug.

## R3. Deterministic Mocked Coverage

Every implemented fix must be backed by a new or updated deterministic test in `tests/` that mocks the newly discovered failure mode.
- The mocked tests must reproduce the failure before the fix and pass after the fix.
- Ensure no real credentials, tokens, or account identifiers are leaked into the test fixtures.

## R4. Build the Independent Critic Loop and Run Validations

The supervisor must launch a fresh critic after each builder attempt, strictly following the existing 12-point Quality Bar established in `automation/gauntlet/quality-bar.md`.

Before the critic can pass the slice, run the following deterministic validations:
```bash
make automation-check
make syntax-check
make test
make test-security
make lint
git diff --check
```
(No live traffic is permitted during these checks).

## Acceptance Criteria

### Workflow Analysis
- [ ] A live GitHub Actions workflow run was executed and its logs were analyzed.
- [ ] All unhandled exceptions or implementation errors found in the logs are documented.

### Implementation
- [ ] All discovered runtime exceptions are fixed in the codebase.
- [ ] Fixes handle edge cases safely and gracefully without tracebacks.
- [ ] No gameplay logic or resource-spending thresholds were inadvertently altered.

### Validation & Quality Bar
- [ ] Every fix is covered by a new deterministic unit test using mocked traffic.
- [ ] All required validation commands (`make test`, `make lint`, etc.) pass cleanly.
- [ ] The independent critic returns a `verdict: "pass"`.
- [ ] The Gauntlet Workbench is updated with the new slice evidence, test counts, and critic verdict.


