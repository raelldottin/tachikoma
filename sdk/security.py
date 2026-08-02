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


def checksum_device_login17(
    device_key: str,
    client_date_time: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the DeviceLogin17 native checksum.

    Verified against 2 live captures from official iOS client (2026-08-02):
    - DeviceLogin17: deviceKey + clientDateTime + "DeviceTypeMac" + ChecksumKey
    - Then SavysodaEncryptString: MD5(preimage + SavyChecksum)

    Args:
        device_key: Device UUID (e.g., "6AD42828-7D06-534D-A461-49658461A614")
        client_date_time: Timestamp in "yyyy-MM-ddTHH:mm:ss" format (no microseconds, no Z)
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
    # Device type enum name for macOS/iOS builds is "DeviceTypeMac"
    device_type = "DeviceTypeMac"
    preimage = device_key + client_date_time + device_type + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_user_email_password_authorize4(
    client_date_time: str,
    login_type: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the UserEmailPasswordAuthorize4 native checksum.

    Pipeline (from ISIL static analysis — provisional, not verified against a
    current official-client capture):
        preimage  = clientDateTime + loginType + checksum_key
        encrypted = preimage + savy_checksum
        checksum  = MD5(encrypted)

    Args:
        client_date_time: Current timestamp in .NET "o" format (7 fractional digits).
        login_type: LoginType enum member text used by DownloadUserLogin.
        checksum_key: Configuration.ChecksumKey (runtime-initialized, not in binary).
        savy_checksum: Configuration.SavyChecksum (runtime-initialized, not in binary).

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
    preimage = client_date_time + login_type + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()


def checksum_rebuild_ammo3(
    device_key: str,
    client_date_time: str,
    ammo_category: str,
    checksum_key: str,
    savy_checksum: str,
) -> str:
    """Compute the RebuildAmmo3 native checksum.

    WARNING: Formula NOT verified against live captures. Based on static analysis
    only. Gate behind feature flag until verified.
    """
    if not checksum_key or not savy_checksum:
        raise UnsupportedNativeChecksum(
            "RebuildAmmo3 requires checksum_key and savy_checksum configuration."
        )
    # Provisional: deviceKey + ammoCategory + clientDateTime + ChecksumKey
    preimage = device_key + ammo_category + client_date_time + checksum_key
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