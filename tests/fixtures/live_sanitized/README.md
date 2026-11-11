# Sanitized live captures

This directory holds sanitized copies of real Pixel Starships responses. Parsers in
`sdk/state/profile_reads.py` are built and tested against them
([SPEC-03](../../../docs/specs/SPEC-03-verified-profile-reads.md), S03-R1).

Only the owner adds captures here. An agent never makes a live request to get one.

Today the directory holds only this README. SPEC-03 expects these captures:

| Capture | Endpoint | Used by |
| --- | --- | --- |
| Current user's profile (trophies, starbux) | whichever read returns them; the capture decides | S03-R3 |
| Ranking | `FindUserRanking` | S03-R3 |
| Battle history | `ListPvPBattles2` | S03-R4 to S03-R6 |

## What must never appear

None of these may appear in any file here, this README included:

- access tokens, refresh tokens, device keys, checksums, passwords or `Authorization` headers
- JWTs, UUIDs, long hex strings or long base64 strings
- e-mail addresses
- user ids: a number in any field named `Id` or ending in `UserId` (`UserId`, `@UserId`, `@Id`,
  `AttackingUserId`, `DefendingUserId`, and so on), whatever the case
- player names, ship names and alliance names of real players, the owner's included
- anything from `.device`, `.private-fixtures/` or a secrets store

Game data is fine: design ids (`ShipDesignId`, `ItemDesignId`, `RoomDesignId`), levels, trophy
counts, starbux balances and timestamps can stay as captured.

## How to sanitize a capture

1. Capture the raw response on the owned test account only, and keep the raw file outside this
   repository.
2. Delete the request URL and headers. Keep only the response body.
3. Drop every token, device key, checksum and password field. If the field must stay for the
   parser, set it to empty (`accessToken=""`) or to the exact marker `sdk/redaction.py` produces
   (`***REDACTED***`, `***REDACTED_EMAIL***`).
4. Replace each user id with a stable non-numeric placeholder: `UserId="USER-A"` for the test
   account, `USER-B`, `USER-C` and so on for opponents. Use the same placeholder for the same user
   across all captures, so battle records still line up.
5. Replace each player, ship and alliance name with a placeholder such as `PLAYER-A`, `SHIP-A`,
   `ALLIANCE-A`.
6. Replace any other value that could identify the account (friend lists, message text, device
   names) with a placeholder, or delete it.
7. Keep everything a parser needs: battle ids, roles, outcomes, trophy changes, hull levels,
   trophies and starbux. If a battle id is a large number that could link back to the account,
   replace it with a placeholder such as `BATTLE-1` and note that in the table below.
8. Save the file under a name that says what it is, for example `list_pvp_battles2.xml`, and
   record it below.
9. Run the scan and fix every finding before committing:

   ```sh
   python -m unittest tests.test_profile_reads
   ```

   The scan reads every file here, recursively. Its failure message names the file, line and kind
   of finding, never the value.

## Record of captures

Add one row per file. A capture without a row is not used.

| File | Endpoint | Captured (date) | Game version | Sanitization steps applied | Reviewed by |
| --- | --- | --- | --- | --- | --- |
| *(none yet)* | | | | | |

"Game version" is the client version the response came from, as the game reports it. "Sanitization
steps applied" lists the step numbers above and anything done beyond them.

## What the scan checks

`tests/test_profile_reads.py` fails when any file here contains:

- a value one of the patterns in `sdk/redaction.py` would redact (an exact redaction marker is
  allowed, since redacting it again changes nothing)
- a number in a field named `Id` or ending in `UserId`, as an XML attribute, XML element, JSON key
  or query parameter, with or without a leading `@`
- an e-mail address
- a UUID, a hex string of 32 or more characters, or a non-empty, non-marker value in a field whose
  name contains `token`, `secret`, `password`, `checksum` or `devicekey`

The scan cannot recognise player names. Step 5 is checked by the reviewer.
