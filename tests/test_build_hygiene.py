"""Checkout-level checks of the local build entry points (the Makefile) and of .gitignore.

These tests read the repository checkout itself, so they are never a Bazel target. Every make command runs from
a fresh temporary working directory and only as a dry run (``make -n``), and the git commands only read, so
nothing here touches ./.device.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tests.test_strict_mode import skip_unless_dependency

ROOT = Path(os.path.abspath(__file__)).parent.parent
CONTRACT_TARGETS = (
    "automation-check",
    "automation-dry-run",
    "syntax-check",
    "test",
    "test-security",
    "git-check",
    "lint",
    "pre-commit",
)
PYTHON_311_GUARD = "this target needs Python >= 3.11"
# A test recipe ends with: "<interpreter>" "<root>/scripts/ci.py" test [--names ...]
CI_TEST_COMMAND = re.compile(r'"([^"]+)" "([^"]+)" test(?: --names (\S+))?$', re.MULTILINE)

# A missing make fails under TACHIKOMA_STRICT_DEPS=1; a non-POSIX host or a copy without the Makefile still skips.
REQUIRES_POSIX_MAKE = skip_unless_dependency(
    shutil.which("make"),
    "requires make, a POSIX shell and the repository Makefile",
    applicable=os.name == "posix" and (ROOT / "Makefile").is_file(),
)


@REQUIRES_POSIX_MAKE
class MakefileContractTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.cwd = directory.name
        # A make run that started this suite passes its variables (PYTHON=...) to child makes through MAKEFLAGS.
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONSAFEPATH", "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "PYTHON"}
        }

    def dry_run(self, target, *arguments):
        result = subprocess.run(
            ["make", "--no-print-directory", "-n", "-C", str(ROOT), target, *arguments],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def ci_test_command(self, recipe):
        match = CI_TEST_COMMAND.search(recipe)
        self.assertIsNotNone(match, recipe)
        return match.group(1), match.group(2), match.group(3)

    def test_every_contract_target_exists_and_is_phony(self):
        phony = set()
        for line in (ROOT / "Makefile").read_text(encoding="utf-8").splitlines():
            if line.startswith(".PHONY:"):
                phony.update(line.split(":", 1)[1].split())
        for target in CONTRACT_TARGETS:
            with self.subTest(target=target):
                self.assertIn(target, phony)
                self.dry_run(target, f"PYTHON={sys.executable}")

    def test_test_recipes_run_scripts_ci_from_a_removed_temporary_directory(self):
        for target, names in (("test", None), ("test-security", "tests.test_security")):
            with self.subTest(target=target):
                recipe = self.dry_run(target, f"PYTHON={sys.executable}")
                self.assertIn("mktemp -d", recipe)
                self.assertIn("trap 'rm -rf \"$tmp\"' EXIT", recipe)
                self.assertIn('cd "$tmp"', recipe)
                interpreter, script, selected = self.ci_test_command(recipe)
                self.assertEqual(interpreter, sys.executable)
                self.assertTrue(os.path.samefile(script, ROOT / "scripts" / "ci.py"), script)
                self.assertEqual(selected, names)
                self.assertNotIn("unittest discover", recipe)

    def test_test_recipe_interpreter_is_absolute_from_any_working_directory(self):
        # A relative PYTHON is resolved against the repository root, not the caller's working directory.
        cases = [(("PYTHON=.venv/bin/python",), ROOT / ".venv" / "bin" / "python")]
        if shutil.which("python3", path=self.env.get("PATH")):
            # Without PYTHON, an executable .venv interpreter or else python3 from PATH, by absolute path.
            cases.append(((), None))
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                interpreter, _, _ = self.ci_test_command(self.dry_run("test", *arguments))
                self.assertTrue(os.path.isabs(interpreter), interpreter)
                if expected is not None:
                    # make reports the physical directory (e.g. /private/tmp for /tmp on macOS).
                    self.assertEqual(os.path.basename(interpreter), expected.name)
                    self.assertEqual(
                        os.path.realpath(os.path.dirname(interpreter)), os.path.realpath(expected.parent), interpreter
                    )

    def test_lint_mirrors_the_ci_lint_job(self):
        recipe = self.dry_run("lint", f"PYTHON={sys.executable}")
        self.assertIn("ruff --version | grep -qx \"ruff $(sed -n 's/^ruff==//p' .github/requirements-ci.txt)\"", recipe)
        self.assertIn(f"{sys.executable} scripts/ci.py lint", recipe)
        self.assertIn(f"{sys.executable} -m ruff format --check scripts/ci.py tests/test_ci.py", recipe)
        # The advisory ty check must neither rebuild .venv nor rewrite uv.lock.
        self.assertIn("uv run --frozen --no-sync ty check --exit-zero run.py sdk tests", recipe)
        self.assertNotIn("uv run ruff", recipe)
        self.assertIn("-uv run --frozen --no-sync ty check", (ROOT / "Makefile").read_text(encoding="utf-8"))

    def test_python_311_guard_runs_only_for_test_test_security_and_lint(self):
        for target in CONTRACT_TARGETS:
            if target == "pre-commit":
                continue  # pre-commit runs lint and test, so it inherits their guard.
            with self.subTest(target=target):
                guarded = PYTHON_311_GUARD in self.dry_run(target, f"PYTHON={sys.executable}")
                self.assertEqual(guarded, target in {"test", "test-security", "lint"})


class WorkingDirectoryIndependenceTests(unittest.TestCase):
    def test_pvp_sanitized_inputs_passes_from_a_temporary_working_directory(self):
        env = {key: value for key, value in os.environ.items() if key != "PYTHONSAFEPATH"}
        env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_pvp_sanitized_inputs"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"Ran [1-9]\d* tests?")


# Credential files, logs and captures that must never be committed.
PRIVATE_PATHS = (".device", "config.secrets", "passwords.txt", ".profiles/x", "tachikoma.log", "x.har", "x.pcap")
# Each of these would change which Bazel binary or flags run, so none may be tracked. A stray .bazelrc.user is
# deliberately left visible as untracked dirt: personal Bazel flags belong in ~/.bazelrc.
BAZEL_LAUNCHER_FILES = ("tools/bazel", ".bazeliskrc", ".bazelrc.user")
# Git variables a hook or a calling git command may set; they would point the commands at another repository.
GIT_ENVIRONMENT = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"}


def _is_git_checkout():
    if not shutil.which("git"):
        return False
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
        env={key: value for key, value in os.environ.items() if key not in GIT_ENVIRONMENT},
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and os.path.samefile(result.stdout.strip(), ROOT)


# A missing git fails under TACHIKOMA_STRICT_DEPS=1; with git present, a tree that is not this checkout still skips.
@skip_unless_dependency(
    shutil.which("git"),
    "requires git and a repository checkout",
    applicable=not shutil.which("git") or _is_git_checkout(),
)
class GitIgnoreHygieneTests(unittest.TestCase):
    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(ROOT), *arguments],
            env={key: value for key, value in os.environ.items() if key not in GIT_ENVIRONMENT},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def is_ignored(self, path):
        result = self.git("check-ignore", "-q", path)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode == 0

    def test_bazel_output_links_are_ignored(self):
        for path in ("bazel-bin", "bazel-out", "bazel-testlogs", "bazel-tachikoma"):
            with self.subTest(path=path):
                self.assertTrue(self.is_ignored(path))
        # Only the convenience links at the repository root, not files elsewhere that happen to start with bazel-.
        self.assertFalse(self.is_ignored("docs/bazel-notes.md"))

    def test_private_files_are_ignored(self):
        for path in PRIVATE_PATHS:
            with self.subTest(path=path):
                self.assertTrue(self.is_ignored(path))

    def test_personal_bazelrc_is_not_ignored(self):
        self.assertFalse(self.is_ignored(".bazelrc.user"))

    def test_no_bazel_launcher_or_personal_rc_file_is_tracked(self):
        result = self.git("ls-files", "-z")
        self.assertEqual(result.returncode, 0, result.stderr)
        tracked = set(filter(None, result.stdout.split("\0")))
        for path in BAZEL_LAUNCHER_FILES:
            with self.subTest(path=path):
                self.assertNotIn(path, tracked)
        self.assertEqual([path for path in tracked if path.startswith("tools/bazel.")], [])


if __name__ == "__main__":
    unittest.main()
