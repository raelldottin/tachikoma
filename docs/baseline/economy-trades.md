# Market trade reconciliation (R4)

`sdk/economy/trades.py` decides how far one market trade provably got. `reconcile_trade(request, observations, *, max_fee=None)` takes a `SellRequest` or `BuyRequest` and timestamped observations:

| Observation | Meaning |
| --- | --- |
| `ListingSeen(at, listing)` | A [marketplace listing](economy-market.md) was read |
| `ListingGone(at, sale_id)` | A listing previously read is no longer present |
| `InventoryChange(at, item_design_id, change)` | An item quantity changed, for example from the [ledger](economy-ledger.md) |
| `CurrencyChange(at, currency, change)` | A currency balance changed |

Observations before the request are ignored, except that a sale id seen before a sell request marks a listing that already existed and so cannot be the one this request created. Times must be timezone-aware.

## Fee bound

Every amount check uses `max_fee`. When it is `None`, `default_max_fee(price)` applies: 25% of the price, rounded up (13 for a price of 50). It must otherwise be a non-negative integer. Sale proceeds must lie in `[price - max_fee, price]` and a purchase payment in `[price, price + max_fee]`. An amount outside the bound makes the trade `unknown`, because an unrelated spend or collection in the same currency would otherwise read as the trade.

## Sell states

| State | Requires |
| --- | --- |
| `requested` | The listing call was made. A listing without a sale id leaves the trade here, because its identity is unconfirmed. So does an "ambiguous listing", where more than one distinct sale id matches the request, and a match that was already open before the request. |
| `listed` | Exactly one listing with a sale id was first seen after the request and matches the item (when the listing names it), the quantity (when given), the currency and the price |
| `completed` | The listing was seen and then gone, the item left the inventory in the requested quantity, and the trade currency rose at or after the `ListingGone` observation by an amount within the fee bound. `proceeds` is the rise and `fees` is the price minus proceeds. A rise while the listing was still open, such as a resource collection, never counts as proceeds. |
| `expired` | The listing is gone and the item came back with no proceeds |
| `unknown` | The listing is gone but the item and currency movements are missing or inconsistent (item never left, no proceeds after the listing was gone, proceeds outside the fee bound, wrong currency) |

The item leaving the inventory before the listing is seen is expected: the game takes the item when it is listed.

## Buy states

| State | Requires |
| --- | --- |
| `requested` | The purchase call was made; neither the item nor the payment was observed |
| `completed` | Exactly the requested quantity of the item arrived and the currency fell by an amount within the fee bound. `cost` is the fall and `fees` is cost minus price. A `ListingGone` for the targeted sale id is recorded in the evidence. |
| `unknown` | Only one of item or payment was observed, more or fewer items arrived than requested, the payment is outside the fee bound, or the targeted listing was still seen open after both the item and the payment |

Creating a listing never makes a trade `completed`; the roadmap's R4 exit forbids claiming a transfer from listing creation. Each result carries an `evidence` list naming the observations it relied on.
