# Training report

`sdk/reports/training.py` reports crew training outcomes per account and for the fleet: training attempts by outcome, finishes, dry runs, skip reasons, consumables used and, from snapshots, training points gained, crew at their training cap and idle crew-hours in training rooms. It reads [journal](gameplay-journal.md) events and, optionally, [account snapshots](account-snapshot.md). It makes no network call.

```bash
python run.py --report training --journal alpha.log --journal bravo.log --report-format json
```

## API

- `build_training_report(journal_entries, snapshots=None, *, now, training_room_design_ids=None, max_gap_hours=26.0)` returns a `TrainingReport`. `now` must be timezone-aware (`TrainingReportError` otherwise). `snapshots` is one flat iterable for every account, grouped by each snapshot's `account_alias`. An empty `snapshots` is treated like `None`: not provided.
- `render_markdown(report)` (also `to_markdown`) and `to_json(report)` render it. Both pass the whole text through `sdk.redaction.redact_secrets`.
- Building blocks: `skip_reason(event)`, `parse_decision(event)`, `validate_decision_evidence(evidence)`, `contract_violations(event)`, `observations_from_journal(events, alias)`, `observations_from_snapshots(snapshots, training_room_design_ids=...)`, `crew_idle(observations, now=..., max_gap_hours=26.0)`, `snapshot_training_active(member, observed_at)`, `crew_label(account_alias, character_id)` and `is_failed_or_unknown(event)`. The [alert rules](alerts.md) use the same observations, idle rules, crew labels and failed-or-unknown test.
- For producers: `decision_evidence(*, character_id, decision, in_training_room, training_active, reason_code=None)` returns the evidence of a decision record at the current contract version. It does not check the values; test producers with `validate_decision_evidence`, which returns `()` for a valid record.
- Every timestamp (`now`, a record's `finished_at`, a snapshot's or observation's `observed_at`) must be timezone-aware; a naive one raises `TrainingReportError`. The journal reader accepts a naive `finished_at`, so `run.py --report training` catches the error and prints `error: finished_at must be timezone-aware` to stderr with exit code 2, like a missing or malformed journal.
- `TrainingReport` holds `generated_at` (`now`), `duplicates_dropped`, one `AccountTraining` per account, fleet `TrainingTotals`, `snapshots_provided`, `snapshot_training_points` (some snapshot crew member up to `now` carried a known `training_points` value) and `training_room_design_ids_provided`. An `AccountTraining` holds the training window, the counts of training records, observations and snapshots, its `TrainingTotals` and one `CrewTraining` row per crew member.

## Journal record contract

Training actions are events whose `action_id` is in `TRAINING_ACTIONS`:

| Action | Params |
| --- | --- |
| `add_training` | `character_id`, `training_design_id` |
| `finish_training` | `character_id` |
| `use_consumable` | `item_design_id` (text or number), optional `quantity` (positive integer, default 1) |

Decision records explain what training did for each crew member considered in a run: at most one record per crew member per run. They are events with `action_id="training_decision"`, `outcome="skipped"`, `attempted=False`, `dry_run` set to the run mode, a unique `attempt_id`, a free-text `reason` (never rendered) and this evidence (contract version 1, `CONTRACT_VERSION`):

```json
{"kind": "training_decision", "v": 1, "decision": "skip", "reason_code": "fatigued",
 "character_id": "<crew id>", "in_training_room": true, "training_active": false}
```

- `v` is the contract version. A record without `v` is read but counted as a `bad_version` violation; a record with any other `v` (including `"1"`, `1.0` or `true`) is counted as `bad_version` and not read at all, because its schema is unknown.
- `decision` is `start` or `skip`. `reason_code` is set on skips only, one of `complete`, `design_missing`, `fatigued`, `not_in_training_room`, `research_missing`, `tp_cap_reached`, `training_in_progress` and `unknown_role` (`SKIP_REASON_CODES`). A skip with an unknown or missing code counts as `unknown_reason_code` in the skip histogram, so it stays apart from the runner's legacy `other`.
- `in_training_room` is a bool. `training_active` is a bool, or `null` when it was not observed.
- The state fields describe the crew member at the record's `finished_at`. `training_active` is true only while the crew member's `training_end` is after that moment: training that finished but was not collected is not active, and producers must compute it that way.
- Room ids never go in the evidence.

Decision records are journal events, so the generic reports and rules count them as skipped events: they inflate the `skipped` counts in the progression and fleet reports. This is known and left for a follow-up. A runner skip event on a training action (for example `add_training` refused by its preconditions) is a separate signal from the decision record for the same crew member, so the skip histogram can count that crew member twice.

### Contract violations

Every breach of the record contract is counted per account and for the fleet, under one of these codes (`CONTRACT_VIOLATIONS`). One record can break several rules, and each counts.

| Code | Counted when |
| --- | --- |
| `missing_kind` | a `training_decision` record's evidence `kind` is not `training_decision` (the record is not read and no other rule is checked) |
| `bad_version` | `v` is missing (still read) or is not the integer 1 (not read) |
| `bad_decision` | `decision` is not exactly `start` or `skip` (for example `Skip`) |
| `missing_character_id` | `character_id` is missing or empty |
| `non_bool_in_room` | `in_training_room` is missing or not a bool |
| `bad_training_active` | `training_active` is present and neither a bool nor `null` |
| `unknown_reason_code` | a skip has a missing or unknown `reason_code` |
| `reason_code_on_start` | a start has a `reason_code` |
| `decision_attempted` | a decision record is marked `attempted` |
| `action_missing_param` | an attempted `add_training` or `finish_training` has no `character_id`, or an attempted `use_consumable` has no `item_design_id` |

A decision record with a bad field is still read for its other fields: for example a `Skip` record with valid state fields is an observation but not a skip.

The daily training step (`Client.manageTraining`, `sdk/execution/training.py`) is the producer. It runs `add_training`, `finish_training` and `use_consumable` through the action runner, and writes one decision record per crew member considered to `Client.training_journal`, under `Client.training_account_alias` (default `legacy`). A crew member that used a consumable is recorded as the skip the consumable stood in for. The daily run sets no journal yet, so its records stay in memory and real journals still give no decision skip codes. The snapshot `CrewMember` has no training points, so idle crew-hours show `0.00` or `n/a`, and the `training-stalled` alert is evaluated but stays silent until a journal is kept.

## Metrics

- **Input.** Records are de-duplicated by `(account_alias, attempt_id)`; the first occurrence wins and `duplicates_dropped` counts the rest. Records that finished after `now` and snapshots observed after `now` are ignored; exactly `now` counts. There is no lower bound: pass the window you want.
- **Accounts.** Every alias in the journals (any action) and every snapshot alias. An account without training shows zeros and `n/a`.
- **Training records.** Training actions plus decision records. The window is their earliest and latest `finished_at`.
- **Outcomes.** Over attempted training actions: `attempted` (the markdown row is `Training actions attempted`: it sums `add_training`, `finish_training` and `use_consumable`); `verified`, `not_observed` and `contradicted` by outcome; `unknown` for any other outcome (`unknown` or anything unexpected: `is_unknown_outcome`); `failed` when an `error_class` is set; `failed_or_unknown` when either an error class is set or the outcome is unknown in that same sense (counted once, `is_failed_or_unknown`, shared with the `training-failed-rate-high` alert). The `Training starts attempted (add_training)` row is the attempted `add_training` count, failed and contradicted attempts included, and the `Training starts verified (add_training)` row is the verified `add_training` count (each 0 when there is none). Also per action, for each training action that has any record. The failed-or-unknown rate is `failed_or_unknown / attempted`, rounded to 4 places, or `n/a` with no attempts.
- **Finishes.** Attempted `finish_training` with a verified outcome.
- **Dry runs.** Training actions that were not attempted and are marked `dry_run`.
- **Skips.** A decision record that is read with `decision: "skip"` gives its reason code, or `unknown_reason_code` (dry runs included). A training action that was neither attempted nor a dry run gives `precondition_failed` (reason starts with `preconditions failed`), `execute_gate_refused` (reason starts with `execute gate refused`), `uncertain_prior_attempt` (error class `UncertainPriorAttemptError`) or `other`. Only these fixed codes are shown, never the free-text reason.
- **Contract violations.** The [contract violation](#contract-violations) counts by code, or `none`.
- **Consumables used.** Verified, attempted `use_consumable`, summed by `item_design_id` (`unknown` when missing). A quantity counts only when it is a positive whole number written as an integer or a digit string; anything else (missing, `0`, `-2`, `2.0`, `"2.0"`, `x`) counts as 1.
- **TP gained** (snapshots only). Per crew member, the sum of the increases between consecutive known `training_points` values; `n/a` with fewer than two. The account total is the sum of the known values, or `n/a`.
- **Crew at TP cap** (snapshots only). A crew member is at cap when, in the most recent snapshot that shows both values, `training_points >= training_capacity`. The count is `n/a` when no crew member shows both.
- **Fleet.** Counts and maps are summed; optional values are summed over the accounts that have them, or `n/a`; the rate is recomputed from the fleet counts.

## Idle crew-hours in training rooms

Observations of a crew member come from:

- decision records that are read and whose evidence has a `character_id` and a real bool `in_training_room` (a non-bool `training_active` counts as unknown). Dry-run decision records count too: they observe the crew just as a real run does;
- verified, attempted `add_training` events with a `character_id` param (in a training room, training running);
- snapshots, only when `training_room_design_ids` is given: one per crew member per snapshot. A crew member with no room is out of a training room; one whose room is in the snapshot is in a training room when the room's design id (compared as text) is in the set; one whose room is not in the snapshot is not observed. `training_active` follows the decision record rule (`snapshot_training_active`): `None` when training was not observed (`training_active` is `None`) or `training_end` cannot be read, `false` when no training is set, else whether `training_end` is after the snapshot's `observed_at`. Finished but uncollected training is not active, so a crew member who sits in a training room after training ended is idle. `training_end` is read as an ISO 8601 text or a `datetime`; a naive value is read as UTC, the game server's convention.

An observation is idle when the crew member is in a training room and training is known not to run; an unknown state is not idle. Per crew member, observations at the same moment are idle only if all of them are. The interval between two consecutive observations counts as idle time only when both ends are idle and it is at most `max_gap_hours` (26h) long. Idle crew-hours are therefore an upper bound: training could have started and ended between two idle observations. The idle streak is the time from the start of the current idle run to the last observation: an idle observation starts a new run after a non-idle one or after a gap longer than the cap. Streaks end at the last observation, never at `now`. A crew member none of whose observations has a known (bool) `training_active` has `n/a` idle hours and streak, not `0.00`. Account idle crew-hours are the sum over its crew with a known state, rounded to 4 places, or `n/a` when no crew member has one. The CLI has no snapshots, so it reports idle time from decision records and verified `add_training` alone.

## Snapshot attribute contract

Snapshots are read duck-typed: `account_alias`, `observed_at`, `crew`, `room(room_id)`. A crew member needs `character_id`, `room_id`, `training_active` and, when training is active, `training_end`; training points come from the optional attributes `training_points` and `training_capacity` (read with `getattr`, so a `CrewMember` without them reports `n/a`). Only integers count (not bools).

## Privacy

Crew are shown as `crew_label(alias, character_id)`: `crew-` plus the first 8 hex characters of `sha256("<alias>:<character id>")`. The label stays the same across runs and crew changes, and the report and the `training-stalled` alert use the same label for the same crew member. It is a pseudonym, not a secret: anyone who knows the alias can test candidate character ids against it. Crew rows are sorted by label. Room ids and character ids are never printed. Free-text reasons are never rendered. The markdown and JSON pass through `redact_secrets`.

## Output

Markdown: a `# Training report` title, a line with the generation time, the account count and the dropped duplicates, then, when the report was built without snapshots (`snapshots=None` or empty, as in the CLI), the line `Snapshots not provided: TP gained and crew at TP cap are n/a.` (those rows still show `n/a`; JSON has `snapshots_provided`). With snapshots, one or two note lines may follow instead: `Snapshot crew carry no training points: TP gained and crew at TP cap are n/a.` when no snapshot crew member up to `now` has a known `training_points` value (JSON `snapshot_training_points`), and `Snapshots provided without training room design ids: idle crew-hours use journal records only.` when `training_room_design_ids` is empty (JSON `training_room_design_ids_provided`). Then a `## Fleet` metric table and action table, then per account a `## <alias>` section with its records line (or `No training records.`), metric table, action table (or `No training actions.`) and crew table (or `No crew observed.`). Missing values show as `n/a`; `|` is escaped. With no accounts, the report says `No training records or snapshots.` JSON is the report as a dict, keys sorted, two-space indent.

## Tests

`tests/test_training_report.py` compares the markdown and JSON for a fixed scenario with `tests/golden/training_report/fleet.md` and `fleet.json`. After an intended change, rewrite them with `TACHIKOMA_UPDATE_GOLDEN=1 make test` and review the diff.
