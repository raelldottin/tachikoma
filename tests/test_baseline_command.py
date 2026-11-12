"""Tests for run.py --baseline with fake clients (SPEC-04, S04-R1, S04-R3, S04-R6).

Exercises the flags --baseline, --baseline-summary, --registry, --output, --concurrent and
--identity-salt-env, and sdk.fleet.factory.server_game_version / open_baseline. Every client is a
fake or the real Client behind an in-memory HTTP adapter; sockets are patched to raise. No real
credentials, ids or e-mails are used.
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
from unittest.mock import patch
from urllib.parse import urlsplit

import run  # imported here so run.py's log file opens in the repository, not a test tmpdir
from sdk.fleet.concurrent import ConcurrentFleetOrchestrator
from sdk.fleet.factory import (
    EnvironmentAccountFactory,
    LogScrubber,
    SERVER_VERSION_ATTRIBUTES,
    server_game_version,
)
from sdk.fleet.accounts import AccountProfile
from sdk.fleet.orchestrator import FleetOrchestrator
from sdk.scheduler import DailyScheduler
from sdk.state.baseline import load_baseline_file, scan_baseline_directory
from sdk.state.snapshot import game_identity_fingerprint
from tests import test_fleet_dry_run as fleet_tests  # a module import: its test cases are not rerun here
from tests.test_fleet_dry_run import (
    ENV,
    MUTATING,
    MUTATING_PATHS,
    REAL_ENV,
    REAL_REGISTRY,
    REGISTRY,
    SALT,
    SECRET_VALUES,
    SESSION,
    FakeClient,
    forbidden,
    xml_response,
)

OBSERVED = datetime(2026, 11, 11, 12, 30, 45, tzinfo=timezone.utc)
STAMP = "20261111T123045Z"
SERVER_VERSION = "0.999.60"
DECLARED_VERSION = "0.999.59"  # the ClientVersion sdk/client.py declares at login; never the baseline's version
SNAPSHOT_READS = {"getShipByUserId", "listAllCharactersOfUser", "listItemsOfAShip"}
BASELINE_KEYS = {"format", "format_version", "account_alias", "observed_at", "game_version", "client_version",
                 "game_identity", "source", "hull_level", "trophies", "resources", "crew", "rooms", "research",
                 "inventory", "completeness", "notes"}


def latest_setting(**attributes):
    """``Client.latestVersion`` as ``getLatestVersion3`` stores it (xmltodict output)."""
    setting = {"@RoomDesignVersion": "1", "@ResearchDesignVersion": "1", **attributes}
    return {"SettingService": {"GetLatestSetting": {"Setting": setting}}}


class BaselineClient(FakeClient):
    """``FakeClient`` whose login also reads the server's latest-version setting, as ``Client.login`` does."""

    latest = latest_setting(**{"@LatestVersion": SERVER_VERSION, "@MinimumClientVersion": "0.998.0"})
    touched = []  # every mutating or session attribute looked up on any instance

    def __getattr__(self, name):
        if name in MUTATING or name in SESSION:
            BaselineClient.touched.append(name)
        return super().__getattr__(name)

    def login(self, email=None, password=None):
        ok = super().login(email=email, password=password)
        if ok and BaselineClient.latest is not None:
            self.latestVersion = BaselineClient.latest
        return ok


class BaselineCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.output = self.dir / "baselines"
        self.registry = self.dir / "fleet.json"
        self.registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        FakeClient.instances = []
        FakeClient.login_error = False
        FakeClient.designs4_ok = True
        BaselineClient.touched = []
        BaselineClient.latest = latest_setting(**{"@LatestVersion": SERVER_VERSION,
                                                  "@MinimumClientVersion": "0.998.0"})
        # The baseline has no execute path: no orchestrator or scheduler may run at all.
        for cls, name in ((FleetOrchestrator, "run"), (ConcurrentFleetOrchestrator, "run"),
                          (DailyScheduler, "run")):
            patcher = patch.object(cls, name, forbidden(f"{cls.__name__}.{name}"))
            patcher.start()
            self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.dir)  # EphemeralDevice must never touch ./.device
        self.addCleanup(os.chdir, cwd)

    def run_main(self, *args, env=ENV, output=True, observed=OBSERVED):
        argv = ["run.py", "--baseline", "--registry", str(self.registry), *args]
        if output:
            argv += ["--output", str(self.output)]
        stdout, stderr = io.StringIO(), io.StringIO()
        clock = patch("run.run_baseline", partial(run.run_baseline, clock=lambda: observed))
        with patch.object(sys, "argv", argv), patch.dict(os.environ, env, clear=True), \
                patch("run.Client", BaselineClient), clock, \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch("getpass.getpass", side_effect=forbidden("getpass")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue()

    def row(self, output, alias, section="## Accounts"):
        lines = output.split(section, 1)[1].splitlines()
        return next(line for line in lines if line.startswith(f"| {alias} |"))

    def files(self):
        return sorted(path.name for path in self.output.iterdir()) if self.output.is_dir() else []

    def assert_read_only(self):
        self.assertEqual(BaselineClient.touched, [])
        self.assertTrue(FakeClient.instances)
        for client in FakeClient.instances:
            self.assertEqual(client.calls[0], "login")
            self.assertLessEqual(set(client.calls[1:]), SNAPSHOT_READS)  # no design reads either
            self.assertEqual(sorted(client.calls[1:]), sorted(SNAPSHOT_READS))  # exactly one snapshot

    # Verifies: S04-R1, S04-R3
    def test_writes_one_baseline_per_enabled_alias_with_the_server_version(self):
        # S04-AC1: one file per enabled alias, every field, the server's version and no secret.
        for mode in ((), ("--concurrent", "2")):
            with self.subTest(mode=mode):
                FakeClient.instances, BaselineClient.touched = [], []
                for path in self.output.glob("*"):
                    path.unlink()
                code, out, err = self.run_main(*mode, "--identity-salt-env", "FLEET_SALT",
                                               env={**ENV, "FLEET_SALT": SALT})
                self.assertEqual(code, 0, err)
                self.assertEqual(self.files(), [f"alpha-{STAMP}.json", f"bravo-{STAMP}.json"])
                self.assertEqual(len(FakeClient.instances), 2)  # charlie is disabled: never logged in
                self.assert_read_only()
                self.assertIn("| charlie | disabled | n/a | n/a | n/a |", out)
                for alias, user_id in (("alpha", "1001"), ("bravo", "1002")):
                    path = self.output / f"{alias}-{STAMP}.json"
                    record = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(set(record), BASELINE_KEYS)
                    self.assertEqual(record["account_alias"], alias)
                    self.assertEqual(record["observed_at"], OBSERVED.isoformat())
                    self.assertEqual(record["game_version"], SERVER_VERSION)
                    self.assertNotEqual(record["game_version"], DECLARED_VERSION)
                    self.assertEqual(record["source"], "run.py --baseline")
                    self.assertEqual(record["hull_level"], 5)
                    self.assertEqual(record["resources"], {"minerals": 1000, "gas": 1000, "starbux": None})
                    self.assertIsNone(record["trophies"])  # not observed by the provider: None, never 0
                    self.assertEqual(record["game_identity"], game_identity_fingerprint(user_id, SALT))
                    loaded = load_baseline_file(path)
                    self.assertEqual(loaded.game_version, SERVER_VERSION)
                    self.assertEqual(loaded.snapshot.account_alias, alias)
                    self.assertIn(f"| {alias} | written | {alias}-{STAMP}.json | {SERVER_VERSION} |", out)
                self.assertEqual(scan_baseline_directory(self.output), [])
                text = out + err + "".join(p.read_text(encoding="utf-8") for p in self.output.iterdir())
                for value in SECRET_VALUES:
                    self.assertNotIn(value, text)
                self.assertNotIn("1001", text)
                self.assertFalse((self.dir / ".device").exists())

    # Verifies: S04-R3
    def test_game_version_is_none_when_the_server_setting_has_none(self):
        for latest in (None, latest_setting(), latest_setting(**{"@MinimumClientVersion": "0.998.0"}),
                       latest_setting(**{"@LatestVersion": "see https://example.invalid/notes"})):
            with self.subTest(latest=latest):
                for path in self.output.glob("*"):
                    path.unlink()
                BaselineClient.latest = latest
                code, out, err = self.run_main()
                self.assertEqual(code, 0, err)
                record = json.loads((self.output / f"alpha-{STAMP}.json").read_text(encoding="utf-8"))
                self.assertIsNone(record["game_version"])
                self.assertIsNone(record["client_version"])
                self.assertIn(f"| alpha | written | alpha-{STAMP}.json | n/a |", out)

    # Verifies: S04-R6
    def test_prints_and_writes_the_summary_of_every_baseline(self):
        summary = self.dir / "summary" / "baselines.md"
        with patch.object(BaselineClient, "listItemsOfAShip", lambda client: False):
            code, out, err = self.run_main("--baseline-summary", str(summary))
        self.assertEqual(code, 0, err)
        self.assertIn("## Summary", out)
        self.assertIn("| Account | Observed (UTC) | Game version | Hull bracket | Missing sections |", out)
        row = self.row(out, "alpha", "## Summary")
        self.assertEqual(row, f"| alpha | 2026-11-11T12:30:45Z | {SERVER_VERSION} | hull 5-7 | "
                              "profile, resources, inventory |")
        self.assertIn("reads that returned no data: listItemsOfAShip", self.row(out, "alpha"))
        self.assertIn(f"Summary written: {summary}", out)
        written = summary.read_text(encoding="utf-8")
        self.assertIn(row, written)
        self.assertIn("| bravo | 2026-11-11T12:30:45Z |", written)
        self.assertNotIn("charlie", written)
        for value in SECRET_VALUES:
            self.assertNotIn(value, written)

    # Verifies: S04-R6
    def test_summary_lists_earlier_baselines_and_flags_invalid_files(self):
        later = datetime(2026, 11, 12, 8, 0, 0, tzinfo=timezone.utc)
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        (self.output / "stray.json").write_text('{"UserId": "700000101"}', encoding="utf-8")
        code, out, err = self.run_main(observed=later)
        self.assertEqual(code, 0, err)
        summary = out.split("## Summary", 1)[1]
        alpha_rows = [line for line in summary.splitlines() if line.startswith("| alpha |")]
        self.assertEqual([line.split(" | ")[1] for line in alpha_rows],
                         ["2026-11-11T12:30:45Z", "2026-11-12T08:00:00Z"])
        self.assertIn("- stray.json: BaselineError: refusing to load a baseline that fails the secret scan", summary)
        self.assertNotIn("700000101", out)

    # Verifies: S04-R1
    def test_existing_baseline_for_the_same_alias_and_time_is_refused(self):
        self.output.mkdir()
        existing = self.output / f"alpha-{STAMP}.json"
        existing.write_text("kept\n", encoding="utf-8")
        code, out, err = self.run_main()
        self.assertEqual(code, 1, err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "kept\n")
        self.assertIn("| alpha | error | n/a |", out)
        self.assertIn(f"BaselineExistsError: a baseline for 'alpha' at this time already exists: alpha-{STAMP}.json",
                      self.row(out, "alpha"))
        self.assertIn(f"| bravo | written | bravo-{STAMP}.json |", out)
        self.assertIn(f"- alpha-{STAMP}.json: BaselineError:", out)  # the stray file is listed, not loaded

    # Verifies: S04-R1
    def test_login_errors_are_redacted_and_other_accounts_still_written(self):
        FakeClient.login_error = True
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.files(), [])
        for alias in ("alpha", "bravo"):
            self.assertIn(f"| {alias} | error | n/a | n/a | AccountOpenError: RuntimeError: login rejected", out)
        for value in SECRET_VALUES:
            self.assertNotIn(value, out + err)
        FakeClient.login_error = False
        code, out, _ = self.run_main(env={"ALPHA_AUTH": ENV["ALPHA_AUTH"]})
        self.assertEqual(code, 1)
        self.assertEqual(self.files(), [f"alpha-{STAMP}.json"])
        self.assertIn("CredentialError: no credentials in the environment: set BRAVO (auth string)",
                      self.row(out, "bravo"))

    # Verifies: S04-R1
    def test_two_aliases_on_one_game_account_are_not_written(self):
        from tests.test_fleet_dry_run import USER_IDS

        with patch.dict(USER_IDS, {ENV["BRAVO_EMAIL"]: "1001"}):
            code, out, _ = self.run_main("--identity-salt-env", "FLEET_SALT", env={**ENV, "FLEET_SALT": SALT})
        self.assertEqual(code, 1)
        self.assertEqual(self.files(), [f"alpha-{STAMP}.json"])
        self.assertIn("| bravo | refused |", out)
        self.assertIn("game identity already observed for 'alpha'", self.row(out, "bravo"))

    # Verifies: S04-R1
    def test_setup_errors_exit_2_before_any_login(self):
        code, _, err = self.run_main("--identity-salt-env", "FLEET_SALT")
        self.assertEqual(code, 2)
        self.assertIn("identity salt variable FLEET_SALT is not set", err)
        code, out, err = self.run_main("--identity-salt-env", "FLEET_SALT", env={**ENV, "FLEET_SALT": "short-salt"})
        self.assertEqual(code, 2)
        self.assertNotIn("short-salt", out + err)
        code, _, err = self.run_main("--baseline-summary", str(self.dir / "summary.json"))
        self.assertEqual(code, 2)
        self.assertIn("must not be a .json file", err)
        self.registry.unlink()
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("registry not found", err)
        self.assertEqual(FakeClient.instances, [])

    # Verifies: S04-R1
    def test_flag_validation(self):
        cases = [
            (("--registry", "x.json"), "--baseline needs --registry PATH and --output DIR"),
            (("--output", "d"), "--baseline needs --registry PATH and --output DIR"),
            (("--registry", "x.json", "--output", "d", "--fleet-dry-run"), "--baseline cannot be combined with "
                                                                           "--fleet-dry-run"),
            (("--registry", "x.json", "--output", "d", "--fleet-execute", "--authorization", "a.json"),
             "--baseline cannot be combined with --fleet-execute, --authorization"),
            (("--registry", "x.json", "--output", "d", "--fleet-journal-dir", "j"),
             "--baseline cannot be combined with --fleet-journal-dir"),
            (("--registry", "x.json", "--output", "d", "--auth-file", "a", "--smtp-email", "x"),
             "--baseline cannot be combined with --auth-file, --smtp-email"),
            (("--registry", "x.json", "--output", "d", "--report", "fleet"), "--baseline cannot be combined with "
                                                                             "--report"),
            (("--registry", "x.json", "--output", "d", "--concurrent", "0"), "must be a positive integer"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                stderr = io.StringIO()
                with patch.object(sys, "argv", ["run.py", "--baseline", *args]), \
                        patch("run.Client", side_effect=forbidden("run.Client")), \
                        redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
                    run.main()
                self.assertEqual(exit_info.exception.code, 2)
                self.assertIn(message, stderr.getvalue())
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["run.py", "--baseline-summary", "s.md"]), \
                patch("run.Client", side_effect=forbidden("run.Client")), \
                redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("--baseline-summary is only used with --baseline", stderr.getvalue())


class ServerGameVersionTests(unittest.TestCase):
    # Verifies: S04-R3
    def test_reads_the_latest_version_setting_only(self):
        class Holder:
            pass

        client = Holder()
        self.assertIsNone(server_game_version(client))  # never read: unobserved
        client.latestVersion = {}
        self.assertIsNone(server_game_version(client))
        client.latestVersion = latest_setting(**{"@MinimumClientVersion": "0.998.0"})
        self.assertIsNone(server_game_version(client))  # the minimum accepted version is not the game version
        client.latestVersion = latest_setting(**{"@LatestVersion": " 0.999.60 "})
        self.assertEqual(server_game_version(client), "0.999.60")
        client.latestVersion = latest_setting(**{"@LatestVersion": "", SERVER_VERSION_ATTRIBUTES[-1]: "1.0.2"})
        self.assertEqual(server_game_version(client), "1.0.2")
        client.latestVersion = latest_setting(**{"@LatestVersion": "eyJhbGciOiJIUzI1NiJ9.e30.x"})
        self.assertIsNone(server_game_version(client))
        client.latestVersion = {"SettingService": {"GetLatestSetting": "unexpected"}}
        self.assertIsNone(server_game_version(client))
        client.headers = {"ClientVersion": DECLARED_VERSION}  # the declared version is never consulted
        self.assertIsNone(server_game_version(client))

    # Verifies: S04-R3
    def test_live_setting_shape_gives_recommended_version_and_data_counter(self):
        """The G1 capture showed none of the first three attributes; RecommendedVersion and the counter exist."""
        class Holder:
            pass

        client = Holder()
        live = {"@MinimumClientVersion": "1.0", "@MinimumVersion": "1.0", "@CurrentAndroidVersion": "1.0",
                "@RecommendedVersion": "1.0", "@ServerSettingVersion": "1234567"}
        client.latestVersion = latest_setting(**live)
        self.assertEqual(server_game_version(client), "1.0+settings.1234567")
        client.latestVersion = latest_setting(**{**live, "@LatestVersion": "0.999.60"})
        self.assertEqual(server_game_version(client), "0.999.60+settings.1234567")
        client.latestVersion = latest_setting(**{"@ServerSettingVersion": "1234567"})
        self.assertEqual(server_game_version(client), "settings.1234567")
        client.latestVersion = latest_setting(**{"@RecommendedVersion": "1.0", "@ServerSettingVersion": "12ab"})
        self.assertEqual(server_game_version(client), "1.0")  # a non-numeric counter is not recorded
        client.latestVersion = latest_setting(**{"@MinimumClientVersion": "1.0", "@CurrentAndroidVersion": "1.0"})
        self.assertIsNone(server_game_version(client))  # minimum and other-platform versions are not used

    # Verifies: S04-R1, S04-R3
    def test_open_baseline_takes_one_read_only_snapshot(self):
        BaselineClient.touched = []
        BaselineClient.latest = latest_setting(**{"@LatestVersion": SERVER_VERSION})
        FakeClient.instances = []
        profile = AccountProfile(alias="alpha", objective="grow", policy_id="baseline-v1",
                                 credentials_secret="ALPHA_AUTH")
        scrubber = LogScrubber()
        with tempfile.TemporaryDirectory() as tmp:
            factory = EnvironmentAccountFactory(tmp, client_cls=BaselineClient, environ=ENV, log_scrubber=scrubber,
                                                source="run.py --baseline")
            observation = factory.open_baseline(profile, clock=lambda: OBSERVED)
            self.assertEqual(os.listdir(tmp), [])  # no journal is opened
        self.assertEqual(observation.game_version, SERVER_VERSION)
        self.assertEqual(observation.snapshot.observed_at, OBSERVED)
        self.assertEqual(observation.failed_reads, ())
        (client,) = FakeClient.instances
        self.assertEqual(client.calls, ["login", "getShipByUserId", "listAllCharactersOfUser", "listItemsOfAShip"])
        self.assertEqual(BaselineClient.touched, [])
        self.assertIn(ENV["ALPHA_AUTH"], scrubber.scrub(ENV["ALPHA_AUTH"]) + ENV["ALPHA_AUTH"])
        self.assertNotIn(ENV["ALPHA_AUTH"], scrubber.scrub(f"line {ENV['ALPHA_AUTH']}"))


class BaselineGameAdapter(fleet_tests.FakeGameAdapter):
    """``FakeGameAdapter`` whose server announces a latest version and serves one ship per account.

    The real ``Client.login`` parses the ``GetLatestVersion3`` reply in ``getLatestVersion3``.
    """

    def __init__(self):
        super().__init__()
        self.bodies = []

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        if request.body:
            self.bodies.append(request.body if isinstance(request.body, str) else request.body.decode())
        if path.endswith("GetLatestVersion3"):
            with self.lock:
                self.paths.append(path)
            return xml_response(f'<SettingService><GetLatestSetting><Setting LatestVersion="{SERVER_VERSION}" '
                                'MinimumClientVersion="0.998.0" RoomDesignVersion="1" ResearchDesignVersion="1"/>'
                                '</GetLatestSetting></SettingService>')
        if path.endswith("GetShipByUserId"):
            with self.lock:
                self.paths.append(path)
            return xml_response('<ShipService><GetShipByUserId><Ship ShipLevel="9"><Rooms><Room RoomId="r1" '
                                'RoomDesignId="100" RoomStatus="Normal"/></Rooms><Researches><Research ResearchDesignId="400" '
                                'ResearchState="Completed"/></Researches></Ship>'
                                '</GetShipByUserId></ShipService>')
        return super().send(request, **kwargs)


class RealClientBaselineTests(unittest.TestCase):
    """The real ``Client`` parses the server's setting; nothing secret reaches any output or file."""

    FORBIDDEN = fleet_tests.RealClientLeakTests.FORBIDDEN

    # Verifies: S04-R1, S04-R3
    def test_real_client_baseline_uses_the_server_version_and_leaks_nothing(self):
        from sdk.client import Client

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        registry, output = directory / "fleet.json", directory / "baselines"
        registry.write_text(json.dumps(REAL_REGISTRY), encoding="utf-8")
        adapter = BaselineGameAdapter()
        log = io.StringIO()
        capture = logging.StreamHandler(log)
        capture.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root = logging.getLogger()
        argv = ["run.py", "--baseline", "--registry", str(registry), "--output", str(output),
                "--identity-salt-env", "SYNTH_SALT", "--concurrent", "3"]
        stdout, stderr = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, cwd)
        with patch.object(sys, "argv", argv), patch.dict(os.environ, REAL_ENV, clear=True), \
                patch.object(root, "handlers", [capture]), \
                patch.dict(Client.session.adapters, {"https://": adapter}), \
                patch.object(Client, "request", inspect.unwrap(Client.request)), \
                patch("run.run_baseline", partial(run.run_baseline, clock=lambda: OBSERVED)), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        out, err = stdout.getvalue(), stderr.getvalue()
        self.assertEqual(capture.filters, [], "the log scrubber must be removed after the run")
        self.assertEqual(exit_info.exception.code, 1, err)  # charlie's e-mail login is rejected by the fake server
        self.assertEqual(sorted(path.name for path in output.iterdir()), [f"alpha-{STAMP}.json", f"bravo-{STAMP}.json"])
        self.assertTrue(any(DECLARED_VERSION in body for body in adapter.bodies))  # the client did declare it
        for alias, user_id in (("alpha", "700000101"), ("bravo", "700000202")):
            loaded = load_baseline_file(output / f"{alias}-{STAMP}.json")
            self.assertEqual(loaded.game_version, SERVER_VERSION)
            self.assertEqual(loaded.snapshot.hull_level, 9)
            self.assertEqual(loaded.snapshot.game_identity, game_identity_fingerprint(user_id, SALT))
        self.assertIn("| charlie | error |", out)
        self.assertIn(f"| alpha | {OBSERVED:%Y-%m-%dT%H:%M:%SZ} | {SERVER_VERSION} | hull 8-10 |", out)
        self.assertFalse([path for path in adapter.paths if any(word in path for word in MUTATING_PATHS)])
        self.assertNotIn("/DesignService/ListAllDesigns4", adapter.paths)
        self.assertEqual(scan_baseline_directory(output), [])
        files = "".join(path.read_text(encoding="utf-8") for path in output.iterdir())
        combined = "\n".join((out, err, log.getvalue(), files))
        self.assertEqual([value for value in self.FORBIDDEN if value in combined], [], combined)
        self.assertNotRegex(combined, r"(?i)user_?id['\"]?\s*[=:]\s*['\"]?\d")
        self.assertFalse((directory / ".device").exists())


if __name__ == "__main__":
    unittest.main()
