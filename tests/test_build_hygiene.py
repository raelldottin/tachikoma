"""Checkout-level checks of the local build entry points (the Makefile).

These tests read the repository checkout itself, so they are never a Bazel target. Every command runs from a
fresh temporary working directory and only as a dry run (``make -n``), so nothing here touches ./.device.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

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

REQUIRES_POSIX_MAKE = unittest.skipUnless(
    os.name == "posix" and shutil.which("make") and (ROOT / "Makefile").is_file(),
    "requires make, a POSIX shell and the repository Makefile",
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


if __name__ == "__main__":
    unittest.main()
