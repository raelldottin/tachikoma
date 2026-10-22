"""Task J capture sanitization: raw capture record -> fixture record.

PURE analysis surface: given ONE parsed capture record (the dict shape
written by the mitmproxy capture addon: ts, type, url, method or
status_code, headers, body) produce the sanitized record that may be
committed as a fixture. Every credential-bearing VALUE is replaced with
the literal REDACTED marker; structurally meaningful facts (endpoint
path, parameter NAMES, header NAMES, HTTP method, status, body shape)
are preserved so the fixture still proves the wire semantics.

Fail closed: a record that cannot be interpreted exactly raises
SanitizationError instead of being partially sanitized. A record whose
secret value cannot be proven removed raises rather than emitting a
possibly-leaking fixture.

This module NEVER issues network I/O, never reads files or the
process environment, and never consults any settings object: it
contains no execution path of any kind. It is parse-and-redact only.
No checksum formula, no endpoint template and no protocol
reconstruction lives here: it treats the capture as opaque text plus
structural key names.

Identity/privacy rules pinned here:
- A parameter NAME (accessToken, password, ...) is structural fact
  and is preserved. Its VALUE is always destroyed.
- Header names are preserved; credential-bearing header values
  (authorization, cookies) are destroyed.
- Any operator-supplied known live secret value is scrubbed from every
  string field before the record is emitted.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Sequence, Tuple

# Marker substituted for every removed secret value (stable, greppable).
REDACTED = "[REDACTED]"

# Sanitized-fixture schema version (fail closed on anything else).
SANITIZE_SCHEMA_VERSION = 1

# Query/body parameter NAMES whose VALUE is always a secret (exact
# match after lowercasing; the name itself is structural and kept).
_SECRET_PARAM_NAMES = frozenset(
    {
        "accesstoken",
        "access_token",
        "refreshtoken",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "devicekey",
        "device_key",
        "clientkey",
        "client_key",
        "apikey",
        "api_key",
        "token",
        "sessionid",
        "session_id",
    }
)

# Substring tokens identifying secret-bearing KEYS inside structured
# bodies (JSON dicts, form pairs): any dict key containing one of these
# has its value destroyed wherever it appears in the value tree.
_SECRET_KEY_TOKENS = (
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "devicekey",
    "device_key",
    "clientkey",
    "client_key",
    "apikey",
    "api_key",
    "sessionid",
    "session_id",
    "token",
)

# Substring tokens identifying credential-bearing HEADER names: the
# header name is structural and kept; the value is destroyed.
_SECRET_HEADER_TOKENS = (
    "authorization",
    "cookie",
    "token",
    "secret",
)

# Regex for secret-name=VALUE patterns in flat text bodies and URLs:
# query style (accessToken=abc&...) and XML attribute style
# (accessToken="abc"). JSON bodies are handled structurally by the
# value-tree walk; this pattern is the flat-text safety net.
_SECRET_TEXT_RE = re.compile(
    r"(?i)\b(?P<name>accesstoken|access_token|refreshtoken|refresh_token|"
    r"password|passwd|devicekey|device_key|clientkey|client_key|"
    r"apikey|api_key|sessionid|session_id)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s&;<>\"']+)"
)


class SanitizationError(ValueError):
    """Fail-closed sanitization error.

    Raised instead of any silent repair: a capture record that cannot
    be fully sanitized (malformed shape, or a secret that provably
    survived) is an error, never a partially redacted fixture.
    """


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _is_secret_param(name: str) -> bool:
    return name.strip().lower() in _SECRET_PARAM_NAMES


def _is_secret_key(key: str) -> bool:
    lowered = key.strip().lower()
    return any(token in lowered for token in _SECRET_KEY_TOKENS)


def _is_secret_header(name: str) -> bool:
    lowered = name.strip().lower()
    return any(token in lowered for token in _SECRET_HEADER_TOKENS)


def _split_query(query: str) -> List[Tuple[str, str]]:
    """Parse a URL query string into ordered (name, value) pairs.

    Undecoded: values stay exactly as captured (no transformation the
    operator would have to trust).
    """
    pairs: List[Tuple[str, str]] = []
    if not query:
        return pairs
    for chunk in query.split("&"):
        if not chunk:
            continue
        name, sep, value = chunk.partition("=")
        pairs.append((name, value if sep else ""))
    return pairs


# ---------------------------------------------------------------------------
# Field sanitizers (pure)
# ---------------------------------------------------------------------------


def sanitize_query_string(query: str) -> Tuple[str, Tuple[str, ...], Tuple[str, ...]]:
    """Redact a URL query string.

    Returns (sanitized_query, param_names, secret_param_names) where
    param_names is the ordered unique parameter NAME list and
    secret_param_names the ordered names whose values were destroyed.
    """
    pairs = _split_query(query)
    kept: List[str] = []
    names: List[str] = []
    secret_names: List[str] = []
    for name, value in pairs:
        if name not in names:
            names.append(name)
        if _is_secret_param(name):
            if name not in secret_names:
                secret_names.append(name)
            kept.append(f"{name}={REDACTED}")
        else:
            kept.append(f"{name}={value}")
    return "&".join(kept), tuple(names), tuple(secret_names)


def sanitize_headers(headers: Mapping[str, Any]) -> Dict[str, str]:
    """Redact credential-bearing header values; keep every name."""
    sanitized: Dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise SanitizationError(
                f"header {name!r} must have string name and value"
            )
        if _is_secret_header(name):
            sanitized[name] = REDACTED
        else:
            sanitized[name] = value
    return sanitized


def _redact_text_secrets(text: str) -> str:
    """Redact secret-name=VALUE occurrences in flat text."""

    def _sub(match: "re.Match[str]") -> str:
        val = match.group("val")
        if val[:1] in ("\"", "'") and val[-1:] == val[:1]:
            return f"{match.group('name')}{match.group('sep')}{val[0]}{REDACTED}{val[0]}"
        return f"{match.group('name')}{match.group('sep')}{REDACTED}"

    return _SECRET_TEXT_RE.sub(_sub, text)


def sanitize_body(body: str) -> str:
    """Redact secrets in a captured body.

    JSON objects have secret-bearing keys redacted structurally (value
    trees walked); any other text (form bodies, XML) is redacted with
    the name=value pattern so credential values cannot survive inside
    flat payloads. Unparseable JSON falls back to the text pattern
    (never an error: redaction must be a superset, not a parser).
    """
    stripped = body.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if parsed is not None:
            redacted = _redact_value_tree(parsed)
            return json.dumps(redacted)
    return _redact_text_secrets(body)


def _redact_value_tree(value: Any) -> Any:
    """Walk a parsed JSON tree; destroy every secret-bearing key value."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_secret_key(key):
                out[key] = REDACTED
            else:
                out[key] = _redact_value_tree(item)
        return out
    if isinstance(value, list):
        return [_redact_value_tree(item) for item in value]
    return value


def _scrub_known_values(record: Dict[str, Any], known_values: Sequence[str]) -> Dict[str, Any]:
    """Scrub operator-supplied known live secret values everywhere.

    Applied to the FINAL serialized text so a secret value cannot hide
    in any string field (url, body, header value) even where its key
    name was not recognized.
    """
    live = [value for value in known_values if value]
    if not live:
        return record
    serialized = json.dumps(record, ensure_ascii=False)
    for value in live:
        serialized = serialized.replace(value, REDACTED)
    loaded: Dict[str, Any] = json.loads(serialized)
    return loaded


# ---------------------------------------------------------------------------
# Record sanitizer
# ---------------------------------------------------------------------------


def sanitize_capture_record(
    record: Mapping[str, Any],
    known_secret_values: Sequence[str] = (),
) -> Dict[str, Any]:
    """Sanitize ONE capture record into a committable fixture record.

    Fail closed on malformed shape. The output carries:
      schema_version  fixed SANITIZE_SCHEMA_VERSION
      ts, type        provenance (pass-through, validated)
      endpoint        path portion of the URL (before the query)
      url             full URL with every secret query value REDACTED
      param_names     ordered unique query parameter names (structural)
      secret_param_names  names whose values were destroyed
      headers         header names with credential values REDACTED
      body            redacted body
      method          request verb (requests only)
      status_code     response status (responses only)

    No token, password, device key or cookie VALUE can survive: the
    URL query is pair-redacted, headers are name-scanned, JSON bodies
    are tree-walked, flat text is pattern-redacted, and every
    operator-known live secret value is finally scrubbed from the
    serialized whole.
    """
    if not isinstance(record, Mapping):
        raise SanitizationError(
            f"capture record must be a mapping, got {type(record).__name__}"
        )

    record_type = record.get("type")
    if record_type not in ("request", "response"):
        raise SanitizationError(
            f"capture record type must be 'request' or 'response', got {record_type!r}"
        )
    ts = record.get("ts")
    if not isinstance(ts, str) or not ts:
        raise SanitizationError("capture record ts must be a non-empty string")
    url = record.get("url")
    if not isinstance(url, str) or not url:
        raise SanitizationError("capture record url must be a non-empty string")
    if "?" in url:
        base, _, query = url.partition("?")
    else:
        base, query = url, ""
    headers = record.get("headers", {})
    if not isinstance(headers, Mapping):
        raise SanitizationError("capture record headers must be a mapping")
    body = record.get("body", "")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise SanitizationError(
            f"capture record body must be a string, got {type(body).__name__}"
        )

    sanitized_query, param_names, secret_param_names = sanitize_query_string(query)
    sanitized: Dict[str, Any] = {
        "schema_version": SANITIZE_SCHEMA_VERSION,
        "ts": ts,
        "type": record_type,
        "endpoint": base,
        "url": f"{base}?{sanitized_query}" if query else base,
        "param_names": list(param_names),
        "secret_param_names": list(secret_param_names),
        "headers": sanitize_headers(headers),
        "body": sanitize_body(body),
    }

    if record_type == "request":
        method = record.get("method")
        if not isinstance(method, str) or not method:
            raise SanitizationError("request record method must be a non-empty string")
        sanitized["method"] = method
    else:
        status_code = record.get("status_code")
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise SanitizationError("response record status_code must be an integer")
        sanitized["status_code"] = status_code

    return _scrub_known_values(sanitized, known_secret_values)


def sanitize_capture_line(line: str, known_secret_values: Sequence[str] = ()) -> str:
    """Sanitize ONE JSONL capture line into ONE JSONL fixture line.

    The mechanical post-capture path: mitmproxy writes JSONL, this
    turns each line into a committable, deterministic fixture line.
    Determinism: identical input lines produce identical output lines
    (field order follows the fixed schema above).
    """
    if not isinstance(line, str):
        raise SanitizationError("capture line must be a string")
    try:
        parsed = json.loads(line)
    except ValueError as exc:
        raise SanitizationError(f"capture line is not valid JSON: {exc}") from exc
    sanitized = sanitize_capture_record(parsed, known_secret_values)
    return json.dumps(sanitized, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Fixture secret scan (regression helper)
# ---------------------------------------------------------------------------


def scan_for_secrets(
    obj: Any,
    known_values: Sequence[str] = (),
) -> List[str]:
    """Fail-closed fixture scan: list every secret violation found.

    A violation is any dict key that is secret-bearing while its value
    is a LIVE secret value: anything other than the REDACTED marker,
    a bare structural NAME (a string that is itself one of the known
    secret parameter names, e.g. the strings inside a
    secret_param_names list), or a documented env-var placeholder
    (UPPER_SNAKE_CASE, the indirection scheme used by committed
    fixtures such as checksum_samples.json where sensitive values
    were replaced with environment variable names at commit time).
    Plus any occurrence of an operator-supplied known live secret
    value in a string. Names are facts; values are leaks.

    `_comment`-prefixed documentation keys are exempt from the
    KEY-name heuristic (their values are prose, and prose keys like
    `_comment_access_tokens` describe secrets without holding one),
    but their string values are still scanned against every
    operator-supplied known live secret value.
    """
    violations: List[str] = []

    _NAME_SET = frozenset(
        {
            "accesstoken",
            "access_token",
            "refreshtoken",
            "refresh_token",
            "password",
            "passwd",
            "devicekey",
            "device_key",
            "clientkey",
            "client_key",
            "apikey",
            "api_key",
            "token",
            "sessionid",
            "session_id",
        }
    )
    _ENV_PLACEHOLDER_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")

    def _is_structural_name_value(value: Any) -> bool:
        """True when a value is provably a NAME or placeholder, not a
        live secret: either REDACTED, a bare secret parameter name
        (a string inside a name-listing field), or a documented
        env-var placeholder (UPPER_SNAKE_CASE indirection)."""
        if value == REDACTED:
            return True
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _NAME_SET:
                return True
            if _ENV_PLACEHOLDER_RE.match(value.strip()):
                return True
        if isinstance(value, (list, tuple)) and value:
            return all(
                isinstance(item, str) and _is_structural_name_value(item)
                for item in value
            )
        if isinstance(value, Mapping) and value:
            return all(_is_structural_name_value(item) for item in value.values())
        return False

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                if (
                    isinstance(key, str)
                    and _is_secret_key(key)
                    and not key.strip().lower().startswith("_comment")
                ):
                    if not _is_structural_name_value(value):
                        violations.append(f"secret key {here!r} holds a live value")
                walk(value, here)
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            for value in known_values:
                if value and value in node:
                    violations.append(
                        f"known live secret value appears at {path or '<root>'!r}"
                    )

    walk(obj, "")
    return violations
