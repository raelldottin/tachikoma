"""Tests for run.py --fleet-execute (SPEC-05) with fake clients. No network traffic, no real credentials.

Exercises the flags --fleet-execute, --authorization, --mode, --plan-only, --registry, --concurrent,
--fleet-journal-dir and --identity-salt-env, and the execute gate in sdk.fleet.factory. Every client is a
fake (or the real Client behind an in-memory HTTP adapter); sockets are patched to raise. A fleet dry run
with the dry-run fakes runs first wherever the execute run needs one.
"""

import ast
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import run  # imported here so run.py's log file opens in the repository, not a test tmpdir
from sdk.fleet.accounts import load_registry
from sdk.fleet.authorization import Placement, authorization_from_dict, authorization_hash, load_authorization
from sdk.fleet.concurrent import ConcurrentFleetOrchestrator
from sdk.fleet.factory import (
    DRY_RUN_ID,
    DRY_RUN_POLICY,
    DRY_RUN_REGISTRY,
    DryRunJournal,
    ExecutionGate,
    AuthorizedPolicy,
    EnvironmentAccountFactory,
    ExecutionRefusedError,
    GatedActionRunner,
    _ExecuteGateMixin,
    registry_hash,
    registry_stamp,
)
from sdk.fleet.orchestrator import FleetOrchestrator
from sdk.journal import GameplayEvent, GameplayJournal, JournalError, read_events
from sdk.policy import ActionRequest, DesignData, Plan, PolicyConfig, get_policy
from sdk.state import build_snapshot
from sdk.state.snapshot import game_identity_fingerprint
from tests.synthetic_fixtures import SYNTHETIC_DEVICE_KEY_IOS, SYNTHETIC_DEVICE_KEY_MAC
from tests.test_fleet_dry_run import (
    ALPHA_AUTH,
    ALPHA_REFRESH,
    BRAVO_EMAIL,
    BRAVO_PASSWORD,
    REAL_IDS,
    REAL_NAMES,
    REAL_REFRESH,
    REAL_TOKENS,
    SALT,
    USER_IDS,
    FakeClient,
    FakeGameAdapter,
    forbidden,
    xml_response,
)
from tests.test_fleet_dry_run import ENV as CREDENTIALS
from tests import test_room_placement as placement_tests  # a module import: its test cases are not rerun here

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
SALT_ENV = "FLEET_SALT"
ENV = {**CREDENTIALS, SALT_ENV: SALT}
SECRET_VALUES = (ALPHA_AUTH, ALPHA_REFRESH, BRAVO_EMAIL, BRAVO_PASSWORD, SALT)
ALPHA_ID = game_identity_fingerprint("1001", SALT)
BRAVO_ID = game_identity_fingerprint("1002", SALT)
SAFE_ACTIONS = ["collect_all_resources", "collect_daily_reward"]


def profile(alias, secret, identity, **extra):
    return {"alias": alias, "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": secret,
            "expected_game_identity": identity, **extra}


def registry_payload(**overrides):
    accounts = {
        "alpha": profile("alpha", "ALPHA_AUTH", ALPHA_ID),
        "bravo": profile("bravo", "BRAVO", BRAVO_ID),
        "charlie": profile("charlie", "CHARLIE_AUTH", None, enabled=False),
    }
    for alias, fields in overrides.items():
        accounts[alias] = {**accounts[alias], **fields}
    for entry in accounts.values():
        if entry.get("expected_game_identity") is None:
            entry.pop("expected_game_identity", None)
    return {"version": 1, "accounts": list(accounts.values())}


def utc(moment):
    return moment.isoformat().replace("+00:00", "Z")


def authorization_payload(**overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "version": 1,
        "owner": "owner",
        "purpose": "offline execute test",
        "accounts": ["alpha", "bravo"],
        "actions": list(SAFE_ACTIONS),
        "not_before": utc(now - timedelta(hours=1)),
        "expires": utc(now + timedelta(days=7)),
        "caps": {"alpha": {"minerals": 1000, "gas": 1000}, "bravo": {"minerals": 1000, "gas": 1000}},
        "modes": ["canary", "full"],
    }
    payload.update(overrides)
    return payload


class ExecClient(FakeClient):
    """The dry-run fake plus the mutating calls an execute run makes; records every one.

    ``effects`` maps a method name to a function of the client that replaces its default effect.
    """

    executed = []
    effects = {}

    def __init__(self, device=None, settings=None):
        super().__init__(device, settings)
        self.minerals, self.gas = 1000, 1000

    def login(self, email=None, password=None):
        self.calls.append("login")
        key = email if email else self.device.refreshToken
        self.user = type("User", (), {"id": USER_IDS.get(key, "1001")})()
        return True

    def listItemsOfAShip(self):
        self.calls.append("listItemsOfAShip")
        self.itemsOfAShip = {"Items": {"Item": [
            {"@ItemDesignId": "1", "@ItemType": "Mineral", "@Quantity": str(self.minerals)},
            {"@ItemDesignId": "2", "@ItemType": "Gas", "@Quantity": str(self.gas)},
        ]}}
        return True

    def _mutate(self, name, default, *args):
        ExecClient.executed.append((self.user.id, name))
        self.calls.append(name)
        effect = ExecClient.effects.get(name)
        return effect(self, *args) if effect else default(*args)

    def _collect(self):
        self.minerals += 100
        return True

    def _reward(self):
        self.gas += 10
        return True

    def collectAllResources(self):
        return self._mutate("collectAllResources", self._collect)

    def collectDailyReward(self):
        return self._mutate("collectDailyReward", self._reward)

    def upgradeRoom(self, room_id, upgrade_room_design_id):
        return self._mutate("upgradeRoom", lambda *args: True, room_id, upgrade_room_design_id)

    def addResearch(self, research_design_id):
        return self._mutate("addResearch", lambda *args: True, research_design_id)

    def runBattleEndToEnd(self):
        return self._mutate("runBattleEndToEnd", lambda: True)


class OverplanningPolicy:
    """Plans ``requests`` whatever the caps say, so the runner's own checks are reached."""

    name = "baseline-v1"

    def __init__(self, *requests):
        self.requests = requests

    def plan(self, snapshot, design, config):
        return Plan(self.name, snapshot.account_alias, snapshot.observed_at.isoformat(), self.requests, ())


# Every attribute a live ListItemsOfAShip item carries (first G2 canary, 2026-11-12): none names a type.
LIVE_ITEM_ATTRIBUTES = ("BonusEnhancementType", "BonusEnhancementValue", "Flags", "IsNew", "ItemDesignId", "ItemId",
                        "LegendaryBonusEnhancementType", "LegendaryBonusEnhancementValue", "Quantity", "ShipId",
                        "SkinKey")


def live_item(design_id, quantity):
    item = {f"@{name}": "0" for name in LIVE_ITEM_ATTRIBUTES}
    item.update({"@ItemDesignId": design_id, "@Quantity": str(quantity)})
    return item


class LiveShapedItems:
    """Ship items shaped as the live server sends them, and the item designs ``listAllDesigns4`` stores."""

    def listItemsOfAShip(self):
        self.calls.append("listItemsOfAShip")
        self.itemsOfAShip = {"ItemService": {"ListItemsOfAShip": {"Items": {"Item": [
            live_item("1", getattr(self, "minerals", 1000)), live_item("2", getattr(self, "gas", 1000)),
            live_item("900", 3),
        ]}}}}
        return True

    def listAllDesigns4(self):
        loaded = super().listAllDesigns4()
        if loaded:
            self.itemDesigns = {"ItemDesign": [
                {"@ItemDesignId": "1", "@ItemName": "Mineral", "@ItemType": "Mineral"},
                {"@ItemDesignId": "2", "@ItemName": "Gas", "@ItemType": "Gas"},
                {"@ItemDesignId": "900", "@ItemName": "Helmet", "@ItemType": "Equipment"},
            ]}
        return loaded


class LiveExecClient(LiveShapedItems, ExecClient):
    """``ExecClient`` with live-shaped ship items."""


class LowMineralsDryClient(FakeClient):
    """The dry-run fake with too few minerals for the room upgrade, so baseline-v1 plans no collection."""

    def listItemsOfAShip(self):
        self.calls.append("listItemsOfAShip")
        self.itemsOfAShip = {"Items": {"Item": [
            {"@ItemDesignId": "1", "@ItemType": "Mineral", "@Quantity": "100"},
            {"@ItemDesignId": "2", "@ItemType": "Gas", "@Quantity": "1000"},
        ]}}
        return True


class LowMineralsExecClient(ExecClient):
    """``ExecClient`` with too few minerals for the room upgrade (300), as on the first live canary."""

    def __init__(self, device=None, settings=None):
        super().__init__(device, settings)
        self.minerals = 100


SERVER_VERSIONS = {}  # user id -> ServerSettingVersion the fake server reports at login


class VersionedExecClient(ExecClient):
    """``ExecClient`` whose login reads the server's latest-version setting, as ``Client.login`` does."""

    def login(self, email=None, password=None):
        ok = super().login(email, password)
        self.latestVersion = {"SettingService": {"GetLatestSetting": {"Setting": {
            "@RecommendedVersion": "1.0", "@ServerSettingVersion": SERVER_VERSIONS[self.user.id]}}}}
        return ok


class FleetExecuteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.journal_dir = self.dir / "journals"
        self.registry = self.dir / "fleet.json"
        self.authorization = self.dir / "authorization.json"
        self.write_registry()
        self.write_authorization()
        FakeClient.instances = []
        FakeClient.login_error = False
        FakeClient.designs4_ok = True
        ExecClient.executed = []
        ExecClient.effects = {}
        self.execute_values = []
        for cls in (FleetOrchestrator, ConcurrentFleetOrchestrator):
            original = cls.__dict__["run"]

            def spy(orchestrator, *, execute=False, _original=original):
                self.execute_values.append(execute)
                return _original(orchestrator, execute=execute)
            patcher = patch.object(cls, "run", spy)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = io.StringIO()
        capture = logging.StreamHandler(self.log)
        root = logging.getLogger()
        patcher = patch.object(root, "handlers", [capture])
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.dir)  # EphemeralDevice must never touch ./.device
        self.addCleanup(os.chdir, cwd)

    def write_registry(self, **overrides):
        self.registry.write_text(json.dumps(registry_payload(**overrides)), encoding="utf-8")

    def write_authorization(self, **overrides):
        self.authorization.write_text(json.dumps(authorization_payload(**overrides)), encoding="utf-8")

    def main(self, *argv, env=ENV, client=ExecClient):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["run.py", *argv]), patch.dict(os.environ, env, clear=True), \
                patch("run.Client", client), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch("getpass.getpass", side_effect=forbidden("getpass")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue()

    def dry_run(self, env=ENV):
        """A fleet dry run with the dry-run fakes, which fail on any mutating call."""
        code, out, err = self.main("--fleet-dry-run", "--registry", str(self.registry), "--fleet-journal-dir",
                                   str(self.journal_dir), "--identity-salt-env", SALT_ENV, env=env, client=FakeClient)
        self.assertEqual(code, 0, err)
        return out

    def execute(self, *extra, env=ENV, client=ExecClient):
        return self.main("--fleet-execute", "--registry", str(self.registry), "--authorization",
                         str(self.authorization), "--fleet-journal-dir", str(self.journal_dir),
                         "--identity-salt-env", SALT_ENV, *extra, env=env, client=client)

    def execute_function(self, **options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ENV, clear=True), redirect_stdout(stdout), redirect_stderr(stderr), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")):
            code = run.run_fleet_execute(str(self.registry), str(self.authorization), journal_dir=self.journal_dir,
                                         identity_salt_env=SALT_ENV, client_cls=ExecClient, **options)
        return code, stdout.getvalue(), stderr.getvalue()

    def exec_logins(self):
        return [client for client in FakeClient.instances if isinstance(client, ExecClient)]

    def assert_refused_before_login(self, code, err, *messages):
        self.assertEqual(code, 2, err)
        self.assertIn("refused before any login", err)
        for message in messages:
            self.assertIn(message, err)
        self.assertEqual(self.exec_logins(), [])
        self.assertNotIn(True, self.execute_values)
        self.assertFalse((self.journal_dir / "runs").exists())

    def row(self, output, alias):
        return next(line for line in output.splitlines() if line.startswith(f"| {alias} |"))

    def records(self):
        records = [json.loads(path.read_text(encoding="utf-8")) for path in (self.journal_dir / "runs").glob("*.json")]
        return sorted(records, key=lambda record: record["started_at"])

    def attempted(self, alias):
        return [event for event in read_events(self.journal_dir / f"{alias}.log") if event.attempted]


# Verifies: S05-R1
class ExecutePathTests(unittest.TestCase):
    def test_run_fleet_execute_is_the_only_execute_true_call(self):
        def execute_true_calls(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = []
            for function in ast.walk(tree):
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(function):
                        if isinstance(node, ast.Call) and any(
                                keyword.arg == "execute" and isinstance(keyword.value, ast.Constant)
                                and keyword.value.value is True for keyword in node.keywords):
                            found.append(function.name)
            return found

        self.assertEqual(execute_true_calls(REPO_ROOT / "run.py"), ["run_fleet_execute"])
        for path in sorted((REPO_ROOT / "sdk").rglob("*.py")):
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                self.assertEqual(execute_true_calls(path), [])


class AuthorizationRefusalTests(FleetExecuteTestCase):
    # Verifies: S05-R1, S05-R2
    def test_missing_authorization_exits_non_zero_before_any_login(self):
        self.dry_run()
        self.authorization.unlink()
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, "authorization not found")
        code, _, err = self.main("--fleet-execute", "--registry", str(self.registry), "--identity-salt-env", SALT_ENV)
        self.assertEqual(code, 2)
        self.assertIn("--fleet-execute needs --registry PATH and --authorization PATH", err)
        self.assertEqual(self.exec_logins(), [])

    # Verifies: S05-R1, S05-R2
    def test_expired_and_not_yet_valid_authorizations_are_refused(self):
        self.dry_run()
        now = datetime.now(timezone.utc)
        cases = [
            ({"not_before": utc(now - timedelta(days=10)), "expires": utc(now - timedelta(days=3))},
             "authorization has expired"),
            ({"not_before": utc(now + timedelta(days=1)), "expires": utc(now + timedelta(days=2))},
             "authorization is not valid yet"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                self.write_authorization(**overrides)
                code, _, err = self.execute()
                self.assert_refused_before_login(code, err, message)

    # Verifies: S05-R1, S05-R2
    def test_malformed_authorizations_are_refused(self):
        self.dry_run()
        cases = [
            ("{not json", "not valid JSON"),
            (json.dumps({**authorization_payload(), "token": "x"}), "invalid authorization"),
            (json.dumps(authorization_payload(caps={"alpha": {"minerals": 1, "gas": 1, "starbux": 5},
                                                    "bravo": {"minerals": 1, "gas": 1}})),
             "starbux spending is never authorizable"),
            (json.dumps(authorization_payload(owner=BRAVO_EMAIL)), "invalid authorization: secret-shaped value at owner"),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                self.authorization.write_text(text, encoding="utf-8")
                code, out, err = self.execute()
                self.assert_refused_before_login(code, err, message)
                self.assertNotIn(BRAVO_EMAIL, out + err)

    # Verifies: S05-R1, S05-R2
    def test_out_of_scope_authorizations_are_refused(self):
        self.dry_run()
        cases = [
            ({"accounts": ["alpha"], "caps": {"alpha": {"minerals": 1, "gas": 1}}}, (), "account 'bravo' is not authorized"),
            ({"modes": ["canary"]}, ("--mode", "full"), "mode 'full' is not allowed by the authorization"),
        ]
        for overrides, extra, message in cases:
            with self.subTest(message=message):
                self.write_authorization(**overrides)
                code, _, err = self.execute(*extra)
                self.assert_refused_before_login(code, err, message)

    # Verifies: S05-R1
    def test_execute_flags_are_validated(self):
        cases = [
            (("--fleet-execute", "--fleet-dry-run", "--registry", "x", "--authorization", "a",
              "--identity-salt-env", SALT_ENV), "--fleet-execute cannot be combined with --fleet-dry-run"),
            (("--fleet-execute", "--registry", "x", "--authorization", "a"), "--identity-salt-env NAME"),
            (("--fleet-dry-run", "--registry", "x", "--authorization", "a"),
             "--authorization, --mode, --plan-only, --place-room and --halt-share are only used with "),
            (("--plan-only",), "only used with --fleet-execute"),
            (("--halt-share", "0.5"), "only used with --fleet-execute"),
            (("--fleet-execute", "--mode", "yolo"), "invalid choice"),
            (("--fleet-execute", "--halt-share", "1"), "must be at least 0 and below 1"),
            (("--fleet-execute", "--halt-share", "nan"), "must be at least 0 and below 1"),
            (("--fleet-execute", "--halt-share", "x"), "not a number"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                code, _, err = self.main(*argv)
                self.assertEqual(code, 2)
                self.assertIn(message, err)
        self.assertEqual(FakeClient.instances, [])


class DryRunRequirementTests(FleetExecuteTestCase):
    # Verifies: S05-R3
    def test_no_dry_run_is_refused_and_a_fresh_one_proceeds(self):
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, "account 'alpha': no completed --fleet-dry-run plan",
                                         "account 'bravo': no completed --fleet-dry-run plan")
        self.dry_run()
        code, out, err = self.execute()
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.execute_values[-1], True)
        self.assertEqual(len(self.exec_logins()), 2)

    # Verifies: S05-R3
    def test_a_dry_run_older_than_24_hours_is_refused(self):
        self.dry_run()
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        self.write_authorization(expires=utc(later + timedelta(days=1)))
        code, _, err = self.execute_function(clock=lambda: later)
        self.assert_refused_before_login(code, err, "its latest dry run is older than 24 hours")

    # Verifies: S05-R3
    def test_a_dry_run_of_another_policy_or_registry_is_refused(self):
        self.dry_run()
        self.write_registry(alpha={"policy_id": "hull-reserve-v1"})
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, "account 'alpha': its latest dry run did not use policy "
                                                    "'hull-reserve-v1'")
        self.write_registry(alpha={"max_actions": 5})
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, "its latest dry run was of a different registry")

    # Verifies: S05-R3
    def test_an_account_whose_dry_run_did_not_complete_is_refused(self):
        self.dry_run(env={"ALPHA_AUTH": ALPHA_AUTH, SALT_ENV: SALT})  # bravo's credentials are missing
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, "account 'bravo': no completed --fleet-dry-run plan")
        self.assertNotIn("account 'alpha'", err)

    # Verifies: S05-R3
    def test_only_a_completed_dry_run_is_stamped(self):
        path = self.dir / "alpha.log"
        now = datetime.now(timezone.utc)

        def event():
            return GameplayEvent.create(account_alias="alpha", action_id="collect_daily_reward", started_at=now,
                                        finished_at=now, outcome="skipped", reason="dry run: would call "
                                        "Client.collectDailyReward", attempted=False, dry_run=True)

        journal = DryRunJournal(path, policy_id="baseline-v1", registry_stamp="0123456789abcdef")
        journal.append(event())
        self.assertFalse(path.exists())
        self.assertEqual(len(journal.read()), 1)
        journal.commit(completed=False)
        journal.append(event())
        journal.commit(completed=True)
        first, second = read_events(path)
        self.assertEqual(dict(first.evidence), {})
        self.assertEqual(second.evidence[DRY_RUN_POLICY], "baseline-v1")
        self.assertEqual(second.evidence[DRY_RUN_REGISTRY], "0123456789abcdef")
        self.assertRegex(second.evidence[DRY_RUN_ID], r"^[0-9a-f]{32}$")

    # Verifies: S05-R3
    def test_the_fleet_dry_run_stamps_its_plan_with_the_registry_and_policy(self):
        self.dry_run()
        stamp = registry_stamp(load_registry(self.registry))
        self.assertEqual(stamp, registry_hash(load_registry(self.registry))[7:23])
        for alias in ("alpha", "bravo"):
            events = read_events(self.journal_dir / f"{alias}.log")
            self.assertEqual(len(events), 4)
            self.assertEqual({event.evidence[DRY_RUN_REGISTRY] for event in events}, {stamp})
            self.assertEqual({event.evidence[DRY_RUN_POLICY] for event in events}, {"baseline-v1"})
            self.assertEqual(len({event.evidence[DRY_RUN_ID] for event in events}), 1)


class CanaryTests(FleetExecuteTestCase):
    # Verifies: S05-R4
    def test_canary_executes_exactly_one_safe_to_retry_action_per_account(self):
        self.dry_run()
        for extra in ((), ("--concurrent", "2")):
            with self.subTest(extra=extra):
                ExecClient.executed = []
                code, out, err = self.execute(*extra)
                self.assertEqual(code, 0, err + out)
                self.assertEqual(sorted(ExecClient.executed),
                                 [("1001", "collectAllResources"), ("1002", "collectAllResources")])
                self.assertIn("# Fleet execute (canary,", out)
                self.assertIn("## Reconciliation", out)
                self.assertIn("| alpha | collect_all_resources | yes | verified | resources increased: "
                              "gas +0, minerals +100 |", out)
                self.assertIn("| bravo | collect_all_resources | yes | verified |", out)
                self.assertRegex(self.row(out, "alpha"), rf"\| alpha \| completed \| 1 \| {ALPHA_ID} \| alpha\.log \|")

    # Verifies: S05-R4
    def test_canary_with_no_safe_to_retry_action_executes_nothing(self):
        self.dry_run()
        self.write_authorization(actions=["collect_daily_reward"])
        code, out, err = self.execute()
        self.assertEqual(code, 0, err + out)
        self.assertEqual(ExecClient.executed, [])
        self.assertIn("| alpha | none | no | n/a | no action ran |", out)

    # Verifies: S05-R4, S05-R10
    def test_full_mode_executes_only_authorized_actions(self):
        self.dry_run()
        code, out, err = self.execute("--mode", "full")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(sorted(name for uid, name in ExecClient.executed if uid == "1001"),
                         ["collectAllResources", "collectDailyReward"])
        self.assertNotIn("upgradeRoom", [name for _, name in ExecClient.executed])
        self.assertNotIn("addResearch", [name for _, name in ExecClient.executed])

    # Verifies: S05-R10
    def test_plan_only_prints_the_plan_without_logging_in(self):
        self.dry_run()
        for mode, alpha in (("canary", "collect_all_resources"), ("full", "collect_all_resources, collect_daily_reward")):
            with self.subTest(mode=mode):
                code, out, err = self.execute("--plan-only", "--mode", mode)
                self.assertEqual(code, 0, err)
                self.assertIn(f"# Fleet execute plan ({mode}), plan only", out)
                self.assertIn("nothing was logged in", out)
                self.assertIn(f"| {alpha} | add_research, upgrade_room | 1000 | 1000 |", self.row(out, "alpha"))
        self.assertEqual(self.exec_logins(), [])
        self.assertNotIn(True, self.execute_values)
        self.assertFalse((self.journal_dir / "runs").exists())
        self.authorization.unlink()
        code, _, err = self.execute("--plan-only")
        self.assert_refused_before_login(code, err, "authorization not found")

    # Verifies: S05-R4
    def test_canary_collects_resources_when_the_plan_keeps_no_safe_to_retry_action(self):
        # Regression (first G2 canary, 2026-11-12): baseline-v1 plans a collection only after a room
        # upgrade, so with the upgrade unaffordable the canary kept nothing and rejected the daily reward
        # with "only the first safe-to-retry action runs" although nothing ran.
        self.dry_run()
        code, out, err = self.execute(client=LowMineralsExecClient)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(sorted(ExecClient.executed), [("1001", "collectAllResources"), ("1002", "collectAllResources")])
        self.assertIn("| alpha | collect_all_resources | yes | verified |", out)
        for entry in self.records()[0]["accounts"]:
            if entry["alias"] == "charlie":
                continue
            rejected = {item["candidate"]: item["reason"] for item in entry["rejected"]}
            self.assertEqual(rejected["collect_daily_reward"], "canary: not safe to retry")
            self.assertNotIn("canary: only the first safe-to-retry action runs", rejected.values())
            self.assertIn("needs 300 mineral", rejected["upgrade_room r1"])
            self.assertEqual(entry["planned"], 1)

    # Verifies: S05-R4, S05-R10
    def test_plan_only_canary_names_the_collection_a_plan_without_one_gets(self):
        code, out, err = self.main("--fleet-dry-run", "--registry", str(self.registry), "--fleet-journal-dir",
                                   str(self.journal_dir), "--identity-salt-env", SALT_ENV, client=LowMineralsDryClient)
        self.assertEqual(code, 0, err)
        code, out, err = self.execute("--plan-only", "--mode", "canary")
        self.assertEqual(code, 0, err)
        self.assertIn("| collect_all_resources | add_research | 1000 | 1000 |", self.row(out, "alpha"))
        self.write_authorization(actions=["collect_daily_reward"])
        code, out, err = self.execute("--plan-only", "--mode", "canary")
        self.assertEqual(code, 0, err)
        self.assertIn("| nothing | add_research | 1000 | 1000 |", self.row(out, "alpha"))

    # Verifies: S05-R4, S05-R6
    def test_the_canary_fallback_is_only_planned_when_allowed_and_observed(self):
        now = datetime.now(timezone.utc)
        registry = load_registry(self.registry)

        def plan(mode, *requests, actions=SAFE_ACTIONS, resources=None):
            gate = ExecutionGate(authorization_from_dict(authorization_payload(actions=list(actions))), registry,
                                 mode, lambda: now)
            snapshot = build_snapshot("alpha", now, "test", resources=resources)
            return AuthorizedPolicy(OverplanningPolicy(*requests), gate).plan(snapshot, DesignData(), PolicyConfig())

        observed = {"minerals": 5, "gas": 5, "starbux": None}
        daily = ActionRequest("collect_daily_reward", {}, "daily reward")
        collect = ActionRequest("collect_all_resources", {}, "collect")
        result = plan("canary", daily, resources=observed)
        self.assertEqual([request.action_id for request in result.requests], ["collect_all_resources"])
        self.assertEqual([(item.candidate, item.reason) for item in result.rejected],
                         [("collect_daily_reward", "canary: not safe to retry")])
        result = plan("canary", collect, collect, daily, resources=observed)
        self.assertEqual(result.requests, (collect,))
        self.assertEqual([item.reason for item in result.rejected],
                         ["canary: only the first safe-to-retry action runs", "canary: not safe to retry"])
        # Not outside canary mode, not when the authorization leaves the collection out, and not when the
        # resources it needs are unobserved.
        self.assertEqual(plan("full", daily, resources=observed).requests, (daily,))
        self.assertEqual(plan("canary", daily, actions=["collect_daily_reward"], resources=observed).requests, ())
        result = plan("canary", daily)
        self.assertEqual(result.requests, ())
        self.assertIn("collect_all_resources", [item.candidate for item in result.rejected])
        # Verifier finding: a resources section that is present but holds no mineral or gas value (item
        # designs unavailable) must not plan a collection the canary could never verify.
        result = plan("canary", daily, resources={"minerals": None, "gas": None, "starbux": None})
        self.assertEqual(result.requests, ())
        self.assertIn(("collect_all_resources", "minerals and gas not observed; the collection could not be verified"),
                      [(item.candidate, item.reason) for item in result.rejected])
        self.assertEqual([r.action_id for r in plan("canary", daily, resources={"minerals": 5, "gas": None,
                                                                                  "starbux": None}).requests],
                         ["collect_all_resources"])
        # A collection the gate rejected for its starbux cost is never planned again without one.
        result = plan("canary", ActionRequest("collect_all_resources", {}, "starbux", {"starbux": 10}),
                      resources=observed)
        self.assertEqual(result.requests, ())


# Verifies: S05-R4
class LiveShapedResourceTests(FleetExecuteTestCase):
    def test_live_ship_items_are_resources_through_the_item_designs_the_execute_path_reads(self):
        # Regression (first G2 canary, 2026-11-12): live items carry no @Type/@ItemType, so minerals and
        # gas were None and baseline-v1 refused every cost-bearing action ("gas balance not observed").
        factory = EnvironmentAccountFactory(self.journal_dir, client_cls=LiveExecClient, environ=ENV,
                                            identity_salt=SALT)
        alpha = load_registry(self.registry).get("alpha")
        context = factory(alpha)
        snapshot = context.snapshot_provider()
        self.assertEqual((snapshot.resources.minerals, snapshot.resources.gas), (1000, 1000))
        plan = get_policy("baseline-v1").plan(snapshot, context.design, PolicyConfig())
        self.assertEqual([request.action_id for request in plan.requests],
                         ["add_research", "upgrade_room", "collect_all_resources", "collect_daily_reward"])
        self.assertNotIn("not observed", " ".join(item.reason for item in plan.rejected))

        # The baseline reads no designs, so its calls and its unobserved resources are unchanged.
        observation = factory.open_baseline(alpha)
        client = FakeClient.instances[-1]
        self.assertEqual(client.calls, ["login", "getShipByUserId", "listAllCharactersOfUser", "listItemsOfAShip"])
        self.assertEqual((observation.snapshot.resources.minerals, observation.snapshot.resources.gas), (None, None))

    def test_a_live_shaped_canary_executes_its_collection(self):
        self.dry_run()
        code, out, err = self.execute(client=LiveExecClient)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(sorted(ExecClient.executed), [("1001", "collectAllResources"), ("1002", "collectAllResources")])
        self.assertIn("| alpha | collect_all_resources | yes | verified | resources increased: "
                      "gas +0, minerals +100 |", out)


class StopConditionTests(FleetExecuteTestCase):
    # Verifies: S05-R5
    def test_contradicted_result_stops_the_account_and_halts_the_fleet(self):
        self.dry_run()

        def shrink(client):
            client.minerals -= 50
            return True
        ExecClient.effects = {"collectAllResources": shrink}
        code, out, _ = self.execute("--mode", "full")
        self.assertEqual(code, 1)
        self.assertIn("| alpha | stopped |", out)
        self.assertIn("collect_all_resources contradicted", self.row(out, "alpha"))
        self.assertIn("| bravo | not-run |", out)
        self.assertIn("fleet halted: 1 of 2 accounts stopped", self.row(out, "bravo"))
        self.assertEqual(ExecClient.executed, [("1001", "collectAllResources")])
        self.assertEqual(len(self.exec_logins()), 1)

    # Verifies: S05-R5
    def test_halt_share_is_configurable(self):
        self.dry_run()
        ExecClient.effects = {"collectAllResources": lambda client: setattr(client, "minerals", 0) or True}
        code, out, _ = self.execute("--mode", "full", "--halt-share", "0.5")
        self.assertEqual(code, 1)
        self.assertIn("| alpha | stopped |", out)
        self.assertIn("| bravo | stopped |", out)  # bravo still ran: one of two is not more than half
        self.assertEqual(self.records()[0]["stop_reason"], "fleet halted: 2 of 2 accounts stopped (more than 50%)")
        self.assertEqual(self.records()[0]["halt_share"], 0.5)
        logins = len(self.exec_logins())
        code, _, err = self.execute_function(halt_share=1.0)
        self.assertEqual(code, 2)
        self.assertIn("halt share must be at least 0 and below 1", err)
        self.assertEqual(len(self.exec_logins()), logins)

    # Verifies: S05-R5
    def test_a_halt_stops_accounts_that_are_already_running_in_both_modes(self):
        """Regression: a concurrent halt used to stop only accounts that had not started."""
        self.dry_run()
        bravo_collecting, halted = threading.Event(), threading.Event()
        original_finish = _ExecuteGateMixin._finish

        def finish(orchestrator, *args, **kwargs):
            run_ = original_finish(orchestrator, *args, **kwargs)
            if orchestrator.halt_reason is not None:
                halted.set()
            return run_

        def collect(client):
            if client.user.id == "1001":  # alpha: contradicted, once bravo is mid-collection (concurrent only)
                bravo_collecting.wait(5)
                client.minerals -= 50
            else:
                bravo_collecting.set()
                halted.wait(5)
                client.minerals += 100
            return True

        ExecClient.effects = {"collectAllResources": collect}
        with patch.object(_ExecuteGateMixin, "_finish", finish):
            code, out, _ = self.execute("--mode", "full", "--concurrent", "2")
        self.assertEqual(code, 1)
        self.assertTrue(halted.is_set())
        self.assertIn("collect_all_resources contradicted", self.row(out, "alpha"))
        self.assertIn("| bravo | stopped |", out)
        self.assertIn("runner refused collect_daily_reward: fleet halted: 1 of 2 accounts stopped",
                      self.row(out, "bravo"))
        self.assertEqual(sorted(ExecClient.executed), [("1001", "collectAllResources"), ("1002", "collectAllResources")])
        refusal = read_events(self.journal_dir / "bravo.log")[-1]
        self.assertEqual((refusal.action_id, refusal.attempted, refusal.error_class),
                         ("collect_daily_reward", False, "ExecutionRefusedError"))

        # Sequential: the halting account is the only one running, so the next account never starts.
        ExecClient.executed, bravo_collecting = [], threading.Event()
        bravo_collecting.set()
        code, out, _ = self.execute("--mode", "full")
        self.assertEqual(code, 1)
        self.assertIn("| bravo | not-run |", out)
        self.assertEqual(ExecClient.executed, [("1001", "collectAllResources")])

        # The runner itself refuses once the fleet is halted, before any call.
        gate = ExecutionGate(authorization_from_dict(authorization_payload()), load_registry(self.registry), "full",
                             lambda: datetime.now(timezone.utc))
        runner = GatedActionRunner(gate, {"minerals": 1000, "gas": 1000, "starbux": 0}, timedelta(hours=24), "alpha",
                                   ExecClient(), lambda: build_snapshot("alpha", datetime.now(timezone.utc), "test"),
                                   GameplayJournal(self.dir / "unit.log"), halted=lambda: "fleet halted: test")
        with self.assertRaises(ExecutionRefusedError) as raised:
            runner.run("collect_all_resources")
        self.assertEqual(str(raised.exception), "fleet halted: test")
        self.assertEqual(ExecClient.executed, [("1001", "collectAllResources")])

    # Verifies: S05-R5
    def test_a_non_idempotent_action_without_a_planned_cost_is_refused_under_caps(self):
        """Regression: an empty planned cost used to pass any cap, even a cap of 0."""
        self.write_authorization(actions=["upgrade_room", *SAFE_ACTIONS],
                                 caps={"alpha": {"minerals": 0, "gas": 0}, "bravo": {"minerals": 0, "gas": 0}})
        self.dry_run()
        policy = OverplanningPolicy(ActionRequest("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "101"},
                                                  "no cost planned"))
        with patch("sdk.fleet.factory.get_policy", return_value=policy):
            code, out, _ = self.execute("--mode", "full")
        self.assertEqual(code, 1)
        self.assertIn("runner refused upgrade_room: a non-idempotent action without a planned cost cannot be "
                      "checked against the caps", self.row(out, "alpha"))
        self.assertEqual(ExecClient.executed, [])

    # Verifies: S05-R5, S05-R6
    def test_a_battle_is_refused_when_a_capped_budget_cannot_cover_its_rearm(self):
        """Regression: a battle rearms (RebuildAmmo3) at an unrecorded cost, and used to run under a cap of 0."""
        battle_auth = {"actions": ["run_pvp_battle", *SAFE_ACTIONS], "battles": {"max_per_window": 5, "window_hours": 24}}

        def rearm_then_battle(client):
            client.minerals -= 50  # the rearm's cost, which no plan records
            return True

        policy = OverplanningPolicy(ActionRequest("run_pvp_battle", {}, "battle"))
        for caps, kind in (({"minerals": 0, "gas": 1000}, "minerals"), ({"minerals": 1000, "gas": 0}, "gas")):
            with self.subTest(kind=kind):
                ExecClient.executed = []
                self.write_authorization(caps={"alpha": caps, "bravo": caps}, **battle_auth)
                self.dry_run()
                ExecClient.effects = {"runBattleEndToEnd": rearm_then_battle}
                with patch("sdk.fleet.factory.get_policy", return_value=policy):
                    code, out, _ = self.execute("--mode", "full")
                self.assertEqual(code, 1)
                self.assertIn(f"runner refused run_pvp_battle: a battle may rearm at an unrecorded {kind} cost, and 0 "
                              f"{kind} of the cap is left (it needs at least 1)", self.row(out, "alpha"))
                self.assertEqual(ExecClient.executed, [])

        # With budget left the battle runs, and its observed rearm spend is charged to the period.
        self.write_authorization(**battle_auth)
        self.dry_run()
        with patch("sdk.fleet.factory.get_policy", return_value=policy):
            self.execute("--mode", "full")
        self.assertEqual(ExecClient.executed, [("1001", "runBattleEndToEnd")])
        battle = self.attempted("alpha")[-1]
        self.assertEqual(battle.cost["minerals_delta"], -50)

    # Verifies: S05-R5, S08-R2
    def test_the_battle_cap_is_enforced_by_the_execute_run(self):
        """Regression: the authorization's battle cap was never checked by --fleet-execute."""
        battle_auth = {"actions": ["run_pvp_battle", *SAFE_ACTIONS], "battles": {"max_per_window": 1, "window_hours": 24}}
        self.write_authorization(**battle_auth)
        self.dry_run()
        policy = OverplanningPolicy(ActionRequest("run_pvp_battle", {}, "battle"))
        with patch("sdk.fleet.factory.get_policy", return_value=policy):
            self.execute("--mode", "full")
            self.assertEqual(ExecClient.executed, [("1001", "runBattleEndToEnd")])
            code, out, _ = self.execute("--mode", "full")
        # alpha's one battle in the window used its cap: the second run plans none for it.
        self.assertIn("| alpha | completed | 0 |", out)
        cap = "account 'alpha' has used 1 of 1 battles allowed in 24 hours"
        alpha = next(entry for entry in self.records()[1]["accounts"] if entry["alias"] == "alpha")
        self.assertIn({"candidate": "run_pvp_battle", "reason": f"not authorized: {cap}"}, alpha["rejected"])
        self.assertEqual([call for call in ExecClient.executed if call[0] == "1001"], [("1001", "runBattleEndToEnd")])
        code, out, _ = self.execute("--plan-only", "--mode", "full")
        self.assertEqual(code, 0)

        # The runner checks the cap again with the journal's events, before any call.
        gate = ExecutionGate(authorization_from_dict(authorization_payload(**battle_auth)),
                             load_registry(self.registry), "full", lambda: datetime.now(timezone.utc))
        calls = len(ExecClient.executed)
        runner = GatedActionRunner(gate, {"minerals": 1000, "gas": 1000, "starbux": 0}, timedelta(hours=24), "alpha",
                                   ExecClient(), lambda: build_snapshot("alpha", datetime.now(timezone.utc), "test"),
                                   GameplayJournal(self.journal_dir / "alpha.log"))
        with self.assertRaises(ExecutionRefusedError) as raised:
            runner.run("run_pvp_battle")
        self.assertEqual(str(raised.exception), cap)
        self.assertEqual(len(ExecClient.executed), calls)

        # Fail closed: without the account's events, or when they cannot be read, a battle is refused.
        self.assertIn("battle cap cannot be checked", gate.refusal("bravo", "run_pvp_battle"))
        self.assertIsNone(gate.refusal("bravo", "run_pvp_battle", lambda: []))

        class UnreadableJournal(GameplayJournal):
            def read(self):
                raise JournalError("corrupt")

        runner = GatedActionRunner(gate, {"minerals": 1000, "gas": 1000, "starbux": 0}, timedelta(hours=24), "bravo",
                                   ExecClient(), lambda: build_snapshot("bravo", datetime.now(timezone.utc), "test"),
                                   UnreadableJournal(self.dir / "unreadable.log"))
        with self.assertRaises(ExecutionRefusedError) as raised:
            runner.run("run_pvp_battle")
        self.assertIn("cannot read the journal to check the battle cap (JournalError)", str(raised.exception))
        self.assertEqual(len(ExecClient.executed), calls)

    # Verifies: S05-R5
    def test_unknown_result_of_a_non_idempotent_action_stops_the_account(self):
        self.write_authorization(actions=["upgrade_room", *SAFE_ACTIONS])
        self.dry_run()

        def raises(client, *args):
            raise ConnectionError("timed out")
        ExecClient.effects = {"upgradeRoom": raises}
        code, out, _ = self.execute("--mode", "full")
        self.assertEqual(code, 1)
        self.assertIn("upgrade_room outcome uncertain: call raised ConnectionError", self.row(out, "alpha"))
        self.assertEqual(ExecClient.executed, [("1001", "upgradeRoom")])
        self.assertIn("| alpha | upgrade_room | yes | unknown |", out)

    # Verifies: S05-R5
    def test_isolation_refusal_stops_the_account_before_any_call(self):
        self.dry_run()
        with patch.dict(USER_IDS, {ALPHA_REFRESH: "1003"}):  # the credentials now log in as another account
            code, out, _ = self.execute()
        self.assertEqual(code, 1)
        self.assertIn("| alpha | error |", out)
        self.assertIn("does not match expected_game_identity", self.row(out, "alpha"))
        self.assertIn("| bravo | not-run |", out)
        self.assertEqual(ExecClient.executed, [])

    # Verifies: S05-R5
    def test_an_action_whose_cap_would_be_exceeded_is_refused(self):
        self.dry_run()
        policy = OverplanningPolicy(ActionRequest("collect_all_resources", {}, "overplanned", {"mineral": 5000}))
        with patch("sdk.fleet.factory.get_policy", return_value=policy):
            code, out, _ = self.execute()
        self.assertEqual(code, 1)
        self.assertIn("runner refused collect_all_resources: minerals cap would be exceeded "
                      "(0 spent + 5000 planned > 1000)", self.row(out, "alpha"))
        self.assertEqual(ExecClient.executed, [])
        refusal = read_events(self.journal_dir / "alpha.log")[-1]
        self.assertEqual((refusal.outcome, refusal.attempted, refusal.error_class),
                         ("skipped", False, "ExecutionRefusedError"))

    # Verifies: S05-R5
    def test_fleet_stop_file_stops_between_actions_and_accounts(self):
        self.dry_run()
        stop = self.journal_dir / "STOP"
        stop.write_text("", encoding="utf-8")
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, f"fleet stop file present: {stop}")
        stop.unlink()

        def collect_then_stop(client):
            stop.write_text("", encoding="utf-8")
            client.minerals += 100
            return True
        ExecClient.effects = {"collectAllResources": collect_then_stop}
        code, out, _ = self.execute("--mode", "full")
        self.assertEqual(code, 1)
        self.assertIn("| alpha | stopped |", out)
        self.assertIn("operator stop file present", self.row(out, "alpha"))
        self.assertIn("| bravo | not-run | 0 |", out)
        self.assertIn("fleet stop file present", self.row(out, "bravo"))
        self.assertNotIn("fleet halted", out)  # the stop file, not the halt share, stopped the fleet
        self.assertEqual(ExecClient.executed, [("1001", "collectAllResources")])

    # Verifies: S05-R6
    def test_starbux_is_never_spent_even_when_the_registry_budget_allows_it(self):
        self.write_registry(alpha={"budgets": {"starbux": 500}})
        self.dry_run()
        policy = OverplanningPolicy(ActionRequest("collect_all_resources", {}, "starbux", {"starbux": 10}))
        with patch("sdk.fleet.factory.get_policy", return_value=policy):
            code, out, err = self.execute()
        self.assertEqual(code, 0, err + out)
        self.assertEqual(ExecClient.executed, [])
        record = self.records()[0]
        self.assertEqual(record["accounts"][0]["budget_remaining"]["starbux"], 0)

        gate = ExecutionGate(authorization_from_dict(authorization_payload()), load_registry(self.registry), "full",
                             lambda: datetime.now(timezone.utc))
        self.assertEqual(gate.budgets(load_registry(self.registry).get("alpha"))["starbux"], 0)
        runner = GatedActionRunner(gate, {"starbux": 0}, timedelta(hours=24), "alpha", ExecClient(),
                                   lambda: build_snapshot("alpha", datetime.now(timezone.utc), "test"),
                                   GameplayJournal(self.dir / "unit.log"))
        with self.assertRaises(ExecutionRefusedError) as raised:
            runner.run("collect_all_resources", expected_cost={"starbux": 1})
        self.assertIn("starbux spending is disabled", str(raised.exception))
        self.assertEqual(ExecClient.executed, [])


class RunRecordTests(FleetExecuteTestCase):
    # Verifies: S05-R7
    def test_run_record_is_complete_and_every_attempt_is_journaled(self):
        self.dry_run()
        code, out, err = self.execute("--mode", "full")
        self.assertEqual(code, 0, err + out)
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIn(f"Run record: {next((self.journal_dir / 'runs').glob('*.json'))}", out)
        self.assertEqual(record["authorization_hash"], authorization_hash(load_authorization(self.authorization)))
        self.assertEqual(record["registry_hash"], registry_hash(load_registry(self.registry)))
        self.assertEqual(record["policy_versions"], {"alpha": "baseline-v1", "bravo": "baseline-v1"})
        self.assertEqual(record["game_version"]["server"], None)
        self.assertRegex(record["game_version"]["client"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(record["mode"], "full")
        self.assertLessEqual(datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["finished_at"]))
        by_alias = {entry["alias"]: entry for entry in record["accounts"]}
        self.assertEqual(by_alias["charlie"]["status"], "disabled")
        for alias, identity in (("alpha", ALPHA_ID), ("bravo", BRAVO_ID)):
            entry = by_alias[alias]
            self.assertEqual((entry["status"], entry["policy_version"], entry["game_identity"]),
                             ("completed", "baseline-v1", identity))
            self.assertRegex(entry["dry_run_id"], r"^[0-9a-f]{32}$")
            journaled = {event.attempt_id for event in self.attempted(alias)}
            recorded = {result["attempt_id"] for result in entry["results"] if result["attempted"]}
            self.assertEqual(len(journaled), entry["attempted"])
            self.assertEqual(journaled, recorded)
            self.assertEqual({result["outcome"] for result in entry["results"]}, {"verified"})
        code, _, _ = self.execute()
        self.assertEqual(len(self.records()), 2)  # a second run never replaces the first record

    # Verifies: S05-R7
    def test_run_record_reports_the_server_game_version_each_account_saw(self):
        # Regression (first G2 canary, 2026-11-12): the record hard-coded the server version as null.
        self.dry_run()
        for versions, server in (({"1001": "42", "1002": "42"}, "1.0+settings.42"),
                                 ({"1001": "42", "1002": "43"}, None)):
            with self.subTest(versions=versions):
                SERVER_VERSIONS.clear()
                SERVER_VERSIONS.update(versions)
                for path in (self.journal_dir / "runs").glob("*.json"):
                    path.unlink()
                code, out, err = self.execute(client=VersionedExecClient)
                self.assertEqual(code, 0, err + out)
                record = self.records()[0]
                self.assertEqual(record["game_version"]["server"], server)
                by_alias = {entry["alias"]: entry["game_version"] for entry in record["accounts"]}
                self.assertEqual(by_alias, {"alpha": f"1.0+settings.{versions['1001']}",
                                            "bravo": f"1.0+settings.{versions['1002']}", "charlie": None})
        SERVER_VERSIONS.clear()

    # Verifies: S05-R8, S10-R1
    def test_a_profile_without_a_fingerprint_or_a_missing_salt_is_refused(self):
        self.write_registry(bravo={"expected_game_identity": None})
        self.dry_run()
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, "account 'bravo' declares no expected_game_identity")
        self.write_registry()
        self.dry_run()
        code, _, err = self.execute(env=CREDENTIALS)
        self.assert_refused_before_login(code, err, f"identity salt variable {SALT_ENV} is not set")
        code, out, err = self.execute(env={**CREDENTIALS, SALT_ENV: "short-salt"})
        self.assert_refused_before_login(code, err, "identity salt must be at least")
        self.assertNotIn("short-salt", out + err)

    # Verifies: S06-R2, S10-R1
    def test_a_legacy_run_account_must_be_confirmed_skipped_before_execute(self):
        self.write_registry(alpha={"legacy_run_account": 1})
        self.dry_run()
        code, _, err = self.execute()
        self.assert_refused_before_login(code, err, "is legacy daily run account 1")
        self.write_authorization(legacy_run_skipped=[1])
        code, _, err = self.execute()
        self.assertNotIn("refused before any login", err)
        self.assertNotIn("legacy daily run account", err)

    # Verifies: S05-R8
    def test_the_salt_is_required_by_the_function_too(self):
        self.dry_run()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run.run_fleet_execute(str(self.registry), str(self.authorization), journal_dir=self.journal_dir,
                                         environ=ENV, client_cls=ExecClient)
        self.assert_refused_before_login(code, stderr.getvalue(), "--identity-salt-env NAME is required")

    # Verifies: S05-R9
    def test_no_credential_salt_or_raw_id_reaches_any_output(self):
        self.dry_run()

        def leaky(client):
            logging.error("collect failed for %s with %s userId=%s", client.device.authentication_string,
                          SALT, client.user.id)
            raise RuntimeError(f"rejected {client.device.refreshToken} {SALT} userId={client.user.id}")
        ExecClient.effects = {"collectAllResources": leaky}
        code, out, err = self.execute("--mode", "full")
        self.assertEqual(code, 0, err + out)  # an unknown collection is safe to retry and does not stop
        texts = [out, err, self.log.getvalue()]
        texts += [path.read_text(encoding="utf-8") for path in self.journal_dir.glob("*.log")]
        texts += [path.read_text(encoding="utf-8") for path in (self.journal_dir / "runs").glob("*.json")]
        combined = "\n".join(texts)
        self.assertIn("collect failed for", combined)
        for value in SECRET_VALUES:
            self.assertNotIn(value, combined)
        self.assertNotRegex(combined, r"(?i)user_?id['\"]?\s*[=:]\s*['\"]?\d")


# --- Real Client, fake HTTP adapter -------------------------------------------------------------

REAL_SALT_ENV = "SYNTH_SALT"
REAL_ENV = {
    "SYNTH_ALPHA": f"fleet|{SYNTHETIC_DEVICE_KEY_IOS}|{REAL_REFRESH['alpha']}|en",
    "SYNTH_BRAVO": f"fleet|{SYNTHETIC_DEVICE_KEY_MAC}|{REAL_REFRESH['bravo']}|en",
    REAL_SALT_ENV: SALT,
}
REAL_REGISTRY = {"version": 1, "accounts": [
    profile(alias, f"SYNTH_{alias.upper()}", game_identity_fingerprint(REAL_IDS[REAL_REFRESH[alias]], SALT))
    for alias in ("alpha", "bravo")]}
ITEMS_XML = ('<ItemService><ListItemsOfAShip><Items><Item ItemDesignId="1" ItemType="Mineral" Quantity="1000"/>'
             '<Item ItemDesignId="2" ItemType="Gas" Quantity="1000"/></Items></ListItemsOfAShip></ItemService>')
LIVE_OPS_XML = '<LiveOpsService><GetTodayLiveOps><LiveOps DailyRewardArgument="7"/></GetTodayLiveOps></LiveOpsService>'


class ExecuteGameAdapter(FakeGameAdapter):
    """The dry-run adapter, answering ship items too and letting an execute run's calls through."""

    def __init__(self, execute=False):
        super().__init__()
        self.execute = execute

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        answers = {"ListItemsOfAShip": ITEMS_XML, "GetTodayLiveOps2": LIVE_OPS_XML}
        answer = next((text for name, text in answers.items() if path.endswith(name)), None)
        if answer is None and self.execute and "Collect" in path:
            answer = '<UserService errorMessage="not today"/>'
        if answer is None:
            return super().send(request, **kwargs)
        with self.lock:
            self.paths.append(path)
        return xml_response(answer)


# Verifies: S05-R9
class RealClientExecuteLeakTests(unittest.TestCase):
    """The real ``Client`` logs request URLs and player names; none of it may reach any output."""

    FORBIDDEN = tuple(sorted(set(REAL_REFRESH.values()) | {REAL_ENV["SYNTH_ALPHA"], REAL_ENV["SYNTH_BRAVO"], SALT}
                             | set(REAL_TOKENS) | set(REAL_TOKENS.values()) | set(REAL_NAMES.values())))

    def run_main(self, directory, *argv, execute=False):
        from sdk.client import Client

        adapter = ExecuteGameAdapter(execute)
        log = io.StringIO()
        capture = logging.StreamHandler(log)
        capture.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["run.py", *argv]), patch.dict(os.environ, REAL_ENV, clear=True), \
                patch.object(logging.getLogger(), "handlers", [capture]), \
                patch.dict(Client.session.adapters, {"https://": adapter}), \
                patch.object(Client, "request", inspect.unwrap(Client.request)), \
                patch("run.Device", side_effect=forbidden("run.Device")), \
                patch.object(socket, "socket", side_effect=forbidden("socket.socket")), \
                patch.object(socket, "create_connection", side_effect=forbidden("socket.create_connection")), \
                redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_info:
            run.main()
        self.assertEqual(capture.filters, [], "the log scrubber must be removed after the run")
        self.assertFalse((directory / ".device").exists())
        return exit_info.exception.code, stdout.getvalue(), stderr.getvalue(), log.getvalue(), adapter

    def test_no_secret_reaches_output_logs_journals_or_the_run_record(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        cwd = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, cwd)
        registry, journals, authorization = directory / "fleet.json", directory / "journals", directory / "auth.json"
        registry.write_text(json.dumps(REAL_REGISTRY), encoding="utf-8")
        authorization.write_text(json.dumps(authorization_payload()), encoding="utf-8")
        common = ("--registry", str(registry), "--fleet-journal-dir", str(journals), "--identity-salt-env", REAL_SALT_ENV)
        code, *texts, _ = self.run_main(directory, "--fleet-dry-run", *common)
        self.assertEqual(code, 0, texts)
        code, out, err, log, adapter = self.run_main(directory, "--fleet-execute", "--authorization", str(authorization),
                                                     "--mode", "full", *common, execute=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("/UserService/CollectDailyReward2", adapter.paths)
        self.assertIn("ERROR", log)  # the client did log, and the records were scrubbed
        self.assertIn("| alpha | collect_daily_reward | yes |", out)
        written = [path.read_text(encoding="utf-8") for path in journals.glob("*.log")]
        written += [path.read_text(encoding="utf-8") for path in (journals / "runs").glob("*.json")]
        combined = "\n".join([*texts, out, err, log, *written])
        leaked = [value for value in self.FORBIDDEN if value in combined]
        self.assertEqual(leaked, [], combined)
        self.assertNotRegex(combined, r"(?i)user_?id['\"]?\s*[=:]\s*['\"]?\d")


class PlaceClient(ExecClient):
    """pss5's hull, rooms and room designs as captured live (2026-11-12); ``placeRoom`` buys on its ship.

    Bought rooms live in ``ships`` (by user id), so a later run's login sees them, as on the real server.
    ``drop_room_id`` leaves one placed room without ``@RoomId``.
    """

    ships = {}
    drop_room_id = False

    def __init__(self, device=None, settings=None):
        super().__init__(device, settings)

    @property
    def placed(self):
        return PlaceClient.ships.setdefault(self.user.id, [])

    def listAllDesigns4(self):
        self.calls.append("listAllDesigns4")
        if not FakeClient.designs4_ok:
            return False
        self.shipDesigns = {"ShipDesign": placement_tests.SHIP_DESIGNS}
        self.roomDesignPurchases = {"RoomDesignPurchase": placement_tests.PURCHASES}
        self.roomDesigns = {"stale": "overwritten by listRoomDesigns2"}
        return True

    def listRoomDesigns2(self):
        self.calls.append("listRoomDesigns2")
        self.roomDesigns = {"RoomDesigns": {"RoomDesign": placement_tests.ROOM_DESIGNS}}

    def getShipByUserId(self):
        self.calls.append("getShipByUserId")
        rooms = placement_tests.room_records() + self.placed
        if PlaceClient.drop_room_id:
            rooms = [{key: value for key, value in rooms[0].items() if key != "@RoomId"}] + rooms[1:]
        self.shipByUserId = {"Ship": {"@ShipLevel": "2", "@ShipDesignId": "6", "Rooms": {"Room": rooms},
                                      "Researches": None}}
        return True

    def _buy(self, design_id, row, column):
        self.placed.append({"@RoomId": f"room-instance-{len(self.placed) + 7000}", "@RoomDesignId": str(design_id),
                            "@RoomStatus": "Building", "@Row": str(row), "@Column": str(column)})
        self.minerals -= 10
        return True

    def placeRoom(self, room_design_id, row, column):
        return self._mutate("placeRoom", self._buy, room_design_id, row, column)


class PlaceModeTests(FleetExecuteTestCase):
    """``--fleet-execute --mode place`` (SPEC-13, S13-R7, R8, R10) with pss5's real layout; no game traffic."""

    def setUp(self):
        super().setUp()
        PlaceClient.ships = {}
        PlaceClient.drop_room_id = False
        self.write_registry(bravo={"enabled": False})
        self.write_placement_authorization()

    def write_placement_authorization(self, **overrides):
        fields = dict(accounts=["alpha"], actions=["place_room"], modes=["place"],
                      caps={"alpha": {"minerals": 10, "gas": 0}},
                      placements={"alpha": [{"room_design_id": 42, "position": "first-free"}]})
        fields.update(overrides)
        self.write_authorization(**fields)

    def place(self, spec="42@first-free", *extra):
        return self.execute("--mode", "place", "--place-room", spec, *extra, client=PlaceClient)

    # Verifies: S13-R7
    def test_first_free_places_the_gas_collector_and_verifies_it(self):
        code, out, err = self.place()
        self.assertEqual(code, 0, err + out)
        self.assertEqual(ExecClient.executed, [("1001", "placeRoom")])
        (event,) = self.attempted("alpha")
        self.assertEqual((event.action_id, dict(event.params), event.outcome),
                         ("place_room", {"room_design_id": "42", "row": 1, "column": 1}, "verified"))
        self.assertEqual(event.cost["minerals_delta"], -10)
        (record,) = self.records()
        self.assertEqual((record["mode"], record["placement"]), ("place", {"room_design_id": 42,
                                                                           "position": "first-free"}))
        self.assertIn("row 1, column 1", record["accounts"][0]["results"][0]["reason"])
        self.assertNotIn("room-instance-7000", (self.journal_dir / "alpha.log").read_text(encoding="utf-8"))
        self.assertFalse(any(call == "collectAllResources" for _, call in ExecClient.executed))

    # Verifies: S13-R7
    def test_place_mode_needs_no_dry_run_but_still_checks_identity(self):
        code, out, err = self.place()  # no --fleet-dry-run first
        self.assertEqual(code, 0, err + out)
        self.write_registry(alpha={"expected_game_identity": BRAVO_ID}, bravo={"enabled": False})
        code, out, err = self.place()
        self.assertNotEqual(code, 0)
        self.assertEqual(ExecClient.executed, [("1001", "placeRoom")])  # the second run placed nothing

    # Verifies: S13-R7
    def test_requests_outside_the_authorization_are_refused_before_login(self):
        cases = {
            "--mode place without --place-room": (("--mode", "place"), "--place-room is required"),
            "--place-room in canary mode": (("--mode", "canary", "--place-room", "42@first-free"),
                                            "--place-room is required with --mode place"),
            "a position the file does not name": (("--mode", "place", "--place-room", "42@1,1"), "not named"),
            "another design": (("--mode", "place", "--place-room", "26@first-free"), "not named"),
            "a malformed placement": (("--mode", "place", "--place-room", "42-first-free"), "--place-room must be"),
            "a negative row": (("--mode", "place", "--place-room", "42@-1,1"), "non-negative integer"),
        }
        for name, (args, message) in cases.items():
            with self.subTest(case=name):
                code, _, err = self.execute(*args, client=PlaceClient)
                self.assert_refused_before_login(code, err, message)

    # Verifies: S13-R7
    def test_place_mode_needs_exactly_one_enabled_account_and_the_mode_allowed(self):
        self.write_registry()
        self.write_placement_authorization(accounts=["alpha", "bravo"],
                                           caps={"alpha": {"minerals": 10, "gas": 0}, "bravo": {"minerals": 0,
                                                                                               "gas": 0}})
        code, _, err = self.place()
        self.assert_refused_before_login(code, err, "exactly one enabled account")
        self.write_registry(bravo={"enabled": False})
        self.write_placement_authorization(modes=["canary"])
        code, _, err = self.place()
        self.assert_refused_before_login(code, err, "mode 'place' is not allowed")

    # Verifies: S13-R3, S13-R7
    def test_an_authorized_position_that_does_not_fit_is_not_attempted(self):
        self.write_placement_authorization(placements={"alpha": [{"room_design_id": 42, "row": 2, "column": 8}]})
        code, out, err = self.place("42@2,8")
        self.assertEqual(ExecClient.executed, [])
        (record,) = self.records()
        self.assertIn("occupied by a placed room", record["accounts"][0]["rejected"][0]["reason"])

    # Verifies: S13-R8
    def test_the_cap_is_checked_against_the_rooms_price(self):
        self.write_placement_authorization(caps={"alpha": {"minerals": 5, "gas": 0}})
        code, out, err = self.place()
        self.assertEqual(ExecClient.executed, [])
        refused = [event for event in read_events(self.journal_dir / "alpha.log") if event.action_id == "place_room"]
        self.assertIn("minerals cap would be exceeded", refused[-1].reason)

    # Verifies: S13-R8
    def test_a_room_priced_in_starbux_is_never_placed(self):
        self.write_placement_authorization(placements={"alpha": [{"room_design_id": 900, "position": "first-free"}]})
        code, out, err = self.place("900@first-free")
        self.assertEqual(ExecClient.executed, [])
        (record,) = self.records()
        self.assertIn("not priced in minerals or gas", record["accounts"][0]["rejected"][0]["reason"])

    # Verifies: S13-R10
    def test_plan_only_shows_the_authorized_placement_without_logging_in(self):
        code, out, err = self.place("42@first-free", "--plan-only")
        self.assertEqual(code, 0, err)
        self.assertIn("| alpha | room design 42 at first-free | 10 | 0 |", out)
        self.assertEqual(self.exec_logins(), [])
        code, _, err = self.place("42@1,1", "--plan-only")
        self.assert_refused_before_login(code, err, "not named")

    def test_the_gate_holds_a_placement_only_in_place_mode(self):
        auth = load_authorization(self.authorization)
        reg = load_registry(self.registry)
        with self.assertRaises(ValueError):
            ExecutionGate(auth, reg, "place", lambda: datetime.now(timezone.utc))
        with self.assertRaises(ValueError):
            ExecutionGate(auth, reg, "canary", lambda: datetime.now(timezone.utc), placement=Placement(42))
        gate = ExecutionGate(auth, reg, "place", lambda: datetime.now(timezone.utc), placement=Placement(42))
        self.assertIsNone(gate.refusal("alpha", "place_room"))
        self.assertIn("place mode runs only place_room", gate.refusal("alpha", "collect_all_resources"))
        self.assertEqual(gate.max_actions(reg.get("alpha")), 1)

    # Verifies: S13-R6
    def test_an_uncertain_placement_blocks_the_next_run(self):
        ExecClient.effects = {"placeRoom": lambda client, *args: object()}  # not the captured success shape
        self.place()
        (first,) = self.attempted("alpha")
        self.assertEqual(first.outcome, "unknown")
        ExecClient.effects = {}
        # A different, authorized position: only the account-wide block (not a same-params match) stops it.
        self.write_placement_authorization(placements={"alpha": [{"room_design_id": 42, "position": "first-free"},
                                                                 {"room_design_id": 42, "row": 6, "column": 4}]})
        code, out, err = self.place("42@6,4")
        self.assertEqual(ExecClient.executed, [("1001", "placeRoom")])  # the second run reached no game call
        last = read_events(self.journal_dir / "alpha.log")[-1]
        self.assertEqual((last.action_id, last.attempted), ("place_room", False))
        self.assertIn("still uncertain", last.reason)

    # Verifies: S13-R3
    def test_unknown_design_data_or_a_partial_room_list_refuses_the_placement(self):
        for name, setup, reason in (
                ("no hull or purchase records", lambda: setattr(FakeClient, "designs4_ok", False),
                 "interior grid of hull design 6 is not known"),
                ("a room without an id", lambda: setattr(PlaceClient, "drop_room_id", True),
                 "rooms were not all observed")):
            with self.subTest(case=name):
                FakeClient.designs4_ok, PlaceClient.drop_room_id = True, False
                for path in (self.journal_dir / "runs").glob("*.json") if (self.journal_dir / "runs").exists() else ():
                    path.unlink()
                setup()
                self.place()
                self.assertEqual(ExecClient.executed, [])
                (record,) = self.records()
                self.assertIn(reason, record["accounts"][0]["rejected"][0]["reason"])
        FakeClient.designs4_ok = True

    # Verifies: S13-R7
    def test_the_gate_checks_place_room_mode_and_params(self):
        auth = load_authorization(self.authorization)
        reg = load_registry(self.registry)
        now = lambda: datetime.now(timezone.utc)  # noqa: E731
        canary = ExecutionGate(auth, reg, "canary", now)
        self.assertIn("runs only in place mode", canary.refusal("alpha", "place_room"))
        first_free = ExecutionGate(auth, reg, "place", now, placement=Placement(42))
        self.assertIsNone(first_free.params_refusal("place_room", {"room_design_id": "42", "row": 5, "column": 9}))
        self.assertIn("room design", first_free.params_refusal("place_room", {"room_design_id": "26", "row": 1,
                                                                              "column": 1}))
        fixed = ExecutionGate(auth, reg, "place", now, placement=Placement(42, 1, 1))
        self.assertIsNone(fixed.params_refusal("place_room", {"room_design_id": "42", "row": 1, "column": 1}))
        self.assertIn("position", fixed.params_refusal("place_room", {"room_design_id": "42", "row": 1, "column": 2}))
        self.assertIsNone(canary.params_refusal("collect_all_resources", {}))

    def test_parse_place_room(self):
        self.assertEqual(run.parse_place_room("42@first-free"), Placement(42))
        self.assertEqual(run.parse_place_room("42@3,4"), Placement(42, 3, 4))
        for bad in ("42", "42@", "42@1", "x@first-free", "42@1,x", "0@first-free"):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                run.parse_place_room(bad)


if __name__ == "__main__":
    unittest.main()
