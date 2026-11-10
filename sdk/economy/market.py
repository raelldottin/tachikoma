"""Typed marketplace listings (R4, AUTO-008).

``Client.listActiveMarketplaceMessages`` reads ``ListActiveMarketplaceMessages5``
and keeps the parsed ``Message`` records in ``client.marketplaceMessages``.
``parse_listing`` turns one record into a ``MarketListing`` using a whitelist
of attributes. Only ``@Message`` and ``@ActivityArgument`` (``currency:price``)
are seen in the repository's fixtures; the identity and item attributes are
read only when present and are otherwise ``None``, never guessed.

A listing says something is offered. It is not evidence that anything was
sold, bought or transferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from sdk.redaction import redact_secrets


class MarketInputError(ValueError):
    """Raised for a malformed record or one carrying secret-shaped values."""


def _text(value: Any) -> Optional[str]:
    if isinstance(value, list):
        value = "".join(str(part) for part in value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> Optional[int]:
    text = _text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class MarketListing:
    item_name: Optional[str]
    currency: Optional[str]
    price: Optional[int]
    sale_id: Optional[str] = None
    message_id: Optional[str] = None
    item_design_id: Optional[str] = None
    quantity: Optional[int] = None

    @property
    def has_identity(self) -> bool:
        """True only when the server gave a sale id; names and prices are not identities."""
        return self.sale_id is not None

    def to_dict(self) -> dict:
        return {
            "item_name": self.item_name,
            "currency": self.currency,
            "price": self.price,
            "sale_id": self.sale_id,
            "message_id": self.message_id,
            "item_design_id": self.item_design_id,
            "quantity": self.quantity,
        }


def _activity(argument: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    if not argument or ":" not in argument:
        return None, None
    currency, _, price = argument.partition(":")
    currency = currency.strip().lower() or None
    return currency, _int_or_none(price)


def parse_listing(message: Mapping[str, Any]) -> MarketListing:
    if not isinstance(message, Mapping):
        raise MarketInputError("a marketplace message must be a mapping")
    currency, price = _activity(_text(message.get("@ActivityArgument")))
    listing = MarketListing(
        item_name=_text(message.get("@Message")),
        currency=currency,
        price=price if price is None or price >= 0 else None,
        sale_id=_text(message.get("@SaleId")),
        message_id=_text(message.get("@MessageId")),
        item_design_id=_text(message.get("@ItemDesignId")),
        quantity=_int_or_none(message.get("@Quantity")),
    )
    for value in listing.to_dict().values():
        if isinstance(value, str) and redact_secrets(value) != value:
            raise MarketInputError("marketplace message carries a secret-shaped value; refusing it")
    return listing


def listings_from_messages(messages: Iterable[Mapping[str, Any]]) -> tuple[MarketListing, ...]:
    return tuple(parse_listing(message) for message in messages)


def observed_listings(holder: Any) -> Optional[tuple[MarketListing, ...]]:
    """Listings from an object exposing ``marketplaceMessages`` (a Client after a read).

    Returns ``None`` when no successful read is recorded, so "not observed"
    is never confused with "no listings".
    """
    messages = getattr(holder, "marketplaceMessages", None)
    if messages is None:
        return None
    return listings_from_messages(messages)
