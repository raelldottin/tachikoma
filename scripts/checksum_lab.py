#!/usr/bin/env python3
"""Checksum research harness for CollectMarker2 and RebuildAmmo3.

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
from pathlib import Path
from urllib.parse import quote


FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "checksum_samples.json"


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


# ─── Candidate formulas ──────────────────────────────────────────────
# Each formula takes a dict of labeled values and returns a string to hash.
# The lab computes MD5(result) and compares to the expected checksum.

def formula_device_email_param_ts_at_salt_suffix(p):
    return p["device_key"] + p["email"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_device_param_ts_at_salt_suffix(p):
    return p["device_key"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_param_ts_at_salt_suffix(p):
    return p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_endpoint_param_ts_at_salt_suffix(p):
    return p["endpoint"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_device_endpoint_param_ts_at_salt_suffix(p):
    return p["device_key"] + p["endpoint"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_query_string_salt_suffix(p):
    """Query string as it appears in the URL (without checksum param)."""
    qs = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return qs + p["salt"] + p["suffix"]

def formula_query_string_suffix(p):
    qs = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return qs + p["suffix"]

def formula_post_body_salt_suffix(p):
    """POST body params (without checksum) + salt + suffix."""
    body = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return body + p["salt"] + p["suffix"]

def formula_url_encoded_body_salt_suffix(p):
    """POST body with URL-encoded timestamp."""
    body = f"{p['param_name']}={p['param_value']}&clientDateTime={quote(p['clientDateTime'])}&accessToken={p['accessToken']}"
    return body + p["salt"] + p["suffix"]

def formula_device_query_salt_suffix(p):
    qs = f"{p['param_name']}={p['param_value']}&clientDateTime={p['clientDateTime']}&accessToken={p['accessToken']}"
    return p["device_key"] + qs + p["salt"] + p["suffix"]

def formula_sorted_query_salt_suffix(p):
    """Query params sorted alphabetically by key."""
    qs = f"accessToken={p['accessToken']}&clientDateTime={p['clientDateTime']}&{p['param_name']}={p['param_value']}"
    return qs + p["salt"] + p["suffix"]

def formula_values_only_salt_suffix(p):
    """Just the values, no field names."""
    return p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_device_values_salt_suffix(p):
    return p["device_key"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_email_param_ts_at_salt_suffix(p):
    return p["email"] + p["param_value"] + p["clientDateTime"] + p["accessToken"] + p["salt"] + p["suffix"]

def formula_salt_ts_at_suffix(p):
    return p["salt"] + p["clientDateTime"] + p["accessToken"] + p["suffix"]

def formula_ts_param_at_salt_suffix(p):
    return p["clientDateTime"] + p["param_value"] + p["accessToken"] + p["salt"] + p["suffix"]

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
}


def test_formula(name, formula_fn, samples, session):
    """Test a candidate formula against all samples. Returns (matches, total)."""
    matches = 0
    results = []
    for sample in samples:
        params = get_sample_params(sample, session)
        preimage = formula_fn(params)
        computed = hashlib.md5(preimage.encode("utf-8")).hexdigest()
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
        status = "✓ ALL MATCH" if matches == total else f"{matches}/{total} matched"
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
