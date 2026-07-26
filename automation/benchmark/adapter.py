"""Adapters that turn one ProgramBench task into a ``submission.tar.gz``.

The production adapter, ``SupervisorAgentAdapter``, exercises the *real* harness: it
models the rebuild as a single supervisor slice, renders the harness prompt with the
existing ``run_next.render_prompt`` (base + slice fragments), and launches the harness
agent runner (``run_agent.sh``) in a fresh local workspace. Nothing here uses a
container; the agent rebuilds on the local filesystem and the workspace is archived as
the submission. ProgramBench's cleanroom is only used later, by the eval step.

The agent invocation is a single injectable seam (``runner``) so tests drive the whole
adapter without an LLM or any external process.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

from automation.context import build_context
from automation.supervisor import run_next

from .instances import TaskSpec

DEFAULT_TIMEOUT_SECONDS = 1800
# Allow the agent to touch the whole rebuild workspace.
WORKSPACE_ALLOWED_PATH = "./"
# Effectively unbounded diff budget: a from-scratch rebuild is not a bounded slice.
REBUILD_DIFF_BUDGET = 1_000_000

# (formatted_command, workspace, env, timeout_seconds) -> return code
CommandRunner = Callable[[str, Path, Mapping[str, str], int], int]


@dataclass
class SubmissionResult:
    instance_id: str
    tar_path: Path
    workspace: Path
    returncode: int


class AgentAdapter(Protocol):
    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult: ...


def build_slice_record(task: TaskSpec) -> dict:
    """Model a ProgramBench rebuild as a schema-valid supervisor slice."""
    return {
        "slice_id": task.instance_id,
        "title": f"Rebuild {task.repository} from scratch",
        "status": "queued",
        "priority": 0,
        "domain": "programbench",
        "allowed_paths": [WORKSPACE_ALLOWED_PATH],
        "required_validations": ["sh compile.sh"],
        "depends_on": [],
        "max_files_changed": REBUILD_DIFF_BUDGET,
        "notes": task.objective,
    }


def build_queue_data(slice_record: dict, agent_command_template: str, timeout_seconds: int) -> dict:
    return {
        "version": 1,
        "policy": {
            "consecutive_autonomous_limit": 1,
            "handoff_timeout_seconds": timeout_seconds,
            "agent_command_template": agent_command_template,
            "supervisor_owned_paths": ["automation/"],
        },
        "slices": [slice_record],
    }


def build_context_bundle(queue_data: dict, slice_record: dict) -> dict:
    """Assemble the minimal bundle ``run_next.render_prompt`` consumes, reusing helpers."""
    return {
        "policy_sentence": build_context.POLICY_SENTENCE,
        "slice": build_context.summarize_slice(slice_record),
        "queue": build_context.build_queue_metadata(queue_data, slice_record),
        "validation_ownership": build_context.summarize_validation_ownership(slice_record),
        "previous_handoff": None,
        "previous_handoff_summary": build_context.render_previous_handoff_summary(None),
        "acceptance_checks": build_context.build_acceptance_checks(slice_record),
        "handoff_template": build_context.build_handoff_template(slice_record),
        "documents": [],
    }


def _default_command_template(repo_root: Path) -> str:
    """run_agent.sh referenced by absolute path: the agent's cwd is the workspace."""
    script = repo_root / "automation/supervisor/run_agent.sh"
    return (
        f"{shlex.quote(str(script))} --repo-root {{repo_root}} --prompt-file {{prompt_file}} "
        "--context-file {context_file} --handoff-file {handoff_file} --slice-id {slice_id}"
    )


def _subprocess_runner(command: str, workspace: Path, env: Mapping[str, str], timeout: int) -> int:
    result = subprocess.run(command, cwd=workspace, shell=True, env=dict(env), timeout=timeout)
    return result.returncode


def _archive_workspace(workspace: Path, out_tar: Path) -> None:
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_tar, "w:gz") as tar:
        for entry in sorted(workspace.iterdir()):
            if entry.name == ".git":
                continue
            tar.add(entry, arcname=entry.name)


class SupervisorAgentAdapter:
    """Drive the repo-automation supervisor loop to produce a submission, locally."""

    def __init__(
        self,
        repo_root: Path,
        agent_command_template: Optional[str] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        env: Optional[Mapping[str, str]] = None,
        runner: Optional[CommandRunner] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._template = agent_command_template or _default_command_template(self._repo_root)
        self._timeout = timeout_seconds
        self._env = dict(env) if env is not None else None
        self._runner = runner or _subprocess_runner

    def _run_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self._env:
            environment.update(self._env)
        return environment

    def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
        out_tar = Path(out_tar)
        slice_record = build_slice_record(task)
        queue_data = build_queue_data(slice_record, self._template, self._timeout)
        context_bundle = build_context_bundle(queue_data, slice_record)

        workspace = Path(tempfile.mkdtemp(prefix=f"pb-{task.instance_id}-"))
        # run_agent.sh refuses a non-git repo root; the workspace is the agent's repo.
        subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)

        control_dir = Path(tempfile.mkdtemp(prefix=f"pb-control-{task.instance_id}-"))
        handoff_path = control_dir / "handoff.json"  # must not pre-exist for run_agent.sh
        prompt_path = control_dir / "prompt.md"
        context_path = control_dir / "context.json"

        prompt_text = run_next.render_prompt(
            repo_root=self._repo_root,
            slice_record=slice_record,
            context_bundle=context_bundle,
            handoff_path=handoff_path,
        )
        prompt_path.write_text(prompt_text, encoding="utf-8")
        context_path.write_text(json.dumps(context_bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        command = run_next.format_agent_command(
            command_template=self._template,
            repo_root=workspace,
            prompt_path=prompt_path,
            context_path=context_path,
            handoff_path=handoff_path,
            slice_id=slice_record["slice_id"],
        )
        returncode = self._runner(command, workspace, self._run_environment(), self._timeout)

        _archive_workspace(workspace, out_tar)
        return SubmissionResult(
            instance_id=task.instance_id,
            tar_path=out_tar,
            workspace=workspace,
            returncode=returncode,
        )
