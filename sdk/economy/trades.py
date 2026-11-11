"""Market trade lifecycle reconciliation (R4).

``reconcile_trade`` decides how far one market trade provably got, from
timestamped observations only. A created listing is at most ``listed``.

Every amount check uses a fee bound: ``max_fee`` (default
``default_max_fee(price)``, 25% of the price rounded up). Sale proceeds must
lie in ``[price - max_fee, price]`` and a purchase payment in
``[price, price + max_fee]``; anything outside is ``unknown``.

Sell side:

- ``requested``: the listing call was made; nothing else is confirmed. This
  includes "ambiguous listing": more than one distinct sale id matches.
- ``listed``: exactly one listing with a server sale id was first seen after
  the request, matching the item (when the listing names it), quantity
  (when given), currency and price. Sale ids already seen before the request
  are older listings and never match.
- ``completed``: the listing was seen, then disappeared, the item left the
  inventory in the requested quantity, and the trade currency rose, at or
  after the disappearance was observed, by an amount within the fee bound.
  Proceeds are that rise; fees are the price minus proceeds.
- ``expired``: the listing disappeared and the item came back.
- ``unknown``: the listing disappeared without a consistent item and
  currency movement, or the movements contradict the trade.

Buy side:

- ``requested``: the purchase call was made.
- ``completed``: exactly the requested quantity of the item entered the
  inventory and the trade currency fell by an amount within the fee bound.
  Cost is that fall; fees are cost minus price.
- ``unknown``: money left or the item arrived, but not both, amounts are
  inconsistent, or the targeted listing was still seen open after both.

Observations before the request are ignored, except that a sale id seen
before a sell request marks an older listing. Pure and offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence, Union

from sdk.economy.market import MarketListing

REQUESTED, LISTED, COMPLETED, EXPIRED, UNKNOWN = "requested", "listed", "completed", "expired", "unknown"
SELL, BUY = "sell", "buy"


def default_max_fee(price: int) -> int:
    """The default fee bound: 25% of the price, rounded up."""
    return -(-price // 4)


class TradeInputError(ValueError):
    """Raised for a malformed trade request or observation."""


@dataclass(frozen=True)
class SellRequest:
    at: datetime
    item_design_id: str
    quantity: int
    currency: str
    price: int


@dataclass(frozen=True)
class BuyRequest:
    at: datetime
    sale_id: str
    item_design_id: str
    quantity: int
    currency: str
    price: int


@dataclass(frozen=True)
class ListingSeen:
    at: datetime
    listing: MarketListing


@dataclass(frozen=True)
class ListingGone:
    at: datetime
    sale_id: str


@dataclass(frozen=True)
class InventoryChange:
    at: datetime
    item_design_id: str
    change: int


@dataclass(frozen=True)
class CurrencyChange:
    at: datetime
    currency: str
    change: int


Observation = Union[ListingSeen, ListingGone, InventoryChange, CurrencyChange]


@dataclass(frozen=True)
class TradeReconciliation:
    side: str
    state: str
    reason: str
    item_design_id: str
    quantity: int
    currency: str
    price: int
    sale_id: Optional[str] = None
    proceeds: Optional[int] = None
    cost: Optional[int] = None
    fees: Optional[int] = None
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return self.state == COMPLETED


def _validate(request: Union[SellRequest, BuyRequest]) -> None:
    if request.at.tzinfo is None:
        raise TradeInputError("request time must be timezone-aware")
    if not request.item_design_id or not request.currency:
        raise TradeInputError("item_design_id and currency are required")
    if isinstance(request.quantity, bool) or not isinstance(request.quantity, int) or request.quantity < 1:
        raise TradeInputError("quantity must be a positive integer")
    if isinstance(request.price, bool) or not isinstance(request.price, int) or request.price < 1:
        raise TradeInputError("price must be a positive integer")
    if isinstance(request, BuyRequest) and not request.sale_id:
        raise TradeInputError("a purchase needs the sale id it targets")


def _split(request, observations: Iterable[Observation]) -> tuple[list[Observation], set[str]]:
    """Observations at or after the request, in time order, and sale ids already seen before it."""
    kept: list[Observation] = []
    earlier_sale_ids: set[str] = set()
    for observation in observations:
        if observation.at.tzinfo is None:
            raise TradeInputError("observation times must be timezone-aware")
        if observation.at >= request.at:
            kept.append(observation)
        elif isinstance(observation, ListingSeen) and observation.listing.sale_id is not None:
            earlier_sale_ids.add(observation.listing.sale_id)
    return sorted(kept, key=lambda o: o.at), earlier_sale_ids


def _matches(request: SellRequest, listing: MarketListing) -> bool:
    if not listing.has_identity:
        return False
    if listing.item_design_id is not None and listing.item_design_id != request.item_design_id:
        return False
    if listing.quantity is not None and listing.quantity != request.quantity:
        return False
    if listing.currency is not None and listing.currency != request.currency.lower():
        return False
    return listing.price is None or listing.price == request.price


def _result(request, side, state, reason, evidence, **extra) -> TradeReconciliation:
    return TradeReconciliation(
        side=side, state=state, reason=reason, item_design_id=request.item_design_id, quantity=request.quantity,
        currency=request.currency.lower(), price=request.price, evidence=tuple(evidence), **extra,
    )


def _reconcile_sell(request: SellRequest, observations: Sequence[Observation], earlier_sale_ids: set[str],
                    max_fee: int) -> TradeReconciliation:
    evidence: list[str] = ["listing requested"]
    matching = [o for o in observations if isinstance(o, ListingSeen) and _matches(request, o.listing)]
    fresh = [o for o in matching if o.listing.sale_id not in earlier_sale_ids]
    sale_ids = sorted({o.listing.sale_id for o in fresh})
    if len(sale_ids) > 1:
        return _result(request, SELL, REQUESTED, f"ambiguous listing: sale ids {', '.join(sale_ids)} all match",
                       evidence)
    if not fresh:
        unconfirmed = any(isinstance(o, ListingSeen) and not o.listing.has_identity for o in observations)
        if matching:
            reason = "only listings already open before the request match"
        elif unconfirmed:
            reason = "a listing without a sale id was seen; its identity is unconfirmed"
        else:
            reason = "no matching listing with a sale id was observed"
        return _result(request, SELL, REQUESTED, reason, evidence)

    seen = fresh[0]
    sale_id = seen.listing.sale_id
    evidence.append(f"listing {sale_id} seen")
    gone = next((o for o in observations if isinstance(o, ListingGone) and o.sale_id == sale_id and o.at >= seen.at),
                None)
    if gone is None:
        return _result(request, SELL, LISTED, f"listing {sale_id} is still open as far as observed", evidence,
                       sale_id=sale_id)
    evidence.append(f"listing {sale_id} gone")

    item_moves = [o.change for o in observations if isinstance(o, InventoryChange)
                  and o.item_design_id == request.item_design_id]
    left = -sum(change for change in item_moves if change < 0)
    returned = sum(change for change in item_moves if change > 0)
    # Proceeds can only follow the sale, and the sale is only known once the listing is gone.
    income = [o.change for o in observations if isinstance(o, CurrencyChange)
              and o.currency.lower() == request.currency.lower() and o.at >= gone.at and o.change > 0]

    if returned >= request.quantity and not income:
        evidence.append(f"item {request.item_design_id} returned x{returned}")
        return _result(request, SELL, EXPIRED, "the listing disappeared and the item came back", evidence,
                       sale_id=sale_id)
    if left < request.quantity or returned:
        return _result(request, SELL, UNKNOWN,
                       f"item movement not consistent with a sale (left {left}, returned {returned})", evidence,
                       sale_id=sale_id)
    evidence.append(f"item {request.item_design_id} left x{left}")
    if not income:
        return _result(request, SELL, UNKNOWN,
                       "the listing disappeared but no proceeds were observed after it did", evidence,
                       sale_id=sale_id)
    proceeds = sum(income)
    if not request.price - max_fee <= proceeds <= request.price:
        return _result(request, SELL, UNKNOWN,
                       f"{request.currency.lower()} rose by {proceeds}, outside the expected proceeds "
                       f"{request.price - max_fee}..{request.price}", evidence, sale_id=sale_id)
    evidence.append(f"{request.currency.lower()} +{proceeds}")
    return _result(request, SELL, COMPLETED, "listing sold: item left and proceeds arrived", evidence,
                   sale_id=sale_id, proceeds=proceeds, fees=request.price - proceeds)


def _reconcile_buy(request: BuyRequest, observations: Sequence[Observation], max_fee: int) -> TradeReconciliation:
    evidence: list[str] = ["purchase requested"]
    arrivals = [o for o in observations if isinstance(o, InventoryChange)
                and o.item_design_id == request.item_design_id and o.change > 0]
    payments = [o for o in observations if isinstance(o, CurrencyChange)
                and o.currency.lower() == request.currency.lower() and o.change < 0]
    arrived = sum(o.change for o in arrivals)
    paid = -sum(o.change for o in payments)
    if arrived:
        evidence.append(f"item {request.item_design_id} arrived x{arrived}")
    if paid:
        evidence.append(f"{request.currency.lower()} -{paid}")
    if any(isinstance(o, ListingGone) and o.sale_id == request.sale_id for o in observations):
        evidence.append(f"listing {request.sale_id} gone")
    if not arrived and not paid:
        return _result(request, BUY, REQUESTED, "neither the item nor the payment was observed", evidence,
                       sale_id=request.sale_id)
    if not arrived or not paid:
        missing = "item" if not arrived else "payment"
        return _result(request, BUY, UNKNOWN, f"the {missing} was not observed", evidence, sale_id=request.sale_id)
    if arrived != request.quantity or not request.price <= paid <= request.price + max_fee:
        return _result(request, BUY, UNKNOWN,
                       f"amounts do not match the purchase (item x{arrived} of {request.quantity}, paid {paid}, "
                       f"expected {request.price}..{request.price + max_fee})", evidence, sale_id=request.sale_id)
    last = max(o.at for o in arrivals + payments)
    if any(isinstance(o, ListingSeen) and o.listing.sale_id == request.sale_id and o.at > last
           for o in observations):
        return _result(request, BUY, UNKNOWN,
                       f"listing {request.sale_id} was still open after the item and payment were observed",
                       evidence, sale_id=request.sale_id)
    return _result(request, BUY, COMPLETED, "item arrived and payment left", evidence, sale_id=request.sale_id,
                   cost=paid, fees=paid - request.price)


def reconcile_trade(
    request: Union[SellRequest, BuyRequest],
    observations: Iterable[Observation] = (),
    *,
    max_fee: Optional[int] = None,
) -> TradeReconciliation:
    """Reconcile one trade. ``max_fee`` bounds fees; ``None`` means ``default_max_fee(request.price)``."""
    _validate(request)
    if max_fee is None:
        max_fee = default_max_fee(request.price)
    elif isinstance(max_fee, bool) or not isinstance(max_fee, int) or max_fee < 0:
        raise TradeInputError("max_fee must be a non-negative integer")
    kept, earlier_sale_ids = _split(request, observations)
    if isinstance(request, SellRequest):
        return _reconcile_sell(request, kept, earlier_sale_ids, max_fee)
    return _reconcile_buy(request, kept, max_fee)
