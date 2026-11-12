"""Tests for run.py --fleet-dry-run with fake clients. No network traffic, no real credentials.

Exercises the flags --fleet-dry-run, --registry, --concurrent, --fleet-journal-dir and
--identity-salt-env, and sdk.fleet.factory (credential convention, EphemeralDevice, design
reads). Every client is a fake; sockets are patched to raise.
"""

import inspect
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, quote_plus, urlsplit

import requests
from requests.adapters import BaseAdapter

import run  # imported here so run.py's log file opens in the repository, not a test tmpdir
from sdk.fleet.accounts import AccountProfile
from sdk.fleet.concurrent import ConcurrentFleetOrchestrator
from sdk.fleet.factory import (
    EMAIL_FORM_NOTE,
    AccountOpenError,
    CredentialError,
    LogScrubber,
    scrub,
    EnvironmentAccountFactory,
    EphemeralDevice,
    isolate_session,
    read_design_data,
    resolve_credentials,
)
from sdk.fleet.orchestrator import FleetOrchestrator
from sdk.journal import read_events
from sdk.state.snapshot import game_identity_fingerprint
from tests.synthetic_fixtures import SYNTHETIC_DEVICE_KEY_IOS, SYNTHETIC_DEVICE_KEY_MAC, SYNTHETIC_EMAIL

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
INVENTORY = json.loads((REPO_ROOT / "docs/baseline/capability-inventory.json").read_text(encoding="utf-8"))
MUTATING = {entry["name"] for entry in INVENTORY["client_methods"] if entry["kind"] == "mutating"}
SESSION = {entry["name"] for entry in INVENTORY["client_methods"] if entry["kind"] == "session"}
READS = {"listAllDesigns4", "listRoomDesigns2", "listAllResearchDesigns2", "getShipByUserId",
         "listAllCharactersOfUser", "listItemsOfAShip"}

ALPHA_REFRESH = "synthetic-refresh-alpha-0001"
ALPHA_AUTH = f"fleet|{SYNTHETIC_DEVICE_KEY_IOS}|{ALPHA_REFRESH}|en"
BRAVO_EMAIL = SYNTHETIC_EMAIL
BRAVO_PASSWORD = "synthetic-password-bravo-0002"
SALT = "synthetic-fleet-salt-0001"
USER_IDS = {ALPHA_REFRESH: "1001", BRAVO_EMAIL: "1002"}

REGISTRY = {"version": 1, "accounts": [
    {"alias": "alpha", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "ALPHA_AUTH"},
    {"alias": "bravo", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "BRAVO"},
    {"alias": "charlie", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "CHARLIE_AUTH",
     "enabled": False},
]}
ENV = {"ALPHA_AUTH": ALPHA_AUTH, "BRAVO_EMAIL": BRAVO_EMAIL, "BRAVO_PASSWORD": BRAVO_PASSWORD}
SECRET_VALUES = (ALPHA_AUTH, ALPHA_REFRESH, BRAVO_EMAIL, BRAVO_PASSWORD, SALT)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeClient:
    """Logs in and serves read-only payloads; any mutating call fails the test."""

    instances = []
    login_error = False  # True: login raises an error that repeats the credentials
    designs4_ok = True
    lock = threading.Lock()

    def __init__(self, device=None, settings=None):
        self.device = device
        self.settings = settings
        self.calls = []
        with FakeClient.lock:
            FakeClient.instances.append(self)

    def __getattr__(self, name):
        if name in MUTATING:
            raise AssertionError(f"fleet dry run called mutating Client.{name}")
        raise AttributeError(name)

    def login(self, email=None, password=None):
        self.calls.append("login")
        if FakeClient.login_error:
            # Echo this account's own credential values, as a careless library error might.
            raise RuntimeError(f"login rejected for {email} with {password} / {self.device.authentication_string}")
        key = email if email else self.device.refreshToken
        self.user = FakeUser(USER_IDS.get(key, "1001"))
        return True

    def listAllDesigns4(self):
        self.calls.append("listAllDesigns4")
        if not FakeClient.designs4_ok:
            return False
        self.shipDesigns = {"ShipDesign": [
            {"@ShipDesignId": "1", "@ShipLevel": "1", "@MineralCost": "0"},
            {"@ShipDesignId": "2", "@ShipLevel": "2", "@MineralCost": "5000"},
        ]}
        self.roomDesigns = {"stale": "overwritten by listRoomDesigns2"}
        return True

    def listRoomDesigns2(self):
        self.calls.append("listRoomDesigns2")
        self.roomDesigns = {"RoomDesigns": {"RoomDesign": [
            {"@RoomDesignId": "100", "@RoomName": "Engine"},
            {"@RoomDesignId": "101", "@RoomName": "Engine II", "@UpgradeFromRoomDesignId": "100",
             "@PriceString": "mineral:300"},
        ]}}

    def listAllResearchDesigns2(self):
        self.calls.append("listAllResearchDesigns2")
        self.allResearchDesigns = {"ResearchDesigns": {"ResearchDesign": [
            {"@ResearchDesignId": "510", "@RootResearchDesignId": "510", "@GasCost": "50"},
        ]}}

    def getShipByUserId(self):
        self.calls.append("getShipByUserId")
        self.shipByUserId = {"Ship": {"@ShipLevel": "5", "Rooms": {"Room": [
            {"@RoomId": "r1", "@RoomDesignId": "100", "@RoomStatus": "Normal"}]},
            "Researches": {"Research": {"@ResearchDesignId": "400", "@ResearchState": "Completed"}}}}
        return True

    def listAllCharactersOfUser(self):
        self.calls.append("listAllCharactersOfUser")
        self.allCharactersOfUser = {"Characters": {"Character": [{"@CharacterId": "c1", "@Level": "3"}]}}
        return True

    def listItemsOfAShip(self):
        self.calls.append("listItemsOfAShip")
        self.itemsOfAShip = {"Items": {"Item": [
            {"@ItemDesignId": "1", "@ItemType": "Mineral", "@Quantity": "1000"},
            {"@ItemDesignId": "2", "@ItemType": "Gas", "@Quantity": "1000"},
        ]}}
        return True


def forbidden(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"fleet dry run touched {name}")
    return _raise


class FleetDryRunCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.journal_dir = self.dir / "journals"
        self.registry = self.dir / "fleet.json"
        self.registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        FakeClient.instances = []
        FakeClient.login_error = False
        FakeClient.designs4_ok = True
        self.execute_values = []
        # Every fleet run must be a dry run: record each orchestrator run's execute flag.
        for cls in (FleetOrchestrator, ConcurrentFleetOrchestrator):
            original = cls.__dict__["run"]

            def spy(orchestrator, *, execute=False, _original=original):
                self.execute_values.append(execute)
                return _original(orchestrator, execute=execute)
            patcher = patch.object(cls, "run", spy)
            patcher.start()
            self.addCleanup(patcher.stop)
        # EphemeralDevice must never touch ./.device: run from an empty working directory.
        cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)

    def run_main(self, *args, env=ENV, journal_dir=True):
        argv = ["run.py", "--fleet-dry-run", "--registry", str(self.registry), *args]
        if journal_dir:
            argv += ["--fleet-journal-dir", str(self.journal_dir)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch.dict(os.environ, env, clear=True), \
                patch("run.Client", FakeClient), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch("getpass.getpass", side_effect=forbidden("getpass")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            from run import main
            main()
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue()

    def row(self, output, alias):
        return next(line for line in output.splitlines() if line.startswith(f"| {alias} |"))

    def all_text(self, output, error):
        journals = "".join(path.read_text(encoding="utf-8") for path in self.journal_dir.glob("*"))
        return output + error + journals

    def assert_dry_run_only(self):
        self.assertTrue(self.execute_values)
        self.assertEqual(set(self.execute_values), {False})
        for client in FakeClient.instances:
            self.assertEqual([name for name in client.calls if name in MUTATING or
                              (name in SESSION and name != "login")], [])
            self.assertLessEqual(set(client.calls) - {"login"}, READS)

    def assert_per_alias_journals(self):
        self.assertEqual(sorted(path.name for path in self.journal_dir.iterdir()), ["alpha.log", "bravo.log"])
        for alias in ("alpha", "bravo"):
            events = read_events(self.journal_dir / f"{alias}.log")
            self.assertTrue(events)
            self.assertTrue(all(event.account_alias == alias for event in events))
            self.assertTrue(all(event.dry_run and not event.attempted for event in events))

    def test_sequential_dry_run_journals_each_alias_and_prints_reports(self):
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("# Fleet dry run (sequential)", out)
        self.assertIn("**Mode:** sequential (dry run)", out)
        self.assertIn("## Alerts", out)
        self.assertRegex(self.row(out, "alpha"), r"\| alpha \| completed \| 4 \| n/a \| alpha\.log \|")
        self.assertRegex(self.row(out, "bravo"), r"\| bravo \| completed \| 4 \| n/a \| bravo\.log \|")
        self.assertIn("| charlie | disabled |", out)
        self.assertEqual(len(FakeClient.instances), 2)
        self.assert_dry_run_only()
        self.assert_per_alias_journals()
        self.assertFalse((self.dir / ".device").exists())

    def test_concurrent_dry_run_matches_sequential(self):
        code, out, err = self.run_main("--concurrent", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("# Fleet dry run (concurrent)", out)
        self.assertIn("**Mode:** concurrent (dry run)", out)
        self.assertIn("| alpha | completed | 4 |", out)
        self.assertIn("| bravo | completed | 4 |", out)
        self.assert_dry_run_only()
        self.assert_per_alias_journals()

    def test_clients_get_their_own_credentials_and_settings(self):
        self.run_main()
        by_refresh = {client.device.refreshToken: client for client in FakeClient.instances}
        alpha = by_refresh[ALPHA_REFRESH]
        self.assertIsInstance(alpha.device, EphemeralDevice)
        self.assertEqual(alpha.device.key, SYNTHETIC_DEVICE_KEY_IOS)
        self.assertNotIn("allow_email_password_login", alpha.settings)
        bravo = by_refresh[None]
        self.assertTrue(bravo.settings["allow_email_password_login"])
        self.assertEqual(bravo.settings["checksum_key"], "5343")
        self.assertNotEqual(bravo.device.key, alpha.device.key)

    def test_missing_secret_errors_that_account_and_others_continue(self):
        for mode in ((), ("--concurrent", "2")):
            with self.subTest(mode=mode):
                FakeClient.instances = []
                code, out, err = self.run_main(*mode, env={"ALPHA_AUTH": ALPHA_AUTH})
                self.assertEqual(code, 0, err)
                self.assertIn("| alpha | completed | 4 |", out)
                bravo = self.row(out, "bravo")
                self.assertIn("| bravo | error | 0 |", bravo)
                self.assertIn("CredentialError: no credentials in the environment: set BRAVO (auth string) "
                              "or BRAVO_EMAIL and BRAVO_PASSWORD", bravo)
                self.assertEqual(len(FakeClient.instances), 1)

    def test_partial_and_ambiguous_secrets_error_without_values(self):
        code, out, _ = self.run_main(env={"ALPHA_AUTH": ALPHA_AUTH, "ALPHA_AUTH_PASSWORD": BRAVO_PASSWORD,
                                          "BRAVO_EMAIL": BRAVO_EMAIL})
        self.assertEqual(code, 0)
        self.assertIn("both ALPHA_AUTH and ALPHA_AUTH_EMAIL/ALPHA_AUTH_PASSWORD are set", self.row(out, "alpha"))
        self.assertIn("BRAVO_PASSWORD is not set", self.row(out, "bravo"))
        for value in SECRET_VALUES:
            self.assertNotIn(value, out)

    def test_credentials_and_salt_never_reach_output_or_journals(self):
        code, out, err = self.run_main("--identity-salt-env", "FLEET_SALT", env={**ENV, "FLEET_SALT": SALT})
        self.assertEqual(code, 0, err)
        for alias, user_id in (("alpha", "1001"), ("bravo", "1002")):
            self.assertIn(game_identity_fingerprint(user_id, SALT), self.row(out, alias))
        text = self.all_text(out, err)
        for value in SECRET_VALUES:
            self.assertNotIn(value, text)

    def test_login_exception_text_is_scrubbed_of_credentials(self):
        FakeClient.login_error = True
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        for alias in ("alpha", "bravo"):
            self.assertIn(f"| {alias} | error | 0 |", out)
        self.assertIn("AccountOpenError: RuntimeError: login rejected for None with None / ***REDACTED***",
                      self.row(out, "alpha"))
        self.assertIn("login rejected for ***REDACTED*** with ***REDACTED*** / None", self.row(out, "bravo"))
        for value in SECRET_VALUES:
            self.assertNotIn(value, self.all_text(out, err))

    def test_failed_login_is_an_account_error(self):
        with patch.object(FakeClient, "login", lambda self, email=None, password=None: False):
            code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("| alpha | error | 0 | n/a | n/a | LoginError: login failed |", out)

    def test_same_game_account_for_two_aliases_is_refused(self):
        env = {**ENV, "FLEET_SALT": SALT}
        with patch.dict(USER_IDS, {BRAVO_EMAIL: "1001"}):
            code, out, _ = self.run_main("--identity-salt-env", "FLEET_SALT", env=env)
        self.assertEqual(code, 0)
        self.assertIn("| alpha | completed |", out)
        self.assertIn("game identity already observed for 'alpha'", self.row(out, "bravo"))

    def test_hull_designs_unavailable_is_noted(self):
        FakeClient.designs4_ok = False
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("hull designs unavailable (listAllDesigns4 returned no data)", self.row(out, "alpha"))
        self.assertIn("| alpha | completed | 4 |", out)

    def test_default_journal_dir_is_local_and_gitignored(self):
        code, _, err = self.run_main(journal_dir=False)
        self.assertEqual(code, 0, err)
        self.assertTrue((self.dir / run.FLEET_JOURNAL_DIR_DEFAULT / "alpha.log").is_file())
        self.assertIn("*.log", (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split())

    def test_identity_salt_errors_exit_2_without_echoing_the_salt(self):
        code, _, err = self.run_main("--identity-salt-env", "FLEET_SALT")
        self.assertEqual(code, 2)
        self.assertIn("identity salt variable FLEET_SALT is not set", err)
        code, out, err = self.run_main("--identity-salt-env", "FLEET_SALT", env={**ENV, "FLEET_SALT": "short-salt"})
        self.assertEqual(code, 2)
        self.assertIn("identity salt must be at least", err)
        self.assertNotIn("short-salt", out + err)
        self.assertEqual(FakeClient.instances, [])

    def test_registry_errors_exit_2(self):
        self.registry.unlink()
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("registry not found", err)
        self.registry.write_text(json.dumps({"version": 1, "accounts": [{"alias": "Bad Alias"}]}), encoding="utf-8")
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertEqual(FakeClient.instances, [])

    def test_flag_validation(self):
        cases = [
            ((), "--fleet-dry-run needs --registry PATH"),
            (("--registry", "x.json", "--login-email", SYNTHETIC_EMAIL),
             "--fleet-dry-run cannot be combined with --login-email"),
            (("--registry", "x.json", "--report", "fleet", "--journal", "a.log"),
             "--fleet-dry-run cannot be combined with --report, --journal"),
            (("--registry", "x.json", "--concurrent", "0"), "must be a positive integer"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                stderr = io.StringIO()
                with patch.object(sys, "argv", ["run.py", "--fleet-dry-run", *args]), \
                        patch("run.Client", side_effect=forbidden("run.Client")), \
                        redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
                    from run import main
                    main()
                self.assertEqual(exit_info.exception.code, 2)
                self.assertIn(message, stderr.getvalue())
        for flag in (("--concurrent", "2"), ("--fleet-journal-dir", "d"), ("--identity-salt-env", "S")):
            with self.subTest(flag=flag):
                stderr = io.StringIO()
                with patch.object(sys, "argv", ["run.py", *flag]), \
                        patch("run.Client", side_effect=forbidden("run.Client")), \
                        redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
                    from run import main
                    main()
                self.assertEqual(exit_info.exception.code, 2)
                self.assertIn("only used with --fleet-dry-run", stderr.getvalue())


class FleetFactoryTests(unittest.TestCase):
    def test_resolve_credentials_forms(self):
        auth = resolve_credentials("ALPHA_AUTH", {"ALPHA_AUTH": f" {ALPHA_AUTH}\n"})
        self.assertEqual(auth.auth_string, ALPHA_AUTH)
        pair = resolve_credentials("BRAVO", ENV)
        self.assertEqual((pair.email, pair.password), (BRAVO_EMAIL, BRAVO_PASSWORD))
        for credentials in (auth, pair):
            for value in SECRET_VALUES:
                self.assertNotIn(value, repr(credentials) + str(credentials))
        with self.assertRaises(CredentialError):
            resolve_credentials("BRAVO", {"BRAVO_EMAIL": BRAVO_EMAIL, "BRAVO_PASSWORD": "  "})

    def test_ephemeral_device_never_touches_the_device_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                Path(".device").write_text(f"Android|{SYNTHETIC_DEVICE_KEY_MAC}|other-account-refresh|en",
                                           encoding="utf-8")
                fresh = EphemeralDevice(language="en")
                self.assertIsNone(fresh.refreshToken)
                self.assertNotEqual(fresh.key, SYNTHETIC_DEVICE_KEY_MAC)
                parsed = EphemeralDevice(language="en", authentication_string=ALPHA_AUTH)
                self.assertEqual((parsed.key, parsed.refreshToken), (SYNTHETIC_DEVICE_KEY_IOS, ALPHA_REFRESH))
                parsed.refreshTokenAcquire("synthetic-refresh-new-0003")
                parsed.reset()
                self.assertIn("other-account-refresh", Path(".device").read_text(encoding="utf-8"))
                Path(".device").unlink()
                EphemeralDevice(language="en", authentication_string=ALPHA_AUTH).refreshTokenAcquire("x" * 8)
                self.assertFalse(Path(".device").exists())
            finally:
                os.chdir(cwd)

    def test_read_design_data_passes_hull_designs(self):
        client = FakeClient()
        client.latestVersion = {}
        design, notes = read_design_data(client)
        self.assertEqual(notes, ())
        self.assertEqual([(hull.design_id, hull.level, hull.mineral_cost) for hull in design.hulls],
                         [("1", 1, 0), ("2", 2, 5000)])
        self.assertEqual([room.design_id for room in design.rooms], ["100", "101"])
        self.assertEqual([research.design_id for research in design.research], ["510"])
        self.assertEqual(client.calls, ["listAllDesigns4", "listRoomDesigns2", "listAllResearchDesigns2"])

    def test_read_design_data_survives_a_raising_listAllDesigns4(self):
        client = FakeClient()
        with patch.object(FakeClient, "listAllDesigns4", side_effect=KeyError("DesignService"), create=True):
            design, notes = read_design_data(client)
        self.assertEqual(design.hulls, ())
        self.assertEqual(notes, ("hull designs unavailable (listAllDesigns4 raised KeyError)",))
        self.assertEqual(len(design.rooms), 2)

    def test_factory_context_is_account_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = EnvironmentAccountFactory(tmp, client_cls=FakeClient, environ=ENV, identity_salt=SALT)
            profile = AccountProfile(alias="bravo", objective="grow", policy_id="baseline-v1",
                                     credentials_secret="BRAVO")
            context = factory(profile)
            self.assertEqual(Path(context.journal.path), Path(tmp) / "bravo.log")
            snapshot = context.snapshot_provider()
            self.assertEqual(snapshot.account_alias, "bravo")
            self.assertEqual(snapshot.game_identity, game_identity_fingerprint("1002", SALT))
            self.assertEqual(len(context.design.hulls), 2)
            self.assertEqual(factory.notes(), {"bravo": (EMAIL_FORM_NOTE,)})

    def test_isolate_session_replaces_the_shared_class_session(self):
        import requests

        class SharedSessionClient:
            session = requests.Session()

        client = SharedSessionClient()
        isolate_session(client)
        self.assertIsNot(client.session, SharedSessionClient.session)
        self.assertIs(client.session.adapters["https://"], SharedSessionClient.session.adapters["https://"])
        own = client.session
        isolate_session(client)
        self.assertIs(client.session, own)
        isolate_session(FakeClient())  # no session attribute: left alone


SPECIAL_EMAIL = "synthetic.user@example.invalid"
SPECIAL_PASSWORD = "p@ss w+rd/%"


def encoded_forms(value):
    """Raw, percent-encoded (both hex cases) and form-encoded spellings of ``value``."""
    forms = {value, quote(value, safe=""), quote_plus(value)}
    return forms | {form.lower() for form in forms if "%" in form} | {form.upper() for form in forms if "%" in form}


def urllib3_error(url):
    parts = urlsplit(url)
    return requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host={parts.hostname!r}, port=443): Max retries exceeded with url: "
        f"{parts.path}?{parts.query} (Caused by NewConnectionError: timed out)")


class ScrubTests(unittest.TestCase):
    def test_scrub_removes_encoded_credentials_from_urllib3_error_text(self):
        from sdk.client import lowercase_urlencode

        query = lowercase_urlencode({"clientDateTime": "2026-11-11T12:24:45", "deviceKey": SYNTHETIC_DEVICE_KEY_IOS,
                                     "email": SPECIAL_EMAIL, "password": SPECIAL_PASSWORD, "userId": "700000101"})
        error = urllib3_error(f"https://api.pixelstarships.com/UserService/UserEmailPasswordAuthorize4?{query}")
        for secrets in ((SPECIAL_EMAIL, SPECIAL_PASSWORD), ()):
            with self.subTest(secrets=secrets):
                text = scrub(str(error), secrets)
                for value in (SPECIAL_EMAIL, SPECIAL_PASSWORD, "700000101", SYNTHETIC_DEVICE_KEY_IOS):
                    for form in encoded_forms(value):
                        self.assertNotIn(form, text)
                self.assertIn("/UserService/UserEmailPasswordAuthorize4?***REDACTED***", text)

    def test_scrub_replaces_encoded_values_outside_urls_and_raw_ids(self):
        text = scrub(f"email={quote_plus(SPECIAL_EMAIL)} pw {quote(SPECIAL_PASSWORD, safe='').lower()} "
                     f"{{'@UserId': '700000101'}} userId=700000202", (SPECIAL_PASSWORD,))
        for value in (SPECIAL_EMAIL, SPECIAL_PASSWORD, "700000101", "700000202"):
            for form in encoded_forms(value):
                self.assertNotIn(form, text)

    def test_short_secrets_are_not_replaced_literally(self):
        text = "2 events at 2026-11-11T12:00:01 for abc"
        self.assertEqual(scrub(text, ("1", "ab", "abc")), text)
        self.assertEqual(scrub("password=abc&x=1 and ?password=abc&user=1", ("abc",)),
                         "password=***REDACTED***&x=1 and ?***REDACTED***")
        scrubber = LogScrubber()
        scrubber.add_secrets(["1"])
        scrubber.add_names(["Bob"])
        self.assertEqual(scrubber.scrub("Bob has 1 ship"), "Bob has 1 ship")

    def test_ids_and_names_in_dumped_payloads_are_redacted(self):
        parsed = "{'User': {'@Id': '700000101', '@Name': 'SyntheticCaptain101', '@UserId': '700000101'}}"
        raw = '<UserLogin UserId="700000101"><User Id="700000101" Name="SyntheticCaptain101" RoomName="Bridge"/>'
        for text in (parsed, raw, scrub(parsed, ()), json.dumps({"@Id": "700000101", "@Name": "SyntheticCaptain101"})):
            with self.subTest(text=text):
                scrubbed = scrub(text, ())
                self.assertNotIn("700000101", scrubbed)
                self.assertNotIn("SyntheticCaptain101", scrubbed)
        self.assertIn('RoomName="Bridge"', scrub(raw, ()))
        record = logging.LogRecord("root", logging.ERROR, str(REPO_ROOT / "sdk" / "client.py"), 1,
                                   "{%s} - {%s}", ("https://x/DeviceLogin17", parsed), None)
        LogScrubber().filter(record)
        self.assertNotIn("700000101", record.getMessage())
        self.assertNotIn("SyntheticCaptain101", record.getMessage())

    def test_auth_string_fields_are_registered_individually(self):
        access, user_id = "dddddddd-1111-2222-3333-444444444444", "700000101"
        auth = f"fleet|{SYNTHETIC_DEVICE_KEY_IOS}|{ALPHA_REFRESH}|en|{access}|{user_id}"
        values = resolve_credentials("ALPHA_AUTH", {"ALPHA_AUTH": auth}).values()
        for part in (auth, SYNTHETIC_DEVICE_KEY_IOS, ALPHA_REFRESH, access, user_id):
            self.assertIn(part, values)
        self.assertNotIn("fleet", values)
        self.assertNotIn("en", values)
        bare = f"device {SYNTHETIC_DEVICE_KEY_IOS} refresh {ALPHA_REFRESH} token {access} id {user_id}"
        scrubbed = scrub(bare, values)
        for part in (SYNTHETIC_DEVICE_KEY_IOS, ALPHA_REFRESH, access, user_id):
            self.assertNotIn(part, scrubbed)

        def raising_login(client, email=None, password=None):
            raise RuntimeError(f"refresh {client.device.refreshToken} rejected for device {client.device.key}")

        profile = AccountProfile(alias="alpha", objective="grow", policy_id="baseline-v1",
                                 credentials_secret="ALPHA_AUTH")
        scrubber = LogScrubber()
        with tempfile.TemporaryDirectory() as tmp, patch.object(FakeClient, "login", raising_login):
            with self.assertRaises(AccountOpenError) as raised:
                EnvironmentAccountFactory(tmp, client_cls=FakeClient, environ={"ALPHA_AUTH": auth},
                                          log_scrubber=scrubber)(profile)
        for part in (SYNTHETIC_DEVICE_KEY_IOS, ALPHA_REFRESH):
            self.assertNotIn(part, str(raised.exception))
            self.assertNotIn(part, scrubber.scrub(f"log line with {part}"))

    def test_factory_scrubs_a_connection_error_raised_during_login(self):
        from sdk.client import lowercase_urlencode

        def raising_login(client, email=None, password=None):
            query = lowercase_urlencode({"email": email, "password": password})
            raise urllib3_error(f"https://api.pixelstarships.com/UserService/UserEmailPasswordAuthorize4?{query}")

        env = {"BRAVO_EMAIL": SPECIAL_EMAIL, "BRAVO_PASSWORD": SPECIAL_PASSWORD}
        profile = AccountProfile(alias="bravo", objective="grow", policy_id="baseline-v1", credentials_secret="BRAVO")
        with tempfile.TemporaryDirectory() as tmp, patch.object(FakeClient, "login", raising_login):
            with self.assertRaises(AccountOpenError) as raised:
                EnvironmentAccountFactory(tmp, client_cls=FakeClient, environ=env)(profile)
        text = str(raised.exception)
        self.assertIn("ConnectionError: HTTPSConnectionPool(host='api.pixelstarships.com'", text)
        for value in (SPECIAL_EMAIL, SPECIAL_PASSWORD):
            for form in encoded_forms(value):
                self.assertNotIn(form, text)

    def test_log_scrubber_drops_client_chatter_and_scrubs_the_rest(self):
        scrubber = LogScrubber()
        scrubber.add_secrets([SPECIAL_PASSWORD])
        scrubber.add_names(["SyntheticCaptain101", "ab"])
        client_file = str(REPO_ROOT / "sdk" / "client.py")

        def record(level, msg, *args, pathname=client_file):
            return logging.LogRecord("root", level, pathname, 1, msg, args, None)

        self.assertFalse(scrubber.filter(record(logging.INFO, "[%s] Authenticated...", "SyntheticCaptain101")))
        error = record(logging.ERROR, "[%s] {%s} - %s", "Someone Else",
                       f"https://x/S?userId=700000101&password={quote(SPECIAL_PASSWORD, safe='')}", "done")
        self.assertTrue(scrubber.filter(error))
        self.assertEqual(error.getMessage(), "[account] {https://x/S?***REDACTED***} - done")
        label = record(logging.ERROR, "[authorize_email_password] failed: %s", "SyntheticCaptain101")
        scrubber.filter(label)
        self.assertEqual(label.getMessage(), "[authorize_email_password] failed: ***REDACTED***")
        other = record(logging.INFO, "user %s said ab", SPECIAL_PASSWORD, pathname="/elsewhere/run.py")
        self.assertTrue(scrubber.filter(other))
        self.assertEqual(other.getMessage(), "user ***REDACTED*** said ab")

        handler = logging.StreamHandler(io.StringIO())
        logger = logging.getLogger("tachikoma.test.scrubber")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with scrubber.installed(logger):
            self.assertIn(scrubber, handler.filters)
        self.assertNotIn(scrubber, handler.filters)


# --- Real Client, fake HTTP adapter -------------------------------------------------------------

REAL_REFRESH = {"alpha": "RT-alpha-synthetic-refresh-0001", "bravo": "RT-bravo-synthetic-refresh-0002"}
REAL_IDS = {REAL_REFRESH["alpha"]: "700000101", REAL_REFRESH["bravo"]: "700000202", "": "700000909"}
REAL_TOKENS = {"700000101": "aaaaaaaa-1111-2222-3333-444444444444",
               "700000202": "bbbbbbbb-1111-2222-3333-444444444444",
               "700000909": "cccccccc-1111-2222-3333-444444444444"}
REAL_NAMES = {uid: f"SyntheticCaptain{uid[-3:]}" for uid in REAL_TOKENS}
REAL_REGISTRY = {"version": 1, "accounts": [
    {"alias": alias, "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": f"SYNTH_{alias.upper()}"}
    for alias in ("alpha", "bravo", "charlie")]}
REAL_ENV = {
    "SYNTH_ALPHA": f"fleet|{SYNTHETIC_DEVICE_KEY_IOS}|{REAL_REFRESH['alpha']}|en",
    "SYNTH_BRAVO": f"fleet|{SYNTHETIC_DEVICE_KEY_MAC}|{REAL_REFRESH['bravo']}|en",
    "SYNTH_CHARLIE_EMAIL": SPECIAL_EMAIL,
    "SYNTH_CHARLIE_PASSWORD": SPECIAL_PASSWORD,
    "SYNTH_SALT": SALT,
}
MUTATING_PATHS = ("Collect", "Upgrade", "AddResearch", "Train", "Battle", "Finalise", "Buy", "Speed", "Rebuild",
                  "Claim", "Accept")


def xml_response(text):
    response = requests.Response()
    response.status_code = 200
    response._content = text.encode()
    response.encoding = "utf-8"
    return response


class FakeGameAdapter(BaseAdapter):
    """Answers the real Client's requests in memory. ``raise_on`` paths raise a urllib3-style ConnectionError."""

    def __init__(self, raise_on=()):
        super().__init__()
        self.raise_on = raise_on
        self.paths = []
        self.lock = threading.Lock()

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        with self.lock:
            self.paths.append(parts.path)
        if any(word in parts.path for word in MUTATING_PATHS):
            raise AssertionError(f"fleet dry run sent mutating request {parts.path}")
        if any(parts.path.endswith(path) for path in self.raise_on):
            raise urllib3_error(request.url)
        if parts.path.endswith("DeviceLogin17"):
            uid = REAL_IDS[json.loads(request.body).get("RefreshToken") or ""]
            return xml_response(f'<UserService><UserLogin UserId="{uid}" accessToken="{REAL_TOKENS[uid]}">'
                                f'<User Id="{uid}" Name="{REAL_NAMES[uid]}"/></UserLogin></UserService>')
        if parts.path.endswith("UserEmailPasswordAuthorize4"):
            return xml_response('<UserService errorMessage="Email or password is incorrect"/>')
        if parts.path.endswith("GetLatestVersion3"):
            return xml_response('<SettingService><GetLatestSetting><Setting RoomDesignVersion="1" '
                                'ResearchDesignVersion="1"/></GetLatestSetting></SettingService>')
        if parts.path.endswith("GetShipByUserId"):
            return xml_response('<ShipService errorMessage="Ship temporarily unavailable"/>')
        return xml_response("<Empty/>")

    def close(self):
        pass


class RealClientLeakTests(unittest.TestCase):
    """The real ``Client`` logs request URLs and player names; none of it may reach any output."""

    FORBIDDEN = tuple(sorted(
        set().union(*(encoded_forms(value) for value in (SPECIAL_EMAIL, SPECIAL_PASSWORD)))
        | set(REAL_REFRESH.values()) | {REAL_ENV["SYNTH_ALPHA"], REAL_ENV["SYNTH_BRAVO"], SALT}
        | set(REAL_TOKENS) | set(REAL_TOKENS.values()) | set(REAL_NAMES.values())))

    def run_fleet(self, *extra, raise_on=()):
        from sdk.client import Client

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        registry, journal_dir = directory / "fleet.json", directory / "journals"
        registry.write_text(json.dumps(REAL_REGISTRY), encoding="utf-8")
        adapter = FakeGameAdapter(raise_on)
        log = io.StringIO()
        capture = logging.StreamHandler(log)
        capture.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root = logging.getLogger()
        argv = ["run.py", "--fleet-dry-run", "--registry", str(registry), "--fleet-journal-dir", str(journal_dir),
                "--identity-salt-env", "SYNTH_SALT", *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, cwd)
        with patch.object(sys, "argv", argv), patch.dict(os.environ, REAL_ENV, clear=True), \
                patch.object(root, "handlers", [capture]), \
                patch.dict(Client.session.adapters, {"https://": adapter}), \
                patch.object(Client, "request", inspect.unwrap(Client.request)), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        self.assertEqual(capture.filters, [], "the log scrubber must be removed after the run")
        journals = "".join(path.read_text(encoding="utf-8") for path in journal_dir.glob("*"))
        self.assertFalse((directory / ".device").exists())
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue(), log.getvalue(), journals, adapter

    def assert_clean(self, *texts):
        combined = "\n".join(texts)
        leaked = [value for value in self.FORBIDDEN if value in combined]
        self.assertEqual(leaked, [], combined)
        self.assertNotRegex(combined, r"(?i)user_?id['\"]?\s*[=:]\s*['\"]?\d")

    def test_no_secret_reaches_output_logs_or_journals(self):
        for mode in ((), ("--concurrent", "3")):
            for raise_on in ((), ("UserEmailPasswordAuthorize4", "GetShipByUserId")):
                with self.subTest(mode=mode, raise_on=raise_on):
                    code, out, err, log, journals, adapter = self.run_fleet(*mode, raise_on=raise_on)
                    self.assertEqual(code, 0, err)
                    self.assertIn("/UserService/UserEmailPasswordAuthorize4", adapter.paths)
                    self.assertIn("/ShipService/GetShipByUserId", adapter.paths)
                    self.assertIn("ERROR", log)  # the client did log, and the records were scrubbed
                    self.assertNotIn("Authenticated", log)
                    self.assertIn("| charlie | error | 0 |", out)
                    self.assertIn(EMAIL_FORM_NOTE, out)
                    self.assertIn(game_identity_fingerprint("700000101", SALT), out)
                    self.assert_clean(out, err, log, journals)


if __name__ == "__main__":
    unittest.main()
