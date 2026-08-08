#!/usr/bin/env python3
"""Trigger a DeviceLogin17 request to capture the preimage."""
import sys
sys.path.insert(0, ".")

from sdk.device import Device
from sdk.client import Client
import requests

device = Device(language="en")
device.refreshToken = None

client = Client(device=device, settings={"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"})
payload = client._build_device_login_payload()
url = f"{client.baseUrl}/UserService/DeviceLogin17"
r = requests.post(url, json=payload)
print(f"Status: {r.status_code}")
print(f"Has accessToken: {'accessToken' in r.text}")
print(f"Has errorCode: {'errorCode' in r.text}")