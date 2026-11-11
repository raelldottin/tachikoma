"""Tests for sdk.economy.trades: market trade lifecycle from observations. Synthetic, offline."""

import unittest
from datetime import datetime, timedelta, timezone

from sdk.economy import (
    BuyRequest,
    CurrencyChange,
    InventoryChange,
    ListingGone,
    ListingSeen,
    MarketListing,
    SellRequest,
    TradeInputError,
    reconcile_trade,
)

T0 = datetime(2027, 2, 10, 8, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


SELL = SellRequest(at=at(0), item_design_id="77", quantity=1, currency="Starbux", price=50)
LISTING = MarketListing("Laser Cannon", "starbux", 50, sale_id="9001", item_design_id="77", quantity=1)


def seen(minutes=1, listing=LISTING):
    return ListingSeen(at(minutes), listing)


class SellTests(unittest.TestCase):
    def test_request_alone_is_requested(self):
        result = reconcile_trade(SELL)
        self.assertEqual(result.state, "requested")
        self.assertFalse(result.complete)

    def test_listing_without_sale_id_is_not_listed(self):
        result = reconcile_trade(SELL, [seen(listing=MarketListing("Laser Cannon", "starbux", 50))])
        self.assertEqual(result.state, "requested")
        self.assertIn("unconfirmed", result.reason)

    def test_listing_with_identity_is_listed_never_completed(self):
        result = reconcile_trade(SELL, [InventoryChange(at(1), "77", -1), seen()])
        self.assertEqual((result.state, result.sale_id), ("listed", "9001"))
        self.assertIsNone(result.proceeds)

    def test_mismatched_listing_is_ignored(self):
        other = MarketListing("Laser Cannon", "starbux", 75, sale_id="9002")
        self.assertEqual(reconcile_trade(SELL, [seen(listing=other)]).state, "requested")

    def test_completed_needs_item_out_and_proceeds(self):
        observations = [InventoryChange(at(1), "77", -1), seen(2), ListingGone(at(30), "9001"),
                        CurrencyChange(at(31), "starbux", 45)]
        result = reconcile_trade(SELL, observations)
        self.assertEqual((result.state, result.proceeds, result.fees), ("completed", 45, 5))
        self.assertTrue(result.complete)
        self.assertEqual(result.evidence[-1], "starbux +45")

    def test_gone_without_proceeds_is_unknown(self):
        result = reconcile_trade(SELL, [InventoryChange(at(1), "77", -1), seen(2), ListingGone(at(30), "9001")])
        self.assertEqual(result.state, "unknown")

    def test_item_returned_is_expired(self):
        observations = [InventoryChange(at(1), "77", -1), seen(2), ListingGone(at(30), "9001"),
                        InventoryChange(at(31), "77", 1)]
        self.assertEqual(reconcile_trade(SELL, observations).state, "expired")

    def test_inconsistent_movements_are_unknown(self):
        cases = [
            [seen(2), ListingGone(at(30), "9001"), CurrencyChange(at(31), "starbux", 45)],  # item never left
            [InventoryChange(at(1), "77", -1), seen(2), ListingGone(at(30), "9001"),
             CurrencyChange(at(31), "starbux", 80)],  # more than the price
            [InventoryChange(at(1), "77", -1), seen(2), ListingGone(at(30), "9001"),
             CurrencyChange(at(31), "gas", 45)],  # wrong currency
        ]
        for observations in cases:
            with self.subTest(observations=observations):
                self.assertEqual(reconcile_trade(SELL, observations).state, "unknown")

    def test_observations_before_the_request_are_ignored(self):
        observations = [InventoryChange(at(-5), "77", -1), seen(-4), ListingGone(at(-3), "9001"),
                        CurrencyChange(at(-2), "starbux", 45)]
        self.assertEqual(reconcile_trade(SELL, observations).state, "requested")

    def test_proceeds_before_the_listing_was_seen_do_not_count(self):
        observations = [CurrencyChange(at(1), "starbux", 45), InventoryChange(at(1), "77", -1), seen(2),
                        ListingGone(at(30), "9001")]
        self.assertEqual(reconcile_trade(SELL, observations).state, "unknown")


class BuyTests(unittest.TestCase):
    BUY = BuyRequest(at=at(0), sale_id="9001", item_design_id="77", quantity=1, currency="starbux", price=50)

    def test_states(self):
        self.assertEqual(reconcile_trade(self.BUY).state, "requested")
        self.assertEqual(reconcile_trade(self.BUY, [CurrencyChange(at(1), "starbux", -50)]).state, "unknown")
        self.assertEqual(reconcile_trade(self.BUY, [InventoryChange(at(1), "77", 1)]).state, "unknown")
        done = reconcile_trade(self.BUY, [CurrencyChange(at(1), "starbux", -52), InventoryChange(at(2), "77", 1)])
        self.assertEqual((done.state, done.cost, done.fees, done.sale_id), ("completed", 52, 2, "9001"))
        short = reconcile_trade(self.BUY, [CurrencyChange(at(1), "starbux", -30), InventoryChange(at(2), "77", 1)])
        self.assertEqual(short.state, "unknown")


class ValidationTests(unittest.TestCase):
    def test_bad_requests_and_observations(self):
        bad = [
            SellRequest(at=datetime(2027, 2, 10), item_design_id="77", quantity=1, currency="starbux", price=50),
            SellRequest(at=at(0), item_design_id="", quantity=1, currency="starbux", price=50),
            SellRequest(at=at(0), item_design_id="77", quantity=0, currency="starbux", price=50),
            SellRequest(at=at(0), item_design_id="77", quantity=1, currency="starbux", price=True),
            BuyRequest(at=at(0), sale_id="", item_design_id="77", quantity=1, currency="starbux", price=50),
        ]
        for request in bad:
            with self.subTest(request=request), self.assertRaises(TradeInputError):
                reconcile_trade(request)
        with self.assertRaises(TradeInputError):
            reconcile_trade(SELL, [CurrencyChange(datetime(2027, 2, 10, 9), "starbux", 5)])


if __name__ == "__main__":
    unittest.main()
