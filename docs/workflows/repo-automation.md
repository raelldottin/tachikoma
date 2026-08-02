# Reusable Repo Automation

This workflow defines how Owlory's repo automation becomes reusable in other repositories while the current Owlory checkout remains the initial source of truth.

## Target Home

The reusable automation distribution target is:

```text
/Users/raelldottin/Documents/Personal/repo-automation
```

`/Users/raelldottin/Personal` does not exist on this machine. The active Personal workspace is `/Users/raelldottin/Documents/Personal`.

Until an explicit ownership flip is documented, updates flow one way:

```text
Owlory reusable automation source -> /Users/raelldottin/Documents/Personal/repo-automation
```

The external folder is a reusable distribution target, not a second source of truth. Reverse edits from `repo-automation` back into Owlory require an explicit migration or patch slice.

## Goals

- Reuse the supervised slice harness in other repositories without copying Owlory product state.
- Keep the external `repo-automation` folder current whenever Owlory's reusable automation changes.
- Make reusable assets manifest-owned so sync behavior is deterministic and reviewable.
- Preserve Owlory-specific release, localization, UI proof, product, and SecondBrain history inside Owlory.

## Reusable Inventory

These assets are reusable or intended to become reusable with light parameterization:

- `automation/supervisor/`: queue selection, policy checks, validation ownership, diff-budget checks, and fresh-run launching.
- `automation/context/build_context.py`: compact slice context bundle generation.
- `automation/prompts/`: base, slice, and review prompt fragments.
- `automation/schemas/`: JSON contracts for slices and handoffs.
- `automation/examples/`: starter queue and handoff payloads for new repositories.
- `automation/README.md`: harness behavior and operator model.
- `automation/tests/test_harness.py`: core supervisor and context-builder regression coverage.
- `Tools/clean-stop-check.py`: reusable after repository name and queue path are configurable.
- `Tools/agent-handoff.sh`: reusable after repository name, read order, and validation shortcuts are configurable.
- `pyproject.toml` `[tool.ruff]` and `[tool.ty]` sections: reusable lint and type-check configuration; a consumer copies the blocks into its own `pyproject.toml` and adjusts paths.
- Make targets for `handoff`, `clean-stop`, `automation-check`, and the lint/type checks (`uv run ruff check .`, `uv run ty check`): reusable after app-specific targets are excluded.

## Owlory-Specific Exclusions

The sync manifest must not copy these into the reusable automation distribution unless a later slice explicitly extracts and generalizes them:

- `automation/queue/slices.json`: live Owlory work queue and product history.
- `automation/handoffs/`: live Owlory handoff history.
- `automation/proofs/`: app, localization, TestFlight, screenshot, and design proof artifacts.
- `automation/smoke/`: currently tied to Owlory's Xcode app, simulator, localization, and screenshot proof paths.
- Owlory-specific automation tests such as release provenance, localization drift, localized screenshots, running app smoke, and version bump tests.
- Owlory product docs under `docs/product/`, `docs/runtime/`, and most `docs/workflows/` entries that describe app behavior rather than harness behavior.
- `SecondBrain/`: Owlory operational history.
- `localization/`, `owlory_xcode/`, app resources, and generated app artifacts.
- Release tooling such as `Tools/bump-version.sh`, `Tools/set-build-number.sh`, `Tools/generate-build-info.sh`, `Tools/verify-build-provenance.sh`, `Tools/release-preflight.sh`, and `.githooks/pre-push` as currently written.
- App-specific Make targets such as `fast`, `verify`, `test-domain`, `ui-smoke`, `ui-regression`, `build-provenance`, `release-preflight`, and localization targets.

## Manifest Contract

The tracked manifest lives at `automation/reusable-manifest.json` and owns the distribution file list.

The manifest is explicit rather than glob-heavy. Each entry identifies:

- source path in Owlory
- destination path under `repo-automation`
- file or directory kind
- whether executable mode should be preserved
- whether stale destination files under that owned path may be deleted
- whether the entry is reusable now or copied as a template for consumer customization

The sync tool rejects paths outside the repository root and outside the target root. It does not follow symlinks into untracked locations.

## Sync Contract

Use `Tools/repo-automation-sync.sh` for manifest-owned sync:

- `--check`: report drift between Owlory reusable sources and the external folder without changing files.
- `--sync`: update the external folder to match the manifest.
- `--auto-update`: for validation and pre-push use only; require the target to be an existing clean Git repository, sync manifest-owned files, then verify `--check` passes.
- `--target <path>`: override the target path for tests and future consumers.
- `--source <path>` and `--manifest <path>`: test hooks for temp repositories and alternate manifests.

The tool must be idempotent. Running `--sync` twice against the same source should produce no second change. Running `--check` after a successful sync should pass.

Stale-file deletion is allowed only under manifest-owned destination paths. The tool must not clean arbitrary files in `repo-automation`.

## Automatic Update Contract

Owlory wires repo-automation currentness into the normal local automation path:

- `make repo-automation-check` runs `Tools/repo-automation-sync.sh --check --target /Users/raelldottin/Documents/Personal/repo-automation`.
- `make repo-automation-update` runs `Tools/repo-automation-sync.sh --auto-update --target /Users/raelldottin/Documents/Personal/repo-automation`.
- `.githooks/pre-push` detects whether the pending push touches manifest-owned reusable automation sources. If so, it runs `make repo-automation-update` before allowing the push.

Expected behavior:

- If a commit changes manifest-owned reusable automation, the pre-push path syncs `/Users/raelldottin/Documents/Personal/repo-automation` locally and verifies the target is current.
- If the external target is missing, `--auto-update` fails and points to the bootstrap path.
- If the external target is not a Git repository, `--auto-update` fails rather than creating an untracked copy.
- If the external target has local dirt before the update, `--auto-update` fails rather than overwriting external work.
- Automatic update means the external folder contents are updated locally. External Git commit or remote push remains explicit unless a later slice adds documented opt-in behavior.

## Consumer Repository Contract

A future repository should consume `repo-automation` as a reusable package or template, then own its repo-specific configuration:

- repository name and read order
- domain or work-area docs
- validation commands and proof levels
- local queue file
- handoff directory
- prompt fragments if local policy differs
- Make targets that map to that repository's build and test commands

New repositories must start from examples or templates, not Owlory's live queue, handoffs, proofs, or SecondBrain history.

## Clean GitHub Stop Contract

Reusable automation assumes every task ends with a clean GitHub stop across all touched repositories. A clean stop requires:

1. all changes committed in logical commits
2. the current branch pushed to its GitHub upstream
3. `git status --short` returning no output
4. `git rev-list --left-right --count HEAD...@{u}` returning `0 0`

This applies even in multi-agent workspaces. If another agent leaves dirt in a repo, inspect it, commit or deliberately preserve it, push the resulting branch, and report the exact state. If a branch has no upstream, credentials fail, or a push is rejected, record that blocker explicitly and do not call the stop clean.

## Agent Runner Selection

`automation/supervisor/run_agent.sh` is the reusable launch wrapper for fresh slice agents. Its default `auto` mode supports both Codex and Claude Code without changing `policy.agent_command_template` in every consumer queue.

The wrapper chooses Claude Code when it detects that the supervisor was invoked from a Claude Code process tree and a `claude` executable is available. Outside Claude Code it preserves the previous Codex-first behavior: use `codex` when present, then fall back to `claude` if Codex is not installed. Operators can pin a runner with:

```bash
REPO_AUTOMATION_AGENT_RUNNER=claude python3 automation/supervisor/run_next.py
REPO_AUTOMATION_AGENT_RUNNER=codex python3 automation/supervisor/run_next.py
```

Codex runs with the existing no-approval, workspace-write invocation. Claude Code runs non-interactively with prompt stdin, `--print`, `--input-format text`, `--no-session-persistence`, `--permission-mode bypassPermissions`, and `--add-dir <repo_root>`.

Consumer repositories can override executable names with `REPO_AUTOMATION_CODEX_BIN` or `REPO_AUTOMATION_CLAUDE_BIN`. `OWLORY_CODEX_BIN` remains supported for existing Codex setups. Consumers that require a stricter Claude local policy can set `REPO_AUTOMATION_CLAUDE_PERMISSION_MODE` to another Claude Code permission mode.

## Bootstrap Status

As of 2026-05-21, `/Users/raelldottin/Documents/Personal/repo-automation` is initialized as a Git repository on `main`. The bootstrap commit is `6ab871bbf957df24e648b02ef002c0efa2d7c609`. Exact publication commits are recorded in Owlory handoffs so this manifest-synced workflow doc does not have to change for every external commit.

The bootstrap commit was populated only by `Tools/repo-automation-sync.sh --sync --target /Users/raelldottin/Documents/Personal/repo-automation`, then verified with:

```bash
Tools/repo-automation-sync.sh --check --target /Users/raelldottin/Documents/Personal/repo-automation
```

## Remote Status

The external repository remote is:

```text
https://github.com/raelldottin/repo-automation.git
```

`main` tracks `origin/main`, and `git -C /Users/raelldottin/Documents/Personal/repo-automation rev-list --left-right --count HEAD...@{u}` returns `0 0`.

The GitHub repository also exposes the SSH URL `git@github.com:raelldottin/repo-automation.git`, but this machine does not currently have GitHub SSH key authentication configured. Publication used the GitHub CLI authenticated HTTPS path.

## Consumer Adoption Bootstrap

A non-Owlory repository can adopt the reusable automation package by syncing the
manifest-owned subset from Owlory and then committing it into a fresh local Git
repository. The exact sequence proven by `RepoAutomationConsumerAdoptionSmokeTests`
in `automation/tests/test_repo_automation_sync.py` is:

1. Create the consumer directory and run

   ```bash
   Tools/repo-automation-sync.sh --sync --target <consumer-path>
   ```

   from inside Owlory. The manifest at `automation/reusable-manifest.json` decides
   what lands; Owlory product state (live queue, handoffs, proofs, SecondBrain,
   `owlory_xcode/`, localization, product/runtime docs, release tooling, Owlory
   pre-push hook) is rejected by the sync tool unless an entry explicitly opts
   in with `allow_owlory_specific: true`.

2. From the consumer directory:

   ```bash
   git init -b main
   git config user.email <consumer-email>
   git config user.name <consumer-name>
   git add -A
   git commit -m "Bootstrap reusable automation"
   ```

   The supervisor and `make repo-automation-update` both require a clean Git
   working tree. The very first sync produces many untracked files, so the
   bootstrap commit must happen before normal automation runs.

3. Provide the repo-specific local state the reusable assets expect:

   - `automation/queue/slices.json` — copy `automation/examples/example-slices.json`
     as a starting point and rewrite it for the consumer's own slices.
   - `automation/handoffs/` — create the directory; the supervisor writes
     handoff artifacts here.
   - `.gitignore` entry for `__pycache__/`, or invocations should set
     `PYTHONDONTWRITEBYTECODE=1`. Without one of those, supervisor runs
     leave pycache files that the supervisor's own dirty-tree check then
     refuses on the next invocation.

4. Smoke-verify by running the supervisor inside the consumer:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 python3 automation/supervisor/run_next.py --dry-run
   ```

   The dry-run prints `selected_slice` plus a handoff path that resolves under
   the consumer repo (not Owlory). `automation/supervisor/run_next.py` derives
   `REPO_ROOT` from its own file location, so syncing the supervisor file tree
   into the consumer is what makes it operate on the consumer's queue.

### Known consumer-side failure modes

The smoke test asserts the friendly message shape so future changes to the
reusable supervisor do not silently regress it back into raw tracebacks. The
common consumer-side failure modes now exit with code 2 and a two-line
`stop: <reason>` + `hint: <fix>` shape:

- Missing `automation/queue/slices.json` (either via
  `automation/supervisor/run_next.py` or `automation/context/build_context.py`):

  ```text
  stop: queue file not found: <path>
  hint: copy automation/examples/example-slices.json to that path and edit it for this repository's slices.
  ```

- Running the supervisor outside a Git working tree:

  ```text
  stop: not a Git repository: <consumer path>
  hint: run 'git init -b main' in this directory, commit the bootstrap, then re-run.
  ```

- Running the supervisor on a dirty working tree returns the supervisor's own
  `stop: repo is dirty outside the next slice scope` message and a non-zero
  exit code. This path was already friendly before this slice.

The friendly messages flow through a `policy.ConfigError` exception raised by
`automation/supervisor/policy.py` (`load_json`, `load_queue`, `git_dirty_paths`)
and caught at the CLI entry points (`run_next.py:_cli_entry`,
`build_context.py:_cli_entry`).

### Manual steps that remain for a real consumer

These are not covered by the smoke test and require explicit per-repository
work:

- A consumer-specific `Makefile` with targets that map to the consumer's own
  build, test, and validation commands. The reusable tree does not ship a
  Makefile because Owlory's targets are app-specific.
- A consumer-specific `AGENTS.md` (or equivalent) that names the consumer's
  read order, allowed paths, and validation expectations.
- Optional `core.hooksPath` configuration if the consumer wants the same
  commit-msg or pre-push behavior as Owlory.
- Optional override of the prompt fragments under `automation/prompts/` if the
  consumer needs different policy language (see Customizing prompt fragments
  below).
- Optional update of the `[tool.ty]` paths in `pyproject.toml` if the consumer
  wants its own Python paths type-checked.
- A consumer-specific remote (e.g., `git remote add origin <url>`) and an
  initial push. The smoke test does not exercise remote publication.

### Customizing prompt fragments

`automation/supervisor/run_next.py:render_prompt` reads
`automation/prompts/base.md` and `automation/prompts/slice.md` from the
consumer's own `repo_root`, so a consumer can rewrite either file and the
supervisor will use the customized text for every subsequent run. This is
asserted by
`test_consumer_can_override_prompt_fragments` in
`automation/tests/test_repo_automation_sync.py`, which writes sentinel
markers into both files and verifies they appear in the rendered prompt.

The consumer-side flow:

1. **Commit the override first.** The supervisor's dirty-tree check refuses
   to run when files outside the current slice's `allowed_paths` are dirty.
   A consumer who edits `automation/prompts/*.md` must `git add` and commit
   before running the supervisor; otherwise the run aborts with the existing
   `stop: repo is dirty outside the next slice scope` message.

2. **Overrides survive re-sync.** The manifest entries marked
   `template: true` (currently `automation/prompts/` and `automation/examples/`)
   are first-time-only: `Tools/repo-automation-sync.sh`
   copies them when the destination file does not yet exist and otherwise
   leaves them alone. Consumer-added files in those directories also survive
   (those entries set `delete_stale: false`).
   `test_consumer_prompt_override_survives_resync` and
   `test_consumer_added_prompt_file_survives_resync` cover both cases.

3. **Owlory updates to template files don't auto-propagate.** Because the
   sync skips existing template files, an upstream Owlory improvement to
   `base.md` or `slice.md` does not reach a consumer who already has those
   files. Non-template entries (the supervisor, context builder, schemas,
   harness test, docs) continue to be authoritative on every sync.

   To deliberately re-baseline template files to the current Owlory
   content, run:

   ```bash
   Tools/repo-automation-sync.sh --sync --force-templates --target <consumer>
   ```

   `--force-templates` bypasses the first-time-only guard so all template
   entries are rewritten to source content, replacing any local
   customizations. The flag intentionally does NOT remove
   consumer-added files in template directories — those still survive
   because the manifest entries set `delete_stale: false`. This is
   asserted by `test_force_templates_overwrites_consumer_override` and
   `test_force_templates_preserves_consumer_added_files` in
   `automation/tests/test_repo_automation_sync.py`.

### What the smoke test does not prove

- That any specific external repository has actually adopted the package.
- That Make targets, hooks, or prompt fragments composed into a real consumer
  Makefile produce a working CI integration.
- That the reusable supervisor handles consumer-side prompts or LLM
  invocations end-to-end. The smoke proof is limited to `--dry-run` slice
  selection.

## Next Slice Boundary

Consumer adoption proof is complete at the smoke level and the common failure
modes a consumer encounters now exit with friendly `stop:` + `hint:` messages.
The next implementation boundaries (not queued by this slice) are a real
third-party consumer migration when a specific repository is named, prompt
fragment override portability proof, and a consumer Makefile / hooks / CI
smoke.
