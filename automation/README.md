# Automation Harness

This directory holds the supervised slice-chaining harness.

Its job is narrow:

- keep a machine-readable queue of slices
- hand one slice at a time to a fresh agent run
- require a machine-readable handoff artifact at the end of that run
- decide whether another autonomous run is allowed
- stop quickly when scope, validation, or repo-state rules are violated

It does not let a running agent recursively spawn the next agent. The supervisor owns that decision.

Policy in one sentence:

- A run may recommend the next slice, but only the supervisor may continue, and only into a pre-classified adjacent slice that passes scope, validation, and diff-budget gates.

## Ownership

- `automation/queue/slices.json` is the source of truth for queued work and slice status.
- `automation/handoffs/` stores per-run handoff artifacts written by the agent run and consumed by the supervisor.
- `automation/prompts/` stores the tracked prompt fragments for slice runs and manual review runs.
- `automation/context/build_context.py` builds the bounded context bundle for one slice.
- `automation/supervisor/` owns queue selection, schema validation, stop policy, and fresh-run launching.
- `automation/schemas/` owns the JSON contracts for queue and handoff files.
- `automation/examples/` provides reference payloads without mutating the live queue.

## Prompt Package

Each fresh run starts from a tracked three-part prompt package:

- `automation/prompts/base.md`: the harness contract, ownership rules, validation bar, stop conditions, and anti-wandering guardrails
- `automation/prompts/slice.md`: the current slice brief with injected queue metadata, allowed paths, diff budget, adjacent queued slices, previous handoff summary, and handoff starter shape
- `automation/prompts/review.md`: the manual review prompt for inspecting why autonomous chaining stopped and what cleanup or approval is needed next

The agent does not inherit previous conversational state. The prompt package and compact context bundle are the only intended carry-forward inputs for each slice run.

Additional tracked fragments cover the phases around a slice run. They are not injected into every run; use the one that matches the phase:

- `automation/prompts/intake.md`: grill an underspecified request into a queue-ready slice, one decision at a time, before it is queued.
- `automation/prompts/triage.md`: classify and verify raw incoming work, then decompose it into well-formed vertical slices in dependency order.
- `automation/prompts/design.md`: for a slice that reshapes structure, design deep modules and place seams in the project's vocabulary before implementing.
- `automation/prompts/tdd.md`: for a slice that builds behavior test-first, drive a red-green loop one tracer bullet at a time, testing behavior over implementation.
- `automation/prompts/diagnose.md`: for a bugfix slice, build a red-capable feedback loop before hypothesizing, then minimise, fix, regression-test, and strip instrumentation before a clean stop.
- `automation/prompts/resolve-conflicts.md`: for an in-progress merge or rebase, preserve both sides' intent, re-run validations, and finish the integration to a clean stop.

These fragments adapt concepts from the engineering and productivity skills of [mattpocock/skills](https://github.com/mattpocock/skills) (MIT); see `THIRD-PARTY-NOTICES.md`.

## Lifecycle

1. A human or review run adds queued slices to `automation/queue/slices.json`.
2. `run_next.py` loads the queue and refuses to start if another slice is already `in_progress`.
3. The supervisor selects the highest-priority eligible `queued` slice whose dependencies are already `done`.
4. The supervisor checks current repo dirt against that slice's `allowed_paths`.
5. The supervisor marks the slice `in_progress`.
6. The supervisor builds a compact context bundle for that slice.
7. The supervisor renders the tracked base prompt plus the slice prompt and launches a fresh agent process.
8. The agent run writes one JSON handoff artifact into `automation/handoffs/`.
9. The supervisor validates the handoff, updates the queue to `done`, `blocked`, or `failed`, emits a supervisor decision report, and decides whether another autonomous run is allowed.
10. The supervisor stops as soon as a stop condition is hit.

When adjacent approved slices are already queued, a human does not need to press continue between them. The handoff may recommend the next queued slice, and the supervisor decides whether that continuation is allowed.

## Queue Model

The live queue file is `automation/queue/slices.json`.

Top-level fields:

- `version`: queue contract version
- `policy`: supervisor settings such as the autonomous run cap and supervisor-owned paths
- `slices`: ordered slice records

Each slice record includes:

- `slice_id`: stable identifier used by the queue and handoffs
- `title`: short human-readable label
- `status`: one of `queued`, `in_progress`, `blocked`, `deferred`, `done`, or `failed`
- `priority`: lower number means higher priority
- `domain`: owner or product/runtime area used for context selection
- `allowed_paths`: repo-relative path prefixes the slice is allowed to leave dirty
- `required_validations`: exact command strings the handoff must report as passed
- `depends_on`: slice IDs that must already be `done`
- `max_files_changed`: hard cap on final changed paths for the slice
- `notes`: concise operator note for the slice
- `entry_condition`: required for `blocked` and `deferred` slices; names the external fact or explicit decision that must become true before the slice may run
- `recommended_unblocker`: optional slice ID that prepares or satisfies the blocked slice's entry condition

`allowed_paths` are prefix-based, not glob-based. `owlory_xcode/Owlory/Features/Today/` allows any file under that directory. `README.md` allows only that file.
Use `blocked` or `deferred` instead of leaving work `queued` when it is parked behind external input. This keeps `make clean-stop` meaningful: no `queued` or `in_progress` slice should remain when the repo is truly at a clean stop.
Do not make blocked slices executable just to keep the supervisor busy. Queue the smallest unblocker slice instead, and have the blocked slice name it in `recommended_unblocker`.

## Handoff Model

Each autonomous slice run must write one JSON handoff artifact that matches `automation/schemas/handoff.schema.json`.

Required fields:

- `slice_id`
- `status`
- `summary`
- `files_touched`
- `validations_passed`
- `validations_failed`
- `proof_level`
- `missing_proof_levels`
- `contract_status_changes`
- `residual_risks`
- `recommended_next_slice`
- `recommended_next_reason`
- `repo_clean_status`
- `git_mirror_status`
- `dirty_paths_outside_scope`
- `timestamp`

The supervisor treats `done`, `blocked`, and `failed` differently:

- `done` can allow continuation, but only after all policy checks pass
- `blocked` always stops chaining
- `failed` always stops chaining

## Context Bundle

`automation/context/build_context.py` packages only the inputs a fresh slice run should need:

- the current slice metadata
- compact queue metadata for dependencies and adjacent queued slices
- a policy sentence that explains the supervisor model
- a previous handoff summary when a dependency or nearby predecessor already left one
- a handoff starter shape seeded with the current slice ID and required validations
- maintained docs relevant to the slice domain and explicitly allowed docs

It intentionally excludes broad repo context, unrelated docs, and raw repo noise. The goal is to make each run start cleanly, not to recreate a full conversational backlog.

## Handoff Quality

A useful handoff is specific enough that the next fresh run does not need to infer intent from raw diffs.

The quality bar is:

- `summary` names the actual landed behavior or rule change
- `files_touched` lists only repo-relative paths actually edited
- `validations_passed` and `validations_failed` use exact command strings
- `proof_level` names the highest proof actually reached
- `missing_proof_levels` lists relevant higher proof that still has not been run
- `contract_status_changes` names any product, workflow, or architecture contract status changed by the slice
- `residual_risks` preserves residual risk; use "No known residual risk." only when that is true
- `recommended_next_slice` is either an existing queued slice ID or `""`
- `recommended_next_reason` explains why that queued slice is the adjacent follow-up
- `repo_clean_status` records whether the repository was `clean`, `dirty`, or `unknown` at handoff time
- `git_mirror_status` records whether the branch was `mirrored`, `not-mirrored`, `not-relevant`, or `not-checked`
- `dirty_paths_outside_scope` truthfully lists out-of-scope dirt when it exists

## Handoff Evidence Fields

Reviewers should be able to understand what changed, what proof exists, and what remains risky without rereading the whole diff.

Use `contract_status_changes` for durable contract movement, not for every small file edit. A useful entry names the contract, its previous status, its new status, and the proof backing that status claim.

Use `residual_risks` for gaps that still matter after validation. Do not leave this empty; use `No known residual risk.` only when there is genuinely nothing useful to call out.

Use `repo_clean_status` and `git_mirror_status` to separate local handoff truth from GitHub/Xcode/release mirroring. `mirrored` means the current local branch is even with its upstream after all changes have been committed and pushed to GitHub; otherwise use `not-mirrored`, `not-relevant`, or `not-checked`.

Use the optional `open_questions` array for decisions the run could not resolve autonomously — questions surfaced during diagnosis, or that slice intake left open. It carries forward what a fresh run cannot infer, so the supervisor or a human can close it. Omit the field or leave it empty when nothing is open.

## Clean GitHub Stop

Every completed task must end with a clean GitHub stop for each touched repository:

1. Commit all changes in logical commits.
2. Push the current branch to its GitHub upstream.
3. Verify `git status --short` prints no output.
4. Verify `git rev-list --left-right --count HEAD...@{u}` prints `0 0`.

If the branch has no upstream, the push is rejected, credentials are unavailable, or any other blocker prevents the check, record the concrete blocker and do not call the stop clean.

## Proof Level Ladder

Handoffs must use this exact proof vocabulary. The value in `proof_level` is the highest rung actually reached by the slice; it is not a product-completeness claim by itself.

- `doc-only`: documentation, policy, or contract text changed without executable proof.
- `domain-tested`: deterministic domain/unit tests or automation harness tests passed.
- `build-tested`: the relevant app, package, target, or project compiled successfully.
- `running-app-smoke`: the app built, installed, launched, and produced a basic artifact such as a screenshot or log.
- `flow-verified`: a concrete user flow was exercised end to end.
- `screenshot-verified`: screenshot or snapshot artifacts prove the relevant UI state.
- `device-verified`: behavior was verified on physical device.
- `testflight-verified`: behavior was verified from a TestFlight build.

Use `missing_proof_levels` for proof that still matters for the slice but has not been run. For example, a UI behavior covered only by domain tests might report `proof_level: "domain-tested"` with `missing_proof_levels: ["running-app-smoke", "flow-verified", "screenshot-verified"]`.

## Running App Smoke

Use `python3 automation/smoke/running_app_smoke.py` when a slice needs proof that the current checkout can produce a runnable simulator app.

The runner first checks the Xcode project contract:

- the project path exists
- the requested scheme is listed
- the selected scheme exposes an application target
- the target has a bundle identifier and app bundle path
- the requested simulator destination can be resolved

Only after those checks pass does it build, boot the simulator if needed, install the app, launch it, and capture a screenshot under `/tmp/owlory-running-app-smoke/`.

Successful output reports `status: "passed"` and `proof_level: "running-app-smoke"`. Blocked output reports `status: "blocked"`, `blocked_before: "running-app-smoke"`, and the precise `blocked_contract`. A failed install, launch, or screenshot must not claim running-app proof.

Use `--locale <locale>` when proving localization resource loading in a running simulator app:

```bash
python3 automation/smoke/running_app_smoke.py --locale es --output /tmp/owlory-locale-smoke-es.json
```

Locale smoke adds launch arguments such as `-AppleLanguages (es)` and checks the built app bundle for the requested `<locale>.lproj/Localizable.strings` resources before install. If English plural resources are packaged, the same locale must package `Localizable.stringsdict`. This proof level stays `running-app-smoke`; preserving screenshots as reviewed repo artifacts is a separate screenshot-proof slice.

## Supervisor Validation Replay

The harness no longer relies only on the run's validation claims.

For a tiny exact-match allowlist of safe commands, the supervisor replays the validation itself after the slice run finishes and before continuation is allowed.

Current replayable commands:

- `make architecture`
- `git diff --check`

Replay is intentionally narrow:

- only exact command-string matches are eligible
- only commands already required by the slice are candidates
- only commands the handoff reported as passed are replayed
- anything outside this allowlist remains handoff-reported only for now

## Validation Ownership Tiers

Each required validation now has a small ownership tier so slice authors and reviewers can tell, at a glance, what the supervisor is expected to do with it.

The tiers are:

- `supervisor_replayable`: the command is in the tiny exact-match replay allowlist and the supervisor is expected to rerun it
- `run_report_only`: the command is expected to be run by the slice and reported honestly, but the supervisor does not currently replay it
- `never_supervisor_owned`: the command is intentionally outside supervisor ownership, usually because it is manual or UI-launch oriented

Current practical examples:

- `make architecture` -> `supervisor_replayable`
- `git diff --check` -> `supervisor_replayable`
- `make test-domain DOMAIN=today` -> `run_report_only`

The classifier is intentionally small. It exists to make ownership legible, not to classify every possible shell command exhaustively.

## Continuation Policy

Continuation is allowed only when all of these are true:

1. The current slice handoff reports `status: "done"`.
2. Every command in the slice's `required_validations` appears in `validations_passed`.
3. No required validation appears in `validations_failed`.
4. Any replayable required validation that the handoff reported as passed also passes supervisor replay.
5. No newly dirty path from the run falls outside the current slice's `allowed_paths`.
6. No path in `files_touched` falls outside the current slice's `allowed_paths`.
7. The run stays within `max_files_changed`.
8. The consecutive autonomous run count is still below `policy.consecutive_autonomous_limit`.
9. If `recommended_next_slice` is present, it must already exist in the queue, still be `queued`, have satisfied dependencies, and match the highest-priority eligible queued next slice.
10. If `recommended_next_slice` is empty, the supervisor may continue only into the highest-priority eligible queued next slice.

If any of those checks fail, the supervisor stops. It never auto-continues into queue-unknown work.

Eligibility for the next slice is explicit and narrow:

- only `queued` slices are candidates
- dependencies must already be `done`
- the queue may have at most one `in_progress` slice
- the selected next slice is always the highest-priority eligible queued slice
- a handoff recommendation can confirm that next slice, but it cannot bypass queue priority

## Decision Report

After each completed slice run, the supervisor emits one JSON decision report.

Decision values:

- `continue`: the current slice is `done` and the next eligible queued slice may start immediately
- `stop`: the current slice is `done` and no eligible queued next slice remains
- `stop_for_review`: the current slice is `done`, but continuation was rejected by policy and needs human review
- `stop_blocked`: the handoff reported `blocked`
- `stop_failed`: the handoff reported `failed` or the supervisor rejected the run for validation, scope, or diff-budget reasons

The decision report includes the final queue status, reason, recommended/selected next slice IDs, changed-file count, validation failures, out-of-scope paths, and the autonomous run counter.
It also includes any supervisor validation replays and whether they passed.

When no queued slice is eligible, use:

```bash
python3 automation/supervisor/run_next.py --dry-run --include-blocked
```

This does not run blocked work. It reports parked slices, their missing entry conditions, and their `recommended_unblocker` values. The intended flow is:

```text
blocked target -> unblocker slice -> blocked target becomes queued only after its entry condition is true
```

## Stop Conditions

The supervisor stops immediately when any of these are true:

- another slice is already `in_progress`
- the repo is dirty outside the candidate slice's `allowed_paths`
- the agent command exits without writing the expected handoff
- the handoff does not match schema
- the handoff reports `blocked`
- the handoff reports `failed`
- a required validation is missing from `validations_passed`
- a required validation appears in `validations_failed`
- a replayable required validation fails supervisor replay
- the slice leaves newly dirty paths outside its allowed scope
- the handoff reports touched files outside its allowed scope
- the slice exceeds `max_files_changed`
- the handoff recommends a next slice that is not already in the queue
- the handoff recommends a slice that is not `queued`, is dependency-blocked, or conflicts with the highest-priority eligible queued slice
- there is no eligible next slice
- the autonomous run limit is reached

The queue file and handoff directory are supervisor-owned administrative paths. Scope checks ignore those paths so the harness can update its own state without falsely failing every slice.

## First Proof

The smallest useful autonomy proof stays intentionally small:

- start from one queued slice
- allow continuation into one adjacent approved queued slice
- stop for human review after that second autonomous completion

The example Today sequence is:

- `today-nonfocus-add-to-focus`
- `today-continue-ui-regression-coverage`
- stop for review before auto-entering `today-continue-copy-proofread`

`make automation-check` includes a focused proof test for that sequence so the harness keeps demonstrating bounded adjacent continuation instead of open-ended chaining.

## Manual Use

Inspect the bounded context for one queued slice:

```bash
python3 automation/context/build_context.py \
  --slice-id today-continue-ui-regression-coverage \
  --queue automation/examples/example-slices.json
```

Preview the next eligible slice without launching anything:

```bash
python3 automation/supervisor/run_next.py --dry-run
```

Run that from a clean worktree or a worktree whose dirt already fits the candidate slice's `allowed_paths`. The dry run uses the same scope gate as a real run.

Run the supervisor with the repo-owned default agent command:

```bash
python3 automation/supervisor/run_next.py
```

The live queue points `policy.agent_command_template` at `automation/supervisor/run_agent.sh`. That wrapper launches a fresh non-interactive agent process with the rendered prompt on stdin. In `auto` mode it selects Claude Code when the supervisor is already running inside a Claude Code process tree, otherwise it prefers Codex when `codex` is available and falls back to Claude Code when only `claude` is available. The prompt still requires the run to write the JSON handoff at the supervisor-provided handoff path.

Runner selection can be pinned with:

```bash
REPO_AUTOMATION_AGENT_RUNNER=claude python3 automation/supervisor/run_next.py
REPO_AUTOMATION_AGENT_RUNNER=codex python3 automation/supervisor/run_next.py
```

Supported runner values are `auto`, `codex`, and `claude`. Codex runs with `--ask-for-approval never`, `workspace-write` sandboxing, and the prompt on stdin. Claude Code runs with `--print`, `--input-format text`, `--no-session-persistence`, `--permission-mode bypassPermissions`, `--add-dir <repo_root>`, and the prompt on stdin. Override executables with `REPO_AUTOMATION_CODEX_BIN` or `REPO_AUTOMATION_CLAUDE_BIN`; legacy `OWLORY_CODEX_BIN` is still accepted for Codex. Override the Claude permission mode with `REPO_AUTOMATION_CLAUDE_PERMISSION_MODE` when a consumer repository needs a stricter local policy.

Run the supervisor with an explicit override only when testing another compatible agent runner:

```bash
python3 automation/supervisor/run_next.py \
  --agent-cmd 'your-agent-runner --cwd {repo_root} --prompt-file {prompt_file}'
```

Supported command-template placeholders:

- `{repo_root}`
- `{prompt_file}`
- `{context_file}`
- `{handoff_file}`
- `{slice_id}`

Placeholder values are shell-quoted by the supervisor before execution. Do not add extra quotes around placeholders in the template unless you have tested the rendered command.

If the live queue file does not define `policy.agent_command_template`, pass `--agent-cmd`.

Smallest end-to-end manual proof with the example artifacts:

```bash
python3 automation/context/build_context.py \
  --slice-id today-continue-ui-regression-coverage \
  --queue automation/examples/example-slices.json \
  --handoff-dir automation/examples

python3 automation/supervisor/run_next.py \
  --dry-run \
  --queue automation/examples/example-slices.json \
  --handoff-dir automation/examples
```

## Failure Inspection

When the supervisor stops:

1. Read the latest artifact in `automation/handoffs/`.
2. Check `automation/queue/slices.json` for the slice status and remaining queued work.
3. Read the supervisor decision report to see whether it stopped with `stop`, `stop_for_review`, `stop_blocked`, or `stop_failed`.
4. Run `python3 automation/supervisor/run_next.py --dry-run` to see what would run next.
5. Rebuild the context directly with `build_context.py` if you need to inspect the bounded input.
6. Use `automation/prompts/review.md` for the manual review run that decides whether to clean, split, requeue, or stop.

## Validation

When changing this harness, run:

```bash
make architecture
make automation-check
```

Use `python3 automation/supervisor/run_next.py --dry-run` as a focused smoke check when the supervisor logic changes.
