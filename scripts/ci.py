"""Offline CI policy and source-release tooling; never imports the game client."""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = (
    "blocking-ci.yml",
    "automation-check.yml",
    "python-tests.yml",
    "postmerge-ci.yml",
    "release.yml",
)
MAX_BLOB_BYTES = 1024 * 1024
RELEASE_ROOT_FILES = ("run.py", "requirements.txt", "LICENSE", "README.md")
# Non-Python files under sdk/ that the code reads at run time. Anything else under sdk/ is left out.
RELEASE_SDK_DATA_SUFFIXES = (".json",)
RELEASE_EXCLUDED_DIRS = frozenset({"tests", "test", "__pycache__"})
# Package data whose path contains one of these words is refused, not silently shipped or dropped.
RELEASE_PRIVATE_WORDS = frozenset(
    """
    secret secrets password passwords passwd token tokens credential credentials cookie cookies session sessions
    auth capture captures device devices profile profiles dump dumps har pcap log logs env
    """.split()
)
VERSION_RE = re.compile(r"v?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))\Z")


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def require_success(results: object, required: list[str]) -> None:
    """Missing, skipped, cancelled, malformed and failed jobs all fail the gate."""
    if not required or len(set(required)) != len(required):
        raise ValueError("required jobs must be nonempty and unique")
    if not isinstance(results, dict) or set(results) != set(required):
        raise ValueError("upstream results do not match the required jobs")
    for name in required:
        result = results[name]
        if not isinstance(result, dict) or result.get("result") != "success":
            raise ValueError(f"required job did not succeed: {name}")


def lint_counts(diagnostics: list[dict], repo: Path) -> Counter:
    counts = Counter()
    for item in diagnostics:
        path = Path(item["filename"]).resolve().relative_to(repo.resolve()).as_posix()
        counts[(path, item["code"], item["message"])] += 1
    return counts


def lint_baseline(payload: dict) -> Counter:
    if payload.get("schema_version") != 1 or not isinstance(payload.get("violations"), list):
        raise ValueError("invalid lint baseline schema")
    counts = Counter()
    for item in payload["violations"]:
        key = (item["path"], item["code"], item["message"])
        if key in counts or type(item["count"]) is not int or item["count"] <= 0:
            raise ValueError("duplicate or invalid lint baseline entry")
        counts[key] = item["count"]
    return counts


def check_lint(repo: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "run.py", "sdk", "tests", "scripts", "--output-format", "json"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if run.returncode not in (0, 1):
        raise ValueError(f"Ruff could not run: {run.stderr.strip()}")
    actual = lint_counts(json.loads(run.stdout), repo)
    expected = lint_baseline(json.loads((repo / ".github/ruff-baseline.json").read_text(encoding="utf-8")))
    if actual != expected:
        for key, count in sorted((actual - expected).items()):
            print(f"NEW ({count}): {key[0]}: {key[1]} {key[2]}")
        for key, count in sorted((expected - actual).items()):
            print(f"REMOVE FIXED BASELINE ENTRY ({count}): {key[0]}: {key[1]} {key[2]}")
        raise ValueError("lint findings changed; fix new findings and remove resolved baseline entries")
    print(f"No new lint findings; {sum(expected.values())} explicitly recorded legacy findings remain.")


def workflow_documents(repo: Path) -> dict:
    import yaml

    # BaseLoader preserves GitHub's YAML 1.2 `on` key (SafeLoader treats it as True).
    return {
        name: yaml.load((repo / ".github/workflows" / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for name in WORKFLOWS
    }


def check_workflows(documents: dict) -> None:
    for name, document in documents.items():
        if document.get("permissions") != {"contents": "read"}:
            raise ValueError(f"workflow needs read-only default permissions: {name}")
        if "pull_request_target" in document.get("on", {}):
            raise ValueError("privileged pull-request execution is forbidden")
        for job_name, job in document["jobs"].items():
            allowed_permissions = (
                {"contents": "write"} if (name, job_name) == ("release.yml", "publish") else {"contents": "read"}
            )
            if job.get("permissions", {"contents": "read"}) != allowed_permissions:
                raise ValueError(f"unexpected job permissions: {name}/{job_name}")
            if "runs-on" in job and "timeout-minutes" not in job:
                raise ValueError(f"job lacks a timeout: {name}")
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if action and not re.fullmatch(r"[\w.-]+/[\w.-]+@[a-f0-9]{40}", action):
                    raise ValueError(f"external action must be pinned to a commit: {action}")
                if (
                    action.startswith("actions/checkout@")
                    and step.get("with", {}).get("persist-credentials") != "false"
                ):
                    raise ValueError("checkout must not persist its token")
    blocking = documents["blocking-ci.yml"]
    if not {"pull_request", "merge_group"} <= set(blocking["on"]):
        raise ValueError("blocking CI must cover pull requests and merge queues")
    if any("paths" in event or "paths-ignore" in event for event in blocking["on"].values() if isinstance(event, dict)):
        raise ValueError("required workflow must not skip changes via path filters")
    for filename in ("blocking-ci.yml", "postmerge-ci.yml"):
        gate = documents[filename]["jobs"]["required"]
        if gate.get("if") != "${{ always() }}" or gate.get("needs") != ["checks", "tests"]:
            raise ValueError("aggregate gate must always evaluate all required workflows")
    release = documents["release.yml"]
    jobs = release["jobs"]
    if "workflow_dispatch" not in release["on"]:
        raise ValueError("source release must support manual verification")
    if jobs["build"].get("if") != "${{ github.event_name == 'push' || github.ref == 'refs/heads/main' }}":
        raise ValueError("manual release verification must build only main")
    if jobs["verify"].get("needs") != "build" or jobs["publish"].get("needs") != "verify":
        raise ValueError("release publication must depend on downloaded artifact verification")
    if jobs["publish"].get("if") != "${{ github.event_name == 'push' && github.ref_type == 'tag' }}":
        raise ValueError("release publication must require a real tag push")


def check_repository(repo: Path) -> None:
    check_workflows(workflow_documents(repo))
    paths = [Path(os.fsdecode(name)) for name in git(repo, "ls-files", "-z").split(b"\0") if name]
    for relative in paths:
        path = repo / relative
        if path.lstat().st_size > MAX_BLOB_BYTES:
            raise ValueError(f"tracked file exceeds the 1 MiB blob policy: {relative}")
        if path.suffix == ".py" and not path.is_symlink():
            compile(path.read_bytes(), str(relative), "exec")
    git(repo, "diff", "--check")
    if git(repo, "rev-list", "--max-parents=0", "HEAD").strip() != git(repo, "rev-parse", "HEAD").strip():
        git(repo, "diff", "--check", "HEAD^", "HEAD")
    else:
        git(repo, "show", "--check", "--format=", "HEAD")
    print("Workflow policies, tracked-file syntax, blob sizes and whitespace passed.")


def release_identity(repo: Path, tag: str, main_ref: str) -> tuple[str, str, int]:
    match = VERSION_RE.fullmatch(tag)
    if not match:
        raise ValueError("release tag must be X.Y.Z or vX.Y.Z without leading zeroes")
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    if git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}").decode().strip() != commit:
        raise ValueError("release tag does not identify the checked-out commit")
    git(repo, "merge-base", "--is-ancestor", commit, main_ref)
    epoch = int(git(repo, "show", "-s", "--format=%ct", commit))
    return match[1], commit, epoch


def release_includes(path: str) -> bool:
    """Select a tracked path for the source archive, refusing sdk/ data that looks private."""
    pure = PurePosixPath(path)
    if path in RELEASE_ROOT_FILES:
        return True
    if pure.parts[0] != "sdk" or RELEASE_EXCLUDED_DIRS.intersection(pure.parts[:-1]):
        return False
    if pure.suffix == ".py":
        return True
    if pure.suffix not in RELEASE_SDK_DATA_SUFFIXES:
        return False
    words = set(re.split(r"[^a-z0-9]+", path.lower()))
    if words & RELEASE_PRIVATE_WORDS or any(part.startswith(".") for part in pure.parts):
        raise ValueError(f"release refuses package data that looks like a secret, log or capture: {path}")
    return True


def build_release(repo: Path, tag: str, output: Path, main_ref: str = "origin/main") -> dict:
    """Build from Git objects, never from dirty files, logs or local credentials."""
    version, commit, epoch = release_identity(repo, tag, main_ref)
    tree = git(repo, "ls-tree", "-rz", commit)
    members = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, object_id = meta.split()
        path = os.fsdecode(name)
        pure = PurePosixPath(path)
        if not release_includes(path):
            continue
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError(f"release source must be a regular file: {path}")
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("unsafe archive member")
        contents = git(repo, "cat-file", "blob", object_id.decode())
        if pure.parts[0] == "sdk" and pure.suffix == ".json":
            try:
                json.loads(contents.decode("utf-8"))
            except ValueError:
                raise ValueError(f"release package data is not valid UTF-8 JSON: {path}") from None
        members.append((path, mode, contents))
    names = {path for path, _, _ in members}
    if not set(RELEASE_ROOT_FILES) <= names or "sdk/client.py" not in names:
        raise ValueError("release is missing required application source files")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"tachikoma-{version}.tar.gz"
    with archive.open("wb") as stream:
        with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as package:
                for path, mode, contents in sorted(members):
                    info = tarfile.TarInfo(f"tachikoma-{version}/{path}")
                    info.size, info.mtime = len(contents), epoch
                    info.mode = 0o755 if mode == b"100755" else 0o644
                    package.addfile(info, io.BytesIO(contents))
    manifest = {"version": version, "tag": tag, "commit": commit, "source_date_epoch": epoch, "files": len(members)}
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in (archive, manifest_path)
    )
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8", newline="\n")
    return manifest


def close_root_log_files_under(directory: Path) -> None:
    """Close and detach root-logger file handlers whose file lies inside ``directory``."""
    directory = Path(os.path.realpath(directory))
    for handler in list(logging.root.handlers):
        if isinstance(handler, logging.FileHandler):
            if Path(os.path.realpath(handler.baseFilename)).is_relative_to(directory):
                logging.root.removeHandler(handler)
                handler.close()


def run_tests(names: list[str]) -> int:
    """Run mocked tests and fail even when a forbidden network error is swallowed."""
    attempts = []

    def forbid_network(event, arguments):
        if event in {"socket.connect", "http.client.connect"}:
            attempts.append(event)
            raise RuntimeError("CI tests must mock network traffic")

    # Audit hooks cannot be removed; this command deliberately owns its interpreter.
    sys.addaudithook(forbid_network)
    sys.path.insert(0, str(ROOT))
    # Tests that construct Device() or import run.py write ./.device and ./tachikoma.log. Running them in a
    # fresh, empty working directory keeps them away from the real credential files in the repository root.
    previous_cwd = os.getcwd()
    with TemporaryDirectory(prefix="tachikoma-test-", ignore_cleanup_errors=True) as scratch:
        os.chdir(scratch)
        try:
            suite = (
                unittest.defaultTestLoader.loadTestsFromNames(names)
                if names
                else unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
            )
            result = unittest.TextTestRunner().run(suite)
        finally:
            # Leave the directory and close log files opened in it (run.py logs to ./tachikoma.log) before it is
            # removed: Windows cannot delete the working directory or an open file.
            os.chdir(previous_cwd)
            close_root_log_files_under(Path(scratch))
    if attempts:
        print(f"CI rejected {len(attempts)} unmocked network connection attempt(s).", file=sys.stderr)
    return 0 if result.wasSuccessful() and not attempts else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate")
    gate.add_argument("--required", nargs="+", required=True)
    commands.add_parser("lint")
    commands.add_parser("repository")
    tests = commands.add_parser("test")
    tests.add_argument("--names", nargs="+", default=[])
    release = commands.add_parser("release")
    release.add_argument("--tag", required=True)
    release.add_argument("--main-ref", default="origin/main")
    release.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    try:
        if args.command == "gate":
            require_success(json.loads(os.environ.get("NEEDS_JSON", "null")), args.required)
            print("Every required workflow succeeded.")
        elif args.command == "lint":
            check_lint(ROOT)
        elif args.command == "repository":
            check_repository(ROOT)
        elif args.command == "test":
            return run_tests(args.names)
        else:
            print(json.dumps(build_release(ROOT, args.tag, args.output, args.main_ref), sort_keys=True))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"CI check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
