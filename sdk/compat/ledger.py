"""Versioned game-rule and endpoint evidence ledger (roadmap AUTO-015).

Every game rule, endpoint and formula the repository relies on is a
``LedgerEntry``. Each entry carries one or more ``Evidence`` records, and each
record states its evidence class, the game version it was observed on (``None``
when the source records none), the date (``None`` when unknown) and the source
it came from (a repository path or a public URL the repository already cites).

``check_compatibility`` compares the ledger with a target game version and
reports entries never observed on that version or newer, entries whose only
evidence on it is weak, entries resting only on weak evidence, and entries with
no recorded version. Only strong evidence makes an entry current.

Pure and offline: nothing here imports the client, reads game state or opens a
network connection.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Iterable

KINDS = ("endpoint", "rule", "formula")

EVIDENCE_CLASSES = (
    "official-release",
    "official-announcement",
    "community",
    "static-analysis",
    "dynamically-demonstrated",
    "unverified",
)

# Evidence that, on its own, supports treating an entry as current for the
# version it was observed on. Everything else is weak.
STRONG_EVIDENCE = frozenset({"official-release", "dynamically-demonstrated"})

_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")


def parse_version(text: str) -> tuple[int, ...]:
    """Parse ``"0.999.59"``, ``"V0.999.55"`` or ``"1.0"`` into an int tuple.

    Raises ``ValueError`` for anything else.
    """
    if not isinstance(text, str):
        raise ValueError(f"game version must be a string, not {type(text).__name__}")
    match = _VERSION_RE.match(text.strip())
    if not match:
        raise ValueError(f"unrecognised game version {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0 or 1 as ``left`` is older than, equal to or newer than ``right``.

    Missing trailing components count as zero, so ``"1.0"`` equals ``"1.0.0"``.
    """
    a, b = parse_version(left), parse_version(right)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return (a > b) - (a < b)


@dataclass(frozen=True)
class Evidence:
    """One recorded observation backing a ledger entry."""

    evidence_class: str
    source: str
    game_version: str | None = None
    observed: str | None = None  # ISO date, YYYY-MM-DD
    note: str = ""

    def __post_init__(self) -> None:
        if self.evidence_class not in EVIDENCE_CLASSES:
            raise ValueError(f"unknown evidence class {self.evidence_class!r}")
        if not self.source or not self.source.strip():
            raise ValueError("evidence needs a source reference")
        if self.game_version is not None:
            parse_version(self.game_version)
        if self.observed is not None:
            _dt.date.fromisoformat(self.observed)

    @property
    def is_strong(self) -> bool:
        return self.evidence_class in STRONG_EVIDENCE

    def to_dict(self) -> dict:
        return {
            "evidence_class": self.evidence_class,
            "source": self.source,
            "game_version": self.game_version,
            "observed": self.observed,
            "note": self.note,
        }


@dataclass(frozen=True)
class LedgerEntry:
    """A game rule, endpoint or formula and the evidence for it.

    ``code`` lists the repository symbols that rely on the entry, as
    ``"module:attribute"`` (for example ``"sdk.client:Client.createBattle9"``).
    """

    entry_id: str
    kind: str
    subject: str
    evidence: tuple[Evidence, ...]
    code: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.entry_id or self.entry_id != self.entry_id.strip():
            raise ValueError(f"invalid entry id {self.entry_id!r}")
        if self.kind not in KINDS:
            raise ValueError(f"{self.entry_id}: unknown kind {self.kind!r}")
        if not self.subject:
            raise ValueError(f"{self.entry_id}: subject is required")
        if not self.evidence:
            raise ValueError(f"{self.entry_id}: at least one evidence record is required")
        for ref in self.code:
            if ":" not in ref:
                raise ValueError(f"{self.entry_id}: code reference {ref!r} must be module:attribute")

    @property
    def evidence_classes(self) -> tuple[str, ...]:
        return tuple(sorted({item.evidence_class for item in self.evidence}))

    @property
    def known_versions(self) -> tuple[str, ...]:
        return tuple(item.game_version for item in self.evidence if item.game_version is not None)

    @property
    def latest_version(self) -> str | None:
        """Newest game version any evidence record names, or ``None``."""
        return _newest(self.known_versions)

    @property
    def latest_strong_version(self) -> str | None:
        """Newest game version named by strong evidence, or ``None``."""
        return _newest(
            item.game_version for item in self.evidence if item.is_strong and item.game_version is not None
        )

    @property
    def weak_only(self) -> bool:
        return not any(item.is_strong for item in self.evidence)

    def client_methods(self) -> tuple[str, ...]:
        """Names of ``sdk.client.Client`` methods listed in ``code``."""
        prefix = "sdk.client:Client."
        return tuple(ref[len(prefix):] for ref in self.code if ref.startswith(prefix))

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "subject": self.subject,
            "code": list(self.code),
            "latest_version": self.latest_version,
            "latest_strong_version": self.latest_strong_version,
            "weak_only": self.weak_only,
            "evidence": [item.to_dict() for item in self.evidence],
        }


def _newest(versions: Iterable[str]) -> str | None:
    latest = None
    for version in versions:
        if latest is None or compare_versions(version, latest) > 0:
            latest = version
    return latest


def validate_ledger(entries: Iterable[LedgerEntry]) -> tuple[LedgerEntry, ...]:
    """Return the entries as a tuple, refusing duplicate ids."""
    result = tuple(entries)
    seen: set[str] = set()
    for entry in result:
        if entry.entry_id in seen:
            raise ValueError(f"duplicate ledger entry id {entry.entry_id!r}")
        seen.add(entry.entry_id)
    return result


def entries_for_client_method(name: str, entries: Iterable[LedgerEntry]) -> tuple[LedgerEntry, ...]:
    """Entries whose ``code`` names ``sdk.client.Client.<name>``."""
    return tuple(entry for entry in entries if name in entry.client_methods())


@dataclass(frozen=True)
class CompatibilityReport:
    """Result of ``check_compatibility`` for one target game version.

    - ``current``: strong evidence was observed on the target version or newer.
    - ``weak_at_target``: evidence was observed on the target or newer, but only weak evidence.
    - ``stale``: entries with a recorded version, all older than the target.
    - ``unknown_version``: entries none of whose evidence records a game version.
    - ``weak_only``: entries with no official-release or dynamically-demonstrated evidence.

    Every entry is in exactly one of the first four lists and may also be in
    ``weak_only``. Everything except ``current`` is flagged.
    """

    target_version: str
    declared_client_version: str | None
    stale: tuple[str, ...]
    weak_only: tuple[str, ...]
    unknown_version: tuple[str, ...]
    current: tuple[str, ...]
    total: int
    weak_at_target: tuple[str, ...] = ()

    @property
    def client_behind_target(self) -> bool | None:
        """Whether the client declares an older version than the target; ``None`` if undeclared."""
        if self.declared_client_version is None:
            return None
        return compare_versions(self.declared_client_version, self.target_version) < 0

    @property
    def flagged(self) -> tuple[str, ...]:
        """Distinct ids from ``stale``, ``weak_at_target``, ``unknown_version`` and ``weak_only``, in that order."""
        ordered: dict[str, None] = {}
        for entry_id in self.stale + self.weak_at_target + self.unknown_version + self.weak_only:
            ordered.setdefault(entry_id, None)
        return tuple(ordered)

    def summary(self) -> dict:
        return {
            "target_version": self.target_version,
            "declared_client_version": self.declared_client_version,
            "client_behind_target": self.client_behind_target,
            "total": self.total,
            "stale": len(self.stale),
            "weak_at_target": len(self.weak_at_target),
            "weak_only": len(self.weak_only),
            "unknown_version": len(self.unknown_version),
            "current": len(self.current),
            "flagged": len(self.flagged),
            "clean": self.total - len(self.flagged),
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "stale": list(self.stale),
            "weak_at_target": list(self.weak_at_target),
            "weak_only": list(self.weak_only),
            "unknown_version": list(self.unknown_version),
            "current": list(self.current),
        }

    def render_text(self) -> str:
        s = self.summary()
        lines = [
            f"Compatibility check against game version {self.target_version}",
            f"Ledger entries: {s['total']}; flagged: {s['flagged']}; clean: {s['clean']}",
        ]
        if self.declared_client_version is not None:
            relation = "older than" if self.client_behind_target else "not older than"
            lines.append(
                f"Client declares version {self.declared_client_version} ({relation} the target)"
            )
        for title, ids in (
            ("Never observed on the target version or newer", self.stale),
            ("Only weak evidence on the target version or newer", self.weak_at_target),
            ("No recorded game version", self.unknown_version),
            ("Weak evidence only", self.weak_only),
            ("Strong evidence on the target version or newer", self.current),
        ):
            lines.append(f"{title} ({len(ids)}):")
            lines.extend(f"  - {entry_id}" for entry_id in ids)
            if not ids:
                lines.append("  (none)")
        return "\n".join(lines)


def check_compatibility(
    target_version: str,
    entries: Iterable[LedgerEntry] | None = None,
    declared_client_version: str | None = None,
) -> CompatibilityReport:
    """Report which ledger entries are not shown to hold on ``target_version``.

    With ``entries`` omitted, the shipped ledger and its declared client
    version are used. Raises ``ValueError`` for an unparseable version.
    """
    parse_version(target_version)
    if entries is None:
        from sdk.compat.entries import DECLARED_CLIENT_VERSION, LEDGER

        entries = LEDGER
        if declared_client_version is None:
            declared_client_version = DECLARED_CLIENT_VERSION
    if declared_client_version is not None:
        parse_version(declared_client_version)
    ledger = validate_ledger(entries)

    stale, weak, unknown, current, weak_at_target = [], [], [], [], []
    for entry in ledger:
        latest = entry.latest_version
        latest_strong = entry.latest_strong_version
        if latest is None:
            unknown.append(entry.entry_id)
        elif latest_strong is not None and compare_versions(latest_strong, target_version) >= 0:
            current.append(entry.entry_id)
        elif compare_versions(latest, target_version) >= 0:
            weak_at_target.append(entry.entry_id)
        else:
            stale.append(entry.entry_id)
        if entry.weak_only:
            weak.append(entry.entry_id)

    return CompatibilityReport(
        target_version=target_version,
        declared_client_version=declared_client_version,
        stale=tuple(stale),
        weak_only=tuple(weak),
        unknown_version=tuple(unknown),
        current=tuple(current),
        total=len(ledger),
        weak_at_target=tuple(weak_at_target),
    )
