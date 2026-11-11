# Bazel migration roadmap

**Status: Planned — not started; reviewed by security, accuracy and feasibility QA (4 rounds).** Date: 2026-11-11,
written against `main` at `050f076` (since then `7b1dd5a` (#153) only queued `bazel-migration-roadmap-docs`, the slice
that lands this document). The queue entries in
[bazel-migration-queue-entries.json](bazel-migration-queue-entries.json) are **proposals**: none is in
[`automation/queue/slices.json`](../automation/queue/slices.json) yet. The owner adds them in a queue PR after answering
the questions in section 6 (at least Q-1a, Q-5 and Q-14 for B0).

This is the execution document: it holds only what is needed to queue and run the slices. Rationale, evidence, the risk
register and the decisions log are in [BAZEL_MIGRATION_DESIGN_NOTES.md](BAZEL_MIGRATION_DESIGN_NOTES.md). **When this
document and the queue disagree, the exact strings in the queue win** (the JSON file before the queue PR,
`automation/queue/slices.json` after it; the JSON file is not updated afterwards). Items marked **UNVERIFIED** are
confirmed by the spike or run named next to them. Main repository inputs: [`Makefile`](../Makefile),
[`scripts/ci.py`](../scripts/ci.py), [`.github/workflows/`](../.github/workflows/),
[`automation/supervisor/policy.py`](../automation/supervisor/policy.py), [RELEASE.md](RELEASE.md) and
[`AGENTS.md`](../AGENTS.md).

> **Owner action now (A-0).** On 2026-11-11, review test runs started from the repository root rewrote the untracked
> `./.device` and `./tachikoma.log` (09:35 and 09:37). If `.device` held a real device key, treat it as exposed and
> re-provision, and afterwards keep no real `.device` in the checkout (run `run.py` from a separate runtime directory;
> the daily runs use repository secrets). `make test` is safe once `b0-make-hermetic-local` merges, and a direct
> `scripts/ci.py test` once `b0-ci-root-and-runner` merges. `python -m unittest discover -s tests` or running a test file
> as a script from the repo root is never safe while a real `.device` is there. Until then run tests only as
> `cd "$(mktemp -d)" && <repo>/.venv/bin/python <repo>/scripts/ci.py test`.

## 1. Goal and success criteria

**Goal.** Add Bazel as a hermetic build and test layer for `run.py`, `sdk/`, `scripts/ci.py` and `tests/`: pinned
Python 3.11/3.13 toolchains, hash-locked dependencies, per-file cached test targets. No repository invariant weakens:
mocked network traffic, no secrets in tracked files, the workflow policy, reproducible releases, and the supervisor's
validation strings.

**Non-goals.** No change to `run.py` or `sdk/` behaviour or code. The daily live workflows, `test_live_auth`, the
release workflow and README generation stay outside Bazel. No remote cache, remote execution or BES. unittest stays the
runner. No ruff bump and no `ruff format` adoption. The synced automation code changes only through an owner-run sync.

| # | Success criterion | Measured by |
|---|---|---|
| S1a | PR gate: Bazel on ubuntu-latest / 3.13 is part of `CI required` and green | B6 |
| S1b | Post-merge: ubuntu and macOS × 3.11 and 3.13 green under Bazel (not merge-blocking) | B4 and B6 exit rules |
| S2 | Per-module parity: normalized test ids equal between `ci.py test --verbose` and Bazel; skips differ only by the allowlist | `manual bazel parity` |
| S3 | Every Bazel test installs the audit hook before loading the test; a swallowed connect fails; no `py_test` outside the macro; Linux sandbox canary green | `test_bazel_main`, hygiene test, CI query and canary |
| S4 | No workspace dirt after Bazel commands (`git status --porcelain` and `git ls-files --others --exclude-standard` empty), also in a worktree | every phase exit |
| S5 | Release parity: identical bytes for tag `1.1.8` and for a throwaway tag on the candidate commit; no Bazel or lock file in the archive; no release function edited | V-REL |
| S6 | `make syntax-check`, `make test`, `make test-security`, `make automation-check`, `make lint`, `make git-check`, `git diff --check` exit 0 from the end of B0; pre-commit and gauntlet strings still resolve; synced code unchanged | V-SUP |
| S7 | `python scripts/ci.py repository` passes with `bazel-tests.yml` in `WORKFLOWS`; every action pinned to a verified 40-hex commit | CI |
| S8 | Every distribution, including sdists and their build requirements, is hash-pinned from one lock exported from `uv.lock` | `tests/test_dependency_locks.py` |
| S9 | No `Path(__file__).resolve()` or `os.path.realpath(__file__)` call in `tests/**/*.py`, `scripts/ci.py`, `scripts/bazel_parity.py` (exception: `sdk/simulation/game.py:43`) | AST guard in `tests/test_ci.py` |
| S10 | Every tracked `tests/test_*.py` has one Bazel target except `test_live_auth` and `test_build_hygiene` | hygiene test (runs in every PR) |
| S11 | `release.yml` never enables Bazel | `tests/test_ci.py` (B6) |
| S12 | A direct `ci.py test` from any CWD never reads or writes the caller's `.device` | regression test (B0) |

## 2. Target summary

### 2.1 Pins

| Component | Pin |
|---|---|
| Bazel (`.bazelversion`) | 9.3.2 (from 2026-11-19; 9.3.1 if earlier) |
| Bazelisk | developers 1.30.0; CI uses the runner image's preinstalled Bazelisk |
| `bazel-contrib/setup-bazel` | 0.19.1 = `104e3eea9447cdaa4b00d0fb46191a3985d26ab3` |
| `rules_python` | 2.4.6 |
| Python toolchains | 3.13 (default, maps to 3.13.18) and 3.11 (3.11.18) |
| `bazel_skylib` / `platforms` | 1.9.2 / 1.1.0 |
| uv (lock producer) | 0.13.7; `required-version = ">=0.13.7,<0.14"` |
| ruff / PyYAML (CI group) | 0.15.5 / 6.0.3 |

Rule: exact pins, each at least 14 days old when its slice merges. Every bump records its source, release date,
tag-to-commit dereference (actions), `bazel mod graph` and `registryFileHashes` diffs (modules) and new checksums
(Bazel), as detailed in the design notes.

### 2.2 `MODULE.bazel`

```starlark
module(name = "tachikoma", version = "0.0.0")  # not the release version; releases come from git tags

bazel_dep(name = "rules_python", version = "2.4.6")
bazel_dep(name = "bazel_skylib", version = "1.9.2")
bazel_dep(name = "platforms", version = "1.1.0")

python = use_extension("@rules_python//python/extensions:python.bzl", "python")
python.defaults(python_version = "3.13")
python.toolchain(python_version = "3.13")
python.toolchain(python_version = "3.11")

# Added by b2-pip-hub:
pip = use_extension("@rules_python//python/extensions:pip.bzl", "pip")
[pip.parse(
    hub_name = "pip_deps",
    python_version = v,
    requirements_lock = "//:requirements_lock.txt",
    # extra_pip_args per Q-11, default ["--no-build-isolation"]
) for v in ("3.11", "3.13")]
use_repo(pip, "pip_deps")
```

No `*_override`, no extra `--registry`, no `http_archive`/`http_file` without `integrity`. `MODULE.bazel.lock` is
committed and stays under 1 MiB.

### 2.3 `.bazelrc`

```
common --enable_platform_specific_config
common --experimental_convenience_symlinks=ignore
common --lockfile_mode=error
common --repository_disable_download
common --@rules_python//python/config_settings:incompatible_default_to_explicit_init_py=true
common:fetch --norepository_disable_download
common:py311 --@rules_python//python/config_settings:python_version=3.11
common:py313 --@rules_python//python/config_settings:python_version=3.13
test --test_output=errors

common:ci --config=fetch
common:ci --repo_contents_cache=
common:ci --@rules_python//python/config_settings:validate_test_main=enabled
common:ci_linux --strategy=TestRunner=linux-sandbox
common:ci_macos --strategy=TestRunner=darwin-sandbox
test:ci --nocache_test_results --test_summary=terse --color=yes
test:ci --sandbox_default_allow_network=false

startup:windows --windows_enable_symlinks
common:windows --enable_runfiles
```

- Every config name used anywhere has a `common:` line, so `bazel build`, `bazel info` and `bazel query` accept the
  same `--config` set as `bazel test`.
- Downloads are off by default: plain `bazel build //...` and `bazel test //tests:all`, the agent validation
  strings, never run a `ctx.download` that is not already in the repository cache (checksummed downloads are served
  from the cache; unchecksummed ones such as PyPI index pages are refused). Bzlmod registry files are still fetched
  (hash-checked against the lockfile) when missing from the cache, and `ctx.execute` in a repository rule (such as an
  sdist build) still runs when a new output base re-materializes repositories. The owner fetch (section 3) runs in each
  new worktree before an agent uses it. CI gets downloads through `--config=ci`. Spike item 13 decides whether
  `common --nofetch` or `common --repo_env=PIP_NO_INDEX=1` (allowlisted by exact value) joins the defaults.
- `--lockfile_mode=error` everywhere. Only the owner updates the lockfile, with
  `bazel mod deps --config=fetch --lockfile_mode=update`, in a slice that changes modules or extensions. `update` is
  never put in `common:fetch`, because `common:ci` includes `--config=fetch`.
- There is no workspace `try-import`: personal flags go in `~/.bazelrc`, and agents never create or edit `.bazelrc*`.
- `validate_test_main` is kept only if spike item 11 shows it accepts `bazel_main.py`. The strategy pins turn a silent
  sandbox fallback into a failure (Linux UNVERIFIED on `ubuntu-latest`, Q-16; macOS spike item 16).
- `--lockfile_mode=off` is never used; `.gitignore` has `/bazel-*` (not `.bazelrc.user`, so a stray one shows up as
  untracked dirt and halts the supervisor).

### 2.4 BUILD layout

- `.bazelignore`: `.venv`, `venv`, `.worktrees`, `.claude`, `Tools`, `cpp2il_output`, `.agents`, `.profiles`.
- Every `BUILD.bazel` (root, `sdk/`, `scripts/`, `tests/`, later `automation/`) calls
  `package(default_visibility = ["//:__subpackages__"])` exactly once, after its `load()` statements (Starlark requires
  loads first) and before its first rule. The hygiene test checks that placement and forbids `//visibility:public`.
- Root `BUILD.bazel`: `exports_files([".gitignore", "README.template", "requirements.txt", "requirements_lock.txt",
  "pyproject.toml", "uv.lock"])`; `repo_docs = glob(["docs/**"], exclude = SECRET_PATTERNS) + ["README.template"]`;
  `ci_policy_inputs = glob([".github/workflows/*.yml"]) + [".github/ruff-baseline.json"]`; from B2 `run_lib`.
  `SECRET_PATTERNS` = `**/*.har`, `**/*.pcap`, `**/*.dump`, `**/*.dat`, `**/*.log`, `**/.device`,
  `**/config.secrets`, `**/passwords.txt`, written as a literal list. `tests/BUILD.bazel` repeats the same literal list
  (a BUILD file cannot load another), and the hygiene test asserts the two are equal.
- `sdk/BUILD.bazel`: one `py_library` `//sdk`. `scripts/BUILD.bazel`: `ci_lib`, `provision_account_secrets_lib` (no
  `py_binary`, ever), `bazel_parity_lib`.
- `tests/BUILD.bazel`: `:support`, `:test_sources = glob(["**/*.py"], exclude = SECRET_PATTERNS)`, `:pvp_fixtures`,
  one `tachikoma_unittest` per covered module, `sandbox_canary`. No `test_suite`; `//tests:all` is the wildcard.
- Every BUILD and `.bzl` file `load()`s its rules (`@rules_python//python:py_library.bzl`, `py_binary.bzl`,
  `py_test.bzl`; `@bazel_skylib//rules:build_test.bzl`).
- No BUILD file under `docs/`, `.github/`, `automation/tests/` or any synced `automation/` directory. No root label names
  a file under `automation/`.

Test data (`b2-test-main-macro`, `b2-all-test-targets`):

| Target | Extra data / deps |
|---|---|
| `:support` (so every importer) | `//:repo_docs` |
| `test_action_catalog`, `test_capability_inventory`, `test_client_snapshot_provider`, `test_compat_ledger`, `test_progression_dry_run`, `test_fleet_dry_run` | `//:repo_docs`, `//:run_lib`; `test_capability_inventory` also `:test_sources`; the two dry-run tests also `//:.gitignore` |
| `test_ci` | `//:ci_policy_inputs`, `:test_sources`, `@pip_deps//pyyaml`, size medium, Windows-compatible; from B2 parity also `//scripts:bazel_parity_lib` |
| `test_pvp_safety` | `//sdk` |
| `test_pvp_integration`, `test_pvp_sanitized_inputs` | `:pvp_fixtures`, `//sdk` |
| `test_security`, `test_game_formulas`, `test_crew_leveling`, `test_ship_layout` | Windows-compatible (the smoke subset) |
| `test_dependency_locks` | `//:pyproject.toml`, `//:uv.lock`, `//:requirements_lock.txt`, `//:requirements.txt` |
| `test_bazel_main` / `test_bazel_parity` | `:bazel_main.py`, `//scripts:ci_lib` / `//scripts:bazel_parity_lib` |
| `test_live_auth`, `test_build_hygiene` | no target |

The four test modules other tests import (`test_client_snapshot_provider`, `test_fleet_concurrent`,
`test_fleet_orchestrator`, `test_progression_policy`) are in `:support` and also have their own targets.

### 2.5 Test macro and main

```starlark
load("@rules_python//python:py_test.bzl", "py_test")

_MAIN = Label("//tests:bazel_main.py")

def tachikoma_unittest(name, deps = [], data = [], windows = False, size = "small", tags = [], **kwargs):
    if "main" in kwargs or "srcs" in kwargs:
        fail("tachikoma_unittest fixes srcs and main to //tests:bazel_main.py")
    py_test(
        name = name,
        srcs = [name + ".py", _MAIN],
        main = _MAIN,
        args = ["tests." + name],
        deps = deps + [Label("//tests:support"), Label("//scripts:ci_lib")],
        data = data,
        size = size,
        legacy_create_init = 0,
        env = {"TACHIKOMA_STRICT_DEPS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        tags = tags,
        target_compatible_with = [] if windows else select({
            "@platforms//os:windows": ["@platforms//:incompatible"],
            "//conditions:default": [],
        }),
        **kwargs
    )
```

`tests/bazel_main.py`: put the runfiles workspace root (from `TEST_SRCDIR`/`TEST_WORKSPACE`, no `resolve()`) at
`sys.path[0]`; `os.chdir(os.environ["TEST_TMPDIR"])`; import `scripts.ci`; return
`ci.run_tests([module], require_tests=True, verbosity=2)`. Strict mode (`TACHIKOMA_STRICT_DEPS=1`) turns dependency
skips into failures. Allowed skips, by normalized id: Bazel `test_ci.RepositoryReleaseSelectionTests.*` (no `.git`);
`ci.py` `test_live_auth.TestLiveAuth.*`. B5 adds optional `src` and `module` parameters (defaults `name + ".py"` and
`"tests." + name`) that drive `srcs` and `args`, so `//automation` can call the macro; `main` stays fixed.

### 2.6 `make` recipes (B0)

```make
PYTHON = $(shell if [ -x .venv/bin/python ]; then echo "$(CURDIR)/.venv/bin/python"; else command -v python3 2>/dev/null || echo python3; fi)
REQUIRE_311 = $(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11) and "make: this target needs Python >= 3.11 (found " + sys.version.split()[0] + "); create .venv")'

test:
	@$(REQUIRE_311)
	d=$$(mktemp -d) && trap 'rm -rf "$$d"' EXIT && cd "$$d" && PYTHONDONTWRITEBYTECODE=1 $(PYTHON) "$(CURDIR)/scripts/ci.py" test

test-security:
	@$(REQUIRE_311)
	d=$$(mktemp -d) && trap 'rm -rf "$$d"' EXIT && cd "$$d" && PYTHONDONTWRITEBYTECODE=1 $(PYTHON) "$(CURDIR)/scripts/ci.py" test --names tests.test_security

lint:
	@$(REQUIRE_311)
	@$(PYTHON) -m ruff --version | grep -qx "ruff $$(sed -n 's/^ruff==//p' .github/requirements-ci.txt)" || { echo "make lint: install .github/requirements-ci.txt into .venv"; exit 1; }
	$(PYTHON) scripts/ci.py lint
	$(PYTHON) -m ruff format --check scripts/ci.py tests/test_ci.py
	-uv run --frozen --no-sync ty check --exit-zero run.py sdk tests
```

`make automation-check`, `automation-dry-run`, `syntax-check`, `git-check` and `pre-commit` are unchanged. `uv run
--frozen --no-sync` leaves `.venv` and `uv.lock` alone (it would create `.venv` only if it were missing).

### 2.7 CI shape (`.github/workflows/bazel-tests.yml`, B4)

- `on: workflow_call` with inputs `operating-systems` and `python-versions` (strings, defaults as in
  `python-tests.yml`) and, from B7, boolean `include-windows` (default false). `permissions: contents: read`.
- Job `plan` (ubuntu-latest, `timeout-minutes: 5`): one `run:` step reads the inputs from `env:` and writes the OS list
  without `windows-latest` (unless `include-windows`) to `$GITHUB_OUTPUT`; it fails with a clear message if the list is
  empty. No matrix `exclude` is relied on.
- Job `bazel` (`needs: plan`, `timeout-minutes: 25`, `fail-fast: false`, matrix from the plan output and
  `python-versions`). Steps:
  1. Refuse tags: `if: github.ref_type == 'tag'` → `exit 1`.
  2. `actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803` with `persist-credentials: false`.
  3. `actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1`, `pip install -r .github/requirements-ci.txt`,
     then the oracle `python scripts/ci.py test --verbose > "$RUNNER_TEMP/ci.txt" 2>&1`.
  4. Write to `$GITHUB_ENV`: `BAZELISK_HOME=$RUNNER_TEMP/bazelisk` (a fresh directory, so Bazel is downloaded and
     verified); `BAZELISK_VERIFY_SHA256` chosen by `$RUNNER_OS-$RUNNER_ARCH` (`Linux-X64` → value of
     `bazel-9.3.2-linux-x86_64.sha256`, `macOS-ARM64` → `bazel-9.3.2-darwin-arm64.sha256`, `Windows-X64` →
     `bazel-9.3.2-windows-x86_64.exe.sha256`, literal values copied in B4 after checking the `.sig` files);
     `CFG="--config=ci --config=ci_linux|ci_macos --config=py311|py313"`.
  5. `bazel-contrib/setup-bazel@104e3eea9447cdaa4b00d0fb46191a3985d26ab3` with `bazelisk-version: ""`, `token: ""`,
     `bazelisk-cache: false`, `disk-cache: false`, `external-cache: false`, `repository-cache` listing `MODULE.bazel`,
     `MODULE.bazel.lock`, `requirements_lock.txt`, `bazelrc: common --repo_contents_cache=`, and
     `cache-save: ${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}`.
  6. `test -z "${BAZELISK_GITHUB_TOKEN:-}"`; then `rc="$(bazel info $CFG repository_cache)"`,
     `tl="$(bazel info $CFG bazel-testlogs)"`, `test -n "$rc"`, `test -n "$tl"`, `rm -rf -- "$rc/contents"` and
     `echo "TESTLOGS=$tl" >> "$GITHUB_ENV"`, each on its own line so a failure stops the job.
  7. `bazel test $CFG //tests:all`; on Linux also `bazel test $CFG //tests:sandbox_canary`.
  8. The two queries below must print nothing (exact syntax confirmed on the first run).
  9. `python scripts/bazel_parity.py --ci-log "$RUNNER_TEMP/ci.txt" --testlogs "$TESTLOGS"`.
  10. `if: failure() && env.TESTLOGS != ''`: `actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9` with
      `path` exactly `${{ env.TESTLOGS }}/**/test.log` and `${{ env.TESTLOGS }}/**/test.xml`.

```
bazel query $CFG 'kind("py_test", //...) except attr(main, "bazel_main\.py$", //...) except //tests:sandbox_canary'
bazel query $CFG 'attr(tags, "(^|[\[, ])(requires-network|no-sandbox|local|external)([\],]|$)", //...)'
```

Policy tests assert: every `bazel` invocation except `bazel version` uses `$CFG`; `CFG` is exactly one of
`--config=ci --config=ci_linux --config=py311`, `--config=ci --config=ci_linux --config=py313`,
`--config=ci --config=ci_macos --config=py311` or `--config=ci --config=ci_macos --config=py313`; every `--config` name
has a `common:` line in `.bazelrc`; the exact setup-bazel inputs, with `bazelrc` exactly `common --repo_contents_cache=`;
`BAZELISK_HOME`; the empty-token step; the guarded `TESTLOGS`; the secret-flag deny list (`PSS_`, `NIM_API_KEY`,
`SMTP_`, `--test_env`, `--action_env`, `--host_action_env`, `--repo_env`, `--client_env`, `--remote_`, `--bes_`,
`--build_event_`, `--google_credentials`, `--credential_helper`) finds nothing; no `github.token`, no `secrets.`, no
`continue-on-error`; the exact upload paths; the tag refusal.

## 3. How slices run

- The owner adds the entries from [bazel-migration-queue-entries.json](bazel-migration-queue-entries.json) to
  `automation/queue/slices.json` in `chore(queue)` PRs. Before merging each queue PR, on a clean checkout of its
  committed branch (`git status --porcelain` prints nothing; the supervisor's dirt gate stops a dry run on uncommitted
  changes with exit 2), the owner runs `git diff --check origin/main...HEAD` and
  `python3 automation/supervisor/run_next.py --dry-run`. A slice never edits `automation/queue/slices.json`.
- **Merge each slice before the next supervisor run**, and use `--autonomous-limit 1` for every Bazel and workflow
  slice. The supervisor marks a slice `done` when it accepts the handoff, not when it merges.
- **Owner-run slices** (`b1-spike-findings`, `b1-bazel-bootstrap`, `b2-pip-hub`, `b9-rules-python-bump`) are never
  set to `queued`. Their first validation is `manual owner-run slice: an agent must stop without changes` and their
  notes start with an instruction to stop. The owner does the work on a branch without `run_next.py` (no handoff
  timeout applies), merges it, then sets the slice `done` in the next `chore(queue)` PR. A slice that changes modules
  or extensions runs `bazel mod deps --config=fetch --lockfile_mode=update` before the owner fetch and commits
  `MODULE.bazel.lock`. Before merging, the owner runs the scope check below on the slice branch and pastes its output in
  the PR body; the supervisor never checks these slices. In the `chore(queue)` PR that marks the slice `done`, the owner
  also adds a handoff file `automation/handoffs/<YYYYMMDDTHHMMSSZ>-<slice_id>.json` that is valid against
  `automation/schemas/handoff.schema.json` (`status` `done`, `summary`, `files_touched`, the exact
  `validations_passed` strings, `proof_level` `doc-only` for the spike and `build-tested` otherwise, `repo_clean_status`
  `clean`, `git_mirror_status` `not-relevant`, at least one `residual_risks` entry, empty lists for
  `validations_failed`, `missing_proof_levels`, `contract_status_changes` and `dirty_paths_outside_scope`, a
  `recommended_next_slice` and reason, and a `timestamp`), so the next
  agent slice's context shows what the owner slice produced. Check it with
  `python3 -c 'import sys; sys.path.insert(0, "."); from pathlib import Path; from automation.supervisor import policy; policy.load_handoff(Path(sys.argv[1]), Path("automation/schemas/handoff.schema.json"))' <file>`.

```bash
# Owner scope check for an owner-run slice: run on the slice branch before merge, with the entry already in the queue.
SLICE=b2-pip-hub
git diff --name-only origin/main...HEAD | python3 -c '
import json, sys
sys.path.insert(0, ".")
from automation.supervisor import policy
entry = {s["slice_id"]: s for s in json.load(open("automation/queue/slices.json"))["slices"]}[sys.argv[1]]
paths = [line.strip() for line in sys.stdin if line.strip()]
outside = policy.out_of_scope_paths(paths, entry["allowed_paths"], [])
print("files %d/%d; outside scope: %s" % (len(paths), entry["max_files_changed"], outside or "none"))
sys.exit(1 if outside or len(paths) > entry["max_files_changed"] else 0)
' "$SLICE"
```

- **Owner fetch** (before any agent Bazel slice, in each new worktree, after each pin bump; `make bazel-fetch` from
  B3): `bazel build --config=fetch --nobuild //...` and `bazel build --config=fetch --config=py311 --nobuild //...`;
  once `tests/BUILD.bazel` exists also `bazel test --config=fetch --nobuild //tests:all` with and without
  `--config=py311`. It never updates the lockfile. `b2-pip-hub`'s `build_test` covers every hub package, so later
  agent slices only reference repositories this fetch already materialized; an agent slice that would add a new
  external label stops and reports instead.
- **CI evidence log.** `docs/workflows/bazel-ci-log.md` is owner-maintained between slices, in owner `docs(ci-log)`
  PRs; only `b4-postmerge-shadow` (creates it) and `b6-blocking-bazel-job` edit it inside a slice.
- **Workflow slices** (B4, B6, B7, `b9-retire-pip-legs`): exact files only; `scripts/ci.py` may only gain a
  `WORKFLOWS` name; `tests/test_ci.py` and `tests/test_build_hygiene.py` may only gain assertions; `daily-run*.yml`
  and `release.yml` are never in scope; owner review before merge.
- **Dispatch validation.** `manual owner dispatch of postmerge-ci.yml on the branch is green` means the owner
  runs `gh workflow run postmerge-ci.yml --ref <branch>` (Windows and Python 3.11 before merge). An agent never
  invokes `gh workflow run`. Dispatched runs never save a cache.
- `manual owner rerun of the Bazel validations` (B1 to B3): the owner repeats the Bazel validations before merge,
  because Bazel results are self-reported until CI runs them. `manual V-REL release parity` is the section 5 procedure.
- **End-to-end run before every handoff:** `make test`, `make lint`, `python scripts/ci.py repository`,
  `make automation-check`; Python 3.11:
  `cd "$(mktemp -d)" && uv run --no-project --isolated --python 3.11 --with-requirements <repo>/.github/requirements-ci.txt python <repo>/scripts/ci.py test`;
  from B2 also `bazel test //tests:all` and `manual bazel parity`.
- Roll back by reverting whole slice PRs in reverse dependency order.

### Spike items (answered by `b1-spike-findings` unless noted)

1. Runfiles root on `sys.path` at runtime.
2. Children inherit `PYTHONSAFEPATH`; `sdk.pvp` is stdlib-only for the `test_pvp_safety` child.
3. The explicit-init flag works.
4. `MODULE.bazel.lock`, including the pip extension `facts`, is identical per OS (Linux at the first B4 run, Windows in
   B7); a difference would fail CI in `error` mode.
5. Sandbox network flag on macOS.
6. `/usr/bin/git` inside `darwin-sandbox` (B2 entry).
7. Cold start and download sizes (CI at B4 exit).
8. Windows git on the strict PATH (B7 dispatch).
9. `<repository_cache>/contents` stays empty under `--config=ci`; a corrupted cache entry is refetched.
10. How rules_python 2.4.6 fetches and builds the `ratelimit` sdist (source reading first; any build in a container or
    with a throwaway `HOME`); decides Q-11.
11. `validate_test_main` accepts `bazel_main.py`.
12. `--experimental_convenience_symlinks=ignore` leaves no links.
13. What `--repository_disable_download` leaves possible (`ctx.execute`, pip, registry files) and whether `--nofetch`
    or `PIP_NO_INDEX=1` closes it; whether a fresh output base resolves registry files and repositories from a shared
    repository cache filled from another workspace path. The answer goes into `validation.md`.
14. Record `bazel mod graph`.
15. `bazel build //...` on a filegroup-only graph exits 0.
16. `--strategy=TestRunner=darwin-sandbox` works on macOS 26.
17. `linux-sandbox` works on `ubuntu-latest` (first B4 run; Q-16).
18. After the owner fetch, a fresh shell without network passes `bazel build //...` and `--config=py311` (checked by
    `b2-pip-hub`), then `bazel test //tests:all` and `--config=py311` (checked by every B2 agent slice), also in a second
    worktree.

### Timeline

Earliest dates, **assuming the Q-18 default** (owner dispatches on idle days); every date after B4 depends on it.

| Milestone | Earliest | Gate |
|---|---|---|
| B0 queued | 2026-11-12 | Q-1a, Q-5, Q-14 |
| `b0-dependency-lock` | 2026-11-17 | uv 0.13.7 is 14 days old |
| B1 spike | 2026-11-17 | Q-8, Q-9 |
| `b1-bazel-bootstrap` | 2026-11-19 | Bazel 9.3.2 is 14 days old |
| B2, B3 | 2026-11-20 to 11-26 | spike items 1, 2, 5, 6, 10; Q-11 |
| B4 shadow live | 2026-11-27 | Q-10, Q-12, Q-15, Q-17, Q-18 |
| B4 exit | 2026-12-04 (timebox 2026-12-18) | 7 days, 5 SHAs |
| B6 exit | 2026-12-18 (timebox 2027-01-01) | 14 days, 10 runs |
| B7, B8 | from 2026-12-18 | B6 exit; Q-1b |

## 4. Phases and slices


### B0: Prerequisite fixes (no Bazel files, no `run.py`/`sdk/` edits)

Close the `.device` exposure, make `make test` and `make lint` hermetic and contract-safe, remove source-tree escapes and silent skips, and add a hashed lock. **Entry:** `bazel-migration-roadmap-docs` (already queued on `main`) merged this document; the owner approved it and answered Q-1a and Q-5. **Exit:** all 7 merged; V-SUP passes; `make test` prints `Ran 945+n tests ... OK (skipped=1)` without touching `.device`; `uv lock --check` passes. The slices form one chain, so no two touch the same file concurrently.

#### `b0-make-hermetic-local`: Run local tests from a temporary CWD and make make lint mirror CI

Priority 400 · domain `tests` · status `queued` · depends_on: `bazel-migration-roadmap-docs`

- **files (each is also an allowed path):** `Makefile`, `tests/test_pvp_sanitized_inputs.py`, `tests/test_build_hygiene.py` (new), `docs/workflows/tachikoma-automation.md`
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 6
- **required_validations:** `make syntax-check`, `make test`, `make test-security`, `make lint`, `make automation-check`, `git diff --check`
- **notes:** Makefile: the `PYTHON`, `REQUIRE_311`, `test`, `test-security` and `lint` recipes of section 2.6 (absolute interpreter; never `$(abspath)` on the fallback; guard only on those three targets). `tests/test_pvp_sanitized_inputs.py:114-117` reads its 4 sdk files relative to `os.path.abspath(__file__)`. New checkout-level `tests/test_build_hygiene.py` (never a Bazel target): `make -n -C <root> test` from a temp CWD shows an absolute interpreter, `mktemp -d` and `scripts/ci.py test`; `make -n lint` shows the ratchet and the format check; the 8 contract targets exist; `tests.test_pvp_sanitized_inputs` passes from a temp CWD. Fix the stale Python 3.9 and `hourly-run.yml` notes. Record `uv pip freeze` and `git status` before and after `make lint`.
- **exit check:** `make test` from the repo root prints `Ran 945+n tests ... OK (skipped=1)`, the repo-root `.device` mtime is unchanged, and `make lint` exits 0.

#### `b0-ci-root-and-runner`: Make scripts/ci.py runfiles-safe and run every suite in a temporary CWD

Priority 410 · domain `tests` · status `queued` · depends_on: `b0-make-hermetic-local`

- **files (each is also an allowed path):** `scripts/ci.py`, `tests/test_ci.py`, `tests/__init__.py`
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 5
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `manual V-REL release parity`, `manual owner dispatch of postmerge-ci.yml on the branch is green`
- **notes:** `ROOT = Path(os.path.abspath(__file__)).parent.parent`. `run_tests(names, require_tests=False, verbosity=1)` runs inside a fresh `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)` (unless the CWD is already `$TEST_TMPDIR`) and restores the CWD in a `finally`; `require_tests` fails on 0 tests; `ci.py test --verbose` sets verbosity 2. No release function or constant changes. `tests/test_ci.py:93` passes `PYTHONPATH=ci.ROOT` (joined by `os.pathsep`) to its child; `:107` uses `os.path.abspath`. `tests/__init__.py` refuses to run when the CWD is the repo root and `./.device` exists. Portable regression tests, including: `ci.py test --names tests.test_battle_flow` (constructs `Device()`) started next to a sentinel `.device` leaves it byte-identical with the same mtime.
- **exit check:** A direct `.venv/bin/python scripts/ci.py test` from the repo root leaves `.device` untouched; V-REL passes.

#### `b0-bazel-ignore-hygiene`: Ignore Bazel outputs and personal rc files before any Bazel command runs

Priority 420 · domain `build` · status `queued` · depends_on: `b0-ci-root-and-runner`

- **files (each is also an allowed path):** `.gitignore`, `tests/test_build_hygiene.py`
- **max_files_changed:** 4
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`
- **notes:** Add /bazel-* to .gitignore (not .bazelrc.user: personal Bazel flags belong in ~/.bazelrc, so a stray .bazelrc.user stays visible as untracked dirt). Extend tests/test_build_hygiene.py: bazel-bin is ignored; git check-ignore -q succeeds for .device, config.secrets, passwords.txt, .profiles/x, tachikoma.log, x.har and x.pcap; git ls-files contains none of tools/bazel, tools/bazel.*, .bazeliskrc or .bazelrc.user (each would change which binary or flags run).
- **exit check:** `git check-ignore bazel-bin .device config.secrets` prints all three paths; `.bazelrc.user` is not ignored.

#### `b0-test-repo-root-a`: Stop six test modules from resolving __file__ out of the runfiles tree

Priority 430 · domain `tests` · status `queued` · depends_on: `b0-bazel-ignore-hygiene`

- **files (each is also an allowed path):** `tests/test_action_catalog.py`, `tests/test_capability_inventory.py`, `tests/test_client_snapshot_provider.py`, `tests/test_compat_ledger.py`, `tests/test_progression_dry_run.py`, `tests/test_fleet_dry_run.py`
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 8
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`
- **notes:** In the six modules replace Path(__file__).resolve().parents[1] with Path(os.path.abspath(__file__)).parents[1]. No assertion changes. The regression test is the AST guard added by b0-test-repo-root-b.
- **exit check:** No `Path(__file__).resolve()` call remains in the six files.

#### `b0-test-repo-root-b`: Fix the last resolve() escapes in tests and add the AST guard

Priority 440 · domain `tests` · status `queued` · depends_on: `b0-test-repo-root-a`

- **files (each is also an allowed path):** `tests/test_pvp_safety.py`, `tests/test_tui.py`, `tests/test_ci.py`
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 5
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `manual owner dispatch of postmerge-ci.yml on the branch is green`
- **notes:** Same fix at tests/test_pvp_safety.py:14 and tests/test_tui.py:11; tests/test_pvp_safety.py:160 joins PYTHONPATH with os.pathsep. Add to TestIsolationTests in tests/test_ci.py an AST guard (ast.Call nodes only, so the string literal at test_ci.py:395 passes) that forbids Path(__file__).resolve() and os.path.realpath(__file__) in tests/**/*.py and in an explicit file list that starts as [scripts/ci.py]; a listed file that is missing fails the guard. Record sdk/simulation/game.py:43 in the test as the one known runtime exception. The owner dispatches postmerge-ci.yml on the branch.
- **exit check:** The guard passes and fails on a planted `Path(__file__).resolve()` (checked in its own test).

#### `b0-strict-skip-mode`: Turn dependency skips into failures under TACHIKOMA_STRICT_DEPS=1

Priority 450 · domain `tests` · status `queued` · depends_on: `b0-test-repo-root-b`

- **files (each is also an allowed path):** `tests/test_ci.py`, `tests/test_pvp_safety.py`, `tests/test_build_hygiene.py`, `tests/test_strict_mode.py` (new)
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 6
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `manual owner dispatch of postmerge-ci.yml on the branch is green`
- **notes:** With TACHIKOMA_STRICT_DEPS=1 these fail instead of skipping: missing yaml (test_ci.py:159), missing git binary (test_ci.py:277 and 387), a missing pvp shim (test_pvp_safety.py:36), and missing make or git in tests/test_build_hygiene.py. test_ci.py:566 (RepositoryReleaseSelectionTests) stays a skip when only .git is missing. Unset, behaviour is unchanged. tests/test_strict_mode.py simulates each case with patch. The owner dispatches postmerge-ci.yml on the branch.
- **exit check:** `TACHIKOMA_STRICT_DEPS=1 make test` passes in a full checkout.

#### `b0-dependency-lock`: Declare runtime and CI dependencies once and lock them with hashes

Priority 460 · domain `build` · status `blocked` · depends_on: `b0-strict-skip-mode`

- **entry_condition:** On or after 2026-11-17, when uv 0.13.7 (released 2026-11-03) is 14 days old. The owner then sets status to queued in a chore(queue) PR.
- **files (each is also an allowed path):** `pyproject.toml`, `uv.lock`, `requirements_lock.txt` (new), `tests/test_dependency_locks.py` (new), `docs/RELEASE.md`
- **max_files_changed:** 7
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `uv lock --check`
- **notes:** `[project]` with `name = "tachikoma"`, `version = "0.0.0"` (not the release version), `requires-python = ">=3.11"`, the 7 runtime pins; `[dependency-groups] ci`; `[tool.uv] required-version`; no `[build-system]`. `uv lock`, then `uv export --frozen --group ci --no-emit-project --no-annotate --no-header --format requirements-txt -o requirements_lock.txt`. `tests/test_dependency_locks.py` (offline): the root `tachikoma` package (source virtual) is the only non-registry entry; all others come from `https://pypi.org/simple`; versions and per-package sha256 sets match uv.lock; non-comment lines are only requirements and `--hash` continuations; every package but `ratelimit` has a wheel; pyproject runtime pins equal `requirements.txt`.
- **exit check:** `uv lock --check` exits 0 and `tests.test_dependency_locks` passes.

### B1: Spike, then an inert bootstrap (owner-run)

Answer the spike items before any Bazel file is committed, then make `bazel build //...` pass on a filegroup-only graph. **Entry:** B0 merged; Q-8, Q-9. **Exit:** macOS spike items answered; bootstrap clean under S4; lockfile not rewritten.

#### `b1-spike-findings`: Run the Bazel spike and record its answers in the roadmap

Priority 500 · domain `build` · status `blocked` · depends_on: `b0-dependency-lock`

- **entry_condition:** Owner answered Q-8 and Q-9 (Bazelisk 1.30.0 installed, `uv python install 3.11` done) and will run this slice personally, because the spike fetches and runs repository rules. Never set to queued: the owner does the work on a branch, merges it, then sets status done in a chore(queue) PR.
- **files (each is also an allowed path):** `docs/BAZEL_MIGRATION_ROADMAP.md`, `docs/BAZEL_MIGRATION_DESIGN_NOTES.md`
- **max_files_changed:** 3
- **required_validations:** `manual owner-run slice: an agent must stop without changes`, `make syntax-check`, `make test`, `make lint`, `git diff --check`
- **notes:** AGENT: do not execute this slice; stop and report. Owner-run, in a scratch clone outside the repository. Answer the Spike items of section 3: facts and evidence go to the design notes, plan changes to this document. Item 10 is read from rules_python 2.4.6 source first; any empirical sdist build runs only in a disposable container or with a throwaway `HOME`, never in an agent session. If an answer changes a slice, the owner updates that queue entry in a `chore(queue)` PR. Close with the owner scope check and a handoff file (section 3).
- **exit check:** Every spike item is answered or explicitly deferred to the run that answers it, and any affected queue entry is updated in a chore(queue) PR.

#### `b1-bazel-bootstrap`: Add the pinned, inert Bazel module and rc files

Priority 510 · domain `build` · status `blocked` · depends_on: `b1-spike-findings`

- **entry_condition:** On or after 2026-11-19, when Bazel 9.3.2 (released 2026-11-05) is 14 days old (the owner may pick 9.3.1 earlier). Owner-run: it is the first slice that fetches. Never set to queued: the owner does the work on a branch, merges it, then sets status done in a chore(queue) PR.
- **files (each is also an allowed path):** `.bazelversion` (new), `MODULE.bazel` (new), `MODULE.bazel.lock` (new), `.bazelrc` (new), `.bazelignore` (new), `BUILD.bazel` (new), `tests/test_build_hygiene.py`
- **max_files_changed:** 8
- **required_validations:** `manual owner-run slice: an agent must stop without changes`, `make syntax-check`, `make test`, `make lint`, `git diff --check`, `bazel build //...`, `manual owner rerun of the Bazel validations`
- **notes:** AGENT: do not execute this slice; stop and report. Owner-run. Files exactly as sections 2.2 (no pip hub), 2.3 and 2.4 (root `package()` visibility after any `load()`, exports, and the two filegroups with a literal `SECRET_PATTERNS`). Run the lockfile-then-fetch sequence of section 3, then the validations; commit the lockfile. Hygiene assertions: `release_includes` rejects every Bazel file; lockfile under 1 MiB; no BUILD/`.bzl` in the forbidden directories; secret-flag scan over `.bazelrc`, BUILD, `.bzl` and `Makefile`; no overrides, extra registries or unhashed archives; every `--config=NAME` used has a `common:NAME` line; the glob excludes are present; no `//visibility:public`; exactly one `package(default_visibility = …)` before the first rule in every BUILD file; no `try-import` in `.bazelrc`. Close with the owner scope check and a handoff file (section 3).
- **exit check:** `bazel build //...` exits 0 on macOS; `git status --porcelain` and `git ls-files --others --exclude-standard` are empty; `bazel mod deps` does not rewrite the lockfile.

### B2: Pip hub, libraries, test targets, parity (local)

**Entry:** B1 merged; spike items 1, 2, 5, 6 answered; Q-11 for `b2-pip-hub`. **Exit:** on macOS both `bazel test //tests:all` and `bazel test --config=py311 //tests:all` pass; `manual bazel parity` passes (target count = tracked modules minus the 2 exclusions, 57 of 59 at the time of writing); a temporary target that swallows a `socket.connect` fails.

#### `b2-pip-hub`: Add the hashed pip hub for Python 3.11 and 3.13

Priority 600 · domain `build` · status `blocked` · depends_on: `b1-bazel-bootstrap`

- **entry_condition:** Q-11 decided from spike item 10 (how the ratelimit sdist is built with hashed build dependencies). Owner-run, because it fetches wheels and may build the sdist. Never set to queued: the owner does the work on a branch, merges it, then sets status done in a chore(queue) PR.
- **files (each is also an allowed path):** `MODULE.bazel`, `MODULE.bazel.lock`, `BUILD.bazel`, `tests/test_dependency_locks.py`, `pyproject.toml`, `uv.lock`, `requirements_lock.txt`
- **max_files_changed:** 8
- **required_validations:** `manual owner-run slice: an agent must stop without changes`, `make syntax-check`, `make test`, `make lint`, `git diff --check`, `bazel build //...`, `uv lock --check`, `manual owner rerun of the Bazel validations`
- **notes:** AGENT: do not execute this slice; stop and report. Owner-run. Add the pip hub of section 2.2 with the Q-11 mechanism (default: a `bazel-build` group with setuptools and wheel under the 14-day rule, exported with hashes, `--no-build-isolation`). The lock test also requires the sdist's build requirements to be hashed. Add a `build_test` over every package in `requirements_lock.txt` for both versions. Run the lockfile-then-fetch sequence; commit the lockfile; confirm both builds pass without network in a second worktree. Close with the owner scope check and a handoff file (section 3). Touch `pyproject.toml`, `uv.lock`, `requirements_lock.txt` only for option a.
- **exit check:** With downloads disabled and no network, `bazel build //...` passes for both Python versions in two worktrees.

#### `b2-libraries`: Add the sdk, run, ci and provisioning libraries

Priority 610 · domain `build` · status `queued` · depends_on: `b2-pip-hub`

- **files (each is also an allowed path):** `sdk/BUILD.bazel` (new), `scripts/BUILD.bazel` (new), `BUILD.bazel`
- **max_files_changed:** 5
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `bazel build //...`, `manual owner rerun of the Bazel validations`
- **notes:** `//sdk` (one `py_library`, scenario JSON as data, deps requests, urllib3, ratelimit, xmltodict), `//:run_lib`, `//scripts:ci_lib`, `//scripts:provision_account_secrets_lib` (never a `py_binary`), a `build_test` over all of them, and `package()` visibility (after the `load()` lines, before any rule) in `sdk/` and `scripts/`. Only `@pip_deps` packages the hub already builds; a new external label means stop and report.
- **exit check:** `bazel build //...` exits 0.

#### `b2-test-main-macro`: Add the audit-hooked test main, the macro and the first test targets

Priority 620 · domain `tests` · status `queued` · depends_on: `b2-libraries`

- **files (each is also an allowed path):** `tests/defs.bzl` (new), `tests/bazel_main.py` (new), `tests/BUILD.bazel` (new), `tests/test_bazel_main.py` (new)
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 6
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `bazel test //tests:all`, `manual owner rerun of the Bazel validations`
- **notes:** `tests/defs.bzl` and `tests/bazel_main.py` as section 2.5. `tests/BUILD.bazel`: `package()` visibility after the `load()` lines, a literal `SECRET_PATTERNS` equal to the root one, `:support`, `:test_sources`, `:pvp_fixtures`, and targets for the 5 Windows-smoke modules, `test_game_identity` and `test_bazel_main`, with the data of section 2.4. `tests/test_bazel_main.py` (subprocess, fake `TEST_SRCDIR`/`TEST_TMPDIR`): a swallowed connect exits 1; zero tests exit 1; CWD is `TEST_TMPDIR`; no `.device` in the source tree; a missing `TEST_TMPDIR` fails.
- **exit check:** `bazel test //tests:all` passes the 7 targets.

#### `b2-all-test-targets`: Add a test target for every covered module, with its data

Priority 630 · domain `tests` · status `queued` · depends_on: `b2-test-main-macro`

- **files (each is also an allowed path):** `tests/BUILD.bazel`, `BUILD.bazel`, `tests/test_build_hygiene.py`
- **max_files_changed:** 5
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `bazel test //tests:all`, `bazel test --config=py311 //tests:all`, `manual owner rerun of the Bazel validations`
- **notes:** Remaining targets with the section 2.4 data; no target for `test_live_auth` or `test_build_hygiene`; no `test_suite`. Hygiene: one `tachikoma_unittest` line per covered tracked module and none for the two exclusions; no `py_test(` outside `tests/defs.bzl` except `sandbox_canary`; no `requires-network`, `no-sandbox`, `local` or `external` tag; the macro's main is still `//tests:bazel_main.py`; the two `SECRET_PATTERNS` lists are equal. Later slices only add hygiene assertions.
- **exit check:** Both Bazel validations pass; the coverage assertion passes under `make test`.

#### `b2-parity-tool`: Add the per-module parity tool and the Linux sandbox canary

Priority 640 · domain `tests` · status `queued` · depends_on: `b2-all-test-targets`

- **files (each is also an allowed path):** `scripts/bazel_parity.py` (new), `scripts/BUILD.bazel`, `tests/test_bazel_parity.py` (new), `tests/sandbox_canary.py` (new), `tests/BUILD.bazel`, `tests/test_ci.py`
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 8
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `bazel test //tests:all`, `manual bazel parity`, `manual owner dispatch of postmerge-ci.yml on the branch is green`, `manual owner rerun of the Bazel validations`
- **notes:** `scripts/bazel_parity.py` (stdlib; `.venv/bin/python` locally) implements the Parity rule of section 5, `--expect-targets N`, and resolves relative paths against `BUILD_WORKING_DIRECTORY`; `bazel_parity_lib`; tests use real `--verbose` output plus swap, count and missing-module cases. `tests/sandbox_canary.py` plus a `manual`, `ci_canary`, Linux-only target: a `-I` child sees only the `lo` interface. Add `scripts/bazel_parity.py` to the AST guard list and `//scripts:bazel_parity_lib` to `test_ci`.
- **exit check:** `manual bazel parity` prints parity OK for 3.13 and 3.11 on macOS.

### B3: Developer entry points and docs

**Entry:** B2 merged. **Exit:** `make bazel-build` and `make bazel-test` work; `make -n bazel-fetch` shows the owner-fetch commands, and the owner runs `make bazel-fetch` once before merge (the agent never runs it); `validation.md` appears untruncated in a context bundle.

#### `b3-make-bazel-targets`: Add Bazel make targets and the validation guide

Priority 700 · domain `docs` · status `queued` · depends_on: `b2-parity-tool`

- **files (each is also an allowed path):** `Makefile`, `docs/workflows/validation.md` (new), `AGENTS.md`, `tests/test_build_hygiene.py`
- **max_files_changed:** 6
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `make automation-check`, `bazel test //tests:all`, `manual owner rerun of the Bazel validations`
- **notes:** Do not run `make bazel-fetch` (it re-enables downloads; the owner runs it once before merge); check it with `make -n bazel-fetch`. `make bazel-build`, `make bazel-test`, `make bazel-fetch` (the owner fetch of section 3; the only target that downloads; never updates the lockfile). `docs/workflows/validation.md` under 2,500 characters, Bazel rules first: the strings and their Bazel equivalents; no tests from the repo root; no secrets through any Bazel env flag (`bazel clean --expunge` if it happened); agents never create or edit `.bazelrc*`, personal flags go in `~/.bazelrc`; never `unittest discover -s tests` or a test script from the repo root; new test modules need a BUILD line; agents never dispatch workflows; the Bazelisk version. One-line pointer in `AGENTS.md`.
- **exit check:** `make -n bazel-fetch` shows the owner-fetch commands and the owner ran `make bazel-fetch` once before merge; `python3 automation/context/build_context.py --slice-id r5-fleet-dry-run-command` shows docs/workflows/validation.md untruncated.

#### `b3-docs-tables`: Add Bazel equivalents to the release guide and fix a stale test count

Priority 710 · domain `docs` · status `queued` · depends_on: `b3-make-bazel-targets`

- **files (each is also an allowed path):** `docs/RELEASE.md`, `docs/auth-operations.md`
- **max_files_changed:** 3
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`
- **notes:** Add a Bazel-equivalents table to the local gate in docs/RELEASE.md, keeping the existing lines. Replace the stale "40 unit tests" at docs/auth-operations.md:160.
- **exit check:** Both docs pass git diff --check.

### B4: CI shadow (post-merge, non-blocking)

**Entry:** B3 merged; Q-10, Q-12, Q-15, Q-17, Q-18; owner reviewed both slices. **Exit (Q-18 default):** green `bazel-shadow` on all cells on at least 7 distinct days, covering at least 5 distinct main SHAs (push runs, or an owner dispatch on main on a day without a push); Linux canary green; the cache saved by the first main push contains no `contents/` directory; cold start under 15 minutes; all recorded in `docs/workflows/bazel-ci-log.md`. Timebox: 21 days, then the owner decides from the log.

#### `b4-bazel-reusable-workflow`: Add the reusable Bazel test workflow under the workflow policy

Priority 800 · domain `ci` · status `blocked` · depends_on: `b3-docs-tables`

- **entry_condition:** Owner reviewed this workflow slice and answered Q-10, Q-12, Q-15, Q-17 and Q-18. Run with --autonomous-limit 1; owner review before merge.
- **files (each is also an allowed path):** `.github/workflows/bazel-tests.yml` (new), `scripts/ci.py`, `tests/test_ci.py`, `tests/test_build_hygiene.py`, `.github/workflows/README.md`
- **max_files_changed:** 6
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `python scripts/ci.py repository`, `bazel test //tests:all`, `manual V-REL release parity`, `manual owner dispatch of postmerge-ci.yml on the branch is green`
- **notes:** `bazel-tests.yml` exactly as section 2.7. `scripts/ci.py` only gains `"bazel-tests.yml"` in `WORKFLOWS`. `tests/test_ci.py` gains the section 2.7 policy assertions on the workflow (including the secret-flag deny list); `tests/test_build_hygiene.py` gains the ones that read `.bazelrc`. Update the workflows README, including its "No Rust, Bazel" sentence. The PR body shows `git diff -U0 origin/main -- scripts/ci.py tests/test_ci.py`. The dispatch does not call the new workflow yet.
- **exit check:** `python scripts/ci.py repository` and actionlint pass; the dispatched run is green (it does not yet call the new workflow).

#### `b4-postmerge-shadow`: Run the Bazel workflow post-merge as a non-gating shadow

Priority 810 · domain `ci` · status `blocked` · depends_on: `b4-bazel-reusable-workflow`

- **entry_condition:** Owner reviewed this workflow slice. Run with --autonomous-limit 1; owner review before merge.
- **files (each is also an allowed path):** `.github/workflows/postmerge-ci.yml`, `.github/workflows/README.md`, `docs/workflows/bazel-ci-log.md` (new)
- **max_files_changed:** 4
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `python scripts/ci.py repository`, `manual owner dispatch of postmerge-ci.yml on the branch is green`
- **notes:** Add a bazel-shadow job to postmerge-ci.yml (uses ./.github/workflows/bazel-tests.yml with OS ["ubuntu-latest","macos-latest"] and Python ["3.11","3.13"], or the Q-12 subset), not in needs. Document it in the workflows README. Create docs/workflows/bazel-ci-log.md with the shadow-run table (date, SHA, cells, parity, cold-start minutes) and the Bazel-only failure table. The owner's dispatch on the branch is the first real run of bazel-tests.yml; record its result.
- **exit check:** The dispatched run shows bazel-shadow green on every cell, no Bazelisk download by setup-bazel, the empty-token check passing and the Linux canary green.

### B5: Automation tests under Bazel (optional)

**Entry:** the owner files the upstream issue (Q-6) and later merges one unsplit inbound sync PR (with the `repo-automation-sync` skill, from a released tag or 40-hex commit, upstream SHA in the PR body). The sync is not a slice. **Exit:** `bazel test //automation:all` passes; `make automation-check` unchanged; the synced-code baseline in V-SUP moves to the sync commit. B6 does not depend on B5.

#### `b5-automation-bazel-targets`: Add Bazel targets for the automation tests

Priority 900 · domain `automation` · status `blocked` · depends_on: `b2-parity-tool`

- **entry_condition:** The owner merged an inbound repo-automation sync PR from a released tag or 40-hex commit that fixes the resolve() escapes, the build_context git check=True calls and the test_harness skips (Q-6).
- **files (each is also an allowed path):** `automation/BUILD.bazel` (new), `tests/defs.bzl`, `tests/BUILD.bazel`, `automation/tests/test_adapter.py`, `tests/test_build_hygiene.py`
- **allowed_paths also include:** `.github/ruff-baseline.json`
- **max_files_changed:** 7
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `make automation-check`, `bazel test //automation:all`
- **notes:** `automation/BUILD.bazel`: one library over `supervisor/policy.py` and `context/build_context.py`, and 3 tests via `tachikoma_unittest` with new `src` and `module` parameters that drive `srcs` and `args` (for example `tests/test_harness.py` and `automation.tests.test_harness`; main stays fixed; `tests/BUILD.bazel` exports `bazel_main.py`), `package()` visibility after the `load()` lines, Linux and macOS only, with the schemas, examples, prompts, fixtures, queue and `supervisor/*.sh` as data. Fix `automation/tests/test_adapter.py:17` locally. Hygiene only gains assertions.
- **exit check:** `bazel test //automation:all` passes 51 tests and `make automation-check` passes 37.

### B6: Bazel blocks PRs (ubuntu / 3.13)

**Entry:** B4 exit; Q-2, Q-2b. **Exit (Q-18 default):** at least 14 days and at least 10 Blocking CI runs on distinct days (PRs or owner dispatches of `blocking-ci.yml` on main) with no Bazel-only false failure, each failure logged in `docs/workflows/bazel-ci-log.md`. Timebox: 28 days.

#### `b6-blocking-bazel-job`: Gate pull requests on Bazel through the existing tests workflow

Priority 1000 · domain `ci` · status `blocked` · depends_on: `b4-postmerge-shadow`

- **entry_condition:** B4 exit evidence recorded in docs/workflows/bazel-ci-log.md and Q-2 (and Q-2b) answered. Run with --autonomous-limit 1; owner review before merge.
- **files (each is also an allowed path):** `.github/workflows/python-tests.yml`, `.github/workflows/blocking-ci.yml`, `.github/workflows/postmerge-ci.yml`, `tests/test_ci.py`, `.github/workflows/README.md`, `docs/RELEASE.md`, `docs/workflows/bazel-ci-log.md`
- **max_files_changed:** 8
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `python scripts/ci.py repository`, `manual owner dispatch of postmerge-ci.yml on the branch is green`, `manual owner dispatch of release.yml on the branch shows tests success`
- **notes:** `python-tests.yml`: boolean input `bazel` (default false) and a `bazel` job (`if: ${{ inputs.bazel }}`, uses `bazel-tests.yml` with the matrix inputs). `blocking-ci.yml` and `postmerge-ci.yml` pass `bazel: true`; `postmerge-ci.yml` drops `bazel-shadow`; `release.yml` is not edited. `tests/test_ci.py` asserts all of that and no `continue-on-error`. Update the README, `docs/RELEASE.md` (protection is still `CI required`) and the CI log.
- **exit check:** A throwaway PR with a failing Bazel target turns CI required red.

### B7: Windows Bazel (experimental)

**Entry:** B6 exit. **Exit:** 10 green runs on distinct days with exactly 5 targets and the git tests executed; Q-4. The pip Windows smoke leg stays blocking.

#### `b7-windows-experimental`: Add an experimental Windows Bazel job post-merge

Priority 1100 · domain `ci` · status `blocked` · depends_on: `b6-blocking-bazel-job`

- **entry_condition:** B6 exit evidence recorded in docs/workflows/bazel-ci-log.md. Run with --autonomous-limit 1; owner review before merge.
- **files (each is also an allowed path):** `.github/workflows/postmerge-ci.yml`, `.github/workflows/bazel-tests.yml`, `tests/BUILD.bazel`, `tests/test_ci.py`, `.github/workflows/README.md`
- **max_files_changed:** 7
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `python scripts/ci.py repository`, `manual owner dispatch of postmerge-ci.yml on the branch is green`
- **notes:** bazel-tests.yml gains a boolean include-windows input that the plan job honours. postmerge-ci.yml gains a bazel-windows job outside needs (operating-systems ["windows-latest"], include-windows: true). If the dispatched run shows git missing from the strict PATH, add env_inherit = ["PATH"] to test_ci only, the one env_inherit the hygiene scan allows. Parity runs with --expect-targets 5. tests/test_ci.py only gains assertions. The owner's dispatch on the branch answers spike item 8.
- **exit check:** The dispatched run executes exactly 5 targets with the git tests not skipped.

### B8: Opt-in `make` wrapper and README

**Entry:** B6 exit; Q-1b; nothing else queued or in progress. **Exit:** `make test` behaves as in B0 unless `TACHIKOMA_BAZEL=1`; pre-commit and gauntlet strings pass both ways.

#### `b8-make-thin-wrappers`: Let make test use Bazel when explicitly opted in

Priority 1200 · domain `build` · status `blocked` · depends_on: `b6-blocking-bazel-job`, `b3-make-bazel-targets`

- **entry_condition:** B6 exit evidence recorded, Q-1b answered, and no other slice queued or in_progress.
- **files (each is also an allowed path):** `Makefile`, `docs/workflows/validation.md`, `tests/test_build_hygiene.py`
- **max_files_changed:** 5
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`, `make test-security`, `manual TACHIKOMA_BAZEL=1 make test`
- **notes:** Opt-in only: with TACHIKOMA_BAZEL=1 and bazel on PATH, make test runs `bazel test //tests:all` then `$(PYTHON) "$(CURDIR)/scripts/ci.py" test --names tests.test_build_hygiene` from a temp CWD, and make test-security runs `bazel test //tests:test_security`; otherwise the B0 recipe. Update docs/workflows/validation.md. tests/test_build_hygiene.py only gains assertions: both branches exist and `make -n test` without the variable equals the B0 recipe.
- **exit check:** `make test`, `TACHIKOMA_BAZEL=1 make test` and `TACHIKOMA_BAZEL=1 PATH=/usr/bin:/bin make test` all pass.

#### `b8-readme-developer-section`: Document developing with Bazel in the README

Priority 1210 · domain `docs` · status `blocked` · depends_on: `b8-make-thin-wrappers`

- **entry_condition:** b8-make-thin-wrappers merged on main.
- **files (each is also an allowed path):** `README.template`, `README.md`
- **max_files_changed:** 3
- **required_validations:** `make syntax-check`, `make test`, `make lint`, `git diff --check`
- **notes:** Add a Developing with Bazel subsection to README.template (install Bazelisk from a package manager or a checksum-verified release; make bazel-fetch; bazel test //tests:all; the pip path stays for people who only run the bot). Then regenerate README.md with .githooks/replace_by_git_vars.py. Template first.
- **exit check:** README.md matches the regenerated template.

### B9: Optional follow-ups (status `deferred`; full entries in [bazel-migration-queue-entries.json](bazel-migration-queue-entries.json))

| slice_id | Title | depends_on | max_files_changed |
|---|---|---|---|
| `b9-rules-python-bump` | Move to rules_python 2.5.x and evaluate pip.parse(uv_lock=...) | `b2-parity-tool` | 8 |
| `b9-sdk-target-split` | Split leaf libraries out of //sdk | `b2-parity-tool` | 3 |
| `b9-test-main-filter-xml` | Support --test_filter and JUnit XML in bazel_main | `b2-parity-tool` | 3 |
| `b9-retire-pip-legs` | Drop duplicate pip test cells where Bazel blocks | `b6-blocking-bazel-job` | 4 |
| `b9-audit-hook-coverage` | Hook DNS and datagram sends in the test audit hook | `b0-ci-root-and-runner` | 4 |
| `b9-dev-bazel-checksums` | Verify the developer Bazel download with tracked checksums | `b3-make-bazel-targets` | 4 |

## 5. Verification essentials

**V-SUP (every phase exit).** `make -n syntax-check test test-security automation-check automation-dry-run git-check
lint pre-commit` resolves; `make syntax-check && make test && make lint && git diff --check` exits 0; the gauntlet and
`.pre-commit-config.yaml` strings name existing targets; `python3 automation/supervisor/run_next.py --dry-run` works
without Bazel on PATH; `git diff --quiet 050f076 HEAD -- automation/supervisor automation/context automation/benchmark
automation/schemas automation/tests/test_harness.py automation/tests/test_benchmark_effectiveness.py
automation/tests/fixtures` (re-baselined after the B5 sync); `find automation -name 'BUILD*' -not -path
automation/BUILD.bazel` prints nothing; no slice handoff lists `automation/queue/slices.json` in `files_touched`.

**S4 dirt check (every Bazel phase exit, also in a `.claude/worktrees/*` checkout).**
`bazel build //... && bazel test //tests:all && git status --porcelain && git ls-files --others --exclude-standard`
prints nothing after the Bazel output.

**Parity rule.** For every module except `test_live_auth` and `test_build_hygiene`: identical normalized test ids
(Bazel ids lose a leading `tests.`; a `setUpClass (<module>.<Class>)` holder counts as `<module>.<Class>.*`), every
target PASSED, Bazel skips exactly `test_ci.RepositoryReleaseSelectionTests.*`, `ci.py` skips exactly
`test_live_auth.TestLiveAuth.*`, and the target set equals the tracked modules minus the exclusions.

**V-REL (every slice that touches `scripts/ci.py`).** Run in a scratch clone, never a worktree of the repository:

```bash
set -euo pipefail
REPO=<path to the developer checkout>
OLD=origin/main; NEW=origin/<slice-branch>      # refs as seen in the clone
PY="$REPO/.venv/bin/python"   # there is no bare python on the Mac
W=$(mktemp -d); trap 'echo "V-REL failed; evidence kept in $W"' ERR
git clone --quiet --no-local "$REPO" "$W/clone"; cd "$W/clone"
git show "$OLD:scripts/ci.py" > "$W/ci-old.py"; git show "$NEW:scripts/ci.py" > "$W/ci-new.py"
git checkout --quiet --detach 1.1.8
for v in old new; do cp "$W/ci-$v.py" scripts/ci.py; "$PY" scripts/ci.py release --tag 1.1.8 --main-ref origin/main --output "$W/r1-$v"; git checkout --quiet -- scripts/ci.py; done
cmp "$W/r1-old/SHA256SUMS" "$W/r1-new/SHA256SUMS"
git checkout --quiet --detach "$NEW" && git tag 9.9.9 HEAD
for v in old new; do cp "$W/ci-$v.py" scripts/ci.py; "$PY" scripts/ci.py release --tag 9.9.9 --main-ref HEAD --output "$W/r2-$v"; git checkout --quiet -- scripts/ci.py; done
cmp "$W/r2-old/SHA256SUMS" "$W/r2-new/SHA256SUMS"
tar -tzf "$W/r2-new/tachikoma-9.9.9.tar.gz" | grep -E '(^|/)(BUILD(\.bazel)?|[^/]*\.bzl|MODULE\.bazel(\.lock)?|\.bazel[a-z]*|uv\.lock|requirements_lock\.txt)$' && { echo "V-REL: Bazel or lock file in the archive; kept $W"; exit 1; }
"$PY" - "$W/r2-new/release-manifest.json" <<'PY'
import json, subprocess, sys
sys.path.insert(0, "scripts"); import ci
paths = [p for p in subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True, check=True).stdout.splitlines() if ci.release_includes(p)]
assert json.load(open(sys.argv[1]))["files"] == len(paths), "manifest count differs"
PY
rm -rf "$W"
```

Also: `git diff -U0 "$OLD" "$NEW" -- scripts/ci.py` touches no line in `release_identity`, `release_includes`,
`build_release`, `VERSION_RE` or the `RELEASE_*` constants; `SourceReleaseTests` and `ReleasePackageDataTests` run
unskipped with `TACHIKOMA_STRICT_DEPS=1`; both runs use the same interpreter.

## 6. Owner questions (safe default applies if unanswered)

| ID | Question | Needed before | Safe default |
|---|---|---|---|
| A-0 | Check the repo-root `.device` rewritten during the reviews | now | treat as exposed; re-provision |
| Q-1a | `make test`/`test-security` via `ci.py test` from a temporary CWD, and `make lint` = the CI ratchet? | B0 | yes |
| Q-1b | Bazel behind `make test` in B8: opt-in or opt-out? | B8 | opt-in (`TACHIKOMA_BAZEL=1`) |
| Q-2 | Bazel blocks PRs (B6)? | B6 | no; B6 stays blocked |
| Q-2b | macOS or 3.11 also block PRs? | B6 | no |
| Q-3 | Declare `langgraph-checkpoint-postgres` in `requirements.txt`? | none | leave |
| Q-4 | Promote Windows Bazel to blocking? | B7 exit | no |
| Q-5 | How to pin `ty`? | B0 | advisory, `uv run --frozen --no-sync`, errors ignored |
| Q-6 | File the upstream repo-automation issue for B5? | during B0 | not filed; B5 stays blocked |
| Q-7 | Keep one pip `ci.py test` leg permanently? | B9 | yes |
| Q-8 | Accept the 14-day rule and best-effort support of non-latest pins? | B1 | yes |
| Q-9 | Install Bazelisk 1.30.0 and `uv python install 3.11`; allow the agent sandbox Bazel's output base and server socket (no network); owner runs every fetch | B1 | yes, owner fetches |
| Q-10 | Should Bazel ever gate releases? | B4 | no; `release.yml` untouched |
| Q-11 | How to build the `ratelimit` sdist with hashed build dependencies | `b2-pip-hub` | option a (hashed backend, `--no-build-isolation`); else B2 stays blocked |
| Q-12 | Actions minutes for 4 macOS-heavy shadow cells | B4 | 4 cells; macOS 3.13 only if minutes matter |
| Q-13 | Early Linux spike answers via Docker or a throwaway workflow? | B1 | no; from the B4 runs |
| Q-14 | Add the B0 entries from the JSON file in the first queue PR, after `git diff --check origin/main...HEAD` and `run_next.py --dry-run` on its clean, committed branch? | B0 | yes |
| Q-15 | Workflow and owner-run slices never run autonomously; agents never dispatch workflows | B4 | yes |
| Q-16 | If `linux-sandbox` is unusable on `ubuntu-latest`: runner `sysctl` step or hook-only? | B4 exit | hook-only, recorded |
| Q-17 | Install the CI oracle from `requirements_lock.txt` with `--require-hashes`? | B4 | no |
| Q-18 | How the B4 and B6 windows get evidence when little else merges | B4 | owner dispatches `postmerge-ci.yml` / `blocking-ci.yml` on main on idle days; counts by distinct days; timeboxes 21 and 28 days |
