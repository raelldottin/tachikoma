import random
import os


class Device(object):

    name = "Android"
    key = ""
    refreshToken = None
    languageKey = "en"
    DB = "./.device"
    authentication_string = None
    accessToken = None  # pre-provisioned access token (bypass DeviceLogin17)
    userId = None  # user ID associated with the pre-provisioned token

    def __init__(
        self, name="Android", key=None, language="en", authentication_string=None
    ):

        if not key:
            key = "{}-{}-{}-{}-{}".format(
                "".join(random.choice("0123456789abcdef") for n in range(8)),
                "".join(random.choice("0123456789abcdef") for n in range(4)),
                "".join(random.choice("0123456789abcdef") for n in range(4)),
                "".join(random.choice("0123456789abcdef") for n in range(4)),
                "".join(random.choice("0123456789abcdef") for n in range(12)),
            )

        self.name = name
        self.key = key
        self.languageKey = language
        self.authentication_string = authentication_string
        # Always use .device file for persistence (even with auth string)
        self.DB = "./.device"

        # Compute deviceType from the provided name BEFORE load() can overwrite it
        self.deviceType = self._resolve_device_type(name)

        if not self.load():
            self.save()

    def refreshTokenAcquire(self, refreshToken):

        self.refreshToken = refreshToken
        self.save()

    def reset(self):
        if self.DB:
            os.unlink(self.DB)

    def save(self):
        if self.DB:
            with open(self.DB, "w+") as f:
                f.write(
                    "{}|{}|{}|{}|{}|{}".format(
                        self.name,
                        self.key,
                        self.refreshToken if self.refreshToken else "",
                        self.languageKey,
                        self.accessToken if self.accessToken else "",
                        self.userId if self.userId else "",
                    )
                )

    def load(self):
        if self.authentication_string:
            data = self.authentication_string.split("|")

        elif not os.path.isfile(self.DB):
            return False

        else:
            with open(self.DB, "r") as f:
                data = f.read().split("|")

        self.name = data[0]
        self.key = data[1]
        self.refreshToken = data[2] if len(data[2]) > 3 else None
        self.languageKey = data[3] if len(data) > 3 else "en"
        # Optional 5th field: pre-provisioned access token (UUID or JWT)
        if len(data) > 4 and len(data[4]) > 3:
            self.accessToken = data[4]
        else:
            self.accessToken = None
        # Optional 6th field: user ID associated with the pre-provisioned token
        if len(data) > 5 and data[5]:
            self.userId = data[5]
        else:
            self.userId = None

        # Map device name (first auth field) to correct deviceType enum
        # Official iOS client uses DeviceTypeIPhone (capital P)
        # Mac client uses DeviceTypeMac
        # Any other value is invalid and will cause "An error occurred."
        # Only override deviceType when we have an auth string (real device)
        if self.authentication_string:
            self.deviceType = self._resolve_device_type(self.name)

    def _resolve_device_type(self, name: str) -> str:
        """Map auth string name field to valid deviceType enum value."""
        # Case-insensitive matching for known platforms
        lower = name.lower()
        if lower == "iphone" or lower == "ios":
            return "DeviceTypeMac"  # Official iOS client uses DeviceTypeMac (value 2)
        elif lower == "mac" or lower == "macos":
            return "DeviceTypeMac"
        elif lower == "android":
            return "DeviceTypeAndroid"
        # Default to Mac (known working) rather than sending invalid value
        return "DeviceTypeMac"

        return True
