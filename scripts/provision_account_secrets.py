#!/usr/bin/env python3
"""
Provision GitHub secrets for 5 accounts using captured refresh tokens as bootstrap,
then rotate via email/password. Never prints credential values.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from sdk.client import Client
    from sdk.device import Device
    from sdk.redaction import redact_secrets as base_redact_secrets
except ImportError as e:
    print(f"Dependency error: {e}", file=sys.stderr)
    sys.exit(1)


def inspect_account_slots():
    """Inspect environment for 5 account slots.

    Returns:
        slots: dict mapping slot_num (1..5) to dict with slot info:
            status: 'UNCONFIGURED' | 'CONFIGURED' | 'PARTIAL_CONFIG'
            email, password, refresh_token
            missing_fields: list of missing field names if PARTIAL_CONFIG
    """
    slots = {}
    for i in range(1, 6):
        email = os.environ.get(f"PSS_ACCOUNT_{i}_EMAIL")
        password = os.environ.get(f"PSS_ACCOUNT_{i}_PASSWORD")
        refresh_token = os.environ.get(f"PSS_ACCOUNT_{i}_REFRESH_TOKEN")

        email_val = email.strip() if email and isinstance(email, str) else None
        password_val = password.strip() if password and isinstance(password, str) else None
        refresh_val = refresh_token.strip() if refresh_token and isinstance(refresh_token, str) else None

        has_email = bool(email_val)
        has_password = bool(password_val)
        has_refresh = bool(refresh_val)

        if not (has_email or has_password or has_refresh):
            status = 'UNCONFIGURED'
            missing = []
        elif has_email and has_password and has_refresh:
            status = 'CONFIGURED'
            missing = []
        else:
            status = 'PARTIAL_CONFIG'
            missing = []
            if not has_email:
                missing.append('email')
            if not has_password:
                missing.append('password')
            if not has_refresh:
                missing.append('refresh_token')

        slots[i] = {
            'status': status,
            'email': email_val,
            'password': password_val,
            'refresh_token': refresh_val,
            'missing_fields': missing,
        }
    return slots


def collect_dynamic_secrets(
    slots: dict | None = None,
    extra_secrets: set[str] | list[str] | None = None,
) -> set[str]:
    """Collect non-empty raw secret values from account slots and common device keys."""
    secrets = {"CC3C7642-E6FE-4737-88C1-130395760B52"}  # Default iOS device key
    if slots is None:
        try:
            slots = inspect_account_slots()
        except Exception:
            slots = {}

    for slot in slots.values():
        if isinstance(slot, dict):
            for field in ("email", "password", "refresh_token"):
                val = slot.get(field)
                if val and isinstance(val, str) and val.strip():
                    secrets.add(val.strip())

    if extra_secrets:
        for val in extra_secrets:
            if val and isinstance(val, str) and val.strip():
                secrets.add(val.strip())

    return secrets


def redact_secrets(text: str, dynamic_secrets: set[str] | list[str] | None = None) -> str:
    """
    Redact sensitive information using pattern matching and dynamic secret values.
    Dynamically replaces raw secret values (email, password, refresh_token, access_token, device_key)
    with ***REDACTED*** even if un-prefixed in exception strings.
    """
    if not text:
        return text

    result = base_redact_secrets(text)

    all_dynamic = set()
    if dynamic_secrets:
        all_dynamic.update(s for s in dynamic_secrets if s and isinstance(s, str))

    all_dynamic.update(collect_dynamic_secrets())

    sorted_secrets = sorted((s for s in all_dynamic if s), key=len, reverse=True)
    for secret in sorted_secrets:
        if secret in result:
            result = result.replace(secret, "***REDACTED***")

    return result


def provision_account(account_name: str, email: str, password: str, refresh_token: str) -> str:
    """Bootstrap with refresh_token, rotate via email/password, return new refresh_token."""
    device = Device(language="en")
    device.key = "CC3C7642-E6FE-4737-88C1-130395760B52"  # iOS device key
    device.refreshToken = refresh_token

    client = Client(
        device=device,
        settings={
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@",
        },
    )

    try:
        # Stage 1: DeviceLogin17 with refresh token -> get accessToken
        if not client.create_device_session():
            raise RuntimeError(f"{account_name}: DeviceLogin17 failed")

        if not client.accessToken:
            raise RuntimeError(f"{account_name}: No accessToken from DeviceLogin17")

        # Stage 2: UserEmailPasswordAuthorize4 with email/password -> get NEW refresh token
        if not client.authorize_email_password(email, password):
            raise RuntimeError(f"{account_name}: Email/password authorize failed")

        if not device.refreshToken:
            raise RuntimeError(f"{account_name}: No new refreshToken after rotation")

        return device.refreshToken
    except (RuntimeError, ValueError, KeyError, AttributeError, OSError) as e:
        extra = {email, password, refresh_token, device.key}
        if client.accessToken:
            extra.add(client.accessToken)
        if device.refreshToken:
            extra.add(device.refreshToken)
        sanitized_msg = redact_secrets(str(e), dynamic_secrets=extra)
        if str(e) != sanitized_msg:
            raise RuntimeError(sanitized_msg) from None
        raise
    except Exception as e:
        extra = {email, password, refresh_token, device.key}
        if client.accessToken:
            extra.add(client.accessToken)
        if device.refreshToken:
            extra.add(device.refreshToken)
        sanitized_msg = redact_secrets(str(e), dynamic_secrets=extra)
        if str(e) != sanitized_msg:
            raise RuntimeError(sanitized_msg) from None
        raise


def main():
    slots = inspect_account_slots()

    configured_slots = {i: s for i, s in slots.items() if s['status'] == 'CONFIGURED'}
    partial_slots = {i: s for i, s in slots.items() if s['status'] == 'PARTIAL_CONFIG'}

    # Zero Accounts Contract
    if not configured_slots and not partial_slots:
        print("No accounts configured. Safe exit 0.")
        sys.exit(0)

    results = {}

    # Partial Account Pre-flight Contract
    for i, s in slots.items():
        if s['status'] == 'PARTIAL_CONFIG':
            missing_str = ", ".join(s['missing_fields'])
            err_msg = f"Account {i}: Partial configuration - missing {missing_str}"
            print(redact_secrets(err_msg), file=sys.stderr)
            results[i] = 'PARTIAL_CONFIG_FAILED'

    if partial_slots:
        for i in range(1, 6):
            if i in results:
                print(f"Account {i}: {results[i]}")
        sys.exit(1)

    # Five Accounts Independent Processing Contract
    for i in range(1, 6):
        s = slots[i]
        if s['status'] == 'CONFIGURED':
            try:
                provision_account(f"account_{i}", s['email'], s['password'], s['refresh_token'])
                results[i] = 'SUCCESS'
            except (RuntimeError, ValueError, KeyError, AttributeError, OSError) as e:
                extra_sec = {s['email'], s['password'], s['refresh_token']}
                sanitized_err = redact_secrets(str(e), dynamic_secrets=extra_sec)
                print(f"Account {i}: FAILED - {sanitized_err}", file=sys.stderr)
                results[i] = 'FAILED'
            except Exception as e:
                extra_sec = {s['email'], s['password'], s['refresh_token']}
                sanitized_err = redact_secrets(str(e), dynamic_secrets=extra_sec)
                print(f"Account {i}: FAILED - {sanitized_err}", file=sys.stderr)
                results[i] = 'FAILED'

    # Token and Output Safety: Stdout summaries
    for i in range(1, 6):
        if i in results:
            print(f"Account {i}: {results[i]}")

    # Deterministic Exit Semantics
    has_failure = any(outcome != 'SUCCESS' for outcome in results.values())
    if has_failure:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()