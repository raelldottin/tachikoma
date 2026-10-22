# Swarm worker report: t_730c8a08 — Inventory code & data-model investigation

Scope: WORKSTREAM A primarily, plus protocol evidence contributed to B. Investigation-only; no live mutations performed; no files in the repo changed by this worker.

## 1. Current inventory architecture (repo-truth)

- All inventory behavior lives in `sdk/client.py` (~3760 LOC) as methods on the `Client` class. There is no dedicated inventory module. Other domains are also embedded here (training, research, battles, drones, market).
- Supporting modules and their inventory relevance:
  - `sdk/crew_leveling.py` — pure stat/training math. Does NOT touch items.
  - `sdk/ship_layout.py` — room-grid parsing (image bytes). No item/module model.
  - `sdk/game_formulas.py` — XP curve constants only.
  - `sdk/security.py` — checksum primitives for auth, battle, marker, RebuildAmmo3, BuyReward2, etc. No CombineItem/ConsumeItem checksum helpers yet.
  - `sdk/redaction.py` — `redact_secrets`, used on logged URLs.
  - `sdk/commands.py`, `sdk/tui.py` — dispatch/UI; do not reference items today.

Conclusion: an `InventoryCatalog`-style layer should live in a new `sdk/inventory*.py` rather than grow `client.py`; `_extract_collection` (`sdk/client.py:50`) is the de-facto normalization primitive and should be reused or moved.

## 2. Data model today (Observed)

- `Client.itemsOfAShip` — raw `xmltodict` parse of `ItemService/ListItemsOfAShip` response (client.py:690-697). Normalized only at point of use by `_extract_collection(..., "Item")`.
- Item attributes referenced anywhere in code: `@ItemId`, `@ItemDesignId`.
- Resource entries in `RoomService/CollectAllResources` distinguish by `@Type`/`@ItemType` strings containing "mineral"/"gas" (client.py:2980-3012).
- `Client.itemDesigns` (raw) and `itemDesignActions` (raw) from `DesignService/ListAllDesigns4` (client.py:746-900). ItemDesign fields referenced: `@ItemDesignId`, `@ItemName`, `@ItemSubType` (`findConsumableDesignId`, client.py:1681-1702 — matches `@ItemSubType == "Consumable"` or name contains "Consumable", plus stat-type string match).
- `Client.trainingDesigns`, `craftDesigns`, `roomDesigns` — raw dicts; used for training selection (manageTraining) and room upgrades. No crafting-recipe consumption logic exists anywhere.
- No equipped-state, enhancement-level, room-module assignment, or stack-limit fields are currently consumed by code — though captures show servers emit them (see below).

## 3. Server-observed item/character fields (CAPTURED from ~/pss-mitm-capture.jsonl)

From a `ConsumeItem2` response (2026-08-16):
- Item element: `ItemId, ShipId, ItemDesignId="563", Quantity, IsNew, SkinKey` (no rarity/enhancement/equipped fields present on the post-consume Item node; quantity goes to 0 and the node persists in response).
- Character element includes `ItemIds="188871714"`, `CharacterDesignId`, improvement fields (`HpImprovement`, `AttackImprovement`, `StaminaImprovement`, ...), `TrainingDesignId`, `Fatigue`, `BoostLevel`. This proves equipment association = `Character/Items` child + `ItemIds` attribute pattern.
From an `ActivateItem4` response: `<ActivateItem success="True"><Situation SituationId=... SituationCategory="User" .../>` — i.e. some items activate into Situations (buffs), not crew stat changes.
From `MarkItemsNotNew` response: empty element — pure side-effect call.

## 4. Endpoint verification status — this worker's contributions (VERIFIED via hash recomputation against captures)

All verified with the standard new-style formula `MD5(preimage + ChecksumKey + SavyChecksum)`, CK="5343", SC="Savvy!s0d@":

| Endpoint | Preimage (order matters) | Samples | Notes |
|---|---|---|---|
| ItemService/ConsumeItem2 | clientDateTime + itemId + quantity + characterId | 3/3 unique MATCHES | Contradicts INVENTORY_ENDPOINT_ANALYSIS.md which listed it as no-checksum `?itemId=`. Actual URL: `itemId&quantity&characterId&clientDateTime&checksum&accessToken`. This is the consumable-use-on-crew call. |
| ItemService/ActivateItem4 | clientDateTime only | 1/1 | Params on wire: itemId, quantity, targetId — none in preimage. Only 1 sample; recommend 2nd capture before production. |
| ItemService/MarkItemsNotNew | clientDateTime + accessToken | 1/1 | Harmless UI-state endpoint, but confirms the pattern. |
| RoomService/CollectResources3 | (not attempted by this worker; 14 captures exist) | — | Has checksum param; static table says preimage roomId+amount+collectDate. |

INFERRED but not yet hashed: quantity semantics of DeleteItem3 (`remainingQuantity` param name suggests post-delete expected count, i.e. server-side reconciliation hint — UNVERIFIED).

Caveat on parameter elimination for ConsumeItem2: 3 samples all share the same characterId (55159886) and two share quantity=1, so alternative orderings that keep the verified relative order (ts, itemId, quantity, characterId) cannot be fully excluded; the listed order exactly matches wire order, which is the strong prior.

## 5. Existing tests

- `tests/test_e2e_live_fixes.py` covers `CollectAllResources` parsing incl. single-dict fallback; no ListItemsOfAShip parsing tests.
- No tests exist for `useConsumable`, `findConsumableDesignId`, or item-design classification.
- `tests/synthetic_fixtures.py` + `tests/fixtures/checksum_samples.json` provide checksum verification harness patterns this swarm should reuse for the three formulas above.

## 6. Recommended placement for inventory code (handoff to architect t_1a2fac28)

- New `sdk/inventory/catalog.py`: fetch+normalize ListItemsOfAShip / ListItemDesigns2 into typed records (ItemId, ItemDesignId, quantity, subtype, rarity, isNew, skinKey, equippedCharacterId from Character.ItemIds).
- New `sdk/inventory/checksums.py` (or extend `sdk/security.py`): `checksum_consume_item2(client_date_time, item_id, quantity, character_id, ck, sc)`, `checksum_activate_item4(client_date_time, ck, sc)`, `checksum_mark_items_not_new(client_date_time, access_token, ck, sc)` — all verified by this worker.
- Keep mutations out of `run.py` daily flow until safety worker's policy (t_a20d45b8) is integrated.

## 7. Gaps / NOT investigated here

- No capture of ListItemsOfAShip exists at ~/pss-mitm-capture.jsonl, so full live field list for Item nodes (rarity, enhancement, module linkage) is UNKNOWN — must be captured or taken from ListAllDesigns static data.
- CraftDesign consumption links (CraftDesigns -> required ItemDesignIds): not present in code; UNKNOWN.
- SellItem3/Purchase4/GiftItem/CombineItem4/DeleteItem3/WearItem/UnequipItems/room-module item endpoints: no captures exist anywhere in the working set; remain STATICALLY DISCOVERED or INFERRED only.
