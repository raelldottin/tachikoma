import hashlib
from .dotnet import DotNet

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
    """
    Checksum for UserEmailPasswordAuthorize4 endpoint.
    Based on captured traffic: checksum = ChecksumTimeForDate(DotNet.get_time())
    This is an integer (not MD5), matching the ChecksumTimeForDate pattern.
    """
    dt = DotNet.get_time()
    return ChecksumTimeForDate(dt)
