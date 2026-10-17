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
  The existing `daily-run.yml` and `daily-run-langgraph.yml` schedules are
  separate operational workflows and retain their existing behavior.
- New workflows pin external actions to full commit hashes, use bounded
  job timeouts, cache pip downloads, and disable checkout credential
  persistence. Validation defaults to `contents: read`.

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
python scripts/ci.py test
python -m ruff format --check scripts/ci.py tests/test_ci.py
python -m unittest tests.test_ci
make automation-check
make test
actionlint
git diff --check
```

`tests/test_ci.py` tests aggregate failure handling, lint regression/debt
handling, workflow permissions and checkout policy, malformed/mismatched
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
