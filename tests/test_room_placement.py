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


if __name__ == "__main__":
    unittest.main()
