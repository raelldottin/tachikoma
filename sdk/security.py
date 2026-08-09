from __future__ import annotations

import hashlib


def first_stub(dt):
    return int((dt & 0x3FFFFFFFFFFFFFFF) // 0x989680) % 60


def second_stub(dt):
    return int((dt & 0x3FFFFFFFFFFFFFFF) // 0x23C34600) % 60


def ChecksumTimeForDate(dt):
    return first_stub(dt) * second_stub(dt)


def ChecksumCreateDevice(device_key: str, device_type: str) -> str:
    result = hashlib.md5((device_key + 'DeviceType' + device_type + 'savysoda').encode('utf-8')).hexdigest()
    return result


def ChecksumPasswordWithString(accessToken):
    return int(accessToken[0].encode('utf-8').hex(), 16) + int(accessToken[1].encode('utf-8').hex(), 16) + int(accessToken[3].encode('utf-8').hex(), 16)


def ChecksumEmailAuthorize(deviceKey, email, ts, accessToken, salt):
    return hashlib.md5((deviceKey + email + ts + accessToken + salt + 'savysoda').encode('utf-8')).hexdigest()


class UnsupportedNativeChecksum(RuntimeError):
    """Raised when a native IL2CPP checksum cannot be computed without runtime-only constants.

    Several PSS endpoints (UserEmailPasswordAuthorize4, RebuildAmmo3, CollectMarker2)
    require Configuration.ChecksumKey and Configuration.SavyChecksum, which are
    runtime-initialized and not present in the game binary. These values must be
    supplied via configuration; without them the checksum cannot be reproduced.
    """
    pass


def checksum_device_type_name() -> str:
    """Device type enum name used in the checksum preimage for iOS builds.

    Verified endpoint-specific behavior (2026-08-03, 7/7 captures):
        iOS (DeviceType=0): checksum uses "DeviceTypeIPhone"
        macOS (DeviceType=2): checksum uses "DeviceTypeMac"
    The app always identifies as iOS (DeviceType=0), so the checksum uses
    "DeviceTypeIPhone".  This is intentional protocol behavior, not an
    inconsistency -- do not "clean up" to DeviceTypeMac.
    """
    return "DeviceTypeIPhone"


def request_device_type_name() -> str:
    """Device type enum name used in URL query parameters.

    All requests use DeviceType=0 (DeviceTypeIPhone) in URL parameters and
    POST body DeviceType field, regardless of the platform running the code.
    """
    return "DeviceTypeIPhone"


def checksum_device_login17(
    device_key: str,
    client_date_time: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the DeviceLogin17 native checksum.

    Verified against 7 live captures from official iOS + macOS clients
    (2026-08-03):
    - iOS  (DeviceType=0): DeviceTypeIPhone in checksum preimage
    - macOS(DeviceType=2): DeviceTypeMac    in checksum preimage

    The client_date_time passed in MUST be stripped to second precision
    (format "yyyy-MM-ddTHH:mm:ss" -- no microseconds, no Z suffix) because
    the official client strips the fractional seconds before hashing even
    though the full-precision timestamp is sent in the request body.

    Formula:
        preimage  = deviceKey + strippedClientDateTime + DeviceTypeName + ChecksumKey
        encrypted = preimage + SavyChecksum
        checksum  = MD5(encrypted)

    Args:
        device_key: Device UUID (e.g., "CC3C7642-E6FE-4737-88C1-130395760B52")
        client_date_time: Timestamp in "yyyy-MM-ddTHH:mm:ss" format
        checksum_key: Configuration.ChecksumKey = "5343" (runtime-initialized)
        savy_checksum: Configuration.SavyChecksum = "Savvy!s0d@" (runtime-initialized)

    Returns:
        32-char MD5 hex digest.

    Raises:
        UnsupportedNativeChecksum: If checksum_key or savy_checksum is empty/None.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "DeviceLogin17 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )
    # App always identifies as iOS: checksum uses DeviceTypeIPhone
    device_type = checksum_device_type_name()
    preimage = device_key + client_date_time + device_type + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_user_email_password_authorize4(
    device_key: str,
    email: str,
    client_date_time: str,
    access_token: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the UserEmailPasswordAuthorize4 native checksum.

    Live-verified (2026-08-02) against official iOS client captures and e2e three-stage flow:
        Stage 1: DeviceLogin17 → accessToken
        Stage 2: UserEmailPasswordAuthorize4 → new refreshToken (verified 5/5 captures + fresh e2e)
        Stage 3: DeviceLogin17 with new refreshToken → authenticated session

    Formula:
        preimage  = deviceKey + email + clientDateTime + accessToken + checksumKey
        encrypted = preimage + savy_checksum
        checksum  = MD5(encrypted)

    Note: The password is sent in the URL query string but is NOT part of
    the checksum preimage.

    Args:
        device_key: Device UUID (from DeviceLogin17 stage-1 response).
        email: User email address.
        client_date_time: Current UTC timestamp "yyyy-MM-ddTHH:mm:ss".
        access_token: Access token from the stage-1 DeviceLogin17 response.
        checksum_key: Configuration.ChecksumKey ("5343").
        savy_checksum: Configuration.SavyChecksum ("Savvy!s0d@").

    Returns:
        32-char MD5 hex digest.

    Raises:
        UnsupportedNativeChecksum: If checksum_key or savy_checksum is empty/None.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "UserEmailPasswordAuthorize4 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )
    preimage = device_key + email + client_date_time + access_token + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_rebuild_ammo3(
    device_key: str,
    client_date_time: str,
    ammo_category: str,
    email: str,
    access_token: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the RebuildAmmo3 native checksum.

    Formula matches URL parameter order (parameters before checksum in URL):
    URL: /RoomService/RebuildAmmo3?ammoCategory={0}&clientDateTime={1}&checksum={2}&accessToken={3}
    Parameters before checksum: ammoCategory, clientDateTime, accessToken
    
    preimage  = ammoCategory + clientDateTime + accessToken + checksumKey
    encrypted = preimage + savyChecksum
    checksum  = MD5(encrypted)
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "RebuildAmmo3 requires checksum_key and savy_checksum configuration."
        )
    # URL parameter order before checksum: ammoCategory, clientDateTime, accessToken
    preimage = ammo_category + client_date_time + access_token + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_collect_marker2(
    marker_id: str,
    client_date_time: str,
    design_version: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the CollectMarker2 native checksum.

    WARNING: Formula NOT verified against live captures. Based on static analysis
    only. Gate behind feature flag until verified.

    Note: design_version comes from Configuration.GetLatestVersion4() at runtime,
    which returns a server-synced design data version string. The exact format
    is not yet determined.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "CollectMarker2 requires checksum_key and savy_checksum configuration."
        )
    # Provisional: markerId + clientDateTime + designVersion + ChecksumKey
    preimage = marker_id + client_date_time + design_version + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_finalise_battle15(
    battle_id: str,
    client_outcome_type: int,
    client_end_frame: int,
    client_result_string: str,
    attacking_ship_hp: int,
    client_version: str,
    access_token: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the FinaliseBattle15 native checksum.

    Derived from static analysis of IL2CPP metadata (v31):
    - URL template: /BattleService/{0}?battleId={1}&clientOutcomeType={2}&clientEndFrame={3}&clientResultString={4}&attackingShipHp={5}&checksum={6}&clientVersion={7}&accessToken={8}
    - Where {0} = "FinaliseBattle15"
    - Parameters ordered as they appear in URL (excluding checksum output param)

    Formula:
        preimage  = battleId + clientOutcomeType + clientEndFrame + clientResultString + attackingShipHp + clientVersion + accessToken + checksumKey
        encrypted = preimage + savyChecksum
        checksum  = MD5(encrypted)

    Args:
        battle_id: Battle ID from CreateStarBattle5 response
        client_outcome_type: Outcome type (1=Victory, 2=Defeat, 3=Draw)
        client_end_frame: Final battle frame number
        client_result_string: Battle replay/result data string
        attacking_ship_hp: Remaining HP of attacking ship
        client_version: Client version string (e.g., "0.999.59")
        access_token: Current session access token
        checksum_key: Configuration.ChecksumKey ("5343")
        savy_checksum: Configuration.SavyChecksum ("Savvy!s0d@")

    Returns:
        32-char MD5 hex digest.

    Raises:
        UnsupportedNativeChecksum: If checksum_key or savy_checksum is empty/None.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "FinaliseBattle15 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )

    preimage = (
        battle_id
        + str(client_outcome_type)
        + str(client_end_frame)
        + client_result_string
        + str(attacking_ship_hp)
        + client_version
        + access_token
        + checksum_key
    )
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_character_draw(
    draw_design_id: str,
    client_date_time: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the CharacterService/Draw native checksum for purchasing pods/draws with Starbux.

    Derived from static analysis of IL2CPP metadata (v31):
    - URL: CharacterService/Draw?drawDesignId={0}&clientDateTime={1}&checksum={2}&accessToken={3}
    - Parameters in URL order: drawDesignId, clientDateTime, checksum (output), accessToken
    - The checksum is computed BEFORE the accessToken is added to URL

    Formula:
        preimage  = drawDesignId + clientDateTime + checksumKey
        encrypted = preimage + savyChecksum
        checksum  = MD5(encrypted)

    Args:
        draw_design_id: The draw design ID (e.g., "123" for Scorched Pod)
        client_date_time: Current UTC timestamp "yyyy-MM-ddTHH:mm:ss"
        checksum_key: Configuration.ChecksumKey ("5343")
        savy_checksum: Configuration.SavyChecksum ("Savvy!s0d@")

    Returns:
        32-char MD5 hex digest.

    Raises:
        UnsupportedNativeChecksum: If checksum_key or savy_checksum is empty/None.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "CharacterService/Draw requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )

    preimage = draw_design_id + client_date_time + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_purchase_catalog2(
    argument: str,
    client_date_time: str,
    access_token: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the ShopService/PurchaseCatalog2 native checksum for purchasing items from shop.

    URL: /ShopService/PurchaseCatalog2?argument={0}&clientDateTime={1}&checksum={2}&accessToken={3}
    Parameters before checksum in URL: argument, clientDateTime, accessToken

    Formula:
        preimage  = argument + clientDateTime + accessToken + checksumKey
        encrypted = preimage + savyChecksum
        checksum  = MD5(encrypted)

    Args:
        argument: The catalog item argument (e.g., "1291" for Scorched Pod)
        client_date_time: Current UTC timestamp "yyyy-MM-ddTHH:mm:ss"
        access_token: Current session access token
        checksum_key: Configuration.ChecksumKey ("5343")
        savy_checksum: Configuration.SavyChecksum ("Savvy!s0d@")

    Returns:
        32-char MD5 hex digest.

    Raises:
        UnsupportedNativeChecksum: If checksum_key or savy_checksum is empty/None.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "PurchaseCatalog2 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )

    # URL parameter order before checksum: argument, clientDateTime, accessToken
    preimage = argument + client_date_time + access_token + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()