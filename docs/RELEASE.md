# Release instructions


A Tachikoma release is a source archive built from a version tag on `main`. There is no installer, wheel or PyPI package. Every check that gates a change or a release runs offline with mocked game traffic. A release therefore says nothing about compatibility with the current live game server.

The workflow details live in [.github/workflows/README.md](../.github/workflows/README.md). This page is the short operator procedure.

## Versioning

- **The tag is the version.** `pyproject.toml` holds only Ruff settings, and no module declares a `__version__`. `uv.lock` lists no packages.
- **Format.** `X.Y.Z` or `vX.Y.Z`, with no leading zeros. `scripts/ci.py` rejects anything else before packaging. Existing tags use bare numbers (`1.1.0` to `1.1.8`), so keep that form.
- **Latest release.** `1.1.8`, tagged on `6442298` (#85) and published on 2026-10-19. The roadmap work merged after #91 is not in any release yet.
- **Bump rules are not defined.** The repository has no policy for when to raise the major, minor or patch number. The maintainer chooses the next version.
- **README.md version line.** `README.md` is generated from `README.template` by the hooks in `.githooks/`. They write `git describe` output into `git config current.version`, and run only in a clone with `git config core.hooksPath .githooks`. The line is not tied to releases and can be stale; `main` currently shows `1.1.7-280`.

## CI gates for a change

Every pull request to `main` runs **Blocking CI** (`blocking-ci.yml`). Branch protection on `main` requires the **`CI required`** check, requires the branch to be up to date, and applies to administrators. No review approval is required by branch protection.

`CI required` passes only if both upstream workflows succeed. A failed, cancelled, skipped or missing result fails it (`python scripts/ci.py gate`).

| Job | Workflow | What it checks |
| --- | --- | --- |
| Repository and automation harness | `automation-check.yml` | Workflow policy (read-only permissions, pinned actions, timeouts), every tracked Python file compiles, no tracked file over 1 MiB, no whitespace errors, actionlint 1.7.12, `automation.tests.test_harness` |
| Python lint ratchet | `automation-check.yml` | Ruff findings must match `.github/ruff-baseline.json` exactly (12 recorded legacy findings). New findings fail, and so do baseline entries that have been fixed. `scripts/ci.py` and `tests/test_ci.py` must also be Ruff-formatted. |
| Tests | `python-tests.yml` | The full `tests/test_*.py` unittest suite on Ubuntu, Python 3.13. A Python audit hook fails the run on any socket or HTTP connection, even if a test swallows the error. |

After merge, **Post-merge CI** (`postmerge-ci.yml`) runs the same checks on Ubuntu, macOS and Windows with Python 3.11 and 3.13. Windows runs only a portable smoke subset (`test_security`, `test_game_formulas`, `test_crew_leveling`, `test_ship_layout`, `test_ci`). A post-merge failure is a failure on `main`; it does not undo the merge.

Type checking (`ty`) is advisory and not part of any gate.

### Running the gates locally

```sh
python -m pip install -r .github/requirements-ci.txt
python scripts/ci.py repository
python scripts/ci.py lint
python scripts/ci.py test
python -m ruff format --check scripts/ci.py tests/test_ci.py
make automation-check
```

Queued slices also list their own required validations in `automation/queue/slices.json`, usually `make syntax-check`, `make test` and `git diff --check`. `make lint` still reports the whole-tree legacy Ruff debt and is not the CI gate.

## Cutting a release

1. Make sure the commit to release is on `main` and its Post-merge CI run passed.
2. Optional dry run. This runs every check and builds the archive under a synthetic `0.0.0` tag, without creating a tag or a release:

   ```sh
   gh workflow run release.yml --repo raelldottin/tachikoma --ref main
   ```

   The archive is kept as the `source-release` Actions artifact for 14 days.
3. Tag the commit and push the tag:

   ```sh
   git tag X.Y.Z <commit-on-main>
   git push origin X.Y.Z
   ```

4. The tag push starts **Source release** (`release.yml`):
   - `checks` and `tests` run again on the tagged commit, on the full Ubuntu/macOS/Windows and Python 3.11/3.13 matrix;
   - `build` checks that the tag names the checked-out commit and that the commit is an ancestor of `origin/main`, then writes `tachikoma-X.Y.Z.tar.gz`, `release-manifest.json` and `SHA256SUMS` to `dist/`;
   - `verify` downloads the artifact, checks the digests, and checks that the manifest's tag, version and commit match the workflow;
   - `publish` checks the digests again and creates a **draft** GitHub release with generated notes. It is the only job with `contents: write`, and it does not run project code.
5. Review the draft in GitHub, edit the notes if needed, and publish it by hand. Nothing is published automatically.

If a release for the tag already exists, `publish` fails rather than overwriting it. Resolve the existing draft or release by hand before re-running.

## What the archive contains

The archive is built from Git objects at the tagged commit, not from the working tree. It contains `run.py`, `requirements.txt`, `LICENSE`, `README.md` and the `.py` files under `sdk/`. Member order, timestamps, ownership and gzip headers are fixed, so the same commit builds byte-identical archives.

It does not contain tests, docs, `automation/`, `scripts/`, logs, captures or device files. It also leaves out non-Python files under `sdk/`, so the bundled scenarios in `sdk/simulation/scenarios/*.json` are missing from a release archive. Dependencies are not bundled; install them from `requirements.txt`.

## What is manual or undefined

- Choosing the version number (no bump policy).
- Writing release notes beyond GitHub's generated list.
- Publishing the draft release.
- Keeping the `README.md` version line current.
- There is no changelog file.
- No release step runs anything against the live game. Any live verification has to be done and recorded separately, and none has been recorded for the roadmap work (see [known limitations](baseline/known-limitations.md)).

The scheduled `daily-run.yml` workflow is separate from validation and releases. It runs the legacy `run.py` daily sequence with `--run-battle` against the accounts configured in repository secrets. It does not use the roadmap's runner, journal or scheduler.
