from __future__ import annotations

"""Read-only inventory catalog: normalize raw item responses into typed Items.

Pure domain logic — no I/O. Inputs are already-parsed dicts (test seam:
sanitized fixtures) or a Client instance (``from_client``).

ItemId is the account-scoped instance identifier; ItemDesignId is the global
design key. They are never conflated. Unknown design resolution happens in
the caller (catalog stores Items as-is; ``DesignCatalog.get`` returns None
for unknown ids, and the classifier/policy layer fails closed on that).
"""

import logging
from typing import Any, Dict, Mapping, Optional, Sequence

from sdk.client import _extract_collection
from ._types import CAPTURED_ITEM_ATTRIBUTES, Item
from .designs import DesignCatalog


class DuplicateItemIdError(ValueError):
    """Raised when a ship item response contains duplicate ItemId rows.

    AD-4: duplicate ItemId rows are never silently double-counted; the caller
    decides whether to normalize or abort.
    """


def parse_item(node: Mapping[str, Any]) -> Optional[Item]:
    """Normalize one raw Item dict into an Item.

    Preserves exactly the six capture-observed attributes as first-class
    fields (RT-4); any additional attributes land in ``raw`` for forward
    compatibility but are never consumed by this slice.

    Tolerant of missing attributes (quantity defaults to 1, is_new False).
    Missing/unparseable ItemId or ItemDesignId returns None (fail closed).
    """
    if not isinstance(node, Mapping):
        return None
    try:
        item_id = int(str(node.get("@ItemId", "")).strip())
        design_id = int(str(node.get("@ItemDesignId", "")).strip())
    except (TypeError, ValueError):
        return None
    try:
        ship_id = int(str(node.get("@ShipId", "0")).strip() or "0")
    except (TypeError, ValueError):
        ship_id = 0
    try:
        quantity = int(str(node.get("@Quantity", "1")).strip() or "1")
    except (TypeError, ValueError):
        quantity = 1
    is_new = str(node.get("@IsNew", "false")).strip().lower() == "true"
    skin_key = node.get("@SkinKey") or None
    raw = dict(node)

    extras = set(k.lstrip("@") for k in raw) - set(CAPTURED_ITEM_ATTRIBUTES)
    if extras:
        logging.info(
            "parse_item: Item %s carries attributes beyond the six "
            "capture-observed ones: %s (preserved in raw, not consumed)",
            item_id,
            sorted(extras),
        )

    return Item(
        item_id=item_id,
        ship_id=ship_id,
        item_design_id=design_id,
        quantity=quantity,
        is_new=is_new,
        skin_key=skin_key,
        raw=raw,
    )


class InventoryCatalog:
    """Read-only typed view of a ship's inventory.

    Deterministic: items are deduped by ItemId and iterated in ascending
    ItemId order. Fails closed on duplicate ItemId rows (DuplicateItemIdError)
    unless ``allow_duplicate_item_ids=True`` (first occurrence wins, logged).
    """

    def __init__(
        self,
        items: Sequence[Item],
        design_catalog: Optional[DesignCatalog] = None,
        allow_duplicate_item_ids: bool = False,
    ):
        by_id: Dict[int, Item] = {}
        for item in items:
            if item.item_id in by_id:
                if not allow_duplicate_item_ids:
                    raise DuplicateItemIdError(
                        f"duplicate ItemId {item.item_id} in ship inventory response"
                    )
                logging.warning(
                    "InventoryCatalog: duplicate ItemId %s; keeping first occurrence",
                    item.item_id,
                )
                continue
            by_id[item.item_id] = item
        self._items = dict(sorted(by_id.items()))
        self.design_catalog = design_catalog

    @classmethod
    def from_dicts(
        cls,
        items_response: Mapping[str, Any],
        design_catalog: Optional[DesignCatalog] = None,
        allow_duplicate_item_ids: bool = False,
    ) -> "InventoryCatalog":
        """Build from a parsed ListItemsOfAShip response dict.

        Handles both single-dict and list forms via _extract_collection.
        NOTE: ListItemsOfAShip has NEVER been captured; the full live field
        list is UNKNOWN beyond the six observed attributes (see
        INVENTORY_ENDPOINT_ANALYSIS.md). Do not relabel it VERIFIED.
        """
        nodes = _extract_collection(items_response, "Item")
        items = []
        for node in nodes:
            item = parse_item(node)
            if item is not None:
                items.append(item)
        return cls(items, design_catalog, allow_duplicate_item_ids)

    @classmethod
    def from_client(cls, client, design_catalog: Optional[DesignCatalog] = None) -> "InventoryCatalog":
        """Build from a Client, fetching items and designs read-only as needed."""
        raw = getattr(client, "itemsOfAShip", None)
        if raw is None:
            if not client.listItemsOfAShip():
                logging.error("InventoryCatalog: failed to fetch ship items")
                return cls((), design_catalog)
            raw = client.itemsOfAShip
        if design_catalog is None:
            design_catalog = DesignCatalog.from_client(client)
        return cls.from_dicts(raw, design_catalog)

    def items(self) -> Sequence[Item]:
        """All items, deterministic ascending ItemId order."""
        return list(self._items.values())

    def by_design(self, design_id: int) -> Sequence[Item]:
        """All items with the given ItemDesignId, ascending ItemId order.

        ItemDesignId (design key) is matched here, NOT ItemId (instance id).
        """
        return [i for i in self._items.values() if i.item_design_id == int(design_id)]

    def quantity_of(self, design_id: int) -> int:
        """Total quantity owned of a design; zero-quantity rows count as 0."""
        return sum(i.quantity for i in self.by_design(design_id))

    def resolve(self, item: Item):
        """Resolve an Item's ItemDesign via the catalog; None when unknown.

        Unknown designs are unresolvable — the caller (classification layer)
        fails closed on None (AD-1); nothing here invents a design.
        """
        if self.design_catalog is None:
            return None
        return self.design_catalog.get(item.item_design_id)

    def __len__(self) -> int:
        return len(self._items)
