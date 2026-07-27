from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from automation.context.build_context import build_context_bundle
from automation.supervisor import policy
from automation.supervisor.run_next import format_agent_command, make_decision_report, render_prompt


class AutomationHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.queue_schema_path = self.repo_root / "automation/schemas/slice.schema.json"
        self.handoff_schema_path = self.repo_root / "automation/schemas/handoff.schema.json"
        self.example_queue_path = self.repo_root / "automation/examples/example-slices.json"
        self.example_handoff_path = self.repo_root / "automation/examples/example-handoff.json"

    def require_slice_record(self, queue_data: dict[str, Any], slice_id: str) -> dict[str, Any]:
        slice_record = policy.find_slice(queue_data, slice_id)
        if slice_record is None:
            self.fail(f"Expected test fixture to contain slice {slice_id!r}.")
        return slice_record

    def require_selected_slice(self, queue_data: dict[str, Any]) -> dict[str, Any]:
        slice_record = policy.select_next_slice(queue_data)
        if slice_record is None:
            self.fail("Expected test fixture to have an eligible selected slice.")
        return slice_record

    def example_queue_data(self) -> dict[str, Any]:
        return policy.load_queue(self.example_queue_path, self.queue_schema_path)

    def example_slice_at(self, index: int) -> dict[str, Any]:
        return self.example_queue_data()["slices"][index]

    def example_slice_id_at(self, index: int) -> str:
        return self.example_slice_at(index)["slice_id"]

    def example_handoff_filename(self) -> str:
        return f"20260421T153000Z-{self.example_slice_id_at(0)}.json"

    def example_allowed_file(self, slice_record: dict[str, Any], filename: str = "Changed.swift") -> str:
        for allowed_path in slice_record["allowed_paths"]:
            if allowed_path.endswith("/"):
                return f"{allowed_path}{filename}"
        return slice_record["allowed_paths"][0]

    def example_markdown_path(self, slice_record: dict[str, Any]) -> str:
        for allowed_path in slice_record["allowed_paths"]:
            if allowed_path.endswith(".md"):
                return allowed_path
        return f"docs/product/domains/{slice_record['domain']}.md"

    def test_example_queue_matches_schema(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_schema = policy.load_schema(self.queue_schema_path)
        validation = policy.validate_document(queue_data, queue_schema)
        self.assertTrue(validation.is_valid, validation.errors)
        self.assertEqual([], policy.validate_queue_integrity(queue_data))

    def test_queue_schema_accepts_parked_slice_with_entry_condition(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"][0]["status"] = "deferred"
        queue_data["slices"][0]["entry_condition"] = "External reviewer input exists."
        queue_schema = policy.load_schema(self.queue_schema_path)

        validation = policy.validate_document(queue_data, queue_schema)

        self.assertTrue(validation.is_valid, validation.errors)
        self.assertEqual([], policy.validate_queue_integrity(queue_data))

    def test_queue_integrity_rejects_parked_slice_without_entry_condition(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"][0]["status"] = "blocked"
        queue_data["slices"][0].pop("entry_condition", None)

        errors = policy.validate_queue_integrity(queue_data)

        self.assertTrue(any("missing an explicit entry_condition" in error for error in errors), errors)

    def test_queue_integrity_rejects_unknown_recommended_unblocker(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"][0]["status"] = "blocked"
        queue_data["slices"][0]["entry_condition"] = "External reviewer input exists."
        queue_data["slices"][0]["recommended_unblocker"] = "missing-unblocker"

        errors = policy.validate_queue_integrity(queue_data)

        self.assertTrue(any("recommends unknown unblocker" in error for error in errors), errors)

    def test_blocked_slice_reports_surface_entry_condition_and_unblocker(self) -> None:
        queue_data = policy.load_json(self.example_queue_path)
        queue_data["slices"].append(
            {
                "slice_id": "review-packet",
                "title": "Prepare review packet",
                "status": "done",
                "priority": 1,
                "domain": "localization",
                "allowed_paths": ["docs/"],
                "required_validations": ["make architecture"],
                "depends_on": [],
                "max_files_changed": 1,
                "notes": "",
            }
        )
        queue_data["slices"][0]["status"] = "blocked"
        queue_data["slices"][0]["entry_condition"] = "Reviewed values exist."
        queue_data["slices"][0]["recommended_unblocker"] = "review-packet"

        reports = policy.blocked_slice_reports(queue_data)

        self.assertEqual(1, len(reports))
        self.assertEqual(self.example_slice_id_at(0), reports[0].slice_id)
        self.assertEqual("Reviewed values exist.", reports[0].entry_condition)
        self.assertEqual("review-packet", reports[0].recommended_unblocker)

    def test_example_handoff_matches_schema(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff_schema = policy.load_schema(self.handoff_schema_path)
        validation = policy.validate_document(handoff, handoff_schema)
        self.assertTrue(validation.is_valid, validation.errors)

    def test_handoff_schema_accepts_open_questions(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["open_questions"] = ["Should the empty-state copy be localized in this slice?"]
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertTrue(validation.is_valid, validation.errors)

    def test_phase_reference_fragments_exist_and_are_nonempty(self) -> None:
        prompts_dir = self.repo_root / "automation/prompts"
        for fragment in (
            "intake.md",
            "triage.md",
            "design.md",
            "tdd.md",
            "diagnose.md",
            "resolve-conflicts.md",
        ):
            path = prompts_dir / fragment
            self.assertTrue(path.is_file(), f"missing fragment: {fragment}")
            self.assertTrue(path.read_text(encoding="utf-8").strip(), f"empty fragment: {fragment}")

    def test_handoff_schema_accepts_valid_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["proof_level"] = "running-app-smoke"
        handoff["missing_proof_levels"] = ["flow-verified", "screenshot-verified"]
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertTrue(validation.is_valid, validation.errors)

    def test_handoff_schema_rejects_missing_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff.pop("proof_level")
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(any("missing required property 'proof_level'" in error for error in validation.errors), validation.errors)

    def test_handoff_schema_rejects_invalid_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["proof_level"] = "verified-in-simulator"
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.proof_level" in error and "verified-in-simulator" in error for error in validation.errors), validation.errors
        )

    def test_handoff_schema_rejects_invalid_missing_proof_level(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["missing_proof_levels"] = ["manual-vibes"]
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.missing_proof_levels[0]" in error and "manual-vibes" in error for error in validation.errors),
            validation.errors,
        )

    def test_handoff_schema_rejects_missing_residual_risks(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff.pop("residual_risks")
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("missing required property 'residual_risks'" in error for error in validation.errors), validation.errors
        )

    def test_handoff_schema_rejects_empty_residual_risks(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["residual_risks"] = []
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.residual_risks" in error and "expected at least 1 items" in error for error in validation.errors),
            validation.errors,
        )

    def test_handoff_schema_rejects_missing_contract_status_changes(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff.pop("contract_status_changes")
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("missing required property 'contract_status_changes'" in error for error in validation.errors), validation.errors
        )

    def test_handoff_schema_rejects_invalid_repo_clean_status(self) -> None:
        handoff = policy.load_json(self.example_handoff_path)
        handoff["repo_clean_status"] = "probably-clean"
        handoff_schema = policy.load_schema(self.handoff_schema_path)

        validation = policy.validate_document(handoff, handoff_schema)

        self.assertFalse(validation.is_valid)
        self.assertTrue(
            any("$.repo_clean_status" in error and "probably-clean" in error for error in validation.errors), validation.errors
        )

    def test_live_queue_configures_repo_owned_agent_command(self) -> None:
        queue_path = self.repo_root / "automation/queue/slices.json"
        if not queue_path.exists():
            self.skipTest("consumer live queue is not present in this checkout")
        queue_data = policy.load_queue(queue_path, self.queue_schema_path)
        command_template = queue_data["policy"]["agent_command_template"]

        self.assertIn("automation/supervisor/run_hermes.sh", command_template)
        self.assertIn("{repo_root}", command_template)
        self.assertIn("{prompt_file}", command_template)
        self.assertIn("{context_file}", command_template)
        self.assertIn("{handoff_file}", command_template)
        self.assertIn("{slice_id}", command_template)
        script_path = self.repo_root / "automation/supervisor/run_hermes.sh"
        self.assertTrue(script_path.exists())
        self.assertTrue(os.access(script_path, os.X_OK))

    def test_agent_wrapper_auto_selects_claude_for_claude_code_context(self) -> None:
        script_path = self.repo_root / "automation/supervisor/run_hermes.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            repo_root.mkdir()
            (repo_root / ".git").mkdir()
            prompt_path = temp_path / "prompt.md"
            prompt_path.write_text("slice prompt", encoding="utf-8")
            context_path = temp_path / "context.json"
            context_path.write_text("{}", encoding="utf-8")
            handoff_path = temp_path / "handoff.json"
            capture_path = temp_path / "capture.txt"
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(
                bin_dir / "claude",
                """#!/usr/bin/env bash
{
  for arg in "$@"; do printf 'arg:%s\\n' "$arg"; done
  printf 'cwd:%s\\n' "$PWD"
  printf 'context:%s\\n' "${REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE:-}"
  printf 'handoff:%s\\n' "${REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE:-}"
  printf 'slice:%s\\n' "${REPO_AUTOMATION_SUPERVISOR_SLICE_ID:-}"
  printf 'legacy_context:%s\\n' "${OWLORY_SUPERVISOR_CONTEXT_FILE:-}"
  printf 'stdin:'
  cat
  printf '\\n'
} > "$CAPTURE_FILE"
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["CAPTURE_FILE"] = str(capture_path)
            env["CLAUDE_CODE"] = "1"
            env.pop("REPO_AUTOMATION_AGENT_RUNNER", None)

            result = subprocess.run(
                [
                    str(script_path),
                    "--repo-root",
                    str(repo_root),
                    "--prompt-file",
                    str(prompt_path),
                    "--context-file",
                    str(context_path),
                    "--handoff-file",
                    str(handoff_path),
                    "--slice-id",
                    "slice-a",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            capture = capture_path.read_text(encoding="utf-8")
            self.assertIn("arg:--print", capture)
            self.assertIn("arg:--input-format", capture)
            self.assertIn("arg:text", capture)
            self.assertIn("arg:--no-session-persistence", capture)
            self.assertIn("arg:--permission-mode", capture)
            self.assertIn("arg:bypassPermissions", capture)
            self.assertIn("arg:--add-dir", capture)
            self.assertIn(f"arg:{repo_root}", capture)
            self.assertIn(f"cwd:{repo_root}", capture)
            self.assertIn(f"context:{context_path}", capture)
            self.assertIn(f"handoff:{handoff_path}", capture)
            self.assertIn("slice:slice-a", capture)
            self.assertIn(f"legacy_context:{context_path}", capture)
            self.assertIn("stdin:slice prompt", capture)

    def test_agent_wrapper_can_still_launch_codex_when_requested(self) -> None:
        script_path = self.repo_root / "automation/supervisor/run_hermes.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            repo_root.mkdir()
            (repo_root / ".git").mkdir()
            prompt_path = temp_path / "prompt.md"
            prompt_path.write_text("codex prompt", encoding="utf-8")
            context_path = temp_path / "context.json"
            context_path.write_text("{}", encoding="utf-8")
            handoff_path = temp_path / "handoff.json"
            capture_path = temp_path / "capture.txt"
            bin_dir = temp_path / "bin"
            bin_dir.mkdir()
            self.write_fake_executable(
                bin_dir / "codex",
                """#!/usr/bin/env bash
{
  for arg in "$@"; do printf 'arg:%s\\n' "$arg"; done
  printf 'cwd:%s\\n' "$PWD"
  printf 'stdin:'
  cat
  printf '\\n'
} > "$CAPTURE_FILE"
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["CAPTURE_FILE"] = str(capture_path)
            env["REPO_AUTOMATION_AGENT_RUNNER"] = "codex"

            result = subprocess.run(
                [
                    str(script_path),
                    "--repo-root",
                    str(repo_root),
                    "--prompt-file",
                    str(prompt_path),
                    "--context-file",
                    str(context_path),
                    "--handoff-file",
                    str(handoff_path),
                    "--slice-id",
                    "slice-b",
                ],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            capture = capture_path.read_text(encoding="utf-8")
            self.assertIn("arg:--ask-for-approval", capture)
            self.assertIn("arg:never", capture)
            self.assertIn("arg:exec", capture)
            self.assertIn("arg:--sandbox", capture)
            self.assertIn("arg:workspace-write", capture)
            self.assertIn("arg:-", capture)
            self.assertIn(f"cwd:{repo_root}", capture)
            self.assertIn("stdin:codex prompt", capture)

    def test_format_agent_command_shell_quotes_placeholder_values(self) -> None:
        formatted = format_agent_command(
            command_template=(
                "runner --repo {repo_root} --prompt {prompt_file} "
                "--context {context_file} --handoff {handoff_file} --slice {slice_id}"
            ),
            repo_root=Path("/tmp/Repo With Space"),
            prompt_path=Path("/tmp/prompt file.md"),
            context_path=Path("/tmp/context file.json"),
            handoff_path=Path("/tmp/handoff file.json"),
            slice_id="today slice",
        )

        self.assertIn("--repo '/tmp/Repo With Space'", formatted)
        self.assertIn("--prompt '/tmp/prompt file.md'", formatted)
        self.assertIn("--context '/tmp/context file.json'", formatted)
        self.assertIn("--handoff '/tmp/handoff file.json'", formatted)
        self.assertIn("--slice 'today slice'", formatted)

    def test_replay_validation_commands_replays_only_exact_allowlist(self) -> None:
        calls: list[list[str]] = []

        def runner(_repo_root: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        replay_results = policy.replay_validation_commands(
            repo_root=self.repo_root,
            required_validations=["make architecture", "make test-domain DOMAIN=today", "git diff --check"],
            validations_passed=["make architecture", "make test-domain DOMAIN=today", "git diff --check"],
            runner=runner,
        )

        self.assertEqual([["make", "architecture"], ["git", "diff", "--check"]], calls)
        self.assertEqual(["make architecture", "git diff --check"], [replay.command for replay in replay_results])
        self.assertTrue(all(replay.success for replay in replay_results))

    def test_validation_ownership_classifier_marks_replayable_report_only_and_never_owned(self) -> None:
        ownership = policy.classify_validation_ownerships(
            ["make architecture", "make test-domain DOMAIN=today", "open -a Simulator"]
        )

        self.assertEqual(
            ["supervisor_replayable", "run_report_only", "never_supervisor_owned"], [item.tier for item in ownership]
        )

    def test_select_next_slice_respects_priority_and_dependencies(self) -> None:
        queue_data = {
            "version": 1,
            "policy": {
                "consecutive_autonomous_limit": 2,
                "handoff_timeout_seconds": 30,
                "agent_command_template": "",
                "supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"],
            },
            "slices": [
                {
                    "slice_id": "a",
                    "title": "A",
                    "status": "done",
                    "priority": 20,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": [],
                    "max_files_changed": 2,
                    "notes": "",
                },
                {
                    "slice_id": "b",
                    "title": "B",
                    "status": "queued",
                    "priority": 30,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["a"],
                    "max_files_changed": 2,
                    "notes": "",
                },
                {
                    "slice_id": "c",
                    "title": "C",
                    "status": "queued",
                    "priority": 10,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["missing"],
                    "max_files_changed": 2,
                    "notes": "",
                },
            ],
        }

        self.assertEqual("b", self.require_selected_slice(queue_data)["slice_id"])

    def test_scope_check_ignores_supervisor_owned_paths(self) -> None:
        dirty_paths = [
            "automation/queue/slices.json",
            "automation/handoffs/20260421T153000Z-slice.json",
            "docs/product/domains/today.md",
        ]
        unexpected = policy.out_of_scope_paths(
            dirty_paths=dirty_paths,
            allowed_paths=["docs/product/domains/today.md"],
            supervisor_owned_paths=["automation/queue/slices.json", "automation/handoffs/"],
        )
        self.assertEqual([], unexpected)

    def test_completion_decision_stops_on_required_validation_failure(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["validations_passed"] = [
            validation for validation in slice_record["required_validations"] if validation != "git diff --check"
        ]
        handoff["recommended_next_slice"] = ""
        handoff["recommended_next_reason"] = ""

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=2,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertEqual(["git diff --check"], decision.required_validation_failures)

    def test_completion_decision_rejects_unknown_recommended_next_slice(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["recommended_next_slice"] = "brand-new-slice"
        handoff["recommended_next_reason"] = "The agent guessed at unqueued work."

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("stop_for_review", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertIn("not present in automation/queue/slices.json", decision.stop_reason)

    def test_completion_decision_rejects_recommendation_that_skips_higher_priority_slice(self) -> None:
        queue_data = {
            "version": 1,
            "policy": {
                "consecutive_autonomous_limit": 3,
                "handoff_timeout_seconds": 30,
                "agent_command_template": "",
                "supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"],
            },
            "slices": [
                {
                    "slice_id": "bootstrap",
                    "title": "Bootstrap",
                    "status": "in_progress",
                    "priority": 10,
                    "domain": "repo-tooling",
                    "allowed_paths": ["automation/"],
                    "required_validations": ["make architecture"],
                    "depends_on": [],
                    "max_files_changed": 10,
                    "notes": "",
                },
                {
                    "slice_id": "higher-priority",
                    "title": "Higher priority",
                    "status": "queued",
                    "priority": 20,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["bootstrap"],
                    "max_files_changed": 4,
                    "notes": "",
                },
                {
                    "slice_id": "lower-priority",
                    "title": "Lower priority",
                    "status": "queued",
                    "priority": 30,
                    "domain": "today",
                    "allowed_paths": ["docs/"],
                    "required_validations": ["make architecture"],
                    "depends_on": ["bootstrap"],
                    "max_files_changed": 4,
                    "notes": "",
                },
            ],
        }
        slice_record = self.require_slice_record(queue_data, "bootstrap")
        handoff = {
            "slice_id": "bootstrap",
            "status": "done",
            "summary": "Bootstrap finished.",
            "files_touched": ["automation/README.md"],
            "validations_passed": ["make architecture"],
            "validations_failed": [],
            "risks": [],
            "recommended_next_slice": "lower-priority",
            "recommended_next_reason": "Skip directly to the lower-priority slice.",
            "dirty_paths_outside_scope": [],
            "timestamp": "2026-04-21T15:46:00Z",
        }

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=["automation/README.md"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("stop_for_review", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertIn("highest-priority eligible queued slice", decision.stop_reason)

    def test_completion_decision_fails_when_files_touched_leave_scope(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        allowed_file = self.example_allowed_file(slice_record)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["files_touched"] = [allowed_file, "README.md"]

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=[allowed_file],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertEqual(["README.md"], decision.files_touched_outside_scope)

    def test_completion_decision_fails_when_diff_budget_is_exceeded(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        handoff["files_touched"] = [
            self.example_allowed_file(slice_record, f"file-{index}.swift")
            for index in range(slice_record["max_files_changed"] + 1)
        ]
        handoff["recommended_next_slice"] = ""
        handoff["recommended_next_reason"] = ""

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertIn("max_files_changed", decision.stop_reason)

    def test_completion_decision_fails_when_supervisor_replay_fails(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)
        validation_replays = [
            policy.ValidationReplayResult(
                command="git diff --check", success=False, exit_code=1, reason="Supervisor replay exited with code 1."
            )
        ]

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
            validation_replays=validation_replays,
        )

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertEqual(["git diff --check"], decision.required_validation_failures)
        self.assertEqual(1, len(decision.supervisor_validation_replays))

    def test_completion_decision_stops_for_review_at_autonomous_limit(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=2,
            run_limit=2,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("stop_for_review", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertIn("human review", decision.stop_reason)

    def test_completion_decision_continues_when_recommended_next_is_known_and_eligible(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice_id = self.example_slice_id_at(1)
        queue_data = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        slice_record = self.require_slice_record(queue_data, first_slice_id)
        handoff = policy.load_json(self.example_handoff_path)

        decision = policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=3,
        )

        self.assertEqual("done", decision.queue_status)
        self.assertEqual("continue", decision.decision)
        self.assertTrue(decision.should_continue)
        self.assertEqual(second_slice_id, decision.next_slice_id)

    def test_first_proof_runs_two_adjacent_slices_then_stops_for_review(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice_id = self.example_slice_id_at(1)
        third_slice_id = self.example_slice_id_at(2)
        first_queue = self.example_queue_with_slice_status(first_slice_id, "in_progress")
        first_slice = self.require_slice_record(first_queue, first_slice_id)
        first_handoff = policy.load_json(self.example_handoff_path)
        first_replays = self.successful_replays(["make architecture", "git diff --check"])

        first_decision = policy.evaluate_completion(
            queue_data=first_queue,
            slice_record=first_slice,
            handoff=first_handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=first_handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=2,
            validation_replays=first_replays,
        )

        self.assertEqual("continue", first_decision.decision)
        self.assertEqual(second_slice_id, first_decision.next_slice_id)

        second_queue = policy.set_slice_status(first_queue, first_slice_id, "done")
        second_queue = policy.set_slice_status(second_queue, second_slice_id, "in_progress")
        second_slice = self.require_slice_record(second_queue, second_slice_id)
        queue_if_second_done = policy.set_slice_status(second_queue, second_slice_id, "done")
        self.assertEqual(third_slice_id, self.require_selected_slice(queue_if_second_done)["slice_id"])

        second_handoff = {
            "slice_id": second_slice_id,
            "status": "done",
            "summary": "Added targeted regression coverage for the next example interaction.",
            "files_touched": [
                self.example_allowed_file(second_slice, "RegressionTests.swift"),
                self.example_markdown_path(second_slice),
            ],
            "validations_passed": second_slice["required_validations"],
            "validations_failed": [],
            "risks": ["No manual simulator pass for the regression flow"],
            "recommended_next_slice": third_slice_id,
            "recommended_next_reason": "Adjacent proofread slice after the regression coverage pass.",
            "dirty_paths_outside_scope": [],
            "timestamp": "2026-04-21T16:00:00Z",
        }
        second_replays = self.successful_replays(["make architecture", "git diff --check"])

        second_decision = policy.evaluate_completion(
            queue_data=second_queue,
            slice_record=second_slice,
            handoff=second_handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=second_handoff["files_touched"],
            completed_autonomous_runs=2,
            run_limit=2,
            validation_replays=second_replays,
        )

        self.assertEqual("done", second_decision.queue_status)
        self.assertEqual("stop_for_review", second_decision.decision)
        self.assertFalse(second_decision.should_continue)
        self.assertEqual(third_slice_id, second_decision.recommended_next_slice)
        self.assertIn("human review", second_decision.stop_reason)

    def test_build_context_includes_compact_previous_handoff_and_relevant_docs(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice = self.example_slice_at(1)
        second_slice_id = second_slice["slice_id"]
        third_slice_id = self.example_slice_id_at(2)
        example_handoff = policy.load_json(self.example_handoff_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_dir = Path(temp_dir)
            shutil.copy(self.example_handoff_path, handoff_dir / self.example_handoff_filename())

            bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=second_slice_id,
                max_doc_chars=1200,
            )

        document_paths = [document["path"] for document in bundle["documents"]]
        expected_doc = self.example_markdown_path(second_slice)
        if (self.repo_root / expected_doc).exists():
            self.assertIn(expected_doc, document_paths)
        self.assertEqual(first_slice_id, bundle["previous_handoff"]["slice_id"])
        self.assertEqual("domain-tested", bundle["previous_handoff"]["proof_level"])
        self.assertIn("running-app-smoke", bundle["previous_handoff"]["missing_proof_levels"])
        self.assertIn(
            example_handoff["contract_status_changes"][0]["contract"],
            bundle["previous_handoff"]["contract_status_changes"][0]["contract"],
        )
        self.assertEqual("clean", bundle["previous_handoff"]["repo_clean_status"])
        self.assertEqual("not-checked", bundle["previous_handoff"]["git_mirror_status"])
        self.assertIn(example_handoff["summary"].split(".")[0], bundle["previous_handoff_summary"])
        self.assertIn("Proof level: `domain-tested`", bundle["previous_handoff_summary"])
        self.assertIn(
            f"Contract status changes: {example_handoff['contract_status_changes'][0]['contract']}",
            bundle["previous_handoff_summary"],
        )
        self.assertIn("Residual risks: No manual simulator pass", bundle["previous_handoff_summary"])
        self.assertIn("Repo clean status: `clean`", bundle["previous_handoff_summary"])
        self.assertIn("Git mirror status: `not-checked`", bundle["previous_handoff_summary"])
        self.assertEqual(
            ["supervisor_replayable", "run_report_only", "supervisor_replayable"],
            [item["tier"] for item in bundle["validation_ownership"]],
        )
        self.assertEqual(third_slice_id, bundle["queue"]["adjacent_queued_slices"][0]["slice_id"])

    def test_build_context_preserves_legacy_previous_handoff_as_read_only_context(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice_id = self.example_slice_id_at(1)
        example_handoff = policy.load_json(self.example_handoff_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_dir = Path(temp_dir)
            legacy_handoff = json.loads(json.dumps(example_handoff))
            legacy_handoff.pop("proof_level")
            legacy_handoff.pop("missing_proof_levels")
            legacy_handoff["risks"] = legacy_handoff.pop("residual_risks")
            legacy_handoff.pop("contract_status_changes")
            legacy_handoff.pop("repo_clean_status")
            legacy_handoff.pop("git_mirror_status")
            legacy_path = handoff_dir / self.example_handoff_filename()
            legacy_path.write_text(json.dumps(legacy_handoff), encoding="utf-8")

            bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=second_slice_id,
                max_doc_chars=1200,
            )

        self.assertEqual(first_slice_id, bundle["previous_handoff"]["slice_id"])
        self.assertEqual("legacy-unknown", bundle["previous_handoff"]["proof_level"])
        self.assertEqual(example_handoff["residual_risks"], bundle["previous_handoff"]["residual_risks"])
        self.assertEqual("legacy-unknown", bundle["previous_handoff"]["repo_clean_status"])
        self.assertEqual("legacy-unknown", bundle["previous_handoff"]["git_mirror_status"])
        self.assertIn("Proof level: `legacy-unknown`", bundle["previous_handoff_summary"])

    def test_render_prompt_includes_slice_fields_previous_handoff_and_template(self) -> None:
        first_slice_id = self.example_slice_id_at(0)
        second_slice = self.example_slice_at(1)
        second_slice_id = second_slice["slice_id"]
        third_slice_id = self.example_slice_id_at(2)
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_dir = Path(temp_dir)
            shutil.copy(self.example_handoff_path, handoff_dir / self.example_handoff_filename())
            queue_data = policy.load_queue(self.example_queue_path, self.queue_schema_path)
            slice_record = self.require_slice_record(queue_data, second_slice_id)
            context_bundle = build_context_bundle(
                repo_root=self.repo_root,
                queue_path=self.example_queue_path,
                handoff_dir=handoff_dir,
                slice_id=second_slice_id,
                max_doc_chars=1200,
            )
            prompt_text = render_prompt(
                repo_root=self.repo_root,
                slice_record=slice_record,
                context_bundle=context_bundle,
                handoff_path=Path("/tmp/handoff.json"),
            )

        self.assertIn(second_slice_id, prompt_text)
        self.assertIn(second_slice["title"], prompt_text)
        self.assertIn(f"`{second_slice['domain']}`", prompt_text)
        self.assertIn("`git diff --check`", prompt_text)
        self.assertIn("`supervisor_replayable`", prompt_text)
        self.assertIn("`run_report_only`", prompt_text)
        self.assertIn(f"`{second_slice['max_files_changed']}`", prompt_text)
        self.assertIn(first_slice_id, prompt_text)
        self.assertIn(third_slice_id, prompt_text)
        self.assertIn("/tmp/handoff.json", prompt_text)
        self.assertIn("<describe what changed", prompt_text)
        self.assertIn('"proof_level"', prompt_text)
        self.assertIn('"missing_proof_levels"', prompt_text)
        self.assertIn('"contract_status_changes"', prompt_text)
        self.assertIn('"residual_risks"', prompt_text)
        self.assertIn('"repo_clean_status"', prompt_text)
        self.assertIn('"git_mirror_status"', prompt_text)

    def test_decision_report_includes_supervisor_validation_replays(self) -> None:
        decision = policy.CompletionDecision(
            queue_status="done",
            decision="continue",
            should_continue=True,
            stop_reason="Continuation allowed into the recommended eligible next slice.",
            next_slice_id="today-continue-ui-regression-coverage",
            recommended_next_slice="today-continue-ui-regression-coverage",
            required_validation_failures=[],
            dirty_paths_outside_scope=[],
            files_touched_outside_scope=[],
            changed_file_count=2,
            supervisor_validation_replays=[
                policy.ValidationReplayResult(
                    command="make architecture", success=True, exit_code=0, reason="Supervisor replay passed."
                )
            ],
        )

        report = make_decision_report(
            slice_record={"slice_id": "today-nonfocus-add-to-focus"}, decision=decision, completed_runs=1, autonomous_limit=2
        )

        self.assertEqual(
            [{"command": "make architecture", "success": True, "exit_code": 0, "reason": "Supervisor replay passed."}],
            report["supervisor_validation_replays"],
        )

    def example_queue_with_slice_status(self, slice_id: str, status: str) -> dict[str, Any]:
        queue_data = policy.load_queue(self.example_queue_path, self.queue_schema_path)
        cloned = json.loads(json.dumps(queue_data))
        for slice_record in cloned["slices"]:
            if slice_record["slice_id"] == slice_id:
                slice_record["status"] = status
        return cloned

    def successful_replays(self, commands: list[str]) -> list[policy.ValidationReplayResult]:
        return [
            policy.ValidationReplayResult(command=command, success=True, exit_code=0, reason="Supervisor replay passed.")
            for command in commands
        ]

    def write_fake_executable(self, path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
