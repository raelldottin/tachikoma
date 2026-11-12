"""Tests for the run_pvp_battle catalog action and its journaled evidence tiers. Fake client, no network."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sdk.actions import CATALOG, Idempotency, ReconciliationStatus, reconcile
from sdk.actions.catalog import BATTLE_EVIDENCE_TIERS
from sdk.execution import ActionRunner, UncertainPriorAttemptError
from sdk.journal import GameplayJournal, read_events
from sdk.state import build_snapshot

T0 = datetime(2027, 1, 4, 12, 0, tzinfo=timezone.utc)
SHIP = {"@ShipLevel": "8"}


def snap(trophies=None, ship=SHIP, at=T0):
    profile = {"@Trophy": str(trophies)} if trophies is not None else None
    return build_snapshot("alpha", at, "synthetic", profile=profile, ship=ship)


def battle_list(*battles):
    """A ListPvPBattles2 response shaped like the pss1 capture.

    One (battle id, attacker, defender[, outcome type]) tuple per battle; the outcome defaults to "Attacker Won".
    """
    return {"BattleService": {"ListPvPBattles": {"Battles": {"Battle": [
        {"@BattleId": battle[0], "@AttackingUserId": battle[1], "@DefendingUserId": battle[2],
         "@OutcomeType": battle[3] if len(battle) > 3 else "Attacker Won",
         "@WinTrophyResult": "40", "@LoseTrophyResult": "1",
         "@DefendingShipDesignId": "12", "@DefendingUserTrophy": "0", "@BattleDate": "2027-01-04T12:00:00"}
        for battle in battles
    ]}}}}


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class FakeBattleClient:
    """Only runBattleEndToEnd exists; it replaces the per-step response objects it reaches."""

    def __init__(self, steps=("create", "accept", "finalise"), result=True, raise_error=None, battle_id="4028975",
                 battles_after=None, id_extracted=True):
        self.steps = steps
        self.result = result
        self.raise_error = raise_error
        self.battle_id = battle_id
        # A ListPvPBattles2 response read after the battle (xmltodict shape), or None for no read.
        self.battles_after = battles_after
        # False mimics Client.createBattle9 replacing its result when no battle id could be read from it.
        self.id_extracted = id_extracted
        self.calls = 0
        self.user = SimpleNamespace(id="USER-A")
        self.pvpBattles = battle_list(("BATTLEID-OLD", "USER-A", "USER-B"))  # read before the call
        # Stale objects from an earlier battle must not count as this call's evidence.
        self.createBattle9Result = {"stale": True}
        self.acceptBattle5Result = {"stale": True}
        self.finaliseBattle15Result = {"stale": True}
        self.lastBattleId = "1111"

    def runBattleEndToEnd(self):
        self.calls += 1
        if "create" in self.steps:
            self.createBattle9Result = {"BattleService": {"CreateBattle": {"Battle": {"@BattleId": self.battle_id}}}}
            if self.id_extracted:
                self.lastBattleId = self.battle_id
        if "accept" in self.steps:
            self.acceptBattle5Result = {"BattleService": {}}
        if "finalise" in self.steps:
            self.finaliseBattle15Result = {"BattleService": {}}
        if self.battles_after is not None:
            self.pvpBattles = self.battles_after
        if self.raise_error:
            raise self.raise_error
        return self.result


class BattleSpecTests(unittest.TestCase):
    def test_spec_shape(self):
        spec = CATALOG["run_pvp_battle"]
        self.assertEqual(spec.client_method, "runBattleEndToEnd")
        self.assertIs(spec.idempotency, Idempotency.NOT_IDEMPOTENT)
        self.assertEqual(BATTLE_EVIDENCE_TIERS[0], "request-sent")
        self.assertEqual(BATTLE_EVIDENCE_TIERS[-1], "outcome-observed")

    def test_outcome_needs_an_observed_trophy_change(self):
        spec = CATALOG["run_pvp_battle"]
        cases = [
            (snap(), snap(), ReconciliationStatus.UNKNOWN),
            (snap(1500), snap(), ReconciliationStatus.UNKNOWN),
            (snap(1500), snap(1512), ReconciliationStatus.VERIFIED),
            (snap(1500), snap(1492), ReconciliationStatus.VERIFIED),
            (snap(1500), snap(1500), ReconciliationStatus.NOT_OBSERVED),
        ]
        for before, after, expected in cases:
            with self.subTest(before=before.trophies, after=after.trophies):
                self.assertIs(reconcile(spec, before, after, response_ok=True).status, expected)

    def test_http_success_alone_is_never_verified(self):
        result = reconcile(CATALOG["run_pvp_battle"], snap(), snap(), response_ok=True)
        self.assertIsNot(result.status, ReconciliationStatus.VERIFIED)


class BattleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_battle(self, client, snapshots, **kwargs):
        feed = iter(snapshots)
        runner = ActionRunner("alpha", client, lambda: next(feed), self.journal, clock=FakeClock())
        return runner.run("run_pvp_battle", **kwargs)

    def test_finalised_without_observed_trophies_is_unknown(self):
        client = FakeBattleClient()
        result = self.run_battle(client, [snap(), snap()])
        self.assertEqual(result.outcome, "unknown")
        evidence = result.event.evidence
        self.assertEqual(evidence["evidence_tier"], "finalised")
        self.assertEqual(evidence["battle_id"], "4028975")
        self.assertFalse(evidence["steps"]["outcome-observed"])
        self.assertFalse(evidence["client_reported_outcome_trusted"])

    def test_observed_trophy_change_reaches_outcome_observed(self):
        result = self.run_battle(FakeBattleClient(), [snap(1500), snap(1512)])
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(result.event.evidence["evidence_tier"], "outcome-observed")
        self.assertEqual(result.event.evidence["steps"],
                         {"battle-created": True, "battle-accepted": True, "finalised": True, "outcome-observed": True})

    def test_stale_step_objects_do_not_count(self):
        client = FakeBattleClient(steps=(), result=False)
        result = self.run_battle(client, [snap(1500), snap(1500)])
        self.assertEqual(result.event.evidence["evidence_tier"], "request-sent")
        self.assertIsNone(result.event.evidence["battle_id"])
        self.assertEqual(result.outcome, "not-observed")

    def test_partial_flow_records_highest_step(self):
        client = FakeBattleClient(steps=("create",), battle_id="0")
        result = self.run_battle(client, [snap(), snap()])
        self.assertEqual(result.event.evidence["evidence_tier"], "battle-created")
        self.assertIsNone(result.event.evidence["battle_id"])  # the flow's dummy id is not a battle id

    def test_exception_is_unknown_with_evidence_and_blocks_retry(self):
        client = FakeBattleClient(steps=("create",), raise_error=TimeoutError("synthetic"))
        result = self.run_battle(client, [snap(1500), snap(1500)])
        self.assertEqual((result.outcome, result.event.error_class), ("unknown", "TimeoutError"))
        self.assertEqual(result.event.evidence["evidence_tier"], "battle-created")
        with self.assertRaises(UncertainPriorAttemptError):
            self.run_battle(FakeBattleClient(), [snap(1500), snap(1500)])

    def test_failed_after_snapshot_keeps_evidence(self):
        def snapshots():
            yield snap(1500)
            raise ConnectionError("synthetic read failure")

        feed = snapshots()
        runner = ActionRunner("alpha", FakeBattleClient(), lambda: next(feed), self.journal, clock=FakeClock())
        result = runner.run("run_pvp_battle")
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.event.evidence["evidence_tier"], "finalised")

    def test_unobserved_ship_skips_without_calling(self):
        client = FakeBattleClient()
        result = self.run_battle(client, [snap(ship=None)])
        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(client.calls, 0)

    def test_dry_run_does_not_call(self):
        client = FakeBattleClient()
        result = self.run_battle(client, [snap(1500)], dry_run=True)
        self.assertEqual((result.outcome, client.calls), ("skipped", 0))
        self.assertEqual(result.event.evidence, {})

    def test_evidence_round_trips_through_the_journal(self):
        self.run_battle(FakeBattleClient(), [snap(1500), snap(1512)])
        [event] = read_events(self.journal.path)
        self.assertEqual(event.evidence["evidence_tier"], "outcome-observed")
        line = json.loads(self.journal.path.read_text().splitlines()[0])
        self.assertNotIn("stale", json.dumps(line))  # raw responses are never journaled

    def test_journal_lines_without_evidence_still_read(self):
        self.run_battle(FakeBattleClient(), [snap(1500), snap(1512)])
        payload = json.loads(self.journal.path.read_text().splitlines()[0])
        del payload["evidence"]
        self.journal.path.write_text(json.dumps(payload) + "\n")
        [event] = read_events(self.journal.path)
        self.assertEqual(event.evidence, {})


class BattleRecordEvidenceTests(unittest.TestCase):
    """S03-R5: which observed battle record, or battle step, supports a journaled outcome."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = GameplayJournal(Path(self.tmp.name) / "events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_battle(self, client, snapshots):
        feed = iter(snapshots)
        return ActionRunner("alpha", client, lambda: next(feed), self.journal, clock=FakeClock()).run("run_pvp_battle")

    # Verifies: S03-R5
    def test_trophy_change_without_record_or_finalise_is_unknown(self):
        unrelated = battle_list(("BATTLEID-OTHER", "USER-A", "USER-B"))
        for battles_after in (None, unrelated):
            with self.subTest(fresh_list=battles_after is not None):
                client = FakeBattleClient(steps=("create", "accept"), battles_after=battles_after)
                result = self.run_battle(client, [snap(1500), snap(1512)])
                self.assertEqual(result.outcome, "unknown")
                self.assertFalse(result.event.evidence["outcome_supported"])
                self.assertNotIn("battle_record", result.event.evidence)
                self.journal.path.unlink()

    # Verifies: S03-R5
    def test_matching_attack_record_verifies(self):
        client = FakeBattleClient(steps=("create", "accept"),
                                  battles_after=battle_list(("4028975", "USER-A", "USER-B")))
        result = self.run_battle(client, [snap(1500), snap(1540)])
        self.assertEqual(result.outcome, "verified")
        evidence = result.event.evidence
        self.assertEqual(evidence["evidence_tier"], "outcome-observed")
        self.assertTrue(evidence["outcome_supported"])
        self.assertEqual(evidence["battle_record"]["role"], "attacker")
        self.assertEqual(evidence["battle_record"]["trophy_change"], 40)
        self.assertNotIn("USER-A", json.dumps(evidence))  # user ids are never journaled

    # Verifies: S03-R5
    def test_matching_defense_record_never_verifies(self):
        client = FakeBattleClient(battles_after=battle_list(("4028975", "USER-B", "USER-A")))
        result = self.run_battle(client, [snap(1500), snap(1499)])  # finalised, trophies changed
        self.assertEqual(result.outcome, "unknown")
        evidence = result.event.evidence
        self.assertFalse(evidence["outcome_supported"])
        self.assertFalse(evidence["steps"]["outcome-observed"])
        self.assertEqual(evidence["battle_record"]["role"], "defender")
        self.assertIn("defense never verifies an attack", result.event.reason)

    # Verifies: S03-R5
    def test_battle_list_read_before_the_call_is_not_observed_after_it(self):
        client = FakeBattleClient(steps=("create", "accept"))
        client.pvpBattles = battle_list(("4028975", "USER-A", "USER-B"))  # stale object, never replaced
        result = self.run_battle(client, [snap(1500), snap(1540)])
        self.assertEqual(result.outcome, "unknown")
        self.assertNotIn("battle_record", result.event.evidence)

    # Verifies: S03-R5
    def test_previous_battle_id_never_matches_this_call(self):
        # The battle was created but its id could not be read, so lastBattleId still holds the
        # previous battle's id; that battle's attack record must not verify this call's trophy change.
        client = FakeBattleClient(steps=("create", "accept"), id_extracted=False,
                                  battles_after=battle_list(("4028975", "USER-A", "USER-B")))
        client.lastBattleId = "4028975"
        result = self.run_battle(client, [snap(1500), snap(1540)])
        self.assertEqual(result.outcome, "unknown")
        evidence = result.event.evidence
        self.assertIsNone(evidence["battle_id"])
        self.assertNotIn("battle_record", evidence)
        self.assertFalse(evidence["outcome_supported"])

    # Verifies: S03-R5
    def test_undecided_attack_record_does_not_support(self):
        client = FakeBattleClient(battles_after=battle_list(("4028975", "USER-A", "USER-B", "Draw")))
        result = self.run_battle(client, [snap(1500), snap(1512)])  # finalised, trophies changed
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.event.evidence["outcome_supported"])
        self.assertIn("does not say who won", result.event.reason)

    # Verifies: S03-R5
    def test_attack_record_disagreeing_with_the_trophy_change_does_not_support(self):
        client = FakeBattleClient(battles_after=battle_list(("4028975", "USER-A", "USER-B")))  # won, +40
        result = self.run_battle(client, [snap(1500), snap(1490)])
        self.assertEqual(result.outcome, "unknown")
        self.assertFalse(result.event.evidence["steps"]["outcome-observed"])
        self.assertIn("disagrees in sign", result.event.reason)

    def _winner_disagrees(self, outcome, trophies, drop):
        battles = battle_list(("4028975", "USER-A", "USER-B", outcome))
        del battles["BattleService"]["ListPvPBattles"]["Battles"]["Battle"][0][drop]
        result = self.run_battle(FakeBattleClient(battles_after=battles), [snap(1500), snap(trophies)])
        self.assertEqual(result.outcome, "unknown")
        self.assertIn("winner disagrees", result.event.reason)

    # Verifies: S03-R5
    def test_lost_attack_record_never_supports_a_trophy_gain(self):
        """A record without a trophy number still fixes the sign through who won (verifier finding)."""
        self._winner_disagrees("Defender Won", 1512, "@LoseTrophyResult")

    # Verifies: S03-R5
    def test_won_attack_record_never_supports_a_trophy_loss(self):
        self._winner_disagrees("Attacker Won", 1495, "@WinTrophyResult")

    # Verifies: S03-R5
    def test_evidence_failure_after_the_battle_is_journaled_unsupported(self):
        class BrokenAfterBattle(FakeBattleClient):
            @property
            def pvpBattles(self):
                if self.calls:
                    raise RuntimeError("synthetic read failure")
                return None

            @pvpBattles.setter
            def pvpBattles(self, value):
                pass

        result = self.run_battle(BrokenAfterBattle(), [snap(1500), snap(1512)])  # finalised, trophies changed
        self.assertEqual(result.outcome, "unknown")
        evidence = result.event.evidence
        self.assertEqual(evidence["evidence_tier"], "request-sent")
        self.assertFalse(evidence["outcome_supported"])
        self.assertIsNone(evidence["battle_id"])
        self.assertNotIn("4028975", json.dumps(evidence))
        self.assertNotIn("synthetic read failure", json.dumps(evidence) + result.event.reason)

    # Verifies: S03-R5
    def test_evidence_builder_never_raises(self):
        build = CATALOG["run_pvp_battle"].evidence
        finalised = {"finalised": object()}
        for before, after, delta in ((None, finalised, None), ({}, None, None), ({}, finalised, object())):
            with self.subTest(before=before, after=after, delta=delta):
                evidence = build(before, after, ReconciliationStatus.VERIFIED, delta)
                self.assertFalse(evidence["outcome_supported"])
                self.assertIn("outcome_support_reason", evidence)

    # Verifies: S03-R5
    def test_record_without_a_known_own_id_falls_back_to_finalise(self):
        client = FakeBattleClient(battles_after=battle_list(("4028975", "USER-B", "USER-A")))
        client.user = SimpleNamespace(id="0")
        result = self.run_battle(client, [snap(1500), snap(1512)])
        self.assertEqual(result.outcome, "verified")  # finalised and trophies changed, as before
        self.assertNotIn("battle_record", result.event.evidence)


if __name__ == "__main__":
    unittest.main()
