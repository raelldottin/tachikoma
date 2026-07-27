from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from automation.supervisor import policy


POLICY_SENTENCE = (
    "A run may recommend the next slice, but only the supervisor may continue, "
    "and only into a pre-classified adjacent slice that passes scope, validation, "
    "and diff-budget gates."
)

CORE_DOCS = [
    ("AGENTS.md", "repo operating contract"),
    ("automation/README.md", "automation harness loop"),
    ("Gymphant/Docs/ARCHITECTURE.md", "boundary model"),
    ("docs/architecture/boundaries.md", "boundary model"),
    ("docs/workflows/validation.md", "validation workflow"),
    ("docs/workflows/agent-handoff.md", "handoff workflow"),
]

DOMAIN_DOCS = {
    "automation": [("automation/README.md", "automation harness contract")],
    "repo-tooling": [("automation/README.md", "automation harness contract")],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a bounded context bundle for one queued slice.")
    parser.add_argument("--slice-id", required=True, help="Queue slice_id to package.")
    parser.add_argument("--queue", default="automation/queue/slices.json", help="Path to the live or example queue JSON file.")
    parser.add_argument("--handoff-dir", default="automation/handoffs", help="Directory containing prior handoff artifacts.")
    parser.add_argument("--output", help="Optional file path for the generated JSON bundle. Defaults to stdout.")
    parser.add_argument(
        "--max-doc-chars", type=int, default=2500, help="Maximum characters to keep from each selected document excerpt."
    )
    return parser.parse_args()


def build_context_bundle(
    repo_root: Path, queue_path: Path, handoff_dir: Path, slice_id: str, max_doc_chars: int = 2500
) -> dict[str, Any]:
    queue_schema_path = repo_root / "automation/schemas/slice.schema.json"
    handoff_schema_path = repo_root / "automation/schemas/handoff.schema.json"
    queue_data = policy.load_queue(queue_path, queue_schema_path)
    slice_record = policy.find_slice(queue_data, slice_id)
    if slice_record is None:
        raise KeyError(f"Unknown slice_id: {slice_id}")

    previous_handoff = find_previous_handoff(
        queue_data=queue_data, slice_record=slice_record, handoff_dir=handoff_dir, handoff_schema_path=handoff_schema_path
    )
    compact_previous_handoff = compact_handoff(previous_handoff)

    document_specs = resolve_document_specs(slice_record=slice_record, repo_root=repo_root)
    documents = load_documents(repo_root, document_specs, max_doc_chars=max_doc_chars)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy_sentence": POLICY_SENTENCE,
        "repo": {
            "root": str(repo_root),
            "branch": git_output(repo_root, ["git", "branch", "--show-current"]),
            "head": git_output(repo_root, ["git", "rev-parse", "HEAD"]),
        },
        "slice": summarize_slice(slice_record),
        "queue": build_queue_metadata(queue_data, slice_record),
        "validation_ownership": summarize_validation_ownership(slice_record),
        "previous_handoff": compact_previous_handoff,
        "previous_handoff_summary": render_previous_handoff_summary(compact_previous_handoff),
        "acceptance_checks": build_acceptance_checks(slice_record),
        "handoff_template": build_handoff_template(slice_record),
        "documents": documents,
    }


def summarize_slice(slice_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "slice_id": slice_record["slice_id"],
        "title": slice_record["title"],
        "status": slice_record["status"],
        "priority": slice_record["priority"],
        "domain": slice_record["domain"],
        "allowed_paths": slice_record["allowed_paths"],
        "required_validations": slice_record["required_validations"],
        "depends_on": slice_record["depends_on"],
        "max_files_changed": slice_record["max_files_changed"],
        "notes": slice_record["notes"],
    }


def build_queue_metadata(queue_data: dict[str, Any], slice_record: dict[str, Any]) -> dict[str, Any]:
    dependency_status: list[dict[str, Any]] = []
    for dependency_id in slice_record["depends_on"]:
        dependency_record = policy.find_slice(queue_data, dependency_id)
        dependency_status.append(
            {
                "slice_id": dependency_id,
                "title": dependency_record["title"] if dependency_record else "",
                "status": dependency_record["status"] if dependency_record else "missing",
            }
        )

    adjacent_queued_slices: list[tuple[int, int, dict[str, Any]]] = []
    for index, candidate in enumerate(queue_data["slices"]):
        if candidate["slice_id"] == slice_record["slice_id"]:
            continue
        if candidate["status"] != "queued":
            continue
        if (
            candidate["domain"] == slice_record["domain"]
            or slice_record["slice_id"] in candidate["depends_on"]
            or candidate["slice_id"] in slice_record["depends_on"]
        ):
            adjacent_queued_slices.append((candidate["priority"], index, candidate))

    adjacent_queued_slices.sort(key=lambda item: (item[0], item[1]))

    return {
        "version": queue_data["version"],
        "policy": {
            "consecutive_autonomous_limit": queue_data["policy"]["consecutive_autonomous_limit"],
            "handoff_timeout_seconds": queue_data["policy"]["handoff_timeout_seconds"],
        },
        "dependency_status": dependency_status,
        "adjacent_queued_slices": [
            {
                "slice_id": candidate["slice_id"],
                "title": candidate["title"],
                "priority": candidate["priority"],
                "domain": candidate["domain"],
                "depends_on": candidate["depends_on"],
                "notes": candidate["notes"],
            }
            for _, _, candidate in adjacent_queued_slices[:5]
        ],
    }


def summarize_validation_ownership(slice_record: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"command": ownership.command, "tier": ownership.tier, "reason": ownership.reason}
        for ownership in policy.classify_validation_ownerships(slice_record["required_validations"])
    ]


def build_acceptance_checks(slice_record: dict[str, Any]) -> list[str]:
    return [
        f"Stay within allowed_paths: {', '.join(slice_record['allowed_paths'])}",
        f"Keep changed files at or below max_files_changed={slice_record['max_files_changed']}",
        "Preserve ownership boundaries for domain, application, and adapter code",
        f"Run required validations exactly: {', '.join(slice_record['required_validations'])}",
        "Name the highest proof level reached and any relevant missing proof levels",
        "Write one honest JSON handoff before exiting",
    ]


def build_handoff_template(slice_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "slice_id": slice_record["slice_id"],
        "status": "done",
        "summary": f"<describe what changed for {slice_record['title'].lower()}>",
        "files_touched": ["<repo-relative-path>"],
        "validations_passed": slice_record["required_validations"],
        "validations_failed": [],
        "proof_level": "<highest proof level reached>",
        "missing_proof_levels": ["<proof levels still missing, if relevant>"],
        "contract_status_changes": [
            {
                "contract": "<contract or product/workflow rule>",
                "before": "<status before this slice>",
                "after": "<status after this slice>",
                "proof": ["<validation or artifact supporting the change>"],
            }
        ],
        "residual_risks": ["<remaining risk or 'No known residual risk.'>"],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
        "repo_clean_status": "<clean|dirty|unknown>",
        "git_mirror_status": "<mirrored|not-mirrored|not-relevant|not-checked>",
        "dirty_paths_outside_scope": [],
        "timestamp": "<UTC ISO-8601 timestamp>",
    }


def compact_handoff(handoff: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if handoff is None:
        return None

    return {
        "slice_id": handoff["slice_id"],
        "status": handoff["status"],
        "summary": handoff["summary"],
        "files_touched": sorted(set(handoff["files_touched"])),
        "validations_passed": handoff["validations_passed"],
        "validations_failed": handoff["validations_failed"],
        "proof_level": handoff.get("proof_level", "legacy-unknown"),
        "missing_proof_levels": handoff.get("missing_proof_levels", []),
        "contract_status_changes": handoff.get("contract_status_changes", []),
        "residual_risks": handoff.get("residual_risks", handoff.get("risks", [])),
        "recommended_next_slice": handoff["recommended_next_slice"],
        "recommended_next_reason": handoff["recommended_next_reason"],
        "repo_clean_status": handoff.get("repo_clean_status", "legacy-unknown"),
        "git_mirror_status": handoff.get("git_mirror_status", "legacy-unknown"),
        "timestamp": handoff["timestamp"],
    }


def render_previous_handoff_summary(handoff: Optional[dict[str, Any]]) -> str:
    if handoff is None:
        return "No previous handoff is available for this slice."

    parts = [f"Previous slice `{handoff['slice_id']}` ended with `{handoff['status']}`.", handoff["summary"]]

    if handoff["files_touched"]:
        parts.append("Files touched: " + ", ".join(f"`{path}`" for path in handoff["files_touched"]))
    if handoff["validations_passed"]:
        parts.append("Validations passed: " + ", ".join(f"`{command}`" for command in handoff["validations_passed"]))
    if handoff["proof_level"]:
        parts.append(f"Proof level: `{handoff['proof_level']}`")
    if handoff["missing_proof_levels"]:
        parts.append("Missing proof levels: " + ", ".join(f"`{level}`" for level in handoff["missing_proof_levels"]))
    if handoff["contract_status_changes"]:
        parts.append(
            "Contract status changes: "
            + "; ".join(render_contract_status_change(change) for change in handoff["contract_status_changes"])
        )
    if handoff["residual_risks"]:
        parts.append("Residual risks: " + "; ".join(handoff["residual_risks"]))
    if handoff["repo_clean_status"]:
        parts.append(f"Repo clean status: `{handoff['repo_clean_status']}`")
    if handoff["git_mirror_status"]:
        parts.append(f"Git mirror status: `{handoff['git_mirror_status']}`")
    if handoff["recommended_next_slice"]:
        parts.append(
            f"Recommended next slice: `{handoff['recommended_next_slice']}` because {handoff['recommended_next_reason']}"
        )

    return "\n".join(f"- {part}" for part in parts)


def render_contract_status_change(change: dict[str, Any]) -> str:
    contract = change.get("contract", "unknown contract")
    before = change.get("before", "unknown")
    after = change.get("after", "unknown")
    proof = change.get("proof", [])
    proof_text = ", ".join(f"`{item}`" for item in proof) if proof else "`no proof listed`"
    return f"{contract}: {before} -> {after} ({proof_text})"


def find_previous_handoff(
    queue_data: dict[str, Any], slice_record: dict[str, Any], handoff_dir: Path, handoff_schema_path: Path
) -> Optional[dict[str, Any]]:
    dependency_ids = list(reversed(slice_record["depends_on"]))
    for dependency_id in dependency_ids:
        handoff = policy.latest_handoff_for_slice(handoff_dir, dependency_id, handoff_schema_path)
        if handoff is not None:
            return handoff

    slices = queue_data["slices"]
    current_index = next(index for index, record in enumerate(slices) if record["slice_id"] == slice_record["slice_id"])
    for index in range(current_index - 1, -1, -1):
        previous_id = slices[index]["slice_id"]
        handoff = policy.latest_handoff_for_slice(handoff_dir, previous_id, handoff_schema_path)
        if handoff is not None:
            return handoff
    return None


def resolve_document_specs(slice_record: dict[str, Any], repo_root: Path) -> list[tuple[str, str]]:
    specs = list(CORE_DOCS)
    specs.extend(DOMAIN_DOCS.get(slice_record["domain"], []))
    if slice_record["domain"] not in DOMAIN_DOCS:
        domain_index = "docs/product/domain-index.md"
        if (repo_root / domain_index).exists():
            specs.append((domain_index, "owner lookup"))
    specs.extend(slice_scoped_doc_specs(slice_record, repo_root))

    fallback_domain_doc = f"docs/product/domains/{slice_record['domain']}.md"
    if (repo_root / fallback_domain_doc).exists():
        specs.append((fallback_domain_doc, "domain-specific contract"))

    unique_specs: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for path, reason in specs:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        unique_specs.append((path, reason))
    return unique_specs


def slice_scoped_doc_specs(slice_record: dict[str, Any], repo_root: Path) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    for allowed_path in slice_record["allowed_paths"]:
        if allowed_path.startswith("SecondBrain/"):
            continue
        if allowed_path.endswith(".md") and (repo_root / allowed_path).exists():
            specs.append((allowed_path, "slice-scoped maintained doc"))
        elif allowed_path in {"README.md", "AGENTS.md"} and (repo_root / allowed_path).exists():
            specs.append((allowed_path, "slice-scoped maintained doc"))
    return specs


def load_documents(repo_root: Path, document_specs: list[tuple[str, str]], max_doc_chars: int) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for relative_path, reason in document_specs:
        full_path = repo_root / relative_path
        if not full_path.exists():
            continue
        excerpt, truncated = compact_document_excerpt(full_path.read_text(encoding="utf-8"), max_doc_chars=max_doc_chars)
        documents.append({"path": relative_path, "reason": reason, "truncated": truncated, "excerpt": excerpt})
    return documents


def compact_document_excerpt(text: str, max_doc_chars: int) -> tuple[str, bool]:
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(normalized) <= max_doc_chars:
        return normalized, False
    return normalized[:max_doc_chars].rstrip() + "\n...[truncated]\n", True


def git_output(repo_root: Path, command: list[str]) -> str:
    result = subprocess.run(command, cwd=repo_root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def main() -> int:
    args = parse_args()
    repo_root = REPO_ROOT
    queue_path = repo_root / args.queue
    handoff_dir = repo_root / args.handoff_dir
    bundle = build_context_bundle(
        repo_root=repo_root,
        queue_path=queue_path,
        handoff_dir=handoff_dir,
        slice_id=args.slice_id,
        max_doc_chars=args.max_doc_chars,
    )

    payload = json.dumps(bundle, indent=2, ensure_ascii=False)
    if args.output:
        output_path = repo_root / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
        print(output_path)
        return 0

    print(payload)
    return 0


def _cli_entry() -> int:
    try:
        return main()
    except policy.ConfigError as error:
        print(f"stop: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli_entry())
