# G1 evidence: read-only live runs, 2026-11-12

**Gate:** G1, approved by the owner on 2026-11-12 for `pss1`–`pss5` (read-only). See the [runbook](RUNBOOK.md).
**Who ran it:** the owner, by dispatching [live-trial.yml](../../../.github/workflows/live-trial.yml) on `main`. No agent logged in or saw a credential; agents only downloaded the sanitized artifacts, re-scanned them and read them in full.

## Runs

| Run | Step | Result | What it showed | Fixed by |
| --- | --- | --- | --- | --- |
| 43183835436 | capture | failure (exit 1) | `GetShipByUserId` crashed for `pss2`–`pss5` (a new ship's empty `<Rooms/>` / `<Researches/>`); the server settings were withheld by the scan | #183 |
| 43184271666 | baseline | success, sections missing | `pss2`–`pss5` ship, rooms and research missing (same crash) | #183 |
| 43185783525 | capture | partial (exit 1 by design: a response was withheld) | every ship read worked; the files kept ship, battle, item and alliance ids and crew names the scan cannot recognise; the settings field summary was withheld (slash-joined paths look like base64) | #185 |
| 43185803782 | baseline | success | every ship section for all five; `game_version` `n/a`, profile missing | #187, #199 |
| 43187703923 | capture | partial (exit 1 by design: the raw settings stay withheld) | all files clean, instance ids and crew names replaced; the settings field summary showed no `LatestVersion*` attribute: `RecommendedVersion`, `MinimumClientVersion`, `MinimumVersion` and `CurrentAndroidVersion` are all `1.0` | #187 |
| 43256802775 | baseline | success | every section for all five, profile observed, game version `1.0+settings.3307714`; minerals and gas not observed | g2-canary-fixes (execute path) |

Eight further dispatches in between (43184063781 to 43184229116) were cancelled while queued behind one another; they made no request. Every artifact was downloaded with `gh run download`, re-scanned with the S03-R2 rules (0 findings in all of them), and re-sanitized offline with each fix to confirm it. Artifacts expire after seven days; the reviewed captures that matter are committed.

## What is committed

- Six captures from run 43187703923 in [tests/fixtures/live_sanitized/](../../../tests/fixtures/live_sanitized/README.md) (#189, reviewed and merged by the owner): `pss5`'s profile, `FindUserRanking` for `pss1` and `pss5`, `ListPvPBattles2` for `pss1` (10 battles) and `pss5` (none), and `pss1`'s settings field summary. `pss1`'s profile is not committed: it is an account created in 2018 whose creation date and lifetime PvP totals could identify it.
- Parsers built and tested on them: profile and ranking (#199), battle history (#200).
- Five read-path entries in the [compatibility ledger](../../baseline/compatibility-ledger.md), `dynamically-demonstrated` at `1.0`.

## Findings worth keeping

- The server recommends client version `1.0`, while `sdk/client.py` declares `0.999.59`. Reads still work.
- No read-only call returns the current user's trophies or starbux; only the login response does. SPEC-03's profile read therefore uses the login record once, within five minutes (S03-R3 note).
- Battle records report trophy counts of `0` for every participant, so they are read as unreported; no battle field holds the opponent's hull level.
- `pss1` is not a new account. It is in G1 (read-only) only; G2 execution is `pss5` only.

## Still to record

- The G2 `enrol` and `canary` steps (SPEC-05, SPEC-07, SPEC-11).
