# Consumable Classification and Inventory-Pressure Analysis

Swarm lane: `t_f6a9edd1` (consumables).
Sources: `INVENTORY_ENDPOINT_ANALYSIS.md` (80 inventory endpoints, 12
checksum-required, 68 checksum-free), `sdk/client.py`, `sdk/security.py`,
`run.py`, `docs/security/mutation-safety-analysis.md`.
Sibling lanes: protocol (`t_9f78e590`), safety (`t_9eec6b78`),
cleanup (`t_457d0913`).

> Note on card body: the Kanban body for this card carries the Opnory
> Gate 3 / SEC-02 template text in error (all five lane cards do). The
> operative statement of work is the card title plus the swarm root
> comment from the researcher lane (`t_315b6cb2`), which names this lane
> the consumables lane and defines its scope: classify consumable
> handling, and answer whether `BuyReward2`'s unique checksum stays in
> its own family.

## 1. What a "consumable" is in this codebase

A consumable is an inventory item with `ItemSubType == "Consumable"`
(or a name containing "Consumable") whose use path is:

```
findConsumableDesignId(statType, tier)   # enumerate ItemDesigns
  -> useConsumable(itemDesignId, characterId)   # find instance on ship
      -> activateItem3(itemId, targetId)        # ItemService/ActivateItem3
```

Consumption destroys the item instance and applies its effect to the
target character. There is no API-level undo.

### Classification (with hazard classes from mutation-safety-analysis)

| Consumable category | Endpoint | Class | Autonomous use today |
|---------------------|----------|-------|----------------------|
| Training stat consumables (grey/Common HP/ATK/ABL/STA/WPN/PLT/SCI/ENG) | `ItemService/ActivateItem3` | H2 (destroys an item) | Yes — `client.py:1297-1325`, early-TP path (<30% TP) |
| Hero training consumables (guaranteed TP at 100 fatigue) | `ItemService/ActivateItem3` | H2 | Yes — `client.py:1327-1350` |
| Rename item | `ItemService/ActivateRenameItem` | H2 | Not implemented |
| Generic single-item consume | `ItemService/ConsumeItem2` | H3 | Not implemented — must stay manual |
| Reward purchase (e.g. Scorched Pod via shop reward designs) | `RewardService/BuyReward2` | H2 (spends currency/mats) | Implemented; affordability guard pattern is `purchaseScorchedPodIfAffordable` (`run.py:251`) |
| Reward listing (read-only) | `RewardService/ListAllRewardDesigns2` | H0 | — |

`ActivateItem4` exists in the binary (`INVENTORY_ENDPOINT_ANALYSIS.md`
line 102) as the newer activation variant; per the safety lane it must
be treated exactly like `ActivateItem3` — allow-list by itemDesignId —
if it is ever wired.

### Current guard gaps for consumables

1. `useConsumable` is called from the autonomous training loop with no
   allow-list at the call site. The match is purely
   `ItemSubType == "Consumable"` plus name-substring matching on
   stat/tier (`client.py:1694-1704`). A renamed or newly introduced
   consumable whose name contains a stat word and a tier word would be
   silently eligible. Per safety-lane rule 3, the set of design IDs the
   loop may consume should be a fixed table, not a name heuristic.
2. `findConsumableDesignId`'s tier matching uses substring checks on
   the item name (`"Common" in ItemName`). This depends on localized
   display names from `ListAllDesigns4`/`ListItemDesigns2` and is the
   most fragile part of the consumable path. Prefer matching on
   structured `ItemSubType`/rarity fields when available.
3. No per-run budget: a misclassified loop could burn every consumable
   of a matched design in one run. Recommend a per-process counter
   (e.g. `PSS_MAX_CONSUMABLES_PER_RUN`, default small) alongside the
   safety lane's `PSS_MAX_STARBUX_PER_RUN` rule (rule 4).

## 2. Answer to the safety lane's open question

> Confirm `BuyReward2`'s unique checksum formula stays in the same
> checksum family even if `RewardService` grows new H2 endpoints. The
> formula's exclusion of `accessToken` and `rewardDesignId` is unusual
> and any sibling endpoint may not share that property.

**Confirmed: `BuyReward2` is its own family and must not be generalized.**

Evidence:

- Formula (`sdk/security.py:412-450`, capture-verified 4/4):
  `MD5(clientDateTime + ChecksumKey + SavyChecksum)`. The preimage
  contains neither `rewardDesignId` nor `accessToken`, even though both
  appear in the URL.
- Every other capture-verified checksum in this repo follows the
  standard new-style pattern and **does** bind the resource parameters:
  - `CollectResources3` (researcher lane, 14/14 captures):
    `MD5(collectDate + roomId + amount + accessToken + ChecksumKey + SavyChecksum)`
  - `RebuildAmmo3`, `GoTo`, `SpeedUpTravelling`, `AddStarbux2`,
    `CollectReward2` — all bind their identifying parameters.
- Therefore a future `RewardService` endpoint (e.g. a BuyReward3 or a
  different reward purchase variant) should be assumed to follow the
  standard pattern `params + clientDateTime + ChecksumKey + SavyChecksum`
  **until captures prove otherwise** — not the BuyReward2 minimal
  pattern. Do not factor `checksum_buy_reward2` into a shared helper
  that drops identifying parameters.
- Practical rule for any new RewardService checksum: verify against at
  least 2 unique captures where only the identifying parameter changes;
  if the checksum changes, the parameter is in the preimage.

This closes the open question in
`docs/security/mutation-safety-analysis.md` ("Open questions for the
consumables lane").

## 3. Inventory pressure

The autonomous loop's inventory pressure comes from three sources:

1. **Reward/task collection** — `collectTaskReward`, `collectDailyReward`
   (`run.py:270-309`) can fail when storage is full (observed:
   `collectTaskReward returned False (expected if storage full)`).
2. **Bulk resource collection** — `CollectAllResources` /
   `CollectResources3` (researcher lane) add minerals/gas; these are not
   item-slot consumers.
3. **Consumable manufacture/reward intake** — training consumables
   accumulate until the training loop burns them.

Pressure observations:

- Item-slot pressure is the real constraint (`storage full` failures
  above), not currency. The mitigation endpoints are all H3
  (`DeleteItem3`, `ConsumeItem2`, `SellItem3`) and per the safety lane
  none may enter the autonomous path.
- Therefore the only safe autonomous relief valve today is to **consume
  consumables as intended** (training loop) and to **stop collecting**
  when a collection call returns a full-inventory signal. There is no
  safe autonomous deletion or selling path, and none should be added.
- Recommendation: when a reward-collection call fails with a
  storage-full message, the loop should log and defer rather than retry
  in a tight loop (matches safety-lane rule 2 semantics: retry only
  after re-reading state).

## 4. Notes for the cleanup lane (`t_457d0913`)

- Do not add an autonomous `SellItem3`/`DeleteItem3` cleanup path for
  inventory pressure; per safety-lane H3 rule this is out of bounds.
  Surface "inventory full" as a logged, human-visible condition.
- `useConsumable` lacks an idempotency check beyond "first matching
  itemId"; a retry after a timeout would consume a second item. If the
  cleanup lane touches retry behavior, consumable calls must remain
  send-once per decision.

## 5. What this lane changed

No code changed. Deliverable is this document plus the
`CONSUMABLES_ANALYSIS=COMPLETE` signal on the swarm root with
machine-readable facts for the synthesizer/verifier.
