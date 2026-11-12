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

Battles (S03-R4). ``parse_battles`` reads the ``Battle`` elements of
``BattleService/ListPvPBattles/Battles`` (``client.pvpBattles``) into
``BattleRecord`` values, from the point of view of one user id. What the
captures (pss1 with 10 battles, pss5 with none; G1, 2026-11-12) show, and so
what a record keeps:

- ``outcome`` is ``@OutcomeType`` as the server lists it (``"Attacker Won"``).
  In the capture it always equals ``@ClientOutcomeType`` while
  ``@ServerOutcomeType`` is ``"Waiting"``, so it may echo what the attacking
  client reported; both raw values are kept. ``won`` is read from it and the
  role only for ``"Attacker Won"`` and ``"Defender Won"``.
- ``trophy_change`` is the attacker's result: ``+@WinTrophyResult`` when the
  attacker won, ``-@LoseTrophyResult`` when it lost. It is ``None`` on a
  defense: nothing in the capture says how a defender's trophies change.
- ``opponent_trophies`` is ``None`` for ``"0"``: every ``@AttackingUserTrophy``
  and ``@DefendingUserTrophy`` in the capture is ``"0"``, for the account and
  for each opponent, so a 0 there cannot be told from "not reported".
- ``opponent_hull_level`` is always ``None``: no battle field holds a hull
  level, and a ship design id is not one.
- ``battle_date`` is ``@BattleDate``, server time without a zone, read as UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


ATTACKER = "attacker"
DEFENDER = "defender"
# The ``@OutcomeType`` values that name a winner; any other value leaves ``won`` unknown.
_OUTCOME_WINNER = {"Attacker Won": ATTACKER, "Defender Won": DEFENDER}


@dataclass(frozen=True)
class BattleRecord:
    """One ``ListPvPBattles2`` battle seen from one user; ``None`` where the response does not say."""

    battle_id: Optional[str]
    role: Optional[str]  # "attacker", "defender", or None when the user is neither
    outcome: Optional[str]  # @OutcomeType as the server lists it
    server_outcome: Optional[str]  # @ServerOutcomeType
    client_outcome: Optional[str]  # @ClientOutcomeType
    won: Optional[bool]
    trophy_change: Optional[int]
    opponent_ship_design_id: Optional[str]
    opponent_trophies: Optional[int]
    opponent_hull_level: Optional[int]
    battle_date: Optional[datetime]


def _text_or_none(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (Mapping, list)):
        return None
    text = str(value).strip()
    return text or None


def _id_or_none(value: Any) -> Optional[str]:
    """An opaque id as text; ``None`` when absent, empty or the ``"0"`` the server sends for no id."""
    text = _text_or_none(value)
    return None if text in (None, "0") else text


def _utc_or_none(value: Any) -> Optional[datetime]:
    text = _text_or_none(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _battle_elements(payload: Any) -> Optional[list]:
    """The ``Battle`` mappings; ``[]`` for an empty element, ``None`` for a missing or malformed one."""
    if not isinstance(payload, Mapping):
        return None
    service = payload.get("BattleService", payload)
    if not isinstance(service, Mapping) or "ListPvPBattles" not in service:
        return None
    level: Any = service["ListPvPBattles"]
    for child in ("Battles", "Battle"):
        if level is None:  # an empty element, as in the pss5 capture
            return []
        if not isinstance(level, Mapping):
            return None
        level = level.get(child)
    if level is None:
        return []
    if isinstance(level, Mapping):
        return [level]
    if isinstance(level, list) and all(isinstance(battle, Mapping) for battle in level):
        return list(level)
    return None


def _battle_record(battle: Mapping[str, Any], own_user_id: Optional[str]) -> BattleRecord:
    role = None
    if own_user_id is not None:
        if _id_or_none(battle.get("@AttackingUserId")) == own_user_id:
            role = ATTACKER
        elif _id_or_none(battle.get("@DefendingUserId")) == own_user_id:
            role = DEFENDER
    outcome = _text_or_none(battle.get("@OutcomeType"))
    winner = _OUTCOME_WINNER.get(outcome or "")
    won = None if role is None or winner is None else winner == role
    trophy_change = None
    if role == ATTACKER and winner == ATTACKER:
        trophy_change = int_or_none(battle.get("@WinTrophyResult"))
    elif role == ATTACKER and winner == DEFENDER:
        lost = int_or_none(battle.get("@LoseTrophyResult"))
        trophy_change = -abs(lost) if lost is not None else None
    opponent = {ATTACKER: "Defending", DEFENDER: "Attacking"}.get(role or "")
    opponent_trophies = int_or_none(battle.get(f"@{opponent}UserTrophy")) if opponent else None
    return BattleRecord(
        battle_id=_id_or_none(battle.get("@BattleId")),
        role=role,
        outcome=outcome,
        server_outcome=_text_or_none(battle.get("@ServerOutcomeType")),
        client_outcome=_text_or_none(battle.get("@ClientOutcomeType")),
        won=won,
        trophy_change=trophy_change,
        opponent_ship_design_id=_id_or_none(battle.get(f"@{opponent}ShipDesignId")) if opponent else None,
        opponent_trophies=opponent_trophies or None,  # "0" is indistinguishable from unreported
        opponent_hull_level=None,  # no battle field holds it
        battle_date=_utc_or_none(battle.get("@BattleDate")),
    )


def parse_battles(payload: Any, own_user_id: Any) -> Optional[list[BattleRecord]]:
    """``BattleRecord`` values from a ``ListPvPBattles2`` response (``client.pvpBattles``) seen from ``own_user_id``.

    ``[]`` for an empty battle list, ``None`` when the payload holds no
    ``ListPvPBattles`` element. An unknown own id (``None``, ``""``, ``"0"``)
    gives every record the role ``None``.
    """
    battles = _battle_elements(payload)
    if battles is None:
        return None
    own = _id_or_none(own_user_id)
    return [_battle_record(battle, own) for battle in battles]


def matching_battle(records: Optional[list[BattleRecord]], battle_id: Any) -> Optional[BattleRecord]:
    """The record with ``battle_id``, or ``None`` when there is no such record or no battle id."""
    wanted = _id_or_none(battle_id)
    if wanted is None or not records:
        return None
    return next((record for record in records if record.battle_id == wanted), None)
