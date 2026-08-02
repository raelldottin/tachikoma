#!/usr/bin/env python3
"""Debug: compare our UserEmailPasswordAuthorize4 request against the capture."""
import sys, os, re, json, hashlib, urllib.parse
sys.path.insert(0, ".")

from sdk.device import Device
from sdk.client import Client
from sdk.security import checksum_user_email_password_authorize4
import requests

# Load capture
with open("/tmp/pss_capture.json") as f:
    captures = json.load(f)

# Captured request
captured_req = captures[6][1]
captured_url = captured_req["url"]
captured_query = captured_req.get("query", {})

print("=== CAPTURED REQUEST ===")
print(f"URL params: {list(captured_query.keys())}")
print(f"  clientDateTime: {captured_query['clientDateTime']}")
print(f"  checksum: {captured_query['checksum']}")
print(f"  deviceKey: {captured_query['deviceKey']}")
print(f"  email: {captured_query['email']}")
print(f"  languageKey: {captured_query['languageKey']}")
print(f"  isWeb: {captured_query['isWeb']}")
print(f"  accessToken: {captured_query['accessToken']}")
print(f"  password: [REDACTED]")

# Now compute our checksum with the SAME captured values
our_checksum = checksum_user_email_password_authorize4(
    captured_query["deviceKey"],
    captured_query["email"],
    captured_query["clientDateTime"],
    captured_query["accessToken"],
    "5343",
    "Savvy!s0d@",
)
print(f"\n=== CHECKSUM COMPARISON ===")
print(f"Captured: {captured_query['checksum']}")
print(f"Our:      {our_checksum}")
print(f"Match:    {captured_query['checksum'] == our_checksum}")

# Now let's look at what our client sends
device = Device(language="en")
device.refreshToken = re.findall(r'refreshToken="([^"]+)"', captures[7][1]["text"])[0]
client = Client(device=device, settings={"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"})
client.create_device_session()

print(f"\n=== OUR STAGE 1 RESULT ===")
print(f"Access token: {client.accessToken}")
print(f"Device key: {device.key}")

# Now compute checksum with OUR access token and current time
ts = client._client_datetime_utc()
our_cs = checksum_user_email_password_authorize4(
    device.key,
    "ack@syncpool.com",
    ts,
    client.accessToken,
    "5343",
    "Savvy!s0d@",
)
print(f"\n=== OUR COMPUTED CHECKSUM ===")
print(f"  clientDateTime: {ts}")
print(f"  checksum: {our_cs}")
print(f"  deviceKey: {device.key}")
print(f"  email: ack@syncpool.com")
print(f"  accessToken: {client.accessToken}")

# Verify our checksum independently
preimage = device.key + "ack@syncpool.com" + ts + client.accessToken + "5343"
verify = hashlib.md5((preimage + "Savvy!s0d@").encode()).hexdigest()
print(f"\n=== INDEPENDENT VERIFICATION ===")
print(f"  preimage: {preimage[:80]}...")
print(f"  MD5:      {verify}")
print(f"  Match:    {verify == our_cs}")

# Now send the actual request manually
post_data = urllib.parse.urlencode({
    "clientDateTime": ts,
    "checksum": our_cs,
    "deviceKey": device.key,
    "email": "ack@syncpool.com",
    "password": "cymfe0-mifkUn-mymhix",
    "languageKey": "en",
    "isWeb": "False",
    "accessToken": client.accessToken,
})

url = f"{client.baseUrl}/UserService/UserEmailPasswordAuthorize4"
print(f"\n=== MANUAL REQUEST ===")
print(f"POST {url}")
r = requests.post(url, data=post_data)
print(f"Status: {r.status_code}")
print(f"Response (first 500): {r.text[:500]}")