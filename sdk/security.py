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


def checksum_collect_marker2(
    marker_id: str,
    client_date_time: str,
    access_token: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the CollectMarker2 checksum.

    Formula verified by analogy with UpdateMarkerMovement (same Galaxy marker
    endpoint family, identical URL parameter shape):
        MD5(starSystemMarkerId + clientDateTime + accessToken + ChecksumKey + SavyChecksum)

    Note: Static analysis originally hypothesized designVersion was in the
    preimage, but the verified UpdateMarkerMovement formula (6 captures) proves
    the Galaxy marker family does NOT include designVersion in the checksum.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "CollectMarker2 requires checksum_key and savy_checksum configuration."
        )
    preimage = marker_id + client_date_time + access_token + checksum_key
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


# =============================================================================
# CHECKSUM CONSTANTS (extracted from IL2CPP binary / capture verification)
# =============================================================================
CHECKSUM_KEY = "5343"
SAVY_CHECKSUM = "Savvy!s0d@"


def checksum_create_battle9(
    client_hp: int,
    client_date_time: str,
    access_token: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the CreateBattle9 native checksum.

    URL: /BattleService/CreateBattle9?clientHp={0}&clientDateTime={1}&checksum={2}&accessToken={3}&itemDesignId={4}
    Parameters before checksum in URL: clientHp, clientDateTime, accessToken

    Formula (VERIFIED against capture: 8118b3ffc06d9e8b520c1b6956e7ca9a):
    preimage  = clientDateTime + checksumKey
    encrypted = preimage + savyChecksum
    checksum  = MD5(encrypted)

    Args:
        client_hp: Ship HP × 100 (e.g., 4000 for HP=40)
        client_date_time: Current UTC timestamp "yyyy-MM-ddTHH:mm:ss"
        access_token: Current session access token (NOT used in checksum)
        checksum_key: Configuration.ChecksumKey ("5343")
        savy_checksum: Configuration.SavyChecksum ("Savvy!s0d@")

    Returns:
        32-char MD5 hex digest.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "CreateBattle9 requires checksum_key and savy_checksum"
        )

    preimage = client_date_time + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_accept_battle5(
    battle_id: str,
    item_design_id: int,
    client_date_time: str,
    access_token: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the AcceptBattle5 native checksum.

    URL: /BattleService/AcceptBattle5?battleId={0}&itemDesignId={1}&clientDateTime={2}&checksum={3}&accessToken={4}

    Verified formula (matched against 3 mitmproxy captures, 2026-08-08 to 2026-08-10):
        preimage  = accessToken + battleId + clientDateTime
        encrypted = preimage + savyChecksum
        checksum  = MD5(encrypted)

    Note: itemDesignId is NOT included in the preimage, and ChecksumKey is NOT used.
    Only SavyChecksum ("Savvy!s0d@") is appended as the salt.

    Args:
        battle_id: Battle ID from CreateBattle9 response
        item_design_id: Item design ID (usually 0 for standard battles) — NOT used in checksum
        client_date_time: Current UTC timestamp "yyyy-MM-ddTHH:mm:ss"
        access_token: Current session access token
        checksum_key: Configuration.ChecksumKey ("5343") — NOT used (kept for API compat)
        savy_checksum: Configuration.SavyChecksum ("Savvy!s0d@")

    Returns:
        32-char MD5 hex digest.
    """
    if not savy_checksum:
        raise UnsupportedNativeChecksum(
            "AcceptBattle5 requires savy_checksum"
        )

    preimage = access_token + str(battle_id) + client_date_time
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


def checksum_get_catalog_quantity(
    client_date_time: str,
    access_token: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the LibeOpsService/GetCatalogQuantity native checksum.

    URL: /LibeOpsService/GetCatalogQuantity?clientDateTime={0}&checksum={1}&accessToken={1}
    Parameters before checksum in URL: clientDateTime, accessToken

    Formula:
        preimage  = clientDateTime + accessToken + checksumKey
        encrypted = preimage + savyChecksum
        checksum  = MD5(encrypted)

    Args:
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
            "GetCatalogQuantity requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )

    # URL parameter order before checksum: clientDateTime, accessToken
    preimage = client_date_time + access_token + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_create_star_battle5(
    client_hp: str,
    client_date_time: str,
    access_token: str,
    search_number: str,
    value: str,
    device_key: str,
    email: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the CreateStarBattle5 native checksum.

    URL: /BattleService/CreateStarBattle5?clientHp={0}&clientDateTime={1}&checksum={2}&accessToken={3}&searchNumber={4}&value={5}
    Parameters before checksum in URL: clientHp, clientDateTime, accessToken, searchNumber, value

    Formula (original implementation with deviceKey and email):
    preimage  = clientHp + clientDateTime + accessToken + searchNumber + value + deviceKey + email + checksumKey
    encrypted = preimage + savyChecksum
    checksum  = MD5(encrypted)

    Args:
        client_hp: Client HP as string (e.g., "100000")
        client_date_time: Current UTC timestamp "yyyy-MM-ddTHH:mm:ss"
        access_token: Current session access token
        search_number: Search number as string (e.g., "0")
        value: Value as string (e.g., "0")
        device_key: Device UUID
        email: User email
        checksum_key: Configuration.ChecksumKey ("5343")
        savy_checksum: Configuration.SavyChecksum ("Savvy!s0d@")

    Returns:
        32-char MD5 hex digest.

    Raises:
        UnsupportedNativeChecksum: If checksum_key or savy_checksum is empty/None.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "CreateStarBattle5 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )

    # Original implementation includes deviceKey and email
    preimage = client_hp + client_date_time + access_token + search_number + value + device_key + email + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_update_marker_movement(
    marker_id: str,
    client_date_time: str,
    access_token: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the UpdateMarkerMovement native checksum.

    URL: /GalaxyService/UpdateMarkerMovement?starSystemMarkerId={0}&checksum={1}&clientDateTime={2}&accessToken={3}

    Verified formula (matched against 6 mitmproxy captures, 2026-08-08 to 2026-08-10):
        preimage  = markerId + clientDateTime + accessToken + checksumKey
        encrypted = preimage + savyChecksum
        checksum  = MD5(encrypted)

    Args:
        marker_id: Star system marker ID to update
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
            "UpdateMarkerMovement requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )

    # URL parameter order before checksum: starSystemMarkerId, clientDateTime, accessToken
    preimage = marker_id + client_date_time + access_token + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_rebuild_ammo3(
    ammo_category: str,
    client_date_time: str,
    access_token: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Compute the RebuildAmmo3 native checksum.

    URL: /RoomService/RebuildAmmo3?ammoCategory={0}&clientDateTime={1}&checksum={2}&accessToken={3}

    Verified formula (matched against mitmproxy capture, 2026-08-10):
        preimage  = ammoCategory + clientDateTime + accessToken + checksumKey
        encrypted = preimage + savyChecksum
        checksum  = MD5(encrypted)

    Args:
        ammo_category: Ammo category (e.g., "None" for all ammo)
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
            "RebuildAmmo3 requires checksum_key and savy_checksum "
            "configuration values compatible with the installed game version."
        )

    # URL parameter order before checksum: ammoCategory, clientDateTime, accessToken
    preimage = ammo_category + client_date_time + access_token + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_heartbeat4(
    ticks: int,
    access_token: str,
) -> str:
    """Compute the HeartBeat4 checksum.

    Verified formula (matched against 267 mitmproxy captures, 2026-08-08 to 2026-08-10):
        checksum = str(ChecksumTimeForDate(ticks) + ChecksumPasswordWithString(access_token))

    This is a NUMERIC SUM (not string concatenation). The original code used
    string concatenation which produced different but still server-accepted values.

    Args:
        ticks: .NET ticks (100ns intervals since 0001-01-01) from DotNet.get_time()
        access_token: Current session access token

    Returns:
        Checksum string (numeric sum as string).
    """
    time_part = ChecksumTimeForDate(ticks)
    pwd_part = ChecksumPasswordWithString(access_token)
    return str(time_part + pwd_part)