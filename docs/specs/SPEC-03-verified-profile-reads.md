# SPEC-03: Verified trophy, starbux and battle-history reads

**Status:** Draft
**Release:** R1 (state) and R3 (PvP evidence)
**Owner gate:** G1 (read-only access and sanitized captures)
**Depends on:** SPEC-01
**Implements roadmap item:** "No verified trophy or battle-history read" and "No trophies or starbux in live snapshots" ([known-limitations.md](../baseline/known-limitations.md))

## Problem

`ClientSnapshotProvider` leaves the `profile` section missing because trophies and starbux are only held from login (`sdk/state/client_provider.py`). `Client.findUserRanking` (`sdk/client.py:1624`) and `Client.listPvPBattles2` (`sdk/client.py:648`) are untested and their response shapes are unknown. As a result every live battle outcome is `unknown`, opponent cohorts are `unknown`, and starbux changes are never observed.

## Scope

- **In:** parsers built from sanitized real responses, a fresh profile read in the snapshot provider, battle-history records with opponent facts, and ledger entries recording the evidence.
- **Out:** any mutating call; changing how `Client` sends these requests.

## Requirements

- **S03-R1 (MUST)** The owner supplies sanitized captures of the responses for the current user's profile (trophies, starbux), `FindUserRanking` and `ListPvPBattles2`, stored under `tests/fixtures/live_sanitized/` with a `README` recording date, game version and how they were sanitized. They contain no access token, refresh token, device key, e-mail, user id or player name.
- **S03-R2 (MUST)** A test fails if any file under `tests/fixtures/live_sanitized/` matches the redaction patterns in `sdk/redaction.py`, or contains a numeric id in a field named like `UserId` or `Id`.
- **S03-R3 (MUST)** A read-only method returns current trophies and starbux from a fresh read (not login state), and `ClientSnapshotProvider` reports the `profile` section as observed when that read succeeds and missing when it fails.
- **S03-R4 (MUST)** `listPvPBattles2` results are parsed into typed battle records: battle id, attacker or defender role, outcome as reported by the server, trophy change, opponent hull level and trophies where present. Fields absent from the capture stay `None`.
- **S03-R5 (MUST)** A journaled `run_pvp_battle` outcome is `verified` only when a battle record with its battle id is observed after the call, or the trophy change is observed and the call reached `FinaliseBattle15`. Defense records never verify an attack.
- **S03-R6 (MUST)** Opponent cohort labels in `sdk/reports/battles.py` come from the observed battle record when one matches.
- **S03-R7 (MUST)** The [compatibility ledger](../baseline/compatibility-ledger.md) records each confirmed field with evidence class `dynamically-demonstrated` and the capture's game version.
- **S03-R8 (SHOULD)** The capability inventory entries for `findUserRanking` and `listPvPBattles2` move from `untested` to `mocked-test`, citing the new tests.

## Interfaces

- New: `sdk/state/profile_reads.py` (parsers) and a read-only `Client` method only if no existing call returns the profile; otherwise reuse the existing call.
- Changed: `sdk/state/client_provider.py` (`profile` section), `sdk/actions/catalog.py` (battle postcondition), `sdk/reports/battles.py` (cohorts).

## Safety invariants

- Read-only calls only.
- Captures are sanitized and checked by S03-R2.
- Unknown fields stay `None`.
- A server-reported outcome is evidence only when tied to this call's battle id.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S03-AC1 | R2 | offline | The fixture scan fails on a planted token or id and passes on the real captures |
| S03-AC2 | R3 | offline | Given the captured profile response, the snapshot has observed trophies and starbux; given a failed read, `profile` is missing |
| S03-AC3 | R4, R6 | offline | Given the captured battle list, the parsed records match hand-checked values, and cohorts are labelled |
| S03-AC4 | R5 | offline | A trophy change with no matching battle record and no finalise step is `unknown`; a matching attack record verifies |
| S03-AC5 | R1, R7 | live | The owner records one read-only run on the test account: profile and battle history read, game version noted, ledger updated |

## Test plan

`tests/test_profile_reads.py` (parsers, provider, fixture scan); extend `tests/test_pvp_battle_action.py` and `tests/test_battle_results_from_journal.py`.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec03-captures` | R1 | `tests/fixtures/live_sanitized/*` | **owner** |
| `spec03-fixture-scan` | R2 | `tests/test_profile_reads.py` | agent |
| `spec03-profile-read` | R3, R8 | `sdk/state/profile_reads.py`, `sdk/state/client_provider.py`, tests, inventory JSON | agent |
| `spec03-battle-history` | R4–R6 | `sdk/state/profile_reads.py`, `sdk/actions/catalog.py`, `sdk/reports/battles.py`, tests | agent |
| `spec03-ledger-evidence` | R7 | `sdk/compat/entries.py`, `docs/baseline/compatibility-ledger.md` | agent, after AC5 |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S03-Q1 | Which captures can be provided, from which game version, and how will they be sanitized? | `spec03-captures` | The owner sanitizes by hand; an agent reviews with S03-R2 |
| S03-Q2 | Is a read-only live run on the test account authorized for AC5? | AC5 | Not authorized |

## Evidence

Pending.

## Open questions and risks

Whether a dedicated profile endpoint exists is unknown; the captures decide it. If battle history omits opponent facts, S03-R6 cohorts stay `unknown` and the R3 comparison must be unstratified.
