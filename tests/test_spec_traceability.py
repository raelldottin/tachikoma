"""SPEC-01 traceability check: synthetic spec trees in temporary directories, plus the real repository."""

import ast
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import os
import socket
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest import mock

from scripts import spec_trace


REPO_ROOT = Path(os.path.abspath(__file__)).parent.parent
# Built by concatenation so that no line of this file is itself read as a citation.
MARK = "# " + "Verifies:"

ACCEPTANCE = """
| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S90-AC1 | R1, R2 | offline | Given a thing, when it runs, then it works |
| S90-AC2 | R3 | live | The owner runs it |
"""


def spec_text(
    number="90",
    status="Approved",
    requirements=(
        "- **S90-R1 (MUST)** One.",
        "- **S90-R2 (SHOULD)** Two.",
        "- **S90-R3 (MUST)** Three, live only.",
    ),
    acceptance=ACCEPTANCE,
    evidence="Pending.",
    status_line=None,
):
    lines = [f"# SPEC-{number}: Synthetic spec", ""]
    if status_line is not None:
        lines.append(status_line)
    elif status is not None:
        lines.append(f"**Status:** {status}")
    lines += ["**Release:** process", "", "## Requirements", ""]
    lines += list(requirements)
    lines += ["", "## Acceptance criteria", "", textwrap.dedent(acceptance).strip(), ""]
    if evidence is not None:
        lines += ["## Evidence", "", evidence, ""]
    lines += ["## Open questions and risks", "", "None known.", ""]
    return "\n".join(lines)


def citation_file(*entries):
    """A test module whose methods carry the given citation lists."""
    body = ["import unittest", "", "", "class T(unittest.TestCase):"]
    for index, ids in enumerate(entries):
        body += [f"    {MARK} {ids}", f"    def test_{index}(self):", "        pass", ""]
    return "\n".join(body) + "\n"


class Tree:
    """A temporary repository root holding docs/specs/ and tests/."""

    def __init__(self, test):
        self._directory = TemporaryDirectory()
        test.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        (self.root / "docs/specs").mkdir(parents=True)
        (self.root / "tests").mkdir()

    def spec(self, name="SPEC-90-synthetic.md", **kwargs):
        (self.root / "docs/specs" / name).write_text(spec_text(**kwargs), encoding="utf-8")
        return self

    def raw_spec(self, name, text):
        (self.root / "docs/specs" / name).write_text(text, encoding="utf-8")
        return self

    def tests(self, name, text, directory="tests"):
        path = self.root / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self

    def check(self):
        return spec_trace.check(self.root)


class LoadSpecsTests(unittest.TestCase):
    # Verifies: S01-R1
    def test_reads_id_status_and_requirements(self):
        tree = Tree(self).spec(
            requirements=(
                "- **S90-R1 (MUST)** One.",
                "- **S90-R2 (SHOULD)** Two.",
                "- **S90-R3 (MUST)** Three. (Withdrawn: replaced by R4)",
                "- **S90-R4 (MAY)** Four.",
            ),
            acceptance="""
            | Id | Proves | Kind | Criterion |
            | --- | --- | --- | --- |
            | S90-AC1 | R1, R2 | offline | ... |
            """,
        )
        tree.spec(name="SPEC-91-other.md", number="91", status="Verified offline", requirements=(
            "- **S91-R1 (MUST)** One.",
        ), acceptance="| Id | Proves | Kind | Criterion |\n| --- | --- | --- | --- |\n| S91-AC1 | R1 | live | x |")
        specs = spec_trace.load_specs(tree.root)
        self.assertEqual([spec.id for spec in specs], ["S90", "S91"])
        first = specs[0]
        self.assertEqual((first.name, first.status, first.path), ("SPEC-90", "Approved", "docs/specs/SPEC-90-synthetic.md"))
        self.assertEqual(
            [(r.id, r.level, r.withdrawn) for r in first.requirements.values()],
            [("S90-R1", "MUST", False), ("S90-R2", "SHOULD", False), ("S90-R3", "MUST", True), ("S90-R4", "MAY", False)],
        )
        self.assertEqual(specs[1].status, "Verified offline")

    # Verifies: S01-R1, S01-R6
    def test_each_malformation_yields_a_named_error(self):
        cases = {
            "missing status line": (dict(status=None), "missing status line"),
            "unknown status": (dict(status="Shipped"), "unknown status 'Shipped'"),
            "duplicate requirement id": (
                dict(requirements=("- **S90-R1 (MUST)** One.", "- **S90-R1 (SHOULD)** Again.")),
                "duplicate requirement id S90-R1",
            ),
            "unknown level": (
                dict(requirements=("- **S90-R1 (MUST)** One.", "- **S90-R2 (REQUIRED)** Two.")),
                "unknown level 'REQUIRED' for S90-R2",
            ),
            "malformed requirement line": (
                dict(requirements=("- **S90-R1 (MUST)** One.", "- **S90-R2** no level")),
                "malformed requirement line",
            ),
            "requirement of another spec": (
                dict(requirements=("- **S90-R1 (MUST)** One.", "- **S12-R2 (MUST)** Two.")),
                "S12-R2 does not belong to SPEC-90",
            ),
            "missing table column": (
                dict(acceptance="| Id | Requirements | Criterion |\n| --- | --- | --- |\n| S90-AC1 | R1 | x |"),
                "lacks column(s) Proves, Kind",
            ),
            "duplicate criterion id": (
                dict(acceptance=ACCEPTANCE + "| S90-AC1 | R1 | offline | again |\n"),
                "duplicate acceptance criterion id S90-AC1",
            ),
            "unreadable proves": (
                dict(acceptance="| Id | Proves | Kind | C |\n|---|---|---|---|\n| S90-AC1 | everything | offline | x |"),
                "cannot read Proves entry 'everything'",
            ),
            "missing kind": (
                dict(acceptance="| Id | Proves | Kind | C |\n|---|---|---|---|\n| S90-AC1 | R1 |  | x |"),
                "S90-AC1 has no Kind",
            ),
        }
        for label, (kwargs, expected) in cases.items():
            with self.subTest(label):
                tree = Tree(self).spec(**kwargs)
                errors = tree.check()
                self.assertTrue(any(expected in error for error in errors), errors)
                self.assertTrue(all(error.startswith("docs/specs/SPEC-90-synthetic.md") for error in errors), errors)
                with self.assertRaises(spec_trace.SpecFormatError) as raised:
                    spec_trace.load_specs(tree.root)
                self.assertTrue(any(expected in error for error in raised.exception.errors))

    # Verifies: S01-R6
    def test_missing_requirements_section_and_bad_heading_are_errors(self):
        tree = Tree(self).raw_spec("SPEC-90-bare.md", "# Untitled\n\n**Status:** Approved\n")
        errors = tree.check()
        self.assertTrue(any("missing '## Requirements' section" in error for error in errors), errors)
        self.assertTrue(any("first heading must be '# SPEC-90: Title'" in error for error in errors), errors)

    # Verifies: S01-R6
    def test_an_empty_spec_directory_is_an_error_not_a_pass(self):
        self.assertEqual(Tree(self).check(), ["docs/specs: no SPEC-*.md files found"])


class CitationTests(unittest.TestCase):
    # Verifies: S01-R2
    def test_finds_citations_under_tests_and_automation_tests(self):
        tree = Tree(self).spec()
        tree.tests("test_one.py", citation_file("S90-R1", "S90-R1, S90-R2"))
        tree.tests("test_two.py", citation_file("S90-R2"), directory="automation/tests")
        tree.tests("notes.txt", f"{MARK} S90-R9\n")  # not Python, not read
        tree.tests("test_elsewhere.py", citation_file("S90-R9"), directory="scripts")  # outside the scanned dirs
        citations = spec_trace.load_citations(tree.root)
        self.assertEqual(
            [(c.path, c.line, c.ids, c.target) for c in citations],
            [
                ("tests/test_one.py", 5, ("S90-R1",), "test_0"),
                ("tests/test_one.py", 9, ("S90-R1", "S90-R2"), "test_1"),
                ("automation/tests/test_two.py", 5, ("S90-R2",), "test_0"),
            ],
        )
        self.assertEqual(tree.check(), [])

    # Verifies: S01-R2, S01-R4
    def test_missing_withdrawn_and_malformed_citations_name_file_and_line(self):
        tree = Tree(self).spec(
            requirements=(
                "- **S90-R1 (MUST)** One.",
                "- **S90-R2 (SHOULD)** Two. (Withdrawn: no longer needed)",
                "- **S90-R3 (MUST)** Three.",
            )
        )
        tree.tests("test_cites.py", citation_file("S90-R1", "S90-R7", "S90-R2", "S90-R1 and more", "S77-R1"))
        self.assertEqual(
            tree.check(),
            [
                "tests/test_cites.py:17: malformed '# Verifies:' comment; expected '# Verifies: Snn-Rk[, Snn-Rk]'",
                "tests/test_cites.py:9: cites S90-R7, which no spec defines",
                "tests/test_cites.py:13: cites S90-R2, which is withdrawn",
                "tests/test_cites.py:21: cites S77-R1, which no spec defines",
            ],
        )

    # Verifies: S01-R4
    def test_citing_a_withdrawn_spec_fails(self):
        tree = Tree(self).spec(status="Withdrawn")
        tree.tests("test_cites.py", citation_file("S90-R1"))
        self.assertEqual(tree.check(), ["tests/test_cites.py:5: cites S90-R1, which is withdrawn"])


class CoverageTests(unittest.TestCase):
    # Verifies: S01-R3
    def test_verified_offline_spec_names_uncited_offline_musts(self):
        for status in ("Verified offline", "Done"):
            with self.subTest(status):
                tree = Tree(self).spec(status=status, evidence="See [the record](../experiments/x.md).")
                errors = tree.check()
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f"is '{status}'", errors[0])
                self.assertTrue(errors[0].endswith("have no citing test: S90-R1"), errors)
                tree.tests("test_cover.py", citation_file("S90-R1"))
                self.assertEqual(tree.check(), [])

    # Verifies: S01-R3
    def test_should_and_withdrawn_musts_need_no_test(self):
        tree = Tree(self).spec(
            status="Verified offline",
            requirements=(
                "- **S90-R1 (MUST)** One. (Withdrawn: out of scope)",
                "- **S90-R2 (SHOULD)** Two.",
                "- **S90-R3 (MAY)** Three.",
            ),
            acceptance="| Id | Proves | Kind | C |\n|---|---|---|---|\n| S90-AC1 | R1–R3 | offline | x |",
        )
        self.assertEqual(tree.check(), [])

    # Verifies: S01-R3
    def test_draft_specs_are_ignored(self):
        tree = Tree(self).spec(status="Draft", acceptance="")
        self.assertEqual(tree.check(), [])

    # Verifies: S01-R3, S01-R9
    def test_approved_spec_with_uncited_musts_passes_and_is_reported(self):
        tree = Tree(self).spec(status="Approved")
        self.assertEqual(tree.check(), [])
        report, errors = spec_trace.format_report(tree.root)
        self.assertEqual(errors, [])
        self.assertIn("| SPEC-90 | Approved | S90-R1 | MUST | AC1 (offline) | - | uncovered |", report)
        self.assertIn("| SPEC-90 | Approved | S90-R3 | MUST | AC2 (live) | - | live only |", report)

    # Verifies: S01-R9
    def test_a_must_with_no_acceptance_criterion_fails_unless_draft(self):
        acceptance = "| Id | Proves | Kind | C |\n|---|---|---|---|\n| S90-AC1 | R2 | offline | x |"
        for status in ("Approved", "Verified offline", "Done"):
            with self.subTest(status):
                tree = Tree(self).spec(status=status, acceptance=acceptance, evidence="No live evidence required.")
                errors = tree.check()
                self.assertIn(
                    "docs/specs/SPEC-90-synthetic.md: MUST requirement(s) not proved by any acceptance criterion: "
                    "S90-R1, S90-R3",
                    errors,
                )
        self.assertEqual(Tree(self).spec(status="Draft", acceptance=acceptance).check(), [])

    # Verifies: S01-R3, S01-R9
    def test_a_live_only_must_in_a_verified_offline_spec_needs_no_test(self):
        tree = Tree(self).spec(status="Verified offline")
        tree.tests("test_cover.py", citation_file("S90-R1"))
        self.assertEqual(tree.check(), [])

    # Verifies: S01-R9
    def test_proves_ranges_references_and_kinds_are_read(self):
        requirements = tuple(f"- **S90-R{k} (MUST)** Requirement {k}." for k in range(1, 10))
        acceptance = """
        | Id | Proves | Kind | Criterion |
        | --- | --- | --- | --- |
        | S90-AC1 | R1–R3 | offline | en dash |
        | S90-AC2 | `S90-R4-R5` | offline, after owner edit | hyphen, qualified |
        | S90-AC3 | R6..R6, R7 | owner review | dotted |
        | S90-AC4 | AC3, R8 | live | refers to AC3 |
        | S90-AC5 | S90-AC4, R9 | owner | refers to AC4 |
        """
        tree = Tree(self).spec(status="Verified offline", requirements=requirements, acceptance=acceptance)
        (spec,) = spec_trace.load_specs(tree.root)
        proves = {c.id: c.proves for c in spec.criteria}
        self.assertEqual(proves["S90-AC1"], ("S90-R1", "S90-R2", "S90-R3"))
        self.assertEqual(proves["S90-AC2"], ("S90-R4", "S90-R5"))
        self.assertEqual(proves["S90-AC3"], ("S90-R6", "S90-R7"))
        self.assertEqual(proves["S90-AC4"], ("S90-R6", "S90-R7", "S90-R8"))
        self.assertEqual(proves["S90-AC5"], ("S90-R6", "S90-R7", "S90-R8", "S90-R9"))
        self.assertEqual(
            {c.id: c.offline for c in spec.criteria},
            {"S90-AC1": True, "S90-AC2": True, "S90-AC3": False, "S90-AC4": False, "S90-AC5": False},
        )
        errors = tree.check()
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].endswith("no citing test: S90-R1, S90-R2, S90-R3, S90-R4, S90-R5"), errors)

    # Verifies: S01-R9
    def test_bad_proves_references_fail(self):
        cases = {
            "R4": "S90-AC1 proves unknown requirement S90-R4",
            "R3–R1": "invalid range 'R3–R1'",
            "R1–AC2": "invalid range 'R1–AC2'",
            "S12-R1": "'S12-R1' names another spec",
            "AC9": "S90-AC1 refers to unknown criterion S90-AC9",
            "AC1": "S90-AC1 refers to itself through S90-AC1",
            " ": "empty Proves cell",
        }
        for cell, expected in cases.items():
            with self.subTest(cell):
                acceptance = f"| Id | Proves | Kind | C |\n|---|---|---|---|\n| S90-AC1 | {cell} | offline | x |"
                errors = Tree(self).spec(acceptance=acceptance).check()
                self.assertTrue(any(expected in error for error in errors), errors)


class EvidenceTests(unittest.TestCase):
    OFFLINE_ONLY = "| Id | Proves | Kind | C |\n|---|---|---|---|\n| S90-AC1 | R1–R3 | offline | x |"

    def done_tree(self, evidence, acceptance=ACCEPTANCE):
        tree = Tree(self).spec(status="Done", evidence=evidence, acceptance=acceptance)
        return tree.tests("test_cover.py", citation_file("S90-R1, S90-R3"))

    # Verifies: S01-R5
    def test_done_spec_without_evidence_fails(self):
        cases = {
            "no section": (None, "has no '## Evidence' section"),
            "pending": ("Pending.", "lists no evidence link for live-only MUST requirement(s): S90-R3"),
        }
        for label, (evidence, expected) in cases.items():
            with self.subTest(label):
                errors = self.done_tree(evidence).check()
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(expected, errors[0])
        errors = self.done_tree("Pending.", acceptance=self.OFFLINE_ONLY).check()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("does not state 'no live evidence required'", errors[0])

    # Verifies: S01-R5
    def test_live_only_musts_need_a_link_not_just_the_statement(self):
        errors = self.done_tree("No live evidence required.").check()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("live-only MUST requirement(s): S90-R3", errors[0])
        self.assertEqual(self.done_tree("2026-12-01: [canary record](../experiments/canary.md)").check(), [])

    # Verifies: S01-R5
    def test_offline_only_done_spec_may_state_no_live_evidence(self):
        self.assertEqual(self.done_tree("No live evidence required.", acceptance=self.OFFLINE_ONLY).check(), [])


class ReportCommandTests(unittest.TestCase):
    # Verifies: S01-R7
    def test_report_prints_spec_requirement_and_citing_tests(self):
        tree = Tree(self).spec()
        tree.tests("test_cover.py", citation_file("S90-R1"))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = spec_trace.main(["--report", "--root", str(tree.root)])
        self.assertEqual(code, 0, err.getvalue())
        report = out.getvalue()
        self.assertIn("| Spec | Status | Requirement | Level | Proved by | Citing tests | State |", report)
        self.assertIn(
            "| SPEC-90 | Approved | S90-R1 | MUST | AC1 (offline) | tests/test_cover.py:5 test_0 | covered |", report
        )
        self.assertIn("| SPEC-90 | Approved | S90-R2 | SHOULD | AC1 (offline) | - | optional |", report)
        self.assertIn("1 specs, 3 requirements, 1 citations", report)

    # Verifies: S01-R7
    def test_command_exits_non_zero_and_lists_problems(self):
        tree = Tree(self).spec(status=None)
        for argv in ([], ["--report"]):
            with self.subTest(argv):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = spec_trace.main(argv + ["--root", str(tree.root)])
                self.assertEqual(code, 1)
                self.assertIn("missing status line", err.getvalue())
                self.assertNotIn("passed", out.getvalue())


class RepositoryTests(unittest.TestCase):
    # Verifies: S01-R8
    def test_real_repository_passes_with_sockets_blocked(self):
        blocked = AssertionError("spec_trace must not use the network")
        with mock.patch.object(socket, "socket", side_effect=blocked), mock.patch.object(
            socket, "create_connection", side_effect=blocked
        ):
            self.assertEqual(spec_trace.check(REPO_ROOT), [])
            report, errors = spec_trace.format_report(REPO_ROOT)
        self.assertEqual(errors, [])
        self.assertIn("| SPEC-01 |", report)

    # Verifies: S01-R1, S01-R9
    def test_real_specs_parse(self):
        specs = {spec.id: spec for spec in spec_trace.load_specs(REPO_ROOT)}
        self.assertEqual(sorted(specs), [f"S{n:02d}" for n in range(1, 13)])
        own = specs["S01"]
        self.assertEqual(list(own.requirements), [f"S01-R{k}" for k in range(1, 10)])
        self.assertEqual(own.requirements["S01-R7"].level, "SHOULD")
        live = {c.id: c for c in specs["S05"].criteria}["S05-AC6"]
        self.assertEqual(live.proves, tuple(f"S05-R{k}" for k in range(1, 10)))
        self.assertFalse(live.offline)
        self.assertTrue({c.id: c for c in specs["S06"].criteria}["S06-AC3"].offline)

    # Verifies: S01-R2
    def test_this_module_cites_every_spec01_requirement(self):
        cited = {
            requirement_id
            for citation in spec_trace.load_citations(REPO_ROOT)
            if citation.path == "tests/test_spec_traceability.py"
            for requirement_id in citation.ids
        }
        self.assertEqual(cited, {f"S01-R{k}" for k in range(1, 10)})

    # Verifies: S01-R8
    def test_check_reads_files_only(self):
        tree = Tree(self).spec(status="Verified offline")
        tree.tests("test_cover.py", citation_file("S90-R1"))

        def snapshot():
            return {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in tree.root.rglob("*") if path.is_file()}

        before = snapshot()
        spec_trace.check(tree.root)
        spec_trace.format_report(tree.root)
        self.assertEqual(snapshot(), before)

    # Verifies: S01-R8
    def test_imports_only_the_standard_library(self):
        tree = ast.parse((REPO_ROOT / "scripts/spec_trace.py").read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertTrue(modules)
        self.assertLessEqual(modules, set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
