# SPEC-13: Automated room placement

**Status:** Done
**Drafted:** 2026-11-12, at the owner's request ("automate placing the room on the ship", then "draft the spec"), and revised after an agent review the same day.
**Approval:** owner, 2026-11-12 ("Approve SPEC-13", with its safe defaults and the capture included). The owner cannot tell which game account is pss5, so placement on pss5 is automated rather than done by hand. The owner decisions below are recorded, and R1 and R7 are amended accordingly. G6 is given for the captures; the live placement still needs its own authorization.
**Release:** operations (supports R2; not a roadmap exit)
**Owner gate:** G6: sanitized captures of the room-purchase request, and one live placement on the trial account
**Depends on:** SPEC-03 (capture scanning), SPEC-05 (authorized execution)
**Implements roadmap item:** none directly. It came out of the G2 canaries: pss5 has no mineral or gas collector and no storage room ([G2 evidence](../experiments/live/g2-evidence.md#why-the-collections-had-nothing-to-collect)), and the client can only upgrade rooms that already exist.

## Problem

`sdk/client.py` can upgrade an existing room (`upgradeRoom`, `UpgradeRoom2`) and collect resources. It has no call that buys and places a new room, and nothing in the repo records that request's shape. A new account such as pss5 therefore cannot get a collector unless the owner places one in the game by hand.

Placing a room means choosing a free cell range inside the ship's interior grid. pss5's ship capture from the owner-dispatched capture run 43187703923 (sanitized, not committed) shows room records with `@Row`, `@Column` and `@ConstructionStartDate`. No committed capture has them. Room sizes and the ship's interior mask come from design data that has not been captured. `sdk/ship_layout.py` already reads `@Row`, `@Column`, `@ColumnWidth` and `@RowHeight`, but it defaults missing values to 0 or 1, so its parsing cannot be reused where an unobserved value must refuse.

## Scope

- **In:**
  - the purchase-and-place client call, written from sanitized current captures;
  - a non-idempotent catalog action that reconciles from a later ship read;
  - a pure placement check that a cell range is free and inside the ship;
  - an authorization that allows a named room design at a named position on a named account, with a mineral or gas cap;
  - one live placement on the trial account.
- **Out:**
  - Any change to `baseline-v1`, or any policy that decides on its own to build rooms. A future policy version may use the action, under its own spec.
  - Moving, removing or selling rooms.
  - Starbux-priced rooms.
  - Speeding up construction.
  - The legacy daily run.
  - Agents editing any workflow that holds secrets.

## Requirements

- **S13-R1 (MUST)** Sanitized captures on the current game version, stored under `tests/fixtures/live_sanitized/rooms/` and passing the S03-R2 capture scan:
  - **From the owner:** the request and response that buy and place one room, including how any checksum is formed, and `GetShipByUserId` before and after. Any account the owner can log into will do; it need not be the trial account.
  - **From the agents' read-only capture step (G1, amended 2026-11-12):** the `RoomDesign`, `ShipDesign` and `RoomDesignPurchase` records, which give room sizes, the ship's interior grid and any per-ship-level limits. These are game data, read with `ListRoomDesigns2` and `ListAllDesigns4`, and need no write access.
- **S13-R2 (MUST)** `Client.placeRoom(room_design_id, row, column)` reproduces the captured request shape exactly. No existing code path calls it. It does no eligibility or cost checks of its own. Its return value follows what the captures allow: it returns `True` or `False` only for a response shape the captures show to mean success or refusal, and otherwise the raw response, which the runner treats as ambiguous.
- **S13-R3 (MUST)** A pure function `placement_refusal(snapshot, layout, room_design_id, row, column)` returns `None` only when all of the following hold:
  - the room design is known and priced in minerals or gas, never starbux;
  - the ship's design id, its interior grid, and every placed room's position and size are observed;
  - the whole cell range the room would occupy is inside the interior and overlaps no placed room;
  - any per-ship-level limit the captured design data states for that room design is respected.

  Otherwise it returns the reason. An unobserved input is a refusal, never assumed free. `layout` is a `ShipLayout` built from the `ShipDesign` and `RoomDesign` records (see Interfaces), because a catalog precondition receives only the snapshot and the params.
- **S13-R4 (MUST)** The catalog action `place_room` (`client_method="placeRoom"`) is `NOT_IDEMPOTENT`.
  - **Parameters:** `room_design_id`, `row` and `column`.
  - **Planned cost:** the design's mineral or gas price, so the gated runner checks it against the caps. A non-idempotent action without a planned cost is already refused while the account has caps.
  - **Preconditions:** the rooms section is observed. Placement mode calls `placement_refusal` and refuses before the runner is invoked, since the catalog precondition cannot see the layout.
- **S13-R5 (MUST)** The postcondition is decided from a later ship read alone:
  - **`verified`:** a room of that design appears at that row and column that was not there before, in any status (built or under construction).
  - **`contradicted`:** a room that was there before has disappeared, or changed to a different design without an upgrade.
  - **`unknown`:** the ship read fails, or the response was ambiguous and no room appeared.
  - **`not-observed`:** the response was a definite success and no room appeared.

  The response is never evidence of placement. The journal records the design id, row, column and the observed mineral or gas change, never the new room's instance id.
- **S13-R6 (MUST)** After any `unknown` `place_room` outcome, every later placement on that account is refused, at any position, until an owner reconciles it from a fresh ship read. The runner's existing no-retry check matches only the same action with the same params, so it does not cover a different position.
- **S13-R7 (MUST)** Placement runs only as an explicit one-off, never from a policy plan. `--fleet-execute --mode place` takes one account, one `room_design_id`, one `row` and one `column`, and runs at most one placement per run. Anything outside the authorization is refused before login. The authorization must have:
  - `place` in `modes`;
  - `place_room` in `actions`;
  - a `placements` entry for that account naming that design and either a row and column or `first-free` (amended 2026-11-12). `first-free` means the first position, in a fixed top-left scan order, that `placement_refusal` accepts on a fresh ship read in the same run. The run record shows the position used.
- **S13-R8 (MUST)** Starbux is never spent: a design priced in starbux is refused whatever the authorization says. The authorization's mineral and gas caps apply.
- **S13-R9 (MUST)** `baseline-v1` and every existing policy are unchanged. A test proves their plans for every bundled scenario are identical before and after this spec.
- **S13-R10 (SHOULD)** `--plan-only --mode place` prints, without logging in, whether the authorization would refuse the requested placement (mode, action, account, design, position, caps). The layout check needs a fresh ship read, so it runs only in the live run.
- **S13-R11 (MUST)** One live placement on the trial account, under an owner authorization, reconciles `verified`, and the result is recorded in `docs/experiments/live/`. On a SPEC-07 trial account it happens outside trial blocks, or is logged as an intervention (S07-R6).

## Interfaces

- **`sdk/client.py`:** `Client.placeRoom(room_design_id, row, column)`, written from the R1 captures. The endpoint name is not assumed: it is whatever the captures show.
- **`sdk/policy/placement.py` (new):** `ShipLayout` (interior mask, room sizes and per-level limits), parsed from `ShipDesign`, `RoomDesign` and `RoomDesignPurchase` records, with an unobserved value kept as `None`; and `placement_refusal(...)`. `DesignData.RoomDesign` already has `cost_kind` and `cost_amount` for the price.
- **`sdk/state/snapshot.py`:**
  - `Room` gains `row` and `column` (optional, default `None`, `None` when unobserved), so the postcondition can see where a room is.
  - The snapshot gains the ship's design id, if it is not already readable from the ship fields.
  - The baseline format (`sdk/state/baseline.py`, `_ROOM_KEYS`) is unchanged, so existing baselines still load. Journaled snapshots gain the two keys, which is an additive change.
- **`sdk/actions/catalog.py`:** `place_room`, with `CALL_ARGUMENTS` in `sdk/execution/runner.py`. The runner gains the account-wide block after an `unknown` placement (R6).
- **`sdk/fleet/authorization.py`:**
  - `place` is added to `RUN_MODES` and `MODE_PATTERN`.
  - A `placements` field maps each account alias to its allowed `{room_design_id, row, column}` entries. It is required when, and only when, `place_room` is in `actions`, following the pattern of `battles` and `market`. A file without it hashes as before.
- **`run.py`:** `--mode place --place-room DESIGN_ID@ROW,COLUMN` (a draft name, confirmed at approval) is added to `EXECUTE_MODES`. It is refused unless exactly one account is enabled. It reuses the registry, the identity check, caps, journaling and the run record from SPEC-05.
- **`sdk/fleet/factory.py`:**
  - Placement mode takes a separate path that plans nothing through `AuthorizedPolicy`, and the `ExecutionGate` mode check accepts `place`.
  - SPEC-05's dry-run freshness rule checks the account's policy plan, so it does not fit. A placement instead needs a fresh read-only ship snapshot taken in the same run, before the call.

## Safety invariants

- **API success is not verification:** only a later ship read showing the room at that position verifies it (R5).
- **Unobserved is `None`:** an unknown ship design, grid, size, position or price refuses the placement (R3), and a failed read is `unknown`.
- **Accounts never mix:** SPEC-05's identity check and per-account journals are reused unchanged. Placements are keyed per account (R7).
- **Secrets never leak:** the captures are sanitized and scanned (R1), and the journal holds no room or user ids (R5).
- **No starbux spend:** starbux-priced designs are refused (R8).
- **No retry after an uncertain outcome:** the action is `NOT_IDEMPOTENT`, and an `unknown` placement blocks every later placement on that account (R6).
- **Specific to this spec:**
  - Placement never comes from a policy (R7, R9), so a running trial's strategy cannot change through it.
  - At most one placement per run, on one account.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S13-AC1 | R1, R2 | offline | The committed captures pass the S03-R2 scan, and `Client.placeRoom` produces a request equal to the captured one, with secrets substituted |
| S13-AC2 | R3 | offline | Given synthetic and captured layouts, a placement is refused when it is off the interior, overlaps a room, has an unknown ship design, grid, size, position or price, is starbux-priced, or breaks a captured per-level limit. It is accepted only when every check passes |
| S13-AC3 | R4, R5 | offline | Replaying the captures through the runner gives `verified` only when the after-read shows the new room at that position. With no room, a definite success gives `not-observed`, and an ambiguous response or a failed read gives `unknown`. A finished upgrade of another room is not `contradicted`. The journal holds no room ids |
| S13-AC4 | R6 | offline | After an `unknown` placement, a placement at a different position on the same account is refused before any call |
| S13-AC5 | R7, R8 | offline | Each of these is refused before any call: a placement not named for that account in the authorization (design, position, mode or action); a second placement in one run; more than one enabled account; a starbux-priced design. An authorization listing `place_room` without `placements`, or the reverse, is invalid |
| S13-AC6 | R9 | offline | `baseline-v1` and the other policies plan identically for every bundled scenario before and after the change |
| S13-AC7 | R10 | offline | `--plan-only --mode place` shows the authorization's refusal reason and makes no game call |
| S13-AC8 | R11 | live | The owner runs one placement of the chosen room on the trial account. It reconciles `verified`, spends only minerals or gas within the cap, and the record is committed |

## Test plan

- `tests/test_room_placement.py` (new): `ShipLayout` and `placement_refusal` (AC2), the request shape and the capture scan (AC1), and `--plan-only` (AC7).
- Extend `tests/test_action_runner.py` and `tests/test_action_catalog.py` for `place_room` reconciliation and the account-wide block (AC3, AC4).
- Extend `tests/test_fleet_authorization.py` and `tests/test_fleet_execute.py` for the `place` mode (AC5).
- A policy-parity test over the bundled scenarios (AC6).

Fixtures are the sanitized captures from R1 plus synthetic layouts. All game traffic is mocked.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec13-captures` | R1 | `tests/fixtures/live_sanitized/rooms/*` | **owner** (the purchase request) |
| `spec13-capture-designs` | R1 | `sdk/state/capture.py`, `tests/test_capture_reads.py`, `docs/experiments/live/RUNBOOK.md` | agent (the design records, read-only) |
| `spec13-grid-model` | R3, R9 | `sdk/policy/placement.py`, `sdk/state/snapshot.py` (`Room.row`, `Room.column`, ship design id), tests | agent, after `spec13-capture-designs` and an owner `step=capture` run (the grid encoding comes from the captured design records) |
| `spec13-client-call` | R2 | `sdk/client.py`, tests | agent, after `spec13-captures` |
| `spec13-place-action` | R4–R6, R8 | `sdk/actions/catalog.py`, `sdk/execution/runner.py`, tests | agent, after `spec13-grid-model` and `spec13-client-call` |
| `spec13-place-mode` | R7, R10 | `run.py`, `sdk/fleet/authorization.py`, `sdk/fleet/factory.py`, `README.template`, `README.md`, tests | agent, after `spec13-place-action` |
| `spec13-live-workflow` | R11 | `.github/workflows/live-trial.yml` (a `place` step and its authorization) | **owner** only: the workflow holds secrets |
| `spec13-live-placement` | R11 | `docs/experiments/live/*` | **owner** runs it, then an agent records it |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S13-Q1 | Approve this spec? | any code | **Answered 2026-11-12: yes** |
| S13-Q2 | Can you capture and sanitize one room purchase (R1)? | `spec13-client-call` | **Answered 2026-11-12: yes**, on any account the owner can log into; the design records come from the agents' read-only capture |
| S13-Q3 | Which account, room design, row and column for the live placement? | the authorization | **Answered 2026-11-12, then changed the same day:** a **Gas Collector Lv1** (room design 42) on `pss5` at `first-free`, outside SPEC-07 trial blocks or logged as an intervention. The first answer was a level-1 mineral collector, but capture run 43314838395 showed that Mineral Extractor Lv1 costs 25 gas and pss5 has no gas above its reserve. The Gas Collector costs 10 minerals |
| S13-Q4 | The cap for the placement's mineral or gas price | the authorization | **Answered 2026-11-12:** the design's price, nothing more: 10 minerals for the Gas Collector Lv1 |
| S13-Q5 | Should a future policy version build rooms on its own? | a later spec | **Answered 2026-11-12: no** |

## Evidence

**Design records (S13-R1, read-only):** capture run 43314838395 (2026-11-12) recorded `ListRoomDesigns2` and the `ShipDesigns` and `RoomDesignPurchases` parts of `ListAllDesigns4`. The capture artifact is not committed. It confirmed these fields:

- **Hull:** pss5's ship is `ShipDesign` 6, ship level 2, with `@Rows` 9 and `@Columns` 17.
- **Rooms:** each placed room has `@Row` and `@Column`.
- **Room designs:** each gives its footprint as `@Rows` × `@Columns`, plus `@PriceString`, `@MinShipLevel`, `@MaxCount` and `@ConstructionTime`.
- **Purchase limits:** each `RoomDesignPurchase` gives `@RoomDesignId`, `@Level` and `@Quantity`.
- **The chosen room:** Gas Collector Lv1 (room design 42) is 2×2, costs `mineral:10` and builds in 10 seconds.

The interior `@Mask` was redacted in that run. `spec13-capture-mask` keeps it from the next capture run.

**First live placement (S13-AC8), not verified:** owner-dispatched run 43389706745 (2026-11-13, main 996a9d3) under [authorization-g6-pss5-place.json](../experiments/live/authorization-g6-pss5-place.json). It read pss5's ship fresh, chose row 1, column 1 for room design 42 (`first-free`), and sent `BuyRoom2` once. The reply was not the captured success shape, and the ship read after it showed no new room, so the outcome is `unknown`. Minerals and gas were unchanged. The run stopped the account and the fleet, as it should after an uncertain outcome. Nothing recorded the server's reply, so the reason is not known. `spec13-place-diagnostics` now journals a sanitized summary of the reply (HTTP status, element names, leak-scanned error or message text) as the attempt's evidence.

**Second live placement, not verified:** owner-dispatched run 43391935925 (2026-11-13, main 36fb742) journaled the reply: HTTP 405 (method not allowed) with an empty body. No room appeared, and minerals and gas were unchanged. The captured request is a GET, but the server refuses that method for `BuyRoom2`. `spec13-place-post` (owner decision, 2026-11-13) sends the same query as a POST, like the client's other room-changing calls (`UpgradeRoom2`, `CollectAllResources`).

**Third live placement, verified (S13-AC8, R11):** owner-dispatched run 43393262041 (2026-11-13, main 7aefe06) sent `BuyRoom2` as a POST. The reply was HTTP 200, and the ship read after it showed a Gas Collector Lv1 (room design 42) at row 1, column 1 in `Building` status, at the position `first-free` chose. The outcome was `verified`, at a cost of exactly 10 minerals, with nothing else spent. The run completed with no alerts. The success reply's element is `RoomService/BuyRoom` rather than the captured `BuyRoom2`, so the client did not recognise the reply as a success. The outcome came from the ship read, as R5 requires. `spec13-buyroom-success-shape` (2026-11-13) now accepts `RoomService/BuyRoom` as well as the captured `BuyRoom2`. pss5 then had a gas collector for the SPEC-07 canary to collect from. The owner later removed it while playing pss5 by hand (2026-11-13, canary 43401277661); the placement's verification stands.

**A limit found by that run:** each `live-trial.yml` run starts with an empty journal folder. So the account-wide block after an uncertain placement (S13-R6) holds within a run, and wherever journals are kept between runs, but not across CI dispatches. Carrying journals from one dispatch to the next needs an owner change to the workflow.

## Open questions and risks

- **The request is unknown:** the endpoint, its parameters, its response and any checksum are unknown until R1. If the checksum depends on a secret or changes with each game version, the call needs upkeep on game updates; the compatibility ledger flags it.
- **The grid is unconfirmed:** how the ship's interior and room sizes are encoded (for example a mask on `ShipDesign`, and a size on `RoomDesign` such as `@Rows`/`@Columns`) is not confirmed. R3 refuses until it is parsed from captures.
- **Timing:** this does not help SPEC-07's verified canary in time. That needs a collector before 2026-11-23, so the owner places it by hand. This spec is for later accounts and later trials.
- **Construction time:** a placed room may take time to build before it produces. R5 verifies placement, not production.
