"""Append-only, redacted gameplay event journal (roadmap R1 evidence layer).

Each line of a journal file is one JSON event describing one attempted (or
deliberately skipped) gameplay action for one account: what was expected,
what was observed, how it reconciled, and what it cost. Parameters are
redacted through ``sdk.redaction`` before writing, and an event that still
carries a secret-shaped value after redaction is refused outright.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from sdk.redaction import redact_dict, redact_secrets
from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN

SCHEMA_VERSION = 1
OUTCOMES = frozenset({"verified", "not-observed", "contradicted", "unknown", "skipped"})


class JournalError(ValueError):
    """Raised for an invalid event, a secret-shaped value, or a corrupt journal."""


def new_attempt_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class GameplayEvent:
    account_alias: str
    action_id: str
    attempt_id: str
    started_at: str
    finished_at: str
    outcome: str
    reason: str
    attempted: bool
    expected_effect: str = ""
    response_ok: Optional[bool] = None
    error_class: Optional[str] = None
    dry_run: bool = False
    params: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Optional[float]] = field(default_factory=dict)
    # Optional, action-specific evidence derived from observation (for example
    # battle evidence tiers). Lines written before this field existed read back
    # with an empty mapping.
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        account_alias: str,
        action_id: str,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        reason: str,
        attempted: bool,
        attempt_id: Optional[str] = None,
        **extra: Any,
    ) -> "GameplayEvent":
        for moment in (started_at, finished_at):
            if moment.tzinfo is None:
                raise JournalError("event timestamps must be timezone-aware")
        if finished_at < started_at:
            raise JournalError("finished_at is earlier than started_at")
        return cls(
            account_alias=account_alias,
            action_id=action_id,
            attempt_id=attempt_id or new_attempt_id(),
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            outcome=outcome,
            reason=reason,
            attempted=attempted,
            **extra,
        )

    def validate(self) -> None:
        if not ACCOUNT_ALIAS_PATTERN.match(self.account_alias or ""):
            raise JournalError("account_alias must be a lowercase alias, not an e-mail or display name")
        if not self.action_id or not self.attempt_id:
            raise JournalError("action_id and attempt_id are required")
        if self.outcome not in OUTCOMES:
            raise JournalError(f"unknown outcome {self.outcome!r}")
        if self.outcome == "skipped" and self.attempted:
            raise JournalError("a skipped event cannot also be attempted")
        if self.dry_run and self.attempted:
            raise JournalError("a dry-run event cannot record an attempted mutating call")

    def to_json_line(self) -> str:
        self.validate()
        payload = asdict(self)
        payload["params"] = redact_dict(dict(self.params))
        payload["evidence"] = redact_dict(dict(self.evidence))
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if redact_secrets(line) != line:
            raise JournalError("event still contains a secret-shaped value after redaction; refusing to write")
        return line


class GameplayJournal:
    """Append-only JSONL journal at ``path``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, event: GameplayEvent) -> None:
        line = event.to_json_line()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[GameplayEvent]:
        return read_events(self.path)


def read_events(path: Path | str) -> list[GameplayEvent]:
    """Read every complete event. A final line without a newline (an
    interrupted write) is ignored; any other malformed line is an error."""
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    complete_lines = text.split("\n")[:-1]
    events = []
    for number, line in enumerate(complete_lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            event = GameplayEvent(**payload)
            event.validate()
        except (json.JSONDecodeError, TypeError, JournalError) as error:
            raise JournalError(f"{path}:{number}: malformed journal line ({error})") from None
        events.append(event)
    return events


def summarize(events: Iterable[GameplayEvent]) -> dict[str, dict[str, int]]:
    """Per-account counts: attempted, verified, not_observed, contradicted,
    uncertain (attempted with an unknown outcome), failed (an error class was
    recorded) and skipped."""
    summary: dict[str, Counter] = {}
    for event in events:
        counts = summary.setdefault(event.account_alias, Counter())
        if event.attempted:
            counts["attempted"] += 1
        if event.error_class:
            counts["failed"] += 1
        if event.outcome == "verified":
            counts["verified"] += 1
        elif event.outcome == "not-observed":
            counts["not_observed"] += 1
        elif event.outcome == "contradicted":
            counts["contradicted"] += 1
        elif event.outcome == "unknown" and event.attempted:
            counts["uncertain"] += 1
        elif event.outcome == "skipped":
            counts["skipped"] += 1
    keys = ("attempted", "verified", "not_observed", "contradicted", "uncertain", "failed", "skipped")
    return {alias: {key: counts.get(key, 0) for key in keys} for alias, counts in sorted(summary.items())}
