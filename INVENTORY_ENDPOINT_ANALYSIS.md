# Inventory Management Endpoint Analysis (IL2CPP v31.1)

Static analysis of all inventory-related API endpoints extracted from the Pixel
Starships `global-metadata.dat` binary. Cross-referenced against `sdk/client.py`
implementations and `~/pss-mitm-capture.jsonl` live traffic captures.

---

## Summary

| Metric | Count |
|--------|-------|
| Total inventory endpoints (IL2CPP) | 80 |
| Endpoints requiring checksum (🔐) | 13 |
| Endpoints without checksum (🔓) | 67 |
| Implemented in `client.py` (✅) | 14 |
| Not implemented (❌) | 66 |
| Endpoints seen in captures | 12 (+2 in the 2026-10-05 Task J session: SellItem3, Purchase4) |
| Endpoints seen with checksum | 7 of 13 (+ SellItem3 [6/6] and Purchase4 [1/1, wire-derived] from the 2026-10-05 Task J session) |

> Corrections applied (2026-09, verification-ledger VeLedger-8cf2, gate
> `t_6391549a` PASS): ConsumeItem2 moved from 🔓 to 🔐 (capture-verified
> checksum, 3/3 samples); MarkItemsNotNew template resolved
> (`?checksum=&clientDateTime=&accessToken=`); CombineItem4 has exactly one
> capture (2026-08-01, empty `itemIds`) and is CAPTURED-BUT-AMBIGUOUS, not
> "no captures". ActivateItem4 was captured once (checksum matches
> `clientDateTime`-only preimage).

---

## Checksum-Requiring Endpoints (12)

These endpoints have `checksum=` in their IL2CPP URL template. Their checksum
formulas follow the standard new-style MD5 pattern unless otherwise noted:

```
preimage  = params_before_checksum + ChecksumKey
encrypted = preimage + SavyChecksum
checksum  = MD5(encrypted)
```

### ItemService (2 checksum endpoints)

| Endpoint | URL Template | Preimage Params | Status |
|----------|-------------|----------------|--------|
| `CombineItem4` | `?itemIds={0}&itemDesignId={1}&quantity={2}&clientDateTime={3}&checksum={4}&accessToken={5}` | ambiguous — `clientDateTime`-only matches the single capture (empty `itemIds`); static template implies `itemIds, itemDesignId, quantity, clientDateTime` | ❌ Not implemented |
| `MarkItemsNotNew` | `?checksum={0}&clientDateTime={1}&accessToken={2}` | clientDateTime, accessToken | ❌ Not implemented |
| `RepairBrokenModules` | `?roomId={0}&itemid={1}&quantity={2}&clientDateTime={3}&checksum={4}&accessToken={5}` | roomId, itemid, quantity, clientDateTime | ❌ Not implemented |

### RoomService (6 checksum endpoints)

| Endpoint | URL Template | Preimage Params | Status |
|----------|-------------|----------------|--------|
| `AddItemIdToRoom` | `?roomId={0}&itemId={1}&checksum={2}&clientDateTime={3}&accessToken={4}` | roomId, itemId | ❌ Not implemented |
| `CollectResources3` | `?roomId={0}&amount={1}&collectDate={2}&checksum={3}&accessToken={4}` | roomId, amount, collectDate | ❌ Not implemented |
| `MoveItemIdToRoom3` | `?roomId={0}&toRoomId={1}&itemId={2}&checksum={3}&clientDateTime={4}&accessToken={5}` | roomId, toRoomId, itemId | ❌ Not implemented |
| `RebuildAmmo3` | `?ammoCategory={0}&clientDateTime={1}&checksum={2}&accessToken={3}` | ammoCategory, clientDateTime | ✅ **Verified** (1 capture) |
| `Refill2` | `?roomId={0}&clientDateTime={1}&checksum={2}&accessToken={3}` | roomId, clientDateTime | ❌ Not implemented |
| `RemoveItemIdFromRoom` | `?roomId={0}&itemId={1}&checksum={2}&clientDateTime={3}&accessToken={4}` | roomId, itemId | ❌ Not implemented |
| `UpdateItemIdsForRoom2` | `?roomId={0}&itemIds={1}&checksum={2}&clientDateTime={3}&accessToken={4}` | roomId, itemIds | ❌ Not implemented |

### MarketService (1 checksum endpoint)

| Endpoint | URL Template | Preimage Params | Status |
|----------|-------------|----------------|--------|
| `Purchase4` | `?saleId={0}&clientDateTime={1}&checksum={2}&accessToken={3}&allowConversion={4}` | saleId, clientDateTime | ❌ Not implemented |

### CharacterService (1 checksum endpoint)

| Endpoint | URL Template | Preimage Params | Status |
|----------|-------------|----------------|--------|
| `MarkCharactersNotNew` | `?checksum=` (truncated template) | Unknown (template truncated) | ❌ Not implemented |

### RewardService (1 checksum endpoint)

| Endpoint | URL Template | Preimage Params | Status |
|----------|-------------|----------------|--------|
| `BuyReward2` | `?rewardDesignId=` (truncated template) | **clientDateTime only** (verified) | ✅ **Verified** (4 captures) |

**Note**: `BuyReward2` is unique — it uses only `MD5(clientDateTime + ChecksumKey +
SavyChecksum)`, excluding both `rewardDesignId` and `accessToken` from the preimage.

---

## Complete Endpoint Catalog by Service

### ItemService (14 endpoints)

#### Checksum Required (🔐)

1. **CombineItem4** — Combine multiple items into a target item
   - URL: `/ItemService/CombineItem4?itemIds={0}&itemDesignId={1}&quantity={2}&clientDateTime={3}&checksum={4}&accessToken={5}`
   - Preimage: AMBIGUOUS — the single capture (2026-08-01, empty `itemIds`)
     matches `MD5(clientDateTime + ChecksumKey + SavyChecksum)`, but with an
     empty item list this cannot rule out param-including orderings. A
     non-empty-itemIds capture is required before implementation.
   - In code: ❌ Not implemented
   - In captures: ⚠️ 1 capture (2026-08-01, empty `itemIds` — CAPTURED-BUT-AMBIGUOUS)

2. **MarkItemsNotNew** — Mark items as seen/not-new
   - URL: `/ItemService/MarkItemsNotNew?checksum={0}&clientDateTime={1}&accessToken={2}`
   - Preimage (VERIFIED, 1/1 capture): `MD5(clientDateTime + accessToken + ChecksumKey + SavyChecksum)`
   - In code: ❌ Not implemented
   - In captures: ✅ 1 capture, checksum verified 1/1

3. **RepairBrokenModules** — Repair broken room modules
   - URL: `/ItemService/RepairBrokenModules?roomId={0}&itemid={1}&quantity={2}&clientDateTime={3}&checksum={4}&accessToken={5}`
   - Preimage: `roomId + itemid + quantity + clientDateTime`
   - In code: ❌ Not implemented
   - In captures: ❌ No captures

#### No Checksum (🔓) — accessToken only or languageKey

4. **ActivateItem4** — Activate a usable item
   - URL: `/ItemService/ActivateItem4?itemId=&quantity=&targetId=&clientDateTime=&checksum=&accessToken=`
   - Preimage (VERIFIED, 1/1 capture — single sample): `MD5(clientDateTime + ChecksumKey + SavyChecksum)`;
     `itemId`/`quantity`/`targetId` are on the wire but NOT in the preimage.
     Needs a 2nd independent capture before production use.
   - In code: ❌ Not implemented

5. **ActivateRenameItem** — Activate a rename item
   - URL: `/ItemService/ActivateRenameItem?itemId=`
   - In code: ❌ Not implemented

6. **ConsumeItem2** — Consume/destroy a single item
   - URL: `/ItemService/ConsumeItem2?itemId={0}&quantity={1}&characterId={2}&clientDateTime={3}&checksum={4}&accessToken={5}`
   - Preimage (VERIFIED, 3/3 captured samples, 2026-08-16):
     `MD5(clientDateTime + itemId + quantity + characterId + ChecksumKey + SavyChecksum)`
   - In captures: response retains the consumed Item node with `Quantity=0`.
   - In code: ❌ Not implemented (checksum helper in `sdk/inventory/checksums.py`;
     call stays disabled until an authorized mutation slice)

7. **DeleteAmmo** — Delete ammunition from a room
   - URL: `/ItemService/DeleteAmmo?itemId={0}&remainingQuantity={1}&roomId={2}&targetItemDesignString={3}&accessToken={4}`
   - In code: ❌ Not implemented

8. **DeleteItem3** — Delete an item from inventory
   - URL: `/ItemService/DeleteItem3?itemId={0}&remainingQuantity={1}&accessToken={2}`
   - In code: ❌ Not implemented

9. **FixUnsynchronizedItems** — Sync inventory with server
   - URL: `/ItemService/FixUnsynchronizedItems?accessToken=`
   - In code: ❌ Not implemented

10. **GiftItem** — Gift an item to another player
   - URL: `/ItemService/GiftItem?itemId=` (template truncated in the binary)
   - In code: ❌ Not implemented
   - In captures: ❌ NEVER captured (0 in the historical corpus, 0 in the 2026-10-05
     Task J capture). The 2026-10-04 official-client UI survey found NO
     user-reachable Gift/Transfer inventory action (item detail exposes Use and
     Sell only) — evidence of no discovered user-reachable gift path, NOT proof
     the endpoint is removed. Evidence tier: STATICALLY_DISCOVERED, unchanged.
     Do not infer semantics; no checksum preimage evidence exists. See
     `docs/task-j-capture-analysis-2026-10-05-t_1fce40eb.md`.

11. **ListItemDesignActions** — List item design action types
    - URL: `/ItemService/ListItemDesignActions`
    - In code: ❌ Not implemented

12. **ListItemDesigns2** — List all item designs (static data)
   - URL: `/ItemService/ListItemDesigns2?languageKey=`
   - In code: ❌ Not implemented
   - Superseded in practice: ItemDesigns come from
     `DesignService/ListAllDynamicDesigns` (2,557 ItemDesign rows decoded from
     the 2026-08-08 capture; fetched in code via `Client.listAllDynamicDesigns`).
     The static `ListAllDesigns4` branch contains 0 ItemDesign rows and is NOT
     the ItemDesign source.

13. **ListItemsOfAShip** — List all items on a ship
   - URL: `/ItemService/ListItemsOfAShip?accessToken=`
   - In code: ✅ Implemented (used for ship inspection)
   - In captures: ❌ NEVER captured — including the 2026-10-05 Task J session
     (`~/pss-task-j-capture.jsonl`, 357 records, 4 full official-client account
     logins: uIDs 9228834, 9238149, 9231708, 9181430). Task J OBSERVED finding:
     the official client does NOT call ListItemsOfAShip in normal login/ship/
     inventory flows — inventory Item nodes arrive only via
     `UserService/ListAllUserDataFirst2` (once per login), `ShipService/
     GetShipByUserId`, and `ShipService/FinaliseBattle15`. Re-opening the ship
     view does not trigger a fresh inventory fetch; a full client restart does.
     Evidence tier remains STATICALLY_DISCOVERED; the inventory read path is
     unverified on the wire. See `docs/task-j-capture-analysis-2026-10-05-t_1fce40eb.md`.
   - Item-node response shape (observed in adjacent ItemService responses across
     4,168 Item tags): exactly six attributes — `ItemId`, `ShipId`,
     `ItemDesignId`, `Quantity`, `IsNew`, `SkinKey`. Equipment linkage lives on
     `Character/@ItemIds`, not on Item nodes. Do NOT assume additional fields
     (rarity, enhancement, module linkage) until a real capture proves them.

14. **UnequipItems** (via CharacterService) — Unequip items from character
    - URL: `/CharacterService/UnequipItems?characterId={0}&accessToken={1}`
    - In code: ❌ Not implemented

---

### RoomService (36 endpoints)

#### Checksum Required (🔐)

1-7. See checksum table above.

#### No Checksum (🔓)

8. **BuyMissile4** — Buy/manufacture missiles in a room
   - URL: `/RoomService/BuyMissile4?roomId={0}&itemDesignString={1}&targetItemDesignString={2}&manufactureStartDate={3}&accessToken={4}`
   - In code: ❌ Not implemented

9. **BuyRoom2** — Purchase and place a new room
   - URL: `/RoomService/BuyRoom2?roomDesignId=`
   - In code: ❌ Not implemented

10. **CancelRoomUpgrade** — Cancel an in-progress room upgrade
    - URL: `/RoomService/CancelRoomUpgrade?roomId=`
    - In code: ❌ Not implemented

11. **CollectAllResources** — Collect all resources from all rooms
    - URL: `/RoomService/CollectAllResources?itemType=`
    - In code: ✅ **Implemented** + captures (4 requests)

12. **CollectSalvageItems** — Collect salvage from a room
    - URL: `/RoomService/CollectSalvageItems?roomId=`
    - In code: ❌ Not implemented

13. **CopyRoomActions** — Copy actions between rooms
    - URL: `/RoomService/CopyRoomActions?roomId=`
    - In code: ❌ Not implemented

14. **CreateRoomAction** — Create a room action/automation rule
    - URL: `/RoomService/CreateRoomAction?roomId=`
    - In code: ❌ Not implemented

15. **DeleteRoomAction** — Delete a room action
    - URL: `/RoomService/DeleteRoomAction?roomActionId=`
    - In code: ❌ Not implemented

16. **DeleteRoomActions** — Delete all actions for a room
    - URL: `/RoomService/DeleteRoomActions?roomId=`
    - In code: ❌ Not implemented

17. **EditRoomAction** — Edit a room action
    - URL: `/RoomService/EditRoomAction?roomActionId=`
    - In code: ❌ Not implemented

18. **ListActionTypes2** — List room action types
    - URL: `/RoomService/ListActionTypes2?languageKey={0}&designVersion={1}`
    - In code: ✅ Implemented

19. **ListAllRoomActionsOfShip** — List all room actions on a ship
    - URL: `/RoomService/ListAllRoomActionsOfShip?shipId=`
    - In code: ✅ Implemented

20. **ListConditionTypes2** — List room condition types
    - URL: `/RoomService/ListConditionTypes2?languageKey={0}&designVersion={1}`
    - In code: ✅ Implemented

21. **ListCraftDesigns** — List crafting designs
    - URL: `/RoomService/ListCraftDesigns`
    - In code: ❌ Not implemented

22. **ListMissileDesigns** — List missile designs
    - URL: `/RoomService/ListMissileDesigns`
    - In code: ❌ Not implemented

23. **ListRoomDesignPurchase** — List purchasable room designs
    - URL: `/RoomService/ListRoomDesignPurchase`
    - In code: ❌ Not implemented

24. **ListRoomDesigns2** — List all room designs
    - URL: `/RoomService/ListRoomDesigns2?languageKey=`
    - In code: ✅ Implemented

25. **ListRoomsViaAccessToken** — List rooms on the player's ship
    - URL: `/RoomService/ListRoomsViaAccessToken?accessToken=`
    - In code: ✅ Implemented

26. **MoveRoom** — Move a room on the ship grid
    - URL: `/RoomService/MoveRoom?roomId=`
    - In code: ❌ Not implemented

27. **MoveRoomAction** — Reorder a room action
    - URL: `/RoomService/MoveRoomAction?roomActionId=`
    - In code: ❌ Not implemented

28. **RemoveRoom** — Remove/ demolish a room
    - URL: `/RoomService/RemoveRoom?shipId=`
    - In code: ❌ Not implemented

29. **SetRoomPriority** — Set room power priority
    - URL: `/RoomService/SetRoomPriority?roomId={0}&priority={1}&accessToken={2}`
    - In code: ❌ Not implemented

30. **SetTargetManufactureString** — Set manufacturing target for a room
    - URL: `/RoomService/SetTargetManufactureString?roomId={0}&targetItemDesignString={1}&accessToken={2}`
    - In code: ❌ Not implemented

31. **SpeedUpBuildingMissilesUsingCredits** — Speed up missile production
    - URL: `/RoomService/SpeedUpBuildingMissilesUsingCredits?roomId=`
    - In code: ❌ Not implemented

32. **SpeedUpRoomConstructionUsingBoostGauge** — Speed up with boost
    - URL: `/RoomService/SpeedUpRoomConstructionUsingBoostGauge?roomId=`
    - In code: ✅ Implemented

33. **SpeedUpRoomConstructionUsingCredits** — Speed up with credits
    - URL: `/RoomService/SpeedUpRoomConstructionUsingCredits?roomId=`
    - In code: ❌ Not implemented

34. **SwapRoom** — Swap two rooms' positions
    - URL: `/RoomService/SwapRoom?roomId={0}&newRoomId={1}&accessToken={2}`
    - In code: ❌ Not implemented

35. **UpgradeRoom2** — Upgrade a room to the next level
    - URL: `/RoomService/UpgradeRoom2?roomId=`
    - In code: ✅ Implemented

---

### CharacterService (28 endpoints)

#### Checksum Required (🔐)

1. **MarkCharactersNotNew** — Mark characters as seen
   - URL: `/CharacterService/MarkCharactersNotNew?checksum=` (truncated)
   - In code: ❌ Not implemented

#### No Checksum (🔓)

2. **BlendCharacter** — Blend/merge characters
   - URL: `/CharacterService/BlendCharacter?roomId=`
   - In code: ❌ Not implemented

3. **ConsumeCharacter** — Consume/sacrifice a character
   - URL: `/CharacterService/ConsumeCharacter?consumedCharacterId=`
   - In code: ❌ Not implemented

4. **CopyCharacterActions** — Copy action settings between characters
   - URL: `/CharacterService/CopyCharacterActions?characterId=`
   - In code: ❌ Not implemented

5. **CreateCharacterAction** — Create a character AI action
   - URL: `/CharacterService/CreateCharacterAction?characterId=`
   - In code: ❌ Not implemented

6. **CreateMissionCharacter** — Create a character for a mission
   - URL: `/CharacterService/CreateMissionCharacter?characterDesignId=`
   - In code: ❌ Not implemented

7. **DeleteCharacter** — Delete a character
   - URL: `/CharacterService/DeleteCharacter?characterId=`
   - In code: ❌ Not implemented

8. **DeleteCharacterAction** — Delete a character action
   - URL: `/CharacterService/DeleteCharacterAction?characterActionId=`
   - In code: ❌ Not implemented

9. **DeleteCharacterActions** — Delete all actions for a character
   - URL: `/CharacterService/DeleteCharacterActions?characterId=`
   - In code: ❌ Not implemented

10. **Draw** — Character draw/gacha
    - URL: `/CharacterService/Draw?drawDesignId=`
    - In code: ✅ Implemented (uses `checksum_character_draw`)

11. **EditCharacterAction** — Edit a character action
    - URL: `/CharacterService/EditCharacterAction?characterActionId=`
    - In code: ❌ Not implemented

12. **FixInvalidEquipment** — Fix equipment sync issues
    - URL: `/CharacterService/FixInvalidEquipment?characterIds=`
    - In code: ❌ Not implemented

13. **ListAllCharacterDesignActions** — List character action designs
    - URL: `/CharacterService/ListAllCharacterDesignActions`
    - In code: ❌ Not implemented

14. **ListAllCharacterDesigns2** — List all character designs
    - URL: `/CharacterService/ListAllCharacterDesigns2?languageKey=`
    - In code: ✅ Implemented

15. **ListAllCharactersOfUser** — List all characters on a ship
    - URL: `/CharacterService/ListAllCharactersOfUser?accessToken=`
    - In code: ✅ Implemented

16. **ListAllDrawDesigns** — List all draw/gacha designs
    - URL: `/CharacterService/ListAllDrawDesigns?languageKey=`
    - In code: ❌ Not implemented

17. **ListCharacterActionsByShipId** — List character actions for a ship
    - URL: `/CharacterService/ListCharacterActionsByShipId?shipId=`
    - In code: ❌ Not implemented

18. **MoveCharacterAction** — Reorder character actions
    - URL: `/CharacterService/MoveCharacterAction?characterActionId=`
    - In code: ❌ Not implemented

19. **MoveCharacterToRoom** — Assign character to a room
    - URL: `/CharacterService/MoveCharacterToRoom?characterId=`
    - In code: ❌ Not implemented

20. **PrestigeCharacters** — Prestige/ascend characters
    - URL: `/CharacterService/PrestigeCharacters?characterId1=`
    - In code: ❌ Not implemented

21. **RedeemItem2** — Redeem an item from a room
    - URL: `/CharacterService/RedeemItem2?roomId=`
    - In code: ❌ Not implemented

22. **RemoveItem** — Remove an item from a character
    - URL: `/CharacterService/RemoveItem?characterId=`
    - In code: ❌ Not implemented

23. **SetFlags** — Set character flags
    - URL: `/CharacterService/SetFlags?characterId=`
    - In code: ❌ Not implemented

24. **SpeedUpEgg** — Speed up character egg hatching
    - URL: `/CharacterService/SpeedUpEgg?characterId=`
    - In code: ❌ Not implemented

25. **UnequipItems** — Unequip all items from a character
    - URL: `/CharacterService/UnequipItems?characterId={0}&accessToken={1}`
    - In code: ❌ Not implemented

26. **UpgradeCharacter** — Upgrade a character's level
    - URL: `/CharacterService/UpgradeCharacter?characterId=`
    - In code: ✅ Implemented + captures (2 requests)

27. **WearItem** — Equip an item to a character
    - URL: `/CharacterService/WearItem?characterId=`
    - In code: ❌ Not implemented

---

### MarketService (4 endpoints)

#### Checksum Required (🔐)

1. **Purchase4** — Purchase an item from the marketplace
   - URL: `/MarketService/Purchase4?saleId={0}&clientDateTime={1}&checksum={2}&accessToken={3}&allowConversion={4}`
   - Preimage: `accessToken + saleId + clientDateTime` (accessToken PREPENDED —
     single wire-derived capture, 1/1 match, 2026-10-05 Task J session;
     `allowConversion` is on the wire but NOT in the preimage). SINGLE-SAMPLE
     evidence: formula is wire-derived only, stays below VERIFIED for
     execution purposes; endpoint remains unreachable in Tachikoma.
   - In code: ❌ Not implemented (pure checksum helper `checksum_purchase4`
     exists in `sdk/inventory/checksums.py` — verification only, invokes nothing)
   - In captures: ✅ 1 capture (2026-10-05, saleId 44414085, success=True,
     buyer uID 9238149, allowConversion=False)

#### No Checksum (🔓)

2. **ListSalesByItemDesignId** — List marketplace sales for an item
   - URL: `/MarketService/ListSalesByItemDesignId?itemDesignId={0}&saleStatus={1}&from={2}&to={3}`
   - In code: ❌ Not implemented

3. **SellItem3** — List an item for sale on the marketplace
   - URL: `/MarketService/SellItem3?itemId={0}&quantity={1}&currencyType={2}&currencyValue={3}&clientDateTime={4}&checksum={5}&accessToken={6}`
   - Preimage: `accessToken + itemId + quantity + currencyType + currencyValue
     + clientDateTime` (accessToken PREPENDED — VERIFIED against 6/6 captured
     samples, 2026-10-05 Task J session). The endpoint stays unreachable in
     Tachikoma.
   - In code: ❌ Not implemented (pure checksum helper `checksum_sell_item3`
     exists in `sdk/inventory/checksums.py` — verification only, invokes nothing)
   - In captures: ✅ 6 captures (2026-10-05, all Gas/Mineral priced,
     currencyValues 50k–500k; includes the human-operated market-mediated
     transfer leg: itemId 187296241, design 360, qty 1 @ 250,000 Gas,
     seller uID 9228834)

> Classification correction (Task J, 2026-10-05): SellItem3 belongs under
> "Checksum Required" — the full wire template carries `checksum=` and 6/6
> captured samples match the formula above. The static IL2CPP template was
> truncated at `?itemId=`, which is why it was originally classified 🔓.

---

### RewardService (3 endpoints)

#### Checksum Required (🔐)

1. **BuyReward2** — Purchase a reward from the shop
   - URL: `/RewardService/BuyReward2?rewardDesignId=`
   - Preimage: `clientDateTime` (only! — no rewardDesignId, no accessToken)
   - In code: ✅ Function exists (`checksum_buy_reward2`), not called in daily flow
   - In captures: ✅ 8 captures (4 unique), all verified

#### No Checksum (🔓)

2. **ListAllRewardDesigns2** — List all reward designs
   - URL: `/RewardService/ListAllRewardDesigns2?languageKey=`
   - In code: ❌ Not implemented

---

## Capture Coverage

| Endpoint | Capture Count | Checksum Verified |
|----------|--------------|-------------------|
| RewardService/BuyReward2 | 8 | ✅ (4/4 unique) |
| RoomService/CollectAllResources | 4 | N/A (no checksum) |
| RoomService/RebuildAmmo3 | 2 | ✅ (1/1 unique) |
| CharacterService/UpgradeCharacter | 2 | N/A (no checksum) |
| DesignService/ListAllDynamicDesigns | 1 (2026-08-08) | N/A (no checksum; 2,557 ItemDesign rows) |
| DesignService/ListAllStaticDesigns2 | 10 | N/A (no checksum) |
| ItemService/ConsumeItem2 | 3 | ✅ (3/3 unique) |
| ItemService/ActivateItem4 | 1 | ✅ (1/1 — single sample; 2nd capture required before use) |
| ItemService/ActivateItem3 | 1 (2026-08-01) | N/A (legacy; no checksum in capture) |
| ItemService/MarkItemsNotNew | 1 | ✅ (1/1) |
| ItemService/CombineItem4 | 1 (2026-08-01, empty `itemIds`) | ⚠️ AMBIGUOUS (`clientDateTime`-only matches; params unresolvable) |
| ItemService/ListItemDesignActions | 1 | N/A (no checksum) |
| ItemService/ListItemsOfAShip | 0 | ❌ NEVER captured — including the 2026-10-05 Task J session; official client observed to never call it in normal flows (see entry 13 above) |
| MarketService/SellItem3 | 6 (2026-10-05) | ✅ (6/6) — accessToken-prepended preimage |
| MarketService/Purchase4 | 1 (2026-10-05) | ✅ wire-derived (1/1) — single sample, below VERIFIED for execution purposes |

**Full-corpus ListItemsOfAShip scan (2026-09-26, evidence-gate lane t_f4dd7bad):**
0 captures across ALL 6 capture files (50,755 records total: 49,774 in
`pss-mitm-capture.jsonl`, 681 in `pss-login-capture.jsonl`, 300 across the four
`~/.tachikoma-credential-capture/previous-capture-*.jsonl` files). Binary-safe,
case-insensitive scan for `ListItemsOfAShip` and prefix-variant `ItemsOfAShip`. The
two binary-corrupted MITM records (lines 46902/46921) were re-scanned leniently:
they contain Item tags (2 each) but no ListItemsOfAShip reference. The only
ItemService records in the corpus are mutation endpoints (MarkItemsNotNew 3+1,
CombineItem4 1+1, ConsumeItem2 6, ActivateItem4 2, FixUnsynchronizedItems 2); zero
inventory-feed read captures exist. **Consequence: inventory-feed evidence stays at
CAPTURED_NOT_VERIFIED — never relabeled VERIFIED — until a ListItemsOfAShip capture
exists. No live traffic may be created solely to obtain one.**

**Task J capture (2026-10-05, evidence lane t_497be142):** a seventh capture
file, `~/pss-task-j-capture.jsonl` (357 records, official-client session
07:54–08:18 UTC, 4 operator account logins), is separate from the historical
six-file corpus. It contains 0 ListItemsOfAShip and 0 GiftItem calls, but
6 MarketService/SellItem3 and 1 MarketService/Purchase4 records (one
human-operated market-mediated A→B transfer, SaleId 44414085, no Starbux
involved). Post-transaction inventory deltas remain UNRECONCILED (fail-closed:
no post-transaction snapshots exist). Sanitized fixtures live in
`tests/fixtures/task_j_capture/`; evidence analysis in
`docs/task-j-capture-analysis-2026-10-05-t_1fce40eb.md` and
`docs/worker-report-task-j-capture-analysis-t_497be142.md`.

**7 of 13** checksum-requiring inventory endpoints have been captured and
verified (RebuildAmmo3, BuyReward2, ActivateItem4 (single-sample),
MarkItemsNotNew; ResourceService/CollectResources3 has 7/7 verified but is a
RoomService resource endpoint tracked in this inventory surface).
ConsumeItem2 is a 6th VERIFIED checksum endpoint (3/3). The 2026-10-05 Task J
session adds MarketService/SellItem3 (6/6 samples — accessToken-prepended
preimage) and MarketService/Purchase4 (1/1, wire-derived single sample,
stays below VERIFIED for execution purposes). CombineItem4 is the only
CAPTURED-BUT-AMBIGUOUS checksum endpoint. The remaining checksum endpoints
need live capture sessions for verification.

---

## Recommended Implementation Priority

### High Priority (Daily Automation Value)

1. **RoomService/Refill2** — Auto-refill rooms (gas, oxygen). Checksum formula
   inferred: `MD5(roomId + clientDateTime + ChecksumKey + SavyChecksum)`

2. **GalaxyService/PurchaseFromMarker3** — Purchase items from galaxy markers.
   No checksum required (accessToken only).

3. **UserService/ConvertStarbuxToItem** — Convert starbux to items.
   No checksum required.

4. **ItemService/CombineItem4** — Combine items. **DO NOT IMPLEMENT**: checksum
   is CAPTURED-BUT-AMBIGUOUS. The single capture (2026-08-01) has empty
   `itemIds`; `MD5(clientDateTime + ChecksumKey + SavyChecksum)` matches it,
   but the inferred full-param formula
   `MD5(itemIds + itemDesignId + quantity + clientDateTime + ChecksumKey + SavyChecksum)`
   is UNVERIFIED. Needs a capture with non-empty `itemIds`.

### Medium Priority (Ship Optimization)

5. **RoomService/MoveItemIdToRoom3** — Move items between rooms.
6. **RoomService/RemoveItemIdFromRoom** — Remove items from rooms.
7. **RoomService/UpdateItemIdsForRoom2** — Batch update room contents.
8. **CharacterService/MoveCharacterToRoom** — Assign crew to rooms.
9. **MarketService/Purchase4** — Buy from marketplace.

### Low Priority (Edge Cases)

10. **ItemService/RepairBrokenModules** — Repair broken room modules.
11. **ItemService/DeleteItem3** — Delete inventory items.
12. **CharacterService/MarkCharactersNotNew** — Mark seen (UI only).
13. **ItemService/MarkItemsNotNew** — Mark seen (UI only).
14. **MarketService/SellItem3** — Sell on marketplace.

---

## Checksum Formula Reference

All new-style MD5 endpoints follow this pattern:
```
preimage  = <params_before_checksum> + ChecksumKey
encrypted = preimage + SavyChecksum
checksum  = MD5(encrypted)
```

Where:
- `ChecksumKey` = `"5343"`
- `SavyChecksum` = `"Savvy!s0d@"`
- Params are concatenated in the order they appear in the URL template
  (before the `checksum=` parameter)

**Exceptions confirmed:**
- `BuyReward2`: Only `clientDateTime` in preimage (no rewardDesignId, no accessToken)
- `CreateBattle9`: Only `clientDateTime` in preimage (no clientHp, no accessToken)
- `AcceptBattle5`/`FinaliseBattle15`: No `ChecksumKey` in preimage, `accessToken` prepended
- `HeartBeat4`/`PurchaseCatalog2 (UserService)`: Old-style numeric sum, not MD5
