"""Regression tests for the run.py collection actions, the inter-fleet actions, fleet-v1 and the client guards added with them.

Synthetic snapshots and design data only; all Pixel Starships network traffic is mocked.
"""

import unittest
from unittest.mock import MagicMock

from sdk.actions import CATALOG, Idempotency, ReconciliationStatus, check_preconditions, get_spec
from sdk.client import Client
from sdk.device import Device
from sdk.policy import PolicyConfig, get_policy
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN
from tests.test_action_catalog import outcome, snap
from tests.test_progression_policy import DESIGN, ids, reasons
from tests.test_progression_policy import snap as policy_snap

V, N, C, U = (
    ReconciliationStatus.VERIFIED,
    ReconciliationStatus.NOT_OBSERVED,
    ReconciliationStatus.CONTRADICTED,
    ReconciliationStatus.UNKNOWN,
)
FLEET_PARAMS = {
    "fleet_pool_resources": {"action": "contribute", "resource_type": "gas", "amount": 10},
    "fleet_marketplace_sync": {},
    "fleet_share_crew_xp": {"source_account": "alpha", "target_account": "beta", "character_id": "c1"},
}
SETTINGS = {
    "SettingService": {
        "GetLatestSetting": {
            "Setting": {
                "@RoomDesignVersion": "11",
                "@TaskDesignVersion": "22",
                "@TrainingDesignVersion": "33",
                "@ResearchDesignVersion": "44",
            }
        }
    }
}


class OptionBActionTests(unittest.TestCase):
    def test_task_reward_and_markers_are_runnable_when_resources_are_observed(self):
        # Their preconditions once named "tasks" and "markers" sections no snapshot ever records.
        for action_id in ("collect_task_reward", "collect_available_markers"):
            with self.subTest(action=action_id):
                self.assertEqual(check_preconditions(get_spec(action_id), snap(), {}), [])
                self.assertTrue(check_preconditions(get_spec(action_id), snap(resources=None), {}))

    def test_grab_flying_starbux_can_run_more_than_once_a_day(self):
        self.assertIs(get_spec("grab_flying_starbux").idempotency, Idempotency.SAFE_TO_RETRY)

    def test_starbux_reconciliation(self):
        self.assertIs(outcome("grab_flying_starbux", {"resources": {"minerals": "100", "gas": "100",
                                                                     "starbux": "8"}}).status, V)
        result = outcome("grab_flying_starbux", {"resources": {"minerals": "100", "gas": "100", "starbux": "2"}})
        self.assertIs(result.status, C)
        self.assertEqual(result.reason, "starbux decreased by 3")

    def test_collections_reuse_the_existing_postconditions(self):
        self.assertIs(outcome("collect_available_markers", {"resources": {"minerals": "150", "gas": "100",
                                                                           "starbux": "5"}}).status, V)
        self.assertIs(outcome("collect_mining_drones", {}).status, N)
        self.assertIs(outcome("collect_task_reward", {"inventory": [{"@ItemDesignId": "900",
                                                                     "@Quantity": "2"}]}).status, V)


class OptionCActionTests(unittest.TestCase):
    def test_fleet_effects_are_never_verified_from_a_snapshot(self):
        gained = {"resources": {"minerals": "500", "gas": "500", "starbux": "50"}}
        for action_id, params in FLEET_PARAMS.items():
            with self.subTest(action=action_id):
                self.assertIs(outcome(action_id, {}, params).status, U)
                self.assertIs(outcome(action_id, gained, params).status, U)

    def test_fleet_battle_needs_an_observed_trophy_change(self):
        params = {"target_fleet": "rivals", "battle_type": "tournament"}
        before = {"profile": {"@Trophy": "1000"}}
        self.assertIs(outcome("fleet_battle_coordinate", {}, params, before).status, N)
        self.assertIs(outcome("fleet_battle_coordinate", {"profile": {"@Trophy": "1010"}}, params, before).status, V)
        self.assertIs(outcome("fleet_battle_coordinate", {}, params).status, U)

    def test_fleet_pool_is_not_retried_and_validates_its_params(self):
        spec = get_spec("fleet_pool_resources")
        self.assertIs(spec.idempotency, Idempotency.NOT_IDEMPOTENT)
        self.assertEqual(check_preconditions(spec, snap(), FLEET_PARAMS["fleet_pool_resources"]), [])
        for bad in ({"action": "steal"}, {"resource_type": "crew"}, {"amount": -1}, {"amount": True}):
            with self.subTest(bad=bad):
                params = {**FLEET_PARAMS["fleet_pool_resources"], **bad}
                self.assertTrue(check_preconditions(spec, snap(), params))


SHIP = {"@ShipLevel": "3"}


class FleetV1PolicyTests(unittest.TestCase):
    def test_wraps_baseline_v1_in_run_py_order(self):
        config = PolicyConfig()
        baseline = get_policy("baseline-v1").plan(policy_snap(ship=SHIP), DESIGN, config)
        fleet = get_policy("fleet-v1").plan(policy_snap(ship=SHIP), DESIGN, config)
        self.assertEqual(fleet.policy, "fleet-v1")
        self.assertEqual(
            ids(fleet),
            [("grab_flying_starbux", {}), ("collect_task_reward", {})] + ids(baseline)
            + [("collect_available_markers", {}), ("collect_mining_drones", {})],
        )
        self.assertEqual(reasons(fleet)["add_training"], "crew training is not modelled by fleet-v1")
        self.assertEqual([item.candidate for item in fleet.rejected].count("add_training"), 1)

    def test_max_requests_caps_the_whole_plan(self):
        fleet = get_policy("fleet-v1").plan(policy_snap(ship=SHIP), DESIGN, PolicyConfig(max_requests=3))
        self.assertEqual(len(fleet.requests), 3)
        self.assertIn("over max_requests (3)", reasons(fleet)["collect_mining_drones"])

    def test_unobserved_sections_are_rejected_not_planned(self):
        fleet = get_policy("fleet-v1").plan(policy_snap(resources=None, ship=SHIP), DESIGN, PolicyConfig())
        planned = {action_id for action_id, _ in ids(fleet)}
        self.assertFalse(planned & {"grab_flying_starbux", "collect_task_reward", "collect_available_markers",
                                    "collect_mining_drones"})
        self.assertIn("resources section was not observed", reasons(fleet)["collect_task_reward"])


class ClientGuardTests(unittest.TestCase):
    def setUp(self):
        device = Device(language="en")
        device.key = "00000000-0000-0000-0000-000000000000"
        self.client = Client(device=device)
        self.client.accessToken = SYNTHETIC_ACCESS_TOKEN
        self.client.info = {"@Name": "TestCaptain"}
        self.client.user = MagicMock(id="123", isAuthorized=True)
        self.client.request = MagicMock(return_value=MagicMock(content=b"<RoomService/>"))
        self.client.getLatestVersion3 = MagicMock()

    def test_fleet_stubs_do_not_claim_success(self):
        self.assertIs(self.client.fleetPoolResources("contribute", "gas", 10), False)
        self.assertIs(self.client.fleetMarketplaceSync(), False)
        self.assertIs(self.client.fleetBattleCoordinate("rivals", "tournament"), False)
        self.assertIs(self.client.fleetShareCrewXp("alpha", "beta", "c1"), False)

    def test_design_lists_return_false_on_an_error_settings_payload(self):
        self.client.latestVersion = {"SettingService": {"GetLatestVersion3": {"@errorMessage": "x"}}}
        for name in ("listRoomDesigns2", "listAllTaskDesigns2", "listAllTrainingDesigns2", "listActionTypes2",
                     "listConditionTypes2", "listAllCharacterDesigns2", "listAllResearchDesigns2",
                     "listAllDesigns4"):
            with self.subTest(method=name):
                self.assertIs(getattr(self.client, name)(), False)
        self.client.request.assert_not_called()

    def test_missing_settings_are_fetched_once_per_call(self):
        def load():
            self.client.latestVersion = SETTINGS

        self.client.getLatestVersion3 = MagicMock(side_effect=load)
        self.assertIs(self.client.listRoomDesigns2(), True)
        self.client.getLatestVersion3.assert_called_once()

    def test_room_designs_report_success_and_lists_use_their_own_version(self):
        self.client.latestVersion = SETTINGS
        self.assertIs(self.client.listRoomDesigns2(), True)
        self.assertIs(self.client.listAllTaskDesigns2(), True)
        self.assertIn("designVersion=22", self.client.request.call_args.args[0])
        self.assertIs(self.client.listAllTrainingDesigns2(), True)
        self.assertIn("designVersion=33", self.client.request.call_args.args[0])

    def test_research_designs_fail_cleanly_on_an_empty_response(self):
        self.client.latestVersion = SETTINGS
        self.client.request.return_value = MagicMock(content=b"")
        self.assertIs(self.client.listAllResearchDesigns2(), False)

    def test_empty_ship_service_is_not_a_crash(self):
        for body in (b"<ShipService/>", b"<ShipService><GetShipByUserId/></ShipService>"):
            with self.subTest(body=body):
                self.client.request.return_value = MagicMock(content=body)
                self.assertIs(self.client.getShipByUserId(), False)



class RemainingReviewFindingTests(unittest.TestCase):
    """Regression coverage for the PR #272 findings fixed after the first review pass."""

    def test_marketplace_sync_is_not_retried(self):
        self.assertIs(get_spec("fleet_marketplace_sync").idempotency, Idempotency.NOT_IDEMPOTENT)

    def test_only_the_resource_collection_is_canary_safe(self):
        self.assertEqual([spec.action_id for spec in CATALOG.values() if spec.canary_safe], ["collect_all_resources"])

    def test_grab_flying_starbux_runs_without_an_observed_starbux_balance(self):
        # The runner's snapshots never carry starbux (only the plan's does), so requiring it blocked every grab.
        unobserved = {"minerals": "100", "gas": "100", "starbux": None}
        self.assertEqual(check_preconditions(get_spec("grab_flying_starbux"), snap(resources=unobserved), {}), [])
        self.assertIs(outcome("grab_flying_starbux", {}, before_overrides={"resources": unobserved}).status, U)


class MarkerCollectionResultTests(unittest.TestCase):
    def setUp(self):
        device = Device(language="en")
        device.key = "00000000-0000-0000-0000-000000000000"
        self.client = Client(device=device)
        self.client.accessToken = SYNTHETIC_ACCESS_TOKEN
        self.client.info = {"@Name": "TestCaptain"}

    def listed(self, payload):
        def fake_list():
            self.client.starSystemMarkersAndUserMarkers = payload
        self.client.listStarSystemMarkersAndUserMarkers = fake_list

    def test_a_failed_read_is_false_and_an_empty_one_is_zero(self):
        self.listed({"GalaxyService": {"UserMarkers": None}})
        self.assertIs(self.client.collectAvailableMarkers(), 0)
        self.listed({"SettingService": {"@errorMessage": "x"}})
        self.assertIs(self.client.collectAvailableMarkers(), False)
        self.client.accessToken = None
        self.assertIs(self.client.collectAvailableMarkers(), False)
        self.assertIs(self.client.collectMiningDronesWithTravel(), False)


class SettingsRetryTests(ClientGuardTests):
    def test_missing_settings_are_retried_once_per_session(self):
        self.client.getLatestVersion3 = MagicMock()  # the reply never loads
        self.assertIs(self.client.listRoomDesigns2(), False)
        self.assertIs(self.client.listAllTaskDesigns2(), False)
        self.client.getLatestVersion3.assert_called_once()

    def test_an_error_settings_payload_is_retried_once_per_session(self):
        # Login can leave an error payload rather than nothing; it, too, is fetched once more.
        self.client.latestVersion = {"SettingService": {"GetLatestVersion3": {"@errorMessage": "x"}}}

        def load():
            self.client.latestVersion = SETTINGS

        self.client.getLatestVersion3 = MagicMock(side_effect=load)
        self.assertIs(self.client.listRoomDesigns2(), True)
        self.assertIs(self.client.listAllTaskDesigns2(), True)
        self.client.getLatestVersion3.assert_called_once()


class SecondReviewFindingTests(ClientGuardTests):
    """Regression coverage for the PR #272 findings fixed after the high-effort review."""

    def test_markers_nested_under_the_method_node_are_collected(self):
        # The reply nests UserMarkers under <ListStarSystemMarkersAndUserMarkers>, as findMiningDrones reads it.
        def fake_list():
            self.client.starSystemMarkersAndUserMarkers = {"GalaxyService": {"ListStarSystemMarkersAndUserMarkers": {
                "UserMarkers": {"UserMarker": [{"@StarSystemMarkerId": "7"}, {"@StarSystemMarkerId": "8"}]}}}}

        self.client.listStarSystemMarkersAndUserMarkers = fake_list
        self.client.collectMiningDrone = MagicMock(return_value=True)
        self.assertEqual(self.client.collectAvailableMarkers(), 2)
        self.assertEqual([c.args[0] for c in self.client.collectMiningDrone.call_args_list], ["7", "8"])

    def test_drone_travel_tolerates_an_empty_ship_reply(self):
        drone = {"markerId": "9", "starSystemId": "5"}
        self.client.dronesCollected = {}
        self.client.findMiningDrones = MagicMock(return_value=[drone])

        def empty_ship(*args, **kwargs):
            self.client.shipByUserId = {"ShipService": None}
            return False

        self.client.getShipByUserId = MagicMock(side_effect=empty_ship)
        self.client.goToStarSystem = MagicMock(return_value="2026-11-21T00:00:00")
        self.client.waitForArrival = MagicMock(return_value=True)
        self.client.collectMiningDrone = MagicMock(return_value=True)
        self.assertEqual(self.client.collectMiningDronesWithTravel(), 1)
        self.client.goToStarSystem.assert_called_once_with("5")

    def test_character_designs_use_their_own_version_and_fail_on_an_empty_reply(self):
        self.client.latestVersion = {"SettingService": {"GetLatestSetting": {"Setting": {
            "@CharacterDesignVersion": "55"}}}}
        self.client.request.return_value = MagicMock(content=b"<CharacterService/>")
        self.assertIs(self.client.listAllCharacterDesigns2(), True)
        self.assertIn("CharacterService/ListAllCharacterDesigns2", self.client.request.call_args.args[0])
        self.assertIn("designVersion=55", self.client.request.call_args.args[0])
        self.client.request.return_value = MagicMock(content=b"")
        self.assertIs(self.client.listAllCharacterDesigns2(), False)


if __name__ == "__main__":
    unittest.main()
