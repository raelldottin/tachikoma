#!/usr/bin/env python3
"""Training, crew, consumable and research wrappers report success only on a parsed success reply.

Every request is mocked at ``Client.request``; nothing reaches Pixel Starships.
"""

import datetime
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, urlsplit

import requests

from sdk import client as client_module
from sdk.client import Client, DotNet, _extract_collection
from sdk.device import Device

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


def completed(records, designs):
    return client_module._completed_research_names(records, designs)


class TestCompletedResearchNames(unittest.TestCase):
    """The join of per-account research records onto research designs, on plain parsed payloads."""

    def test_joins_on_the_research_design_id(self):
        self.assertEqual(completed(researches(("1", "900", "Completed")), research_designs(("900", "Fitness 201"))),
                         ["Fitness 201"])

    def test_only_completed_research_counts(self):
        designs = research_designs(("900", "Fitness 201"))
        for state in ("Researching", "", "None", "Cancelled", "completed"):
            with self.subTest(state=state):
                self.assertEqual(completed(researches(("1", "900", state)), designs), [])

    def test_join_ignores_the_root_research_design_id(self):
        designs = research_designs(("9", "Fitness 203"))
        designs["ResearchService"]["ListAllResearchDesigns"]["ResearchDesigns"]["ResearchDesign"][0][
            "@RootResearchDesignId"] = "5"
        self.assertEqual(completed(researches(("1", "5", "Completed")), designs), [])

    def test_join_uses_the_design_id_not_the_research_id(self):
        designs = research_designs(("7", "Fitness 203"), ("9", "Fitness 101"))
        self.assertEqual(completed(researches(("7", "9", "Completed")), designs), ["Fitness 101"])

    def test_designs_missing_a_name_or_id_contribute_nothing(self):
        designs = {"ResearchService": {"ListAllResearchDesigns": {"ResearchDesigns": {"ResearchDesign": [
            {"@ResearchDesignId": "1"},
            {"@ResearchDesignId": "2", "@ResearchName": ""},
            {"@ResearchName": "Fitness 301"},
            {"@ResearchDesignId": "4", "@ResearchName": "Education 101"},
        ]}}}}
        records = researches(("a", "1", "Completed"), ("b", "2", "Completed"), ("c", "4", "Completed"))
        self.assertEqual(completed(records, designs), ["Education 101"])

    def test_records_missing_a_design_id_contribute_nothing(self):
        records = {"ResearchService": {"ListAllResearches": {"Researches": {"Research": [
            {"@ResearchId": "1", "@ResearchState": "Completed"}]}}}}
        self.assertEqual(completed(records, research_designs(("900", "Fitness 201"))), [])

    def test_a_single_record_and_a_single_design_as_plain_mappings(self):
        records = {"ResearchService": {"ListAllResearches": {"Researches": {"Research": {
            "@ResearchId": "1", "@ResearchDesignId": "900", "@ResearchState": "Completed"}}}}}
        designs = {"ResearchService": {"ListAllResearchDesigns": {"ResearchDesigns": {"ResearchDesign": {
            "@ResearchDesignId": "900", "@ResearchName": "Fitness 201"}}}}}
        self.assertEqual(completed(records, designs), ["Fitness 201"])

    def test_missing_or_error_payloads_are_empty(self):
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
                self.assertEqual(completed(given_records, given_designs), [])


class TestResearchLevelOfNames(unittest.TestCase):
    """The highest completed course of a research line, from completed research names."""

    def level(self, names, line):
        return client_module._research_level(names, line)

    def test_highest_course_of_the_line_wins(self):
        self.assertEqual(self.level(["Fitness 203", "Fitness 101", "Fitness 201"], "Fitness"), 203)

    def test_no_course_of_the_line_is_zero(self):
        self.assertEqual(self.level([], "Fitness"), 0)
        self.assertEqual(self.level(["Education 203"], "Fitness"), 0)

    def test_other_lines_sharing_a_prefix_do_not_count(self):
        names = ["Education 203", "Fitness Center 301"]
        self.assertEqual(self.level(names, "Fitness"), 0)
        self.assertEqual(self.level(names, "Education"), 203)
        self.assertEqual(self.level(names, "Fitness Center"), 301)

    def test_a_course_number_in_the_line_is_ignored(self):
        """Documented behaviour: callers pass the line; a course in it does not cap or filter the result."""
        self.assertEqual(self.level(["Fitness 201"], "Fitness 101"), 201)
        self.assertEqual(self.level(["Fitness 201"], "Fitness 999"), 201)

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(self.level([" Fitness 201 "], "  Fitness\t"), 201)
        self.assertEqual(self.level(["Fitness 201"], " Fitness 101 "), 201)

    def test_names_without_an_ascii_course_number_do_not_count(self):
        self.assertEqual(self.level(["Fitness", "Fitness two", "Fitness ٢٠١"], "Fitness"), 0)
        self.assertEqual(self.level(["Fitness 201"], "Fitness"), 0)


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
        for minimum in (101, 201, 202, 203):
            self.client.hasResearch("Fitness", minimum)
            self.client.hasResearch("Education", minimum)
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
        """speedUpResearchUsingBoostGauge fetches designs only when the attribute is absent; None would crash it."""
        self.client.listAllResearches = MagicMock()
        self.client.listAllResearchDesigns2 = MagicMock(return_value=False)
        self.assertEqual(self.client.getResearchLevel("Fitness"), 0)
        self.assertFalse(hasattr(self.client, "allResearches"))
        self.assertFalse(hasattr(self.client, "allResearchDesigns"))
        self.assertIs(self.client.speedUpResearchUsingBoostGauge("1", "900"), False)
        self.assertEqual(self.client.listAllResearchDesigns2.call_count, 2)

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


STATS = ("@HpImprovement", "@PilotImprovement", "@RepairImprovement", "@WeaponImprovement", "@ScienceImprovement",
         "@EngineImprovement", "@AttackImprovement", "@AbilityImprovement", "@StaminaImprovement")
ADDED = b'<TrainingService><AddTraining><Character CharacterId="501"/></AddTraining></TrainingService>'


def finished_reply(weapon, fatigue, stats=STATS):
    """A FinishTraining reply: each of ``stats`` at 5, WeaponImprovement at ``weapon``."""
    values = {"@WeaponImprovement": str(weapon)}
    attributes = " ".join(f'{stat[1:]}="{values.get(stat, "5")}"' for stat in stats)
    return reply(f'<TrainingService><FinishTraining><Character CharacterId="501" {attributes} Fatigue="{fatigue}"/>'
                 f"</FinishTraining></TrainingService>")


class TestManageTrainingWithHonestWrappers(EndpointCase):
    """One weapons crew member at 40% in an Academy with Education 101 done: the primary T1 tier is chosen."""

    def setUp(self):
        super().setUp()
        character = {stat: "5" for stat in STATS}  # eight stats at 5 and WeaponImprovement 0: 40 of 100
        character.update({"@CharacterId": "501", "@CharacterDesignId": "1", "@CharacterName": "Galactic Succubus",
                          "@RoomId": "r1", "@TrainingEndDate": "", "@Fatigue": "0", "@WeaponImprovement": "0"})
        self.client.allCharactersOfUser = {"CharacterService": {"ListAllCharactersOfUser": {"Characters": {
            "Character": character}}}}
        self.client.allCharacterDesigns = {"CharacterService": {"ListAllCharacterDesigns": {"CharacterDesigns": {
            "CharacterDesign": [
                {"@CharacterDesignId": "1", "@TrainingCapacity": "100", "@SpecialAbilityType": "None"},
                {"@CharacterDesignId": "2", "@TrainingCapacity": "100", "@SpecialAbilityType": "None"},
            ]}}}}
        self.client.roomsViaAccessToken = {"RoomService": {"ListRoomsViaAccessToken": {"Rooms": {"Room": {
            "@RoomId": "r1", "@RoomDesignId": "50"}}}}}
        self.client.roomDesigns = {"RoomService": {"ListRoomDesigns": {"RoomDesigns": {"RoomDesign": {
            "@RoomDesignId": "50", "@RoomName": "Academy"}}}}}
        self.client.trainingDesigns = {"TrainingService": {"ListAllTrainingDesigns": {"TrainingDesigns": {
            "TrainingDesign": [
                {"@TrainingDesignId": "31", "@TrainingName": "Read Expert Weapon Theory"},
                {"@TrainingDesignId": "32", "@TrainingName": "Bench Press"},
            ]}}}}
        self.client.allResearches = researches(("1", "700", "Completed"))
        self.client.allResearchDesigns = research_designs(("700", "Education 101"))

    def paths(self):
        return [urlsplit(url).path for url, _ in self.calls]

    def test_a_failed_finish_still_starts_the_training(self):
        self.serve(reply(b'<TrainingService><FinishTraining errorMessage="Training not found"/></TrainingService>'),
                   reply(ADDED))
        with self.assertLogs(level="INFO") as logs:
            self.assertIs(self.client.manageTraining(), True)
        self.assertEqual(self.paths(), ["/TrainingService/FinishTraining", "/TrainingService/AddTraining"])
        self.assertEqual(self.sent(1)[2][:2], [("trainingDesignId", "31"), ("characterId", "501")])
        output = "\n".join(logs.output)
        self.assertNotIn("Completed training", output)
        self.assertIn("Starting training Read Expert Weapon Theory for Galactic Succubus", output)

    def test_the_finished_character_reported_is_the_returned_one(self):
        self.serve(finished_reply(weapon=10, fatigue=7), reply(ADDED))
        with self.assertLogs(level="INFO") as logs:
            self.assertIs(self.client.manageTraining(), True)
        self.assertIn("10.00% training increase and 7 fatigue increase", "\n".join(logs.output))

    def test_the_returned_character_decides_not_the_trainingFinish_attribute(self):
        returned = {stat: "5" for stat in STATS}
        returned.update({"@WeaponImprovement": "20", "@Fatigue": "3"})

        def finish(character_id):
            self.client.trainingFinish = "stale"
            return returned

        self.serve(reply(ADDED))
        with patch.object(self.client, "finishTraining", side_effect=finish), self.assertLogs(level="INFO") as logs:
            self.assertIs(self.client.manageTraining(), True)
        self.assertIn("20.00% training increase and 3 fatigue increase", "\n".join(logs.output))

    def run_with_finish(self, left_behind, returned):
        """manageTraining with finishTraining stubbed to leave ``left_behind`` in trainingFinish and return ``returned``."""
        def finish(character_id):
            self.client.trainingFinish = left_behind
            return returned

        self.serve(reply(ADDED))
        with patch.object(self.client, "finishTraining", side_effect=finish), self.assertLogs(level="INFO") as logs:
            self.assertIs(self.client.manageTraining(), True)
        self.assertEqual(self.paths(), ["/TrainingService/AddTraining"])
        output = "\n".join(logs.output)
        self.assertIn("Starting training Read Expert Weapon Theory for Galactic Succubus", output)
        return output

    def test_a_failed_finish_is_not_reported_even_with_a_stale_trainingFinish(self):
        stale = {"TrainingService": {"FinishTraining": {"Character": {"@WeaponImprovement": "20", "@Fatigue": "3"}}}}
        self.assertNotIn("Completed training", self.run_with_finish(left_behind=stale, returned=None))

    def test_a_returned_character_is_reported_even_with_no_trainingFinish(self):
        returned = {stat: "5" for stat in STATS}
        returned.update({"@WeaponImprovement": "20", "@Fatigue": "3"})
        output = self.run_with_finish(left_behind=None, returned=returned)
        self.assertIn("Completed training for Galactic Succubus", output)
        self.assertIn("20.00% training increase and 3 fatigue increase", output)

    def test_a_partial_finished_character_does_not_crash(self):
        self.serve(finished_reply(weapon=10, fatigue=7, stats=("@WeaponImprovement",)), reply(ADDED))
        with self.assertLogs(level="INFO") as logs:
            self.assertIs(self.client.manageTraining(), True)
        self.assertEqual(self.paths(), ["/TrainingService/FinishTraining", "/TrainingService/AddTraining"])
        self.assertIn("10.00% training increase and 7 fatigue increase", "\n".join(logs.output))

    def test_an_unknown_training_sends_no_add(self):
        self.client.trainingDesigns = {"TrainingService": {"ListAllTrainingDesigns": {"TrainingDesigns": {
            "TrainingDesign": [{"@TrainingDesignId": "32", "@TrainingName": "Bench Press"}]}}}}
        self.serve(finished_reply(weapon=10, fatigue=7))
        self.assertIs(self.client.manageTraining(), True)
        self.assertEqual(self.paths(), ["/TrainingService/FinishTraining"])

    def test_a_crew_list_left_none_by_an_earlier_failed_read_is_fetched_again(self):
        self.client.allCharactersOfUser = None  # what a failed listAllCharactersOfUser leaves
        self.serve(http_error())
        self.assertIs(self.client.manageTraining(), False)
        self.assertEqual(self.paths(), ["/CharacterService/ListAllCharactersOfUser"])

    def test_without_research_the_primary_tier_is_skipped(self):
        self.client.allResearches = researches(("1", "700", "Researching"))
        request = self.refuse_requests()
        self.assertIs(self.client.manageTraining(), True)
        request.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
