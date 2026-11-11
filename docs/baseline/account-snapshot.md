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
