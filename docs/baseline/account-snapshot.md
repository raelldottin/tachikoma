# Account snapshot (R1 / AUTO-002)

`sdk/state/` turns responses that `sdk.client.Client` already parsed into a frozen, typed `AccountSnapshot` for one account. It performs no network traffic, so it can be tested offline and reused by the action catalog, journal and runner.

## Contract

- **Identity.** Every snapshot carries an `account_alias`, a lowercase label such as `alpha` (`^[a-z0-9][a-z0-9_-]{0,63}$`). E-mail addresses and display names are refused. `diff_snapshots` raises `AccountMismatchError` for two different aliases, so one account's state can never be credited to another. A snapshot may also carry a [game identity](#game-identity) fingerprint of the logged-in game account.
- **Provenance.** `observed_at` must be timezone-aware. `source` names where the data came from, and `game_version` records the client version when known.
- **Completeness.** Each section (`profile`, `ship`, `resources`, `crew`, `rooms`, `research`, `inventory`) is `complete`, `partial` or `missing`:
  - a section that was not supplied is `missing`, and its values are `None` or empty;
  - a record without its identifier (`@RoomId`, `@CharacterId` and so on) is skipped, the section becomes `partial`, and a note records the skip;
  - an unparseable number becomes `None`, never `0`.
- **Secrets.** Only whitelisted gameplay fields are copied, so `@Email`, tokens and device keys in raw payloads never reach the snapshot. A copied text field that `sdk.redaction` would redact (for example an e-mail address in a crew name) is refused with `SnapshotInputError`.

## Diffs

`diff_snapshots(before, after)` reports the observed change: resource, trophy and hull deltas, crew level changes, room design and status changes, research state changes, and inventory quantity changes.

- `None` means unknown, never "no change".
- `crew_training` lists `(design, end)` changes, but only for crew whose training was observed in both snapshots.
- A keyed section that is missing on either side is listed in `unknown_sections`, and its mapping is empty. A missing section is therefore never reported as rooms or items being removed.
- An item absent from an observed inventory counts as quantity 0. An item with an unparseable quantity yields `None`.

## Field sources

| Snapshot field | Parsed attribute |
| --- | --- |
| `trophies` | `User/@Trophy` |
| `hull_level` | `Ship/@ShipLevel` |
| `rooms` | `Room/@RoomId`, `@RoomDesignId`, `@RoomStatus` |
| `crew` | `@CharacterId`, `@CharacterDesignId`, `@CharacterName`, `@Level`, `@Fatigue`, `@RoomId`, `@TrainingEndDate`, `@TrainingDesignId` (training is observed only when `@TrainingEndDate` is present; empty means idle, and `0` means no design) |
| `research` | `Research/@ResearchDesignId`, `@ResearchState` |
| `inventory` | `@ItemDesignId`, `@Quantity` |
| `resources` | `minerals`, `gas`, `starbux`, supplied by the caller from `Client` totals |
| `game_identity` | Keyed fingerprint of `client.user.id`, computed by the live provider; never the raw id |

These attribute names come from existing client code and mocked tests. Whether they match the current live server has not been checked.

## Live provider (R2)

`ClientSnapshotProvider(client, alias)` is the snapshot provider for `sdk.execution.ActionRunner`. Each call makes exactly three read-only calls, `getShipByUserId`, `listAllCharactersOfUser` and `listItemsOfAShip`, and builds a fresh snapshot. It never calls a method that changes the account. A test fails if it calls any method the [capability inventory](capability-inventory.json) does not list as `read-only`.

- A read that raises, returns `False` or yields no payload leaves its sections `missing`. The method names are recorded in `failed_reads`.
- A container element that is present but has no records, such as `<Researches/>` on a new ship, is observed and empty: the section is `complete`, with no records. Only an absent container, or a failed read, leaves the section `missing`.
- Minerals and gas come only from inventory items whose `@ItemType`/`@Type` names them, with exactly one match each. Unlike `collectAllResources`, there is no fallback to item position.
- Starbux and trophies are not reported, so the `profile` section stays `missing`. The client only holds those values from login, and reporting them would mislabel stale data with the current observation time.
- A copied value that looks like a secret still raises `SnapshotInputError`. Refusing outright is deliberate.

## Game identity

`AccountSnapshot.game_identity` is an optional fingerprint of the game user id the client is logged in as. The alias is only a label the caller stamps on the snapshot, so the fingerprint is what lets the [fleet orchestrator](fleet-accounts.md#game-identity) tell whether the credentials logged in to the intended game account.

- **Field.** `None` means unknown. Snapshots built without it, including every snapshot from before it existed, have `None` and still work. `to_dict()` includes `game_identity`. `build_snapshot(..., game_identity=...)` accepts only `None` or a well-formed fingerprint, so a raw user id is refused with `SnapshotInputError`.
- **Format.** `game_identity_fingerprint(user_id, salt)` returns `gid1-` plus the first 32 hex characters (128 bits) of HMAC-SHA256 keyed with `salt`, over a fixed domain prefix and the canonical decimal id. The id must be a positive decimal integer; `None`, `0`, an empty or non-numeric id raises `SnapshotInputError`. The salt must be at least 16 bytes. Error text never repeats the id or the salt.
- **Provider salt.** `ClientSnapshotProvider(client, alias, identity_salt=...)` fingerprints `client.user.id`, the id `Client` stores at login and uses for `getShipByUserId`. Computing it makes no extra call. Without a salt, or when the id is unknown, `game_identity` is `None`. A salt shorter than 16 bytes fails when the provider is built. The caller supplies the salt and must keep it secret: game ids are small integers, so anyone holding the salt could confirm a guessed id.
- **Raw id.** The raw id is read inside the provider only. It is never stored on the provider or the snapshot, and never written to snapshot dicts, journals or error text.

## Baseline files

`sdk/state/baseline.py` stores one snapshot as a sanitized JSON file, the per-ship baseline of [SPEC-04](../specs/SPEC-04-account-baselines.md). It makes no network calls. `run.py --baseline` (below) takes the snapshots and writes the files and the summary table.

- **Write.** `write_baseline(snapshot, directory, game_version)` writes `<alias>-<YYYYMMDDTHHMMSSZ>.json` (UTC, to the second) and returns the path. `game_version` is the version the observation was made on; SPEC-04 says it must come from the server's latest-version setting. `None` means it was not observed.
- **No overwrite.** The file is created exclusively. If a file with that name already exists, whoever wrote it, `BaselineExistsError` is raised and the file is left untouched. Two snapshots of one alias in the same UTC second count as the same time.
- **Load.** `load_baseline(path)` returns an `AccountSnapshot` equal to the one written, so `diff_snapshots(load_baseline(path), later)` works. `load_baseline_file(path)` also returns the recorded `game_version`. Loading is strict: an unknown or missing key, a wrong type, a time not in UTC, or a section whose data disagrees with its completeness raises `BaselineError`.
- **Sanitized.** Nothing is written or loaded if the text fails the secret scan below. The error names the rule that matched, never the value. The snapshot whitelist already leaves out e-mail, tokens, device keys, the raw user id and the player's name. `game_identity` is kept, because it is a keyed fingerprint; a raw user id there is refused.

### Format (version 1)

Keys are sorted and indented by two spaces, so the same snapshot always produces the same bytes.

| Key | Value |
| --- | --- |
| `format`, `format_version` | `"tachikoma-account-baseline"`, `1` |
| `account_alias` | The lowercase alias; one account per file |
| `observed_at` | ISO 8601 time in UTC (`+00:00`), with microseconds when present |
| `game_version` | Game version of the observation, or `null` |
| `client_version` | The snapshot's own `game_version` (the client's), or `null` |
| `game_identity` | `gid1-` fingerprint, or `null` |
| `source` | Where the snapshot came from |
| `hull_level`, `trophies` | Integer, or `null` when not observed |
| `resources` | `{"minerals", "gas", "starbux"}`, each an integer or `null` |
| `rooms` | List of `{"room_id", "design_id", "status"}`, or `null` when the section is missing |
| `research` | List of `{"design_id", "state"}`, or `null` |
| `crew` | List of `{"character_id", "design_id", "name", "level", "fatigue", "room_id", "training_observed", "training_design_id", "training_end"}`, or `null` |
| `inventory` | List of `{"item_design_id", "quantity"}`, or `null` |
| `completeness` | Every section (`profile`, `ship`, `resources`, `crew`, `rooms`, `research`, `inventory`) mapped to `complete`, `partial` or `missing` |
| `notes` | Builder notes, such as skipped records |

A list section is `null` exactly when its completeness is `missing`. An observed section with no records is `[]`. Values inside a record are `null` when unparseable, never `0`. A room's level is not a separate field: the room design id identifies it. `crew.name` is the crew member's name, not the player's.

### Secret scan

`scan_baseline_directory(directory)` scans every file under a directory, such as `docs/experiments/baselines/`, and returns findings (path, line, rule) without the matched text. A test runs it over `docs/experiments/baselines/`. The rules are those SPEC-03 (S03-R2) sets for sanitized captures, implemented here in `scan_text`:

- any line that `sdk.redaction.redact_secrets` would change (tokens, JWTs, long base64 strings, device keys, e-mail addresses, passwords);
- a numeric id in a field named `Id` or ending in `UserId`, as an XML attribute (`UserId="…"`) or a JSON key (`"user_id": …`). Gameplay ids such as `room_id` or `@RoomDesignId` do not match, and an id of `0` does not count.

A `.json` file is also decoded and each string scanned, because JSON escaping would otherwise hide a pattern such as `"DeviceKey":"…"` inside a value. A file that is not UTF-8 text is reported rather than skipped.

### Baseline command

`run.py --baseline --registry PATH --output DIR` writes one baseline per enabled registry account (SPEC-04, S04-R1, S04-R3, S04-R6). Usage is in the [README](../../README.md#account-baselines).

- **Read-only.** `EnvironmentAccountFactory.open_baseline(profile)` in `sdk/fleet/factory.py` logs the account in with the same credential handling and `LogScrubber` as `--fleet-dry-run`, reads no design data, opens no journal, and calls the provider once: its three read-only calls and nothing else. No orchestrator, policy, scheduler or action runner is built, so there is no execute path. A test fails if any mutating or session method other than `login` is looked up.
- **Game version.** `server_game_version(client)` reads the `Setting` record of `client.latestVersion`, which `Client.login` fills from `SettingService/GetLatestVersion3`, the same record the design version reads use. It makes no call and returns the first of `@LatestVersion`, `@LatestClientVersion` and `@LatestVersionIOS` that looks like a version (`0.999.60`), else `None`. These attribute names are not verified live. `@MinimumClientVersion` (the oldest version the server accepts) is deliberately not used, and neither is the `ClientVersion` the client declares at login. The command gives the provider no version, so `client_version` is `null`.
- **Isolation.** Snapshots are claimed in alias order with `FleetClaims`, as in the fleet orchestrators: an identity that differs from the profile's `expected_game_identity`, or was already accepted for another alias in the same run, is refused and nothing is written for that alias.
- **Summary.** The summary table lists every `.json` file in the output directory that loads as a baseline, ordered by alias and time: alias, observation time, game version, `hull_bracket` of the hull level (`sdk/reports/battles.py`), and the sections whose completeness is `missing`. Files that do not load are listed with the reason, which never repeats a refused value.
