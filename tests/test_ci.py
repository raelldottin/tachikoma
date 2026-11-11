"""CI gates and release provenance tests using synthetic, temporary Git repos."""

import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import ci
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN, SYNTHETIC_DEVICE_KEY_IOS

try:
    import yaml
except ImportError:
    yaml = None


class RequiredGateTests(unittest.TestCase):
    def test_all_success_is_required(self):
        ci.require_success({"checks": {"result": "success"}, "tests": {"result": "success"}}, ["checks", "tests"])

    def test_every_other_upstream_state_fails(self):
        for result in ("failure", "cancelled", "skipped", "neutral", "", None, True):
            for failing_job in ("checks", "tests"):
                with self.subTest(result=result, job=failing_job):
                    results = {"checks": {"result": "success"}, "tests": {"result": "success"}}
                    results[failing_job]["result"] = result
                    with self.assertRaises(ValueError):
                        ci.require_success(results, ["checks", "tests"])

    def test_missing_extra_and_malformed_results_fail(self):
        for results in (
            None,
            [],
            {},
            {"checks": {"result": "success"}},
            {"checks": "success"},
            {"checks": {"result": "success"}, "extra": {"result": "success"}},
        ):
            with self.subTest(results=results), self.assertRaises(ValueError):
                ci.require_success(results, ["checks", "tests"])

    def test_empty_or_duplicate_required_names_fail(self):
        for required in ([], ["checks", "checks"]):
            with self.assertRaises(ValueError):
                ci.require_success({}, required)

    def test_cli_fails_closed_on_missing_or_invalid_json(self):
        for value in (None, "not-json", "null", '{"checks":{"result":"skipped"}}'):
            env = {} if value is None else {"NEEDS_JSON": value}
            with patch.dict(os.environ, env, clear=True), redirect_stderr(io.StringIO()):
                self.assertEqual(ci.main(["gate", "--required", "checks"]), 1)


class TestIsolationTests(unittest.TestCase):
    def test_test_modules_do_not_override_checkout_with_absolute_literals(self):
        for path in (ci.ROOT / "tests").glob("test_*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and ast.unparse(node.func) == "sys.path.insert":
                    for argument in node.args:
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            self.assertFalse(PurePosixPath(argument.value).is_absolute(), path.name)
                            self.assertFalse(PureWindowsPath(argument.value).is_absolute(), path.name)

    def test_swallowed_network_attempt_still_fails_the_test_command(self):
        program = """
import socket
import sys
import unittest
from unittest.mock import patch
from scripts import ci

class CaughtNetworkAttempt(unittest.TestCase):
    def runTest(self):
        with socket.socket() as connection:
            try:
                connection.connect(('127.0.0.1', 9))
            except RuntimeError:
                pass

with patch.object(unittest.defaultTestLoader, 'discover', return_value=unittest.TestSuite([CaughtNetworkAttempt()])):
    sys.exit(ci.run_tests([]))
"""
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ci.ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("CI rejected 1 unmocked network", result.stderr)
        self.assertIn("OK", result.stderr)


class LintRatchetTests(unittest.TestCase):
    def test_signature_includes_file_rule_and_message_but_not_line_number(self):
        root = Path(__file__).resolve().parent.parent
        findings = [
            {"filename": str(root / "run.py"), "code": "F401", "message": "unused import", "location": {"row": 1}}
        ]
        first = ci.lint_counts(findings, root)
        findings[0]["location"]["row"] = 200
        self.assertEqual(first, ci.lint_counts(findings, root))
        findings.append(dict(findings[0]))
        self.assertEqual(ci.lint_counts(findings, root)[("run.py", "F401", "unused import")], 2)

    def test_invalid_baseline_counts_and_duplicates_fail(self):
        entry = {"path": "run.py", "code": "F401", "message": "unused", "count": 1}
        for count in (0, -1, True, "1"):
            with self.assertRaises(ValueError):
                ci.lint_baseline({"schema_version": 1, "violations": [{**entry, "count": count}]})
        with self.assertRaises(ValueError):
            ci.lint_baseline({"schema_version": 1, "violations": [entry, entry]})

    def test_new_lint_and_stale_allowances_both_fail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".github").mkdir()
            baseline = root / ".github/ruff-baseline.json"
            finding = {"filename": str(root / "run.py"), "code": "F401", "message": "unused"}
            baseline.write_text(json.dumps({"schema_version": 1, "violations": []}), encoding="utf-8")
            result = subprocess.CompletedProcess([], 1, stdout=json.dumps([finding]), stderr="")
            with patch.object(ci.subprocess, "run", return_value=result), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "lint findings changed"):
                    ci.check_lint(root)
            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "violations": [
                            {"path": "run.py", "code": "F401", "message": "unused", "count": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
            with patch.object(ci.subprocess, "run", return_value=result), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "lint findings changed"):
                    ci.check_lint(root)

    def test_tool_failure_cannot_be_treated_as_no_findings(self):
        result = subprocess.CompletedProcess([], 2, stdout="", stderr="configuration error")
        with patch.object(ci.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "Ruff could not run"):
                ci.check_lint(ci.ROOT)


@unittest.skipUnless(yaml, "install .github/requirements-ci.txt for workflow checks")
class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.documents = ci.workflow_documents(ci.ROOT)

    def test_actual_workflows_pass_policy(self):
        ci.check_workflows(self.documents)

    def test_pull_requests_keep_github_default_merge_checkout(self):
        for filename in ("blocking-ci.yml", "automation-check.yml", "python-tests.yml"):
            for job in self.documents[filename]["jobs"].values():
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/checkout@"):
                        self.assertNotIn("ref", step.get("with", {}))

    def test_merge_gate_cannot_be_skipped_or_filter_out_changes(self):
        for mutation in ("condition", "dependency", "paths", "merge_queue"):
            docs = copy.deepcopy(self.documents)
            if mutation == "condition":
                docs["blocking-ci.yml"]["jobs"]["required"]["if"] = "success()"
            elif mutation == "dependency":
                docs["blocking-ci.yml"]["jobs"]["required"]["needs"] = ["tests"]
            elif mutation == "paths":
                docs["blocking-ci.yml"]["on"]["pull_request"]["paths-ignore"] = ["docs/**"]
            else:
                del docs["blocking-ci.yml"]["on"]["merge_group"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci.check_workflows(docs)

    def test_mutable_action_or_persisted_token_fails(self):
        for field, value in (("uses", "actions/checkout@v6"), ("with", {"persist-credentials": "true"})):
            docs = copy.deepcopy(self.documents)
            docs["blocking-ci.yml"]["jobs"]["required"]["steps"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ci.check_workflows(docs)

    def test_only_release_publisher_has_write_permissions(self):
        for filename, document in self.documents.items():
            for name, job in document["jobs"].items():
                if job.get("permissions", {}).get("contents") == "write":
                    self.assertEqual((filename, name), ("release.yml", "publish"))
        publish = self.documents["release.yml"]["jobs"]["publish"]
        self.assertEqual(publish["needs"], "verify")
        self.assertFalse(any("checkout@" in step.get("uses", "") for step in publish["steps"]))
        command = publish["steps"][-1]["run"]
        self.assertIn("--draft", command)
        self.assertIn("--verify-tag", command)

    def test_manual_release_cannot_publish_or_bypass_artifact_verification(self):
        for mutation in (
            "publish_condition",
            "publish_dependency",
            "build_condition",
            "verify_dependency",
            "permissions",
        ):
            docs = copy.deepcopy(self.documents)
            jobs = docs["release.yml"]["jobs"]
            if mutation == "publish_condition":
                jobs["publish"].pop("if")
            elif mutation == "publish_dependency":
                jobs["publish"]["needs"] = "build"
            elif mutation == "build_condition":
                jobs["build"].pop("if")
            elif mutation == "verify_dependency":
                jobs["verify"]["needs"] = "checks"
            else:
                jobs["verify"]["permissions"] = {"contents": "write"}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                ci.check_workflows(docs)

    def test_manual_release_uses_only_a_local_synthetic_tag(self):
        release = self.documents["release.yml"]
        self.assertIn("workflow_dispatch", release["on"])
        steps = release["jobs"]["build"]["steps"]
        tag_step = next(step for step in steps if step.get("name") == "Create local verification tag")
        self.assertEqual(tag_step["if"], "${{ github.event_name == 'workflow_dispatch' }}")
        self.assertEqual(tag_step["run"], 'git tag 0.0.0 "$GITHUB_SHA"')
        build_step = next(
            step for step in steps if step.get("name") == "Validate tag and build reproducible source archive"
        )
        self.assertEqual(
            build_step["env"]["RELEASE_TAG"],
            "${{ github.event_name == 'workflow_dispatch' && '0.0.0' || github.ref_name }}",
        )

    def test_downloaded_manifest_must_match_workflow_identity(self):
        steps = self.documents["release.yml"]["jobs"]["verify"]["steps"]
        script = next(step["run"] for step in steps if step.get("name") == "Verify release identity")
        code = "\n".join(script.splitlines()[1:-1])
        for tag in ("0.0.0", "v1.2.3"):
            expected = {"tag": tag, "version": tag.removeprefix("v"), "commit": "abc123"}
            for field in (None, "tag", "version", "commit", "missing"):
                with self.subTest(tag=tag, field=field), TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "dist").mkdir()
                    manifest = expected.copy()
                    if field == "missing":
                        manifest.pop("commit")
                    elif field:
                        manifest[field] = "wrong"
                    (root / "dist/release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        cwd=root,
                        env={**os.environ, "GITHUB_SHA": "abc123", "RELEASE_TAG": tag},
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0 if field is None else 1, result.stderr)

    def test_validation_does_not_call_live_game_or_request_secrets(self):
        for filename in ("blocking-ci.yml", "automation-check.yml", "python-tests.yml", "postmerge-ci.yml"):
            text = json.dumps(self.documents[filename])
            for forbidden in ("secrets.", "secrets: inherit", "--run-battle", "pss_langgraph_automation"):
                self.assertNotIn(forbidden, text)


@unittest.skipUnless(shutil.which("git"), "release provenance tests require Git")
class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.output = Path(self.temporary.name) / "dist"
        ci.git(self.root, "init", "-b", "main")
        ci.git(self.root, "config", "user.name", "CI Test")
        ci.git(self.root, "config", "user.email", "ci@example.invalid")
        ci.git(self.root, "config", "commit.gpgsign", "false")
        ci.git(self.root, "config", "core.autocrlf", "false")
        ci.git(self.root, "config", "core.hooksPath", str(self.root / "no-hooks"))
        files = {
            "run.py": "print('synthetic application')\n",
            "requirements.txt": "requests==2.28.2\n",
            "LICENSE": "Synthetic fixture license\n",
            "README.md": "Synthetic fixture readme\n",
            "sdk/client.py": "class Client: pass\n",
            "sdk/inventory/example.py": "VALUE = 1\n",
            "sdk/.device": "synthetic private device fixture\n",
            "run_log.txt": "synthetic private log fixture\n",
        }
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.commit("initial fixture")
        ci.git(self.root, "tag", "1.2.3")

    def commit(self, message):
        ci.git(self.root, "add", ".")
        ci.git(self.root, "commit", "-m", message)

    def build(self, tag="1.2.3"):
        return ci.build_release(self.root, tag, self.output, "main")

    def test_release_is_reproducible_and_checksums_cover_every_artifact(self):
        manifest = self.build()
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["commit"], ci.git(self.root, "rev-parse", "HEAD").decode().strip())
        original = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.build()
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.output.iterdir()})
        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            digest, filename = line.split("  ")
            self.assertEqual(hashlib.sha256((self.output / filename).read_bytes()).hexdigest(), digest)

    def test_archive_excludes_logs_devices_untracked_and_dirty_contents(self):
        (self.root / "run.py").write_text("DIRTY_SYNTHETIC_SENTINEL\n", encoding="utf-8")
        (self.root / "passwords.txt").write_text("UNTRACKED_SYNTHETIC_SENTINEL\n", encoding="utf-8")
        self.build()
        with tarfile.open(self.output / "tachikoma-1.2.3.tar.gz") as archive:
            names = archive.getnames()
            self.assertEqual(names, sorted(names))
            self.assertIn("tachikoma-1.2.3/sdk/inventory/example.py", names)
            self.assertEqual(len(names), 6)
            contents = b"".join(archive.extractfile(name).read() for name in names)
            self.assertNotIn(b"SYNTHETIC_SENTINEL", contents)
            self.assertFalse(any(".device" in name or "log" in name or "password" in name for name in names))

    def test_prefixed_and_annotated_tags_are_supported(self):
        ci.git(self.root, "tag", "-a", "v1.2.3", "-m", "synthetic annotated release")
        self.assertEqual(self.build("v1.2.3")["version"], "1.2.3")

    def test_malformed_or_option_like_tags_are_rejected_before_git(self):
        for tag in ("1.2", "v1.2.3-rc1", "01.2.3", "v1.2.3;echo injected", "--help", "1.2.3\n"):
            with self.subTest(tag=tag), patch.object(ci, "git") as mocked:
                with self.assertRaises(ValueError):
                    self.build(tag)
                mocked.assert_not_called()

    def test_tag_must_match_checked_out_commit(self):
        (self.root / "run.py").write_text("print('new commit')\n", encoding="utf-8")
        self.commit("next fixture")
        with self.assertRaisesRegex(ValueError, "checked-out commit"):
            self.build()

    def test_release_must_already_be_on_main(self):
        ci.git(self.root, "switch", "-c", "candidate")
        (self.root / "run.py").write_text("print('unmerged')\n", encoding="utf-8")
        self.commit("unmerged fixture")
        ci.git(self.root, "tag", "1.2.4")
        with self.assertRaises(subprocess.CalledProcessError):
            self.build("1.2.4")

    def test_source_symlink_is_rejected_without_dereferencing_it(self):
        object_id = ci.git(self.root, "hash-object", "run.py").decode().strip()
        ci.git(self.root, "update-index", "--add", "--cacheinfo", f"120000,{object_id},sdk/linked.py")
        ci.git(self.root, "commit", "-m", "synthetic symlink fixture")
        ci.git(self.root, "tag", "1.2.4")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.build("1.2.4")

    def test_missing_required_application_source_refuses_release(self):
        ci.git(self.root, "rm", "sdk/client.py")
        ci.git(self.root, "commit", "-m", "remove fixture client")
        ci.git(self.root, "tag", "1.2.4")
        with self.assertRaisesRegex(ValueError, "missing required"):
            self.build("1.2.4")

    def test_blob_policy_rejects_oversized_tracked_file(self):
        (self.root / "large.bin").write_bytes(b"0" * (ci.MAX_BLOB_BYTES + 1))
        ci.git(self.root, "add", "large.bin")
        with patch.object(ci, "workflow_documents", return_value={}), patch.object(ci, "check_workflows"):
            with self.assertRaisesRegex(ValueError, "blob policy"):
                ci.check_repository(self.root)


@unittest.skipUnless(shutil.which("git"), "release provenance tests require Git")
class ReleasePackageDataTests(unittest.TestCase):
    """Package data under sdk/ ships in the archive; tests, captures and secrets do not."""

    LOADER = (
        "import json\n"
        "from pathlib import Path\n"
        "def load(name):\n"
        "    path = Path(__file__).resolve().parent / 'scenarios' / f'{name}.json'\n"
        "    return json.loads(path.read_text(encoding='utf-8'))\n"
    )

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        ci.git(self.root, "init", "-b", "main")
        ci.git(self.root, "config", "user.name", "CI Test")
        ci.git(self.root, "config", "user.email", "ci@example.invalid")
        ci.git(self.root, "config", "commit.gpgsign", "false")
        ci.git(self.root, "config", "core.autocrlf", "false")
        ci.git(self.root, "config", "core.hooksPath", str(self.root / "no-hooks"))
        self.write(
            {
                "run.py": "print('synthetic application')\n",
                "requirements.txt": "requests==2.28.2\n",
                "LICENSE": "Synthetic fixture license\n",
                "README.md": "Synthetic fixture readme\n",
                "sdk/__init__.py": "",
                "sdk/client.py": "class Client: pass\n",
                "sdk/simulation/__init__.py": "",
                "sdk/simulation/loader.py": self.LOADER,
                "sdk/simulation/scenarios/alpha.json": '{"name": "alpha", "seed": 7}\n',
                "sdk/simulation/scenarios/beta.json": '{"name": "beta", "seed": 11}\n',
                "sdk/notes.txt": "PRIVATE_SYNTHETIC_SENTINEL notes\n",
                "sdk/data.bin": "PRIVATE_SYNTHETIC_SENTINEL binary\n",
                "sdk/tests/fixture.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
                "sdk/tests/test_client.py": "PRIVATE_SYNTHETIC_SENTINEL = 1\n",
                "tests/test_app.py": "PRIVATE_SYNTHETIC_SENTINEL = 1\n",
                "tests/fixture.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
                "docs/scenario.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
                "scripts/tool.py": "PRIVATE_SYNTHETIC_SENTINEL = 1\n",
                "sdk/.device": "PRIVATE_SYNTHETIC_SENTINEL device\n",
                "captures/session.json": '{"sentinel": "PRIVATE_SYNTHETIC_SENTINEL"}\n',
            }
        )
        self.release("1.2.3")

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")

    def release(self, tag):
        ci.git(self.root, "add", ".")
        ci.git(self.root, "commit", "-m", f"fixture {tag}")
        ci.git(self.root, "tag", tag)

    def build(self, tag="1.2.3", output="dist"):
        return ci.build_release(self.root, tag, Path(self.temporary.name) / output, "main")

    def archive_path(self, tag="1.2.3", output="dist"):
        return Path(self.temporary.name) / output / f"tachikoma-{tag}.tar.gz"

    def test_sdk_json_ships_and_excluded_kinds_stay_out(self):
        manifest = self.build()
        with tarfile.open(self.archive_path()) as archive:
            names = archive.getnames()
            contents = b"".join(archive.extractfile(name).read() for name in names)
        prefix = "tachikoma-1.2.3/"
        self.assertEqual(
            names,
            [
                prefix + "LICENSE",
                prefix + "README.md",
                prefix + "requirements.txt",
                prefix + "run.py",
                prefix + "sdk/__init__.py",
                prefix + "sdk/client.py",
                prefix + "sdk/simulation/__init__.py",
                prefix + "sdk/simulation/loader.py",
                prefix + "sdk/simulation/scenarios/alpha.json",
                prefix + "sdk/simulation/scenarios/beta.json",
            ],
        )
        self.assertEqual(manifest["files"], len(names))
        self.assertNotIn(b"PRIVATE_SYNTHETIC_SENTINEL", contents)

    def test_archive_with_package_data_is_byte_reproducible(self):
        self.build(output="first")
        self.build(output="second")
        first, second = Path(self.temporary.name) / "first", Path(self.temporary.name) / "second"
        for name in ("tachikoma-1.2.3.tar.gz", "release-manifest.json", "SHA256SUMS"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)
        raw = self.archive_path(output="first").read_bytes()
        self.assertEqual(raw[4:8], b"\0\0\0\0", "gzip header must not carry a timestamp")
        epoch = int(ci.git(self.root, "show", "-s", "--format=%ct", "HEAD"))
        with tarfile.open(self.archive_path(output="first")) as archive:
            for info in archive.getmembers():
                with self.subTest(member=info.name):
                    self.assertTrue(info.isfile())
                    self.assertEqual((info.mtime, info.uid, info.gid, info.uname, info.gname), (epoch, 0, 0, "", ""))
                    self.assertEqual(info.mode, 0o644)

    def test_scenario_loads_from_the_extracted_archive(self):
        self.build()
        extracted = Path(self.temporary.name) / "extracted"
        with tarfile.open(self.archive_path()) as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(extracted, filter="data")
            else:  # Python builds without extraction filters; members are regular files only.
                archive.extractall(extracted)
        program = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from sdk.simulation.loader import load\n"
            "print(load('alpha')['seed'], load('beta')['seed'])\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", program, str(extracted / "tachikoma-1.2.3")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "7 11")

    def test_private_looking_package_data_refuses_the_release(self):
        names = (
            "sdk/secrets.json",
            "sdk/config/auth.json",
            "sdk/captures/alpha.json",
            "sdk/simulation/run_log.json",
            "sdk/device-token.json",
            "sdk/.profiles/alpha.json",
            "sdk/Session.json",
        )
        for index, name in enumerate(names, start=4):
            with self.subTest(name=name):
                tag = f"1.2.{index}"
                self.write({name: '{"value": "synthetic"}\n'})
                self.release(tag)
                with self.assertRaisesRegex(ValueError, "looks like a secret, log or capture"):
                    self.build(tag, output=f"refused-{index}")
                self.assertFalse((Path(self.temporary.name) / f"refused-{index}").exists())
                ci.git(self.root, "rm", "-q", name)
                ci.git(self.root, "commit", "-q", "-m", f"remove {name}")

    def test_invalid_json_package_data_refuses_the_release(self):
        self.write({"sdk/simulation/scenarios/broken.json": "{not json\n"})
        self.release("1.2.4")
        with self.assertRaisesRegex(ValueError, "not valid UTF-8 JSON"):
            self.build("1.2.4", output="broken")
        self.assertFalse((Path(self.temporary.name) / "broken").exists())

    def test_member_selection_allow_list(self):
        for path, expected in (
            ("run.py", True),
            ("sdk/client.py", True),
            ("sdk/device.py", True),
            ("sdk/simulation/scenarios/two-accounts.json", True),
            ("sdk/actions/catalog.json", True),
            ("sdk/notes.md", False),
            ("sdk/image.png", False),
            ("sdk/.device", False),
            ("sdk/tests/fixture.json", False),
            ("sdk/tests/test_client.py", False),
            ("sdk/__pycache__/client.cpython-313.pyc", False),
            ("tests/test_ci.py", False),
            ("docs/RELEASE.md", False),
            ("captures/session.json", False),
            ("scripts/ci.py", False),
        ):
            with self.subTest(path=path):
                self.assertIs(ci.release_includes(path), expected)


@unittest.skipUnless(shutil.which("git") and (ci.ROOT / ".git").exists(), "requires the tachikoma Git checkout")
class RepositoryReleaseSelectionTests(unittest.TestCase):
    def test_tracked_scenarios_are_selected_and_no_tracked_sdk_file_is_refused(self):
        tracked = [os.fsdecode(name) for name in ci.git(ci.ROOT, "ls-files", "-z").split(b"\0") if name]
        selected = {path for path in tracked if ci.release_includes(path)}
        scenarios = {path for path in tracked if PurePosixPath(path).match("sdk/simulation/scenarios/*.json")}
        self.assertTrue(scenarios, "the bundled simulation scenarios must be tracked")
        self.assertLessEqual(scenarios, selected)
        self.assertIn("sdk/simulation/game.py", selected)
        self.assertFalse({path for path in selected if path.startswith(("tests/", "docs/", "automation/"))})
        for path in sorted(path for path in selected if path.startswith("sdk/") and path.endswith(".json")):
            with self.subTest(path=path):
                json.loads((ci.ROOT / path).read_text(encoding="utf-8"))


SENTINEL_PROBE = """
import os
import unittest

import run
from sdk.device import Device


class SentinelProbe(unittest.TestCase):
    def test_writes_device_and_log_in_the_working_directory(self):
        Device(language="en")
        run.logging.getLogger().info("sentinel probe")
        self.assertTrue(os.path.isfile(".device"))
        self.assertTrue(os.path.isfile("tachikoma.log"))
"""
SENTINEL_MTIME_NS = 1_600_000_000_000_000_000
# The Makefile recipes use mktemp, trap and a POSIX shell; Windows runs only scripts/ci.py test.
REQUIRES_POSIX_MAKE = unittest.skipUnless(
    os.name == "posix" and shutil.which("make"), "requires make and a POSIX shell"
)


class CredentialFileSafetyTests(unittest.TestCase):
    """Runs the test entry points in a scratch copy of the repository whose root holds sentinel credential files."""

    def setUp(self):
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "repo"
        self.temp = Path(directory.name).resolve() / "tmp"
        self.temp.mkdir()
        shutil.copytree(ci.ROOT / "sdk", self.root / "sdk", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        for name in ("run.py", "Makefile", "scripts/ci.py", "tests/__init__.py"):
            shutil.copy2(ci.ROOT / name, self.root / name)
        # The probe writes ./.device and ./tachikoma.log in its working directory, as the real device and
        # run.py tests do. Its name matches both `make test` (test_*.py) and `make test-security` (test_security*.py).
        (self.root / "tests" / "test_security_sentinel_probe.py").write_text(SENTINEL_PROBE, encoding="utf-8")
        self.sentinels = {
            ".device": f"Android|{SYNTHETIC_DEVICE_KEY_IOS}|sentinel-refresh|en|{SYNTHETIC_ACCESS_TOKEN}|1".encode(),
            "tachikoma.log": b"sentinel log line\n",
        }
        for name, content in self.sentinels.items():
            path = self.root / name
            path.write_bytes(content)
            os.utime(path, ns=(SENTINEL_MTIME_NS, SENTINEL_MTIME_NS))
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONSAFEPATH", "MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
        }
        # Every temporary directory the commands create lands here, so the test can see that it was removed.
        self.env.update(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.temp), TEMP=str(self.temp), TMP=str(self.temp))

    def run_in_root(self, command):
        return subprocess.run(command, cwd=self.root, env=self.env, capture_output=True, text=True, timeout=120)

    def assert_sentinels_untouched(self):
        for name, content in self.sentinels.items():
            path = self.root / name
            self.assertEqual(path.read_bytes(), content, name)
            self.assertEqual(path.stat().st_mtime_ns, SENTINEL_MTIME_NS, name)

    def assert_probe_ran_and_temp_dir_removed(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertEqual(list(self.temp.iterdir()), [], "the temporary working directory must be removed")

    # Verifies: S02-R1, S02-R2
    def test_ci_test_command_leaves_root_credential_files_untouched(self):
        for names in ([], ["--names", "tests.test_security_sentinel_probe"]):
            with self.subTest(names=names):
                result = self.run_in_root([sys.executable, "scripts/ci.py", "test", *names])
                self.assert_probe_ran_and_temp_dir_removed(result)
                self.assert_sentinels_untouched()

    # Verifies: S02-R1
    def test_run_tests_uses_a_fresh_temporary_directory_and_restores_the_working_directory(self):
        program = """
import os
import unittest
from unittest.mock import patch
from scripts import ci

seen = []

class RecordWorkingDirectory(unittest.TestCase):
    def runTest(self):
        seen.append(os.getcwd())
        self.assertEqual(os.listdir("."), [])

with patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite([RecordWorkingDirectory()])):
    code = ci.run_tests([])
print(repr((code, seen[0], os.getcwd(), os.path.exists(seen[0]))))
"""
        result = self.run_in_root([sys.executable, "-c", program])
        self.assertEqual(result.returncode, 0, result.stderr)
        code, seen, after, still_exists = ast.literal_eval(result.stdout.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(Path(after).resolve(), self.root)
        self.assertEqual(Path(seen).resolve().parent, self.temp)
        self.assertFalse(still_exists)

    # Verifies: S02-R1
    def test_log_files_opened_in_the_temporary_directory_are_closed_so_it_can_be_removed(self):
        inside = logging.FileHandler(self.temp / "tachikoma.log", delay=False)
        outside = logging.FileHandler(self.root / "outside.log", delay=False)
        for handler in (inside, outside):
            logging.root.addHandler(handler)
        self.addCleanup(outside.close)
        self.addCleanup(logging.root.removeHandler, outside)
        self.addCleanup(logging.root.removeHandler, inside)
        ci.close_root_log_files_under(self.temp)
        self.assertNotIn(inside, logging.root.handlers)
        self.assertIsNone(inside.stream)
        self.assertIn(outside, logging.root.handlers)
        self.assertIsNotNone(outside.stream)

    # Verifies: S02-R1, S02-R2
    @REQUIRES_POSIX_MAKE
    def test_make_test_targets_leave_root_credential_files_untouched(self):
        for target in ("test", "test-security"):
            with self.subTest(target=target):
                result = self.run_in_root(["make", target, f"PYTHON={sys.executable}"])
                self.assert_probe_ran_and_temp_dir_removed(result)
                self.assert_sentinels_untouched()

    # Verifies: S02-R1
    @REQUIRES_POSIX_MAKE
    def test_make_test_recipes_use_an_absolute_interpreter_and_a_temporary_directory(self):
        venv_python = self.root / ".venv" / "bin" / "python"
        cases = [((f"PYTHON={sys.executable}",), sys.executable), (("PYTHON=.venv/bin/python",), str(venv_python))]
        if shutil.which("python3", path=self.env.get("PATH")):
            cases.append(((), None))
        for arguments, expected in cases:
            for target in ("test", "test-security"):
                with self.subTest(arguments=arguments, target=target):
                    result = self.run_in_root(["make", "-n", target, *arguments])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recipe = result.stdout
                    self.assertIn("mktemp -d", recipe)
                    self.assertIn("trap ", recipe)
                    self.assertIn(f'-s "{self.root / "tests"}" -t "{self.root}"', recipe)
                    interpreter = recipe.split('cd "$tmp" && "', 1)[1].split('"', 1)[0]
                    self.assertTrue(os.path.isabs(interpreter), interpreter)
                    if expected is not None:
                        self.assertEqual(interpreter, expected)
        # With no PYTHON given, an executable .venv interpreter is preferred, by its absolute path.
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        venv_python.chmod(0o755)
        result = self.run_in_root(["make", "-n", "test"])
        self.assertIn(f'cd "$tmp" && "{venv_python}" -m unittest discover', result.stdout)

    # Verifies: S02-R3
    def test_repository_root_unittest_run_is_refused_while_device_exists(self):
        result = self.run_in_root([sys.executable, "-m", "unittest", "tests.test_security_sentinel_probe"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python scripts/ci.py test --names tests.<module>", result.stderr)
        self.assertNotIn("Ran ", result.stderr)
        self.assert_sentinels_untouched()

    # Verifies: S02-R3
    def test_guard_allows_a_repository_root_run_without_device(self):
        (self.root / ".device").unlink()
        result = self.run_in_root([sys.executable, "-m", "unittest", "tests.test_security_sentinel_probe"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
