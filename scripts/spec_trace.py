"""Spec traceability check (SPEC-01): links requirements in docs/specs/ to the tests that prove them.

Reads files only: no network, no writes, standard library only.

- ``load_specs(root)`` parses every ``docs/specs/SPEC-*.md`` into its id, status, requirements and
  acceptance criteria.
- ``load_citations(root)`` finds every ``# Verifies: Snn-Rk[, Snn-Rk...]`` comment line in the Python
  files under ``tests/`` and ``automation/tests/``.
- ``check(root)`` returns the list of problems; an empty list means the repository passes.
- ``python scripts/spec_trace.py --report`` prints a coverage table for review.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = Path("docs/specs")
CITATION_DIRS = (Path("tests"), Path("automation/tests"))

STATUSES = ("Draft", "Approved", "Verified offline", "Done", "Withdrawn")
CHECKED_STATUSES = frozenset({"Verified offline", "Done"})
LEVELS = frozenset({"MUST", "SHOULD", "MAY"})
NO_LIVE_EVIDENCE = "no live evidence required"

SPEC_FILE_RE = re.compile(r"SPEC-(\d{2})-[^/]*\.md\Z")
TITLE_RE = re.compile(r"#\s+SPEC-(\d{2})\b")
STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(.*?)\s*\Z")
SECTION_RE = re.compile(r"##\s+(.+?)\s*\Z")
REQUIREMENT_RE = re.compile(r"-\s+\*\*(S\d{2}-R\d+)\s+\(([^)]*)\)\*\*(.*)\Z")
# A bullet that names a requirement id in bold but does not match the required line format.
REQUIREMENT_LIKE_RE = re.compile(r"-\s+\*\*\s*S\d+-R\d+")
WITHDRAWN_RE = re.compile(r"\(Withdrawn:.*\)\s*\Z")
LINK_RE = re.compile(r"\[[^\]]+\]\([^)\s]+\)")
CITATION_RE = re.compile(r"\s*#\s*Verifies:(.*)\Z")
CITED_ID_RE = re.compile(r"S\d{2}-R\d+\Z")
TARGET_RE = re.compile(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)")
# One reference in a Proves cell: R3, S01-R3, AC4, S01-AC4.
REF = r"(?:(S\d{2})-)?(R|AC)(\d+)"
REF_RE = re.compile(REF + r"\Z")
RANGE_RE = re.compile(REF + r"\s*(?:–|—|-|\.\.)\s*" + REF + r"\Z")


class SpecFormatError(ValueError):
    """Raised by ``load_specs`` when a spec file is malformed; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Requirement:
    id: str
    level: str
    withdrawn: bool
    line: int


@dataclass(frozen=True)
class Criterion:
    id: str
    proves: tuple[str, ...]
    kind: str
    line: int

    @property
    def offline(self) -> bool:
        return "offline" in self.kind.lower()


@dataclass
class Spec:
    id: str  # "S01"
    name: str  # "SPEC-01"
    path: str  # repository-relative POSIX path
    status: str
    requirements: dict[str, Requirement] = field(default_factory=dict)
    criteria: list[Criterion] = field(default_factory=list)
    evidence: str | None = None  # text of the "## Evidence" section, None when absent

    def proved_by(self, requirement_id: str) -> list[Criterion]:
        return [criterion for criterion in self.criteria if requirement_id in criterion.proves]

    @property
    def evidence_links(self) -> list[str]:
        return LINK_RE.findall(self.evidence or "")

    @property
    def states_no_live_evidence(self) -> bool:
        return NO_LIVE_EVIDENCE in (self.evidence or "").lower()


@dataclass(frozen=True)
class Citation:
    path: str  # repository-relative POSIX path
    line: int
    ids: tuple[str, ...]
    target: str | None  # the test function or class the comment sits above

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _sections(lines: list[str]) -> dict[str, list[tuple[int, str]]]:
    sections: dict[str, list[tuple[int, str]]] = {}
    current = None
    for number, line in enumerate(lines, 1):
        match = SECTION_RE.match(line)
        if match:
            current = match.group(1).strip().lower()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append((number, line))
    return sections


def _parse_status(lines: list[str], where: str, errors: list[str]) -> str | None:
    for number, line in enumerate(lines, 1):
        match = STATUS_RE.match(line)
        if not match:
            continue
        value = match.group(1)
        for status in STATUSES:
            if value == status or (value.startswith(status) and not value[len(status)].isalnum()):
                return status
        errors.append(f"{where}:{number}: unknown status {value!r}; expected one of {', '.join(STATUSES)}")
        return None
    errors.append(f"{where}: missing status line '**Status:** <{' | '.join(STATUSES)}>'")
    return None


def _parse_requirements(spec: Spec, body: list[tuple[int, str]], errors: list[str]) -> None:
    for number, line in body:
        match = REQUIREMENT_RE.match(line)
        if not match:
            if REQUIREMENT_LIKE_RE.match(line):
                errors.append(
                    f"{spec.path}:{number}: malformed requirement line; expected '- **{spec.id}-Rk (MUST|SHOULD|MAY)** text'"
                )
            continue
        requirement_id, level, text = match.groups()
        level = level.strip()
        if not requirement_id.startswith(spec.id + "-"):
            errors.append(f"{spec.path}:{number}: requirement {requirement_id} does not belong to {spec.name}")
            continue
        if level not in LEVELS:
            errors.append(
                f"{spec.path}:{number}: unknown level {level!r} for {requirement_id}; expected MUST, SHOULD or MAY"
            )
            continue
        if requirement_id in spec.requirements:
            first = spec.requirements[requirement_id].line
            errors.append(f"{spec.path}:{number}: duplicate requirement id {requirement_id} (first on line {first})")
            continue
        spec.requirements[requirement_id] = Requirement(
            requirement_id, level, bool(WITHDRAWN_RE.search(text)), number
        )


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _qualify(spec: Spec, prefix: str | None, kind: str, number: str) -> str | None:
    if prefix is not None and prefix != spec.id:
        return None
    return f"{spec.id}-{kind}{int(number)}"


def _expand_proves(spec: Spec, cell: str, where: str, errors: list[str]) -> list[str] | None:
    """Return the requirement and criterion ids a Proves cell names, or None after recording an error."""
    references: list[str] = []
    tokens = [token.strip().strip("`") for token in cell.split(",")]
    if not any(tokens):
        errors.append(f"{where}: empty Proves cell")
        return None
    for token in tokens:
        single = REF_RE.match(token)
        span = RANGE_RE.match(token)
        if single:
            reference = _qualify(spec, *single.groups())
            if reference is None:
                errors.append(f"{where}: {token!r} names another spec; a criterion proves its own spec only")
                return None
            references.append(reference)
        elif span:
            start_prefix, start_kind, start, end_prefix, end_kind, end = span.groups()
            if start_kind != end_kind or int(start) > int(end):
                errors.append(f"{where}: invalid range {token!r}")
                return None
            if _qualify(spec, start_prefix, start_kind, start) is None or _qualify(spec, end_prefix, end_kind, end) is None:
                errors.append(f"{where}: {token!r} names another spec; a criterion proves its own spec only")
                return None
            references.extend(f"{spec.id}-{start_kind}{k}" for k in range(int(start), int(end) + 1))
        else:
            errors.append(f"{where}: cannot read Proves entry {token!r}; use R1, R1-R3 or AC1 forms")
            return None
    return references


def _parse_criteria(spec: Spec, body: list[tuple[int, str]], errors: list[str]) -> None:
    rows = [(number, _cells(line)) for number, line in body if line.lstrip().startswith("|")]
    if not rows:
        return
    header_line, header = rows[0]
    columns = {name.lower(): index for index, name in enumerate(header)}
    missing = [name for name in ("id", "proves", "kind") if name not in columns]
    if missing:
        errors.append(
            f"{spec.path}:{header_line}: acceptance criteria table lacks column(s) "
            + ", ".join(name.capitalize() for name in missing)
        )
        return
    raw: dict[str, tuple[int, list[str], str]] = {}
    for number, cells in rows[1:]:
        if all(set(cell) <= set("-: ") for cell in cells):
            continue  # the |---|---| separator row
        where = f"{spec.path}:{number}"
        if len(cells) <= max(columns["id"], columns["proves"], columns["kind"]):
            errors.append(f"{where}: acceptance criterion row has too few columns")
            continue
        criterion_id = cells[columns["id"]].strip("`")
        if not re.fullmatch(rf"{spec.id}-AC\d+", criterion_id):
            errors.append(f"{where}: malformed acceptance criterion id {criterion_id!r}; expected {spec.id}-ACk")
            continue
        if criterion_id in raw:
            errors.append(f"{where}: duplicate acceptance criterion id {criterion_id} (first on line {raw[criterion_id][0]})")
            continue
        kind = cells[columns["kind"]]
        if not kind:
            errors.append(f"{where}: {criterion_id} has no Kind")
            continue
        references = _expand_proves(spec, cells[columns["proves"]], where, errors)
        if references is None:
            continue
        raw[criterion_id] = (number, references, kind)

    def resolve(criterion_id: str, seen: tuple[str, ...]) -> list[str] | None:
        number, references, _ = raw[criterion_id]
        resolved: list[str] = []
        for reference in references:
            if "-AC" not in reference:
                if reference not in spec.requirements:
                    errors.append(f"{spec.path}:{number}: {criterion_id} proves unknown requirement {reference}")
                    return None
                resolved.append(reference)
            elif reference in seen:
                errors.append(f"{spec.path}:{number}: {criterion_id} refers to itself through {reference}")
                return None
            elif reference not in raw:
                errors.append(f"{spec.path}:{number}: {criterion_id} refers to unknown criterion {reference}")
                return None
            else:
                inherited = resolve(reference, seen + (reference,))
                if inherited is None:
                    return None
                resolved.extend(inherited)
        return list(dict.fromkeys(resolved))

    for criterion_id, (number, _, kind) in raw.items():
        proves = resolve(criterion_id, (criterion_id,))
        if proves is not None:
            spec.criteria.append(Criterion(criterion_id, tuple(proves), kind, number))


def _parse_spec(path: Path, root: Path) -> tuple[Spec | None, list[str]]:
    where = _relative(path, root)
    errors: list[str] = []
    match = SPEC_FILE_RE.search(path.name)
    if not match:
        return None, [f"{where}: file name must look like SPEC-nn-title.md"]
    number = match.group(1)
    lines = path.read_text(encoding="utf-8").splitlines()
    title = next((line for line in lines if line.startswith("# ")), None)
    title_match = TITLE_RE.match(title or "")
    if not title_match or title_match.group(1) != number:
        errors.append(f"{where}: first heading must be '# SPEC-{number}: Title'")
    status = _parse_status(lines, where, errors)
    spec = Spec(id=f"S{number}", name=f"SPEC-{number}", path=where, status=status or "")
    sections = _sections(lines)
    if "requirements" not in sections:
        errors.append(f"{where}: missing '## Requirements' section")
    else:
        _parse_requirements(spec, sections["requirements"], errors)
    _parse_criteria(spec, sections.get("acceptance criteria", []), errors)
    if "evidence" in sections:
        spec.evidence = "\n".join(line for _, line in sections["evidence"]).strip()
    return spec, errors


def _load(root: Path) -> tuple[list[Spec], list[str]]:
    specs: list[Spec] = []
    errors: list[str] = []
    directory = Path(root) / SPEC_DIR
    paths = sorted(directory.glob("SPEC-*.md"))
    if not paths:
        return [], [f"{SPEC_DIR.as_posix()}: no SPEC-*.md files found"]
    seen: dict[str, str] = {}
    for path in paths:
        spec, spec_errors = _parse_spec(path, Path(root))
        errors.extend(spec_errors)
        if spec is None:
            continue
        if spec.id in seen:
            errors.append(f"{spec.path}: duplicate spec id {spec.name} (also {seen[spec.id]})")
            continue
        seen[spec.id] = spec.path
        specs.append(spec)
    return specs, errors


def load_specs(root: Path | str = ROOT) -> list[Spec]:
    """Parse every docs/specs/SPEC-*.md under ``root``; raise SpecFormatError naming every malformation."""
    specs, errors = _load(Path(root))
    if errors:
        raise SpecFormatError(errors)
    return specs


def _target(lines: list[str], index: int) -> str | None:
    for line in lines[index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "@")):
            continue
        match = TARGET_RE.match(line)
        return match.group(1) if match else None
    return None


def _scan_citations(root: Path) -> tuple[list[Citation], list[str]]:
    citations: list[Citation] = []
    errors: list[str] = []
    for directory in CITATION_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            where = _relative(path, root)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines):
                match = CITATION_RE.match(line)
                if not match:
                    continue
                tokens = [token.strip() for token in match.group(1).split(",")]
                bad = [token for token in tokens if not CITED_ID_RE.match(token)]
                if bad or not tokens:
                    errors.append(
                        f"{where}:{index + 1}: malformed '# Verifies:' comment; expected '# Verifies: Snn-Rk[, Snn-Rk]'"
                    )
                    continue
                citations.append(Citation(where, index + 1, tuple(tokens), _target(lines, index)))
    return citations, errors


def load_citations(root: Path | str = ROOT) -> list[Citation]:
    """Return every well-formed '# Verifies:' comment under tests/ and automation/tests/."""
    return _scan_citations(Path(root))[0]


def _live_only(spec: Spec, requirement: Requirement) -> bool:
    proofs = spec.proved_by(requirement.id)
    return bool(proofs) and not any(criterion.offline for criterion in proofs)


def _evaluate(root: Path) -> tuple[list[Spec], list[Citation], list[str]]:
    specs, errors = _load(root)
    citations, citation_errors = _scan_citations(root)
    errors.extend(citation_errors)
    requirements = {
        requirement.id: (spec, requirement) for spec in specs for requirement in spec.requirements.values()
    }
    cited: dict[str, list[Citation]] = {}
    for citation in citations:
        for requirement_id in citation.ids:
            known = requirements.get(requirement_id)
            if known is None:
                errors.append(f"{citation.location}: cites {requirement_id}, which no spec defines")
            elif known[1].withdrawn or known[0].status == "Withdrawn":
                errors.append(f"{citation.location}: cites {requirement_id}, which is withdrawn")
            else:
                cited.setdefault(requirement_id, []).append(citation)

    for spec in specs:
        if spec.status in ("", "Draft", "Withdrawn"):
            continue
        musts = [r for r in spec.requirements.values() if r.level == "MUST" and not r.withdrawn]
        unproved = [r.id for r in musts if not spec.proved_by(r.id)]
        if unproved:
            errors.append(
                f"{spec.path}: MUST requirement(s) not proved by any acceptance criterion: {', '.join(unproved)}"
            )
        if spec.status not in CHECKED_STATUSES:
            continue
        uncovered = [
            r.id for r in musts if any(c.offline for c in spec.proved_by(r.id)) and r.id not in cited
        ]
        if uncovered:
            errors.append(
                f"{spec.path}: {spec.name} is '{spec.status}' but these MUST requirements with offline "
                f"acceptance criteria have no citing test: {', '.join(uncovered)}"
            )
        if spec.status == "Done":
            live_only = [r.id for r in musts if _live_only(spec, r)]
            if spec.evidence is None:
                errors.append(f"{spec.path}: {spec.name} is 'Done' but has no '## Evidence' section")
            elif live_only and not spec.evidence_links:
                errors.append(
                    f"{spec.path}: {spec.name} is 'Done' but lists no evidence link for live-only MUST "
                    f"requirement(s): {', '.join(live_only)}"
                )
            elif not spec.evidence_links and not spec.states_no_live_evidence:
                errors.append(
                    f"{spec.path}: {spec.name} is 'Done' but its Evidence section has no link "
                    f"and does not state '{NO_LIVE_EVIDENCE}'"
                )
    return specs, citations, errors


def check(root: Path | str = ROOT) -> list[str]:
    """Return every traceability problem under ``root``; an empty list means the check passes."""
    return _evaluate(Path(root))[2]


def coverage_rows(specs: list[Spec], citations: list[Citation]) -> list[dict[str, str]]:
    """One row per requirement: spec, status, requirement, level, proving criteria, citing tests, state."""
    cited: dict[str, list[Citation]] = {}
    for citation in citations:
        for requirement_id in citation.ids:
            cited.setdefault(requirement_id, []).append(citation)
    rows = []
    for spec in specs:
        for requirement in spec.requirements.values():
            proofs = spec.proved_by(requirement.id)
            tests = cited.get(requirement.id, [])
            if requirement.withdrawn:
                state = "withdrawn"
            elif tests:
                state = "covered"
            elif not proofs:
                state = "no criterion"
            elif _live_only(spec, requirement):
                state = "live only"
            elif requirement.level == "MUST":
                state = "uncovered"
            else:
                state = "optional"
            rows.append(
                {
                    "spec": spec.name,
                    "status": spec.status,
                    "requirement": requirement.id,
                    "level": requirement.level,
                    "criteria": ", ".join(f"{c.id.split('-', 1)[1]} ({c.kind})" for c in proofs) or "-",
                    "tests": ", ".join(
                        c.location + (f" {c.target}" if c.target else "") for c in tests
                    )
                    or "-",
                    "state": state,
                }
            )
    return rows


def format_report(root: Path | str = ROOT) -> tuple[str, list[str]]:
    """Return the Markdown coverage table and the check errors."""
    specs, citations, errors = _evaluate(Path(root))
    rows = coverage_rows(specs, citations)
    header = ("Spec", "Status", "Requirement", "Level", "Proved by", "Citing tests", "State")
    keys = ("spec", "status", "requirement", "level", "criteria", "tests", "state")
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(row[key] for key in keys) + " |" for row in rows]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    summary = ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))
    lines += ["", f"{len(specs)} specs, {len(rows)} requirements, {len(citations)} citations: {summary}."]
    return "\n".join(lines), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true", help="print the coverage table (spec, requirement, tests)")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)
    if args.report:
        table, errors = format_report(args.root)
        print(table)
    else:
        errors = check(args.root)
    if errors:
        print(f"\nSpec traceability check failed with {len(errors)} problem(s):", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Spec traceability check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
