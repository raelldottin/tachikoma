# Market trade reconciliation (R4)

`sdk/economy/trades.py` decides how far one market trade provably got. `reconcile_trade(request, observations)` takes a `SellRequest` or `BuyRequest` and timestamped observations:

| Observation | Meaning |
| --- | --- |
| `ListingSeen(at, listing)` | A [marketplace listing](economy-market.md) was read |
| `ListingGone(at, sale_id)` | A listing previously read is no longer present |
| `InventoryChange(at, item_design_id, change)` | An item quantity changed, for example from the [ledger](economy-ledger.md) |
| `CurrencyChange(at, currency, change)` | A currency balance changed |

Observations before the request are ignored. Times must be timezone-aware.

## Sell states

| State | Requires |
| --- | --- |
| `requested` | The listing call was made. A listing without a sale id leaves the trade here, because its identity is unconfirmed. |
| `listed` | A listing with a sale id, matching the item, currency and price, was seen after the request |
| `completed` | The listing was seen and then gone, the item left the inventory in the requested quantity, and the trade currency rose after the listing was seen by between 1 and the price. `proceeds` is the rise and `fees` is the price minus proceeds. |
| `expired` | The listing is gone and the item came back with no proceeds |
| `unknown` | The listing is gone but the item and currency movements are missing or inconsistent (item never left, proceeds above the price, wrong currency) |

## Buy states

| State | Requires |
| --- | --- |
| `requested` | The purchase call was made; neither the item nor the payment was observed |
| `completed` | The item arrived in at least the requested quantity and the currency fell by at least the price. `cost` is the fall and `fees` is cost minus price. |
| `unknown` | Only one of item or payment was observed, or the amounts are short |

Creating a listing never makes a trade `completed`; the roadmap's R4 exit forbids claiming a transfer from listing creation. Each result carries an `evidence` list naming the observations it relied on.
