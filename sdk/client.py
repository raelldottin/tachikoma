import urllib.parse
import time
import datetime
import sys
import collections
import xmltodict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from requests.adapters import HTTPAdapter
from ratelimit import limits, sleep_and_retry
from .security import (
    ChecksumCreateDevice,
    ChecksumTimeForDate,
    ChecksumPasswordWithString,
    ChecksumEmailAuthorize,
)
from .dotnet import DotNet
from sdk.device import Device


DEFAULT_TIMEOUT = 5  # seconds
ONE_MINUTE = 60
MAX_CALLS_PER_MINUTE = 5


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
    lastHeartBeat = "" 
    clientDateTime = 0

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
    baseUrl = "https://api.pixelstarships.com/UserService/"

    # runtime data
    accessToken = None
    checksum = None
    freeStarbuxToday = 0
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

    @ sleep_and_retry
    @ limits(calls=MAX_CALLS_PER_MINUTE, period=ONE_MINUTE)
    def request(self, url, method="", data=None):
        r = self.session.request(method, url, headers=self.headers, data=data)

        if "errorMessage" in r.text:
            d = xmltodict.parse(r.content, xml_attribs=True)
            print(f"[{self.info['@Name']}] {d}")


        if "Failed to authorize access token" in r.text:
            print(f"[{self.info['@Name']}] Attempting to reauthorized access token.")
            self.quickReload()
            r = self.session.request(
                method, url, headers=self.headers, data=data)

        return r

    def parseUserLoginData(self, r):

        d = xmltodict.parse(r.content, xml_attribs=True)

        self.info = d["UserService"]["UserLogin"]["User"]
        userId = d["UserService"]["UserLogin"]["@UserId"]
        if "@Credits" in d["UserService"]["UserLogin"]["User"]:
            self.credits = int(
                d["UserService"]["UserLogin"]["User"]["@Credits"])
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
        LastHeartBeat = d["UserService"]["UserLogin"]["User"]["@LastHeartBeatDate"]

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

    def getAccessToken(self):

        if self.accessToken:
            return self.accessToken

        self.checksum = ChecksumCreateDevice(self.device.key, self.device.name)

        url = (
            self.baseUrl
            + "DeviceLogin8?deviceKey="
            + self.device.key
            + "&advertisingKey=&isJailBroken=False&checksum="
            + self.checksum
            + "&deviceType=DeviceType"
            + self.device.name
            + "&signal=False&languageKey="
            + self.device.languageKey
        )
        url += "&refreshToken=" + (
            self.device.refreshToken if self.device.refreshToken else ""
        )

        r = self.request(url, "POST")
        if not r or r.status_code != 200:
            print("[getAccessToken]", "failed with data:", r.text)
            return None

        if "errorCode" in r.text:
            print("[getAccessToken]", "got an error with data:", r.text)
            #sys.exit(1)
            return None

        self.parseUserLoginData(r)

        if "accessToken" not in r.text:
            print("[getAccessToken]", "got no accessToken with data:", r.text)
            return None

        self.accessToken = r.text.split('accessToken="')[1].split('"')[0]

        return True

    def quickReload(self):
        self.accessToken = None
        self.getAccessToken()

    def login(self, email=None, password=None):

        if not self.getAccessToken():
            print("[login] failed to get access token")
            return None

        # double check if something goes wrong
        if not self.accessToken:
            return None

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

        #        self.checksum = checksum

        # if refreshToken was used we get acquire session without credentials
        if self.device.refreshToken:
            r = requests.Response
            url = (
                self.baseUrl
                + "UserEmailPasswordAuthorize2?clientDateTime={}&checksum={}&deviceKey={}&accessToken={}&refreshToken={}".format(
                    ts,
                    self.checksum,
                    self.device.key,
                    self.accessToken,
                    self.device.refreshToken,
                )
            )

            r = self.request(url, "POST")

            if "Email=" not in r.text:
                print("[login] failed to authenticate with refreshToken:", r.text)
                return None

            self.parseUserLoginData(r)

        else:

            self.email = urllib.parse.quote(email)

            url = (
                self.baseUrl
                + "UserEmailPasswordAuthorize2?clientDateTime={}&checksum={}&deviceKey={}&email={}&password={}&accessToken={}".format(
                    ts, self.checksum, self.device.key, self.email, password, self.accessToken
                )
            )

            r = self.request(url, "POST")

            if "errorMessage=" in r.text:
                print(
                    "[login] failed to authorize with credentials with the reason:",
                    r.text,
                )
                sys.exit(1)
                return False

            if "refreshToken" not in r.text:
                print("[login] failed to acquire refreshToken with th reason", r.text)
                return False

            self.device.refreshTokenAcquire(r.text.split('refreshToken="')[1].split('"')[0])

            if 'RequireReload="True"' in r.text:
                return self.quickReload()

        if "refreshToken" in r.text:
            self.device.refreshTokenAcquire(r.text.split('refreshToken="')[1].split('"')[0])

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
        self.starSystemMarkersAndUserMarkers = xmltodict.parse(r.content, xml_attribs=True)

    def listTasksOfAUser(self):
        url = f"https://api.pixelstarships.com/TaskService/ListTasksOfAUser?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        r = self.request(url, "GET")
        self.tasksOfAUser = xmltodict.parse(r.content, xml_attribs=True)

    def listCompletedMissionEvents(self):
        ts = '{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())
        checksum = ChecksumEmailAuthorize(self.device.key, self.info['@Email'], ts, self.accessToken, self.salt)
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
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/ResearchService/ListAllResearches?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.allResearches = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listItemsOfAShip(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/ItemService/ListItemsOfAShip?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.itemsOfAShip = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listRoomsViaAccessToken(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListRoomsViaAccessToken?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.roomsViaAccessToken = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listAllCharactersOfUser(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/CharacterService/ListAllCharactersOfUser?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.allCharactersOfUser = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listAllRoomActionsOfShip(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListAllRoomActionsOfShip?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            self.allRoomActionsOfShip = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def pusherAuth(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/UserService/PusherAuth?accessToken={self.accessToken}"
            r = self.request(url, "POST")
            self.auth = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listSystemMessagesForUser3(self, fromMessageId=0, take=10000):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/MessageService/ListSystemMessagesForUser3?fromMessageId={fromMessageId}&take={take}&accessToken={self.accessToken}"
            r = self.request(url, "POST")
            self.systemMessagesForUser = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False

    def listFriends(self, userId=0):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/UserService/ListFriends?UserId={userId if userId else self.info['@Id']}&accessToken={self.accessToken}"
            print(url)
            r = self.request(url, "POST")
            self.systemMessagesForUser = xmltodict.parse(r.content, xml_attribs=True)
            return True
        return False
      
    def listMessagesForChannelKey(self, channelKey="alliance-43958"):
        url = f"https://api.pixelstarships.com/MessageService/ListMessagesForChannelKey?channelKey=channelKey={channelKey}&accessToken={self.accessToken}"
        r = self.request(url, "GET")
        self.messagesForChannelKey = xmltodict.parse(r.content, xml_attribs=True)
        # Perform error handling and return values based on the results
        #return True
        #return False

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
        print("[{}] {} for {} {}.".format( self.info["@Name"], message, price, currency))

    def listActiveMarketplaceMessages(self):
        if self.user.isAuthorized:
            url = "https://api.pixelstarships.com/MessageService/ListActiveMarketplaceMessages5?itemSubType=None&rarity=None&currencyType=Unknown&itemDesignId=0&userId={}&accessToken={}".format( self.user.id, self.accessToken)
            r = self.request(url, "GET")
            d = xmltodict.parse(r.content, xml_attribs=True)
            if "errorMessage=" in r.text:
                print(f'An error occurred: {r.text}.')
                return False
            if d["MessageService"]["ListActiveMarketplaceMessages"]["Messages"] == None:
                print( f'[{self.info["@Name"]}] You have no items listed on the marketplace.')
                return False

            for k, v in d["MessageService"]["ListActiveMarketplaceMessages"][
                "Messages"
            ].items():
                if isinstance(v, dict):
                    self.print_market_data(v)
                elif isinstance(v, list):
                    for i in v:
                        if isinstance(i, dict):
                            self.print_market_data(i)
            return True

    def infoBux(self):
        print(f"[{self.info['@Name']}] A total of {self.freeStarbuxToday} free starbux was collected today.")
        print(f"[{self.info['@Name']}] You have a total of {self.credits} starbux.")

    def collectAllResources(self):
        if self.user.isAuthorized and self.rssCollectedTimestamp + 120 < time.time():
            url = "https://api.pixelstarships.com/RoomService/CollectAllResources?itemType=None&collectDate={}&accessToken={}".format(
                "{0:%Y-%m-%dT%H:%M:%S}".format(DotNet.validDateTime()),
                self.accessToken,
            )
            r = self.request(url, "POST")
            d = xmltodict.parse(r.content, xml_attribs=True)
            if "errorMessage=" in r.text:
                return False

            try:
                self.credits = d["RoomService"]["CollectResources"]["User"]["@Credits"]
            except:
                pass

            self.rssCollectedTimestamp = time.time()

            print(
                f'[{self.info["@Name"]}] There is a total of {d["RoomService"]["CollectResources"]["Items"]["Item"][0]["@Quantity"]} minerals on your ship.'
            )
            print(
                f'[{self.info["@Name"]}] There is a total of {d["RoomService"]["CollectResources"]["Items"]["Item"][1]["@Quantity"]} gas on your ship.'
            )
            return True
        return False

    def collectDailyReward(self):
        self.dailyRewardArgument = self.todayLiveOps["LiveOpsService"]["GetTodayLiveOps"]["LiveOps"]["@DailyRewardArgument"]
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
                print(f"[{self.info['@User']}] You have already collected the daily reward from the dropship.")


            print(f"[{self.info['@User'] if '@User' in self.info else ''}] You have collected the daily reward from the dropship.")
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

    def grabFlyingStarbux(self, quantity):

        if (
            self.freeStarbuxToday < 10
            and self.freeStarbuxTodayTimestamp + 180 < time.time()
        ):
            t = DotNet.validDateTime()

            url = (
                self.baseUrl
                + "AddStarbux2?quantity={}&clientDateTime={}&checksum={}&accessToken={}".format(
                    quantity,
                    "{0:%Y-%m-%dT%H:%M:%S}".format(t),
                    ChecksumTimeForDate(DotNet.get_time())
                    + ChecksumPasswordWithString(self.accessToken),
                    self.accessToken,
                )
            )
            r = self.request(url, "POST")

            self.freeStarbuxToday = int(
                r.text.split('FreeStarbuxReceivedToday="')[1].split('"')[0]
            )
            #print(f'[{self.info["@Name"]}] You\'ve collected a total of {self.freeStarbuxToday} starbux today.')
            self.freeStarbuxTodayTimestamp = time.time()

            return True
        return False

    def listRoomsViaAccessToken(self):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/ListRoomsViaAccessToken?accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            r = self.request(url, "GET")
            d = xmltodict.parse(r.content, xml_attribs=True)
            return d
        return False


    # Determine the boost gauge before attempting to speed up a room
    def speedUpResearchUsingBoostGauge(self, researchId, researchDesignId):
        url = f"https://api.pixelstarships.com/ResearchService/SpeedUpResearchUsingBoostGauge?researchId={researchId}&accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
        d = self.listAllResearchDesigns()
        if not d:
            return False
        for i in d["ResearchService"]["ListAllResearchDesigns"]["ResearchDesigns"][
            "ResearchDesign"
        ]:
            if i["@ResearchDesignId"] == researchDesignId:
                print(
                    f"[{self.info['@Name']}] Speeding up research for {''.join(i['@ResearchName'])}."
                )
                self.request(url, "POST")
                break

    # Determine the boost gauge before attempting to speed up a room
    def speedUpRoomConstructionUsingBoostGauge(self, roomId, roomDesignId):
        if self.user.isAuthorized:
            url = f"https://api.pixelstarships.com/RoomService/SpeedUpRoomConstructionUsingBoostGauge?roomId={roomId}&accessToken={self.accessToken}&clientDateTime={'{0:%Y-%m-%dT%H:%M:%S}'.format(DotNet.validDateTime())}"
            if not self.roomDesigns:
             return False
            for i in self.roomDesigns["RoomService"]["ListRoomDesigns"]["RoomDesigns"]["RoomDesign"]:
                if i["@RoomDesignId"] == roomDesignId:
                    print(f"[{self.info['@Name']}] Speeding up contruction for {''.join(i['@RoomName'])}.")
                    self.request(url, "POST")
                    break
            return True
        return False

    def rushResearchOrConstruction(self):
        self.getShipByUserId()
        if "ShipService" in self.shipByUserId:
            for i in self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Researches"][
                "Research"
            ]:
                if i["@ResearchState"] == "Researching":
                    self.speedUpResearchUsingBoostGauge(
                        i["@ResearchId"], i["@ResearchDesignId"]
                    )
                    return True
                for i in self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Rooms"]["Room"]:
                    if i["@RoomStatus"] == "Upgrading":
                        self.speedUpRoomConstructionUsingBoostGauge(
                            i["@RoomId"], i["@RoomDesignId"]
                        )
                        return True
        print(f'[{self.info["@Name"]}] There are no rooms or research to speed up.')
        return False

    def upgradeResearchorRoom(self):
        if self.user.isAuthorized:
            self.getShipByUserId()
            shipByUserId = self.shipByUserId
            roomDesigns = self.roomDesigns
            if shipByUserId:
                # Implement upgrading of research items
                for research in shipByUserId["ShipService"]["GetShipByUserId"]["Ship"][
                    "Researches"
                ]["Research"]:
                    if (research["@ResearchState"] != "Researching") and (
                        research["@ResearchState"] != "Completed"
                    ):
                        pass
                for room in shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Rooms"][
                    "Room"
                ]:
                    roomId = room["@RoomId"]
                    roomStatus = room["@RoomStatus"]
                    roomDesignId = room["@RoomDesignId"]
                    roomName = ""
                    upgradeRoomDesignId = ""
                    upgradeRoomName = ""

                    for roomDesignData in roomDesigns["RoomService"]["ListRoomDesigns"][
                        "RoomDesigns"
                    ]["RoomDesign"]:
                        if roomDesignId == roomDesignData["@RoomDesignId"]:
                            roomName = "".join(roomDesignData["@RoomName"])
                        if roomDesignId == roomDesignData["@UpgradeFromRoomDesignId"]:
                            upgradeRoomDesignId = roomDesignData["@RoomDesignId"]
                            upgradeRoomName = "".join(
                                roomDesignData["@RoomName"])
                            cost = roomDesignData["@PriceString"].split(":")
                            url = "https://api.pixelstarships.com/RoomService/CollectAllResources?itemType=None&collectDate={}&accessToken={}".format(
                                "{0:%Y-%m-%dT%H:%M:%S}".format(
                                    DotNet.validDateTime()),
                                self.accessToken,
                            )
                            r = self.request(url, "POST")
                            d = xmltodict.parse(r.content, xml_attribs=True)
                            if "RoomService" not in d:
                                continue
                            self.mineralTotal = d["RoomService"]["CollectResources"][
                                "Items"
                            ]["Item"][0]["@Quantity"]
                            self.gasTotal = d["RoomService"]["CollectResources"][
                                "Items"
                            ]["Item"][1]["@Quantity"]
                            if (cost[0] == "mineral") and (
                                int(cost[1]) > int(self.mineralTotal)
                            ):
                                continue

                            if (cost[0] == "gas") and (
                                int(cost[1]) > int(self.gasTotal)
                            ):
                                continue

                            if (
                                roomName
                                and upgradeRoomName
                                and (roomStatus != "Upgrading")
                                and upgradeRoomDesignId != "0"
                            ):
                                print(
                                    f'[{self.info["@Name"]}] Upgradng {roomName} to {upgradeRoomName}.')
                                url = f'https://api.pixelstarships.com/RoomService/UpgradeRoom2?roomId={roomId}&upgradeRoomDesignId={upgradeRoomDesignId}&accessToken={self.accessToken}'
                                # time.sleep(random.uniform(5.0, 10.0))
                                r = self.request(url, "POST")
                                roomName = ""
                                upgradeRoomName = ""
                                if "concurrent" in r.text:
                                    print(
                                        f'[{self.info["@Name"]}] You have reached the maximum number of concurrent constructions allowed.'
                                    )
                                    break
            return True

    def listUpgradingRooms(self):
        if self.user.isAuthorized:
            self.getShipByUserId()
            shipByUserId = self.shipByUserId
            roomDesigns = self.roomDesigns
            if shipByUserId and roomDesigns:
                for room in shipByUserId["ShipService"]["GetShipByUserId"]["Ship"]["Rooms"][
                    "Room"
                ]:
                    if room["@RoomStatus"] == "Upgrading":
                        for roomDesignData in roomDesigns["RoomService"][
                            "ListRoomDesigns"
                        ]["RoomDesigns"]["RoomDesign"]:
                            if room["@RoomDesignId"] == roomDesignData["@RoomDesignId"]:
                                print(
                                    f"[{self.info['@Name']}] {''.join(roomDesignData['@RoomName'])} is currently being upgraded."
                                )
            return True
        return False


    def listAllResearchDesigns(self):
        if self.user.isAuthorized:
            if self.latestVersion:
                url = f"https://api.pixelstarships.com/ResearchService/ListAllResearchDesigns2?languageKey={self.device.languageKey}&designVersion={self.latestVersion['SettingService']['GetLatestSetting']['Setting']['@ResearchDesignVersion']}"
                r = self.request(url, "GET")
                d = xmltodict.parse(r.content, xml_attribs=True)
                return d
        return False

    def rebuildAmmo(self):
        if self.user.isAuthorized:
            self.clientDateTime = "{0:%Y-%m-%dT%H:%M:%S}".format(
                DotNet.validDateTime()
            )
            ammoCategories = ["None", "Ammo", "Android",
                "Craft", "Module", "Charge"]
            for ammoCategory in ammoCategories:
                if ammoCategory == "None":
                    print(f'[{self.info["@Name"]}] Restocking all ammo items.')
                else:
                    print(f'[{self.info["@Name"]}] Restocking {ammoCategory.lower()} items.')
                url = f'http://api.pixelstarships.com/RoomService/RebuildAmmo2?ammoCategory={ammoCategory}&clientDateTime={self.clientDateTime}&checksum={self.checksum}&accessToken={self.accessToken}'
                self.request(url, "POST")
            return True
        return False

    def getCrewInfo(self):
        if self.user.isAuthorized:
            character_list = []
            fatigue_characters = collections.defaultdict(str)
            self.listAllCharactersOfUser()

            for character in self.allCharactersOfUser["CharacterService"]["ListAllCharactersOfUser"][
                "Characters"
            ]["Character"]:
                character_list.append(character["@CharacterName"])
                if int(character["@Fatigue"]) > 0:
                    fatigue_characters[character["@CharacterName"]] = character[
                    "@Fatigue"
                ]
            if character_list:
                print(f"[{self.info['@Name']}]: List of characters on your ship: {', '.join(character_list)}")
            if fatigue_characters:
                print(f"[{self.info['@Name']}]: List ot fatigue characters on your ship: {', '.join(f'{key} has {value} fatigue' for key, value in fatigue_characters.items())}.")
            return True
        return False

    def heartbeat(self):
        if self.user.lastHeartBeat:
            hours = self.user.lastHeartBeat.split("T")[1]
            seconds = hours.split(":")[-1]
            if DotNet.validDateTime().second == int(seconds):
                # print(f'{DotNet.validDateTime().second=} {int(seconds)=}')
                return

        t = DotNet.validDateTime()

        url = (
            self.baseUrl
            + "HeartBeat4?clientDateTime={}&checksum={}&accessToken={}".format(
                "{0:%Y-%m-%dT%H:%M:%S}".format(t),
                ChecksumTimeForDate(DotNet.get_time())
                + ChecksumPasswordWithString(self.accessToken),
                self.accessToken,
            )
        )

        r = self.request(url, "POST")
        success = False

        if r.status_code == 200 and 'success="t' in r.text:
            success = True
        else:
            print(f'[{self.info["@Name"]}] Heartbeat fail. Attempting to reauthorized access token.')
            self.quickReload()

        self.user.lastHeartBeat = "{0:%Y-%m-%dT%H:%M:%S}".format(t)

        return success


