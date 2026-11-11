# SPEC-01: Spec traceability check

**Status:** Approved
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** process
**Owner gate:** G0 (approve)
**Depends on:** —
**Implements roadmap item:** "Definition of done: acceptance tests against representative fixtures ... and an updated roadmap status" ([PRODUCT_ROADMAP.md](../PRODUCT_ROADMAP.md) §10)

## Problem

The plan in [README.md](README.md) relies on every requirement having a test. Nothing checks that today, so a spec could be marked verified with requirements no test covers, or tests could cite ids that were renamed.

## Scope

- **In:** a parser for spec files, a conformance test run by `make test`, and the `# Verifies:` convention.
- **Out:** generating tests, judging test quality, and live evidence.

## Requirements

- **S01-R1 (MUST)** A function reads every `docs/specs/SPEC-*.md` and returns its id, status, and requirements (id, level, withdrawn or not).
- **S01-R2 (MUST)** It finds every `# Verifies: Snn-Rk[, Snn-Rk...]` comment under `tests/` and `automation/tests/`.
- **S01-R3 (MUST)** For each spec whose status is `Verified offline` or `Done`, every MUST requirement that is not withdrawn and is proved by at least one offline acceptance criterion has at least one test citing it. Otherwise the conformance test fails and names the uncovered ids. `Approved` specs are reported but never fail the check, so a spec can be approved before its tests exist.
- **S01-R4 (MUST)** A citation of an id that does not exist, or that belongs to a `Withdrawn` requirement, fails the test and names the file and line.
- **S01-R5 (MUST)** A spec marked `Done` must list at least one evidence link, or state "no live evidence required". A MUST proved only by live or owner acceptance criteria needs no test; a `Done` spec must have evidence for it.
- **S01-R6 (MUST)** Malformed spec files (missing status line, duplicate id, unknown level) fail with a clear message instead of being skipped.
- **S01-R7 (SHOULD)** A command prints a coverage table (spec, requirement, citing tests) for review.
- **S01-R8 (MUST)** The check runs offline, reads files only, and adds no dependency.
- **S01-R9 (MUST)** It reads each acceptance criterion's `Proves` and `Kind` columns, expanding ranges such as `R1–R9`, and fails when a MUST is not proved by any acceptance criterion at all, whatever the spec's status (except `Draft`).

## Interfaces

- `scripts/spec_trace.py` with `load_specs(root)`, `load_citations(root)` and `check(root) -> list[str]` (empty means pass); `python scripts/spec_trace.py --report` prints the S01-R7 table.
- `tests/test_spec_traceability.py` calls `check()` on the real repo and on synthetic spec trees in temporary directories.
- Requirement line format: `- **Snn-Rk (MUST|SHOULD|MAY)** text`, optionally ending with `(Withdrawn: reason)`.

## Safety invariants

Read-only; touches no game code, credentials or network.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S01-AC1 | R1, R6 | offline | Given synthetic specs with each malformation, when `check()` runs, then each yields a named error |
| S01-AC2 | R3 | offline | Given an approved spec with an uncited MUST, then `check()` reports that id; Draft specs are ignored |
| S01-AC3 | R2, R4 | offline | Given a test citing a missing or withdrawn id, then the file and line are reported |
| S01-AC4 | R5 | offline | Given a `Done` spec without evidence, then it fails |
| S01-AC5 | R8 | offline | The real repository passes, with sockets blocked |
| S01-AC6 | R3, R9 | offline | An `Approved` spec with uncited MUSTs passes with a report; a MUST with no acceptance criterion fails; a live-only MUST in a `Verified offline` spec needs no test |

## Test plan

`tests/test_spec_traceability.py`, synthetic spec trees in temporary directories; one test on the real `docs/specs/`.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec01-traceability-check` | R1–R9 | `scripts/spec_trace.py`, `tests/test_spec_traceability.py`, `docs/specs/README.md` | agent |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S01-Q1 | Should SHOULD requirements also need a test? | approval | No; only MUST |

## Evidence

No live evidence required.

## Open questions and risks

None known.
