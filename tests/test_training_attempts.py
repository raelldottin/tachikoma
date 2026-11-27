"""Tests for sdk.fleet.attempts: training-v2's attempt store and per-identity account lock (SPEC-14 step 4).

Pure core with fixed inputs and times, the file and in-memory ports against one contract, the flock-based lock across
open file descriptions, threads and processes, and crash/restart. No Pixel Starships traffic at all.
"""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sdk.fleet import attempts
from sdk.fleet.attempts import (
    PENDING,
    RESOLVED,
    UNRESOLVED,
    VERIFIED,
    FileAccountLock,
    FileAttemptStore,
    MemoryAccountLock,
    MemoryAttemptStore,
    StoreMissing,
    StoreUnreadable,
    TrainingAttempts,
    block_reason,
    classify_resolution,
    dump_store,
    new_intent,
    parse_store,
    resolved,
    settled,
    start_refusal,
    virgin_store,
    with_intent,
)
from sdk.policy import ActionRequest, Plan, Rejection
from sdk.policy.training import CrewPreview
from sdk.training.planner import CrewDecision
from tests import training_fixtures as tf

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
ID_A = "gid1-" + "a" * 32
ID_B = "gid1-" + "b" * 32
REPO = Path(os.path.abspath(__file__)).parents[1]
VOCABULARY = json.loads((REPO / "tests/fixtures/training/workflow_benchmark.json").read_text(encoding="utf-8"))[
    "vocabulary"]["skip_codes"]
HARNESS_CORRUPTION = b"\x00not-a-store\xff\n"  # what the workflow benchmark writes over a store


def codes_in(text):
    """The vocabulary codes ``text`` names as whole words (the workflow benchmark's rule)."""
    return {code for code in VOCABULARY if re.search(rf"(?<![A-Za-z0-9_]){re.escape(code)}(?![A-Za-z0-9_])", text)}


def params(cid="1001", course="41"):
    return {"training_design_id": course, "character_id": cid}


def intent(intent_id="0" * 32, cid="1001", course="41", minerals=500, now=T0):
    return new_intent(intent_id, params(cid, course), {"minerals": minerals} if minerals else {}, now)


def store_artifacts(root, alias):
    """The workflow benchmark's store scan: ``<alias>`` or ``<alias>.<suffix>`` with a suffix other than ``.lock``."""
    return sorted(path.name for path in Path(root).iterdir()
                  if path.name == alias or (path.name.startswith(alias + ".") and path.suffix != ".lock"))


class TempDirCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tachikoma-attempts-")
        self.addCleanup(tmp.cleanup)
        self.journal_dir = Path(tmp.name)
        self.root = self.journal_dir / attempts.STORE_DIR


class PureCoreTests(unittest.TestCase):
    # Verifies: S14-R5
    def test_store_round_trips_through_its_schema(self):
        state = with_intent(virgin_store("alpha", ID_A, T0), intent())
        raw = dump_store(state)
        self.assertEqual(parse_store(raw, "alpha"), state)
        payload = json.loads(raw)
        self.assertEqual(payload["kind"], attempts.STORE_KIND)
        self.assertEqual(payload["version"], attempts.STORE_VERSION)
        self.assertEqual(payload["game_identity"], ID_A)
        self.assertEqual(payload["attempts"][0]["state"], PENDING)
        self.assertEqual(payload["attempts"][0]["planned_cost"], {"minerals": 500})
        self.assertTrue(raw.endswith(b"\n"))

    def test_unreadable_stores_are_never_fresh(self):
        good = json.loads(dump_store(with_intent(virgin_store("alpha", ID_A, T0), intent())))
        cases = {
            "harness corruption": HARNESS_CORRUPTION,
            "empty": b"",
            "a list": b"[]",
            "wrong kind": json.dumps({**good, "kind": "other"}).encode(),
            "future version": json.dumps({**good, "version": 2}).encode(),
            "other alias": json.dumps({**good, "alias": "bravo"}).encode(),
            "bad identity": json.dumps({**good, "game_identity": "12345"}).encode(),
            "unknown state": json.dumps({**good, "attempts": [{**good["attempts"][0], "state": "maybe"}]}).encode(),
            "missing key": json.dumps({**good, "attempts": [{k: v for k, v in good["attempts"][0].items()
                                                            if k != "character_id"}]}).encode(),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                with self.assertRaises(StoreUnreadable) as caught:
                    parse_store(raw, "alpha")
                self.assertEqual(caught.exception.code, "state_unreadable")

    def test_settle_transitions(self):
        state = with_intent(virgin_store("alpha", ID_A, T0), intent())
        verified = settled(state, "0" * 32, {"outcome": "verified", "reason": "ok", "journal_attempt_id": "j1",
                                             "settled_at": T0.isoformat()})
        self.assertEqual(verified.attempts[0].state, VERIFIED)
        self.assertIsNone(verified.blocking())
        for outcome in ("unknown", "contradicted", "not-observed"):
            with self.subTest(outcome):
                after = settled(state, "0" * 32, {"outcome": outcome, "reason": "r", "journal_attempt_id": "j1",
                                                  "settled_at": T0.isoformat()})
                self.assertEqual(after.attempts[0].state, UNRESOLVED)
                self.assertEqual(after.blocking().intent_id, "0" * 32)
        with self.assertRaises(ValueError):  # an unknown intent
            settled(state, "1" * 32, {"outcome": "verified", "settled_at": T0.isoformat()})
        with self.assertRaises(ValueError):  # not pending any more
            settled(verified, "0" * 32, {"outcome": "verified", "settled_at": T0.isoformat()})
        with self.assertRaises(ValueError):  # a second pending intent
            with_intent(state, intent("1" * 32))

    # Verifies: S14-R5
    def test_block_is_account_wide_and_names_one_code(self):
        state = with_intent(virgin_store("alpha", ID_A, T0), intent(cid="1001", course="41"))
        reason = block_reason(state)
        self.assertTrue(reason.startswith("unresolved_attempt: "))
        self.assertEqual(codes_in(reason), {"unresolved_attempt"})
        self.assertIsNone(block_reason(virgin_store("alpha", ID_A, T0)))

    def test_resolution_lifts_the_block_and_records_its_evidence(self):
        state = with_intent(virgin_store("alpha", ID_A, T0), intent())
        evidence = {"verdict": "started", "observed_phase": "running", "observed_course_id": 41,
                    "observed_end": (T0 + timedelta(hours=1)).isoformat()}
        after = resolved(state, {"0" * 32: evidence}, T0)
        self.assertIsNone(after.blocking())
        self.assertEqual(after.attempts[0].state, RESOLVED)
        self.assertEqual(after.attempts[0].resolution["verdict"], "started")
        self.assertEqual(after.attempts[0].resolution["previous_state"], PENDING)
        self.assertEqual(parse_store(dump_store(after), "alpha"), after)
        with self.assertRaises(ValueError):  # only a blocking attempt is resolved
            resolved(after, {"0" * 32: evidence}, T0)

    # Verifies: S14-R5 (regression C2/D1)
    def test_a_resolution_links_an_unsettled_attempt_to_its_journal_event(self):
        from sdk.journal import GameplayEvent

        def event(attempt_id, finished, cid="1001", course="41", attempted=True, action_id="add_training"):
            return GameplayEvent.create(account_alias="alpha", action_id=action_id, started_at=finished - timedelta(
                seconds=5), finished_at=finished, outcome="unknown" if attempted else "skipped", reason="r",
                attempted=attempted, attempt_id=attempt_id, params=params(cid, course))

        state = with_intent(virgin_store("alpha", ID_A, T0), intent(now=T0))  # never settled: no journal id
        second = timedelta(seconds=1)
        events = [event("before", T0 - second), event("other-course", T0 + second, course="42"),
                  event("other-crew", T0 + second, cid="1002"), event("skipped", T0 + second, attempted=False),
                  event("other-action", T0 + second, action_id="add_research"), event("mine", T0 + second),
                  event("later", T0 + 3 * second)]
        evidence = {"verdict": "not_running", "observed_phase": "none", "observed_course_id": None,
                    "observed_end": None}
        after = resolved(state, {"0" * 32: evidence}, T0 + 2 * second, events)
        self.assertEqual(after.attempts[0].journal_attempt_id, "mine")
        self.assertEqual(attempts.reconciled_attempt_ids(after), frozenset({"mine"}))
        # resolved without the journal: the pure view still links it from the events, within the resolution window
        bare = resolved(state, {"0" * 32: evidence}, T0 + 2 * second)
        self.assertIsNone(bare.attempts[0].journal_attempt_id)
        self.assertEqual(attempts.reconciled_attempt_ids(bare), frozenset())
        self.assertEqual(attempts.reconciled_attempt_ids(bare, events), frozenset({"mine"}))
        self.assertEqual(attempts.reconciled_attempt_ids(bare, [event("later", T0 + 3 * second)]), frozenset())
        # a settled id is kept; a blocking attempt covers nothing
        self.assertEqual(attempts.reconciled_attempt_ids(state, events), frozenset())
        linked = settled(state, "0" * 32, {"outcome": "unknown", "journal_attempt_id": "j1",
                                           "settled_at": T0.isoformat()})
        self.assertEqual(resolved(linked, {"0" * 32: evidence}, T0, events).attempts[0].journal_attempt_id, "j1")

    def test_reconciled_never_raises_and_covers_nothing_without_a_readable_store(self):
        training = TrainingAttempts(MemoryAttemptStore(), MemoryAccountLock())
        self.assertEqual(training.reconciled("alpha"), frozenset())  # missing
        training.store._data["alpha"] = HARNESS_CORRUPTION
        self.assertEqual(training.reconciled("alpha"), frozenset())  # unreadable
        state = settled(with_intent(virgin_store("alpha", ID_A, T0), intent()), "0" * 32,
                        {"outcome": "unknown", "journal_attempt_id": "j1", "settled_at": T0.isoformat()})
        training.store._data["alpha"] = dump_store(state)
        self.assertEqual(training.reconciled("alpha"), frozenset())  # unresolved
        training.store._data["alpha"] = dump_store(resolved(state, {"0" * 32: {"verdict": "not_running"}}, T0))
        self.assertEqual(training.reconciled("alpha"), frozenset({"j1"}))

    # Verifies: S14-R5 (regression C6/D6: evidence must be newer than the attempt)
    def test_classify_resolution_needs_a_read_newer_than_the_attempt(self):
        attempt = intent(cid="101", course="5010", now=T0)
        second = timedelta(seconds=1)
        for observed in (T0 - second, T0):
            with self.subTest(observed=observed):
                evidence, why = classify_resolution(attempt, tf.snap(crew_records=[tf.crew("101")],
                                                                      observed_at=observed))
                self.assertIsNone(evidence)
                self.assertIn("not newer than", why)
        evidence, why = classify_resolution(attempt, tf.snap(crew_records=[tf.crew("101")], observed_at=T0 + second))
        self.assertEqual((evidence["verdict"], why), ("not_running", None))
        settled_later = settled(with_intent(virgin_store("alpha", ID_A, T0), attempt), attempt.intent_id,
                                {"outcome": "unknown", "settled_at": (T0 + 5 * second).isoformat()}).attempts[0]
        evidence, why = classify_resolution(settled_later, tf.snap(crew_records=[tf.crew("101")],
                                                                    observed_at=T0 + 2 * second))
        self.assertIsNone(evidence)
        self.assertIn("not newer than", why)
        evidence, _ = classify_resolution(settled_later, tf.snap(crew_records=[tf.crew("101")],
                                                                  observed_at=T0 + 6 * second))
        self.assertEqual(evidence["verdict"], "not_running")

    # Verifies: S14-R5 (regression C6: a running session without a course is no evidence)
    def test_a_running_session_without_a_course_id_is_no_evidence(self):
        attempt = intent(cid="101", course="5010", now=T0 - timedelta(minutes=1))
        running = tf.crew("101", training="0", end=tf.server_time(T0 + timedelta(hours=1)))
        snapshot = tf.snap(crew_records=[running])
        self.assertEqual(snapshot.training_session("101").phase, "running")
        self.assertIsNone(snapshot.training_session("101").course_id)
        evidence, why = classify_resolution(attempt, snapshot)
        self.assertIsNone(evidence)
        self.assertIn("unknown", why)

    def test_classify_resolution_table(self):
        attempt = intent(cid="101", course="5010", now=T0 - timedelta(minutes=1))
        hour = timedelta(hours=1)
        rows = [
            ("running requested", [tf.ended("101", ago=-hour, design_id="5010")], "started", None),
            ("running other", [tf.ended("101", ago=-hour, design_id="5011")], "other_course", None),
            ("idle", [tf.crew("101")], "not_running", None),
            ("member absent", [tf.crew("102")], None, "not on the roster"),
            ("roster unread", None, None, "not complete"),
        ]
        for name, crew, verdict, refusal in rows:
            with self.subTest(name):
                evidence, why = classify_resolution(attempt, tf.snap(crew_records=crew))
                if verdict is None:
                    self.assertIsNone(evidence)
                    self.assertIn(refusal, why)
                else:
                    self.assertIsNone(why)
                    self.assertEqual(evidence["verdict"], verdict)

    def test_messages_hold_exactly_one_code(self):
        state = with_intent(virgin_store("alpha", ID_A, T0), intent())
        texts = [
            block_reason(state),
            attempts.intent_failure_reason(OSError("disk full")),
            attempts.settle_failure_reason(intent(), OSError("disk full")),
            attempts.LOCKED_REASON,
            attempts.state_reason("journals/training-attempts/alpha.json", StoreMissing("no store")),
            attempts.state_reason("journals/training-attempts/alpha.json", StoreMissing("established", established=True)),
            attempts.state_reason("journals/training-attempts/alpha.json", StoreUnreadable("UnicodeDecodeError")),
            attempts.state_reason("journals/training-attempts/alpha.json", StoreUnreadable("x", other_identity=True)),
        ]
        for text in texts:
            with self.subTest(text):
                self.assertEqual(len(codes_in(text)), 1, text)


def preview(requests=(), decisions=(), rejected=()):
    base = Plan("training-v2", "alpha", T0.isoformat(), (), ())
    return CrewPreview(base, None, None, tuple(decisions), tuple(requests), tuple(rejected))


def start_request(cid="1001", course="41", minerals=500, gas=0):
    return ActionRequest("add_training", params(cid, course), "repair specialist: manual",
                         {"minerals": minerals, "gas": gas})


class StartRefusalTests(unittest.TestCase):
    # Verifies: S14-R4
    def test_the_same_start_at_the_same_price_is_not_refused(self):
        self.assertIsNone(start_refusal(preview([start_request()]), params(), {"minerals": 500, "gas": 0}))
        self.assertIsNone(start_refusal(preview([start_request(minerals=0)]), params(), {"minerals": 0, "gas": 0}))

    def test_a_changed_price_is_refused(self):
        reason = start_refusal(preview([start_request(minerals=600)]), params(), {"minerals": 500, "gas": 0})
        self.assertTrue(reason.startswith("price_unavailable: "), reason)
        self.assertEqual(codes_in(reason), {"price_unavailable"})

    def test_a_crew_skip_on_the_fresh_read_names_its_code(self):
        for code in ("insufficient_funds", "room_not_ready", "premium_currency", "training_in_progress"):
            with self.subTest(code):
                decision = CrewDecision("repair", 1001, None, code, "detail")
                reason = start_refusal(preview(decisions=[decision]), params(), {"minerals": 500, "gas": 0})
                self.assertTrue(reason.startswith(f"{code}: "), reason)
                self.assertEqual(codes_in(reason), {code})

    def test_no_longer_planned_is_refused(self):
        reason = start_refusal(preview(rejected=[Rejection("training", "nothing to plan")]), params(),
                               {"minerals": 500, "gas": 0})
        self.assertIsNotNone(reason)
        reason = start_refusal(preview([start_request(cid="1002")]), params(), {"minerals": 500, "gas": 0})
        self.assertIsNotNone(reason)


class StoreContract:
    """The AttemptStore contract, run against the file and the in-memory implementation."""

    def make_store(self):
        raise NotImplementedError

    def test_load_of_an_unprovisioned_store_is_state_missing(self):
        store = self.make_store()
        with self.assertRaises(StoreMissing) as caught:
            store.load("alpha")
        self.assertEqual(caught.exception.code, "state_missing")

    def test_provision_creates_a_virgin_store_once(self):
        store = self.make_store()
        self.assertTrue(store.provision("alpha", ID_A, T0))
        state = store.load("alpha")
        self.assertEqual((state.alias, state.game_identity, state.attempts), ("alpha", ID_A, ()))
        self.assertTrue(store.established(ID_A))
        self.assertFalse(store.provision("alpha", ID_A, T0 + timedelta(hours=1)))  # a no-op, nothing rewritten
        self.assertEqual(store.load("alpha").provisioned_at, T0.isoformat())

    def test_provision_refuses_another_identity(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        with self.assertRaises(StoreUnreadable):
            store.provision("alpha", ID_B, T0)
        self.assertEqual(store.load("alpha").game_identity, ID_A)

    def test_intent_settle_and_resolve_persist(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        store.write_intent("alpha", intent())
        self.assertEqual(store.load("alpha").blocking().state, PENDING)
        store.settle("alpha", "0" * 32, {"outcome": "unknown", "reason": "r", "journal_attempt_id": "j",
                                         "settled_at": T0.isoformat()})
        self.assertEqual(store.load("alpha").blocking().state, UNRESOLVED)
        store.resolve("alpha", {"0" * 32: {"verdict": "not_running", "observed_phase": "none",
                                           "observed_course_id": None, "observed_end": None}}, T0)
        self.assertIsNone(store.load("alpha").blocking())

    def test_a_second_pending_intent_is_refused(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        store.write_intent("alpha", intent())
        with self.assertRaises(ValueError):
            store.write_intent("alpha", intent("1" * 32, cid="1002"))


class FileStoreTests(StoreContract, TempDirCase):
    def make_store(self):
        return FileAttemptStore(self.journal_dir)

    def test_provision_writes_one_alias_named_store_with_private_modes(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        self.assertEqual(store_artifacts(self.root, "alpha"), ["alpha.json"])
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "alpha.json").st_mode), 0o600)
        self.assertEqual(store.location("alpha"), str(self.root / "alpha.json"))

    # Verifies: S14-R6 (regression C4/D3: the marker lives outside the directory it protects)
    def test_the_establishment_marker_lives_beside_the_store_directory(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        markers = self.journal_dir / attempts.MARKER_DIR
        marker = markers / f"{ID_A}.json"
        self.assertEqual(sorted(path.name for path in markers.iterdir()), [marker.name])
        self.assertEqual(stat.S_IMODE(os.stat(markers).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(marker).st_mode), 0o600)
        self.assertEqual(json.loads(marker.read_text())["game_identity"], ID_A)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["alpha.json"])
        import shutil

        shutil.rmtree(self.root)  # the whole store directory is lost
        with self.assertRaises(StoreMissing) as caught:
            store.load("alpha", identity=ID_A)
        self.assertTrue(caught.exception.established)
        with self.assertRaises(StoreMissing) as caught:
            store.provision("alpha", ID_A, T0)
        self.assertTrue(caught.exception.established)
        self.assertFalse((self.root / "alpha.json").exists())

    def test_a_crash_between_the_store_and_its_marker_leaves_a_usable_store(self):
        store = self.make_store()
        with patch.object(FileAttemptStore, "_mark", side_effect=OSError("crash before the marker")):
            with self.assertRaises(OSError):
                store.provision("alpha", ID_A, T0)
        self.assertEqual(store.load("alpha").game_identity, ID_A)  # the store first ...
        self.assertFalse(store.established(ID_A))  # ... then its marker
        self.assertFalse(store.provision("alpha", ID_A, T0))  # provisioning again only writes the marker
        self.assertTrue(store.established(ID_A))

    def test_provision_refuses_to_recreate_established_state(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        (self.root / "alpha.json").unlink()
        with self.assertRaises(StoreMissing) as caught:
            store.provision("alpha", ID_A, T0)
        self.assertTrue(caught.exception.established)
        self.assertFalse((self.root / "alpha.json").exists())

    def test_unreadable_bytes_are_never_overwritten(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        (self.root / "alpha.json").write_bytes(HARNESS_CORRUPTION)
        for operation in (lambda: store.load("alpha"), lambda: store.provision("alpha", ID_A, T0),
                          lambda: store.write_intent("alpha", intent()),
                          lambda: store.settle("alpha", "0" * 32, {"outcome": "verified"})):
            with self.subTest(operation):
                with self.assertRaises(StoreUnreadable):
                    operation()
                self.assertEqual((self.root / "alpha.json").read_bytes(), HARNESS_CORRUPTION)
        (self.root / "alpha.json").unlink()
        (self.root / "alpha.json").mkdir()  # a directory in place of the file
        with self.assertRaises(StoreUnreadable):
            store.load("alpha")

    def test_atomic_write_order_and_failure(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
            real_fsync(fd)

        def replace(src, dst):
            events.append("replace")
            real_replace(src, dst)

        with patch("os.fsync", fsync), patch("os.replace", replace):
            store.write_intent("alpha", intent())
        self.assertEqual(events, ["fsync-file", "replace", "fsync-dir"])
        before = (self.root / "alpha.json").read_bytes()
        with patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                store.settle("alpha", "0" * 32, {"outcome": "verified", "settled_at": T0.isoformat()})
        self.assertEqual((self.root / "alpha.json").read_bytes(), before)
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.endswith(".tmp")], [])

    # Regression C11: a write can land even though its directory fsync failed; the messages must allow for it
    def test_failure_messages_allow_for_a_write_that_landed(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        with patch.object(FileAttemptStore, "_sync_root", side_effect=OSError("directory fsync failed")):
            with self.assertRaises(OSError) as caught:
                store.write_intent("alpha", intent())
        self.assertEqual(store.load("alpha").blocking().state, PENDING)  # the intent did land
        text = attempts.intent_failure_reason(caught.exception)
        self.assertEqual(codes_in(text), {"intent_not_persisted"})
        self.assertIn("nothing was sent", text)
        self.assertIn("may", text)
        self.assertIn("--fleet-resolve-training", text)
        with patch.object(FileAttemptStore, "_sync_root", side_effect=OSError("directory fsync failed")):
            with self.assertRaises(OSError) as caught:
                store.settle("alpha", "0" * 32, {"outcome": "verified", "settled_at": T0.isoformat()})
        self.assertEqual(store.load("alpha").attempts[0].state, VERIFIED)  # the settlement did land
        text = attempts.settle_failure_reason(intent(), caught.exception)
        self.assertEqual(codes_in(text), {"unresolved_attempt"})
        self.assertIn("may", text)
        self.assertNotIn("stays pending", text)

    # Verifies: S14-R5
    def test_restart_sees_a_pending_intent(self):
        FileAttemptStore(self.journal_dir).provision("alpha", ID_A, T0)
        FileAttemptStore(self.journal_dir).write_intent("alpha", intent())  # then the process "crashes"
        self.assertEqual(FileAttemptStore(self.journal_dir).load("alpha").blocking().intent_id, "0" * 32)

    def test_lock_and_marker_files_are_never_store_artifacts(self):
        store = self.make_store()
        store.provision("alpha", ID_A, T0)
        handle = FileAccountLock(self.root).try_acquire(ID_A)
        handle.release()
        self.assertEqual(store_artifacts(self.root, "alpha"), ["alpha.json"])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["alpha.json", f"{ID_A}.lock"])

    def test_aliases_that_could_leave_the_directory_are_refused(self):
        store = self.make_store()
        for alias in ("../x", "Alpha", "", "a/b"):
            with self.subTest(alias):
                with self.assertRaises(ValueError):
                    store.provision(alias, ID_A, T0)


class MemoryStoreTests(StoreContract, unittest.TestCase):
    def make_store(self):
        return MemoryAttemptStore()


class LockContract:
    def make_lock(self):
        raise NotImplementedError

    def test_lock_is_exclusive_per_identity_and_reacquirable(self):
        lock = self.make_lock()
        first = lock.try_acquire(ID_A)
        self.assertIsNotNone(first)
        self.assertIsNone(lock.try_acquire(ID_A))  # same thread, a new open file description
        other = lock.try_acquire(ID_B)  # another identity is independent
        self.assertIsNotNone(other)
        seen = []
        thread = threading.Thread(target=lambda: seen.append(lock.try_acquire(ID_A)))
        thread.start()
        thread.join()
        self.assertEqual(seen, [None])  # another thread never blocks and never gets it
        first.release()
        first.release()  # idempotent
        again = lock.try_acquire(ID_A)
        self.assertIsNotNone(again)
        again.release()
        other.release()

    def test_lock_rejects_a_malformed_identity(self):
        lock = self.make_lock()
        for identity in (None, "../x", "gid1-XYZ", "gid1-" + "a" * 31):
            with self.subTest(identity):
                with self.assertRaises(ValueError):
                    lock.try_acquire(identity)


class FileLockTests(LockContract, TempDirCase):
    def make_lock(self):
        self.root.mkdir()
        return FileAccountLock(self.root)

    def test_nothing_is_created_outside_the_root(self):
        lock = self.make_lock()
        with self.assertRaises(ValueError):
            lock.try_acquire("../escape")
        self.assertEqual(sorted(path.name for path in self.journal_dir.iterdir()), [attempts.STORE_DIR])

    # Verifies: S14-R6
    def test_lock_is_exclusive_across_processes_and_released_by_a_killed_holder(self):
        lock = self.make_lock()
        ready = self.journal_dir / "ready"
        child = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(REPO)!r})
            from pathlib import Path
            from sdk.fleet.attempts import FileAccountLock
            handle = FileAccountLock(Path({str(self.root)!r})).try_acquire({ID_A!r})
            assert handle is not None
            Path({str(ready)!r}).write_text("ok")
            time.sleep(60)
        """)])
        try:
            deadline = time.monotonic() + 30
            while not ready.exists():
                self.assertIsNone(child.poll(), "the child exited before taking the lock")
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            started = time.monotonic()
            self.assertIsNone(lock.try_acquire(ID_A))
            self.assertLess(time.monotonic() - started, 5)  # non-blocking
        finally:
            child.kill()  # a crash: the OS releases the flock
            child.wait()
        handle = lock.try_acquire(ID_A)
        self.assertIsNotNone(handle)
        handle.release()


class MemoryLockTests(LockContract, unittest.TestCase):
    def make_lock(self):
        return MemoryAccountLock()


class Crash(BaseException):
    """Stands in for a process that dies mid-dispatch."""


class DispatchTests(TempDirCase):
    def attempts_service(self):
        return TrainingAttempts.for_journal_dir(self.journal_dir)

    # Verifies: S14-R6
    def test_dispatch_loads_the_store_under_the_lock_and_releases_it(self):
        service = self.attempts_service()
        service.store.provision("alpha", ID_A, T0)
        with service.dispatch("alpha", ID_A) as dispatch:
            self.assertFalse(dispatch.locked)
            self.assertIsNone(dispatch.refusal)
            self.assertEqual(dispatch.state.game_identity, ID_A)
            self.assertIsNone(service.lock.try_acquire(ID_A))  # held for the whole dispatch
        handle = service.lock.try_acquire(ID_A)
        self.assertIsNotNone(handle)
        handle.release()

    def test_dispatch_releases_on_a_crash_without_settling(self):
        service = self.attempts_service()
        service.store.provision("alpha", ID_A, T0)
        with self.assertRaises(Crash):
            with service.dispatch("alpha", ID_A):
                service.store.write_intent("alpha", intent())
                raise Crash()
        self.assertEqual(service.store.load("alpha").blocking().state, PENDING)
        handle = service.lock.try_acquire(ID_A)
        self.assertIsNotNone(handle)
        handle.release()

    def test_a_locked_dispatch_reads_no_store(self):
        store = MemoryAttemptStore()
        store.provision("alpha", ID_A, T0)
        lock = MemoryAccountLock()
        held = lock.try_acquire(ID_A)
        with patch.object(store, "load", side_effect=AssertionError("read while locked")):
            with TrainingAttempts(store, lock).dispatch("alpha", ID_A) as dispatch:
                self.assertTrue(dispatch.locked)
                self.assertEqual(codes_in(dispatch.refusal), {"locked"})
        held.release()

    def test_dispatch_refusals(self):
        service = self.attempts_service()
        with service.dispatch("alpha", ID_A) as dispatch:  # nothing provisioned: no lock file is created either
            self.assertEqual(codes_in(dispatch.refusal), {"state_missing"})
        self.assertFalse(self.root.exists())
        service.store.provision("alpha", ID_A, T0)
        with service.dispatch("alpha", ID_B) as dispatch:
            self.assertEqual(codes_in(dispatch.refusal), {"state_unreadable"})
        with service.dispatch("alpha", None) as dispatch:
            self.assertEqual(codes_in(dispatch.refusal), {"state_unreadable"})
        (self.root / "alpha.json").write_bytes(HARNESS_CORRUPTION)
        with service.dispatch("alpha", ID_A) as dispatch:
            self.assertEqual(codes_in(dispatch.refusal), {"state_unreadable"})
        (self.root / "alpha.json").unlink()
        with service.dispatch("alpha", ID_A) as dispatch:
            self.assertEqual(codes_in(dispatch.refusal), {"state_missing"})
        with patch("os.open", side_effect=PermissionError("no")):
            with service.dispatch("alpha", ID_A) as dispatch:
                self.assertEqual(codes_in(dispatch.refusal), {"state_unreadable"})

    # Verifies: S14-R6 (regression C3)
    def test_hold_takes_only_the_lock(self):
        service = self.attempts_service()
        with patch.object(service.store, "load", side_effect=AssertionError("the store is not read")):
            with service.hold("alpha", ID_A) as dispatch:  # unprovisioned: the lock alone, its directory created
                self.assertEqual((dispatch.locked, dispatch.refusal, dispatch.state), (False, None, None))
                self.assertIsNone(service.lock.try_acquire(ID_A))
                with service.hold("alpha", ID_A) as overlapping:
                    self.assertTrue(overlapping.locked)
                    self.assertEqual(codes_in(overlapping.refusal), {"locked"})
        handle = service.lock.try_acquire(ID_A)
        self.assertIsNotNone(handle)
        handle.release()
        self.assertEqual(store_artifacts(self.root, "alpha"), [])  # no store is created by holding the lock
        with service.hold("alpha", None) as dispatch:
            self.assertEqual(codes_in(dispatch.refusal), {"state_unreadable"})

    # Verifies: S14-R5 (regression C6/D6)
    def test_resolve_reads_its_evidence_under_the_lock(self):
        service = self.attempts_service()
        service.provision("alpha", ID_A, T0)
        observe = Mock(side_effect=AssertionError("nothing blocks: no read"))
        self.assertEqual(service.resolve("alpha", ID_A, observe, lambda: T0), [])
        service.store.write_intent("alpha", intent(cid="101", course="5010", now=T0))
        held = []

        def read():
            held.append(service.lock.try_acquire(ID_A))
            return tf.snap(crew_records=[tf.crew("101")], observed_at=T0 + timedelta(seconds=1), game_identity=ID_A)

        [(attempt, evidence)] = service.resolve("alpha", ID_A, read, lambda: T0 + timedelta(seconds=2))
        self.assertEqual(held, [None])  # the read happened under the lock
        self.assertEqual((attempt.character_id, evidence["verdict"]), ("101", "not_running"))
        self.assertEqual(service.store.load("alpha").attempts[0].state, RESOLVED)
        # another identity's read resolves nothing
        service.store.write_intent("alpha", intent("1" * 32, cid="101", course="5010", now=T0 + timedelta(seconds=3)))
        foreign = Mock(return_value=tf.snap(crew_records=[tf.crew("101")], observed_at=T0 + timedelta(seconds=9),
                                            game_identity=ID_B))
        with self.assertRaises(attempts.ResolutionRefused):
            service.resolve("alpha", ID_A, foreign, lambda: T0 + timedelta(seconds=9))
        self.assertEqual(service.store.load("alpha").blocking().intent_id, "1" * 32)

    def test_dispatch_block_reason(self):
        service = self.attempts_service()
        service.store.provision("alpha", ID_A, T0)
        service.store.write_intent("alpha", intent())
        with service.dispatch("alpha", ID_A) as dispatch:
            self.assertEqual(codes_in(dispatch.block_reason()), {"unresolved_attempt"})

    def test_preflight(self):
        service = self.attempts_service()
        self.assertEqual(codes_in(service.preflight("alpha", ID_A)), {"state_missing"})
        service.store.provision("alpha", ID_A, T0)
        self.assertIsNone(service.preflight("alpha", ID_A))
        self.assertEqual(codes_in(service.preflight("alpha", ID_B)), {"state_unreadable"})
        (self.root / "alpha.json").write_bytes(HARNESS_CORRUPTION)
        self.assertEqual(codes_in(service.preflight("alpha", ID_A)), {"state_unreadable"})
        (self.root / "alpha.json").unlink()
        why = service.preflight("alpha", ID_A)
        self.assertEqual(codes_in(why), {"state_missing"})
        self.assertIn("never recreated", why)

    # Verifies: S14-R6 (regression C4/D3)
    def test_a_removed_store_directory_is_established_state_missing(self):
        import shutil

        service = self.attempts_service()
        service.provision("alpha", ID_A, T0)
        shutil.rmtree(self.root)
        why = service.preflight("alpha", ID_A)
        self.assertEqual(codes_in(why), {"state_missing"})
        self.assertIn("never recreated", why)
        with service.dispatch("alpha", ID_A) as dispatch:
            self.assertEqual(codes_in(dispatch.refusal), {"state_missing"})
            self.assertIn("never recreated", dispatch.refusal)
        with self.assertRaises(StoreMissing) as caught:
            service.provision("alpha", ID_A, T0)
        self.assertTrue(caught.exception.established)
        self.assertFalse((self.root / "alpha.json").exists())

    # Verifies: S14-R6 (regression C4: the account journal is a second witness)
    def test_a_journal_with_a_training_v2_start_is_never_provisioned_fresh(self):
        from sdk.fleet.factory import DRY_RUN_POLICY
        from sdk.journal import GameplayEvent

        self.assertEqual(attempts.DRY_RUN_POLICY_KEY, DRY_RUN_POLICY)

        def event(action_id="add_training", attempted=True, dry_run=False, policy=None):
            return GameplayEvent.create(account_alias="alpha", action_id=action_id, started_at=T0, finished_at=T0,
                                        outcome="skipped" if not attempted else "verified", reason="r",
                                        attempted=attempted, dry_run=dry_run, params=params(),
                                        evidence={DRY_RUN_POLICY: policy} if policy else {})

        v2_plan, v1_plan = event(attempted=False, dry_run=True, policy="training-v2"), event(
            attempted=False, dry_run=True, policy="training-v1")
        witness = attempts.journal_witnesses_store
        self.assertFalse(witness([], "alpha", "training-v2"))
        self.assertFalse(witness([v1_plan, event()], "alpha", "training-v2"))  # a training-v1 start before the switch
        self.assertFalse(witness([v2_plan, event(attempted=False)], "alpha", "training-v2"))  # skipped only
        self.assertFalse(witness([v2_plan, event("collect_all_resources")], "alpha", "training-v2"))
        self.assertTrue(witness([v1_plan, event(), v2_plan, event()], "alpha", "training-v2"))
        self.assertFalse(witness([v2_plan, event()], "bravo", "training-v2"))  # another account's events
        service = self.attempts_service()
        with self.assertRaises(StoreMissing) as caught:
            service.provision("alpha", ID_A, T0, events=lambda: [v2_plan, event()], policy_id="training-v2")
        self.assertTrue(caught.exception.established)
        self.assertFalse((self.root / "alpha.json").exists())
        self.assertEqual(codes_in(attempts.state_reason("x", caught.exception)), {"state_missing"})
        self.assertTrue(service.provision("alpha", ID_A, T0, events=lambda: [v1_plan, event()],
                                          policy_id="training-v2"))

    def test_provision_under_the_lock(self):
        service = self.attempts_service()
        self.assertTrue(service.provision("alpha", ID_A, T0))
        held = service.lock.try_acquire(ID_A)
        with self.assertRaises(attempts.AccountLocked):
            service.provision("alpha", ID_A, T0)
        held.release()
        self.assertFalse(service.provision("alpha", ID_A, T0))


class GuardTests(TempDirCase):
    def test_before_send_checks_in_order(self):
        store = FileAttemptStore(self.journal_dir)
        store.provision("alpha", ID_A, T0)
        recheck = Mock(return_value="price_unavailable: changed")
        guard = attempts.TrainingGuard(store, "alpha", ID_A, recheck, lambda: T0)
        before = tf.snap()
        self.assertEqual(guard.before_send(before, params(), {"minerals": 500}), ("price_unavailable: changed", False))
        recheck.assert_called_once_with(before, params(), {"minerals": 500})
        recheck.return_value = None
        self.assertIsNone(guard.before_send(before, params(), {"minerals": 500}))
        guard.mark_sent(None)
        self.assertEqual(guard.before_send(before, params(), {}), (attempts.ONE_START_REASON, False))
        store.write_intent("alpha", intent())  # another dispatch's pending intent: the store is read again
        fresh = attempts.TrainingGuard(store, "alpha", ID_A, recheck, lambda: T0)
        reason, stop = fresh.before_send(before, params(), {})
        self.assertEqual((codes_in(reason), stop), ({"unresolved_attempt"}, True))
        (self.root / "alpha.json").unlink()
        reason, stop = fresh.before_send(before, params(), {})
        self.assertEqual((codes_in(reason), stop), ({"state_missing"}, True))
        self.assertEqual(recheck.call_count, 2)

    def test_factory_imports_the_attempt_module_at_its_top_level(self):
        # FakePss swaps the clocks of the sdk modules loaded before it is installed (tests/fakes/pss_server.py).
        probe = subprocess.run([sys.executable, "-c", "import sys; import sdk.fleet.factory; "
                                "print('sdk.fleet.attempts' in sys.modules)"],
                               cwd=REPO, capture_output=True, text=True, check=True)
        self.assertEqual(probe.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
