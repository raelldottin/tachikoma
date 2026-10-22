from __future__ import annotations

"""Inventory checksum helpers.

Pure MD5 functions reproducing the official-client native checksums for
inventory endpoints that have capture-verified formulas. These helpers are
PLUMBING ONLY: nothing in the runtime calls ItemService mutation endpoints
before an explicitly authorized Phase-9 slice (safety invariants S1/S2).

Everything here is a pure function of its arguments — no I/O, no requests,
no endpoint invocation.

Placement: sdk/inventory/checksums.py per gate verdict Q1 (NOT sdk/security.py,
which remains the home of generic auth/transport checksums).
"""

import hashlib

CHECKSUM_KEY = "5343"
SAVY_CHECKSUM = "Savvy!s0d@"


class UnsupportedNativeChecksum(RuntimeError):
    """Raised when a checksum cannot be computed without runtime-only config."""


def checksum_consume_item2(
    client_date_time: str,
    item_id: str,
    quantity: str,
    character_id: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the ItemService/ConsumeItem2 native checksum.

    URL: /ItemService/ConsumeItem2?itemId={0}&quantity={1}&characterId={2}
         &clientDateTime={3}&checksum={4}&accessToken={5}

    Formula (VERIFIED against 3/3 captured samples, 2026-08-16):
        preimage  = clientDateTime + itemId + quantity + characterId + ChecksumKey
        encrypted = preimage + SavyChecksum
        checksum  = MD5(encrypted)

    Note: clientDateTime comes FIRST (before itemId), unlike the URL ordering.
    accessToken is sent on the wire but is NOT part of the preimage.

    The endpoint stays DISABLED_INTERFACE: this helper exists for verification
    and later gated use; it invokes nothing.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "ConsumeItem2 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )
    preimage = client_date_time + item_id + quantity + character_id + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_activate_item4(
    client_date_time: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the ItemService/ActivateItem4 native checksum.

    URL: /ItemService/ActivateItem4?itemId=&quantity=&targetId=
         &clientDateTime=&checksum=&accessToken=

    Formula (VERIFIED against the single capture 1/1):
        preimage  = clientDateTime + ChecksumKey
        encrypted = preimage + SavyChecksum
        checksum  = MD5(encrypted)

    itemId/quantity/targetId appear on the wire but are NOT in the preimage.
    SINGLE-SAMPLE evidence: the gate stays at DISABLED_INTERFACE until a
    second independent capture exists (capture list item 6).
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "ActivateItem4 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )
    preimage = client_date_time + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_sell_item3(
    access_token: str,
    item_id: str,
    quantity: str,
    currency_type: str,
    currency_value: str,
    client_date_time: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the MarketService/SellItem3 native checksum.

    URL: /MarketService/SellItem3?itemId={0}&quantity={1}&currencyType={2}
         &currencyValue={3}&clientDateTime={4}&checksum={5}&accessToken={6}

    Formula (VERIFIED against 6/6 captured samples, 2026-10-05 capture):
        preimage  = accessToken + itemId + quantity + currencyType
                    + currencyValue + clientDateTime + ChecksumKey
        encrypted = preimage + SavyChecksum
        checksum  = MD5(encrypted)

    Note: accessToken comes FIRST in the preimage (unlike most ItemService
    checksums) and the parameter order follows the URL order minus checksum.

    Plumbing only: MarketService mutation stays unreachable (safety
    invariants S1/S2). This helper invokes nothing.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "SellItem3 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )
    preimage = (
        access_token
        + item_id
        + quantity
        + currency_type
        + currency_value
        + client_date_time
        + checksum_key
    )
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_purchase4(
    access_token: str,
    sale_id: str,
    client_date_time: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the MarketService/Purchase4 native checksum.

    URL: /MarketService/Purchase4?saleId={0}&clientDateTime={1}&checksum={2}
         &accessToken={3}&allowConversion={4}

    Formula (VERIFIED against the single capture 1/1, 2026-10-05):
        preimage  = accessToken + saleId + clientDateTime + ChecksumKey
        encrypted = preimage + SavyChecksum
        checksum  = MD5(encrypted)

    allowConversion is sent on the wire but is NOT part of the preimage.
    SINGLE-SAMPLE evidence (1/1): the formula is only wire-derived; the
    endpoint stays unreachable and this helper invokes nothing.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "Purchase4 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )
    preimage = access_token + sale_id + client_date_time + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_mark_items_not_new(
    client_date_time: str,
    access_token: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the ItemService/MarkItemsNotNew native checksum.

    URL: /ItemService/MarkItemsNotNew?checksum={0}&clientDateTime={1}&accessToken={2}

    Formula (VERIFIED against 6/6 captured samples, 2026-10-05 capture;
    first captured 1/1 on 2026-08-16, formula unchanged):
        preimage  = clientDateTime + accessToken + ChecksumKey
        encrypted = preimage + SavyChecksum
        checksum  = MD5(encrypted)

    Harmless UI-state call; remains DISABLED_INTERFACE (low priority, not
    wired into any flow in this slice).
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "MarkItemsNotNew requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )
    preimage = client_date_time + access_token + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()
