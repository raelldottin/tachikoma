#!/usr/bin/env python3
"""End-to-end email/password login validation.

Stage 1: DeviceLogin17 → get accessToken
Stage 2: UserEmailPasswordAuthorize4 → get refreshToken
Stage 3: DeviceLogin17 with refreshToken → full session
"""
import sys
import re
import json
sys.path.insert(0, ".")

from sdk.device import Device
from sdk.client import Client
from sdk.redaction import redact_secrets
from scripts.script_credentials import load_email_password

# Load captured refresh token from private fixture (in-memory only, never print)
with open("/tmp/pss_capture.json") as f:
    captures = json.load(f)

resp_text = captures[7][1].get("text", "")
captured_refresh_token = re.findall(r'refreshToken="([^"]+)"', resp_text)[0]

device = Device(language="en")
client = Client(device=device, settings={
    "checksum_key": "5343",
    "savy_checksum": "Savvy!s0d@",
})

# ── Stage 1: DeviceLogin17 with existing refresh token ════════════
device.refreshToken = captured_refresh_token
print("=== Stage 1: DeviceLogin17 ===")
ok = client.create_device_session()
print(f"Result: {ok}")
print(f"Access token: {redact_secrets(client.accessToken) if client.accessToken else 'NONE'}")

if not client.accessToken:
    print("FAILED at Stage 1 — no access token")
    sys.exit(1)

# ── Stage 2: UserEmailPasswordAuthorize4 ═══════════════════════════
# Test account credentials come from PSS_TEST_EMAIL / PSS_TEST_PASSWORD(_FILE)
captured_email, captured_password = load_email_password()

print()
print("=== Stage 2: UserEmailPasswordAuthorize4 ===")
ok = client.authorize_email_password(captured_email, captured_password)
print(f"Result: {ok}")
if client.device.refreshToken:
    print(f"Refresh token: {redact_secrets(client.device.refreshToken)[:20]}...")
else:
    print("Refresh token: NONE")

if not ok:
    print("FAILED at Stage 2 — email/password authorize failed")
    sys.exit(1)

# ── Stage 3: DeviceLogin17 with new refresh token ══════════════════
print()
print("=== Stage 3: DeviceLogin17 (exchange refresh token) ===")
ok = client.exchange_refresh_token()
print(f"Result: {ok}")
print(f"Access token: {redact_secrets(client.accessToken) if client.accessToken else 'NONE'}")

if client.accessToken:
    print()
    print("✅ END-TO-END EMAIL/PASSWORD LOGIN SUCCESSFUL")
else:
    print()
    print("❌ Stage 3 failed — no access token after refresh exchange")
    sys.exit(1)