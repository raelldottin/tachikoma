"""Boost-gauge speedups (issue #296): the pure planner in sdk/boost.py and Client.speedUpBoostGauge.

Covers speedUpResearchUsingBoostGauge, speedUpRoomConstructionUsingBoostGauge,
speedUpShipConstructionUsingBoostGauge and speedUpBoostGauge. All network traffic is mocked.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import importlib
import logging
import unittest
from unittest.mock import MagicMock, patch

import requests
import xmltodict

from sdk import boost
from sdk.boost import BoostDecision, plan_boost_gauge
from sdk.client import Client, DotNet
from sdk.device import Device

TOKEN = "00000000-1111-2222-3333-444444444444"
NOW = datetime.datetime(2026, 11, 25, 1, 2, 3)
NOW_TEXT = "2026-11-25T01:02:03"
API = "https://api.pixelstarships.com"
RESEARCH_URL = f"{API}/ResearchService/SpeedUpResearchUsingBoostGauge?researchId=501&accessToken={TOKEN}&clientDateTime={NOW_TEXT}"
ROOM_URL = f"{API}/RoomService/SpeedUpRoomConstructionUsingBoostGauge?roomId={{}}&clientDateTime={NOW_TEXT}&accessToken={TOKEN}"
SHIP_URL = f"{API}/ShipService/SpeedUpShipConstructionUsingBoostGauge?shipId=800&clientDateTime={NOW_TEXT}&accessToken={TOKEN}"
SHIP_READ_URL = f"{API}/ShipService/GetShipByUserId?userId=1&accessToken={TOKEN}&clientDateTime={NOW_TEXT}"


def research(research_id="501", design_id="10", state="Researching"):
    record = {"@ResearchDesignId": design_id, "@ResearchState": state}
    if research_id is not None:
        record["@ResearchId"] = research_id
    return record


def room(room_id="104", status="Upgrading"):
    return {"@RoomId": room_id, "@RoomDesignId": "50", "@RoomStatus": status}


def ship(researches=None, rooms=None, upgrade="0", ship_id="800"):
    """A parsed GetShipByUserId Ship record; ``researches``/``rooms`` are the raw Research/Room values."""
    record = {"@ShipDesignId": "108", "@ShipLevel": "8"}
    if ship_id is not None:
        record["@ShipId"] = ship_id
    if upgrade is not None:
        record["@UpgradeShipDesignId"] = upgrade
    if researches is not None:
        record["Researches"] = {"Research": researches}
    if rooms is not None:
        record["Rooms"] = {"Room": rooms}
    return record


def ship_xml(body="", upgrade="0"):
    return (f'<ShipService><GetShipByUserId><Ship ShipId="800" ShipDesignId="108" UpgradeShipDesignId="{upgrade}">'
            f"{body}</Ship></GetShipByUserId></ShipService>")


ALL_UNDER_WAY = ship_xml(
    '<Rooms><Room RoomId="104" RoomDesignId="50" RoomStatus="Upgrading"/>'
    '<Room RoomId="105" RoomDesignId="51" RoomStatus="Normal"/>'
    '<Room RoomId="106" RoomDesignId="52" RoomStatus="Upgrading"/></Rooms>'
    '<Researches><Research ResearchId="500" ResearchDesignId="9" ResearchState="Completed"/>'
    '<Research ResearchId="501" ResearchDesignId="10" ResearchState="Researching"/></Researches>',
    upgrade="109")


def parsed_ship(xml):
    return xmltodict.parse(xml)["ShipService"]["GetShipByUserId"]["Ship"]


def research_d(research_id="501"):
    return BoostDecision("research", research_id)


def room_d(room_id="104"):
    return BoostDecision("room", room_id)


SHIP_D = BoostDecision("ship", "800")


class PlanDetectionTests(unittest.TestCase):
    def test_a_research_under_way_is_boosted_by_its_research_id(self):
        self.assertEqual(plan_boost_gauge(ship(researches=research())), (research_d("501"),))

    def test_the_research_design_id_is_never_used_as_the_research_id(self):
        # Regression: the PR planner sent @ResearchDesignId and only fell back to @ResearchId.
        self.assertEqual(plan_boost_gauge(ship(researches=research("501", "10"))), (research_d("501"),))
        for missing in (None, "", "  "):
            with self.subTest(research_id=missing):
                self.assertEqual(plan_boost_gauge(ship(researches=research(missing, "10"))), ())

    def test_research_not_under_way_is_not_boosted(self):
        for state in ("Completed", "researching", "", None):
            with self.subTest(state=state):
                self.assertEqual(plan_boost_gauge(ship(researches=research(state=state))), ())

    def test_an_upgrading_room_is_boosted_by_its_room_id(self):
        self.assertEqual(plan_boost_gauge(ship(rooms=room("104"))), (room_d("104"),))

    def test_rooms_not_upgrading_or_without_an_id_are_not_boosted(self):
        for record in (room(status="Normal"), room(status="upgrading"), room(status=None),
                       {"@RoomStatus": "Upgrading"}, room(room_id="")):
            with self.subTest(record=record):
                self.assertEqual(plan_boost_gauge(ship(rooms=record)), ())

    def test_a_ship_upgrade_under_way_is_boosted_by_the_ship_id(self):
        self.assertEqual(plan_boost_gauge(ship(upgrade="109")), (SHIP_D,))
        self.assertEqual(plan_boost_gauge(ship(upgrade=" 109 ")), (SHIP_D,))

    def test_no_ship_upgrade_when_the_upgrade_design_is_empty_zero_or_missing(self):
        for upgrade in ("", "0", " 0 ", None):
            with self.subTest(upgrade=upgrade):
                self.assertEqual(plan_boost_gauge(ship(upgrade=upgrade)), ())

    def test_no_ship_decision_without_a_ship_id(self):
        for ship_id in (None, ""):
            with self.subTest(ship_id=ship_id):
                self.assertEqual(plan_boost_gauge(ship(upgrade="109", ship_id=ship_id)), ())


class PlanShapeTests(unittest.TestCase):
    def test_single_dict_and_list_shapes_give_the_same_decisions(self):
        self.assertEqual(plan_boost_gauge(ship(researches=research(), rooms=room())),
                         plan_boost_gauge(ship(researches=[research()], rooms=[room()])))

    def test_missing_and_empty_collections_give_no_decisions(self):
        for record in (ship(), {"@ShipId": "800", "Researches": None, "Rooms": None},
                       {"@ShipId": "800", "Researches": {}, "Rooms": {"Room": None}},
                       {"@ShipId": "800", "Researches": "", "Rooms": []},
                       ship(researches=[], rooms=[])):
            with self.subTest(record=record):
                self.assertEqual(plan_boost_gauge(record), ())

    def test_non_mapping_entries_are_skipped(self):
        self.assertEqual(plan_boost_gauge(ship(researches=["x", None, research()], rooms=[42, room()])),
                         (research_d(), room_d()))

    def test_a_non_mapping_ship_gives_no_decisions(self):
        for record in (None, [], "ship", 0, [ship(upgrade="109")]):
            with self.subTest(record=record):
                self.assertEqual(plan_boost_gauge(record), ())

    def test_parsed_xml_single_and_multiple_records(self):
        one = parsed_ship(ship_xml('<Rooms><Room RoomId="104" RoomStatus="Upgrading"/></Rooms>'
                                   '<Researches><Research ResearchId="501" ResearchDesignId="10" '
                                   'ResearchState="Researching"/></Researches>'))
        self.assertIsInstance(one["Rooms"]["Room"], dict)
        self.assertEqual(plan_boost_gauge(one), (research_d("501"), room_d("104")))
        empty = parsed_ship(ship_xml("<Rooms/><Researches/>"))
        self.assertEqual(plan_boost_gauge(empty), ())
        self.assertEqual(plan_boost_gauge(parsed_ship(ALL_UNDER_WAY)),
                         (research_d("501"), room_d("104"), room_d("106"), SHIP_D))


class PlanOrderTests(unittest.TestCase):
    def test_order_is_research_then_rooms_in_document_order_then_ship(self):
        record = ship(researches=[research("501")], rooms=[room("106"), room("104")], upgrade="109")
        self.assertEqual(plan_boost_gauge(record), (research_d("501"), room_d("106"), room_d("104"), SHIP_D))

    def test_order_does_not_depend_on_key_order(self):
        record = ship(researches=research(), rooms=room(), upgrade="109")
        reordered = dict(reversed(list(record.items())))
        self.assertEqual(plan_boost_gauge(reordered), (research_d(), room_d(), SHIP_D))

    def test_planning_is_deterministic_and_leaves_the_input_unchanged(self):
        record = parsed_ship(ALL_UNDER_WAY)
        before = copy.deepcopy(record)
        first = plan_boost_gauge(record)
        self.assertEqual([plan_boost_gauge(record) for _ in range(3)], [first] * 3)
        self.assertEqual(record, before)
        self.assertIsInstance(first, tuple)


class DecisionTests(unittest.TestCase):
    def test_a_decision_is_a_plain_frozen_dataclass(self):
        decision = BoostDecision("room", "104")
        self.assertEqual([f.name for f in dataclasses.fields(decision)], ["kind", "id"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.id = "105"  # type: ignore[misc]
        self.assertNotEqual(decision, ("room", "104"))
        self.assertNotEqual(decision, {"kind": "room", "id": "104"})

    def test_there_is_one_planner_module_and_no_aliases(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("sdk.boost_planner")
        public = sorted(name for name in vars(boost) if not name.startswith("_")
                        and getattr(vars(boost)[name], "__module__", None) == "sdk.boost")
        self.assertEqual(public, ["BoostDecision", "plan_boost_gauge"])


def reply(text):
    return MagicMock(content=text.encode(), text=text)


SUCCESS = reply("<ResearchService><SpeedUpResearchUsingBoostGauge success=\"Success\"/></ResearchService>")
REFUSED = reply('<RoomService><SpeedUpRoomConstructionUsingBoostGauge errorMessage="Not enough boost"/></RoomService>')


def http_error(status=404):
    response = requests.Response()
    response.status_code = status
    response._content = b""
    return response


class ClientCase(unittest.TestCase):
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
        logging.disable(logging.INFO)  # keep the per-call INFO lines out of the test output; warnings still log
        self.addCleanup(logging.disable, logging.NOTSET)

    @staticmethod
    def serving(*answers):
        """A patch of Client.request answering in turn (an exception is raised)."""
        return patch.object(Client, "request", side_effect=list(answers))

    def serve(self, *answers):
        """Patch Client.request for the rest of the test; returns the mock."""
        request = self.serving(*answers)
        mock = request.start()
        self.addCleanup(request.stop)
        return mock

    @staticmethod
    def sent(mock):
        return [call.args for call in mock.call_args_list]


class EndpointTests(ClientCase):
    def test_each_endpoint_posts_its_exact_url(self):
        rows = (("speedUpResearchUsingBoostGauge", "501", RESEARCH_URL),
                ("speedUpRoomConstructionUsingBoostGauge", "104", ROOM_URL.format("104")),
                ("speedUpShipConstructionUsingBoostGauge", "800", SHIP_URL))
        for method, ident, url in rows:
            with self.subTest(method=method), self.serving(SUCCESS) as request:
                self.assertIs(getattr(self.client, method)(ident), True)
                self.assertEqual(self.sent(request), [(url, "POST")])

    def test_an_error_message_or_falsy_reply_is_a_failure(self):
        for answer in (REFUSED, reply('<X errorMessage="x"/>'), None, http_error(404), http_error(400)):
            for method in ("speedUpResearchUsingBoostGauge", "speedUpRoomConstructionUsingBoostGauge",
                           "speedUpShipConstructionUsingBoostGauge"):
                with self.subTest(answer=answer, method=method), self.serving(answer):
                    self.assertIs(getattr(self.client, method)("1"), False)


class SpeedUpBoostGaugeTests(ClientCase):
    def use_ship(self, xml):
        self.client.shipByUserId = xmltodict.parse(xml)

    def test_stops_after_the_first_success(self):
        self.use_ship(ALL_UNDER_WAY)
        request = self.serve(SUCCESS)
        self.assertIs(self.client.speedUpBoostGauge(), True)
        self.assertEqual(self.sent(request), [(RESEARCH_URL, "POST")])

    def test_a_failed_call_moves_on_to_the_next_candidate(self):
        self.use_ship(ALL_UNDER_WAY)
        request = self.serve(REFUSED, None, SUCCESS)
        self.assertIs(self.client.speedUpBoostGauge(), True)
        self.assertEqual(self.sent(request), [(RESEARCH_URL, "POST"), (ROOM_URL.format("104"), "POST"),
                                              (ROOM_URL.format("106"), "POST")])

    def test_a_raising_call_moves_on_and_never_raises(self):
        self.use_ship(ALL_UNDER_WAY)
        request = self.serve(requests.ConnectionError("down"), RuntimeError("boom"), REFUSED, SUCCESS)
        with self.assertLogs(level="WARNING") as logs:
            self.assertIs(self.client.speedUpBoostGauge(), True)
        self.assertEqual(self.sent(request), [(RESEARCH_URL, "POST"), (ROOM_URL.format("104"), "POST"),
                                              (ROOM_URL.format("106"), "POST"), (SHIP_URL, "POST")])
        self.assertEqual(len(logs.records), 2)

    def test_a_raising_call_logs_no_access_token(self):
        self.use_ship(ALL_UNDER_WAY)
        leak = RuntimeError(f"failed: {RESEARCH_URL}")
        request = self.serve(leak, SUCCESS)
        with self.assertLogs(level="WARNING") as logs:
            self.assertIs(self.client.speedUpBoostGauge(), True)
        self.assertEqual(self.sent(request), [(RESEARCH_URL, "POST"), (ROOM_URL.format("104"), "POST")])
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_a_raising_ship_read_logs_no_access_token(self):
        request = self.serve(RuntimeError(f"failed: {SHIP_READ_URL}"))
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(self.client.speedUpBoostGauge())
        self.assertEqual(self.sent(request), [(SHIP_READ_URL, "GET")])
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_every_call_failing_tries_each_candidate_once_and_returns_none(self):
        self.use_ship(ALL_UNDER_WAY)
        request = self.serve(REFUSED, REFUSED, http_error(), RuntimeError("boom"))
        self.assertIsNone(self.client.speedUpBoostGauge())
        self.assertEqual(self.sent(request), [(RESEARCH_URL, "POST"), (ROOM_URL.format("104"), "POST"),
                                              (ROOM_URL.format("106"), "POST"), (SHIP_URL, "POST")])

    def test_the_ship_upgrade_alone_is_boosted(self):
        self.use_ship(ship_xml("<Rooms/><Researches/>", upgrade="109"))
        request = self.serve(SUCCESS)
        self.assertIs(self.client.speedUpBoostGauge(), True)
        self.assertEqual(self.sent(request), [(SHIP_URL, "POST")])

    def test_nothing_under_way_sends_nothing(self):
        self.use_ship(ship_xml('<Rooms><Room RoomId="104" RoomStatus="Normal"/></Rooms>'
                               '<Researches><Research ResearchId="501" ResearchDesignId="10" '
                               'ResearchState="Completed"/></Researches>'))
        request = self.serve()
        self.assertIsNone(self.client.speedUpBoostGauge())
        request.assert_not_called()

    def test_reads_the_ship_when_none_is_cached(self):
        request = self.serve(reply(ALL_UNDER_WAY), SUCCESS)
        self.assertIs(self.client.speedUpBoostGauge(), True)
        self.assertEqual(self.sent(request), [(SHIP_READ_URL, "GET"), (RESEARCH_URL, "POST")])

    def test_a_failed_or_raising_ship_read_sends_no_boost_and_never_raises(self):
        for answer in (None, reply("<ShipService/>"), requests.ConnectionError("down")):
            with self.subTest(answer=answer), self.serving(answer) as request:
                self.client.__dict__.pop("shipByUserId", None)
                self.assertIsNone(self.client.speedUpBoostGauge())
                self.assertEqual(self.sent(request), [(SHIP_READ_URL, "GET")])


if __name__ == "__main__":
    unittest.main()
