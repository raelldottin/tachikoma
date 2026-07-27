from __future__ import annotations

import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from automation.benchmark import adapter as benchmark_adapter
from automation.benchmark import run as benchmark_run
from automation.benchmark.adapter import SubmissionResult, SupervisorAgentAdapter, build_queue_data, build_slice_record
from automation.benchmark.evalrunner import RecordedEvalRunner
from automation.benchmark.instances import TaskSpec, task_spec
from automation.benchmark.scoring import (
    EffectivenessReport,
    InstanceScore,
    score_eval_data,
    score_eval_file,
    score_run_dir,
)
from automation.supervisor import policy
from automation.supervisor.run_next import render_prompt


def _eval_data(passed: int, total: int, error_code: str | None = None) -> dict:
    results = [{"status": "passed"} for _ in range(passed)]
    results += [{"status": "failed"} for _ in range(total - passed)]
    return {"test_results": results, "error_code": error_code}


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.fixture = self.repo_root / "automation/tests/fixtures/cmatrix.eval.json"

    def test_fully_passing_submission_is_resolved(self) -> None:
        score = score_eval_data("x", _eval_data(20, 20))
        self.assertTrue(score.resolved)
        self.assertTrue(score.near_resolved)
        self.assertEqual(1.0, score.pass_fraction)

    def test_near_threshold_is_near_resolved_but_not_resolved(self) -> None:
        score = score_eval_data("x", _eval_data(19, 20))
        self.assertFalse(score.resolved)
        self.assertTrue(score.near_resolved)

    def test_below_near_threshold_is_neither(self) -> None:
        score = score_eval_data("x", _eval_data(18, 20))
        self.assertFalse(score.resolved)
        self.assertFalse(score.near_resolved)

    def test_zero_tests_never_counts_as_resolved(self) -> None:
        score = score_eval_data("x", _eval_data(0, 0))
        self.assertFalse(score.resolved)
        self.assertFalse(score.near_resolved)
        self.assertEqual(0.0, score.pass_fraction)

    def test_real_cmatrix_fixture_surfaces_error_code(self) -> None:
        score = score_eval_file(self.fixture)
        self.assertEqual("copy_executable_failed", score.error_code)
        self.assertFalse(score.resolved)

    def test_report_aggregates_rates_and_errors(self) -> None:
        report = EffectivenessReport(
            (
                InstanceScore("a", 10, 10),
                InstanceScore("b", 20, 19),
                InstanceScore("c", 10, 0, error_code="copy_executable_failed"),
                InstanceScore("d", 10, 0, error_code="copy_executable_failed"),
            )
        )
        self.assertEqual(4, report.instance_count)
        self.assertEqual(1, report.resolved_count)
        self.assertEqual(2, report.near_resolved_count)
        self.assertAlmostEqual(0.25, report.resolve_rate)
        self.assertAlmostEqual(0.5, report.near_resolve_rate)
        self.assertAlmostEqual((1.0 + 0.95 + 0.0 + 0.0) / 4, report.mean_pass_fraction)
        self.assertEqual({"copy_executable_failed": 2}, report.error_counts)
        self.assertIn("Harness Effectiveness", report.summary_table())

    def test_score_run_dir_reads_per_instance_eval_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            for iid, data in (("inst-a", _eval_data(10, 10)), ("inst-b", _eval_data(1, 10))):
                instance_dir = run_dir / iid
                instance_dir.mkdir()
                (instance_dir / f"{iid}.eval.json").write_text(__import__("json").dumps(data), encoding="utf-8")
            report = score_run_dir(run_dir)
        self.assertEqual(2, report.instance_count)
        self.assertEqual(1, report.resolved_count)
        self.assertEqual(["inst-a", "inst-b"], [s.instance_id for s in report.instances])


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.queue_schema = policy.load_schema(self.repo_root / "automation/schemas/slice.schema.json")
        self.task = TaskSpec("owner__proj.abc1234", "owner/proj", "abc1234", "c", "easy")

    def test_generated_queue_matches_slice_schema(self) -> None:
        slice_record = build_slice_record(self.task)
        queue_data = build_queue_data(slice_record, "cmd {repo_root}", 1800)
        validation = policy.validate_document(queue_data, self.queue_schema)
        self.assertTrue(validation.is_valid, validation.errors)
        self.assertEqual([], policy.validate_queue_integrity(queue_data))

    def test_rendered_prompt_carries_rebuild_objective(self) -> None:
        slice_record = build_slice_record(self.task)
        queue_data = build_queue_data(slice_record, "cmd {repo_root}", 1800)
        bundle = benchmark_adapter.build_context_bundle(queue_data, slice_record)
        prompt = render_prompt(
            repo_root=self.repo_root,
            slice_record=slice_record,
            context_bundle=bundle,
            handoff_path=Path("/tmp/handoff.json"),
        )
        self.assertIn("owner/proj", prompt)
        self.assertIn("compile.sh", prompt)

    def test_produce_submission_archives_agent_workspace(self) -> None:
        captured: dict[str, str] = {}

        def fake_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
            captured["command"] = command
            (workspace / "compile.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (workspace / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            return 0

        adapter = SupervisorAgentAdapter(repo_root=self.repo_root, runner=fake_runner)
        with tempfile.TemporaryDirectory() as tmp:
            out_tar = Path(tmp) / "submission.tar.gz"
            result = adapter.produce_submission(self.task, out_tar)
            self.assertIsInstance(result, SubmissionResult)
            self.assertEqual(0, result.returncode)
            self.assertTrue(out_tar.is_file())
            with tarfile.open(out_tar, "r:gz") as tar:
                names = tar.getnames()
        self.assertIn("compile.sh", names)
        self.assertIn("main.c", names)
        self.assertNotIn(".git", names)
        # The agent was invoked with the rendered prompt + the workspace as repo root.
        self.assertIn("--prompt-file", captured["command"])
        self.assertIn("--slice-id", captured["command"])


class OrchestratorTests(unittest.TestCase):
    def test_run_benchmark_is_container_free_end_to_end(self) -> None:
        class FakeAdapter:
            def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
                out_tar.parent.mkdir(parents=True, exist_ok=True)
                with tarfile.open(out_tar, "w:gz"):
                    pass  # empty but present submission
                return SubmissionResult(task.instance_id, out_tar, out_tar.parent, 0)

        recorded = RecordedEvalRunner.from_mapping(
            {
                "inst-a": _eval_data(10, 10),
                "inst-b": _eval_data(0, 10, error_code="compile_failed"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report = benchmark_run.run_benchmark(
                run_dir=run_dir,
                instances=["inst-a", "inst-b"],
                adapter=FakeAdapter(),
                eval_runner=recorded,
            )
            self.assertIsNotNone(report)
            self.assertTrue((run_dir / benchmark_run.REPORT_FILENAME).is_file())
        assert report is not None
        self.assertEqual(2, report.instance_count)
        self.assertEqual(1, report.resolved_count)
        self.assertEqual({"compile_failed": 1}, report.error_counts)


class InstancesTests(unittest.TestCase):
    def test_task_spec_falls_back_to_instance_id_when_metadata_absent(self) -> None:
        spec = task_spec("someowner__someproj.deadbee")
        self.assertEqual("someowner__someproj.deadbee", spec.instance_id)
        self.assertIn("someowner", spec.repository)
        self.assertIn("from scratch", spec.objective)


if __name__ == "__main__":
    unittest.main()
