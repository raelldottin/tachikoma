"""sdk.training.actions: each decision maps to exactly the catalog action and params its spec requires."""

import dataclasses
import itertools
import unittest
from datetime import datetime, timedelta, timezone

from sdk.actions import catalog
from sdk.actions.catalog import ReconciliationStatus, get_spec, reconcile, training_start_verified
from sdk.state import build_snapshot, diff_snapshots
from sdk.training.actions import TRAINING_ACTION_IDS, action_for
from sdk.training.model import (
    SESSION_ENDED_UNCLAIMED,
    SESSION_FINISHED,
    SESSION_NONE,
    SESSION_RUNNING,
    SESSION_UNKNOWN,
    FinishTraining,
    Skip,
    StartTraining,
    UseConsumable,
)


class ActionForTests(unittest.TestCase):
    def test_each_decision_maps_to_its_action_and_string_params(self):
        cases = (
            (FinishTraining(101, "ended"), ("finish_training", {"character_id": "101"})),
            (StartTraining(101, 5030, "band"), ("add_training", {"training_design_id": "5030", "character_id": "101"})),
            (UseConsumable(102, 901, "rule"), ("use_consumable", {"item_design_id": "901", "character_id": "102"})),
        )
        for decision, expected in cases:
            with self.subTest(decision=decision):
                self.assertEqual(action_for(decision), expected)

    def test_a_skip_sends_nothing(self):
        self.assertIsNone(action_for(Skip(101, "complete", "at cap")))

    def test_anything_else_is_refused(self):
        for value in (None, "add_training", (101, 5030)):
            with self.subTest(value=value), self.assertRaises(TypeError):
                action_for(value)

    def test_params_are_exactly_each_catalog_specs_required_params(self):
        decisions = (FinishTraining(7, ""), StartTraining(7, 8, ""), UseConsumable(7, 9, ""))
        seen = []
        for decision in decisions:
            action_id, params = action_for(decision)
            seen.append(action_id)
            self.assertEqual(sorted(params), sorted(get_spec(action_id).required_params), action_id)
        self.assertEqual(tuple(seen), TRAINING_ACTION_IDS)


T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
V, N, C, U = (
    ReconciliationStatus.VERIFIED,
    ReconciliationStatus.NOT_OBSERVED,
    ReconciliationStatus.CONTRADICTED,
    ReconciliationStatus.UNKNOWN,
)
E1, E2 = "2026-11-10T13:00:00", "2026-11-10T14:00:00"
DUE = "2026-11-10T11:00:00"  # an hour before T0, the before-snapshot's observation time
BETWEEN = "2026-11-10T12:00:30"  # after T0, before the after-snapshot's observation (T0 + 60s)
AT_AFTER = "2026-11-10T12:01:00"  # exactly the after-snapshot's observation time
JUST_AFTER = "2026-11-10T12:01:01"
PLACEHOLDER = "0001-01-01T00:00:00"
PARAMS = {"character_id": "c1", "training_design_id": "7"}
# Course and end values for the differential checks, including formats parse_datetime refuses.
SESSION_DESIGNS = (None, "", " ", "0", "00", " 0 ", "7", " 7 ", "8", "abc", "-7", "7.0", "\u0667")
SESSION_ENDS = (
    None, "", "   ", E1, E2, DUE, BETWEEN, AT_AFTER, JUST_AFTER, PLACEHOLDER, "soon", "2026-11-11", "20261110T130000",
    "2026-11-10 13:00:00", "2026-11-10T13:00:00Z", "2026-11-10T13:00:00+02:00", "2026-11-10T13:00:00.500000",
    "2026-11-10T12:00:00",
)
STRICT = dataclasses.replace(get_spec("add_training"), postcondition=training_start_verified)
STATS = {"@HpImprovement": "1", "@PilotImprovement": "2", "@RepairImprovement": "4", "@WeaponImprovement": "8",
         "@ScienceImprovement": "16", "@EngineImprovement": "32", "@AttackImprovement": "64",
         "@AbilityImprovement": "128", "@StaminaImprovement": "256"}
RAISED = {**STATS, "@RepairImprovement": "9"}
ABSENT = "absent"  # the crew section is observed, but c1 is not in it
UNSEEN = "unseen"  # the crew section is not observed at all
OTHER = {"@CharacterId": "c2", "@Level": "3", "@TrainingEndDate": "", "@TrainingDesignId": "0"}


def member(end, design="7", stats=STATS):
    """c1's record; ``end`` None leaves @TrainingEndDate out (training not observed)."""
    record = {"@CharacterId": "c1", "@Level": "3", "@Fatigue": "0", "@TrainingDesignId": design, **stats}
    if end is not None:
        record["@TrainingEndDate"] = end
    return record


IDLE = member("", "0")
IDLE_PLACEHOLDER = member(PLACEHOLDER, "0")


def snap(seconds, crew):
    if crew == UNSEEN:
        records = None
    elif crew == ABSENT:
        records = [OTHER]
    else:
        records = [crew, OTHER]
    return build_snapshot("alpha", T0 + timedelta(seconds=seconds), "synthetic-fixture", crew=records)


def naive(snapshot):
    return dataclasses.replace(snapshot, observed_at=snapshot.observed_at.replace(tzinfo=None))


def aware(snapshot):
    if snapshot.observed_at.tzinfo is not None:
        return snapshot
    return dataclasses.replace(snapshot, observed_at=snapshot.observed_at.replace(tzinfo=timezone.utc))


def strict(before, after, params=PARAMS):
    """The strict verdict, with the delta taken from aware copies (diff_snapshots cannot subtract a naive time)."""
    return training_start_verified(diff_snapshots(aware(before), aware(after)), before, after, params)[0]


# Verifies: S14-R5
class StrictTrainingStartTests(unittest.TestCase):
    def test_the_s14_r5_verdict_table(self):
        rows = (
            ("crew observed, then not", snap(0, IDLE), snap(60, UNSEEN), U),
            ("crew not observed before", snap(0, UNSEEN), snap(60, member(E1)), U),
            ("idle, then the member is gone", snap(0, IDLE), snap(60, ABSENT), C),
            ("absent before, then the requested course runs", snap(0, ABSENT), snap(60, member(E1)), U),
            ("no @TrainingEndDate before", snap(0, member(None, "0")), snap(60, member(E1)), U),
            ("no @TrainingEndDate after", snap(0, IDLE), snap(60, member(None)), U),
            ("idle, then idle", snap(0, IDLE), snap(60, IDLE), N),
            ("placeholder, then the same placeholder", snap(0, IDLE_PLACEHOLDER), snap(60, IDLE_PLACEHOLDER), N),
            ("the same running course", snap(0, member(E1)), snap(60, member(E1)), N),
            ("running, then the same course with a new end", snap(0, member(E1)), snap(60, member(E2)), U),
            ("another course running, then the requested one", snap(0, member(E1, "8")), snap(60, member(E2)), U),
            ("ended unclaimed, then the requested course", snap(0, member(DUE)), snap(60, member(E2)), U),
            ("unreadable end, then the requested course", snap(0, member("soon")), snap(60, member(E2)), U),
            ("idle with a naive observation time", naive(snap(0, IDLE)), snap(60, member(E1)), U),
            ("placeholder with a naive observation time", naive(snap(0, IDLE_PLACEHOLDER)), snap(60, member(E1)), U),
            ("idle, then the requested course runs", snap(0, IDLE), snap(60, member(E1)), V),
            ("placeholder, then the requested course runs", snap(0, IDLE_PLACEHOLDER), snap(60, member(E1)), V),
            ("idle, then another course runs", snap(0, IDLE), snap(60, member(E1, "8")), C),
            ("idle, then another course with an ended timer", snap(0, IDLE), snap(60, member(DUE, "8")), C),
            ("idle, then a timer with course 0", snap(0, IDLE), snap(60, member(E1, "0")), U),
            ("idle, then a timer with an empty course", snap(0, IDLE), snap(60, member(E1, "")), U),
            ("idle, then an unreadable timer", snap(0, IDLE), snap(60, member("soon")), U),
            ("idle, then a timer ending between the reads", snap(0, IDLE), snap(60, member(BETWEEN)), U),
            ("idle, then a timer ending at the post-read", snap(0, IDLE), snap(60, member(AT_AFTER)), U),
            ("idle, then a timer ending just after the post-read", snap(0, IDLE), snap(60, member(JUST_AFTER)), V),
            ("idle, then an already ended timer", snap(0, IDLE), snap(60, member(DUE)), U),
            ("idle, then a naive post-read", snap(0, IDLE), naive(snap(60, member(E1))), U),
            ("running, then idle", snap(0, member(E1)), snap(60, IDLE), U),
            ("idle, then a date-only end", snap(0, IDLE), snap(60, member("2026-11-11")), U),
            ("idle, then a basic-format end", snap(0, IDLE), snap(60, member("20261110T130000")), U),
            ("idle, then a space-separated end", snap(0, IDLE), snap(60, member("2026-11-10 13:00:00")), U),
            ("idle, then another course with no timer", snap(0, IDLE), snap(60, member("", "8")), C),
            ("idle, then another course with an unreadable timer", snap(0, IDLE), snap(60, member("soon", "8")), C),
            ("idle, then the requested course with no timer", snap(0, IDLE), snap(60, member("", "7")), U),
            ("idle, then a non-numeric course", snap(0, IDLE), snap(60, member(E1, "abc")), U),
            ("idle, then the requested course padded", snap(0, IDLE), snap(60, member(E1, " 7 ")), V),
            ("a missing course with an ended timer before", snap(0, member(DUE, None)), snap(60, member(E1)), U),
            ("a blank course with an ended timer before", snap(0, member(DUE, "")), snap(60, member(E1)), U),
        )
        for name, before, after, expected in rows:
            with self.subTest(name):
                self.assertIs(strict(before, after), expected)

    def test_a_padded_requested_course_id_is_compared_stripped(self):
        params = {"character_id": "c1", "training_design_id": " 7 "}
        self.assertIs(strict(snap(0, IDLE), snap(60, member(E1)), params), V)

    def test_reasons_name_what_was_decided(self):
        cases = (
            (snap(0, IDLE), snap(60, member(E1)), "c1 is training 7 until " + E1),
            (snap(0, member(E1, "8")), snap(60, member(E2)), "not observed idle before the call"),
            (snap(0, IDLE), snap(60, member(E1, "0")), "course ID"),
            (snap(0, IDLE), snap(60, member(BETWEEN)), "no running timer"),
        )
        for before, after, reason in cases:
            with self.subTest(reason=reason):
                _, text = training_start_verified(diff_snapshots(before, after), before, after, PARAMS)
                self.assertIn(reason, text)

    def test_completion_and_stat_gains_are_separate(self):
        self.assertIs(strict(snap(0, IDLE), snap(60, member(E1, stats=RAISED))), V)
        self.assertIs(strict(snap(0, IDLE), snap(60, member("", "0", stats=RAISED))), N)
        self.assertIs(strict(snap(0, member(DUE)), snap(60, member("", "0", stats=RAISED))), U)
        # A stat gain alone never verifies a start: only an observed idle -> requested running change does.
        gains = (member("", "0", stats=RAISED), member(DUE, stats=RAISED), member(E1, stats=RAISED))
        pairs = [(before, after) for before in (member(DUE), member(E1), member("soon")) for after in gains]
        pairs += [(IDLE, gains[0]), (IDLE, gains[1])]
        for before, after in pairs:
            with self.subTest(before=before["@TrainingEndDate"], after=after["@TrainingEndDate"]):
                self.assertIsNot(strict(snap(0, before), snap(60, after)), V)

    def test_strict_verdict_deliberately_disagrees_with_add_training(self):
        # Owner-required: training-v1's add_training verifies these starts; S14-R5 does not, because the member was
        # not observed idle before the call or no timer runs at the post-read. Both verdicts side by side.
        cases = (
            ("same course, new end", snap(0, member(E1)), snap(60, member(E2))),
            ("another course replaced", snap(0, member(E1, "8")), snap(60, member(E2))),
            ("timer ends between the reads", snap(0, IDLE), snap(60, member(BETWEEN))),
        )
        for name, before, after in cases:
            with self.subTest(name):
                self.assertIs(reconcile(get_spec("add_training"), before, after, PARAMS).status, V)
                self.assertIs(reconcile(STRICT, before, after, PARAMS).status, U)

    def test_add_training_spec_is_unchanged(self):
        spec = get_spec("add_training")
        self.assertIs(spec.postcondition, catalog._training_started)
        self.assertEqual(
            (STRICT.action_id, STRICT.client_method, STRICT.preconditions, STRICT.required_params),
            (spec.action_id, spec.client_method, spec.preconditions, spec.required_params),
        )
        self.assertIs(STRICT.postcondition, training_start_verified)

    def test_docstring_names_the_training_v2_switch(self):
        self.assertIn(
            'dataclasses.replace(get_spec("add_training"), postcondition=training_start_verified)',
            training_start_verified.__doc__ or "",
        )

    def test_snapshot_session_phase_agrees_with_catalog_phase_whenever_known(self):
        to_catalog = {
            SESSION_RUNNING: catalog._TrainingPhase.RUNNING,
            SESSION_ENDED_UNCLAIMED: catalog._TrainingPhase.ENDED,
            SESSION_NONE: catalog._TrainingPhase.IDLE,
            SESSION_FINISHED: catalog._TrainingPhase.IDLE,
        }
        compared = 0
        for seconds, design, end in itertools.product((0, 60), SESSION_DESIGNS, SESSION_ENDS):
            snapshot = snap(seconds, member(end, design))
            session = snapshot.training_session("c1")
            if session.phase == SESSION_UNKNOWN:
                continue
            compared += 1
            with self.subTest(seconds=seconds, design=design, end=end):
                self.assertIs(
                    to_catalog[session.phase], catalog._training_phase(snapshot.crew_member("c1"), snapshot.observed_at)
                )
        self.assertGreater(compared, 100)

    def test_strict_verdict_never_verifies_without_an_idle_before_and_the_requested_course_running_after(self):
        befores = (IDLE, IDLE_PLACEHOLDER, member("", None), member(DUE, None), member(DUE, ""), member(E1))
        verified = 0
        for before_record, design, end in itertools.product(befores, SESSION_DESIGNS, SESSION_ENDS):
            before, after = snap(0, before_record), snap(60, member(end, design))
            if strict(before, after) is not V:
                continue
            verified += 1
            with self.subTest(before=before_record, design=design, end=end):
                self.assertIn(before.training_session("c1").phase, (SESSION_NONE, SESSION_FINISHED))
                session = after.training_session("c1")
                self.assertEqual((session.phase, session.course_id), (SESSION_RUNNING, 7))
        self.assertGreater(verified, 0)
