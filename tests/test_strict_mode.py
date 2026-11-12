#!/usr/bin/env python3
"""TACHIKOMA_STRICT_DEPS=1 turns missing-dependency skips into failures (tests/test_strict_mode.py).

A skipped test still reports green, so a runner without yaml, git or make would silently run fewer tests. With
TACHIKOMA_STRICT_DEPS=1 those cases fail instead. A test that does not apply to the checkout (no .git, no
Makefile, a non-POSIX host) still skips. Unset, or set to anything but 1, every decorator below is plain
unittest.skipUnless and nothing changes.
"""

from __future__ import annotations

from contextlib import ExitStack
import functools
import importlib.util
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

STRICT_DEPS_ENV = "TACHIKOMA_STRICT_DEPS"


def strict_deps_enabled():
    return os.environ.get(STRICT_DEPS_ENV) == "1"


def strict_dependency_failure(reason):
    return AssertionError(f"{STRICT_DEPS_ENV}=1 turns this skip into a failure: {reason}")


def skip_unless_dependency(available, reason, *, applicable=True):
    """unittest.skipUnless(available and applicable, reason), except that a missing dependency fails when strict.

    available is the dependency (a module, a binary on PATH); applicable says whether the test applies here at
    all. A test that does not apply skips even in strict mode.
    """
    if available or not applicable or not strict_deps_enabled():
        return unittest.skipUnless(available and applicable, reason)

    def fail(test_item):
        if isinstance(test_item, type):

            def setUpClass(cls):
                raise strict_dependency_failure(reason)

            test_item.setUpClass = classmethod(setUpClass)
            return test_item

        @functools.wraps(test_item)
        def failing(*arguments, **keywords):
            raise strict_dependency_failure(reason)

        return failing

    return fail


ROOT = Path(os.path.abspath(__file__)).parents[1]
REAL_WHICH = shutil.which


def without_binary(name):
    """Patch shutil.which so that name is not on PATH; other names are looked up as usual."""
    return patch.object(
        shutil, "which", side_effect=lambda command, *a, **k: None if command == name else REAL_WHICH(command, *a, **k)
    )


def fresh_module(path):
    """Execute a test module as a new, unregistered module, so its import-time decorators see the active patches."""
    spec = importlib.util.spec_from_file_location(f"_strict_probe_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outcome(test_class):
    """Run the class without running any of its tests when it is skipped or fails setUpClass."""
    result = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(test_class).run(result)
    return result


class StrictModeTestCase(unittest.TestCase):
    def mode(self, strict, *patches):
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ))
        if strict:
            os.environ[STRICT_DEPS_ENV] = "1"
        else:
            os.environ.pop(STRICT_DEPS_ENV, None)
        for item in patches:
            stack.enter_context(item)
        return stack

    def assertStrictFailure(self, test_class, reason):
        result = outcome(test_class)
        self.assertEqual((result.testsRun, len(result.skipped), len(result.failures)), (0, 0, 0))
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn(f"{STRICT_DEPS_ENV}=1 turns this skip into a failure: {reason}", result.errors[0][1])

    def assertPlainSkip(self, test_class, reason):
        self.assertIs(test_class.__unittest_skip__, True)
        self.assertEqual(test_class.__unittest_skip_why__, reason)
        self.assertNotIn("setUpClass", vars(test_class))

    def assertNotSkipped(self, test_class):
        self.assertFalse(getattr(test_class, "__unittest_skip__", False))


class SkipUnlessDependencyTests(StrictModeTestCase):
    def test_unset_or_other_values_are_plain_skip_unless(self):
        for value in (None, "", "0", "true", "yes"):
            with self.subTest(value=value), self.mode(False):
                if value is not None:
                    os.environ[STRICT_DEPS_ENV] = value

                @skip_unless_dependency(None, "needs a tool")
                class Probe(unittest.TestCase):
                    def test_x(self):
                        pass

                self.assertPlainSkip(Probe, "needs a tool")

    def test_strict_missing_dependency_fails_classes_and_functions(self):
        with self.mode(True):

            @skip_unless_dependency(None, "needs a tool")
            class Probe(unittest.TestCase):
                def test_x(self):
                    pass

            self.assertStrictFailure(Probe, "needs a tool")

            @skip_unless_dependency(None, "needs a tool")
            def probe():
                pass

            with self.assertRaisesRegex(AssertionError, "needs a tool"):
                probe()

    def test_strict_present_dependency_and_inapplicable_tests_keep_skip_unless(self):
        with self.mode(True):

            @skip_unless_dependency("/usr/bin/tool", "needs a tool")
            class Present(unittest.TestCase):
                pass

            @skip_unless_dependency(None, "needs a checkout", applicable=False)
            class Inapplicable(unittest.TestCase):
                pass

        self.assertNotSkipped(Present)
        self.assertPlainSkip(Inapplicable, "needs a checkout")


class CiModuleStrictTests(StrictModeTestCase):
    """tests/test_ci.py: missing yaml and a missing git binary."""

    YAML_REASON = "install .github/requirements-ci.txt for workflow checks"
    GIT_REASON = "release provenance tests require Git"
    CHECKOUT_REASON = "requires the tachikoma Git checkout"

    def load(self):
        return fresh_module(ROOT / "tests" / "test_ci.py")

    def test_missing_yaml(self):
        with self.mode(False, patch.dict(sys.modules, {"yaml": None})):
            self.assertPlainSkip(self.load().WorkflowPolicyTests, self.YAML_REASON)
        with self.mode(True, patch.dict(sys.modules, {"yaml": None})):
            self.assertStrictFailure(self.load().WorkflowPolicyTests, self.YAML_REASON)

    def test_missing_git_binary(self):
        with self.mode(False, without_binary("git")):
            module = self.load()
        self.assertPlainSkip(module.SourceReleaseTests, self.GIT_REASON)
        self.assertPlainSkip(module.ReleasePackageDataTests, self.GIT_REASON)
        self.assertPlainSkip(module.RepositoryReleaseSelectionTests, self.CHECKOUT_REASON)
        with self.mode(True, without_binary("git")):
            module = self.load()
            self.assertStrictFailure(module.SourceReleaseTests, self.GIT_REASON)
            self.assertStrictFailure(module.ReleasePackageDataTests, self.GIT_REASON)
            self.assertStrictFailure(module.RepositoryReleaseSelectionTests, self.CHECKOUT_REASON)

    def test_repository_release_selection_still_skips_when_only_dot_git_is_missing(self):
        from scripts import ci

        with TemporaryDirectory() as directory:
            for strict in (False, True):
                with self.subTest(strict=strict), self.mode(strict, patch.object(ci, "ROOT", Path(directory))):
                    module = self.load()
                self.assertPlainSkip(module.RepositoryReleaseSelectionTests, self.CHECKOUT_REASON)
                self.assertNotSkipped(module.SourceReleaseTests)

    def test_strict_mode_changes_nothing_when_dependencies_are_present(self):
        with self.mode(
            True, patch.dict(sys.modules, {"yaml": object()}), patch.object(shutil, "which", return_value="/bin/x")
        ):
            module = self.load()
        for name in ("WorkflowPolicyTests", "SourceReleaseTests", "ReleasePackageDataTests"):
            with self.subTest(name=name):
                self.assertNotSkipped(getattr(module, name))
                self.assertNotIn("setUpClass", vars(getattr(module, name)))


class PvpShimStrictTests(StrictModeTestCase):
    """tests/test_pvp_safety.py: a missing PvP shim is left out of the scan, or fails when strict."""

    def test_missing_pvp_shim(self):
        from tests import test_pvp_safety

        with TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "sdk" / "pvp").mkdir(parents=True)
            relocated = (
                patch.object(test_pvp_safety, "REPO", repo),
                patch.object(test_pvp_safety, "PVP_DIR", repo / "sdk" / "pvp"),
            )
            with self.mode(False, *relocated):
                self.assertEqual(test_pvp_safety._module_sources(), {})
            with self.mode(True, *relocated):
                with self.assertRaisesRegex(AssertionError, rf"{STRICT_DEPS_ENV}=1 .*sdk/account_progression\.py"):
                    test_pvp_safety._module_sources()

    def test_every_shim_is_present_in_this_checkout(self):
        from tests import test_pvp_safety

        with self.mode(True):
            sources = test_pvp_safety._module_sources()
        for name in test_pvp_safety.SHIMS:
            self.assertIn(f"sdk/{name}", sources)


class BuildHygieneStrictTests(StrictModeTestCase):
    """tests/test_build_hygiene.py: a missing make or git binary."""

    MAKE_REASON = "requires make, a POSIX shell and the repository Makefile"
    GIT_REASON = "requires git and a repository checkout"

    def load(self, root=ROOT):
        return fresh_module(root / "tests" / "test_build_hygiene.py")

    def test_missing_make(self):
        with self.mode(False, without_binary("make")):
            self.assertPlainSkip(self.load().MakefileContractTests, self.MAKE_REASON)
        with self.mode(True, without_binary("make")):
            self.assertStrictFailure(self.load().MakefileContractTests, self.MAKE_REASON)

    def test_missing_git(self):
        with self.mode(False, without_binary("git")):
            self.assertPlainSkip(self.load().GitIgnoreHygieneTests, self.GIT_REASON)
        with self.mode(True, without_binary("git")):
            self.assertStrictFailure(self.load().GitIgnoreHygieneTests, self.GIT_REASON)

    def test_a_copy_without_makefile_or_checkout_still_skips_when_strict(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            source = (ROOT / "tests" / "test_build_hygiene.py").read_text(encoding="utf-8")
            (root / "tests" / "test_build_hygiene.py").write_text(source, encoding="utf-8")
            with self.mode(True):
                module = self.load(root)
        self.assertPlainSkip(module.MakefileContractTests, self.MAKE_REASON)
        self.assertPlainSkip(module.GitIgnoreHygieneTests, self.GIT_REASON)
