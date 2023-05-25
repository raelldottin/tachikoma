import urllib.parse
import time
import datetime
import collections
import xmltodict
import requests
import random
import collections
import logging
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from requests.adapters import HTTPAdapter
from ratelimit import limits, sleep_and_retry
from sdk.device import Device
from .security import (
    ChecksumCreateDevice,
    ChecksumTimeForDate,
    ChecksumPasswordWithString,
    ChecksumEmailAuthorize,
)
from .dotnet import DotNet


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
        "Accept-Encoding": "identity",
        "User-Agent": "UnityPlayer/5.6.0f3 (UnityWebRequest/1.0, libcurl/7.51.0-DEV)",
        "X-Unity-Version": "5.6.0f3",
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
    dronesCollected = dict()
    dailyRewardArgument = 0
    credits = 0
    info = {"@Name": ""}
    user: User

    # tcp session, backoff timer, and rate limiter
    retry_strategy = Retry(
        total=10,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504, 520],
        method_whitelist=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    def __init__(self, device):
        self.device = device

    @sleep_and_retry
    @limits(calls=MAX_CALLS_PER_MINUTE, period=ONE_MINUTE)
    def request(self, url, method, data=None):
        r = self.session.request(method, url, headers=self.headers, data=data)

        if "errorMessage" in r.text:
            d = xmltodict.parse(r.content, xml_attribs=True)
            logging.error(f"[{self.info['@Name']}] {url} - {d}")

        if "Failed to authorize access token" in r.text:
            logging.info(
                f"[{self.info['@Name']}] Attempting to reauthorized access token."
            )
            self.user.isAuthorized = False
            self.quickReload()
            r = self.session.request(method, url, headers=self.headers, data=data)

        # heartbeat should run only to keep the connection alive
        # if hasattr(self, 'user') and divmod((datetime.datetime.utcnow() - self.user.lastHeartBeat).seconds, 60)[0] != 0:
        #    self.heartbeat()

        return r

    def parseUserLoginData(self, r):
        if "UserService" not in r.text:
            logging.error("Failed to login.")
            return False

        d = xmltodict.parse(r.content, xml_attribs=True)

        # heartbeat should only be sent after 60 seconds of network inactivity with the server
        # we need perform date comparison to verify that 60 seconds has not elapsed
        LastHeartBeat = datetime.datetime.strptime(
            d["UserService"]["UserLogin"]["User"]["@LastHeartBeatDate"],
            "%Y-%m-%dT%H:%M:%S",
        )
        self.info = d["UserService"]["UserLogin"]["User"]
        if "@Name" not in self.info:
            self.info["@Name"] = ""
        logging.info(f"[{self.info['@Name']}] Authenticated...")
        userId = d["UserService"]["UserLogin"]["@UserId"]
        if "@Credits" in d["UserService"]["UserLogin"]["User"]:
            self.credits = int(d["UserService"]["UserLogin"]["User"]["@Credits"])
        if "@DailyRewardStatus" in d["UserService"]["UserLogin"]["User"]:
            self.dailyReward = int(
                d["UserService"]["UserLogin"]["User"]["@DailyRewardStatus"]
            )
        else:
            self.dailyReward = 0

        if not self.device.refreshToken:
            myName = "guest"
        else:
            myName = d["UserService"]["UserLogin"]["User"]["@Name"]

        if "FreeStarbuxReceivedToday" in r.text:
            self.freeStarbuxToday = int(
                r.text.split('FreeStarbuxReceivedToday="')[1].split('"')[0]
            )

        # keep it
        # Store User details here.
        self.user = User(
            userId,
            myName,
            LastHeartBeat,
            self.device.refreshToken,
        )

        self.info = d["UserService"]["UserLogin"]["User"]
        self.credits = d["UserService"]["UserLogin"]["User"]["@Credits"]
        return True

    def getAccessToken(self):
        if self.accessToken:
            return self.accessToken

        self.checksum = ChecksumCreateDevice(self.device.key, self.device.name)

        url = f"{self.baseUrl}/UserService/DeviceLogin8?deviceKey={self.device.key}&advertisingKey=&isJailBroken=False&checksum={self.checksum}&deviceType=DeviceType{self.device.name}&signal=False&languageKey={self.device.languageKey}&refreshToken={self.device.refreshToken if self.device.refreshToken else ''}"

        r = self.request(url, "POST")
        d = xmltodict.parse(r.content, xml_attribs=True)
        if (
            (not r or r.status_code != 200)
            or ("errorCode" in r.text)
            or ("accessToken" not in r.text)
        ):
            logging.error(f"{d}")
            self.accessToken = ""
            return False

        self.accessToken = r.text.split('accessToken="')[1].split('"')[0]
        if not self.parseUserLoginData(r):
            return False

        return True

    def quickReload(self):
        self.accessToken = None
        self.getAccessToken()

    def login(self, email=None, password=None):
        if not self.getAccessToken():
            return False

        # double check if something goes wrong
        if not self.accessToken:
            return False

        # authorization just fine with refreshToken, we're in da house
        if self.device.refreshToken and self.accessToken:
            return True

        # accessToken is enough for guest to play a tutorial
        if self.accessToken and not email:
            return True

        # login with credentials and accessToken
        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        self.checksum = ChecksumEmailAuthorize(
            self.device.key, email, ts, self.accessToken, self.salt
        )

        # if refreshToken was used we get acquire session without credentials
        if self.device.refreshToken:
            url = f"{self.baseUrl}/UserService/UserEmailPasswordAuthorize2?clientDateTime={ts}&checksum={self.checksum}&deviceKey={self.device.key}&accessToken={self.accessToken}&refreshToken={self.device.refreshToken}"
            r = self.request(url, "POST")

            if "Email=" not in r.text:
                logging.error(
                    "[login] failed to authenticate with refreshToken:", r.text
                )
                return False

            if not self.parseUserLoginData(r):
                return False

        else:
            self.email = urllib.parse.quote(email)

            url = f"{self.baseUrl}/UserService/UserEmailPasswordAuthorize2?clientDateTime={ts}&checksum={self.checksum}&deviceKey={self.device.ke}&email={self.email}&password={password}&accessToken={self.accessToken}"
            r = self.request(url, "POST")

            if "errorMessage=" in r.text:
                logging.error(
                    "[login] failed to authorize with credentials with the reason:",
                    r.text,
                )
                return False

            if "refreshToken" not in r.text:
                logging.error(
                    "[login] failed to acquire refreshToken with the reason",
                    r.text,
                )
                return False

            self.device.refreshTokenAcquire(
                r.text.split('refreshToken="')[1].split('"')[0]
            )

            if 'RequireReload="True"' in r.text:
                return self.quickReload()

        if "refreshToken" in r.text:
            self.device.refreshTokenAcquire(
                r.text.split('refreshToken="')[1].split('"')[0]
            )

        return True

    def getLatestVersion3(self):
        url = f"https://api.pixelstarships.com/SettingService/GetLatestVersion3?languageKey={self.device.languageKey}&deviceType=DeviceType{self.device.name}"
        r = self.request(url, "GET")
        self.latestVersion = xmltodict.parse(r.content, xml_attribs=True)

    def getTodayLiveOps2(self):
        url = f"https://api.pixelstarships.com/LiveOpsService/GetTodayLiveOps2?languageKey={self.device.languageKey}&deviceType=DeviceType{self.device.name}"
        r = self.request(url, "GET")
        self.todayLiveOps = xmltodict.parse(r.content, xml_attribs=True)

    def listRoomDesigns2(self):
        url = f"https://api.pixelstarships.com/RoomService/ListRoomDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@RoomDesignVersion']}"
        r = self.request(url, "GET")
        self.roomDesigns = xmltodict.parse(r.content, xml_attribs=True)

    def listAllTaskDesigns2(self):
        url = f"https://api.pixelstarships.com/TaskService/ListAllTaskDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@RoomDesignVersion']}"
        r = self.request(url, "GET")
        self.allTaskDesigns = xmltodict.parse(r.content, xml_attribs=True)

    def getShipByUserId(self, userId=0):
        url = f"https://api.pixelstarships.com/ShipService/GetShipByUserId?userId={userId if userId else self.user.id}&accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.shipByUserId = xmltodict.parse(r.content, xml_attribs=True)
        if "ShipService" not in self.allCharactersOfUser:
            logging.error("CharacterService data not avaialble.")
            return False

    def listAchievementsOfAUser(self):
        url = f"https://api.pixelstarships.com/AchievementService/ListAchievementsOfAUser?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.achievementsOfAUser = xmltodict.parse(r.content, xml_attribs=True)

    def listImportantMessagesForUser(self):
        url = f"https://api.pixelstarships.com/MessageService/ListImportantMessagesForUser?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.importantMessagesForUser = xmltodict.parse(r.content, xml_attribs=True)

    def listUserStarSystems(self):
        url = f"https://api.pixelstarships.com/GalaxyService/ListUserStarSystems?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.userStarSystems = xmltodict.parse(r.content, xml_attribs=True)

    def listStarSystemMarkersAndUserMarkers(self):
        url = f"https://api.pixelstarships.com/GalaxyService/ListStarSystemMarkersAndUserMarkers?accessToken={self.accessToken}"
        r = self.request(url, "GET")
        self.starSystemMarkersAndUserMarkers = xmltodict.parse(
            r.content, xml_attribs=True
        )

    def listTasksOfAUser(self):
        url = f"https://api.pixelstarships.com/TaskService/ListTasksOfAUser?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.tasksOfAUser = xmltodict.parse(r.content, xml_attribs=True)

    def listCompletedMissionEvents(self):
        ts = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
        checksum = ChecksumEmailAuthorize(
            self.device.key,
            self.info["@Email"],
            ts,
            self.accessToken,
            self.salt,
        )
        url = f"https://api.pixelstarships.com/MissionService/ListCompletedMissionEvents?clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        self.completedMissionEvents = xmltodict.parse(r.content, xml_attribs=True)

    def listSituations(self):
        url = f"https://api.pixelstarships.com/SituationService/ListSituations?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.situations = xmltodict.parse(r.content, xml_attribs=True)

    def listPvPBattles2(self, take=25, skip=0):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/BattleService/ListPvPBattles2?take={take}&skip={skip}&accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.pvpBattles = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listMissionBattles(self, take=25, skip=0):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/BattleService/ListMissionBattles?take={take}&skip={skip}&accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.missionBattles = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listActionTypes2(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListActionTypes2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@ResearchDesignVersion']}"
            r = self.request(url, "GET")
            self.actionTypes = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listConditionTypes2(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListConditionTypes2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@ResearchDesignVersion']}"
            r = self.request(url, "GET")
            self.conditionTypes = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listAllResearches(self):
        url = f"https://api.pixelstarships.com/ResearchService/ListAllResearches?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.allResearches = xmltodict.parse(r.content, xml_attribs=True)

    def listItemsOfAShip(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/ItemService/ListItemsOfAShip?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.itemsOfAShip = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listRoomsViaAccessToken(self):
        url = f"https://api.pixelstarships.com/RoomService/ListRoomsViaAccessToken?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.roomsViaAccessToken = xmltodict.parse(r.content, xml_attribs=True)

    def listAllCharactersOfUser(self):
        url = f"https://api.pixelstarships.com/CharacterService/ListAllCharactersOfUser?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.allCharactersOfUser = xmltodict.parse(r.content, xml_attribs=True)
        if "CharacterService" not in self.allCharactersOfUser:
            logging.error(f"Failed to get list of characters on the ship.")
            return False
        return True

    def getRoomName(self, roomDesignId):
        if not hasattr(self, "roomDesigns"):
            self.listAllDesigns4()
            if "RoomDesign" not in self.roomDesigns:
                self.roomName = ""
                return False

        design = {}
        for design in self.roomDesigns["RoomDesign"]:
            if roomDesignId == design["@RoomDesignId"]:
                self.roomName = "".join(design["@RoomName"])
                return True

        self.roomName = ""
        return False

    def finishTraining(self, characterId):
        url = f"{self.baseUrl}/TrainingService/FinishTraining?characterId={characterId}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if "errorMessage" in r.text:
            return False

        return True

    def getTrainingUpdate(self, characterId):
        url = f"{self.baseUrl}/TrainingService/GetTrainingUpdate?characterId={characterId}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if "errorMessage" in r.text:
            return False
        self.trainingUpdate = xmltodict.parse(r.content, xml_attribs=True)
        return True

    def listAllDesigns4(self):
        if not self.latestVersion:
            self.getLatestVersion3()
        if "SettingService" not in self.latestVersion:
            return False
        versions = self.latestVersion["SettingService"]["GetLatestSetting"]["Setting"]
        url = f"{self.baseUrl}/DesignService/ListAllDesigns4?LanguageKey=en&ListFileVersion={versions['@FileVersion']}&ListSpriteVersion={versions['@SpriteVersion']}&ListBackgroundVersion={versions['@BackgroundVersion']}&ListAllShipDesignVersion={versions['@ShipDesignVersion']}&ListRoomDesignVersion={versions['@RoomDesignVersion']}&ListAllCharacterDesignVersion={versions['@CharacterDesignVersion']}&ListAllCharacterDesignActionVersion={versions['@CharacterDesignActionVersion']}&ListItemDesignVersion={versions['@ItemDesignVersion']}&ListCraftDesignVersion={versions['@CraftDesignVersion']}&ListMissileDesignVersion={versions['@MissileDesignVersion']}&ListStarSystemVersion={versions['@StarSystemVersion']}&ListStarSystemLinkVersion={versions['@StarSystemLinkVersion']}&ListAllNewsDesignVersion={versions['@NewsDesignVersion']}&ListLeagueVersion={versions['@LeagueVersion']}&ListAchievementDesignVersion={versions['@AchievementDesignVersion']}&ListRoomDesignPurchaseVersion={versions['@RoomDesignPurchaseVersion']}&ListRoomDesignSpriteVersion={versions['@RoomDesignSpriteVersion']}&ListAllMissionDesignVersion={versions['@MissionDesignVersion']}&ListAnimationVersion={versions['@AnimationVersion']}&ListAllResearchDesignVersion={versions['@ResearchDesignVersion']}&ListAllTrainingDesignVersion={versions['@TrainingDesignVersion']}&ListAllChallengeDesignVersion={versions['@ChallengeDesignVersion']}&ListAllRewardDesignVersion={versions['@RewardDesignVersion']}&ListAllDivisionDesignVersion={versions['@DivisionDesignVersion']}&ListAllCollectionDesignVersion={versions['@CollectionDesignVersion']}&ListAllDrawDesignVersion={versions['@DrawDesignVersion']}&ListAllPromotionDesignVersion={versions['@PromotionDesignVersion']}&ListAllSituationDesignVersion={versions['@SituationDesignVersion']}&ListAllTaskDesignVersion={versions['@TaskDesignVersion']}&ListActionTypeVersion={versions['@ActionTypeVersion']}&ListConditionTypeVersion={versions['@ConditionTypeVersion']}&ListItemDesignActionVersion={versions['@ItemDesignActionVersion']}&ListSeasonDesignVersion={versions['@SeasonDesignVersion']}&ListAssetVersion={versions['@AssetVersion']}&ListMarkerGeneratorDesignVersion={versions['@MarkerGeneratorDesignVersion']}"
        r = self.request(url, "GET")
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
        self.sprites = allDesignVersion["DesignService"]["ListAllDesigns"]["Sprites"]
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
        self.leagues = allDesignVersion["DesignService"]["ListAllDesigns"]["Leagues"]
        self.achievementDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
            "AchievementDesigns"
        ]
        self.roomDesignPurchases = allDesignVersion["DesignService"]["ListAllDesigns"][
            "RoomDesignPurchases"
        ]
        self.roomDesignSprites = allDesignVersion["DesignService"]["ListAllDesigns"][
            "RoomDesignSprites"
        ]
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
        self.collectionDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
            "CollectionDesigns"
        ]
        self.drawDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
            "DrawDesigns"
        ]
        self.promotionDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
            "PromotionDesigns"
        ]
        self.situationDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
            "SituationDesigns"
        ]
        self.itemDesignActions = allDesignVersion["DesignService"]["ListAllDesigns"][
            "ItemDesignActions"
        ]
        self.seasonDesigns = allDesignVersion["DesignService"]["ListAllDesigns"][
            "SeasonDesigns"
        ]
        self.assets = allDesignVersion["DesignService"]["ListAllDesigns"]["Assets"]
        self.starSystemMarkerGenerators = allDesignVersion["DesignService"][
            "ListAllDesigns"
        ]["StarSystemMarkerGenerators"]
        return True

    def addTraining(self, trainingDesignId, characterId):
        url = f"{self.baseUrl}/TrainingService/AddTraining?trainingDesignId={trainingDesignId}&characterId={characterId}&trainingStartDate={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if "errorMessage" in r.text:
            return False
        return True

    def manageTraining(self):
        if not hasattr(self, "allCharactersOfUser"):
            if not self.listAllCharactersOfUser():
                return False

        if not hasattr(self, "allCharacterDesigns"):
            self.listAllCharacterDesigns2()
            if "CharacterService" not in self.allCharactersOfUser:
                logging.error("CharacterService data not avaialble.")
                return False

        if not hasattr(self, "roomsViaAccessToken"):
            self.listRoomsViaAccessToken()
            if "RoomService" not in self.roomsViaAccessToken:
                logging.error("RoomService data not available.")
                return False

        if not hasattr(self, "trainingDesigns"):
            self.listAllDesigns4()
            if "TrainingDesign" not in self.trainingDesigns:
                logging.error("TrainingDesign data not available.")
                return False

        characterAbilities = ["ProtectRoom", "Freeze"]
        fatigueMax = 1
        for character in self.allCharactersOfUser["CharacterService"][
            "ListAllCharactersOfUser"
        ]["Characters"]["Character"]:
            room = {}
            design = {}
            trainingName = ""
            for room in self.roomsViaAccessToken["RoomService"][
                "ListRoomsViaAccessToken"
            ]["Rooms"]["Room"]:
                if character["@RoomId"] == room["@RoomId"]:
                    break
            self.getRoomName(room["@RoomDesignId"])
            if "Academy" in self.roomName or "GYM" in self.roomName:
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

                logging.debug(f"{character['@TrainingEndDate']=}")
                if character["@TrainingEndDate"]:
                    trainingEndDate = datetime.datetime.strptime(
                        character["@TrainingEndDate"], "%Y-%m-%dT%H:%M:%S"
                    )

                logging.debug(
                    f"Total: {count=} {characterDesign['@TrainingCapacity']=} {count / int(characterDesign['@TrainingCapacity']) * 100}"
                )
                percent = count / int(characterDesign["@TrainingCapacity"]) * 100
                trainingDesignId = ""
                design = {}
                if trainingEndDate < datetime.datetime.utcnow() or (
                    trainingEndDate
                    < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                ):
                    if self.finishTraining(character["@CharacterId"]):
                        logging.debug(
                            f"[{self.info['@Name']}] Completed training for {character['@CharacterName']} in {self.roomName} with ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue, and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                        )
                    logging.warning(
                        f"[{self.info['@Name']}] {character['@CharacterName']} has {percent} training percentage in {self.roomName} with ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue, and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )
                if percent < 1 and (
                    int(character["@Fatigue"]) < fatigueMax
                    or trainingEndDate
                    < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                ):
                    if characterDesign["@SpecialAbilityType"] in characterAbilities:
                        trainingName = "Read Expert Weapon Theory"
                    elif characterDesign["@SpecialAbilityType"] == "AddReload":
                        trainingName = "Steam Yoga"
                    else:
                        trainingName = "Read Expert Weapon Theory"

                    logging.debug(
                        f"[{self.info['@Name']}] Use Blue (T2) primary training for {character['@CharacterName']} in {self.roomName} with {character['@Fatigue']} fatigue and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )

                elif (
                    percent > 0
                    and percent < 51
                    and (
                        int(character["@Fatigue"]) < fatigueMax
                        or trainingEndDate
                        < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                    )
                    and trainingEndDate < datetime.datetime.utcnow()
                ):
                    if characterDesign["@SpecialAbilityType"] in characterAbilities:
                        trainingName = "Read Expert Weapon Theory"
                    elif characterDesign["@SpecialAbilityType"] == "AddReload":
                        trainingName = "Steam Yoga"
                    else:
                        trainingName = "Read Expert Weapon Theory"

                    logging.debug(
                        f"[{self.info['@Name']}] Use Green (T1) {trainingName} primary training for {character['@CharacterName']} in {self.roomName} with ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue, {trainingEndDate < datetime.datetime.utcnow()} time logic, and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )
                    logging.debug(
                        f"\n{datetime.datetime.utcnow()=}\n{trainingEndDate=}\n{(datetime.datetime.utcnow() - trainingEndDate).seconds=}\n{(trainingEndDate - datetime.datetime.utcnow()).seconds=}"
                    )
                elif (
                    percent > 50
                    and percent < 65
                    and (
                        int(character["@Fatigue"]) < fatigueMax
                        or trainingEndDate
                        < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                    )
                    and trainingEndDate < datetime.datetime.utcnow()
                ):
                    if characterDesign["@SpecialAbilityType"] in characterAbilities:
                        trainingName = "Weapons Summit"
                    elif characterDesign["@SpecialAbilityType"] == "AddReload":
                        trainingName = "Crew vs Wild"
                    else:
                        trainingName = "Weapons Summit"
                    logging.debug(
                        f"[{self.info['@Name']}] Use Blue (T2) primary training for {character['@CharacterName']} in {self.roomName} with {character['@Fatigue']} fatigue and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )
                elif (
                    percent > 64
                    and percent < 72
                    and (
                        int(character["@Fatigue"]) < fatigueMax
                        or trainingEndDate
                        < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                    )
                    and trainingEndDate < datetime.datetime.utcnow()
                ):
                    if characterDesign["@SpecialAbilityType"] in characterAbilities:
                        trainingName = "Weapons PHD"
                    elif characterDesign["@SpecialAbilityType"] == "AddReload":
                        trainingName = "Space Marine"
                    else:
                        trainingName = "Weapons PHD"
                    logging.debug(
                        f"[{self.info['@Name']}] Use Yellow (T3) primary training for {character['@CharacterName']} in {self.roomName} with {character['@Fatigue']} fatigue and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )
                elif (
                    percent > 71
                    and percent < 74
                    and (
                        int(character["@Fatigue"]) < fatigueMax
                        or trainingEndDate
                        < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                    )
                    and trainingEndDate < datetime.datetime.utcnow()
                ):
                    if characterDesign["@SpecialAbilityType"] in characterAbilities:
                        trainingName = "Bench Press"
                    elif characterDesign["@SpecialAbilityType"] == "AddReload":
                        trainingName = "Bench Press"
                    logging.debug(
                        f"[{self.info['@Name']}] Use Green (T1) secondary training for {character['@CharacterName']} in {self.roomName} with {character['@Fatigue']} fatigue and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )
                elif (
                    percent > 73
                    and percent < 85
                    and (
                        (
                            int(character["@Fatigue"]) < fatigueMax
                            or trainingEndDate
                            < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                        )
                        and trainingEndDate < datetime.datetime.utcnow()
                    )
                ):
                    if characterDesign["@SpecialAbilityType"] in characterAbilities:
                        trainingName = "Muscle Beach"
                    elif characterDesign["@SpecialAbilityType"] == "AddReload":
                        trainingName = "Muscle Beach"
                    else:
                        trainingName = "Muscle Beach"
                    logging.debug(
                        f"[{self.info['@Name']}] Use Blue (T2) secondary training for {character['@CharacterName']} in {self.roomName} with {character['@Fatigue']} fatigue and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )
                elif (
                    percent > 84
                    and percent < 90
                    and (
                        int(character["@Fatigue"]) < fatigueMax
                        or trainingEndDate
                        < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                    )
                    and trainingEndDate < datetime.datetime.utcnow()
                ):
                    if characterDesign["@SpecialAbilityType"] in characterAbilities:
                        trainingName = "Olympic Weightlifting"
                    elif characterDesign["@SpecialAbilityType"] == "AddReload":
                        trainingName = "Olympic Weightlifting"
                    else:
                        trainingName = "Olympic Weightlifting"
                    logging.debug(
                        f"[{self.info['@Name']}] Use Yellow (T3) secondary training for {character['@CharacterName']} in {self.roomName} with {character['@Fatigue']} fatigue and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )

                logging.debug(
                    f"[{self.info['@Name']}] {character['@CharacterName']} in {self.roomName} with ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue, and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                )
                logging.debug(
                    f"[{self.info['@Name']}] {'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())=}\n{character=}\n{character['@CharacterName']} in {self.roomName} with ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue, and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                )

                if (
                    trainingEndDate < datetime.datetime.utcnow()
                    and (
                        int(character["@Fatigue"]) < fatigueMax
                        or trainingEndDate
                        < datetime.datetime.utcnow() - datetime.timedelta(days=1)
                    )
                    and trainingName
                ):
                    for design in self.trainingDesigns["TrainingDesign"]:
                        if design["@TrainingName"] == trainingName:
                            trainingDesignId = design["@TrainingDesignId"]
                            break

                    logging.debug(
                        f"[{self.info['@Name']}] {character['@CharacterName']} in {self.roomName} with ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue, and {(datetime.datetime.utcnow() - trainingEndDate).seconds} seconds to complete training."
                    )
                    if self.addTraining(trainingDesignId, character["@CharacterId"]):
                        logging.info(
                            f"[{self.info['@Name']}] Starting training {design['@TrainingName']} for {character['@CharacterName']} in {self.roomName} with ability {characterDesign['@SpecialAbilityType']}, {character['@Fatigue']} fatigue."
                        )

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

    def listAllCharacterDesigns2(self):
        if self.latestVersion:
            url = f"{self.baseUrl}/CharacterService/ListAllCharacterDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@ResearchDesignVersion']}"
            r = self.request(url, "GET")
            self.allCharacterDesigns = xmltodict.parse(r.content, xml_attribs=True)
            if "CharacterService" not in self.allCharacterDesigns:
                logging.error(
                    f"[{self.info['@Name']}] CharacterService data not avaialble."
                )
                return False
            return True
        return False

    def upgradeCharacter(self, characterId):
        url = f"{self.baseUrl}/CharacterService/UpgradeCharacter?characterId={characterId}&accessToken={self.accessToken}"
        self.request(url, "POST")

    def upgradeCharacters(self):
        character_names = []

        if not self.allCharactersOfUser:
            self.listAllCharactersOfUser()

        if not hasattr(self, "itemsOfAShip"):
            self.listItemsOfAShip()

        if "CharacterService" not in self.allCharactersOfUser:
            return False

        if not hasattr(self, "allCharacterDesigns"):
            self.listAllCharacterDesigns2()

        for character in self.allCharactersOfUser["CharacterService"][
            "ListAllCharactersOfUser"
        ]["Characters"]["Character"]:
            # if character['@Level'] == '40':
            #    for characterDesign in self.allCharacterDesigns['CharacterService']['ListAllCharacterDesigns']['CharacterDesigns']['CharacterDesign']:
            #        if character['@CharacterDesignId'] == characterDesign['@CharacterDesignId']:
            #            logging.warn(f"{character['@CharacterName']=} {character['@Level']=} {character['@Xp']=} {characterDesign['@Rarity']=}")

            if character["@RoomId"] != "0" and character["@Level"] != "40":
                for characterDesign in self.allCharacterDesigns["CharacterService"][
                    "ListAllCharacterDesigns"
                ]["CharacterDesigns"]["CharacterDesign"]:
                    if (
                        character["@CharacterDesignId"]
                        == characterDesign["@CharacterDesignId"]
                    ):
                        character_names.append(character["@CharacterName"])
                        logging.info(
                            f"[{self.info['@Name']}] Upgrading {character['@CharacterName']} to level {int(character['@Level']) + 1}."
                        )
                        self.upgradeCharacter(character["@CharacterId"])
                        # logging.warn(f"{character['@CharacterName']=} {character['@Level']=} {character['@Xp']=} {characterDesign['@Rarity']=}")

        if character_names:
            logging.info(
                f"[{self.info['@Name']}] The following characters are below level 40: {' ,'.join(character_names)}"
            )

    def listAllRoomActionsOfShip(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListAllRoomActionsOfShip?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.allRoomActionsOfShip = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def pusherAuth(self):
        url = f"https://api.pixelstarships.com/UserService/PusherAuth?accessToken={self.accessToken}"
        self.request(url, "POST")

    def listSystemMessagesForUser3(self, fromMessageId=0, take=10000):
        url = f"https://api.pixelstarships.com/MessageService/ListSystemMessagesForUser3?fromMessageId={fromMessageId}&take={take}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        self.systemMessagesForUser = xmltodict.parse(r.content, xml_attribs=True)
        if "MessageService" not in self.systemMessagesForUser:
            logging.error("MessageService data unavailable.")
            return False

        return True

    def listFriends(self, userId=0):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/UserService/ListFriends?UserId={userId if userId else self.info['@Id']}&accessToken={self.accessToken}"
            logging.debug(url)
            r = self.request(url, "POST")
            self.systemMessagesForUser = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listMessagesForChannelKey(self, channelKey="alliance-43958"):
        url = f"https://api.pixelstarships.com/MessageService/ListMessagesForChannelKey?channelKey=channelKey={channelKey}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        self.messagesForChannelKey = xmltodict.parse(r.content, xml_attribs=True)
        # Perform error handling and return values based on the results
        # return True
        # return False

    def findUserRanking(self):
        url = f"https://api.pixelstarships.com/LadderService/FindUserRanking?accessToken={self.accessToken}"
        r = self.request(url, "GET")
        self.userRanking = xmltodict.parse(r.content, xml_attribs=True)

    def activateItem3(self, itemId=0, targetId=0):
        url = f"https://api.pixelstarships.com/ItemService/ActivateItem3?itemId={itemId}&targetId={targetId}&"
        r = self.request(url, "POST")
        self.item = xmltodict.parse(r.content, xml_attribs=True)

    def print_market_data(self, v):
        message = "".join(v["@Message"])
        currency = v["@ActivityArgument"].split(":")[0]
        price = v["@ActivityArgument"].split(":")[1]
        logging.info(f"[{self.info['@Name']}] {message} for {price} {currency}.")

    def listActiveMarketplaceMessages(self):
        if self.user.isAuthorized:
            url = "https://api.pixelstarships.com/MessageService/ListActiveMarketplaceMessages5?itemSubType=None&rarity=None&currencyType=Unknown&itemDesignId=0&userId={}&accessToken={}".format(
                self.user.id, self.accessToken
            )
            r = self.request(url, "GET")
            d = xmltodict.parse(r.content, xml_attribs=True)
            if "errorMessage=" in r.text:
                logging.error(f"An error occurred: {r.text}.")
                return False
            if d["MessageService"]["ListActiveMarketplaceMessages"]["Messages"] == None:
                logging.debug(
                    f'[{self.info["@Name"]}] You have no items listed on the marketplace.'
                )
                return False

            for v in d["MessageService"]["ListActiveMarketplaceMessages"][
                "Messages"
            ].values():
                if isinstance(v, dict):
                    self.print_market_data(v)
                elif isinstance(v, list):
                    for i in v:
                        if isinstance(i, dict):
                            self.print_market_data(i)
            return True

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
        r = self.request(url, "POST")
        d = xmltodict.parse(r.content, xml_attribs=True)
        if "RoomService" not in d:
            return False
        self.mineralTotal = d["RoomService"]["CollectResources"]["Items"]["Item"][0][
            "@Quantity"
        ]
        self.gasTotal = d["RoomService"]["CollectResources"]["Items"]["Item"][1][
            "@Quantity"
        ]

        if "User" in d["RoomService"]["CollectResources"]:
            self.credits = d["RoomService"]["CollectResources"]["User"]["@Credits"]

        self.rssCollectedTimestamp = time.time()

    def getResourceTotals(self):
        logging.info(
            f'[{self.info["@Name"]}] There is a total of {self.mineralTotal} minerals on your ship.'
        )
        logging.info(
            f'[{self.info["@Name"]}] There is a total of {self.gasTotal} gas on your ship.'
        )

    def collectDailyReward(self):
        self.dailyRewardArgument = self.todayLiveOps["LiveOpsService"][
            "GetTodayLiveOps"
        ]["LiveOps"]["@DailyRewardArgument"]
        if datetime.datetime.now().time() == datetime.time(
            hour=0, minute=0, tzinfo=datetime.timezone.utc
        ):
            self.dailyReward = 0

        if self.user.isAuthorized and not self.dailyReward:
            url = "https://api.pixelstarships.com/UserService/CollectDailyReward2?dailyRewardStatus=Box&argument={}&accessToken={}".format(
                self.dailyRewardArgument,
                self.accessToken,
            )

            r = self.request(url, "POST")

            if "You already collected this reward" in r.text:
                self.dailyRewardTimestamp = time.time()
                self.dailyReward = 1
                logging.info(
                    f"[{self.info['@Name']}] You have already collected the daily reward from the dropship."
                )

            logging.info(
                f"[{self.info['@Name']}] You have collected the daily reward from the dropship."
            )
            return True
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
            if "UserService" not in self.starbux:
                self.quickReload()
                return False
            self.freeStarbuxToday = int(
                self.starbux["UserService"]["AddStarbux"]["User"][
                    "@FreeStarbuxReceivedToday"
                ]
            )

            logging.info(
                f'[{self.info["@Name"]}] You have collected a total of {self.freeStarbuxToday} starbux today.'
            )
            self.freeStarbuxTodayTimestamp = time.time()

            return True
        return False

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
                if "@errorMessage" in r.text:
                    logging.info(
                        f"[{self.info['@Name']}] Failed to speed up research for {''.join(i['@ResearchName'])}."
                    )
                    return False
                else:
                    logging.info(
                        f"[{self.info['@Name']}] Speeding up research for {''.join(i['@ResearchName'])}."
                    )
                    return True
        return False

    # Determine the boost gauge before attempting to speed up a room
    def speedUpRoomConstructionUsingBoostGauge(self, roomId, roomDesignId):
        if not self.roomDesigns:
            self.listAllDesigns4()

        for i in self.roomDesigns["RoomDesign"]:
            if i["@RoomDesignId"] == roomDesignId:
                url = f"https://api.pixelstarships.com/RoomService/SpeedUpRoomConstructionUsingBoostGauge?roomId={roomId}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}&accessToken={self.accessToken}"
                r = self.request(url, "POST")
                if "errorMessage" in r.text:
                    logging.info(
                        f"[{self.info['@Name']}] Failed to speed contruction for {''.join(i['@RoomName'])}."
                    )
                    return False
                else:
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
        for research in self.allResearches["ResearchService"]["ListAllResearches"][
            "Researches"
        ]["Research"]:
            for design in self.allResearchDesigns["ResearchService"][
                "ListAllResearchDesigns"
            ]["ResearchDesigns"]["ResearchDesign"]:
                if (
                    research["@ResearchDesignId"] == design["@ResearchDesignId"]
                    and design["@ResearchDesignId"] not in designExceptionList
                ):
                    if research["@ResearchState"] == "Researching":
                        logging.info(
                            f"[{self.info['@Name']}] {''.join(design['@ResearchName'])} is currently being researched."
                        )
                        researchingFlag = True
                    designExceptionList.append(design["@ResearchDesignId"])
        for design in self.allResearchDesigns["ResearchService"][
            "ListAllResearchDesigns"
        ]["ResearchDesigns"]["ResearchDesign"]:
            if (
                design["@ResearchDesignId"] not in designExceptionList
                and design["@RootResearchDesignId"] not in rootDesignExceptionList
            ):
                rootDesigns[design["@RootResearchDesignId"]].append(design)
                upgradeList.append(
                    [
                        design["@ResearchDesignId"],
                        design["@GasCost"],
                        design["@StarbuxCost"],
                        design["@ResearchName"],
                    ]
                )
                rootDesignExceptionList.append(design["@RootResearchDesignId"])
        self.collectAllResources()
        if not researchingFlag:
            for researchItem in upgradeList:
                if int(researchItem[1]) > 0 and int(researchItem[1]) < int(
                    self.gasTotal
                ):
                    if self.addResearch(researchItem[0]):
                        logging.info(
                            f"[{self.info['@Name']}] Beginning research for {researchItem[3]}"
                        )
                        researchingFlag = True
                        break

    def upgradeRooms(self):
        if not self.roomDesigns:
            self.listRoomDesigns2()
        roomDesigns = self.roomDesigns
        self.listUpgradingRooms()
        self.getShipByUserId()
        shipByUserId = self.shipByUserId
        if shipByUserId:
            for room in shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Rooms"][
                "Room"
            ]:
                roomId = room["@RoomId"]
                roomStatus = room["@RoomStatus"]
                roomDesignId = room["@RoomDesignId"]
                roomName = ""
                upgradeRoomDesignId = ""
                upgradeRoomName = ""

                for roomDesignData in roomDesigns["RoomDesign"]:
                    if roomDesignId == roomDesignData["@RoomDesignId"]:
                        roomName = "".join(roomDesignData["@RoomName"])
                    if roomDesignId == roomDesignData["@UpgradeFromRoomDesignId"]:
                        upgradeRoomDesignId = roomDesignData["@RoomDesignId"]
                        upgradeRoomName = "".join(roomDesignData["@RoomName"])
                        cost = roomDesignData["@PriceString"].split(":")
                        if (cost[0] == "mineral") and (
                            int(cost[1]) > int(self.mineralTotal)
                        ):
                            continue

                        if (cost[0] == "gas") and (int(cost[1]) > int(self.gasTotal)):
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
                            if "concurrent" in r.text:
                                logging.info(
                                    f'[{self.info["@Name"]}] You have reached the maximum number of concurrent constructions allowed.'
                                )
                                break
                            self.collectAllResources()
        return True

    def listUpgradingRooms(self):
        self.getShipByUserId()
        shipByUserId = self.shipByUserId
        roomDesigns = self.roomDesigns
        if shipByUserId and roomDesigns:
            if "ShipService" not in shipByUserId:
                logging.debug(f"{shipByUserId=}")
            for room in shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Rooms"][
                "Room"
            ]:
                if room["@RoomStatus"] == "Upgrading":
                    for roomDesignData in roomDesigns["RoomDesign"]:
                        if room["@RoomDesignId"] == roomDesignData["@RoomDesignId"]:
                            logging.info(
                                f"[{self.info['@Name']}] {''.join(roomDesignData['@RoomName'])} is currently being upgraded."
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
        if "errorMessage" in r.text:
            return False
        else:
            return True

    def rebuildAmmo(self):
        self.clientDateTime = "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime())
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
            checksum = ChecksumEmailAuthorize(
                self.device.key,
                self.info["@Email"],
                ts,
                self.accessToken,
                self.checksum,
            )
            url = f"http://api.pixelstarships.com/RoomService/RebuildAmmo2?ammoCategory={ammoCategory}&clientDateTime={ts}&checksum={checksum}&accessToken={self.accessToken}"
            logging.debug(f"{url=}")
            self.request(url, "POST")
            return True

    def getCrewInfo(self):
        character_list = []
        fatigue_characters = []
        self.listAllCharactersOfUser()

        for character in self.allCharactersOfUser["CharacterService"][
            "ListAllCharactersOfUser"
        ]["Characters"]["Character"]:
            character_list.append(character["@CharacterName"])
            if int(character["@Fatigue"]) > 0:
                fatigue_characters.append(
                    "".join(
                        [
                            character["@CharacterName"],
                            " has ",
                            character["@Fatigue"],
                            " fatigue",
                        ]
                    )
                )
        if character_list:
            logging.info(
                f"[{self.info['@Name']}] List of characters on your ship: {', '.join(character_list)}"
            )
        if fatigue_characters:
            logging.info(
                f"[{self.info['@Name']}] List ot fatigue characters on your ship: {', '.join(fatigue_characters)}."
            )
        return True

    def getMessages(self):
        if not self.listSystemMessagesForUser3():
            return False
        if not self.systemMessagesForUser["MessageService"][
            "ListSystemMessagesForUser"
        ]["Messages"]:
            return True
        elif isinstance(
            self.systemMessagesForUser["MessageService"]["ListSystemMessagesForUser"][
                "Messages"
            ]["Message"],
            dict,
        ):
            message = self.systemMessagesForUser["MessageService"][
                "ListSystemMessagesForUser"
            ]["Messages"]["Message"]
            if (
                "@ActivityArgument" in message
                and message["@ActivityArgument"] != "None"
                and message["@ActivityArgument"] != ""
            ):
                logging.info(
                    f"[{self.info['@Name']}] {message['@Message']}{''.join([' ', message['@ActivityArgument'].split(':')[1]])}{''.join([' ', message['@ActivityArgument'].split(':')[0]])} is collectable."
                )
                if message["@ActivityArgument"].split(":")[0] not in [
                    "gas",
                    "mineral",
                ]:
                    self.collectReward2(message["@MessageId"])
            else:
                logging.info(f"[{self.info['@Name']}] {message['@Message']}")
        elif isinstance(
            self.systemMessagesForUser["MessageService"]["ListSystemMessagesForUser"][
                "Messages"
            ]["Message"],
            list,
        ):
            for message in self.systemMessagesForUser["MessageService"][
                "ListSystemMessagesForUser"
            ]["Messages"]["Message"]:
                if (
                    message["@ActivityArgument"] != "None"
                    and message["@ActivityArgument"] != ""
                ):
                    logging.info(
                        f"[{self.info['@Name']}] {message['@Message']}{''.join([' ', message['@ActivityArgument'].split(':')[1]])}{''.join([' ', message['@ActivityArgument'].split(':')[0]])} is collectable."
                    )
                    if message["@ActivityArgument"].split(":")[0] not in [
                        "gas",
                        "mineral",
                    ]:
                        self.collectReward2(message["@MessageId"])
                else:
                    self.collectReward2(message["@MessageId"])
                    logging.info(f"[{self.info['@Name']}] {message['@Message']}")
        return True

    def listFinishTasks(self):
        self.listTasksOfAUser()
        self.listAllTaskDesigns2()
        for task in self.tasksOfAUser["TaskService"]["ListTasksOfAUser"]["Tasks"][
            "Task"
        ]:
            logging.debug(f"{task=}")
            if task["@Collected"] == "true":
                for taskDesign in self.allTaskDesigns["TaskService"][
                    "ListAllTaskDesigns"
                ]["TaskDesigns"]["TaskDesign"]:
                    if taskDesign["@TaskDesignId"] == task["@TaskDesignId"]:
                        logging.info(
                            f"[{self.info['@Name']}] Completed task to {taskDesign['@Description']}."
                        )

    def collectTaskCompletion(self, taskDesignId):
        url = f"{self.baseUrl}/TaskService/CollectTaskCompletion?taskDesignId={taskDesignId}&accessToken={self.accessToken}"
        r = self.request(url, "POST")
        if "errorMessage" in r.text:
            return False
        else:
            return True

    def collectTaskReward(self):
        self.listTasksOfAUser()
        self.listAllTaskDesigns2()
        for task in self.tasksOfAUser["TaskService"]["ListTasksOfAUser"]["Tasks"][
            "Task"
        ]:
            if task["@Collected"] == "false" and task["@ProgressValue"] != "0":
                for taskDesign in self.allTaskDesigns["TaskService"][
                    "ListAllTaskDesigns"
                ]["TaskDesigns"]["TaskDesign"]:
                    if (
                        taskDesign["@TaskDesignId"] == task["@TaskDesignId"]
                        and taskDesign["@ObjectiveAmount"] == task["@ProgressValue"]
                    ):
                        logging.debug(f"{task=}\n{taskDesign=}")
                        if self.collectTaskCompletion(task["@TaskDesignId"]):
                            logging.info(
                                f"[{self.info['@Name']}] Collecting reward for objective: {taskDesign['@Name']}."
                            )

    def heartbeat(self):
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
