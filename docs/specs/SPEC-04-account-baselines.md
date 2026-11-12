# SPEC-04: Per-ship baselines and game-version provenance

**Status:** Done, 2026-11-12. Evidence: [G1 baseline](../experiments/baselines/2026-11-12-g1.md).
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** R0 (the remaining part)
**Owner gate:** G1
**Depends on:** SPEC-01, SPEC-03
**Implements roadmap item:** "Per-ship baselines, observed game version, test-account dataset — Not started" ([PRODUCT_ROADMAP.md](../PRODUCT_ROADMAP.md) implementation status)

## Problem

R0's exit needs a recorded starting state for every participating ship and the game version it was observed on. Nothing records either today; the [study design](../experiments/study-design.md) cannot start without them.

## Scope

- **In:** a read-only baseline command, a sanitized baseline record format, and the game version on every record.
- **Out:** any change to an account.

## Requirements

- **S04-R1 (MUST)** `run.py --baseline --registry PATH --output DIR` logs in to each enabled account, takes one snapshot with read-only calls, and writes one baseline file per alias. It has no execute path.
- **S04-R2 (MUST)** A baseline file holds the account alias, observation time (UTC), game version, hull level, trophies, minerals, gas, starbux, room designs and levels, research states, crew levels and training, and inventory, each `None` when not observed. It holds no credential, raw user id, e-mail or player name.
- **S04-R3 (MUST)** The game version comes from the server's latest-version setting the client already reads, not from the client's declared version string.
- **S04-R4 (MUST)** Writing a baseline refuses to overwrite an existing file for the same alias and time.
- **S04-R5 (MUST)** A baseline file loads back into an `AccountSnapshot`, so `diff_snapshots` can compare a later snapshot with it.
- **S04-R6 (SHOULD)** A Markdown summary table lists every baseline with the version, hull bracket and which sections were missing.
- **S04-R7 (MUST)** The secret scan from SPEC-03 (S03-R2) runs over baseline files placed in `docs/experiments/baselines/`.

## Interfaces

- `run.py --baseline` reuses the fleet factory (`sdk/fleet/factory.py`), the snapshot provider and the registry; JSON files go in `docs/experiments/baselines/<alias>-<UTC time>.json`.
- `sdk/state/baseline.py`: `write_baseline`, `load_baseline`.

## Safety invariants

- Read-only calls.
- Sanitized output, scanned.
- One account per file.
- Unobserved values are `None`.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S04-AC1 | R1, R2, R3 | offline | With fake clients, the command writes one file per enabled alias containing all fields and the server version, and no secret |
| S04-AC2 | R4, R5 | offline | A second write for the same alias and time is refused; a baseline round-trips into a snapshot and diffs cleanly |
| S04-AC3 | R7 | offline | A planted secret in a baseline file fails the scan |
| S04-AC4 | R1–R3 | live | The owner records a baseline for each test account, with the game version, in `docs/experiments/baselines/` |

## Test plan

`tests/test_account_baselines.py` with fake clients and sockets blocked.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec04-baseline-format` | R2, R4, R5, R7 | `sdk/state/baseline.py`, tests | agent |
| `spec04-baseline-command` | R1, R3, R6 | `run.py`, `README.template`, `README.md`, inventory JSON, tests | agent |
| `spec04-record-baselines` | AC4 | `docs/experiments/baselines/*` | **owner** |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S04-Q1 | Which accounts are test accounts, by alias? | AC4 | None; the baseline is not taken |
| S04-Q2 | May baseline files be committed to the repository, or kept private? | AC4 | Kept private; only the summary table is committed |

## Evidence

**Offline verification:** every MUST with an offline acceptance criterion is covered by a test that cites it (checked by `scripts/spec_trace.py`). Implemented in #166, #175.

**Live findings (G1, 2026-11-12):** the first baseline runs (owner-dispatched `live-trial.yml`, `baseline` step) recorded every ship section for `pss1`–`pss5` once #183 fixed new ships' empty rooms and research. `game_version` was `null` on all of them: the server settings field summary from the capture step showed that none of the expected version attributes exists live. `g1-game-version` reads `RecommendedVersion` and the `ServerSettingVersion` counter instead (S04-R3 still holds: both come from the server's latest-version setting). S04-AC4 waits for a baseline run with that fix; the profile section waits for the SPEC-03 parsers.

**Live evidence (S04-AC4):** the owner-dispatched baseline run 43256802775 (2026-11-12), after #187 and #199, recorded a baseline of each test account, `pss1`–`pss5`, with game version `1.0+settings.3307714` and every section present: [G1 baseline summary](../experiments/baselines/2026-11-12-g1.md). Minerals and gas are `None` (not observed), as S04-R2 allows.

## Open questions and risks

Several snapshot field names are unverified live; SPEC-03 captures should confirm them first.
