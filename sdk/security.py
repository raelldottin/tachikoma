from __future__ import annotations

import hashlib

def first_stub(dt):
    return int((dt & 0x3FFFFFFFFFFFFFFF) // 0x989680) % 60

def second_stub(dt):
    return int((dt & 0x3FFFFFFFFFFFFFFF) // 0x23C34600) % 60


def ChecksumTimeForDate(dt):
    return first_stub(dt) * second_stub(dt)

def ChecksumCreateDevice(device_key: str, device_type: str) -> str:
    result = hashlib.md5((device_key + 'DeviceType'+ device_type +'savysoda').encode('utf-8')).hexdigest()
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


def checksum_user_email_password_authorize4(
    clientDateTime: str,
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
        clientDateTime: Current timestamp in .NET "o" format (7 fractional digits).
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
    preimage = clientDateTime + login_type + checksum_key
    encrypted = preimage + savy_checksum
    return hashlib.md5(encrypted.encode("utf-8")).hexdigest()
