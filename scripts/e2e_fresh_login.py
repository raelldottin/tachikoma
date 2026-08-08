#!/usr/bin/env python3
"""Test full e2e flow: fresh device → email/password → refresh exchange."""
import sys
sys.path.insert(0, ".")

from sdk.device import Device
from sdk.client import Client

# Use a fresh device (no refresh token)
device = Device(language="en")
client = Client(device=device, settings={"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"})

# ── Stage 1: DeviceLogin17 without refresh token ──
print("=== Stage 1: DeviceLogin17 (fresh device) ===")
ok = client.create_device_session()
print(f"Result: {ok}")
print(f"Access token: {client.accessToken}")

if not client.accessToken:
    print("FAILED at Stage 1")
    sys.exit(1)

# ── Stage 2: UserEmailPasswordAuthorize4 ──
print("\n=== Stage 2: UserEmailPasswordAuthorize4 ===")
# The password from the capture - using it for the test
ok = client.authorize_email_password("ack@syncpool.com", "cymfe0-mifkUn-mymhix")
print(f"Result: {ok}")
if client.device.refreshToken:
    print(f"Refresh token: {client.device.refreshToken[:20]}...")
else:
    print("Refresh token: NONE")

if not ok:
    print("FAILED at Stage 2")
    sys.exit(1)

# ── Stage 3: DeviceLogin17 with refresh token ──
print("\n=== Stage 3: DeviceLogin17 (exchange refresh token) ===")
ok = client.exchange_refresh_token()
print(f"Result: {ok}")
print(f"Access token: {client.accessToken}")

if client.accessToken:
    print("\n✅ END-TO-END EMAIL/PASSWORD LOGIN SUCCESSFUL")
else:
    print("\n❌ Stage 3 failed")
    sys.exit(1)