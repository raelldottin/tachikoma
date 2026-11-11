# SPEC-02: Local test runs never touch real credential files

**Status:** Approved
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** safety (prerequisite for every live spec)
**Owner gate:** G0 (approve); owner action A-0 (check the existing `.device`)
**Depends on:** —
**Implements roadmap item:** [BAZEL_MIGRATION_ROADMAP.md](../BAZEL_MIGRATION_ROADMAP.md) owner action A-0 and slice `b0-make-hermetic-local`

## Problem

Running `make test` or `scripts/ci.py test` from the repository root rewrites the gitignored `./.device`, which holds a device key and refresh token, and `./tachikoma.log`. Eight test modules construct `Device()` and five import `run.py`, which writes the log on import. During this session, several runs from the repo root rewrote the owner's `./.device`.

## Scope

- **In:** making the supported test entry points run in a temporary working directory, a guard against the known unsafe forms, and tests that prove it.
- **Out:** changing `sdk/device.py` or `run.py` behaviour for real runs.

## Requirements

- **S02-R1 (MUST)** `make test`, `make test-security` and `scripts/ci.py test` run every test module with a temporary directory as the working directory, created fresh for the run and removed afterwards.
- **S02-R2 (MUST)** After any of those commands, a sentinel `./.device` and `./tachikoma.log` placed in the repository root before the run are byte-identical and have the same modification time.
- **S02-R3 (MUST)** Running `python -m unittest tests.<module>` from the repository root while a `./.device` exists fails before any test runs, with a message naming the safe command.
- **S02-R4 (MUST)** Tests that read repository files keep working: they locate the repository from their own file path, not from the working directory.
- **S02-R5 (SHOULD)** The documentation states the unsafe forms (`unittest discover -s tests` and running a test file as a script from the root) and that the guard covers them only partly.
- **S02-R6 (MUST)** The test count and pass or fail result of `scripts/ci.py test` are unchanged (945 tests, 1 skipped, at `f3c255e`).

## Interfaces

Delivered by slice `spec02-credential-safety`, which takes over the temporary-directory part of the Bazel plan's `b0-make-hermetic-local` (that slice keeps its `make lint` work). `scripts/ci.py run_tests` gains the temporary directory; the `Makefile` test recipes use an absolute interpreter path; `tests/__init__.py` gains the guard.

## Safety invariants

This spec exists to keep secrets out of reach of the test suite. It adds no network access and changes no game code.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S02-AC1 | R1, R2 | offline | Given sentinel files in a scratch clone's root, when `make test` runs, then both are unchanged |
| S02-AC2 | R3 | offline | Given a `./.device`, when `python -m unittest tests.test_security` runs from the root, then it exits non-zero before any test |
| S02-AC3 | R4, R6 | offline | The suite still reports 945 tests and 1 skip |

## Test plan

Extend `tests/test_ci.py` with a sentinel test that runs `run_tests` on one module inside a scratch tree; a guard test that imports `tests` with a fake `.device` present.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec02-credential-safety` | R1–R6 | `Makefile`, `scripts/ci.py`, `tests/__init__.py`, `tests/test_ci.py`, `tests/test_pvp_sanitized_inputs.py`, `docs/specs/SPEC-02-local-credential-safety.md` | agent |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S02-Q1 | Does the current `./.device` hold a real device key that must be re-provisioned (A-0)? | any live spec | Treat it as compromised: re-provision before G1 |

## Evidence

No live evidence required.

## Open questions and risks

Until this lands, run the suite only in a scratch clone.
