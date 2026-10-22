"""Contracts for the revised Task J market evidence, without network access.

The derived ledger is a whitelist projection with aliases. The existing
sanitized wire fixtures provide separate request/response parsing evidence.
Neither is promoted to a reconciled delivery or a completed reciprocal swap.
"""

import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from sdk.inventory._types import Evidence
from sdk.inventory.reconcile import checksum_tier_ceiling, decide_transferability
from sdk.inventory.sanitize import scan_for_secrets


FIXTURES = Path(__file__).parent / "fixtures" / "task_j_capture"


class TestMarketEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = json.loads((FIXTURES / "sanitized_market_evidence.json").read_text())
        cls.wire = json.loads((FIXTURES / "sanitized_market_transfer.json").read_text())
        cls.records = json.loads((FIXTURES / "sanitized_records.json").read_text())

    def test_counts_distinguish_records_from_request_response_pairs(self):
        counts = self.ledger["endpoint_counts"]
        self.assertEqual(counts["MarketService/SellItem3"], {"requests": 6, "responses": 6})
        self.assertEqual(counts["MarketService/Purchase4"], {"requests": 1, "responses": 1})
        for name in ("GiftItem", "ListItemsOfAShip"):
            self.assertEqual(counts[f"ItemService/{name}"], {"requests": 0, "responses": 0})

    def test_sell_and_purchase_parse_as_post_forms_with_query_agreement(self):
        for name, node_name in (("a_sellitem3", "SellItem"), ("b_purchase4", "Purchase")):
            request = self.wire[f"{name}_req"]
            self.assertEqual(request["method"], "POST")
            params = parse_qs(urlsplit(request["url"]).query)
            self.assertEqual(params, parse_qs(request["body"]))
            self.assertEqual(params["accessToken"], ["[REDACTED]"])
            response = self.wire[f"{name}_resp"]
            self.assertEqual(response["status_code"], 200)
            node = ElementTree.fromstring(response["body"]).find(node_name)
            self.assertIsNotNone(node)
            self.assertEqual(node.get("success"), "True")
        sell = parse_qs(urlsplit(self.wire["a_sellitem3_req"]["url"]).query)
        self.assertEqual(sell["quantity"], [str(self.ledger["transfer"]["requested_quantity"])])
        self.assertEqual(sell["currencyType"], [self.ledger["transfer"]["currency_type"]])
        self.assertEqual(sell["currencyValue"], [str(self.ledger["transfer"]["currency_value"])])

    def test_purchase_joins_listing_sale_and_achievement_names_seller(self):
        purchase = parse_qs(urlsplit(self.wire["b_purchase4_req"]["url"]).query)
        listings = ElementTree.fromstring(self.wire["b_marketlist_pre_resp"]["body"])
        matches = [node for node in listings.iter() if node.get("SaleId") == purchase["saleId"][0]]
        self.assertEqual(len(matches), 1)
        listing = matches[0]
        self.assertEqual(listing.get("Argument"), "item:360x1")
        self.assertEqual(listing.get("ActivityArgument"), "gas:250000")
        response = ElementTree.fromstring(self.wire["b_purchase4_resp"]["body"])
        seller = response.find("Achievements/Achievement").get("UserId")
        self.assertEqual(seller, listing.get("UserId"))
        buyer = parse_qs(urlsplit(self.records["b_userdata_pre"]["url"]).query)["userId"][0]
        self.assertNotEqual(buyer, seller)
        self.assertEqual(purchase["allowConversion"], ["False"])
        self.assertNotIn("userId", purchase)
        self.assertIsNone(self.ledger["transfer"]["explicit_recipient_parameter"])

    def test_lifecycle_uses_comparable_browse_not_filtered_359_absence(self):
        before, after, filtered = self.ledger["market_browse"]
        self.assertEqual(before["filters"], after["filters"])
        self.assertTrue(before["target_sale_present"])
        self.assertFalse(after["target_sale_present"])
        self.assertLess(before["response_line"], self.ledger["transfer"]["purchase_request_line"])
        self.assertGreater(after["request_line"], self.ledger["transfer"]["purchase_response_line"])
        self.assertEqual(after["response_line"], 220)
        self.assertNotEqual(before["filters"], filtered["filters"])
        self.assertEqual(filtered["filters"]["itemDesignId"], "359")
        self.assertFalse(filtered["comparable_to_pre_purchase"])

    def test_pre_inventory_instances_are_distinct_and_post_state_stays_missing(self):
        observed_ids = []
        for alias in ("A", "B"):
            snapshot = self.ledger["inventory"][alias]
            root = ElementTree.fromstring(self.records[f"{alias.lower()}_userdata_pre_resp"]["body"])
            items = [node for node in root.iter("Item") if node.get("ItemDesignId") == "360"]
            self.assertEqual(len(items), 1)
            self.assertEqual(int(items[0].get("Quantity")), snapshot["pre_quantity"])
            observed_ids.append(items[0].get("ItemId"))
            self.assertIsNone(snapshot["post_quantity"])
        self.assertNotEqual(*observed_ids)
        sell = parse_qs(urlsplit(self.wire["a_sellitem3_req"]["url"]).query)
        self.assertEqual(sell["itemId"], [observed_ids[0]])
        self.assertNotIn("itemDesignId", sell)

    def test_later_listing_does_not_prove_reciprocal_swap(self):
        later = self.ledger["later_b_listing"]
        self.assertNotEqual(later["item_design_id"], self.ledger["transfer"]["item_design_id"])
        self.assertFalse(later["purchase_captured"])
        self.assertFalse(self.ledger["decision"]["reciprocal_swap_proven"])
        self.assertEqual(self.ledger["decision"]["reconciliation"], "UNRECONCILED")

    def test_single_purchase_checksum_does_not_promote_formula(self):
        ceiling = checksum_tier_ceiling(
            has_capture=True,
            has_independent_preimage_evidence=self.ledger["evidence"]["purchase_checksum_independent_preimage_proven"],
        )
        self.assertEqual(ceiling.value, self.ledger["evidence"]["purchase_checksum"])
        self.assertNotEqual(ceiling, Evidence.VERIFIED)

    def test_missing_market_post_state_closes_no_gift_transferability_gaps(self):
        decision = decide_transferability(verdicts=(), gaps_closed=())
        self.assertEqual(decision.decision, self.ledger["decision"]["transferability"])
        self.assertEqual(decision.proposed_quantity, 0)
        self.assertFalse(decision.actionable)
        self.assertEqual(self.ledger["evidence"]["GiftItem"], Evidence.STATICALLY_DISCOVERED.value)
        self.assertFalse(self.ledger["evidence"]["ListItemsOfAShip_promoted"])
        self.assertFalse(self.ledger["decision"]["gift_capture_required_for_this_worker"])

    def test_derived_fixture_contains_no_secrets_or_personal_account_fields(self):
        self.assertEqual(scan_for_secrets(self.ledger), [])
        forbidden = {"headers", "url", "body", "UserName", "Email", "Password", "accessToken", "checksum"}

        def walk(node):
            if isinstance(node, dict):
                self.assertFalse(forbidden.intersection(node))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.ledger)
