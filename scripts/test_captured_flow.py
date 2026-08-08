#!/usr/bin/env python3
"""Test: use captured access token for Stage 2, then captured refresh token for Stage 3."""
import sys
import re
import json
sys.path.insert(0, ".")

from sdk.device import Device
from sdk.client import Client

# Load captures
with open("/tmp/pss_capture.json") as f:
    captures = json.load(f)

# Capture 6 request access token
captured_access_token = captures[6][1]["query"]["accessToken"]

# Capture 7 response refresh token
resp_text = captures[7][1]["text"]
captured_refresh_token = re.findall(r'refreshToken="([^"]+)"', resp_text)[0]

print(f"Captured access token: {captured_access_token}")
print(f"Captured refresh token: {captured_refresh_token[:20]}...")

device = Device(language="en")
device.key = "6AD42828-7D06-534D-A461-49658461A614"
client = Client(device=device, settings={"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"})

# Use captured access token for Stage 2
client.accessToken = captured_access_token

print("\n=== Stage 2: UserEmailPasswordAuthorize4 (with captured access token) ===")
ok = client.authorize_email_password("ack@syncpool.com", "cymfe0-mifkUn-mymhix")
print(f"Result: {ok}")
if client.device.refreshToken:
    print(f"Refresh token: {client.device.refreshToken[:20]}...")
else:
    print("Refresh token: NONE")

if not ok:
    print("FAILED at Stage 2")
    sys.exit(1)

# Use captured refresh token for Stage 3
print("\n=== Stage 3: DeviceLogin17 (with captured refresh token) ===")
device.refreshToken = captured_refresh_token
client.accessToken = None
ok = client.create_device_session()
print(f"Result: {ok}")
print(f"Access token: {client.accessToken}")

if client.accessToken:
    print("\n✅ END-TO-END EMAIL/PASSWORD LOGIN SUCCESSFUL")
else:
    print("\n❌ Stage 3 failed")
    sys.exit(1)