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
"""

from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote, quote_plus

from sdk.client import _extract_collection
from sdk.device import Device
from sdk.fleet.accounts import AccountProfile
from sdk.fleet.orchestrator import AccountContext
from sdk.journal import GameplayJournal
from sdk.policy import DesignData
from sdk.redaction import redact_secrets
from sdk.state import ClientSnapshotProvider

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
    before login, and its display names after login.
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
    ) -> None:
        self.journal_dir = Path(journal_dir)
        self.client_cls = client_cls
        self.device_cls = device_cls
        self.environ = os.environ if environ is None else environ
        self.client_settings = dict(client_settings or {})
        self._identity_salt = identity_salt
        self.log_scrubber = log_scrubber
        self.source = source
        self._lock = threading.Lock()
        self._notes: dict[str, list[str]] = {}

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

    def __call__(self, profile: AccountProfile) -> AccountContext:
        credentials = resolve_credentials(profile.credentials_secret, self.environ)
        if self.log_scrubber is not None:
            self.log_scrubber.add_secrets(credentials.values())
        if credentials.email:
            self._note(profile.alias, [EMAIL_FORM_NOTE])
        try:
            client = self._login(credentials)
            design, notes = read_design_data(client)
        except LoginError:
            raise
        except Exception as error:  # never let a credential value reach the AccountRun reason
            raise AccountOpenError(
                f"{type(error).__name__}: {scrub(str(error), credentials.values())}") from None
        self._note(profile.alias, notes)
        provider = ClientSnapshotProvider(client, profile.alias, source=self.source,
                                          identity_salt=self._identity_salt)
        return AccountContext(client, provider, GameplayJournal(self.journal_path(profile.alias)), design)
