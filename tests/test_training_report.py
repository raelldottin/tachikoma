"""Tests for sdk.reports.training: the training outcome report. Synthetic, offline."""

import hashlib
import io
import json
import os
import random
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import run  # imported here so run.py's log file opens in the repository, not a test tmpdir
from sdk.journal import GameplayEvent, GameplayJournal, JournalError, read_events
from sdk.reports.training import (
    CONTRACT_VERSION,
    CONTRACT_VIOLATIONS,
    DECISION_KIND,
    KNOWN_OUTCOMES,
    SKIP_REASON_CODES,
    TRAINING_ACTIONS,
    TRAINING_DECISION_ACTION,
    CrewIdle,
    CrewTraining,
    Decision,
    OutcomeCounts,
    TrainingObservation,
    TrainingReportError,
    build_training_report,
    crew_idle,
    crew_label,
    decision_evidence,
    is_failed_or_unknown,
    is_unknown_outcome,
    observations_from_journal,
    observations_from_snapshots,
    parse_decision,
    render_markdown,
    skip_reason,
    to_json,
    to_markdown,
    validate_decision_evidence,
)
from sdk.state.snapshot import AccountSnapshot, CrewMember, Resources, Room
from tests.synthetic_fixtures import SYNTHETIC_EMAIL
from tests.test_offline_reports import forbidden

NOW = datetime(2027, 4, 3, 12, 0, tzinfo=timezone.utc)
GOLDEN_DIR = Path(os.path.abspath(__file__)).parent / "golden" / "training_report"
SYNTHETIC_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0.SflKxwR0NTY"
_ids = iter(range(100_000))


def at(hours_ago):
    return NOW - timedelta(hours=hours_ago)


def ev(alias, action, hours_ago, outcome="verified", attempted=True, error_class=None, dry_run=False, params=None,
       attempt_id=None, reason="synthetic"):
    finish = at(hours_ago)
    return GameplayEvent.create(
        account_alias=alias, action_id=action, started_at=finish - timedelta(minutes=1), finished_at=finish,
        outcome=outcome if attempted else "skipped", reason=reason, attempted=attempted, error_class=error_class,
        dry_run=dry_run, params=params or {}, attempt_id=attempt_id or f"{alias}-{next(_ids):05d}",
    )


def decision(alias, hours_ago, cid, decision="skip", reason_code=None, in_room=True, active=False, dry_run=False,
             reason="synthetic decision", attempt_id=None, kind=DECISION_KIND, version=CONTRACT_VERSION,
             attempted=False):
    """A training_decision record; ``in_room=None`` leaves the in-room flag out, ``version=None`` the version."""
    evidence = {"kind": kind, "decision": decision, "character_id": cid, "training_active": active}
    if version is not None:
        evidence["v"] = version
    if reason_code is not None:
        evidence["reason_code"] = reason_code
    if in_room is not None:
        evidence["in_training_room"] = in_room
    finish = at(hours_ago)
    return GameplayEvent.create(
        account_alias=alias, action_id=TRAINING_DECISION_ACTION, started_at=finish - timedelta(minutes=1),
        finished_at=finish, outcome="skipped", reason=reason, attempted=attempted, dry_run=dry_run,
        evidence=evidence, attempt_id=attempt_id or f"{alias}-{next(_ids):05d}",
    )


def scenario_a(alias="alpha"):
    return [
        ev(alias, "add_training", 10), ev(alias, "add_training", 9),
        ev(alias, "add_training", 8, "not-observed"), ev(alias, "add_training", 7, "contradicted"),
        ev(alias, "add_training", 6, "unknown", error_class="TimeoutError"),
        ev(alias, "finish_training", 5), ev(alias, "finish_training", 4, "unknown"),
        ev(alias, "use_consumable", 3, params={"item_design_id": "501", "quantity": 2}),
        ev(alias, "use_consumable", 2.5, params={"item_design_id": 501}),
        ev(alias, "use_consumable", 2, "not-observed", params={"item_design_id": "777", "quantity": 5}),
        ev(alias, "add_training", 1.5, attempted=False, dry_run=True),
        ev(alias, "add_training", 1.4, attempted=False, reason="preconditions failed: x"),
        ev(alias, "add_training", 1.3, attempted=False, reason="execute gate refused: y"),
        ev(alias, "add_training", 1.2, attempted=False, error_class="UncertainPriorAttemptError"),
        ev(alias, "add_training", 1.1, attempted=False, reason="something else"),
        decision(alias, 1.0, "c1", reason_code="fatigued", in_room=None),
        decision(alias, 0.9, "c2", reason_code="fatigued", in_room=None),
        decision(alias, 0.8, "c3", reason_code="tp_cap_reached", in_room=None),
        decision(alias, 0.7, "c4", reason_code="bogus", in_room=None),
        decision(alias, 0.6, "c5", decision="start", in_room=None),
        ev(alias, "collect_all_resources", 0.5),
        ev(alias, "add_training", -1),
    ]


def label(cid, alias="alpha"):
    return crew_label(alias, cid)


def account(report, alias):
    (found,) = [row for row in report.accounts if row.account_alias == alias]
    return found


def totals_for(events, alias="alpha", **kwargs):
    return account(build_training_report(events, now=NOW, **kwargs), alias).totals


@dataclass(frozen=True)
class TPCrew(CrewMember):
    """A crew member that also carries the training points the snapshot contract reads."""

    training_points: Optional[int] = None
    training_capacity: Optional[int] = None


def member(cid, room_id, active=False, tp=None, cap=None):
    """``active`` True or False is observed training; None leaves training unobserved."""
    return TPCrew(character_id=cid, design_id=None, name=None, level=None, fatigue=None, room_id=room_id,
                  training_observed=active is not None, training_end="2027-04-04T00:00:00" if active else None,
                  training_points=tp, training_capacity=cap)


def snap(alias, hours_ago, crew, rooms=()):
    return AccountSnapshot(account_alias=alias, observed_at=at(hours_ago), source="synthetic", game_version=None,
                           hull_level=None, trophies=None, resources=Resources(), crew=tuple(crew),
                           rooms=tuple(rooms), research=(), inventory=())


K1, K2 = "char-instance-9001", "char-instance-9002"
TR_ROOM, BR_ROOM = "room-instance-4471", "room-instance-4472"
BRAVO_ROOMS = (Room(TR_ROOM, "TR", None), Room(BR_ROOM, "BR", None))


def bravo_snapshots():
    return [
        snap("bravo", 24, [member(K1, TR_ROOM, False, 10, 50), member(K2, BR_ROOM, False, 40, 40)], BRAVO_ROOMS),
        snap("bravo", 12, [member(K1, TR_ROOM, False, 10, 50), member(K2, TR_ROOM, False, 40, 40)], BRAVO_ROOMS),
        snap("bravo", 0, [member(K1, TR_ROOM, True, 15, 50), member(K2, TR_ROOM, False, 40, 40)], BRAVO_ROOMS),
    ]


class OutcomeMetricTests(unittest.TestCase):
    def test_outcome_buckets_over_attempted_training_actions(self):
        totals = totals_for(scenario_a())
        self.assertEqual(totals.outcomes, OutcomeCounts(attempted=10, verified=5, not_observed=2, contradicted=1,
                                                        unknown=2, failed=1, failed_or_unknown=2))
        self.assertEqual(totals.failed_or_unknown_rate, 0.2)

    def test_outcomes_per_action(self):
        totals = totals_for(scenario_a())
        self.assertEqual(dict(totals.by_action), {
            "add_training": OutcomeCounts(5, 2, 1, 1, 1, 1, 1),
            "finish_training": OutcomeCounts(2, 1, 0, 0, 1, 0, 1),
            "use_consumable": OutcomeCounts(3, 2, 1, 0, 0, 0, 0),
        })

    def test_action_with_only_skipped_records_is_listed_with_zero_attempts(self):
        totals = totals_for([ev("alpha", "finish_training", 1, attempted=False, dry_run=True)])
        self.assertEqual(dict(totals.by_action), {"finish_training": OutcomeCounts()})
        self.assertIsNone(totals.failed_or_unknown_rate)

    def test_any_other_attempted_outcome_counts_as_unknown_and_failed_or_unknown(self):
        odd = GameplayEvent("alpha", "add_training", "odd-1", at(1).isoformat(), at(1).isoformat(), "mystery",
                            "synthetic", True)
        self.assertEqual(totals_for([odd]).outcomes, OutcomeCounts(attempted=1, unknown=1, failed_or_unknown=1))
        self.assertEqual(totals_for([odd]).failed_or_unknown_rate, 1.0)

    def test_unknown_and_failed_or_unknown_predicates(self):
        self.assertEqual(KNOWN_OUTCOMES, ("verified", "not-observed", "contradicted"))
        for outcome, unknown in (("verified", False), ("not-observed", False), ("contradicted", False),
                                 ("unknown", True), ("mystery", True), ("Verified", True)):
            with self.subTest(outcome=outcome):
                event = ev("alpha", "add_training", 1, outcome)
                self.assertIs(is_unknown_outcome(event), unknown)
                self.assertIs(is_failed_or_unknown(event), unknown)
                self.assertIs(is_failed_or_unknown(ev("alpha", "add_training", 1, outcome, error_class="E")), True)

    def test_finishes_count_only_verified_attempted_finish_training(self):
        events = scenario_a() + [ev("alpha", "finish_training", 3.5, "contradicted"),
                                 ev("alpha", "add_training", 3.4),
                                 ev("alpha", "finish_training", 3.3, attempted=False, dry_run=True)]
        self.assertEqual(totals_for(events).finishes, 1)

    def test_dry_runs_are_counted_and_not_skips(self):
        totals = totals_for(scenario_a())
        self.assertEqual(totals.dry_runs, 1)
        # A dry run is not a skip: if it were, it would land in the histogram as "other".
        dry_run_only = totals_for([ev("alpha", "add_training", 1, attempted=False, dry_run=True)])
        self.assertEqual((dry_run_only.dry_runs, dict(dry_run_only.skip_reasons)), (1, {}))
        # A non-training dry run and a dry-run decision record are not training dry runs.
        events = [ev("alpha", "collect_all_resources", 1, attempted=False, dry_run=True),
                  decision("alpha", 1, "c1", reason_code="fatigued", dry_run=True)]
        self.assertEqual(totals_for(events).dry_runs, 0)

    def test_an_attempted_record_is_never_a_dry_run(self):
        # Docs: a dry run is a training action that was not attempted and is marked dry_run. The journal refuses
        # to write or read an attempted dry run, so this in-memory record pins the report's own rule.
        attempted_dry_run = ev("alpha", "add_training", 1, attempted=True, dry_run=True)
        with self.assertRaises(JournalError):
            attempted_dry_run.validate()
        totals = totals_for([attempted_dry_run])
        self.assertEqual((totals.dry_runs, totals.outcomes.attempted, dict(totals.skip_reasons)), (0, 1, {}))

    def test_skip_reason_histogram(self):
        self.assertEqual(dict(totals_for(scenario_a()).skip_reasons), {
            "precondition_failed": 1, "execute_gate_refused": 1, "uncertain_prior_attempt": 1, "other": 1,
            "fatigued": 2, "tp_cap_reached": 1, "unknown_reason_code": 1,
        })

    def test_skip_reason_codes_map_to_themselves(self):
        self.assertEqual(len(SKIP_REASON_CODES), 8)
        for code in SKIP_REASON_CODES:
            with self.subTest(code=code):
                self.assertEqual(skip_reason(decision("alpha", 1, "c1", reason_code=code)), code)
        self.assertEqual(skip_reason(decision("alpha", 1, "c1")), "unknown_reason_code")
        self.assertEqual(skip_reason(decision("alpha", 1, "c1", reason_code="bogus")), "unknown_reason_code")
        self.assertIsNone(skip_reason(decision("alpha", 1, "c1", decision="Skip", reason_code="fatigued")))
        self.assertIsNone(skip_reason(decision("alpha", 1, "c1", reason_code="fatigued", version=2)))
        self.assertEqual(skip_reason(decision("alpha", 1, "c1", reason_code="fatigued", version=None)), "fatigued")
        self.assertIsNone(skip_reason(decision("alpha", 1, "c1", decision="start", reason_code="fatigued")))

    def test_skip_reason_ignores_records_that_are_not_training_skips(self):
        self.assertIsNone(skip_reason(decision("alpha", 1, "c1", reason_code="fatigued", kind="other_kind")))
        self.assertIsNone(skip_reason(ev("alpha", "collect_all_resources", 1, attempted=False,
                                         reason="preconditions failed: x")))
        self.assertIsNone(skip_reason(ev("alpha", "add_training", 1)))
        self.assertIsNone(skip_reason(ev("alpha", "add_training", 1, attempted=False, dry_run=True,
                                         reason="preconditions failed: x")))
        self.assertEqual(skip_reason(ev("alpha", "use_consumable", 1, attempted=False,
                                        reason="preconditions failed: crew")), "precondition_failed")
        self.assertEqual(skip_reason(ev("alpha", "finish_training", 1, attempted=False,
                                        reason="execute gate refused: cap")), "execute_gate_refused")
        self.assertEqual(skip_reason(ev("alpha", "add_training", 1, attempted=False,
                                        error_class="AccountMismatchError")), "other")
        # The prefix must start the reason.
        self.assertEqual(skip_reason(ev("alpha", "add_training", 1, attempted=False,
                                        reason="note: preconditions failed")), "other")

    def test_dry_run_skip_decision_is_still_counted(self):
        totals = totals_for([decision("alpha", 1, "c1", reason_code="fatigued", dry_run=True)])
        self.assertEqual(dict(totals.skip_reasons), {"fatigued": 1})

    def test_consumables_used(self):
        self.assertEqual(dict(totals_for(scenario_a()).consumables_used), {"501": 3})
        cases = ((({"item_design_id": "9", "quantity": "4"}), {"9": 4}),
                 (({"item_design_id": "9", "quantity": 3}), {"9": 3}),
                 (({"item_design_id": "9", "quantity": 0}), {"9": 1}),
                 (({"item_design_id": "9", "quantity": "x"}), {"9": 1}),
                 (({"item_design_id": "9", "quantity": "-2"}), {"9": 1}),
                 (({"item_design_id": "9", "quantity": "\u00b2"}), {"9": 1}),
                 # Only whole numbers written as an int or digit string count (docs: training-report.md).
                 (({"item_design_id": "9", "quantity": 2.0}), {"9": 1}),
                 (({"item_design_id": "9", "quantity": "2.0"}), {"9": 1}),
                 (({"item_design_id": "9", "quantity": True}), {"9": 1}),
                 (({"quantity": 2}), {"unknown": 2}),
                 (({"item_design_id": ""}), {"unknown": 1}))
        for params, expected in cases:
            with self.subTest(params=params):
                totals = totals_for([ev("alpha", "use_consumable", 1, params=params)])
                self.assertEqual(dict(totals.consumables_used), expected)
        unverified = [ev("alpha", "use_consumable", 1, "unknown", params={"item_design_id": "9"}),
                      ev("alpha", "use_consumable", 1, attempted=False, params={"item_design_id": "9"}),
                      ev("alpha", "add_training", 1, params={"item_design_id": "9"})]
        self.assertEqual(dict(totals_for(unverified).consumables_used), {})

    def test_records_and_window(self):
        row = account(build_training_report(scenario_a(), now=NOW), "alpha")
        self.assertEqual(row.records, 20)
        self.assertEqual(row.window_start, at(10).isoformat())
        self.assertEqual(row.window_end, at(0.6).isoformat())

    def test_event_exactly_at_now_counts_and_later_is_ignored(self):
        on_time = totals_for([ev("alpha", "add_training", 0)])
        self.assertEqual(on_time.outcomes.attempted, 1)
        late = build_training_report([ev("alpha", "add_training", -1 / 3600)], now=NOW)
        self.assertEqual(late.accounts, ())

    def test_account_without_training_is_listed_with_zeros(self):
        report = build_training_report(scenario_a() + [ev("bravo", "collect_all_resources", 1)], now=NOW)
        self.assertEqual([row.account_alias for row in report.accounts], ["alpha", "bravo"])
        row = account(report, "bravo")
        self.assertEqual((row.records, row.window_start, row.window_end, row.observations, row.snapshots),
                         (0, None, None, 0, 0))
        self.assertEqual(row.totals.outcomes, OutcomeCounts())
        self.assertEqual(dict(row.totals.by_action), {})
        self.assertEqual((row.totals.failed_or_unknown_rate, row.totals.idle_crew_hours, row.totals.tp_gained,
                          row.totals.crew_at_tp_cap), (None, None, None, None))
        self.assertEqual((row.totals.finishes, row.totals.dry_runs), (0, 0))
        self.assertEqual((dict(row.totals.skip_reasons), dict(row.totals.consumables_used)), ({}, {}))
        self.assertEqual(dict(row.totals.contract_violations), {})
        self.assertEqual(row.crew, ())

    def test_rate_is_rounded_to_four_places(self):
        events = [ev("alpha", "add_training", 1, error_class="HTTPError"), ev("alpha", "add_training", 2),
                  ev("alpha", "add_training", 3)]
        self.assertEqual(totals_for(events).failed_or_unknown_rate, 0.3333)


def with_evidence(record, **changes):
    """``record`` with evidence fields changed; a value of ``...`` removes the field."""
    evidence = {key: value for key, value in {**record.evidence, **changes}.items() if value is not ...}
    return GameplayEvent(**{**record.__dict__, "evidence": evidence})


def violations_of(events, alias="alpha"):
    return dict(totals_for(events, alias).contract_violations)


class DecisionContractTests(unittest.TestCase):
    def valid(self, **kwargs):
        return decision("alpha", 1, "c1", reason_code="fatigued", **kwargs)

    def test_contract_constants(self):
        self.assertEqual(CONTRACT_VERSION, 1)
        self.assertEqual(CONTRACT_VIOLATIONS, (
            "missing_kind", "bad_version", "bad_decision", "missing_character_id", "non_bool_in_room",
            "bad_training_active", "unknown_reason_code", "reason_code_on_start", "decision_attempted",
            "action_missing_param"))

    def test_decision_evidence_builds_a_valid_record(self):
        skip = decision_evidence(character_id="c1", decision="skip", in_training_room=True, training_active=False,
                                 reason_code="fatigued")
        self.assertEqual(skip, {"kind": "training_decision", "v": 1, "decision": "skip", "character_id": "c1",
                                "in_training_room": True, "training_active": False, "reason_code": "fatigued"})
        start = decision_evidence(character_id="c2", decision="start", in_training_room=False, training_active=None)
        self.assertEqual(start, {"kind": "training_decision", "v": 1, "decision": "start", "character_id": "c2",
                                 "in_training_room": False, "training_active": None})
        self.assertEqual((validate_decision_evidence(skip), validate_decision_evidence(start)), ((), ()))

    def test_decision_evidence_round_trips_into_an_observation(self):
        evidence = decision_evidence(character_id="c1", decision="skip", in_training_room=True,
                                     training_active=False, reason_code="tp_cap_reached")
        record = GameplayEvent(**{**self.valid().__dict__, "evidence": evidence})
        self.assertEqual(parse_decision(record),
                         (Decision("skip", "c1", True, False, "tp_cap_reached"), ()))
        self.assertEqual(observations_from_journal([record], "alpha"),
                         [TrainingObservation("alpha", "c1", at(1), True, False, "tp_cap_reached")])
        totals = totals_for([record])
        self.assertEqual((dict(totals.contract_violations), dict(totals.skip_reasons)), ({}, {"tp_cap_reached": 1}))

    def test_parse_decision_ignores_other_actions(self):
        self.assertEqual(parse_decision(ev("alpha", "add_training", 1)), (None, ()))
        self.assertEqual(parse_decision(ev("alpha", "collect_all_resources", 1)), (None, ()))

    def test_missing_kind(self):
        for kind in ("other_kind", ...):
            with self.subTest(kind=kind):
                record = with_evidence(self.valid(), kind=kind)
                self.assertEqual(parse_decision(record), (None, ("missing_kind",)))
                self.assertEqual(violations_of([record]), {"missing_kind": 1})
                self.assertEqual(observations_from_journal([record], "alpha"), [])
        self.assertEqual(validate_decision_evidence(["kind"]), ("missing_kind",))

    def test_missing_version_is_a_violation_but_still_parsed(self):
        record = self.valid(version=None)
        self.assertEqual(parse_decision(record), (Decision("skip", "c1", True, False, "fatigued"), ("bad_version",)))
        totals = totals_for([record])
        self.assertEqual((dict(totals.contract_violations), dict(totals.skip_reasons)),
                         ({"bad_version": 1}, {"fatigued": 1}))
        self.assertEqual(len(observations_from_journal([record], "alpha")), 1)

    def test_other_version_is_a_violation_and_not_parsed(self):
        for version in (2, 0, "1", True, 1.0):
            with self.subTest(version=version):
                record = self.valid(version=version)
                self.assertEqual(parse_decision(record), (None, ("bad_version",)))
                totals = totals_for([record])
                self.assertEqual((dict(totals.contract_violations), dict(totals.skip_reasons)),
                                 ({"bad_version": 1}, {}))
                self.assertEqual(observations_from_journal([record], "alpha"), [])

    def test_bad_decision(self):
        for value in ("Skip", "", None, ...):
            with self.subTest(decision=value):
                record = with_evidence(self.valid(), decision=value)
                self.assertEqual(parse_decision(record), (Decision(None, "c1", True, False, None), ("bad_decision",)))
                totals = totals_for([record])
                self.assertEqual((dict(totals.contract_violations), dict(totals.skip_reasons)),
                                 ({"bad_decision": 1}, {}))
                self.assertEqual(observations_from_journal([record], "alpha"),
                                 [TrainingObservation("alpha", "c1", at(1), True, False)])

    def test_missing_character_id(self):
        for value in ("", None, ...):
            with self.subTest(character_id=value):
                record = with_evidence(self.valid(), character_id=value)
                self.assertEqual(parse_decision(record),
                                 (Decision("skip", None, True, False, "fatigued"), ("missing_character_id",)))
                self.assertEqual(violations_of([record]), {"missing_character_id": 1})
                self.assertEqual(observations_from_journal([record], "alpha"), [])

    def test_non_bool_in_room(self):
        for value in ("yes", 1, None, ...):
            with self.subTest(in_training_room=value):
                record = with_evidence(self.valid(), in_training_room=value)
                self.assertEqual(parse_decision(record),
                                 (Decision("skip", "c1", None, False, "fatigued"), ("non_bool_in_room",)))
                self.assertEqual(violations_of([record]), {"non_bool_in_room": 1})
                self.assertEqual(observations_from_journal([record], "alpha"), [])

    def test_bad_training_active(self):
        for value in ("no", 0, "false"):
            with self.subTest(training_active=value):
                record = with_evidence(self.valid(), training_active=value)
                self.assertEqual(parse_decision(record),
                                 (Decision("skip", "c1", True, None, "fatigued"), ("bad_training_active",)))
                self.assertEqual(violations_of([record]), {"bad_training_active": 1})
        for value in (None, ..., True, False):
            with self.subTest(training_active=value):
                self.assertEqual(violations_of([with_evidence(self.valid(), training_active=value)]), {})

    def test_unknown_reason_code(self):
        for value in ("bogus", "Fatigued", "", None, ...):
            with self.subTest(reason_code=value):
                record = with_evidence(self.valid(), reason_code=value)
                self.assertEqual(parse_decision(record),
                                 (Decision("skip", "c1", True, False, None), ("unknown_reason_code",)))
                totals = totals_for([record])
                self.assertEqual((dict(totals.contract_violations), dict(totals.skip_reasons)),
                                 ({"unknown_reason_code": 1}, {"unknown_reason_code": 1}))

    def test_reason_code_on_start(self):
        record = decision("alpha", 1, "c1", decision="start", reason_code="fatigued")
        self.assertEqual(parse_decision(record),
                         (Decision("start", "c1", True, False, None), ("reason_code_on_start",)))
        totals = totals_for([record])
        self.assertEqual((dict(totals.contract_violations), dict(totals.skip_reasons)),
                         ({"reason_code_on_start": 1}, {}))
        for value in (None, ...):
            with self.subTest(reason_code=value):
                start = with_evidence(record, reason_code=value)
                self.assertEqual(parse_decision(start), (Decision("start", "c1", True, False, None), ()))

    def test_falsy_reason_code_on_start_is_still_a_reason_code(self):
        # Only an absent or None reason_code is "no code"; an empty string (or 0) on a start is still present.
        for value in ("", 0):
            with self.subTest(reason_code=value):
                start = with_evidence(decision("alpha", 1, "c1", decision="start"), reason_code=value)
                self.assertEqual(parse_decision(start),
                                 (Decision("start", "c1", True, False, None), ("reason_code_on_start",)))
                self.assertEqual(violations_of([start]), {"reason_code_on_start": 1})

    def test_decision_attempted(self):
        record = self.valid(attempted=True)
        self.assertEqual(parse_decision(record),
                         (Decision("skip", "c1", True, False, "fatigued"), ("decision_attempted",)))
        self.assertEqual(violations_of([record]), {"decision_attempted": 1})
        self.assertEqual(validate_decision_evidence(record.evidence), ())

    def test_action_missing_param(self):
        missing = [ev("alpha", "add_training", 1), ev("alpha", "finish_training", 2, "unknown"),
                   ev("alpha", "use_consumable", 3, "not-observed", params={"character_id": "c1"}),
                   ev("alpha", "add_training", 4, params={"character_id": ""})]
        self.assertEqual(violations_of(missing), {"action_missing_param": 4})
        complete = [ev("alpha", "add_training", 1, params={"character_id": "c1", "training_design_id": "T1"}),
                    ev("alpha", "finish_training", 2, params={"character_id": "c1"}),
                    ev("alpha", "use_consumable", 3, params={"item_design_id": "501"}),
                    ev("alpha", "add_training", 4, attempted=False),
                    ev("alpha", "add_training", 5, attempted=False, dry_run=True),
                    ev("alpha", "collect_all_resources", 6)]
        self.assertEqual(violations_of(complete), {})

    def test_one_record_can_break_several_rules(self):
        record = with_evidence(self.valid(version=None, attempted=True), character_id=..., in_training_room="x",
                               training_active="y", reason_code="bogus")
        expected = ("bad_version", "missing_character_id", "non_bool_in_room", "bad_training_active",
                    "unknown_reason_code", "decision_attempted")
        self.assertEqual(parse_decision(record), (Decision("skip", None, None, None, None), expected))
        self.assertEqual(validate_decision_evidence(record.evidence), expected[:-1])
        again = GameplayEvent(**{**record.__dict__, "attempt_id": "another-attempt"})
        self.assertEqual(violations_of([record, again, self.valid()]), {code: 2 for code in expected})

    def test_violations_are_summed_into_the_fleet_and_rendered(self):
        events = [self.valid(version=None), decision("bravo", 1, "c1", in_room="x", version=None),
                  ev("bravo", "add_training", 1)]
        report = build_training_report(events, now=NOW)
        self.assertEqual(dict(report.fleet.contract_violations),
                         {"action_missing_param": 1, "bad_version": 2, "non_bool_in_room": 1,
                          "unknown_reason_code": 1})
        self.assertEqual(list(report.fleet.contract_violations),
                         ["action_missing_param", "bad_version", "non_bool_in_room", "unknown_reason_code"])
        markdown = render_markdown(report)
        self.assertIn("\n| Contract violations | action_missing_param 1, bad_version 2, non_bool_in_room 1, "
                      "unknown_reason_code 1 |\n", markdown)
        self.assertIn("\n| Contract violations | bad_version 1 |\n", markdown)
        clean = render_markdown(build_training_report([self.valid()], now=NOW))
        self.assertEqual(clean.count("\n| Contract violations | none |\n"), 2)

    def test_decisions_with_bad_kind_or_version_still_count_as_training_records(self):
        records = [with_evidence(self.valid(), kind="x"), self.valid(version=2)]
        self.assertEqual(account(build_training_report(records, now=NOW), "alpha").records, 2)


def idle_crew(alias, cid, pattern):
    """Decision records for ``cid``: ``pattern`` maps hours ago to "idle", "active", "unknown" or "out"."""
    flags = {"idle": (True, False), "active": (True, True), "unknown": (True, None), "out": (False, False)}
    return [decision(alias, hours, cid, in_room=flags[state][0], active=flags[state][1])
            for hours, state in pattern]


def idle_of(events, alias="alpha", **kwargs):
    """``{crew label: (idle hours, idle streak hours)}`` for ``alias``."""
    row = account(build_training_report(events, now=NOW, **kwargs), alias)
    return {crew.crew: (crew.idle_hours, crew.idle_streak_hours) for crew in row.crew}


class IdleFromJournalTests(unittest.TestCase):
    def test_idle_throughout(self):
        events = idle_crew("alpha", "c1", [(20, "idle"), (14, "idle"), (8, "idle"), (2, "idle")])
        self.assertEqual(idle_of(events), {label("c1"): (18.0, 18.0)})
        (result,) = crew_idle(observations_from_journal(events, "alpha"), now=NOW).values()
        self.assertEqual(result, CrewIdle(idle_seconds=64800.0, streak_seconds=64800.0, streak_start=at(20),
                                          last_observed_at=at(2), known_observations=4))

    def test_training_breaks_idle_time_and_streak(self):
        events = idle_crew("alpha", "c2", [(20, "idle"), (14, "active"), (8, "idle"), (2, "idle")])
        self.assertEqual(idle_of(events), {label("c2"): (6.0, 6.0)})
        (result,) = crew_idle(observations_from_journal(events, "alpha"), now=NOW).values()
        self.assertEqual(result.streak_start, at(8))

    def test_leaving_the_training_room_breaks_idle_time(self):
        events = idle_crew("alpha", "c3", [(20, "idle"), (14, "out"), (8, "idle")])
        self.assertEqual(idle_of(events), {label("c3"): (0.0, 0.0)})
        (result,) = crew_idle(observations_from_journal(events, "alpha"), now=NOW).values()
        self.assertEqual((result.streak_seconds, result.streak_start, result.last_observed_at), (0.0, at(8), at(8)))

    def test_last_observation_not_idle_has_no_streak(self):
        events = idle_crew("alpha", "c3", [(20, "idle"), (14, "idle"), (8, "active")])
        (result,) = crew_idle(observations_from_journal(events, "alpha"), now=NOW).values()
        self.assertEqual(result, CrewIdle(idle_seconds=21600.0, streak_seconds=0.0, streak_start=None,
                                          last_observed_at=at(8), known_observations=3))

    def test_crew_without_a_known_training_state_has_no_idle_figures(self):
        events = idle_crew("alpha", "c1", [(20, "unknown"), (14, "unknown"), (8, "unknown")])
        events.append(decision("alpha", 2, "c1", in_room=False, active=None))
        row = account(build_training_report(events, now=NOW), "alpha")
        self.assertEqual(row.observations, 4)
        self.assertEqual(row.crew, (CrewTraining(label("c1"), None, None, None, None),))
        self.assertIsNone(row.totals.idle_crew_hours)
        self.assertIsNone(build_training_report(events, now=NOW).fleet.idle_crew_hours)
        (result,) = crew_idle(observations_from_journal(events, "alpha"), now=NOW).values()
        self.assertEqual(result, CrewIdle(idle_seconds=0.0, streak_seconds=0.0, streak_start=None,
                                          last_observed_at=at(2), known_observations=0))

    def test_last_reason_code_comes_from_the_last_observation(self):
        events = [decision("alpha", 20, "c1", reason_code="fatigued"), decision("alpha", 2, "c1", reason_code=None),
                  decision("alpha", 2, "c1", reason_code="tp_cap_reached"),
                  decision("alpha", 2, "c1", reason_code="complete")]
        (result,) = crew_idle(observations_from_journal(events, "alpha"), now=NOW).values()
        self.assertEqual(result.last_reason_code, "complete")
        (result,) = crew_idle(observations_from_journal(events[:2], "alpha"), now=NOW).values()
        self.assertIsNone(result.last_reason_code)
        (result,) = crew_idle(observations_from_journal(events[:1], "alpha"), now=NOW).values()
        self.assertEqual(result.last_reason_code, "fatigued")
        later = TrainingObservation("alpha", "c1", NOW + timedelta(hours=1), True, False, "tp_cap_reached")
        (result,) = crew_idle(observations_from_journal(events[:1], "alpha") + [later], now=NOW).values()
        self.assertEqual(result.last_reason_code, "fatigued")

    def test_known_observations_count_bool_states_up_to_now(self):
        events = idle_crew("alpha", "c1", [(20, "unknown"), (14, "out"), (8, "active"), (6, "idle"), (5, "idle"),
                                           (4, "unknown")])
        observed = observations_from_journal(events, "alpha")
        future = TrainingObservation("alpha", "c1", NOW + timedelta(hours=1), True, False)
        (result,) = crew_idle(observed + [future], now=NOW).values()
        self.assertEqual((result.known_observations, result.idle_seconds), (4, 3600.0))

    def test_one_known_observation_is_enough_for_idle_figures(self):
        events = idle_crew("alpha", "c1", [(20, "unknown"), (14, "active")])
        self.assertEqual(idle_of(events), {label("c1"): (0.0, 0.0)})
        self.assertEqual(account(build_training_report(events, now=NOW), "alpha").totals.idle_crew_hours, 0.0)

    def test_account_idle_total_skips_crew_without_a_known_state(self):
        events = (idle_crew("alpha", "c1", [(20, "unknown"), (14, "unknown")])
                  + idle_crew("alpha", "c2", [(20, "idle"), (14, "idle")]))
        self.assertEqual(idle_of(events), {label("c1"): (None, None), label("c2"): (6.0, 6.0)})
        self.assertEqual(account(build_training_report(events, now=NOW), "alpha").totals.idle_crew_hours, 6.0)

    def test_gap_cap_drops_long_intervals_and_counts_exactly_the_cap(self):
        events = idle_crew("alpha", "c4", [(60, "idle"), (30, "idle"), (4, "idle")])
        self.assertEqual(idle_of(events), {label("c4"): (26.0, 26.0)})
        (result,) = crew_idle(observations_from_journal(events, "alpha"), now=NOW).values()
        self.assertEqual(result.streak_start, at(30))
        over = idle_crew("alpha", "c4", [(26 + 4 + 1 / 3600, "idle"), (4, "idle")])
        self.assertEqual(idle_of(over), {label("c4"): (0.0, 0.0)})
        (result,) = crew_idle(observations_from_journal(over, "alpha"), now=NOW).values()
        self.assertEqual(result.streak_start, at(4))

    def test_verified_add_training_is_an_active_observation(self):
        params = {"character_id": "c5", "training_design_id": "T1"}
        events = idle_crew("alpha", "c5", [(20, "idle"), (2, "idle")])
        verified = events + [ev("alpha", "add_training", 19.9, params=params)]
        self.assertEqual(idle_of(verified), {label("c5"): (0.0, 0.0)})
        self.assertEqual(observations_from_journal([verified[-1]], "alpha"),
                         [TrainingObservation("alpha", "c5", at(19.9), True, True)])
        for unverified in (ev("alpha", "add_training", 19.9, "not-observed", params=params),
                           ev("alpha", "add_training", 19.9, attempted=False, params=params),
                           ev("alpha", "add_training", 19.9),
                           ev("alpha", "finish_training", 19.9, params=params)):
            with self.subTest(action=unverified.action_id, outcome=unverified.outcome, params=dict(unverified.params)):
                self.assertEqual(idle_of(events + [unverified]), {label("c5"): (18.0, 18.0)})

    def test_unknown_training_state_is_not_idle(self):
        events = idle_crew("alpha", "c1", [(20, "idle"), (14, "unknown"), (8, "idle")])
        self.assertEqual(idle_of(events), {label("c1"): (0.0, 0.0)})
        obs = observations_from_journal([decision("alpha", 1, "c1", active="no")], "alpha")
        self.assertEqual(obs, [TrainingObservation("alpha", "c1", at(1), True, None)])
        self.assertFalse(obs[0].idle)

    def test_same_time_observations_are_idle_only_if_all_are(self):
        events = idle_crew("alpha", "c1", [(20, "idle"), (14, "idle"), (14, "active"), (8, "idle")])
        self.assertEqual(idle_of(events), {label("c1"): (0.0, 0.0)})
        active_first = idle_crew("alpha", "c1", [(20, "idle"), (14, "active"), (14, "idle"), (8, "idle")])
        self.assertEqual(idle_of(active_first), {label("c1"): (0.0, 0.0)})
        both_idle = idle_crew("alpha", "c1", [(20, "idle"), (14, "idle"), (14, "idle"), (8, "idle")])
        self.assertEqual(idle_of(both_idle), {label("c1"): (12.0, 12.0)})

    def test_account_total_and_crew_rows(self):
        events = (idle_crew("alpha", "c1", [(20, "idle"), (14, "idle"), (8, "idle"), (2, "idle")])
                  + idle_crew("alpha", "c2", [(20, "idle"), (14, "active"), (8, "idle"), (2, "idle")])
                  + idle_crew("alpha", "c3", [(20, "idle"), (14, "out"), (8, "idle")])
                  + idle_crew("alpha", "c4", [(60, "idle"), (30, "idle"), (4, "idle")])
                  + idle_crew("alpha", "c5", [(20, "idle"), (2, "idle")])
                  + [ev("alpha", "add_training", 19.9, params={"character_id": "c5"})])
        row = account(build_training_report(events, now=NOW), "alpha")
        self.assertEqual(row.totals.idle_crew_hours, 50.0)
        self.assertEqual(row.observations, 17)
        self.assertEqual(idle_of(events), {label("c1"): (18.0, 18.0), label("c2"): (6.0, 6.0), label("c3"): (0.0, 0.0),
                                           label("c4"): (26.0, 26.0), label("c5"): (0.0, 0.0)})
        self.assertEqual(idle_of(events, max_gap_hours=6)[label("c1")], (18.0, 18.0))
        self.assertEqual(idle_of(events, max_gap_hours=6)[label("c4")], (0.0, 0.0))
        self.assertEqual(account(build_training_report(events, now=NOW, max_gap_hours=6), "alpha")
                         .totals.idle_crew_hours, 24.0)

    def test_same_character_id_in_two_accounts_is_two_crew(self):
        events = idle_crew("alpha", "c1", [(20, "idle")]) + idle_crew("bravo", "c1", [(14, "idle")])
        self.assertEqual(idle_of(events, "alpha"), {label("c1"): (0.0, 0.0)})
        self.assertEqual(idle_of(events, "bravo"), {label("c1", "bravo"): (0.0, 0.0)})
        self.assertEqual(sorted(crew_idle(observations_from_journal(events, "alpha")
                                          + observations_from_journal(events, "bravo"), now=NOW)),
                         [("alpha", "c1"), ("bravo", "c1")])

    def test_short_interval_rounds_to_four_places(self):
        events = idle_crew("alpha", "c1", [(1, "idle"), (1 - 1 / 3, "idle")])
        self.assertEqual(idle_of(events), {label("c1"): (0.3333, 0.3333)})

    def test_observation_after_now_is_ignored(self):
        future = TrainingObservation("alpha", "c1", NOW + timedelta(seconds=1), True, False)
        events = idle_crew("alpha", "c1", [(10, "idle"), (0, "idle")])
        observed = observations_from_journal(events, "alpha")
        self.assertEqual(crew_idle(observed + [future], now=NOW), crew_idle(observed, now=NOW))
        self.assertEqual(crew_idle([future], now=NOW), {})

    def test_decisions_without_usable_flags_are_not_observations(self):
        unusable = [decision("alpha", 1, "c1", in_room=None), decision("alpha", 1, "c1", in_room="yes"),
                    decision("alpha", 1, "", in_room=True), decision("alpha", 1, "c1", kind="other_kind"),
                    decision("bravo", 1, "c1")]
        self.assertEqual(observations_from_journal(unusable, "alpha"), [])
        row = account(build_training_report(unusable, now=NOW), "alpha")
        self.assertEqual((row.observations, row.totals.idle_crew_hours, row.crew), (0, None, ()))

    def test_decision_observation_fields(self):
        self.assertEqual(observations_from_journal([decision("alpha", 1, 42, in_room=False, active=True)], "alpha"),
                         [TrainingObservation("alpha", "42", at(1), False, True)])
        self.assertTrue(TrainingObservation("alpha", "c1", NOW, True, False).idle)
        self.assertFalse(TrainingObservation("alpha", "c1", NOW, False, False).idle)
        self.assertFalse(TrainingObservation("alpha", "c1", NOW, True, True).idle)


class SnapshotMetricTests(unittest.TestCase):
    def test_idle_from_snapshots_with_training_room_designs(self):
        report = build_training_report([], bravo_snapshots(), now=NOW, training_room_design_ids={"TR"})
        row = account(report, "bravo")
        self.assertEqual(row.crew, (CrewTraining(label(K1, "bravo"), 5, 12.0, 0.0, False),
                                    CrewTraining(label(K2, "bravo"), 0, 12.0, 12.0, True)))
        self.assertEqual((row.totals.idle_crew_hours, row.totals.tp_gained, row.totals.crew_at_tp_cap), (24.0, 5, 1))
        self.assertEqual((row.records, row.observations, row.snapshots, row.window_start), (0, 6, 3, None))

    def test_without_training_room_designs_there_is_no_idle_but_tp_counts(self):
        for ids in (None, set()):
            with self.subTest(ids=ids):
                row = account(build_training_report([], bravo_snapshots(), now=NOW, training_room_design_ids=ids),
                              "bravo")
                self.assertEqual(row.crew, (CrewTraining(label(K1, "bravo"), 5, None, None, False),
                                            CrewTraining(label(K2, "bravo"), 0, None, None, True)))
                self.assertEqual((row.observations, row.totals.idle_crew_hours, row.totals.tp_gained), (0, None, 5))

    def test_design_ids_compare_as_text(self):
        snapshots = [snap("bravo", 2, [member(K1, TR_ROOM)], [Room(TR_ROOM, "7", None)]),
                     snap("bravo", 1, [member(K1, TR_ROOM)], [Room(TR_ROOM, "7", None)])]
        row = account(build_training_report([], snapshots, now=NOW, training_room_design_ids=[7]), "bravo")
        self.assertEqual(row.totals.idle_crew_hours, 1.0)
        numeric = [snap("bravo", 2, [member(K1, TR_ROOM)], [Room(TR_ROOM, 7, None)]),
                   snap("bravo", 1, [member(K1, TR_ROOM)], [Room(TR_ROOM, 7, None)])]
        row = account(build_training_report([], numeric, now=NOW, training_room_design_ids={"7"}), "bravo")
        self.assertEqual(row.totals.idle_crew_hours, 1.0)

    def test_tp_gained_sums_only_increases(self):
        snapshots = [snap("bravo", hours, [member(K1, None, tp=tp)]) for hours, tp in ((3, 20), (2, 5), (1, 9))]
        row = account(build_training_report([], snapshots, now=NOW), "bravo")
        self.assertEqual((row.crew[0].tp_gained, row.totals.tp_gained), (4, 4))

    def test_tp_needs_two_known_integer_values(self):
        cases = (
            [snap("bravo", 1, [member(K1, None, tp=10)])],
            [snap("bravo", 2, [member(K1, None, tp=10)]), snap("bravo", 1, [member(K1, None)])],
            [snap("bravo", 2, [member(K1, None, tp=10)]), snap("bravo", 1, [member(K1, None, tp=True)])],
            [snap("bravo", 2, [member(K1, None, tp=10)]), snap("bravo", 1, [member(K1, None, tp="12")])],
        )
        for snapshots in cases:
            with self.subTest(len(snapshots)):
                row = account(build_training_report([], snapshots, now=NOW), "bravo")
                self.assertEqual(row.crew, (CrewTraining(label(K1, "bravo"), None, None, None, None),))
                self.assertEqual((row.totals.tp_gained, row.totals.crew_at_tp_cap), (None, None))

    def test_crew_without_training_point_attributes_has_no_row(self):
        plain = CrewMember(character_id=K1, design_id=None, name=None, level=None, fatigue=None, room_id=None)
        row = account(build_training_report([], [snap("bravo", 2, [plain]), snap("bravo", 1, [plain])], now=NOW),
                      "bravo")
        self.assertEqual((row.crew, row.totals.tp_gained, row.totals.crew_at_tp_cap, row.snapshots),
                         ((), None, None, 2))

    def test_at_cap_uses_the_most_recent_snapshot_with_both_values(self):
        raised = [snap("bravo", 3, [member(K1, None, tp=40, cap=40)]),
                  snap("bravo", 2, [member(K1, None, tp=40, cap=50)]),
                  snap("bravo", 1, [member(K1, None, tp=40)])]
        row = account(build_training_report([], raised, now=NOW), "bravo")
        self.assertEqual((row.crew[0].at_tp_cap, row.totals.crew_at_tp_cap), (False, 0))
        over = [snap("bravo", 2, [member(K1, None, tp=40, cap=50)]),
                snap("bravo", 1, [member(K1, None, tp=60, cap=50)]),
                snap("bravo", 0, [member(K1, None, tp=60, cap=True)])]
        row = account(build_training_report([], over, now=NOW), "bravo")
        self.assertEqual((row.crew[0].at_tp_cap, row.totals.crew_at_tp_cap), (True, 1))

    def test_room_lookup(self):
        missing_room = snap("bravo", 1, [member(K1, "room-gone"), member(K2, None)], BRAVO_ROOMS)
        self.assertEqual(observations_from_snapshots([missing_room], training_room_design_ids={"TR"}),
                         [TrainingObservation("bravo", K2, at(1), False, False)])
        unobserved = snap("bravo", 1, [member(K1, TR_ROOM, active=None)], BRAVO_ROOMS)
        self.assertEqual(observations_from_snapshots([unobserved], training_room_design_ids={"TR"}),
                         [TrainingObservation("bravo", K1, at(1), True, None)])

    def test_snapshot_training_is_active_only_while_training_end_is_after_observed_at(self):
        # Regression: finished but uncollected training (training_end set, in the past) counted as active.
        def seen(end, observed=True):
            crew = CrewMember(character_id=K1, design_id=None, name=None, level=None, fatigue=None, room_id=TR_ROOM,
                              training_observed=observed, training_end=end)
            (obs,) = observations_from_snapshots([snap("bravo", 1, [crew], BRAVO_ROOMS)],
                                                 training_room_design_ids={"TR"})
            return obs.training_active

        cases = (("2027-04-03T10:00:00", False), ("2027-04-03T11:00:00", False), ("2027-04-03T11:00:01", True),
                 ("2027-04-03T12:00:00+00:00", True), ("2027-04-03T13:00:00+02:00", False),
                 ("2027-04-03T13:00:01+02:00", True), ("not a date", None), ("", False), (None, False))
        for end, expected in cases:
            with self.subTest(end=end):
                self.assertIs(seen(end), expected)
        self.assertIsNone(seen("2027-04-03T10:00:00", observed=False))
        self.assertIsNone(seen("2027-04-04T00:00:00", observed=False))

    def test_finished_uncollected_training_in_a_training_room_is_idle(self):
        ended = TPCrew(character_id=K1, design_id=None, name=None, level=None, fatigue=None, room_id=TR_ROOM,
                       training_observed=True, training_end="2027-04-03T08:00:00")
        snapshots = [snap("bravo", 3, [ended], BRAVO_ROOMS), snap("bravo", 1, [ended], BRAVO_ROOMS)]
        row = account(build_training_report([], snapshots, now=NOW, training_room_design_ids={"TR"}), "bravo")
        self.assertEqual((row.crew[0].idle_hours, row.totals.idle_crew_hours), (2.0, 2.0))

    def test_snapshot_after_now_is_ignored(self):
        late = snap("bravo", -1 / 3600, [member(K1, TR_ROOM, False, 99, 99)], BRAVO_ROOMS)
        report = build_training_report([], [late], now=NOW, training_room_design_ids={"TR"})
        self.assertEqual(report.accounts, ())
        row = account(build_training_report([], bravo_snapshots() + [late], now=NOW,
                                            training_room_design_ids={"TR"}), "bravo")
        self.assertEqual((row.snapshots, row.observations, row.totals.tp_gained, row.totals.idle_crew_hours),
                         (3, 6, 5, 24.0))

    def test_snapshot_order_does_not_matter_and_accounts_merge(self):
        report = build_training_report(scenario_a("bravo"), list(reversed(bravo_snapshots())), now=NOW,
                                       training_room_design_ids={"TR"})
        row = account(report, "bravo")
        self.assertEqual((row.records, row.snapshots, row.totals.tp_gained, row.totals.idle_crew_hours),
                         (20, 3, 5, 24.0))

    def test_crew_label_is_a_stable_pseudonym_of_alias_and_character_id(self):
        expected = "crew-" + hashlib.sha256(f"bravo:{K1}".encode("utf-8")).hexdigest()[:8]
        self.assertEqual(crew_label("bravo", K1), expected)
        self.assertEqual(crew_label("bravo", K1), "crew-8dca3eb0")
        self.assertEqual(crew_label("bravo", K2), "crew-f63ac07d")
        self.assertEqual(crew_label("alpha", K1), "crew-8ee1b1a6")
        self.assertEqual(crew_label("alpha", "42"), crew_label("alpha", 42))
        self.assertRegex(crew_label("alpha", "c1"), r"\Acrew-[0-9a-f]{8}\Z")

    def test_crew_rows_are_sorted_by_label(self):
        # c5 sorts after c2 by id but before it by label (crew-cb128e83 < crew-f62a8364).
        events = idle_crew("alpha", "c2", [(2, "idle")]) + idle_crew("alpha", "c5", [(2, "idle")])
        row = account(build_training_report(events, now=NOW), "alpha")
        self.assertEqual([crew.crew for crew in row.crew], [label("c5"), label("c2")])
        self.assertEqual([crew.crew for crew in row.crew], ["crew-cb128e83", "crew-f62a8364"])


def golden_events():
    params = {"character_id": K1, "training_design_id": "T1"}
    return scenario_a("alpha") + [ev("bravo", "add_training", 6, params=params)]


class EdgeTests(unittest.TestCase):
    def test_empty_input(self):
        report = build_training_report([], now=NOW)
        self.assertEqual((report.accounts, report.duplicates_dropped, report.generated_at), ((), 0, NOW.isoformat()))
        fleet = report.fleet
        self.assertEqual((fleet.outcomes, dict(fleet.by_action), fleet.finishes, fleet.dry_runs),
                         (OutcomeCounts(), {}, 0, 0))
        self.assertEqual((fleet.failed_or_unknown_rate, fleet.idle_crew_hours, fleet.tp_gained, fleet.crew_at_tp_cap),
                         (None, None, None, None))
        self.assertEqual((dict(fleet.skip_reasons), dict(fleet.consumables_used)), ({}, {}))
        self.assertEqual(dict(fleet.contract_violations), {})

    def test_duplicate_attempt_ids_are_counted_once_and_the_first_wins(self):
        first = ev("alpha", "add_training", 2, attempt_id="dup")
        again = ev("alpha", "add_training", 1, "contradicted", attempt_id="dup")
        report = build_training_report([first, again, first], now=NOW)
        self.assertEqual(report.duplicates_dropped, 2)
        self.assertEqual(account(report, "alpha").totals.outcomes, OutcomeCounts(attempted=1, verified=1))
        self.assertEqual(account(report, "alpha").records, 1)

    def test_duplicate_attempt_id_keeps_the_first_record_not_the_last(self):
        first = ev("alpha", "add_training", 2, attempt_id="dup")
        later = ev("alpha", "add_training", 1, "contradicted", attempt_id="dup")
        report = build_training_report([first, later], now=NOW)
        self.assertEqual(report.duplicates_dropped, 1)
        outcomes = account(report, "alpha").totals.outcomes
        self.assertEqual((outcomes.verified, outcomes.contradicted), (1, 0))
        self.assertEqual(outcomes, OutcomeCounts(attempted=1, verified=1))

    def test_duplicate_attempt_id_first_seen_on_a_non_training_action_drops_the_training_copy(self):
        # Dedupe runs over every action before the training filter (tests/test_alerts.py pins the same rule).
        other = ev("alpha", "collect_all_resources", 2, attempt_id="dup")
        training = ev("alpha", "add_training", 1, "contradicted", attempt_id="dup")
        report = build_training_report([other, training], now=NOW)
        self.assertEqual(report.duplicates_dropped, 1)
        self.assertEqual(account(report, "alpha").records, 0)
        self.assertEqual(account(report, "alpha").totals.outcomes, OutcomeCounts())

    def test_same_attempt_id_in_two_accounts_counts_in_both(self):
        report = build_training_report([ev("alpha", "add_training", 1, attempt_id="same"),
                                        ev("bravo", "add_training", 1, attempt_id="same")], now=NOW)
        self.assertEqual(report.duplicates_dropped, 0)
        self.assertEqual(report.fleet.outcomes.attempted, 2)

    def test_future_duplicate_is_not_a_dropped_duplicate(self):
        report = build_training_report([ev("alpha", "add_training", 1, attempt_id="x"),
                                        ev("alpha", "add_training", -1, attempt_id="x")], now=NOW)
        self.assertEqual(report.duplicates_dropped, 0)

    def test_error_with_unknown_outcome_counts_once(self):
        totals = totals_for([ev("alpha", "add_training", 1, "unknown", error_class="TimeoutError")])
        self.assertEqual(totals.outcomes, OutcomeCounts(attempted=1, unknown=1, failed=1, failed_or_unknown=1))
        self.assertEqual(totals.failed_or_unknown_rate, 1.0)

    def test_error_on_verified_attempt_is_failed(self):
        totals = totals_for([ev("alpha", "add_training", 1, error_class="HTTPError"), ev("alpha", "add_training", 2)])
        self.assertEqual(totals.outcomes, OutcomeCounts(attempted=2, verified=2, failed=1, failed_or_unknown=1))

    def test_input_order_does_not_matter(self):
        events = golden_events() + idle_crew("alpha", "c1", [(20, "idle"), (14, "idle"), (8, "active")])
        shuffled = list(events)
        random.Random(7).shuffle(shuffled)
        snapshots = bravo_snapshots()
        self.assertEqual(build_training_report(shuffled, list(reversed(snapshots)), now=NOW,
                                               training_room_design_ids={"TR"}),
                         build_training_report(events, snapshots, now=NOW, training_room_design_ids={"TR"}))

    def test_naive_now_is_refused(self):
        with self.assertRaises(TrainingReportError):
            build_training_report([], now=NOW.replace(tzinfo=None))

    def test_naive_finished_at_is_refused(self):
        stamp = at(1).replace(tzinfo=None).isoformat()
        naive = GameplayEvent("alpha", "add_training", "naive-1", stamp, stamp, "verified", "synthetic", True)
        with self.assertRaisesRegex(TrainingReportError, "timezone-aware"):
            build_training_report([naive], now=NOW)
        with self.assertRaises(TrainingReportError):
            observations_from_journal([GameplayEvent(**{**decision("alpha", 1, "c1").__dict__,
                                                        "finished_at": stamp})], "alpha")

    def test_naive_observed_at_is_refused(self):
        naive = SimpleNamespace(account_alias="bravo", observed_at=at(1).replace(tzinfo=None), crew=(),
                                room=lambda room_id: None)
        with self.assertRaisesRegex(TrainingReportError, "timezone-aware"):
            build_training_report([], [naive], now=NOW)
        with self.assertRaisesRegex(TrainingReportError, "timezone-aware"):
            crew_idle([TrainingObservation("alpha", "c1", at(1).replace(tzinfo=None), True, False)], now=NOW)

    def test_fleet_totals(self):
        fleet = build_training_report(golden_events(), bravo_snapshots(), now=NOW,
                                      training_room_design_ids={"TR"}).fleet
        self.assertEqual(fleet.outcomes, OutcomeCounts(11, 6, 2, 1, 2, 1, 2))
        self.assertEqual(dict(fleet.by_action), {
            "add_training": OutcomeCounts(6, 3, 1, 1, 1, 1, 1),
            "finish_training": OutcomeCounts(2, 1, 0, 0, 1, 0, 1),
            "use_consumable": OutcomeCounts(3, 2, 1, 0, 0, 0, 0),
        })
        self.assertEqual((fleet.failed_or_unknown_rate, fleet.finishes, fleet.dry_runs), (0.1818, 1, 1))
        self.assertEqual((fleet.tp_gained, fleet.idle_crew_hours, fleet.crew_at_tp_cap), (5, 24.0, 1))

    def test_fleet_sums_accounts(self):
        fleet = build_training_report(scenario_a("alpha") + scenario_a("charlie"), now=NOW).fleet
        self.assertEqual(fleet.outcomes, OutcomeCounts(20, 10, 4, 2, 4, 2, 4))
        self.assertEqual(fleet.by_action["use_consumable"], OutcomeCounts(6, 4, 2, 0, 0, 0, 0))
        self.assertEqual((fleet.finishes, fleet.dry_runs, fleet.failed_or_unknown_rate), (2, 2, 0.2))
        self.assertEqual(dict(fleet.skip_reasons), {
            "execute_gate_refused": 2, "fatigued": 4, "other": 2, "precondition_failed": 2, "tp_cap_reached": 2,
            "uncertain_prior_attempt": 2, "unknown_reason_code": 2,
        })
        self.assertEqual(dict(fleet.contract_violations), {"action_missing_param": 14, "non_bool_in_room": 10,
                                                           "unknown_reason_code": 2})
        self.assertEqual(dict(fleet.consumables_used), {"501": 6})

    def test_fleet_rate_is_recomputed_and_optional_sums_skip_missing(self):
        events = ([ev("alpha", "add_training", 1, error_class="HTTPError"), ev("alpha", "add_training", 2)]
                  + [ev("bravo", "add_training", 1 + i) for i in range(8)])
        report = build_training_report(events, now=NOW)
        self.assertEqual([row.totals.failed_or_unknown_rate for row in report.accounts], [0.5, 0.0])
        self.assertEqual(report.fleet.failed_or_unknown_rate, 0.1)
        self.assertEqual((report.fleet.idle_crew_hours, report.fleet.tp_gained, report.fleet.crew_at_tp_cap),
                         (None, None, None))
        mixed = build_training_report(events, bravo_snapshots(), now=NOW, training_room_design_ids={"TR"})
        self.assertEqual([row.totals.idle_crew_hours for row in mixed.accounts], [None, 24.0])
        self.assertEqual((mixed.fleet.idle_crew_hours, mixed.fleet.tp_gained, mixed.fleet.crew_at_tp_cap),
                         (24.0, 5, 1))

    def test_fleet_idle_hours_are_rounded(self):
        events = (idle_crew("alpha", "c1", [(1, "idle"), (1 - 1 / 3, "idle")])
                  + idle_crew("bravo", "c1", [(1, "idle"), (1 - 1 / 3, "idle")])
                  + idle_crew("charlie", "c1", [(1, "idle"), (1 - 1 / 3, "idle")]))
        self.assertEqual(build_training_report(events, now=NOW).fleet.idle_crew_hours, 0.9999)
        # 0.1 + 0.2 is 0.30000000000000004 in floating point.
        tenths = idle_crew("alpha", "c1", [(1, "idle"), (0.9, "idle")]) + idle_crew("bravo", "c1", [(1, "idle"),
                                                                                                   (0.8, "idle")])
        self.assertEqual(build_training_report(tenths, now=NOW).fleet.idle_crew_hours, 0.3)


class UnattemptedVerifiedTests(unittest.TestCase):
    """A record marked verified but not attempted is not evidence of a training call."""

    def test_not_counted_as_finish_consumable_or_observation(self):
        stamp = at(1).isoformat()
        events = [GameplayEvent("alpha", action, f"v-{action}", stamp, stamp, "verified", "synthetic", False,
                                params={"character_id": "c1", "item_design_id": "9"})
                  for action in TRAINING_ACTIONS]
        totals = totals_for(events)
        self.assertEqual((totals.finishes, dict(totals.consumables_used), totals.outcomes.attempted), (0, {}, 0))
        self.assertEqual(observations_from_journal(events, "alpha"), [])


def golden_report():
    return build_training_report(golden_events(), bravo_snapshots(), now=NOW, training_room_design_ids={"TR"})


NO_SNAPSHOTS = "Snapshots not provided: TP gained and crew at TP cap are n/a."
NO_TP = "Snapshot crew carry no training points: TP gained and crew at TP cap are n/a."
NO_ROOM_IDS = "Snapshots provided without training room design ids: idle crew-hours use journal records only."
EMPTY_MARKDOWN = ("# Training report\n\nGenerated at 2027-04-03T12:00:00+00:00. Accounts: 0. Duplicate records "
                  f"dropped: 0.\n\n{NO_SNAPSHOTS}\n\nNo training records or snapshots.\n")


class RendererTests(unittest.TestCase):
    def check_golden(self, name, text):
        path = GOLDEN_DIR / name
        if os.environ.get("TACHIKOMA_UPDATE_GOLDEN") == "1":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_golden_markdown(self):
        markdown = render_markdown(golden_report())
        self.check_golden("fleet.md", markdown)
        self.assertIn("\n| Training actions attempted | 11 |\n| Training starts attempted (add_training) | 6 |\n"
                      "| Training starts verified (add_training) | 3 |\n", markdown)
        self.assertNotIn("Training attempts (started)", markdown)
        self.assertNotIn("Trainings started", markdown)
        for line in (NO_SNAPSHOTS, NO_TP, NO_ROOM_IDS):
            self.assertNotIn(line, markdown)
        self.assertIn("| Failed or unknown | 18% (2/11) |", markdown)
        self.assertIs(to_markdown, render_markdown)

    def test_golden_json(self):
        text = to_json(golden_report())
        self.check_golden("fleet.json", text + "\n")
        payload = json.loads(text)
        self.assertEqual(payload["fleet"]["failed_or_unknown_rate"], 0.1818)
        self.assertEqual(payload["accounts"][1]["totals"]["idle_crew_hours"], 24.0)
        self.assertEqual(payload["accounts"][1]["crew"][1],
                         {"crew": label(K2, "bravo"), "tp_gained": 0, "idle_hours": 12.0, "idle_streak_hours": 12.0,
                          "at_tp_cap": True})

    def test_empty_report(self):
        report = build_training_report([], now=NOW)
        self.assertEqual(render_markdown(report), EMPTY_MARKDOWN)
        payload = json.loads(to_json(report))
        self.assertEqual(set(payload), {"generated_at", "duplicates_dropped", "accounts", "fleet",
                                        "snapshots_provided", "snapshot_training_points",
                                        "training_room_design_ids_provided"})
        self.assertIs(payload["snapshots_provided"], False)
        self.assertEqual((payload["accounts"], payload["duplicates_dropped"]), ([], 0))
        self.assertIsNone(payload["fleet"]["failed_or_unknown_rate"])

    def test_snapshots_not_provided_line(self):
        events = [ev("alpha", "add_training", 1)]
        without = build_training_report(events, now=NOW)
        self.assertIs(without.snapshots_provided, False)
        self.assertIn(f"Duplicate records dropped: 0.\n\n{NO_SNAPSHOTS}\n\n## Fleet\n", render_markdown(without))
        self.assertIn("\n| TP gained | n/a |\n", render_markdown(without))
        report = build_training_report(events, bravo_snapshots(), now=NOW)
        self.assertIs(report.snapshots_provided, True)
        self.assertNotIn("Snapshots not provided", render_markdown(report))
        self.assertIs(json.loads(to_json(report))["snapshots_provided"], True)

    def test_empty_snapshot_list_is_not_provided(self):
        events = [ev("alpha", "add_training", 1)]
        for snapshots in ([], (), iter([])):
            with self.subTest(snapshots=snapshots):
                report = build_training_report(events, snapshots, now=NOW, training_room_design_ids={"TR"})
                self.assertIs(report.snapshots_provided, False)
                markdown = render_markdown(report)
                self.assertIn(f"Duplicate records dropped: 0.\n\n{NO_SNAPSHOTS}\n\n## Fleet\n", markdown)
                self.assertNotIn(NO_TP, markdown)
                self.assertNotIn(NO_ROOM_IDS, markdown)

    def test_snapshot_crew_without_training_points_line(self):
        plain = CrewMember(character_id=K1, design_id=None, name=None, level=None, fatigue=None, room_id=None)
        unknown_tp = [snap("bravo", 2, [plain]), snap("bravo", 1, [member(K1, None, tp=True)])]
        for snapshots in ([snap("bravo", 2, [plain]), snap("bravo", 1, [plain])], unknown_tp):
            with self.subTest(snapshots=len(snapshots)):
                report = build_training_report([], snapshots, now=NOW, training_room_design_ids={"TR"})
                markdown = render_markdown(report)
                self.assertIn(f"Duplicate records dropped: 0.\n\n{NO_TP}\n\n## Fleet\n", markdown)
                self.assertNotIn(NO_SNAPSHOTS, markdown)
                self.assertNotIn(NO_ROOM_IDS, markdown)
                self.assertIs(json.loads(to_json(report))["snapshot_training_points"], False)
        one_known = [snap("bravo", 1, [member(K1, None, tp=10)])]
        report = build_training_report([], one_known, now=NOW, training_room_design_ids={"TR"})
        self.assertNotIn(NO_TP, render_markdown(report))
        self.assertIs(json.loads(to_json(report))["snapshot_training_points"], True)
        late = [snap("bravo", -1, [member(K1, None, tp=10)])]
        self.assertIn(NO_TP, render_markdown(build_training_report([], late, now=NOW, training_room_design_ids={"TR"})))

    def test_training_points_on_any_account_suppress_the_no_training_points_line(self):
        # The alphabetically last account carries no training points; an earlier one does, so the fleet has points.
        plain = CrewMember(character_id=K2, design_id=None, name=None, level=None, fatigue=None, room_id=None)
        snapshots = [snap("alpha", 2, [member(K1, None, tp=10)]), snap("alpha", 1, [member(K1, None, tp=12)]),
                     snap("zulu", 2, [plain]), snap("zulu", 1, [plain])]
        report = build_training_report([], snapshots, now=NOW, training_room_design_ids={"TR"})
        self.assertEqual([row.account_alias for row in report.accounts], ["alpha", "zulu"])
        self.assertIs(report.snapshot_training_points, True)
        self.assertIs(json.loads(to_json(report))["snapshot_training_points"], True)
        self.assertNotIn(NO_TP, render_markdown(report))
        self.assertEqual(account(report, "alpha").totals.tp_gained, 2)

    def test_snapshots_without_training_room_design_ids_line(self):
        for ids in (None, set(), ()):
            with self.subTest(ids=ids):
                report = build_training_report([], bravo_snapshots(), now=NOW, training_room_design_ids=ids)
                markdown = render_markdown(report)
                self.assertIn(f"Duplicate records dropped: 0.\n\n{NO_ROOM_IDS}\n\n## Fleet\n", markdown)
                self.assertNotIn(NO_TP, markdown)
                self.assertNotIn(NO_SNAPSHOTS, markdown)
                self.assertIs(json.loads(to_json(report))["training_room_design_ids_provided"], False)
        plain = CrewMember(character_id=K1, design_id=None, name=None, level=None, fatigue=None, room_id=None)
        both = render_markdown(build_training_report([], [snap("bravo", 1, [plain])], now=NOW))
        self.assertIn(f"Duplicate records dropped: 0.\n\n{NO_TP}\n{NO_ROOM_IDS}\n\n## Fleet\n", both)
        no_snapshots = render_markdown(build_training_report([ev("alpha", "add_training", 1)], now=NOW))
        self.assertNotIn(NO_ROOM_IDS, no_snapshots)
        self.assertNotIn(NO_TP, no_snapshots)

    def test_training_starts_attempted_and_verified(self):
        # Regression: the start row counted failed and contradicted add_training attempts as trainings started.
        events = [ev("alpha", "add_training", 1), ev("alpha", "add_training", 2, "unknown"),
                  ev("alpha", "add_training", 2.5, "contradicted"),
                  ev("alpha", "add_training", 2.6, error_class="HTTPError"),
                  ev("alpha", "add_training", 3, attempted=False), ev("alpha", "finish_training", 4),
                  ev("alpha", "use_consumable", 5, params={"item_design_id": "9"})]
        markdown = render_markdown(build_training_report(events, now=NOW))
        self.assertEqual(markdown.count("\n| Training actions attempted | 6 |\n"
                                        "| Training starts attempted (add_training) | 4 |\n"
                                        "| Training starts verified (add_training) | 2 |\n"), 2)
        none_started = render_markdown(build_training_report([ev("alpha", "finish_training", 1)], now=NOW))
        self.assertEqual(none_started.count("\n| Training actions attempted | 1 |\n"
                                            "| Training starts attempted (add_training) | 0 |\n"
                                            "| Training starts verified (add_training) | 0 |\n"), 2)

    def test_text_ends_with_one_newline_and_has_no_trailing_spaces(self):
        for text in (render_markdown(golden_report()), to_json(golden_report()) + "\n"):
            self.assertTrue(text.endswith("\n"))
            self.assertFalse(text.endswith("\n\n"))
            self.assertEqual([line for line in text.split("\n") if line != line.rstrip()], [])

    def test_missing_values_render_as_not_available(self):
        events = [ev("alpha", "collect_all_resources", 1)]
        snapshots = [snap("alpha", 1, [member(K1, None, tp=10)])]
        markdown = render_markdown(build_training_report(events, snapshots, now=NOW))
        for line in ("No training records. Observations: 0. Snapshots: 1.", "| Failed or unknown | n/a |",
                     "| Training actions attempted | 0 |", "| Training starts attempted (add_training) | 0 |",
                     "| Training starts verified (add_training) | 0 |",
                     "| Contract violations | none |",
                     "| Skips | none |", "| Consumables used | none |", "| TP gained | n/a |",
                     "| Idle crew-hours in training rooms | n/a |", "| Crew at TP cap | n/a |",
                     "No training actions.", "| crew-8ee1b1a6 | n/a | n/a | n/a | n/a |"):
            self.assertIn("\n" + line + "\n", markdown)
        self.assertIn("\n\nNo crew observed.\n",
                      render_markdown(build_training_report(events, now=NOW)))

    def test_account_section_layout(self):
        markdown = render_markdown(golden_report())
        self.assertIn("\n## bravo\n\nTraining records: 1 (2027-04-03T06:00:00+00:00 to 2027-04-03T06:00:00+00:00). "
                      "Observations: 7. Snapshots: 3.\n", markdown)
        self.assertIn("\n| crew-8dca3eb0 | 5 | 12.00 | 0.00 | no |\n| crew-f63ac07d | 0 | 12.00 | 12.00 | yes |\n",
                      markdown)

    def test_pipes_are_escaped(self):
        markdown = render_markdown(build_training_report(
            [ev("alpha", "use_consumable", 1, params={"item_design_id": "a|b"})], now=NOW))
        self.assertIn("| Consumables used | a\\|b x1 |", markdown)


class PrivacyTests(unittest.TestCase):
    def test_secret_shaped_values_are_redacted(self):
        stamp = at(1).isoformat()
        leaky = [
            GameplayEvent(SYNTHETIC_EMAIL, "use_consumable", "p1", stamp, stamp, "verified", "synthetic", True,
                          params={"item_design_id": SYNTHETIC_JWT}),
            decision("alpha", 1, "c1", reason_code=SYNTHETIC_EMAIL),
        ]
        report = build_training_report(leaky, now=NOW)
        self.assertEqual(dict(account(report, "alpha").totals.skip_reasons), {"unknown_reason_code": 1})
        for text in (render_markdown(report), to_json(report)):
            self.assertNotIn(SYNTHETIC_EMAIL, text)
            self.assertNotIn(SYNTHETIC_JWT, text)
            self.assertIn("***REDACTED_EMAIL***", text)
            self.assertIn("***REDACTED_JWT***", text)

    def test_room_and_character_ids_are_never_rendered(self):
        report = golden_report()
        for text in (render_markdown(report), to_json(report)):
            self.assertNotIn("room-instance-", text)
            self.assertNotIn("char-instance-900", text)

    def test_free_text_reasons_are_never_rendered(self):
        events = [decision("alpha", 1, "c1", reason="SECRETREASON a"),
                  ev("alpha", "add_training", 1, attempted=False, reason="SECRETREASON b")]
        report = build_training_report(events, now=NOW)
        for text in (render_markdown(report), to_json(report)):
            self.assertNotIn("SECRETREASON", text)


def recent(event, hours_ago):
    """``event`` moved to ``hours_ago`` before the wall clock, which the CLI uses as now."""
    finish = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return GameplayEvent(**{**event.__dict__, "started_at": (finish - timedelta(minutes=1)).isoformat(),
                            "finished_at": finish.isoformat()})


class TrainingReportCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def journal(self, name, events):
        path = self.dir / name
        journal = GameplayJournal(path)
        for item in events:
            journal.append(item)
        if not events:
            path.write_text("", encoding="utf-8")
        return path

    def run_main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["run.py", *args]), \
                patch("sdk.client.Client", side_effect=forbidden("sdk.client.Client")), \
                patch("run.Client", side_effect=forbidden("run.Client")), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch("getpass.getpass", side_effect=forbidden("getpass")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue()

    def training_journal(self):
        return self.journal("alpha.jsonl", [
            recent(ev("alpha", "add_training", 0), 3), recent(ev("alpha", "add_training", 0, "contradicted"), 2),
            recent(ev("alpha", "use_consumable", 0, "unknown", error_class="TimeoutError"), 1),
        ])

    def test_markdown_report(self):
        code, out, err = self.run_main("--report", "training", "--journal", str(self.training_journal()))
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("# Training report\n"))
        self.assertIn(f"\n\n{NO_SNAPSHOTS}\n\n", out)
        self.assertIn("\n| Training actions attempted | 3 |\n| Training starts attempted (add_training) | 2 |\n"
                      "| Training starts verified (add_training) | 1 |\n", out)
        self.assertIn("\n| Failed or unknown | 33% (1/3) |\n", out)

    def test_json_report(self):
        code, out, _ = self.run_main("--report", "training", "--journal", str(self.training_journal()),
                                     "--report-format", "json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        generated = datetime.fromisoformat(payload.pop("generated_at"))
        self.assertLess(abs((datetime.now(timezone.utc) - generated).total_seconds()), 300)
        self.assertEqual(payload["duplicates_dropped"], 0)
        self.assertEqual([row["account_alias"] for row in payload["accounts"]], ["alpha"])
        self.assertEqual(payload["fleet"]["outcomes"], {"attempted": 3, "verified": 1, "not_observed": 0,
                                                        "contradicted": 1, "unknown": 1, "failed": 1,
                                                        "failed_or_unknown": 1})

    def test_same_journal_twice_drops_duplicates(self):
        path = str(self.training_journal())
        code, out, _ = self.run_main("--report", "training", "--journal", path, "--journal", path,
                                     "--report-format", "json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual((payload["duplicates_dropped"], payload["fleet"]["outcomes"]["attempted"]), (3, 3))

    def test_idle_from_decision_records_alone(self):
        path = self.journal("decisions.jsonl", [recent(decision("alpha", 0, "c1"), 20),
                                                recent(decision("alpha", 0, "c1"), 14)])
        code, out, _ = self.run_main("--report", "training", "--journal", str(path))
        self.assertEqual(code, 0)
        self.assertIn("\n| Idle crew-hours in training rooms | 6.00 |\n", out)
        self.assertIn("\n| Skips | unknown_reason_code 2 |\n", out)
        self.assertIn("\n| Contract violations | unknown_reason_code 2 |\n", out)

    def test_naive_timestamp_is_an_input_error(self):
        # Regression: a journal line with a naive finished_at crashed the report with a traceback (exit 1).
        event = recent(ev("alpha", "add_training", 0), 1)
        naive = datetime.fromisoformat(event.finished_at).replace(tzinfo=None).isoformat()
        path = self.dir / "naive.jsonl"
        path.write_text(json.dumps({**event.__dict__, "started_at": naive, "finished_at": naive}) + "\n",
                        encoding="utf-8")
        self.assertEqual([e.finished_at for e in read_events(path)], [naive])
        code, out, err = self.run_main("--report", "training", "--journal", str(path))
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "error: finished_at must be timezone-aware\n")

    def test_missing_and_empty_journals(self):
        missing = self.dir / "absent.jsonl"
        code, out, err = self.run_main("--report", "training", "--journal", str(missing))
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"journal not found: {missing}", err)
        code, out, _ = self.run_main("--report", "training", "--journal", str(self.journal("empty.jsonl", [])))
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("\n\nNo training records or snapshots.\n"))


if __name__ == "__main__":
    unittest.main()
