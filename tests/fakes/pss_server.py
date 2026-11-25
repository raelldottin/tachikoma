"""FakePss: a stateful, URL-routing fake of the Pixel Starships API for offline tests.

The fake answers the real ``sdk.client.Client`` at its ``requests`` adapter, the one seam that sees every request
(login, re-auth and heartbeat bypass ``Client.request``). It routes on (method, path), applies the request to
``fake.state`` (a ``tests.fakes.state.World``) and replies with the XML shapes the client parses. Every request is
logged in ``fake.exchanges``. An unknown endpoint lands in ``fake.unknown``; a protocol error lands in
``fake.violations``: a bad or missing accessToken, a missing or undeclared parameter (``Route.required`` /
``Route.optional``), a malformed date, a wrong designVersion/userId, or an id that names nothing in the world. Game-rule
refusals (already collected, not enough gas, not finished yet) are ordinary error replies, not violations. So
``fake.unknown == fake.violations == []`` is a strong check even on a world without golden lists.

``fake.install()`` patches the client for one run (adapter, rate limiter, clock, randomness) and restores everything
on exit. The clock covers every loaded sdk module (DAILY_PATH_MODULES are imported first), with stand-ins that forward
any clock-free attribute; an sdk module first imported inside ``install()`` that still holds the real clock is a
violation. ``fake.advance(hours=...)`` moves the clock that both the fake and production read. The fake host is UTC
unless ``FakePss(world, local_offset=HOST_UTC_MINUS_5)`` (or any offset) says otherwise: then production's local clock
reads (``datetime.now()``, ``time.localtime()``...) differ from its UTC reads, as on a developer laptop (FakeClock).

Typical multi-day use (state and clock carry over between runs; each run logs in again)::

    fake = FakePss(default_world())
    run_daily(fake)                      # tests/test_daily_run_offline_e2e.py: real run.main()
    fake.advance(hours=2, minutes=1)     # training sessions end; production's utcnow() moves too
    run_daily(fake)
    fake.state.character("1001").stats   # what FinishTraining applied

Faults: ``fake.fail(path, status=404 | error="..." | body="..." | raises=requests.ConnectionError, times=, when=)``
and ``fake.expire_token_once(at)``; both refuse a path that is not a route.
Advancing by whole days hits defect H1: production's heartbeat check uses ``timedelta.seconds`` (ignores days).
Only training sessions follow the clock: room upgrades and research never complete, and the daily counters
(DailyRewardStatus, FreeStarbuxReceivedToday) never reset. A multi-day test must set those in ``fake.state`` itself.
Defect IDs (D1, M1, H1, ...) are listed in KNOWN_DEFECTS in tests/test_daily_run_offline_e2e.py.
"""

from __future__ import annotations

import calendar
import contextlib
import datetime
import importlib
import inspect
import json
import random
import sys
import time
import types
from collections.abc import Callable, Generator
from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import quoteattr

from requests import Response, Session
from requests.adapters import BaseAdapter, HTTPAdapter
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    ConnectTimeout,
    ProxyError,
    RequestException,
    RetryError,
    SSLError,
    Timeout,
)
from urllib3 import exceptions as urllib3_errors
from urllib3.util import Retry

from .state import (
    FAKE_EPOCH,
    GUEST_USER_ID,
    Character,
    Message,
    Research,
    TrainingSession,
    default_world,
    fmt,
    parse_price,
)

AUTH_ERROR = "Failed to authorize access token"
# The status of the reply to a defect in the fake itself (a crashing handler, a raising ``when=`` predicate). Not a 500:
# production's urllib3 Retry re-sends 500/502/503/504/520, which would re-run the broken handler 10 more times and reach
# production as a RetryError. 599 is in no status_forcelist and is >= 400, so production sees one falsy reply.
FAKE_ERROR_STATUS = 599
_StructTime = time.struct_time  # FakeClock.time (a method) shadows the module inside the class
_UNIX_EPOCH = datetime.datetime(1970, 1, 1)


def el(tag: str, attrs: dict | None = None, *children: str) -> str:
    """One XML element; attribute values are str()-ed and quoted."""
    attr_text = "".join(f" {key}={quoteattr(str(value))}" for key, value in (attrs or {}).items())
    if not children:
        return f"<{tag}{attr_text}/>"
    return f"<{tag}{attr_text}>{''.join(children)}</{tag}>"


def envelope(service: str, element: str, *children: str, **attrs) -> str:
    return el(service, None, el(element, attrs, *children))


def error_envelope(service: str, element: str, message: str) -> str:
    return envelope(service, element, errorCode="1", errorMessage=message)


class _Reject(Exception):
    """A handler's refusal: answered as a 200 errorMessage envelope.

    ``violation=True`` marks a protocol error that production should never cause, such as an id that names nothing
    in the world (production only sends ids the server gave it). Game-rule refusals (already collected, not enough
    gas, not finished yet) are plain rejections: production may legitimately run into them.
    """

    def __init__(self, message: str, *, violation: bool = False):
        super().__init__(message)
        self.violation = violation


@dataclass(frozen=True)
class Route:
    """One endpoint: its handler, reply element, token kind and parameter schema.

    ``required`` and ``optional`` name every parameter the endpoint accepts besides ``accessToken`` (which ``token``
    implies). A missing required parameter or an undeclared one is a protocol violation: production never has a
    reason to send either.
    """

    handler: str
    service: str
    element: str
    token: str  # "none" | "guest" | "account"
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    mutating: bool = False


def _route(handler: str, service: str, element: str, token: str, required: str = "", optional: str = "",
           mutating: bool = False) -> Route:
    return Route(handler, service, element, token, tuple(required.split()), tuple(optional.split()), mutating)


CHECKSUMMED = "clientDateTime checksum"
ROUTES = {
    # Login: the fake checks only what it uses; the device fields are pinned by the e2e checksum golden. The JSON body's
    # nested UserDeviceInfo object arrives flattened (_body_params), one dotted parameter per field.
    ("POST", "/UserService/DeviceLogin17"): _route(
        "device_login", "UserService", "UserLogin", "none", "RefreshToken",
        "DeviceKey DeviceType Checksum ClientDateTime LanguageKey AccessToken AdvertisingKey IsJailBroken Signal "
        "UserDeviceInfo.OsVersion UserDeviceInfo.Locale UserDeviceInfo.DeviceName UserDeviceInfo.OSBuild "
        "UserDeviceInfo.ClientBuild UserDeviceInfo.ClientVersion"),
    ("POST", "/UserService/UserEmailPasswordAuthorize4"): _route(
        "authorize", "UserService", "UserLogin", "guest", "email password",
        "checksum clientDateTime deviceKey isWeb languageKey"),
    ("POST", "/UserService/HeartBeat4"): _route("heartbeat", "UserService", "HeartBeat", "account", CHECKSUMMED,
                                                mutating=True),
    ("GET", "/SettingService/GetLatestVersion3"): _route(
        "settings", "SettingService", "GetLatestSetting", "none", optional="languageKey deviceType"),
    ("GET", "/LiveOpsService/GetTodayLiveOps2"): _route(
        "live_ops", "LiveOpsService", "GetTodayLiveOps", "none", optional="languageKey deviceType"),
    # Design lists
    ("GET", "/TrainingService/ListAllTrainingDesigns2"): _route(
        "training_designs", "TrainingService", "ListAllTrainingDesigns", "none", "designVersion", "languageKey"),
    ("GET", "/TaskService/ListAllTaskDesigns2"): _route(
        "task_designs", "TaskService", "ListAllTaskDesigns", "none", "designVersion", "languageKey"),
    ("GET", "/ResearchService/ListAllResearchDesigns2"): _route(
        "research_designs", "ResearchService", "ListAllResearchDesigns", "none", "designVersion", "languageKey"),
    ("GET", "/RoomService/ListRoomDesigns2"): _route(
        "room_designs", "RoomService", "ListRoomDesigns", "none", "designVersion", "languageKey"),
    ("GET", "/CharacterService/ListAllCharacterDesigns2"): _route(
        "character_designs", "CharacterService", "ListAllCharacterDesigns", "none", "designVersion", "languageKey"),
    # Account reads
    ("GET", "/CharacterService/ListAllCharactersOfUser"): _route(
        "characters", "CharacterService", "ListAllCharactersOfUser", "account", optional="clientDateTime"),
    ("GET", "/TaskService/ListTasksOfAUser"): _route("tasks", "TaskService", "ListTasksOfAUser", "account",
                                                     optional="clientDateTime"),
    ("GET", "/ResearchService/ListAllResearches"): _route(
        "researches", "ResearchService", "ListAllResearches", "account", optional="clientDateTime"),
    ("GET", "/RoomService/ListRoomsViaAccessToken"): _route(
        "rooms", "RoomService", "ListRoomsViaAccessToken", "account", optional="clientDateTime"),
    ("GET", "/ItemService/ListItemsOfAShip"): _route("items", "ItemService", "ListItemsOfAShip", "account",
                                                     optional="clientDateTime"),
    ("GET", "/ShipService/GetShipByUserId"): _route("ship", "ShipService", "GetShipByUserId", "account", "userId",
                                                    "clientDateTime"),
    ("GET", "/GalaxyService/ListStarSystemMarkersAndUserMarkers"): _route(
        "markers", "GalaxyService", "ListStarSystemMarkersAndUserMarkers", "account"),
    ("GET", "/MessageService/ListActiveMarketplaceMessages5"): _route(
        "marketplace", "MessageService", "ListActiveMarketplaceMessages", "account", "userId",
        "itemSubType rarity currencyType itemDesignId"),
    ("GET", "/MessageService/ListSystemMessagesForUser3"): _route(
        "system_messages", "MessageService", "ListSystemMessagesForUser", "account", optional="fromMessageId take"),
    # Mutations
    ("POST", "/ItemService/ActivateItem3"): _route("activate_item", "ItemService", "ActivateItem3", "account",
                                                   "itemId targetId", mutating=True),
    ("POST", "/TrainingService/AddTraining"): _route(
        "add_training", "TrainingService", "AddTraining", "account", "trainingDesignId characterId trainingStartDate",
        mutating=True),
    ("POST", "/TrainingService/FinishTraining"): _route(
        "finish_training", "TrainingService", "FinishTraining", "account", "characterId", mutating=True),
    ("POST", "/CharacterService/UpgradeCharacter"): _route(
        "upgrade_character", "CharacterService", "UpgradeCharacter", "account", "characterId", mutating=True),
    ("POST", "/UserService/CollectDailyReward2"): _route(
        "daily_reward", "UserService", "CollectDailyReward", "account", "dailyRewardStatus argument", mutating=True),
    ("POST", "/GalaxyService/CollectMarker2"): _route(
        "collect_marker", "GalaxyService", "CollectMarker", "account", f"starSystemMarkerId {CHECKSUMMED}",
        mutating=True),
    ("POST", "/RoomService/UpgradeRoom2"): _route("upgrade_room", "RoomService", "UpgradeRoom", "account",
                                                  "roomId upgradeRoomDesignId", mutating=True),
    ("POST", "/ResearchService/AddResearch"): _route(
        "add_research", "ResearchService", "AddResearch", "account", "researchDesignId researchStartDate",
        mutating=True),
    ("POST", "/RoomService/CollectAllResources"): _route(
        "collect_resources", "RoomService", "CollectResources", "account", "itemType collectDate", mutating=True),
    ("POST", "/TaskService/CollectTaskCompletion"): _route(
        "collect_task", "TaskService", "CollectTaskCompletion", "account", "taskDesignId", mutating=True),
    ("POST", "/MessageService/CollectReward2"): _route(
        "collect_reward", "MessageService", "CollectReward", "account", f"messageId {CHECKSUMMED}", mutating=True),
    ("GET", "/MessageService/ActionMessage"): _route(
        "action_message", "MessageService", "ActionMessage", "account", "messageId", mutating=True),
    ("POST", "/ShopService/PurchaseCatalog2"): _route(
        "purchase", "ShopService", "PurchaseCatalog2", "account", f"argument {CHECKSUMMED}", mutating=True),
    ("POST", "/UserService/AddStarbux2"): _route(
        "add_starbux", "UserService", "AddStarbux", "account", f"quantity {CHECKSUMMED}", mutating=True),
    ("POST", "/GalaxyService/GoTo"): _route("go_to", "GalaxyService", "GoTo", "account",
                                            f"starSystemId {CHECKSUMMED}", mutating=True),
    ("POST", "/GalaxyService/SpeedUpTravelling"): _route(
        "speed_up", "GalaxyService", "SpeedUpTravelling", "account", CHECKSUMMED, mutating=True),
}
# Parameters that carry the server's date format (production formats them with "{0:%Y-%m-%dT%H:%M:%S}").
DATE_PARAMS = ("clientDateTime", "trainingStartDate", "researchStartDate", "collectDate")
SERVER_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
# GetTrainingUpdate and ListAllDesigns4 are deliberately absent: they answer as unknown endpoints. ListAllDesigns4 is
# never sent because the Setting carries no FileVersion.
MAX_FREE_STARBUX = 10
MAX_CREW_LEVEL = 40
# The server's text when the concurrent-upgrade limit is reached (production lists it in ROOM_UPGRADE_BLOCKERS; a
# self-test checks they agree, so a production edit cannot silently change what the fake answers).
ROOM_LIMIT_TEXT = "You cannot build or upgrade more rooms. Please wait for other constructions to be completed."
SCORCHED_POD_ARGUMENT, SCORCHED_POD_PRICE = "1291", 1000
DAILY_REWARD_MINERAL = 1000
TASK_REWARD_CREDITS = 10


@dataclass
class Fault:
    """One injected failure; see ``FakePss.fail``."""

    path: str
    method: str | None = None
    times: int = 1
    status: int | None = None
    error: str | None = None
    body: str | None = None
    when: Callable[[dict], bool] | None = None
    raises: type[RequestException] | None = None
    hits: int = 0

    def matches(self, method: str, path: str, params: dict) -> bool:
        return (self.hits < self.times and self.path == path and self.method in (None, method)
                and (self.when is None or bool(self.when(params))))


@dataclass
class Exchange:
    method: str
    path: str
    params: dict
    token_status: str  # "n/a" | "missing" | "current" | "guest" | "revoked" | "unknown"
    status: int = 200
    error: str | None = None


def _flatten_json(value, prefix: str, out: dict) -> None:
    """Nested objects become dotted keys and list items ``key[i]``; an empty object or list is kept as its JSON text.

    Scalars keep their JSON spelling (``false``, ``0``, ``null``), so every field production sends, at any depth, is
    a parameter the route must declare.
    """
    if isinstance(value, dict) and value:
        for key, item in value.items():
            _flatten_json(item, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            _flatten_json(item, f"{prefix}[{index}]", out)
    else:
        out[prefix] = value if isinstance(value, str) else json.dumps(value)


def _body_params(body) -> dict:
    if not body:
        return {}
    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    if text.lstrip().startswith("{"):
        params: dict = {}
        _flatten_json(json.loads(text), "", params)
        return params
    return dict(parse_qsl(text, keep_blank_values=True))


class _RaisedFault(Exception):
    """A ``raises=`` fault on its way from ``FakePss.handle`` to the adapter, which raises the real exception."""

    def __init__(self, error_class: type[RequestException]):
        super().__init__(error_class.__name__)
        self.error_class = error_class


def _exhausted_network_error(error_class: type[RequestException], request) -> RequestException:
    """What requests 2.28 raises once urllib3 1.26 has used up its retries on a network failure of this kind.

    urllib3 raises ``MaxRetryError(reason=<the last error>)`` and ``HTTPAdapter.send`` maps it: a connect timeout to
    ``ConnectTimeout``, an SSL error to ``SSLError``, a proxy error to ``ProxyError``, and everything else, a read
    timeout included, to ``ConnectionError``. The urllib3 error the reason carries is synthetic (no pool, no socket).
    """
    url, text = request.url, f"FakePss network fault: {request.method} {request.url}"
    if issubclass(error_class, ConnectTimeout):
        surfaced, reason = ConnectTimeout, urllib3_errors.ConnectTimeoutError(text)
    elif issubclass(error_class, SSLError):
        surfaced, reason = SSLError, urllib3_errors.SSLError(text)
    elif issubclass(error_class, ProxyError):
        surfaced, reason = ProxyError, urllib3_errors.ProxyError(text, OSError(text))
    elif issubclass(error_class, Timeout):
        surfaced, reason = RequestsConnectionError, urllib3_errors.ReadTimeoutError(None, url, text)
    else:
        surfaced, reason = RequestsConnectionError, urllib3_errors.NewConnectionError(None, text)
    return surfaced(urllib3_errors.MaxRetryError(None, url, reason), request=request)


class _Adapter(BaseAdapter):
    """A requests adapter that answers from the fake instead of the network.

    ``retry`` mirrors the urllib3 ``Retry`` of the adapter it replaces (``install()`` copies production's): a status
    in its ``status_forcelist`` is re-sent, as urllib3 would, and once the retries run out ``RetryError`` is raised
    instead of a response. A ``raises=`` network failure (a ``ConnectionError`` or ``Timeout``) is re-sent the same
    way, and once the retries run out it surfaces as requests would surface urllib3's ``MaxRetryError``
    (``_exhausted_network_error``: a persistent ``ReadTimeout`` is a ``ConnectionError``). Any other ``raises=``
    class (``ChunkedEncodingError``...) happens outside urllib3's retries and is raised on the first send. So a
    one-off 500 or connection error is invisible to production, and only a 4xx (or a persistent failure) is a falsy
    reply or an exception. Backoff sleeps are not modelled. ``fake.session()`` mounts it with no retry and raises the
    ``raises=`` class itself.
    """

    def __init__(self, fake: FakePss, retry: Retry | None = None):
        super().__init__()
        self.fake = fake
        self.retry = retry

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        remaining = (self.retry.total or 0) if self.retry is not None else 0
        while True:
            try:
                status, text = self.fake.handle(request.method, request.url, request.body)
            except _RaisedFault as raised:
                if self.retry is None or not issubclass(raised.error_class, (RequestsConnectionError, Timeout)):
                    raise raised.error_class(f"FakePss network fault: {request.method} {request.url}",
                                             request=request) from None
                if remaining <= 0:
                    raise _exhausted_network_error(raised.error_class, request) from None
                remaining -= 1
                continue
            if self.retry is None or not self.retry.is_retry(request.method, status):
                break
            if remaining <= 0:
                raise RetryError(f"too many {status} error responses (FakePss)", request=request)
            remaining -= 1
        response = Response()
        response.status_code = status
        response._content = text.encode("utf-8")
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


# The sdk modules run.main()'s daily path executes. install() imports them first so their clock globals are swapped
# too; any sdk module first imported inside install() that still holds the real clock is reported as a violation.
DAILY_PATH_MODULES = ("sdk.client", "sdk.device", "sdk.dotnet", "sdk.security", "sdk.redaction", "sdk.ship_layout",
                      "sdk.crew_leveling")


class _StandIn(types.ModuleType):
    """Stands in for a module: the overrides first, every other attribute forwarded to the real module."""

    def __init__(self, real: types.ModuleType, **overrides):
        super().__init__(f"{real.__name__} (FakePss stand-in)")
        self.__dict__.update(overrides)
        self.__dict__["_real"] = real

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)


# Host clocks (local time minus UTC). The daily workflow runs on ubuntu-latest, whose clock is UTC, so the goldens use
# HOST_UTC; a developer laptop is usually not UTC. Every server date is UTC, so a correct client decides the same on any
# host: tests/test_daily_run_offline_e2e.py NonUtcHostTests runs the default world on the two hosts below and pins the
# decisions that move (defects TZ1, TZ2). A behind-UTC host (US) and an ahead-of-UTC host (Asia) move them in opposite
# directions, so a local/UTC swap is caught whichever way it errs.
HOST_UTC = datetime.timedelta(0)
HOST_UTC_MINUS_5 = datetime.timedelta(hours=-5)  # e.g. New York in November (EST)
HOST_UTC_PLUS_9 = datetime.timedelta(hours=9)  # e.g. Tokyo


class _FrozenClassMeta(type):
    """Keeps ``isinstance``/``issubclass`` against a frozen class true for every instance of the real class it stands for.

    Production holds the frozen class under the real name (``datetime.datetime``), so ``isinstance(value,
    datetime.datetime)`` must hold for a real datetime too (strptime results, the fake's own dates), as it does without
    the fake. A frozen class's ``_real`` is the real class it subclasses.
    """

    _real: type

    def __instancecheck__(cls, instance):
        return isinstance(instance, cls._real)

    def __subclasscheck__(cls, subclass):
        return issubclass(subclass, cls._real)


class FakeClock:
    """The one clock the fake and (inside ``install()``) production read. It only moves when told to.

    ``now`` is naive UTC: ``utcnow()``, ``now(tz)`` (aware), ``time.time()``, ``time.time_ns()``, ``time.gmtime()`` and
    every date the fake emits read it. The host's local clock is ``now + local_offset``: naive ``now()``, ``today()``,
    ``date.today()``, ``fromtimestamp(ts)``, ``time.localtime()``, ``time.strftime()``, ``time.ctime()`` and
    ``time.mktime()`` read or invert it, exactly as on a real host in that zone (no DST). With the default ``HOST_UTC``
    the two agree, as on the CI runners; any other offset makes every local/UTC mix-up in production observable.
    ``time.monotonic()`` and ``time.perf_counter()`` (and their ``_ns`` forms) are seconds since the clock was made plus
    MONOTONIC_BASE, so they move with ``sleep()`` and ``advance()`` and a monotonic-bounded wait loop ends. The frozen
    datetime/date classes keep ``isinstance`` working both ways (``_FrozenClassMeta``). Other clock-free attributes
    (``timedelta``, ``thread_time``...) are forwarded to the real modules.
    """

    MONOTONIC_BASE = datetime.timedelta(seconds=1000)  # not 0, so "monotonic() is elapsed time" is not true by accident

    def __init__(self, now: datetime.datetime, local_offset: datetime.timedelta = HOST_UTC):
        self.now = now
        self.local_offset = local_offset
        self.sleeps: list[float] = []
        self._monotonic_zero = now - self.MONOTONIC_BASE
        clock = self

        class FrozenDateTime(datetime.datetime, metaclass=_FrozenClassMeta):
            """datetime.datetime whose clock reads (utcnow, now, today, fromtimestamp) read this clock."""

            _real = datetime.datetime

            @classmethod
            def utcnow(cls):
                return clock.now

            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return clock.local_now
                return clock.now.replace(tzinfo=datetime.timezone.utc).astimezone(tz)

            @classmethod
            def today(cls):
                return clock.local_now

            @classmethod
            def fromtimestamp(cls, timestamp, tz=None):
                if tz is None:  # the real one would use the machine's zone, not the fake host's
                    return _UNIX_EPOCH + datetime.timedelta(seconds=timestamp) + clock.local_offset
                return datetime.datetime.fromtimestamp(timestamp, tz)

        class FrozenDate(datetime.date, metaclass=_FrozenClassMeta):
            """datetime.date whose today() reads this clock's local date."""

            _real = datetime.date

            @classmethod
            def today(cls):
                return clock.local_now.date()

        self.datetime_class = FrozenDateTime
        self.date_class = FrozenDate
        self.datetime_stand_in = _StandIn(datetime, datetime=FrozenDateTime, date=FrozenDate)
        self.time_stand_in = _StandIn(time, time=self.time, time_ns=self.time_ns, sleep=self.sleep,
                                      gmtime=self.gmtime, localtime=self.localtime, mktime=self.mktime,
                                      strftime=self.strftime, ctime=self.ctime, monotonic=self.monotonic,
                                      monotonic_ns=self.monotonic_ns, perf_counter=self.monotonic,
                                      perf_counter_ns=self.monotonic_ns)
        # Each real clock object and what replaces it wherever an sdk module holds it. Only sdk modules are patched:
        # the ratelimit package keeps the real time.monotonic (install() bypasses the limiter by unwrapping
        # Client.request instead).
        self._stand_ins = ((datetime, self.datetime_stand_in), (datetime.datetime, FrozenDateTime),
                           (datetime.date, FrozenDate), (time, self.time_stand_in), (time.time, self.time),
                           (time.time_ns, self.time_ns), (time.sleep, self.sleep), (time.monotonic, self.monotonic),
                           (time.monotonic_ns, self.monotonic_ns), (time.perf_counter, self.monotonic),
                           (time.perf_counter_ns, self.monotonic_ns))

    @property
    def local_now(self) -> datetime.datetime:
        """The host's naive local time (what production's ``datetime.now()`` returns)."""
        return self.now + self.local_offset

    def advance(self, **timedelta_kwargs) -> datetime.datetime:
        self.now += datetime.timedelta(**timedelta_kwargs)
        return self.now

    def time(self) -> float:
        """Epoch seconds of ``now`` (production's time.time())."""
        return (self.now - _UNIX_EPOCH).total_seconds()

    def time_ns(self) -> int:
        """Exact: integer microseconds since the epoch times 1000 (a float product would be off by up to ~200 ns)."""
        return (self.now - _UNIX_EPOCH) // datetime.timedelta(microseconds=1) * 1000

    def monotonic(self) -> float:
        """Production's ``time.monotonic()`` and ``time.perf_counter()``: moves only with the fake clock."""
        return (self.now - self._monotonic_zero).total_seconds()

    def monotonic_ns(self) -> int:
        return (self.now - self._monotonic_zero) // datetime.timedelta(microseconds=1) * 1000

    def gmtime(self, secs: float | None = None) -> _StructTime:
        return time.gmtime(self.time() if secs is None else secs)

    def localtime(self, secs: float | None = None) -> _StructTime:
        """``time.localtime`` on the fake host: UTC shifted by ``local_offset``, with its tm_zone and tm_gmtoff."""
        offset = int(self.local_offset.total_seconds())
        shifted = time.gmtime((self.time() if secs is None else secs) + offset)
        hours, minutes = divmod(abs(offset) // 60, 60)
        zone = "UTC" if offset == 0 else f"UTC{'+' if offset > 0 else '-'}{hours:02d}{minutes:02d}"
        return _StructTime((*shifted[:8], 0, zone, offset))

    def mktime(self, t) -> float:
        """The inverse of ``localtime``."""
        return float(calendar.timegm(tuple(t)[:9]) - int(self.local_offset.total_seconds()))

    def strftime(self, fmt_text: str, t: _StructTime | None = None) -> str:
        return time.strftime(fmt_text, self.localtime() if t is None else t)

    def ctime(self, secs: float | None = None) -> str:
        return time.asctime(self.localtime(secs))

    def sleep(self, seconds: float) -> None:
        """Record the sleep and move the clock instead of waiting."""
        self.sleeps.append(seconds)
        self.advance(seconds=seconds)

    def stand_in_for(self, value):
        """The replacement for ``value`` if it is a real clock object (module, class or function), else None."""
        return next((stand_in for real, stand_in in self._stand_ins if value is real), None)

    def leaks(self) -> list[tuple[str, str]]:
        """(module, global) for every loaded sdk module global that is still a real clock object."""
        return [(name, attr) for name, module in sorted(sys.modules.items()) if _is_sdk(name) and module is not None
                for attr, value in sorted(vars(module).items()) if self.stand_in_for(value) is not None]


def _is_sdk(module_name: str) -> bool:
    return module_name == "sdk" or module_name.startswith("sdk.")


class FakePss:
    def __init__(self, world=None, *, seed: int = 5343, local_offset: datetime.timedelta = HOST_UTC):
        """``local_offset``: the host's local time minus UTC (HOST_UTC, HOST_UTC_MINUS_5, HOST_UTC_PLUS_9...)."""
        self.state = world if world is not None else default_world()
        self.state.validate()
        self.clock = FakeClock(FAKE_EPOCH, local_offset)
        self.seed = seed
        self.exchanges: list[Exchange] = []
        self.unknown: list[tuple[str, str]] = []
        self.violations: list[str] = []
        self.faults: list[Fault] = []
        self.tokens: dict[str, str] = {}  # token -> "guest" | "account"
        self._revoked: set[str] = set()
        self._expire_at: str | None = None
        self.adapter = _Adapter(self)

    # --- test controls ------------------------------------------------------------------------------------------

    def fail(self, path: str, *, times: int = 1, status: int | None = None, error: str | None = None,
             body: str | None = None, method: str | None = None, when: Callable[[dict], bool] | None = None,
             raises: type[RequestException] | None = None) -> Fault:
        """Fail the next ``times`` matching requests to ``path`` without running its handler (no state change).

        The reply is ``body`` if given, else an errorMessage envelope with ``error``, else empty; ``raises`` (e.g.
        ``requests.ConnectionError``) raises that exception from the adapter instead of replying. ``status`` defaults
        to 200 when a body or error is given, else 404 (a falsy ``requests`` response). ``when`` filters on the merged
        params. The first pending matching fault wins. Inside ``install()`` production's urllib3 retry re-sends a
        500/502/503/504/520 or a network failure up to 10 times (each re-send is a request here and uses up one of
        ``times``), so a one-off 5xx or ``raises`` is invisible to production: a falsy reply needs a 4xx such as 404,
        and a ``RetryError`` (or a network exception) needs ``times=11``. The network exception is then what requests
        raises after urllib3's retries (a persistent ``ReadTimeout`` arrives as a ``ConnectionError``; see
        ``_exhausted_network_error``). ``path`` must be a route (ValueError).
        """
        self._check_route_path(path)
        fault = Fault(path, method, times, status, error, body, when, raises)
        self.faults.append(fault)
        return fault

    def expire_token_once(self, at: str) -> None:
        """Revoke the current account token just before the first request to path ``at`` is routed."""
        self._check_route_path(at)
        self._expire_at = at

    @staticmethod
    def _check_route_path(path: str) -> None:
        if not any(path == route_path for _, route_path in ROUTES):
            raise ValueError(f"{path!r} is not a FakePss route: the fault would never fire")

    @contextlib.contextmanager
    def install(self) -> Generator[FakePss, None, None]:
        """Point the real ``sdk.client.Client`` at this fake for the block; everything is restored on exit.

        Every loaded sdk module's clock globals (``datetime``, ``time``, ``from datetime import datetime``...) read
        ``fake.clock``. On a normal exit, an sdk module first imported inside the block that still holds the real
        clock is recorded as a violation, so a new module on the daily path cannot silently read the wall clock.
        """
        for name in DAILY_PATH_MODULES:
            importlib.import_module(name)
        from sdk import client as client_mod, device as device_mod

        client_cls = client_mod.Client
        assert isinstance(client_mod.datetime, types.ModuleType), "sdk.client changed its datetime import"
        assert isinstance(client_mod.time, types.ModuleType), "sdk.client changed its time import"
        production_retry = client_cls.session.get_adapter(client_cls.baseUrl).max_retries
        assert isinstance(production_retry, Retry), "sdk.client changed its HTTPAdapter retry"
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(self.adapter, "retry", production_retry))  # urllib3's status retries
            # Any other session (a new requests.Session, requests.get) would reach the network: refuse and record it,
            # also outside scripts/ci.py's socket audit hook (e.g. --print-golden).
            stack.enter_context(patch.object(HTTPAdapter, "send", self._refuse_real_send))
            stack.enter_context(patch.dict(client_cls.session.adapters, {"https://": self.adapter,
                                                                         "http://": self.adapter}))
            stack.enter_context(patch.object(client_cls.session, "trust_env", False))  # no ~/.netrc or proxies
            # The raw request body, without @sleep_and_retry/@limits: they would wait out 30 calls a minute.
            stack.enter_context(patch.object(client_cls, "request", inspect.unwrap(client_cls.request)))
            # Client.dronesCollected is a CLASS-level dict that every Client in the process shares (defect DC1,
            # characterised by RealClientTests in tests/test_fake_pss_server.py). A fresh dict per install keeps one
            # test's (or run's) drones from leaking into the next; it does not hide DC1 inside one install.
            stack.enter_context(patch.object(client_cls, "dronesCollected", {}))
            for module_name, module in sorted(sys.modules.items()):
                if _is_sdk(module_name) and module is not None:
                    for attr, value in sorted(vars(module).items()):
                        stand_in = self.clock.stand_in_for(value)
                        if stand_in is not None:
                            stack.enter_context(patch.object(module, attr, stand_in))
            stack.enter_context(patch.object(client_mod, "random", random.Random(self.seed)))
            stack.enter_context(patch.object(device_mod, "random", random.Random(self.seed + 1)))
            yield self
            for module_name, attr in self.clock.leaks():
                self.violations.append(f"{module_name}: reads the real clock through {attr!r} (imported inside "
                                       "install(); add it to DAILY_PATH_MODULES)")

    def _refuse_real_send(self, request, *args, **kwargs):
        """Stands in for HTTPAdapter.send inside ``install()``. Bound to the fake, so no adapter is passed."""
        self.violations.append(f"{request.method} {urlsplit(request.url).path}: real network send blocked")
        raise RequestsConnectionError("FakePss.install() blocks real network sends", request=request)

    # --- clock --------------------------------------------------------------------------------------------------

    @property
    def now(self) -> datetime.datetime:
        return self.clock.now

    def advance(self, **timedelta_kwargs) -> datetime.datetime:
        """Move the clock forward, e.g. ``advance(hours=1)``; production sees it too inside ``install()``."""
        return self.clock.advance(**timedelta_kwargs)

    # --- inspection ---------------------------------------------------------------------------------------------

    @property
    def requests(self) -> list[tuple[str, str, dict]]:
        """The ordered request log: (method, path, params), password redacted."""
        return [(e.method, e.path, e.params) for e in self.exchanges]

    def paths(self) -> list[str]:
        """``"METHOD /path"`` for every request, in order."""
        return [f"{e.method} {e.path}" for e in self.exchanges]

    def mutations(self, exchanges: list[Exchange] | None = None) -> list[tuple[str, str, dict]]:
        """Every request to a mutating route, with token, checksum and device key normalized for golden lists.

        accessToken becomes "<token>" when it was the current account token, else "<revoked-token>" and so on; a
        missing one stays absent. Timestamps stay literal: the frozen clock makes them deterministic. ``exchanges``
        limits it to a slice of ``fake.exchanges`` (e.g. one daily step); the default is all of them.
        """
        result = []
        for e in self.exchanges if exchanges is None else exchanges:
            route = ROUTES.get((e.method, e.path))
            if route is None or not route.mutating:
                continue
            params = dict(e.params)
            if "accessToken" in params:
                params["accessToken"] = "<token>" if e.token_status == "current" else f"<{e.token_status}-token>"
            for key in ("checksum", "deviceKey"):
                if key in params:
                    params[key] = f"<{key}>"
            result.append((e.method, e.path, params))
        return result

    def session(self) -> Session:
        """A session that talks to this fake (self-tests; production uses ``install()``)."""
        session = Session()
        session.trust_env = False
        session.mount("https://", self.adapter)
        session.mount("http://", self.adapter)
        return session

    # --- routing ------------------------------------------------------------------------------------------------

    def handle(self, method: str, url: str, body) -> tuple[int, str]:
        """Log one request and answer it: (HTTP status, body text). The adapter wraps this in a Response.

        A defect in the fake itself (a bad body, a raising ``when=`` predicate) is a violation and an empty
        FAKE_ERROR_STATUS reply (answered once, never retried), never an exception production could catch and log as
        its own failure.
        """
        logged = len(self.exchanges)
        try:
            return self._handle(method, url, body)
        except _RaisedFault:
            raise
        except Exception as bug:
            self.violations.append(f"{method} {urlsplit(url).path}: FakePss error {bug!r}")
            if len(self.exchanges) > logged:
                self.exchanges[-1].status, self.exchanges[-1].error = FAKE_ERROR_STATUS, f"FakePss error {bug!r}"
            return FAKE_ERROR_STATUS, ""

    def _handle(self, method: str, url: str, body) -> tuple[int, str]:
        parts = urlsplit(url)
        path = parts.path
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        params.update(_body_params(body))
        route = ROUTES.get((method, path))
        if self._expire_at == path:
            self._expire_at = None
            self._revoked.update(token for token, kind in self.tokens.items() if kind == "account")
        logged = {key: "<redacted>" if key == "password" else value for key, value in params.items()}
        exchange = Exchange(method, path, logged, self._token_status(route, params))
        self.exchanges.append(exchange)
        if route is None:
            self.unknown.append((method, path))
            exchange.error = "unknown endpoint"
            segments = [segment for segment in path.split("/") if segment] + ["Unknown", "Unknown"]
            return 200, error_envelope(segments[0], segments[1], f"Unknown endpoint {method} {path}")
        # Protocol checks run before any fault, so a fault cannot hide a dropped accessToken or parameter.
        violation = self._token_violation(route, exchange.token_status)
        if violation:
            self._record_violation(exchange, violation)
        param_violations = self._param_violations(route, params)
        for problem in param_violations:
            self._record_violation(exchange, problem)
        fault = next((f for f in self.faults if f.matches(method, path, params)), None)
        if fault is not None:
            return self._answer_fault(fault, exchange, route)
        if violation:
            return 200, error_envelope(route.service, route.element, AUTH_ERROR)
        if param_violations:
            return 200, error_envelope(route.service, route.element, "; ".join(param_violations))
        return self._dispatch(exchange, route, params)

    @staticmethod
    def _param_violations(route: Route, params: dict) -> list[str]:
        allowed = set(route.required) | set(route.optional) | ({"accessToken"} if route.token != "none" else set())
        problems = [f"missing {name}" for name in route.required if name not in params]
        problems += [f"unexpected {name}" for name in params if name not in allowed]
        for name in DATE_PARAMS:
            if name in params:
                try:
                    datetime.datetime.strptime(params[name], SERVER_DATE_FORMAT)
                except ValueError:
                    problems.append(f"{name} {params[name]!r} is not in the server date format")
        return problems

    def _dispatch(self, exchange: Exchange, route: Route, params: dict) -> tuple[int, str]:
        try:
            return 200, getattr(self, f"_h_{route.handler}")(params)
        except _Reject as reject:
            if reject.violation:
                self._record_violation(exchange, str(reject))
            exchange.error = str(reject)
            return 200, error_envelope(route.service, route.element, str(reject))
        except Exception as bug:  # a defect in the fake itself: fail every test that checks violations
            self._record_violation(exchange, f"FakePss handler error {bug!r}")
            exchange.status = FAKE_ERROR_STATUS
            return FAKE_ERROR_STATUS, ""

    @staticmethod
    def _answer_fault(fault: Fault, exchange: Exchange, route: Route) -> tuple[int, str]:
        fault.hits += 1
        exchange.error = "fault"
        if fault.raises is not None:
            exchange.status = 0  # no HTTP reply at all
            raise _RaisedFault(fault.raises)
        exchange.status = fault.status or (200 if fault.body is not None or fault.error else 404)
        if fault.body is not None:
            return exchange.status, fault.body
        return exchange.status, error_envelope(route.service, route.element, fault.error) if fault.error else ""

    def _record_violation(self, exchange: Exchange, violation: str) -> None:
        self.violations.append(f"{exchange.method} {exchange.path}: {violation}")
        exchange.error = violation

    def _token_status(self, route: Route | None, params: dict) -> str:
        if route is None or route.token == "none":
            return "n/a"
        token = params.get("accessToken")
        if token is None:
            return "missing"
        if token in self._revoked:
            return "revoked"
        kind = self.tokens.get(token)
        return {"account": "current", "guest": "guest"}.get(kind, "unknown")

    @staticmethod
    def _token_violation(route: Route, status: str) -> str | None:
        wanted = {"guest": "guest", "account": "current"}.get(route.token)
        if wanted is None or status == wanted:
            return None
        return f"{status} accessToken (wanted {wanted})"

    def _issue(self, kind: str) -> str:
        token = f"{len(self.tokens) + 1:08d}-1111-2222-3333-444444444444"
        self.tokens[token] = kind
        return token

    # --- login --------------------------------------------------------------------------------------------------

    def _h_device_login(self, p: dict) -> str:
        account = self.state.account
        refresh = p.get("RefreshToken", "")
        if refresh == "":
            token = self._issue("guest")
            user = el("User", {"Id": GUEST_USER_ID, "Name": ""})
            return envelope("UserService", "UserLogin", user, UserId=GUEST_USER_ID, accessToken=token)
        if refresh != account.refresh_token:
            raise _Reject("Invalid refresh token")
        self._revoked.update(token for token, kind in self.tokens.items() if kind == "account")
        token = self._issue("account")
        user = el("User", {"Id": account.user_id, "Name": account.name, "Credits": account.credits,
                           "DailyRewardStatus": account.daily_reward_status,
                           "FreeStarbuxReceivedToday": account.free_starbux_today,
                           "LastHeartBeatDate": fmt(account.last_heartbeat)})
        return envelope("UserService", "UserLogin", user, UserId=account.user_id, accessToken=token)

    def _h_authorize(self, p: dict) -> str:
        account = self.state.account
        if p.get("email") != account.email or p.get("password") != account.password:
            raise _Reject("Email or password is incorrect")
        return envelope("UserService", "UserLogin", refreshToken=account.refresh_token)

    def _h_heartbeat(self, p: dict) -> str:
        self.state.account.last_heartbeat = self.now
        return envelope("UserService", "HeartBeat", success="true")

    # --- reads --------------------------------------------------------------------------------------------------

    def _check_version(self, p: dict, kind: str) -> None:
        wanted = self.state.design_versions[kind]
        if p.get("designVersion") != wanted:
            raise _Reject(f"designVersion {p.get('designVersion')!r} is not {kind} version {wanted}", violation=True)

    def _check_user(self, p: dict) -> None:
        if p.get("userId") != self.state.account.user_id:
            raise _Reject(f"userId {p.get('userId')!r} is not the logged-in user", violation=True)

    def _h_training_designs(self, p: dict) -> str:
        self._check_version(p, "Training")
        rows = [el("TrainingDesign", {"TrainingDesignId": d.id, "TrainingName": d.name, "Stat": d.stat,
                                      "Gain": d.gain, "Fatigue": d.fatigue,
                                      "Duration": int(d.duration.total_seconds())})
                for d in self.state.training_designs]
        return envelope("TrainingService", "ListAllTrainingDesigns", el("TrainingDesigns", None, *rows))

    def _h_marketplace(self, p: dict) -> str:
        self._check_user(p)
        rows = [el("Message", {"MessageId": m.id, "Message": m.text, "ActivityArgument": m.activity_argument})
                for m in self.state.market_listings]
        return envelope("MessageService", "ListActiveMarketplaceMessages", el("Messages", None, *rows))

    def _h_settings(self, p: dict) -> str:
        # No FileVersion: with it, production would call ListAllDesigns4, which needs ~35 more versions.
        versions = {f"{kind}DesignVersion": value for kind, value in self.state.design_versions.items()}
        return envelope("SettingService", "GetLatestSetting", el("Setting", versions))

    def _h_live_ops(self, p: dict) -> str:
        live_ops = el("LiveOps", {"DailyRewardArgument": self.state.daily_reward_argument})
        return envelope("LiveOpsService", "GetTodayLiveOps", live_ops)

    def _h_tasks(self, p: dict) -> str:
        rows = [el("Task", self._task_attrs(t)) for t in self.state.tasks]
        return envelope("TaskService", "ListTasksOfAUser", el("Tasks", None, *rows))

    def _h_task_designs(self, p: dict) -> str:
        self._check_version(p, "Task")
        rows = [el("TaskDesign", {"TaskDesignId": d.id, "Name": d.name, "Description": d.name,
                                  "ObjectiveAmount": d.objective}) for d in self.state.task_designs]
        return envelope("TaskService", "ListAllTaskDesigns", el("TaskDesigns", None, *rows))

    def _h_researches(self, p: dict) -> str:
        rows = [el("Research", self._research_attrs(r)) for r in self.state.researches]
        return envelope("ResearchService", "ListAllResearches", el("Researches", None, *rows))

    def _h_research_designs(self, p: dict) -> str:
        self._check_version(p, "Research")
        rows = [el("ResearchDesign", {"ResearchDesignId": d.id, "RootResearchDesignId": d.root_id,
                                      "ResearchName": d.name, "GasCost": d.gas_cost, "StarbuxCost": d.starbux_cost})
                for d in self.state.research_designs]
        return envelope("ResearchService", "ListAllResearchDesigns", el("ResearchDesigns", None, *rows))

    def _h_room_designs(self, p: dict) -> str:
        self._check_version(p, "Room")
        rows = [el("RoomDesign", {"RoomDesignId": d.id, "RoomName": d.name, "RoomType": d.room_type,
                                  "UpgradeFromRoomDesignId": d.upgrade_from, "PriceString": d.price,
                                  "ColumnWidth": d.column_width, "RowHeight": d.row_height, "RoomHp": d.room_hp,
                                  "PowerGenerated": d.power_generated, "MaxPopulation": d.max_population})
                for d in self.state.room_designs]
        return envelope("RoomService", "ListRoomDesigns", el("RoomDesigns", None, *rows))

    def _h_ship(self, p: dict) -> str:
        self._check_user(p)
        ship, state = self.state.ship, self.state
        rooms = el("Rooms", None, *(el("Room", self._room_attrs(r)) for r in state.rooms))
        researches = el("Researches", None, *(el("Research", self._research_attrs(r)) for r in state.researches))
        attrs = {"ShipId": ship.ship_id, "UserId": state.account.user_id, "ShipName": ship.name,
                 "ShipLevel": ship.level, "ShipDesignId": ship.design_id, "StarSystemId": ship.star_system_id,
                 "UpgradeShipDesignId": ship.upgrade_design_id}
        return envelope("ShipService", "GetShipByUserId", el("Ship", attrs, rooms, researches))

    def _h_rooms(self, p: dict) -> str:
        rows = [el("Room", self._room_attrs(r)) for r in self.state.rooms]
        return envelope("RoomService", "ListRoomsViaAccessToken", el("Rooms", None, *rows))

    def _h_markers(self, p: dict) -> str:
        # Standard envelope: UserMarkers sits under the action element, where collectAvailableMarkers does not look
        # (defect M1); findMiningDrones reads StarSystemMarkers from the same element.
        star = [el("StarSystemMarker", self._marker_attrs(m)) for m in self.state.markers if not m.user_marker]
        user = [el("UserMarker", self._marker_attrs(m)) for m in self.state.markers if m.user_marker]
        return envelope("GalaxyService", "ListStarSystemMarkersAndUserMarkers",
                        el("StarSystemMarkers", None, *star), el("UserMarkers", None, *user))

    def _h_system_messages(self, p: dict) -> str:
        rows = [el("Message", self._message_attrs(m)) for m in self.state.messages]
        return envelope("MessageService", "ListSystemMessagesForUser", el("Messages", None, *rows))

    def _h_character_designs(self, p: dict) -> str:
        self._check_version(p, "Character")
        rows = [el("CharacterDesign", {"CharacterDesignId": d.id, "CharacterDesignName": d.name,
                                       "TrainingCapacity": d.training_capacity, "Rarity": d.rarity,
                                       "SpecialAbilityType": d.special_ability_type})
                for d in self.state.character_designs]
        return envelope("CharacterService", "ListAllCharacterDesigns", el("CharacterDesigns", None, *rows))

    def _h_items(self, p: dict) -> str:
        rows = [el("Item", {"ItemId": i.id, "ItemDesignId": i.design_id, "Quantity": i.quantity})
                for i in self.state.items]
        return envelope("ItemService", "ListItemsOfAShip", el("Items", None, *rows))

    def _h_add_starbux(self, p: dict) -> str:
        account = self.state.account
        quantity = p.get("quantity", "")
        if not quantity.isdigit():
            raise _Reject(f"Invalid quantity {quantity!r}")
        granted = max(0, min(int(quantity), MAX_FREE_STARBUX - account.free_starbux_today))
        account.free_starbux_today += granted
        account.credits += granted
        user = el("User", {"Id": account.user_id, "FreeStarbuxReceivedToday": account.free_starbux_today})
        return envelope("UserService", "AddStarbux", user)

    def _h_go_to(self, p: dict) -> str:
        ship = self.state.ship
        ship.star_system_id = p.get("starSystemId", "")
        ship.arrival = self.now + datetime.timedelta(seconds=self.state.travel_seconds)
        attrs = {"ShipId": ship.ship_id, "StarSystemId": ship.star_system_id,
                 "StarSystemArrivalDate": fmt(ship.arrival)}
        return envelope("GalaxyService", "GoTo", el("Ship", attrs))

    def _h_speed_up(self, p: dict) -> str:
        self.state.ship.arrival = self.now
        return envelope("GalaxyService", "SpeedUpTravelling", result="Success")

    # --- crew ---------------------------------------------------------------------------------------------------

    @staticmethod
    def _character_attrs(c) -> dict:
        attrs = {"CharacterId": c.id, "CharacterDesignId": c.design_id, "CharacterName": c.name,
                 "RoomId": c.room_id, "Level": c.level, "Xp": c.xp, "Fatigue": c.fatigue}
        attrs.update(c.stats)
        attrs.update({"TrainingEndDate": fmt(c.training_end), "AvailableDate": fmt(c.available_date)})
        return attrs

    def _h_characters(self, p: dict) -> str:
        rows = [el("Character", self._character_attrs(c)) for c in self.state.characters]
        return envelope("CharacterService", "ListAllCharactersOfUser", el("Characters", None, *rows))

    def _h_activate_item(self, p: dict) -> str:
        try:
            item = self.state.item(p.get("itemId", ""))
            self.state.character(p.get("targetId", ""))
        except KeyError as missing:
            raise _Reject(f"Invalid item or target: {missing}", violation=True) from None
        if item.quantity <= 0:
            raise _Reject("Not enough items")
        item.quantity -= 1
        return envelope("ItemService", "ActivateItem3", el("Item", {"ItemId": item.id, "Quantity": item.quantity}))

    def _crew(self, cid) -> Character:
        try:
            return self.state.character(cid or "")
        except KeyError:
            raise _Reject(f"Character {cid!r} not found", violation=True) from None

    def _in_training_room(self, crew: Character) -> bool:
        try:
            return self.state.room_design(self.state.room(crew.room_id).design_id).room_type == "Training"
        except KeyError:
            return False

    def _h_add_training(self, p: dict) -> str:
        try:
            design = self.state.training_design(p.get("trainingDesignId", ""))
        except KeyError:
            raise _Reject(f"Invalid training design {p.get('trainingDesignId')!r}", violation=True) from None
        crew = self._crew(p.get("characterId"))
        if not self._in_training_room(crew):
            raise _Reject("Crew must be in a training room")
        if crew.session and not crew.session.applied:
            raise _Reject("Crew is already training")
        crew.session = TrainingSession(design.id, self.now, self.now + design.duration)  # TrainingEndDate follows
        return envelope("TrainingService", "AddTraining", el("Character", self._character_attrs(crew)))

    def _h_finish_training(self, p: dict) -> str:
        crew = self._crew(p.get("characterId"))
        session = crew.session
        if session is None or session.applied:
            raise _Reject("No training to finish")
        if session.end > self.now:
            raise _Reject("Training is not finished yet")
        design = self.state.training_design(session.design_id)
        crew.stats[design.stat] += design.gain
        crew.fatigue += design.fatigue
        session.applied = True
        return envelope("TrainingService", "FinishTraining", el("Character", self._character_attrs(crew)))

    def _h_upgrade_character(self, p: dict) -> str:
        # Unassigned crew (RoomId "0") is accepted: skipping it is production's own rule (upgradeCharacters,
        # sdk/crew_leveling.py), and nothing in the repo (fixtures, captures, README) shows the server refusing it.
        # Nothing is charged: the xp and gas costs exist only as production's tables, and a fake that copied them
        # would agree with production by construction instead of checking it. The goldens pin which crew production
        # upgrades; the fake only refuses what the server is known to refuse (max level, AvailableDate not reached).
        crew = self._crew(p.get("characterId"))
        if crew.level >= MAX_CREW_LEVEL:
            raise _Reject("Crew is already at the maximum level")
        if crew.available_date is not None and crew.available_date > self.now:  # both UTC
            raise _Reject("Crew is not available yet")
        crew.level += 1
        return envelope("CharacterService", "UpgradeCharacter", el("Character", self._character_attrs(crew)))

    # --- economy ------------------------------------------------------------------------------------------------

    def _grant(self, reward: str) -> None:
        kind, _, amount = reward.partition(":")
        if not amount.isdigit():
            return
        if kind == "mineral":
            self.state.resources.mineral += int(amount)
        elif kind == "gas":
            self.state.resources.gas += int(amount)
        elif kind == "starbux":
            self.state.account.credits += int(amount)

    def _h_purchase(self, p: dict) -> str:
        account = self.state.account
        if p.get("argument") != SCORCHED_POD_ARGUMENT:
            raise _Reject(f"Unknown catalog item {p.get('argument')!r}")
        if account.credits < SCORCHED_POD_PRICE:
            raise _Reject("Not enough starbux")
        account.credits -= SCORCHED_POD_PRICE
        return envelope("ShopService", "PurchaseCatalog2", el("User", {"Credits": account.credits}), errorCode="0")

    def _h_collect_task(self, p: dict) -> str:
        try:
            task = self.state.task(p.get("taskDesignId", ""))
            design = self.state.task_design(task.design_id)
        except KeyError:
            raise _Reject(f"Task {p.get('taskDesignId')!r} not found", violation=True) from None
        if task.collected:
            raise _Reject("Task already collected")
        if task.progress < design.objective:
            raise _Reject("Task is not complete")
        task.collected = True
        self.state.account.credits += TASK_REWARD_CREDITS
        return envelope("TaskService", "CollectTaskCompletion", el("Task", self._task_attrs(task)))

    def _h_collect_resources(self, p: dict) -> str:
        resources = self.state.resources
        held = (("Mineral", resources.mineral), ("Gas", resources.gas))
        items = el("Items", None, *(el("Item", {"Type": kind, "Quantity": quantity}) for kind, quantity in held
                                    if quantity or not self.state.collect_omits_empty_items))
        user = el("User", {"Credits": self.state.account.credits}) if self.state.collect_reply_has_user else ""
        return envelope("RoomService", "CollectResources", items, user)

    def _h_daily_reward(self, p: dict) -> str:
        account = self.state.account
        if p.get("argument") != self.state.daily_reward_argument:
            raise _Reject(f"Invalid daily reward argument {p.get('argument')!r}", violation=True)
        if account.daily_reward_status == "1":
            raise _Reject("You already collected this reward")
        account.daily_reward_status = "1"
        self.state.resources.mineral += DAILY_REWARD_MINERAL
        return envelope("UserService", "CollectDailyReward",
                        el("User", {"Id": account.user_id, "DailyRewardStatus": account.daily_reward_status}))

    def _message(self, p: dict) -> Message:
        try:
            return self.state.message(p.get("messageId", ""))
        except KeyError:
            raise _Reject(f"Message {p.get('messageId')!r} not found", violation=True) from None

    def _h_collect_reward(self, p: dict) -> str:
        message = self._message(p)
        if message.collected:
            raise _Reject("Reward already collected")
        message.collected = True
        self._grant(message.activity_argument)
        return envelope("MessageService", "CollectReward", el("Message", self._message_attrs(message)))

    def _h_action_message(self, p: dict) -> str:
        message = self._message(p)
        message.actioned = True
        return envelope("MessageService", "ActionMessage", el("Message", self._message_attrs(message)))

    # --- ship ---------------------------------------------------------------------------------------------------

    def _h_collect_marker(self, p: dict) -> str:
        try:
            marker = self.state.marker(p.get("starSystemMarkerId", ""))
        except KeyError:
            raise _Reject(f"Marker {p.get('starSystemMarkerId')!r} not found", violation=True) from None
        if marker.collected:
            raise _Reject("Marker already collected")
        marker.collected = True
        self._grant(marker.reward)
        return envelope("GalaxyService", "CollectMarker", el("StarSystemMarker", self._marker_attrs(marker)))

    def _h_upgrade_room(self, p: dict) -> str:
        state = self.state
        try:
            room, target = state.room(p.get("roomId", "")), state.room_design(p.get("upgradeRoomDesignId", ""))
        except KeyError as missing:
            raise _Reject(f"Invalid room upgrade: {missing}", violation=True) from None
        if target.upgrade_from != room.design_id:
            raise _Reject(f"Room {room.id} cannot be upgraded to {target.id}")
        if room.status == "Upgrading":
            raise _Reject("Room is already upgrading")
        if sum(r.status == "Upgrading" for r in state.rooms) >= state.max_concurrent_upgrades:
            raise _Reject(ROOM_LIMIT_TEXT)
        kind, cost = parse_price(target.price)  # ValueError (a misconfigured world): a fake error, never free
        wallet, field = (state.account, "credits") if kind == "starbux" else (state.resources, kind)
        if cost > getattr(wallet, field):
            raise _Reject(f"Not enough {kind}s" if kind == "mineral" else f"Not enough {kind}")
        setattr(wallet, field, getattr(wallet, field) - cost)
        room.status, room.pending_design_id = "Upgrading", target.id
        return envelope("RoomService", "UpgradeRoom", el("Room", self._room_attrs(room)))

    def _h_add_research(self, p: dict) -> str:
        state = self.state
        try:
            design = state.research_design(p.get("researchDesignId", ""))
        except KeyError:
            raise _Reject(f"Research design {p.get('researchDesignId')!r} not found", violation=True) from None
        if any(r.design_id == design.id for r in state.researches):
            raise _Reject("Research already done")
        if any(r.state == "Researching" for r in state.researches):
            raise _Reject("Another research is in progress")
        if design.gas_cost > state.resources.gas:
            raise _Reject("Not enough gas")
        state.resources.gas -= design.gas_cost
        research = Research(design.id, "Researching")
        state.researches.append(research)
        return envelope("ResearchService", "AddResearch", el("Research", self._research_attrs(research)))

    # --- XML records --------------------------------------------------------------------------------------------

    @staticmethod
    def _task_attrs(task) -> dict:
        return {"TaskDesignId": task.design_id, "ProgressValue": task.progress,
                "Collected": "true" if task.collected else "false"}

    @staticmethod
    def _message_attrs(message) -> dict:
        return {"MessageId": message.id, "Message": message.text, "ActivityArgument": message.activity_argument}

    @staticmethod
    def _marker_attrs(marker) -> dict:
        return {"StarSystemMarkerId": marker.id, "StarSystemId": marker.star_system_id,
                "MarkerType": marker.marker_type, "IsCollected": "true" if marker.collected else "false",
                "RewardString": marker.reward, "MissionDesignId": marker.mission_design_id,
                "MissionEventId": marker.mission_event_id, "IsRepeatable": "false"}

    @staticmethod
    def _room_attrs(room) -> dict:
        attrs = {"RoomId": room.id, "RoomDesignId": room.design_id, "RoomStatus": room.status,
                 "Row": room.row, "Column": room.column}
        if room.pending_design_id:
            attrs["UpgradeRoomDesignId"] = room.pending_design_id
        return attrs

    @staticmethod
    def _research_attrs(research) -> dict:
        attrs = {"ResearchDesignId": research.design_id, "ResearchState": research.state}
        if research.name is not None:
            attrs["ResearchName"] = research.name
        if research.level is not None:
            attrs["ResearchLevel"] = research.level
        return attrs
