"""Orchestrate and drive the ProgramBench effectiveness benchmark.

Subcommands:
  run    Produce ``submission.tar.gz`` per instance by driving the harness (no Docker).
  eval   Run ProgramBench's authoritative test suites over the submissions (Docker).
  score  Reduce eval outputs to an effectiveness report (no Docker).
  all    run -> eval -> score.

Point the harness agent at any OpenAI-compatible endpoint (e.g. NVIDIA) via the runner
env: REPO_AUTOMATION_AGENT_RUNNER=codex, OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1,
OPENAI_API_KEY=$NVIDIA_API_KEY.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .adapter import AgentAdapter, SupervisorAgentAdapter
from .evalrunner import EvalRunner, ProgramBenchEvalRunner
from .instances import resolve_instances, task_spec
from .scoring import EffectivenessReport, score_run_dir, write_report

REPORT_FILENAME = "effectiveness-report.json"
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]


def produce_submissions(run_dir: Path, instances: Sequence[str], adapter: AgentAdapter) -> None:
    run_dir = Path(run_dir)
    for instance_id in instances:
        instance_dir = run_dir / instance_id
        instance_dir.mkdir(parents=True, exist_ok=True)
        adapter.produce_submission(task_spec(instance_id), instance_dir / "submission.tar.gz")


def score_and_write(run_dir: Path) -> EffectivenessReport:
    report = score_run_dir(run_dir)
    write_report(report, Path(run_dir) / REPORT_FILENAME)
    return report


def run_benchmark(
    run_dir: Path,
    instances: Sequence[str],
    adapter: AgentAdapter,
    eval_runner: Optional[EvalRunner] = None,
    score: bool = True,
) -> Optional[EffectivenessReport]:
    """End-to-end orchestration used by both the CLI and tests."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    produce_submissions(run_dir, instances, adapter)
    if eval_runner is not None:
        eval_runner.evaluate(run_dir)
    if score:
        return score_and_write(run_dir)
    return None


def _build_adapter(args: argparse.Namespace) -> SupervisorAgentAdapter:
    return SupervisorAgentAdapter(
        repo_root=Path(args.repo_root),
        agent_command_template=args.agent_cmd,
        timeout_seconds=args.timeout,
    )


def _instances_from_args(args: argparse.Namespace) -> list[str]:
    explicit = list(args.instances) if args.instances else None
    return resolve_instances(explicit, use_all=args.all)


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, type=Path, help="Directory holding per-instance submissions/results.")
    parser.add_argument("--instances", nargs="*", help="Explicit instance ids (default: smoke set).")
    parser.add_argument("--all", action="store_true", help="Run the full ProgramBench instance set.")


def _add_adapter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=_DEFAULT_REPO_ROOT, help="Harness repo root.")
    parser.add_argument("--agent-cmd", help="Override the agent command template (default: run_agent.sh).")
    parser.add_argument("--timeout", type=int, default=1800, help="Per-instance agent timeout (seconds).")


def _add_eval_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workers", type=int, help="ProgramBench eval workers.")
    parser.add_argument(
        "--programbench-cmd",
        nargs="+",
        default=["uvx", "programbench"],
        help="Command used to invoke the ProgramBench CLI.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m automation.benchmark", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Produce submissions (no Docker).")
    _add_selection_args(run_parser)
    _add_adapter_args(run_parser)

    eval_parser = subparsers.add_parser("eval", help="Run ProgramBench eval (Docker).")
    eval_parser.add_argument("--run-dir", required=True, type=Path)
    _add_eval_args(eval_parser)

    score_parser = subparsers.add_parser("score", help="Score eval outputs (no Docker).")
    score_parser.add_argument("--run-dir", required=True, type=Path)

    all_parser = subparsers.add_parser("all", help="run -> eval -> score.")
    _add_selection_args(all_parser)
    _add_adapter_args(all_parser)
    _add_eval_args(all_parser)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "run":
        produce_submissions(args.run_dir, _instances_from_args(args), _build_adapter(args))
        print(f"Wrote submissions to {args.run_dir}")
        return 0

    if args.command == "eval":
        ProgramBenchEvalRunner(args.programbench_cmd, workers=args.workers).evaluate(args.run_dir)
        return 0

    if args.command == "score":
        report = score_and_write(args.run_dir)
        print(report.summary_table())
        return 0

    if args.command == "all":
        report = run_benchmark(
            run_dir=args.run_dir,
            instances=_instances_from_args(args),
            adapter=_build_adapter(args),
            eval_runner=ProgramBenchEvalRunner(args.programbench_cmd, workers=args.workers),
        )
        assert report is not None
        print(report.summary_table())
        return 0

    return 1  # pragma: no cover - argparse enforces a valid subcommand.


if __name__ == "__main__":
    sys.exit(main())
