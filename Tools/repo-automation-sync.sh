#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 - "$ROOT" "$@" <<'PY'
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


FORBIDDEN_SOURCE_PREFIXES = (
    ".githooks/",
    "SecondBrain/",
    "automation/handoffs/",
    "automation/proofs/",
    "automation/queue/",
    "automation/smoke/",
    "docs/product/",
    "docs/runtime/",
    "localization/",
    "owlory_xcode/",
)

FORBIDDEN_SOURCE_FILES = {
    ".githooks/pre-push",
    "Makefile",
    "Tools/bump-version.sh",
    "Tools/generate-build-info.sh",
    "Tools/release-preflight.sh",
    "Tools/set-build-number.sh",
    "Tools/verify-build-provenance.sh",
}

SKIP_NAMES = {"__pycache__", ".DS_Store"}


@dataclass(frozen=True)
class Entry:
    source: str
    destination: str
    kind: str
    preserve_executable: bool
    delete_stale: bool
    template: bool
    allow_owlory_specific: bool


class SyncError(Exception):
    pass


def fail(message: str) -> int:
    print(f"repo-automation-sync: error: {message}", file=sys.stderr)
    return 2


def normalize_relative_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SyncError(f"{field_name} must be a non-empty string")

    path = PurePosixPath(value)
    if path.is_absolute():
        raise SyncError(f"{field_name} must be repo-relative, got absolute path {value!r}")

    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SyncError(f"{field_name} must not contain '.', '..', or empty path parts: {value!r}")

    return path.as_posix()


def bool_field(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise SyncError(f"manifest entry {raw.get('source', '<unknown>')!r} has non-boolean {key}")
    return value


def parse_entry(raw: object) -> Entry:
    if not isinstance(raw, dict):
        raise SyncError("manifest entries must be objects")

    source = normalize_relative_path(raw.get("source"), "source")
    destination = normalize_relative_path(raw.get("destination"), "destination")

    kind = raw.get("kind")
    if kind not in {"file", "directory"}:
        raise SyncError(f"manifest entry {source!r} has unsupported kind {kind!r}")

    allow_owlory_specific = bool(raw.get("allow_owlory_specific", False))
    if not allow_owlory_specific:
        if source in FORBIDDEN_SOURCE_FILES:
            raise SyncError(f"forbidden Owlory-specific source requires explicit approval: {source}")
        for prefix in FORBIDDEN_SOURCE_PREFIXES:
            if source == prefix.rstrip("/") or source.startswith(prefix):
                raise SyncError(f"forbidden Owlory-specific source requires explicit approval: {source}")

    return Entry(
        source=source,
        destination=destination,
        kind=kind,
        preserve_executable=bool_field(raw, "preserve_executable"),
        delete_stale=bool_field(raw, "delete_stale"),
        template=bool_field(raw, "template"),
        allow_owlory_specific=allow_owlory_specific,
    )


def load_manifest(path: Path) -> tuple[Path, list[Entry]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SyncError(f"manifest not found at {path}") from error
    except json.JSONDecodeError as error:
        raise SyncError(f"manifest is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise SyncError("manifest root must be an object")
    if data.get("version") != 1:
        raise SyncError("manifest version must be 1")

    default_target_raw = data.get("default_target")
    if not isinstance(default_target_raw, str) or not default_target_raw:
        raise SyncError("manifest default_target must be a non-empty string")
    default_target = Path(default_target_raw).expanduser()

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SyncError("manifest entries must be a non-empty list")

    return default_target, [parse_entry(raw) for raw in raw_entries]


def resolve_under(base: Path, relative: str, *, must_exist: bool) -> Path:
    base_resolved = base.resolve(strict=False)
    candidate = base.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as error:
        raise SyncError(f"source path does not exist: {relative}") from error

    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise SyncError(f"path escapes root: {relative}")
    return resolved


def should_skip(path: Path) -> bool:
    return any(part in SKIP_NAMES for part in path.parts)


def source_files(source_root: Path, target_root: Path, entry: Entry) -> list[tuple[Path, Path, str]]:
    source = resolve_under(source_root, entry.source, must_exist=True)
    destination = resolve_under(target_root, entry.destination, must_exist=False)

    if source.is_symlink():
        raise SyncError(f"symlink sources are not supported: {entry.source}")

    if entry.kind == "file":
        if not source.is_file():
            raise SyncError(f"manifest entry {entry.source} is not a file")
        return [(source, destination, entry.destination)]

    if not source.is_dir():
        raise SyncError(f"manifest entry {entry.source} is not a directory")

    pairs: list[tuple[Path, Path, str]] = []
    for child in sorted(source.rglob("*")):
        if should_skip(child):
            continue
        if child.is_symlink():
            raise SyncError(f"symlink sources are not supported: {child.relative_to(source_root)}")
        if child.is_dir():
            continue
        relative = child.relative_to(source)
        target = destination / relative
        display = PurePosixPath(entry.destination, relative.as_posix()).as_posix()
        pairs.append((child, target, display))
    return pairs


def executable_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode) & 0o111


def set_target_mode(source: Path, target: Path, *, preserve_executable: bool) -> None:
    if preserve_executable:
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
    else:
        os.chmod(target, 0o644)


def file_drift(source: Path, target: Path, *, preserve_executable: bool) -> str | None:
    if not target.exists():
        return "missing"
    if not target.is_file():
        return "type"
    if source.read_bytes() != target.read_bytes():
        return "changed"
    if preserve_executable and executable_bits(source) != executable_bits(target):
        return "mode"
    return None


def stale_files(target_root: Path, entry: Entry, expected: set[Path]) -> list[tuple[Path, str]]:
    if entry.kind != "directory" or not entry.delete_stale or entry.template:
        return []

    destination = resolve_under(target_root, entry.destination, must_exist=False)
    if not destination.exists():
        return []

    stale: list[tuple[Path, str]] = []
    for child in sorted(destination.rglob("*")):
        if should_skip(child):
            continue
        if child.is_dir():
            continue
        if child not in expected:
            display = child.relative_to(target_root).as_posix()
            stale.append((child, display))
    return stale


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for child in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        try:
            child.rmdir()
        except OSError:
            pass


def sync_entries(
    source_root: Path,
    target_root: Path,
    entries: list[Entry],
    *,
    check: bool,
    force_templates: bool = False
) -> int:
    target_root_resolved = target_root.resolve(strict=False)
    issues: list[str] = []
    changes = 0

    for entry in entries:
        pairs = source_files(source_root, target_root_resolved, entry)
        expected = {target for _, target, _ in pairs}

        for source, target, display in pairs:
            if entry.template and target.exists() and not force_templates:
                continue

            drift = file_drift(source, target, preserve_executable=entry.preserve_executable)
            if drift is None:
                continue

            if check:
                issues.append(f"{drift}: {display}")
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            set_target_mode(source, target, preserve_executable=entry.preserve_executable)
            print(f"synced: {display}")
            changes += 1

        for stale, display in stale_files(target_root_resolved, entry, expected):
            if check:
                issues.append(f"stale: {display}")
                continue
            stale.unlink()
            print(f"removed stale: {display}")
            changes += 1

        if not check and entry.kind == "directory" and entry.delete_stale:
            remove_empty_dirs(resolve_under(target_root_resolved, entry.destination, must_exist=False))

    if check:
        if issues:
            for issue in sorted(issues):
                print(issue)
            print(f"result: drift found ({len(issues)} issue(s))")
            return 1
        print("result: target is current")
        return 0

    if changes:
        print(f"result: synced {changes} change(s)")
    else:
        print("result: already current")
    return 0


def target_git_status(target_root: Path) -> tuple[bool, list[str]]:
    git_root = subprocess.run(
        ["git", "-C", str(target_root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if git_root.returncode != 0:
        return False, []

    status = subprocess.run(
        ["git", "-C", str(target_root), "status", "--short", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in status.stdout.splitlines() if line.strip()]
    return True, lines


def ensure_auto_update_target_safe(target_root: Path) -> int:
    if not target_root.exists():
        print(f"missing target: {target_root}")
        print("result: auto-update refused (target missing)")
        print("remediation: run the repo-automation bootstrap slice or sync the target explicitly with --sync first")
        return 1

    is_git_repo, dirty_lines = target_git_status(target_root)
    if not is_git_repo:
        print(f"repo-automation-sync: error: auto-update target is not a Git repository: {target_root}", file=sys.stderr)
        print("remediation: initialize the external repo-automation folder before enabling automatic updates", file=sys.stderr)
        return 2

    if dirty_lines:
        print(f"repo-automation-sync: error: refusing auto-update because target has local dirt: {target_root}", file=sys.stderr)
        for line in dirty_lines:
            print(f"  {line}", file=sys.stderr)
        print("remediation: commit, stash, or clean the external repo-automation worktree, then retry", file=sys.stderr)
        return 1

    return 0


def main(argv: list[str]) -> int:
    repo_root = Path(argv[0]).resolve()

    parser = argparse.ArgumentParser(description="Mirror Owlory reusable automation into an external repo-automation folder.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Report drift without changing files.")
    mode.add_argument("--sync", action="store_true", help="Update the target to match the reusable manifest.")
    mode.add_argument("--auto-update", action="store_true", help="Safely update an existing clean Git target, then verify it is current.")
    parser.add_argument("--target", help="Destination repo-automation folder. Defaults to manifest default_target.")
    parser.add_argument("--source", default=str(repo_root), help="Source repository root. Defaults to this checkout.")
    parser.add_argument("--manifest", help="Manifest path. Defaults to <source>/automation/reusable-manifest.json.")
    parser.add_argument(
        "--force-templates",
        action="store_true",
        help="Re-baseline template entries to source content even when destination files already exist. "
        "Consumer-added files in template directories still survive."
    )
    args = parser.parse_args(argv[1:])

    source_root = Path(args.source).expanduser().resolve(strict=True)
    manifest = Path(args.manifest).expanduser() if args.manifest else source_root / "automation/reusable-manifest.json"
    default_target, entries = load_manifest(manifest)
    target_root = Path(args.target).expanduser() if args.target else default_target

    if args.auto_update:
        safe_result = ensure_auto_update_target_safe(target_root)
        if safe_result != 0:
            return safe_result
        sync_result = sync_entries(
            source_root,
            target_root,
            entries,
            check=False,
            force_templates=args.force_templates
        )
        if sync_result != 0:
            return sync_result
        return sync_entries(
            source_root,
            target_root,
            entries,
            check=True,
            force_templates=args.force_templates
        )

    if args.sync:
        target_root.mkdir(parents=True, exist_ok=True)
    elif not target_root.exists():
        print(f"missing target: {target_root}")
        print("result: drift found (target missing)")
        return 1

    return sync_entries(
        source_root,
        target_root,
        entries,
        check=args.check,
        force_templates=args.force_templates
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SyncError as error:
        raise SystemExit(fail(str(error)))
PY
