import hashlib
from .dotnet import DotNet


class UnsupportedNativeChecksum(RuntimeError):
    """Raised when an endpoint requires the unrecovered native IL2CPP checksum algorithm.

    The checksum pipeline (BuildKeyChecksum → FinaliseChecksumWithDesigns) has not
    been reverse-engineered. Live requests with placeholder checksums will fail.
    """
    pass


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


def ChecksumUserEmailPasswordAuthorize4(deviceKey, email, password, ts, languageKey, isWeb, accessToken):
    """Checksum for UserEmailPasswordAuthorize4 endpoint.

    REQUIRES the unrecovered native IL2CPP algorithm:
      BuildKeyChecksum → FinaliseChecksumWithDesigns

    This checksum has not been reproduced offline. Captured checksum
    8418e1e0a07c1ed794789df7d8edc6ea is a 32-char MD5 hex digest.
    All 74 permutation-based candidates tested — zero matches.
    """
    raise UnsupportedNativeChecksum(
        "UserEmailPasswordAuthorize4 requires the unrecovered "
        "BuildKeyChecksum/FinaliseChecksumWithDesigns algorithm. "
        "See scripts/checksum_lab.py for research context."
    )
