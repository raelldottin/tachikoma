# Bazel migration: design notes

Status: Planned — not started; reviewed by security, accuracy and feasibility QA (4 rounds). Date: 2026-11-11, written
against `main` at `050f076`.

Companion to [BAZEL_MIGRATION_ROADMAP.md](BAZEL_MIGRATION_ROADMAP.md) (the execution document) and
[bazel-migration-queue-entries.json](bazel-migration-queue-entries.json) (the proposed queue entries). This file holds
why the plan looks the way it does: current state evidence, rationale, version evidence, the decisions log, the risk
register and the review trace. Bracketed IDs (`SEC-*`, `ACC-*`, `FEA-*`, and the `-R2-`, `-R3-`, `-R4-` round forms)
point at findings in the four QA rounds. The review reports and their per-round resolution tables are QA working files
and are not part of the repository; section 7 keeps the trace for rounds 2 to 4. Citations of the form `research-1` to
`research-5` refer to the migration team's research reports, which are also working files outside the repository;
every repository fact they support is cited here by file and line.

## 1. Rationale by area

### 1.1 Credential-file safety (`.device`)

`Device()` reads and writes `./.device` relative to the CWD (`sdk/device.py:11,35`), and importing `run` writes
`./tachikoma.log`. Any test run from the repo root therefore touches the developer's real device file; this happened
twice during the reviews (A-0). v4 closes every path except two, without editing `sdk/` [SEC-R2-3, SEC-R3-3, FEA-11]:
- `ci.run_tests` runs every suite inside a fresh temporary directory, so CI, `make`, agents and direct
  `python scripts/ci.py test` runs are all covered (`b0-ci-root-and-runner`). A regression test starts a
  `Device()`-constructing module next to a sentinel `.device` and checks bytes and mtime.
- `make test` also runs from `mktemp -d` (`b0-make-hermetic-local`), and Bazel runs from `TEST_TMPDIR`.
- `tests/__init__.py` refuses package-style imports (`python -m unittest tests.x`) when the CWD is the repo root and a
  `.device` exists there. `ci.py test` discovery imports top-level modules from a temporary CWD, so it is unaffected.
- The protection is against writes and CWD-relative reads; Bazel's sandboxes do not hide the host filesystem, so
  `--sandbox_block_path` (verified to exist in 9.3.2, ACC r2) is offered for `~/.bazelrc`.
- Only partly covered: `python -m unittest discover -s tests` from the repo root, and not covered: running a test file
  as a script. On Python 3.13 (ACC r4), discovery imports the `tests` package only through modules that use
  `from tests.… import`, so the guard stops those modules, while discovery records their import errors and still runs
  the modules that never import `tests.*`, some of which construct `Device()` (the SEC r3 measurement on 3.9.6 showed
  no import at all, but 3.9 cannot run the suite) [SEC-R3-3, ACC-R4-2]. `validation.md` forbids both forms from the
  repo root, and A-0 removes the hazard class: after re-provisioning, no real `.device` stays in the checkout. Per-module `setUpModule` fixtures
  were considered and not adopted, because they would touch many test modules for a case A-0 already removes.
- `run_tests` uses `TemporaryDirectory(ignore_cleanup_errors=True)` and restores the CWD in `finally`, so an open
  handle on Windows cannot turn a green suite into a crash [FEA-R3-05].

### 1.2 Downloads and repository rules on the owner's Mac

Repository rules are not sandboxed and run with the owner's `HOME`. v3 makes the safe path the default
[SEC-R2-2, FEA-R2-04, ACC-R2-5]:
- The tracked rc denies `ctx.download` for every command (`common --repository_disable_download`); `--config=fetch`
  re-enables it, and `--config=ci` includes `--config=fetch` (expanded in place after the rc `common` lines, so the
  later `--norepository_disable_download` wins; `ConfigExpander.java` @9.3.2, ACC r3). What the flag does and does not
  do [ACC-R3-5]: a checksummed `ctx.download` already in the repository cache is served before the check
  (`DownloadManager.java:264-273` vs `:331-333`), so "owner fetches once, agent runs offline" works for wheels and
  toolchains; unchecksummed downloads such as PyPI index pages are refused, so the pip extension can only evaluate from
  the committed lockfile facts; Bzlmod registry files are fetched regardless (`downloadAndReadOneUrlForBzlmod`,
  ~395–398), hash-checked against `registryFileHashes`, so a cold cache still touches the network but runs no code.
- The flag does not stop `ctx.execute` (Bazel 9.3.2 help text: network access "is not completely disabled"), and it
  does not stop repository rules from re-running when a new output base re-materializes repositories. Each worktree has
  its own output base. So the owner runs the fetch in each new worktree before an agent uses it, and the slices that
  change fetched content are owner-run (D-28).
- The owner fetch is analysis-level in both Python configurations (`bazel build --config=fetch --nobuild //...`, the
  same with `--config=py311`, and the `bazel test … --nobuild //tests:all` pair once tests exist), because a pattern
  `bazel fetch` may not materialize registered toolchains or the 3.11 wheel repositories [FEA-R3-01]. `b2-pip-hub`'s
  `build_test` covers every hub package, so no later agent slice references an unfetched repository (for example
  `xmltodict` in `b2-libraries`). Spike item 18 and the B2 exit checks confirm a no-network run in a second worktree.
- Lockfile: in `error` mode Bazel refuses a missing or outdated `MODULE.bazel.lock` and asks for
  `bazel mod deps --lockfile_mode=update` (`BazelLockFileFunction.java:130-150`,
  `SingleExtensionEvalFunction.java:274-283, 509-510` @9.3.2). Owner-run slices that create or change modules or
  extensions therefore run `bazel mod deps --config=fetch --lockfile_mode=update` before the fetch. `update` is never
  in `common:fetch`, because `common:ci` includes it and CI must stay in `error` mode [ACC-R3-1].
- No workspace `try-import`: a gitignored `.bazelrc.user` would be invisible to the dirt gate and could undo
  default-deny for every later run; personal flags go in `~/.bazelrc`, and a stray `.bazelrc.user` is untracked dirt
  that halts the supervisor [SEC-R3-5].
- Spike item 13 decides whether `common --nofetch` ("the command will utilize any cached version of the dependency, and
  if none exists, the command will result in failure", `PackageOptions.java:171-179` @9.3.2; behaviour under Bzlmod
  UNVERIFIED) or `common --repo_env=PIP_NO_INDEX=1` (allowlisted by exact value in the secret-flag scan) join the
  defaults. rules_python 2.4.6 downloads wheels through Bazel's downloader by default (`pypi/extension.bzl:264,
  683-705`), so pip should not need an index.
- The agent sandbox needs Bazel's output base and server socket, not network access (Q-9).

### 1.3 The `ratelimit` sdist

`ratelimit==2.2.1` has no wheel on PyPI. Building it runs `setup.py` and, with build isolation, an unpinned build
backend inside an unsandboxed repository rule [SEC-2]. The lock test allows exactly one sdist and, from `b2-pip-hub`,
requires its build requirements to be hashed. Q-11 picks the mechanism after spike item 10, which is answered from
rules_python source first; any empirical build happens in a disposable container or with a throwaway `HOME`
[SEC-R2-5]. Default: a hashed `bazel-build` group plus `--no-build-isolation`.

### 1.4 Locking with uv

uv 0.13.7 refuses a `[project]` table without `version` (reproduced by ACC r2), so `version = "0.0.0"` is set, with the
same "not the release version" note as `MODULE.bazel` [ACC-R2-1]. uv records the virtual root project as a package with
`source = { virtual = "." }`; the lock test exempts exactly that entry [ACC-R2-3, FEA-R2-12]. The export uses
`--no-annotate --no-header`, and the test skips comment lines [ACC-R2-14]. Freshness is checked with `uv lock --check`
[ACC-R2-9]. `uv run --frozen --no-sync` leaves `uv.lock` and `.venv` alone (it creates `.venv` only when missing), and
the `uv run --no-project --isolated --with-requirements` form for the 3.11 run honours the `-c ../requirements.txt`
constraint (both verified on uv 0.13.7 by ACC r2) [ACC-R2-10, ACC-R2-15]. CPython 3.11 is not installed on the Mac, so
Q-9 includes `uv python install 3.11`.

### 1.5 Test identity and parity

`ci.py test` discovers from `tests/` as the top-level directory, so its ids have no `tests.` prefix; Bazel loads
`tests.<module>`. The class-level live-auth skip appears as the pseudo-test `setUpClass (test_live_auth.TestLiveAuth)`.
The parity tool normalizes both and compares ids per module, which also catches a swapped skip that equal counts would
hide [ACC-6, ACC-R2-4]. `test_build_hygiene` (checkout-level: `.git`, `make`, the sync manifest) and `test_live_auth`
(live credentials) have no Bazel target; the coverage assertion runs in every PR through `ci.py test` [FEA-04, SEC-8].

### 1.6 Network defence

Layer 1 on every OS is the audit hook, installed by `ci.run_tests` before the test module loads; `bazel_main.py` is
the only test main, the macro fixes it and `fail()`s on overrides [SEC-6, SEC-R2-4], the hygiene test forbids raw
`py_test` and network-opening tags, and CI runs two queries. Layer 2 on Linux and macOS CI is the sandbox with network
denied. `linux-sandbox`/`darwin-sandbox` are registered only when supported, and requesting an unregistered strategy
fails loudly (`SandboxModule.java:284-336`, `SpawnStrategyRegistry.java:723` @9.3.2, ACC r2); the generic `sandboxed`
alias is what silently falls back. The Linux canary checks that a child sees only `lo` without connecting anywhere.
There is no macOS canary, because a connect probe would be a real connection attempt outside the hook [SEC-7]. Known
gap, as today: `getaddrinfo`, `sendto` and child processes are not hooked (`b9-audit-hook-coverage`) [SEC-21].

### 1.7 CI caching and tool integrity

- The repo contents cache (default `{--repository_cache}/contents`; an empty value disables it,
  `RepositoryOptions.java:57-71` @9.3.2) would store extracted, unverified repository trees inside the directory that
  setup-bazel saves. v3 disables it for every command in the job through setup-bazel's `bazelrc` input, keeps the
  `common:ci` line as a second belt, deletes any `contents/` directory after restore, and requires `$CFG` on every Bazel
  call [SEC-1, SEC-R2-1].
- Bazelisk v1.30.0 returns a binary already in its cache without checking `BAZELISK_VERIFY_SHA256` (`core/core.go`
  ~494–529). `bazelisk-cache: false` only stops setup-bazel restoring a cache, so `BAZELISK_HOME` points at a fresh
  `$RUNNER_TEMP` directory, forcing a verified download [ACC-R2-6]. Release asset names are
  `bazel-9.3.2-linux-x86_64`, `bazel-9.3.2-darwin-arm64` and `bazel-9.3.2-windows-x86_64.exe`, each with `.sha256` and
  `.sig` [ACC-R2-8].
- setup-bazel with `bazelisk-version: ""` returns before any download or API call (`index.js:33-36`); `token` is
  exported unconditionally as `BAZELISK_GITHUB_TOKEN` (`config.js:138-139`), so a step fails the job unless it is empty
  [SEC-3, SEC-R2-6]. Whether the runner passes an explicit empty input rather than the default stays UNVERIFIED until
  the first run; the step makes the check permanent. ACC r3 confirmed at source level that actions/runner applies an
  input default only when the key is absent (`ActionRunner.cs:210-217`).
- `bazel-tests.yml` itself gets the secret-flag scan in `tests/test_ci.py`, `CFG` must equal one of four strings, and
  the setup-bazel `bazelrc` input must equal `common --repo_contents_cache=` [SEC-R3-2]. `TESTLOGS` and the
  repository-cache path are computed in step 6 with assignments on their own lines, so a failed `bazel info` stops the
  job instead of producing `rm -rf /contents` or an empty upload path; the upload runs only when `TESTLOGS` is set
  [SEC-R3-1, ACC-R3-7]. The `plan` job fails on an empty OS list [ACC-R3-8].
- Local commands use `.venv/bin/python`; the Mac has no bare `python` [ACC-R3-6].
- Upload paths are written as `${{ env.TESTLOGS }}/**/test.log` and `.../test.xml`, because `with:` values are not
  shell-expanded [SEC-R2-9].
- `release.yml` never enables Bazel: the `bazel` input of `python-tests.yml` defaults to false, `release.yml` is not
  edited, and `bazel-tests.yml` refuses to run on tags [SEC-4, FEA-06].
- Windows is filtered in a `plan` job, not with a matrix `exclude` (D-29) [ACC-R2-13, FEA-R2-14].

### 1.8 Supervisor fit

- Queue entries are generated as JSON and validated with `policy.load_queue`, `automation.tests.test_harness` and
  `run_next.py --dry-run` on a copy of the queue in a scratch clone of `main` at `7b1dd5a` (2026-11-11: 84 slices
  load, 37 harness tests OK; the dry run selects `bazel-migration-roadmap-docs`, the already-queued slice that lands
  these documents, and `b0-make-hermetic-local` depends on it). `recommended_unblocker` is omitted, because a non-empty value must be
  an existing `slice_id` (`policy.py:248-250`) [FEA-R2-01].
- `done` is set at handoff acceptance, not at merge (`policy.py:269-291`), so the plan says "merge each slice before
  the next supervisor run" and chains B0 linearly; same-file slices can then never overlap [FEA-R2-11].
- Owner-run slices are never `queued`; their first validation tells an agent to stop, the owner works on a branch
  without `run_next.py` (so the 1800-second handoff timeout never applies), merges, and marks the slice `done` in a
  `chore(queue)` PR. Because `policy.evaluate_completion` only runs inside `run_next.py`, the owner runs a pre-merge
  scope check that calls the supervisor's own `policy.out_of_scope_paths` and compares the file count with
  `max_files_changed` [FEA-R4-01]. The `chore(queue)` PR that marks the slice `done` also adds a schema-valid handoff
  under `automation/handoffs/` (a supervisor-owned path an owner PR may write); otherwise
  `build_context.find_previous_handoff` (`build_context.py:263-279`) would skip the owner slice and show the next agent
  an older handoff [FEA-R4-02]. Queue-PR checks run on a clean, committed branch, because the dirt gate stops
  `run_next.py --dry-run` with exit 2 on uncommitted changes (observed by FEA r4) [FEA-R4-03]. If the spike changes a slice, the owner updates that entry in a `chore(queue)` PR
  [SEC-R3-4, FEA-R3-03, FEA-R3-06].
- After the queue PR, `automation/queue/slices.json`, not the JSON fragment, is the authority for exact strings
  [FEA-R3-06]. `docs/workflows/bazel-ci-log.md` is owner-maintained between slices in `docs(ci-log)` PRs [FEA-R3-02].
- `b6-blocking-bazel-job` also gets an owner dispatch of `release.yml` on the branch, so the skipped nested job is shown
  to leave the release `tests` result `success` before merge (`build` is skipped off `main`, so nothing publishes)
  [FEA-R3-04].
- Agents never dispatch workflows; the owner runs `gh workflow run` [SEC-R2-7]. The agent implementing
  `b3-make-bazel-targets` checks `make bazel-fetch` only with `make -n`; the owner runs it once before merge
  [SEC-R4-1]. In `b4-postmerge-shadow` the owner's
  dispatch is the first real run of `bazel-tests.yml`, before merge [FEA-R2-07]. Windows facts come from the B7
  slice's own dispatch [FEA-R2-08].
- B5's upstream prerequisites: the `resolve()` escapes in `run_next.py`, `build_context.py`, `benchmark/run.py` and the two synced tests; the `git … check=True` calls in `build_context.py` (lines 75–76 and 335–337); the `skipTest` at `test_harness.py:250-251` and the conditional assertion at `:841` [ACC-17, ACC-R2-7].
- `validation.md` is a core context doc truncated at 2,500 characters; logs go to `bazel-ci-log.md` [FEA-R2-10].
- Markdown recipe blocks start at column 0 so tab-indented recipe lines pass `git diff --check` [FEA-R2-03].

### 1.9 Visibility and shared lists

Bazel targets are private by default (`site/en/concepts/visibility.md:94-109` @9.3.2) and only `exports_files` entries
are public, while the plan depends across packages everywhere. Every BUILD file therefore declares
`package(default_visibility = ["//:__subpackages__"])` (repository-internal, never public), and the hygiene test forbids
`//visibility:public` [ACC-R3-2]. Starlark requires `load()` statements before any other statement
(`Resolver.java:1280-1292`, `FileOptions.java:108-110` @9.3.2), so `package()` comes after the loads and before the
first rule, and the hygiene check tests exactly that placement [ACC-R4-1]. A BUILD file cannot `load()` another, so `SECRET_PATTERNS` is a literal list in both
the root and `tests/` BUILD files, asserted equal [ACC-R3-3]. B5 adds `src` and `module` parameters to the macro,
because from `//automation` the default `name + ".py"` and `"tests." + name` would point at the wrong file and module
[ACC-R3-4].

### 1.10 Evidence windows

Main pushes are bursty (1 commit on 2026-11-04, 44 on 2026-11-10), and during B4 and B6 the migration itself merges
almost nothing. The exits therefore count distinct days, allow owner dispatches on main on idle days, keep a softer SHA
floor, and end in an owner decision after 21 (B4) or 28 (B6) days (Q-18) [FEA-R2-05]. Dispatched runs never save a
cache.

## 2. Spike: prior evidence

| # | Prior evidence |
|---|---|
| 1 | rules_python appends every runfiles top-level directory, `_main` included (`site_init_template.py:167-205`) |
| 2 | Both bootstraps export `PYTHONSAFEPATH=1` (`stage1_bootstrap_template.sh:288-297`) |
| 3 | Flag spelling verified at 2.4.6 (`python/config_settings/BUILD.bazel:222-227`) |
| 4 | pip and python extensions are `reproducible = True` (`pypi/extension.bzl:633-641`) |
| 5 | `--sandbox_default_allow_network` verified (`SandboxOptions.java:284-292`) |
| 9 | Empty `--repo_contents_cache` disables it (`RepositoryOptions.java:57-71`) |
| 11 | `validate_test_main` exists from 2.3.0 (`python/private/flags.bzl:90-108`) |
| 12 | `--experimental_convenience_symlinks` values `normal`/`clean`/`ignore`/`log_only`; `ignore` neither creates nor cleans links (`BuildRequestOptions.java:253-269`) |
| 13 | `--repository_disable_download` blocks `ctx.download` only (`RepositoryOptions.java:166-177`); `--nofetch` (`PackageOptions.java:171-179`) |
| 16, 17 | Strategies registered only when supported (`SandboxModule.java:284-336`) |
| others | UNVERIFIED until run |

## 3. Version evidence

Pinning rule: **pin exact versions, and only versions at least 14 days old when the pinning slice merges** (owner
question Q-8). rules_python's policy is "in general, only the latest version is supported", and for Bazel it supports
only the latest minor/patch of the active and prior majors. Pinning a release that is no longer the newest (2.4.6 while
2.5.0 exists; Bazel 9.3.1 if used) is therefore **best-effort, not officially supported**. That is the accepted cost of
the 14-day rule [ACC-24]. Bumps are ordinary slices that follow the pin and bump procedure in section 3.1 below.

| Component | Pin | Released | Evidence / how confirmed | Note |
|---|---|---|---|---|
| Bazel (`.bazelversion`) | **9.3.2** | 2026-11-05 | GitHub releases API (re-checked by roadmap author and ACC review: not a prerelease). Bazel 9 is the Active LTS, EOS Dec 2028 (https://bazel.build/release) | 14 days on 2026-11-19, which is `b1-bazel-bootstrap`'s entry date. Fallback 9.3.1 (2026-10-21). Bazel 10 is rc3, not targeted |
| Bazelisk | not pinned in CI; developers install **v1.30.0** (2026-10-20) | 2026-10-20 | https://github.com/bazelbuild/bazelisk/releases/tag/v1.30.0 | CI uses the runner image's preinstalled Bazelisk (research-3 §2 saw 1.28.1 and 1.30.0 on different images), so setup-bazel downloads nothing unverified [SEC-3]. The Bazel binary itself is verified by `BAZELISK_VERIFY_SHA256` with a fresh `BAZELISK_HOME` (section 1.7) |
| `bazel-contrib/setup-bazel` | **0.19.1** = `104e3eea9447cdaa4b00d0fb46191a3985d26ab3` | 2026-10-14 | research-3 §2; ACC review dereferenced annotated tag `9f075053…` to this commit | 0.19.2 (`f34d6a05…`, 2026-11-09) only through the bump procedure (section 3.1) |
| `rules_python` | **2.4.6** | 2026-10-29/30 | BCR `modules/rules_python/metadata.json` lists `…2.4.5, 2.4.6, 2.5.0-rc0…rc4`; 2.5.0 final is GitHub-only (dated 2026-11-11) | 2.5.x is optional later (`b9-rules-python-bump`) |
| Python toolchains | `3.13` (default) and `3.11` | n/a | rules_python 2.4.6 `MINOR_MAPPING` → **3.11.18 / 3.13.18** (`python/versions.bzl:45-53`, ACC) | `.python-version` is 3.13.15, which **is not in** the 2.4.6 runtime manifest (ACC: 3.13.x has .0–.2, .4, .6, .9–.13, .18). Minor pins match setup-python in CI |
| `bazel_skylib` | **1.9.2** | n/a | BCR metadata (re-checked) | `build_test`, `diff_test` |
| `platforms` | **1.1.0** | n/a | BCR metadata (re-checked) | Windows `select` |
| `rules_shell` | **0.8.0**, only if an `sh_test` is ever needed | n/a | BCR metadata | Bazel 9.3.2 has no autoloads (`DEFAULT_INCOMPATIBLE_AUTOLOAD_EXTERNALLY = ""`, ACC-21). No shell rules are planned |
| `aspect_rules_lint` | not adopted | n/a | 2.9.1 in BCR | D-08 |
| `rules_pkg` | not adopted | n/a | 1.3.0 in BCR | D-10 |
| uv (lock producer, outside Bazel) | **0.13.7** produces the lock; `[tool.uv] required-version = ">=0.13.7,<0.14"` [FEA-28] | **2026-11-03** | GitHub releases API (ACC-25); `.venv/pyvenv.cfg` | 14 days on **2026-11-17**, which is `b0-dependency-lock`'s entry date [ACC-25] |

### 3.1 Pin and bump procedure [SEC-20, SEC-12]

Every PR that pins or bumps an action SHA, `.bazelversion`, a `bazel_dep`, rules_python, uv or a locked package records:
the release date (14-day rule) and source (BCR `metadata.json`, GitHub releases API, PyPI JSON); for an action,
`gh api repos/<owner>/<repo>/git/ref/tags/<tag>` dereferenced through `git/tags/<sha>` when annotated, equal to the pin,
plus a check that the commit is on the upstream default branch or a release tag and, for JavaScript actions, a review
of the `dist/` diff; for Bazel modules, the `bazel mod graph` and `registryFileHashes` diffs, produced by the owner with
`bazel mod deps --config=fetch --lockfile_mode=update` and then the owner fetch; for Bazel, the new
`BAZELISK_VERIFY_SHA256` values and the `.sig` verification command.

## 4. Current state evidence (at `050f076`)

| Area | Today | Evidence |
|---|---|---|
| Interpreter | `.venv` is uv-managed with CPython 3.13.15 (uv 0.13.7). `Makefile:2` sets `PYTHON` to the **relative** path `.venv/bin/python` when it exists, otherwise `python3` on PATH, which is 3.9.6 on macOS and cannot run the code [ACC-1] | [`Makefile`](../Makefile) line 2, `.python-version`, `.venv/pyvenv.cfg` |
| Tests | **54** `tests/test_*.py` modules [ACC-5, FEA-21]; `scripts/ci.py test` gives **945 tests, 1 skip (the `test_live_auth` class skip), ~10 s**; `tests.test_ci` alone is ~9.5 s. Every module passes alone. 3 `automation/tests` modules hold 51 tests (37/2/12); only `test_harness` runs in CI | `git ls-files 'tests/test_*.py'`; ACC verification; `automation-check.yml:30` |
| Network guard | Only `scripts/ci.py test` installs the `socket.connect`/`http.client.connect` audit hook (`run_tests`, lines 252–272; it also inserts `ROOT` at `sys.path[0]`, line 263). `make test` is plain `unittest discover` with no guard. The hook does not see `getaddrinfo`, `sendto` or child processes [SEC-21] | [`scripts/ci.py`](../scripts/ci.py) |
| Dependencies | `requirements.txt`: runtime pins plus the optional LangGraph stack, no hashes, `certifi>=`. `.github/requirements-ci.txt` constrains on `requirements.txt` and adds `ruff==0.15.5`, `PyYAML==6.0.3`. **`uv.lock` is a 3-line stub with zero packages** (`requires-python = ">=3.9"`). `pyproject.toml` has only `[tool.ruff]`. **`ratelimit==2.2.1` is sdist-only on PyPI** [SEC-2] | research-1 §4; `https://pypi.org/pypi/ratelimit/2.2.1/json` (checked 2026-11-11 by SEC review) |
| Undeclared imports | `langgraph.checkpoint.postgres` (`pss_langgraph_automation.py:358`), `programbench`, `mitmproxy`, `lldb`, `lief` | research-1 §4 |
| Lint gate | CI: `python scripts/ci.py lint` (exact-count ratchet against `.github/ruff-baseline.json`, 12 F401, ruff 0.15.5) **and** `python -m ruff format --check scripts/ci.py tests/test_ci.py` (`automation-check.yml`). `make lint` runs plain `uv run ruff check …` (exit 1 whenever any finding exists) and `uv run ty check --exit-zero`. Findings depend on which ruff runs: 12 with `.venv` ruff 0.15.5, 1,179 with a PATH ruff 0.17.4 [ACC-19]. `uv run` against the stub lock is reported to re-sync `.venv` and rewrite `uv.lock` (owner's environment notes; UNVERIFIED by the reviews) [FEA-10] | [`Makefile`](../Makefile), `scripts/ci.py:84-101` |
| Contract users of `make` | 52/53/54 queue slices use `make test`/`make syntax-check`/`git diff --check`; the gauntlet slice also requires `make lint` and `make test-security`; `.pre-commit-config.yaml` runs `make lint`, `make test`, `make git-check` [FEA-32] | `slices.json`, `automation/gauntlet/slice_definition.json`, `.pre-commit-config.yaml` |
| CI | `CI required` and `Post-merge result` gates: `if: always()`, `needs: [checks, tests]`, enforced by `check_workflows` (`scripts/ci.py:142-145`). `blocking-ci.yml` calls `python-tests.yml` **with no inputs**, so PRs run ubuntu/3.13 only [ACC-11]. Post-merge runs Ubuntu/macOS/Windows × 3.11/3.13; Windows runs a 5-module smoke subset (`python-tests.yml` lines 41–49) [ACC-15]. **`release.yml` calls the same `python-tests.yml` as its `tests` job** on 3 OS × 2 versions [FEA-06]. `postmerge-ci.yml` has `workflow_dispatch`. The only cache is pip | [workflows README](../.github/workflows/README.md), `release.yml:20-24` |
| Release | `ci.py release` reads git blobs (`ls-tree`/`cat-file`), writes a PAX tar with commit-epoch mtimes and gzip `mtime=0`, a manifest and `SHA256SUMS`. A dirty tree has no effect. Tag `1.1.8` (lightweight, `6442298`) yields only 14 members: no sdk subpackages and no sdk JSON [ACC-10] | [RELEASE.md](RELEASE.md), `scripts/ci.py` lines 175–249 |
| Hermeticity hazards | **9 test files, 10 call sites** use `Path(__file__).resolve()` (`test_action_catalog:19`, `test_capability_inventory:9`, `test_client_snapshot_provider:11`, `test_compat_ledger:24`, `test_fleet_dry_run:46`, `test_progression_dry_run:17`, `test_pvp_safety:14`, `test_tui:11`, `test_ci:107`; `test_ci:395` is inside a string literal) plus `scripts/ci.py:20` and `sdk/simulation/game.py:43` [ACC-12, SEC-15]. `test_pvp_sanitized_inputs.py:114-117` reads **4** sdk files relative to CWD, so `ci.py test` from an empty CWD fails with 1 error [ACC-13, FEA evidence]. Importing `run` writes `./tachikoma.log`. `Device()` writes `./.device`, so **`make test` reads and overwrites the developer's real `.device`** (observed live, A-0). `test_pvp_safety.py:160` joins `PYTHONPATH` with a hard-coded `:`. Silent-skip guards: `test_ci.py:159, 277, 387, 566`, `test_pvp_safety.py:36` | research-4 §2; `grep -n __file__` |
| Supervisor contract | 54 slices (49 done, 5 blocked). The supervisor replays only allowlisted commands: `git diff --check`, plus an unused `make architecture` (`policy.py:56-59`) [ACC-16, SEC-17]; everything else is `run_report_only`, and `manual `-prefixed strings are never replayed. The dirt gate runs `git ls-files --others --exclude-standard` (`policy.py:319-324`). `allowed_paths` match per path component (`policy.py:355-360`). Required slice fields: `slice_id`, `title`, `status`, `priority`, `domain`, `allowed_paths`, `required_validations`, `depends_on`, `max_files_changed`, `notes`; optional `entry_condition`, `recommended_unblocker` [FEA-08]. Highest priority today: 380. `consecutive_autonomous_limit: 2` | `policy.py`, `slice.schema.json`, `slices.json` |
| Synced code | `automation/{supervisor,context,benchmark,schemas}` are synced with `delete_stale: true`; `automation/{examples,prompts}` as templates; single files `automation/README.md`, `automation/reusable-manifest.json`, `automation/tests/test_harness.py`, `automation/tests/test_benchmark_effectiveness.py`, `automation/tests/fixtures/cmatrix.eval.json`, `docs/workflows/repo-automation.md`, `docs/workflows/effectiveness-benchmark.md`, `Tools/repo-automation-sync.sh` and others [ACC-18]. **`Tools/repo-automation-sync.sh` exports from this repo to `default_target`; it cannot pull upstream fixes, and `--check` exits 1 today because the target folder is missing** [FEA-02]. The local supervisor predates plugin 0.2.5 | [`automation/reusable-manifest.json`](../automation/reusable-manifest.json), `Tools/repo-automation-sync.sh` |
| Bazel | None: no `MODULE.bazel`, `BUILD*`, `.bazelrc` or `.bazelversion`; no `bazel-*` entry in `.gitignore`. **Neither `bazel` nor `bazelisk` is installed on the execution Mac** [FEA-05] | `git ls-files`; `command -v bazel bazelisk` |

---

## 5. Decisions log

| ID | Decision | Alternatives | Reason | Evidence |
|---|---|---|---|---|
| D-01 | Bazel **9.3.2** exact (9.3.1 if B1 must land before 2026-11-19) | `9.x`; Bazel 8; Bazel 10 rc | Active LTS to Dec 2028; Bzlmod only; exact pin | https://github.com/bazelbuild/bazel/releases/tag/9.3.2, https://bazel.build/release |
| D-02 | rules_python **2.4.6** | 2.5.0; 2.4.1 | Newest final in BCR; 2.5.0 is days old and BCR has only rc4; best-effort support accepted (Q-8) [ACC-24] | BCR `metadata.json`; rules_python `docs/support.md` @2.5.0 |
| D-03 | Hub `pip_deps` from a hashed `requirements_lock.txt` exported from a real `uv.lock`; sdists only by allowlist with hashed build deps (Q-11) | `pip.parse(uv_lock=)`; the `lock` rule; feeding `requirements.txt` | Fixes the empty-lock trap; gives Bazel hashes; avoids the 2.4.x `uv_lock` marker bug; `requirements.txt` is not a lock and includes psycopg2 [SEC-2] | research-2 §3.3, §7.8; PyPI JSON for ratelimit |
| D-04 | `requirements.txt` stays human-maintained and unchanged | generate it from the lock | Ships in the release and feeds the daily workflows | `scripts/ci.py` `RELEASE_ROOT_FILES` |
| D-05 | **One `py_test` per test file** via a macro; documented exclusions `test_live_auth`, `test_build_hygiene` | one discovery wrapper; per-class targets | Cache precision, per-target data, expressible Windows subset; same hook guarantee per target | research-4 §4, research-1 §7 |
| D-06 | **One `//sdk` library** at first | per-file; per-directory | Per-directory forms cycles; per-file adds ~64 targets for little gain | research-1 §10 |
| D-07 | Hand-written BUILD files, no Gazelle | Gazelle 0.54.0 | ~20 packages; Gazelle adds a Go toolchain and misfires on `scripts/test_captured_*` | research-2 §4 |
| D-08 | `ci.py lint` ratchet stays outside Bazel; `make lint` mirrors it | aspect_rules_lint 2.9.1 | Exact-count ratchet semantics; a checkout check [FEA-01] | research-2 §5, research-3 §7 |
| D-09 | Test main `tests/bazel_main.py` delegates to `scripts/ci.py run_tests` | `sitecustomize`; a copy of the hook | One hook implementation; linted | research-4 H-10 |
| D-10 | Release stays `python scripts/ci.py release` outside Bazel | `pkg_tar`; `tar.bzl` | Cannot build from git blobs or reproduce PAX/gzip/epoch bytes | research-2 §6, research-3 §8 |
| D-11 | Bazel joins the PR gate by a nested call inside `python-tests.yml` **behind a `bazel` input that defaults to false**; `blocking-ci.yml`/`postmerge-ci.yml` opt in; `release.yml` untouched | a new gate `needs` entry; Bazel only post-merge; default true with release opting out | Leaves the gate assertions and branch protection untouched; a forgotten caller fails safe (no Bazel) rather than pulling Bazel into the release gate [SEC-4, FEA-06] | `scripts/ci.py:142-145`; `release.yml:20-24`; GitHub reusable-workflow docs |
| D-12 | `make test` moves to `ci.py test` from a temporary CWD in B0 (first slice); Bazel only opt-in in B8 | parallel forever; opt-out cut-over | Closes the live `.device` exposure first; adds the hook; opt-in keeps agent runs from fetching [FEA-11, SEC-17] | research-4 H-6; observed rewrites (A-0) |
| D-13 | Only the download cache is restored; repo contents cache disabled job-wide through setup-bazel's `bazelrc` input and deleted after restore; Bazelisk cache off and `BAZELISK_HOME` fresh; saves only on main pushes; tags refused; `--nocache_test_results` | disk cache; remote cache; setup-bazel defaults | Restored trees and binaries would not be re-verified; Bazelisk returns a cached binary without checking the hash [SEC-1, SEC-R2-1, ACC-R2-6] | setup-bazel 0.19.1 `config.js`; Bazelisk `core/core.go` ~494–529 @v1.30.0; Bazel 9.3.2 `RepositoryOptions.java:57-71` |
| D-14 | `--experimental_convenience_symlinks=ignore`, `/bazel-*` ignored, `--lockfile_mode=error` (never `off`), lockfile committed | `--symlink_prefix=/`; `update`/`off` mode | Dirt gate; `/` form to be deprecated [ACC-23]; `off` drops registry-hash checks [SEC-5] | `BuildRequestOptions.java:237-248` @9.3.2; command-line reference |
| D-15 | Automation tests join Bazel only after an **owner-run** upstream sync (B5); `make automation-check` never moves | local edits to synced files; an agent sync slice | `delete_stale` drift; an agent must not hold write scope over the supervisor that judges it [SEC-10] | `automation/reusable-manifest.json` |
| D-16 | Windows Bazel is experimental; the pip smoke leg stays authoritative | Windows blocking at B6 | No sandbox; symlink/runfiles caveats; git on the strict PATH UNVERIFIED | research-3 §5 |
| D-17 | setup-bazel **0.19.1** at `104e3eea…`, with `bazelisk-version: ""` and `token: ""` | 0.19.2; preinstalled Bazelisk without the action; a `run:` install with a committed hash | 14-day rule; no unverified Bazelisk download; no token in later steps [SEC-3] | research-3 §2; setup-bazel 0.19.1 `action.yml`, `index.js` (SEC review) |
| D-18 | Explicit `__init__.py` behaviour; namespace `sdk/` kept | add `sdk/__init__.py` | Same package semantics inside and outside Bazel; implicit creation deprecated since 2.2.0 | research-2 §3.4; ACC |
| D-19 | Toolchains pinned by minor (3.11, 3.13), default 3.13 | patch pins | Matches setup-python; 3.13.15 is not in the 2.4.6 manifest | research-2 §3.2; ACC |
| D-20 | `test_build_hygiene` is checkout-level and has no Bazel target | make it Bazel-safe with data | It needs `.git`, `make` and the sync manifest; under Bazel it would only take fallbacks; it blocks PRs through `ci.py test` [FEA-04] | §3.12 |
| D-21 | Parity tool runs with plain Python and compares ids, not only counts | `bazel run`; count-only | No runfiles CWD issue [ACC-9]; detects swapped skips [ACC-6] | Bazel user manual (`run`), ACC |
| D-22 | `ci_policy_inputs`, `repo_docs` and `test_sources` are globs; `repo_docs` and `test_sources` exclude the `.gitignore` secret patterns, and `.bazelignore` lists `.profiles` | explicit lists | New workflows and docs need no root BUILD edit; globs read the working tree and ignore `.gitignore` [FEA-03, FEA-14, SEC-R2-8] | Bazel `glob` docs (ACC-4) |
| D-23 | `--experimental_use_hermetic_linux_sandbox` **not adopted** for now | adopt in `test:ci_linux` | `test_ci` needs host `git` and children need the interpreter; revisit after B4 [FEA-33] | research-4 H-1 |
| D-24 | `validate_test_main=enabled` in `common:ci`, if spike 11 shows it accepts `bazel_main` | leave `auto` | Cheap defence in depth beside `require_tests` [ACC-26, FEA-33] | rules_python 2.4.6 `python/private/flags.bzl:90-108` (ACC) |
| D-25 | No `run.py` or `sdk/` edits; `.device` is protected by CWD isolation in `ci.run_tests` (every caller), `make test`, Bazel's `TEST_TMPDIR`, and a `tests/__init__.py` guard | v1's logging move and `Device(path=)` | Avoids gameplay-adjacent change while closing the direct-run hole [SEC-R2-3] | `sdk/device.py:11,35`; A-0 observation |
| D-26 | Workflow slices: exact-file scopes, add-only policy edits, owner review, never autonomous | directory scopes | Keeps secret-holding and release workflows and the policy code out of reach [SEC-9] | `policy.py:355-360` |

---
| D-27 | Downloads denied by default in the tracked `.bazelrc`; `--config=fetch` (and `--config=ci`) re-enable them | `--config=agent` on every validation string | The plain validation strings become safe without editing them; nothing can forget the flag [SEC-R2-2, FEA-R2-04, ACC-R2-5] | Bazel 9.3.2 `RepositoryOptions.java:166-177` |
| D-28 | Fetch-changing slices are owner-run (`b1-spike-findings`, `b1-bazel-bootstrap`, `b2-pip-hub`, `b9-rules-python-bump`) | an agent fetch with a mid-slice pause | No pause point exists under `handoff_timeout_seconds: 1800`; the owner already holds the network decision [FEA-R2-04, SEC-R2-5] | `slices.json` policy |
| D-29 | `bazel-tests.yml` filters Windows in a `plan` job and builds the matrix from its output | matrix `exclude` on a `fromJSON` matrix; job-level `if` on `matrix` | `exclude` on expression matrices is undocumented; the `matrix` context is not available in a job-level `if`; a filtered list leaves no skipped legs [ACC-R2-13, FEA-R2-14] | GitHub workflow-syntax reference (read 2026-11-11) |
| D-30 | Every Bazel config name has a `common:` line | `test:` lines | `bazel info`/`build`/`query` reject configs defined only for `test` [ACC-R2-2] | `ConfigExpander.java:238-300`, `BlazeOptionHandler.java:91-93` @9.3.2 |
| D-31 | Parity compares normalized ids (strip `tests.`; class-level skip holders) | literal ids | `ci.py` discovers from `tests/` as top level, Bazel loads `tests.<module>` [ACC-R2-4] | `scripts/ci.py:262-266` |
| D-32 | Execution core, design notes and a validated JSON fragment as three artifacts | one 130 KB document | Agents and the owner execute from the core and paste exact strings from the JSON [FEA-R2-06] | FEA-R2-06 |
| D-33 | CI evidence logs live in `docs/workflows/bazel-ci-log.md`, not in `validation.md` | logs in `validation.md` | `validation.md` is a core context doc truncated at 2,500 characters [FEA-R2-10] | `build_context.py:25-32, 328-332` |
| D-34 | `.bazelrc` assertions live in `tests/test_build_hygiene.py` | assert from `tests/test_ci.py` | `test_ci` runs under Bazel and `.bazelrc` is not in its runfiles [FEA-R2-02] | — |
| D-35 | The macro fixes `srcs` and `main` to `Label("//tests:bazel_main.py")` and `fail()`s on overrides | a `main` parameter | Keeps the hook unavoidable even when B5 reuses the macro from another package [SEC-R2-4] | — |
| D-36 | B4/B6 evidence is counted by distinct days, with owner dispatches on idle days and timeboxes (Q-18) | distinct SHAs only; a `schedule` trigger | No workflow change is needed, and a stalled window ends in an owner decision [FEA-R2-05] | merge history 2026-11-04 to 11-11 |
| D-37 | Owner-run slices are never `queued`; first validation tells an agent to stop; the owner closes them with a `chore(queue)` status edit | queue them and trust the sandbox | An agent given the slice would run `--config=fetch` [SEC-R3-4, FEA-R3-03] | `policy.py:269-291`; `handoff_timeout_seconds: 1800` |
| D-38 | `bazel mod deps --config=fetch --lockfile_mode=update` only in owner-run slices; never `update` in a config | `common:fetch --lockfile_mode=update` | `common:ci` includes `fetch`, and CI must stay in `error` mode [ACC-R3-1] | `BazelLockFileFunction.java:130-150` @9.3.2 |
| D-39 | `package(default_visibility = ["//:__subpackages__"])` in every BUILD file; `//visibility:public` forbidden | per-target `visibility`; public | Targets are private by default and the plan depends across packages [ACC-R3-2] | `visibility.md:94-109` @9.3.2 |
| D-40 | Owner fetch = analysis-level `--nobuild` builds (and tests) in both Python configurations; the hub `build_test` covers every lock package | `bazel fetch //...` | A pattern fetch may miss toolchains and 3.11 repositories, and a slice-added label would be unfetched [FEA-R3-01] | FEA-R3-01 |
| D-41 | No workspace `try-import`; personal flags in `~/.bazelrc` | `try-import %workspace%/.bazelrc.user` | A gitignored rc is invisible to the dirt gate and could undo default-deny [SEC-R3-5] | `policy.py:319-324` |

## 6. Risk register

L = low, M = medium, H = high. "Owner lane": **build** (Bazel files and macros), **ci** (workflows and policy),
**tests**, **automation** (supervisor and upstream sync), **release**.

| ID | Risk | L | I | Mitigation | Owner lane |
|---|---|---|---|---|---|
| R-01 | Undeclared inputs (`.resolve()` escapes, `git ls-files`, workflow YAML) give stale cached PASS results | M | H | B0 removes the escapes and the AST guard keeps them out; `data` per target with globs; CI uses `--nocache_test_results` and no disk cache | tests |
| R-02 | The audit hook is lost in a `py_test`, so an unmocked call goes unnoticed | L | H | One main (`tests/bazel_main.py` → `ci.run_tests`); `test_bazel_main.py` proves fail-closed; **mechanical checks**: the hygiene text check (no `py_test(` outside the macro, no network tags, macro still sets `main`) on every PR, and the `bazel query` step in CI [SEC-6]; sandbox network deny with pinned strategy as a second layer (Linux canary). Known gap: `getaddrinfo`/`sendto`/children are not hooked (today too) [SEC-21] | build |
| R-03 | Silent skips under Bazel turn into green results with fewer tests | M | H | `TACHIKOMA_STRICT_DEPS=1`; parity compares test ids and skipped ids per module | tests |
| R-04 | A Bazel command dirties the workspace and stops `run_next.py` | M | H | `.gitignore` first (B0); `--experimental_convenience_symlinks=ignore`; `--lockfile_mode=error`; S4 at every phase exit | build |
| R-05 | `MODULE.bazel.lock` differs per OS | **L** | M | rules_python's extensions are reproducible (ACC); checked on each OS; `off` is never used [SEC-5] | build |
| R-06 | The B0 `make test`/`make lint` change breaks an agent habit or a gauntlet run | L | M | Same test set; CI already enforces the hook; hygiene tests pin the recipes; documented in `validation.md` | automation |
| R-07 | A BUILD file under a synced directory is exported upstream or deleted by `delete_stale` | M | M | Only `automation/BUILD.bazel`; the hygiene test asserts it against the manifest | automation |
| R-08 | Release bytes or release provenance change because of `scripts/ci.py` edits | L | H | V-REL (two runs, member list, untouched release functions, strict release tests) in every slice that touches `scripts/ci.py` | release |
| R-09 | Nested reusable workflow unsupported | **L** | M | Documented as supported up to 10 levels (ACC); the B4 shadow exercises the same workflow | ci |
| R-10 | Windows rules_python/runfiles/symlink issues, or git missing from the strict PATH | H | L (non-blocking) | B7 experimental; pip Windows leg stays blocking; strict mode turns a skip into a failure | ci |
| R-11 | Cold start exceeds the job timeout | M | M | Repository (download) cache restored on PRs and saved on main; timeout 25 min; measured at B4 exit | ci |
| R-12 | A poisoned restored cache yields a false green or code execution [SEC-1, SEC-R2-1, ACC-R2-6] | L | H | Only the sha256-keyed download cache is restored; the repo contents cache is disabled for every command in the job by setup-bazel's `bazelrc: common --repo_contents_cache=` (and `common:ci`), and its directory is deleted after restore; `BAZELISK_HOME` is a fresh `$RUNNER_TEMP` directory, so Bazel is downloaded and checked against `BAZELISK_VERIFY_SHA256` on every run; no disk/action cache; saves only on main pushes; `bazel-tests.yml` refuses tags; release build/verify/publish and the daily runs use no Bazel cache. The existing pip caches are out of scope. Residual: the preinstalled Bazelisk binary is trusted as part of the runner image | ci |
| R-13 | Very new versions regress | M | M | 14-day rule; best-effort support accepted (Q-8); bump procedure (section 3.1) | build |
| R-14 | Lock drift between `requirements_lock.txt`, `uv.lock`, `pyproject.toml`, `requirements.txt` | M | M | `tests/test_dependency_locks.py` (versions, hashes, sources, options), offline, every run | build |
| R-15 | Secrets reach Bazel through env flags, BEP or caches | L | H | Daily runs stay out of Bazel; `test_live_auth` has no target; the secret-flag scan covers `.bazelrc`, BUILD, `.bzl` and `Makefile` (hygiene test) and `bazel-tests.yml` including `CFG` and the setup-bazel `bazelrc` input (`tests/test_ci.py`); no workspace `try-import`; no BES; no BEP file in CI [SEC-8, SEC-18, SEC-R3-2, SEC-R3-5] | ci |
| R-16 | Runfiles root not on `sys.path`, or children inherit `PYTHONSAFEPATH` | M | M | Source-level answers known (ACC); `bazel_main` inserts the root; B0 sets `PYTHONPATH` for children; spike items 1–2 | build |
| R-17 | Two sources of truth rot independently | M | L | Parity job compares them on every run; B9 retire plan keeps one oracle | ci |
| R-18 | A test run from the repo root reads and rewrites the real `.device` [SEC-R2-3, SEC-R3-3] | M (observed) | H | `run_tests` runs every suite in a temporary CWD; `tests/__init__.py` refuses root-CWD package imports when `.device` exists; `make test` and Bazel isolate the CWD. Not covered: `unittest discover -s tests` and script-form runs, forbidden in `validation.md`; A-0 keeps no real `.device` in the checkout | tests |
| R-19 | **Sdist build code** (`ratelimit`) runs unsandboxed at fetch time with an unpinned build backend [SEC-2] | M | H | Q-11 mechanism with hashed build deps; `b2-pip-hub` blocked until decided; owner-run fetches (§3.13) | build |
| R-20 | **Job token exposure** to build code via setup-bazel [SEC-3] | M | M | `token: ""`, `bazelisk-version: ""`; policy test forbids `github.token` | ci |
| R-21 | **Sandbox layer silently absent** [SEC-7] | M | M | Pinned `--strategy=TestRunner=…`; Linux canary; Q-16 | ci |
| R-22 | **Coverage drift**: a new test module has no target, or reads an undeclared doc [FEA-14] | M | H | Coverage test in `ci.py test` (PR-blocking from B2); `docs/**` glob | tests |
| R-23 | **Release gate inherits Bazel** through `python-tests.yml` [SEC-4, FEA-06] | L | H | `bazel` input default false; `release.yml` unchanged; asserted in `test_ci` | release |
| R-24 | **Execution environment lacks Bazel**, sandbox or network allowances [FEA-05] | H until Q-9 | H | B1 entry requires Q-9; owner-run fetch; documented in `validation.md` | automation |
| R-25 | Agent runs execute fetch-time code on the owner's machine [SEC-17, SEC-R2-2, ACC-R2-5, FEA-R2-04] | M | M | Downloads are off by default in the tracked rc (`common --repository_disable_download`); only `--config=fetch`/`--config=ci` enable them; fetch-changing slices are owner-run; the owner fetches in each new worktree. Residual: `ctx.execute` in a repository rule (the sdist build) can still run when a new output base re-materializes repositories; spike 13 evaluates `--nofetch` and `PIP_NO_INDEX=1` | automation |
| R-26 | **Workflow slices widen scope or relax policy** [SEC-9, SEC-10] | L | H | Exact-file `allowed_paths`; add-only convention for `scripts/ci.py`/`tests/test_ci.py`; owner review; the B5 sync is owner-run | ci |

| R-27 | The B4/B6 evidence windows stall because nothing else merges [FEA-R2-05] | M | M | Q-18: owner dispatches on idle days; evidence counted by distinct days; timeboxes of 21 and 28 days | ci |
| R-28 | Queue file made invalid by a queue PR (unknown `recommended_unblocker`, missing `entry_condition`) [FEA-R2-01] | L | H | Entries generated as JSON and validated with `policy.load_queue`, the harness test and `run_next.py --dry-run` on a copy; no `recommended_unblocker` is set | automation |
| R-29 | An agent Bazel slice needs a repository the owner fetch did not materialize (a new label, a toolchain, the 3.11 set) [FEA-R3-01] | M | M | Hub `build_test` over every lock package; analysis-level owner fetch in both configurations; spike 18 and the B2 exit checks; an agent that would add an external label stops | build |
| R-30 | The lockfile cannot be created or refreshed under `error` mode [ACC-R3-1] | M | H | Owner-run `bazel mod deps --config=fetch --lockfile_mode=update` before the fetch; never `update` in a config | build |

## 7. Review traceability

Rounds 2 to 4 are traced below; round 1 (82 findings, all fixed or partly fixed with recorded residuals) is traced in the QA working files.

### Round 4

| ID | Severity | Disposition | Where | Note |
|---|---|---|---|---|
| SEC-R4-1 | MINOR | fixed | roadmap B3 phase exit; JSON `b3-make-bazel-targets` notes and exit check; design notes §1.8 | The agent checks `make -n bazel-fetch`; the owner runs the fetch once before merge |
| FEA-R4-01 | MINOR | fixed | roadmap §3 owner-run slices (scope-check script); JSON owner-run notes; design notes §1.8 | Uses `policy.out_of_scope_paths` and `max_files_changed`; output goes in the PR body |
| FEA-R4-02 | MINOR | fixed | roadmap §3 owner-run slices; JSON owner-run notes; design notes §1.8 | Schema-valid handoff under `automation/handoffs/` in the `chore(queue)` PR that marks the slice done |
| FEA-R4-03 | MINOR | fixed | roadmap §3 first bullet, Q-14; design notes §1.8 | Clean, committed branch; `git diff --check origin/main...HEAD` |
| ACC-R4-1 | MINOR | fixed | roadmap §2.4; JSON `b1-bazel-bootstrap` (placement check), `b2-libraries`, `b2-test-main-macro`, `b5-automation-bazel-targets`; design notes §1.9 | `package()` after the `load()` statements, before the first rule |
| ACC-R4-2 | MINOR | fixed | design notes §1.1 | Protection described as partial for `discover -s tests` |

### Round 3

| ID | Severity | Disposition | Where | Note |
|---|---|---|---|---|
| SEC-R3-1 | MINOR | fixed | roadmap §2.7 steps 6 and 10; design notes §1.7 | `TESTLOGS` set in step 6 with a guard; upload only when set |
| SEC-R3-2 | MINOR | fixed | roadmap §2.7 policy tests; JSON `b4-bazel-reusable-workflow`; design notes §1.7, R-15 | Deny list on `bazel-tests.yml`, exact `CFG` values, exact `bazelrc` input |
| SEC-R3-3 | MINOR | fixed (optional part not adopted) | roadmap A-0; JSON `b3-make-bazel-targets`; design notes §1.1, R-18 | Claim narrowed; `discover -s tests` and script runs forbidden from the root; no real `.device` in the checkout. Per-module `setUpModule` fixtures not adopted: A-0 removes the hazard class |
| SEC-R3-4 | MINOR | fixed | roadmap §3 owner-run slices; JSON `b1-spike-findings`, `b1-bazel-bootstrap`, `b2-pip-hub`, `b9-rules-python-bump`; design notes §1.8, D-37 | Never queued; first validation `manual owner-run slice: an agent must stop without changes`; notes start with `AGENT: do not execute this slice` |
| SEC-R3-5 | MINOR | fixed | roadmap §2.3; JSON `b0-bazel-ignore-hygiene`, `b1-bazel-bootstrap`, `b3-make-bazel-targets`; design notes §1.2, D-41 | No `try-import`; `.bazelrc.user` not ignored; agents never edit `.bazelrc*` |
| SEC-R3-6 | MINOR | fixed | JSON `b0-bazel-ignore-hygiene` | `git check-ignore -q` for `.device`, `config.secrets`, `passwords.txt`, `.profiles/x`, `tachikoma.log`, `x.har`, `x.pcap` |
| ACC-R3-1 | BLOCKING | fixed | roadmap §2.3 lockfile bullet, §3 owner-run slices; JSON `b1-bazel-bootstrap`, `b2-pip-hub`, `b9-rules-python-bump`; design notes §1.2, §3.1, D-38, R-30 | `bazel mod deps --config=fetch --lockfile_mode=update` before the fetch; never in `common:fetch` |
| ACC-R3-2 | BLOCKING | fixed | roadmap §2.4; JSON `b1-bazel-bootstrap` (root `package()` and the no-public assertion), `b2-libraries`, `b2-test-main-macro`, `b5-automation-bazel-targets`; design notes §1.9, D-39 | `package(default_visibility = ["//:__subpackages__"])` |
| ACC-R3-3 | MINOR | fixed | roadmap §2.4; JSON `b1-bazel-bootstrap`, `b2-test-main-macro`, `b2-all-test-targets`; design notes §1.9 | Literal list in both BUILD files, asserted equal |
| ACC-R3-4 | MINOR | fixed | roadmap §2.5; JSON `b5-automation-bazel-targets`; design notes §1.9 | `src` and `module` parameters drive `srcs` and `args` |
| ACC-R3-5 | MINOR | fixed | roadmap §2.3, spike items 4 and 13; design notes §1.2 | Registry files, cache-served checksummed downloads and refused index pages described; spike 4 compares pip `facts` per OS |
| ACC-R3-6 | MINOR | fixed | roadmap §5 V-REL; JSON `b2-parity-tool`; design notes §1.7 | `PY="$REPO/.venv/bin/python"` |
| ACC-R3-7 | MINOR | fixed | roadmap §2.7 step 6 | Assignments on their own lines, `test -n`, `rm -rf --` |
| ACC-R3-8 | MINOR | fixed | roadmap §2.7 plan job; design notes §1.7 | Plan step fails on an empty list |
| FEA-R3-01 | MAJOR | fixed | roadmap §3 owner fetch, spike item 18; JSON `b2-pip-hub` (all-package `build_test`, no-network check in two worktrees), `b2-libraries`, `b3-make-bazel-targets` (`make bazel-fetch`); design notes §1.2, D-40, R-29 | Analysis-level fetch in both configurations; agent slices that would add an external label stop |
| FEA-R3-02 | MINOR | fixed | roadmap §3 CI evidence log; design notes §1.8 | Owner `docs(ci-log)` PRs |
| FEA-R3-03 | MINOR | fixed | roadmap §3 owner-run slices; design notes §1.8 | No `run_next.py`, so no handoff timeout |
| FEA-R3-04 | MINOR | fixed | JSON `b6-blocking-bazel-job`; design notes §1.8 | `manual owner dispatch of release.yml on the branch shows tests success` |
| FEA-R3-05 | MINOR | fixed | JSON `b0-ci-root-and-runner`; design notes §1.1 | `ignore_cleanup_errors=True`, CWD restored in `finally` |
| FEA-R3-06 | MINOR | fixed | roadmap header and A-0; JSON `b1-spike-findings` notes and exit; design notes §1.8 | Queue wins after the queue PR; spike re-syncs entries; one wording for when each run form is safe |
| FEA-R3-07 | MINOR | fixed | roadmap spike item 13; §2.3 | Registry files in a fresh output base with a shared cache; answer recorded in `validation.md` |

Round-2 items reported partly resolved in round 3:

| ID | r3 status | Disposition now | Where | Note |
|---|---|---|---|---|
| SEC-R2-3 | partly (r3) | fixed, documented exception | see SEC-R3-3 | `discover -s tests` and script runs stay outside the guard; A-0 removes the hazard |
| SEC-R2-9 | partly (r3) | fixed | see SEC-R3-1 |  |

### Round 2

| ID | Severity | Disposition | Where | Note |
|---|---|---|---|---|
| SEC-R2-1 | MAJOR | fixed | roadmap §2.7 steps 5–6 and policy-test list; design notes §1.7, D-13, R-12 | Contents cache disabled job-wide via setup-bazel `bazelrc`, directory deleted after restore, `$CFG` (starting `--config=ci`) asserted on every Bazel call; B4 exit checks the saved cache has no `contents/` |
| SEC-R2-2 | MAJOR | fixed | roadmap §2.3 (default deny), §3 owner-run slices, Q-9; design notes §1.2, D-27, D-28, R-25 | Downloads denied by default in the tracked rc, so the unchanged validation strings are safe; agent sandbox gets no network; `ctx.execute` residual recorded under ACC-R2-5 |
| SEC-R2-3 | MAJOR | fixed | roadmap `b0-ci-root-and-runner`, S12; design notes §1.1, D-25, R-18 | `run_tests` runs in a temporary CWD for every caller; sentinel-`.device` regression test; `tests/__init__.py` guard for `python -m unittest` |
| SEC-R2-4 | MINOR | fixed | roadmap §2.5 macro, `b2-all-test-targets`, `b5-automation-bazel-targets`; design notes D-35 | Macro fixes `srcs`/`main` and `fail()`s on overrides; hygiene test is add-only after B2 |
| SEC-R2-5 | MINOR | fixed | roadmap `b1-spike-findings` (owner-run), spike item 10; design notes §1.3 |  |
| SEC-R2-6 | MINOR | fixed | roadmap §2.7 step 6 and policy tests |  |
| SEC-R2-7 | MINOR | fixed | roadmap §3 dispatch rule, Q-15; design notes §1.8 | Agents never run `gh workflow run` |
| SEC-R2-8 | MINOR | fixed | roadmap §2.4 `SECRET_PATTERNS` and `.bazelignore`, `b1-bazel-bootstrap` hygiene; design notes D-22 |  |
| SEC-R2-9 | MINOR | fixed | roadmap §2.7 step 10; design notes §1.7 |  |
| ACC-R2-1 | BLOCKING | fixed | roadmap `b0-dependency-lock`; design notes §1.4 | `version = "0.0.0"` |
| ACC-R2-2 | BLOCKING | fixed | roadmap §2.3 (`common:ci_linux`/`common:ci_macos`), `b1-bazel-bootstrap` and `b4-bazel-reusable-workflow` hygiene assertions; design notes D-30 | Every config name has a `common:` line, asserted for `.bazelrc`, `Makefile` and `bazel-tests.yml` |
| ACC-R2-3 | MAJOR | fixed | roadmap `b0-dependency-lock`; design notes §1.4 | Root virtual entry exempted and asserted to be the only non-registry entry |
| ACC-R2-4 | MAJOR | fixed | roadmap §2.5 allowed skips, §5 Parity rule, `b2-parity-tool`; design notes §1.5, D-31 | Normalization specified; test uses real `--verbose` output |
| ACC-R2-5 | MAJOR | fixed | roadmap §2.3 bullet, §3 (owner fetch per worktree), spike item 13; design notes §1.2, R-25 | Reworded; `--nofetch` and `PIP_NO_INDEX=1` are spike-13 candidates |
| ACC-R2-6 | MAJOR | fixed | roadmap §2.7 step 4; design notes §1.7, D-13 | `BAZELISK_HOME=$RUNNER_TEMP/bazelisk`, asserted |
| ACC-R2-7 | MINOR | fixed | design notes §1.8 (B5 upstream list); round-1 trace corrected | `test_harness.py:250-251` |
| ACC-R2-8 | MINOR | fixed | roadmap §2.7 step 4; design notes §1.7 | darwin and `.exe` asset names |
| ACC-R2-9 | MINOR | fixed | roadmap `b0-dependency-lock` validations | `uv lock --check` |
| ACC-R2-10 | MINOR | fixed | roadmap §3 end-to-end run (UNVERIFIED removed), Q-9; design notes §1.4 | `uv python install 3.11` added |
| ACC-R2-11 | MINOR | fixed | roadmap §2.7 fenced queries | Plain pipe characters, anchored tag regex |
| ACC-R2-12 | MINOR | fixed | roadmap §5 V-REL | `set -euo pipefail`, `trap … ERR` keeps evidence |
| ACC-R2-13 | MINOR | fixed | roadmap §2.7 plan job, `b7-windows-experimental`; design notes D-29 | No matrix `exclude` relied on |
| ACC-R2-14 | MINOR | fixed | roadmap `b0-dependency-lock` | `--no-annotate --no-header`; comment lines skipped |
| ACC-R2-15 | MINOR | fixed | roadmap §2.6 note; design notes §1.4 | UNVERIFIED removed; `.venv` creation caveat stated |
| FEA-R2-01 | BLOCKING | fixed | bazel-migration-queue-entries.json; roadmap §3 queue-PR checks; design notes §1.8, R-28 | No `recommended_unblocker`; validated with `load_queue`, the harness test and `run_next.py --dry-run` on a copy |
| FEA-R2-02 | BLOCKING | fixed | roadmap `b4-bazel-reusable-workflow` (adds `tests/test_build_hygiene.py`); design notes D-34 | `.bazelrc` assertions are checkout-level |
| FEA-R2-03 | MAJOR | fixed | roadmap §2.6 (recipes at column 0) | `git diff --check` clean on all three new files (scratch repo) |
| FEA-R2-04 | MAJOR | fixed | roadmap §2.3, §3 owner-run slices, `b1-bazel-bootstrap`, `b2-pip-hub`, `b9-rules-python-bump`; design notes D-27, D-28 | Different mechanism: default-deny rc instead of `--config=agent` in every string; same effect, nothing to forget |
| FEA-R2-05 | MAJOR | fixed + owner decision (Q-18) | roadmap B4 and B6 exits, Timeline, Q-18; design notes §1.10, D-36, R-27 | Distinct days, owner dispatch on idle days, timeboxes 21/28 days; timeline marked conditional |
| FEA-R2-06 | MAJOR | partly fixed | BAZEL_MIGRATION_ROADMAP.md + BAZEL_MIGRATION_DESIGN_NOTES.md + bazel-migration-queue-entries.json; design notes D-32 | Split done, finding IDs removed from the core (0 left), JSON is the exact-string source. **Not met:** the core is ~58 KB, not ≤ 40 KB, because the 23 fully expanded slice blocks alone are ~35 KB |
| FEA-R2-07 | MINOR | fixed | roadmap `b4-postmerge-shadow` | Owner dispatch on the branch is the first real run |
| FEA-R2-08 | MINOR | fixed | roadmap B7 entry/exit, spike item 8 |  |
| FEA-R2-09 | MINOR | fixed | roadmap `b1-spike-findings` depends on `b0-dependency-lock` |  |
| FEA-R2-10 | MINOR | fixed | roadmap `b4-postmerge-shadow`, `b6-blocking-bazel-job`, B4/B6 exits; design notes D-33 | `docs/workflows/bazel-ci-log.md` |
| FEA-R2-11 | MINOR | fixed | roadmap §3 merge rule, B0 linear chain; design notes §1.8 |  |
| FEA-R2-12 | MINOR | fixed | roadmap `b0-dependency-lock` | Same fix as ACC-R2-3 |
| FEA-R2-13 | MINOR | fixed | roadmap `b0-test-repo-root-b`, `b2-parity-tool` (adds `tests/test_ci.py`) | Explicit list, missing file fails; list extended in the slice that adds the file |
| FEA-R2-14 | MINOR | fixed | roadmap §2.7 plan job; design notes D-29 | `matrix` is unavailable in job-level `if`, so filtering moves to a plan job |

Round-1 carry-overs:

| ID | r2 status | Disposition now | Where | Note |
|---|---|---|---|---|
| SEC-1 | partly (r2) | fixed | see SEC-R2-1 |  |
| SEC-7 | partly (r2, residual accepted) | partly fixed, residual accepted | design notes §1.6 | No macOS canary (would be a real connect outside the hook) |
| SEC-13 | partly (r2, residual accepted) | partly fixed, residual accepted | roadmap §2.7 step 4; `b9-dev-bazel-checksums` | Developer-side checksum stays optional |
| SEC-17 | partly (r2) | fixed | see SEC-R2-2 and ACC-R2-5 | `ctx.execute` residual recorded |
| SEC-21 | partly (r2, residual accepted) | partly fixed, residual accepted | design notes §1.6; `b9-audit-hook-coverage` |  |
| ACC-2 | partly (r2) | fixed | see ACC-R2-2 |  |
| ACC-6 | partly (r2) | fixed | see ACC-R2-4 |  |
| ACC-17 | regressed (r2) | fixed | see ACC-R2-7 |  |
| FEA-08 | partly (r2) | fixed | see FEA-R2-01 |  |
| FEA-35 | partly (r2) | fixed | see FEA-R2-05 | Timeline is conditional on Q-18 |
