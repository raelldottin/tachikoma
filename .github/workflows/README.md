# GitHub workflow strategy

Tachikoma separates a fast pull-request gate, broader verification after
merge, and source releases. The architecture follows the Codex example;
the checks use this repository's Python/unittest application and automation
harness. No Rust, Bazel, binary signing, or package-registry publication is
introduced.

| Stage | Entry workflow | Coverage |
|---|---|---|
| Pull request / merge queue | `blocking-ci.yml` | Repository policy, workflow syntax, automation harness, Python lint ratchet, full mocked application suite on Ubuntu / Python 3.13 |
| Push to `main` / manual run | `postmerge-ci.yml` | Same policies plus Python 3.11 and 3.13 on Ubuntu, macOS, and Windows |
| Version tag push | `release.yml` | Fresh checks and the platform matrix on the tagged commit, tag/main ancestry validation, reproducible source packaging, draft release |
| Manual source-release verification on `main` | `release.yml` | Same checks and packaging, artifact download, checksums and manifest identity; publisher skipped |

`automation-check.yml` and `python-tests.yml` are reusable workflows, not
additional event listeners. This replaces the old standalone Automation
Framework workflow. Post-merge failures are failures on `main`; they cannot
retroactively prevent a merge. Each distinct main commit has its own
concurrency group. Superseded pull-request runs are cancelled.

## Merge protection

After these files are committed and the first pull-request run appears,
configure the `main` branch protection rule or ruleset to require the
**`CI required`** check from GitHub Actions. Require the branch to be up to
date, or use GitHub's merge queue. Remove any obsolete requirement for
`Automation Framework / harness` if one exists.

The final job uses `if: always()` and explicitly checks every required
workflow's result. Failure, cancellation, skipped work, missing results and
malformed results all fail. There are no path filters on the required
workflow. Checkout retains GitHub's default pull-request merge revision
and merge-group revision, rather than overriding it with the PR head.
No repository settings are changed merely by adding these files.

## Validation coverage and dependencies

- Ubuntu and macOS run the full `tests/test_*.py` unittest suite. Windows
  runs the portable security/checksum, formulas, crew, ship-layout and CI
  tooling smoke suites. The full suite includes POSIX file-permission
  tests, so Windows smoke coverage is not claimed as full parity.
- The automation harness runs on Ubuntu. The hosted repository job also
  runs actionlint 1.7.12 through Go's checksum-verified module mechanism.
- `.github/requirements-ci.txt` installs only core SDK/application test
  dependencies, constrained by `requirements.txt`, plus pinned Ruff and
  PyYAML. It does not install or validate the optional LangGraph/NVIDIA,
  database, or pytest integration stack. Native unittest is the existing
  canonical test runner.
- Repository policy compiles tracked Python files without running them,
  rejects tracked files larger than 1 MiB, and checks both working-tree
  whitespace and the checked-out commit's change from its first parent.
- CI performs no game login, live capture, battle, transfer, SMTP action,
  or other game mutation. It does not read GitHub game-account secrets.
  The test runner denies socket/HTTP connections through a Python audit
  hook and fails even if a test catches the resulting error. A regression
  test exercises this guard using a synthetic connection attempt blocked
  before any connection is established.
  The `daily-run.yml` and `daily-run-langgraph.yml` schedules are separate
  operational workflows. `daily-run.yml` runs every account at the same
  time: the `accounts` job is a matrix with one leg per account number
  (`fail-fast: false`), each on its own runner. A leg's single account step
  reads only that leg's `PSS_ACCOUNT_<N>_*` secrets, as
  `secrets[format('PSS_ACCOUNT_{0}_<NAME>', matrix.account)]`, is skipped
  when `LEGACY_RUN_SKIP` lists its `matrix.account`, and passes
  `--account-alias "account-$ACCOUNT_NUMBER" --training-journal-dir
  "$RUNNER_TEMP/tachikoma-training/journals"` (with `ACCOUNT_NUMBER` set from
  `matrix.account`), so each account journals its crew training under its own
  alias. The leg then uploads its journal directory as
  `training-journal-account-<N>` (`if: always()`, `continue-on-error: true`,
  `overwrite: true` so a re-run leg replaces its journal, kept 1 day). The
  `training-report` job (`needs: accounts`, `if: always()`, given no secrets)
  downloads every `training-journal-account-*` artifact of the run, merged,
  into the same directory, appends the list of journals to the job summary,
  then runs `run.py --report training` over them, writes
  `report/training-report.md` and `.json`, and appends the Markdown report; a
  failing report run fails the step after the list is written. An
  `actions/upload-artifact` step (`if: always()`, `continue-on-error: true`,
  so an artifact service failure cannot fail the run) then uploads only
  `$RUNNER_TEMP/tachikoma-training` as `training-report-<run id>-<attempt>`
  (kept 14 days). `run.py` creates an account's journal before login, so an
  account that failed at login is listed with an empty journal; only an
  account skipped through `LEGACY_RUN_SKIP` (or one that stopped before
  `run.py` ran) writes no journal and is not listed. `scripts/ci.py`
  (`check_daily_run`) enforces this shape: exactly the two jobs; a matrix of
  only distinct positive account numbers (no `include` or `exclude`) with
  `fail-fast: false`; one account step reading account secrets only in the
  `matrix.account` form, with the `LEGACY_RUN_SKIP` condition, its alias and
  journal flag exactly once and `continue-on-error: true`; one journal upload
  after it, named per leg, with `overwrite: true`; in the report job, one
  download of only `training-journal-account-*`, merged into the journal
  directory, then a single report step under `set -euo pipefail`, without
  `continue-on-error`, credentials, `||`, `exit 0`, `set +e` or a trap, then
  an upload of only the training directory with a retention of 1 to 30 days;
  and no secrets outside the account step (a `secrets` reference anywhere
  inside a `${{ }}` expression counts). Each `if: always()` may be written
  `always()` or `${{ always() }}`.
- `daily-run.yml` (00:00, 08:00 and 16:00 UTC), `live-trial.yml` (owner
  dispatch only) and `live-trial-schedule.yml` (the R2 trial, once a day at
  04:30 UTC) all act on pss5 and share the workflow-level concurrency group
  `live-trial` with `cancel-in-progress: false`, so GitHub never runs two of
  them at once (SPEC-06 S06-R6). The group is the guarantee; the 04:30 time
  only makes queueing behind the daily run less likely. A group holds one
  running and one pending run, so a third run that queues cancels the
  pending one. While a file under `docs/experiments/live/` declares
  `legacy_run_serialized`, `scripts/ci.py`
  (`check_serialized_legacy_run`) requires the same fixed group, set at the
  workflow level only and with `cancel-in-progress: false`, in all three.
- New workflows pin external actions to full commit hashes, use bounded
  job timeouts, cache pip downloads, and disable checkout credential
  persistence. Validation defaults to `contents: read`.
- `python-tests.yml` also runs two mutation jobs (`scripts/mutation.py`,
  Ubuntu / Python 3.13) when its `run-mutation` input is true. Only Blocking CI
  passes `run-mutation: true`; post-merge and release runs skip both. Each job
  installs `.github/requirements-ci.txt` with the same pip cache as the tests
  job and gets its targets from `python scripts/ci.py mutation-config
  --blocking` or `--advisory`, which prints the matching part of
  `automation/mutation/config.json` to `$RUNNER_TEMP` for `--config`.
- The `mutation-gate` job (blocking) runs the `sdk/training/` targets. It has
  no `continue-on-error`, and its step runs under `set -euo pipefail` with a
  25-minute timeout inside the job's 30-minute timeout, so a training target
  below its `min_score` (exit 1), a config, baseline or harness error (exit 2)
  or a timeout fails the job, the reusable workflow and `CI required`.
  `mutation-config` refuses a config that drops `sdk/training/model.py`,
  `parse.py`, `planner.py` or `strategy.py` or stops running
  `tests.test_training_planner` and `tests.test_training_parse` for one of
  them. Every `sdk/training/` target blocks, so any of them, those four or a
  new one, is refused if it lists no tests, narrows itself with `functions` or
  `operators`, sets its `timeout` (a tiny one would count every mutant as a
  killing timeout), or sets a `min_score` that is not a finite number of at
  least 0.90 (every entry is checked). A target path must be written the way
  `scripts/mutation.py` normalizes it (no `./`, `//` or backslashes) and a glob
  target must not be able to reach `sdk/training/`, so neither spelling can
  slip a training file into the advisory job; `scripts/ci.py repository` runs the
  same check. Listing a mutant under `equivalent` stays possible and
  is left to review. `check_mutation_gate` keeps the job's shape: its
  condition, no `continue-on-error` outside the upload, no `if:` on the
  mutation step, exactly `set -euo pipefail`, the `config=` line and the
  `mutation-config --blocking` line before one plain `scripts/mutation.py`
  command that reads `--config "$config"` and takes only `--config`, `--jobs`,
  `--json`, `--summary`, `--quiet` and `--scratch-dir`, no `||`, `exit 0`,
  `set +e` or trap, and `run-mutation: true` in Blocking CI. Besides that
  step the job may only check out, set up Python, run
  `python -m pip install -r .github/requirements-ci.txt` and upload, so no
  earlier step can rewrite the config, the tests or `sdk/training`. The
  advisory job's `mutation-config --advisory` skips the training policy, so a
  weakened training target fails only the gate. The step writes `mutation-gate.json`,
  uploaded as `mutation-gate-report-<attempt>` like the advisory report below.
- The advisory job runs every other target. Its mutation step uses
  `continue-on-error: true` and a 15-minute timeout inside the job's 20-minute
  timeout, so a score below a target's `min_score` (a warning annotation), a
  harness or `mutation-config` error (exit 2, an error annotation) or a slow
  run is reported but cannot fail `CI required`. The job itself has no
  `continue-on-error`, so a failed checkout, setup or dependency install still
  fails it.
- Each mutation step writes its JSON report and, through
  `--summary "$GITHUB_STEP_SUMMARY"`, appends a Markdown report to the job
  summary: each target's score, minimum and headroom, operators used,
  per-operator counts, surviving mutants and any `ratchet:` hint. A ratchet hint appears when a
  passing target scores at least two points above its `min_score`; it names
  the new floor (the score rounded down to two decimals) to record in the
  config. The summary's headroom is rounded down too, so it never shows two
  points without a ratchet hint, nor zero or more next to a failure.
- In the advisory job, a following step uploads `mutation.json` as the
  `mutation-report-<attempt>` artifact (`actions/upload-artifact`, pinned to a commit; kept 14 days). It
  runs with `if: always()`, so the report survives a failed mutation step, and
  with `continue-on-error: true`, so a failed upload cannot fail the job;
  a missing file is ignored.
- Without a configured or `--timeout` value, each mutant run times out after
  five times the baseline test time multiplied by the number of parallel runs
  (CI uses `--jobs 4`), at least 10 seconds: the baseline runs alone, while
  mutant runs contend with each other.
- Config targets can also set `functions` (qualnames such as
  `PixelStarshipsApi.addTraining`; nested functions and methods are included),
  `operators` (from the defaults plus the opt-in `arithmetic` and `minmax`) and
  `aggregate: true`, which gates a glob such as `sdk/training/**/*.py` on its
  combined score while still printing each file's report. A qualname in a
  glob target's `functions` needs to match in at least one of its files. A
  glob file with nothing to mutate (an empty `__init__.py`, or nothing left
  after `functions` or `operators`) is reported as skipped and adds nothing to
  the combined score; an explicit single target with nothing to mutate under
  the default operators is still a configuration error (exit 2).
- `make mutation TARGET=...` (`--target`) can name a configured glob to run it
  whole. Naming one file of an aggregate glob runs that file without gating it
  against the glob's `min_score` (unless `--min-score` is given) and without a
  ratchet hint; only that config entry is expanded and validated, so a broken
  unrelated entry does not block it. A `--timeout` that differs from the
  target's configured one is recorded as an override and suppresses the
  ratchet hint, like `--operators`, `--tests` and `--min-score`.

## Lint debt is explicit

The clean `main` checkout had 12 Ruff findings before this change. They are recorded
by file, diagnostic code, message and occurrence count in
`.github/ruff-baseline.json`; they are not hidden by global Ruff ignores.
`python scripts/ci.py lint` rejects new findings and rejects stale allowances
when findings have been fixed. Remove the matching baseline allowance when
fixing old code. Do not add allowances for new code. Moving an unchanged
diagnostic to a different line does not inflate the baseline.

The CI helper and its regression tests additionally require Ruff formatting.
The existing `make lint` command still reports the legacy whole-tree debt;
the new gate uses the explicit ratchet rather than claiming a clean full
lint result. Type checking remains outside the required gate; it was already
advisory in the existing Makefile.

## Hollow tests are explicit

`python scripts/ci.py hollow-tests` parses every git-tracked
`tests/**/test_*.py` module, and the `tests` modules they import, with Python's
`ast` module (it imports and runs nothing) and finds unittest tests that
cannot catch a regression. Untracked files and tracked symlinks are not read,
the same file set `python scripts/ci.py repository` checks; outside a git
checkout (a Bazel runfiles tree) every module under `tests/` is read. A test
breaks one of three rules:

- `no-assertion`: neither the test nor any helper it references asserts anything.
- `mock-only`: its only assertions check how mocks were called
  (`assert_called_once_with`, `call_count`, `called`, or anything read out of
  `call_args`, `call_args_list`, `mock_calls`, `method_calls`, `await_args`
  or `await_args_list`), never a result.
- `tautology`: its only assertions check values the test built itself, from
  literals or from mocks no project code ever sees, so they cannot detect a
  regression: `self.assertTrue(True)`, `x = 4` then `self.assertEqual(x, 4)`,
  `m = MagicMock(return_value=3)` then `self.assertEqual(m(), 3)`, or a flag
  set only by `if m() is False:` on such a mock. Comparing an expression with
  itself where that always passes (`==`, `is`, `<=`, `>=`, `assertEqual`,
  `assertIs`, `assertLessEqual`, `assertCountEqual`, ...), or a value that is
  always true (`assert (result, "message")`,
  `self.assertTrue(mock.assert_called)`), is a tautology too; the same
  comparison under `!=`, `is not`, `<`, `>`, `assertNotEqual`, `assertLess`,
  ... always fails, so it counts as a real assertion like `self.fail()`. A
  name stays test-controlled only while every binding and use of it is a
  plain assignment, assertion, expression statement or attribute assignment
  that runs unconditionally or under `if`/`while` tests that are themselves
  test-controlled. Passing it to any function other than a side-effect-free
  builtin (`print`, `str`, `repr`, `len`, `int`, `float`, `bool`, `type`,
  `isinstance`, `sorted`, `list`, `tuple`, `dict`, `set`, `format`, while the
  module and test leave that name unbound), storing it where project code can
  reach it, configuring it from a project value, or touching it inside a
  loop, `try`, `with` or nested function makes it a project value, so a flag
  or counter set from project calls is not a tautology. A mock from `patch()`
  is never test-controlled: the patched project code sees it.

One real assertion anywhere in the test, or in a helper it calls or passes on,
makes the test clean. Names resolve the way Python binds them, never by
searching for any definition with the same name: a bare name finds a function
nested in the test (or in a function enclosing it), then a module-level
function, defined in the module or imported from the `tests` package;
`self.`, `cls.` and `super().` find methods of the test's class and its bases
only; `<Class>.` and `<tests module>.` find that class's methods or that
module's functions. A call named `assert*` that resolves to such a helper is
followed like any other helper; one that resolves to nothing in the tree
(`self.assertEqual`, a library's `assert_frame_equal`) counts as an assertion
by its name. Methods of a non-test class defined inside the test, such as a
fake, count too. `self.fail()`, `assert False` and `assertTrue(False)` count
as real assertions.

Tests are the `test*` methods that run in a module-level `unittest` TestCase
of a test module: the class's own methods and those of every base it inherits
from in the `tests` tree, including mixins imported from another `tests`
module (reported under the module that defines them). A class is a TestCase
when a base resolves to `unittest.TestCase` or `IsolatedAsyncioTestCase`
(through `import unittest [as u]` or `from unittest import TestCase [as TC]`)
or to a TestCase defined anywhere in the tree, worked out across all modules
at once so an import cycle cannot hide one. A class defined in the tree is
never a TestCase because of its name (`class FakeTestCase:` is not one), but a
base from outside the tree and `unittest` is trusted by name when it ends in
`TestCase` (`django.test.TestCase`, `absltest.TestCase`), and a base imported
from a `tests` module that is not in the tree is assumed to be a test base.
`unittest` loads neither module-level functions nor TestCase classes nested in
a class or defined inside a function, so those are not tests. A test that is
skipped unconditionally
(`@unittest.skip` on the test or its class, or a `self.skipTest(...)` or
`raise unittest.SkipTest(...)` statement directly in its body) is exempt; a
conditional skip is not.

Legacy hollow tests are recorded in `.github/hollow-tests-baseline.json`, sorted
(an unsorted baseline is rejected) and keyed by `tests/<file>.py::<Class>.<test>`
and rule. The check prints
`NEW: <test>: <rule>` for each hollow test missing from the baseline and
`REMOVE FIXED BASELINE ENTRY: <test>: <rule>` for each entry whose test is no
longer hollow, then fails. Fix new hollow tests instead of recording them.
`python scripts/ci.py repository` runs the same check, so Blocking CI enforces it.

Known limits of the static analysis: `self.hook()` defined only in a subclass
of the test's class (a template-method hook) is not followed, so a mixin whose
assertions live only in such a hook is reported; a test inheriting from a
class skipped with `@unittest.skip` is still examined; an import inside a
function is treated as if it were at module level; and a value read through
any call other than the builtins listed above counts as handed to project
code, even when the callee is harmless.

### How to fix a flagged test

Make the test fail when the behaviour it names breaks. Acceptable evidence is
a return value of project code, a state change project code made, a log line
captured with `assertLogs`, an error raised (`assertRaises`), or a request
recorded at the HTTP seam (a fake `Client.request` that stores what was sent,
then an assertion on that list). Checking only how a mock was called proves
the code called something, not that it did the right thing. Wrapping a call in
`assertIsNone(...)` or `assertTrue(...)` only to silence the check is not a
fix: assert something the behaviour actually determines, and plant the bug the
test is meant to catch to see it fail.

## Source releases

Use a stable `X.Y.Z` or `vX.Y.Z` tag, including the repository's existing
bare-number convention. The version tag is the version authority: this
application has no package version declaration or wheel distribution.
The release helper requires the tag to resolve to the checked-out commit
and that commit to be an ancestor of `origin/main`. Malformed versions,
unmerged branch commits and mismatched checkouts fail before packaging.

After all validation succeeds, the build job creates:

- `tachikoma-X.Y.Z.tar.gz`: committed `run.py`, `requirements.txt`,
  `LICENSE`, `README.md`, and regular Python source files below `sdk/`.
- `release-manifest.json`: version, exact tag, commit, commit timestamp,
  and source file count.
- `SHA256SUMS`: SHA-256 digests of the archive and manifest.

The archive reads Git blobs, not working-tree files. Logs, captures,
device files, snapshots, tests, and local/untracked data are excluded.
Symlinks in the source allowlist are rejected. Member order, timestamps,
ownership and gzip header metadata are fixed, making repeated builds with
the same commit and toolchain byte-identical. Dependency binaries are not
bundled; this is a source distribution, not an installer.

A read-only verification job downloads the build artifact, checks its digests,
and requires its manifest tag, version and commit to match the workflow.
A separate publisher depends on that verification, downloads only the build
artifact, checks its digests again, and creates a **draft** GitHub release with
generated notes. Only that job
has `contents: write`; it does not check out or execute project code.
Review and publish the draft in GitHub when ready. No PyPI credentials,
custom signing keys, or API keys are required. Re-running a tag whose
release already exists fails instead of overwriting it; reconcile the
existing draft or release explicitly before retrying.

To verify the hosted build without choosing a release version, run:

```sh
gh workflow run release.yml --repo raelldottin/tachikoma --ref main
```

Manual builds require `main` and create a synthetic `0.0.0` tag only inside
the fresh runner checkout. The normal tag identity and main ancestry checks
still apply. An existing local tag causes failure instead of being replaced.
The archive, manifest and checksums are retained as the `source-release`
Actions artifact for 14 days. No tag is pushed; publication requires an actual
tag-push event, so manual runs create no GitHub release, including drafts.

## Local validation

Install dependencies in an isolated environment, then run:

```sh
python -m pip install -r .github/requirements-ci.txt
python scripts/ci.py repository
python scripts/ci.py lint
python scripts/ci.py hollow-tests
python scripts/ci.py test
python -m ruff format --check scripts/ci.py tests/test_ci.py
python -m unittest tests.test_ci
make automation-check
make test
make mutation
actionlint
git diff --check
```

`tests/test_ci.py` tests aggregate failure handling, lint regression/debt
handling, hollow-test detection and its baseline ratchet, workflow permissions and checkout policy, malformed/mismatched
tags, main ancestry, repeatable archives, checksum validation, source
symlink rejection and exclusion of dirty/private synthetic files. It creates
only temporary test repositories and initiates no live game requests.

Hosted post-merge validation passed all nine jobs on the merged pipeline
commit `6d785b76c43a9128c02b40d4afc5226cd99a3fe2`, including the Linux,
macOS and Windows matrix. The `main` branch requires `CI required` from
GitHub Actions with an up-to-date branch, including for administrators.
Manual source-release verification proves packaging and artifact transport;
draft-release publication remains untested until a real version-tag push.

Publication is isolated from the original dirty inventory/gateway checkout:
only the pipeline implementation and necessary test-harness corrections are
included. Tests now derive their own checkout path, explicitly mock the
battle acceptance step, and initialize `_automation_done=False` instead of
letting a truthy MagicMock silently skip the exit-code aggregation checks.
No production gameplay or credential code is changed.

Clean-publication validation on 2026-10-16: 373 tests ran successfully
(one skipped) on Python 3.11 and 3.13 with the network audit enabled.
The portable smoke selection ran 237 tests on both interpreters. The 26
CI-tooling tests, 37 automation-harness tests, actionlint 1.7.12, repository
policy, lint ratchet, formatting and whitespace checks passed locally.
These counts intentionally exclude the unpublished inventory/gateway work
in the original checkout.
