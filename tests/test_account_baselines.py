"""Tests for sdk.state.baseline (SPEC-04). Synthetic data only; sockets blocked."""

import json
import os
import socket
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sdk.state import Completeness, build_snapshot, diff_snapshots
from sdk.state.baseline import (
    BASELINE_FORMAT,
    BaselineError,
    BaselineExistsError,
    baseline_filename,
    load_baseline,
    load_baseline_file,
    scan_baseline_directory,
    scan_text,
    write_baseline,
)
from sdk.state.snapshot import SECTIONS, game_identity_fingerprint
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_DEVICE_KEY_IOS, SYNTHETIC_EMAIL

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
T0 = datetime(2026, 11, 10, 12, 0, 5, 123456, tzinfo=timezone.utc)
SERVER_VERSION = "0.999.60"
SALT = "synthetic-identity-salt-0123456789"

PROFILE = {"@Name": "SyntheticCaptain", "@Trophy": "1200", "@Email": SYNTHETIC_EMAIL, "@Id": "4242"}
SHIP = {"@ShipLevel": "6", "@ShipDesignId": "106", "@UserId": "4242"}
ROOMS = [
    {"@RoomId": "r1", "@RoomDesignId": "11", "@RoomStatus": "Normal"},
    {"@RoomId": "r2", "@RoomDesignId": "21", "@RoomStatus": "Upgrading"},
]
CREW = [
    {"@CharacterId": "c1", "@CharacterDesignId": "300", "@CharacterName": "Crew One", "@Level": "10",
     "@Fatigue": "0", "@RoomId": "r1", "@TrainingEndDate": "2026-11-10T14:00:00", "@TrainingDesignId": "7"},
    {"@CharacterId": "c2", "@CharacterDesignId": "301", "@CharacterName": "Crew Two", "@Level": "4",
     "@Fatigue": "12", "@RoomId": "r2"},
]
RESEARCH = {"@ResearchDesignId": "500", "@ResearchState": "Completed"}
INVENTORY = [{"@ItemDesignId": "900", "@Quantity": "3"}, {"@ItemDesignId": "901", "@Quantity": "x"}]
RESOURCES = {"minerals": "1000", "gas": "2000", "starbux": "15"}


def full_snapshot(alias="alpha", observed_at=T0, **overrides):
    kwargs = dict(
        game_version="1.0.0-client",
        profile=PROFILE,
        ship=SHIP,
        resources=RESOURCES,
        crew=CREW,
        rooms=ROOMS,
        research=RESEARCH,
        inventory=INVENTORY,
        game_identity=game_identity_fingerprint("4242", SALT),
    )
    kwargs.update(overrides)
    return build_snapshot(alias, observed_at, "synthetic-fixture", **kwargs)


def forbidden(name):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be used by baseline code")

    return fail


class BaselineTestCase(unittest.TestCase):
    def setUp(self):
        for target in ("socket", "create_connection"):
            patcher = patch.object(socket, target, side_effect=forbidden(f"socket.{target}"))
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "baselines"

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))


# Verifies: S04-R2
class BaselineContentTests(BaselineTestCase):
    def test_file_holds_alias_utc_time_version_and_every_section(self):
        path = write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        self.assertEqual(path.name, "alpha-20261110T120005Z.json")
        record = self.read_json(path)
        self.assertEqual(record["format"], BASELINE_FORMAT)
        self.assertEqual(record["account_alias"], "alpha")
        self.assertEqual(record["observed_at"], "2026-11-10T12:00:05.123456+00:00")
        self.assertEqual(record["game_version"], SERVER_VERSION)
        self.assertEqual(record["client_version"], "1.0.0-client")
        self.assertEqual(record["hull_level"], 6)
        self.assertEqual(record["trophies"], 1200)
        self.assertEqual(record["resources"], {"minerals": 1000, "gas": 2000, "starbux": 15})
        self.assertEqual([room["design_id"] for room in record["rooms"]], ["11", "21"])
        self.assertEqual(record["research"], [{"design_id": "500", "state": "Completed"}])
        self.assertEqual(record["crew"][0]["level"], 10)
        self.assertEqual(record["crew"][0]["training_design_id"], "7")
        self.assertFalse(record["crew"][1]["training_observed"])
        self.assertEqual(record["inventory"][1], {"item_design_id": "901", "quantity": None})
        self.assertEqual(set(record["completeness"]), set(SECTIONS))
        self.assertTrue(all(value == "complete" for value in record["completeness"].values()))

    def test_unobserved_sections_are_null_and_recorded_missing(self):
        snapshot = build_snapshot("alpha", T0, "synthetic-fixture", rooms=ROOMS)
        record = self.read_json(write_baseline(snapshot, self.dir, None))
        self.assertIsNone(record["game_version"])
        self.assertIsNone(record["hull_level"])
        self.assertIsNone(record["trophies"])
        self.assertEqual(record["resources"], {"minerals": None, "gas": None, "starbux": None})
        for name in ("crew", "research", "inventory"):
            self.assertIsNone(record[name], name)
            self.assertEqual(record["completeness"][name], "missing", name)
        self.assertEqual(record["completeness"]["rooms"], "complete")
        self.assertEqual(len(record["rooms"]), 2)

    def test_observed_empty_section_is_an_empty_list_not_null(self):
        snapshot = full_snapshot(research=[])
        record = self.read_json(write_baseline(snapshot, self.dir, SERVER_VERSION))
        self.assertEqual(record["research"], [])
        self.assertEqual(record["completeness"]["research"], "complete")

    def test_partial_sections_keep_their_completeness_and_notes(self):
        snapshot = full_snapshot(rooms=ROOMS + [{"@RoomDesignId": "99"}])
        record = self.read_json(write_baseline(snapshot, self.dir, SERVER_VERSION))
        self.assertEqual(record["completeness"]["rooms"], "partial")
        self.assertEqual(len(record["notes"]), 1)

    def test_no_credential_user_id_email_or_player_name_in_file(self):
        path = write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        text = path.read_text(encoding="utf-8")
        for value in (SYNTHETIC_EMAIL, "SyntheticCaptain", "4242", SYNTHETIC_ACCESS_TOKEN, SALT):
            self.assertNotIn(value, text)
        self.assertEqual(scan_text(text), [])
        self.assertTrue(self.read_json(path)["game_identity"].startswith("gid1-"))

    def test_non_utc_observation_time_is_stored_in_utc(self):
        local = T0.astimezone(timezone(timedelta(hours=-5)))
        path = write_baseline(full_snapshot(observed_at=local), self.dir, SERVER_VERSION)
        self.assertEqual(path.name, "alpha-20261110T120005Z.json")
        self.assertTrue(self.read_json(path)["observed_at"].endswith("+00:00"))

    def test_secret_shaped_values_are_refused_and_nothing_is_written(self):
        base = full_snapshot()
        cases = {
            "e-mail in a note": (replace(base, notes=(f"owner {SYNTHETIC_EMAIL}",)), SERVER_VERSION),
            "token in the source": (replace(base, source=f"run?accessToken={SYNTHETIC_ACCESS_TOKEN}"), None),
            "device key in a note": (replace(base, notes=(f'"DeviceKey":"{SYNTHETIC_DEVICE_KEY_IOS}"',)), None),
            "user id in a note": (replace(base, notes=('UserId="4242"',)), None),
            "e-mail as game version": (base, SYNTHETIC_EMAIL),
        }
        for label, (snapshot, version) in cases.items():
            with self.subTest(label):
                with self.assertRaises(BaselineError) as caught:
                    write_baseline(snapshot, self.dir, version)
                for secret in (SYNTHETIC_EMAIL, SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_DEVICE_KEY_IOS, "4242"):
                    self.assertNotIn(secret, str(caught.exception))
        self.assertFalse(self.dir.exists() and any(self.dir.iterdir()))

    def test_raw_user_id_as_game_identity_is_refused(self):
        with self.assertRaises(BaselineError):
            write_baseline(replace(full_snapshot(), game_identity="4242"), self.dir, SERVER_VERSION)
        self.assertFalse(self.dir.exists())

    def test_naive_time_or_bad_alias_is_refused(self):
        with self.assertRaises(BaselineError):
            write_baseline(replace(full_snapshot(), observed_at=T0.replace(tzinfo=None)), self.dir, None)
        with self.assertRaises(BaselineError):
            write_baseline(replace(full_snapshot(), account_alias=SYNTHETIC_EMAIL), self.dir, None)

    def test_format_is_stable(self):
        first = write_baseline(full_snapshot(), self.dir / "a", SERVER_VERSION).read_bytes()
        second = write_baseline(full_snapshot(), self.dir / "b", SERVER_VERSION).read_bytes()
        self.assertEqual(first, second)
        record = json.loads(first)
        self.assertEqual(list(record), sorted(record))


# Verifies: S04-R4
class BaselineOverwriteTests(BaselineTestCase):
    def test_second_write_for_same_alias_and_time_is_refused(self):
        path = write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        original = path.read_bytes()
        later_state = full_snapshot(resources={"minerals": "1", "gas": "2", "starbux": "3"})
        with self.assertRaises(BaselineExistsError):
            write_baseline(later_state, self.dir, SERVER_VERSION)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(list(self.dir.iterdir())), 1)

    def test_same_second_counts_as_the_same_time(self):
        write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        with self.assertRaises(BaselineExistsError):
            write_baseline(full_snapshot(observed_at=T0.replace(microsecond=0)), self.dir, SERVER_VERSION)

    def test_existing_file_not_written_by_this_module_is_kept(self):
        self.dir.mkdir(parents=True)
        path = self.dir / baseline_filename("alpha", T0)
        path.write_text("owner notes\n", encoding="utf-8")
        with self.assertRaises(BaselineExistsError):
            write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        self.assertEqual(path.read_text(encoding="utf-8"), "owner notes\n")

    def test_other_alias_or_time_gets_its_own_file(self):
        write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        write_baseline(full_snapshot(alias="bravo"), self.dir, SERVER_VERSION)
        write_baseline(full_snapshot(observed_at=T0 + timedelta(seconds=1)), self.dir, SERVER_VERSION)
        self.assertEqual(len(list(self.dir.iterdir())), 3)


# Verifies: S04-R5
class BaselineRoundTripTests(BaselineTestCase):
    def test_round_trip_returns_an_equal_snapshot(self):
        snapshot = full_snapshot()
        loaded = load_baseline_file(write_baseline(snapshot, self.dir, SERVER_VERSION))
        self.assertEqual(loaded.snapshot, snapshot)
        self.assertEqual(loaded.game_version, SERVER_VERSION)
        self.assertEqual(load_baseline(loaded.path), snapshot)

    def test_round_trip_of_sparse_snapshot_keeps_sections_missing(self):
        snapshot = build_snapshot("alpha", T0, "synthetic-fixture", rooms=ROOMS + [{"@RoomDesignId": "9"}])
        loaded = load_baseline(write_baseline(snapshot, self.dir, None))
        self.assertEqual(loaded, snapshot)
        self.assertIs(loaded.section("crew"), Completeness.MISSING)
        self.assertIs(loaded.section("rooms"), Completeness.PARTIAL)
        self.assertIsNone(loaded.resources.minerals)

    def test_loaded_baseline_diffs_cleanly_against_itself_and_a_later_snapshot(self):
        snapshot = full_snapshot()
        loaded = load_baseline(write_baseline(snapshot, self.dir, SERVER_VERSION))
        same = diff_snapshots(loaded, snapshot)
        self.assertEqual((same.minerals, same.trophies, same.hull_level), (0, 0, 0))
        self.assertEqual(same.room_design, {})
        self.assertEqual(same.crew_level, {})
        self.assertEqual(same.inventory_quantity, {})
        self.assertEqual(same.unknown_sections, ())

        later = full_snapshot(
            observed_at=T0 + timedelta(hours=1),
            resources={"minerals": "1500", "gas": "2000", "starbux": "15"},
            rooms=[ROOMS[0], {"@RoomId": "r2", "@RoomDesignId": "22", "@RoomStatus": "Normal"}],
        )
        delta = diff_snapshots(loaded, later)
        self.assertEqual(delta.minerals, 500)
        self.assertEqual(delta.room_design, {"r2": ("21", "22")})
        self.assertEqual(delta.elapsed_seconds, 3600)

    def test_malformed_or_unsafe_files_are_refused(self):
        good = self.read_json(write_baseline(full_snapshot(), self.dir, SERVER_VERSION))

        def variant(**changes):
            record = dict(good)
            record.update(changes)
            return record

        cases = {
            "unknown key": variant(player_name="SyntheticCaptain"),
            "wrong format": variant(format="other"),
            "missing section with records": variant(
                completeness={**good["completeness"], "crew": "missing"}),
            "observed section as null": variant(rooms=None),
            "naive time": variant(observed_at="2026-11-10T12:00:05"),
            "non-UTC time": variant(observed_at="2026-11-10T07:00:05-05:00"),
            "raw user id identity": variant(game_identity="4242"),
            "boolean as number": variant(trophies=True),
            "bad completeness": variant(completeness={**good["completeness"], "ship": "unknown"}),
            "planted e-mail": variant(notes=[SYNTHETIC_EMAIL]),
        }
        for index, (label, record) in enumerate(cases.items()):
            path = self.dir / f"case-{index}.txt"
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.subTest(label):
                with self.assertRaises(BaselineError) as caught:
                    load_baseline(path)
                self.assertNotIn(SYNTHETIC_EMAIL, str(caught.exception))


# Verifies: S04-R7
class BaselineScanTests(BaselineTestCase):
    def test_clean_baseline_directory_passes(self):
        write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        write_baseline(full_snapshot(alias="bravo"), self.dir, SERVER_VERSION)
        self.assertEqual(scan_baseline_directory(self.dir), [])

    def test_planted_secret_in_a_baseline_file_fails_the_scan(self):
        path = write_baseline(full_snapshot(), self.dir, SERVER_VERSION)
        clean = path.read_text(encoding="utf-8")
        plants = {
            "e-mail": SYNTHETIC_EMAIL,
            "access token": f"accessToken={SYNTHETIC_ACCESS_TOKEN}",
            "device key": f'"DeviceKey":"{SYNTHETIC_DEVICE_KEY_IOS}"',
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGljLXNpZ25hdHVyZQ",
            "user id json": '"UserId": "4242"',
            "user id xml": 'AttackingUserId="4242"',
            "bare id": '"Id": 4242',
        }
        for label, plant in plants.items():
            with self.subTest(label):
                path.write_text(clean.replace('"source": "synthetic-fixture"', f'"source": "{plant}"'),
                                encoding="utf-8")
                findings = scan_baseline_directory(self.dir)
                self.assertTrue(findings, label)
                self.assertEqual({finding.path for finding in findings}, {path})
                for finding in findings:
                    self.assertNotIn("4242", str(finding).replace(str(path), ""))
                    self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, str(finding))

    def test_secret_hidden_by_json_escaping_fails_the_scan(self):
        snapshot = replace(full_snapshot(), notes=(f'"DeviceKey":"{SYNTHETIC_DEVICE_KEY_IOS}"', 'UserId="4242"'))
        self.dir.mkdir(parents=True)
        path = self.dir / "alpha-20261110T120005Z.json"
        path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
        rules = {finding.rule for finding in scan_baseline_directory(self.dir)}
        self.assertIn("sdk.redaction pattern 5 (in a JSON string)", rules)
        self.assertIn("numeric id in a UserId/Id field (in a JSON string)", rules)

    def test_gameplay_ids_and_zero_user_id_are_not_flagged(self):
        text = '"room_id": "12", "@RoomDesignId": "11", "item_design_id": "900", UserId="0"'
        self.assertEqual(scan_text(text), [])

    def test_non_text_files_are_reported(self):
        self.dir.mkdir(parents=True)
        (self.dir / "blob.bin").write_bytes(b"\xff\xfe\x00")
        self.assertEqual([finding.rule for finding in scan_baseline_directory(self.dir)],
                         ["not readable as UTF-8 text"])

    def test_repository_baseline_directory_passes_the_scan(self):
        findings = scan_baseline_directory(REPO_ROOT / "docs" / "experiments" / "baselines")
        self.assertEqual([str(finding) for finding in findings], [])


if __name__ == "__main__":
    unittest.main()
