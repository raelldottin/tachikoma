"""Tests for sdk.reports.economy: market return and resource allocation. Synthetic, offline."""

import json
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
    build_ledger,
    reconcile_trade,
)
from sdk.journal import GameplayEvent
from sdk.reports import LedgerRecord, TradeRecord, compute_economy_report
from sdk.reports.economy import to_json, to_markdown
from sdk.state import build_snapshot

T0 = datetime(2027, 3, 1, 8, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def sold(proceeds=45):
    request = SellRequest(at=at(0), item_design_id="77", quantity=1, currency="starbux", price=50)
    listing = MarketListing("Laser", "starbux", 50, sale_id="s1", item_design_id="77")
    return reconcile_trade(request, [InventoryChange(at(1), "77", -1), ListingSeen(at(2), listing),
                                     ListingGone(at(9), "s1"), CurrencyChange(at(10), "starbux", proceeds)])


def bought(cost=52):
    request = BuyRequest(at=at(0), sale_id="s9", item_design_id="88", quantity=1, currency="starbux", price=50)
    return reconcile_trade(request, [CurrencyChange(at(1), "starbux", -cost), InventoryChange(at(2), "88", 1)])


def listed():
    request = SellRequest(at=at(0), item_design_id="77", quantity=1, currency="starbux", price=50)
    return reconcile_trade(request, [ListingSeen(at(2), MarketListing("Laser", "starbux", 50, sale_id="s2"))])


def expired():
    request = SellRequest(at=at(0), item_design_id="77", quantity=1, currency="starbux", price=50)
    listing = MarketListing("Laser", "starbux", 50, sale_id="s3")
    return reconcile_trade(request, [InventoryChange(at(1), "77", -1), ListingSeen(at(2), listing),
                                     ListingGone(at(9), "s3"), InventoryChange(at(10), "77", 1)])


def snap(minutes, minerals, gas="50"):
    return build_snapshot("alpha", at(minutes), "synthetic",
                          resources={"minerals": minerals, "gas": gas, "starbux": None})


def event(start, end, action_id):
    return GameplayEvent.create(account_alias="alpha", action_id=action_id, started_at=at(start),
                                finished_at=at(end), outcome="verified", reason="synthetic", attempted=True)


class EconomyReportTests(unittest.TestCase):
    def test_market_return_uses_completed_trades_only(self):
        [row] = compute_economy_report([TradeRecord("alpha", "v1", t) for t in (sold(), bought(), listed(), expired())])
        self.assertEqual((row.trades, row.completed, row.expired, row.unreconciled), (4, 2, 1, 1))
        self.assertEqual(row.market_return, {"starbux": 45 - 52})
        self.assertEqual(row.fees, {"starbux": 5 + 2})

    def test_no_completed_trade_means_no_return(self):
        [row] = compute_economy_report([TradeRecord("alpha", "v1", listed())])
        self.assertIsNone(row.market_return)
        self.assertEqual(row.fees, {})

    def test_ledger_resources_per_hour_and_spend_by_action(self):
        ledger = build_ledger("alpha", [snap(0, "100"), snap(30, "400"), snap(60, "250")],
                              [event(-1, 31, "collect_all_resources"), event(29, 61, "upgrade_room")])
        [row] = compute_economy_report(ledgers=[LedgerRecord("v1", ledger)])
        self.assertEqual(row.window_hours, 1.0)
        self.assertEqual(row.gained, {"minerals": 300})
        self.assertEqual(row.spent, {"minerals": 150})
        self.assertEqual(row.gained_per_hour["minerals"], 300.0)
        self.assertEqual(row.samples, {"minerals": 2, "gas": 2})
        self.assertIsNone(row.gained_per_hour["starbux"])
        self.assertEqual(row.spend_by_action, {"upgrade_room": {"minerals": 150}})

    def test_unattributed_and_ambiguous_counts(self):
        ledger = build_ledger("alpha", [snap(0, "100"), snap(30, "120"), snap(60, "90")],
                              [event(35, 36, "a"), event(40, 41, "b")])
        [row] = compute_economy_report(ledgers=[LedgerRecord("v1", ledger)])
        self.assertEqual((row.unattributed_entries, row.ambiguous_entries), (1, 1))

    def test_rows_split_by_account_and_variant(self):
        rows = compute_economy_report([TradeRecord("alpha", "v1", sold()), TradeRecord("alpha", "v2", sold(40)),
                                       TradeRecord("bravo", "v1", bought())])
        self.assertEqual([(r.account_alias, r.variant, r.market_return) for r in rows],
                         [("alpha", "v1", {"starbux": 45}), ("alpha", "v2", {"starbux": 40}),
                          ("bravo", "v1", {"starbux": -52})])

    def test_outputs(self):
        self.assertEqual(compute_economy_report(), [])
        self.assertEqual(to_markdown([]), "_No trades or ledger entries._\n")
        rows = compute_economy_report([TradeRecord("alpha", "v1", sold()), TradeRecord("alpha", "v1", listed())])
        self.assertEqual(json.loads(to_json(rows))[0]["market_return"], {"starbux": 45})
        self.assertIn("| alpha | v1 | 2 | 1 | 0 | 1 | starbux +45 | starbux +5 | n/a | n/a | n/a | 0 |",
                      to_markdown(rows))


if __name__ == "__main__":
    unittest.main()
