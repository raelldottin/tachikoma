#!/usr/bin/env python3
"""
Provision GitHub secrets for 5 accounts using email/password authentication.
Uses DeviceLogin17 + UserEmailPasswordAuthorize4 flow without refresh token persistence.
Never prints credential values.
"""
from __future__ import annotations

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from sdk.client import Client
    from sdk.device import Device
    from sdk.redaction import redact_secrets as base_redact_secrets
except ImportError as e:
    print(f"Dependency error: {e}", file=sys.stderr)
    sys.exit(1)


def redact_secrets(text: str, dynamic_secrets: set[str] | list[str] | None = None) -> str:
    """Redact sensitive information using pattern matching and dynamic secret values."""
    if not text:
        return text

    result = base_redact_secrets(text)

    all_dynamic = set()
    if dynamic_secrets:
        all_dynamic.update(s for s in dynamic_secrets if s and isinstance(s, str))

    sorted_secrets = sorted((s for s in all_dynamic if s), key=len, reverse=True)
    for secret in sorted_secrets:
        if secret in result:
            result = result.replace(secret, "***REDACTED***")

    return result


def inspect_account_slots():
    """Inspect environment for 5 account slots.

    Returns:
        slots: dict mapping slot_num (1..5) to dict with slot info:
            status: 'UNCONFIGURED' | 'CONFIGURED' | 'PARTIAL_CONFIG'
            email, password
            missing_fields: list of missing field names if PARTIAL_CONFIG
    """
    slots = {}
    for i in range(1, 6):
        email = os.environ.get(f"PSS_ACCOUNT_{i}_EMAIL")
        password = os.environ.get(f"PSS_ACCOUNT_{i}_PASSWORD")

        email_val = email.strip() if email and isinstance(email, str) else None
        password_val = password.strip() if password and isinstance(password, str) else None

        has_email = bool(email_val)
        has_password = bool(password_val)

        if not (has_email or has_password):
            status = 'UNCONFIGURED'
            missing = []
        elif has_email and has_password:
            status = 'CONFIGURED'
            missing = []
        else:
            status = 'PARTIAL_CONFIG'
            missing = []
            if not has_email:
                missing.append('email')
            if not has_password:
                missing.append('password')

        slots[i] = {
            'status': status,
            'email': email_val,
            'password': password_val,
            'missing_fields': missing,
        }
    return slots


def provision_account(account_name: str, email: str, password: str) -> bool:
    """Authenticate using email/password without refresh token persistence.

    Flow:
    1. DeviceLogin17 with empty refresh token → get accessToken
    2. UserEmailPasswordAuthorize4 with email/password → authenticated session

    Returns:
        True if authentication successful.
    """
    device = Device(language="en")
    device.key = "CC3C7642-E6FE-4737-88C1-130395760B52"  # iOS device key
    device.refreshToken = ""  # Empty refresh token each run

    client = Client(
        device=device,
        settings={
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@",
        },
    )

    try:
        # Stage 1: DeviceLogin17 with empty refresh token → get accessToken
        if not client.create_device_session():
            raise RuntimeError(f"{account_name}: DeviceLogin17 failed")

        if not client.accessToken:
            raise RuntimeError(f"{account_name}: No accessToken from DeviceLogin17")

        # Stage 2: UserEmailPasswordAuthorize4 with email/password
        if not client.authorize_email_password(email, password):
            raise RuntimeError(f"{account_name}: Email/password authorize failed")

        # Verify we have an authenticated session
        if not client.user.isAuthorized:
            raise RuntimeError(f"{account_name}: Session not authorized after email/password")

        logging.info(f"{account_name}: Authentication successful")
        return True
    except (RuntimeError, ValueError, KeyError, AttributeError, OSError) as e:
        extra = {email, password, device.key}
        if client.accessToken:
            extra.add(client.accessToken)
        sanitized_msg = redact_secrets(str(e), dynamic_secrets=extra)
        if str(e) != sanitized_msg:
            raise RuntimeError(sanitized_msg) from None
        raise
    except Exception as e:
        extra = {email, password, device.key}
        if client.accessToken:
            extra.add(client.accessToken)
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
            print(err_msg, file=sys.stderr)
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
                provision_account(f"account_{i}", s['email'], s['password'])
                results[i] = 'SUCCESS'
            except (RuntimeError, ValueError, KeyError, AttributeError, OSError) as e:
                extra_sec = {s['email'], s['password']}
                sanitized_err = redact_secrets(str(e), dynamic_secrets=extra_sec)
                print(f"Account {i}: FAILED - {sanitized_err}", file=sys.stderr)
                results[i] = 'FAILED'
            except Exception as e:
                extra_sec = {s['email'], s['password']}
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