"""run.py --training-journal-dir: the daily run's crew-training journal. No network traffic, no real credentials.

OpenTrainingJournalTests exercise the file-system helper alone; TrainingJournalFlagTests the flag's refusals in
run.main() with a patched Device and Client. DailyRunTrainingJournalE2ETests run the real daily path against FakePss
with and without the flag, and DailyRunReportStepTests run .github/workflows/daily-run.yml's own report step.
"""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import run
from scripts import ci
from sdk.client import Client
from sdk.journal import GameplayEvent, GameplayJournal, read_events
from tests.fakes.pss_server import FakePss
from tests.fakes.state import FAKE_EPOCH, FAKE_PASSWORD, default_world
from tests.synthetic_fixtures import SYNTHETIC_EMAIL
from tests.test_daily_run_offline_e2e import DAILY_STEPS, DailyRun, _step_recorder
from tests.test_strict_mode import skip_unless_dependency

try:
    import yaml
except ImportError:
    yaml = None


class OpenTrainingJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_creates_nested_directory_and_empty_journal_named_after_alias(self):
        journal = run.open_training_journal(self.root / "a" / "b", "account-1")
        self.assertIsInstance(journal, GameplayJournal)
        self.assertEqual(journal.path, self.root / "a" / "b" / "account-1.jsonl")
        self.assertTrue(journal.path.is_file())
        self.assertEqual(journal.path.stat().st_size, 0)

    def test_existing_journal_is_kept_byte_for_byte(self):
        directory = self.root / "journals"
        directory.mkdir()
        existing = b'{"line": 1}\n'
        (directory / "account-1.jsonl").write_bytes(existing)
        journal = run.open_training_journal(directory, "account-1")
        self.assertEqual(journal.path.read_bytes(), existing)

    def test_refuses_alias_that_is_not_a_lowercase_alias(self):
        aliases = ("test@example.com", "Account-1", "", "account-1\n", "../x", "-x", "a" * 65)
        for index, alias in enumerate(aliases):
            directory = self.root / f"journals-{index}"
            with self.subTest(alias=alias):
                with self.assertRaises(run.TrainingJournalError) as raised:
                    run.open_training_journal(directory, alias)
                message = str(raised.exception)
                self.assertIn("--account-alias", message)
                if alias:
                    self.assertNotIn(alias, message)
                self.assertFalse(directory.exists())

    def test_unwritable_directory_reports_strerror(self):
        # A regular file blocks the directory, not chmod: permission bits do not stop a test running as root.
        blocker = self.root / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")
        for case, directory, strerror in (("parent is a file", blocker / "journals", "Not a directory"),
                                          ("directory is a file", blocker, "File exists")):
            with self.subTest(case=case):
                with self.assertRaises(run.TrainingJournalError) as raised:
                    run.open_training_journal(directory, "account-1")
                self.assertEqual(str(raised.exception),
                                 f"cannot create training journal directory {directory}: {strerror}")

    def test_oserror_without_errno_keeps_its_message(self):
        # Regression: an OSError raised with only a message has strerror None, so the error read "...: None".
        directory = self.root / "journals"
        with patch.object(Path, "mkdir", side_effect=OSError("synthetic mkdir failure")):
            with self.assertRaises(run.TrainingJournalError) as raised:
                run.open_training_journal(directory, "account-1")
        self.assertEqual(str(raised.exception),
                         f"cannot create training journal directory {directory}: synthetic mkdir failure")
        with patch.object(Path, "open", side_effect=OSError("synthetic open failure")):
            with self.assertRaises(run.TrainingJournalError) as raised:
                run.open_training_journal(directory, "account-1")
        self.assertEqual(str(raised.exception),
                         f"cannot create training journal {directory / 'account-1.jsonl'}: synthetic open failure")

    def test_journal_path_that_is_a_directory_is_refused(self):
        # Path.touch(exist_ok=True) would succeed here (os.utime works on a directory) and hand over a journal
        # that fails on its first append.
        path = self.root / "account-1.jsonl"
        path.mkdir()
        with self.assertRaises(run.TrainingJournalError) as raised:
            run.open_training_journal(self.root, "account-1")
        self.assertEqual(str(raised.exception), f"cannot create training journal {path}: Is a directory")


class TrainingJournalFlagTests(unittest.TestCase):
    """The flag's refusals: each exits 2 before SMTP setup, Device or Client construction and login."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.password_file = self.root / "pw.txt"
        self.password_file.write_text("synthetic-password\n", encoding="utf-8")

    def run_main(self, *argv):
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["run.py", *argv]), \
                patch("run.Device", side_effect=AssertionError("no Device")) as device, \
                patch("run.Client", side_effect=AssertionError("no Client")) as client, \
                patch("getpass.getpass", side_effect=AssertionError("no prompt")), \
                patch("run.email_logfile", side_effect=AssertionError("no SMTP")), \
                patch("run.send_email", side_effect=AssertionError("no SMTP")), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as exit_info:
            run.main()
        device.assert_not_called()
        client.assert_not_called()
        return exit_info.exception.code, stderr.getvalue()

    def daily_argv(self, *extra):
        return ("--login-email", SYNTHETIC_EMAIL, "--password-file", str(self.password_file), *extra)

    def test_unwritable_dir_exits_2_before_any_login(self):
        blocker = self.root / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")
        code, stderr = self.run_main(*self.daily_argv("--account-alias", "account-1",
                                                      "--training-journal-dir", str(blocker / "journals")))
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("error: cannot create training journal directory"), stderr)

    def test_email_shaped_alias_exits_2_without_echo_or_login(self):
        journal_dir = self.root / "journals"
        code, stderr = self.run_main(*self.daily_argv("--account-alias", "pilot@example.com",
                                                      "--training-journal-dir", str(journal_dir)))
        self.assertEqual(code, 2)
        self.assertIn("--account-alias must be a lowercase alias", stderr)
        self.assertNotIn("pilot@example.com", stderr)
        self.assertFalse(journal_dir.exists())

    def test_empty_dir_exits_2_instead_of_journaling_into_the_working_directory(self):
        # Regression: an empty DIR (an unset variable in a workflow) was falsy, so the flag was silently ignored; read
        # as Path("") it would journal into the checkout. Refused before any login, and nothing lands in the cwd.
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        for value in ("", "  "):
            with self.subTest(value=value):
                code, stderr = self.run_main(*self.daily_argv("--account-alias", "account-1",
                                                              "--training-journal-dir", value))
                self.assertEqual(code, 2)
                self.assertIn("--training-journal-dir needs a directory", stderr)
                self.assertFalse((self.root / "account-1.jsonl").exists())
        code, stderr = self.run_main("--report", "training", "--journal", str(self.root / "j.jsonl"),
                                     "--training-journal-dir", "")
        self.assertEqual(code, 2)
        self.assertIn("--training-journal-dir is only used by the daily run", stderr)

    def test_refused_with_offline_fleet_and_read_only_commands(self):
        registry, output = str(self.root / "registry.json"), str(self.root / "out")
        cases = {
            "report": ("--report", "training", "--journal", str(self.root / "j.jsonl")),
            "progression dry run": self.daily_argv("--progression-dry-run"),
            "fleet dry run": ("--fleet-dry-run", "--registry", registry),
            "fleet execute": ("--fleet-execute", "--registry", registry, "--authorization",
                              str(self.root / "authorization.json"), "--identity-salt-env", "X"),
            "baseline": ("--baseline", "--registry", registry, "--output", output),
            "capture reads": ("--capture-reads", "--registry", registry, "--output", output),
        }
        for index, (case, argv) in enumerate(cases.items()):
            journal_dir = self.root / f"journals-{index}"
            with self.subTest(case=case):
                code, stderr = self.run_main(*argv, "--account-alias", "account-1",
                                             "--training-journal-dir", str(journal_dir))
                self.assertEqual(code, 2)
                self.assertIn("--training-journal-dir", stderr)
                self.assertFalse(journal_dir.exists())


def run_daily_with(fake: FakePss, extra_argv, around=None) -> DailyRun:
    """tests.test_daily_run_offline_e2e.run_daily with ``extra_argv`` appended and Client.manageTraining wrapped.

    ``around(client, original)`` runs in place of manageTraining and returns its result; by default it calls the
    original. Everything else (FakePss, the real Client and login, SMTP and prompts refused) is run_daily's.
    """
    tmp = tempfile.TemporaryDirectory(prefix="tachikoma-e2e-")
    cwd = os.getcwd()
    os.chdir(tmp.name)
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    steps: list[tuple[str, int]] = []
    first_exchange = len(fake.exchanges)
    original = Client.manageTraining

    def manage_training(self):
        return around(self, original) if around is not None else original(self)

    try:
        pw = Path(tmp.name, "pw.txt")
        pw.write_text(FAKE_PASSWORD + "\n", encoding="utf-8")
        argv = ["run.py", "--login-email", SYNTHETIC_EMAIL, "--password-file", str(pw), *extra_argv]
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        with ExitStack() as stack:
            stack.enter_context(fake.install())
            depth = [0]
            for name in DAILY_STEPS:
                method = manage_training if name == "manageTraining" else getattr(Client, name)
                stack.enter_context(patch.object(Client, name, _step_recorder(name, method, fake, steps, depth)))
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch.object(root, "handlers", [handler]))
            email_mock = stack.enter_context(patch("run.email_logfile",
                                                   side_effect=AssertionError("SMTP must stay off")))
            stack.enter_context(patch("run.send_email", side_effect=AssertionError("SMTP must stay off")))
            prompt = stack.enter_context(patch("getpass.getpass", side_effect=AssertionError("no prompt")))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            try:
                run.main()
                code = "returned-without-exit"
            except SystemExit as exc:
                code = exc.code
        return DailyRun(code, buf.getvalue(), fake, email_mock.call_count, prompt.call_count, steps, first_exchange,
                        len(fake.exchanges))
    finally:
        root.setLevel(old_level)
        os.chdir(cwd)
        tmp.cleanup()


def run_report(*argv):
    """run.py --report training: offline, so any connection attempt fails the test (scripts/ci.py's audit hook)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", ["run.py", "--report", "training", *argv]), \
            patch("run.Device", side_effect=AssertionError("no Device")), \
            patch("run.Client", side_effect=AssertionError("no Client")), \
            redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            run.main()
            code = "returned-without-exit"
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue() + stderr.getvalue()


TRAINING_ATTRIBUTES = frozenset({"training_journal", "training_account_alias"})


class DailyRunTrainingJournalE2ETests(unittest.TestCase):
    """The real daily path (run.main(), real Client and login, FakePss) with and without --training-journal-dir."""

    @classmethod
    def setUpClass(cls):
        # Its own directory, so the journal outlives run_daily_with's working directory. The layout mirrors the
        # workflow: $RUNNER_TEMP/tachikoma-training/journals.
        temporary = tempfile.TemporaryDirectory(prefix="tachikoma-runner-temp-")
        cls.addClassCleanup(temporary.cleanup)
        cls.runner_temp = Path(temporary.name)
        cls.journal_dir = cls.runner_temp / "tachikoma-training" / "journals"
        cls.seen = {}
        cls.fake = FakePss(default_world())
        cls.with_flag = run_daily_with(
            cls.fake,
            ["--account-alias", "account-1", "--training-journal-dir", str(cls.journal_dir)],
            around=cls.recorder("with_flag", probe=True),
        )
        cls.without_flag = run_daily_with(FakePss(default_world()), [], around=cls.recorder("without_flag"))

    @classmethod
    def recorder(cls, label, *, probe=False):
        """Record what manageTraining finds on the Client when it starts, run it, then (probe) journal one event.

        The probe stands in for Unit 9's journaling: it carries the run's real access token, device key, e-mail and
        password, so the test proves the daily path's journal holds none of them whatever manageTraining writes.
        """

        def around(client, original):
            attributes = vars(client)
            journal = attributes.get("training_journal")
            seen = cls.seen[label] = {
                "training_attributes": TRAINING_ATTRIBUTES & attributes.keys(),
                "journal": (type(journal), getattr(journal, "path", None), attributes.get("training_account_alias")),
                "device_key": client.device.key,
            }
            result = original(client)
            if probe:
                moment = FAKE_EPOCH.replace(tzinfo=timezone.utc)
                event = GameplayEvent.create(
                    account_alias=client.training_account_alias, action_id="add_training", started_at=moment,
                    finished_at=moment, outcome="verified", reason="probe journaled through the daily path",
                    attempted=True,
                    params={"character_id": "1001", "training_design_id": "3", "accessToken": client.accessToken,
                            "deviceKey": client.device.key},
                    # password= comes first: sdk.redaction's password= pattern runs to the next '&' or space, so a
                    # trailing one would swallow the rest of the JSON line and the journal would refuse the event.
                    evidence={"email": SYNTHETIC_EMAIL,
                              "request": f"/TrainingService/AddTraining?password={FAKE_PASSWORD}"
                                         f"&accessToken={client.accessToken}"},
                )
                client.training_journal.append(event)
                seen["probe_attempt_id"] = event.attempt_id
            return result

        return around

    def secrets(self):
        """Every secret the flagged run handled: the password, the e-mail, each issued token and the device key."""
        values = {FAKE_PASSWORD, SYNTHETIC_EMAIL, self.seen["with_flag"]["device_key"], *self.fake.tokens}
        self.assertTrue(all(values), values)
        return values

    def test_report_reads_the_daily_journal(self):
        code, stdout = run_report("--journal", str(self.journal_dir / "account-1.jsonl"), "--report-format", "json")
        self.assertEqual(code, 0, stdout)
        accounts = {row["account_alias"]: row for row in json.loads(stdout)["accounts"]}
        self.assertIn("account-1", accounts)
        self.assertGreaterEqual(accounts["account-1"]["totals"]["by_action"]["add_training"]["attempted"], 1)
        for secret in self.secrets():
            self.assertNotIn(secret, stdout)

    def test_report_on_untouched_journal_is_the_empty_report(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journal = run.open_training_journal(Path(temporary.name) / "journals", "account-3")
        code, stdout = run_report("--journal", str(journal.path))
        self.assertEqual(code, 0, stdout)
        self.assertIn("No training records or snapshots.", stdout)

    def test_manage_training_failure_keeps_exit_code(self):
        def failing(client, original):
            original(client)
            return False

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for extra in ([], ["--account-alias", "account-2", "--training-journal-dir", temporary.name]):
            with self.subTest(flag=bool(extra)):
                daily = run_daily_with(FakePss(default_world()), extra, around=failing)
                self.assertEqual(daily.code, 1, daily.log)

    def test_journal_written_through_the_daily_path_holds_no_secret(self):
        self.assertEqual(self.with_flag.code, 0, self.with_flag.log)
        self.assertEqual(sorted(path.name for path in self.journal_dir.iterdir()), ["account-1.jsonl"])
        events = read_events(self.journal_dir / "account-1.jsonl")
        self.assertIn(self.seen["with_flag"]["probe_attempt_id"], [event.attempt_id for event in events])
        for path in self.runner_temp.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for secret in self.secrets():
                    with self.subTest(path=path.name):
                        self.assertNotIn(secret.encode("utf-8"), data)

    def test_flag_changes_no_request_order_or_exit_code(self):
        self.assertEqual((self.with_flag.code, self.without_flag.code), (0, 0))
        self.assertIn("manageTraining", self.with_flag.step_order())
        self.assertEqual(self.with_flag.step_order(), self.without_flag.step_order())
        self.assertEqual(self.with_flag.traffic_by_step(), self.without_flag.traffic_by_step())

    def test_without_flag_client_has_no_training_attributes(self):
        # Unit 9's class defaults (no journal, the "legacy" alias) apply only while nothing overrides them.
        self.assertEqual(self.seen["without_flag"]["training_attributes"], set())

    def test_journal_and_alias_are_on_the_client_when_manage_training_starts(self):
        self.assertEqual(self.seen["with_flag"]["journal"],
                         (GameplayJournal, self.journal_dir / "account-1.jsonl", "account-1"))

    @skip_unless_dependency(yaml, "install .github/requirements-ci.txt to read the workflow")
    @unittest.skipUnless(shutil.which("bash"), "the workflow's report step is a bash script")
    def test_daily_run_journal_flows_into_the_workflow_report(self):
        # The whole chain: the daily run's journal, read by daily-run.yml's own report step, into the job summary.
        result, summary, markdown, report = run_report_step(self, self.runner_temp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## account-1", summary)
        self.assertIn("- account-1.jsonl", summary)
        (account,) = report["accounts"]
        self.assertEqual(account["account_alias"], "account-1")
        self.assertGreaterEqual(account["totals"]["by_action"]["add_training"]["attempted"], 1)
        for secret in self.secrets():
            for name, text in (("summary", summary), ("markdown", markdown), ("json", json.dumps(report)),
                               ("output", result.stdout + result.stderr)):
                with self.subTest(output=name):
                    self.assertNotIn(secret, text)


def run_report_step(test: unittest.TestCase, runner_temp: Path):
    """Run daily-run.yml's "Training report" step as GitHub runs it: bash --noprofile --norc -eo pipefail.

    It runs from a temporary directory holding a copy of run.py (run.py opens ./tachikoma.log on import), with a
    ``python`` on PATH that is this interpreter. Returns the process result, the job summary, the markdown report and
    the parsed JSON report.
    """
    documents = ci.workflow_documents(ci.ROOT)
    (job,) = documents["daily-run.yml"]["jobs"].values()
    (step,) = [step for step in job["steps"] if ci.DAILY_TRAINING_REPORT_COMMAND in step.get("run", "")]
    temporary = tempfile.TemporaryDirectory(prefix="tachikoma-report-step-")
    test.addCleanup(temporary.cleanup)
    work = Path(temporary.name)
    (work / "bin").mkdir()
    (work / "home").mkdir()
    (work / "tmp").mkdir()
    (work / "checkout").mkdir()
    shutil.copy(ci.ROOT / "run.py", work / "checkout" / "run.py")
    shim = work / "bin" / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    script = work / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    summary = work / "summary.md"
    summary.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{work / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "PYTHONPATH": str(ci.ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_STEP_SUMMARY": str(summary),
        "TMPDIR": str(work / "tmp"),
        "HOME": str(work / "home"),
    }
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=work / "checkout",
                            env=env, capture_output=True, text=True, timeout=120)
    report_dir = runner_temp / "tachikoma-training" / "report"
    markdown_path, json_path = report_dir / "training-report.md", report_dir / "training-report.json"
    markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
    report = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else None
    return result, summary.read_text(encoding="utf-8"), markdown, report


@skip_unless_dependency(yaml, "install .github/requirements-ci.txt to read the workflow")
@unittest.skipUnless(shutil.which("bash"), "the workflow's report step is a bash script")
class DailyRunReportStepTests(unittest.TestCase):
    """daily-run.yml's report step on hand-made journals. Kept out of tests/test_ci.py: the Windows smoke suite runs
    that module, and this needs bash."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tachikoma-runner-temp-")
        self.addCleanup(temporary.cleanup)
        self.runner_temp = Path(temporary.name)

    def test_no_journals_still_writes_summary_and_both_reports(self):
        result, summary, markdown, report = run_report_step(self, self.runner_temp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No training records or snapshots.", summary)
        self.assertIn("No account wrote a training journal", summary)
        self.assertIn("No training records or snapshots.", markdown)
        self.assertEqual(report["accounts"], [])

    def test_skipped_account_and_empty_journal(self):
        journals = self.runner_temp / "tachikoma-training" / "journals"
        moment = FAKE_EPOCH.replace(tzinfo=timezone.utc)
        for alias in ("account-1", "account-4"):
            run.open_training_journal(journals, alias).append(GameplayEvent.create(
                account_alias=alias, action_id="add_training", started_at=moment, finished_at=moment,
                outcome="verified", reason="synthetic", attempted=True,
                params={"character_id": "1001", "training_design_id": "3"}))
        run.open_training_journal(journals, "account-3")  # logged in, journaled nothing; account 2 was skipped
        result, summary, _, report = run_report_step(self, self.runner_temp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Journals read:\n- account-1.jsonl\n- account-3.jsonl\n- account-4.jsonl\n", summary)
        self.assertNotIn("account-2", summary)
        self.assertEqual([row["account_alias"] for row in report["accounts"]], ["account-1", "account-4"])
        for row in report["accounts"]:
            self.assertEqual(row["totals"]["by_action"]["add_training"]["attempted"], 1)

    def test_failing_report_still_lists_the_journals_and_fails_the_step(self):
        # Regression: the summary block came after both report runs, so one account's unreadable record (here a
        # finished_at without a time zone, which the report refuses with exit 2) stopped the step under set -e before
        # anything reached the job summary. The journal list now comes first; the step still fails.
        journals = self.runner_temp / "tachikoma-training" / "journals"
        moment = FAKE_EPOCH.replace(tzinfo=timezone.utc)
        journal = run.open_training_journal(journals, "account-2")
        journal.append(GameplayEvent.create(
            account_alias="account-2", action_id="add_training", started_at=moment, finished_at=moment,
            outcome="verified", reason="synthetic", attempted=True,
            params={"character_id": "1001", "training_design_id": "3"}))
        record = json.loads(journal.path.read_text(encoding="utf-8"))
        record["finished_at"] = record["finished_at"].removesuffix("+00:00")
        journal.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result, summary, _, report = run_report_step(self, self.runner_temp)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("timezone-aware", result.stderr)
        self.assertEqual(summary, "Journals read:\n- account-2.jsonl\n\n")
        self.assertIsNone(report)


if __name__ == "__main__":
    unittest.main()
