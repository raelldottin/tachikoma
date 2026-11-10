"""Build AccountSnapshots from read-only ``sdk.client.Client`` calls (R2 input).

``ClientSnapshotProvider`` is a snapshot provider for ``sdk.execution``. Each
call performs three reads (ship, crew, inventory) and nothing else. It never
calls a method that changes the account. A read that raises, returns
``False`` or yields no payload leaves its sections ``missing``.

Values the client only holds from login, such as starbux credits and
trophies, are deliberately not reported. They could be hours old and would
make reconciliation report "no change" when the value is simply unknown.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN, AccountSnapshot, SnapshotInputError, build_snapshot

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
    ) -> None:
        if not ACCOUNT_ALIAS_PATTERN.match(account_alias or ""):
            raise SnapshotInputError("account_alias must be a lowercase alias, not an e-mail or display name")
        self.client = client
        self.account_alias = account_alias
        self.game_version = game_version
        self.source = source
        self.clock = clock
        self.failed_reads: tuple[str, ...] = ()

    def _read(self, method_name: str, attribute: str) -> Any:
        try:
            ok = getattr(self.client, method_name)()
        except Exception:  # a failed read leaves the section missing
            return None
        if ok is False:
            return None
        return getattr(self.client, attribute, None)

    def __call__(self) -> AccountSnapshot:
        failed = []

        ship_payload = self._read("getShipByUserId", "shipByUserId")
        ship = _find(ship_payload, "Ship") if ship_payload is not None else None
        if not isinstance(ship, dict):
            ship = None
            failed.append("getShipByUserId")
        rooms = _as_records(_find(ship.get("Rooms"), "Room")) if ship else None
        research = _as_records(_find(ship.get("Researches"), "Research")) if ship else None

        crew_payload = self._read("listAllCharactersOfUser", "allCharactersOfUser")
        crew = _as_records(_find(crew_payload, "Character")) if crew_payload is not None else None
        if crew is None:
            failed.append("listAllCharactersOfUser")

        items_payload = self._read("listItemsOfAShip", "itemsOfAShip")
        items = _as_records(_find(items_payload, "Item")) if items_payload is not None else None
        if items is None:
            failed.append("listItemsOfAShip")

        resources = None
        if items is not None:
            resources = {
                "minerals": _resource_from_items(items, "mineral"),
                "gas": _resource_from_items(items, "gas"),
                "starbux": None,
            }

        self.failed_reads = tuple(failed)
        ship_fields = {key: value for key, value in ship.items() if key.startswith("@")} if ship else None
        return build_snapshot(
            self.account_alias,
            self.clock(),
            self.source,
            game_version=self.game_version,
            ship=ship_fields,
            resources=resources,
            crew=crew,
            rooms=rooms,
            research=research,
            inventory=items,
        )
