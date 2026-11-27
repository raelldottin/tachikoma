#!/usr/bin/env python3
"""Training, crew, consumable and research wrappers report success only on a parsed success reply.

Every request is mocked at ``Client.request``, or answered by FakePss (tests/fakes) for manageTraining; nothing
reaches Pixel Starships.
"""

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, urlsplit

import requests
import xmltodict

from sdk import client as client_module
from sdk.client import Client, DotNet, _extract_collection
from sdk.device import Device
from sdk.execution.training import MemoryJournal
from sdk.training.parse import parse_research_levels
from tests.fakes.pss_server import FakePss
from tests.fakes.state import FAKE_PASSWORD, Item, default_world, minimal_world, stats
from tests.synthetic_fixtures import SYNTHETIC_EMAIL

TOKEN = "00000000-1111-2222-3333-444444444444"
NOW = datetime.datetime(2026, 11, 25, 1, 2, 3)
NOW_TEXT = "2026-11-25T01:02:03"
BASE = urlsplit(Client.baseUrl)


def reply(body):
    """A successful HTTP response carrying ``body``."""
    data = body.encode() if isinstance(body, str) else body
    return MagicMock(content=data, text=data.decode(errors="replace"))


def http_error(status=500, body=b"<Error/>"):
    """A real (falsy) requests.Response for an HTTP error status."""
    response = requests.Response()
    response.status_code = status
    response._content = body
    return response


class EndpointCase(unittest.TestCase):
    """A client with synthetic credentials and a recording fake ``request``."""

    def setUp(self):
        device = MagicMock(spec=Device)  # type: ignore
        device.languageKey = "en"
        self.client = Client(device=device)
        self.client.accessToken = TOKEN
        self.client.user = MagicMock(id="1")
        self.client.info = {"@Name": ""}
        clock = patch.object(DotNet, "validDateTime", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.calls = []

    def serve(self, *responses):
        """Answer successive requests with ``responses``, recording (url, method)."""
        queue = list(responses)

        def fake(url, method, data=None):
            self.calls.append((url, method))
            return queue.pop(0)

        self.request = MagicMock(side_effect=fake)
        patcher = patch.object(self.client, "request", self.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.request

    def refuse_requests(self):
        self.request = MagicMock(side_effect=AssertionError("no request expected"))
        patcher = patch.object(self.client, "request", self.request)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.request

    def sent(self, index=0):
        """(scheme+netloc, path, ordered params, method) of the index-th request."""
        url, method = self.calls[index]
        parts = urlsplit(url)
        params = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        return (parts.scheme, parts.netloc), parts.path, params, method


FAILURE_BODIES = {
    "empty body": lambda: reply(b""),
    "http error": lambda: http_error(),
    "not xml": lambda: reply(b"junk"),
}


class TestGameId(unittest.TestCase):
    def test_accepts_positive_integers_as_decimal_strings(self):
        self.assertEqual(client_module._game_id(12), "12")
        self.assertEqual(client_module._game_id("12"), "12")
        self.assertEqual(client_module._game_id("007"), "7")

    def test_refuses_anything_else(self):
        for value in (None, True, False, 0, "0", -1, "-1", 1.5, "", " 12", "12a", "abc", "١٢", []):
            with self.subTest(value=value):
                self.assertIsNone(client_module._game_id(value))


class TestParseReply(unittest.TestCase):
    def test_no_response_or_http_error_is_none(self):
        self.assertIsNone(client_module._parse_reply(None))
        self.assertIsNone(client_module._parse_reply(http_error(500, b"<A><B/></A>")))

    def test_empty_or_non_text_content_is_none(self):
        self.assertIsNone(client_module._parse_reply(reply(b"")))
        self.assertIsNone(client_module._parse_reply(MagicMock()))

    def test_unparseable_body_is_none_without_raising(self):
        for body in (b"not xml", b"<A>"):
            with self.subTest(body=body):
                self.assertIsNone(client_module._parse_reply(reply(body)))

    def test_error_message_anywhere_is_none(self):
        bodies = {
            "method element": b'<S><M errorMessage="no"/></S>',
            "service root": b'<S errorMessage="no"><M/></S>',
            "nested character": b'<S><M><Character CharacterId="1" errorMessage="no"/></M></S>',
            "repeated child": b'<S><M><C Id="1"/><C Id="2" errorMessage="no"/></M></S>',
            "child element": b"<S><M><errorMessage>no</errorMessage></M></S>",
        }
        for name, body in bodies.items():
            with self.subTest(name):
                self.assertIsNone(client_module._parse_reply(reply(body)))

    def test_success_is_the_parsed_dict(self):
        parsed = client_module._parse_reply(reply(b'<S><M><C Id="1"/></M></S>'))
        self.assertEqual(parsed, {"S": {"M": {"C": {"@Id": "1"}}}})

    def test_text_content_is_parsed_too(self):
        response = MagicMock(content='<S><M><C Id="1"/></M></S>', text='<S><M><C Id="1"/></M></S>')
        self.assertEqual(client_module._parse_reply(response), {"S": {"M": {"C": {"@Id": "1"}}}})


class TestReplyElement(unittest.TestCase):
    def test_missing_service_or_element_is_none(self):
        self.assertIsNone(client_module._reply_element({"Other": {"M": {}}}, "S", ("M",)))
        self.assertIsNone(client_module._reply_element({"S": {"Other": {}}}, "S", ("M",)))
        self.assertIsNone(client_module._reply_element({"S": None}, "S", ("M",)))
        self.assertIsNone(client_module._reply_element(None, "S", ("M",)))

    def test_empty_element_is_an_empty_mapping(self):
        element = client_module._reply_element({"S": {"M": None}}, "S", ("M",))
        self.assertIsNotNone(element)
        self.assertEqual(element, {})

    def test_repeated_or_text_element_is_none(self):
        self.assertIsNone(client_module._reply_element({"S": {"M": [{}, {}]}}, "S", ("M",)))
        self.assertIsNone(client_module._reply_element({"S": {"M": "text"}}, "S", ("M",)))

    def test_alternate_element_name(self):
        parsed = client_module._parse_reply(reply(b"<ItemService><ActivateItem/></ItemService>"))
        element = client_module._reply_element(parsed, "ItemService", client_module.ACTIVATE_ITEM_REPLY_ELEMENTS)
        self.assertEqual(element, {})

    def test_element_mapping_is_returned(self):
        element = client_module._reply_element({"S": {"M": {"@A": "1"}}}, "S", ("M",))
        self.assertEqual(element, {"@A": "1"})


class TestAddTraining(EndpointCase):
    def test_request_shape(self):
        self.serve(reply(b"<TrainingService><AddTraining/></TrainingService>"))
        self.client.addTraining("31", "501")
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sent(), (
            (BASE.scheme, BASE.netloc), "/TrainingService/AddTraining",
            [("trainingDesignId", "31"), ("characterId", "501"), ("trainingStartDate", NOW_TEXT),
             ("accessToken", TOKEN)],
            "POST"))

    def test_success_reply_is_true(self):
        self.serve(reply(b'<TrainingService><AddTraining><Character CharacterId="501"/></AddTraining></TrainingService>'))
        self.assertIs(self.client.addTraining("31", "501"), True)

    def test_empty_element_is_true(self):
        self.serve(reply(b"<TrainingService><AddTraining/></TrainingService>"))
        self.assertIs(self.client.addTraining("31", "501"), True)

    def test_failures_are_false(self):
        failures = dict(FAILURE_BODIES)
        failures.update({
            "element error": lambda: reply(b'<TrainingService><AddTraining errorMessage="busy"/></TrainingService>'),
            "root error": lambda: reply(b'<TrainingService errorMessage="busy"><AddTraining/></TrainingService>'),
            "empty service": lambda: reply(b"<TrainingService/>"),
            "other service": lambda: reply(b"<OtherService><AddTraining/></OtherService>"),
        })
        for name, make in failures.items():
            with self.subTest(name):
                self.serve(make())
                self.assertIs(self.client.addTraining("31", "501"), False)

    def test_refuses_invalid_ids_without_a_request(self):
        request = self.refuse_requests()
        for ids in ((None, "501"), ("31", None), ("0", "501"), ("31", "-1"), ("x", "501"), (True, "501"), ("31", 1.0)):
            with self.subTest(ids=ids):
                self.assertIs(self.client.addTraining(*ids), False)
        request.assert_not_called()

    def test_a_transport_error_propagates(self):
        self.request = MagicMock(side_effect=requests.ConnectionError("down"))
        with patch.object(self.client, "request", self.request), self.assertRaises(requests.ConnectionError):
            self.client.addTraining("31", "501")
        self.assertEqual(self.request.call_count, 1)

    def test_accepts_int_ids(self):
        self.serve(reply(b"<TrainingService><AddTraining/></TrainingService>"))
        self.assertIs(self.client.addTraining(31, 501), True)
        self.assertEqual(self.sent()[2][:2], [("trainingDesignId", "31"), ("characterId", "501")])


FINISHED = (b'<TrainingService><FinishTraining><Character CharacterId="501" HpImprovement="3" Fatigue="7"/>'
            b"</FinishTraining></TrainingService>")


def error_variants(service, element):
    """Failure replies shared by the mutating wrappers: transport failures, errors, wrong shapes."""
    failures = dict(FAILURE_BODIES)
    failures.update({
        "element error": lambda: reply(f'<{service}><{element} errorMessage="no"/></{service}>'),
        "root error": lambda: reply(f'<{service} errorMessage="no"><{element}/></{service}>'),
        "empty service": lambda: reply(f"<{service}/>"),
        "other service": lambda: reply(f"<OtherService><{element}/></OtherService>"),
    })
    return failures


PRESTIGE_REPLY = (b'<CharacterService><PrestigeCharacterTo><Prestiges><Prestige CharacterDesignId1="30001" '
                  b'CharacterDesignId2="30001" ToCharacterDesignId="30002"/></Prestiges></PrestigeCharacterTo>'
                  b"</CharacterService>")


class TestPrestigeCharacterTo(EndpointCase):
    # Verifies: S14-R3
    def test_request_shape(self):
        self.serve(reply(PRESTIGE_REPLY))
        self.client.prestigeCharacterTo("30002")
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sent(), ((BASE.scheme, BASE.netloc), "/CharacterService/PrestigeCharacterTo",
                                       [("characterDesignId", "30002"), ("languageKey", "en")], "GET"))
        self.assertNotIn("accessToken", self.calls[0][0])
        self.assertNotIn(TOKEN, self.calls[0][0])

    def test_success_returns_the_parsed_reply(self):
        from sdk.training.model import PrestigeRecipe
        from sdk.training.parse import parse_prestige_recipes

        self.serve(reply(PRESTIGE_REPLY))
        result = self.client.prestigeCharacterTo("30002")
        self.assertEqual(result, xmltodict.parse(PRESTIGE_REPLY, xml_attribs=True))
        self.assertEqual(parse_prestige_recipes(result), (PrestigeRecipe(30001, 30001, 30002),))

    def test_empty_list_is_success(self):
        from sdk.training.parse import parse_prestige_recipes

        for body in (b"<CharacterService><PrestigeCharacterTo><Prestiges/></PrestigeCharacterTo></CharacterService>",
                     b"<CharacterService><PrestigeCharacterTo/></CharacterService>"):
            with self.subTest(body=body):
                self.serve(reply(body))
                result = self.client.prestigeCharacterTo("30002")
                self.assertIsNotNone(result)
                self.assertEqual(parse_prestige_recipes(result), ())

    def test_failures_are_none(self):
        for name, make in error_variants("CharacterService", "PrestigeCharacterTo").items():
            with self.subTest(name):
                self.serve(make())
                self.assertIsNone(self.client.prestigeCharacterTo("30002"))

    def test_refuses_invalid_ids_without_a_request(self):
        request = self.refuse_requests()
        for value in (None, "0", "-1", "x", True, 1.5, 0):
            with self.subTest(value=value):
                self.assertIsNone(self.client.prestigeCharacterTo(value))
        request.assert_not_called()

    def test_a_transport_error_propagates(self):
        self.request = MagicMock(side_effect=requests.ConnectionError("down"))
        with patch.object(self.client, "request", self.request), self.assertRaises(requests.ConnectionError):
            self.client.prestigeCharacterTo("30002")
        self.assertEqual(self.request.call_count, 1)

    def test_accepts_int_id(self):
        self.serve(reply(PRESTIGE_REPLY))
        self.assertIsNotNone(self.client.prestigeCharacterTo(30002))
        self.assertEqual(self.sent()[2][0], ("characterDesignId", "30002"))

    def test_fake_server_sees_no_token(self):
        from run import CLIENT_CHECKSUM_SETTINGS
        from sdk.training.model import PrestigeRecipe
        from sdk.training.parse import parse_prestige_recipes

        world = default_world()
        world.prestige_recipes = [("502", "502", "501"), ("503", "504", "505")]
        fake = FakePss(world)
        with tempfile.TemporaryDirectory(prefix="tachikoma-prestige-") as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)  # Device() reads and writes ./.device
            self.addCleanup(os.chdir, cwd)
            with self.assertLogs(level="INFO"), fake.install():
                client = Client(Device(language="en"), {"allow_email_password_login": True,
                                                        **CLIENT_CHECKSUM_SETTINGS})
                self.assertTrue(client.login(SYNTHETIC_EMAIL, FAKE_PASSWORD))
                result = client.prestigeCharacterTo("501")
            os.chdir(cwd)
        self.assertEqual((fake.violations, fake.unknown), ([], []))
        self.assertEqual(parse_prestige_recipes(result), (PrestigeRecipe(502, 502, 501),))
        self.assertEqual(fake.mutations(), [])


class TestFinishTraining(EndpointCase):
    def test_request_shape(self):
        self.serve(reply(FINISHED))
        self.client.finishTraining("501")
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sent(), (
            (BASE.scheme, BASE.netloc), "/TrainingService/FinishTraining",
            [("characterId", "501"), ("accessToken", TOKEN)], "POST"))

    def test_returns_the_finished_character(self):
        self.serve(reply(FINISHED))
        character = self.client.finishTraining("501")
        self.assertEqual(character, {"@CharacterId": "501", "@HpImprovement": "3", "@Fatigue": "7"})
        self.assertEqual(self.client.trainingFinish["TrainingService"]["FinishTraining"]["Character"], character)

    def test_reply_without_a_character_is_none(self):
        for body in (b"<TrainingService><FinishTraining/></TrainingService>",
                     b"<TrainingService><FinishTraining><Character/></FinishTraining></TrainingService>",
                     b"<TrainingService><FinishTraining><Character>501</Character></FinishTraining></TrainingService>",
                     b'<TrainingService><FinishTraining><Character CharacterId="501"/><Character CharacterId="502"/>'
                     b"</FinishTraining></TrainingService>"):
            with self.subTest(body=body):
                self.serve(reply(body))
                self.assertIsNone(self.client.finishTraining("501"))
                self.assertIsNone(self.client.trainingFinish)

    def test_failures_are_none(self):
        for name, make in error_variants("TrainingService", "FinishTraining").items():
            with self.subTest(name):
                self.serve(make())
                self.assertIsNone(self.client.finishTraining("501"))
                self.assertIsNone(self.client.trainingFinish)

    def test_failure_clears_an_earlier_reply(self):
        self.serve(reply(FINISHED), http_error())
        self.assertIsNotNone(self.client.finishTraining("501"))
        self.assertIsNone(self.client.finishTraining("501"))
        self.assertIsNone(self.client.trainingFinish)

    def test_a_transport_error_propagates_with_the_earlier_reply_cleared(self):
        self.client.trainingFinish = {"TrainingService": {"FinishTraining": {"Character": {"@CharacterId": "501"}}}}
        self.request = MagicMock(side_effect=requests.ConnectionError("down"))
        with patch.object(self.client, "request", self.request), self.assertRaises(requests.ConnectionError):
            self.client.finishTraining("501")
        self.assertIsNone(self.client.trainingFinish)

    def test_refuses_invalid_id_without_a_request(self):
        request = self.refuse_requests()
        self.client.trainingFinish = "stale"
        for value in (None, "", "0", "abc", -5):
            with self.subTest(value=value):
                self.assertIsNone(self.client.finishTraining(value))
                self.assertIsNone(self.client.trainingFinish)
        request.assert_not_called()


class TestUpgradeCharacter(EndpointCase):
    def test_request_shape(self):
        self.serve(reply(b"<CharacterService><UpgradeCharacter/></CharacterService>"))
        self.client.upgradeCharacter("501")
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sent(), (
            (BASE.scheme, BASE.netloc), "/CharacterService/UpgradeCharacter",
            [("characterId", "501"), ("accessToken", TOKEN)], "POST"))

    def test_success_is_true(self):
        for body in (b"<CharacterService><UpgradeCharacter/></CharacterService>",
                     b'<CharacterService><UpgradeCharacter><Character CharacterId="501" Level="2"/>'
                     b"</UpgradeCharacter></CharacterService>"):
            with self.subTest(body=body):
                self.serve(reply(body))
                self.assertIs(self.client.upgradeCharacter("501"), True)

    def test_failures_are_false(self):
        for name, make in error_variants("CharacterService", "UpgradeCharacter").items():
            with self.subTest(name):
                self.serve(make())
                self.assertIs(self.client.upgradeCharacter("501"), False)

    def test_refuses_invalid_id_without_a_request(self):
        request = self.refuse_requests()
        for value in (None, "", "0", "1.5", False):
            with self.subTest(value=value):
                self.assertIs(self.client.upgradeCharacter(value), False)
        request.assert_not_called()


ACTIVATED = b'<ItemService><ActivateItem3><Item ItemId="5" Quantity="0"/></ActivateItem3></ItemService>'
SHIP_ITEMS = {"ItemService": {"ListItemsOfAShip": {"Items": {"Item": [
    {"@ItemId": "4", "@ItemDesignId": "76"},
    {"@ItemId": "5", "@ItemDesignId": "77"},
]}}}}


class TestActivateItem3(EndpointCase):
    def test_sends_the_access_token(self):
        self.serve(reply(ACTIVATED))
        self.client.activateItem3(itemId="5", targetId=42)
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sent(), (
            (BASE.scheme, BASE.netloc), "/ItemService/ActivateItem3",
            [("itemId", "5"), ("targetId", "42"), ("accessToken", TOKEN)], "POST"))

    def test_success_keeps_the_reply(self):
        for body in (ACTIVATED, b"<ItemService><ActivateItem3/></ItemService>", b"<ItemService><ActivateItem/></ItemService>"):
            with self.subTest(body=body):
                self.serve(reply(body))
                self.assertIs(self.client.activateItem3(itemId="5", targetId="42"), True)
                self.assertIn("ItemService", self.client.item)

    def test_failures_clear_an_earlier_reply(self):
        for name, make in error_variants("ItemService", "ActivateItem3").items():
            with self.subTest(name):
                self.client.item = {"ItemService": {"ActivateItem3": {}}}
                self.serve(make())
                self.assertIs(self.client.activateItem3(itemId="5", targetId="42"), False)
                self.assertIsNone(self.client.item)

    def test_refuses_invalid_ids_without_a_request(self):
        request = self.refuse_requests()
        for ids in ((None, "42"), ("5", None), ("0", "42"), ("5", "x"), (0, 0)):
            with self.subTest(ids=ids):
                self.assertIs(self.client.activateItem3(itemId=ids[0], targetId=ids[1]), False)
        request.assert_not_called()


class TestUseConsumable(EndpointCase):
    def test_activates_the_matching_item_on_the_character(self):
        self.client.itemsOfAShip = SHIP_ITEMS
        self.serve(reply(ACTIVATED))
        self.assertIs(self.client.useConsumable(77, 42), True)
        self.assertEqual(self.request.call_count, 1)
        _, path, params, method = self.sent()
        self.assertEqual((path, method), ("/ItemService/ActivateItem3", "POST"))
        self.assertEqual(params, [("itemId", "5"), ("targetId", "42"), ("accessToken", TOKEN)])

    def test_single_item_shape(self):
        self.client.itemsOfAShip = {"ItemService": {"ListItemsOfAShip": {"Items": {"Item": {
            "@ItemId": "9", "@ItemDesignId": "77"}}}}}
        self.serve(reply(ACTIVATED))
        self.assertIs(self.client.useConsumable(77, 42), True)
        self.assertEqual(self.sent()[2][0], ("itemId", "9"))

    def test_error_reply_is_false(self):
        self.client.itemsOfAShip = SHIP_ITEMS
        self.serve(reply(b'<ItemService><ActivateItem3 errorMessage="Item not found"/></ItemService>'))
        self.assertIs(self.client.useConsumable(77, 42), False)

    def test_empty_reply_is_false_without_raising(self):
        self.client.itemsOfAShip = SHIP_ITEMS
        self.serve(reply(b"<ItemService/>"))
        self.assertIs(self.client.useConsumable(77, 42), False)

    def test_an_earlier_success_does_not_decide(self):
        self.client.itemsOfAShip = SHIP_ITEMS
        self.client.item = client_module._parse_reply(reply(ACTIVATED))
        self.serve(http_error())
        self.assertIs(self.client.useConsumable(77, 42), False)

    def test_missing_design_sends_nothing(self):
        self.client.itemsOfAShip = SHIP_ITEMS
        request = self.refuse_requests()
        self.assertIs(self.client.useConsumable(99, 42), False)
        request.assert_not_called()


CREW_TWO = (b'<CharacterService><ListAllCharactersOfUser><Characters><Character CharacterId="501"/>'
            b'<Character CharacterId="502"/></Characters></ListAllCharactersOfUser></CharacterService>')
CREW_ONE = (b'<CharacterService><ListAllCharactersOfUser><Characters><Character CharacterId="501"/>'
            b"</Characters></ListAllCharactersOfUser></CharacterService>")


class TestListAllCharactersOfUser(EndpointCase):
    def test_request_shape(self):
        self.serve(reply(CREW_ONE))
        self.client.listAllCharactersOfUser()
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sent(), (
            (BASE.scheme, BASE.netloc), "/CharacterService/ListAllCharactersOfUser",
            [("accessToken", TOKEN), ("clientDateTime", NOW_TEXT)], "GET"))

    def test_crew_list_or_single_character_is_true(self):
        for body, size in ((CREW_TWO, 2), (CREW_ONE, 1)):
            with self.subTest(size=size):
                self.serve(reply(body))
                self.assertIs(self.client.listAllCharactersOfUser(), True)
                self.assertEqual(len(_extract_collection(self.client.allCharactersOfUser, "Character")), size)

    def test_empty_crew_is_true(self):
        self.serve(reply(b"<CharacterService><ListAllCharactersOfUser><Characters/></ListAllCharactersOfUser>"
                         b"</CharacterService>"))
        self.assertIs(self.client.listAllCharactersOfUser(), True)
        self.assertEqual(self.client.allCharactersOfUser,
                         {"CharacterService": {"ListAllCharactersOfUser": {"Characters": None}}})

    def test_failures_are_false_and_keep_nothing(self):
        failures = dict(FAILURE_BODIES)
        failures.update({
            "root error": lambda: reply(b'<CharacterService errorMessage="no"/>'),
            "element error": lambda: reply(
                b'<CharacterService><ListAllCharactersOfUser errorMessage="no"/></CharacterService>'),
            "empty service": lambda: reply(b"<CharacterService/>"),
            "no service root": lambda: reply(b"<ListAllCharactersOfUser/>"),
            "no characters": lambda: reply(b"<CharacterService><ListAllCharactersOfUser/></CharacterService>"),
        })
        for name, make in failures.items():
            with self.subTest(name):
                self.serve(make())
                self.assertIs(self.client.listAllCharactersOfUser(), False)
                self.assertIsNone(self.client.allCharactersOfUser)

    def test_failure_clears_an_earlier_crew(self):
        self.serve(reply(CREW_TWO), http_error())
        self.assertIs(self.client.listAllCharactersOfUser(), True)
        self.assertIs(self.client.listAllCharactersOfUser(), False)
        self.assertIsNone(self.client.allCharactersOfUser)


def researches(*records):
    """A ListAllResearches payload: per-account records carry only ids and state."""
    return {"ResearchService": {"ListAllResearches": {"Researches": {"Research": [
        {"@ResearchId": research_id, "@ResearchDesignId": design_id, "@ResearchState": state}
        for research_id, design_id, state in records]}}}}


def research_designs(*designs):
    """A ListAllResearchDesigns2 payload."""
    return {"ResearchService": {"ListAllResearchDesigns": {"ResearchDesigns": {"ResearchDesign": [
        {"@ResearchDesignId": design_id, "@ResearchName": name, "@RootResearchDesignId": "0"}
        for design_id, name in designs]}}}}


class TestResearchLevelOfLoadedLists(unittest.TestCase):
    """getResearchLevel on lists already loaded: the join, the line and the level all come from sdk.training.parse."""

    def level(self, records, designs, line):
        client = Client.__new__(Client)
        client.allResearches, client.allResearchDesigns = records, designs
        client.listAllResearches = client.listAllResearchDesigns2 = MagicMock(side_effect=AssertionError("fetched"))
        return client.getResearchLevel(line)

    def test_joins_on_the_research_design_id(self):
        self.assertEqual(self.level(researches(("1", "900", "Completed")), research_designs(("900", "Fitness 201")),
                                    "Fitness"), 201)

    def test_only_completed_research_counts(self):
        designs = research_designs(("900", "Fitness 201"))
        for state in ("Researching", "", "None", "Cancelled", "completed"):
            with self.subTest(state=state):
                self.assertEqual(self.level(researches(("1", "900", state)), designs, "Fitness"), 0)

    def test_a_record_naming_only_a_root_id_counts_nothing(self):
        designs = research_designs(("9", "Fitness 203"))
        designs["ResearchService"]["ListAllResearchDesigns"]["ResearchDesigns"]["ResearchDesign"][0][
            "@RootResearchDesignId"] = "5"
        self.assertEqual(self.level(researches(("1", "5", "Completed")), designs, "Fitness"), 0)

    def test_join_uses_the_design_id_not_the_research_id(self):
        designs = research_designs(("7", "Fitness 203"), ("9", "Fitness 101"))
        self.assertEqual(self.level(researches(("7", "9", "Completed")), designs, "Fitness"), 101)

    def test_highest_course_of_the_line_wins(self):
        designs = research_designs(("1", "Fitness 203"), ("2", "Fitness 101"), ("3", "Fitness 201"))
        records = researches(("a", "1", "Completed"), ("b", "2", "Completed"), ("c", "3", "Completed"))
        self.assertEqual(self.level(records, designs, "Fitness"), 203)

    def test_other_lines_sharing_a_prefix_do_not_count(self):
        designs = research_designs(("1", "Education 203"), ("2", "Fitness Center 301"))
        records = researches(("a", "1", "Completed"), ("b", "2", "Completed"))
        self.assertEqual(self.level(records, designs, "Fitness"), 0)
        self.assertEqual(self.level(records, designs, "Education"), 203)
        self.assertEqual(self.level(records, designs, "Fitness Center"), 301)

    def test_a_course_number_in_the_line_is_ignored(self):
        """Documented behaviour: callers pass the line; a course in it does not cap or filter the result."""
        records, designs = researches(("1", "900", "Completed")), research_designs(("900", "Fitness 201"))
        for line in ("Fitness 101", "Fitness 999", " Fitness 101 ", "  Fitness\t"):
            with self.subTest(line=line):
                self.assertEqual(self.level(records, designs, line), 201)

    def test_unnumbered_and_lv_names_follow_the_shared_contract(self):
        # Regression: the client's own parser gave 0 for "Advanced Training" (an unnumbered first level) and for
        # "Advanced Training Lv2", while crew training's parser read 1 and 2. One contract now serves both.
        designs = research_designs(("38", "Advanced Training"), ("39", "Advanced Training Lv2"),
                                   ("5", "Fitness ٢٠١"))
        self.assertEqual(self.level(researches(("a", "38", "Completed")), designs, "Advanced Training"), 1)
        records = researches(("a", "38", "Completed"), ("b", "39", "Completed"), ("c", "5", "Completed"))
        self.assertEqual(self.level(records, designs, "Advanced Training"), 2)
        self.assertEqual(self.level(records, designs, "Fitness"), 0)  # non-ASCII digits are part of the name

    def test_a_line_query_is_never_answered_by_an_exact_design_name(self):
        # Review: "Night Classes" is a course of the Education line (its root), not a line of its own.
        designs = research_designs(("101", "Education 101"), ("107", "Night Classes"), ("108", "Night Classes 202"))
        for record in designs["ResearchService"]["ListAllResearchDesigns"]["ResearchDesigns"]["ResearchDesign"]:
            record["@RootResearchDesignId"] = "101"
        records = researches(("a", "107", "Completed"), ("b", "108", "Completed"))
        self.assertEqual(self.level(records, designs, "Night Classes"), 0)
        self.assertEqual(self.level(records, designs, "Education"), 202)

    def test_designs_missing_a_name_or_id_contribute_nothing(self):
        designs = {"ResearchService": {"ListAllResearchDesigns": {"ResearchDesigns": {"ResearchDesign": [
            {"@ResearchDesignId": "1"},
            {"@ResearchDesignId": "2", "@ResearchName": ""},
            {"@ResearchName": "Fitness 301"},
            {"@ResearchDesignId": "4", "@ResearchName": "Education 101"},
        ]}}}}
        records = researches(("a", "1", "Completed"), ("b", "2", "Completed"), ("c", "4", "Completed"))
        self.assertEqual(self.level(records, designs, "Fitness"), 0)
        self.assertEqual(self.level(records, designs, "Education"), 101)

    def test_records_missing_a_design_id_contribute_nothing(self):
        records = {"ResearchService": {"ListAllResearches": {"Researches": {"Research": [
            {"@ResearchId": "1", "@ResearchState": "Completed"}]}}}}
        self.assertEqual(self.level(records, research_designs(("900", "Fitness 201")), "Fitness"), 0)

    def test_a_single_record_and_a_single_design_as_plain_mappings(self):
        records = {"ResearchService": {"ListAllResearches": {"Researches": {"Research": {
            "@ResearchId": "1", "@ResearchDesignId": "900", "@ResearchState": "Completed"}}}}}
        designs = {"ResearchService": {"ListAllResearchDesigns": {"ResearchDesigns": {"ResearchDesign": {
            "@ResearchDesignId": "900", "@ResearchName": "Fitness 201"}}}}}
        self.assertEqual(self.level(records, designs, "Fitness"), 201)

    def test_missing_or_error_payloads_are_zero(self):
        records, designs = researches(("1", "900", "Completed")), research_designs(("900", "Fitness 201"))
        cases = {
            "nothing loaded": (None, None),
            "designs missing": (records, None),
            "records missing": (None, designs),
            "error payloads": ({"ResearchService": {"ListAllResearches": {"@errorMessage": "no"}}},
                               {"ResearchService": {"@errorMessage": "no"}}),
        }
        for name, (given_records, given_designs) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.level(given_records, given_designs, "Fitness"), 0)


LIVE_RESEARCH_DESIGNS = Path(__file__).parent / "fixtures" / "training" / "live_research_designs.xml"


class TestClientAndTrainingParserAgreeOnLiveData(unittest.TestCase):
    """getResearchLevel and sdk.training.parse.parse_research_levels on the live reply (design version 717)."""

    LINES = ("Fitness", "Education", "Advanced Training")

    def test_every_completion_prefix_of_every_line_agrees(self):
        designs = xmltodict.parse(LIVE_RESEARCH_DESIGNS.read_text(encoding="utf-8"), xml_attribs=True)
        chains = {"Fitness": ["74", "75", "76", "77", "78", "79"],
                  "Education": ["101", "102", "103", "104", "105", "106"],
                  "Advanced Training": ["38", "39", "40", "41", "42", "43", "48"]}
        expected_top = {"Fitness": 203, "Education": 203, "Advanced Training": 7}
        for line, chain in chains.items():
            for count in range(len(chain) + 1):
                records = researches(*((f"r{i}", design_id, "Completed") for i, design_id in enumerate(chain[:count])))
                with self.subTest(line=line, completed=count):
                    client = Client.__new__(Client)
                    client.allResearches, client.allResearchDesigns = records, designs
                    parsed = parse_research_levels(records, designs)
                    for name in self.LINES:
                        self.assertEqual(client.getResearchLevel(name), parsed.get(name, 0))
                    if count == len(chain):
                        self.assertEqual(client.getResearchLevel(line), expected_top[line])
                    if count == 0:
                        self.assertEqual(client.getResearchLevel(line), 0)


RESEARCH_DESIGNS_XML = (
    b'<ResearchService><ListAllResearchDesigns><ResearchDesigns>'
    b'<ResearchDesign ResearchDesignId="7" ResearchName="Fitness 101" RootResearchDesignId="7"/>'
    b'<ResearchDesign ResearchDesignId="9" ResearchName="Fitness 201" RootResearchDesignId="7"/>'
    b'<ResearchDesign ResearchDesignId="11" ResearchName="Education 202" RootResearchDesignId="10"/>'
    b'</ResearchDesigns></ListAllResearchDesigns></ResearchService>')
RESEARCHES_XML = (
    b'<ResearchService><ListAllResearches><Researches>'
    b'<Research ResearchId="7001" ResearchDesignId="7" ResearchState="Completed"/>'
    b'<Research ResearchId="7002" ResearchDesignId="9" ResearchState="Completed"/>'
    b'<Research ResearchId="7003" ResearchDesignId="11" ResearchState="Researching"/>'
    b'</Researches></ListAllResearches></ResearchService>')


class TestResearchLevel(EndpointCase):
    """Client-level research: the fetch-once logic and the real fetchers behind getResearchLevel/hasResearch."""

    def given(self, records, designs):
        """Install fetchers that load ``records``/``designs`` when the lists are first needed."""
        def load_researches():
            self.client.allResearches = records

        def load_designs():
            self.client.allResearchDesigns = designs
            return True

        self.client.listAllResearches = MagicMock(side_effect=load_researches)
        self.client.listAllResearchDesigns2 = MagicMock(side_effect=load_designs)

    def test_real_fetchers_and_join_through_the_request_seam(self):
        self.client.latestVersion = {"SettingService": {"GetLatestSetting": {"Setting": {
            "@ResearchDesignVersion": "42"}}}}
        bodies = {"/ResearchService/ListAllResearches": RESEARCHES_XML,
                  "/ResearchService/ListAllResearchDesigns2": RESEARCH_DESIGNS_XML}

        def fake(url, method, data=None):
            self.calls.append((url, method))
            return reply(bodies[urlsplit(url).path])

        with patch.object(self.client, "request", side_effect=fake):
            self.assertEqual(self.client.getResearchLevel("Fitness"), 201)
            self.assertEqual(self.client.getResearchLevel("Education"), 0)
            self.assertIs(self.client.hasResearch("Fitness", 201), True)
        self.assertEqual(sorted(urlsplit(url).path for url, _ in self.calls), sorted(bodies))
        self.assertTrue(all(method == "GET" for _, method in self.calls))
        designs_query = dict(parse_qsl(urlsplit(self.calls[1][0]).query))
        self.assertEqual(designs_query["designVersion"], "42")

    def test_has_research_threshold(self):
        self.given(researches(("1", "900", "Completed")), research_designs(("900", "Fitness 201")))
        self.assertIs(self.client.hasResearch("Fitness", 201), True)
        self.assertIs(self.client.hasResearch("Fitness", 101), True)
        self.assertIs(self.client.hasResearch("Fitness", 202), False)
        self.assertIs(self.client.hasResearch("Education", 101), False)

    def test_lists_are_fetched_once_when_absent(self):
        self.given(researches(("1", "900", "Completed")), research_designs(("900", "Fitness 201")))
        answers = [(self.client.hasResearch("Fitness", minimum), self.client.hasResearch("Education", minimum))
                   for minimum in (101, 201, 202, 203)]
        # The cached lists keep answering correctly, not just quietly: Fitness 201 is done, Education never started.
        self.assertEqual(answers, [(True, False), (True, False), (False, False), (False, False)])
        self.assertEqual(self.client.listAllResearches.call_count, 1)
        self.assertEqual(self.client.listAllResearchDesigns2.call_count, 1)

    def test_a_failed_fetch_is_not_retried(self):
        self.client.listAllResearches = MagicMock()
        self.client.listAllResearchDesigns2 = MagicMock(return_value=False)
        self.assertEqual(self.client.getResearchLevel("Fitness"), 0)
        self.assertEqual(self.client.getResearchLevel("Fitness"), 0)
        self.assertEqual(self.client.listAllResearches.call_count, 1)
        self.assertEqual(self.client.listAllResearchDesigns2.call_count, 1)

    def test_a_failed_fetch_leaves_no_placeholder_for_other_readers(self):
        """A failed fetch leaves the attribute absent, not None; the research boost does not read designs at all."""
        self.client.listAllResearches = MagicMock()
        self.client.listAllResearchDesigns2 = MagicMock(return_value=False)
        self.assertEqual(self.client.getResearchLevel("Fitness"), 0)
        self.assertFalse(hasattr(self.client, "allResearches"))
        self.assertFalse(hasattr(self.client, "allResearchDesigns"))
        self.client.request = MagicMock(return_value=None)
        self.assertIs(self.client.speedUpResearchUsingBoostGauge("1"), False)
        self.assertEqual(self.client.listAllResearchDesigns2.call_count, 1)

    def test_lists_already_loaded_are_used_without_fetching(self):
        self.client.allResearches = researches(("1", "900", "Completed"))
        self.client.allResearchDesigns = research_designs(("900", "Education 202"))
        self.client.listAllResearches = MagicMock()
        self.client.listAllResearchDesigns2 = MagicMock()
        self.assertEqual(self.client.getResearchLevel("Education"), 202)
        self.client.listAllResearches.assert_not_called()
        self.client.listAllResearchDesigns2.assert_not_called()

    def test_unparseable_research_reply_does_not_raise(self):
        self.client.listAllResearchDesigns2 = MagicMock(return_value=False)
        self.serve(reply(b"junk"))
        self.assertEqual(self.client.getResearchLevel("Fitness"), 0)
        self.assertEqual(self.sent()[1], "/ResearchService/ListAllResearches")


class TestManageTrainingAgainstFakePss(unittest.TestCase):
    """Client.manageTraining through the real Client against FakePss: it runs sdk.execution.training.run_training.

    Its own reads (crew, ship, character, training, room and research designs) replace the hasResearch and
    getResearchLevel helpers and the cached attributes the old method reused.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tachikoma-training-")
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)  # Device() reads and writes ./.device
        self.addCleanup(os.chdir, cwd)

    def manage_training(self, fake, prepare=lambda client: None):
        """Log in, run ``prepare(client)``, then manageTraining; (result, log text, client)."""
        from run import CLIENT_CHECKSUM_SETTINGS

        with self.assertLogs(level="INFO") as logs, fake.install():
            client = Client(Device(language="en"), {"allow_email_password_login": True, **CLIENT_CHECKSUM_SETTINGS})
            self.assertTrue(client.login(SYNTHETIC_EMAIL, FAKE_PASSWORD))
            prepare(client)
            result = client.manageTraining()
        return result, "\n".join(logs.output), client

    @staticmethod
    def training_posts(fake):
        return [(path.rsplit("/", 1)[1], params.get("characterId"))
                for _, path, params in fake.mutations() if path.startswith("/TrainingService/")]

    def test_returns_true_finishes_pending_sessions_and_starts_gated_tiers(self):
        # The default world has completed Fitness 101-201 (live names), so the research-gated tiers start too.
        fake = FakePss(default_world())
        result, log, _ = self.manage_training(fake)
        self.assertIs(result, True)
        self.assertEqual(self.training_posts(fake), [
            ("FinishTraining", "1001"), ("AddTraining", "1001"), ("FinishTraining", "1003"), ("AddTraining", "1003"),
            ("AddTraining", "1004"), ("AddTraining", "1005"), ("AddTraining", "1008"), ("FinishTraining", "1016"),
            ("AddTraining", "1016")])
        crew = fake.state.character("1001")
        self.assertEqual((crew.stats["HpImprovement"], crew.fatigue), (18, 10))  # the finished Bench Press
        self.assertEqual((crew.last_session.design_id, crew.last_session.applied), ("4", False))  # S-T2 started
        self.assertIn("INFO:sdk.execution.training:[SyntheticCaptain] Completed training for Galactic Succubus in "
                      "Galaxy Gym with Hp +2", log)
        self.assertIn("INFO:sdk.execution.training:[SyntheticCaptain] training: started=6 finished=3 consumables=0 "
                      "skipped=11 failed=0", log)
        self.assertEqual((fake.violations, fake.unknown), ([], []))

    def test_without_account_info_the_log_lines_carry_no_label(self):
        fake = FakePss(default_world())
        result, log, _ = self.manage_training(fake, lambda client: setattr(client, "info", None))
        self.assertIs(result, True)
        self.assertIn("INFO:sdk.execution.training:training: started=6 finished=3 consumables=0 skipped=11 failed=0",
                      log)
        self.assertNotIn("INFO:sdk.execution.training:[", log)

    def test_returns_false_when_characters_unreadable(self):
        fake = FakePss(default_world())
        fake.fail("/CharacterService/ListAllCharactersOfUser", status=404)
        result, log, _ = self.manage_training(fake)
        self.assertIs(result, False)
        self.assertEqual(self.training_posts(fake), [])
        self.assertIn("ERROR:sdk.execution.training:[SyntheticCaptain] training: cannot read characters; no training "
                      "this run", log)

    def test_consumable_used_with_access_token_when_item_designs_loaded(self):
        # D9 and D12: with item designs loaded the grey weapon consumable is used, through ActivateItem3 with the
        # account's accessToken. One crew member (a single CharacterDesign record, D5) at 20% in the Academy without
        # research: the primary T1 gate is unmet and fatigue is 0.
        world = minimal_world()
        world.add_crew("Delish", room="Academy", stats=stats(Weapon=20))
        world.items = [Item("9101", "101", 5)]
        fake = FakePss(world)

        def load_item_designs(client):
            client.itemDesigns = {"DesignService": {"ListAllDesigns": {"ItemDesigns": {"ItemDesign": [
                {"@ItemDesignId": "101", "@ItemName": "Common Weapon Consumable", "@ItemSubType": "Consumable"}]}}}}

        result, log, _ = self.manage_training(fake, load_item_designs)
        self.assertIs(result, True)
        activations = [params for _, path, params in fake.mutations() if path == "/ItemService/ActivateItem3"]
        self.assertEqual(activations, [{"itemId": "9101", "targetId": "1001", "accessToken": "<token>"}])
        self.assertEqual(fake.state.item("9101").quantity, 4)
        self.assertIn("training: started=0 finished=0 consumables=1 skipped=0 failed=0", log)

    def test_class_attrs_default(self):
        self.assertIsNone(Client.training_journal)
        self.assertEqual(Client.training_account_alias, "legacy")

    def test_client_journal_receives_decision_records(self):
        fake = FakePss(default_world())
        journal = MemoryJournal()

        def use_journal(client):
            client.training_journal = journal
            client.training_account_alias = "primary"

        self.manage_training(fake, use_journal)
        events = journal.read()
        decisions = [e for e in events if e.action_id == "training_decision"]
        self.assertEqual([e.evidence["character_id"] for e in decisions], [str(cid) for cid in range(1001, 1018)])
        self.assertEqual({e.account_alias for e in events}, {"primary"})
        finish, add = ("finish_training", "verified"), ("add_training", "verified")
        self.assertEqual([(e.action_id, e.outcome) for e in events if e.action_id != "training_decision"],
                         [finish, add, finish, add, add, add, add, finish, add])


class TestDeadWrappersAndInventory(unittest.TestCase):
    def test_dead_wrappers_are_removed(self):
        self.assertFalse(hasattr(Client, "getTrainingUpdate"))
        self.assertFalse(hasattr(Client, "getCharacterRooms"))

    def test_touched_wrappers_are_mocked_test_evidence(self):
        """Citations are checked by tests/test_capability_inventory.py; this pins the evidence level only."""
        root = Path(client_module.__file__).resolve().parent.parent
        inventory = json.loads((root / "docs" / "baseline" / "capability-inventory.json").read_text())
        entries = {entry["name"]: entry for entry in inventory["client_methods"]}
        for name in ("finishTraining", "addTraining", "upgradeCharacter", "activateItem3", "useConsumable",
                     "getResearchLevel", "hasResearch", "listAllCharactersOfUser", "manageTraining"):
            with self.subTest(name):
                self.assertEqual(entries[name]["evidence"], "mocked-test")


class CrewPlanWorkflowTests(unittest.TestCase):
    """The SPEC-14 workflow benchmark's preview steps, through the real run.main under FakePss (no network).

    Each preview case (crew plan and dry run only) must match its frozen trace, and every other case's dry-run and
    crew-plan steps must have no unmet assertion. The provision, execute and resolve steps belong to SPEC-14 step 4.
    """

    PREVIEW_CASES = ("role-coverage", "preview-prestige", "tie-break-instance-id", "observed-zero-stat",
                     "two-copy-recipe")

    @classmethod
    def setUpClass(cls):
        from scripts import training_workflow_benchmark as wb

        cls.wb = wb
        cls.suites = [wb.load_suite(wb.FIXTURES["required"]), wb.load_suite(wb.FIXTURES["holdout"])]

    def scenarios(self):
        for suite in self.suites:
            for item in suite["cases"]:
                for entry in item["scenarios"]:
                    yield suite, item["id"], entry

    def run_one(self, suite, case_id, entry):
        return self.wb.run_scenario(suite["suite"], case_id, entry, suite["catalog"], suite["seeds"][0],
                                    suite["vocabulary"]["skip_codes"])

    # Verifies: S14-R1, S14-R2, S14-R3
    def test_preview_cases_pass_their_frozen_traces(self):
        ran = 0
        for suite, case_id, entry in self.scenarios():
            if case_id not in self.PREVIEW_CASES:
                continue
            with self.subTest(case=case_id, scenario=entry["id"]):
                result = self.run_one(suite, case_id, entry)
                self.assertTrue(result.passed, json.dumps(result.to_dict(), indent=1))
                ran += 1
        self.assertEqual(ran, 7)

    # Verifies: S14-R1, S14-R2, S14-R4
    def test_dry_and_crew_plan_steps_of_every_other_case(self):
        preview_kinds = ("dry-run", "crew-plan")
        ran = 0
        for suite, case_id, entry in self.scenarios():
            if case_id in self.PREVIEW_CASES:
                continue
            steps = {step["id"] for step in entry["steps"] if step["kind"] in preview_kinds}
            if not steps:
                continue
            with self.subTest(case=case_id, scenario=entry["id"]):
                result = self.run_one(suite, case_id, entry)
                self.assertEqual([row for row in result.unmet if row["step"] in steps], [])
                self.assertEqual([text for text in result.hard_violations
                                  if any(text.startswith(step) for step in steps)], [])
                ran += 1
        self.assertGreater(ran, 20)


if __name__ == "__main__":
    unittest.main()
