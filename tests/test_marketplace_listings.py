"""Tests for typed marketplace listings and Client.listActiveMarketplaceMessages storage. Mocked, no network."""

import unittest
from unittest.mock import MagicMock, patch

from sdk.client import Client
from sdk.device import Device
from sdk.economy import MarketInputError, MarketListing, listings_from_messages, observed_listings, parse_listing
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_EMAIL

TWO_MESSAGES = (
    b'<MessageService><ListActiveMarketplaceMessages><Messages>'
    b'<Message Message="Laser Cannon" ActivityArgument="starbux:50" SaleId="9001" ItemDesignId="77" Quantity="2"/>'
    b'<Message Message="Gas Tank" ActivityArgument="gas:1200"/>'
    b'</Messages></ListActiveMarketplaceMessages></MessageService>'
)
EMPTY = b'<MessageService><ListActiveMarketplaceMessages><Messages/></ListActiveMarketplaceMessages></MessageService>'


class ParseListingTests(unittest.TestCase):
    def test_known_fields(self):
        listing = parse_listing({"@Message": "Laser Cannon", "@ActivityArgument": "Starbux:50"})
        self.assertEqual((listing.item_name, listing.currency, listing.price), ("Laser Cannon", "starbux", 50))
        self.assertFalse(listing.has_identity)
        self.assertIsNone(listing.quantity)

    def test_optional_identity_fields_only_when_present(self):
        listing = parse_listing({"@Message": "x", "@ActivityArgument": "gas:5", "@SaleId": "9001",
                                 "@MessageId": "m1", "@ItemDesignId": "77", "@Quantity": "3"})
        self.assertTrue(listing.has_identity)
        self.assertEqual((listing.sale_id, listing.message_id, listing.item_design_id, listing.quantity),
                         ("9001", "m1", "77", 3))

    def test_unparseable_values_stay_unknown(self):
        cases = [{"@ActivityArgument": "starbux"}, {"@ActivityArgument": "starbux:lots"},
                 {"@ActivityArgument": "starbux:-5"}, {}]
        for message in cases:
            with self.subTest(message=message):
                listing = parse_listing(message)
                self.assertIsNone(listing.price)
        self.assertEqual(parse_listing({"@ActivityArgument": "starbux"}).currency, None)

    def test_list_message_text_is_joined(self):
        self.assertEqual(parse_listing({"@Message": ["Laser ", "Cannon"]}).item_name, "Laser Cannon")

    def test_secret_shaped_values_are_refused(self):
        with self.assertRaises(MarketInputError):
            parse_listing({"@Message": f"contact {SYNTHETIC_EMAIL}"})
        with self.assertRaises(MarketInputError):
            parse_listing("not a mapping")

    def test_unlisted_attributes_are_ignored(self):
        listing = parse_listing({"@Message": "x", "@UserId": "123", "@accessToken": SYNTHETIC_ACCESS_TOKEN})
        self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, str(listing.to_dict()))

    def test_observed_listings_distinguishes_unread_from_empty(self):
        self.assertIsNone(observed_listings(object()))
        holder = MagicMock(marketplaceMessages=[])
        self.assertEqual(observed_listings(holder), ())
        holder.marketplaceMessages = [{"@Message": "x", "@ActivityArgument": "gas:5"}]
        self.assertEqual(observed_listings(holder), (MarketListing("x", "gas", 5),))
        self.assertEqual(listings_from_messages([]), ())


class ClientMarketplaceStorageTests(unittest.TestCase):
    def setUp(self):
        device = Device(language="en")
        device.key = "00000000-0000-0000-0000-000000000000"
        self.client = Client(device=device)
        self.client.accessToken = SYNTHETIC_ACCESS_TOKEN
        self.client.info = {"@Name": "TestCaptain", "@Id": "123"}
        self.client.user = MagicMock(id="123")

    def read(self, content):
        with patch.object(self.client, "request") as request:
            request.return_value.content = content
            request.return_value.text = content.decode("utf-8") if content else ""
            return self.client.listActiveMarketplaceMessages()

    def test_successful_read_stores_messages(self):
        self.assertTrue(self.read(TWO_MESSAGES))
        listings = observed_listings(self.client)
        self.assertEqual([listing.item_name for listing in listings], ["Laser Cannon", "Gas Tank"])
        self.assertEqual(listings[0].sale_id, "9001")

    def test_empty_read_is_observed_and_empty(self):
        self.assertTrue(self.read(EMPTY))
        self.assertEqual(observed_listings(self.client), ())

    def test_failed_read_clears_previous_messages(self):
        self.read(TWO_MESSAGES)
        self.assertFalse(self.read(b'<MessageService errorMessage="Failed"/>'))
        self.assertIsNone(observed_listings(self.client))
        self.assertFalse(self.read(b""))
        self.assertIsNone(self.client.marketplaceMessages)


if __name__ == "__main__":
    unittest.main()
