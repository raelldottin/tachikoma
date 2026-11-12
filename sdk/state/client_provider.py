"""Build AccountSnapshots from read-only ``sdk.client.Client`` calls (R2 input).

``ClientSnapshotProvider`` is a snapshot provider for ``sdk.execution``. Each
call performs three reads (ship, crew, inventory) and nothing else. It never
calls a method that changes the account. A read that raises, returns
``False`` or yields no payload leaves its sections ``missing``.

Values the client holds from login, such as starbux credits and trophies,
are reported only through a ``ProfileObservation`` (``sdk.state.profile_reads``,
S03-R3): the login ``User`` record with the time the server sent it. The first
snapshot uses it when it is at most ``PROFILE_MAX_AGE`` old; it is never used
again, so a later snapshot (after an action) reports ``profile`` missing
instead of repeating pre-action values, which would make reconciliation report
"no change" when the value is simply unknown. Without one, ``profile`` is
missing and starbux is ``None``.

Game identity: when the caller passes ``identity_salt``, each snapshot carries
``game_identity``, an HMAC fingerprint of ``client.user.id`` (the id the
client logged in as and reads its ship with). The raw id is read only inside
``_game_identity`` and is never stored, returned or put in error text. No salt,
or an unknown id, leaves ``game_identity`` as ``None``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sdk.state.profile_reads import ProfileObservation, fresh_profile
from sdk.state.snapshot import (
    ACCOUNT_ALIAS_PATTERN,
    AccountSnapshot,
    SnapshotInputError,
    build_snapshot,
    game_identity_fingerprint,
    identity_salt_key,
)

READ_METHODS = ("getShipByUserId", "listAllCharactersOfUser", "listItemsOfAShip")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _find(data: Any, key: str) -> Any:
    """Depth-first lookup of ``key`` in nested xmltodict output."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _find(value, key)
            if found is not None:
                return found
    return None


def _as_records(value: Any) -> Optional[list[dict]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return None


def _container_records(data: Any, container_key: str, record_key: str) -> Optional[list[dict]]:
    """Records inside ``container_key`` (for example ``Researches``/``Research``).

    A present container with no records (``<Researches/>`` parses to ``None``)
    is observed and empty, so it returns ``[]``. Only an absent container
    returns ``None``, which leaves the section missing.
    """
    if not isinstance(data, dict):
        return None
    if container_key in data:
        container = data[container_key]
        return _as_records(_find(container, record_key)) or []
    for value in data.values():
        found = _container_records(value, container_key, record_key)
        if found is not None:
            return found
    return None


def _resource_from_items(items: Optional[list[dict]], kind: str) -> Optional[str]:
    """Return the quantity of the item typed as ``kind`` (mineral or gas).

    Unlike ``Client.collectAllResources``, this never falls back to item
    position: an item without a matching type leaves the resource unknown.
    """
    if not items:
        return None
    matches = [
        item
        for item in items
        if kind in str(item.get("@Type") or item.get("@ItemType") or "").lower()
    ]
    return str(matches[0].get("@Quantity")) if len(matches) == 1 else None


class ClientSnapshotProvider:
    def __init__(
        self,
        client: Any,
        account_alias: str,
        *,
        game_version: Optional[str] = None,
        source: str = "client-reads",
        clock: Callable[[], datetime] = _utc_now,
        identity_salt: Optional[bytes | str] = None,
        profile_observation: Optional[ProfileObservation] = None,
    ) -> None:
        if not ACCOUNT_ALIAS_PATTERN.match(account_alias or ""):
            raise SnapshotInputError("account_alias must be a lowercase alias, not an e-mail or display name")
        # Validated now so a short salt fails at construction, not as a silent None.
        self._identity_key = None if identity_salt is None else identity_salt_key(identity_salt)
        self.client = client
        self.account_alias = account_alias
        self.game_version = game_version
        self.source = source
        self.clock = clock
        self.failed_reads: tuple[str, ...] = ()
        self._profile_observation = profile_observation

    def _read(self, method_name: str, attribute: str) -> Any:
        try:
            ok = getattr(self.client, method_name)()
        except Exception:  # a failed read leaves the section missing
            return None
        if ok is False:
            return None
        return getattr(self.client, attribute, None)

    def _game_identity(self) -> Optional[str]:
        if self._identity_key is None:
            return None
        user_id = getattr(getattr(self.client, "user", None), "id", None)
        try:
            return game_identity_fingerprint(user_id, self._identity_key)
        except SnapshotInputError:  # unknown id: identity stays unobserved
            return None

    def _profile(self, now: datetime):
        """The profile for this snapshot: the observation once, while it is fresh; never again."""
        observation, self._profile_observation = self._profile_observation, None
        return fresh_profile(observation, now)

    def __call__(self) -> AccountSnapshot:
        failed = []
        observed_at = self.clock()
        profile = self._profile(observed_at)

        ship_payload = self._read("getShipByUserId", "shipByUserId")
        ship = _find(ship_payload, "Ship") if ship_payload is not None else None
        if not isinstance(ship, dict):
            ship = None
            failed.append("getShipByUserId")
        rooms = _container_records(ship, "Rooms", "Room") if ship else None
        research = _container_records(ship, "Researches", "Research") if ship else None

        crew_payload = self._read("listAllCharactersOfUser", "allCharactersOfUser")
        crew = _container_records(crew_payload, "Characters", "Character") if crew_payload is not None else None
        if crew is None and crew_payload is not None:
            crew = _as_records(_find(crew_payload, "Character"))
        if crew is None:
            failed.append("listAllCharactersOfUser")

        items_payload = self._read("listItemsOfAShip", "itemsOfAShip")
        items = _container_records(items_payload, "Items", "Item") if items_payload is not None else None
        if items is None and items_payload is not None:
            items = _as_records(_find(items_payload, "Item"))
        if items is None:
            failed.append("listItemsOfAShip")

        starbux = profile.starbux if profile is not None else None
        resources = None
        if items is not None or starbux is not None:
            resources = {
                "minerals": _resource_from_items(items, "mineral"),
                "gas": _resource_from_items(items, "gas"),
                "starbux": starbux,
            }

        self.failed_reads = tuple(failed)
        ship_fields = {key: value for key, value in ship.items() if key.startswith("@")} if ship else None
        return build_snapshot(
            self.account_alias,
            observed_at,
            self.source,
            game_version=self.game_version,
            profile=profile.snapshot_profile() if profile is not None else None,
            ship=ship_fields,
            resources=resources,
            crew=crew,
            rooms=rooms,
            research=research,
            inventory=items,
            game_identity=self._game_identity(),
        )
