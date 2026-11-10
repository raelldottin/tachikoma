"""Tests for the run_pvp_battle catalog action and its journaled evidence tiers. Fake client, no network."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class FakeBattleClient:
    """Only runBattleEndToEnd exists; it replaces the per-step response objects it reaches."""

    def __init__(self, steps=("create", "accept", "finalise"), result=True, raise_error=None, battle_id="4028975"):
        self.steps = steps
        self.result = result
        self.raise_error = raise_error
        self.battle_id = battle_id
        self.calls = 0
        # Stale objects from an earlier battle must not count as this call's evidence.
        self.createBattle9Result = {"stale": True}
        self.acceptBattle5Result = {"stale": True}
        self.finaliseBattle15Result = {"stale": True}
        self.lastBattleId = "1111"

    def runBattleEndToEnd(self):
        self.calls += 1
        if "create" in self.steps:
            self.createBattle9Result = {"BattleService": {"CreateBattle": {"Battle": {"@BattleId": self.battle_id}}}}
            self.lastBattleId = self.battle_id
        if "accept" in self.steps:
            self.acceptBattle5Result = {"BattleService": {}}
        if "finalise" in self.steps:
            self.finaliseBattle15Result = {"BattleService": {}}
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


if __name__ == "__main__":
    unittest.main()
