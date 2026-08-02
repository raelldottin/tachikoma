#!/usr/bin/env python3
"""Test: use the captured access token (from the same session as the refresh token)."""
import sys, os, re, json, urllib.parse, hashlib
sys.path.insert(0, ".")

from sdk.device import Device
from sdk.client import Client
from sdk.security import checksum_user_email_password_authorize4
import requests

# Load capture
with open("/tmp/pss_capture.json") as f:
    captures = json.load(f)

resp_text = captures[7][1]["text"]
captured_refresh_token = re.findall(r'refreshToken="([^"]+)"', resp_text)[0]
captured_access_token = captures[6][1]["query"]["accessToken"]

print(f"Captured access token: {captured_access_token}")
print(f"Captured refresh token: {captured_refresh_token[:20]}...")

# Use the captured access token directly
device = Device(language="en")
device.key = "6AD42828-7D06-534D-A461-49658461A614"  # Same device key
client = Client(device=device, settings={"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"})
client.accessToken = captured_access_token

# Compute checksum with captured timestamp
ts = "2026-08-02T06:04:20"  # Use captured timestamp
checksum = checksum_user_email_password_authorize4(
    device.key,
    "ack@syncpool.com",
    ts,
    client.accessToken,
    "5343",
    "Savvy!s0d@",
)
print(f"\nUsing captured timestamp: {ts}")
print(f"Checksum: {checksum}")
print(f"Expected: cb51b89ea3d4b39125b388d9af210a57")

# Send request
post_data = urllib.parse.urlencode({
    "clientDateTime": ts,
    "checksum": checksum,
    "deviceKey": device.key,
    "email": "ack@syncpool.com",
    "password": "cymfe0-mifkUn-mymhix",
    "languageKey": "en",
    "isWeb": "False",
    "accessToken": client.accessToken,
})

url = f"{client.baseUrl}/UserService/UserEmailPasswordAuthorize4"
r = requests.post(url, data=post_data)
print(f"\nStatus: {r.status_code}")
print(f"Response: {r.text[:500]}")

# Now try with CURRENT timestamp but same access token
print("\n=== Trying with CURRENT timestamp but captured access token ===")
from datetime import datetime
ts2 = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
checksum2 = checksum_user_email_password_authorize4(
    device.key,
    "ack@syncpool.com",
    ts2,
    client.accessToken,
    "5343",
    "Savvy!s0d@",
)
print(f"Timestamp: {ts2}")
print(f"Checksum: {checksum2}")

post_data2 = urllib.parse.urlencode({
    "clientDateTime": ts2,
    "checksum": checksum2,
    "deviceKey": device.key,
    "email": "ack@syncpool.com",
    "password": "cymfe0-mifkUn-mymhix",
    "languageKey": "en",
    "isWeb": "False",
    "accessToken": client.accessToken,
})
r2 = requests.post(url, data=post_data2)
print(f"Status: {r2.status_code}")
print(f"Response: {r2.text[:500]}")