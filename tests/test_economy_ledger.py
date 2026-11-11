"""Tests for sdk.economy.ledger: currency and item flows attributed to journaled actions. Offline."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.economy import build_ledger
from sdk.execution import ActionRunner
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.state import AccountMismatchError, SnapshotInputError, build_snapshot

T0 = datetime(2027, 2, 1, 9, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def snap(minutes, minerals="100", gas="50", starbux=None, items=(("900", "1"),), alias="alpha"):
    resources = {"minerals": minerals, "gas": gas, "starbux": starbux}
    inventory = None if items is None else [{"@ItemDesignId": i, "@Quantity": q} for i, q in items]
    return build_snapshot(alias, at(minutes), "synthetic", resources=resources, inventory=inventory)


def event(start, end, action_id="collect_all_resources", alias="alpha", attempted=True):
    return GameplayEvent.create(account_alias=alias, action_id=action_id, started_at=at(start), finished_at=at(end),
                                outcome="verified" if attempted else "skipped", reason="synthetic",
                                attempted=attempted)


class LedgerTests(unittest.TestCase):
    def test_entry_inside_one_action_window_is_attributed(self):
        ledger = build_ledger("alpha", [snap(1, minerals="100"), snap(2, minerals="160")], [event(0, 3)])
        [entry] = ledger.entries
        self.assertEqual((entry.attribution, entry.action_id), ("attributed", "collect_all_resources"))
        self.assertEqual(entry.currency, {"minerals": 60, "gas": 0, "starbux": None})
        self.assertTrue(entry.changed)

    def test_action_inside_a_wide_interval_is_attributed(self):
        ledger = build_ledger("alpha", [snap(0), snap(60, gas="20")], [event(10, 11, "upgrade_room")])
        self.assertEqual(ledger.entries[0].action_id, "upgrade_room")

    def test_partial_overlap_and_gaps_are_unattributed(self):
        # The next action starts before its own before-snapshot (10) and has its after-snapshot (15) in the
        # series: the gap [0, 10] is not its effect.
        ledger = build_ledger("alpha", [snap(0), snap(10, minerals="130"), snap(15, minerals="130")],
                              [event(9, 20)])
        self.assertEqual([e.attribution for e in ledger.entries], ["unattributed", "attributed"])
        self.assertEqual(ledger.unattributed().gained, {"minerals": 30})
        # An action that started before the interval's start snapshot and ends inside it is not a candidate.
        ledger = build_ledger("alpha", [snap(0), snap(10, minerals="130")], [event(-5, 3)])
        self.assertEqual(ledger.entries[0].attribution, "unattributed")

    def test_action_started_inside_and_overlapping_the_end_is_a_candidate(self):
        # Regression: an action that started inside the interval and ran past its end was ignored, so the
        # change was attributed to the only other action.
        ledger = build_ledger("alpha", [snap(0, minerals="1000"), snap(10, minerals="700")],
                              [event(2, 4, "collect_daily_reward"), event(8, 12, "upgrade_room")])
        [entry] = ledger.entries
        self.assertEqual((entry.attribution, entry.action_id, len(entry.candidates)), ("ambiguous", None, 2))
        self.assertEqual(ledger.by_action(), {})
        alone = build_ledger("alpha", [snap(0, minerals="1000"), snap(10, minerals="700")],
                             [event(8, 12, "upgrade_room")])
        self.assertEqual((alone.entries[0].attribution, alone.entries[0].action_id), ("attributed", "upgrade_room"))

    def test_several_candidates_are_ambiguous(self):
        ledger = build_ledger("alpha", [snap(0), snap(60, minerals="90")],
                              [event(10, 11), event(20, 21, "add_research")])
        [entry] = ledger.entries
        self.assertEqual((entry.attribution, entry.action_id, len(entry.candidates)), ("ambiguous", None, 2))
        self.assertEqual(ledger.ambiguous().spent, {"minerals": 10})

    def test_unobserved_values_stay_none(self):
        ledger = build_ledger("alpha", [snap(0, minerals=None, items=None), snap(5, minerals="200", items=None)])
        [entry] = ledger.entries
        self.assertIsNone(entry.currency["minerals"])
        self.assertIsNone(entry.items)
        self.assertEqual(ledger.unattributed().samples, {"gas": 1})

    def test_item_changes_and_totals_by_action(self):
        ledger = build_ledger(
            "alpha",
            [snap(0), snap(1, minerals="150", items=(("900", "3"),)), snap(2, minerals="150", items=(("900", "3"),)),
             snap(3, minerals="120", items=(("900", "3"), ("901", "1")))],
            [event(0, 1.5), event(1.9, 3.1, "upgrade_room")],
        )
        totals = ledger.by_action()
        self.assertEqual(totals["collect_all_resources"].gained, {"minerals": 50})
        self.assertEqual(totals["collect_all_resources"].items, {"900": 2})
        self.assertEqual(totals["upgrade_room"].spent, {"minerals": 30})
        self.assertEqual(totals["upgrade_room"].items, {"901": 1})
        self.assertFalse(ledger.entries[1].changed)

    def test_skipped_and_foreign_events_are_ignored(self):
        ledger = build_ledger("alpha", [snap(1), snap(2)], [event(0, 3, attempted=False), event(0, 3, alias="bravo")])
        self.assertEqual(ledger.entries[0].attribution, "unattributed")

    def test_foreign_snapshot_and_bad_order_are_refused(self):
        with self.assertRaises(AccountMismatchError):
            build_ledger("alpha", [snap(0), snap(1, alias="bravo")])
        with self.assertRaises(SnapshotInputError):
            build_ledger("alpha", [snap(5), snap(1)])
        self.assertEqual(build_ledger("alpha", [snap(0)]).entries, ())


class RunnerLedgerTests(unittest.TestCase):
    def test_runner_snapshots_are_attributed_to_their_action(self):
        state = {"minerals": "100"}

        class Client:
            def collectAllResources(self):
                state["minerals"] = "175"
                return True

        ticks = iter(at(n / 60) for n in range(1000))
        observed = []

        def provider():
            snapshot = build_snapshot("alpha", next(ticks), "synthetic",
                                      resources={"minerals": state["minerals"], "gas": "50", "starbux": None})
            observed.append(snapshot)
            return snapshot

        with tempfile.TemporaryDirectory() as tmp:
            journal = GameplayJournal(Path(tmp) / "events.jsonl")
            ActionRunner("alpha", Client(), provider, journal, clock=lambda: next(ticks)).run("collect_all_resources")
            ledger = build_ledger("alpha", observed, journal.read())
        self.assertEqual(ledger.by_action()["collect_all_resources"].gained, {"minerals": 75})

    def test_consecutive_runs_attribute_each_run_and_leave_the_gap_unattributed(self):
        state = {"minerals": "100"}

        class Client:
            def collectAllResources(self):
                state["minerals"] = str(int(state["minerals"]) + 75)
                return True

        ticks = iter(at(n / 60) for n in range(1000))
        observed = []

        def provider():
            snapshot = build_snapshot("alpha", next(ticks), "synthetic",
                                      resources={"minerals": state["minerals"], "gas": "50", "starbux": None})
            observed.append(snapshot)
            return snapshot

        with tempfile.TemporaryDirectory() as tmp:
            journal = GameplayJournal(Path(tmp) / "events.jsonl")
            runner = ActionRunner("alpha", Client(), provider, journal, clock=lambda: next(ticks))
            runner.run("collect_all_resources")
            state["minerals"] = str(int(state["minerals"]) + 5)  # outside any action
            runner.run("collect_all_resources")
            events = journal.read()
            ledger = build_ledger("alpha", observed, events)
        self.assertEqual([e.attribution for e in ledger.entries], ["attributed", "unattributed", "attributed"])
        self.assertEqual([e.attempt_id for e in ledger.entries if e.attempt_id],
                         [events[0].attempt_id, events[1].attempt_id])
        self.assertEqual(ledger.by_action()["collect_all_resources"].gained, {"minerals": 150})
        self.assertEqual(ledger.unattributed().gained, {"minerals": 5})


if __name__ == "__main__":
    unittest.main()
