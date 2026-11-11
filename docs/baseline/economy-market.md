# Marketplace listings (R4, AUTO-008)

`Client.listActiveMarketplaceMessages` sends the same `ListActiveMarketplaceMessages5` request and logs the same lines as before. It now also keeps the parsed `Message` records in `client.marketplaceMessages`:

- `None` before the first call, and whenever the latest call failed (no response, an `errorMessage`, or unparseable XML);
- `[]` when the read succeeded with no messages;
- the list of message records otherwise.

`sdk/economy/market.py` turns a record into a frozen `MarketListing` through a whitelist:

| Field | Source | Notes |
| --- | --- | --- |
| `item_name` | `@Message` | Seen in fixtures. A list of text parts is joined. |
| `currency`, `price` | `@ActivityArgument` (`currency:price`) | Seen in fixtures. Currency is lowercased; a missing, non-numeric or negative price is `None`. |
| `sale_id` | `@SaleId` | Only when present. `has_identity` is true only with a sale id. |
| `message_id`, `item_design_id`, `quantity` | `@MessageId`, `@ItemDesignId`, `@Quantity` | Only when present |

Other attributes are ignored. A record whose kept values contain anything `redact_secrets` would change is refused with `MarketInputError`.

`observed_listings(client)` returns `None` when no successful read is recorded, so "not observed" is never reported as "no listings".

## Limits

Only `@Message` and `@ActivityArgument` appear in the repository's fixtures; the other attributes are named after the prior capture analysis in the roadmap and are unverified. A listing shows an offer. It is never evidence of a sale, a purchase or a transfer. `SellItem3` and `Purchase4` are still not implemented (see the blocked slice `r4-live-market-trial`).
