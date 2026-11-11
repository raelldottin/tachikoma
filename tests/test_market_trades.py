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
from sdk.economy.trades import default_max_fee

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

    def test_proceeds_before_the_listing_is_gone_do_not_count(self):
        # Regression: an expired listing whose item return is not yet observed, plus a resource collection in
        # the trade currency while the listing was open, read as a sale.
        request = SellRequest(at=at(0), item_design_id="77", quantity=1, currency="gas", price=1000)
        listing = MarketListing("Thing", "gas", 1000, sale_id="111")
        observations = [InventoryChange(at(0.1), "77", -1), seen(0.2, listing), CurrencyChange(at(5), "gas", 40),
                        ListingGone(at(48 * 60), "111")]
        result = reconcile_trade(request, observations)
        self.assertEqual((result.state, result.proceeds, result.fees), ("unknown", None, None))
        self.assertIn("no proceeds", result.reason)

    def test_proceeds_outside_the_fee_bound_are_unknown(self):
        def sold(proceeds, **kwargs):
            observations = [InventoryChange(at(1), "77", -1), seen(2), ListingGone(at(30), "9001"),
                            CurrencyChange(at(31), "starbux", proceeds)]
            return reconcile_trade(SELL, observations, **kwargs)

        self.assertEqual(default_max_fee(50), 13)
        self.assertEqual(sold(37).state, "completed")
        too_low = sold(36)
        self.assertEqual((too_low.state, too_low.proceeds), ("unknown", None))
        self.assertIn("outside the expected proceeds 37..50", too_low.reason)
        self.assertEqual(sold(45, max_fee=4).state, "unknown")
        self.assertEqual(sold(46, max_fee=4).state, "completed")

    def test_several_matching_listings_are_ambiguous(self):
        other = MarketListing("Laser Cannon", "starbux", 50, sale_id="9002", item_design_id="77", quantity=1)
        result = reconcile_trade(SELL, [seen(1), seen(2, other), ListingGone(at(30), "9001")])
        self.assertEqual((result.state, result.sale_id), ("requested", None))
        self.assertIn("ambiguous listing", result.reason)

    def test_listings_open_before_the_request_do_not_match(self):
        older = MarketListing("Laser Cannon", "starbux", 50, sale_id="8000", item_design_id="77", quantity=1)
        result = reconcile_trade(SELL, [seen(-10, older), seen(1, older), seen(2)])
        self.assertEqual((result.state, result.sale_id), ("listed", "9001"))
        only_older = reconcile_trade(SELL, [seen(-10, older), seen(1, older)])
        self.assertEqual(only_older.state, "requested")
        self.assertIn("before the request", only_older.reason)

    def test_listing_for_another_quantity_does_not_match(self):
        bundle = MarketListing("Laser Cannon", "starbux", 50, sale_id="9003", item_design_id="77", quantity=3)
        self.assertEqual(reconcile_trade(SELL, [seen(1, bundle)]).state, "requested")


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

    def test_unrelated_spend_and_arrival_are_not_a_purchase(self):
        # Regression: an item from a daily reward plus an unrelated 500 starbux spend read as a purchase.
        request = BuyRequest(at=at(0), sale_id="s9", item_design_id="77", quantity=1, currency="starbux", price=10)
        result = reconcile_trade(request, [InventoryChange(at(60), "77", 1), CurrencyChange(at(120), "starbux", -500)])
        self.assertEqual((result.state, result.cost, result.fees), ("unknown", None, None))
        self.assertIn("expected 10..13", result.reason)

    def test_payment_bound_and_exact_quantity(self):
        def bought(cost, arrived=1, **kwargs):
            observations = [CurrencyChange(at(1), "starbux", -cost), InventoryChange(at(2), "77", arrived)]
            return reconcile_trade(self.BUY, observations, **kwargs)

        self.assertEqual((bought(63).state, bought(63).fees), ("completed", 13))
        self.assertEqual(bought(64).state, "unknown")
        self.assertEqual(bought(52, max_fee=0).state, "unknown")
        self.assertEqual(bought(50, max_fee=0).state, "completed")
        self.assertEqual(bought(50, arrived=2).state, "unknown")

    def test_target_listing_still_open_contradicts_the_purchase(self):
        observations = [CurrencyChange(at(1), "starbux", -50), InventoryChange(at(2), "77", 1),
                        ListingSeen(at(3), MarketListing("Laser Cannon", "starbux", 50, sale_id="9001"))]
        self.assertEqual(reconcile_trade(self.BUY, observations).state, "unknown")
        gone = reconcile_trade(self.BUY, observations[:2] + [ListingGone(at(3), "9001")])
        self.assertEqual(gone.state, "completed")
        self.assertIn("listing 9001 gone", gone.evidence)


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
        for max_fee in (-1, True, 2.5):
            with self.subTest(max_fee=max_fee), self.assertRaises(TradeInputError):
                reconcile_trade(SELL, max_fee=max_fee)


if __name__ == "__main__":
    unittest.main()
