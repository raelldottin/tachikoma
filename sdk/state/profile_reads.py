"""Parsers for the read-only responses SPEC-03 verified against live captures.

Every parser here is built from a sanitized capture in
``tests/fixtures/live_sanitized/`` (S03-R1) and is pure: it takes parsed
xmltodict output and makes no call. A field absent from the response stays
``None``; nothing is defaulted to 0.

Ranking (S03-R3). ``parse_ranking`` reads the ``FindUserRanking`` element of
``LadderService/FindUserRanking``: the user's ladder position and the user
counts the server reports with it. It holds no trophy count.

Profile (S03-R3). No read-only call in the client returns the current user's
trophies and starbux except the login response (``UserService/DeviceLogin17``),
whose ``User`` record ``Client.login`` keeps as ``client.info``: ``@Trophy`` is
trophies and ``@Credits`` is starbux (G1 capture, 2026-11-12). Values held from
login go stale, so a profile counts as observed only through a
``ProfileObservation``: the record plus the time the server sent it. It is
used for one snapshot, and only while it is at most ``PROFILE_MAX_AGE`` old
(``fresh_profile``). Later snapshots, such as the one after an action, report
the profile as missing instead of repeating pre-action values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

# ASCII digits only: ``str.isdigit`` also accepts characters such as ``²`` that ``int`` rejects.
_WHOLE_NUMBER = re.compile(r"-?[0-9]+")
# How old a server response may be and still count as the current profile.
PROFILE_MAX_AGE = timedelta(minutes=5)
# A response time ahead of the snapshot clock by more than this is refused.
PROFILE_CLOCK_SKEW = timedelta(seconds=30)
# The only ``User`` fields a profile keeps; the rest of the record (e-mail
# status, purchase dates, lifetime counters...) is never copied.
PROFILE_FIELDS = ("@Trophy", "@HighestTrophy", "@Credits")


def int_or_none(value: Any) -> Optional[int]:
    """``value`` as an int when it is a whole number (``"42"``, ``42``), else ``None``. Booleans are not numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if _WHOLE_NUMBER.fullmatch(text) else None


def user_record(payload: Any) -> Optional[Mapping[str, Any]]:
    """The ``User`` record in a login response: ``client.info`` itself, or the ``User`` element of a wrapped one.

    A wrapped response (``UserService/UserLogin/User``) is searched for a ``User`` key first, so the
    ``UserLogin`` element's own attributes are never mistaken for the user. A bare mapping counts as the
    record only when it holds a user field (``@Id`` or ``@Trophy``).
    """
    if not isinstance(payload, Mapping):
        return None
    user = payload.get("User")
    if isinstance(user, Mapping):
        return user
    if "@Id" in payload or "@Trophy" in payload:
        return payload
    for key, value in payload.items():
        if not str(key).startswith("@"):
            found = user_record(value)
            if found is not None:
                return found
    return None


@dataclass(frozen=True)
class Profile:
    """The current user's trophies and starbux from one server response; ``None`` when not in it."""

    trophies: Optional[int]
    highest_trophy: Optional[int]
    starbux: Optional[int]

    def snapshot_profile(self) -> dict[str, str]:
        """The ``profile`` mapping ``build_snapshot`` takes (only observed fields)."""
        values = {"@Trophy": self.trophies, "@HighestTrophy": self.highest_trophy}
        return {key: str(value) for key, value in values.items() if value is not None}


def parse_profile(payload: Any) -> Optional[Profile]:
    """``Profile`` from a login ``User`` record, or ``None`` when the payload holds no ``User`` record."""
    record = user_record(payload)
    return None if record is None else _profile_from_record(record)


def _profile_from_record(record: Mapping[str, Any]) -> Profile:
    return Profile(trophies=int_or_none(record.get("@Trophy")),
                   highest_trophy=int_or_none(record.get("@HighestTrophy")),
                   starbux=int_or_none(record.get("@Credits")))


@dataclass(frozen=True)
class ProfileObservation:
    """A ``User`` record and the timezone-aware time the server sent it."""

    record: Mapping[str, Any]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        kept = {key: self.record[key] for key in PROFILE_FIELDS if key in self.record}
        object.__setattr__(self, "record", kept)


def fresh_profile(observation: Optional[ProfileObservation], now: datetime,
                  max_age: timedelta = PROFILE_MAX_AGE) -> Optional[Profile]:
    """The observation's ``Profile`` when it is at most ``max_age`` old at ``now``, else ``None``."""
    if observation is None or not observation.record:  # no profile field in the record: nothing observed
        return None
    age = now - observation.observed_at
    if age > max_age or age < -PROFILE_CLOCK_SKEW:
        return None
    return _profile_from_record(observation.record)


@dataclass(frozen=True)
class Ranking:
    """The user's ladder position from ``FindUserRanking``; ``None`` when not in the response."""

    ranking: Optional[int]
    total_users: Optional[int]
    total_active_users: Optional[int]


def parse_ranking(payload: Any) -> Optional[Ranking]:
    """``Ranking`` from a ``FindUserRanking`` response (``client.userRanking``), or ``None`` without one."""
    if not isinstance(payload, Mapping):
        return None
    record = payload.get("LadderService", payload)
    record = record.get("FindUserRanking") if isinstance(record, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    return Ranking(ranking=int_or_none(record.get("@Ranking")),
                   total_users=int_or_none(record.get("@TotalUsers")),
                   total_active_users=int_or_none(record.get("@TotalActiveUsers")))
