"""Tests for run.py --capture-reads and sdk.state.capture (SPEC-03, S03-R1 and S03-R2).

Exercises the flags --capture-reads, --registry, --output and --identity-salt-env with fake clients
that serve realistic synthetic XML with planted ids, names, tokens and e-mails, and with the real
Client behind an in-memory HTTP adapter. Sockets are patched to raise. No real credentials, ids or
e-mails are used, and nothing is written under tests/fixtures/.
"""

import inspect
import io
import json
import logging
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
import xmltodict
from requests.adapters import BaseAdapter

import run  # imported here so run.py's log file opens in the repository, not a test tmpdir
from sdk.state.capture import (
    CAPTURE_METHODS,
    CAPTURE_READS,
    SETTINGS_SUMMARY,
    MANIFEST_NAME,
    README_COLUMNS,
    REDACTED,
    Placeholders,
    _letters,
    field_category,
    sanitize_capture,
    scan_capture_text,
)
from sdk.state.snapshot import game_identity_fingerprint
from tests import test_profile_reads as profile_tests  # a module import: its test cases are not rerun here
from tests.synthetic_fixtures import (
    SYNTHETIC_ACCESS_TOKEN,
    SYNTHETIC_DEVICE_KEY_IOS,
    SYNTHETIC_DEVICE_KEY_MAC,
    SYNTHETIC_EMAIL,
    SYNTHETIC_IOS_CHECKSUM_1,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((REPO_ROOT / "docs/baseline/capability-inventory.json").read_text(encoding="utf-8"))
KINDS = {entry["name"]: entry["kind"] for entry in INVENTORY["client_methods"]}
MUTATING_OR_SESSION = {name for name, kind in KINDS.items() if kind in ("mutating", "session")}

OBSERVED = datetime(2026, 11, 12, 9, 30, 0, tzinfo=timezone.utc)
SERVER_VERSION = "0.999.60"
SALT = "synthetic-capture-salt-0001"
ALPHA_REFRESH = "synthetic-refresh-alpha-0001"
ALPHA_AUTH = f"capture|{SYNTHETIC_DEVICE_KEY_IOS}|{ALPHA_REFRESH}|en"
BRAVO_EMAIL = SYNTHETIC_EMAIL
BRAVO_PASSWORD = "synthetic-password-bravo-0002"
REGISTRY = {"version": 1, "accounts": [
    {"alias": "alpha", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "ALPHA_AUTH"},
    {"alias": "bravo", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "BRAVO"},
    {"alias": "charlie", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "CHARLIE_AUTH",
     "enabled": False},
]}
ENV = {"ALPHA_AUTH": ALPHA_AUTH, "BRAVO_EMAIL": BRAVO_EMAIL, "BRAVO_PASSWORD": BRAVO_PASSWORD, "SYNTH_SALT": SALT}

# Planted values: every one of them must be gone from every file the command writes.
UIDS = {"alpha": "700000101", "bravo": "700000202"}
RIVAL = "700000333"
NAMES = {"700000101": "SyntheticCaptainAlpha", "700000202": "SyntheticCaptainBravo",
         "700000333": "SyntheticRivalCharlie"}
SHIP_NAME = "SyntheticVesselOne"
ALLIANCE_NAME = "SyntheticAllianceOne"
OWN_SECRET = {"alpha": ALPHA_REFRESH, "bravo": BRAVO_PASSWORD}  # echoed by a free-text field
LONG_BLOB = "QUFB" * 11  # base64-shaped
PLANTED = (*UIDS.values(), RIVAL, *NAMES.values(), SHIP_NAME, ALLIANCE_NAME, ALPHA_REFRESH, ALPHA_AUTH,
           BRAVO_EMAIL, BRAVO_PASSWORD, SALT, SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_DEVICE_KEY_IOS,
           SYNTHETIC_DEVICE_KEY_MAC, SYNTHETIC_IOS_CHECKSUM_1, LONG_BLOB)
UNSANITIZABLE_KEY = "X" + "a" * 44  # an element name that is a long base64-shaped run


def login_xml(uid, alias, access_token=SYNTHETIC_ACCESS_TOKEN):
    return (f'<UserService><UserLogin UserId="{uid}" accessToken="{access_token}">'
            f'<User Id="{uid}" Name="{NAMES[uid]}" Email="{SYNTHETIC_EMAIL}" Trophy="1534" Credits="250" '
            f'AllianceName="{ALLIANCE_NAME}" DeviceKey="{SYNTHETIC_DEVICE_KEY_MAC}" '
            f'StatusMessage="ping me at {SYNTHETIC_EMAIL}" LastLoginDate="2026-11-11T00:00:00"/>'
            f'</UserLogin></UserService>')


def latest_xml():
    return (f'<SettingService><GetLatestSetting><Setting RoomDesignVersion="1" ResearchDesignVersion="1" '
            f'LatestVersion="{SERVER_VERSION}" MinimumClientVersion="0.998.0" '
            f'Checksum="{SYNTHETIC_IOS_CHECKSUM_1}"/></GetLatestSetting></SettingService>')


def ship_xml(uid):
    return (f'<ShipService><GetShipByUserId><Ship ShipId="88001" UserId="{uid}" ShipDesignId="17" '
            f'ShipName="{SHIP_NAME}" ShipLevel="5"><Rooms><Room RoomId="1" RoomDesignId="100"/></Rooms>'
            f'<Researches><Research ResearchDesignId="400"/></Researches></Ship></GetShipByUserId></ShipService>')


def characters_xml(uid):
    return (f'<CharacterService><ListAllCharactersOfUser><Characters><Character CharacterId="501" '
            f'OwnerUserId="{uid}" CharacterDesignId="9" Level="3"/></Characters></ListAllCharactersOfUser>'
            f'</CharacterService>')


def items_xml():
    return ('<ItemService><ListItemsOfAShip><Items><Item ItemId="61" ItemDesignId="1" Quantity="1000"/>'
            '</Items></ListItemsOfAShip></ItemService>')


def ranking_xml(uid):
    return (f'<LadderService><FindUserRanking><Users>'
            f'<User Id="{RIVAL}" Name="{NAMES[RIVAL]}" Trophy="1600" Ranking="10" AllianceName="{ALLIANCE_NAME}"/>'
            f'<User Id="{uid}" Name="{NAMES[uid]}" Trophy="1534" Ranking="11"/>'
            f'</Users></FindUserRanking></LadderService>')


def battles_xml(uid, alias):
    return (f'<BattleService><ListPvPBattles2><Battles>'
            f'<Battle BattleId="9001" AttackingUserId="{uid}" DefendingUserId="{RIVAL}" '
            f'AttackingUserName="{NAMES[uid]}" DefendingUserName="{NAMES[RIVAL]}" AttackingShipName="{SHIP_NAME}" '
            f'Status="Won" TrophyChange="12" HullLevel="10" ReplayToken="{SYNTHETIC_ACCESS_TOKEN}"/>'
            f'<Battle BattleId="9002" AttackingUserId="{RIVAL}" DefendingUserId="{uid}" '
            f'Note="session {OWN_SECRET[alias]}" Blob="{LONG_BLOB}" TrophyChange="-8">'
            f'<UserId>{RIVAL}</UserId></Battle>'
            f'</Battles></ListPvPBattles2></BattleService>')


def parse(text):
    return xmltodict.parse(text, xml_attribs=True)


def forbidden(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"capture touched {name}")
    return _raise


class CaptureClient:
    """Logs in and serves the read-only responses as ``Client`` stores them (xmltodict output).

    Every attribute it does not define is recorded in ``touched``. ``overrides`` maps a method name
    to a callable that replaces its behaviour for one test.
    """

    instances = []
    touched = []
    overrides = {}
    login_error = False
    same_account = False  # True: every alias logs in to alpha's game account

    def __init__(self, device=None, settings=None):
        self.device = device
        self.settings = settings
        self.calls = []
        CaptureClient.instances.append(self)

    def __getattr__(self, name):
        CaptureClient.touched.append(name)
        raise AttributeError(name)

    def quickReload(self):  # what Client.request runs on an expired token: a fresh DeviceLogin17
        self.calls.append("quickReload")

    def login(self, email=None, password=None):
        self.calls.append("login")
        if CaptureClient.login_error:
            raise RuntimeError(f"login rejected for {email} with {password} / {self.device.authentication_string}")
        self.alias = "bravo" if email else "alpha"
        uid = UIDS["alpha" if CaptureClient.same_account else self.alias]
        self.user = SimpleNamespace(id=uid, isAuthorized=True)
        self.info = parse(login_xml(uid, self.alias))["UserService"]["UserLogin"]["User"]
        self.latestVersion = parse(latest_xml())
        return True

    def _serve(self, method, attribute, text):
        self.calls.append(method)
        override = CaptureClient.overrides.get(method)
        if override is not None:
            return override(self)
        setattr(self, attribute, parse(text))
        return True

    def getShipByUserId(self, userId=0):
        return self._serve("getShipByUserId", "shipByUserId", ship_xml(self.user.id))

    def listAllCharactersOfUser(self):
        return self._serve("listAllCharactersOfUser", "allCharactersOfUser", characters_xml(self.user.id))

    def listItemsOfAShip(self):
        return self._serve("listItemsOfAShip", "itemsOfAShip", items_xml())

    def findUserRanking(self):
        return self._serve("findUserRanking", "userRanking", ranking_xml(self.user.id))

    def listPvPBattles2(self, take=25, skip=0):
        self.pvp_arguments = (take, skip)
        return self._serve("listPvPBattles2", "pvpBattles", battles_xml(self.user.id, self.alias))


class CaptureCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.output = self.dir / "captures"
        self.registry = self.dir / "fleet.json"
        self.registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        CaptureClient.instances = []
        CaptureClient.touched = []
        CaptureClient.overrides = {}
        CaptureClient.login_error = False
        CaptureClient.same_account = False
        cwd = os.getcwd()
        os.chdir(self.dir)  # EphemeralDevice must never touch ./.device
        self.addCleanup(os.chdir, cwd)

    def run_main(self, *args, env=ENV, output=None, registry=True):
        argv = ["run.py", "--capture-reads", *args]
        if registry:
            argv += ["--registry", str(self.registry)]
        if output is not False:
            argv += ["--output", str(output or self.output)]
        stdout, stderr, log = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(log)
        clock = patch("run.run_capture_reads", partial(run.run_capture_reads, clock=lambda: OBSERVED))
        with patch.object(sys, "argv", argv), patch.dict(os.environ, env, clear=True), \
                patch("run.Client", CaptureClient), clock, \
                patch.object(logging.getLogger(), "handlers", [handler]), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch("getpass.getpass", side_effect=forbidden("getpass")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        self.assertFalse((self.dir / ".device").exists())
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue(), log.getvalue()

    def files(self):
        if not self.output.is_dir():
            return []
        return sorted(path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file())

    def manifest(self):
        return json.loads((self.output / MANIFEST_NAME).read_text(encoding="utf-8"))

    def capture(self, alias, stem):
        return parse((self.output / alias / f"{stem}.xml").read_text(encoding="utf-8"))

    def everything_written(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in self.output.rglob("*") if path.is_file())

    def assert_no_planted_value(self, *texts):
        combined = "\n".join(texts)
        self.assertEqual([value for value in PLANTED if value in combined], [], combined)

    # Verifies: S03-R1
    def test_writes_one_file_per_alias_and_endpoint_and_a_manifest(self):
        code, out, err, _ = self.run_main()
        self.assertEqual(code, 0, err + out)
        expected = sorted([f"{alias}/{read.stem}.xml" for alias in ("alpha", "bravo") for read in CAPTURE_READS]
                          + [f"{alias}/{SETTINGS_SUMMARY.stem}.json" for alias in ("alpha", "bravo")]
                          + [MANIFEST_NAME])
        self.assertEqual(self.files(), expected)  # charlie is disabled: nothing for it
        manifest = self.manifest()
        self.assertEqual(manifest["source"], "run.py --capture-reads")
        self.assertEqual(manifest["readme_columns"], list(README_COLUMNS))
        self.assertEqual(len(manifest["captures"]), 2 * (len(CAPTURE_READS) + 1))
        for entry in manifest["captures"]:
            with self.subTest(entry=entry["file"]):
                self.assertEqual(entry["status"], "written")
                self.assertEqual(entry["scan"], "clean")
                self.assertEqual(entry["game_version"], SERVER_VERSION)  # the server's setting, not ClientVersion
                self.assertEqual(entry["captured_at"], "2026-11-12T09:30:00Z")
                self.assertTrue(entry["sanitization_steps"][0].startswith("2: "))
                row = entry["table_row"]
                self.assertEqual(row.count(" | ") + 1, len(README_COLUMNS))
                self.assertTrue(row.startswith(f"| {entry['file']} | {entry['endpoint']} | 2026-11-12 | "
                                               f"{SERVER_VERSION} | "))
                self.assertTrue(row.endswith("| pending |"))
        endpoints = {entry["endpoint"] for entry in manifest["captures"]}
        self.assertEqual(endpoints, {read.endpoint for read in CAPTURE_READS} | {SETTINGS_SUMMARY.endpoint})
        self.assertIn("FindUserRanking", endpoints)
        self.assertIn("ListPvPBattles2", endpoints)
        self.assertIn("| alpha | logged in | 0.999.60 | 8 | 0 |", out)
        self.assertIn("| charlie | disabled |", out)
        self.assertIn("## Rows for tests/fixtures/live_sanitized/README.md", out)

    # Verifies: S03-R1
    def test_only_read_only_methods_are_called(self):
        for method in CAPTURE_METHODS + ("getLatestVersion3",):
            with self.subTest(method=method):
                self.assertEqual(KINDS.get(method), "read-only", f"{method} is not read-only in the inventory")
        self.assertFalse(set(CAPTURE_METHODS) & MUTATING_OR_SESSION)
        code, _, err, _ = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(CaptureClient.instances), 2)
        for client in CaptureClient.instances:
            self.assertEqual(client.calls, ["login", *CAPTURE_METHODS])
            self.assertEqual(client.pvp_arguments, (25, 0))
            self.assertNotIn("quickReload", vars(client))  # the re-login guard is removed afterwards
        self.assertEqual(CaptureClient.touched, [])

    # Verifies: S03-R1, S03-R2
    def test_nothing_secret_survives_in_any_output(self):
        code, out, err, log = self.run_main("--identity-salt-env", "SYNTH_SALT")
        self.assertEqual(code, 0, err + out)
        written = self.everything_written()
        self.assert_no_planted_value(written, out, err, log)
        self.assertNotIn("@", written.replace('encoding="utf-8"', ""))  # no e-mail address of any form
        for path in self.output.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertEqual(scan_capture_text(text), [], path.name)
        # The files would pass the S03-R2 fixture scan once the owner copies them.
        self.assertEqual(profile_tests.scan_tree(self.output), [])
        user = self.capture("alpha", "user_login")["User"]
        self.assertEqual(user["@Email"], "***REDACTED_EMAIL***")
        self.assertEqual(user["@DeviceKey"], REDACTED)
        self.assertEqual(user["@StatusMessage"], REDACTED)
        self.assertEqual((user["@Trophy"], user["@Credits"]), ("1534", "250"))  # game data stays
        setting = self.capture("alpha", "get_latest_version3")["SettingService"]["GetLatestSetting"]["Setting"]
        self.assertEqual((setting["@Checksum"], setting["@LatestVersion"]), (REDACTED, SERVER_VERSION))
        battle, defense = self.capture("alpha", "list_pvp_battles2")["BattleService"]["ListPvPBattles2"][
            "Battles"]["Battle"]
        self.assertEqual(battle["@ReplayToken"], REDACTED)
        self.assertEqual((defense["@Note"], defense["@Blob"]), (REDACTED, REDACTED))
        self.assertEqual((battle["@BattleId"], battle["@TrophyChange"], battle["@HullLevel"]), ("BATTLEID-A", "12", "10"))

    # Verifies: S03-R1
    def test_placeholders_are_stable_and_consistent_across_files(self):
        code, _, err, _ = self.run_main()
        self.assertEqual(code, 0, err)
        for alias in ("alpha", "bravo"):
            with self.subTest(alias=alias):
                user = self.capture(alias, "user_login")["User"]
                ship = self.capture(alias, "get_ship_by_user_id")["ShipService"]["GetShipByUserId"]["Ship"]
                crew = self.capture(alias, "list_all_characters_of_user")["CharacterService"][
                    "ListAllCharactersOfUser"]["Characters"]["Character"]
                rival, me = self.capture(alias, "find_user_ranking")["LadderService"]["FindUserRanking"][
                    "Users"]["User"]
                attack, defense = self.capture(alias, "list_pvp_battles2")["BattleService"]["ListPvPBattles2"][
                    "Battles"]["Battle"]
                # The account itself is USER-A and PLAYER-A in every file, opponents follow.
                self.assertEqual({user["@Id"], ship["@UserId"], crew["@OwnerUserId"], me["@Id"],
                                  attack["@AttackingUserId"], defense["@DefendingUserId"]}, {"USER-A"})
                self.assertEqual({rival["@Id"], attack["@DefendingUserId"], defense["@AttackingUserId"],
                                  defense["UserId"]}, {"USER-B"})
                self.assertEqual({user["@Name"], me["@Name"], attack["@AttackingUserName"]}, {"PLAYER-A"})
                self.assertEqual({rival["@Name"], attack["@DefendingUserName"]}, {"PLAYER-B"})
                self.assertEqual({ship["@ShipName"], attack["@AttackingShipName"]}, {"SHIP-A"})
                self.assertEqual({user["@AllianceName"], rival["@AllianceName"]}, {"ALLIANCE-A"})
                self.assertEqual(ship["@ShipDesignId"], "17")  # design ids are game data
        # A second run of the same responses gives byte-identical files.
        first = {name: (self.output / name).read_text(encoding="utf-8") for name in self.files()
                 if name != MANIFEST_NAME}
        self.output = self.dir / "captures-again"
        CaptureClient.instances = []
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(first, {name: (self.output / name).read_text(encoding="utf-8") for name in first})

    # Verifies: S03-R1, S03-R2
    def test_a_response_that_cannot_be_sanitized_clean_is_withheld(self):
        def unsanitizable(client):
            client.userRanking = {"LadderService": {UNSANITIZABLE_KEY: {"@Trophy": "1600"}}}

        def broken(client):
            client.pvpBattles = f'<BattleService><ListPvPBattles2 UserId="{RIVAL}"'  # raw, not well-formed

        CaptureClient.overrides = {"findUserRanking": unsanitizable, "listPvPBattles2": broken}
        code, out, err, log = self.run_main()
        self.assertEqual(code, 1, out)
        files = self.files()
        for alias in ("alpha", "bravo"):
            self.assertNotIn(f"{alias}/find_user_ranking.xml", files)
            self.assertNotIn(f"{alias}/find_user_ranking.json", files)
            self.assertNotIn(f"{alias}/list_pvp_battles2.xml", files)
            self.assertIn(f"{alias}/get_ship_by_user_id.xml", files)  # the others are still written
        entries = {(entry["alias"], entry["endpoint"]): entry for entry in self.manifest()["captures"]}
        ranking = entries[("alpha", "FindUserRanking")]
        self.assertEqual((ranking["status"], ranking["file"], ranking["table_row"]), ("withheld", None, None))
        self.assertEqual(ranking["scan"], "withheld: redaction pattern 1 (line 3)")
        battles = entries[("alpha", "ListPvPBattles2")]
        self.assertEqual((battles["status"], battles["scan"]),
                         ("withheld", "withheld: response is not well-formed XML"))
        self.assertIn("| alpha | FindUserRanking | withheld |", out)
        self.assertIn("| alpha | logged in | 0.999.60 | 6 | 2 |", out)
        # The rule is reported, never the value.
        self.assertNotIn(UNSANITIZABLE_KEY, out + err + log + self.everything_written())
        self.assert_no_planted_value(out, err, log, self.everything_written())

    # Verifies: S03-R1
    def test_read_errors_are_redacted_and_do_not_stop_other_reads(self):
        def failing(client):
            raise RuntimeError(
                f"GET https://api.pixelstarships.com/ShipService/GetShipByUserId?userId={client.user.id}"
                f"&accessToken={SYNTHETIC_ACCESS_TOKEN} failed for {ALPHA_REFRESH} {BRAVO_PASSWORD} "
                f"{NAMES[client.user.id]} {SYNTHETIC_EMAIL}")

        CaptureClient.overrides = {"getShipByUserId": failing}
        code, out, err, log = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("| alpha | GetShipByUserId | error |", out)
        entry = next(entry for entry in self.manifest()["captures"]
                     if entry["alias"] == "alpha" and entry["endpoint"] == "GetShipByUserId")
        self.assertTrue(entry["reason"].startswith("RuntimeError: "), entry["reason"])
        self.assertIn("alpha/list_pvp_battles2.xml", self.files())
        self.assert_no_planted_value(out, err, log, self.everything_written())

    # Verifies: S03-R1
    def test_login_errors_are_redacted_and_write_nothing_for_the_account(self):
        CaptureClient.login_error = True
        code, out, err, log = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.files(), [MANIFEST_NAME])
        self.assertIn("| alpha | error |", out)
        self.assertIn("| bravo | error |", out)
        for client in CaptureClient.instances:
            self.assertEqual(client.calls, ["login"])
        self.assert_no_planted_value(out, err, log, self.everything_written())

    # Verifies: S03-R1
    def test_an_expired_session_is_never_logged_in_again(self):
        def expired(client):
            client.quickReload()  # Client.request does this on "Failed to authorize access token"

        CaptureClient.overrides = {"getShipByUserId": expired}
        code, out, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("SessionExpiredError: the session expired during the capture; refusing to log in again", out)
        for client in CaptureClient.instances:
            self.assertNotIn("quickReload", client.calls)
            self.assertEqual(client.calls, ["login", *CAPTURE_METHODS])
            self.assertNotIn("quickReload", vars(client))

    # Verifies: S03-R1
    def test_two_aliases_on_one_game_account_are_refused_before_any_read(self):
        CaptureClient.same_account = True
        code, out, _, _ = self.run_main("--identity-salt-env", "SYNTH_SALT")
        self.assertEqual(code, 1)
        alpha, bravo = CaptureClient.instances
        self.assertEqual(alpha.calls, ["login", *CAPTURE_METHODS])
        self.assertEqual(bravo.calls, ["login"])
        self.assertIn("| bravo | refused |", out)
        self.assertIn("already observed for 'alpha'", out)
        self.assertFalse((self.output / "bravo").exists())

    # Verifies: S03-R1
    def test_an_identity_that_differs_from_the_registry_is_refused(self):
        registry = json.loads(json.dumps(REGISTRY))
        registry["accounts"][0]["expected_game_identity"] = game_identity_fingerprint("700000999", SALT)
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        code, out, _, _ = self.run_main("--identity-salt-env", "SYNTH_SALT")
        self.assertEqual(code, 1)
        self.assertEqual(CaptureClient.instances[0].calls, ["login"])
        self.assertIn("| alpha | refused |", out)
        self.assertFalse((self.output / "alpha").exists())

    # Verifies: S03-R1
    def test_never_writes_into_tests_fixtures_or_over_existing_files(self):
        inside = REPO_ROOT / "tests" / "fixtures" / "live_sanitized" / "capture-test-output"
        code, _, err, _ = self.run_main(output=inside)
        self.assertEqual(code, 2)
        self.assertIn("outside tests/fixtures/", err)
        self.assertFalse(inside.exists())
        self.output.mkdir()
        (self.output / "earlier.xml").write_text("<Earlier/>", encoding="utf-8")
        code, _, err, _ = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("must be a new or empty directory", err)
        self.assertEqual(self.files(), ["earlier.xml"])
        self.assertEqual(CaptureClient.instances, [])  # refused before any login

    def test_invalid_registry_or_salt_exits_before_login(self):
        self.registry.unlink()
        self.assertEqual(self.run_main()[0], 2)
        self.registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        code, _, err, _ = self.run_main("--identity-salt-env", "MISSING_SALT")
        self.assertEqual(code, 2)
        self.assertIn("MISSING_SALT is not set", err)
        self.assertEqual(CaptureClient.instances, [])
        self.assertFalse(self.output.exists())

    def test_flag_conflicts_and_missing_flags(self):
        for extra, message in ((("--baseline",), "cannot be combined with --baseline"),
                               (("--concurrent", "2"), "cannot be combined with --concurrent"),
                               (("--fleet-dry-run",), "cannot be combined with --fleet-dry-run"),
                               (("--auth-file", "x"), "cannot be combined with --auth-file")):
            with self.subTest(extra=extra):
                code, _, err, _ = self.run_main(*extra)
                self.assertEqual(code, 2)
                self.assertIn(message, err)
        code, _, err, _ = self.run_main(output=False)
        self.assertEqual(code, 2)
        self.assertIn("--capture-reads needs --registry PATH and --output DIR", err)
        self.assertEqual(CaptureClient.instances, [])


class SanitizerTests(unittest.TestCase):
    # Verifies: S03-R2
    def test_scan_matches_the_live_sanitized_fixture_scan(self):
        samples = (*profile_tests._REDACTION_SAMPLES, *profile_tests._NUMERIC_ID_SAMPLES,
                   *profile_tests._EMAIL_SAMPLES, *profile_tests._TOKEN_SAMPLES,
                   profile_tests._CLEAN_XML, profile_tests._CLEAN_JSON,
                   (REPO_ROOT / "tests/fixtures/live_sanitized/README.md").read_text(encoding="utf-8"))
        for sample in samples:
            with self.subTest(sample=sample[:60]):
                self.assertEqual([tuple(f) for f in scan_capture_text(sample, "x")],
                                 [tuple(f) for f in profile_tests.scan_text(sample, "x")])
        self.assertTrue(scan_capture_text(profile_tests._NUMERIC_ID_SAMPLES[0]))
        self.assertEqual(scan_capture_text(profile_tests._CLEAN_XML), [])

    def test_field_categories(self):
        cases = {"@Id": "USER", "UserId": "USER", "@AttackingUserId": "USER", "@accountid": "USER",
                 "@Name": "PLAYER", "@UserName": "PLAYER", "@DefendingUserName": "PLAYER", "PlayerName": "PLAYER",
                 "@ShipName": "SHIP", "@AllianceName": "ALLIANCE", "@accessToken": "secret",
                 "@RefreshToken": "secret", "@DeviceKey": "secret", "@Checksum": "secret", "Password": "secret",
                 "@client_secret": "secret", "@Email": "email", "@ShipDesignId": None, "@BattleId": "instance",
                 "@Trophy": None, "@RoomName": None, "@CharacterName": "CREW", "@IsPaid": None,
                 "@IconSpriteId": None, "@ActionTypeId": None, "@StarSystemId": None,
                 "@UserStarSystemId": "instance", "@UnlockedShipDesignIds": None, "@ItemIds": "instance"}
        for name, category in cases.items():
            with self.subTest(name=name):
                self.assertEqual(field_category(name), category)

    def test_placeholders_are_letters_per_value(self):
        self.assertEqual([_letters(i) for i in (0, 1, 25, 26, 27, 701, 702)],
                         ["A", "B", "Z", "AA", "AB", "ZZ", "AAA"])
        placeholders = Placeholders()
        placeholders.seed("USER", "42")
        self.assertEqual(placeholders.get("USER", "7"), "USER-B")
        self.assertEqual(placeholders.get("USER", "42"), "USER-A")
        self.assertEqual(placeholders.get("PLAYER", "7"), "PLAYER-A")  # categories are independent

    # Verifies: S03-R1, S03-R2
    def test_raw_xml_and_parsed_responses_sanitize_alike(self):
        text = battles_xml(UIDS["alpha"], "alpha")
        from_text = sanitize_capture(text, Placeholders())
        from_dict = sanitize_capture(parse(text), Placeholders())
        self.assertEqual(from_text, from_dict)
        self.assertTrue(from_text.clean)
        self.assertEqual(from_text.extension, ".xml")

    # Verifies: S03-R1, S03-R2
    def test_json_shaped_payloads_are_sanitized_and_empty_values_kept(self):
        payload = {"first": {"UserId": 700000101, "accessToken": "", "Email": None},
                   "second": [{"@Id": "700000101", "@Name": NAMES["700000101"], "@Level": 3}]}
        capture = sanitize_capture(payload, Placeholders())
        self.assertEqual(capture.extension, ".json")  # two roots: not an XML document
        self.assertTrue(capture.clean, capture.findings)
        self.assertEqual(json.loads(capture.text), {
            "first": {"UserId": "USER-A", "accessToken": "", "Email": None},
            "second": [{"@Id": "USER-A", "@Name": "PLAYER-A", "@Level": 3}]})


# --- Real Client, fake HTTP adapter -------------------------------------------------------------

REAL_REFRESH = {"alpha": "RT-alpha-synthetic-capture-0001", "bravo": "RT-bravo-synthetic-capture-0002"}
REAL_TOKENS = {"alpha": "aaaaaaaa-1111-2222-3333-444444444444", "bravo": "bbbbbbbb-1111-2222-3333-444444444444"}
REAL_ENV = {"SYNTH_ALPHA": f"capture|{SYNTHETIC_DEVICE_KEY_IOS}|{REAL_REFRESH['alpha']}|en",
            "SYNTH_BRAVO": f"capture|{SYNTHETIC_DEVICE_KEY_MAC}|{REAL_REFRESH['bravo']}|en"}
REAL_REGISTRY = {"version": 1, "accounts": [
    {"alias": alias, "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": f"SYNTH_{alias.upper()}"}
    for alias in ("alpha", "bravo")]}
ALLOWED_PATHS = {"/UserService/DeviceLogin17", "/SettingService/GetLatestVersion3",
                 "/LiveOpsService/GetTodayLiveOps2", "/ShipService/GetShipByUserId",
                 "/CharacterService/ListAllCharactersOfUser", "/ItemService/ListItemsOfAShip",
                 "/LadderService/FindUserRanking", "/BattleService/ListPvPBattles2"}


def xml_response(text):
    response = requests.Response()
    response.status_code = 200
    response._content = text.encode()
    response.encoding = "utf-8"
    return response


class CaptureGameAdapter(BaseAdapter):
    """Answers the real Client's requests in memory with the synthetic responses; anything else fails."""

    def __init__(self):
        super().__init__()
        self.requests = []

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        self.requests.append((parts.path, parse_qs(parts.query)))
        if parts.path not in ALLOWED_PATHS:
            raise AssertionError(f"capture sent a request outside the read-only list: {parts.path}")
        if parts.path.endswith("DeviceLogin17"):
            alias = next(a for a, token in REAL_REFRESH.items() if json.loads(request.body)["RefreshToken"] == token)
            self.alias_by_token = getattr(self, "alias_by_token", {})
            self.alias_by_token[REAL_TOKENS[alias]] = alias
            return xml_response(login_xml(UIDS[alias], alias, REAL_TOKENS[alias]))
        if parts.path.endswith("GetLatestVersion3"):
            return xml_response(latest_xml())
        if parts.path.endswith("GetTodayLiveOps2"):
            return xml_response("<LiveOpsService><GetTodayLiveOps/></LiveOpsService>")
        alias = self.alias_by_token[parse_qs(parts.query)["accessToken"][0]]
        uid = UIDS[alias]
        bodies = {"GetShipByUserId": ship_xml(uid), "ListAllCharactersOfUser": characters_xml(uid),
                  "ListItemsOfAShip": items_xml(), "FindUserRanking": ranking_xml(uid),
                  "ListPvPBattles2": battles_xml(uid, alias).replace(OWN_SECRET[alias], REAL_REFRESH[alias])}
        return xml_response(bodies[parts.path.rsplit("/", 1)[1]])

    def close(self):
        pass


class RealClientCaptureTests(unittest.TestCase):
    # Verifies: S03-R1, S03-R2
    def test_real_client_captures_only_read_only_endpoints_and_leaks_nothing(self):
        from sdk.client import Client

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        registry, output = directory / "fleet.json", directory / "captures"
        registry.write_text(json.dumps(REAL_REGISTRY), encoding="utf-8")
        adapter = CaptureGameAdapter()
        log = io.StringIO()
        argv = ["run.py", "--capture-reads", "--registry", str(registry), "--output", str(output)]
        stdout, stderr = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, cwd)
        with patch.object(sys, "argv", argv), patch.dict(os.environ, REAL_ENV, clear=True), \
                patch.object(logging.getLogger(), "handlers", [logging.StreamHandler(log)]), \
                patch.dict(Client.session.adapters, {"https://": adapter}), \
                patch.object(Client, "request", inspect.unwrap(Client.request)), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        out = stdout.getvalue()
        self.assertEqual(exit_info.exception.code, 0, stderr.getvalue() + out)
        paths = [path for path, _ in adapter.requests]
        self.assertEqual(paths.count("/UserService/DeviceLogin17"), 2)  # one login per account, never again
        self.assertEqual(set(paths), ALLOWED_PATHS)
        battles = [query for path, query in adapter.requests if path == "/BattleService/ListPvPBattles2"]
        self.assertEqual([query["take"] for query in battles], [["25"], ["25"]])
        written = "\n".join(path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file())
        forbidden_values = (*PLANTED, *REAL_REFRESH.values(), *REAL_TOKENS.values(), *REAL_ENV.values())
        combined = "\n".join((written, out, stderr.getvalue(), log.getvalue()))
        self.assertEqual([value for value in forbidden_values if value in combined], [])
        self.assertEqual(profile_tests.scan_tree(output), [])
        self.assertEqual(len(list(output.glob("*/*.xml"))), 2 * len(CAPTURE_READS))
        self.assertFalse((directory / ".device").exists())




class SettingsFieldsTests(unittest.TestCase):
    """The server settings summary names fields and keeps only version-like values."""

    # Verifies: S03-R1, S03-R2
    def test_summary_keeps_version_values_and_hides_everything_else(self):
        from datetime import datetime, timezone
        from sdk.state.capture import scan_capture_text, settings_fields
        secret_like = "A" * 48
        settings = {"SettingService": {"GetLatestSetting": {"Setting": {
            "@LatestVersion": "0.999.60", "@ProductionServer": "api.example.invalid",
            "@FileHash": secret_like, "@MinimumClientVersion": "0.999.55"}}}}
        result = settings_fields("pss1", settings, None, lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertEqual((result.status, result.scan), ("captured", "clean"))
        self.assertIn('"LatestVersion": "0.999.60"', result.text)
        self.assertIn('"MinimumClientVersion": "0.999.55"', result.text)
        self.assertIn('"FileHash": "<48 characters>"', result.text)
        self.assertNotIn("@", result.text)
        self.assertNotIn(secret_like, result.text)
        self.assertNotIn("api.example.invalid", result.text)
        self.assertEqual(scan_capture_text(result.text), [])

    def test_no_settings_is_no_data(self):
        from datetime import datetime, timezone
        from sdk.state.capture import settings_fields
        result = settings_fields("pss1", None, None, lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertEqual(result.status, "no data")


class InstanceIdTests(unittest.TestCase):
    """Ids that belong to one account become placeholders; game-data ids stay (second G1 capture review)."""

    def sanitize(self, xml):
        from sdk.state.capture import Placeholders, sanitize_capture
        return sanitize_capture(xml, Placeholders(), path="t.xml")

    # Verifies: S03-R1
    def test_instance_kinds(self):
        from sdk.state.capture import instance_kind
        cases = {"@ShipId": "SHIPID", "@AttackingShipId": "SHIPID", "@OwnerShipId": "SHIPID",
                 "@DefendingAllianceId": "ALLIANCEID", "@BattleId": "BATTLEID", "@ItemIds": "ITEMID",
                 "@CharacterActionId": "CHARACTERACTIONID", "@RoomActionId": "ROOMACTIONID",
                 "@UserStarSystemId": "USERSTARSYSTEMID", "@UserSeasonId": "USERSEASONID",
                 "@ShipDesignId": None, "@AttackingAllianceSpriteId": None, "@ConditionTypeId": None,
                 "@OriginalRaceId": None, "@BackgroundId": None, "@FromStarSystemId": None,
                 "@ExploredStarSystemIds": None, "@CompletedMissionEventIds": None, "@Id": None,
                 "@AttackingUserId": None, "@IsPaid": None, "@Trophy": None}
        for name, kind in cases.items():
            with self.subTest(name=name):
                self.assertEqual(instance_kind(name), kind)

    # Verifies: S03-R1, S03-R2
    def test_ship_battle_and_crew_values_are_replaced_and_cross_references_line_up(self):
        capture = self.sanitize(
            '<R><Ship ShipId="2708491" ShipDesignId="149" StarSystemId="11"/>'
            '<Character CharacterId="16681254" CharacterName="Hulk" OwnerShipId="2708491" RoomId="0" '
            'ItemIds="231455761,0" CharacterDesignId="20"/>'
            '<Battle BattleId="311497" AttackingShipId="2708491" DefendingShipId="9069446" '
            'AttackingAllianceId="12008" DefendingAllianceId="0" BackgroundId="13"/></R>')
        text = capture.text
        for value in ("2708491", "9069446", "16681254", "Hulk", "231455761", "311497", "12008"):
            self.assertNotIn(value, text)
        self.assertIn('ShipId="SHIPID-A"', text)
        self.assertIn('OwnerShipId="SHIPID-A"', text)
        self.assertIn('AttackingShipId="SHIPID-A"', text)
        self.assertIn('DefendingShipId="SHIPID-B"', text)
        self.assertIn('CharacterName="CREW-A"', text)
        self.assertIn('ItemIds="ITEMID-A,0"', text)
        self.assertIn('BattleId="BATTLEID-A"', text)
        self.assertIn('AttackingAllianceId="ALLIANCEID-A"', text)
        self.assertIn('DefendingAllianceId="0"', text)
        self.assertIn('RoomId="0"', text)
        for kept in ('ShipDesignId="149"', 'StarSystemId="11"', 'CharacterDesignId="20"', 'BackgroundId="13"'):
            self.assertIn(kept, text)
        self.assertTrue(any(step.startswith("7: instance ids") for step in capture.steps), capture.steps)
        self.assertTrue(capture.clean)


class SettingsSummaryPathTests(unittest.TestCase):
    """The settings summary of the second G1 capture was withheld: slash-joined paths looked like base64."""

    # Verifies: S03-R1, S03-R2
    def test_long_paths_and_names_do_not_trip_the_scan(self):
        from datetime import datetime, timezone
        from sdk.state.capture import settings_fields
        long_name = "VeryLongSettingNameThatLooksLikeBase64ToTheScan"
        settings = {"SettingService": {"GetLatestVersion": {"Setting": {
            "@LatestVersion": "0.999.60", "@MaintenanceMessage": "x"},
            "SettingsCollectionForExtendedClientVersions": {"@MinimumClientVersion": "0.999.55",
                                                            "@" + long_name: "1"}}}}
        result = settings_fields("pss1", settings, None, lambda: datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertEqual((result.status, result.scan), ("captured", "clean"), result.reason)
        self.assertIn('"SettingService.GetLatestVersion.Setting"', result.text)
        self.assertIn('"LatestVersion": "0.999.60"', result.text)
        self.assertIn('"MinimumClientVersion": "0.999.55"', result.text)
        self.assertIn(f'"<name of {len(long_name)} characters>"', result.text)
        self.assertNotIn(long_name, result.text)


if __name__ == "__main__":
    unittest.main()
