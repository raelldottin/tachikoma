from __future__ import annotations

"""Design feed normalization.

ItemDesign rows come from the DYNAMIC feed (DesignService/ListAllDynamicDesigns
via ``Client.listAllDynamicDesigns``) — 2,557 rows decoded from the 2026-08-08
capture. The static branch (ListAllDesigns4) contains ZERO ItemDesign rows and
is never consulted for ItemDesigns; it remains the source for
TrainingDesign/CraftDesign only.

This module performs no network I/O. Inputs are already-parsed dicts (test
seam: sanitized fixtures) or a Client instance (``from_client``).
"""

import logging
from typing import Any, Mapping, Optional, Sequence, Tuple

from sdk.client import _extract_collection
from ._types import ItemDesign


def _parse_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_tags(raw: Any) -> Tuple[str, ...]:
    """Parse @Tags into a sorted tuple of non-empty tag strings."""
    if not raw:
        return ()
    parts = [t.strip() for t in str(raw).replace(";", ",").split(",")]
    return tuple(sorted(t for t in parts if t))


def parse_ingredients(raw: Any) -> Optional[Tuple[Tuple[int, int], ...]]:
    """Parse an @Ingredients string like ``"81x5"`` or ``"78x3|104x6"``.

    Returns a tuple of (ingredient_design_id, quantity) sorted by design id,
    or None when absent/empty. Raises ValueError on malformed input so the
    caller can fail closed (a malformed recipe marks the design PROTECTED —
    it is never silently repaired, safety invariant S4).
    """
    if raw is None or str(raw).strip() == "":
        return None
    if str(raw).strip() == "0":
        # Live-feed null marker (capture-verified 2026-08-08: designs
        # 544/780/786 carry Ingredients="0" and have no recipe; same "0"
        # null convention as TrainingDesignId="0"). Treating it as absent
        # keeps the design cataloged; genuinely malformed strings below
        # still raise so the caller can fail closed.
        return None
    out = []
    for part in str(raw).split("|"):
        part = part.strip()
        if not part:
            raise ValueError(f"empty ingredient segment in {raw!r}")
        if "x" not in part:
            raise ValueError(f"malformed ingredient segment {part!r}")
        design_s, qty_s = part.split("x", 1)
        if not design_s.strip().isdigit() or not qty_s.strip().isdigit():
            raise ValueError(f"malformed ingredient segment {part!r}")
        out.append((int(design_s), int(qty_s)))
    return tuple(sorted(out))


def parse_item_design(node: Mapping[str, Any]) -> Optional[ItemDesign]:
    """Normalize one raw ItemDesign dict into an ItemDesign.

    Unknown/unresolvable design id returns None (fail closed). Malformed
    @Ingredients raises ValueError for the caller to route to PROTECTED.
    """
    if not isinstance(node, Mapping):
        return None
    design_id = _parse_int(node.get("@ItemDesignId"))
    if design_id is None:
        return None
    module_type = node.get("@ModuleType") or None
    if module_type and str(module_type).lower() in ("none", ""):
        module_type = None
    skin = node.get("@SkinKey") or None  # preserved via raw; not modeled here
    raw = dict(node)
    if skin is not None:
        raw.setdefault("@SkinKey", skin)
    # Live dynamic feed emits @ItemDesignName (capture-verified 2026-08-08:
    # 2557/2557 rows use ItemDesignName, zero use ItemName). @ItemName is
    # accepted as a legacy/synthetic fallback only when the live attribute
    # is absent.
    name = node.get("@ItemDesignName")
    if name is None:
        name = node.get("@ItemName", "")
    # Live-feed null convention: TrainingDesignId="0" (2467/2557 captured
    # rows) means the design has NO training link — 0 must parse to None so
    # the classifier does not mark every design a training material.
    training_design_id = _parse_int(node.get("@TrainingDesignId"))
    if not training_design_id:
        training_design_id = None
    return ItemDesign(
        item_design_id=design_id,
        item_name=str(name),
        item_type=str(node.get("@ItemType", "")),
        item_sub_type=str(node.get("@ItemSubType", "")),
        rarity=str(node.get("@Rarity", "")),
        module_type=module_type,
        tags=_parse_tags(node.get("@Tags")),
        ingredients=parse_ingredients(node.get("@Ingredients")) or (),
        training_design_id=training_design_id,
        raw=raw,
    )


class DesignCatalog:
    """Read-only ItemDesign index keyed by ItemDesignId.

    Built from the dynamic design feed. Deterministic iteration order:
    ascending ItemDesignId.
    """

    def __init__(self, designs: Sequence[ItemDesign]):
        # Deterministic dedupe: first occurrence wins, iteration ordered by id.
        seen = {}
        for d in designs:
            seen.setdefault(d.item_design_id, d)
        self._designs = dict(sorted(seen.items()))

    @classmethod
    def from_dynamic_dict(cls, dynamic_response: Mapping[str, Any]) -> "DesignCatalog":
        """Build from a parsed ListAllDynamicDesigns response dict."""
        nodes = _extract_collection(dynamic_response, "ItemDesign")
        designs = []
        for node in nodes:
            try:
                design = parse_item_design(node)
            except ValueError as e:
                logging.warning("DesignCatalog: skipping malformed ItemDesign: %s", e)
                continue
            if design is not None:
                designs.append(design)
        return cls(designs)

    @classmethod
    def from_client(cls, client) -> "DesignCatalog":
        """Build from a Client, fetching the dynamic feed if needed.

        Uses ONLY the dynamic feed for ItemDesigns (RT-5; the static branch
        is never consulted for ItemDesign rows). The Client stores the
        parsed feed as ``dynamicDesigns`` (client.py listAllDynamicDesigns).
        """
        raw = getattr(client, "dynamicDesigns", None)
        if raw is None:
            if not client.listAllDynamicDesigns():
                logging.error("DesignCatalog: failed to fetch dynamic design feed")
                return cls(())
            raw = client.dynamicDesigns
        return cls.from_dynamic_dict(raw)

    def get(self, design_id: int) -> Optional[ItemDesign]:
        """Return the design for an ItemDesignId, or None if unknown."""
        return self._designs.get(int(design_id))

    def all(self) -> Sequence[ItemDesign]:
        """All designs, deterministic ascending ItemDesignId order."""
        return list(self._designs.values())

    def __len__(self) -> int:
        return len(self._designs)
