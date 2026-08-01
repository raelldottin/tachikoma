#!/usr/bin/env python3
"""Checksum research harness for CollectMarker2, RebuildAmmo3, and UserEmailPasswordAuthorize4.

Loads captured checksum samples from tests/fixtures/checksum_samples.json,
resolves session-specific fields (device key, email, salt, access tokens)
from environment variables, and tests candidate checksum formulas against
all samples.

A valid formula MUST reproduce every captured checksum exactly.

Security: Never prints access tokens, device keys, emails, salt, or the
full candidate preimage. Only prints MATCH / NO MATCH per sample.

Usage:
    # Set required env vars (real values from local game files):
    export PSS_DEVICE_KEY="..."
    export PSS_EMAIL="..."
    export PSS_CHECKSUM_SALT="..."
    export PSS_AT_COLLECT_1="..."  # access token for sample collect_1
    # ... etc for each sample

    # Run the lab:
    python3 scripts/checksum_lab.py

    # Add a custom formula:
    python3 scripts/checksum_lab.py --formula my_formula
"""

import hashlib
import itertools
import json
import os
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from urllib.parse import quote


FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "checksum_samples.json"


class ResultKind(Enum):
    PREIMAGE = auto()
    DIGEST = auto()


@dataclass(frozen=True)
class CandidateResult:
    kind: ResultKind
    value: bytes | str


def evaluate(result: CandidateResult) -> str:
    """Evaluate a candidate result to its final hex digest for comparison.

    PREIMAGE: hash the value as UTF-8 bytes (or raw bytes if already bytes)
    DIGEST: use the value directly as the hex digest (already computed)
    """
    if result.kind is ResultKind.DIGEST:
        return str(result.value).lower()

    value = result.value
    if isinstance(value, str):
        value = value.encode("utf-8")

    return hashlib.md5(value).hexdigest()


def load_fixture():
    """Load checksum samples from the fixture file."""
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def resolve_session_fields(fixture):
    """Resolve session-specific fields from environment variables.

    Returns a dict with real values, or raises if env vars are missing.
    """
    sf = fixture["session_fields"]

    required = {
        "device_key": os.environ.get(sf["device_key_env"]),
        "email": os.environ.get(sf["email_env"]),
        "salt": os.environ.get(sf["salt_env"]),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"ERROR: Missing environment variables: {missing}", file=sys.stderr)
        print(f"Set: {sf['device_key_env']}, {sf['email_env']}, {sf['salt_env']}", file=sys.stderr)
        print("And one PSS_AT_* per sample (see access_token_map in fixture).", file=sys.stderr)
        sys.exit(1)

    result = dict(required)
    result["suffix"] = sf["suffix"]

    # Resolve per-sample access tokens
    at_map = fixture["access_token_map"]
    for sample_id, env_var in at_map.items():
        token = os.environ.get(env_var)
        if not token:
            print(f"ERROR: Missing env var {env_var} for sample {sample_id}", file=sys.stderr)
            sys.exit(1)
        result[f"access_token_{sample_id}"] = token

    return result


def get_sample_params(sample, session):
    """Extract all available fields for a sample as a dict of labeled values."""
    at_key = f"access_token_{sample['id']}"
    return {
        "device_key": session["device_key"],
        "email": session["email"],
        "endpoint": sample["endpoint"],
        "param_name": sample["param_name"],
        "param_value": sample["param_value"],
        "clientDateTime": sample["clientDateTime"],
        "accessToken": session[at_key],
        "salt": session["salt"],
        "suffix": session["suffix"],
    }


# ─── Value transformations ────────────────────────────────────────────

def to_ticks(dt_str: str) -> int:
    """Convert ISO datetime to .NET ticks (100ns since 0001-01-01)."""
    from datetime import datetime
    dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    epoch = datetime(1, 1, 1)
    return int((dt - epoch).total_seconds() * 10000000)


def to_unix_ts(dt_str: str) -> int:
    """Convert ISO datetime to Unix timestamp."""
    from datetime import datetime
    dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    return int(dt.timestamp())


def checksum_time_for_date(dt_str: str) -> int:
    """Replicate ChecksumTimeForDate from sdk/security.py."""
    ticks = to_ticks(dt_str)
    def first_stub(dt):
        return int((dt & 0x3FFFFFFFFFFFFFFF) // 0x989680) % 60
    def second_stub(dt):
        return int((dt & 0x3FFFFFFFFFFFFFFF) // 0x23C34600) % 60
    return first_stub(ticks) * second_stub(ticks)


def derive_savy_checksum(device_key: str, suffix: str) -> str:
    """SavyChecksum = md5(deviceKey + 'savysoda') per IL2CPP pattern."""
    return hashlib.md5((device_key + suffix).encode('utf-8')).hexdigest()


def derive_checksum_key(device_key: str, salt: str) -> str:
    """ChecksumKey = md5(deviceKey + salt) per IL2CPP pattern."""
    return hashlib.md5((device_key + salt).encode('utf-8')).hexdigest()


# ─── Candidate formulas ──────────────────────────────────────────────
# Each formula takes a dict of labeled values and returns a CandidateResult.
# The lab calls evaluate() on the result to get the final hex digest for comparison.
# 
# - PREIMAGE: return CandidateResult(ResultKind.PREIMAGE, constructed_string_or_bytes)
#   The lab will compute MD5(utf8(preimage)) for comparison.
# - DIGEST: return CandidateResult(ResultKind.DIGEST, hex_digest_or_bytes)
#   The lab will use the digest directly (for HMAC, double-MD5, etc.)


def formula_device_email_param_ts_at_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["device_key"] + p["email"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_device_param_ts_at_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["device_key"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_param_ts_at_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_endpoint_param_ts_at_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["endpoint"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_device_endpoint_param_ts_at_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["device_key"] + p["endpoint"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_query_string_salt_suffix(p):
    """Query string as it appears in the URL (without checksum param)."""
    qs = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return CandidateResult(ResultKind.PREIMAGE, qs + p["salt"] + p["suffix"])


def formula_query_string_suffix(p):
    qs = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return CandidateResult(ResultKind.PREIMAGE, qs + p["suffix"])


def formula_post_body_salt_suffix(p):
    """POST body params (without checksum) + salt + suffix."""
    body = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return CandidateResult(ResultKind.PREIMAGE, body + p["salt"] + p["suffix"])


def formula_url_encoded_body_salt_suffix(p):
    """POST body with URL-encoded timestamp."""
    body = f"{p['param_name']}={p['param_value']}&clientDateTime={quote(p['clientDateTime'])}&accessToken={p['accessToken']}"
    return CandidateResult(ResultKind.PREIMAGE, body + p["salt"] + p["suffix"])


def formula_device_query_salt_suffix(p):
    qs = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return CandidateResult(ResultKind.PREIMAGE, p["device_key"] + qs + p["salt"] + p["suffix"])


def formula_sorted_query_salt_suffix(p):
    """Query params sorted alphabetically by key."""
    qs = f"accessToken={p['accessToken']}&clientDateTime={p['clientDateTime']}&{p['param_name']}={p['param_value']}"
    return CandidateResult(ResultKind.PREIMAGE, qs + p["salt"] + p["suffix"])


def formula_values_only_salt_suffix(p):
    """Just the values, no field names."""
    return CandidateResult(ResultKind.PREIMAGE, p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_device_values_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["device_key"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_email_param_ts_at_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["email"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"])


def formula_salt_ts_at_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["salt"] + p["clientDateTime"] + p["accessToken"] + p["suffix"])


def formula_ts_param_at_salt_suffix(p):
    return CandidateResult(ResultKind.PREIMAGE, p["clientDateTime"] + p["param_value"] + p["accessToken"] + p["salt"] + p["suffix"])


# ─── Advanced formulas using derived keys and value transformations ───


def formula_build_key_then_finalise(p):
    """Two-step: key = md5(salt + at + suffix), checksum = md5(pv + cdt + key + suffix)."""
    key = hashlib.md5((p['salt'] + p['accessToken'] + p['suffix']).encode('utf-8')).hexdigest()
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + p['clientDateTime'] + key + p['suffix'])


def formula_build_key_then_finalise_v2(p):
    """key = md5(at + salt + suffix)."""
    key = hashlib.md5((p['accessToken'] + p['salt'] + p['suffix']).encode('utf-8')).hexdigest()
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + p['clientDateTime'] + key + p['suffix'])


def formula_build_key_then_finalise_v3(p):
    """key = md5(salt + at), checksum = md5(pv + cdt + key + suffix)."""
    key = hashlib.md5((p['salt'] + p['accessToken']).encode('utf-8')).hexdigest()
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + p['clientDateTime'] + key + p['suffix'])


def formula_savy_checksum_as_salt(p):
    """Use SavyChecksum (md5(deviceKey + savysoda)) as the salt."""
    savy = derive_savy_checksum(p['device_key'], p['suffix'])
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + p['clientDateTime'] + p['accessToken'] + savy + p['suffix'])


def formula_checksum_key_as_salt(p):
    """Use ChecksumKey (md5(deviceKey + salt)) as the salt."""
    ck = derive_checksum_key(p['device_key'], p['salt'])
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + p['clientDateTime'] + p['accessToken'] + ck + p['suffix'])


def formula_format_string_collect_empty_csum(p):
    """CollectMarker2 format string with empty checksum."""
    fmt = f"starSystemMarkerId={p['param_value']}&checksum=&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return CandidateResult(ResultKind.PREIMAGE, fmt + p['salt'] + p['suffix'])


def formula_format_string_rebuild_empty_csum(p):
    """RebuildAmmo3 format string with empty checksum."""
    fmt = f"ammoCategory={p['param_value']}&clientDateTime={p['clientDateTime']}&checksum=&accessToken={p['accessToken']}"
    return CandidateResult(ResultKind.PREIMAGE, fmt + p['salt'] + p['suffix'])


def formula_ticks_instead_of_datetime(p):
    """Use .NET ticks instead of ISO datetime string."""
    ticks = str(to_ticks(p['clientDateTime']))
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + ticks + p['accessToken'] + p['salt'] + p['suffix'])


def formula_unix_ts_instead_of_datetime(p):
    """Use Unix timestamp instead of ISO datetime string."""
    ts = str(to_unix_ts(p['clientDateTime']))
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + ts + p['accessToken'] + p['salt'] + p['suffix'])


def formula_time_checksum_instead_of_datetime(p):
    """Use ChecksumTimeForDate result instead of datetime."""
    tc = str(checksum_time_for_date(p['clientDateTime']))
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + tc + p['accessToken'] + p['salt'] + p['suffix'])


def formula_hmac_md5_at_key(p):
    """HMAC-MD5 with accessToken as key, params as message."""
    import hmac
    msg = f"{p['param_value']}{p['clientDateTime']}{p['salt']}{p['suffix']}".encode('utf-8')
    return CandidateResult(ResultKind.DIGEST, hmac.new(p['accessToken'].encode('utf-8'), msg, hashlib.md5).hexdigest())


def formula_hmac_md5_salt_key(p):
    """HMAC-MD5 with salt as key."""
    import hmac
    msg = f"{p['param_value']}{p['clientDateTime']}{p['accessToken']}{p['suffix']}".encode('utf-8')
    return CandidateResult(ResultKind.DIGEST, hmac.new(p['salt'].encode('utf-8'), msg, hashlib.md5).hexdigest())


def formula_hmac_md5_device_key(p):
    """HMAC-MD5 with deviceKey as key."""
    import hmac
    msg = f"{p['param_value']}{p['clientDateTime']}{p['accessToken']}{p['suffix']}".encode('utf-8')
    return CandidateResult(ResultKind.DIGEST, hmac.new(p['device_key'].encode('utf-8'), msg, hashlib.md5).hexdigest())


def formula_double_md5(p):
    """md5(md5(params + salt + suffix))."""
    inner = hashlib.md5((p['param_value'] + p['clientDateTime'] + p['accessToken'] + p['salt'] + p['suffix']).encode('utf-8')).hexdigest()
    return CandidateResult(ResultKind.DIGEST, inner)


def formula_lowercase_all(p):
    """All inputs lowercased."""
    return CandidateResult(ResultKind.PREIMAGE, (p['param_value'] + p['clientDateTime'] + p['accessToken'] + p['salt'] + p['suffix']).lower())


def formula_url_encoded_timestamp(p):
    """Timestamp URL-encoded."""
    ts_enc = quote(p['clientDateTime'], safe='')
    return CandidateResult(ResultKind.PREIMAGE, p['param_value'] + ts_enc + p['accessToken'] + p['salt'] + p['suffix'])


def formula_endpoint_with_underscore(p):
    """Endpoint name with underscore separator."""
    return CandidateResult(ResultKind.PREIMAGE, p['endpoint'] + '_' + p['param_value'] + p['clientDateTime'] + p['accessToken'] + p['salt'] + p['suffix'])


def formula_param_name_value_pairs(p):
    """paramName=value&clientDateTime=...&accessToken=..."""
    return CandidateResult(ResultKind.PREIMAGE, f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}" + p['salt'] + p['suffix'])


def formula_rebuild_exact_url_format(p):
    """RebuildAmmo3 exact URL format with all params including checksum placeholder."""
    return CandidateResult(ResultKind.PREIMAGE, f"ammoCategory={p['param_value']}&clientDateTime={p['clientDateTime']}&checksum=&accessToken={p['accessToken']}" + p['salt'] + p['suffix'])


def formula_collect_exact_url_format(p):
    """CollectMarker2 exact URL format."""
    return CandidateResult(ResultKind.PREIMAGE, f"starSystemMarkerId={p['param_value']}&checksum=&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}" + p['salt'] + p['suffix'])


# Registry of all candidate formulas
FORMULAS = {
    "device_email_param_ts_at_salt_suffix": formula_device_email_param_ts_at_salt_suffix,
    "device_param_ts_at_salt_suffix": formula_device_param_ts_at_salt_suffix,
    "param_ts_at_salt_suffix": formula_param_ts_at_salt_suffix,
    "endpoint_param_ts_at_salt_suffix": formula_endpoint_param_ts_at_salt_suffix,
    "device_endpoint_param_ts_at_salt_suffix": formula_device_endpoint_param_ts_at_salt_suffix,
    "query_string_salt_suffix": formula_query_string_salt_suffix,
    "query_string_suffix": formula_query_string_suffix,
    "post_body_salt_suffix": formula_post_body_salt_suffix,
    "url_encoded_body_salt_suffix": formula_url_encoded_body_salt_suffix,
    "device_query_salt_suffix": formula_device_query_salt_suffix,
    "sorted_query_salt_suffix": formula_sorted_query_salt_suffix,
    "values_only_salt_suffix": formula_values_only_salt_suffix,
    "device_values_salt_suffix": formula_device_values_salt_suffix,
    "email_param_ts_at_salt_suffix": formula_email_param_ts_at_salt_suffix,
    "salt_ts_at_suffix": formula_salt_ts_at_suffix,
    "ts_param_at_salt_suffix": formula_ts_param_at_salt_suffix,

    # Advanced formulas
    "build_key_then_finalise": formula_build_key_then_finalise,
    "build_key_then_finalise_v2": formula_build_key_then_finalise_v2,
    "build_key_then_finalise_v3": formula_build_key_then_finalise_v3,
    "savy_checksum_as_salt": formula_savy_checksum_as_salt,
    "checksum_key_as_salt": formula_checksum_key_as_salt,
    "format_string_collect_empty_csum": formula_format_string_collect_empty_csum,
    "format_string_rebuild_empty_csum": formula_format_string_rebuild_empty_csum,
    "ticks_instead_of_datetime": formula_ticks_instead_of_datetime,
    "unix_ts_instead_of_datetime": formula_unix_ts_instead_of_datetime,
    "time_checksum_instead_of_datetime": formula_time_checksum_instead_of_datetime,
    "hmac_md5_at_key": formula_hmac_md5_at_key,
    "hmac_md5_salt_key": formula_hmac_md5_salt_key,
    "hmac_md5_device_key": formula_hmac_md5_device_key,
    "double_md5": formula_double_md5,
    "lowercase_all": formula_lowercase_all,
    "url_encoded_timestamp": formula_url_encoded_timestamp,
    "endpoint_with_underscore": formula_endpoint_with_underscore,
    "param_name_value_pairs": formula_param_name_value_pairs,
    "rebuild_exact_url_format": formula_rebuild_exact_url_format,
    "collect_exact_url_format": formula_collect_exact_url_format,
}


def test_formula(name, formula_fn, samples, session):
    """Test a candidate formula against all samples. Returns (matches, total)."""
    matches = 0
    results = []
    for sample in samples:
        params = get_sample_params(sample, session)
        candidate = formula_fn(params)
        computed = evaluate(candidate)
        expected = sample["expected_checksum"]
        is_match = computed == expected
        if is_match:
            matches += 1
        results.append((sample["id"], "MATCH" if is_match else "NO MATCH"))

    return matches, len(samples), results


def main():
    fixture = load_fixture()
    session = resolve_session_fields(fixture)
    samples = fixture["samples"]

    print(f"Loaded {len(samples)} checksum samples")
    print(f"Testing {len(FORMULAS)} candidate formulas\n")
    print("A valid formula must match ALL samples.\n")

    best_matches = 0
    best_formula = None

    for name, fn in FORMULAS.items():
        matches, total, results = test_formula(name, fn, samples, session)
        status = "ALL MATCH" if matches == total else f"{matches}/{total} matched"
        print(f"{name}: {status}")
        for sample_id, result in results:
            if matches > 0 or matches == total:
                print(f"  {sample_id}: {result}")

        if matches > best_matches:
            best_matches = matches
            best_formula = name

        if matches == total:
            print(f"\n*** FOUND VALID FORMULA: {name} ***")
            print("All samples reproduced. Ready for live validation.")
            return 0

    print(f"\nNo formula matched all samples.")
    print(f"Best: {best_formula} with {best_matches}/{len(samples)} matches")
    print("\nThe checksum may use value transformations (lowercasing, URL encoding,")
    print("Unix timestamps, ticks, field-name prefixes, nested hashing, or binary bytes)")
    print("that permutation search alone cannot discover.")
    print("\nNext step: locate the checksum implementation in the iOS app bundle.")
    return 1


if __name__ == "__main__":
    sys.exit(main())