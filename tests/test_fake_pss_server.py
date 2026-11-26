"""Self-tests for tests/fakes: the stateful fake Pixel Starships server the offline e2e tests run against.

Most of them hit the fake through ``fake.session()``, a ``requests.Session`` with the fake's adapter mounted and no
``Client`` (ShapeContractTests too: they check the XML shapes against the client's readers, not through a Client).
ClockAndInstallTests enter ``fake.install()`` and use production's ``Client.session`` and clock globals directly; only
RealClientTests drive the real ``Client``. No socket ever opens.
"""

import datetime
import math
import os
import tempfile
import time
import unittest
import urllib.parse

import xmltodict

from sdk.client import _extract_collection
from tests.fakes.pss_server import (
    FAKE_ERROR_STATUS,
    HOST_UTC_MINUS_5,
    HOST_UTC_PLUS_9,
    ROOM_LIMIT_TEXT,
    ROUTES,
    FakePss,
)
from tests.fakes.state import (
    DEFAULT_ROSTER,
    FAKE_EPOCH,
    FAKE_PASSWORD,
    STATS,
    Character,
    CharacterDesign,
    MarketListing,
    Research,
    ResearchDesign,
    Room,
    RoomDesign,
    Task,
    TaskDesign,
    TrainingDesign,
    TrainingSession,
    World,
    default_world,
    fmt,
    minimal_world,
    stats,
)
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

BASE = "https://api.pixelstarships.com"
MIN5 = datetime.timedelta(minutes=5)
NOW = fmt(FAKE_EPOCH)  # a well-formed clientDateTime / *StartDate / collectDate


def token_of(response) -> str:
    return response.text.split('accessToken="')[1].split('"')[0]


class FakeTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakePss(default_world())
        self.s = self.fake.session()

    def get(self, path, **params):
        return self.s.get(f"{BASE}{path}?{urllib.parse.urlencode(params)}")

    def post(self, path, **params):
        return self.s.post(f"{BASE}{path}?{urllib.parse.urlencode(params)}")

    def guest_login(self):
        return token_of(self.s.post(f"{BASE}/UserService/DeviceLogin17", json={"RefreshToken": ""}))

    def account_login(self):
        reply = self.s.post(f"{BASE}/UserService/DeviceLogin17",
                            json={"RefreshToken": self.fake.state.account.refresh_token})
        return token_of(reply)

    def parse(self, response):
        return xmltodict.parse(response.content, xml_attribs=True)

    def error_of(self, response):
        """The errorMessage of an error envelope, or None."""
        found = []

        def walk(node):
            if isinstance(node, dict):
                if "@errorMessage" in node:
                    found.append(node["@errorMessage"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.parse(response))
        return found[0] if found else None


# --- Z: zero, empty, unknown ------------------------------------------------------------------------------------

class ZeroTests(FakeTestCase):
    def test_unknown_endpoint_is_recorded_and_answered_with_parseable_error_xml(self):
        reply = self.s.get(f"{BASE}/Nope/Nothing")
        self.assertEqual(reply.status_code, 200)
        self.assertIn("Unknown endpoint", self.error_of(reply))
        self.assertEqual(self.fake.unknown, [("GET", "/Nope/Nothing")])

    def test_wrong_method_on_known_path_is_unknown(self):
        token = self.account_login()
        self.get("/TrainingService/AddTraining", trainingDesignId="3", characterId="1001", accessToken=token)
        self.assertEqual(self.fake.unknown, [("GET", "/TrainingService/AddTraining")])
        self.assertEqual(self.fake.state.character("1001").last_session.end, FAKE_EPOCH - datetime.timedelta(hours=2))

    def test_empty_world_lists_parse_to_empty_collections(self):
        self.fake = FakePss(World())
        self.s = self.fake.session()
        token = self.account_login()
        parsed = self.parse(self.get("/CharacterService/ListAllCharactersOfUser", accessToken=token))
        self.assertIn("CharacterService", parsed)
        self.assertEqual(_extract_collection(parsed, "Character"), [])

    def test_a_crashing_handler_is_a_violation_not_an_exception_production_could_swallow(self):
        token = self.account_login()
        self.fake.state.characters[0].stats = None  # ty: ignore[invalid-assignment]  # breaks the serializer on purpose
        reply = self.get("/CharacterService/ListAllCharactersOfUser", accessToken=token)
        self.assertEqual((reply.status_code, reply.text), (FAKE_ERROR_STATUS, ""))
        self.assertEqual(len(self.fake.violations), 1)
        self.assertIn("FakePss handler error", self.fake.violations[0])

    def test_a_fake_error_is_answered_once_with_a_status_production_does_not_retry(self):
        # A 500 is in production's urllib3 status_forcelist: a fake bug would be re-sent 10 times (re-running a
        # mutating handler) and reach production as a RetryError, which it could log as its own failure.
        from sdk.client import Client

        self.assertFalse(Client.retry_strategy.is_retry("GET", FAKE_ERROR_STATUS))
        self.assertFalse(Client.retry_strategy.is_retry("POST", FAKE_ERROR_STATUS))
        self.assertGreaterEqual(FAKE_ERROR_STATUS, 400)  # a falsy requests response
        token = self.account_login()
        self.fake.state.characters[0].stats = None  # ty: ignore[invalid-assignment]  # breaks the serializer on purpose
        self.fake.fail("/TrainingService/FinishTraining", status=404, when=lambda p: p["noSuchParam"] == "1")
        start = len(self.fake.exchanges)
        with self.fake.install():
            crashed = Client.session.get(f"{BASE}/CharacterService/ListAllCharactersOfUser?accessToken={token}")
            predicate = Client.session.post(f"{BASE}/TrainingService/FinishTraining?characterId=1003&accessToken={token}")
        self.assertEqual((crashed.status_code, predicate.status_code), (FAKE_ERROR_STATUS, FAKE_ERROR_STATUS))
        self.assertEqual([e.status for e in self.fake.exchanges[start:]], [FAKE_ERROR_STATUS, FAKE_ERROR_STATUS])
        self.assertEqual(len(self.fake.violations), 2)
        self.assertFalse(self.fake.state.character("1003").last_session.applied)  # the predicate failed: no handler


# --- O: one request, one entity ---------------------------------------------------------------------------------

class OneRequestTests(FakeTestCase):
    def test_guest_device_login_issues_token_and_guest_user(self):
        reply = self.s.post(f"{BASE}/UserService/DeviceLogin17", json={"RefreshToken": ""})
        self.assertIn('accessToken="00000001-1111-2222-3333-444444444444"', reply.text)
        login = self.parse(reply)["UserService"]["UserLogin"]
        self.assertIsInstance(login["User"], dict)
        self.assertEqual(self.fake.tokens["00000001-1111-2222-3333-444444444444"], "guest")

    def test_authorize_with_guest_token_returns_refresh_token(self):
        guest = self.guest_login()
        body = urllib.parse.urlencode({"email": SYNTHETIC_EMAIL, "password": FAKE_PASSWORD, "accessToken": guest})
        reply = self.s.post(f"{BASE}/UserService/UserEmailPasswordAuthorize4?{body}", data=body)
        self.assertIn('refreshToken="RT-synthetic-daily-0001"', reply.text)
        self.assertEqual(self.fake.violations, [])

    def test_authorize_rejects_wrong_password_without_echoing_it(self):
        guest = self.guest_login()
        wrong = "wrong-synthetic-pass-9"
        body = urllib.parse.urlencode({"email": SYNTHETIC_EMAIL, "password": wrong, "accessToken": guest})
        reply = self.s.post(f"{BASE}/UserService/UserEmailPasswordAuthorize4?{body}", data=body)
        self.assertEqual(self.error_of(reply), "Email or password is incorrect")
        self.assertNotIn(wrong, reply.text)
        self.assertEqual(self.fake.requests[-1][2]["password"], "<redacted>")
        self.assertNotIn(wrong, repr(self.fake.requests))

    def test_refresh_token_login_returns_account_profile_with_minutes_old_heartbeat(self):
        reply = self.s.post(f"{BASE}/UserService/DeviceLogin17", json={"RefreshToken": "RT-synthetic-daily-0001"})
        login = self.parse(reply)["UserService"]["UserLogin"]
        user = login["User"]
        self.assertEqual(login["@UserId"], "700000501")
        self.assertEqual(user["@LastHeartBeatDate"], fmt(FAKE_EPOCH - MIN5))
        self.assertEqual((user["@Credits"], user["@DailyRewardStatus"], user["@FreeStarbuxReceivedToday"]),
                         ("1200", "0", "10"))
        self.assertEqual(self.fake.tokens[login["@accessToken"]], "account")

    def test_request_log_merges_query_and_body_params(self):
        self.s.post(f"{BASE}/Nope/Echo?a=1&blank=", data="b=2")
        self.assertEqual(self.fake.requests[-1], ("POST", "/Nope/Echo", {"a": "1", "blank": "", "b": "2"}))
        self.s.post(f"{BASE}/UserService/DeviceLogin17?q=x", json={"RefreshToken": "", "DeviceType": 0,
                                                                   "UserDeviceInfo": {"Locale": "en"}})
        self.assertEqual(self.fake.requests[-1][2], {"q": "x", "RefreshToken": "", "DeviceType": "0",
                                                     "UserDeviceInfo.Locale": "en"})

    def test_nested_json_body_fields_are_flattened_so_undeclared_ones_are_violations(self):
        body = {"RefreshToken": "", "IsJailBroken": False, "Signal": True, "DeviceType": 2, "AccessToken": None,
                "UserDeviceInfo": {"Locale": "en", "Extra": {"Deep": 1}}, "Tags": ["a", {"b": 1}], "Empty": {}}
        self.s.post(f"{BASE}/UserService/DeviceLogin17", json=body)
        self.assertEqual(self.fake.requests[-1][2], {
            "RefreshToken": "", "IsJailBroken": "false", "Signal": "true", "DeviceType": "2", "AccessToken": "null",
            "UserDeviceInfo.Locale": "en", "UserDeviceInfo.Extra.Deep": "1", "Tags[0]": "a", "Tags[1].b": "1",
            "Empty": "{}"})
        self.assertEqual(sorted(self.fake.violations), [
            "POST /UserService/DeviceLogin17: unexpected Empty",
            "POST /UserService/DeviceLogin17: unexpected Tags[0]",
            "POST /UserService/DeviceLogin17: unexpected Tags[1].b",
            "POST /UserService/DeviceLogin17: unexpected UserDeviceInfo.Extra.Deep"])

    def test_missing_access_token_is_violation_and_auth_error(self):
        self.account_login()
        reply = self.s.post(f"{BASE}/ItemService/ActivateItem3?itemId=9101&targetId=1001&")
        self.assertIn("Failed to authorize access token", reply.text)
        self.assertEqual(len(self.fake.violations), 1)
        self.assertIn("missing accessToken", self.fake.violations[0])
        self.assertEqual(self.fake.state.item("9101").quantity, 5)

    def test_guest_token_rejected_on_account_endpoints(self):
        guest = self.guest_login()
        reply = self.get("/CharacterService/ListAllCharactersOfUser", accessToken=guest)
        self.assertIn("Failed to authorize access token", reply.text)
        self.assertEqual(len(self.fake.violations), 1)
        self.assertIn("guest", self.fake.violations[0])
        self.assertEqual(self.fake.exchanges[-1].token_status, "guest")


# --- M: state transitions, many requests ------------------------------------------------------------------------

class StateTransitionTests(FakeTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.account_login()

    def call(self, method, path, **params):
        """Send like production: accessToken, and clientDateTime/checksum where the route requires them."""
        params.setdefault("accessToken", self.token)
        required = ROUTES[(method, path)].required if (method, path) in ROUTES else ()
        for name, value in (("clientDateTime", NOW), ("checksum", "c")):
            if name in required:
                params.setdefault(name, value)
        return (self.post if method == "POST" else self.get)(path, **params)

    def character_row(self, cid):
        parsed = self.parse(self.call("GET", "/CharacterService/ListAllCharactersOfUser"))
        return next(c for c in _extract_collection(parsed, "Character") if c["@CharacterId"] == cid)

    def add_training(self, design, cid):
        return self.call("POST", "/TrainingService/AddTraining", trainingDesignId=design, characterId=cid,
                         trainingStartDate=fmt(self.fake.now))

    def finish(self, cid):
        return self.call("POST", "/TrainingService/FinishTraining", characterId=cid)

    def test_add_training_starts_session_and_sets_training_end_date(self):
        reply = self.add_training("3", "1005")
        self.assertIsNone(self.error_of(reply))
        self.assertEqual(self.character_row("1005")["@TrainingEndDate"], fmt(FAKE_EPOCH + datetime.timedelta(hours=1)))
        session = self.fake.state.character("1005").last_session
        self.assertEqual((session.design_id, session.start, session.applied), ("3", FAKE_EPOCH, False))

    def test_finish_training_before_end_is_rejected_and_stats_unchanged(self):
        self.add_training("3", "1005")
        self.fake.advance(minutes=59)
        self.assertEqual(self.error_of(self.finish("1005")), "Training is not finished yet")
        self.assertEqual(self.fake.state.character("1005").stats["HpImprovement"], 30)
        self.assertEqual(self.fake.state.character("1005").fatigue, 0)

    def test_finish_training_after_advance_applies_gain_and_fatigue_exactly_once(self):
        self.add_training("3", "1005")
        self.fake.advance(hours=1)
        reply = self.finish("1005")
        character = self.parse(reply)["TrainingService"]["FinishTraining"]["Character"]
        self.assertEqual(character["@HpImprovement"], "32")
        self.assertEqual(character["@Fatigue"], "10")
        self.assertTrue(all(f"@{stat}" in character for stat in STATS))
        self.assertEqual(self.error_of(self.finish("1005")), "No training to finish")
        crew = self.fake.state.character("1005")
        self.assertEqual((crew.stats["HpImprovement"], crew.fatigue, crew.last_session.applied), (32, 10, True))

    def test_training_design_id_is_reported_only_while_a_session_is_unapplied(self):
        # The session's design while it is not yet applied (running or ended), "0" before any session and after the
        # finish; TrainingEndDate keeps the last session's end after the finish.
        self.assertEqual(self.character_row("1001")["@TrainingDesignId"], "3")  # ended 2 h ago, not finished
        self.assertEqual(self.character_row("1005")["@TrainingDesignId"], "0")  # never trained
        self.add_training("4", "1005")
        self.assertEqual(self.character_row("1005")["@TrainingDesignId"], "4")  # running
        self.assertIsNone(self.error_of(self.finish("1001")))
        row = self.character_row("1001")
        self.assertEqual((row["@TrainingDesignId"], row["@TrainingEndDate"]), ("0", fmt(FAKE_EPOCH - datetime.timedelta(hours=2))))

    def test_finish_training_without_session_is_rejected(self):
        self.assertEqual(self.error_of(self.finish("1004")), "No training to finish")
        self.assertEqual(self.fake.state.character("1004").stats, stats(Hp=10, Science=20))

    def test_add_training_rejects_unknown_design_including_literal_None_as_violation(self):
        for design in ("None", "999"):  # "None" is what production sends for an unknown training name (D8)
            with self.subTest(design=design):
                self.assertIn("Invalid training design", self.error_of(self.add_training(design, "1005")))
        self.assertIsNone(self.fake.state.character("1005").session)
        self.assertEqual(len(self.fake.violations), 2)

    def test_add_training_rejects_crew_outside_training_room(self):
        for cid in ("1009", "1002"):  # Mineral Storage; unassigned (RoomId 0)
            with self.subTest(cid=cid):
                self.assertEqual(self.error_of(self.add_training("3", cid)), "Crew must be in a training room")

    def test_add_training_rejects_crew_already_training(self):
        self.assertEqual(self.error_of(self.add_training("3", "1001")), "Crew is already training")
        self.add_training("3", "1005")
        self.assertEqual(self.error_of(self.add_training("4", "1005")), "Crew is already training")
        self.assertEqual(self.fake.state.character("1005").last_session.design_id, "3")

    def test_upgrade_character_increments_level_and_rejects_level_40(self):
        self.assertIsNone(self.error_of(self.call("POST", "/CharacterService/UpgradeCharacter", characterId="1009")))
        self.assertEqual(self.fake.state.character("1009").level, 2)
        reply = self.call("POST", "/CharacterService/UpgradeCharacter", characterId="1001")
        self.assertEqual(self.error_of(reply), "Crew is already at the maximum level")
        self.assertEqual(self.fake.state.character("1001").level, 40)

    def test_upgrade_character_accepts_unassigned_crew_and_charges_nothing(self):
        # Skipping RoomId "0" crew is production's own rule (upgradeCharacters, crew_leveling): nothing in the repo
        # shows the server refusing them, so the fake does not either. It charges no gas or xp: the costs are
        # production's tables, and copying them here would only make the fake agree with production by construction.
        gas, crew = self.fake.state.resources.gas, self.fake.state.character("1017")
        self.assertEqual((crew.room_id, crew.level), ("0", 1))
        self.assertIsNone(self.error_of(self.call("POST", "/CharacterService/UpgradeCharacter", characterId="1017")))
        self.assertEqual((crew.level, crew.xp, self.fake.state.resources.gas), (2, 500, gas))
        self.assertEqual(self.fake.violations, [])

    def test_upgrade_character_is_refused_until_its_utc_available_date(self):
        crew = self.fake.state.character("1012")  # level 1, AvailableDate FAKE_EPOCH + 3 h
        reply = self.call("POST", "/CharacterService/UpgradeCharacter", characterId="1012")
        self.assertEqual(self.error_of(reply), "Crew is not available yet")  # a game rule, not a violation
        self.assertEqual((crew.level, self.fake.violations), (1, []))
        self.fake.advance(hours=3)  # exactly the AvailableDate: available
        self.assertIsNone(self.error_of(self.call("POST", "/CharacterService/UpgradeCharacter", characterId="1012")))
        self.assertEqual(crew.level, 2)

    def test_activate_item_consumes_one_and_rejects_empty_stack(self):
        self.fake.state.item("9101").quantity = 1
        reply = self.call("POST", "/ItemService/ActivateItem3", itemId="9101", targetId="1001")
        self.assertEqual(self.parse(reply)["ItemService"]["ActivateItem3"]["Item"]["@Quantity"], "0")
        reply = self.call("POST", "/ItemService/ActivateItem3", itemId="9101", targetId="1001")
        self.assertEqual(self.error_of(reply), "Not enough items")
        self.assertEqual(self.fake.state.item("9101").quantity, 0)

    def test_collect_daily_reward_sets_status_then_rejects_repeat(self):
        reply = self.call("POST", "/UserService/CollectDailyReward2", dailyRewardStatus="Box", argument="3")
        self.assertIsNone(self.error_of(reply))
        self.assertEqual((self.fake.state.account.daily_reward_status, self.fake.state.resources.mineral),
                         ("1", 61000))
        reply = self.call("POST", "/UserService/CollectDailyReward2", dailyRewardStatus="Box", argument="3")
        self.assertEqual(self.error_of(reply), "You already collected this reward")
        self.assertEqual(self.fake.state.resources.mineral, 61000)

    def test_collect_daily_reward_with_wrong_argument_is_violation(self):
        reply = self.call("POST", "/UserService/CollectDailyReward2", dailyRewardStatus="Box", argument="0")
        self.assertIsNotNone(self.error_of(reply))
        self.assertEqual(len(self.fake.violations), 1)
        self.assertEqual(self.fake.state.account.daily_reward_status, "0")

    def test_collect_marker_marks_collected_and_rejects_repeat(self):
        reply = self.call("POST", "/GalaxyService/CollectMarker2", starSystemMarkerId="9001")
        self.assertIsNone(self.error_of(reply))
        self.assertTrue(self.fake.state.marker("9001").collected)
        self.assertEqual(self.fake.state.resources.mineral, 60500)
        reply = self.call("POST", "/GalaxyService/CollectMarker2", starSystemMarkerId="9001")
        self.assertEqual(self.error_of(reply), "Marker already collected")
        self.assertEqual(self.fake.state.resources.mineral, 60500)

    def test_upgrade_room_deducts_cost_sets_upgrading_rejects_unaffordable_and_blocks_at_limit(self):
        from sdk.client import ROOM_UPGRADE_BLOCKERS

        reply = self.call("POST", "/RoomService/UpgradeRoom2", roomId="102", upgradeRoomDesignId="31")
        self.assertEqual(self.error_of(reply), "Not enough minerals")
        reply = self.call("POST", "/RoomService/UpgradeRoom2", roomId="104", upgradeRoomDesignId="51")
        self.assertIsNone(self.error_of(reply))
        room = self.fake.state.room("104")
        self.assertEqual((room.status, room.pending_design_id, room.design_id), ("Upgrading", "51", "50"))
        self.assertEqual(self.fake.state.resources.mineral, 40000)
        reply = self.call("POST", "/RoomService/UpgradeRoom2", roomId="101", upgradeRoomDesignId="2")
        self.assertEqual(self.error_of(reply), ROOM_LIMIT_TEXT)
        self.assertIn(ROOM_LIMIT_TEXT, ROOM_UPGRADE_BLOCKERS)  # production stops upgradeRooms on this text
        reply = self.call("POST", "/RoomService/UpgradeRoom2", roomId="106", upgradeRoomDesignId="51")
        self.assertIn("cannot be upgraded to", self.error_of(reply))
        self.assertEqual(self.fake.state.room("106").status, "Normal")

    def test_upgrade_room_with_a_misconfigured_price_is_a_fake_error_not_a_free_upgrade(self):
        for price in ("", "mineral", "mineral:", "ore:500", "mineral:-5", "gas:1e3"):
            with self.subTest(price=price):
                self.setUp()
                self.fake.state.room_design("51").price = price  # after validate(): only the handler can catch it
                reply = self.call("POST", "/RoomService/UpgradeRoom2", roomId="104", upgradeRoomDesignId="51")
                self.assertEqual(reply.status_code, FAKE_ERROR_STATUS)
                self.assertEqual(len(self.fake.violations), 1)
                self.assertIn("PriceString", self.fake.violations[0])
                self.assertEqual((self.fake.state.room("104").status, self.fake.state.resources.mineral),
                                 ("Normal", 60000))

    def test_upgrade_room_priced_in_starbux_is_paid_from_credits(self):
        self.fake.state.room_designs.append(RoomDesign("52", "Mineral Storage Lv2b", "Storage", "50", "starbux:200"))
        reply = self.call("POST", "/RoomService/UpgradeRoom2", roomId="104", upgradeRoomDesignId="52")
        self.assertIsNone(self.error_of(reply))
        self.assertEqual((self.fake.state.account.credits, self.fake.state.resources.mineral), (1000, 60000))

    def test_add_research_adds_researching_record_deducts_gas_rejects_second(self):
        reply = self.call("POST", "/ResearchService/AddResearch", researchDesignId="10", researchStartDate=NOW)
        self.assertIsNone(self.error_of(reply))
        self.assertEqual([(r.design_id, r.state) for r in self.fake.state.researches],
                         [("1", "Completed"), ("10", "Researching")])
        self.assertEqual(self.fake.state.resources.gas, 38000)
        reply = self.call("POST", "/ResearchService/AddResearch", researchDesignId="2", researchStartDate=NOW)
        self.assertEqual(self.error_of(reply), "Another research is in progress")
        self.assertEqual(self.fake.state.resources.gas, 38000)

    def test_collect_all_resources_reports_current_totals_and_credits(self):
        self.fake.state.resources.mineral, self.fake.state.resources.gas = 123, 456
        parsed = self.parse(self.call("POST", "/RoomService/CollectAllResources", itemType="None", collectDate=NOW))
        items = {i["@Type"]: i["@Quantity"] for i in _extract_collection(parsed, "Item")}
        self.assertEqual(items, {"Mineral": "123", "Gas": "456"})
        self.assertEqual(parsed["RoomService"]["CollectResources"]["User"]["@Credits"], "1200")

    def test_collect_all_resources_can_list_only_the_resources_on_board(self):
        state = self.fake.state
        state.collect_omits_empty_items = True
        for (mineral, gas), expected in (((123, 0), {"Mineral": "123"}), ((0, 456), {"Gas": "456"}),
                                         ((123, 456), {"Mineral": "123", "Gas": "456"})):
            with self.subTest(mineral=mineral, gas=gas):
                state.resources.mineral, state.resources.gas = mineral, gas
                parsed = self.parse(self.call("POST", "/RoomService/CollectAllResources", itemType="None",
                                              collectDate=NOW))
                self.assertEqual({i["@Type"]: i["@Quantity"] for i in _extract_collection(parsed, "Item")}, expected)

    def test_collect_all_resources_can_leave_out_the_user_record(self):
        self.fake.state.collect_reply_has_user = False
        parsed = self.parse(self.call("POST", "/RoomService/CollectAllResources", itemType="None", collectDate=NOW))
        self.assertNotIn("User", parsed["RoomService"]["CollectResources"])
        self.assertEqual(len(_extract_collection(parsed, "Item")), 2)
        self.assertEqual(self.fake.state.account.credits, 1200)  # only the reply changes, not the balance

    def test_task_reward_message_reward_action_message_and_purchase_transitions(self):
        state = self.fake.state
        self.assertIsNone(self.error_of(self.call("POST", "/TaskService/CollectTaskCompletion", taskDesignId="7001")))
        self.assertEqual((state.task("7001").collected, state.account.credits), (True, 1210))
        reply = self.call("POST", "/TaskService/CollectTaskCompletion", taskDesignId="7002")
        self.assertEqual(self.error_of(reply), "Task is not complete")
        self.call("POST", "/MessageService/CollectReward2", messageId="8001")  # "starbux:5"
        self.call("GET", "/MessageService/ActionMessage", messageId="8003")
        self.assertEqual((state.message("8001").collected, state.message("8003").actioned), (True, True))
        self.assertEqual(self.error_of(self.call("POST", "/MessageService/CollectReward2", messageId="8001")),
                         "Reward already collected")
        reply = self.call("POST", "/ShopService/PurchaseCatalog2", argument="1291")
        purchase = self.parse(reply)["ShopService"]["PurchaseCatalog2"]
        self.assertEqual((purchase["@errorCode"], purchase["User"]["@Credits"]), ("0", "215"))
        purchase = self.parse(self.call("POST", "/ShopService/PurchaseCatalog2", argument="1291"))
        self.assertEqual(purchase["ShopService"]["PurchaseCatalog2"]["@errorMessage"], "Not enough starbux")
        self.assertEqual(state.account.credits, 215)


# --- P: protocol strictness (violations are what production must never send) --------------------------------------

class ProtocolTests(FakeTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.account_login()

    def call(self, method, path, **params):
        """Send like production: accessToken, and clientDateTime/checksum where the route requires them."""
        params.setdefault("accessToken", self.token)
        required = ROUTES[(method, path)].required if (method, path) in ROUTES else ()
        for name, value in (("clientDateTime", NOW), ("checksum", "c")):
            if name in required:
                params.setdefault(name, value)
        return (self.post if method == "POST" else self.get)(path, **params)

    def test_every_route_declares_required_and_optional_params_without_access_token(self):
        for (method, path), route in ROUTES.items():
            with self.subTest(route=f"{method} {path}"):
                self.assertIsInstance(route.required, tuple)
                self.assertIsInstance(route.optional, tuple)
                self.assertNotIn("accessToken", route.required + route.optional)  # implied by route.token
                self.assertEqual(set(route.required) & set(route.optional), set())

    def test_missing_required_param_is_a_violation_and_nothing_is_applied(self):
        reply = self.call("POST", "/TrainingService/AddTraining", trainingDesignId="3", characterId="1005")
        self.assertIn("missing trainingStartDate", self.error_of(reply))
        self.assertEqual(self.fake.violations, ["POST /TrainingService/AddTraining: missing trainingStartDate"])
        self.assertIsNone(self.fake.state.character("1005").session)

    def test_unexpected_param_is_a_violation(self):
        self.call("POST", "/TrainingService/FinishTraining", characterId="1001", trainingDesignId="3")
        self.assertEqual(self.fake.violations, ["POST /TrainingService/FinishTraining: unexpected trainingDesignId"])
        self.assertFalse(self.fake.state.character("1001").last_session.applied)

    def test_malformed_date_param_is_a_violation(self):
        self.call("POST", "/TrainingService/AddTraining", trainingDesignId="3", characterId="1005",
                  trainingStartDate="2026-11-20 12:00:00")
        self.assertEqual(len(self.fake.violations), 1)
        self.assertIn("trainingStartDate '2026-11-20 12:00:00' is not", self.fake.violations[0])

    def test_unknown_entity_ids_are_violations(self):
        cases = (
            ("POST", "/TrainingService/AddTraining", {"trainingDesignId": "None", "characterId": "1005",
                                                      "trainingStartDate": NOW}),
            ("POST", "/TrainingService/AddTraining", {"trainingDesignId": "3", "characterId": "9999",
                                                      "trainingStartDate": NOW}),
            ("POST", "/TrainingService/FinishTraining", {"characterId": "9999"}),
            ("POST", "/CharacterService/UpgradeCharacter", {"characterId": "9999"}),
            ("POST", "/ItemService/ActivateItem3", {"itemId": "9999", "targetId": "1001"}),
            ("POST", "/ItemService/ActivateItem3", {"itemId": "9101", "targetId": "9999"}),
            ("POST", "/MessageService/CollectReward2", {"messageId": "9999", "clientDateTime": NOW, "checksum": "c"}),
            ("GET", "/MessageService/ActionMessage", {"messageId": "9999"}),
            ("POST", "/GalaxyService/CollectMarker2", {"starSystemMarkerId": "9999", "clientDateTime": NOW,
                                                       "checksum": "c"}),
            ("POST", "/RoomService/UpgradeRoom2", {"roomId": "9999", "upgradeRoomDesignId": "51"}),
            ("POST", "/RoomService/UpgradeRoom2", {"roomId": "104", "upgradeRoomDesignId": "9999"}),
            ("POST", "/ResearchService/AddResearch", {"researchDesignId": "9999", "researchStartDate": NOW}),
            ("POST", "/TaskService/CollectTaskCompletion", {"taskDesignId": "9999"}),
        )
        for method, path, params in cases:
            with self.subTest(path=path, params=params):
                before = len(self.fake.violations)
                self.assertIsNotNone(self.error_of(self.call(method, path, **params)))
                self.assertEqual(len(self.fake.violations), before + 1)
                self.assertIn("9999" if "9999" in params.values() else "None", self.fake.violations[-1])

    def test_game_rule_refusals_are_errors_not_violations(self):
        cases = (
            ("POST", "/TrainingService/FinishTraining", {"characterId": "1004"}, "No training to finish"),
            ("POST", "/TrainingService/AddTraining", {"trainingDesignId": "3", "characterId": "1009",
                                                      "trainingStartDate": NOW}, "Crew must be in a training room"),
            ("POST", "/TrainingService/AddTraining", {"trainingDesignId": "3", "characterId": "1001",
                                                      "trainingStartDate": NOW}, "Crew is already training"),
            ("POST", "/CharacterService/UpgradeCharacter", {"characterId": "1001"},
             "Crew is already at the maximum level"),
            ("POST", "/GalaxyService/CollectMarker2", {"starSystemMarkerId": "9002", "clientDateTime": NOW,
                                                       "checksum": "c"}, "Marker already collected"),
            ("POST", "/TaskService/CollectTaskCompletion", {"taskDesignId": "7002"}, "Task is not complete"),
            ("POST", "/RoomService/UpgradeRoom2", {"roomId": "102", "upgradeRoomDesignId": "31"},
             "Not enough minerals"),
        )
        for method, path, params, error in cases:
            with self.subTest(error=error):
                self.assertEqual(self.error_of(self.call(method, path, **params)), error)
        self.assertEqual(self.fake.violations, [])

    def test_a_faulted_request_is_still_checked_for_missing_params(self):
        fault = self.fake.fail("/TrainingService/FinishTraining", status=404)
        self.assertEqual(self.call("POST", "/TrainingService/FinishTraining").status_code, 404)
        self.assertEqual(fault.hits, 1)
        self.assertEqual(self.fake.violations, ["POST /TrainingService/FinishTraining: missing characterId"])


# --- B: boundaries, clock, install ------------------------------------------------------------------------------

class ClockAndInstallTests(FakeTestCase):
    def test_clock_advance_moves_now_and_emitted_dates(self):
        token = self.account_login()
        self.assertEqual(self.fake.advance(hours=2, minutes=30), FAKE_EPOCH + datetime.timedelta(hours=2, minutes=30))
        self.assertEqual(self.fake.now, self.fake.clock.now)
        self.post("/UserService/HeartBeat4", clientDateTime=NOW, checksum="c", accessToken=token)
        self.post("/TrainingService/AddTraining", trainingDesignId="4", characterId="1005",
                  trainingStartDate=fmt(self.fake.now), accessToken=token)
        crew = self.fake.state.character("1005")
        self.assertEqual(crew.training_end, FAKE_EPOCH + datetime.timedelta(hours=5, minutes=30))
        login = self.parse(self.s.post(f"{BASE}/UserService/DeviceLogin17",
                                       json={"RefreshToken": "RT-synthetic-daily-0001"}))
        self.assertEqual(login["UserService"]["UserLogin"]["User"]["@LastHeartBeatDate"], fmt(self.fake.now))

    def test_install_freezes_production_clock_and_restores_on_exit(self):
        import datetime as real_datetime

        from sdk import client as client_mod, dotnet as dotnet_mod

        with self.fake.install():
            # production calls the deprecated utcnow(), so this checks the stand-in answers it
            self.assertEqual(client_mod.datetime.datetime.utcnow(), self.fake.now)  # ty: ignore[deprecated]
            # The default host is UTC (HOST_UTC), like the CI runners, so local time equals UTC here; the non-UTC
            # host test below checks that the two come apart.
            self.assertEqual(client_mod.datetime.datetime.now(), self.fake.now)
            self.assertEqual(dotnet_mod.DotNet.validDateTime(), self.fake.now)
            self.fake.advance(hours=1)
            self.assertEqual(dotnet_mod.DotNet.validDateTime(), FAKE_EPOCH + datetime.timedelta(hours=1))
            self.assertEqual(client_mod.time.time(), (self.fake.now - datetime.datetime(1970, 1, 1)).total_seconds())
            parsed = client_mod.datetime.datetime.strptime("2026-11-20T10:00:00", "%Y-%m-%dT%H:%M:%S")
            utc_now = client_mod.datetime.datetime.utcnow()  # ty: ignore[deprecated]
            hour_ago = utc_now - client_mod.datetime.timedelta(hours=1)
            self.assertLess(parsed, hour_ago)
        self.assertIs(client_mod.datetime, real_datetime)
        self.assertIs(dotnet_mod.datetime, real_datetime.datetime)
        with self.assertRaises(RuntimeError), self.fake.install():
            raise RuntimeError("boom")
        self.assertIs(client_mod.datetime, real_datetime)
        self.assertIs(client_mod.time, time)

    def test_a_non_utc_host_shifts_every_local_clock_read_and_no_utc_read(self):
        from sdk import client as client_mod, crew_leveling, dotnet as dotnet_mod

        # Each clock is set where local and UTC fall on different dates, so a date-only read is checked too.
        for offset, hours in ((HOST_UTC_MINUS_5, 14), (HOST_UTC_PLUS_9, 4)):  # 02:00 UTC Nov 21; 16:00 UTC Nov 20
            with self.subTest(offset=offset):
                fake = FakePss(World(), local_offset=offset)
                fake.advance(hours=hours)
                utc = FAKE_EPOCH + datetime.timedelta(hours=hours)
                local = utc + offset
                epoch = (utc - datetime.datetime(1970, 1, 1)).total_seconds()
                with fake.install():
                    dt, tm = client_mod.datetime, client_mod.time
                    self.assertEqual(fake.clock.local_now, local)
                    # UTC reads (DotNet.validDateTime() is production's utcnow(), the same FrozenDateTime class)
                    self.assertIs(dotnet_mod.datetime, dt.datetime)
                    self.assertEqual(dotnet_mod.DotNet.validDateTime(), utc)
                    self.assertEqual(crew_leveling.datetime.now(datetime.timezone.utc),
                                     utc.replace(tzinfo=datetime.timezone.utc))
                    self.assertEqual(tm.time(), epoch)
                    self.assertEqual(tm.gmtime()[:6], utc.timetuple()[:6])
                    # local reads
                    self.assertEqual(dt.datetime.now(), local)
                    self.assertEqual(dt.datetime.today(), local)
                    self.assertEqual(dt.date.today(), local.date())
                    self.assertEqual(dt.datetime.fromtimestamp(tm.time()), local)
                    self.assertEqual(tm.localtime()[:6], local.timetuple()[:6])
                    self.assertEqual(tm.localtime().tm_gmtoff, offset.total_seconds())
                    self.assertEqual(tm.mktime(tm.localtime()), epoch)
                    self.assertEqual(tm.strftime("%Y-%m-%dT%H:%M:%S"), fmt(local))
                    self.assertEqual(tm.ctime(), time.asctime(local.timetuple()))
                    self.assertNotEqual(dt.datetime.now().date(), dotnet_mod.DotNet.validDateTime().date())
                    self.assertEqual(fake.clock.leaks(), [])
        # The fake itself only speaks UTC: emitted dates do not move with the host.
        fake = FakePss(default_world(), local_offset=HOST_UTC_PLUS_9)
        session = fake.session()
        login = session.post(f"{BASE}/UserService/DeviceLogin17", json={"RefreshToken": "RT-synthetic-daily-0001"})
        self.assertIn(f'LastHeartBeatDate="{fmt(FAKE_EPOCH - MIN5)}"', login.text)

    def test_install_freezes_every_daily_path_sdk_module_with_forwarding_stand_ins(self):
        import datetime as real_datetime
        import time as real_time

        from sdk import client as client_mod, crew_leveling, dotnet as dotnet_mod

        aware_now = FAKE_EPOCH.replace(tzinfo=datetime.timezone.utc)
        with self.fake.install():
            self.assertEqual(crew_leveling.datetime.now(datetime.timezone.utc), aware_now)  # from datetime import
            self.assertEqual(client_mod.datetime.date.today(), FAKE_EPOCH.date())
            self.assertEqual(client_mod.time.strftime("%Y-%m-%dT%H:%M:%S"), fmt(FAKE_EPOCH))
            self.assertEqual(client_mod.time.gmtime()[:6], FAKE_EPOCH.timetuple()[:6])
            self.assertEqual(client_mod.time.time_ns(), int(client_mod.time.time()) * 10**9)
            # Everything else is forwarded to the real modules, so a new production call cannot hit AttributeError.
            self.assertIs(client_mod.datetime.time, real_datetime.time)
            self.assertIs(client_mod.datetime.UTC, real_datetime.UTC)
            self.assertIs(client_mod.time.thread_time, real_time.thread_time)
            self.assertEqual(self.fake.clock.leaks(), [])
        self.assertIs(crew_leveling.datetime, real_datetime.datetime)
        self.assertIs(dotnet_mod.datetime, real_datetime.datetime)
        self.assertIs(client_mod.time, real_time)

    def test_frozen_datetime_classes_keep_isinstance_working_both_ways(self):
        import datetime as real_datetime

        from sdk import client as client_mod, crew_leveling

        real_dt, real_date = real_datetime.datetime(2026, 1, 2, 3, 4, 5), real_datetime.date(2026, 1, 2)
        with self.fake.install():
            frozen_dt, frozen_date = client_mod.datetime.datetime, client_mod.datetime.date
            self.assertIs(crew_leveling.datetime, frozen_dt)
            reads = (frozen_dt.utcnow(), frozen_dt.now(), frozen_dt.now(datetime.timezone.utc),  # ty: ignore[deprecated]
                     frozen_dt.today(), frozen_dt.fromtimestamp(0), frozen_dt.strptime("2026", "%Y"))
            for value in (real_dt, *reads):
                with self.subTest(value=value):
                    self.assertIsInstance(value, frozen_dt)  # production's isinstance(x, datetime.datetime)
                    self.assertIsInstance(value, real_datetime.datetime)
                    self.assertIsInstance(value, frozen_date)  # a datetime is a date, as with the real classes
            for value in (real_date, frozen_date.today()):
                with self.subTest(value=value):
                    self.assertIsInstance(value, frozen_date)
                    self.assertIsInstance(value, real_datetime.date)
                    self.assertNotIsInstance(value, frozen_dt)  # a date is not a datetime
            self.assertNotIsInstance("2026-11-20", frozen_dt)
            self.assertTrue(issubclass(real_datetime.datetime, frozen_dt))
            self.assertTrue(issubclass(real_datetime.date, frozen_date))
            self.assertFalse(issubclass(real_datetime.date, frozen_dt))

    def test_monotonic_and_perf_counter_follow_the_fake_clock_so_bounded_waits_end(self):
        import ratelimit.decorators

        from sdk import client as client_mod
        from sdk.client import Client

        with self.fake.install():
            tm = client_mod.time
            started, perf_started = tm.monotonic(), tm.perf_counter()
            deadline = started + 600
            polls = 0
            while tm.monotonic() < deadline:  # a monotonic-bounded wait loop, as a poller would write it
                polls += 1
                self.assertLess(polls, 100, "monotonic() did not move with sleep()")
                tm.sleep(60)
            self.assertEqual(polls, 10)
            self.assertEqual(tm.monotonic() - started, 600)
            self.assertEqual(tm.perf_counter() - perf_started, 600)
            self.assertEqual(tm.monotonic_ns() - round(started * 10**9), 600 * 10**9)
            self.assertEqual(tm.perf_counter_ns(), tm.monotonic_ns())
            self.fake.advance(hours=1)  # the fake's advance moves it too
            self.assertEqual(tm.monotonic() - started, 4200)
            self.assertIsNotNone(self.fake.clock.stand_in_for(time.monotonic))  # also "from time import monotonic"
            self.assertIsNotNone(self.fake.clock.stand_in_for(time.perf_counter))
            # The rate limiter keeps the real clock, and install() still calls the unwrapped request body.
            self.assertIs(ratelimit.decorators.now, time.monotonic)
            self.assertFalse(hasattr(Client.request, "__wrapped__"))
        self.assertIs(client_mod.time, time)

    def test_time_ns_is_exact_integer_arithmetic(self):
        from sdk import client as client_mod

        self.fake.advance(microseconds=123457)
        exact = ((self.fake.now - datetime.datetime(1970, 1, 1)) // datetime.timedelta(microseconds=1)) * 1000
        with self.fake.install():
            self.assertEqual(client_mod.time.time_ns(), exact)
            self.assertIsInstance(client_mod.time.time_ns(), int)

    def test_an_sdk_module_imported_inside_install_that_holds_the_real_clock_is_a_violation(self):
        import sys
        import types

        probe = types.ModuleType("sdk._fake_clock_probe")
        setattr(probe, "datetime", datetime)  # what "import datetime" inside a module first imported mid-run would bind
        self.addCleanup(sys.modules.pop, probe.__name__, None)
        with self.fake.install():
            sys.modules[probe.__name__] = probe
        self.assertEqual(self.fake.violations, [
            "sdk._fake_clock_probe: reads the real clock through 'datetime' (imported inside install(); "
            "add it to DAILY_PATH_MODULES)"])

    def test_install_restores_adapter_request_and_drones(self):
        from sdk.client import Client

        adapter, drones = Client.session.adapters["https://"], Client.dronesCollected
        with self.fake.install():
            self.assertIs(Client.session.adapters["https://"], self.fake.adapter)
            self.assertFalse(hasattr(Client.request, "__wrapped__"))
            self.assertIsNot(Client.dronesCollected, drones)
            self.assertFalse(Client.session.trust_env)
        self.assertIs(Client.session.adapters["https://"], adapter)
        self.assertTrue(hasattr(Client.request, "__wrapped__"))
        self.assertIs(Client.dronesCollected, drones)
        self.assertEqual(list(Client.session.adapters), ["https://", "http://"])

    def test_install_mirrors_production_retry_of_server_errors(self):
        from requests.exceptions import RetryError

        from sdk.client import Client

        path = "/TrainingService/ListAllTrainingDesigns2"
        url = f"{BASE}{path}?languageKey=en&designVersion=14"
        with self.fake.install():
            once = self.fake.fail(path, status=500)
            self.assertEqual(Client.session.get(url).status_code, 200)  # re-sent, as urllib3 would
            self.assertEqual((once.hits, len(self.fake.exchanges)), (1, 2))
            self.fake.fail(path, status=404)
            self.assertEqual(Client.session.get(url).status_code, 404)  # a 4xx is not retried
            self.assertEqual(len(self.fake.exchanges), 3)
            persistent = self.fake.fail(path, status=503, times=11)
            with self.assertRaises(RetryError):
                Client.session.get(url)
            self.assertEqual((persistent.hits, len(self.fake.exchanges)), (11, 14))
        self.assertIsNone(self.fake.adapter.retry)  # fake.session() stays retry-free
        self.fake.fail(path, status=500)
        self.assertEqual(self.s.get(url).status_code, 500)

    def test_install_retries_a_network_exception_like_urllib3(self):
        import requests

        from sdk.client import Client

        path = "/TrainingService/ListAllTrainingDesigns2"
        url = f"{BASE}{path}?languageKey=en&designVersion=14"
        with self.fake.install():
            once = self.fake.fail(path, raises=requests.ConnectionError)
            self.assertEqual(Client.session.get(url).status_code, 200)  # urllib3 retries connection errors too
            self.assertEqual((once.hits, len(self.fake.exchanges)), (1, 2))
            persistent = self.fake.fail(path, raises=requests.ConnectionError, times=11)
            with self.assertRaises(requests.ConnectionError):
                Client.session.get(url)
            self.assertEqual((persistent.hits, len(self.fake.exchanges)), (11, 13))

    def test_a_persistent_network_fault_surfaces_as_requests_does_after_urllib3_retries(self):
        # requests 2.28 / urllib3 1.26 with production's Retry(total=10): once the retries run out urllib3 raises
        # MaxRetryError, and HTTPAdapter.send turns it into ConnectTimeout (connect timeout), SSLError, ProxyError,
        # or else ConnectionError -- so a persistent ReadTimeout reaches production as a ConnectionError.
        import requests
        from urllib3.exceptions import MaxRetryError, ReadTimeoutError

        from sdk.client import Client

        path = "/TrainingService/ListAllTrainingDesigns2"
        url = f"{BASE}{path}?languageKey=en&designVersion=14"
        cases = ((requests.ReadTimeout, requests.ConnectionError), (requests.Timeout, requests.ConnectionError),
                 (requests.ConnectionError, requests.ConnectionError), (requests.ConnectTimeout, requests.ConnectTimeout),
                 (requests.exceptions.SSLError, requests.exceptions.SSLError),
                 (requests.exceptions.ProxyError, requests.exceptions.ProxyError))
        with self.fake.install():
            for raised, surfaced in cases:
                with self.subTest(raised=raised.__name__):
                    once = self.fake.fail(path, raises=raised)
                    self.assertEqual(Client.session.get(url).status_code, 200)  # retried, as urllib3 would
                    self.assertEqual(once.hits, 1)
                    persistent = self.fake.fail(path, raises=raised, times=11)
                    with self.assertRaises(surfaced) as caught:
                        Client.session.get(url)
                    self.assertIs(type(caught.exception), surfaced)
                    self.assertIsInstance(caught.exception.args[0], MaxRetryError)
                    self.assertEqual(persistent.hits, 11)
            self.fake.fail(path, raises=requests.ReadTimeout, times=11)
            with self.assertRaises(requests.ConnectionError) as caught:
                Client.session.get(url)
            self.assertIsInstance(caught.exception.args[0].reason, ReadTimeoutError)
            self.assertNotIsInstance(caught.exception, requests.Timeout)
            # Not a network failure urllib3 retries (it happens while reading the body): raised on the first send.
            body_fault = self.fake.fail(path, raises=requests.exceptions.ChunkedEncodingError, times=11)
            with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                Client.session.get(url)
            self.assertEqual(body_fault.hits, 1)

    def test_install_blocks_and_records_sends_from_any_other_session(self):
        import requests
        from requests.adapters import HTTPAdapter

        original = HTTPAdapter.send
        with self.fake.install():
            with self.assertRaises(requests.exceptions.ConnectionError):
                requests.Session().get("https://api.pixelstarships.com/UserService/Elsewhere")
        self.assertEqual(self.fake.violations, ["GET /UserService/Elsewhere: real network send blocked"])
        self.assertEqual(self.fake.exchanges, [])
        self.assertIs(HTTPAdapter.send, original)

    def test_sleep_inside_install_advances_clock_and_records_without_sleeping(self):
        from sdk import client as client_mod

        started = time.monotonic()
        with self.fake.install():
            client_mod.time.sleep(300)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.fake.now, FAKE_EPOCH + datetime.timedelta(seconds=300))
        self.assertEqual(self.fake.clock.sleeps, [300])

    def test_install_makes_device_keys_and_random_deterministic(self):
        from sdk import client as client_mod, device as device_mod

        draws = []
        for _ in range(2):
            with FakePss(World()).install():
                # 32 hex digits and 16 small ints: unseeded generators would agree by chance far below 1e-40.
                draws.append(("".join(device_mod.random.choice("0123456789abcdef") for _ in range(32)),
                              [client_mod.random.randint(1, 5) for _ in range(16)]))
        self.assertEqual(draws[0], draws[1])


# --- I: the fault interface -------------------------------------------------------------------------------------

class FaultTests(FakeTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.account_login()

    def test_fail_status_for_n_requests_then_recovers(self):
        fault = self.fake.fail("/UserService/CollectDailyReward2", times=2, status=500)
        replies = [self.post("/UserService/CollectDailyReward2", dailyRewardStatus="Box", argument="3",
                             accessToken=self.token) for _ in range(3)]
        self.assertEqual([bool(r) for r in replies], [False, False, True])
        self.assertEqual(fault.hits, 2)
        self.assertEqual([e.error for e in self.fake.exchanges[-3:]], ["fault", "fault", None])
        self.assertEqual(self.fake.state.account.daily_reward_status, "1")

    def test_fail_with_error_returns_200_error_envelope_for_route_element(self):
        self.fake.fail("/TrainingService/ListAllTrainingDesigns2", error="Design service down")
        reply = self.get("/TrainingService/ListAllTrainingDesigns2", languageKey="en", designVersion="14")
        self.assertEqual(reply.status_code, 200)
        node = self.parse(reply)["TrainingService"]["ListAllTrainingDesigns"]
        self.assertEqual(node["@errorMessage"], "Design service down")
        reply = self.get("/TrainingService/ListAllTrainingDesigns2", languageKey="en", designVersion="14")
        self.assertEqual(len(_extract_collection(self.parse(reply), "TrainingDesign")), 18)

    def test_fail_and_expire_reject_a_path_that_is_no_route(self):
        for path in ("/Nope/Nothing", "/TrainingService/AddTrainin", "TrainingService/AddTraining"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "not a FakePss route"):
                    self.fake.fail(path, status=404)
                with self.assertRaisesRegex(ValueError, "not a FakePss route"):
                    self.fake.expire_token_once(at=path)
        self.assertEqual(self.fake.faults, [])

    def test_fail_raises_a_network_exception_instead_of_replying(self):
        import requests

        fault = self.fake.fail("/TrainingService/ListAllTrainingDesigns2", raises=requests.ConnectionError)
        with self.assertRaises(requests.ConnectionError):
            self.get("/TrainingService/ListAllTrainingDesigns2", languageKey="en", designVersion="14")
        self.assertEqual((fault.hits, self.fake.exchanges[-1].error), (1, "fault"))
        reply = self.get("/TrainingService/ListAllTrainingDesigns2", languageKey="en", designVersion="14")
        self.assertEqual(reply.status_code, 200)

    def test_fail_with_body_returns_raw_body(self):
        self.fake.fail("/ShopService/PurchaseCatalog2", body="<<not xml")
        reply = self.post("/ShopService/PurchaseCatalog2", argument="1291", accessToken=self.token)
        self.assertEqual((reply.status_code, reply.text), (200, "<<not xml"))
        self.assertEqual(self.fake.state.account.credits, 1200)

    def test_fail_when_predicate_targets_one_character(self):
        fault = self.fake.fail("/TrainingService/FinishTraining", status=503,
                               when=lambda p: p.get("characterId") == "1001")
        ok = self.post("/TrainingService/FinishTraining", characterId="1003", accessToken=self.token)
        bad = self.post("/TrainingService/FinishTraining", characterId="1001", accessToken=self.token)
        self.assertEqual((ok.status_code, bad.status_code, fault.hits), (200, 503, 1))
        self.assertTrue(self.fake.state.character("1003").last_session.applied)
        self.assertFalse(self.fake.state.character("1001").last_session.applied)

    def test_fault_still_records_a_missing_or_revoked_token(self):
        fault = self.fake.fail("/CharacterService/ListAllCharactersOfUser", status=404, times=2)
        self.assertEqual(self.get("/CharacterService/ListAllCharactersOfUser").status_code, 404)  # no accessToken
        self.assertEqual(self.get("/CharacterService/ListAllCharactersOfUser", accessToken=self.token).status_code,
                         404)
        self.assertEqual(fault.hits, 2)
        self.assertEqual(len(self.fake.violations), 1)
        self.assertIn("missing accessToken", self.fake.violations[0])
        self.assertEqual([e.error for e in self.fake.exchanges[-2:]], ["fault", "fault"])

    def test_a_raising_fault_predicate_is_a_violation_not_an_exception(self):
        self.fake.fail("/TrainingService/FinishTraining", status=404, when=lambda p: p["noSuchParam"] == "1")
        reply = self.post("/TrainingService/FinishTraining", characterId="1001", accessToken=self.token)
        self.assertEqual((reply.status_code, reply.text), (FAKE_ERROR_STATUS, ""))
        self.assertEqual(len(self.fake.violations), 1)
        self.assertIn("FakePss error KeyError", self.fake.violations[0])

    def test_fail_method_filter_and_first_matching_fault_wins(self):
        first = self.fake.fail("/MessageService/ActionMessage", method="POST", status=500)
        second = self.fake.fail("/MessageService/ActionMessage", error="first GET fault")
        third = self.fake.fail("/MessageService/ActionMessage", error="second GET fault")
        reply = self.get("/MessageService/ActionMessage", messageId="8003", accessToken=self.token)
        self.assertEqual(self.error_of(reply), "first GET fault")
        self.assertEqual((first.hits, second.hits, third.hits), (0, 1, 0))

    def test_expire_token_once_revokes_at_path_and_refresh_login_issues_new_token(self):
        path = "/MessageService/ListActiveMarketplaceMessages5"
        self.fake.expire_token_once(at=path)
        params = {"itemSubType": "None", "userId": "700000501", "accessToken": self.token}
        self.assertIn("Failed to authorize access token", self.get(path, **params).text)
        self.assertIn("Failed to authorize access token", self.get(path, **params).text)
        self.assertEqual([e.token_status for e in self.fake.exchanges[-2:]], ["revoked", "revoked"])
        fresh = self.account_login()
        self.assertEqual(fresh, "00000002-1111-2222-3333-444444444444")
        self.assertNotIn("errorMessage", self.get(path, **dict(params, accessToken=fresh)).text)
        self.assertEqual(self.fake.exchanges[-1].token_status, "current")
        self.assertEqual(len(self.fake.violations), 2)


# --- E: shape contracts (what production reads) -----------------------------------------------------------------

def walk(node, path: str):
    """Follow "A/B/@C" through a parsed reply; KeyError/TypeError names the first missing step."""
    for step in path.split("/"):
        node = node[step]
    return node


CHARACTER_KEYS = ("@CharacterId", "@CharacterName", "@CharacterDesignId", "@RoomId", "@Level", "@Xp", "@Fatigue",
                  "@TrainingDesignId", "@TrainingEndDate", "@AvailableDate", *(f"@{stat}" for stat in STATS))
# (method, path, params without accessToken, item key for _extract_collection or None, keys each item must carry).
READ_CONTRACTS = (
    ("GET", "/TaskService/ListTasksOfAUser", {"clientDateTime": NOW}, "Task",
     ("@TaskDesignId", "@ProgressValue", "@Collected")),
    ("GET", "/TaskService/ListAllTaskDesigns2", {"languageKey": "en", "designVersion": "13"}, "TaskDesign",
     ("@TaskDesignId", "@ObjectiveAmount", "@Name")),
    ("GET", "/CharacterService/ListAllCharactersOfUser", {"clientDateTime": NOW}, "Character", CHARACTER_KEYS),
    ("GET", "/ResearchService/ListAllResearches", {"clientDateTime": NOW}, "Research",
     ("@ResearchDesignId", "@ResearchState")),
    ("GET", "/ResearchService/ListAllResearchDesigns2", {"languageKey": "en", "designVersion": "12"},
     "ResearchDesign", ("@ResearchDesignId", "@RootResearchDesignId", "@GasCost", "@StarbuxCost", "@ResearchName")),
    ("GET", "/RoomService/ListRoomDesigns2", {"languageKey": "en", "designVersion": "11"}, "RoomDesign",
     ("@RoomDesignId", "@RoomName", "@RoomType", "@UpgradeFromRoomDesignId", "@PriceString", "@ColumnWidth",
      "@RowHeight", "@RoomHp", "@PowerGenerated", "@MaxPopulation")),
    ("GET", "/ShipService/GetShipByUserId", {"userId": "700000501", "clientDateTime": NOW}, "Room",
     ("@RoomId", "@RoomDesignId", "@RoomStatus", "@Row", "@Column")),
    ("GET", "/RoomService/ListRoomsViaAccessToken", {"clientDateTime": NOW}, "Room", ("@RoomId", "@RoomDesignId")),
    ("GET", "/GalaxyService/ListStarSystemMarkersAndUserMarkers", {}, "StarSystemMarker",
     ("@StarSystemMarkerId", "@StarSystemId", "@MarkerType", "@IsCollected", "@RewardString")),
    ("GET", "/MessageService/ListActiveMarketplaceMessages5", {"userId": "700000501"}, "Message",
     ("@Message", "@ActivityArgument")),
    ("GET", "/MessageService/ListSystemMessagesForUser3", {"fromMessageId": "0", "take": "10000"}, "Message",
     ("@MessageId", "@Message", "@ActivityArgument")),
    ("GET", "/CharacterService/ListAllCharacterDesigns2", {"languageKey": "en", "designVersion": "15"},
     "CharacterDesign", ("@CharacterDesignId", "@TrainingCapacity", "@SpecialAbilityType", "@Rarity")),
    ("GET", "/TrainingService/ListAllTrainingDesigns2", {"languageKey": "en", "designVersion": "14"},
     "TrainingDesign", ("@TrainingDesignId", "@TrainingName")),
    ("GET", "/ItemService/ListItemsOfAShip", {"clientDateTime": NOW}, "Item", ("@ItemId", "@ItemDesignId")),
    ("POST", "/RoomService/CollectAllResources", {"itemType": "None", "collectDate": NOW}, "Item",
     ("@Type", "@Quantity")),
)
# Exact key paths production walks without _extract_collection (sdk/client.py readers of these replies).
EXACT_PATHS = (
    ("GET", "/SettingService/GetLatestVersion3", {"languageKey": "en", "deviceType": "DeviceTypeIPhone"},
     "SettingService/GetLatestSetting/Setting/@CharacterDesignVersion"),
    ("GET", "/LiveOpsService/GetTodayLiveOps2", {"languageKey": "en", "deviceType": "DeviceTypeIPhone"},
     "LiveOpsService/GetTodayLiveOps/LiveOps/@DailyRewardArgument"),
    ("POST", "/UserService/HeartBeat4", {"clientDateTime": NOW, "checksum": "c"}, "UserService/HeartBeat/@success"),
    ("POST", "/RoomService/CollectAllResources", {"itemType": "None", "collectDate": NOW},
     "RoomService/CollectResources/User/@Credits"),
    ("GET", "/ShipService/GetShipByUserId", {"userId": "700000501", "clientDateTime": NOW},
     "ShipService/GetShipByUserId/Ship/@StarSystemId"),
    ("GET", "/GalaxyService/ListStarSystemMarkersAndUserMarkers", {},
     "GalaxyService/ListStarSystemMarkersAndUserMarkers/StarSystemMarkers/StarSystemMarker"),
    ("GET", "/GalaxyService/ListStarSystemMarkersAndUserMarkers", {},
     "GalaxyService/ListStarSystemMarkersAndUserMarkers/UserMarkers/UserMarker/@StarSystemMarkerId"),
    ("POST", "/UserService/AddStarbux2", {"quantity": "1", "clientDateTime": NOW, "checksum": "c"},
     "UserService/AddStarbux/User/@FreeStarbuxReceivedToday"),
)


class ShapeContractTests(FakeTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.account_login()

    def read(self, method, path, params):
        if ROUTES[(method, path)].token != "none":  # production sends no accessToken to design lists or settings
            params = dict(params, accessToken=self.token)
        return self.parse(self.post(path, **params) if method == "POST" else self.get(path, **params))

    def test_read_shapes_satisfy_client_readers(self):
        for method, path, params, item_key, keys in READ_CONTRACTS:
            with self.subTest(path=path):
                rows = _extract_collection(self.read(method, path, params), item_key)
                self.assertTrue(rows, f"{path} gave no {item_key}")
                for row in rows:
                    self.assertEqual([k for k in keys if k not in row], [], f"{path} {item_key} lacks keys")
        for method, path, params, key_path in EXACT_PATHS:
            with self.subTest(path=path, key_path=key_path):
                walk(self.read(method, path, params), key_path)
        self.assertEqual(self.fake.violations, [])
        self.assertEqual(self.fake.unknown, [])

    def test_settings_carry_the_five_design_versions_and_no_file_version(self):
        setting = walk(self.read("GET", "/SettingService/GetLatestVersion3", {}),
                       "SettingService/GetLatestSetting/Setting")
        kinds = ("Room", "Research", "Task", "Training", "Character")
        versions = {key: setting[f"@{key}DesignVersion"] for key in kinds}
        self.assertEqual(versions, self.fake.state.design_versions)
        self.assertNotIn("@FileVersion", setting)  # with it, production calls ListAllDesigns4, which needs ~35 more design versions

    def test_character_designs_are_a_list_and_root_keys_match_production_checks(self):
        designs = walk(self.read("GET", "/CharacterService/ListAllCharacterDesigns2", {"designVersion": "15"}),
                       "CharacterService/ListAllCharacterDesigns/CharacterDesigns/CharacterDesign")
        self.assertIsInstance(designs, list)  # production's shape (sdk/training/parse.py also reads a lone dict: D5)
        for path, root in (("/CharacterService/ListAllCharactersOfUser", "CharacterService"),
                           ("/RoomService/ListRoomsViaAccessToken", "RoomService"),
                           ("/MessageService/ListSystemMessagesForUser3", "MessageService")):
            self.assertIn(root, self.read("GET", path, {}))
        self.assertIn("ResearchService", self.read("GET", "/ResearchService/ListAllResearchDesigns2",
                                                   {"designVersion": "12"}))

    def test_user_markers_sit_under_the_action_element_not_directly_under_galaxy_service(self):
        parsed = self.read("GET", "/GalaxyService/ListStarSystemMarkersAndUserMarkers", {})
        self.assertNotIn("UserMarkers", parsed["GalaxyService"])  # the old M1 defect read it there
        markers = _extract_collection(parsed, "StarSystemMarker")
        self.assertEqual([m["@StarSystemMarkerId"] for m in markers], ["9001", "9002", "9003"])

    def test_research_records_carry_no_name_or_level_unless_set(self):
        rows = _extract_collection(self.read("GET", "/ResearchService/ListAllResearches", {}), "Research")
        self.assertEqual(rows, [{"@ResearchDesignId": "1", "@ResearchState": "Completed"}])  # D1

    def test_design_version_and_user_id_mismatches_are_violations(self):
        self.read("GET", "/RoomService/ListRoomDesigns2", {"designVersion": "1"})
        self.read("GET", "/ShipService/GetShipByUserId", {"userId": "0"})
        self.assertEqual(len(self.fake.violations), 2)

    def test_go_to_and_speed_up_move_the_ship_and_report_success(self):
        goto = self.read("POST", "/GalaxyService/GoTo", {"starSystemId": "77", "clientDateTime": NOW, "checksum": "c"})
        ship = walk(goto, "GalaxyService/GoTo/Ship")
        arrival = FAKE_EPOCH + datetime.timedelta(seconds=self.fake.state.travel_seconds)
        self.assertEqual((ship["@StarSystemId"], ship["@StarSystemArrivalDate"]), ("77", fmt(arrival)))
        reply = self.post("/GalaxyService/SpeedUpTravelling", checksum="c", clientDateTime=NOW, accessToken=self.token)
        self.assertIn("Success", reply.text)
        self.assertEqual(self.fake.state.ship.arrival, FAKE_EPOCH)

    def test_add_starbux_caps_at_ten_and_credits_only_what_it_grants(self):
        self.fake.state.account.free_starbux_today = 8
        reply = self.read("POST", "/UserService/AddStarbux2", {"quantity": "5", "clientDateTime": NOW, "checksum": "c"})
        self.assertEqual(walk(reply, "UserService/AddStarbux/User/@FreeStarbuxReceivedToday"), "10")
        self.assertEqual(self.fake.state.account.credits, 1202)
        self.read("POST", "/UserService/AddStarbux2", {"quantity": "1", "clientDateTime": NOW, "checksum": "c"})
        self.assertEqual((self.fake.state.account.free_starbux_today, self.fake.state.account.credits), (10, 1202))

    def test_default_world_percents_match_documented_tiers(self):
        world = default_world()
        capacity = {d.id: d.training_capacity for d in world.character_designs}
        for row in DEFAULT_ROSTER:
            with self.subTest(crew=row.id):
                self.assertEqual(math.ceil(sum(row.stats.values()) / capacity[row.design_id] * 100),
                                 row.expected_percent)
                self.assertEqual(world.character(row.id).stats, row.stats)


# --- W: building worlds -----------------------------------------------------------------------------------------

class WorldBuilderTests(unittest.TestCase):
    def test_training_end_is_derived_from_the_session(self):
        crew = Character("1", "Crew", "501", "103")
        self.assertIsNone(crew.training_end)
        crew.session = TrainingSession("3", FAKE_EPOCH, FAKE_EPOCH + datetime.timedelta(hours=1))
        self.assertEqual(crew.training_end, FAKE_EPOCH + datetime.timedelta(hours=1))
        crew.last_session.applied = True  # the server keeps reporting the last end date after FinishTraining
        self.assertEqual(crew.training_end, FAKE_EPOCH + datetime.timedelta(hours=1))

    def test_validate_names_every_dangling_reference(self):
        world = minimal_world()
        world.characters.append(Character("1", "Ghost", "999", "777"))
        world.characters[-1].session = TrainingSession("888", FAKE_EPOCH, FAKE_EPOCH)
        world.rooms.append(Room("778", "666", pending_design_id="665"))
        world.researches.append(Research("555"))
        world.tasks.append(Task("444", 1))
        with self.assertRaises(ValueError) as caught:
            world.validate()
        for expected in ("character 1: design 999", "character 1: room 777", "character 1: training design 888",
                         "room 778: design 666", "room 778: pending design 665", "research 555: design 555",
                         "task 444: design 444"):
            self.assertIn(expected, str(caught.exception))

    def test_fake_refuses_a_world_with_dangling_references(self):
        world = minimal_world()
        world.characters.append(Character("1", "Ghost", "999", "0"))  # RoomId 0 (unassigned) is valid
        with self.assertRaisesRegex(ValueError, "character 1: design 999"):
            FakePss(world)

    def test_default_and_minimal_worlds_validate(self):
        default_world().validate()
        minimal_world().validate()
        names = {d.name for d in minimal_world().room_designs}
        self.assertTrue({"GYM", "Academy", "Galaxy Gym"} <= names, names)

    def test_add_crew_creates_its_design_resolves_the_room_by_name_and_validates(self):
        world = minimal_world()
        crew = world.add_crew("Galactic Succubus", room="Galaxy Gym", stats=stats(Hp=20, Weapon=40), capacity=120,
                              rarity="Legendary", training=("Bench Press", -2 * datetime.timedelta(hours=1)))
        design = next(d for d in world.character_designs if d.id == crew.design_id)
        self.assertEqual((design.training_capacity, design.rarity), (120, "Legendary"))
        self.assertEqual(world.room_design(world.room(crew.room_id).design_id).name, "Galaxy Gym")
        self.assertEqual(crew.stats, stats(Hp=20, Weapon=40))
        self.assertEqual((crew.last_session.design_id, crew.last_session.end, crew.last_session.applied),
                         ("3", FAKE_EPOCH - datetime.timedelta(hours=2), False))
        self.assertEqual(crew.last_session.start, crew.last_session.end - world.training_design("3").duration)
        second = world.add_crew("Spare", room=None)
        self.assertEqual((second.room_id, second.session), ("0", None))
        self.assertNotEqual(second.id, crew.id)
        self.assertNotEqual(second.design_id, crew.design_id)
        world.validate()

    def test_add_crew_rejects_an_unknown_room_or_training_name(self):
        world = minimal_world()
        with self.assertRaisesRegex(KeyError, "no room named 'Moon Gym'"):
            world.add_crew("A", room="Moon Gym")
        with self.assertRaisesRegex(KeyError, "no training design named 'Yoga'"):
            world.add_crew("A", training=("Yoga", datetime.timedelta(0)))

    def test_add_room_adds_a_room_of_a_new_or_existing_design(self):
        world = minimal_world()
        lab = world.add_room("Science Lab", room_type="Laboratory")
        self.assertEqual(world.room_design(lab.design_id).room_type, "Laboratory")
        gym2 = world.add_room("GYM")
        self.assertEqual(world.room_design(gym2.design_id).name, "GYM")
        bridge2 = world.add_room("Bridge", room_type="Bridge")  # agreeing with the existing design is fine
        self.assertEqual(world.room_design(bridge2.design_id).room_type, "Bridge")
        self.assertEqual(len({r.id for r in world.rooms}), len(world.rooms))
        world.validate()

    def test_add_room_refuses_arguments_that_contradict_an_existing_design(self):
        world = minimal_world()
        rooms = len(world.rooms)
        for kwargs in ({"room_type": "Laboratory"}, {"upgrade_from": "900"}, {"price": "mineral:5"}):
            with self.subTest(**kwargs):
                with self.assertRaisesRegex(ValueError, "GYM"):
                    world.add_room("GYM", **kwargs)
        self.assertEqual(len(world.rooms), rooms)

    def test_validate_rejects_duplicate_design_ids_in_every_design_collection(self):
        cases = (("room design", lambda w: w.room_designs.append(RoomDesign("30", "GYM copy", "Training")), "30"),
                 ("research design", lambda w: w.research_designs.append(ResearchDesign("10", "10", "Dup", 1)), "10"),
                 ("character design", lambda w: w.character_designs.append(CharacterDesign("501", "Dup")), "501"),
                 ("training design", lambda w: w.training_designs.append(
                     TrainingDesign("3", "Dup", "HpImprovement", 1, 1, datetime.timedelta(hours=1))), "3"),
                 ("task design", lambda w: w.task_designs.append(TaskDesign("7001", "Dup", 1)), "7001"),
                 ("market listing", lambda w: w.market_listings.append(MarketListing("8501", "Dup", "None")), "8501"))
        for kind, mutate, key in cases:
            with self.subTest(kind=kind):
                world = default_world()
                mutate(world)
                with self.assertRaisesRegex(ValueError, f"duplicate {kind} id {key}"):
                    world.validate()

    def test_validate_rejects_an_upgrade_target_without_a_valid_price(self):
        for price in ("", "mineral", "ore:500", "mineral:abc", "gas:-1"):
            with self.subTest(price=price):
                world = default_world()
                world.room_design("51").price = price
                with self.assertRaisesRegex(ValueError, f"room design 51: PriceString {price!r}"):
                    world.validate()
        world = default_world()
        world.room_design("50").price = ""  # a base design (upgrade_from "0") is never bought through UpgradeRoom2
        world.validate()


# --- S: the real Client against the fake ------------------------------------------------------------------------

def login_settings() -> dict:
    """What run.main() passes to Client for an email login (imported, so the two cannot drift apart)."""
    from run import CLIENT_CHECKSUM_SETTINGS

    return {"allow_email_password_login": True, **CLIENT_CHECKSUM_SETTINGS}


class RealClientTests(unittest.TestCase):
    """The real Client logs at INFO; each test captures it with assertLogs, so the test output stays clean."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tachikoma-fake-")
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)  # Device() reads and writes ./.device
        self.addCleanup(os.chdir, cwd)

    def client(self):
        from sdk.client import Client
        from sdk.device import Device

        client = Client(Device(language="en"), login_settings())
        self.assertTrue(client.login(SYNTHETIC_EMAIL, FAKE_PASSWORD))
        return client

    def test_real_client_three_stage_login_against_fake(self):
        fake = FakePss(default_world())
        with self.assertLogs(level="INFO") as logs, fake.install():
            client = self.client()
        self.assertIn("[SyntheticCaptain] Authenticated...", "\n".join(logs.output))
        self.assertTrue(client.user.isAuthorized)
        self.assertEqual(client.accessToken, "00000002-1111-2222-3333-444444444444")
        self.assertEqual(fake.tokens[client.accessToken], "account")
        self.assertEqual(client._designVersion("TrainingDesignVersion"), "14")
        self.assertIsNone(client._designVersion("FileVersion"))
        self.assertEqual(client.todayLiveOps["LiveOpsService"]["GetTodayLiveOps"]["LiveOps"]["@DailyRewardArgument"],
                         "3")
        self.assertEqual([f"{m} {p}" for m, p, _ in fake.requests], [
            "POST /UserService/DeviceLogin17", "POST /UserService/UserEmailPasswordAuthorize4",
            "POST /UserService/DeviceLogin17", "GET /SettingService/GetLatestVersion3",
            "GET /LiveOpsService/GetTodayLiveOps2"])
        self.assertEqual((fake.violations, fake.unknown), ([], []))

    def test_two_clients_in_one_process_share_drones_collected(self):
        # Client.dronesCollected is a class attribute, so a second Client in the same process (the fleet runner makes
        # one per account) starts with the drones the first one collected and skips them. Here drone 9001 is
        # uncollected again on the server (another day, another account) and the second client never collects it.
        # install() resets the dict per install, which isolates tests but leaves this in-process sharing visible.
        # A fix (an instance dict set in __init__) flips this to: the second client sends CollectMarker2 for 9001.
        fake = FakePss(default_world())
        with self.assertLogs(level="INFO") as logs, fake.install():
            first = self.client()
            first.collectMiningDronesWithTravel()
            self.assertTrue(fake.state.marker("9001").collected)
            fake.state.marker("9001").collected = False
            second = self.client()
            start = len(fake.exchanges)
            second.collectMiningDronesWithTravel()
        self.assertIsNot(first, second)
        self.assertIs(second.dronesCollected, first.dronesCollected)  # PINNED(DC1)
        self.assertNotIn("POST /GalaxyService/CollectMarker2", fake.paths()[start:])  # PINNED(DC1)
        self.assertFalse(fake.state.marker("9001").collected)  # PINNED(DC1)
        self.assertEqual(sum("Collected mining drone 9001" in line for line in logs.output), 1)  # PINNED(DC1)
        self.assertEqual((fake.violations, fake.unknown), ([], []))


if __name__ == "__main__":
    unittest.main()
