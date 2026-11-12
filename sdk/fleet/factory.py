"""Account factory for ``run.py --fleet-dry-run`` (R5).

``EnvironmentAccountFactory`` builds one account's ``AccountContext`` for the
fleet orchestrators: it reads the account's credentials from the environment,
logs in with the existing ``Client.login`` flow, reads design data with
read-only calls, and returns the account's own client, snapshot provider,
journal and design data.

Credential convention. A profile's ``credentials_secret`` (for example
``PSS_ACCOUNT_1``) names environment variables in one of two forms, the same
two forms ``run.py`` accepts for a single account:

- ``<NAME>``: the authentication string that ``--auth-file`` holds
  (``name|deviceKey|refreshToken|language[|accessToken|userId]``);
- ``<NAME>_EMAIL`` and ``<NAME>_PASSWORD``: the e-mail and password that
  ``--login-email`` and ``--password-file`` take. The daily workflow's
  ``PSS_ACCOUNT_<n>_EMAIL`` / ``PSS_ACCOUNT_<n>_PASSWORD`` secrets use this
  form with ``credentials_secret`` ``PSS_ACCOUNT_<n>``.

Exactly one form must be complete. Both forms set, only one of the pair set,
or nothing set ends that account with ``error``; the error names the variables,
never their values. Prefer the auth-string form: the e-mail form starts from a
fresh device every run, so each run repeats a guest ``DeviceLogin17`` and the
unverified ``UserEmailPasswordAuthorize4`` call; such accounts get a note.

Secrets. Values are never logged, printed, journaled or put in error text.
Any exception raised while logging in or reading designs is re-raised with
``scrub``: the account's credential values (raw and URL-encoded), URL query
strings, raw user ids and e-mail addresses are removed. ``LogScrubber`` does
the same for log records while a fleet run is in progress, and drops the
client's below-warning chatter (login banners carry display names).

Isolation. Each account gets a device that never reads or writes the shared
``./.device`` file (so one account's refresh token can never be loaded by
another), and its own HTTP session instead of the session ``Client`` shares
at class level. The sessions still share ``Client``'s retrying HTTP adapter
(its connection pool) and the module-level rate limiter.

Design data. Hull (``ShipDesign``) records come from ``listAllDesigns4``, the
read-only call that populates ``client.shipDesigns``. The call is deprecated
by the game server, so hull designs usually stay empty: when it fails or
returns nothing, they stay empty and the reason is recorded in
``notes()``; only the ``hull-reserve-v1`` policy reads them. Room and
research designs come from ``listRoomDesigns2`` and
``listAllResearchDesigns2``, as in ``run.py --progression-dry-run``. Those
calls run after ``listAllDesigns4``, so their payloads are the ones parsed.

Dry-run plan stamps (SPEC-05, S05-R3). With ``dry_run_registry_stamp`` the
factory gives each account a ``DryRunJournal``: it holds the account's dry-run
events until ``commit_dry_runs`` learns how the account's dry run ended. Events
of an account whose dry run *completed* are written with a stamp in
``evidence`` (``dry_run_policy``, ``dry_run_registry``, ``dry_run_id``); any
other account's events are written unchanged. ``latest_dry_run`` reads the
stamps back for ``run.py --fleet-execute``.

Execute gate (SPEC-05, S05-R1 and S05-R4 to S05-R6). ``ExecuteFleetOrchestrator``
and ``ConcurrentExecuteFleetOrchestrator`` run the fleet orchestrators' own
per-account steps under an ``ExecutionGate`` built from an owner-written
``Authorization``: the plan keeps only authorized actions (in ``canary`` mode
only the first safe-to-retry one), budgets are the tighter of the registry's
and the authorization's caps with starbux at 0, ``GatedActionRunner`` refuses
an action outside the authorization or one whose cap would be exceeded, and
the fleet halts when more than ``halt_share`` of its accounts stopped.

Baselines (SPEC-04, S04-R1 and S04-R3). ``EnvironmentAccountFactory.open_baseline``
logs one account in the same way, reads no design data, takes one snapshot
with the provider's read-only calls and returns it with
``server_game_version``: the version in the server's latest-version setting
(``GetLatestVersion3``, which ``Client.login`` reads into ``latestVersion``),
never the client's declared ``ClientVersion``. ``run.py --baseline`` writes it.

Captures (SPEC-03, S03-R1). ``EnvironmentAccountFactory.open_reads`` logs one
account in the same way and makes no other call; ``run.py --capture-reads``
then makes only the read-only calls in ``sdk.state.capture.CAPTURE_READS``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote, quote_plus

from sdk.actions import Idempotency, get_spec
from sdk.client import _extract_collection
from sdk.device import Device
from sdk.execution import ActionRunner, UncertainPriorAttemptError
from sdk.execution.runner import planned_cost
from sdk.fleet.accounts import REGISTRY_VERSION, AccountProfile, AccountRegistry
from sdk.fleet.authorization import BATTLE_ACTION_ID, Authorization, check_authorization
from sdk.fleet.concurrent import ConcurrentFleetOrchestrator
from sdk.fleet.orchestrator import (
    AccountContext,
    AccountRun,
    FleetClaims,
    FleetOrchestrator,
    FleetRun,
    budgeted_policy_config,
    error_reason,
    spent_in_period,
)
from sdk.journal import GameplayEvent, GameplayJournal, new_attempt_id
from sdk.policy import DesignData, Plan, PolicyConfig, ProgressionPolicy, Rejection, get_policy
from sdk.redaction import redact_secrets
from sdk.scheduler import DailyScheduler, ScheduleReport, SchedulerConfig
from sdk.state import AccountSnapshot, ClientSnapshotProvider
from sdk.state.snapshot import SnapshotInputError, game_identity_fingerprint

EMAIL_SUFFIX = "_EMAIL"
PASSWORD_SUFFIX = "_PASSWORD"
JOURNAL_SUFFIX = ".log"  # covered by the repository's *.log ignore rule
REDACTED = "***REDACTED***"
AUTH_SECRET_FIELDS = (1, 2, 4, 5)  # device key, refresh token, access token, user id
SOURCE = "run.py --fleet-dry-run"


class CredentialError(RuntimeError):
    """An account's credentials are missing or ambiguous in the environment. Never holds a value."""


class LoginError(RuntimeError):
    """``Client.login`` reported failure."""


class AccountOpenError(RuntimeError):
    """Logging in or reading design data raised; the message is scrubbed of credential values."""


class Credentials:
    """One account's credential values. ``repr`` and ``str`` never show them."""

    __slots__ = ("auth_string", "email", "password")

    def __init__(self, *, auth_string: Optional[str] = None, email: Optional[str] = None,
                 password: Optional[str] = None) -> None:
        self.auth_string = auth_string
        self.email = email
        self.password = password

    def values(self) -> tuple[str, ...]:
        """Every secret value: the whole auth string and each of its secret fields, or the e-mail and password."""
        parts = (self.auth_string or "").split("|")
        # Auth string fields: name|deviceKey|refreshToken|language[|accessToken|userId]. The name
        # (an account label) and the language are not secret.
        fields = [parts[index].strip() for index in AUTH_SECRET_FIELDS if index < len(parts)]
        return tuple(value for value in (self.auth_string, *fields, self.email, self.password) if value)

    def __repr__(self) -> str:
        form = "auth-string" if self.auth_string else "email-password"
        return f"Credentials(<{form}, redacted>)"

    __str__ = __repr__


def _env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    return value.strip() if isinstance(value, str) else ""


def resolve_credentials(secret_name: str, environ: Mapping[str, str]) -> Credentials:
    """Read the credentials ``secret_name`` names from ``environ`` (see the module docstring)."""
    email_name, password_name = secret_name + EMAIL_SUFFIX, secret_name + PASSWORD_SUFFIX
    auth_string = _env(environ, secret_name)
    email, password = _env(environ, email_name), _env(environ, password_name)
    if auth_string and (email or password):
        raise CredentialError(
            f"both {secret_name} and {email_name}/{password_name} are set; set only one credential form")
    if auth_string:
        return Credentials(auth_string=auth_string)
    if email and password:
        return Credentials(email=email, password=password)
    if email or password:
        missing = password_name if email else email_name
        raise CredentialError(f"{missing} is not set; {email_name} and {password_name} must be set together")
    raise CredentialError(
        f"no credentials in the environment: set {secret_name} (auth string) or {email_name} and {password_name}")


# A URL query string (``?key=value&...``). Error text from requests/urllib3 and the client's own
# logs carry whole request URLs, whose query strings hold e-mails, passwords and user ids.
QUERY_PATTERN = re.compile(r"\?(?=[^\s'\"<>{}]*=)[^\s'\"<>{}]*")
USER_ID_PATTERN = re.compile(r"(?i)(user_?id['\"]?\s*[=:]\s*['\"]?)\d+")
# Ids and display names in dumped payloads: ``{'@Id': '7', '@Name': 'x'}`` (parsed XML) and
# ``Id="7" Name="x"`` (raw XML). ``\b`` keeps compound names such as ``RoomName`` untouched.
PAYLOAD_FIELD_PATTERN = re.compile(r"""(['"]@?(?:Id|UserId|Name)['"]\s*:\s*)(['"])[^'"]*\2""")
XML_FIELD_PATTERN = re.compile(r"""\b((?:Id|UserId|Name)\s*=\s*)(['"])[^'"]*\2""")
ENCODED_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+%40[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HEX_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
# Shorter values are not replaced literally: replacing "1" would mangle every number and, by what
# it leaves behind, reveal the value. Short values still go through the field-level rules
# (query strings, ``password=``, ids).
MIN_SECRET_LENGTH = 4


def _encoded_forms(value: str) -> set[str]:
    """``value`` as it may appear in text: raw, percent-encoded and form-encoded, either hex case."""
    forms = {value, quote(value, safe=""), quote(value), quote_plus(value), quote_plus(value, safe="")}
    for form in list(forms):
        forms.add(_HEX_ESCAPE.sub(lambda match: match.group(0).lower(), form))
        forms.add(_HEX_ESCAPE.sub(lambda match: match.group(0).upper(), form))
    return forms


def scrub(text: str, secrets: Iterable[str]) -> str:
    """``text`` without any form of ``secrets``, URL query strings, raw user ids or e-mail addresses.

    Each value in ``secrets`` of at least ``MIN_SECRET_LENGTH`` characters is
    replaced raw and in its URL-encoded forms. Every URL query string becomes
    ``?***REDACTED***``; ``userId=<digits>``-style ids, ``Id`` / ``UserId`` /
    ``Name`` values in dumped payloads and percent-encoded e-mail addresses are
    replaced; and ``redact_secrets`` then removes the remaining secret-shaped text.
    """
    forms = set()
    for value in secrets:
        if value and len(value) >= MIN_SECRET_LENGTH:
            forms |= _encoded_forms(value)
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, REDACTED)
    text = QUERY_PATTERN.sub("?" + REDACTED, text)
    text = USER_ID_PATTERN.sub(lambda match: match.group(1) + REDACTED, text)
    text = PAYLOAD_FIELD_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(2)}", text)
    text = XML_FIELD_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(2)}", text)
    text = ENCODED_EMAIL_PATTERN.sub("***REDACTED_EMAIL***", text)
    return redact_secrets(text)


CLIENT_MODULE = os.path.join("sdk", "client.py")
# A leading ``[label]`` in a client log line. The client prefixes most lines with the player's
# display name; snake_case method labels such as ``[authorize_email_password]`` are kept.
CLIENT_PREFIX_PATTERN = re.compile(r"^\[(?![a-z][a-z0-9]*(?:_[a-z0-9]+)+\])[^\]]*\]")


class LogScrubber(logging.Filter):
    """Handler filter that keeps credentials and account details out of the log during a fleet run.

    Every record is scrubbed with ``scrub`` using the credential values,
    salt and display names registered so far. Records from ``sdk/client.py``
    below ``WARNING`` (login banners, progress lines) are dropped, and the
    display-name prefix of the others becomes ``[account]``. ``installed``
    attaches the filter to every handler of a logger and removes it after.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self._names: set[str] = set()

    def add_secrets(self, values: Iterable[Optional[str]]) -> None:
        with self._lock:
            self._secrets.update(value for value in values if isinstance(value, str) and value)

    def add_names(self, names: Iterable[Any]) -> None:
        """Display names to remove from log records (not from ``scrub(names=False)`` text)."""
        with self._lock:
            self._names.update(str(name) for name in names
                               if name is not None and len(str(name).strip()) >= MIN_SECRET_LENGTH)

    def scrub(self, text: str, *, names: bool = True) -> str:
        """``scrub`` with the registered values; ``names=False`` leaves display names alone."""
        with self._lock:
            secrets = tuple(self._secrets | self._names) if names else tuple(self._secrets)
        return scrub(text, secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        from_client = str(record.pathname or "").endswith(CLIENT_MODULE)
        if from_client and record.levelno < logging.WARNING:
            return False
        try:
            message = record.getMessage()
        except Exception:  # a malformed record still must not leak
            message = str(record.msg)
        if from_client:
            message = CLIENT_PREFIX_PATTERN.sub("[account]", message)
        record.msg, record.args = self.scrub(message), None
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self.scrub(record.stack_info)
        return True

    @contextmanager
    def installed(self, logger: Optional[logging.Logger] = None):
        handlers = list((logger or logging.getLogger()).handlers)
        for handler in handlers:
            handler.addFilter(self)
        try:
            yield self
        finally:
            for handler in handlers:
                handler.removeFilter(self)


class EphemeralDevice(Device):
    """A ``Device`` that never reads or writes the shared ``./.device`` file.

    With an authentication string it parses that string as ``Device`` does;
    without one it starts with a fresh random key and no refresh token. A
    refresh token acquired during login stays in memory only.
    """

    def load(self):
        if not self.authentication_string:
            return False
        return super().load()

    def save(self):
        return None

    def reset(self):
        return None


def isolate_session(client: Any) -> None:
    """Give ``client`` its own HTTP session when it still uses the class-level shared one."""
    shared = getattr(type(client), "session", None)
    if shared is None or getattr(client, "session", None) is not shared or not hasattr(shared, "adapters"):
        return
    import requests

    session = requests.Session()
    for prefix, adapter in shared.adapters.items():
        session.mount(prefix, adapter)
    client.session = session


def read_design_data(client: Any) -> tuple[DesignData, tuple[str, ...]]:
    """Room, research and hull designs from read-only calls, and notes on what was not available."""
    notes = []
    ship_designs: list = []
    try:
        loaded = client.listAllDesigns4()
    except Exception as error:  # hull designs are optional; the other designs are not
        loaded = False
        notes.append(f"hull designs unavailable (listAllDesigns4 raised {type(error).__name__})")
    else:
        if loaded is False:
            notes.append("hull designs unavailable (listAllDesigns4 returned no data)")
    if loaded is not False:
        ship_designs = _extract_collection(getattr(client, "shipDesigns", None), "ShipDesign")
        if not ship_designs and not notes:
            notes.append("hull designs unavailable (no ShipDesign records)")
    client.listRoomDesigns2()
    client.listAllResearchDesigns2()
    design = DesignData.from_records(
        _extract_collection(getattr(client, "roomDesigns", None), "RoomDesign"),
        _extract_collection(getattr(client, "allResearchDesigns", None), "ResearchDesign"),
        ship_designs,
    )
    return design, tuple(notes)



EMAIL_FORM_NOTE = ("e-mail login: fresh device, guest DeviceLogin17 and unverified UserEmailPasswordAuthorize4 "
                   "on every run; prefer the auth-string form")


class EnvironmentAccountFactory:
    """``AccountFactory`` that logs each account in from its environment credentials.

    ``client_cls`` and ``device_cls`` are injectable so tests never construct a
    real client. ``client_settings`` are passed to every client (``run.py``
    passes its checksum settings); e-mail logins also get
    ``allow_email_password_login``. ``identity_salt`` is handed to each
    ``ClientSnapshotProvider`` and never stored anywhere else. With a
    ``log_scrubber``, each account's credential values are registered with it
    before login, and its display names after login. With
    ``dry_run_registry_stamp`` (``--fleet-dry-run`` passes ``registry_stamp``),
    each account's journal is a ``DryRunJournal``; call ``commit_dry_runs``
    after the run to write the held events.
    """

    def __init__(
        self,
        journal_dir: Path | str,
        *,
        client_cls: Callable[..., Any],
        device_cls: Callable[..., Any] = EphemeralDevice,
        environ: Optional[Mapping[str, str]] = None,
        client_settings: Optional[Mapping[str, Any]] = None,
        identity_salt: Optional[bytes | str] = None,
        log_scrubber: Optional[LogScrubber] = None,
        source: str = SOURCE,
        dry_run_registry_stamp: Optional[str] = None,
    ) -> None:
        self.journal_dir = Path(journal_dir)
        self.client_cls = client_cls
        self.device_cls = device_cls
        self.environ = os.environ if environ is None else environ
        self.client_settings = dict(client_settings or {})
        self._identity_salt = identity_salt
        self.log_scrubber = log_scrubber
        self.source = source
        self.dry_run_registry_stamp = dry_run_registry_stamp
        self._lock = threading.Lock()
        self._notes: dict[str, list[str]] = {}
        self._dry_run_journals: dict[str, DryRunJournal] = {}

    def journal_path(self, alias: str) -> Path:
        return self.journal_dir / f"{alias}{JOURNAL_SUFFIX}"

    def notes(self) -> dict[str, tuple[str, ...]]:
        """Per alias, operator notes: the e-mail login form, and design data that could not be read."""
        with self._lock:
            return {alias: tuple(items) for alias, items in self._notes.items()}

    def _note(self, alias: str, items: Iterable[str]) -> None:
        with self._lock:
            self._notes.setdefault(alias, []).extend(items)

    def _login(self, credentials: Credentials) -> Any:
        settings = dict(self.client_settings)
        if credentials.auth_string:
            device = self.device_cls(language="en", authentication_string=credentials.auth_string)
        else:
            device = self.device_cls(language="en")
            settings["allow_email_password_login"] = True
        client = self.client_cls(device=device, settings=settings)
        isolate_session(client)
        if credentials.email:
            ok = client.login(email=credentials.email, password=credentials.password)
        else:
            ok = client.login()
        if self.log_scrubber is not None:
            info = getattr(client, "info", None)
            self.log_scrubber.add_names([
                info.get("@Name") if isinstance(info, dict) else None,
                getattr(getattr(client, "user", None), "name", None),
            ])
        if not ok:
            raise LoginError("login failed")
        return client

    def _open(self, profile: AccountProfile, read_designs: bool,
              clock: Optional[Callable[[], datetime]] = None) -> tuple[Any, ClientSnapshotProvider, Any]:
        """Log ``profile`` in; return its client, snapshot provider and (if read) design data."""
        credentials = resolve_credentials(profile.credentials_secret, self.environ)
        if self.log_scrubber is not None:
            self.log_scrubber.add_secrets(credentials.values())
        if credentials.email:
            self._note(profile.alias, [EMAIL_FORM_NOTE])
        design, notes = None, ()
        try:
            client = self._login(credentials)
            if read_designs:
                design, notes = read_design_data(client)
        except LoginError:
            raise
        except Exception as error:  # never let a credential value reach the AccountRun reason
            raise AccountOpenError(
                f"{type(error).__name__}: {scrub(str(error), credentials.values())}") from None
        self._note(profile.alias, notes)
        options = {"clock": clock} if clock is not None else {}
        provider = ClientSnapshotProvider(client, profile.alias, source=self.source,
                                          identity_salt=self._identity_salt, **options)
        return client, provider, design

    def __call__(self, profile: AccountProfile) -> AccountContext:
        client, provider, design = self._open(profile, read_designs=True)
        return AccountContext(client, provider, self._journal(profile), design)

    def open_baseline(self, profile: AccountProfile,
                      clock: Optional[Callable[[], datetime]] = None) -> "BaselineObservation":
        """Log ``profile`` in and take one snapshot with read-only calls (SPEC-04, S04-R1).

        Reads no design data and opens no journal. The game version comes from
        ``server_game_version`` (S04-R3). Errors are raised as for ``__call__``;
        reads that returned no data are listed in ``failed_reads``.
        """
        client, provider, _ = self._open(profile, read_designs=False, clock=clock)
        snapshot = provider()
        return BaselineObservation(snapshot, server_game_version(client), provider.failed_reads)

    def open_reads(self, profile: AccountProfile) -> "ReadSession":
        """Log ``profile`` in and make no other call (SPEC-03 captures, ``run.py --capture-reads``).

        Returns the logged-in client, its game identity (``None`` without an
        identity salt or for an unknown id) and ``server_game_version``.
        Reads no design data, takes no snapshot and opens no journal. Errors
        are raised as for ``__call__``.
        """
        client, _, _ = self._open(profile, read_designs=False)
        identity = None
        if self._identity_salt is not None:
            try:
                identity = game_identity_fingerprint(getattr(getattr(client, "user", None), "id", None),
                                                     self._identity_salt)
            except SnapshotInputError:  # unknown id: identity stays unobserved
                identity = None
        return ReadSession(client, identity, server_game_version(client))

    def _journal(self, profile: AccountProfile) -> GameplayJournal:
        path = self.journal_path(profile.alias)
        if self.dry_run_registry_stamp is None:
            return GameplayJournal(path)
        journal = DryRunJournal(path, policy_id=profile.policy_id, registry_stamp=self.dry_run_registry_stamp)
        with self._lock:
            self._dry_run_journals[profile.alias] = journal
        return journal

    def commit_dry_runs(self, statuses: Mapping[str, str]) -> None:
        """Write every held dry-run event; ``statuses`` maps alias to its ``AccountRun.status``.

        Only an account whose status is ``completed`` gets the dry-run stamp.
        """
        with self._lock:
            journals = dict(self._dry_run_journals)
        for alias, journal in sorted(journals.items()):
            journal.commit(completed=statuses.get(alias) == "completed")


# --- Baselines (SPEC-04, S04-R1 and S04-R3) -----------------------------------------------------

# Attributes of the ``GetLatestVersion3`` ``Setting`` record that may name the current game version, in
# the order they are tried. The live server (G1 capture, 2026-11-12) returns none of the first three; it
# returns ``RecommendedVersion`` (``1.0``), so that is tried last. When none is present the client
# version is unobserved. ``MinimumClientVersion`` is deliberately not one of them: the oldest version
# the server accepts is not the version the observation was made on.
SERVER_VERSION_ATTRIBUTES = ("@LatestVersion", "@LatestClientVersion", "@LatestVersionIOS", "@RecommendedVersion")
# The server's settings data counter. The client version above stayed ``1.0`` live, so the counter is
# what tells two observations on different game data apart. Digits only; anything else is not recorded.
SERVER_DATA_VERSION_ATTRIBUTE = "@ServerSettingVersion"
SERVER_DATA_VERSION_PATTERN = re.compile(r"^[0-9]{1,12}$")
# A version string such as ``0.999.60``; anything else from the server is not recorded.
GAME_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}[A-Za-z0-9]{0,4}$")


@dataclass(frozen=True)
class ReadSession:
    """A logged-in client for read-only captures, its game identity and the server's game version."""

    client: Any
    game_identity: Optional[str]
    game_version: Optional[str]


@dataclass(frozen=True)
class BaselineObservation:
    """One account's baseline snapshot and the server's game version (``None`` when not observed)."""

    snapshot: AccountSnapshot
    game_version: Optional[str]
    failed_reads: tuple[str, ...] = ()


def server_game_version(client: Any) -> Optional[str]:
    """The game version in the server's latest-version setting the client already read, or ``None``.

    ``Client.login`` calls ``getLatestVersion3`` (``SettingService/GetLatestVersion3``, read-only) and
    stores the parsed reply in ``client.latestVersion``; its ``SettingService/GetLatestSetting/Setting``
    record is the one ``listRoomDesigns2`` and ``listAllDesigns4`` take their design versions from.

    The result is the first ``SERVER_VERSION_ATTRIBUTES`` value that looks like a version, followed by
    ``+settings.<n>`` when the record's ``ServerSettingVersion`` counter is all digits (``1.0+settings.42``),
    or ``settings.<n>`` alone when only the counter is present. This makes no call, and never falls back
    to the ``ClientVersion`` the client declares at login.
    """
    setting = getattr(client, "latestVersion", None)
    for key in ("SettingService", "GetLatestSetting", "Setting"):
        setting = setting.get(key) if isinstance(setting, Mapping) else None
    if not isinstance(setting, Mapping):
        return None
    version = None
    for attribute in SERVER_VERSION_ATTRIBUTES:
        value = setting.get(attribute)
        if isinstance(value, str) and GAME_VERSION_PATTERN.match(value.strip()):
            version = value.strip()
            break
    counter = setting.get(SERVER_DATA_VERSION_ATTRIBUTE)
    counter = counter.strip() if isinstance(counter, str) else None
    if counter and SERVER_DATA_VERSION_PATTERN.match(counter):
        return f"{version}+settings.{counter}" if version else f"settings.{counter}"
    return version


# --- Dry-run plan stamps (SPEC-05, S05-R3) ------------------------------------------------------

DRY_RUN_POLICY = "dry_run_policy"
DRY_RUN_REGISTRY = "dry_run_registry"
DRY_RUN_ID = "dry_run_id"
DRY_RUN_MAX_AGE = timedelta(hours=24)
DRY_RUN_PLANNED_PREFIX = "dry run: would call "
# Journal redaction treats 40+ character alphanumeric runs as tokens, so the stamp keeps 16 hex digits.
REGISTRY_STAMP_LENGTH = 16


def registry_hash(registry: AccountRegistry) -> str:
    """``sha256:`` plus the hex SHA-256 of the registry's canonical JSON (profiles in alias order)."""
    payload = {"version": REGISTRY_VERSION,
               "accounts": [registry.get(alias).to_dict() for alias in registry.aliases()]}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def registry_stamp(registry: AccountRegistry) -> str:
    """The first 16 hex digits of ``registry_hash``, as journaled in a dry-run stamp."""
    return registry_hash(registry).split(":", 1)[1][:REGISTRY_STAMP_LENGTH]


class DryRunJournal(GameplayJournal):
    """An account's journal during ``--fleet-dry-run``.

    Dry-run events are validated when appended (so a secret-shaped value is
    still refused at once) but held until ``commit``. ``read`` includes the
    held events, so the scheduler sees the same history as with a plain
    journal. ``commit(completed=True)`` writes them stamped in ``evidence``
    with the policy id, the registry stamp and a new dry-run id;
    ``commit(completed=False)`` writes them unchanged, as a plain journal
    would have. Events that are not dry-run events are written at once.
    """

    def __init__(self, path: Path | str, *, policy_id: str, registry_stamp: str) -> None:
        super().__init__(path)
        self.policy_id = policy_id
        self.registry_stamp = registry_stamp
        self._held: list[GameplayEvent] = []
        self._held_lock = threading.Lock()

    def append(self, event: GameplayEvent) -> None:
        if not event.dry_run:
            super().append(event)
            return
        event.to_json_line()  # validate and refuse secret-shaped values now, as a plain append would
        with self._held_lock:
            self._held.append(event)

    def read(self) -> list[GameplayEvent]:
        with self._held_lock:
            held = list(self._held)
        return super().read() + held

    def commit(self, *, completed: bool) -> int:
        """Write the held events; return how many were written."""
        with self._held_lock:
            held, self._held = self._held, []
        stamp = ({DRY_RUN_POLICY: self.policy_id, DRY_RUN_REGISTRY: self.registry_stamp,
                  DRY_RUN_ID: new_attempt_id()} if completed else None)
        for event in held:
            super().append(replace(event, evidence={**event.evidence, **stamp}) if stamp else event)
        return len(held)


@dataclass(frozen=True)
class DryRunPlan:
    """An account's latest completed, stamped dry run."""

    alias: str
    policy_id: str
    dry_run_id: str
    finished_at: datetime
    events: tuple[GameplayEvent, ...]

    def would_call(self) -> tuple[GameplayEvent, ...]:
        """The planned actions that passed their preconditions, in plan order."""
        return tuple(event for event in self.events if event.reason.startswith(DRY_RUN_PLANNED_PREFIX))


def _finished(event: GameplayEvent) -> datetime:
    return datetime.fromisoformat(event.finished_at)


def latest_dry_run(events: Iterable[GameplayEvent], profile: AccountProfile, stamp: str, now: datetime,
                   max_age: timedelta = DRY_RUN_MAX_AGE) -> tuple[Optional[DryRunPlan], Optional[str]]:
    """The account's latest stamped dry run, or ``None`` and why it does not qualify.

    A dry run is recognised by its stamp: dry-run events of ``profile.alias``
    whose ``evidence`` holds a ``dry_run_id``, written by ``--fleet-dry-run``
    only for an account whose dry run completed. The latest batch (by its
    last ``finished_at``) qualifies when it names ``profile.policy_id`` and
    the current registry ``stamp``, and finished within ``max_age`` before
    ``now`` (not after it). An older batch never stands in for the latest.
    """
    batches: dict[str, list[GameplayEvent]] = {}
    for event in events:
        evidence = event.evidence or {}
        if event.dry_run and not event.attempted and event.account_alias == profile.alias \
                and isinstance(evidence.get(DRY_RUN_ID), str):
            batches.setdefault(evidence[DRY_RUN_ID], []).append(event)
    if not batches:
        return None, "no completed --fleet-dry-run plan in its journal"
    dry_run_id, batch = max(batches.items(), key=lambda item: max(_finished(event) for event in item[1]))
    finished_at = max(_finished(event) for event in batch)
    evidence = batch[0].evidence
    if evidence.get(DRY_RUN_POLICY) != profile.policy_id:
        return None, f"its latest dry run did not use policy {profile.policy_id!r}"
    if evidence.get(DRY_RUN_REGISTRY) != stamp:
        return None, "its latest dry run was of a different registry"
    if finished_at > now:
        return None, "its latest dry run is dated in the future"
    if now - finished_at > max_age:
        hours = int(max_age.total_seconds() // 3600)
        return None, f"its latest dry run is older than {hours} hours"
    return DryRunPlan(profile.alias, profile.policy_id, dry_run_id, finished_at, tuple(batch)), None


# --- Execute gate (SPEC-05, S05-R1, S05-R4 to S05-R6) -------------------------------------------

EXECUTE_SOURCE = "run.py --fleet-execute"
CANARY, FULL = "canary", "full"
CAP_KINDS = ("minerals", "gas")
DEFAULT_HALT_SHARE = 0.0  # any stopped account halts the fleet (S05-Q2 safe default)
# A battle rearms first (RebuildAmmo3) at a cost the repo does not record, so it may only start while at least
# this much of each capped mineral and gas budget is left this period. It is a floor, not an estimate.
BATTLE_REARM_ALLOWANCE = 1


class ExecutionRefusedError(UncertainPriorAttemptError):
    """The gate refused an action before any call.

    It subclasses the runner's refusal so that ``DailyScheduler`` stops the
    account through its existing refusal path and keeps the results so far.
    """


class ExecutionGate:
    """What an authorized execute run may do: actions, time, mode and per-account budgets."""

    def __init__(self, authorization: Authorization, registry: AccountRegistry, mode: str,
                 clock: Callable[[], datetime]) -> None:
        if mode not in (CANARY, FULL):
            raise ValueError("mode must be canary or full")
        self.authorization = authorization
        self.registry = registry
        self.mode = mode
        self.clock = clock

    @property
    def canary(self) -> bool:
        return self.mode == CANARY

    def refusal(self, alias: str, action_id: str,
                events: Optional[Callable[[], Iterable[GameplayEvent]]] = None) -> Optional[str]:
        """Why ``alias`` may not run ``action_id`` now, or ``None``.

        ``events`` reads the account's journal. A battle is checked against the
        authorization's battle cap with those events (S08-R2), and is refused
        when there is no reader or the journal cannot be read: the cap fails closed.
        """
        battle_events = None
        if action_id == BATTLE_ACTION_ID:
            if events is None:
                return "the battle cap cannot be checked without the account's journal; refusing"
            try:
                battle_events = list(events())
            except Exception as error:  # an unreadable journal must never leave the cap unchecked
                return f"cannot read the journal to check the battle cap ({type(error).__name__}); refusing"
        reasons = check_authorization(self.authorization, self.registry, self.clock(), [alias], [action_id],
                                      self.mode, battle_events=battle_events)
        return "; ".join(reasons) if reasons else None

    def budgets(self, profile: AccountProfile) -> dict[str, int]:
        """Per-period budgets: the tighter of the registry's and the authorization's, starbux always 0."""
        budgets = {"starbux": 0}
        for kind in CAP_KINDS:
            cap = self.authorization.cap(profile.alias, kind)
            limit = profile.budget(kind)
            budgets[kind] = cap if limit is None else min(limit, cap)
        return budgets

    def profile(self, profile: AccountProfile) -> AccountProfile:
        """``profile`` with the gate's budgets."""
        return replace(profile, budgets=self.budgets(profile))

    def max_actions(self, profile: AccountProfile) -> int:
        """Actions the scheduler may execute: one in canary mode, else the profile's ``max_actions``.

        The policy still plans up to ``max_actions`` requests, so that the
        canary is chosen from the whole plan, not from its first request.
        """
        return 1 if self.canary else profile.max_actions


def _candidate(action_id: str, params: Mapping[str, Any]) -> str:
    return " ".join([action_id, *(str(value) for value in params.values())])


class AuthorizedPolicy:
    """A progression policy whose plan keeps only what the gate allows.

    Requests the authorization does not allow, and any request with a planned
    starbux cost, move to ``rejected``. In canary mode only the first
    remaining safe-to-retry request is kept. ``journal`` supplies the events
    the battle cap is checked with.
    """

    def __init__(self, policy: ProgressionPolicy, gate: ExecutionGate,
                 journal: Optional[GameplayJournal] = None) -> None:
        self.policy = policy
        self.gate = gate
        self.journal = journal
        self.name = policy.name

    def plan(self, snapshot: AccountSnapshot, design: DesignData, config: PolicyConfig) -> Plan:
        plan = self.policy.plan(snapshot, design, config)
        kept, rejected = [], list(plan.rejected)
        events = self.journal.read if self.journal is not None else None
        for request in plan.requests:
            label = _candidate(request.action_id, request.params)
            refusal = self.gate.refusal(snapshot.account_alias, request.action_id, events)
            if refusal is not None:
                rejected.append(Rejection(label, f"not authorized: {refusal}"))
            elif planned_cost(request.expected_cost).get("starbux"):
                rejected.append(Rejection(label, "starbux spending is disabled in execute runs"))
            else:
                kept.append(request)
        if self.gate.canary:
            canary = next((request for request in kept
                           if get_spec(request.action_id).idempotency is Idempotency.SAFE_TO_RETRY), None)
            rejected += [Rejection(_candidate(request.action_id, request.params),
                                   "canary: only the first safe-to-retry action runs")
                         for request in kept if request is not canary]
            kept = [canary] if canary is not None else []
        return replace(plan, requests=tuple(kept), rejected=tuple(rejected))


class GatedActionRunner(ActionRunner):
    """``ActionRunner`` that refuses, before any call, what the gate does not allow.

    Refused: every action once ``halted()`` returns a fleet halt reason (so a
    running account stops between actions when another account halted the
    fleet); an action outside the authorization (account, action, time, mode,
    or the battle cap checked with this journal's events); any planned
    starbux cost; a non-idempotent action other than a battle that carries no
    planned cost while the account has caps, because its spend could not be
    checked; a battle while any capped mineral or gas budget has less than
    ``BATTLE_REARM_ALLOWANCE`` left this period (``Client.runBattleEndToEnd``
    first rearms with ``rebuildAmmo``, ``RebuildAmmo3``, whose cost the repo
    does not record, so a battle has no planned cost and is held to this
    allowance and its battle cap instead; its observed spend is charged
    afterwards like any other); and an action whose planned cost plus the
    spend already charged this period would exceed a budget. A refusal is
    journaled as ``skipped`` and raised as ``ExecutionRefusedError``. A retry
    after an uncertain attempt is never allowed.
    """

    def __init__(self, gate: ExecutionGate, budgets: Mapping[str, int], period: timedelta, *args: Any,
                 halted: Callable[[], Optional[str]] = lambda: None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate = gate
        self.budgets = dict(budgets)
        self.period = period
        self.halted = halted

    def _refusal(self, action_id: str, planned: Mapping[str, int]) -> Optional[str]:
        halt = self.halted()
        if halt is not None:
            return halt
        refusal = self.gate.refusal(self.account_alias, action_id, self.journal.read)
        if refusal is not None:
            return refusal
        if planned.get("starbux"):
            return "starbux spending is disabled in execute runs"
        if (not planned and action_id != BATTLE_ACTION_ID
                and get_spec(action_id).idempotency is Idempotency.NOT_IDEMPOTENT
                and any(self.budgets.get(kind) is not None for kind in CAP_KINDS)):
            return "a non-idempotent action without a planned cost cannot be checked against the caps"
        spent = spent_in_period(self.journal, self.account_alias, self.clock() - self.period)
        if action_id == BATTLE_ACTION_ID:
            for kind in CAP_KINDS:
                limit = self.budgets.get(kind)
                if limit is not None and limit - spent.get(kind, 0) < BATTLE_REARM_ALLOWANCE:
                    return (f"a battle may rearm at an unrecorded {kind} cost, and {max(limit - spent.get(kind, 0), 0)} "
                            f"{kind} of the cap is left (it needs at least {BATTLE_REARM_ALLOWANCE})")
        for kind, amount in sorted(planned.items()):
            limit = self.budgets.get(kind)
            if limit is not None and spent.get(kind, 0) + amount > limit:
                return f"{kind} cap would be exceeded ({spent.get(kind, 0)} spent + {amount} planned > {limit})"
        return None

    def run(self, action_id: str, params: Optional[Mapping[str, Any]] = None, *, dry_run: bool = False,
            allow_after_uncertain: bool = False, expected_cost: Optional[Mapping[str, Any]] = None):
        refusal = self._refusal(action_id, planned_cost(expected_cost))
        if refusal is not None:
            self._record(get_spec(action_id), self.clock(), dict(params or {}), outcome="skipped",
                         reason=f"execute gate refused: {refusal}", attempted=False,
                         error_class="ExecutionRefusedError", dry_run=dry_run)
            raise ExecutionRefusedError(refusal)
        return super().run(action_id, params, dry_run=dry_run, allow_after_uncertain=False,
                           expected_cost=expected_cost)


class _ExecuteGateMixin:
    """The execute gate on top of a fleet orchestrator's own per-account steps.

    ``_run_scheduler`` is the orchestrator's, with the gate's budgets,
    ``AuthorizedPolicy`` and ``GatedActionRunner``; each account's
    ``ScheduleReport`` is kept in ``reports``. Every account that ends
    ``stopped`` or ``error`` counts as stopped; once more than ``halt_share``
    of the enabled accounts stopped, ``_stop_requested`` holds, so accounts not
    yet started are ``not-run``, and every running account's
    ``GatedActionRunner`` refuses its next action, so it stops between actions
    as with the stop file (in concurrent mode, an action already in flight
    finishes). The fleet run's reasons then name the halt, not the stop file,
    and the halt is the fleet's ``stop_reason``.
    """

    def __init__(self, registry: AccountRegistry, factory: Callable[[AccountProfile], AccountContext], *,
                 gate: ExecutionGate, halt_share: float = DEFAULT_HALT_SHARE, **kwargs: Any) -> None:
        if isinstance(halt_share, bool) or not isinstance(halt_share, (int, float)) or not 0 <= halt_share < 1:
            raise ValueError("halt_share must be at least 0 and below 1")
        super().__init__(registry, factory, **kwargs)
        self.gate = gate
        self.halt_share = halt_share
        self._gate_lock = threading.Lock()
        self.reports: dict[str, ScheduleReport] = {}
        self._stopped: set[str] = set()
        self.halt_reason: Optional[str] = None

    def _finish(self, profile: AccountProfile, started: float, status: str, reason: Optional[str],
                **fields: Any) -> AccountRun:
        run = super()._finish(profile, started, status, reason, **fields)
        if status in ("stopped", "error"):
            enabled = max(len(self.registry.enabled()), 1)
            with self._gate_lock:
                self._stopped.add(profile.alias)
                if self.halt_reason is None and len(self._stopped) / enabled > self.halt_share:
                    self.halt_reason = (f"fleet halted: {len(self._stopped)} of {enabled} accounts stopped "
                                        f"(more than {self.halt_share:.0%})")
        return run

    def _halted(self) -> Optional[str]:
        with self._gate_lock:
            return self.halt_reason

    def _stop_requested(self) -> bool:
        return super()._stop_requested() or self._halted() is not None

    def _run_scheduler(self, profile: AccountProfile, context: AccountContext, execute: bool,
                       started: float, claims: Optional[FleetClaims] = None) -> AccountRun:
        identity = claims.identity(profile.alias) if claims is not None else None
        try:
            gated = self.gate.profile(profile)
            spent = spent_in_period(context.journal, profile.alias, self.clock() - self.period)
            policy_config, remaining = budgeted_policy_config(gated, spent, self.base_policy_config)
            provider = (claims.pinned_provider(profile.alias, context.snapshot_provider) if claims is not None
                        else context.snapshot_provider)
            runner = GatedActionRunner(self.gate, gated.budgets, self.period, profile.alias, context.client,
                                       provider, context.journal, clock=self.clock, halted=self._halted)
            scheduler = DailyScheduler(
                runner, AuthorizedPolicy(get_policy(profile.policy_id), self.gate, context.journal), context.design,
                policy_config,
                SchedulerConfig(max_actions=self.gate.max_actions(gated), period=self.period,
                                stop_file=self.stop_file),
                clock=self.clock,
            )
            report = scheduler.run(execute=execute)
        except Exception as error:  # one account's failure must not stop the others
            return self._finish(profile, started, "error", error_reason(error))
        with self._gate_lock:
            self.reports[profile.alias] = report
        attempted = sum(1 for result in report.results if result.attempted)
        verified = sum(1 for result in report.results if result.outcome == "verified")
        return self._finish(
            profile, started, "completed" if report.completed else "stopped", report.stop_reason,
            attempted=attempted, verified=verified, planned=len(report.plan.requests),
            spent_this_period=spent, budget_remaining=remaining, game_identity=identity,
        )

    def run(self, *, execute: bool = False) -> FleetRun:
        with self._gate_lock:
            self.reports, self._stopped, self.halt_reason = {}, set(), None
        fleet_run = super().run(execute=execute)
        if self.halt_reason is None or (self.stop_file is not None and self.stop_file.exists()):
            return fleet_run
        stop_text = f"fleet stop file present: {self.stop_file}"

        def relabel(reason: Optional[str]) -> Optional[str]:
            return reason.replace(stop_text, self.halt_reason) if reason else reason

        accounts = tuple(replace(run, reason=relabel(run.reason)) if run.status == "not-run" else run
                         for run in fleet_run.accounts)
        # The halt is the fleet's stop reason even when no account was left to skip.
        return replace(fleet_run, accounts=accounts, stop_reason=relabel(fleet_run.stop_reason) or self.halt_reason)


class ExecuteFleetOrchestrator(_ExecuteGateMixin, FleetOrchestrator):
    """``FleetOrchestrator`` under an ``ExecutionGate``."""


class ConcurrentExecuteFleetOrchestrator(_ExecuteGateMixin, ConcurrentFleetOrchestrator):
    """``ConcurrentFleetOrchestrator`` under an ``ExecutionGate``."""


def canary_choice(actions: Sequence[str], gate: ExecutionGate, alias: str,
                  events: Optional[Callable[[], Iterable[GameplayEvent]]] = None) -> Optional[str]:
    """The action a canary run would take from ``actions`` (plan order), or ``None``."""
    for action_id in actions:
        if (get_spec(action_id).idempotency is Idempotency.SAFE_TO_RETRY
                and gate.refusal(alias, action_id, events) is None):
            return action_id
    return None
