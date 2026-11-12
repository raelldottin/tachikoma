<!---
This file is auto-generate by a github hook please modify README.template if you don't want to loose your work
-->
# //github.com/raelldottin/tachikoma 1.1.7-280
[![Daily Automated Actions](https://github.com/raelldottin/tachikoma/actions/workflows/daily-run.yml/badge.svg?event=schedule)](https://github.com/raelldottin/tachikoma/actions/workflows/daily-run.yml)

# Tachikoma - Pixel Starships Automation

This repository contains scripts and resources for automating tasks in the mobile game Pixel Starships. The project is built around a Python script `run.py` which interacts with various game elements to perform a series of automated tasks. It's designed to help players manage routine activities in the game more efficiently.

## Product roadmap

See the [Tachikoma Product Roadmap](docs/PRODUCT_ROADMAP.md) for the November 2026–June 2027 full-gameplay automation research program (R0–R6): account state, progression, PvP, economy, multi-account orchestration and adaptive optimization, each measured against manual play. The [capability inventory](docs/baseline/capability-inventory.md) records what the code does today and how strong the evidence is.

## GitHub validation and releases

Pull requests and merge queues run **Blocking CI**, ending in one
`CI required` result. Pushes to `main` run the broader Python/platform
matrix. Version tags such as `1.2.3` or `v1.2.3` validate the tagged source
and prepare a draft GitHub release with a reproducible source archive and
SHA-256 checksums. These validation workflows use mocked game traffic.

See [.github/workflows/README.md](.github/workflows/README.md) for the
check coverage, lint baseline, local commands, and branch-protection setup.

## Repository Contents

- `.dockerignore`, `.gitignore`: Ignore files.
- `.githooks/`: Custom Git hooks for the repository.
- `.github/`: GitHub Actions workflow `daily-run.yml` (runs 3×/day at 00:00, 08:00, 16:00 UTC).
- `.python-version`: Version-specific configurations.
- `LICENSE`: The license file.
- `README.md`: This readme file.
- `README.template`: Template for generating README files.
- `conf.py`, `index.rst`, `make.bat`: Sphinx documentation configurations.
- `pylintrc`: Configuration file for Python linter Pylint.
- `requirements.txt`: Required Python packages for the project.
- `run.py`: Main Python script to automate tasks in Pixel Starships.
- `sdk/`: Python package containing modules like `client`, `device`, `crew_leveling`, `security`, etc.
- `tests/`: Unit tests covering battle flow, security checksums, crew leveling, and PvP battles.

## run.py

`run.py` is the core script of this repository. It automates various tasks in Pixel Starships, such as resource collection, crew management, and more. The script provides a CLI for easy interaction and is configurable to either run as a guest or with user credentials.

### Key Features:

- Automated collection of resources and rewards.
- Crew and room upgrades management.
- Marketplace and messages handling.
- Optional email logging for monitoring script activities.
- End-to-end ship battle flow (CreateBattle9 → AcceptBattle5 → FinaliseBattle15).
- Crew stat formula integration from the Crew Planning and Training Guide.

### Progression dry run

`--progression-dry-run` logs in, then makes only read-only calls to build an account snapshot and load room and research design data. It plans the day with the `baseline-v1` policy (research, room upgrades, daily reward; see [docs/baseline/progression-policy.md](docs/baseline/progression-policy.md)) and prints the planned actions and every rejected candidate with its reason. Each planned action is recorded as a dry-run event in a JSONL journal, then the script exits. Nothing in the account changes, and the normal daily automation does not run.

```bash
python run.py --login-email you@example.com --progression-dry-run --account-alias primary
```

- `--account-alias` is the lowercase label written to the journal (default `primary`). E-mail addresses and display names are refused.
- `--progression-journal` sets the journal path. The default, `tachikoma-progression.log`, is covered by the repository's `*.log` ignore rule.

This command never executes. The only execute path is `--fleet-execute` (see [Fleet execute](#fleet-execute)); running it against a real account is each live trial's owner step.

### Fleet dry run

`--fleet-dry-run` runs the progression dry run for every enabled account in an account registry (see [docs/baseline/fleet-accounts.md](docs/baseline/fleet-accounts.md)), prints one Markdown report, and exits. Each account logs in with its own credentials, reads room, research and hull designs and its snapshots with read-only calls, and journals its plan. There is no execute path: nothing in any account changes.

```bash
export PSS_ACCOUNT_1_EMAIL=... PSS_ACCOUNT_1_PASSWORD=...   # from your secret store, never a file in the repository
python run.py --fleet-dry-run --registry fleet.json
python run.py --fleet-dry-run --registry fleet.json --concurrent 2 --identity-salt-env TACHIKOMA_FLEET_SALT
```

- **Credentials.** Each profile's `credentials_secret` names environment variables in one of two forms: `<NAME>` holds the authentication string that `--auth-file` takes, or `<NAME>_EMAIL` and `<NAME>_PASSWORD` hold the e-mail and password that `--login-email` and `--password-file` take. With `credentials_secret` `PSS_ACCOUNT_1`, the daily workflow's `PSS_ACCOUNT_1_EMAIL` / `PSS_ACCOUNT_1_PASSWORD` secrets work as they are. An account whose variables are missing, incomplete, or set in both forms ends with `error` and the other accounts still run. The fleet run never reads or writes the `.device` file.
- **Prefer the auth-string form.** With no stored device, the e-mail form starts from a fresh device key on every run, so each run repeats a guest `DeviceLogin17` session and the `UserEmailPasswordAuthorize4` call, which is derived from static analysis and not verified against the live server. The accounts table notes every account that uses it.
- **Secrets.** Credential values, raw user ids, e-mail addresses and the salt are kept out of the printed report, the journals and the log. Error text loses the account's credential values (raw and URL-encoded; for an auth string, also each of its key, token and id fields; values under 4 characters only through the field rules), every URL query string, `userId`-style ids, ids and names in dumped payloads, and e-mail addresses. While the fleet runs, a filter on the log handlers applies the same scrubbing to every record, replaces the client's display-name prefix with `[account]`, and drops the client's info and debug lines (login banners carry player names). The filter is removed when the run ends.
- **Output.** An accounts table (status, planned actions, accepted game identity fingerprint, journal, and the reason or design-data note), then the fleet report and the alerts, as in `--report fleet`, with the isolation audit applied.
- `--fleet-journal-dir DIR` holds one `<alias>.log` journal per account (default `tachikoma-fleet-journals`, covered by the `*.log` ignore rule). Journals are appended to, so the report and alerts include earlier runs.
- **Dry-run stamp.** Each account's plan events are held until its dry run ends. When it ends `completed`, they are journaled with a stamp in `evidence`: `dry_run_policy` (the profile's policy id), `dry_run_registry` (the first 16 hex digits of the registry's SHA-256) and `dry_run_id`. Otherwise they are journaled as before, without a stamp. `--fleet-execute` requires this stamp.
- `--concurrent N` runs up to `N` accounts at once with the concurrent orchestrator. Without it, accounts run one at a time in alias order. Each account has its own HTTP session, but all accounts share the client's HTTP connection pool and its process-wide rate limit (30 `Client.request` calls a minute).
- `--identity-salt-env NAME` reads the game identity salt from that environment variable, so each account's logged-in game account is fingerprinted and checked against the registry's `expected_game_identity`. The salt is never printed. Without it, fingerprints are unknown and a profile that declares one ends with `error`.
- Hull designs come from the read-only `listAllDesigns4` call. The game server has deprecated it, so hull designs usually stay empty (only `hull-reserve-v1` reads them), and the accounts table says so.
- A missing or invalid registry, an unset or too short salt, or a journal directory that cannot be created prints an error and exits with code 2. Otherwise the command exits with 0, even when some accounts ended with `error`. `--fleet-dry-run` refuses the single-account login, SMTP, battle, progression and report flags.

### Account baselines

`--baseline` records the starting state of every enabled account in an account registry ([SPEC-04](docs/specs/SPEC-04-account-baselines.md)): it logs each account in as `--fleet-dry-run` does, takes one snapshot with the snapshot provider's three read-only calls, writes one baseline file per alias, prints a summary, and exits. It reads no design data, plans nothing and has no execute path: nothing in any account changes.

```bash
python run.py --baseline --registry fleet.json --output baselines/
python run.py --baseline --registry fleet.json --output baselines/ --concurrent 2 \
  --identity-salt-env TACHIKOMA_FLEET_SALT --baseline-summary docs/experiments/baselines/README.md
```

- **Files.** Each account's snapshot is written to `--output DIR` as `<alias>-<UTC time>.json` in the [baseline format](docs/baseline/account-snapshot.md#baseline-files); unobserved values are `null`. A file that already exists for the same alias and UTC second is never overwritten: that account ends with `error` and the file is left as it was. Baseline files are kept private by default (SPEC-04, S04-Q2); only the summary table is meant to be committed.
- **Game version.** `game_version` comes from the server's latest-version setting, the `GetLatestVersion3` reply that login already reads, never the `ClientVersion` this build declares. The live server reports `RecommendedVersion` `1.0` and a `ServerSettingVersion` counter, so a version reads like `1.0+settings.<n>`: the counter is what changes when the game data does. When neither is present, `game_version` is `null`.
- **Accounts never mix.** With `--identity-salt-env NAME`, each file carries the game identity fingerprint, and an account whose fingerprint differs from its `expected_game_identity` or was already observed for another alias is `refused` and not written.
- **Output.** An accounts table (status `written`, `error`, `refused` or `disabled`, the file name, the game version and the reason or notes), then a summary table of every baseline file in `--output`: alias, observation time (UTC), game version, hull bracket (as in the PvP cohorts) and the sections that were not observed. A file there that is not a valid baseline is listed by name with the reason. `--baseline-summary PATH` also writes that table to `PATH`, after the secret scan.
- **Secrets.** The dry run's scrubbing applies to the log and to every printed reason, and each file passes the baseline secret scan before it is written.
- A missing or invalid registry, an unset or too short salt, or an output directory that cannot be created prints an error and exits with code 2. Otherwise the exit code is 0 when every enabled account was written, and 1 when any was not. `--concurrent N` works as in the dry run. `--baseline` refuses the other fleet, report, single-account login, battle, progression and SMTP flags, and `--fleet-journal-dir` (it writes no journal).

### Read captures

`--capture-reads` produces the sanitized captures [SPEC-03](docs/specs/SPEC-03-verified-profile-reads.md) needs (S03-R1) in one command, without anyone editing a response by hand. It logs each enabled registry account in as `--fleet-dry-run` does, then makes only read-only calls, sanitizes and scans every response, writes the clean ones and a manifest, and exits. Run it only on an owned test account.

```bash
python run.py --capture-reads --registry fleet.json --output captures/
python run.py --capture-reads --registry fleet.json --output captures/ --identity-salt-env TACHIKOMA_FLEET_SALT
```

- **Reads.** The profile (the `User` record login holds: trophies, starbux) and the `GetLatestVersion3` server settings come from login itself; then `getShipByUserId`, `listAllCharactersOfUser`, `listItemsOfAShip`, `findUserRanking` and `listPvPBattles2` (25 battles). All are `read-only` in the [capability inventory](docs/baseline/capability-inventory.md). No other call is made, and an expired session is reported as an error instead of logging in again.
- **Sanitizing.** Before anything is written, every value in a token, secret, password, checksum, e-mail or device key field becomes `***REDACTED***`; every `Id`, `AccountId` and `*UserId` value becomes `USER-A`, `USER-B`...; player, ship and alliance names become `PLAYER-A`, `SHIP-A`, `ALLIANCE-A`...; and any other value that `sdk/redaction.py`, the account's credential values or the S03-R2 scan would flag becomes `***REDACTED***`. Placeholders are stable per account (the account itself is `USER-A` and `PLAYER-A` in every file), so cross-references still line up. The sanitized text then gets the S03-R2 scan (`sdk/state/capture.py`, the same rules as `tests/test_profile_reads.py`); a response that does not scan clean, or is not well-formed XML, is withheld and only the rule and line are reported, never the value.
- **Files.** `--output DIR` must be new or empty and outside `tests/fixtures/`. Each clean response goes to `<alias>/<endpoint>.xml` (`.json` when it is not one XML document), and `manifest.json` records per alias and endpoint the status, the UTC time, the server's game version (as in `--baseline`), the sanitization steps applied, the scan result and a ready row for the [capture record](tests/fixtures/live_sanitized/README.md#record-of-captures). Files are never overwritten.
- **Review.** The command never writes into `tests/fixtures/`. The owner reviews each file (names the scan cannot recognise, and ids such as `ShipId` or `BattleId`), copies it into `tests/fixtures/live_sanitized/`, adds its row and runs the scan there.
- **Accounts never mix.** Each account has its own placeholders. With `--identity-salt-env NAME`, an account whose game identity differs from its `expected_game_identity` or was already observed for another alias is `refused` before any read.
- A missing or invalid registry, an unset or too short salt, or an output directory that is not empty, is inside `tests/fixtures/` or cannot be created prints an error and exits with code 2 before any login. Otherwise the exit code is 0 when every endpoint of every enabled account was written, and 1 when any was not. `--capture-reads` refuses every other fleet, baseline, report, single-account login, battle, progression and SMTP flag, and `--concurrent`.

### Fleet execute

`--fleet-execute` is the only command that changes an account ([SPEC-05](docs/specs/SPEC-05-live-execution-gate.md)). It runs the plan of every enabled registry account under an owner-written authorization file (format in [docs/baseline/fleet-accounts.md](docs/baseline/fleet-accounts.md#live-execution-authorization)), then prints the reconciliation and writes a run record. Never point it at an account the authorization does not name.

```bash
python run.py --fleet-dry-run --registry fleet.json --identity-salt-env TACHIKOMA_FLEET_SALT
python run.py --fleet-execute --registry fleet.json --authorization authorization.json \
  --identity-salt-env TACHIKOMA_FLEET_SALT --plan-only
python run.py --fleet-execute --registry fleet.json --authorization authorization.json \
  --identity-salt-env TACHIKOMA_FLEET_SALT --mode canary
```

- **Refused before any login** (exit code 2, every reason listed, no value repeated) when: the registry or the authorization is missing or invalid; the authorization does not cover the current time, the mode, or every enabled account (an enabled account it does not name refuses the whole run); `--identity-salt-env` is missing, unset or too short; an enabled profile has no `expected_game_identity`; an enabled account has no qualifying dry run; or the fleet stop file exists.
- **Dry run first.** Each enabled account needs a completed `--fleet-dry-run` in its journal from the last 24 hours, of the same registry and policy. The command reads the latest dry-run stamp in `<journal dir>/<alias>.log` and refuses when there is none, when its policy id or registry stamp differs from the current registry, or when it is older than 24 hours or dated in the future. Any registry change, including adding an `expected_game_identity`, needs a new dry run. A dry run whose plan was empty leaves no stamp.
- `--mode canary` (the default) executes at most one action per account: the first safe-to-retry action of its fresh plan that the authorization allows (today only `collect_all_resources` is safe to retry). If there is none, the canary plans one `collect_all_resources` instead, when the authorization allows it and minerals or gas is observed, so the collection can be reconciled; otherwise the account executes nothing. A collection's journal evidence lists, per room design, the `CapacityUsed` before and after the call (from the ship records the snapshots already read, never room ids), with the design's name, room type, manufacture type and capacity, and notes when no room held anything before a `not-observed` collection. The fill is left unobserved when the ship's rooms were not read fresh on both sides. It never changes the reconciled outcome. `--mode full` executes the authorized part of the plan. Each mode must be listed in the authorization's `modes`.
- **Authorized actions only.** The plan is made from a fresh snapshot as in the dry run. Requests for actions the authorization does not allow, or at a time it does not cover, are rejected and never attempted. Budgets are the tighter of the registry's budget and the authorization's cap for minerals and gas, and are passed to the policy as spend caps. Starbux spending is always disabled, whatever the registry says. With `run_pvp_battle` authorized, each battle is checked against the authorization's battle cap using the account's journal events, and is refused when the journal cannot be read.
- **Stops.** An account stops on its first `contradicted` result, on an `unknown` result of a non-idempotent action, on an isolation refusal (wrong or duplicate game identity, foreign snapshot or journal), when an action's planned cost plus this period's charged spend would exceed a cap, when a non-idempotent action other than a battle has no planned cost (its spend could not be checked against the caps), when the battle cap is full, when a battle would start with less than 1 of a capped mineral or gas budget left (a battle first rearms through `RebuildAmmo3`, whose cost the repo does not record), and on a previous uncertain attempt. Each refusal is journaled as a `skipped` event. The fleet stops when `<journal dir>/STOP` exists or when more than `--halt-share` (0 to below 1, default 0, so any stopped account halts the rest) of the enabled accounts stopped. Both are checked before each account and between actions, so accounts still running stop before their next action; in concurrent mode, an action already in flight finishes.
- **Record.** Every attempt is journaled in the account's journal. Each run writes `<journal dir>/runs/<UTC time>.json` with the authorization hash and owner, the registry hash, each account's policy version, the game version (the client version this build declares, and the server's game version read at each login: per account, and at the top level when every account agrees, else `null`), start and end times, the stop reason, and per account the status, reason, dry-run id, game identity fingerprint, counts, spend, remaining budget, each result's attempt id and outcome, and the plan's rejected candidates with their reasons.
- **Output.** The accounts table, a reconciliation table (one row per action: attempted, outcome, reason), the fleet report, the alerts and an "After the run" list (dashboard path, summary e-mail status, any post-run error). The exit code is 0 when every enabled account completed, the fleet did not stop, the alerts and the dashboard were produced and no critical alert fired, otherwise 1.
- **After the run** ([SPEC-11](docs/specs/SPEC-11-alert-delivery.md)). Once every account has finished, the default [alert rules](docs/baseline/alerts.md) are evaluated over the registered accounts' journals and the isolation audit, and the alerts go into the run record (`alerts`). The dashboard (as `--report dashboard`, with the registry) is written atomically to `--dashboard-out PATH`, by default `<journal dir>/dashboard.html`; the record names it (`dashboard`). A critical alert makes the exit code 1 only then, so every account still runs. If a journal cannot be read, the alerts cannot be evaluated or the dashboard cannot be written, the reason goes into the record's `post_run_errors` and the exit code is 1; the run record is still written. `--dashboard-out` must not name a journal, the registry, the authorization or the stop file, and its directory must exist (checked before any login). `--plan-only` writes no dashboard and sends no e-mail.
- **Summary e-mail** (off by default). With all three of `--smtp-email`, `--smtp-password-file` and `--recipient`, the run sends one plain-text e-mail through the same SMTP server as the log delivery, after the run record is written: the alert counts by severity in the subject, the account statuses, the run record and dashboard file names, and the alerts listed under Critical, Warning and Info. Subject and body pass through the fleet scrubber. Without the flags nothing is sent. One or two of the flags, or an unreadable or empty password file, refuse the run before any login. A failed delivery is logged (scrubbed) and shown, and does not change the exit code.
- **Secrets.** The dry run's scrubbing applies: credentials, the salt, the SMTP settings, raw user ids and display names stay out of the output, the log, the journals, the run record, the alerts and the summary e-mail.
- `--plan-only` makes the same checks, then prints, from each account's latest dry run, what the run would execute, which planned actions the authorization does not allow, and the caps. It never logs in, executes or writes a run record. The real run plans again, so it can differ.
- `--concurrent N` and `--fleet-journal-dir DIR` work as in the dry run. `--fleet-execute` refuses `--fleet-dry-run` and the single-account login, battle, progression and report flags, and takes the SMTP flags only for the summary e-mail; `--authorization`, `--mode`, `--plan-only`, `--halt-share` and `--dashboard-out` are refused without it.

### Offline reports

`--report` prints a report from local journal files and exits (`dashboard` writes an HTML file instead). It needs no login: it never creates a device or client, prompts for a password, or opens a network connection.

```bash
python run.py --report progression --journal tachikoma-progression.log
python run.py --report pvp --journal battles.log --report-format json
python run.py --report fleet --journal alpha.log --journal bravo.log --registry fleet.json
python run.py --report dashboard --journal alpha.log --journal bravo.log --registry fleet.json --output dashboard.html
```

- `progression`: per-account, per-day verified actions, success rate and failure ledger (see [docs/baseline/progression-metrics.md](docs/baseline/progression-metrics.md)).
- `pvp`: the battle scorecard. No experiment schedule is applied, so every strategy shows as `none`.
- `fleet`: throughput per account and for the fleet. Events are grouped by their own `account_alias` across all `--journal` files, so the file an event came from does not matter. With `--registry`, the isolation audit runs too and its incidents are counted. Because events are grouped by alias, the audit's `foreign-event` rule cannot fire here; the other rules apply.
- `dashboard`: one self-contained HTML page with summary tiles (attempted, verified, success rate, uncertain, incidents), the progression table, the PvP scorecard, the fleet table, the audit incidents and the failure ledger. Rates show their sample size; values that were not observed show as `n/a`, and without `--registry` the incident count is `n/a` because the audit did not run. The page has no scripts, external assets or network references, escapes every value, and follows the system light or dark theme. It holds no generation time, so the same journals give the same file. `--output PATH` is required; the file is replaced atomically (owner-only permissions), its directory must exist, it may not be one of the `--journal` files or the `--registry` file, and the path is printed. `--output` is refused with any other report.
- `--journal PATH` is repeatable and required. A missing or malformed journal, or an invalid registry, prints an error and exits with code 2. An empty journal prints the report's empty output.
- `--report-format` is `markdown` (default) or `json`. `--registry` is read only for `fleet` and `dashboard`. The dashboard ignores `--report-format`.

### Battle Flow

The `--run-battle` flag triggers an end-to-end ship battle:
1. Ship HP check (must be 100%)
2. Rearm ship (restock ammo)
3. HeartBeat4 (keep session alive)
4. CreateBattle9 (initiate battle — **verified working** against live server)
5. AcceptBattle5 (accept the battle — **verified working**: `MD5(accessToken + battleId + clientDateTime + SavyChecksum)`)
6. FinaliseBattle15 (finalize battle — **verified working**, battle accepted)

All 3 battle endpoints verified against live server with 5 accounts across CI run 31752041641.

### Crew Stat Formulas

Integrated from the *Crew Planning and Training Guide* by Raisinbunhk:
- `compute_final_stat(base, TP, equipment)` — final stat = base × (1 + TP/100) + equipment
- `compute_final_ability(base, TP, equipment%)` — final ability = base × (1 + TP/100) × (1 + equipment%)
- `compute_final_stamina(TP, equipment)` — final stamina = TP + equipment (additive)
- `get_tp_cap(rarity)` — training point caps: 3★=70, 4★=80, 5★=90, 6★=100, 7★=110
- `get_crew_level_cap(ship_level)` — level cap = ship_level × 4, max 40

### Ship Layout Analysis

Read-only layout evaluation — no HTTP mutations, no room-moving endpoints (PSS API doesn't expose them). The `analyzeShipLayout()` method fetches ship data and room designs, then evaluates:
- **Armor coverage**: percentage of critical rooms (reactors, weapons, shields) with adjacent armor
- **Repair proximity**: average distance from repair rooms to critical rooms
- **Weapon distribution**: left/right balance of weapons across the ship
- **Power balance**: reactor power generated vs estimated consumption
- **Overall defense score**: weighted average of the above (0-100)

Logs actionable recommendations: exposed critical rooms, missing repair/weapon/reactor rooms, weapon imbalance, bedroom capacity, pending upgrades. Runs automatically in the daily loop.

### Wiki Game Formulas

All 9 formulas from the [Pixel Starships Wiki](https://pixelstarships.fandom.com/wiki/Formulas) integrated as pure functions:

| # | Formula | Module | Key Functions |
|---|---------|--------|---------------|
| 1 | Room Reload | `sdk/game_formulas.py` | `room_reload_boosted`, `room_reload_powered`, `room_reload` |
| 2 | Crew Stat Buff | `sdk/crew_leveling.py` | `compute_final_stat`, `compute_final_ability`, `compute_final_stamina` |
| 3 | Escape | `sdk/game_formulas.py` | `escape_chance`, `escape_rate` |
| 4 | Dodge | `sdk/game_formulas.py` | `dodge_evasion` (post-July 2023 formula, capped at 80%) |
| 5 | Damage Reduction | `sdk/game_formulas.py` | `damage_reduction`, `effective_damage` |
| 6 | Fire Damage | `sdk/game_formulas.py` | `fire_damage_reduced`, `fire_crew_damage`, `fire_ap_damage` |
| 7 | Crew Stat by Level | `sdk/game_formulas.py` | `crew_stat_at_level` (ease_out / linear / ease_in) |
| 8 | Gas Draw Price | `sdk/game_formulas.py` | `gas_draw_price` (capped at 2M) |
| 9 | Trophy Gain/Loss | `sdk/game_formulas.py` | `trophy_gain` (capped 1-40) |

## Setup & Usage

1. Clone the repository to your local machine.
2. Install the required Python packages: `pip install -r requirements.txt`.
3. Run the script using Python: `python run.py`.

   Options:
   - `--auth-file`: Authentication string file for the game.
   - `--login-email`: Email for game login (password will be prompted or read from `--password-file`).
   - `--device-key`: Permanent device key (if not provided, generates new one).
   - `--smtp-email`: Email for SMTP (if email logging is desired; with `--fleet-execute`, the alert summary).
   - `--smtp-password-file`: Path to file containing SMTP password.
   - `--recipient`: Recipient email for the log.
   - `--password-file`: Path to file containing game password (for CI).
   - `--run-battle`: Run end-to-end ship battle.

## Contributing

Contributions to enhance the script's functionality or efficiency are welcome. Feel free to fork the repository and submit pull requests.

## License

This project is licensed under the BSD 2-Clause License. See [LICENSE](LICENSE).

---

This README provides a basic overview of the repository. For more detailed information on specific components, please refer to the respective files or the source code comments.
