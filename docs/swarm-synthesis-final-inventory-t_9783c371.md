# Final synthesis — inventory-management implementation plan

Synthesis task: `t_9783c371`. Swarm root: `t_b9080d7a`. Gate task: `t_6391549a`
(verifier, gate=pass, 2026-09-23). This document is the authoritative
deliverable of the swarm. It consolidates the three verified lane outputs and
records the verifier's rulings. No production code changed by this task. No
live mutations performed.

Source documents (all verified against repo HEAD `2cb5aa4` by the gate lane):

- Plan: `docs/inventory-implementation-plan.md` (planner lane `t_23f9c5c6`;
  23 sections — reconciliation, data models, phasing, task breakdown A–L,
  endpoint readiness matrix, mutation-disabled backlog, capture list,
  execution sequence).
- Architecture: `docs/architecture-addendum-inventory-t_71d8b652.md`
  (architect lane `t_71d8b652`; module boundaries, public APIs, determinism
  rules R1–R5, config schema, dry-run output format, audit contract, phasing
  refined against the import graph).
- Safety: `docs/security/inventory-safety-boundaries.md` (safety lane
  `t_d5626222`; invariants S1–S7, reconciliation abort conditions A1–A7,
  adversarial tests AD-1–AD-12, regression tests RT-1–RT-6, per-slice
  acceptance gates).

This synthesis resolves the overlaps between them and pins the decisions the
gate lane ruled on. Where the planner and architect disagree, the resolution
below is binding.

## 1. Gate verdict and what it unblocks

The verifier passed the gate with these spot checks reproduced from capture
evidence:

- ConsumeItem2 checksum reproduced 3/3 (mitm captures, 2026-08-16).
- MarkItemsNotNew checksum reproduced 1/1 (`cdt + accessToken + CK + SC`).
- ActivateItem4: one capture, cdt-only checksum matches; gate stays at
  DISABLED_INTERFACE until a second independent sample exists (capture list
  item 6).
- CombineItem4: exactly one capture (2026-08-01, empty `itemIds`) —
  CAPTURED-BUT-AMBIGUOUS confirmed. The "no captures" row in
  `INVENTORY_ENDPOINT_ANALYSIS.md` is stale.
- ListItemsOfAShip: zero captures in either capture file. Critical gap
  confirmed; blocks modeling Item beyond the six observed attributes.
- Item node attributes: exactly six across 4,168 Item tags (ItemId, ShipId,
  ItemDesignId, Quantity, IsNew, SkinKey).
- Dynamic design feed: 2,557 ItemDesign rows decoded from the 2026-08-08
  ListAllDynamicDesigns capture; this feed, not the static branch, is the
  ItemDesign source.
- Marketplace: zero captures for SellItem3 / Purchase4 /
  ListSalesByItemDesignId across 16,703 exchanges.
- Repo baseline claims true: no `listAllDynamicDesigns` in `sdk/client.py`,
  no `sdk/inventory/` package, no new checksum helpers in `sdk/security.py`.

One bookkeeping caveat, no action needed: both `t_f8b6c248` and `t_23f9c5c6`
claimed `docs/inventory-implementation-plan.md`; the content is a single
consistent 23-section plan, so the overlap is bookkeeping only.

## 2. Rulings on the architect's open questions Q1–Q5

The architect lane left five questions for the verifier. The gate lane ruled
on all of them; these rulings amend the plan and are binding on every
implementation slice:

- Q1 (checksum module placement): the three inventory checksum functions
  (`consume_item2`, `activate_item4`, `mark_items_not_new`) live in
  `sdk/inventory/checksums.py`, NOT `sdk/security.py`. The verifier overrides
  the architect's recommendation. Rationale: inventory-scoped checksums stay
  co-located with the domain package; `sdk/security.py` remains the home of
  generic auth/transport checksums. Task B and Slice 1 must place them there.
- Q2 (`listAllDynamicDesigns` shape): accept the architect's recommendation.
  A sibling method on `Client`, patterned after `listAllDesigns4`. No generic
  feed-fetcher enum.
- Q3 (`AccountSnapshot` source): accept with caveat. Define a versioned
  on-disk snapshot format FIRST (per the dry-run/snapshot schema in architect
  §7), with a tool that materializes snapshots from fetched XML. This blocks
  Phase 5: `AccountPortfolio` consumes snapshots, never raw per-account XML.
- Q4 (Evidence tier granularity): accept "minimum across inputs" for Slice 1
  and the phases that follow, until real planner experience justifies
  per-source tiers. Revisit no earlier than after Phase 3.
- Q5 (marketplace-value protection rule): accept. Rule 9 of the protected
  item policy evaluates as "no data → no demotion", logged with
  `gate=NEEDS_CAPTURE` and a note that marketplace evaluation was skipped.
  The policy is structurally present but cannot change classification until
  ListSalesByItemDesignId is captured.

## 3. Consolidated module layout (post-Q1 ruling)

```
sdk/inventory/__init__.py    re-exports public surface only
sdk/inventory/_types.py      frozen dataclasses + enums (no logic)
sdk/inventory/designs.py     dynamic ItemDesign feed loading/caching;
                             static feed for TrainingDesign/CraftDesign
sdk/inventory/catalog.py     ListItemsOfAShip → typed Items
sdk/inventory/checksums.py   consume_item2 / activate_item4 /
                             mark_items_not_new (pure MD5 functions)
sdk/inventory/protection.py  protected-item policy (10 rules)
sdk/inventory/classify.py    deterministic bucket classifier
sdk/inventory/demand.py      ReservationPlan / InventoryDemandPlanner
sdk/inventory/portfolio.py   AccountPortfolio over on-disk snapshots
sdk/inventory/reduction.py   InventoryReductionPlanner (dry-run)
sdk/inventory/transfer.py    TransferPlanner (proposals only, pure)
sdk/inventory/executor.py    gated mutation dispatcher, HARD-DISABLED
sdk/inventory/reconcile.py   snapshot → mutate → refetch → verify wrapper
```

Rules that do not move:

- `sdk/client.py` gains exactly one method: `listAllDynamicDesigns` (Q2).
  `_extract_collection` stays in `sdk/client.py`; the inventory package
  imports it from there (reconcile lane's disagreement, adopted).
- `sdk/client.py` never imports `sdk.inventory.*`; `sdk/inventory/*` never
  imports `requests` or performs I/O. One-way dependency: commands/TUI →
  inventory → client types.
- `run.py` gains no inventory flag and no autonomous inventory path in any
  read-only phase (safety invariant S6). The TUI command registry is the only
  entry point to inventory verbs until Phase 9.
- `inventory apply` is a delete-to-enable refusal stub (invariant S2), not a
  config flag. It names the blocking phase in its refusal message. The only
  way it stops refusing is deleting the stub in the same commit that ships a
  capture-backed executor.

## 4. The execution sequence (binding, dependency-ordered)

The planner's task list A–L stands, with the phase-gate enforcement from the
safety lane attached. Task letters below are the planner's; the safety gates
that gate each one come from safety doc §5.

```
Task A (docs corrections, READ-ONLY, gate: slice0)
  -> Task B (design feeds + checksums in sdk/inventory/checksums.py, READ-ONLY)
       -> Task C (catalog, READ-ONLY)
            -> Task D (classifier + protection, READ-ONLY, gate: phase2 —
               S3 short-circuit proof, AD-1/AD-6, determinism R1–R5)
                 -> {Task E (inventory / inventory plan CLI + apply refusal
                     stub, READ-ONLY, gate: slice1 incl. T10 static scan),
                     Task F (demand planner, READ-ONLY)}          [parallel]
                      -> Task G (reduction planner, DRY-RUN, gate: phase4 —
                         surplus-only dispositions 5–8, AD-2/3/7, digest
                         replayability)
                           -> Task H (portfolio over versioned snapshots —
                              Q3 format first) [can start once Phase 3
                              ReservationPlan shape is frozen]
                                -> Task I (accounts / deficits / transfers UX)
                                     -> Task J (executor + reconciler,
                                        fakes only, gate: phase8 — A1–A7 all
                                        pass, executor raises on default
                                        construction)
                                          -> Task Ln (per-endpoint enablement,
                                             one endpoint per PR, gate:
                                             phase9 — capture + checksum +
                                             schema + reconciliation + budget
                                             cap + at least one prior dry-run
                                             cycle emitting that action)
Task K (capture backlog, evidence) — fully parallel; gates Task L.
```

Parallel-friendly pairs: {B's checksums, B's designs} — disjoint files;
{F, H} — both consume the frozen ReservationPlan surface; K — entirely
external. Sequential constraints: C needs B; D needs C; E and F need D;
G needs E+F; I needs G+H; J needs G plus at least one captured checksum; L
needs J plus per-endpoint captures from K.

Smallest safe first slice is unchanged: Slice 0 = Task A alone (docs only);
Slice 1 = Tasks B + C + E-minimal. Exact acceptance criteria: `make test`
passes fully mocked, T10 static scan clean, AD-1/4/5/7/8/10 and RT-1/2/4/5
pass, `inventory apply` refuses naming Phase 8/9, the `sdk/client.py` diff
contains only `listAllDynamicDesigns`, `run.py` untouched, README.template
updated before README.md.

## 5. What may be built now vs. what is blocked

Buildable now (read-only, no protocol gaps): everything in Phases 0–6 — the
design feeds, catalog, classifier, protection rules, demand planner, dry-run
reduction planner, portfolio, transfer proposals, and four read-only CLI
verbs. Phase 8's executor/reconciler scaffolding is also buildable now, fakes
only, hard-disabled.

Blocked on protocol evidence (mutation-disabled backlog, unchanged from
planner §20 with safety annotations in safety §6): CombineItem4 (needs a
non-empty `itemIds` capture; planner may not even propose combine actions
until then — CRAFT rows emit without endpoints, gate=NEEDS_CAPTURE),
DeleteItem3 (DISABLED_H3; `remainingQuantity` semantics unknown),
GiftItem, the SellItem3 → ListSalesByItemDesignId → Purchase4 marketplace
chain (zero marketplace traffic in captures; exact-sale matching is a
structural rule, AD-11 — never buy the first listing matching a design),
WearItem/UnequipItems (reconcile delta-spec must include
`Character/@ItemIds` before/after), all four room-module endpoints
(AddItemIdToRoom, MoveItemIdToRoom3, RemoveItemIdFromRoom,
UpdateItemIdsForRoom2), Refill2 (mandatory Starbux budget cap before any
enablement discussion), and the low-priority misc trio (RepairBrokenModules,
DeleteAmmo, ActivateRenameItem). ActivateItem4 needs one more independent
capture.

One flag outside inventory scope, recorded so the first slice cannot worsen
it: SpeedUpTravelling, CharacterService/Draw, AddStarbux2, and RebuildAmmo3
are pre-existing autonomous premium-currency paths with no budget caps.
That is a separate follow-up; regression test RT-6 asserts the inventory
slices do not touch those call sites.

## 6. Capture list (ranked, unchanged from planner §21)

1. ListItemsOfAShip (critical gap; zero captures despite daily use).
2. CombineItem4 with non-empty `itemIds`.
3. DeleteItem3 (`remainingQuantity` semantics).
4. SellItem3 + ListSalesByItemDesignId + Purchase4 sequence.
5. GiftItem.
6. Second independent ActivateItem4 sample.
7. WearItem / UnequipItems.
8. Any one room-module endpoint.
9. RepairBrokenModules, Refill2, DeleteAmmo.
10. ListCraftDesigns (only to prove redundancy vs `@Ingredients`).

## 7. Definition of done for the whole feature

Restated from planner §22 with the safety layer's gates folded in: read-only
catalog/classification/demand/reduction/portfolio shipped with full mocked
coverage; all six CLI verbs present; `inventory apply` enforced per endpoint
with evidence gates in code; `INVENTORY_ENDPOINT_ANALYSIS.md` consistent with
capture truth; no mutation endpoint wired without capture verification,
checksum verification, semantic verification, A1–A7 reconciliation coverage,
per-run budget caps, and a separately authorized slice; unknown or ambiguous
state always resolves to PROTECTED; planner output deterministic and audited
(gate refusals are never silent); T10 static scan stays green on every merge.

## 8. Handoff

Next action: Task A (Slice 0 docs corrections to
`INVENTORY_ENDPOINT_ANALYSIS.md`, the five corrections in planner §1), then
Slice 1 (Tasks B + C + E-minimal) per gate verdict. No further planning work
is required before Slice 0 starts. All decisions above are recorded on the
swarm root blackboard `t_b9080d7a` via this task's completion metadata.
