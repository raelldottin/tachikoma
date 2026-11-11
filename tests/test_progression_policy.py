"""Tests for sdk.policy baseline-v1. Synthetic snapshots and design data only."""

import unittest
from datetime import datetime, timezone

from sdk.actions import CATALOG, check_preconditions, get_spec
from sdk.policy import DesignData, PolicyConfig, get_policy
from sdk.state import build_snapshot

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
DESIGN = DesignData.from_records(
    room_designs=[
        {"@RoomDesignId": "100", "@RoomName": "Engine"},
        {"@RoomDesignId": "101", "@RoomName": "Engine II", "@UpgradeFromRoomDesignId": "100", "@PriceString": "mineral:300"},
        {"@RoomDesignId": "200", "@RoomName": "Shield"},
        {"@RoomDesignId": "201", "@RoomName": "Shield II", "@UpgradeFromRoomDesignId": "200", "@PriceString": "gas:900"},
        {"@RoomDesignId": "300", "@RoomName": "Bridge", "@UpgradeFromRoomDesignId": "0"},
    ],
    research_designs=[
        {"@ResearchDesignId": "500", "@RootResearchDesignId": "500", "@GasCost": "100"},
        {"@ResearchDesignId": "501", "@RootResearchDesignId": "500", "@GasCost": "200"},
        {"@ResearchDesignId": "510", "@RootResearchDesignId": "510", "@GasCost": "50"},
        {"@ResearchDesignId": "520", "@RootResearchDesignId": "520", "@GasCost": "0", "@StarbuxCost": "10"},
    ],
)
ROOMS = [
    {"@RoomId": "r1", "@RoomDesignId": "100", "@RoomStatus": "Normal"},
    {"@RoomId": "r2", "@RoomDesignId": "200", "@RoomStatus": "Normal"},
    {"@RoomId": "r3", "@RoomDesignId": "100", "@RoomStatus": "Upgrading"},
    {"@RoomId": "r4", "@RoomDesignId": "300", "@RoomStatus": "Normal"},
]
RESEARCH = [{"@ResearchDesignId": "500", "@ResearchState": "Completed"}]


def snap(**overrides):
    kwargs = dict(resources={"minerals": "1000", "gas": "2000", "starbux": "5"}, rooms=ROOMS, research=RESEARCH)
    kwargs.update(overrides)
    return build_snapshot("alpha", T0, "synthetic-fixture", **kwargs)


def plan(snapshot=None, **config):
    return get_policy("baseline-v1").plan(snapshot or snap(), DESIGN, PolicyConfig(**config))


def ids(result):
    return [(request.action_id, dict(request.params)) for request in result.requests]


def reasons(result):
    return {rejection.candidate: rejection.reason for rejection in result.rejected}


class BaselineV1Tests(unittest.TestCase):
    def test_order_follows_run_py(self):
        result = plan()
        self.assertEqual(result.policy, "baseline-v1")
        self.assertEqual(
            ids(result),
            [
                ("add_research", {"research_design_id": "501"}),
                ("add_research", {"research_design_id": "510"}),
                ("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "101"}),
                ("collect_all_resources", {}),
                ("upgrade_room", {"room_id": "r2", "upgrade_room_design_id": "201"}),
                ("collect_all_resources", {}),
                ("collect_daily_reward", {}),
            ],
        )

    def test_every_request_is_a_catalog_action_whose_preconditions_hold(self):
        snapshot = snap()
        for request in plan(snapshot).requests:
            with self.subTest(request=request.action_id):
                self.assertIn(request.action_id, CATALOG)
                self.assertEqual(check_preconditions(get_spec(request.action_id), snapshot, request.params), [])

    def test_upgrading_rooms_and_unpriced_tops_are_skipped(self):
        result = plan()
        self.assertIn("room r3 is already upgrading", reasons(result)["upgrade_room r3"])
        self.assertNotIn("upgrade_room r4", reasons(result))
        self.assertNotIn("r4", [request.params.get("room_id") for request in result.requests])

    def test_cumulative_spend_and_reserves_are_respected(self):
        # 2000 gas - 1500 reserve = 500: research 501 (200) and 510 (50) fit, the 900-gas shield does not.
        result = plan(gas_reserve=1500)
        self.assertNotIn(("upgrade_room", {"room_id": "r2", "upgrade_room_design_id": "201"}), ids(result))
        self.assertEqual(reasons(result)["upgrade_room r2"], "needs 900 gas, 250 available above reserve")
        self.assertEqual(result.requests[0].expected_cost, {"gas": 200})

    def test_spend_caps_limit_cumulative_planned_spend(self):
        # Balances are ample (1000 minerals, 2000 gas); only the caps bind.
        result = plan(max_mineral_spend=299, max_gas_spend=250)
        self.assertEqual(reasons(result)["upgrade_room r1"], "needs 300 mineral, 299 left under the mineral spend cap")
        # Research 501 (200) and 510 (50) use the whole gas cap, so the 900-gas shield is refused.
        self.assertIn(("add_research", {"research_design_id": "510"}), ids(result))
        self.assertEqual(reasons(result)["upgrade_room r2"], "needs 900 gas, 0 left under the gas spend cap")

    def test_spend_cap_applies_to_the_planning_snapshot_whatever_its_balance(self):
        rich = snap(resources={"minerals": "100000", "gas": "100000", "starbux": "5"})
        result = plan(rich, max_mineral_spend=50)
        self.assertNotIn(("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "101"}), ids(result))
        self.assertIn("50 left under the mineral spend cap", reasons(result)["upgrade_room r1"])

    def test_no_caps_by_default(self):
        self.assertEqual(plan(), plan(max_mineral_spend=None, max_gas_spend=None))

    def test_missing_or_unparseable_room_price_is_cost_unknown(self):
        for price in (None, "", "mineral:1,500", "mineral", "mineral:", ":300", "mineral:-5", "gas:1_000"):
            with self.subTest(price=price):
                record = {"@RoomDesignId": "101", "@UpgradeFromRoomDesignId": "100"}
                if price is not None:
                    record["@PriceString"] = price
                design = DesignData.from_records(room_designs=[{"@RoomDesignId": "100"}, record])
                result = get_policy("baseline-v1").plan(snap(), design, PolicyConfig())
                self.assertNotIn("upgrade_room", [request.action_id for request in result.requests])
                self.assertEqual(reasons(result)["upgrade_room r1"],
                                 "cost unknown: room design 101 has no parseable @PriceString")

    def test_missing_or_unparseable_research_gas_cost_is_cost_unknown(self):
        for gas in (None, "", "1,500", "abc"):
            with self.subTest(gas=gas):
                record = {"@ResearchDesignId": "600", "@RootResearchDesignId": "600"}
                if gas is not None:
                    record["@GasCost"] = gas
                design = DesignData.from_records(research_designs=[record])
                result = get_policy("baseline-v1").plan(snap(research=[]), design, PolicyConfig())
                self.assertEqual(ids(result), [("collect_daily_reward", {})])
                self.assertEqual(reasons(result)["add_research 600"],
                                 "cost unknown: research 600 has no parseable @GasCost")

    def test_missing_starbux_cost_is_zero_but_unparseable_is_unknown(self):
        missing = DesignData.from_records(research_designs=[
            {"@ResearchDesignId": "600", "@RootResearchDesignId": "600", "@GasCost": "10"}])
        self.assertIn(("add_research", {"research_design_id": "600"}),
                      ids(get_policy("baseline-v1").plan(snap(research=[]), missing, PolicyConfig())))
        garbled = DesignData.from_records(research_designs=[
            {"@ResearchDesignId": "600", "@RootResearchDesignId": "600", "@GasCost": "10", "@StarbuxCost": "1,000"}])
        result = get_policy("baseline-v1").plan(snap(research=[]), garbled, PolicyConfig(allow_starbux_spend=True))
        self.assertNotIn(("add_research", {"research_design_id": "600"}), ids(result))
        self.assertEqual(reasons(result)["add_research 600"],
                         "cost unknown: research 600 has an unparseable @StarbuxCost")

    def test_starbux_spend_is_opt_in(self):
        self.assertIn("starbux spend not allowed", reasons(plan())["add_research 520"])
        self.assertIn(("add_research", {"research_design_id": "520"}), ids(plan(allow_starbux_spend=True)))

    def test_busy_research_line_is_skipped(self):
        result = plan(snap(research=[{"@ResearchDesignId": "500", "@ResearchState": "Researching"}]))
        self.assertNotIn(("add_research", {"research_design_id": "501"}), ids(result))
        self.assertIn("already researching", reasons(result)["add_research 501"])

    def test_unobserved_sections_produce_no_actions(self):
        result = plan(snap(rooms=None, research=None, resources=None))
        self.assertEqual(ids(result), [])
        self.assertEqual(reasons(result)["rooms"], "rooms section not observed")
        self.assertEqual(reasons(result)["research"], "research section not observed")
        self.assertIn("resources section was not observed", reasons(result)["collect_daily_reward"])

    def test_unknown_balance_blocks_priced_actions(self):
        result = plan(snap(resources={"minerals": "1000"}))
        self.assertEqual(reasons(result)["upgrade_room r2"], "gas balance not observed")

    def test_training_is_reported_as_not_modelled(self):
        self.assertEqual(reasons(plan())["add_training"], "crew training is not modelled by baseline-v1")

    def test_max_requests_truncates_and_reports(self):
        result = plan(max_requests=2)
        self.assertEqual(len(result.requests), 2)
        self.assertIn("over max_requests (2)", reasons(result)["collect_daily_reward"])

    def test_plan_is_deterministic(self):
        self.assertEqual(plan(), plan())

    def test_unknown_policy_is_refused(self):
        with self.assertRaises(KeyError):
            get_policy("greedy-v9")


if __name__ == "__main__":
    unittest.main()
