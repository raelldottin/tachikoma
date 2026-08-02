#!/usr/bin/env python3
"""End-to-end DeviceLogin17 validation.

Sends a real DeviceLogin17 request to the PSS server using the verified
checksum formula. This is the first live-network validation that the
checksum implementation produces server-accepted responses.

Usage:
    python3 scripts/e2e_device_login17.py

The device key from .device is reused (it's already registered server-side).
No refresh token is sent — this tests the unauthenticated device session path.

Exit codes:
    0 - Server accepted the request (accessToken returned)
    1 - Server rejected (errorCode or no accessToken)
    2 - Network/error configuration error
"""
import sys
import os

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from sdk.client import Client
from sdk.device import Device
from sdk.redaction import redact_secrets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def main():
    # Load the existing device (has a registered deviceKey)
    device = Device(language="en")
    # Clear the mock refresh token so we test the fresh device session path
    device.refreshToken = None

    logging.info(f"Device key: {redact_secrets(device.key)}")
    logging.info("Sending DeviceLogin17 request with verified checksum...")

    client = Client(
        device=device,
        settings={
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@",
        },
    )

    try:
        success = client.create_device_session()
    except Exception as e:
        logging.error(f"Exception during create_device_session: {redact_secrets(str(e))}")
        return 2

    if success and client.accessToken:
        logging.info("SUCCESS: Server returned an access token.")
        logging.info(f"AccessToken: {redact_secrets(client.accessToken)}")
        if hasattr(client, "info") and "@Name" in client.info:
            logging.info(f"User Name: {client.info['@Name']}")
        return 0
    else:
        logging.error("FAILED: Server did not return an access token.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
