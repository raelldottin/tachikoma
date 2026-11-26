"""sdk.execution.training: the legacy daily training step run through the action runner and catalog specs.

Nothing here touches the network: FakeTrainingClient is a client-shaped object over a small in-memory world. It
answers the reads the executor and the snapshot provider make with xmltodict-shaped envelopes (stored on the same
attributes the real Client uses) and applies AddTraining, FinishTraining and useConsumable like FakePss does:
``@TrainingDesignId`` is reported while a training is not yet collected and "0" afterwards, the end date stays.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdk.journal import GameplayEvent, GameplayJournal, JournalError
from sdk.reports.training import contract_violations, validate_decision_evidence
from sdk.execution import training as executor
from sdk.execution.training import (
    ActionResult,
    CrewOutcome,
    MemoryJournal,
    TrainingCounts,
    count,
    run_training,
    verification_of,
)
from sdk.training.model import Skip, StartTraining

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
FMT = "%Y-%m-%dT%H:%M:%S"
STAT_NAMES = ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina")
LOGGER = "sdk.execution.training"

# Room id -> room design id; room design id -> name. 201 Galaxy Gym and 204 GYM are gyms, 202 Academy an academy.
ROOMS = {"201": "31", "202": "40", "203": "1", "204": "30"}
ROOM_DESIGNS = {"31": "Galaxy Gym", "40": "Academy", "1": "Bridge", "30": "GYM"}
# Training design id -> (name, stat, gain, fatigue, duration).
COURSES = {
    "3": ("Bench Press", "Hp", 2, 10, datetime.timedelta(hours=1)),
    "4": ("Muscle Beach", "Hp", 4, 20, datetime.timedelta(hours=3)),
    "11": ("Read Expert Weapon Theory", "Weapon", 2, 10, datetime.timedelta(hours=1)),
    "12": ("Weapons Summit", "Weapon", 4, 20, datetime.timedelta(hours=3)),
    "21": ("Restful Study", "Science", 2, 0, datetime.timedelta(hours=1)),  # no fatigue: grey consumables stay open
}
# The strategy's research gates name the "Fitness"/"Education" families (parse.py's assumed "<Family> LvN").
RESEARCH_DESIGNS = (("1", "1", "Fitness"), ("2", "1", "Fitness Lv2"), ("3", "1", "Fitness Lv3"))
CONSUMABLE_DESIGNS = {"ItemService": {"ListItemDesigns": {"ItemDesigns": {"ItemDesign": [
    {"@ItemDesignId": "101", "@ItemName": "Common Weapon Consumable", "@ItemSubType": "Consumable"},
]}}}}


def stamp(moment: datetime.datetime) -> str:
    return moment.astimezone(UTC).strftime(FMT)


def crew_record(cid: str, name: str, *, room: str = "201", design: str = "1", fatigue: int = 0, training: str = "0",
                end: str = "", **stats: int) -> dict[str, str]:
    """One ListAllCharactersOfUser record; ``stats`` are stat names without the Improvement suffix."""
    record = {"@CharacterId": cid, "@CharacterName": name, "@CharacterDesignId": design, "@RoomId": room,
              "@Level": "40", "@Fatigue": str(fatigue), "@TrainingDesignId": training, "@TrainingEndDate": end}
    for stat in STAT_NAMES:
        record[f"@{stat}Improvement"] = str(stats.get(stat, 0))
    return record


def delish(**overrides: Any) -> dict[str, str]:
    """A weapons crew member at 72% in the Galaxy Gym: the ungated secondary T1 tier, Bench Press (design 3)."""
    values: dict[str, Any] = {"Weapon": 72}
    values.update(overrides)
    return crew_record("1001", "Delish", **values)


class Clock:
    def __init__(self, now: datetime.datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime.datetime:
        return self.now


_DEFAULT = object()


@dataclass
class Act:
    """One scripted answer: apply the world change (or not), then raise ``raises`` or return ``result``.

    ``payload`` replaces the envelope a read stores (for example an error envelope) while still returning True.
    """

    apply: bool = True
    result: Any = _DEFAULT
    raises: BaseException | None = None
    payload: Any = _DEFAULT
    then: Any = None  # called first: a change made elsewhere (another device) just before this call


@dataclass
class FakeTrainingClient:
    crew: list[dict[str, str]]
    clock: Clock = field(default_factory=Clock)
    researches: tuple[str, ...] = ()
    items: dict[str, int] = field(default_factory=dict)
    character_designs: list[dict[str, str]] = field(default_factory=lambda: [
        {"@CharacterDesignId": "1", "@TrainingCapacity": "100", "@Rarity": "Common"},
        {"@CharacterDesignId": "2", "@TrainingCapacity": "80", "@Rarity": "Common"},
    ])
    training_designs: list[str] = field(default_factory=lambda: list(COURSES))
    calls: list[tuple] = field(default_factory=list)
    scripts: dict[str, list[Act]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.crew = [dict(record) for record in self.crew]  # the world owns its records: callers may reuse theirs

    def script(self, name: str, *acts: Act) -> None:
        self.scripts.setdefault(name, []).extend(acts)

    def record(self, cid: str) -> dict[str, str]:
        return next(record for record in self.crew if record["@CharacterId"] == cid)

    def _run(self, name: str, args: tuple, apply, default):
        self.calls.append((name, *args))
        queue = self.scripts.get(name) or []
        act = queue.pop(0) if queue else Act()
        if act.then is not None:
            act.then()
        result = default
        if act.apply:
            result = apply(act)
        if act.raises is not None:
            raise act.raises
        return result if act.result is _DEFAULT else act.result

    def _read(self, name: str, attribute: str, envelope) -> Any:
        def apply(act: Act) -> bool:
            setattr(self, attribute, envelope() if act.payload is _DEFAULT else act.payload)
            return True

        return self._run(name, (), apply, False)

    # --- reads -------------------------------------------------------------------------------------------------

    def listAllCharactersOfUser(self) -> bool:
        return self._read("listAllCharactersOfUser", "allCharactersOfUser", lambda: {"CharacterService": {
            "ListAllCharactersOfUser": {"Characters": {"Character": [dict(r) for r in self.crew]} if self.crew
                                        else None}}})

    def getShipByUserId(self) -> bool:
        rooms = [{"@RoomId": room, "@RoomDesignId": design} for room, design in ROOMS.items()]
        research = [{"@ResearchDesignId": design, "@ResearchState": "Completed"} for design in self.researches]
        return self._read("getShipByUserId", "shipByUserId", lambda: {"ShipService": {"GetShipByUserId": {"Ship": {
            "@ShipId": "1", "Rooms": {"Room": rooms}, "Researches": {"Research": research} if research else None}}}})

    def listItemsOfAShip(self) -> bool:
        return self._read("listItemsOfAShip", "itemsOfAShip", lambda: {"ItemService": {"ListItemsOfAShip": {
            "Items": {"Item": [{"@ItemId": f"9{design}", "@ItemDesignId": design, "@Quantity": str(quantity)}
                               for design, quantity in self.items.items()]} if self.items else None}}})

    def listAllCharacterDesigns2(self) -> bool:
        return self._read("listAllCharacterDesigns2", "allCharacterDesigns", lambda: {"CharacterService": {
            "ListAllCharacterDesigns": {"CharacterDesigns": {"CharacterDesign": list(self.character_designs)}}}})

    def listAllTrainingDesigns2(self) -> bool:
        designs = [{"@TrainingDesignId": design, "@TrainingName": COURSES[design][0],
                    "@Duration": str(int(COURSES[design][4].total_seconds()))} for design in self.training_designs]
        return self._read("listAllTrainingDesigns2", "trainingDesigns", lambda: {"TrainingService": {
            "ListAllTrainingDesigns": {"TrainingDesigns": {"TrainingDesign": designs} if designs else None}}})

    def listRoomDesigns2(self) -> bool:
        designs = [{"@RoomDesignId": design, "@RoomName": name} for design, name in ROOM_DESIGNS.items()]
        return self._read("listRoomDesigns2", "roomDesigns", lambda: {"RoomService": {"ListRoomDesigns": {
            "RoomDesigns": {"RoomDesign": designs}}}})

    def listAllResearchDesigns2(self) -> bool:
        designs = [{"@ResearchDesignId": i, "@RootResearchDesignId": r, "@ResearchName": n}
                   for i, r, n in RESEARCH_DESIGNS]
        return self._read("listAllResearchDesigns2", "allResearchDesigns", lambda: {"ResearchService": {
            "ListAllResearchDesigns": {"ResearchDesigns": {"ResearchDesign": designs}}}})

    # --- mutations ---------------------------------------------------------------------------------------------

    def addTraining(self, training_design_id, character_id) -> bool:
        def apply(act: Act) -> bool:
            record = self.record(character_id)
            record["@TrainingDesignId"] = training_design_id
            record["@TrainingEndDate"] = stamp(self.clock.now + COURSES[training_design_id][4])
            return True

        return self._run("addTraining", (training_design_id, character_id), apply, False)

    def finishTraining(self, character_id):
        def apply(act: Act):
            record = self.record(character_id)
            design = record["@TrainingDesignId"]
            ended = record["@TrainingEndDate"] and stamp(self.clock.now) >= record["@TrainingEndDate"]
            if design == "0" or not ended:
                return None  # "No training to finish" / "Training is not finished yet"
            _, stat, gain, fatigue, _ = COURSES[design]
            record[f"@{stat}Improvement"] = str(int(record[f"@{stat}Improvement"]) + gain)
            record["@Fatigue"] = str(int(record["@Fatigue"]) + fatigue)
            record["@TrainingDesignId"] = "0"
            return dict(record)

        return self._run("finishTraining", (character_id,), apply, None)

    def useConsumable(self, item_design_id, character_id) -> bool:
        def apply(act: Act) -> bool:
            self.items[item_design_id] -= 1
            return True

        return self._run("useConsumable", (item_design_id, character_id), apply, False)

    def mutations(self) -> list[tuple]:
        return [call for call in self.calls if call[0] in ("addTraining", "finishTraining", "useConsumable")]


READS = ("listAllCharactersOfUser", "getShipByUserId", "listAllCharacterDesigns2", "listAllTrainingDesigns2",
         "listRoomDesigns2", "listAllResearchDesigns2")
LABELS = ("characters", "ship", "character designs", "training designs", "room designs", "research designs")


def run(client: FakeTrainingClient, **kwargs: Any):
    kwargs.setdefault("label", "Cap")
    return run_training(client, now=client.clock, **kwargs)


def logged(test: unittest.TestCase, client: FakeTrainingClient, **kwargs: Any):
    """run() with every executor log line captured: (result, ["LEVEL message", ...])."""
    with test.assertLogs(LOGGER, level="INFO") as logs:
        result = run(client, **kwargs)
    return result, [f"{record.levelname} {record.getMessage()}" for record in logs.records]


_QUIET = logging.NullHandler()


def setUpModule() -> None:
    # Runs that do not capture the executor's log (assertLogs does) stay quiet instead of printing to stderr.
    logging.getLogger(LOGGER).addHandler(_QUIET)
    logging.getLogger(LOGGER).propagate = False


def tearDownModule() -> None:
    logging.getLogger(LOGGER).removeHandler(_QUIET)
    logging.getLogger(LOGGER).propagate = True


SUMMARY_ZERO = "INFO [Cap] training: started=0 finished=0 consumables=0 skipped=0 failed=0"


class ReadabilityTests(unittest.TestCase):
    def test_unreadable_characters_returns_unreadable_and_sends_nothing(self):
        client = FakeTrainingClient([delish()])
        client.script("listAllCharactersOfUser", Act(apply=False, result=False))
        result, lines = logged(self, client)
        self.assertIs(result.readable, False)
        self.assertEqual(result.unreadable, ("characters",))
        self.assertEqual(result.crew, ())
        self.assertEqual(client.mutations(), [])
        self.assertEqual(lines, ["ERROR [Cap] training: cannot read characters; no training this run", SUMMARY_ZERO])

    def test_each_required_payload_unreadable_returns_false(self):
        for method, label in zip(READS, LABELS):
            with self.subTest(method=method):
                client = FakeTrainingClient([delish()])
                client.script(method, Act(apply=False, result=False))
                result, lines = logged(self, client)
                self.assertEqual((result.readable, result.unreadable), (False, (label,)))
                self.assertEqual(lines[0], f"ERROR [Cap] training: cannot read {label}; no training this run")
                self.assertEqual(client.mutations(), [])
                self.assertEqual([call[0] for call in client.calls], list(READS))

    def test_every_unreadable_payload_is_named_in_read_order(self):
        client = FakeTrainingClient([delish()])
        client.script("listAllResearchDesigns2", Act(apply=False, result=False))
        client.script("getShipByUserId", Act(apply=False, result=False))
        result, lines = logged(self, client)
        self.assertEqual(result.unreadable, ("ship", "research designs"))
        self.assertEqual(lines[0], "ERROR [Cap] training: cannot read ship, research designs; no training this run")

    def test_error_envelope_is_unreadable_even_if_the_method_returned_true(self):
        error = {"CharacterService": {"ListAllCharactersOfUser": {"@errorMessage": "Internal server error"}}}
        client = FakeTrainingClient([delish()])
        client.script("listAllCharactersOfUser", Act(payload=error))
        result = run(client)
        self.assertEqual((result.readable, result.unreadable), (False, ("characters",)))
        self.assertEqual(client.mutations(), [])

    def test_a_ship_without_rooms_is_unreadable(self):
        client = FakeTrainingClient([delish()])
        client.script("getShipByUserId", Act(payload={"ShipService": {"GetShipByUserId": {"Ship": {"@ShipId": "1"}}}}))
        self.assertEqual(run(client).unreadable, ("ship",))

    def test_stale_attribute_is_ignored_when_the_read_fails(self):
        client = FakeTrainingClient([delish()])
        client.listAllTrainingDesigns2()  # a good payload left behind by an earlier read
        client.calls.clear()
        client.script("listAllTrainingDesigns2", Act(apply=False, result=False))
        self.assertEqual(run(client).unreadable, ("training designs",))

    def test_a_truthy_non_true_read_result_is_not_success(self):
        client = FakeTrainingClient([delish()])
        client.script("listRoomDesigns2", Act(result=1))
        self.assertEqual(run(client).unreadable, ("room designs",))

    def test_reads_happen_once_in_order_before_any_mutation(self):
        client = FakeTrainingClient([delish()])
        run(client)
        self.assertEqual([call[0] for call in client.calls[:6]], list(READS))
        self.assertNotIn("listItemsOfAShip", [call[0] for call in client.calls])
        self.assertNotIn("getShipByUserId", [call[0] for call in client.calls[6:]])

    def test_items_read_only_when_item_designs_yield_consumables(self):
        for designs, expected in ((None, 0), ({"ItemService": {}}, 0), (CONSUMABLE_DESIGNS, 1)):
            with self.subTest(designs=designs):
                client = FakeTrainingClient([crew_record("1001", "Rookie Pilot", Hp=10)], items={"101": 5})
                run(client, item_designs_raw=designs)
                self.assertEqual([call[0] for call in client.calls].count("listItemsOfAShip"), expected)
                if expected:
                    self.assertEqual(client.calls[6], ("listItemsOfAShip",))

    def test_empty_training_designs_is_readable(self):
        client = FakeTrainingClient([delish()], training_designs=[])
        result, lines = logged(self, client)
        self.assertIs(result.readable, True)
        self.assertEqual(result.unreadable, ())
        self.assertEqual(client.mutations(), [])
        self.assertIn("INFO [Cap] training: Delish (1001) skip design_missing: no design named Bench Press for gym",
                      lines)
        self.assertEqual(lines[-1], "INFO [Cap] training: started=0 finished=0 consumables=0 skipped=1 failed=0")

    def test_empty_roster_is_readable_and_counts_zero(self):
        client = FakeTrainingClient([])
        result, lines = logged(self, client)
        self.assertEqual((result.readable, result.unreadable, result.crew), (True, (), ()))
        self.assertEqual(lines, [SUMMARY_ZERO])
        self.assertEqual([call[0] for call in client.calls], list(READS))

    def test_a_read_that_raises_propagates(self):
        client = FakeTrainingClient([delish()])
        client.script("listAllCharacterDesigns2", Act(apply=False, raises=ValueError("not xml")))
        with self.assertRaises(ValueError):
            run(client)
        self.assertEqual(client.mutations(), [])

    def test_no_label_means_no_prefix(self):
        client = FakeTrainingClient([])
        with self.assertLogs(LOGGER, level="INFO") as logs:
            run_training(client, now=client.clock)
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["training: started=0 finished=0 consumables=0 skipped=0 failed=0"])


ADD_3 = {"training_design_id": "3", "character_id": "1001"}
STARTED = "INFO [Cap] Starting training Bench Press for Delish in Galaxy Gym with 72% training complete"


def maiden(**overrides: Any) -> dict[str, str]:
    """A second weapons crew member at 72% in the GYM (room 204): also Bench Press."""
    values: dict[str, Any] = {"room": "204", "Weapon": 72}
    values.update(overrides)
    return crew_record("1002", "Galactic Snow Maiden", **values)


class StartTests(unittest.TestCase):
    def test_idle_crew_in_band_gets_add_training_verified(self):
        client = FakeTrainingClient([delish()])
        result, lines = logged(self, client)
        self.assertEqual(client.calls[6:], [("listAllCharactersOfUser",), ("addTraining", "3", "1001"),
                                            ("listAllCharactersOfUser",)])
        self.assertEqual(lines, [STARTED, "INFO [Cap] training: started=1 finished=0 consumables=0 skipped=0 failed=0"])
        outcome = result.crew[0]
        self.assertEqual((outcome.character_id, outcome.name, outcome.finish, outcome.error), (1001, "Delish", None, None))
        self.assertEqual(outcome.decision, StartTraining(1001, 3, "secondary T1 Bench Press at 72% in gym"))
        self.assertEqual(outcome.action, ActionResult("add_training", ADD_3, "verified",
                                                      "1001 is training 3 until 2026-11-20T13:00:00", True, False))
        self.assertIs(outcome.failed, False)
        self.assertEqual(result.counts, TrainingCounts(started=1))

    def test_skip_sends_nothing_and_counts_skipped(self):
        client = FakeTrainingClient([crew_record("1010", "Rookie Pilot", Hp=10)])
        result, lines = logged(self, client)
        self.assertEqual(client.calls, [(name,) for name in READS])
        self.assertEqual(lines, ["INFO [Cap] training: Rookie Pilot (1010) skip unknown_role: 'Rookie Pilot' has no "
                                 "training role",
                                 "INFO [Cap] training: started=0 finished=0 consumables=0 skipped=1 failed=0"])
        self.assertEqual((result.crew[0].action, result.crew[0].failed), (None, False))
        self.assertEqual(result.counts, TrainingCounts(skipped=1))

    def test_unverified_add_training_counts_failed(self):
        client = FakeTrainingClient([delish()])
        client.script("addTraining", Act(apply=False, result=True))
        result, lines = logged(self, client)
        self.assertEqual(lines, ["WARNING [Cap] training: Delish (1001) add_training no-change: 1001 is not training "
                                 "after the call",
                                 "INFO [Cap] training: started=0 finished=0 consumables=0 skipped=0 failed=1"])
        self.assertEqual(result.crew[0].action.verification, "no-change")
        self.assertIs(result.crew[0].failed, True)

    def test_add_training_refused_false_counts_failed(self):
        client = FakeTrainingClient([delish()])
        client.script("addTraining", Act(apply=False, result=False))
        result = run(client)
        self.assertEqual(result.crew[0].action, ActionResult("add_training", ADD_3, "no-change",
                                                             "1001 is not training after the call", True, False))
        self.assertEqual(result.counts, TrainingCounts(failed=1))

    def test_add_training_applied_but_reported_false_is_still_verified(self):
        client = FakeTrainingClient([delish()])
        client.script("addTraining", Act(result=False))
        self.assertEqual(run(client).counts, TrainingCounts(started=1))

    def test_mutation_exception_is_unknown_and_next_crew_continues(self):
        client = FakeTrainingClient([delish(), maiden()])
        client.script("addTraining", Act(apply=False, raises=RuntimeError("boom")))
        result, lines = logged(self, client)
        self.assertEqual(client.mutations(), [("addTraining", "3", "1001"), ("addTraining", "3", "1002")])
        self.assertEqual(lines, [
            "WARNING [Cap] training: Delish (1001) add_training unknown: call raised RuntimeError; 1001 is not "
            "training after the call",
            "INFO [Cap] Starting training Bench Press for Galactic Snow Maiden in GYM with 72% training complete",
            "INFO [Cap] training: started=1 finished=0 consumables=0 skipped=0 failed=1"])
        self.assertEqual([outcome.failed for outcome in result.crew], [True, False])

    def test_uncertain_prior_attempt_fails_one_crew_not_the_next(self):
        journal = MemoryJournal()
        journal.append(GameplayEvent.create(account_alias="legacy", action_id="add_training", started_at=NOW,
                                            finished_at=NOW, outcome="unknown", reason="timeout", attempted=True,
                                            params=ADD_3))
        client = FakeTrainingClient([delish(), maiden()])
        result, lines = logged(self, client, journal=journal)
        self.assertEqual(client.mutations(), [("addTraining", "3", "1002")])
        self.assertEqual(lines[0], "ERROR [Cap] training: Delish (1001) failed: UncertainPriorAttemptError: "
                                   "add_training has an uncertain prior attempt")
        self.assertEqual(lines[-1], "INFO [Cap] training: started=1 finished=0 consumables=0 skipped=0 failed=1")
        self.assertEqual(result.crew[0].error, "UncertainPriorAttemptError: add_training has an uncertain prior attempt")
        self.assertEqual((result.crew[0].action, result.crew[0].failed), (None, True))
        self.assertEqual(result.crew[1].action.verification, "verified")

    def test_another_account_alias_is_not_blocked_by_the_legacy_journal(self):
        journal = MemoryJournal()
        journal.append(GameplayEvent.create(account_alias="legacy", action_id="add_training", started_at=NOW,
                                            finished_at=NOW, outcome="unknown", reason="timeout", attempted=True,
                                            params=ADD_3))
        client = FakeTrainingClient([delish()])
        self.assertEqual(run(client, journal=journal, account_alias="primary").counts, TrainingCounts(started=1))

    def test_crew_error_message_is_redacted(self):
        class Refusing(MemoryJournal):
            def append(self, event):
                if event.params.get("character_id") == "1001":
                    raise JournalError("refused accessToken=00000002-1111-2222-3333-444444444444")
                super().append(event)

        client = FakeTrainingClient([delish(), maiden()])
        result, lines = logged(self, client, journal=Refusing())
        self.assertIn("ERROR [Cap] training: Delish (1001) failed: JournalError: refused accessToken=***REDACTED***",
                      lines)
        self.assertEqual(result.crew[0].error, "JournalError: refused accessToken=***REDACTED***")
        self.assertEqual(result.counts, TrainingCounts(started=1, failed=1))


ENDED_2H = stamp(NOW - datetime.timedelta(hours=2))


def ended(**overrides: Any) -> dict[str, str]:
    """Delish with an uncollected Bench Press that ended 2 h ago: FinishTraining, then the advisory Bench Press."""
    values: dict[str, Any] = {"training": "3", "end": ENDED_2H}
    values.update(overrides)
    return delish(**values)


def collected_elsewhere(client: FakeTrainingClient, *, gain: bool = True):
    """The uncollected training is collected by another device: the design is cleared (and the stat rises)."""
    def collect():
        record = client.record("1001")
        record["@TrainingDesignId"] = "0"
        if gain:
            record["@HpImprovement"] = str(int(record["@HpImprovement"]) + 2)
    return collect


FINISH = {"character_id": "1001"}


class FinishTests(unittest.TestCase):
    def test_finish_then_replan_from_reply_does_not_run_advisory_primary(self):
        client = FakeTrainingClient([ended()])
        result, lines = logged(self, client)
        self.assertEqual(client.calls[6:], [("listAllCharactersOfUser",), ("finishTraining", "1001"),
                                            ("listAllCharactersOfUser",)])
        self.assertEqual(lines, [
            "INFO [Cap] Completed training for Delish in Galaxy Gym with Hp +2",
            "INFO [Cap] training: Delish (1001) skip research_missing: secondary T2 research gate not met",
            "INFO [Cap] training: started=0 finished=1 consumables=0 skipped=1 failed=0"])
        outcome = result.crew[0]
        self.assertEqual(outcome.finish, ActionResult("finish_training", FINISH, "verified",
                                                      "training of 1001 finished; stat improvements +2", True, False))
        self.assertEqual(outcome.decision, Skip(1001, "research_missing", "secondary T2 research gate not met"))
        self.assertEqual((outcome.action, outcome.error, outcome.failed), (None, None, False))

    def test_replan_uses_reply_stats_for_next_tier(self):
        client = FakeTrainingClient([ended()], researches=("1", "2"))
        result, lines = logged(self, client)
        self.assertEqual(client.mutations(), [("finishTraining", "1001"), ("addTraining", "4", "1001")])
        self.assertEqual(lines, [
            "INFO [Cap] Completed training for Delish in Galaxy Gym with Hp +2",
            "INFO [Cap] Starting training Muscle Beach for Delish in Galaxy Gym with 74% training complete",
            "INFO [Cap] training: started=1 finished=1 consumables=0 skipped=0 failed=0"])
        self.assertEqual(result.crew[0].decision, StartTraining(1001, 4, "secondary T2 Muscle Beach at 74% in gym"))

    def test_several_stat_changes_are_listed_in_stat_order(self):
        client = FakeTrainingClient([ended()])

        def finish(character_id):
            client.calls.append(("finishTraining", character_id))
            record = client.record(character_id)
            record.update({"@HpImprovement": "2", "@WeaponImprovement": "71", "@TrainingDesignId": "0"})
            return dict(record)

        client.finishTraining = finish
        _, lines = logged(self, client)
        self.assertEqual(lines[0], "INFO [Cap] Completed training for Delish in Galaxy Gym with Hp +2, Weapon -1")

    def test_finish_refused_keeps_crew_from_starting(self):
        client = FakeTrainingClient([ended()])
        client.script("finishTraining", Act(apply=False, result=None))
        result, lines = logged(self, client)
        reason = "the ended training of 1001 is still in place and stat improvements are unchanged"
        self.assertEqual(client.mutations(), [("finishTraining", "1001")])
        self.assertEqual(lines, [
            f"WARNING [Cap] training: Delish (1001) finish_training no-change: {reason}",
            f"INFO [Cap] training: Delish (1001) skip training_in_progress: finish_training no-change: {reason}",
            "INFO [Cap] training: started=0 finished=0 consumables=0 skipped=1 failed=1"])
        self.assertEqual(result.crew[0].finish, ActionResult("finish_training", FINISH, "no-change", reason, True,
                                                             False))
        self.assertEqual(result.crew[0].decision,
                         Skip(1001, "training_in_progress", f"finish_training no-change: {reason}"))

    def test_finish_not_yet_past_grace_is_harmless(self):
        client = FakeTrainingClient([ended(end=stamp(NOW - datetime.timedelta(seconds=30)))])
        result, lines = logged(self, client)
        reason = ("preconditions failed: 1001's training ends at 2026-11-20T11:59:30, less than 60 seconds before the "
                  "observation; finishing now could cost starbux")
        self.assertEqual(client.mutations(), [])
        self.assertEqual(lines, [
            f"INFO [Cap] training: Delish (1001) finish_training skipped: {reason}",
            f"INFO [Cap] training: Delish (1001) skip training_in_progress: finish_training skipped: {reason}",
            "INFO [Cap] training: started=0 finished=0 consumables=0 skipped=1 failed=0"])
        self.assertEqual(result.crew[0].finish, ActionResult("finish_training", FINISH, "skipped", reason, False, True))

    def test_finish_exactly_at_the_grace_is_sent(self):
        client = FakeTrainingClient([ended(end=stamp(NOW - datetime.timedelta(seconds=60)))])
        self.assertEqual(run(client).counts, TrainingCounts(finished=1, skipped=1))

    def test_nothing_to_finish_collected_elsewhere_is_harmless(self):
        client = FakeTrainingClient([ended()])
        client.script("listAllCharactersOfUser", Act(), Act(then=collected_elsewhere(client)))
        result, lines = logged(self, client)
        reason = "preconditions failed: 1001 has no reported training design, so there is no training to finish"
        self.assertEqual(client.mutations(), [])
        self.assertEqual(lines[0], f"INFO [Cap] training: Delish (1001) finish_training skipped: {reason}")
        self.assertEqual(result.counts, TrainingCounts(skipped=1))
        self.assertIs(result.crew[0].finish.harmless, True)

    def test_unobserved_crew_before_the_finish_is_a_failure(self):
        client = FakeTrainingClient([ended()])
        client.script("listAllCharactersOfUser", Act(), Act(apply=False, result=False))
        result, lines = logged(self, client)
        reason = ("preconditions failed: crew section was not observed before the action; crew member 1001 is not in "
                  "the snapshot")
        self.assertEqual(client.mutations(), [])
        self.assertEqual(lines[0], f"WARNING [Cap] training: Delish (1001) finish_training skipped: {reason}")
        self.assertEqual(result.counts, TrainingCounts(skipped=1, failed=1))

    def test_nothing_to_finish_reply_after_precondition_passed_is_harmless(self):
        client = FakeTrainingClient([ended()])
        client.script("finishTraining", Act(apply=False, result=None, then=collected_elsewhere(client, gain=False)))
        result, lines = logged(self, client)
        self.assertEqual(client.mutations(), [("finishTraining", "1001")])
        self.assertEqual(result.crew[0].finish.verification, "unknown")
        self.assertIs(result.crew[0].finish.harmless, True)
        self.assertEqual(lines[0], "INFO [Cap] training: Delish (1001) finish_training unknown: training of 1001 "
                                   "finished with no stat gain; capacity not observed (a training at capacity gains "
                                   "nothing)")
        self.assertEqual(result.counts, TrainingCounts(skipped=1))

    def test_a_contradicted_finish_is_never_harmless(self):
        # No reply and the design cleared, as for "No training to finish" -- but the stats fell: a real failure.
        client = FakeTrainingClient([ended()])

        def collected_with_a_loss():
            client.record("1001").update({"@TrainingDesignId": "0", "@WeaponImprovement": "70"})

        client.script("finishTraining", Act(apply=False, result=None, then=collected_with_a_loss))
        result, lines = logged(self, client)
        self.assertEqual(result.crew[0].finish.verification, "contradicted")
        self.assertIs(result.crew[0].finish.harmless, False)
        self.assertEqual(lines[0], "WARNING [Cap] training: Delish (1001) finish_training contradicted: stat "
                                   "improvements of 1001 fell by 2")
        self.assertEqual(result.counts, TrainingCounts(skipped=1, failed=1))

    def test_a_finish_refused_for_unreadable_training_state_is_a_failure(self):
        # Refused, but neither "not due" nor "nothing to finish": the uncollected training may still be there.
        def drop_end(client):
            return lambda: client.record("1001").pop("@TrainingEndDate")

        def garble_end(client):
            return lambda: client.record("1001").update({"@TrainingEndDate": "soon"})

        cases = ((drop_end, "training state of 1001 was not observed before the action"),
                 (garble_end, "training end of 1001 is not a readable server time"))
        for change, reason in cases:
            with self.subTest(reason=reason):
                client = FakeTrainingClient([ended()])
                client.script("listAllCharactersOfUser", Act(), Act(then=change(client)))
                result, lines = logged(self, client)
                self.assertEqual(client.mutations(), [])
                self.assertEqual(lines[0], f"WARNING [Cap] training: Delish (1001) finish_training skipped: "
                                           f"preconditions failed: {reason}")
                self.assertIs(result.crew[0].finish.harmless, False)
                self.assertEqual(result.counts, TrainingCounts(skipped=1, failed=1))

    def test_finish_reply_without_id_or_stats_is_not_replanned(self):
        broken = ({"@Fatigue": "0", "@HpImprovement": "n/a"},
                  dict(ended(), **{"@CharacterId": "1002"}),
                  dict(ended(), **{"@HpImprovement": "n/a"}),
                  {key: value for key, value in ended().items() if key != "@StaminaImprovement"})
        for reply in broken:
            with self.subTest(reply=reply):
                client = FakeTrainingClient([ended()], researches=("1", "2"))
                client.script("finishTraining", Act(result=reply))
                result, lines = logged(self, client)
                self.assertEqual(client.mutations(), [("finishTraining", "1001")])
                self.assertEqual(lines[0], "INFO [Cap] Completed training for Delish in Galaxy Gym with stat changes "
                                           "not reported")
                self.assertEqual(result.crew[0].decision, Skip(
                    1001, "training_in_progress",
                    "finish_training verified: training of 1001 finished; stat improvements +2"))
                self.assertEqual(result.counts, TrainingCounts(finished=1, skipped=1))

    def test_a_raising_finish_stops_only_that_crew(self):
        client = FakeTrainingClient([ended(), maiden()])
        client.script("finishTraining", Act(apply=False, raises=ConnectionError("reset")))
        result = run(client)
        self.assertEqual(client.mutations(), [("finishTraining", "1001"), ("addTraining", "3", "1002")])
        self.assertEqual(result.crew[0].finish.verification, "unknown")
        self.assertEqual(result.counts, TrainingCounts(started=1, skipped=1, failed=1))


def low(cid: str = "1001", name: str = "Delish", **overrides: Any) -> dict[str, str]:
    """A weapons crew member at 20% in the Academy without research: the primary T1 gate is unmet, so the grey
    (Common) weapon consumable is the fallback."""
    values: dict[str, Any] = {"room": "202", "Weapon": 20}
    values.update(overrides)
    return crew_record(cid, name, **values)


USE_101 = {"item_design_id": "101", "character_id": "1001"}


class ConsumableTests(unittest.TestCase):
    def test_consumable_used_when_nothing_startable(self):
        client = FakeTrainingClient([low()], items={"101": 5})
        result, lines = logged(self, client, item_designs_raw=CONSUMABLE_DESIGNS)
        self.assertEqual(client.calls[6:], [
            ("listItemsOfAShip",), ("listAllCharactersOfUser",), ("listItemsOfAShip",), ("useConsumable", "101", "1001"),
            ("listAllCharactersOfUser",), ("listItemsOfAShip",)])
        reason = "item 101 quantity fell by 1; 1001 stat improvements +0, fatigue +0"
        self.assertEqual(lines, [f"INFO [Cap] training: Delish (1001) use_consumable verified: {reason}",
                                 "INFO [Cap] training: started=0 finished=0 consumables=1 skipped=0 failed=0"])
        self.assertEqual(result.crew[0].action, ActionResult("use_consumable", USE_101, "verified", reason, True, False))
        self.assertEqual(client.items, {"101": 4})

    def test_unverified_consumable_counts_failed(self):
        client = FakeTrainingClient([low()], items={"101": 5})
        client.script("useConsumable", Act(apply=False, result=False))
        result, lines = logged(self, client, item_designs_raw=CONSUMABLE_DESIGNS)
        self.assertEqual(lines[0], "WARNING [Cap] training: Delish (1001) use_consumable no-change: item 101 quantity "
                                   "unchanged; 1001 stat improvements +0, fatigue +0")
        self.assertEqual(result.counts, TrainingCounts(failed=1))

    def test_consumable_stock_shared_across_replans(self):
        # 1001 finishes first, but 1002's decision is final, so the planner gave 1002 the only item; the replan of
        # 1001 must see the stock 1002 left (none), not the ship's.
        finished = low(training="21", end=ENDED_2H)
        client = FakeTrainingClient([finished, low("1002", "Galactic Snow Maiden")], items={"101": 1})
        result = run(client, item_designs_raw=CONSUMABLE_DESIGNS)
        self.assertEqual(client.mutations(), [("finishTraining", "1001"), ("useConsumable", "101", "1002")])
        self.assertEqual(result.crew[0].decision, Skip(1001, "research_missing", "primary T1 research gate not met"))
        self.assertEqual(result.counts, TrainingCounts(finished=1, consumables=1, skipped=1))

    def test_each_replan_takes_from_the_stock_the_earlier_replans_left(self):
        crew = [low(training="21", end=ENDED_2H), low("1002", "Galactic Snow Maiden", training="21", end=ENDED_2H)]
        client = FakeTrainingClient(crew, items={"101": 1})
        result = run(client, item_designs_raw=CONSUMABLE_DESIGNS)
        self.assertEqual(client.mutations(), [("finishTraining", "1001"), ("useConsumable", "101", "1001"),
                                              ("finishTraining", "1002")])
        self.assertEqual(result.crew[1].decision, Skip(1002, "research_missing", "primary T1 research gate not met"))
        self.assertEqual(result.counts, TrainingCounts(finished=2, consumables=1, skipped=1))


class SnapshotTests(unittest.TestCase):
    def characters_reads(self, client: FakeTrainingClient) -> int:
        return [call[0] for call in client.calls].count("listAllCharactersOfUser")

    def test_before_snapshot_reuses_previous_after_snapshot(self):
        client = FakeTrainingClient([delish(), maiden()])
        self.assertEqual(run(client).counts, TrainingCounts(started=2))
        self.assertEqual(self.characters_reads(client), 4)  # the read, 1001 before and after, 1002 after only

    def test_a_skip_by_precondition_reuses_the_snapshot_too(self):
        client = FakeTrainingClient([ended(end=stamp(NOW - datetime.timedelta(seconds=30))), maiden()])
        self.assertEqual(run(client).counts, TrainingCounts(started=1, skipped=1))
        self.assertEqual(self.characters_reads(client), 3)  # the read, one before for both crew, 1002 after

    def test_mutation_invalidates_cache_even_when_the_call_raises(self):
        client = FakeTrainingClient([delish(), maiden()])
        client.script("addTraining", Act(raises=TimeoutError("read timed out")))
        result = run(client)
        self.assertEqual(result.crew[0].action.verification, "verified")  # the after-snapshot was read again
        self.assertEqual(result.counts, TrainingCounts(started=2))
        self.assertEqual(self.characters_reads(client), 4)

    def test_snapshot_read_failure_for_one_action_does_not_stop_others(self):
        client = FakeTrainingClient([delish(), maiden()])
        client.script("listAllCharactersOfUser", Act(), Act(apply=False, result=False))
        result = run(client)
        self.assertEqual(client.mutations(), [("addTraining", "3", "1002")])
        self.assertEqual(result.crew[0].action.verification, "skipped")
        self.assertEqual(result.counts, TrainingCounts(started=1, failed=1))
        self.assertEqual(self.characters_reads(client), 4)  # the failed snapshot is never reused

    def test_failed_after_snapshot_is_unknown_and_never_reused(self):
        client = FakeTrainingClient([delish(), maiden()])
        client.script("listAllCharactersOfUser", Act(), Act(), Act(apply=False, result=False))
        result = run(client)
        self.assertEqual(result.crew[0].action.verification, "unknown")
        self.assertEqual(result.crew[1].action.verification, "verified")
        self.assertEqual(self.characters_reads(client), 5)

    def test_a_snapshot_whose_inventory_read_failed_is_never_reused(self):
        # The executor's own items read, then 1001's before-snapshot whose items read fails: 1001's consumable is
        # refused, and 1002's before-snapshot reads the items again instead of reusing the inventory-less snapshot.
        client = FakeTrainingClient([low(), low("1002", "Galactic Snow Maiden")], items={"101": 5})
        client.script("listItemsOfAShip", Act(), Act(apply=False, result=False))
        result = run(client, item_designs_raw=CONSUMABLE_DESIGNS)
        self.assertEqual(client.mutations(), [("useConsumable", "101", "1002")])
        self.assertEqual(result.crew[0].action.verification, "skipped")
        self.assertEqual(result.counts, TrainingCounts(consumables=1, failed=1))


def decision_records(journal) -> list[GameplayEvent]:
    return [event for event in journal.read() if event.action_id == "training_decision"]


def evidence(cid: str, decision: str, in_room: bool, active: bool, code: str | None = None) -> dict:
    expected = {"kind": "training_decision", "v": 1, "decision": decision, "character_id": cid,
                "in_training_room": in_room, "training_active": active}
    if code is not None:
        expected["reason_code"] = code
    return expected


class DecisionRecordTests(unittest.TestCase):
    def test_one_decision_record_per_crew_meets_contract(self):
        journal = MemoryJournal()
        client = FakeTrainingClient([crew_record("1010", "Rookie Pilot", Hp=10), delish()])
        run(client, journal=journal)
        self.assertEqual([event.action_id for event in journal.read()],
                         ["training_decision", "add_training", "training_decision"])
        records = decision_records(journal)
        self.assertEqual([record.evidence for record in records], [
            evidence("1010", "skip", True, False, "unknown_role"), evidence("1001", "start", True, True)])
        self.assertEqual([record.reason for record in records],
                         ["'Rookie Pilot' has no training role", "secondary T1 Bench Press at 72% in gym"])
        for record in records:
            self.assertEqual((record.account_alias, record.outcome, record.attempted, record.dry_run),
                             ("legacy", "skipped", False, False))
            self.assertEqual((record.started_at, record.finished_at), (NOW.isoformat(), NOW.isoformat()))
            self.assertEqual(validate_decision_evidence(record.evidence), ())
            self.assertEqual(contract_violations(record), ())
        self.assertEqual(len({record.attempt_id for record in records}), 2)

    def test_training_active_only_while_the_end_is_after_the_record(self):
        running = delish(training="3", end=stamp(NOW + datetime.timedelta(hours=1)))
        due = delish(training="3", end=stamp(NOW - datetime.timedelta(seconds=30)))  # ended, but not past the grace
        for record, active in ((running, True), (due, False), (delish(), True), (ended(), False)):
            with self.subTest(record=record):
                journal = MemoryJournal()
                run(FakeTrainingClient([record]), journal=journal)
                self.assertEqual(decision_records(journal)[0].evidence["training_active"], active)

    def test_training_active_is_false_when_no_snapshot_saw_the_crew(self):
        journal = MemoryJournal()
        client = FakeTrainingClient([delish(), maiden(training="3", end=stamp(NOW + datetime.timedelta(hours=2)))])
        client.script("listAllCharactersOfUser", Act(), Act(), Act(apply=False, result=False))
        run(client, journal=journal)
        # 1001's after-snapshot failed: 1002's record falls back to the read the plan used (still running).
        self.assertEqual([r.evidence["training_active"] for r in decision_records(journal)], [False, True])

    def test_in_training_room_flag(self):
        journal = MemoryJournal()
        client = FakeTrainingClient([delish(room="203"), maiden(room="0")])
        run(client, journal=journal)
        self.assertEqual([r.evidence for r in decision_records(journal)], [
            evidence("1001", "skip", False, False, "not_in_training_room"),
            evidence("1002", "skip", False, False, "not_in_training_room")])

    def test_a_used_consumable_records_the_skip_it_stands_in_for(self):
        journal = MemoryJournal()
        run(FakeTrainingClient([low()], items={"101": 5}), journal=journal, item_designs_raw=CONSUMABLE_DESIGNS)
        record = decision_records(journal)[0]
        self.assertEqual(record.evidence, evidence("1001", "skip", True, False, "research_missing"))
        self.assertEqual(record.reason, "primary T1 research gate not met")

    def test_a_replanned_consumable_records_the_replanned_skip(self):
        journal = MemoryJournal()
        client = FakeTrainingClient([low(training="21", end=ENDED_2H, Weapon=12)], items={"101": 5})
        run(client, journal=journal, item_designs_raw=CONSUMABLE_DESIGNS)
        self.assertEqual(client.mutations(), [("finishTraining", "1001"), ("useConsumable", "101", "1001")])
        self.assertEqual(decision_records(journal)[0].evidence,
                         evidence("1001", "skip", True, False, "research_missing"))

    def test_a_failed_finish_records_training_in_progress(self):
        journal = MemoryJournal()
        client = FakeTrainingClient([ended()])
        client.script("finishTraining", Act(apply=False, result=None))
        run(client, journal=journal)
        self.assertEqual(decision_records(journal)[0].evidence,
                         evidence("1001", "skip", True, False, "training_in_progress"))

    def test_runner_events_carry_alias_and_string_params(self):
        for alias in ("legacy", "primary"):
            with self.subTest(alias=alias):
                journal = MemoryJournal()
                run(FakeTrainingClient([ended(), maiden()], researches=("1", "2")), journal=journal,
                    account_alias=alias)
                actions = [(e.account_alias, e.action_id, dict(e.params), e.outcome) for e in journal.read()
                           if e.action_id != "training_decision"]
                self.assertEqual(actions, [
                    (alias, "finish_training", FINISH, "verified"),
                    (alias, "add_training", {"training_design_id": "4", "character_id": "1001"}, "verified"),
                    (alias, "add_training", {"training_design_id": "3", "character_id": "1002"}, "verified")])

    def test_no_journal_uses_memory_journal(self):
        result = run(FakeTrainingClient([delish()]))
        self.assertIsInstance(result.journal, MemoryJournal)
        self.assertEqual([e.action_id for e in result.journal.read()], ["add_training", "training_decision"])

    def test_given_journal_receives_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = GameplayJournal(Path(tmp) / "journal.jsonl")
            result = run(FakeTrainingClient([ended(), crew_record("1010", "Rookie Pilot", Hp=10)]), journal=journal)
            self.assertIs(result.journal, journal)
            events = journal.read()
        self.assertEqual([e.action_id for e in events], ["finish_training", "training_decision", "training_decision"])
        for event in events:
            self.assertEqual(contract_violations(event), ())

    def test_memory_journal_validates_and_copies(self):
        journal = MemoryJournal()
        bad = GameplayEvent.create(account_alias="legacy", action_id="add_training", started_at=NOW, finished_at=NOW,
                                   outcome="skipped", reason="x", attempted=True)
        with self.assertRaises(JournalError):
            journal.append(bad)
        self.assertEqual(journal.read(), [])
        journal.read().append(bad)
        self.assertEqual(journal.read(), [])

    def test_a_failed_decision_record_is_logged_and_never_stops_the_run(self):
        class NoDecisions(MemoryJournal):
            def append(self, event):
                if event.action_id == "training_decision" and event.evidence["character_id"] == "1001":
                    raise OSError("disk full")
                super().append(event)

        journal = NoDecisions()
        client = FakeTrainingClient([delish(), maiden()])
        result, lines = logged(self, client, journal=journal)
        self.assertIn("ERROR [Cap] training: Delish (1001) decision record not written: OSError: disk full", lines)
        self.assertEqual(client.mutations(), [("addTraining", "3", "1001"), ("addTraining", "3", "1002")])
        self.assertEqual(result.counts, TrainingCounts(started=2))
        self.assertEqual([r.evidence["character_id"] for r in decision_records(journal)], ["1002"])


class IsolationAndClockTests(unittest.TestCase):
    def test_no_state_carried_between_crew(self):
        # An unassigned crew member after a trained one never inherits that crew member's room (defect D6).
        client = FakeTrainingClient([delish(), maiden(room="0")])
        result = run(client)
        self.assertEqual(client.mutations(), [("addTraining", "3", "1001")])
        self.assertEqual(result.crew[1].decision, Skip(1002, "not_in_training_room", "room None is not a training room"))

    def test_an_unassigned_crew_member_is_still_finished(self):
        client = FakeTrainingClient([ended(room="0")])
        _, lines = logged(self, client)
        self.assertEqual(lines[0], "INFO [Cap] Completed training for Delish in no room with Hp +2")

    def test_injected_clock_decides_finished(self):
        client = FakeTrainingClient([ended()], clock=Clock(NOW - datetime.timedelta(hours=3)))
        result = run(client)
        self.assertEqual(client.mutations(), [])
        self.assertEqual(result.crew[0].decision,
                         Skip(1001, "training_in_progress", "training ends at 2026-11-20 10:00:00+00:00"))

    def test_naive_clock_raises(self):
        client = FakeTrainingClient([delish()], clock=Clock(NOW.replace(tzinfo=None)))
        with self.assertRaises(ValueError):
            run(client)
        self.assertEqual(client.mutations(), [])


class ClassificationTests(unittest.TestCase):
    def test_verification_mapping(self):
        cases = {"verified": "verified", "not-observed": "no-change", "contradicted": "contradicted",
                 "unknown": "unknown", "skipped": "skipped", "surprise": "unknown"}
        for outcome, expected in cases.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(verification_of(outcome), expected)

    def test_counts_pure(self):
        def result(action_id, verification, harmless=False):
            return ActionResult(action_id, {}, verification, "", True, harmless)

        skip = Skip(1, "complete", "")
        start = StartTraining(1, 3, "")
        outcomes = [
            CrewOutcome(1, "a", start, action=result("add_training", "verified")),
            CrewOutcome(2, "b", start, finish=result("finish_training", "verified"),
                        action=result("add_training", "verified")),
            CrewOutcome(3, "c", skip, finish=result("finish_training", "skipped", harmless=True)),
            CrewOutcome(4, "d", skip, finish=result("finish_training", "unknown")),
            CrewOutcome(5, "e", start, action=result("use_consumable", "verified")),
            CrewOutcome(6, "f", start, action=result("add_training", "contradicted")),
            CrewOutcome(7, "g", skip, error="RuntimeError: boom"),
            CrewOutcome(8, "h", start, action=result("use_consumable", "no-change")),
        ]
        self.assertEqual(count(outcomes), TrainingCounts(started=2, finished=1, consumables=1, skipped=3, failed=4))
        self.assertEqual([o.failed for o in outcomes], [False, False, False, True, False, True, True, True])
        self.assertEqual(count([]), TrainingCounts())
        self.assertEqual(count(iter(outcomes)), count(outcomes))

    def test_summary_line_format(self):
        self.assertEqual(TrainingCounts(1, 2, 3, 4, 5).line(),
                         "training: started=1 finished=2 consumables=3 skipped=4 failed=5")

    def test_module_has_no_wall_clock_reads(self):
        source = Path(executor.__file__).read_text(encoding="utf-8")
        for call in ("datetime.now(", "utcnow(", "time.time(", "date.today("):
            self.assertNotIn(call, source)


class ResponseAndShapeTests(unittest.TestCase):
    def responses(self, client: FakeTrainingClient) -> list[tuple]:
        journal = MemoryJournal()
        run(client, journal=journal)
        return [(e.action_id, e.response_ok) for e in journal.read() if e.action_id != "training_decision"]

    def test_the_runner_sees_a_bool_response_for_each_mutation(self):
        self.assertEqual(self.responses(FakeTrainingClient([ended()], researches=("1", "2"))),
                         [("finish_training", True), ("add_training", True)])
        refused = FakeTrainingClient([ended()])
        refused.script("finishTraining", Act(apply=False, result=None))
        self.assertEqual(self.responses(refused), [("finish_training", False)])

    def test_only_exactly_true_is_a_successful_response(self):
        for name, crew, kwargs in (("addTraining", delish(), {}), ("useConsumable", low(), {"items": {"101": 5}})):
            with self.subTest(name=name):
                client = FakeTrainingClient([crew], **kwargs)
                client.script(name, Act(result="yes"))
                journal = MemoryJournal()
                run(client, journal=journal, item_designs_raw=CONSUMABLE_DESIGNS)
                self.assertEqual([e.response_ok for e in journal.read() if e.action_id != "training_decision"],
                                 [False])
                client = FakeTrainingClient([crew], **kwargs)
                journal = MemoryJournal()
                run(client, journal=journal, item_designs_raw=CONSUMABLE_DESIGNS)
                self.assertEqual([e.response_ok for e in journal.read() if e.action_id != "training_decision"],
                                 [True])

    def test_a_finish_at_capacity_reports_no_stat_change(self):
        client = FakeTrainingClient([dict(ended(Weapon=100), **{"@TrainingCapacity": "100"})])

        def finish(character_id):
            client.calls.append(("finishTraining", character_id))
            client.record(character_id)["@TrainingDesignId"] = "0"
            return dict(client.record(character_id))

        client.finishTraining = finish
        result, lines = logged(self, client)
        self.assertEqual(lines[0], "INFO [Cap] Completed training for Delish in Galaxy Gym with no stat change")
        self.assertEqual(result.crew[0].finish.reason, "training of 1001 finished at capacity; no gain expected")

    def test_an_exception_around_the_finish_leaves_the_crew_skipped(self):
        class NoFinishEvents(MemoryJournal):
            def append(self, event):
                if event.action_id == "finish_training":
                    raise JournalError("refused")
                super().append(event)

        client = FakeTrainingClient([ended(), maiden()])
        result = run(client, journal=NoFinishEvents())
        self.assertEqual(result.crew[0].decision, Skip(1001, "training_in_progress", "finish_training not run"))
        self.assertEqual((result.crew[0].finish, result.crew[0].error), (None, "JournalError: refused"))
        self.assertEqual(client.mutations(), [("finishTraining", "1001"), ("addTraining", "3", "1002")])
        self.assertEqual(result.counts, TrainingCounts(started=1, skipped=1, failed=1))

    def test_results_are_immutable(self):
        result = run(FakeTrainingClient([delish()]))
        for value, attribute in ((result, "readable"), (result.crew[0], "error"), (result.crew[0].action, "reason"),
                                 (result.counts, "started")):
            with self.subTest(type=type(value).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, attribute, None)


if __name__ == "__main__":
    unittest.main()
