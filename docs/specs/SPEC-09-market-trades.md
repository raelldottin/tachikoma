# SPEC-09: Market trade calls and a reconciled live trade

**Status:** Draft
**Release:** R4 exit
**Owner gate:** G4
**Depends on:** SPEC-03, SPEC-05
**Implements roadmap item:** queue slice `r4-live-market-trial`; "R4 exit: successful reconciliation for representative market/economy experiments ... and no claims of completed transfers based only on listing creation"

## Problem

Listings, the ledger, trade reconciliation and the market-return report exist, but `SellItem3` and `Purchase4` are not implemented (`docs/baseline/economy-market.md`). The fee bound of 25% in `sdk/economy/trades.py` is an assumption, and only `@Message` and `@ActivityArgument` listing fields are confirmed.

## Scope

- **In:** the two calls, implemented from sanitized current captures, as catalog actions with reconciliation; one sell and one buy trial; and the observed fee.
- **Out:** automated trading strategies.

## Requirements

- **S09-R1 (MUST)** The owner supplies sanitized captures of `SellItem3`, `ListActiveMarketplaceMessages5` (after listing) and `Purchase4` requests and responses on the current game version, including how any checksum is formed, stored under `tests/fixtures/live_sanitized/market/` and scanned by S03-R2.
- **S09-R2 (MUST)** `Client.sellItem` and `Client.purchaseItem` reproduce the captured request shapes exactly. They are never called by existing code paths.
- **S09-R3 (MUST)** Catalog actions `sell_item` and `purchase_item` are non-idempotent. Their postconditions are evaluated by `reconcile_trade`, so a created listing is at most `listed`.
- **S09-R4 (MUST)** An authorization for market actions names the item design ids and a maximum value per trade and per window.
- **S09-R5 (MUST)** Each live trade's evidence (listing seen with sale id, item movement, currency movement) is journaled, and `reconcile_trade` decides its state.
- **S09-R6 (MUST)** The observed fee from completed trades replaces the 25% assumption as the documented default, with its sample size; until then the assumption stays labelled as such.
- **S09-R7 (MUST)** The listing fields confirmed by the captures (`@SaleId`, `@ItemDesignId`, `@Quantity`) are recorded in the compatibility ledger.
- **S09-R8 (MUST)** The exit report records each trade's state, proceeds, cost and fees, the market return from completed trades only, and the game version.

## Interfaces

- `sdk/client.py`: two methods.
- `sdk/actions/catalog.py`: two actions with `CALL_ARGUMENTS`.
- `sdk/economy/trades.py`: fee default from evidence.
- `scripts/trial_report.py --market`.

## Safety invariants

- Value caps.
- Item allow-list.
- Non-idempotent handling, with no retry after `unknown`.
- A listing is never a sale.
- No starbux unless a single explicit trade in the authorization allows it.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S09-AC1 | R1, R2 | offline | The new methods produce requests byte-equal to the sanitized captures, with secrets substituted |
| S09-AC2 | R3, R5 | offline | Replaying captured responses through the runner gives `listed`, then `completed` only after item and currency movement |
| S09-AC3 | R4 | offline | Trades outside the item list or value cap are refused before any call |
| S09-AC4 | R6, R7, R8 | live | One sell and one buy on the test account reconcile as `completed`, and the report and ledger are committed |

## Test plan

`tests/test_market_calls.py` (request shapes), extend `tests/test_market_trades.py` and `tests/test_fleet_execute.py`.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec09-captures` | R1 | `tests/fixtures/live_sanitized/market/*` | **owner** |
| `spec09-client-calls` | R2 | `sdk/client.py`, tests, inventory JSON | agent |
| `spec09-trade-actions` | R3–R5 | `sdk/actions/catalog.py`, `sdk/execution/runner.py`, `sdk/fleet/authorization.py`, tests | agent |
| `spec09-run-trial` (replaces `r4-live-market-trial`) | R6–R8 | `docs/experiments/*`, `sdk/economy/trades.py`, `sdk/compat/entries.py` | **owner**, then agent |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S09-Q1 | Can current captures be provided and sanitized? | `spec09-captures` | No; R4 stays blocked |
| S09-Q2 | Which item and value cap for the trial trades? | authorization | One low-value item, one unit |

## Evidence

Pending.

## Open questions and risks

If checksums depend on secrets or rotate per version, the calls may need maintenance on each game update; the compatibility ledger flags it.
