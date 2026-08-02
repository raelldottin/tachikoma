"""Tests for the adapter layer in the automation harness."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from automation.supervisor import policy


class AdapterTests(unittest.TestCase):
    """Tests for the adapter layer (run_hermes.sh)."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.script_path = self.repo_root / "automation/supervisor/run_hermes.sh"
        self.assertTrue(self.script_path.exists())
        self.assertTrue(os.access(self.script_path, os.X_OK))

    def test_adapter_with_fake_hermes_produces_valid_handoff(self):
        """Adapter should produce a valid handoff when Hermes outputs valid JSON."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            # Create fake Hermes executable that outputs valid handoff JSON
            fake_hermes = temp_path / "hermes"
            fake_hermes.write_text(
                """#!/usr/bin/env bash
echo '{"slice_id": "test-slice", "status": "done", "summary": "test", "files_touched": [], "validations_passed": [], "validations_failed": [], "proof_level": "domain-tested", "missing_proof_levels": ["build-tested"], "contract_status_changes": [], "residual_risks": ["No known residual risk."], "recommended_next_slice": "", "recommended_next_reason": "", "repo_clean_status": "clean", "git_mirror_status": "not-checked", "dirty_paths_outside_scope": [], "timestamp": "2024-01-01T00:00:00Z"}'
""",
                encoding="utf-8",
            )
            fake_hermes.chmod(0o755)

            # Create required input files
            prompt_file = temp_path / "prompt.md"
            prompt_file.write_text("test prompt", encoding="utf-8")
            context_file = temp_path / "context.json"
            context_file.write_text("{}", encoding="utf-8")
            handoff_file = temp_path / "handoff.json"

            # Set up environment to use our fake Hermes
            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env['PATH']}"
            # Ensure we use the Hermes path (not Codex or Claude Code)
            env.pop("REPO_AUTOMATION_AGENT_RUNNER", None)
            env.pop("CLAUDE_CODE", None)
            env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

            # Run the adapter
            result = subprocess.run(
                [
                    str(self.script_path),
                    "--repo-root",
                    str(temp_path),
                    "--prompt-file",
                    str(prompt_file),
                    "--context-file",
                    str(context_file),
                    "--handoff-file",
                    str(handoff_file),
                    "--slice-id",
                    "test-slice",
                ],
                cwd=str(self.repo_root),
                env=env,
                capture_output=True,
                text=True,
            )

            # Check that the script succeeded
            self.assertEqual(
                result.returncode,
                0,
                msg=f"Adapter failed with stderr: {result.stderr}",
            )

            # Check that the handoff file was created
            self.assertTrue(handoff_file.exists(), "Handoff file was not created")

            # Validate the handoff against the schema
            handoff_data = json.loads(handoff_file.read_text(encoding="utf-8"))
            handoff_schema_path = self.repo_root / "automation/schemas/handoff.schema.json"
            schema = policy.load_schema(handoff_schema_path)
            validation = policy.validate_document(handoff_data, schema)
            self.assertTrue(
                validation.is_valid,
                msg=f"Handoff validation failed: {validation.errors}",
            )

            # Check that the slice_id matches
            self.assertEqual(handoff_data["slice_id"], "test-slice")

            # Ensure no temporary file was left behind (atomic write)
            tmp_file = handoff_file.with_suffix(handoff_file.suffix + ".tmp")
            self.assertFalse(
                tmp_file.exists(),
                f"Temporary file {tmp_file} was not cleaned up",
            )

    def test_adapter_handles_invalid_hermes_json(self):
        """Adapter should produce a failed handoff when Hermes outputs invalid JSON."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            # Create fake Hermes executable that outputs invalid JSON
            fake_hermes = temp_path / "hermes"
            fake_hermes.write_text(
                """#!/usr/bin/env bash
echo 'not json'
""",
                encoding="utf-8",
            )
            fake_hermes.chmod(0o755)

            # Create required input files
            prompt_file = temp_path / "prompt.md"
            prompt_file.write_text("test prompt", encoding="utf-8")
            context_file = temp_path / "context.json"
            context_file.write_text("{}", encoding="utf-8")
            handoff_file = temp_path / "handoff.json"

            # Set up environment to use our fake Hermes
            env = os.environ.copy()
            env["PATH"] = f"{temp_path}{os.pathsep}{env['PATH']}"
            env.pop("REPO_AUTOMATION_AGENT_RUNNER", None)
            env.pop("CLAUDE_CODE", None)
            env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

            # Run the adapter
            result = subprocess.run(
                [
                    str(self.script_path),
                    "--repo-root",
                    str(temp_path),
                    "--prompt-file",
                    str(prompt_file),
                    "--context-file",
                    str(context_file),
                    "--handoff-file",
                    str(handoff_file),
                    "--slice-id",
                    "test-slice",
                ],
                cwd=str(self.repo_root),
                env=env,
                capture_output=True,
                text=True,
            )

            # The script should exit with 0 (it writes a failure handoff and exits 0)
            self.assertEqual(result.returncode, 0)

            # Check that the handoff file was created
            self.assertTrue(handoff_file.exists(), "Handoff file was not created")

            # Validate that the handoff indicates failure
            handoff_data = json.loads(handoff_file.read_text(encoding="utf-8"))
            self.assertEqual(handoff_data["status"], "failed")
            self.assertIn("Agent failed to produce valid JSON handoff", handoff_data["summary"])
            self.assertEqual(handoff_data["proof_level"], "doc-only")


if __name__ == "__main__":
    unittest.main()