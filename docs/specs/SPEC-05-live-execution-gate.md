# SPEC-05: Authorized live execution with a canary and kill switch

**Status:** Approved
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** R2 (prerequisite for every live trial)
**Owner gate:** none to build (offline); G2 to use
**Depends on:** SPEC-01, SPEC-02
**Implements roadmap item:** "No live execute path" ([known-limitations.md](../baseline/known-limitations.md)); the execute path named in `r2-live-progression-trial`

## Problem

The scheduler, runner and fleet orchestrator can execute actions (`run(execute=True)`), but `run.py` exposes only dry runs (`--progression-dry-run`, `--fleet-dry-run`; `run.py:220` asserts dry run). Every live trial needs an execute path that cannot run by accident, that stops on the first sign of trouble, and that leaves a complete record.

## Scope

- **In:** an execute command for one or more registry accounts, an authorization record, canary mode, budgets and stop conditions, and a run record.
- **Out:** running it against a real account (that is each trial's live step), starbux spending, and scheduling.

## Requirements

- **S05-R1 (MUST)** `run.py --fleet-execute --registry PATH --authorization PATH` is the only path that calls `run(execute=True)`. Without a valid authorization file it exits non-zero before any login.
- **S05-R2 (MUST)** The authorization file is JSON naming: the owner (a label, not an e-mail), the account aliases, the allowed action ids, a not-before and expiry time (UTC, at most 14 days apart), per-account per-period mineral and gas caps, the run modes allowed (`canary`, `full`), and a free-text purpose. It holds no credential. Any account, action or time outside it is refused.
- **S05-R3 (MUST)** A dry run of the same registry, using the same policy version, must have completed for each account within the previous 24 hours. The execute run refuses an account whose dry-run plan is missing or older.
- **S05-R4 (MUST)** In `canary` mode each account executes at most one action, chosen as the first safe-to-retry action in its plan, then stops and prints the reconciliation.
- **S05-R5 (MUST)** The run stops an account on its first `contradicted` result, on any `unknown` result for a non-idempotent action, on an isolation refusal, or when its cap would be exceeded. It stops the fleet when a stop file exists, or when more than a configurable share of accounts have stopped.
- **S05-R6 (MUST)** Starbux spending stays disabled, whatever the authorization says.
- **S05-R7 (MUST)** Every attempt is journaled. The run writes a run record (authorization hash, registry hash, policy version, game version, start and end, per-account outcome) next to the journals.
- **S05-R8 (MUST)** The identity check is mandatory in execute mode: each profile must declare `expected_game_identity`, and the run needs `--identity-salt-env`.
- **S05-R9 (MUST)** Credentials, the salt and raw user ids never appear in output, logs, journals or the run record. This reuses the fleet dry run's scrubbing.
- **S05-R10 (SHOULD)** `--plan-only` prints what an execute run would do under the authorization, without logging in.

## Interfaces

- `run.py --fleet-execute`, `--authorization`, `--mode canary|full`; reuses `run_fleet_dry_run`'s factory, scrubbing and reporting.
- `sdk/fleet/authorization.py`: `load_authorization`, `check_authorization(auth, registry, now)`.
- Run record: `<journal dir>/runs/<UTC time>.json`.

## Safety invariants

- Execution needs a dated, scoped, owner-written authorization.
- The canary goes first.
- Every existing safety stop still applies.
- No starbux spending.
- Identity checks are always on.
- Scrubbing as in `--fleet-dry-run`.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S05-AC1 | R1, R2 | offline | Missing, expired, malformed or out-of-scope authorization exits non-zero before any fake login |
| S05-AC2 | R3 | offline | An account with no recent dry run is refused; one with a fresh dry run proceeds |
| S05-AC3 | R4 | offline | Canary mode performs exactly one safe-to-retry action per account against fakes |
| S05-AC4 | R5, R6 | offline | Each stop condition halts the right scope; starbux is never spent even if allowed in the file |
| S05-AC5 | R7, R8, R9 | offline | The run record is complete; a profile without a fingerprint is refused; the leak scan of all outputs is clean |
| S05-AC6 | R1–R9 | live | The owner runs a canary on the authorized test account and records the reconciliation (part of SPEC-07) |

## Test plan

`tests/test_fleet_execute.py` using the fake clients from `tests/test_fleet_dry_run.py` and the fault-injection simulation for stop conditions; sockets blocked.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec05-authorization` | R2 | `sdk/fleet/authorization.py`, tests, `docs/baseline/fleet-accounts.md` | agent |
| `spec05-execute-command` | R1, R3–R10 | `run.py`, `sdk/fleet/factory.py`, tests, `README.template`, `README.md`, inventory JSON | agent |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S05-Q1 | Which actions may the first authorization allow? | first use | `collect_all_resources`, `collect_daily_reward` only |
| S05-Q2 | The share of stopped accounts that halts the fleet | approval | Any one account in a two-account trial |

## Evidence

Pending (recorded under SPEC-07).

## Open questions and risks

The policy's construction-cap limit is only found at execution time; the canary catches it early.
