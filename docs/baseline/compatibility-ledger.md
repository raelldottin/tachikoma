# Compatibility ledger (R6, AUTO-015)

`sdk/compat/` records the game rules, endpoints and formulas the repository relies on, with the evidence for each and the game version that evidence was observed on. A check compares the ledger with a target game version and reports what has not been shown to hold there. Only strong evidence shows an entry holds. It is pure and offline: it reads no game state and opens no connection.

Every entry comes from a fact already recorded in the repository, and no version or date was inferred: where a source records none, the field is `None`. The read-path entries cite the sanitized captures from the owner-dispatched G1 runs of 2026-11-12 ([SPEC-03](../specs/SPEC-03-verified-profile-reads.md) S03-R7).

## Model

A `LedgerEntry` has:

| Field | Meaning |
| --- | --- |
| `entry_id` | Stable id, such as `endpoint.battle.accept-battle5` |
| `kind` | `endpoint`, `rule` or `formula` |
| `subject` | What the entry asserts |
| `evidence` | One or more `Evidence` records (below) |
| `code` | Repository symbols that rely on it, as `module:attribute`, such as `sdk.client:Client.createBattle9` |

Each `Evidence` record has an `evidence_class`, the `game_version` it was observed on (`None` when unknown), the `observed` date (ISO, `None` when unknown), the `source` (a repository path, or a public URL the repository already cites) and a `note`.

| Evidence class | Meaning |
| --- | --- |
| `official-release` | Released patch notes |
| `official-announcement` | An official announcement of something not yet released |
| `community` | Wikis, guides and forums |
| `static-analysis` | Derived from the game binary or metadata, not seen at runtime |
| `dynamically-demonstrated` | Seen in real traffic: captures, or a recorded live run |
| `unverified` | Asserted somewhere, with no evidence in the repository |

`official-release` and `dynamically-demonstrated` are strong. The other four are weak.

When two repository sources disagree about how strong a source is, the weaker class is recorded and the note says why. The `sdk/game_formulas.py` docstring calls the Fandom wiki official, while `docs/pss-pvp-research-digest.md` classes Fandom content as community, so the wiki formulas are `community`. Digest section 4 lists official patch notes and community wikis together without attributing each fact, so its battle rules are `community` too.

## Check

`check_compatibility(target_version, entries=None, declared_client_version=None)` returns a `CompatibilityReport`. With `entries` omitted it uses the shipped `LEDGER` and `DECLARED_CLIENT_VERSION`.

| List | An entry is listed when |
| --- | --- |
| `current` | Strong evidence names the target version or newer |
| `weak_at_target` | Evidence names the target version or newer, but none of that evidence is strong |
| `stale` | At least one evidence record names a version, and every named version is older than the target |
| `unknown_version` | No evidence record names a version |
| `weak_only` | No evidence record is strong |

Every entry is in exactly one of `current`, `weak_at_target`, `stale` and `unknown_version`, and may also be in `weak_only`. Weak evidence never makes an entry current: an entry with an official release at `0.999.55` and only a community report at `1.0` is `weak_at_target` against `1.0`, not `current`, even though it is not `weak_only`. `flagged` is every entry that is not `current`: the distinct ids in `stale`, `weak_at_target`, `unknown_version` and `weak_only`. `summary()` gives the counts, `clean` (entries not flagged) and `client_behind_target`, which is true when the client declares an older version than the target. `to_dict()` is JSON-ready and `render_text()` prints the lists.

Versions parse as dot-separated integers with an optional `V` prefix (`0.999.59`, `V0.999.55`, `1.0`) and compare numerically, with missing trailing parts counting as zero. Anything else raises `ValueError`.

```python
from sdk.compat import check_compatibility

print(check_compatibility("0.999.60").render_text())
```

For the shipped ledger of 36 entries:

| Target | Current | Weak at target | Stale | Unknown version | Weak only | Clean | Client behind target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `0.999.55` | 9 | 1 | 0 | 26 | 22 | 9 | no |
| `0.999.59` | 6 | 1 | 3 | 26 | 22 | 6 | no |
| `0.999.60` | 6 | 0 | 4 | 26 | 22 | 6 | yes |
| `1.0` | 5 | 0 | 5 | 26 | 22 | 5 | yes |

`rule.client.declared-version` is `unverified`, so it is never `current`: up to `0.999.59` it is `weak_at_target`, and above that it is `stale`.

## Entries

### Read path (G1 captures, 2026-11-12)

All five are `dynamically-demonstrated` at `1.0`, observed 2026-11-12, citing [the live captures](../../tests/fixtures/live_sanitized/README.md). `1.0` is the `RecommendedVersion` the server's settings reported in the same runs; the client still declares `0.999.59`, so `client_behind_target` is true from `0.999.60` on. The runs and fixes are recorded in [g1-evidence.md](../experiments/live/g1-evidence.md).

| Entry | Kind | Evidence |
| --- | --- | --- |
| `endpoint.user.device-login17-profile` | endpoint | the login `User` record holds `@Trophy`, `@Credits` (starbux) and `@HighestTrophy` |
| `endpoint.ladder.find-user-ranking` | endpoint | `@Ranking`, `@TotalUsers`, `@TotalActiveUsers`; no trophy count |
| `endpoint.battle.list-pvp-battles2` | endpoint | `Battle` records under `ListPvPBattles/Battles`; trophy counts in records were all `0`, read as unreported |
| `endpoint.ship.get-ship-by-user-id-empty-containers` | endpoint | a new ship's `<Rooms/>` and `<Researches/>` are empty (fixed in #183) |
| `endpoint.setting.get-latest-version3-fields` | endpoint | no `LatestVersion*`; `RecommendedVersion`, minimum versions and data counters such as `ServerSettingVersion` (read by #187) |

### Session and battle chain

| Entry | Kind | Evidence |
| --- | --- | --- |
| `rule.client.declared-version` | rule | `unverified`, `0.999.59`, `sdk/client.py`. The `ClientVersion` the client declares on `DeviceLogin17` and sends to `FinaliseBattle15`, not a version the server reported |
| `endpoint.user.heartbeat4` | endpoint | `dynamically-demonstrated`, 2026-08-10, `sdk/security.py`: the checksum matched 267 mitmproxy captures taken 2026-08-08 to 2026-08-10 |
| `endpoint.battle.create-battle9` | endpoint | `dynamically-demonstrated`, `sdk/client.py` (checksum matched a capture); `dynamically-demonstrated`, 2026-08-13, `README.template` (live claim) |
| `endpoint.battle.create-battle9-response` | endpoint | `dynamically-demonstrated`, 2026-08-12, `sdk/client.py`: the `<CreateBattle>` wrapper seen in live CI run 31656147771 |
| `endpoint.battle.accept-battle5` | endpoint | `dynamically-demonstrated`, `sdk/client.py` (3 mitmproxy captures); `dynamically-demonstrated`, 2026-08-13, `README.template` (live claim) |
| `endpoint.battle.finalise-battle15` | endpoint | `static-analysis`, `sdk/client.py` (IL2CPP metadata); `dynamically-demonstrated`, 2026-08-13, `README.template` (live claim) |
| `rule.battle.outcome-observed` | rule | `unverified`, `docs/baseline/action-catalog.md`: a finalise response is never evidence of a win, and live outcomes stay unknown |

The live claim is README Battle Flow's statement that the three battle endpoints were verified with 5 accounts in CI run 31752041641, dated by commit 5377204. The [capability inventory](capability-inventory.md) classes it `live-claimed`: documentation, not re-verified. It records no game version, so every battle entry has an unknown version, even though `sdk/client.py` declared `0.999.59` at the time.

### Marketplace path

| Entry | Kind | Evidence |
| --- | --- | --- |
| `endpoint.market.list-active-marketplace-messages5` | endpoint | `unverified`, `docs/PRODUCT_ROADMAP.md` section 9 |
| `endpoint.market.sell-item3` | endpoint | `unverified`, `docs/PRODUCT_ROADMAP.md` section 9; not a `Client` method |
| `endpoint.market.purchase4` | endpoint | `unverified`, `docs/PRODUCT_ROADMAP.md` section 9; not a `Client` method |
| `endpoint.market.gift-item` | endpoint | `unverified`, `docs/PRODUCT_ROADMAP.md` section 9: no live route was established |

The roadmap's marketplace evidence is an operator-provided capture analysis that is not in the repository, and the roadmap says to treat it as historical evidence to reconcile, not proof. So the whole path is `unverified`.

### Official releases and announcements

| Entry | Kind | Evidence |
| --- | --- | --- |
| `rule.hull.max-level` | rule | `official-release`, `0.999.55`, 2026-06-17, [Hull 14 notes](https://blog.pixelstarships.com/2026/06/17/hull-14-major-update-patch-notes-v0-999-55/) |
| `rule.crew.cloak-movement-halved` | rule | `official-release`, `0.999.55`, 2026-06-17, `docs/pss-pvp-research-digest.md` |
| `rule.room.shield-reload` | rule | `official-release`, `0.999.58`, no date, `docs/pss-pvp-research-digest.md` |
| `rule.research.room-reinforcement` | rule | `official-release`, `0.999.60`, 2026-08-17, `docs/pss-pvp-research-digest.md` |
| `rule.release.pixel-starships-1-0` | rule | `official-announcement`, no version, 2026-10-16, [1.0 preview](https://blog.pixelstarships.com/2026/10/16/sneak-peek-pixel-starships-1-0/) |

The repository records no mechanics or release version number from the 1.0 preview. Against a `1.0` target, every entry is flagged.

### Community rules and formulas

All of these are `community` with no recorded version or date.

| Entry | Kind | Source |
| --- | --- | --- |
| `rule.pvp.battle-duration` | rule | `docs/pss-pvp-research-facts.json` (180 s) |
| `rule.pvp.defense-immunity` | rule | `docs/pss-pvp-research-facts.json` (8 h / 20 min) |
| `rule.pvp.matchmaking-range` | rule | `docs/pss-pvp-research-digest.md` |
| `rule.room.non-powered-armor` | rule | `docs/pss-pvp-research-facts.json` (300%) |
| `formula.crew.level-cap` | formula | `sdk/crew_leveling.py`, from the Crew Planning and Training Guide |
| `formula.crew.tp-caps` | formula | `sdk/crew_leveling.py`, same guide |
| `formula.crew.stat-buff` | formula | `sdk/crew_leveling.py`, same guide |
| `formula.room.reload` | formula | `sdk/game_formulas.py`, wiki formula 1 |
| `formula.escape` | formula | `sdk/game_formulas.py`, wiki formula 3 |
| `formula.dodge` | formula | `sdk/game_formulas.py`, wiki formula 4 (labelled post-July 2023) |
| `formula.damage-reduction` | formula | `sdk/game_formulas.py`, wiki formula 5 |
| `formula.fire-damage` | formula | `sdk/game_formulas.py`, wiki formula 6 |
| `formula.crew.stat-by-level` | formula | `sdk/game_formulas.py`, wiki formula 7 |
| `formula.gas-draw-price` | formula | `sdk/game_formulas.py`, wiki formula 8 |
| `formula.pvp.trophy-gain` | formula | `sdk/game_formulas.py`, wiki formula 9 |

## Consistency checks

`tests/test_compat_ledger.py` checks that:

- every capability-inventory method marked `live-claimed` has a ledger entry naming it in `code`;
- `DECLARED_CLIENT_VERSION` equals every `ClientVersion` literal in `sdk/client.py`;
- every `code` reference resolves, and every source is a file in the repository or a URL that `README.template`, the roadmap or the PvP research digest already cites;
- versions the sources do not record stay `None`;
- weak evidence on the target never makes an entry current, and every entry lands in exactly one of the four lists;
- the check opens no socket;
- every entry id appears on this page.

## Limits

- The ledger records evidence; it does not gather it. Only the read-path entries come from live captures; nothing else is re-verified against the live server.
- A new patch does not mark anything stale until someone records evidence with a version. Most entries have no recorded version, so they are reported as unknown rather than stale.
- Nothing in `run.py` calls the check yet.
