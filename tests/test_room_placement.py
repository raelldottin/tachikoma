"""Room placement grid model and check (SPEC-13). Game data from live captures; no game traffic."""

import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sdk.policy.placement import ShipLayout, first_free, placement_refusal
from sdk.policy.progression import POLICIES, DesignData, PolicyConfig
from sdk.state import build_snapshot
from sdk.state.baseline import _ROOM_KEYS

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
T0 = datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc)

# pss5's hull (ShipDesign 6) and rooms as capture run 43316397906 recorded them on 2026-11-12. These are game data:
# the mask, the room designs and their positions. The room ids below are synthetic.
PSS5_MASK = ("00000000000000000" "01111000000000000" "01111111111111000" "01111111111111110"
             "01111111111111110" "01111111111111100" "00001111111110000" "00001111111000000"
             "00000000000000000")
PSS5_ROOMS = [("120", 4, 11), ("6", 2, 11), ("4", 4, 4), ("5", 2, 4), ("10", 4, 8), ("3", 5, 7), ("3", 4, 7),
              ("3", 3, 7), ("1", 2, 8)]
SHIP_DESIGNS = [
    {"@ShipDesignId": "6", "@Rows": "9", "@Columns": "17", "@Mask": PSS5_MASK, "@ShipLevel": "2"},
    # A hull with a cell of type 2, whose meaning is not confirmed.
    {"@ShipDesignId": "90", "@Rows": "3", "@Columns": "4", "@Mask": "0000" "0121" "0110", "@ShipLevel": "2"},
    {"@ShipDesignId": "91", "@Rows": "3", "@Columns": "4", "@Mask": "0101"},  # length is not rows x columns
]


def design(design_id, name, rows, columns, price, min_level="1", root=None):
    return {"@RoomDesignId": design_id, "@RoomName": name, "@Rows": str(rows), "@Columns": str(columns),
            "@PriceString": price, "@MinShipLevel": min_level, "@RootRoomDesignId": root or design_id}


ROOM_DESIGNS = [
    design("1", "Bridge Lv1", 2, 3, "mineral:200"),
    design("3", "Lift Lv1", 1, 1, "mineral:25"),
    design("4", "Engine Lv1", 2, 3, "gas:200"),
    design("5", "Small Reactor Lv1", 2, 3, "gas:50"),
    design("6", "Mineral Mining Laser Lv1", 2, 3, "mineral:100"),
    design("7", "Warehouse Lv1", 2, 3, "mineral:80000", min_level="6"),
    design("10", "Bedroom Lv2", 2, 3, "mineral:200", root="2"),
    design("26", "Mineral Extractor Lv1", 2, 2, "gas:25"),
    design("42", "Gas Collector Lv1", 2, 2, "mineral:10"),
    design("43", "Gas Collector Lv2", 2, 2, "mineral:400", root="42"),
    design("120", "Bolter Lv1", 2, 2, "mineral:100"),
    design("386", "Vault Lv1", 3, 3, "supply:10000"),
    design("900", "Starbux Room", 1, 1, "starbux:50"),
    design("901", "No Purchase Record", 1, 1, "mineral:1"),
]


def purchase(design_id, level, quantity, availability="1"):
    return {"@RoomDesignId": design_id, "@Level": str(level), "@Quantity": str(quantity),
            "@AvailabilityMask": availability}


PURCHASES = [
    purchase("1", 1, 1, "3"), purchase("2", 1, 1), purchase("2", 3, 1), purchase("3", 1, 6, "3"),
    purchase("4", 1, 1), purchase("6", 1, 1), purchase("120", 1, 1, "3"), purchase("7", 6, 1, "3"),
    purchase("26", 1, 1), purchase("26", 3, 1), purchase("42", 1, 1), purchase("42", 4, 1), purchase("42", 6, 1),
    purchase("386", 4, 1, "3"), purchase("900", 1, 1),
    # The reactor's records carry two availability masks, which are not understood.
    purchase("5", 1, 1, "1"), purchase("5", 1, 1, "2"),
]
LAYOUT = ShipLayout.from_records(SHIP_DESIGNS, ROOM_DESIGNS, PURCHASES)


def room_records(rooms=PSS5_ROOMS):
    return [{"@RoomId": f"r{index}", "@RoomDesignId": design_id, "@RoomStatus": "Normal", "@Row": str(row),
             "@Column": str(column)} for index, (design_id, row, column) in enumerate(rooms)]


def ship_snapshot(rooms=None, ship=None):
    return build_snapshot("pss5", T0, "synthetic", ship=ship or {"@ShipDesignId": "6", "@ShipLevel": "2"},
                          rooms=room_records() if rooms is None else rooms, crew=[], research=[], inventory=[],
                          resources={"minerals": "500", "gas": "0", "starbux": "0"})


class SnapshotPositionTests(unittest.TestCase):
    # Verifies: S13-R3
    def test_rooms_carry_positions_and_the_ship_its_hull_design(self):
        snapshot = ship_snapshot()
        self.assertEqual((snapshot.ship_design_id, snapshot.hull_level), ("6", 2))
        self.assertEqual([(room.design_id, room.row, room.column) for room in snapshot.rooms], PSS5_ROOMS)
        record = snapshot.to_dict()
        self.assertEqual(record["ship_design_id"], "6")
        self.assertEqual((record["rooms"][0]["row"], record["rooms"][0]["column"]), (4, 11))

    # Verifies: S13-R3
    def test_unobserved_positions_are_none_and_the_baseline_format_is_unchanged(self):
        snapshot = build_snapshot("pss5", T0, "synthetic", ship={"@ShipLevel": "2"}, crew=[], research=[],
                                  inventory=[], rooms=[{"@RoomId": "r1", "@RoomDesignId": "1", "@Row": "x"}])
        self.assertIsNone(snapshot.ship_design_id)
        self.assertEqual((snapshot.rooms[0].row, snapshot.rooms[0].column), (None, None))
        self.assertEqual(_ROOM_KEYS, ("room_id", "design_id", "status"))


    # Verifies: S13-R3
    def test_positions_are_left_out_of_equality_because_baselines_do_not_keep_them(self):
        without = build_snapshot("pss5", T0, "synthetic", ship={"@ShipLevel": "2"}, crew=[], research=[],
                                 inventory=[], resources={"minerals": "500", "gas": "0", "starbux": "0"},
                                 rooms=[{key: value for key, value in record.items() if key not in ("@Row", "@Column")}
                                        for record in room_records()])
        self.assertEqual(ship_snapshot(), without)


class LayoutTests(unittest.TestCase):
    # Verifies: S13-R3
    def test_the_captured_hull_and_rooms_agree(self):
        hull = LAYOUT.hulls["6"]
        self.assertEqual((hull.rows, hull.columns, len(hull.mask)), (9, 17, 153))
        for design_id, row, column in PSS5_ROOMS:  # every placed room sits on buildable cells
            spec = LAYOUT.rooms[design_id]
            cells = [hull.cell(row + dy, column + dx) for dy in range(spec.rows) for dx in range(spec.columns)]
            self.assertEqual(set(cells), {"1"}, design_id)

    # Verifies: S13-R3
    def test_a_split_capture_mask_parses_and_a_bad_mask_is_unknown(self):
        split = " ".join(PSS5_MASK[index:index + 12] for index in range(0, len(PSS5_MASK), 12))
        layout = ShipLayout.from_records([{**SHIP_DESIGNS[0], "@Mask": split}], ROOM_DESIGNS, PURCHASES)
        self.assertEqual(layout.hulls["6"].mask, PSS5_MASK)
        self.assertIsNone(LAYOUT.hulls["91"].mask)
        self.assertIsNone(ShipLayout.from_records([{**SHIP_DESIGNS[0], "@Mask": "***REDACTED***"}]).hulls["6"].mask)

    def test_root_designs(self):
        self.assertEqual((LAYOUT.root_of("43"), LAYOUT.root_of("42"), LAYOUT.root_of("10")), ("42", "42", "2"))
        self.assertIsNone(LAYOUT.root_of("999"))


class PlacementTests(unittest.TestCase):
    def refusal(self, design_id="42", row=1, column=1, snapshot=None, layout=LAYOUT):
        return placement_refusal(snapshot or ship_snapshot(), layout, design_id, row, column)

    # Verifies: S13-R3
    def test_the_gas_collector_fits_first_at_row_1_column_1(self):
        self.assertIsNone(self.refusal())
        self.assertEqual(first_free(ship_snapshot(), LAYOUT, "42"), (1, 1))

    # Verifies: S13-R3
    def test_off_grid_outside_the_hull_and_overlapping_placements_are_refused(self):
        cases = {
            "below the grid": ((9, 0), "outside the ship's grid"),
            "outside the hull": ((0, 0), "not a buildable interior cell"),
            "over the bridge": ((2, 8), "occupied by a placed room"),
            "half over a lift": ((3, 6), "occupied by a placed room"),
            "right of the grid": ((0, 17), "outside the ship's grid"),
            "half off the right edge": ((3, 16), "not a buildable interior cell"),
        }
        for name, ((row, column), reason) in cases.items():
            with self.subTest(case=name):
                self.assertIn(reason, self.refusal(row=row, column=column))

    # Verifies: S13-R3
    def test_a_cell_of_unconfirmed_type_is_refused(self):
        snapshot = ship_snapshot(rooms=[], ship={"@ShipDesignId": "90", "@ShipLevel": "2"})
        self.assertIn("not a buildable interior cell", self.refusal(design_id="3", row=1, column=2, snapshot=snapshot))
        self.assertIsNone(self.refusal(design_id="3", row=1, column=1, snapshot=snapshot))  # a 1-cell next to it

    # Verifies: S13-R3, S13-R8
    def test_prices_other_than_minerals_or_gas_are_refused(self):
        for design_id in ("900", "386"):  # starbux, and a "supply" price
            with self.subTest(design=design_id):
                self.assertIn("not priced in minerals or gas", self.refusal(design_id=design_id))

    # Verifies: S13-R3
    def test_ship_level_and_purchase_limits_are_respected(self):
        self.assertIn("needs ship level 6", self.refusal(design_id="7"))
        self.assertIn("no purchase record", self.refusal(design_id="901"))
        self.assertIn("availability masks that are not understood", self.refusal(design_id="5"))
        with_collector = ship_snapshot(rooms=room_records(PSS5_ROOMS + [("43", 1, 1)]))
        self.assertIn("the most allowed at its level", self.refusal(row=6, column=4, snapshot=with_collector))
        # The extractor is allowed by count, but this ship's level is checked against each level's entries.
        self.assertIsNone(self.refusal(design_id="26", row=1, column=1))

    # Verifies: S13-R3
    def test_unobserved_inputs_are_refused_never_assumed_free(self):
        no_position = room_records()
        del no_position[0]["@Row"]
        unknown_design = room_records(PSS5_ROOMS + [("777", 6, 4)])
        cases = {
            "rooms not observed": build_snapshot("pss5", T0, "synthetic", ship={"@ShipDesignId": "6", "@ShipLevel": "2"},
                                                 crew=[], research=[], inventory=[]),
            "hull design not observed": ship_snapshot(ship={"@ShipLevel": "2"}),
            "hull level not observed": ship_snapshot(ship={"@ShipDesignId": "6"}),
            "hull grid not known": ship_snapshot(ship={"@ShipDesignId": "91", "@ShipLevel": "2"}),
            "a room's position not observed": ship_snapshot(rooms=no_position),
            "a placed room's design not known": ship_snapshot(rooms=unknown_design),
        }
        for name, snapshot in cases.items():
            with self.subTest(case=name):
                self.assertIsNotNone(self.refusal(snapshot=snapshot))
        self.assertIn("size of room design 999", self.refusal(design_id="999"))
        unknown_level = ShipLayout.from_records(SHIP_DESIGNS, [{**ROOM_DESIGNS[8], "@MinShipLevel": ""}], PURCHASES)
        self.assertIn("minimum ship level", self.refusal(layout=unknown_level))

    # Verifies: S13-R3
    def test_first_free_is_none_when_nothing_fits(self):
        self.assertIsNone(first_free(ship_snapshot(), LAYOUT, "7"))
        self.assertIsNone(first_free(ship_snapshot(ship={"@ShipDesignId": "404", "@ShipLevel": "2"}), LAYOUT, "42"))


class PolicyParityTests(unittest.TestCase):
    # Verifies: S13-R9
    def test_policies_plan_the_same_with_or_without_room_positions(self):
        scenario_dir = REPO_ROOT / "sdk/simulation/scenarios"
        checked = 0
        for path in sorted(scenario_dir.glob("*.json")):
            scenario = json.loads(path.read_text(encoding="utf-8"))
            design_data = DesignData.from_records(**scenario.get("design", {}))
            for alias, account in scenario["accounts"].items():
                rooms = account.get("rooms", [])
                placed = [{**room, "@Row": str(index), "@Column": "3"} for index, room in enumerate(rooms)]
                common = dict(profile={"@Trophy": str(account.get("trophies", 0))}, crew=[],
                              research=account.get("research", []), inventory=account.get("inventory", []),
                              resources={kind: str(account.get(kind, 0)) for kind in ("minerals", "gas", "starbux")})
                plain = build_snapshot(alias, T0, "simulation", ship={"@ShipLevel": "1"}, rooms=rooms, **common)
                with_positions = build_snapshot(alias, T0, "simulation",
                                                ship={"@ShipLevel": "1", "@ShipDesignId": "6"}, rooms=placed, **common)
                for name, policy in POLICIES.items():
                    with self.subTest(scenario=path.name, account=alias, policy=name):
                        first = policy.plan(plain, design_data, PolicyConfig())
                        second = policy.plan(with_positions, design_data, PolicyConfig())
                        self.assertEqual((first.requests, first.rejected), (second.requests, second.rejected))
                        checked += 1
        self.assertGreater(checked, 0)

    def test_room_positions_do_not_change_snapshot_diffs(self):
        from sdk.state import diff_snapshots

        before = ship_snapshot()
        after = ship_snapshot(rooms=[{**record, "@Row": "0"} for record in room_records()])
        self.assertFalse(diff_snapshots(before, replace(after, observed_at=T0)).room_design)


FIXTURE = REPO_ROOT / "tests/fixtures/live_sanitized/rooms/buy_room2.json"


class FakeResponse:
    def __init__(self, text):
        self.content = text.encode("utf-8") if isinstance(text, str) else text
        self.text = text if isinstance(text, str) else ""


class PlaceRoomRequestTests(unittest.TestCase):
    """``Client.placeRoom`` reproduces the owner's sanitized BuyRoom2 capture (S13-R1, S13-R2)."""

    def setUp(self):
        from sdk.client import Client

        self.capture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.client = object.__new__(Client)  # no login, no device, no network
        self.client.accessToken = "synthetic-access-token-0001"
        self.sent = []
        self.response = FakeResponse(self.capture["response"])

        def request(url, method, *args, **kwargs):
            self.sent.append((url, method, args, kwargs))
            return self.response

        self.client.request = request

    # Verifies: S13-R1
    def test_the_capture_is_sanitized_and_recorded(self):
        from sdk.state.capture import scan_capture_text

        self.assertEqual(scan_capture_text(FIXTURE.read_text(encoding="utf-8"), "buy_room2.json"), [])
        self.assertEqual(self.capture["query"]["accessToken"], "***REDACTED***")
        readme = (REPO_ROOT / "tests/fixtures/live_sanitized/README.md").read_text(encoding="utf-8")
        self.assertIn("| rooms/buy_room2.json | BuyRoom2", readme)

    # Verifies: S13-R2
    def test_the_request_matches_the_captured_shape(self):
        from urllib.parse import parse_qsl, urlsplit

        self.client.placeRoom("42", 1, 1)
        (url, method, args, kwargs), = self.sent
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        # The capture records a GET, but the live server answered HTTP 405 to a GET in placement runs 43389706745
        # and 43391935925 (2026-11-13), so the request is a POST, like UpgradeRoom2 and CollectAllResources.
        self.assertEqual(self.capture["method"], "GET")
        self.assertEqual((method, parts.scheme, parts.netloc, parts.path),
                         ("POST", "https", "api.pixelstarships.com", self.capture["path"]))
        self.assertEqual([name for name, _ in query], list(self.capture["query"]))  # the same names, in order
        values = dict(query)
        self.assertEqual((values["roomDesignId"], values["column"], values["row"]), ("42", "1", "1"))
        self.assertEqual(values["accessToken"], "synthetic-access-token-0001")
        captured_date = self.capture["query"]["constructionStartDate"]
        self.assertRegex(captured_date, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertRegex(values["constructionStartDate"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual((args, kwargs, self.capture["body"]), ((), {}, ""))  # no body, no checksum

    # Verifies: S13-R2
    def test_only_the_captured_success_shape_returns_true(self):
        self.assertIs(self.client.placeRoom("6", 2, 11), True)  # the captured purchase
        for call in (("6", 2, 12), ("42", 2, 11)):  # a room, but not the one asked for
            with self.subTest(call=call):
                self.assertIs(self.client.placeRoom(*call), self.response)
        for body in ("<RoomService><BuyRoom2 errorMessage=\"Not enough minerals.\"/></RoomService>", "not xml", ""):
            with self.subTest(body=body):
                self.response = FakeResponse(body)
                self.assertIs(self.client.placeRoom("6", 2, 11), self.response)
        self.response = None
        self.assertIsNone(self.client.placeRoom("6", 2, 11))

    # Verifies: S13-R2
    def test_the_reply_summary_keeps_status_elements_and_messages_never_the_body(self):
        self.client.placeRoom("6", 2, 11)
        reply = self.client.placeRoomReply
        self.assertTrue(reply["success_shape"])
        self.assertEqual(reply["elements"][:3], ["RoomService", "BuyRoom2", "Room"])
        self.assertNotIn("ROOM-A", json.dumps(reply))  # attribute values other than messages are not kept
        refusal = FakeResponse('<RoomService><BuyRoom2 errorMessage="Cell is occupied."/></RoomService>')
        refusal.status_code = 200
        self.response = refusal
        self.client.placeRoom("42", 1, 1)
        reply = self.client.placeRoomReply
        self.assertEqual((reply["http_status"], reply["success_shape"], reply["parsed"]), (200, False, True))
        self.assertEqual(reply["messages"], [{"attribute": "errorMessage", "text": "Cell is occupied."}])
        body = "<html>Service Unavailable</html> not xml <"
        self.response = FakeResponse(body)
        self.client.placeRoom("42", 1, 1)
        self.assertEqual((self.client.placeRoomReply["parsed"], self.client.placeRoomReply["length"]),
                         (False, len(body)))
        self.response = FakeResponse("<a message='" + "x" * 900 + "'/>")
        self.client.placeRoom("42", 1, 1)
        self.assertEqual(len(self.client.placeRoomReply["messages"][0]["text"]), 300)

    # Verifies: S13-R2
    def test_the_live_buyroom_reply_is_a_success_too(self):
        # Run 43393262041 journaled the live POST reply's elements as RoomService/BuyRoom/Room, then Items.
        live = self.capture["response"].replace("<BuyRoom2>", "<BuyRoom>").replace("</BuyRoom2>", "<Items/></BuyRoom>")
        self.assertIn("<BuyRoom>", live)
        self.response = FakeResponse(live)
        self.assertIs(self.client.placeRoom("6", 2, 11), True)
        self.assertTrue(self.client.placeRoomReply["success_shape"])
        self.assertEqual(self.client.placeRoomReply["elements"][:3], ["RoomService", "BuyRoom", "Room"])
        self.assertIs(self.client.placeRoom("6", 2, 12), self.response)  # a room, but not where it was asked for
        self.response = FakeResponse("<RoomService><BuyRoom3><Room RoomDesignId=\"6\" Row=\"2\" Column=\"11\"/>"
                                     "</BuyRoom3></RoomService>")
        self.assertIs(self.client.placeRoom("6", 2, 11), self.response)  # an element nobody has seen is not success

    # Verifies: S13-R2
    def test_a_request_that_raises_leaves_no_reply(self):
        def boom(*args, **kwargs):
            raise ConnectionError("synthetic")

        self.client.placeRoomReply = {"left": "over"}
        self.client.request = boom
        with self.assertRaises(ConnectionError):
            self.client.placeRoom("42", 1, 1)
        self.assertIsNone(self.client.placeRoomReply)

    # Verifies: S13-R2, S13-R3
    def test_the_after_read_shows_the_bought_room_where_it_was_placed(self):
        import xmltodict

        self.assertIsNone(self.capture["ship_before"])
        ship = xmltodict.parse(self.capture["ship_after"])["ShipService"]["GetShipByUserId"]["Ship"]
        rooms = ship["Rooms"]["Room"]
        snapshot = build_snapshot("tutorial", T0, "capture", ship={key: value for key, value in ship.items()
                                                                     if key.startswith("@")},
                                  rooms=rooms if isinstance(rooms, list) else [rooms], crew=[], research=[],
                                  inventory=[])
        (room,) = snapshot.rooms
        self.assertEqual((room.design_id, room.status, room.row, room.column), ("6", "Building", 2, 11))

    # Verifies: S13-R2
    def test_no_existing_code_path_calls_place_room(self):
        callers = []
        for path in [REPO_ROOT / "run.py", *sorted((REPO_ROOT / "sdk").rglob("*.py"))]:
            text = path.read_text(encoding="utf-8")
            calls = text.count("placeRoom(") - text.count("def placeRoom(")
            if calls:
                callers.append(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(callers, [])


if __name__ == "__main__":
    unittest.main()
