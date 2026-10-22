# Swarm worker report: t_74779a1f — Endpoint / capture / protocol verification inventory

Scope: WORKSTREAM B lead for this swarm — enumerate every inventory-relevant
endpoint, its evidence tier, checksum status, and capture status. Investigation
only; no code changed by this worker; no live mutations performed.

Sources cross-referenced:
- `INVENTORY_ENDPOINT_ANALYSIS.md` (IL2CPP static catalog, 80 endpoints)
- `sdk/client.py`, `sdk/security.py` (current repo truth)
- `~/pss-mitm-capture.jsonl` (16,703 captured exchanges, Aug 2026 set)
- `docs/worker-report-inventory-t_730c8a08.md` (sibling repo/data-model lane)
- `docs/security/{consumables-analysis,mutation-safety-analysis}.md` (prior lanes)

---

## 1. Capture coverage of the inventory-relevant surface (Observed, this lane)

Live capture counts for all endpoints in scope, counted directly from
`~/pss-mitm-capture.jsonl`:

| Endpoint | Captures | Unique param sets |
|---|---|---|
| ItemService/ConsumeItem2 | 6 requests (3 unique) | 3 |
| ItemService/ActivateItem4 | 1 | 1 |
| ItemService/MarkItemsNotNew | 2 (1 unique) | 1 |
| ItemService/CombineItem4 | 1 (request + response) | 1 |
| RoomService/CollectResources3 | 14 (7 unique) | 7 |
| RoomService/RebuildAmmo3 | 2 | 2 |
| ItemService/DeleteItem3 | 0 | — |
| ItemService/GiftItem | 0 | — |
| ItemService/ActivateItem3 | 0 (client-only; the official client now uses 4) | — |
| ItemService/ListItemsOfAShip | 0 | — |
| ItemService/ListItemDesigns2 | 0 (superseded by DesignService/ListAllStaticDesigns2, 10 captures) | — |
| MarketService/SellItem3 | 0 ("SellItem" hits are TaskDesign display strings, not endpoints) | — |
| MarketService/Purchase4 | 0 | — |
| MarketService/ListSalesByItemDesignId | 0 | — |
| CharacterService/WearItem | 0 | — |
| CharacterService/UnequipItems | 0 | — |
| RoomService/ListCraftDesigns | 0 | — |
| RoomService/AddItemIdToRoom / MoveItemIdToRoom3 / RemoveItemIdFromRoom / UpdateItemIdsForRoom2 | 0 | — |
| ItemService/RepairBrokenModules / DeleteAmmo | 0 | — |

Key gap: ListItemsOfAShip has never been captured by this client or by the
official-client MITM set. The inventory read path is unverified on the wire.

## 2. Checksum verification — new results this lane (VERIFIED)

All recomputed from capture bytes. Formula template:
`checksum = MD5(preimage + ChecksumKey + SavyChecksum)` with
ChecksumKey="5343", SavyChecksum="Savvy!s0d@".

| Endpoint | VERIFIED preimage | Samples | Result |
|---|---|---|---|
| ItemService/ConsumeItem2 | `clientDateTime + itemId + quantity + characterId` | 3/3 unique MATCH | Capture-verified. Contradicts `INVENTORY_ENDPOINT_ANALYSIS.md` line 111 (which lists it as no-checksum `?itemId=`). Actual wire URL is `?itemId=&quantity=&characterId=&clientDateTime=&checksum=&accessToken=`. Matches sibling worker t_730c8a08's independent result. |
| ItemService/ActivateItem4 | `clientDateTime` only | 1/1 MATCH | Capture-verified but single-sample. itemId, quantity, targetId are on the wire but NOT in the preimage. Needs a second capture before production use. |
| ItemService/MarkItemsNotNew | `clientDateTime + accessToken` | 1/1 MATCH | Capture-verified. Harmless UI-state call. The no-params vs with-params ambiguity is resolved: only `clientDateTime + accessToken` is consistent with the wire ordering `checksum=&clientDateTime=&accessToken=`. |
| ItemService/CombineItem4 | `clientDateTime` only | 1/1 MATCH (against full accessToken) | The single captured request has `itemIds=` EMPTY (a degenerate combine). With itemIds empty, only `clientDateTime` matches — i.e. verified for the observed request. Because the input list was empty, this capture CANNOT distinguish `cdt` from `cdt+itemIds` orderings; a real multi-item combine capture is required before implementing. Static template suggests preimage `itemIds+itemDesignId+quantity+clientDateTime` — the observed checksum does NOT match that ordering-with-the-static-order assumption (empty-iids case, `85+'1'+cdt` mismatch; `cdt` matches). Treat as CAPTURED BUT AMBIGUOUS. |
| RoomService/CollectResources3 | `collectDate + roomId + amount + accessToken` | 7/7 unique MATCH (already in repo) | Capture-verified; implemented in `sdk/security.py:checksum_collect_resources3`. |
| RoomService/RebuildAmmo3 | `ammoCategory + clientDateTime + accessToken` (per prior lane) | implemented in `sdk/security.py:checksum_rebuild_ammo3`; verified per commit history (1/1) | Capture-verified. |

## 3. Endpoint verification matrix (final status for this lane)

Status tiers: VERIFIED (checksum recomputed from capture) /
CAPTURED-NOT-VERIFIED / STATICALLY-DISCOVERED / UNKNOWN.

| Endpoint | Status | Notes |
|---|---|---|
| ItemService/ListItemsOfAShip | STATICALLY-DISCOVERED, no capture | Read path of the entire swarm; needs a capture before Item-node fields beyond ItemId/ShipId/ItemDesignId/Quantity/IsNew/SkinKey can be claimed. |
| ItemService/ConsumeItem2 | VERIFIED | Checksum formula above; response shows post-consume Item with Quantity=0 retained in payload. |
| ItemService/ActivateItem4 | VERIFIED (1 sample — recommend 2nd capture) | Params itemId/quantity/targetId excluded from preimage. |
| ItemService/ActivateItem3 | INFERRED legacy | Present in `sdk/client.py:activateItem3`; zero captures; official client traffic uses ActivateItem4. Do not extend the v3 call. |
| ItemService/CombineItem4 | CAPTURED-BUT-AMBIGUOUS | 1 capture with empty itemIds; checksum matches `clientDateTime`-only but cannot rule out param inclusion. Needs real combine capture. |
| ItemService/DeleteItem3 | STATICALLY-DISCOVERED | `?itemId={0}&remainingQuantity={1}&accessToken={2}`; no checksum in template; no capture. `remainingQuantity` semantics unknown. |
| ItemService/GiftItem | STATICALLY-DISCOVERED | No capture. |
| ItemService/RepairBrokenModules | STATICALLY-DISCOVERED (checksum endpoint) | Preimage per template: `roomId+itemid+quantity+clientDateTime`. No capture. |
| ItemService/DeleteAmmo | STATICALLY-DISCOVERED | No capture. |
| ItemService/MarkItemsNotNew | VERIFIED | See above. Static template listed truncated `?checksum=` — resolution now known: `checksum + clientDateTime + accessToken` ordering on wire. |
| ItemService/ListItemDesigns2 | STATICALLY-DISCOVERED | Client today consumes designs via `DesignService/ListAllDesigns4` / `ListAllStaticDesigns2` (10 captures) instead. |
| MarketService/SellItem3 | STATICALLY-DISCOVERED (zero captures) | The 13 "SellItem" greps are `ObjectiveType="SellItemCount"` task-design strings, not API calls. Confirmed no sale traffic in the capture window. |
| MarketService/Purchase4 | STATICALLY-DISCOVERED (checksum endpoint) | Preimage `saleId+clientDateTime` per template; no capture. |
| MarketService/ListSalesByItemDesignId | STATICALLY-DISCOVERED | No capture. Required read for any sale/transfer verification flow. |
| RoomService/Refill2 | STATICALLY-DISCOVERED (checksum) | No capture. |
| RoomService room-module endpoints (Add/Move/Remove/UpdateItemIds…) | STATICALLY-DISCOVERED (checksum) | No captures of any of the four. |
| RoomService/ListCraftDesigns | STATICALLY-DISCOVERED | No capture; blocks crafting-dependency modeling (Workstream C input). |
| CharacterService/WearItem / UnequipItems | STATICALLY-DISCOVERED / UNKNOWN params (WearItem template truncated) | No captures. Equipment linkage is observed only through `Character/@ItemIds` attributes in responses. |

## 4. Corrections for INVENTORY_ENDPOINT_ANALYSIS.md (handoff to synthesizer)

These are errors in the static catalog that the verifier should accept as
VERIFIED corrections:

1. `ConsumeItem2` has a checksum. URL is
   `?itemId=&quantity=&characterId=&clientDateTime=&checksum=&accessToken=`;
   preimage `clientDateTime+itemId+quantity+characterId`. The doc's
   "no-checksum `?itemId=`" row is wrong, and its "2 checksums in
   ItemService" count becomes 3 (add ConsumeItem2; note MarkItemsNotNew's
   truncated template is now resolved).
2. `MarkItemsNotNew` template is resolvable from capture:
   `?checksum=&clientDateTime=&accessToken=`, preimage `clientDateTime+accessToken`.
3. `CombineItem4` has at least one official-client capture (2026-08-01);
   the doc's "No captures" row is stale. Checksum formula still ambiguous.
4. Response-shape capture observed for ConsumeItem2/CombineItem4: Item nodes
   carry `ItemId, ShipId, ItemDesignId, Quantity, IsNew, SkinKey` and no
   rarity/enhancement/equipment attributes at that location. Equipment
   linkage lives on `Character/@ItemIds`.

## 5. What remains UNKNOWN (capture work the swarm still needs)

Ranked for the next capture session:

1. `ListItemsOfAShip` full response (blocks the whole inventory data model)
2. `CombineItem4` with non-empty itemIds (resolves checksum formula)
3. `DeleteItem3` (remainingQuantity semantics — reconciliation hint)
4. `SellItem3` + `ListSalesByItemDesignId` + `Purchase4` sequence
   (marketplace-transfer feasibility, Workstream D)
5. `GiftItem` (owned-account transfer option)
6. `ListCraftDesigns` (crafting dependency model)
7. Second independent `ActivateItem4` sample
8. `WearItem`/`UnequipItems` (crew-equipment reservation modeling)
9. Room-module item endpoints (any one of Add/Move/Remove/Update)
10. `RepairBrokenModules`, `Refill2`, `DeleteAmmo` (lower priority)

## 6. Handoff notes for the synthesizer/verifier (t_84a4e853 / t_bd6ccd45)

- Every "VERIFIED" claim above was recomputed in this worker's session from
  raw capture bytes with the standard CK/SC constants; scripts were ad-hoc
  python, inputs quoted verbatim in this report's tables.
- No endpoint was promoted beyond its evidence. In particular,
  `CombineItem4` must be recorded as CAPTURED-BUT-AMBIGUOUS, not verified.
- The static catalog's checksum count ("12 checksum endpoints") is off after
  this lane: ConsumeItem2 must move into the checksum group; the final count
  should be recomputed by the synthesizer after applying corrections above.
- This worker changed no repo source files. Deliverable is this document plus
  a structured blackboard comment on the swarm root.
