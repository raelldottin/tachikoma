"""CI gates and release provenance tests using synthetic, temporary Git repos."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import ci

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
        self.assertEqual(publish["needs"], "build")
        self.assertFalse(any("checkout@" in step.get("uses", "") for step in publish["steps"]))
        command = publish["steps"][-1]["run"]
        self.assertIn("--draft", command)
        self.assertIn("--verify-tag", command)

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
