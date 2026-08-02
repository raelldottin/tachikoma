#!/usr/bin/env python3
"""
Provision GitHub secrets for 5 accounts using captured refresh tokens as bootstrap,
then rotate via email/password. Never prints credential values.
"""
import os
import sys
import subprocess
import json
import re

# These would come from GitHub secrets at runtime
# PSS_ACCOUNT_1_EMAIL, PSS_ACCOUNT_1_PASSWORD, PSS_ACCOUNT_1_REFRESH_TOKEN
# ... etc for accounts 1-5

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.device import Device
from sdk.client import Client
from sdk.redaction import redact_secrets

def provision_account(account_name: str, email: str, password: str, refresh_token: str) -> str:
    """Bootstrap with refresh_token, rotate via email/password, return new refresh_token."""
    device = Device(language="en")
    device.key = "CC3C7642-E6FE-4737-88C1-130395760B52"  # iOS device key
    device.refreshToken = refresh_token
    
    client = Client(device=device, settings={
        "checksum_key": "5343",
        "savy_checksum": "Savvy!s0d@",
    })
    
    # Stage 1: DeviceLogin17 with refresh token → get accessToken
    if not client.create_device_session():
        raise RuntimeError(f"{account_name}: DeviceLogin17 failed")
    
    if not client.accessToken:
        raise RuntimeError(f"{account_name}: No accessToken from DeviceLogin17")
    
    # Stage 2: UserEmailPasswordAuthorize4 with email/password → get NEW refresh token
    if not client.authorize_email_password(email, password):
        raise RuntimeError(f"{account_name}: Email/password authorize failed")
    
    if not device.refreshToken:
        raise RuntimeError(f"{account_name}: No new refreshToken after rotation")
    
    return device.refreshToken

def main():
    # Read from environment (GitHub secrets)
    accounts = []
    for i in range(1, 6):
        email = os.environ.get(f"PSS_ACCOUNT_{i}_EMAIL")
        password = os.environ.get(f"PSS_ACCOUNT_{i}_PASSWORD")
        refresh_token = os.environ.get(f"PSS_ACCOUNT_{i}_REFRESH_TOKEN")
        
        if not all([email, password, refresh_token]):
            print(f"Account {i}: Missing secrets, skipping", file=sys.stderr)
            continue
        
        accounts.append((f"account_{i}", email, password, refresh_token))
    
    if not accounts:
        print("No accounts configured", file=sys.stderr)
        sys.exit(1)
    
    # Provision each account
    new_tokens = {}
    for name, email, password, refresh_token in accounts:
        try:
            new_refresh = provision_account(name, email, password, refresh_token)
            new_tokens[name] = new_refresh
            print(f"{name}: OK", file=sys.stderr)
        except Exception as e:
            print(f"{name}: FAILED - {redact_secrets(str(e))}", file=sys.stderr)
            sys.exit(1)
    
    # Output new tokens as JSON for GitHub Actions to capture
    # This stdout goes to the workflow, not logs
    print(json.dumps(new_tokens))

if __name__ == "__main__":
    main()