from __future__ import annotations

import urllib.parse
import re
import time
import datetime
import collections
import xmltodict
import requests
import random
import logging
import math
import hashlib
from itertools import accumulate
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from ratelimit import limits, sleep_and_retry
from sdk.device import Device
from .security import (
    ChecksumTimeForDate,
    ChecksumPasswordWithString,
    ChecksumEmailAuthorize,
    checksum_user_email_password_authorize4,
    checksum_device_login17,
)
from .dotnet import DotNet
from .redaction import redact_secrets, safe_log_message  # noqa: F401


def lowercase_urlencode(params: dict) -> str:
    """URL-encode with lowercase hex digits to match official iOS client.
    
    urllib.parse.urlencode uses uppercase (%3A) but the official client
    sends lowercase (%3a). Server requires exact byte-for-byte match.
    """
    parts = []
    for k, v in params.items():
        k_enc = urllib.parse.quote(k, safe='')
        v_enc = urllib.parse.quote(v, safe='')
        # Force lowercase hex digits
        k_enc = re.sub(r'%([0-9A-F]{2})', lambda m: '%' + m.group(1).lower(), k_enc)
        v_enc = re.sub(r'%([0-9A-F]{2})', lambda m: '%' + m.group(1).lower(), v_enc)
        parts.append(f'{k_enc}={v_enc}')
    return '&'.join(parts)


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""
    pass


def _extract_collection(data, item_key: str) -> list[dict]:
    """Extract and normalize an item collection from raw parsed dict into a list of dicts.

    Supports:
    - Top-level key matching (data[item_key])
    - Nested dict matching (e.g. data["RoomService"]["ListRoomDesigns"]["RoomDesigns"][item_key])
    - Dict to 1-element list conversion
    - Missing/None/invalid data returning []
    """
    if not isinstance(data, dict):
        return []
    if item_key in data:
        val = data[item_key]
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
        if isinstance(val, dict):
            return [val]
        return []
    for val in data.values():
        if isinstance(val, dict):
            res = _extract_collection(val, item_key)
            if res:
                return res
    return []



DEFAULT_TIMEOUT = 5  # seconds
ONE_MINUTE = 60
MAX_CALLS_PER_MINUTE = 30


@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=ONE_MINUTE)
class TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        self.timeout = DEFAULT_TIMEOUT
        if "timeout" in kwargs:
            self.timeout = kwargs["timeout"]
            del kwargs["timeout"]
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


class User(object):
    id = 0
    name = None
    isAuthorized = False
    clientDateTime = 0
    lastHeartBeat = datetime.datetime.utcnow()

    def __init__(self, id, name, lastHeartBeat, isAuthorized):
        self.id = id
        self.name = name
        self.lastHeartBeat = lastHeartBeat
        self.isAuthorized = True if isAuthorized else False


class Client(object):
    # device data
    device = Device

    # configuration
    salt = "5343"
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "deflate, gzip",
        "User-Agent": "UnityPlayer/6000.0.77f1 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
        "X-Unity-Version": "6000.0.77f1",
    }
    # Use the actual base url and implement handling for different services
    baseUrl = "https://api.pixelstarships.com"

    # runtime data
    accessToken = None
    checksum = None
    freeStarbuxToday = 0
    freeStarbuxMax = 10
    freeStarbuxTodayTimestamp = 0
    dailyReward = 0
    dailyRewardTimestamp = 0
    rssCollected = 0
    rssCollectedTimestamp = 0
    mineralTotal = 0
    gasTotal = 0
    mineralIncrease = 0
    gasIncrease = 0
    dronesCollected = {}
    dailyRewardArgument = 0
    credits = 0
    max_room_upgrades = False
    info = {"@Name": ""}
    user: User

    # tcp session, backoff timer, and rate limiter
    retry_strategy = Retry(
        total=10,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504, 520],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    def __init__(self, device, settings=None):
        self.device = device
        self.settings = settings or {}

    @sleep_and_retry
    @limits(calls=MAX_CALLS_PER_MINUTE, period=ONE_MINUTE)
    def request(self, url, method, data=None):
        r = self.session.request(method, url, headers=self.headers, data=data)

        if "errorMessage" in r.text:
            # "storage is full" is a benign condition, log as warning not error
            if "storage is full" in r.text.lower():
                logging.warning("[%s] {%s} - storage is full", self.info["@Name"], redact_secrets(url))
            elif "Please upgrade your lab room." not in r.text:
                d = xmltodict.parse(r.content, xml_attribs=True)
                logging.error("[%s] {%s} - {%s}", self.info["@Name"], redact_secrets(url), redact_secrets(str(d)))

        if "Failed to authorize access token" in r.text:
            logging.info(
                "[%s] Attempting to reauthorized access token.", self.info["@Name"]
            )
            self.user.isAuthorized = False
            self.quickReload()
            r = self.session.request(method, url, headers=self.headers, data=data)

        return r

    def parseUserLoginData(self, r):
        if not r or not r.content:
            logging.error("Failed to login.")
            return False

        try:
            d = xmltodict.parse(r.content, xml_attribs=True)
        except Exception:
            logging.error("Failed to login.")
            return False

        if not isinstance(d, dict):
            logging.error("Failed to login.")
            return False

        user_login = d.get("UserLogin")
        if user_login is None and "UserService" in d and isinstance(d["UserService"], dict):
            user_login = d["UserService"].get("UserLogin")

        if not user_login or not isinstance(user_login, dict):
            logging.error("Failed to login.")
            return False

        user_dict = user_login.get("User")
        if not user_dict or not isinstance(user_dict, dict):
            logging.error("Failed to login.")
            return False

        last_hb_str = user_dict.get("@LastHeartBeatDate")
        if last_hb_str:
            try:
                LastHeartBeat = datetime.datetime.strptime(
                    last_hb_str.split(".")[0],
                    "%Y-%m-%dT%H:%M:%S",
                )
            except ValueError:
                LastHeartBeat = datetime.datetime.now()
        else:
            LastHeartBeat = datetime.datetime.now()

        self.info = user_dict
        if "@Name" not in self.info:
            self.info["@Name"] = ""
        logging.info("[%s] Authenticated...", self.info["@Name"])

        userId = user_login.get("@UserId") or user_dict.get("@Id") or "0"

        if "@Credits" in user_dict:
            try:
                self.credits = int(user_dict["@Credits"])
            except (ValueError, TypeError):
                self.credits = 0

        if "@DailyRewardStatus" in user_dict:
            try:
                self.dailyReward = int(user_dict["@DailyRewardStatus"])
            except (ValueError, TypeError):
                self.dailyReward = 0
        else:
            self.dailyReward = 0

        if not self.device.refreshToken:
            myName = "guest"
        else:
            myName = user_dict.get("@Name", "")

        if "@FreeStarbuxReceivedToday" in user_dict:
            try:
                self.freeStarbuxToday = int(user_dict["@FreeStarbuxReceivedToday"])
            except (ValueError, TypeError):
                pass
        elif "FreeStarbuxReceivedToday" in r.text:
            try:
                self.freeStarbuxToday = int(
                    r.text.split('FreeStarbuxReceivedToday="')[1].split('"')[0]
                )
            except Exception:
                pass

        self.user = User(
            userId,
            myName,
            LastHeartBeat,
            self.device.refreshToken,
        )

        return True

    def getAccessToken(self):
        """Backward-compatible accessor — delegates to create_device_session()."""
        return self.create_device_session()

    def _build_device_login_payload(self):
        """Build the DeviceLogin17 JSON payload for the current device state.
        
        Format matches official iOS client (Unity 6000.0.77f1, build 18054).

        Protocol detail (verified 2026-08-03, 7/7 captures):
        - The full-precision timestamp (with microseconds + Z) is sent in the
          request body ClientDateTime field.
        - The checksum preimage uses the timestamp STRIPPED to seconds
          (no microseconds, no Z suffix).
        """
        checksum_key = self.settings.get("checksum_key") or "5343"
        savy_checksum = self.settings.get("savy_checksum") or "Savvy!s0d@"
        
        # Full precision timestamp for the request body
        client_dt = "{0:%Y-%m-%dT%H:%M:%S.%f}Z".format(DotNet.validDateTime())
        # Stripped to seconds for the checksum preimage
        client_dt_checksum = client_dt.split(".")[0]
        
        self.checksum = checksum_device_login17(
            device_key=self.device.key,
            client_date_time=client_dt_checksum,
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        return {
            "DeviceKey": self.device.key,
            "AdvertisingKey": "00000000-0000-0000-0000-000000000000",
            "ClientDateTime": client_dt,
            "IsJailBroken": False,
            "Checksum": self.checksum,
            "DeviceType": 0,  # iOS = 0, Mac = 2 (official iOS client uses 0)
            "Signal": False,
            "LanguageKey": self.device.languageKey or "en",
            "RefreshToken": self.device.refreshToken if self.device.refreshToken else "",
            "UserDeviceInfo": {
                "OsVersion": "iOS 26.5.2",
                "Locale": "en",
                "DeviceName": "iPhone16,2",
                "OSBuild": "0",
                "ClientBuild": "18054",
                "ClientVersion": "0.999.59",
            },
            "AccessToken": "00000000-0000-0000-0000-000000000000",
        }

    @staticmethod
    def _extract_access_token(response):
        """Extract the accessToken attribute value from a DeviceLogin17 response."""
        if (
            (not response or response.status_code != 200)
            or ("accessToken" not in response.text)
        ):
            return None
        return response.text.split('accessToken="')[1].split('"')[0]

    def create_device_session(self) -> bool:
        """Stage 1: Call DeviceLogin17 without a refresh token.

        Establishes an unauthenticated device session and returns an access token.
        If the device already has a refresh token, it is included in the payload
        so DeviceLogin17 creates an authenticated session directly.

        Returns:
            True if the server returned an access token and user data was parsed.
        """
        url = f"{self.baseUrl}/UserService/DeviceLogin17"
        json = self._build_device_login_payload()

        r = self.session.post(url, json=json)
        if r:
            d = xmltodict.parse(r.content, xml_attribs=True)
            token = self._extract_access_token(r)
            if token is None:
                logging.error("{%s}", redact_secrets(str(d)))
                self.accessToken = ""
                return False
            self.accessToken = token

        if not self.parseUserLoginData(r):
            return False

        return True

    def authorize_email_password(self, email: str, password: str) -> bool:
        """Stage 2: Call UserEmailPasswordAuthorize4 to submit email and password.

        Sends the email/password along with the native checksum. On success, the
        server returns a refreshToken which is persisted to the device.

        Requires checksum_key and savy_checksum configuration settings because the
        native IL2CPP checksum depends on runtime-only Configuration values.

        Raises:
            UnsupportedNativeChecksum: If checksum_key or savy_checksum missing.
            ValueError: If called without an existing access token.

        Returns:
            True if the server returned a refreshToken.
        """
        if not self.accessToken:
            raise ValueError("authorize_email_password requires an existing access token")

        checksum_key = self.settings.get("checksum_key") or "5343"
        savy_checksum = self.settings.get("savy_checksum") or "Savvy!s0d@"

        ts = self._client_datetime_utc()
        self.checksum = checksum_user_email_password_authorize4(
            self.device.key,
            email,
            ts,
            self.accessToken,
            checksum_key,
            savy_checksum,
        )

        post_data = lowercase_urlencode({
            "clientDateTime": ts,
            "checksum": self.checksum,
            "deviceKey": self.device.key,
            "email": email,
            "password": password,
            "languageKey": self.device.languageKey or "en",
            "isWeb": "False",
            "accessToken": self.accessToken,
        })
        # Official client sends ALL params in URL query string AND POST body
        url = f"{self.baseUrl}/UserService/UserEmailPasswordAuthorize4?{post_data}"
        r = self.request(url, "POST", data=post_data)

        if r and "errorMessage=" in r.text:
            logging.error(
                "[authorize_email_password] failed: %s",
                redact_secrets(r.text),
            )
            return False

        if r and "refreshToken" not in r.text:
            logging.error(
                "[authorize_email_password] no refreshToken in response: %s",
                redact_secrets(r.text),
            )
            return False

        self.device.refreshTokenAcquire(
            r.text.split('refreshToken="')[1].split('"')[0]
        )
        return True

    def exchange_refresh_token(self) -> bool:
        """Stage 3: Call DeviceLogin17 again with the account refresh token.

        After Stage 2 stored a refresh token, this re-invokes DeviceLogin17 with
        that token to establish a fully authenticated session.

        Returns:
            True if the server returned an access token and user data was parsed.
        """
        self.accessToken = None
        return self.create_device_session()

    @staticmethod
    def _client_datetime_utc() -> str:
        """Return the current UTC time in format: yyyy-MM-ddTHH:mm:ss (no fractional seconds, no Z)."""
        return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    def quickReload(self):
        self.accessToken = None
        self.create_device_session()

    def login(self, email=None, password=None):
        """Orchestrate the three-stage authentication sequence.

        Stage 1: create_device_session() — DeviceLogin17 → access token
        Stage 2: authorize_email_password() — UserEmailPasswordAuthorize4 → refresh token
        Stage 3: exchange_refresh_token() — DeviceLogin17 with refresh token

        If the device already has a refresh token, Stage 1 establishes a full
        session directly and Stages 2-3 are skipped. If only a device session is
        needed (guest/tutorial), Stage 1 alone suffices.
        """
        # Stage 1: create device session
        if not self.create_device_session():
            return False

        if not self.accessToken:
            return False

        # Refresh-token path: device already has a valid refresh token, Stage 1
        # completed the full session — no email/password needed.
        if self.device.refreshToken:
            self.getLatestVersion3()
            self.getTodayLiveOps2()  # Populate todayLiveOps for downstream calls
            return True

        # Guest path: no email means a device-only session for the tutorial.
        if not email:
            return True

        if not password:
            raise ValueError("login() received email but no password")

        # Feature gate: email/password login requires explicit enablement
        # (UserEmailPasswordAuthorize4 is static-analysis-derived and unverified)
        if not self.settings.get("allow_email_password_login", False):
            logging.warning("[login] email/password login blocked: allow_email_password_login feature flag disabled")
            return False

        # Stage 2: submit email + password to acquire a refresh token
        if not self.authorize_email_password(email, password):
            return False

        # Stage 3: exchange refresh token for an authenticated session
        if not self.exchange_refresh_token():
            return False

        self.getLatestVersion3()
        self.getTodayLiveOps2()  # Populate todayLiveOps for downstream calls
        return True

    def getLatestVersion3(self):
        url = f"https://api.pixelstarships.com/SettingService/GetLatestVersion3?languageKey={self.device.languageKey}&deviceType=DeviceTypeIPhone"
        r = self.request(url, "GET")
        if r.content:
            self.latestVersion = xmltodict.parse(r.content, xml_attribs=True)

    def getTodayLiveOps2(self):
        url = f"https://api.pixelstarships.com/LiveOpsService/GetTodayLiveOps2?languageKey={self.device.languageKey}&deviceType=DeviceTypeIPhone"
        r = self.request(url, "GET")
        if r:
            self.todayLiveOps = xmltodict.parse(r.content, xml_attribs=True)

    def listRoomDesigns2(self):
        url = f"https://api.pixelstarships.com/RoomService/ListRoomDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@RoomDesignVersion']}"
        r = self.request(url, "GET")
        if r:
            self.roomDesigns = xmltodict.parse(r.content, xml_attribs=True)

    def listAllTaskDesigns2(self):
        url = f"https://api.pixelstarships.com/TaskService/ListAllTaskDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@RoomDesignVersion']}"
        r = self.request(url, "GET")
        if r:
            self.allTaskDesigns = xmltodict.parse(r.content, xml_attribs=True)

    def listAllTrainingDesigns2(self):
        url = f"https://api.pixelstarships.com/TrainingService/ListAllTrainingDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@RoomDesignVersion']}"
        r = self.request(url, "GET")
        if r:
            self.trainingDesigns = xmltodict.parse(r.content, xml_attribs=True)

    def getShipByUserId(self, userId=0):
        url = f"https://api.pixelstarships.com/ShipService/GetShipByUserId?userId={userId if userId else self.user.id}&accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.shipByUserId = xmltodict.parse(r.content, xml_attribs=True)

            if "ShipService" not in self.shipByUserId:
                logging.error("ShipService data not avaialble.")
                return False

            self.rooms = self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"][
                "Rooms"
            ]["Room"]
            self.researches = self.shipByUserId["ShipService"]["GetShipByUserId"][
                "Ship"
            ]["Researches"]["Research"]
            return True
        return False

    def listAchievementsOfAUser(self):
        url = f"https://api.pixelstarships.com/AchievementService/ListAchievementsOfAUser?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.achievementsOfAUser = xmltodict.parse(r.content, xml_attribs=True)

    def listImportantMessagesForUser(self):
        url = f"https://api.pixelstarships.com/MessageService/ListImportantMessagesForUser?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.importantMessagesForUser = xmltodict.parse(r.content, xml_attribs=True)

    def listUserStarSystems(self):
        url = f"https://api.pixelstarships.com/GalaxyService/ListUserStarSystems?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.userStarSystems = xmltodict.parse(r.content, xml_attribs=True)

    def listStarSystemMarkersAndUserMarkers(self):
        url = f"https://api.pixelstarships.com/GalaxyService/ListStarSystemMarkersAndUserMarkers?accessToken={self.accessToken}"
        r = self.request(url, "GET")
        if r:
            self.starSystemMarkersAndUserMarkers = xmltodict.parse(
                r.content, xml_attribs=True
            )

    def listTasksOfAUser(self):
        url = f"https://api.pixelstarships.com/TaskService/ListTasksOfAUser?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.tasksOfAUser = xmltodict.parse(r.content, xml_attribs=True)

    def listCompletedMissionEvents(self):
        ts = f"{DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        #        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        checksum = ChecksumEmailAuthorize(
            self.device.key,
            self.info["@Email"],
            ts,
            self.accessToken,
            self.salt,
        )
        url = f"https://api.pixelstarships.com/MissionService/ListCompletedMissionEvents?clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        if r:
            self.completedMissionEvents = xmltodict.parse(r.content, xml_attribs=True)

    def listSituations(self):
        url = f"https://api.pixelstarships.com/SituationService/ListSituations?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.situations = xmltodict.parse(r.content, xml_attribs=True)

    def listPvPBattles2(self, take=25, skip=0):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/BattleService/ListPvPBattles2?take={take}&skip={skip}&accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
            r = self.request(url, "GET")
            if r:
                self.pvpBattles = xmltodict.parse(r.content, xml_attribs=True)
                return True
        return False

    def listMissionBattles(self, take=25, skip=0):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/BattleService/ListMissionBattles?take={take}&skip={skip}&accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
            r = self.request(url, "GET")
            if r:
                self.missionBattles = xmltodict.parse(r.content, xml_attribs=True)
                return True
        return False

    def listActionTypes2(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListActionTypes2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@ResearchDesignVersion']}"
            r = self.request(url, "GET")
            if r:
                self.actionTypes = xmltodict.parse(r.content, xml_attribs=True)
                return True
        return False

    def listConditionTypes2(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListConditionTypes2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@ResearchDesignVersion']}"
            r = self.request(url, "GET")
            if r:
                self.conditionTypes = xmltodict.parse(r.content, xml_attribs=True)
                return True
        return False

    def listAllResearches(self):
        url = f"https://api.pixelstarships.com/ResearchService/ListAllResearches?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.allResearches = xmltodict.parse(r.content, xml_attribs=True)

    def listItemsOfAShip(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/ItemService/ListItemsOfAShip?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
            r = self.request(url, "GET")
            if r:
                self.itemsOfAShip = xmltodict.parse(r.content, xml_attribs=True)
                return True
        return False

    def listRoomsViaAccessToken(self):
        url = f"https://api.pixelstarships.com/RoomService/ListRoomsViaAccessToken?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        if r:
            self.roomsViaAccessToken = xmltodict.parse(r.content, xml_attribs=True)

    def listAllCharactersOfUser(self):
        url = f"https://api.pixelstarships.com/CharacterService/ListAllCharactersOfUser?accessToken={self.accessToken}&clientDateTime={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}"
        r = self.request(url, "GET")
        self.allCharactersOfUser = xmltodict.parse(r.content, xml_attribs=True)

        if "CharacterService" not in self.allCharactersOfUser:
            logging.error("Failed to get list of characters on the ship.")
            return False
        return True

    def getRoomName(self, roomDesignId):
        if not hasattr(self, "roomDesigns"):
            self.listAllDesigns4()

        room_designs = _extract_collection(getattr(self, "roomDesigns", None), "RoomDesign")
        for design in room_designs:
            if roomDesignId == design.get("@RoomDesignId"):
                self.roomName = "".join(design.get("@RoomName", ""))
                return True

        self.roomName = ""
        return False

    def finishTraining(self, characterId):
        url = f"{self.baseUrl}/TrainingService/FinishTraining?characterId={characterId}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if r:
            if "errorMessage" in r.text:
                return False
            self.trainingFinish = xmltodict.parse(r.content, xml_attribs=True)
        return True

    def getTrainingUpdate(self, characterId):
        url = f"{self.baseUrl}/TrainingService/GetTrainingUpdate?characterId={characterId}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if r and "errorMessage" in r.text:
            return False
        if r:
            self.trainingUpdate = xmltodict.parse(r.content, xml_attribs=True)
        return True

    def listAllDesigns4(self):
        """
        ListAllDesigns4 has been deprecated
        The design data will be fetched individually
        """
        if not self.latestVersion:
            self.getLatestVersion3()
        if "SettingService" not in self.latestVersion:
            return False
        versions = self.latestVersion["SettingService"]["GetLatestSetting"]["Setting"]
        url = f"{self.baseUrl}/DesignService/ListAllDesigns4?LanguageKey=en&ListFileVersion={versions['@FileVersion']}&ListSpriteVersion={versions['@SpriteVersion']}&ListBackgroundVersion={versions['@BackgroundVersion']}&ListAllShipDesignVersion={versions['@ShipDesignVersion']}&ListRoomDesignVersion={versions['@RoomDesignVersion']}&ListAllCharacterDesignVersion={versions['@CharacterDesignVersion']}&ListAllCharacterDesignActionVersion={versions['@CharacterDesignActionVersion']}&ListItemDesignVersion={versions['@ItemDesignVersion']}&ListCraftDesignVersion={versions['@CraftDesignVersion']}&ListMissileDesignVersion={versions['@MissileDesignVersion']}&ListStarSystemVersion={versions['@StarSystemVersion']}&ListStarSystemLinkVersion={versions['@StarSystemLinkVersion']}&ListAllNewsDesignVersion={versions['@NewsDesignVersion']}&ListLeagueVersion={versions['@LeagueVersion']}&ListAchievementDesignVersion={versions['@AchievementDesignVersion']}&ListRoomDesignPurchaseVersion={versions['@RoomDesignPurchaseVersion']}&ListRoomDesignSpriteVersion={versions['@RoomDesignSpriteVersion']}&ListAllMissionDesignVersion={versions['@MissionDesignVersion']}&ListAnimationVersion={versions['@AnimationVersion']}&ListAllResearchDesignVersion={versions['@ResearchDesignVersion']}&ListAllTrainingDesignVersion={versions['@TrainingDesignVersion']}&ListAllChallengeDesignVersion={versions['@ChallengeDesignVersion']}&ListAllRewardDesignVersion={versions['@RewardDesignVersion']}&ListAllDivisionDesignVersion={versions['@DivisionDesignVersion']}&ListAllCollectionDesignVersion={versions['@CollectionDesignVersion']}&ListAllDrawDesignVersion={versions['@DrawDesignVersion']}&ListAllPromotionDesignVersion={versions['@PromotionDesignVersion']}&ListAllSituationDesignVersion={versions['@SituationDesignVersion']}&ListAllTaskDesignVersion={versions['@TaskDesignVersion']}&ListActionTypeVersion={versions['@ActionTypeVersion']}&ListConditionTypeVersion={versions['@ConditionTypeVersion']}&ListItemDesignActionVersion={versions['@ItemDesignActionVersion']}&ListSeasonDesignVersion={versions['@SeasonDesignVersion']}&ListAssetVersion={versions['@AssetVersion']}&ListMarkerGeneratorDesignVersion={versions['@MarkerGeneratorDesignVersion']}"
        r = self.request(url, "GET")
        if r:
            allDesignVersion = xmltodict.parse(r.content, xml_attribs=True)

            if (
                "DesignService" not in allDesignVersion
                and "ListAllDesigns" not in allDesignVersion["DesignService"]
            ):
                return False
            designs = [
                "Files",
                "Sprites",
                "Backgrounds",
                "ShipDesigns",
                "RoomDesigns",
                "CharacterDesigns",
                "CharacterDesignActions",
                "ItemDesigns",
                "CraftDesigns",
                "MissileDesigns",
                "StarSystems",
                "StarSystemLinks",
                "NewsDesigns",
                "Leagues",
                "AchievementDesigns",
                "RoomDesignPurchases",
                "RoomDesignSprites",
                "MissionDesigns",
                "Animations",
                "ResearchDesigns",
                "TrainingDesigns",
                "ChallengeDesigns",
                "RewardDesigns",
                "DivisionDesigns",
                "CollectionDesigns",
                "DrawDesigns",
                "PromotionDesigns",
                "SituationDesigns",
                "ItemDesignActions",
                "SeasonDesigns",
                "Assets",
                "StarSystemMarkerGenerators",
            ]
            for design in designs:
                if design not in allDesignVersion["DesignService"]["ListAllDesigns"]:
                    logging.error("Missing design data.")
                    return False
            self.files = allDesignVersion["DesignService"]["ListAllDesigns"]["Files"]
            self.sprites = allDesignVersion["DesignService"]["ListAllDesigns"][
                "Sprites"
            ]
            self.backgrounds = allDesignVersion["DesignService"]["ListAllDesigns"][
                "Backgrounds"
            ]
            self.shipDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "ShipDesigns"
            ]
            self.roomDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "RoomDesigns"
            ]
            self.characterDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "CharacterDesigns"
            ]
            self.characterDesignActions = allDesignVersion["DesignService"][
                "ListAllDesigns"
            ]["CharacterDesignActions"]
            self.itemDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "ItemDesigns"
            ]
            self.craftDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "CraftDesigns"
            ]
            self.missileDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "MissileDesigns"
            ]
            self.starSystems = allDesignVersion["DesignService"]["ListAllDesigns"][
                "StarSystems"
            ]
            self.starSystemsLinks = allDesignVersion["DesignService"]["ListAllDesigns"][
                "StarSystemLinks"
            ]
            self.newsDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "NewsDesigns"
            ]
            self.leagues = allDesignVersion["DesignService"]["ListAllDesigns"][
                "Leagues"
            ]
            self.achievementDesigns = allDesignVersion["DesignService"][
                "ListAllDesigns"
            ]["AchievementDesigns"]
            self.roomDesignPurchases = allDesignVersion["DesignService"][
                "ListAllDesigns"
            ]["RoomDesignPurchases"]
            self.roomDesignSprites = allDesignVersion["DesignService"][
                "ListAllDesigns"
            ]["RoomDesignSprites"]
            self.missionDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "MissionDesigns"
            ]
            self.animations = allDesignVersion["DesignService"]["ListAllDesigns"][
                "Animations"
            ]
            self.researchDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "ResearchDesigns"
            ]
            self.trainingDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "TrainingDesigns"
            ]
            self.challengeDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "ChallengeDesigns"
            ]
            self.rewardDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "RewardDesigns"
            ]
            self.divisionDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "DivisionDesigns"
            ]
            self.collectionDesigns = allDesignVersion["DesignService"][
                "ListAllDesigns"
            ]["CollectionDesigns"]
            self.drawDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "DrawDesigns"
            ]
            self.promotionDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "PromotionDesigns"
            ]
            self.situationDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "SituationDesigns"
            ]
            self.itemDesignActions = allDesignVersion["DesignService"][
                "ListAllDesigns"
            ]["ItemDesignActions"]
            self.seasonDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
                "SeasonDesigns"
            ]
            self.assets = allDesignVersion["DesignService"]["ListAllDesigns"]["Assets"]
            self.starSystemMarkerGenerators = allDesignVersion["DesignService"][
                "ListAllDesigns"
            ]["StarSystemMarkerGenerators"]
        return True

    def listAllCharacterDesigns2(self):
        if self.latestVersion:
            url = f"{self.baseUrl}/CharacterService/ListAllCharacterDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@CharacterDesignVersion']}"
            r = self.request(url, "GET")
            if r:
                self.allCharacterDesigns = xmltodict.parse(r.content, xml_attribs=True)

            if "CharacterService" not in self.allCharacterDesigns:
                logging.error(
                    "[%s] CharacterService data not avaialble.", self.info["@Name"]
                )
                return False
            return True
        return False

    def addTraining(self, trainingDesignId, characterId):
        url = f"{self.baseUrl}/TrainingService/AddTraining?trainingDesignId={trainingDesignId}&characterId={characterId}&trainingStartDate={DotNet.validDateTime():%Y-%m-%dT%H:%M:%S}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if r:
            if "errorMessage" in r.text:
                return False
        return True

    def manageTraining(self):
        if (
            not hasattr(self, "allCharactersOfUser")
            and not self.listAllCharactersOfUser()
        ):
            logging.error("allCharactersOfUser data not avaialble.")
            return False

        if (
            not hasattr(self, "allCharacterDesigns")
            and not self.listAllCharacterDesigns2()
        ):
            logging.error("AllCharacterDesigns data not avaialble.")
            return False

        if not hasattr(self, "roomsViaAccessToken"):
            self.listRoomsViaAccessToken()
            if "RoomService" not in self.roomsViaAccessToken:
                logging.error("RoomService data not available.")
                return False

        if not hasattr(self, "trainingDesigns"):
            self.listAllTrainingDesigns2()

        raw_td = getattr(self, "trainingDesigns", None)
        training_designs = _extract_collection(raw_td, "TrainingDesign")
        if not training_designs:
            if raw_td is None or not isinstance(raw_td, dict) or "errorMessage" in str(raw_td):
                logging.error("TrainingDesign data not available.")
                return False
            logging.info("Training design data unavailable; skipping training.")
            return True

        roles = {
            "weapons": {
                "characters": ["Galactic Succubus", "Galactic Snow Maiden", "Delish"],
                "primaryRoom": ["Academy", "Lunar College"],
                "primaryT1": "Read Expert Weapon Theory",
                "primaryT2": "Weapons Summit",
                "primaryT3": "Weapons PHD",
                "secondaryRoom": ["GYM", "Galaxy Gym"],
                "secondaryT1": "Bench Press",
                "secondaryT2": "Muscle Beach",
                "secondaryT3": "Olympic Weightlifting",
            },
            "shields": {
                "characters": ["Mistycball", "C.P.U.", "r2e"],
                "primaryRoom": ["Academy", "Lunar College"],
                "primaryT1": "Big Book of Science",
                "primaryT2": "Scientific Summit",
                "primaryT3": "Science PHD",
                "secondaryRoom": ["Galaxy Gym", "GYM"],
                "secondaryT1": "Bench Press",
                "secondaryT2": "Muscle Beach",
                "secondaryT3": "Olympic Weightlifting",
            },
            "engines": {
                "characters": ["The Conjoint Archon", "Galactic Sprite"],
                "primaryRoom": ["GYM", "Galaxy Gym"],
                "primaryT1": "Bench Press",
                "primaryT2": "Muscle Beach",
                "primaryT3": "Olympic Weightlifting",
                "secondaryRoom": ["Academy", "Lunar College"],
                "secondaryT1": "Study Expert Engineering Manual",
                "secondaryT2": "Engineering Summit",
                "secondaryT3": "Engineering PHD",
            },
            "rushers": {
                "characters": ["Huge Hellaloya", "Cyber Duck"],
                "primaryRoom": ["GYM", "Galaxy Gym"],
                "primaryT1": "Steam Yoga",
                "primaryT2": "Crew vs Wild",
                "primaryT3": "Space Marine",
                "secondaryRoom": ["Galaxy Gym", "GYM"],
                "secondaryT1": "Bench Press",
                "secondaryT2": "Muscle Beach",
                "secondaryT3": "Olympic Weightlifting",
            },
            "defenders": {
                "characters": [
                    "Admiral Serena",
                    "Ancestral Spirit",
                    "Green Ranger - Oliver",
                    "Huntress",
                    "Turkey Hero",
                    "1st engineer Tully",
                    "King Dong",
                ],
                "primaryRoom": ["GYM", "Galexy Gym"],
                "primaryT1": "Bench Press",
                "primaryT2": "Muscle Beach",
                "primaryT3": "Olympic Weightlifting",
                "secondaryRoom": ["Galaxy Gym", "GYM"],
                "secondaryT1": "Kickbox",
                "secondaryT2": "BBJ",
                "secondaryT3": "Shaolin Tradition",
            },
            "pilots": {
                "characters": [],
                "primaryRoom": ["Academy", "Lunar College"],
                "primaryT1": "Read Expert Pilot Handbook",
                "primaryT2": "Pilot Summit",
                "primaryT3": "Pilot Expert",
                "secondaryRoom": ["Galaxy Gym", "GYM"],
                "secondaryT1": "Bench Press",
                "secondaryT2": "Muscle Beach",
                "secondaryT3": "Olympic Weightlifting",
            },
        }
        characters = _extract_collection(getattr(self, "allCharactersOfUser", None), "Character")
        rooms = _extract_collection(getattr(self, "roomsViaAccessToken", None), "Room")
        for character in characters:
            trainingName = ""
            room = {}
            for r_item in rooms:
                if character.get("@RoomId") == r_item.get("@RoomId"):
                    room = r_item
                    break
            if room.get("@RoomDesignId"):
                self.getRoomName(room["@RoomDesignId"])

            logging.debug(
                "{0!r} in {1!r}".format(character["@CharacterName"], self.roomName)
            )
            if any(
                primaryRoom in self.roomName
                for primaryRoom in ["Academy", "GYM", "Galaxy Gym", "Lunar College"]
            ):
                roleData = {}
                for data in roles.values():
                    if character["@CharacterName"] in data["characters"]:
                        roleData = data

                stats = [
                    "@HpImprovement",
                    "@PilotImprovement",
                    "@RepairImprovement",
                    "@WeaponImprovement",
                    "@ScienceImprovement",
                    "@EngineImprovement",
                    "@AttackImprovement",
                    "@AbilityImprovement",
                    "@StaminaImprovement",
                ]
                count = 0
                for stat in stats:
                    count = count + int(character[stat])

                characterDesign = {}
                for characterDesign in self.allCharacterDesigns["CharacterService"][
                    "ListAllCharacterDesigns"
                ]["CharacterDesigns"]["CharacterDesign"]:
                    if (
                        character["@CharacterDesignId"]
                        == characterDesign["@CharacterDesignId"]
                    ):
                        break

                trainingEndDate = None
                if character["@TrainingEndDate"]:
                    trainingEndDate = datetime.datetime.strptime(
                        character["@TrainingEndDate"], "%Y-%m-%dT%H:%M:%S"
                    )

                percent = math.ceil(
                    count / int(characterDesign["@TrainingCapacity"]) * 100
                )
                # Check research prerequisites for each tier
                has_101_research = self.hasResearch("Fitness", 101) or self.hasResearch("Education", 101)
                has_201_research = self.hasResearch("Fitness", 201) or self.hasResearch("Education", 201)
                has_202_research = self.hasResearch("Fitness", 202) or self.hasResearch("Education", 202)
                has_203_research = self.hasResearch("Fitness", 203) or self.hasResearch("Education", 203)
                
                if (
                    roleData
                    and any(
                        primaryRoom in self.roomName
                        for primaryRoom in roleData["primaryRoom"]
                    )
                    and (percent < 51)
                    and has_101_research
                    and (
                        not trainingEndDate
                        or (
                            trainingEndDate
                            < (datetime.datetime.utcnow() - datetime.timedelta(hours=1))
                        )
                    )
                ):
                    trainingName = roleData["primaryT1"]
                    logging.debug(
                        f"[{self.info['@Name']}] Use Green (T1) {trainingName} primary training for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, and {character['@Fatigue']} fatigue."
                    )
                elif (
                    roleData
                    and any(
                        primaryRoom in self.roomName
                        for primaryRoom in roleData["primaryRoom"]
                    )
                    and (50 < percent < 65)
                    and has_201_research
                    and (
                        not trainingEndDate
                        or (
                            trainingEndDate
                            < (
                                datetime.datetime.utcnow()
                                - datetime.timedelta(hours=3, minutes=15)
                            )
                        )
                    )
                ):
                    trainingName = roleData["primaryT2"]
                    logging.debug(
                        f"[{self.info['@Name']}] Use Blue (T2) {trainingName} primary training for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, and {character['@Fatigue']} fatigue."
                    )
                elif (
                    roleData
                    and any(
                        primaryRoom in self.roomName
                        for primaryRoom in roleData["primaryRoom"]
                    )
                    and (64 < percent < 72)
                    and has_203_research
                    and (
                        not trainingEndDate
                        or (
                            trainingEndDate
                            < (
                                datetime.datetime.utcnow()
                                - datetime.timedelta(hours=12, minutes=15)
                            )
                        )
                    )
                ):
                    trainingName = roleData["primaryT3"]
                    logging.debug(
                        f"[{self.info['@Name']}] Use Yellow (T3) {trainingName} primary training for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, and {character['@Fatigue']} fatigue."
                    )
                elif (
                    roleData
                    and percent > 71
                    and not any(
                        secondaryRoom in self.roomName
                        for secondaryRoom in roleData["secondaryRoom"]
                    )
                ):
                    logging.error(
                        f"[{self.info['@Name']}] Move {character['@CharacterName']} with {math.ceil(percent)}% training and {character['@Fatigue']} fatigue in {self.roomName} to the {' or '.join(roleData['secondaryRoom'])} to complete training complete for ability {characterDesign['@SpecialAbilityType']}."
                    )

                elif (
                    roleData
                    and any(
                        secondaryRoom in self.roomName
                        for secondaryRoom in roleData["secondaryRoom"]
                    )
                    and (71 < percent < 74)
                    and (
                        not trainingEndDate
                        or (
                            trainingEndDate
                            < (datetime.datetime.utcnow() - datetime.timedelta(hours=1))
                        )
                    )
                ):
                    trainingName = roleData["secondaryT1"]
                    logging.debug(
                        f"[{self.info['@Name']}] Use Green (T1) {trainingName} primary training for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, and {character['@Fatigue']} fatigue."
                    )
                elif (
                    roleData
                    and any(
                        secondaryRoom in self.roomName
                        for secondaryRoom in roleData["secondaryRoom"]
                    )
                    and (73 < percent < 85)
                    and has_201_research
                    and (
                        not trainingEndDate
                        or (
                            trainingEndDate
                            < (
                                datetime.datetime.utcnow()
                                - datetime.timedelta(hours=3, minutes=15)
                            )
                        )
                    )
                ):
                    trainingName = roleData["secondaryT2"]
                    logging.debug(
                        f"[{self.info['@Name']}] Use Blue (T2) {trainingName} primary training for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, and {character['@Fatigue']} fatigue."
                    )
                elif (
                    roleData
                    and any(
                        secondaryRoom in self.roomName
                        for secondaryRoom in roleData["secondaryRoom"]
                    )
                    and (84 < percent < 90)
                    and has_203_research
                    and (
                        not trainingEndDate
                        or (
                            trainingEndDate
                            < (
                                datetime.datetime.utcnow()
                                - datetime.timedelta(hours=12, minutes=15)
                            )
                        )
                    )
                ):
                    trainingName = roleData["secondaryT3"]
                    logging.debug(
                        f"[{self.info['@Name']}] Use Yellow (T3) {trainingName} primary training for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, and {character['@Fatigue']} fatigue."
                    )
                elif roleData and percent > 89:
                    logging.error(
                        f"[{self.info['@Name']}] Training complete for {character['@CharacterName']} with {math.ceil(percent)}% training and {character['@Fatigue']} fatigue in {self.roomName} for ability {characterDesign['@SpecialAbilityType']}, please move this crew to its designated room."
                    )

                if trainingName:
                    if self.finishTraining(character["@CharacterId"]):
                        statTotal = 0
                        statChange = ""
                        for stat in stats:
                            statTotal = statTotal + int(
                                self.trainingFinish["TrainingService"][
                                    "FinishTraining"
                                ]["Character"][stat]
                            )
                            if int(character[stat]) < int(
                                self.trainingFinish["TrainingService"][
                                    "FinishTraining"
                                ]["Character"][stat]
                            ):
                                if statChange:
                                    statChange = ", ".join(
                                        stat
                                        + " increased by "
                                        + str(
                                            int(
                                                self.trainingFinish["TrainingService"][
                                                    "FinishTraining"
                                                ]["Character"][stat]
                                                - int(character[stat])
                                            )
                                        )
                                    )
                        newPercent = (
                            statTotal / int(characterDesign["@TrainingCapacity"]) * 100
                        )
                        newFatigue = int(
                            self.trainingFinish["TrainingService"]["FinishTraining"][
                                "Character"
                            ]["@Fatigue"]
                        )

                        logging.info(
                            f"[{self.info['@Name']}] Completed training for {character['@CharacterName']} in {self.roomName} with {statChange}, {newPercent - percent:.2f}% training increase and {newFatigue - int(character['@Fatigue'])} fatigue increase."
                        )

                    trainingDesignId = None
                    for design in training_designs:
                        if design.get("@TrainingName") == trainingName:
                            trainingDesignId = design.get("@TrainingDesignId")

                    if self.addTraining(trainingDesignId, character["@CharacterId"]):
                        logging.info(
                            f"[{self.info['@Name']}] Starting training {trainingName} for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue."
                        )
                    if character["@CharacterName"]:
                        logging.info(
                            f"[{self.info['@Name']}] Considering training {trainingName} for {character['@CharacterName']} in {self.roomName} with {percent:.2f}% training complete, ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue."
                        )

                # Step 1 & 2: Use grey consumables for early TP (before 30%)
                # Step 5: Use hero consumables at 100 fatigue for guaranteed TP
                fatigue = int(character.get("@Fatigue", "0"))
                character_id = character.get("@CharacterId")
                if character_id and percent < 30 and fatigue == 0:
                    # Step 2: Spam grey cons in primary stat until 30% TP
                    # Map training stat to consumable stat type
                    stat_to_cons = {
                        "WeaponImprovement": "WPN",
                        "ScienceImprovement": "SCI",
                        "EngineImprovement": "ENG",
                        "PilotImprovement": "PLT",
                        "HpImprovement": "HP",
                        "AttackImprovement": "ATK",
                        "AbilityImprovement": "ABL",
                        "StaminaImprovement": "STA",
                        "RepairImprovement": "STA",
                    }
                    primary_stat = None
                    for stat in stats:
                        if int(character[stat]) > 0:
                            primary_stat = stat_to_cons.get(stat)
                            break
                    
                    if primary_stat:
                        grey_cons_id = self.findConsumableDesignId(primary_stat, "Common")
                        if grey_cons_id:
                            logging.info(f"[{self.info['@Name']}] Using grey {primary_stat} consumable for {character['@CharacterName']} ({percent:.1f}% TP)")
                            self.useConsumable(grey_cons_id, int(character_id))

                # Step 5: Hero consumables at 100 fatigue for guaranteed TP
                elif character_id and percent > 89 and fatigue >= 100:
                    # Need to find which stats are not yet maxed
                    for stat in stats:
                        current_val = int(character[stat])
                        max_val = int(characterDesign.get(stat, "0"))
                        if current_val < max_val:
                            stat_to_cons = {
                                "WeaponImprovement": "WPN",
                                "ScienceImprovement": "SCI",
                                "EngineImprovement": "ENG",
                                "PilotImprovement": "PLT",
                                "HpImprovement": "HP",
                                "AttackImprovement": "ATK",
                                "AbilityImprovement": "ABL",
                                "StaminaImprovement": "STA",
                                "RepairImprovement": "STA",
                            }
                            cons_type = stat_to_cons.get(stat)
                            if cons_type:
                                hero_cons_id = self.findConsumableDesignId(cons_type, "Hero")
                                if hero_cons_id:
                                    logging.info(f"[{self.info['@Name']}] Using hero {cons_type} consumable for {character['@CharacterName']} at 100 fatigue ({percent:.1f}% TP)")
                                    self.useConsumable(hero_cons_id, int(character_id))
                                    break

        return True

    def getCharacterRooms(self):
        if not hasattr(self, "allCharactersOfUser"):
            if not self.listAllCharactersOfUser():
                return False

        for character in self.allCharactersOfUser["CharacterService"][
            "ListAllCharactersOfUser"
        ]["Characters"]["Character"]:
            self.getRoomName(character["@RoomDesignId"])
            if self.roomName != "":
                logging.info(
                    f"[{self.info['@Name']}] {character['@CharacterName']} is located in {self.roomName}."
                )
        return True

    def upgradeCharacter(self, characterId):
        url = f"{self.baseUrl}/CharacterService/UpgradeCharacter?characterId={characterId}&accessToken={self.accessToken}"
        self.request(url, "POST")

    def upgradeCharacters(self):
        try:
            character_names = []

            if not self.allCharactersOfUser:
                self.listAllCharactersOfUser()

            if not hasattr(self, "itemsOfAShip"):
                self.listItemsOfAShip()

            if not hasattr(self, "allCharacterDesigns"):
                self.listAllCharacterDesigns2()

            crewCostsPerLevel = [
                0,
                90,
                270,
                450,
                630,
                810,
                1020,
                1230,
                1440,
                1650,
                1860,
                2130,
                2400,
                2670,
                2940,
                3210,
                3540,
                3870,
                4200,
                4530,
                4860,
                5220,
                5580,
                5940,
                6300,
                6660,
                7050,
                7440,
                7830,
                8220,
                8610,
                9030,
                9450,
                9870,
                10290,
                10710,
                11160,
                11610,
                12060,
                12510,
            ]
            crewCosts = list(accumulate(crewCostsPerLevel))
            legendaryCrewCosts = [cost * 3 for cost in crewCosts]

            legendaryCrewGasCosts = [
                0,
                130000,
                162500,
                195000,
                227500,
                260000,
                292500,
                325000,
                357500,
                390000,
                422500,
                455000,
                487500,
                520000,
                552500,
                585000,
                617500,
                650000,
                682500,
                715000,
                747500,
                780000,
                812500,
                845000,
                877500,
                910000,
                942000,
                975000,
                1007500,
                1040000,
                1072500,
                1105000,
                1137500,
                1170000,
                1202500,
                1235000,
                1267500,
                1300000,
                1332500,
                1365000,
            ]
            crewGasCosts = [
                0,
                0,
                17,
                33,
                65,
                130,
                325,
                650,
                1300,
                3200,
                6500,
                9700,
                13000,
                19500,
                26000,
                35700,
                43800,
                52000,
                61700,
                71500,
                84500,
                104000,
                117000,
                130000,
                156000,
                175000,
                201000,
                227000,
                253000,
                279000,
                312000,
                351000,
                383000,
                422000,
                468000,
                507000,
                552000,
                604000,
                650000,
                715000,
            ]

            characters = _extract_collection(getattr(self, "allCharactersOfUser", {}), "Character")
            character_designs = _extract_collection(getattr(self, "allCharacterDesigns", {}), "CharacterDesign")

            for character in characters:
                if character.get("@RoomId") != "0" and character.get("@Level") != "40":
                    for characterDesign in character_designs:
                        if (
                            character.get("@CharacterDesignId")
                            == characterDesign.get("@CharacterDesignId")
                        ):
                            character_names.append(character.get("@CharacterName", ""))
                            logging.debug(f"{len(crewCosts)=} {len(legendaryCrewCosts)=}")
                            try:
                                lvl = int(character.get("@Level", 0))
                                xp = int(character.get("@Xp", 0))
                            except (ValueError, TypeError):
                                continue

                            rarity = characterDesign.get("@Rarity", "")
                            xp_cost = (
                                legendaryCrewCosts[lvl]
                                if rarity == "Legendary"
                                else crewCosts[lvl]
                            )
                            if xp >= xp_cost:
                                self.collectAllResources()
                                date_str = character.get("@AvailableDate", "")
                                try:
                                    date_to_check = datetime.datetime.strptime(
                                        date_str.split(".")[0], "%Y-%m-%dT%H:%M:%S"
                                    )
                                except ValueError:
                                    date_to_check = datetime.datetime.now()
                                current_datetime = datetime.datetime.now()
                                gas_cost = (
                                    legendaryCrewGasCosts[lvl]
                                    if rarity == "Legendary"
                                    else crewGasCosts[lvl]
                                )
                                try:
                                    gas_avail = int(self.gasTotal)
                                except (ValueError, TypeError):
                                    gas_avail = 0

                                if gas_cost <= gas_avail and date_to_check <= current_datetime:
                                    char_id = character.get("@CharacterId")
                                    char_name = character.get("@CharacterName", "")
                                    logging.info(
                                        f"[{self.info.get('@Name', '')}] Upgrading {char_name} to level {lvl + 1} costing {gas_cost}/{self.gasTotal} gas and {xp}/{xp_cost} xp."
                                    )
                                    if char_id:
                                        self.upgradeCharacter(char_id)

            if character_names:
                logging.info(
                    f"[{self.info.get('@Name', '')}] The following characters are below level 40: {', '.join(character_names)}"
                )
            return True
        except Exception as e:
            logging.error(f"upgradeCharacters failed: {redact_secrets(str(e))}")
            return False

    def listAllRoomActionsOfShip(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListAllRoomActionsOfShip?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            if r:
                self.allRoomActionsOfShip = xmltodict.parse(r.content, xml_attribs=True)
                return True
        return False

    def pusherAuth(self):
        url = f"https://api.pixelstarships.com/UserService/PusherAuth?accessToken={self.accessToken}"
        self.request(url, "POST")

    def listSystemMessagesForUser3(self, fromMessageId=0, take=10000):
        url = f"https://api.pixelstarships.com/MessageService/ListSystemMessagesForUser3?fromMessageId={fromMessageId}&take={take}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        if r:
            self.systemMessagesForUser = xmltodict.parse(r.content, xml_attribs=True)
        if "MessageService" not in self.systemMessagesForUser:
            logging.error("MessageService data unavailable.")
            return False

        return True

    def listFriends(self, userId=0):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/UserService/ListFriends?UserId={userId if userId else self.info['@Id']}&accessToken={self.accessToken}"
            logging.debug(redact_secrets(url))
            r = self.request(url, "POST")
            if r:
                self.systemMessagesForUser = xmltodict.parse(
                    r.content, xml_attribs=True
                )
            return True
        return False

    def listMessagesForChannelKey(self, channelKey="alliance-43958"):
        url = f"https://api.pixelstarships.com/MessageService/ListMessagesForChannelKey?channelKey=channelKey={channelKey}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        if r:
            self.messagesForChannelKey = xmltodict.parse(r.content, xml_attribs=True)
        # Perform error handling and return values based on the results
        # return True
        # return False

    def findUserRanking(self):
        url = f"https://api.pixelstarships.com/LadderService/FindUserRanking?accessToken={self.accessToken}"
        r = self.request(url, "GET")
        if r:
            self.userRanking = xmltodict.parse(r.content, xml_attribs=True)

    def activateItem3(self, itemId=0, targetId=0):
        url = f"https://api.pixelstarships.com/ItemService/ActivateItem3?itemId={itemId}&targetId={targetId}&"
        r = self.request(url, "POST")
        if r:
            self.item = xmltodict.parse(r.content, xml_attribs=True)

    def useConsumable(self, consumableItemDesignId: int, characterId: int) -> bool:
        """Use a consumable item on a character.

        Args:
            consumableItemDesignId: The ItemDesignId of the consumable (e.g., grey HP cons, hero ATK cons)
            characterId: The character to use the consumable on

        Returns:
            True if successful, False otherwise
        """
        if not hasattr(self, "itemsOfAShip") or not self.itemsOfAShip:
            self.listItemsOfAShip()

        items = _extract_collection(getattr(self, "itemsOfAShip", {}), "Item")
        if not items:
            logging.info("No items found on ship")
            return False

        # Find the first available consumable with matching design ID
        item_id = None
        for item in items:
            if item.get("@ItemDesignId") == str(consumableItemDesignId):
                item_id = item.get("@ItemId")
                break

        if not item_id:
            logging.info(f"Consumable itemDesignId {consumableItemDesignId} not found in inventory")
            return False

        logging.info(f"Using consumable {consumableItemDesignId} (itemId={item_id}) on character {characterId}")
        self.activateItem3(itemId=item_id, targetId=characterId)

        # Check for error
        if hasattr(self, "item") and self.item:
            error = self.item.get("ItemService", {}).get("ActivateItem3", {}).get("@errorMessage", "")
            if error:
                logging.error(f"useConsumable failed: {error}")
                return False

        return True

    def findConsumableDesignId(self, statType: str, tier: str = "Common") -> int:
        """Find a consumable item design ID by stat type and tier.
        
        Args:
            statType: One of "HP", "ATK", "ABL", "STA", "WPN", "PLT", "SCI", "ENG"
            tier: One of "Common" (grey), "Uncommon", "Rare", "Epic", "Legendary", "Hero"
            
        Returns:
            ItemDesignId or 0 if not found
        """
        if not hasattr(self, "itemDesigns") or not self.itemDesigns:
            self.listAllDesigns4()
            
        items = _extract_collection(getattr(self, "itemDesigns", {}), "ItemDesign")
        if not items:
            return 0
            
        for item in items:
            item_name = item.get("@ItemName", "")
            item_subtype = item.get("@ItemSubType", "")
            # Look for consumables matching the stat type and tier
            if item_subtype == "Consumable" or "Consumable" in item_name:
                if statType.upper() in item_name.upper() and tier.capitalize() in item_name.capitalize():
                    try:
                        return int(item.get("@ItemDesignId", "0"))
                    except (ValueError, TypeError):
                        pass
        return 0

    def print_market_data(self, v):
        if not isinstance(v, dict):
            return
        message = v.get("@Message", "")
        if isinstance(message, list):
            message = "".join(message)
        activity_arg = v.get("@ActivityArgument", "")
        if activity_arg and isinstance(activity_arg, str) and ":" in activity_arg:
            parts = activity_arg.split(":")
            currency = parts[0]
            price = parts[1] if len(parts) > 1 else ""
            logging.info(f"[{self.info.get('@Name', '')}] {message} for {price} {currency}.")
        else:
            logging.info(f"[{self.info.get('@Name', '')}] {message}.")

    def listActiveMarketplaceMessages(self):
        user_id = getattr(self.user, "id", "0") if hasattr(self, "user") and self.user else "0"
        url = "https://api.pixelstarships.com/MessageService/ListActiveMarketplaceMessages5?itemSubType=None&rarity=None&currencyType=Unknown&itemDesignId=0&userId={}&accessToken={}".format(
            user_id, self.accessToken
        )
        try:
            r = self.request(url, "GET")
            if not r or not r.content:
                return False
            if "errorMessage=" in r.text:
                logging.error(f"An error occurred: {r.text}.")
                return False
            d = xmltodict.parse(r.content, xml_attribs=True)
            if not isinstance(d, dict):
                return False

            messages = _extract_collection(d, "Message")
            if not messages:
                logging.debug(
                    f'[{self.info.get("@Name", "")}] You have no items listed on the marketplace.'
                )
                return True

            for msg in messages:
                self.print_market_data(msg)
            return True
        except Exception as e:
            logging.error(f"listActiveMarketplaceMessages failed: {redact_secrets(str(e))}")
            return False

    def infoBux(self):
        logging.info(
            f"[{self.info['@Name']}] A total of {self.freeStarbuxToday} free starbux was collected today."
        )
        logging.info(
            f"[{self.info['@Name']}] You have a total of {self.credits} starbux."
        )

    def collectAllResources(self):
        url = "https://api.pixelstarships.com/RoomService/CollectAllResources?itemType=None&collectDate={}&accessToken={}".format(
            "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime()),
            self.accessToken,
        )
        try:
            r = self.request(url, "POST")
            if not r or not r.content:
                return False
            d = xmltodict.parse(r.content, xml_attribs=True)
            if not isinstance(d, dict):
                return False
        except Exception as e:
            logging.error(f"collectAllResources failed: {redact_secrets(str(e))}")
            return False

        items = _extract_collection(d, "Item")
        mineral = None
        gas = None
        for item in items:
            t = (item.get("@Type") or item.get("@ItemType") or "").lower()
            qty = str(item.get("@Quantity", "0"))
            if "mineral" in t:
                mineral = qty
            elif "gas" in t:
                gas = qty

        if items:
            if mineral is None and gas is None:
                mineral = str(items[0].get("@Quantity", "0"))
                if len(items) > 1:
                    gas = str(items[1].get("@Quantity", "0"))
            elif mineral is None:
                for item in items:
                    t = (item.get("@Type") or item.get("@ItemType") or "").lower()
                    if "gas" not in t:
                        mineral = str(item.get("@Quantity", "0"))
                        break
            elif gas is None:
                for item in items:
                    t = (item.get("@Type") or item.get("@ItemType") or "").lower()
                    if "mineral" not in t:
                        gas = str(item.get("@Quantity", "0"))
                        break

        self.mineralTotal = mineral if mineral is not None else "0"
        self.gasTotal = gas if gas is not None else "0"

        try:
            user_node = d.get("RoomService", {}).get("CollectResources", {}).get("User", {})
            if isinstance(user_node, dict) and "@Credits" in user_node:
                self.credits = user_node["@Credits"]
        except Exception:
            pass

        self.rssCollectedTimestamp = time.time()
        return True

    def getResourceTotals(self):
        logging.info(
            f'[{self.info["@Name"]}] There is a total of {self.mineralTotal} minerals on your ship.'
        )
        logging.info(
            f'[{self.info["@Name"]}] There is a total of {self.gasTotal} gas on your ship.'
        )

    def collectDailyReward(self):
        try:
            if not getattr(self, "todayLiveOps", None) or "LiveOpsService" not in self.todayLiveOps:
                logging.error(
                    "Unable to collect daily reward because of missing Live Ops data."
                )
                return False
            live_ops = self.todayLiveOps.get("LiveOpsService", {}).get("GetTodayLiveOps", {}).get("LiveOps", {})
            if isinstance(live_ops, dict) and "@DailyRewardArgument" in live_ops:
                self.dailyRewardArgument = live_ops["@DailyRewardArgument"]

            if self.info.get("@DailyRewardStatus") == "1":
                logging.info(
                    f"[{self.info.get('@Name', '')}] Daily reward already collected today."
                )
                return True

            if self.user.isAuthorized:
                url = "https://api.pixelstarships.com/UserService/CollectDailyReward2?dailyRewardStatus=Box&argument={}&accessToken={}".format(
                    getattr(self, "dailyRewardArgument", ""),
                    self.accessToken,
                )

                r = self.request(url, "POST")
                if r:
                    if "You already collected this reward" in r.text:
                        self.dailyRewardTimestamp = time.time()
                        self.dailyReward = 1
                        logging.info(
                            f"[{self.info.get('@Name', '')}] You have already collected the daily reward from the dropship."
                        )
                        return True

                    logging.info(
                        f"[{self.info.get('@Name', '')}] You have collected the daily reward from the dropship."
                    )
                    return True
            return False
        except Exception as e:
            logging.error(f"collectDailyReward failed: {redact_secrets(str(e))}")
            return False

    def collectMiningDrone(self, starSystemMarkerId):
        if self.user.isAuthorized and starSystemMarkerId not in self.dronesCollected:
            url = "https://api.pixelstarships.com/GalaxyService/CollectMarker2?starSystemMarkerId={}&checksum={}&clientDateTime={}&accessToken={}".format(
                starSystemMarkerId,
                self.checksum,
                "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime()),
                self.accessToken,
            )
            r = self.request(url, "POST")
            if "errorMessage=" in r.text:
                return False

            self.dronesCollected[starSystemMarkerId] = 1
            return True
        return False

    def placeMiningDrone(self, missionDesignId, missionEventId):
        if self.user.isAuthorized:
            url = "https://api.pixelstarships.com/MissionService/SelectInstantMission3?missionDesignId={}&missionEventId={}&messageId=0&clientDateTime={},clientNumber=0&checksum={}&accessToken={}".format(
                missionDesignId,
                missionEventId,
                "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime()),
                self.checksum,
                self.accessToken,
            )
            r = self.request(url, "POST")
            if "errorMessage=" in r.text:
                return False
            return True
        return False

    def collectReward2(self, messageId):
        url = f"https://api.pixelstarships.com/MessageService/CollectReward2?messageId={messageId}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&checksum={ChecksumTimeForDate(DotNet.get_time()) + ChecksumPasswordWithString(self.accessToken)}&accessToken={self.accessToken}"
        self.request(url, "POST")

    def AddStarbux2(self, quantity=1):
        url = f"https://api.pixelstarships.com/UserService/AddStarbux2?quantity={quantity}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&checksum={ChecksumTimeForDate(DotNet.get_time()) + ChecksumPasswordWithString(self.accessToken)}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if r:
            self.starbux = xmltodict.parse(r.content, xml_attribs=True)

    def grabFlyingStarbux(self):
        if (
            self.freeStarbuxToday < self.freeStarbuxMax
            and self.freeStarbuxTodayTimestamp + 180 < time.time()
            and self.accessToken
        ):
            logging.debug(f"[{self.info['@Name']}] {self.freeStarbuxToday=}")
            quantity = 0
            if self.freeStarbuxToday < self.freeStarbuxMax:
                quantity = random.randint(1, 5)
                while quantity + self.freeStarbuxToday > self.freeStarbuxMax:
                    quantity = random.randint(1, 5)
            else:
                logging.info(
                    f'[{self.info["@Name"]}] You have collected a total of {self.freeStarbuxToday} starbux today.'
                )
                return True
            logging.debug(f"[{self.info['@Name']}] {quantity=}")
            self.AddStarbux2(quantity)

            if not isinstance(getattr(self, "starbux", None), dict):
                self.quickReload()
                return False

            try:
                user_node = self.starbux.get("UserService", {}).get("AddStarbux", {}).get("User", {})
                if not isinstance(user_node, dict) or "@FreeStarbuxReceivedToday" not in user_node:
                    self.quickReload()
                    return False
                self.freeStarbuxToday = int(user_node["@FreeStarbuxReceivedToday"])
            except (ValueError, TypeError, AttributeError):
                self.quickReload()
                return False

            logging.info(
                f'[{self.info["@Name"]}] You have collected a total of {self.freeStarbuxToday} starbux today.'
            )
            self.freeStarbuxTodayTimestamp = time.time()

            return True
        return False

    def purchaseDrawWithStarbux(self, drawDesignId: str) -> bool:
        """Purchase a draw (pod) using Starbux.
        
        Endpoint: CharacterService/Draw?drawDesignId={drawDesignId}&clientDateTime={}&checksum={}&accessToken={}
        Checksum: MD5(drawDesignId + clientDateTime + ChecksumKey + SavyChecksum)
        
        Args:
            drawDesignId: The ID of the draw design to purchase (e.g., "Scorched Pod")
            
        Returns:
            True if purchase successful, False otherwise
        """
        from sdk.security import checksum_character_draw
        
        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        settings = self.settings or {}
        checksum_key = settings.get("checksum_key", "5343")
        savy_checksum = settings.get("savy_checksum", "Savvy!s0d@")
        
        checksum = checksum_character_draw(
            draw_design_id=drawDesignId,
            client_date_time=ts,
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )
        
        url = f"https://api.pixelstarships.com/CharacterService/Draw?drawDesignId={drawDesignId}&clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        
        if r:
            result = xmltodict.parse(r.content, xml_attribs=True)
            logging.info(f'[{self.info["@Name"]}] Draw purchase result: {result}')
            
            # Check for error
            if "CharacterService" in result:
                draw_result = result["CharacterService"].get("Draw", {})
                if "@errorCode" in draw_result and draw_result["@errorCode"] != "0":
                    logging.error(f'[{self.info["@Name"]}] Draw purchase failed: {draw_result.get("@errorMessage", "Unknown error")}')
                    return False
            
            # Refresh designs to get updated state
            self.listAllDesigns4()
            return True
        
        return False


    def purchaseCatalogItem(self, argument: str) -> bool:
        """Purchase an item from the shop using PurchaseCatalog2 endpoint.

        Endpoint: /ShopService/PurchaseCatalog2?argument={argument}&clientDateTime={}&checksum={}&accessToken={}
        Checksum: MD5(argument + clientDateTime + accessToken + ChecksumKey + SavyChecksum)

        Args:
            argument: The catalog item argument (e.g., "1291" for Scorched Pod)

        Returns:
            True if purchase successful, False otherwise
        """
        from sdk.security import checksum_purchase_catalog2

        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        settings = self.settings or {}
        checksum_key = settings.get("checksum_key", "5343")
        savy_checksum = settings.get("savy_checksum", "Savvy!s0d@")

        if not self.accessToken:
            raise ConfigurationError("purchaseCatalogItem requires accessToken (must be logged in)")

        checksum = checksum_purchase_catalog2(
            argument=argument,
            client_date_time=ts,
            access_token=self.accessToken,
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )

        url = f"https://api.pixelstarships.com/ShopService/PurchaseCatalog2?argument={argument}&clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        r = self.request(url, "POST")

        if r:
            result = xmltodict.parse(r.content, xml_attribs=True)
            logging.info(f'[{self.info["@Name"]}] PurchaseCatalog2 result: {result}')

            # Check for error
            if "ShopService" in result:
                purchase_result = result["ShopService"].get("PurchaseCatalog2", {})
                if "@errorCode" in purchase_result and purchase_result["@errorCode"] != "0":
                    logging.error(f'[{self.info["@Name"]}] PurchaseCatalog2 failed: {purchase_result.get("@errorMessage", "Unknown error")}')
                    return False

                # Update credits balance from response if available
                user_node = purchase_result.get("User", {})
                if isinstance(user_node, dict) and "@Credits" in user_node:
                    try:
                        self.credits = int(user_node["@Credits"])
                    except (ValueError, TypeError):
                        pass
                elif isinstance(purchase_result.get("User"), list) and purchase_result["User"]:
                    user_node = purchase_result["User"][0]
                    if isinstance(user_node, dict) and "@Credits" in user_node:
                        try:
                            self.credits = int(user_node["@Credits"])
                        except (ValueError, TypeError):
                            pass

            return True

        return False


    def purchaseScorchedPodIfAffordable(self) -> bool:
        """Purchase Scorched Pod from Shop if user has enough Starbux.

        Uses PurchaseCatalog2 endpoint with argument=1291.

        Returns:
            True if purchased, False otherwise (not enough Starbux, not found, or error)
        """
        # Check Starbux balance (Credits == Starbux on the User element)
        try:
            starbux = int(self.info.get("@Credits", self.credits))
        except (ValueError, TypeError):
            starbux = int(self.credits) if self.credits else 0

        # Scorched Pod costs 1000 Starbux (argument 1291)
        cost = 1000

        if starbux < cost:
            logging.info(f'[{self.info["@Name"]}] Not enough Starbux for Scorched Pod: have {starbux}, need {cost}')
            return False

        logging.info(f'[{self.info["@Name"]}] Purchasing Scorched Pod (Cost: {cost} Starbux, Argument: 1291)')
        return self.purchaseCatalogItem("1291")

    def getCatalogQuantity(self) -> dict:
        """Get available catalog quantities from the shop.

        Endpoint: /LibeOpsService/GetCatalogQuantity?clientDateTime={}&checksum={}&accessToken={}
        Checksum: MD5(clientDateTime + accessToken + ChecksumKey + SavyChecksum)

        Returns:
            Dict with catalog quantities, or empty dict on failure
        """
        from sdk.security import checksum_get_catalog_quantity

        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        settings = self.settings or {}
        checksum_key = settings.get("checksum_key", "5343")
        savy_checksum = settings.get("savy_checksum", "Savvy!s0d@")

        if not self.accessToken:
            raise ConfigurationError("getCatalogQuantity requires accessToken (must be logged in)")

        checksum = checksum_get_catalog_quantity(
            client_date_time=ts,
            access_token=self.accessToken,
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )

        url = f"https://api.pixelstarships.com/LibeOpsService/GetCatalogQuantity?clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        r = self.request(url, "POST")

        if r:
            result = xmltodict.parse(r.content, xml_attribs=True)
            logging.info(f'[{self.info["@Name"]}] GetCatalogQuantity result: {result}')
            return result

        return {}

    # Determine the boost gauge before attempting to speed up a room
    def speedUpResearchUsingBoostGauge(self, researchId, researchDesignId):
        if not hasattr(self, "allResearchDesigns"):
            if not self.listAllResearchDesigns2():
                return False

        for i in self.allResearchDesigns["ResearchService"]["ListAllResearchDesigns"][
            "ResearchDesigns"
        ]["ResearchDesign"]:
            if i["@ResearchDesignId"] == researchDesignId:
                url = f"https://api.pixelstarships.com/ResearchService/SpeedUpResearchUsingBoostGauge?researchId={researchId}&accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
                r = self.request(url, "POST")
                if r and "@errorMessage" in r.text:
                    logging.info(
                        f"[{self.info['@Name']}] Failed to speed up research for {''.join(i['@ResearchName'])}."
                    )
                    return False
                logging.info(
                    f"[{self.info['@Name']}] Speeding up research for {''.join(i['@ResearchName'])}."
                )
                return True
        return False

    # Determine the boost gauge before attempting to speed up a room
    def speedUpRoomConstructionUsingBoostGauge(self, roomId, roomDesignId):
        if not hasattr(self, "roomDesigns"):
            if not self.listRoomDesigns2():
                return False

        room_designs = _extract_collection(getattr(self, "roomDesigns", None), "RoomDesign")
        for i in room_designs:
            if i.get("@RoomDesignId") == roomDesignId:
                url = f"https://api.pixelstarships.com/RoomService/SpeedUpRoomConstructionUsingBoostGauge?roomId={roomId}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&accessToken={self.accessToken}"
                r = self.request(url, "POST")
                if r and "errorMessage" in r.text:
                    logging.info(
                        f"[{self.info['@Name']}] Failed to speed contruction for {''.join(i['@RoomName'])}."
                    )
                    return False
                logging.info(
                    f"[{self.info['@Name']}] Speeding up contruction for {''.join(i['@RoomName'])}."
                )
                return True
        return False

    def rushResearchOrConstruction(self):
        if not hasattr(self, "shipByUserId"):
            self.getShipByUserId()

        if "ShipService" in self.shipByUserId:
            for i in self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"][
                "Researches"
            ]["Research"]:
                if i["@ResearchState"] == "Researching":
                    return self.speedUpResearchUsingBoostGauge(
                        i["@ResearchId"], i["@ResearchDesignId"]
                    )
                for i in self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"][
                    "Rooms"
                ]["Room"]:
                    if i["@RoomStatus"] == "Upgrading":
                        return self.speedUpRoomConstructionUsingBoostGauge(
                            i["@RoomId"], i["@RoomDesignId"]
                        )
        logging.debug(
            f'[{self.info["@Name"]}] There are no rooms or research to speed up.'
        )
        return False

    def upgradeResearches(self):
        self.listAllResearches()
        self.listAllResearchDesigns2()

        upgradeList = []
        rootDesigns = collections.defaultdict(list)
        designExceptionList = []
        rootDesignExceptionList = []
        researchingFlag = False

        try:
            all_researches = _extract_collection(getattr(self, "allResearches", None), "Research")
            all_research_designs = _extract_collection(getattr(self, "allResearchDesigns", None), "ResearchDesign")

            for research in all_researches:
                for design in all_research_designs:
                    if (
                        research.get("@ResearchDesignId") == design.get("@ResearchDesignId")
                        and design.get("@ResearchDesignId") not in designExceptionList
                    ):
                        if research.get("@ResearchState") == "Researching":
                            logging.info(
                                f"[{self.info['@Name']}] {''.join(design.get('@ResearchName', ''))} is currently being researched."
                            )
                            researchingFlag = True
                        designExceptionList.append(design.get("@ResearchDesignId"))
            for design in all_research_designs:
                if (
                    design.get("@ResearchDesignId") not in designExceptionList
                    and design.get("@RootResearchDesignId") not in rootDesignExceptionList
                ):
                    rootDesigns[design.get("@RootResearchDesignId")].append(design)
                    upgradeList.append(
                        [
                            design.get("@ResearchDesignId"),
                            design.get("@GasCost", "0"),
                            design.get("@StarbuxCost", "0"),
                            design.get("@ResearchName", ""),
                        ]
                    )
                    rootDesignExceptionList.append(design.get("@RootResearchDesignId"))
            self.collectAllResources()
            if not researchingFlag:
                for researchItem in upgradeList:
                    if int(researchItem[1]) > 0 and int(researchItem[1]) < int(
                        self.gasTotal
                    ):
                        res = self.addResearch(researchItem[0])
                        if res is True:
                            logging.info(
                                f"[{self.info['@Name']}] Beginning research for {researchItem[3]}"
                            )
                            researchingFlag = True
                            break
                        elif res == "LAB_UPGRADE_REQUIRED":
                            continue
                        else:
                            return False
            return True
        except Exception:
            logging.exception("Unable to upgrade research.", exc_info=True)
            return False

    def getResearchLevel(self, researchName: str) -> int:
        """Get the current level of a specific research by name.
        
        Args:
            researchName: Name of the research (e.g., "Fitness 101", "Education 201")
            
        Returns:
            Research level (0 if not found)
        """
        if not hasattr(self, "allResearches") or not self.allResearches:
            self.listAllResearches()
        
        all_researches = _extract_collection(getattr(self, "allResearches", None), "Research")
        if not all_researches:
            return 0
            
        for research in all_researches:
            if research.get("@ResearchName", "").startswith(researchName.split()[0]):
                try:
                    level = int(research.get("@ResearchLevel", "0"))
                    return level
                except (ValueError, TypeError):
                    pass
        return 0

    def hasResearch(self, researchName: str, minLevel: int = 1) -> bool:
        """Check if a research has reached at least the minimum level.
        
        Args:
            researchName: Name of the research (e.g., "Fitness", "Education")
            minLevel: Minimum required level (e.g., 101, 201, 202, 203)
            
        Returns:
            True if research level >= minLevel
        """
        return self.getResearchLevel(researchName) >= minLevel

    def upgradeRooms(self):
        try:
            if not hasattr(self, "roomDesigns"):
                self.listRoomDesigns2()

            raw_rd = getattr(self, "roomDesigns", None)
            room_designs = _extract_collection(raw_rd, "RoomDesign")
            if not room_designs:
                logging.info("Room design data unavailable; skipping room upgrades.")
                if raw_rd is None or not isinstance(raw_rd, dict) or "errorMessage" in str(raw_rd):
                    return False
                return True

            self.listUpgradingRooms()
            self.getShipByUserId()
            shipByUserId = getattr(self, "shipByUserId", None)
            if shipByUserId:
                rooms = _extract_collection(shipByUserId, "Room")
                for room in rooms:
                    roomId = room.get("@RoomId")
                    roomStatus = room.get("@RoomStatus")
                    roomDesignId = room.get("@RoomDesignId")
                    roomName = ""
                    upgradeRoomDesignId = ""
                    upgradeRoomName = ""

                    for roomDesignData in room_designs:
                        if roomDesignId == roomDesignData.get("@RoomDesignId"):
                            roomName = "".join(roomDesignData.get("@RoomName", ""))
                        if roomDesignId == roomDesignData.get("@UpgradeFromRoomDesignId"):
                            upgradeRoomDesignId = roomDesignData.get("@RoomDesignId")
                            upgradeRoomName = "".join(roomDesignData.get("@RoomName", ""))
                            cost_str = roomDesignData.get("@PriceString", "")
                            cost = cost_str.split(":") if cost_str else [""]
                            if (cost[0] == "mineral") and (
                                len(cost) > 1 and int(cost[1]) > int(self.mineralTotal)
                            ):
                                continue

                            if (cost[0] == "gas") and (
                                len(cost) > 1 and int(cost[1]) > int(self.gasTotal)
                            ):
                                continue

                            if (
                                roomName
                                and upgradeRoomName
                                and (roomStatus != "Upgrading")
                                and upgradeRoomDesignId != "0"
                            ):
                                logging.info(
                                    f'[{self.info["@Name"]}] Upgradng {roomName} to {upgradeRoomName}.'
                                )
                                url = f"https://api.pixelstarships.com/RoomService/UpgradeRoom2?roomId={roomId}&upgradeRoomDesignId={upgradeRoomDesignId}&accessToken={self.accessToken}"
                                r = self.request(url, "POST")
                                roomName = ""
                                upgradeRoomName = ""
                                if r and "concurrent" in r.text:
                                    logging.info(
                                        f'[{self.info["@Name"]}] You have reached the maximum number of concurrent constructions allowed.'
                                    )
                                    self.max_room_upgrades = True
                                    break
                                self.collectAllResources()
                    if self.max_room_upgrades:
                        break
            return True
        except Exception:
            logging.exception("Unable to upgrade rooms.", exc_info=True)
            return False

    def listUpgradingRooms(self):
        self.getShipByUserId()
        shipByUserId = getattr(self, "shipByUserId", None)
        room_designs = _extract_collection(getattr(self, "roomDesigns", None), "RoomDesign")
        if shipByUserId and room_designs:
            if "ShipService" not in shipByUserId:
                logging.debug(f"{shipByUserId=}")
            rooms = _extract_collection(shipByUserId, "Room")
            for room in rooms:
                if room.get("@RoomStatus") == "Upgrading":
                    for roomDesignData in room_designs:
                        if room.get("@RoomDesignId") == roomDesignData.get("@RoomDesignId"):
                            logging.info(
                                f"[{self.info['@Name']}] {''.join(roomDesignData.get('@RoomName', ''))} is currently being upgraded."
                            )

    def listAllResearchDesigns2(self):
        if self.latestVersion:
            url = f"https://api.pixelstarships.com/ResearchService/ListAllResearchDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@ResearchDesignVersion']}"
            r = self.request(url, "GET")
            self.allResearchDesigns = xmltodict.parse(r.content, xml_attribs=True)
            if "ResearchService" not in self.allResearchDesigns:
                return False

            return True

    def addResearch(self, researchDesignId):
        url = f"https://api.pixelstarships.com/ResearchService/AddResearch?researchDesignId={researchDesignId}&researchStartDate={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if r and "Please upgrade your lab room." in r.text:
            logging.info(f"Skipped research design {researchDesignId}: lab upgrade required.")
            return "LAB_UPGRADE_REQUIRED"
        if not r or "errorMessage" in r.text:
            return False
        return True

    def rebuildAmmo(self):
        """Restock ammo, android, craft, module, and charge items.

        Uses RebuildAmmo3 with checksum derived from configuration values.
        Requires checksum_key and savy_checksum configuration settings.

        Checksum formula (URL parameter order: ammoCategory + clientDateTime + accessToken + checksum_key):
        preimage = ammoCategory + clientDateTime + accessToken + checksum_key
        encrypted = preimage + savy_checksum
        checksum = MD5(encrypted)
        """
        # These must be provided via configuration - no fallback to hardcoded values
        checksum_key = self.settings.get("checksum_key")
        savy_checksum = self.settings.get("savy_checksum")

        if not checksum_key or not savy_checksum:
            raise ConfigurationError(
                "RebuildAmmo3 requires checksum_key and savy_checksum configuration "
                "values compatible with the installed game version."
            )

        if not self.accessToken:
            raise ConfigurationError("RebuildAmmo3 requires accessToken (must be logged in)")

        ammoCategories = [
            "None",
            "Ammo",
            "Android",
            "Craft",
            "Module",
            "Charge",
        ]
        for ammoCategory in ammoCategories:
            if ammoCategory == "None":
                logging.info(f'[{self.info["@Name"]}] Restocking all ammo items.')
            else:
                logging.info(
                    f'[{self.info["@Name"]}] Restocking {ammoCategory.lower()} items.'
                )
            ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
            preimage = ammoCategory + ts + self.accessToken + checksum_key
            encrypted = preimage + savy_checksum
            checksum = hashlib.md5(encrypted.encode("utf-8")).hexdigest()
            url = f"{self.baseUrl}/RoomService/RebuildAmmo3?ammoCategory={ammoCategory}&clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
            logging.debug(redact_secrets(f"{url=}"))
            r = self.request(url, "POST")
            if "errorMessage=" in r.text:
                logging.warning(f'[{self.info["@Name"]}] RebuildAmmo3 {ammoCategory} failed: {r.text[:200]}')
        return True

    def getCrewInfo(self):
        try:
            character_list = []
            self.listAllCharactersOfUser()
            characters = _extract_collection(getattr(self, "allCharactersOfUser", {}), "Character")
            if not characters:
                if not getattr(self, "allCharactersOfUser", {}):
                    logging.error("ListAllCharactersOfUser endpoint failed.")
                    return False
                return True
            for character in characters:
                name = character.get("@CharacterName")
                if name:
                    character_list.append(name)
            if character_list:
                logging.info(
                    f"[{self.info.get('@Name', '')}] List of characters: {', '.join(character_list)}"
                )
            return True
        except Exception as e:
            logging.error(f"getCrewInfo failed: {redact_secrets(str(e))}")
            return False

    def getMessages(self):
        try:
            if not self.listSystemMessagesForUser3():
                return False

            messages = _extract_collection(getattr(self, "systemMessagesForUser", {}), "Message")
            if not messages:
                return True

            for message in messages:
                activity_arg = message.get("@ActivityArgument", "")
                message_text = message.get("@Message", "")
                message_id = message.get("@MessageId")

                if activity_arg and activity_arg != "None" and isinstance(activity_arg, str) and ":" in activity_arg:
                    parts = activity_arg.split(":")
                    arg_type = parts[0]
                    arg_val = parts[1] if len(parts) > 1 else ""
                    logging.info(
                        f"[{self.info.get('@Name', '')}] {message_text} {arg_val} {arg_type} is collectable."
                    )
                    if arg_type not in ["gas", "mineral"] and message_id:
                        self.collectReward2(message_id)
                else:
                    logging.info(f"[{self.info.get('@Name', '')}] {message_text}")
                    if message_id:
                        self.actionMessage(message_id)
            return True
        except Exception as e:
            logging.error(f"getMessages failed: {redact_secrets(str(e))}")
            return False

    def listFinishTasks(self):
        try:
            self.listTasksOfAUser()
            self.listAllTaskDesigns2()
            tasks = _extract_collection(getattr(self, "tasksOfAUser", {}), "Task")
            task_designs = _extract_collection(getattr(self, "allTaskDesigns", {}), "TaskDesign")

            for task in tasks:
                logging.debug(f"{task=}")
                if task.get("@Collected") == "true":
                    for taskDesign in task_designs:
                        if taskDesign.get("@TaskDesignId") == task.get("@TaskDesignId"):
                            logging.info(
                                f"[{self.info.get('@Name', '')}] Completed task to {taskDesign.get('@Description', '')}."
                            )
            return True
        except Exception as e:
            logging.error(f"listFinishTasks failed: {redact_secrets(str(e))}")
            return False

    def collectTaskCompletion(self, taskDesignId):
        url = f"{self.baseUrl}/TaskService/CollectTaskCompletion?taskDesignId={taskDesignId}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if "errorMessage" in r.text:
            return False
        return True

    def actionMessage(self, messageId):
        url = f"{self.baseUrl}/MessageService/ActionMessage?messageId={messageId}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        if r and "errorMessage" in r.text:
            return False
        return True

    def collectTaskReward(self):
        try:
            self.listTasksOfAUser()
            self.listAllTaskDesigns2()
            tasks = _extract_collection(getattr(self, "tasksOfAUser", {}), "Task")
            task_designs = _extract_collection(getattr(self, "allTaskDesigns", {}), "TaskDesign")

            for task in tasks:
                if task.get("@Collected") == "false" and task.get("@ProgressValue") != "0":
                    for taskDesign in task_designs:
                        if taskDesign.get("@TaskDesignId") == task.get("@TaskDesignId"):
                            if task.get("@ProgressValue") == taskDesign.get("@ObjectiveAmount"):
                                if self.collectTaskCompletion(task.get("@TaskDesignId")):
                                    logging.info(
                                        f"[{self.info.get('@Name', '')}] Collecting reward for objective: {taskDesign.get('@Name', '')}."
                                    )
            return True
        except Exception as e:
            logging.error(f"collectTaskReward failed: {redact_secrets(str(e))}")
            return False

    def heartbeat(self, force: bool = False):
        """Send HeartBeat4 to keep the game session alive.

        The official client sends this every ~60 seconds continuously.
        The server uses this to validate that the game session is active;
        without it, CreateBattle9 (and other actions) may return errors.

        Args:
            force: If True, send heartbeat even if <60s since last heartbeat.
                   Use this before critical operations like CreateBattle9.
        """
        if not force:
            if (
                divmod(
                    (datetime.datetime.utcnow() - self.user.lastHeartBeat).seconds,
                    60,
                )[0]
                == 0
            ):
                return False

        if not self.accessToken:
            self.quickReload()

        url = f"{self.baseUrl}/UserService/HeartBeat4?clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&checksum={ChecksumTimeForDate(DotNet.get_time()) + ChecksumPasswordWithString(self.accessToken)}&accessToken={self.accessToken}"
        r = self.session.request("POST", url, headers=self.headers)
        d = xmltodict.parse(r.content, xml_attribs=True)

        if "errorMessage" in r.text:
            logging.error(f"[{self.info['@Name']}] {d}")
            return False

        if "UserService" in d and d["UserService"]["HeartBeat"]["@success"] == "true":
            self.user.lastHeartBeat = datetime.datetime.utcnow()
            logging.info(f"[{self.info['@Name']}] Successful sent heartbeat.")
            return True

        return False

    def createBattle9(self, clientHp: int = 4000) -> bool:
        """Create a battle using the older CreateBattle9 endpoint.

        Uses CreateBattle9 endpoint with checksum (the actual endpoint used by the game client).

        Checksum formula (verified against capture: 8118b3ffc06d9e8b520c1b6956e7ca9a):
        preimage = clientDateTime + checksum_key
        encrypted = preimage + savy_checksum
        checksum = MD5(encrypted)

        Args:
            clientHp: Client HP to use for battle matchmaking (default 4000)

        Returns:
            True if battle created successfully, False otherwise
        """
        checksum_key = self.settings.get("checksum_key")
        savy_checksum = self.settings.get("savy_checksum")

        if not checksum_key or not savy_checksum:
            raise ConfigurationError(
                "CreateBattle9 requires checksum_key and savy_checksum configuration "
                "values compatible with the installed game version."
            )

        if not self.accessToken:
            raise ConfigurationError("CreateBattle9 requires accessToken (must be logged in)")

        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        # CreateBattle9 checksum (VERIFIED against capture): clientDateTime + checksum_key
        preimage = ts + checksum_key
        encrypted = preimage + savy_checksum
        checksum = hashlib.md5(encrypted.encode("utf-8")).hexdigest()

        url = f"{self.baseUrl}/BattleService/CreateBattle9?clientHp={clientHp}&clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        # Game client also sends the same params as form-encoded POST body
        post_data = f"clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        logging.debug(redact_secrets(f"{url=}"))
        r = self.request(url, "POST", data=post_data)
        if r and "errorMessage=" in r.text:
            logging.warning(f'[{self.info["@Name"]}] CreateBattle9 failed: {r.text[:200]}')
            return False

        if r:
            self.createBattle9Result = xmltodict.parse(r.content, xml_attribs=True)
            # Log the raw response for debugging
            logging.debug(f'[{self.info["@Name"]}] CreateBattle9 response: {redact_secrets(r.text[:300])}')
            # Extract battleId from response for subsequent calls.
            # The server returns <CreateBattle> not <CreateBattle9> as the wrapper element,
            # so we try both keys. (Verified against live CI run 31656147771.)
            battle_service = self.createBattle9Result.get("BattleService", {})
            battle_wrapper = (
                battle_service.get("CreateBattle9")
                or battle_service.get("CreateBattle")
                or {}
            )
            try:
                battle_id = battle_wrapper["Battle"]["@BattleId"]
                self.lastBattleId = battle_id
                logging.info(f'[{self.info["@Name"]}] Created battle: {battle_id}')
            except (KeyError, TypeError) as e:
                logging.warning(f'[{self.info["@Name"]}] Could not extract BattleId: {e}. Response structure: {redact_secrets(str(self.createBattle9Result)[:200])}')
            return True
        return False

    def acceptBattle5(self, battleId: str, itemDesignId: int = 0) -> bool:
        """Accept a battle using the AcceptBattle5 endpoint.

        Uses AcceptBattle5 endpoint with checksum.

        Checksum formula (URL parameter order before checksum):
        preimage = battleId + itemDesignId + clientDateTime + accessToken + checksum_key
        encrypted = preimage + savy_checksum
        checksum = MD5(encrypted)

        Args:
            battleId: Battle ID from CreateBattle9 response
            itemDesignId: Item design ID (default 0)

        Returns:
            True if battle accepted successfully, False otherwise
        """
        checksum_key = self.settings.get("checksum_key")
        savy_checksum = self.settings.get("savy_checksum")

        if not checksum_key or not savy_checksum:
            raise ConfigurationError(
                "AcceptBattle5 requires checksum_key and savy_checksum configuration "
                "values compatible with the installed game version."
            )

        if not self.accessToken:
            raise ConfigurationError("AcceptBattle5 requires accessToken (must be logged in)")

        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        # AcceptBattle5 checksum: battleId + itemDesignId + clientDateTime + accessToken + checksum_key
        preimage = battleId + str(itemDesignId) + ts + self.accessToken + checksum_key
        encrypted = preimage + savy_checksum
        checksum = hashlib.md5(encrypted.encode("utf-8")).hexdigest()

        url = f"{self.baseUrl}/BattleService/AcceptBattle5?battleId={battleId}&itemDesignId={itemDesignId}&clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        logging.debug(redact_secrets(f"{url=}"))
        r = self.request(url, "POST")
        if r and "errorMessage=" in r.text:
            logging.warning(f'[{self.info["@Name"]}] AcceptBattle5 failed: {r.text[:200]}')
            return False

        if r:
            self.acceptBattle5Result = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def createStarBattle5(self, clientHp: int, searchNumber: int = 0, value: int = 0) -> bool:
            """Create a star battle (PvP matchmaking).

            Uses CreateStarBattle5 endpoint with checksum.
            Requires checksum_key and savy_checksum configuration settings.

            Checksum formula (original implementation with deviceKey and email):
            preimage = clientHp + clientDateTime + accessToken + searchNumber + value + deviceKey + email + checksumKey
            encrypted = preimage + savyChecksum
            checksum = MD5(encrypted)
            """
            checksum_key = self.settings.get("checksum_key")
            savy_checksum = self.settings.get("savy_checksum")

            if not checksum_key or not savy_checksum:
                raise ConfigurationError(
                    "CreateStarBattle5 requires checksum_key and savy_checksum configuration "
                    "values compatible with the installed game version."
                )

            if not self.accessToken:
                raise ConfigurationError("CreateStarBattle5 requires accessToken (must be logged in)")

            ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
            email = self.info.get("@Email", "unknown@unknown.com")
        
            from sdk.security import checksum_create_star_battle5
        
            checksum = checksum_create_star_battle5(
                client_hp=str(clientHp),
                client_date_time=ts,
                access_token=self.accessToken,
                search_number=str(searchNumber),
                value=str(value),
                device_key=self.device.key,
                email=email,
                checksum_key=checksum_key,
                savy_checksum=savy_checksum,
            )

            url = f"{self.baseUrl}/BattleService/CreateStarBattle5?clientHp={clientHp}&clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}&searchNumber={searchNumber}&value={value}"
            logging.debug(redact_secrets(f"{url=}"))
            r = self.request(url, "POST")
            if r and "errorMessage=" in r.text:
                logging.warning(f'[{self.info["@Name"]}] CreateStarBattle5 failed: {r.text[:200]}')
                return False
        
            if r:
                self.createStarBattle5Result = xmltodict.parse(r.content, xml_attribs=True)
                # Extract battleId from response for subsequent calls
                try:
                    battle_id = self.createStarBattle5Result["BattleService"]["CreateStarBattle5"]["@battleId"]
                    self.lastBattleId = battle_id
                    logging.info(f'[{self.info["@Name"]}] Created battle: {battle_id}')
                except (KeyError, TypeError):
                    pass
                return True
            return False

    def verifyBattle2(
        self,
        battleId: str,
        clientOutcomeType: int,
        clientEndFrame: int,
        clientResultString: str,
        attackingShipHp: int,
        syncStatus: int = 0,
        battleSyncEntity: str = "",
        syncErrorType: int = 0,
        description: str = "",
        score: int = 0,
    ) -> bool:
        """Verify battle result with the server.
        
        Uses VerifyBattle2 endpoint with checksum.
        Requires checksum_key and savy_checksum configuration settings.
        """
        checksum_key = self.settings.get("checksum_key")
        savy_checksum = self.settings.get("savy_checksum")

        if not checksum_key or not savy_checksum:
            raise ConfigurationError(
                "VerifyBattle2 requires checksum_key and savy_checksum configuration "
                "values compatible with the installed game version."
            )

        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        # VerifyBattle2 checksum: battleId + clientOutcomeType + clientEndFrame + clientResultString + attackingShipHp + syncStatus + battleSyncEntity + syncErrorType + description + score + checksumKey
        preimage = (
            battleId
            + str(clientOutcomeType)
            + str(clientEndFrame)
            + clientResultString
            + str(attackingShipHp)
            + str(syncStatus)
            + battleSyncEntity
            + str(syncErrorType)
            + description
            + str(score)
            + checksum_key
        )
        encrypted = preimage + savy_checksum
        checksum = hashlib.md5(encrypted.encode("utf-8")).hexdigest()
        
        url = (
            f"{self.baseUrl}/BattleService/VerifyBattle2?"
            f"battleId={battleId}&clientOutcomeType={clientOutcomeType}&clientEndFrame={clientEndFrame}"
            f"&clientResultString={urllib.parse.quote(clientResultString)}&attackingShipHp={attackingShipHp}"
            f"&checksum={checksum}&syncStatus={syncStatus}&battleSyncEntity={urllib.parse.quote(battleSyncEntity)}"
            f"&syncErrorType={syncErrorType}&description={urllib.parse.quote(description)}&score={score}"
            f"&accessToken={self.accessToken}"
        )
        logging.debug(redact_secrets(f"{url=}"))
        r = self.request(url, "POST")
        if r and "errorMessage=" in r.text:
            logging.warning(f'[{self.info["@Name"]}] VerifyBattle2 failed: {r.text[:200]}')
            return False
        
        if r:
            self.verifyBattle2Result = xmltodict.parse(r.content, xml_attribs=True)
        return True

    def finaliseBattle15(
        self,
        battleId: str,
        clientOutcomeType: int,
        clientEndFrame: int,
        clientResultString: str,
        attackingShipHp: int,
        clientVersion: str = "0.999.59",
    ) -> bool:
        """Finalise battle with the server (FinaliseBattle15 endpoint).
        
        Uses the templated FinaliseBattle15 endpoint with checksum.
        This is the final step to complete a battle.
        Requires checksum_key and savy_checksum configuration settings.
        """
        checksum_key = self.settings.get("checksum_key")
        savy_checksum = self.settings.get("savy_checksum")

        if not checksum_key or not savy_checksum:
            raise ConfigurationError(
                "FinaliseBattle15 requires checksum_key and savy_checksum configuration "
                "values compatible with the installed game version."
            )

        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        # FinaliseBattle15 checksum (from static analysis of IL2CPP metadata):
        # preimage = battleId + clientOutcomeType + clientEndFrame + clientResultString + attackingShipHp + clientVersion + accessToken + checksumKey
        # encrypted = preimage + savyChecksum
        # checksum = MD5(encrypted)
        access_token = self.accessToken or ""
        preimage = (
            battleId
            + str(clientOutcomeType)
            + str(clientEndFrame)
            + clientResultString
            + str(attackingShipHp)
            + clientVersion
            + access_token
            + checksum_key
        )
        encrypted = preimage + savy_checksum
        checksum = hashlib.md5(encrypted.encode("utf-8")).hexdigest()
        
        # URL template: /BattleService/{0}?battleId={1}&clientOutcomeType={2}&clientEndFrame={3}&clientResultString={4}&attackingShipHp={5}&checksum={6}&clientVersion={7}&accessToken={8}
        # Where {0} = "FinaliseBattle15"
        url = (
            f"{self.baseUrl}/BattleService/FinaliseBattle15?"
            f"battleId={battleId}&clientOutcomeType={clientOutcomeType}&clientEndFrame={clientEndFrame}"
            f"&clientResultString={urllib.parse.quote(clientResultString)}&attackingShipHp={attackingShipHp}"
            f"&checksum={checksum}&clientVersion={urllib.parse.quote(clientVersion)}&accessToken={self.accessToken}"
        )
        logging.debug(redact_secrets(f"{url=}"))
        r = self.request(url, "POST")
        if r and "errorMessage=" in r.text:
            logging.warning(f'[{self.info["@Name"]}] FinaliseBattle15 failed: {r.text[:200]}')
            return False
        
        if r:
            self.finaliseBattle15Result = xmltodict.parse(r.content, xml_attribs=True)
            logging.info(f'[{self.info["@Name"]}] Battle finalised successfully: {battleId}')
        return True

    def getShipHpFraction(self) -> float:
            """Return the ship's current HP as a fraction of max (0.0-1.0).

            Ship design is the authoritative source for max HP. The ship data
            provides current HP (@Hp), while the ship design provides max HP.
            """
            logging.debug(f"[{self.info.get('@Name', '')}] Getting ship HP fraction...")
        
            # Get current HP from ship data
            if not hasattr(self, "shipByUserId") or not self.shipByUserId:
                if not self.getShipByUserId():
                    logging.warning(f"[{self.info.get('@Name', '')}] getShipByUserId() failed")
                    return -1.0
        
            try:
                ship = self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]
            except (KeyError, TypeError) as e:
                logging.warning(f"[{self.info.get('@Name', '')}] Ship data structure error: {e}")
                return -1.0

            # Get current HP from ship data
            cur = ship.get("@Hp")
            if cur is None:
                logging.warning(f"[{self.info.get('@Name', '')}] No @Hp in ship data")
                return -1.0
            try:
                cur_i = int(cur)
            except (ValueError, TypeError):
                logging.warning(f"[{self.info.get('@Name', '')}] Invalid @Hp value: {cur}")
                return -1.0

            # Get max HP from ship design
            ship_design_id = ship.get("@ShipDesignId")
            if not ship_design_id:
                logging.warning(f"[{self.info.get('@Name', '')}] No @ShipDesignId in ship data")
                return -1.0

            # Ensure ship designs are loaded
            if not hasattr(self, "shipDesigns") or not self.shipDesigns:
                logging.warning(f"[{self.info.get('@Name', '')}] Loading ship designs...")
                if not self.listAllDesigns4():
                    logging.warning(f"[{self.info.get('@Name', '')}] Failed to load ship designs")
                    return -1.0
                logging.debug(f"[{self.info.get('@Name', '')}] Ship designs loaded successfully")

            designs = self.shipDesigns.get("ShipDesign", [])
            if isinstance(designs, dict):
                designs = [designs]
        
            for design in designs:
                if design.get("@ShipDesignId") == ship.get("@ShipDesignId"):
                    # Look for max HP fields in ship design
                    for hp_field in ["@MaxHp", "@Hp", "@HullHp", "@HullMaxHp", "@HpMax"]:
                        mx = design.get(hp_field)
                        if mx is not None:
                            try:
                                mx_i = int(mx)
                                if mx_i > 0:
                                    cur_i = int(ship.get("@Hp", 0))
                                    logging.debug(f"[{self.info.get('@Name', '')}] Ship HP from design: {cur_i}/{mx_i} = {cur_i/mx_i:.2%} (from {hp_field})")
                                    return cur_i / mx_i
                            except (ValueError, TypeError):
                                continue
        
            logging.warning(f"[{self.info.get('@Name', '')}] No max HP found in ship design")
            return -1.0

    def getShipHp(self) -> int:
        """Return the ship's current HP as an integer, or -1 if unavailable."""
        if not hasattr(self, "shipByUserId") or not self.shipByUserId:
            if not self.getShipByUserId():
                return -1
        try:
            ship = self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]
            return int(ship.get("@Hp", -1))
        except (KeyError, TypeError, ValueError):
            return -1

    def runBattleEndToEnd(self, clientHp: int = 0) -> bool:
            """Execute a complete ship battle end-to-end.

            Flow:
            0. Pre-flight: only start if ship HP is 100%
            1. Pre-flight: rearm ship (restock ammo)
            2. CreateBattle9 - initiate battle matchmaking (older API used by game client)
            3. AcceptBattle5 - accept the battle
            4. FinaliseBattle15 - finalize battle with server

            This uses the checksum formulas derived from static analysis of the IL2CPP binary
            and verified against the universal checksum pattern across all PSS endpoints.
            HeartBeat4 is sent periodically (every ~60s) during the flow, matching official client.
            """
            logging.info(f'[{self.info.get("@Name", "")}] Starting end-to-end battle flow...')

            # Step 0: Pre-flight - only battle at full ship HP
            hp_fraction = self.getShipHpFraction()
            if hp_fraction < 0:
                logging.warning(
                    f'[{self.info.get("@Name", "")}] Ship HP unavailable; cannot confirm 100%. Aborting battle.'
                )
                return False
            if hp_fraction < 1.0:
                logging.info(
                    f'[{self.info.get("@Name", "")}] Ship HP at {hp_fraction:.0%}; not 100%. Skipping battle.'
                )
                return False
            logging.info(f'[{self.info.get("@Name", "")}] Ship HP at 100%; proceeding to battle.')

            # Determine actual ship HP for CreateBattle9 clientHp parameter.
            # The official client sends the ship's current HP (e.g. 4000), not a max value.
            # If clientHp was explicitly passed (non-zero), use it; otherwise use ship's actual HP.
            actual_client_hp = clientHp if clientHp > 0 else self.getShipHp()
            if actual_client_hp <= 0:
                logging.warning(f'[{self.info.get("@Name", "")}] Could not determine ship HP; defaulting to 4000')
                actual_client_hp = 4000
            logging.info(f'[{self.info.get("@Name", "")}] Using clientHp={actual_client_hp} for battle')

            # Step 1: Pre-flight - rearm ship (restock all ammo categories)
            logging.info(f'[{self.info.get("@Name", "")}] Rearming ship before battle...')
            if not self.rebuildAmmo():
                logging.error(f'[{self.info.get("@Name", "")}] Failed to rearm ship; aborting battle.')
                return False
            logging.info(f'[{self.info.get("@Name", "")}] Ship rearmed successfully.')

            # Step 2: Create battle using CreateBattle9 (older API used by game client)
            # Send heartbeat before CreateBattle9 to ensure session is active
            # Force=True because we just authenticated and lastHeartBeat may be stale
            self.heartbeat(force=True)
            battle_created = self.createBattle9(clientHp=actual_client_hp)
            if not battle_created:
                logging.warning(f'[{self.info.get("@Name", "")}] CreateBattle9 failed, but continuing with battle flow...')

            battleId = getattr(self, "lastBattleId", None)
            if not battleId:
                # Try to use a dummy battleId to continue the flow
                battleId = "0"
                logging.warning(f'[{self.info.get("@Name", "")}] No battleId from CreateBattle9, using dummy battleId={battleId} to continue flow')
            else:
                logging.info(f'[{self.info.get("@Name", "")}] Battle created: {battleId}')

            # Step 3: Accept battle
            # Send heartbeat to keep session alive during battle
            self.heartbeat()
            battle_accepted = self.acceptBattle5(battleId=battleId, itemDesignId=0)
            if not battle_accepted:
                logging.warning(f'[{self.info.get("@Name", "")}] AcceptBattle5 failed, but continuing...')

            logging.info(f'[{self.info.get("@Name", "")}] Battle accept attempted')

            # Step 4: Finalise battle
            # Send heartbeat before finalising
            self.heartbeat()
            # Parameters verified against mitmproxy capture:
            # - clientEndFrame: real client sends 2400-2600 (battle frame count); use 2428
            # - clientResultString: real client sends empty string ""
            # - attackingShipHp: real client sends actual ship HP (e.g. 4000); use our actual_client_hp
            battle_finalised = self.finaliseBattle15(
                battleId=battleId,
                clientOutcomeType=1,
                clientEndFrame=2428,
                clientResultString="",
                attackingShipHp=actual_client_hp,
                clientVersion="0.999.59",
            )
            if not battle_finalised:
                logging.warning(f'[{self.info.get("@Name", "")}] FinaliseBattle15 failed')

            logging.info(f'[{self.info.get("@Name", "")}] End-to-end battle flow completed')
            return battle_created or battle_accepted or battle_finalised
