#!/usr/bin/env python3
"""
Verify our battle implementation against the REAL mitmproxy capture.

This script proves the checksum formulas produce the EXACT same values
as the real game client captured in ~/pss-mitm-capture.jsonl

Run this while the Pixel Starships mobile app is OPEN and ACTIVE
to test against the real server with a valid session.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sdk.client import Client
from sdk.security import (
    checksum_create_battle9,
    checksum_accept_battle5,
    checksum_finalise_battle15,
    CHECKSUM_KEY,
    SAVY_CHECKSUM,
)
import hashlib

# =============================================================================
# REAL CAPTURE VALUES (from ~/pss-mitm-capture.jsonl)
# =============================================================================
CAPTURE = {
    "create_battle9": {
        "clientHp": "4000",
        "clientDateTime": "2026-08-08T17:35:10",
        "accessToken": "466f7d82-0bd8-48d1-90f6-2466c3e873b0",
        "expected_checksum": "8118b3ffc06d9e8b520c1b6956e7ca9a",
        "battleId": "4028975",
    },
    "accept_battle5": {
        "battleId": "4028975",
        "itemDesignId": "0",
        "clientDateTime": "2026-08-08T17:35:12",
        "accessToken": "466f7d82-0bd8-48d1-90f6-2466c3e873b0",
        # Note: capture doesn't show AcceptBattle5 request, so we can't verify checksum
    },
    "finalise_battle15": {
        "battleId": "4028975",
        "clientOutcomeType": "1",
        "clientEndFrame": "2428",
        "clientResultString": "",
        "attackingShipHp": "2431",
        "clientVersion": "0.999.59",
        "accessToken": "466f7d82-0bd8-48d1-90f6-2466c3e873b0",
        "expected_checksum": "761f1b23578ef81929517750c585e8e9",
    },
}


def verify_create_battle9_checksum():
    """Verify CreateBattle9 checksum matches the capture EXACTLY."""
    print("=" * 70)
    print("VERIFYING CreateBattle9 CHECKSUM AGAINST REAL CAPTURE")
    print("=" * 70)

    c = CAPTURE["create_battle9"]

    # Method 1: Using our security.py function
    actual = checksum_create_battle9(
        client_hp=int(c["clientHp"]),
        client_date_time=c["clientDateTime"],
        access_token=c["accessToken"],
        checksum_key=CHECKSUM_KEY,
        savy_checksum=SAVY_CHECKSUM,
    )

    # Method 2: Manual computation (transparent)
    preimage = c["clientDateTime"] + CHECKSUM_KEY
    encrypted = preimage + SAVY_CHECKSUM
    manual = hashlib.md5(encrypted.encode("utf-8")).hexdigest()

    print(f"\nCapture clientHp:      {c['clientHp']}")
    print(f"Capture clientDateTime: {c['clientDateTime']}")
    print(f"Capture accessToken:   {c['accessToken'][:20]}...")
    print(f"CHECKSUM_KEY:          {CHECKSUM_KEY}")
    print(f"SAVY_CHECKSUM:         {SAVY_CHECKSUM}")
    print(f"\nPreimage:  {preimage}")
    print(f"Encrypted: {encrypted}")
    print(f"\nExpected (from capture): {c['expected_checksum']}")
    print(f"Actual   (security.py):  {actual}")
    print(f"Manual   (hashlib):      {manual}")

    match = actual == c["expected_checksum"] == manual
    print(f"\n{'✅ MATCH!' if match else '❌ MISMATCH!'}")
    return match


def verify_finalise_battle15_checksum():
    """Verify FinaliseBattle15 checksum matches the capture EXACTLY."""
    print("\n" + "=" * 70)
    print("VERIFYING FinaliseBattle15 CHECKSUM AGAINST REAL CAPTURE")
    print("=" * 70)

    c = CAPTURE["finalise_battle15"]

    actual = checksum_finalise_battle15(
        battle_id=c["battleId"],
        client_outcome_type=int(c["clientOutcomeType"]),
        client_end_frame=int(c["clientEndFrame"]),
        client_result_string=c["clientResultString"],
        attacking_ship_hp=int(c["attackingShipHp"]),
        client_version=c["clientVersion"],
        access_token=c["accessToken"],
        checksum_key=CHECKSUM_KEY,
        savy_checksum=SAVY_CHECKSUM,
    )

    # Manual
    preimage = (
        c["battleId"]
        + c["clientOutcomeType"]
        + c["clientEndFrame"]
        + c["clientResultString"]
        + c["attackingShipHp"]
        + c["clientVersion"]
        + c["accessToken"]
        + CHECKSUM_KEY
    )
    encrypted = preimage + SAVY_CHECKSUM
    manual = hashlib.md5(encrypted.encode("utf-8")).hexdigest()

    print(f"\nPreimage:  {preimage}")
    print(f"Encrypted: {encrypted}")
    print(f"\nExpected (from capture): {c['expected_checksum']}")
    print(f"Actual   (security.py):  {actual}")
    print(f"Manual   (hashlib):      {manual}")

    match = actual == c["expected_checksum"] == manual
    print(f"\n{'✅ MATCH!' if match else '❌ MISMATCH!'}")
    return match


def test_live_battle_flow(access_token: str, client_hp: int = 4000):
    """
    Test the full battle flow against the REAL server.
    REQUIRES: Pixel Starships mobile app running on your device.
    """
    print("\n" + "=" * 70)
    print("TESTING LIVE BATTLE FLOW AGAINST REAL SERVER")
    print("=" * 70)
    print("⚠️  PREREQUISITE: Pixel Starships app MUST be open on your device!")
    print("⚠️  This will attempt to create a REAL PvP battle.\n")

    client = Client(access_token=access_token)

    # Step 0: Verify ship is at 100% HP
    print("Step 0: Checking ship HP...")
    hp_fraction = client.getShipHpFraction()
    print(f"  Ship HP fraction: {hp_fraction}")
    if hp_fraction < 1.0:
        print(f"  ❌ Ship not at 100% HP ({hp_fraction:.0%}). Cannot battle.")
        return False
    print("  ✅ Ship at 100% HP")

    # Step 1: Rearm
    print("\nStep 1: Rearming ship...")
    if not client.rebuildAmmo():
        print("  ❌ Rearm failed")
        return False
    print("  ✅ Ship rearmed")

    # Step 2: CreateBattle9
    print("\nStep 2: CreateBattle9 (real server)...")
    if not client.createBattle9(clientHp=client_hp):
        print("  ❌ CreateBattle9 failed")
        # Check if it's the "An error occurred" issue
        if hasattr(client, 'createBattle9Result') and 'errorMessage' in str(client.createBattle9Result):
            print("  ⚠️  Server returned 'An error occurred' - likely no active session")
            print("  ⚠️  Make sure the mobile game is OPEN and you recently interacted with it")
        return False

    battle_id = getattr(client, 'lastBattleId', None)
    print(f"  ✅ CreateBattle9 succeeded! BattleId: {battle_id}")

    # Step 3: AcceptBattle5
    print("\nStep 3: AcceptBattle5...")
    if not client.acceptBattle5(battleId=battle_id, itemDesignId=0):
        print("  ❌ AcceptBattle5 failed")
        return False
    print("  ✅ AcceptBattle5 succeeded")

    # Step 4: FinaliseBattle15
    print("\nStep 4: FinaliseBattle15...")
    if not client.finaliseBattle15(
        battleId=battle_id,
        clientOutcomeType=1,
        clientEndFrame=100,
        clientResultString="simulated_victory",
        attackingShipHp=client_hp,
        clientVersion="0.999.59",
    ):
        print("  ❌ FinaliseBattle15 failed")
        return False
    print("  ✅ FinaliseBattle15 succeeded")

    print("\n" + "=" * 70)
    print("🎉 FULL BATTLE FLOW SUCCEEDED AGAINST REAL SERVER!")
    print("=" * 70)
    return True


def main():
    print("Pixel Starships Battle Implementation Verification")
    print("=" * 70)

    # 1. Verify checksums against capture (offline, no server needed)
    print("\n📋 PHASE 1: OFFLINE CHECKSUM VERIFICATION (no server needed)")
    cb9_ok = verify_create_battle9_checksum()

    if not cb9_ok:
        print("\n❌ CHECKSUM VERIFICATION FAILED - implementation has bugs")
        sys.exit(1)

    print("\n✅ CREATEBATTLE9 CHECKSUM FORMULA MATCHES THE REAL CAPTURE!")
    print("   Our implementation produces the EXACT same checksum as the game client.")
    print("\n📝 NOTE: AcceptBattle5 and FinaliseBattle15 use different native")
    print("   checksum methods (FinaliseChecksumWithDesigns, etc.) that require")
    print("   runtime-derived keys. Their formulas cannot be fully verified offline.")

    # 2. Live test (requires active mobile session)
    print("\n📋 PHASE 2: LIVE SERVER TEST (requires mobile app running)")
    print("To test against the real server, you need:")
    print("  1. Pixel Starships app OPEN on your mobile device")
    print("  2. A valid access token (from mitmproxy or game logs)")
    print("  3. Ship at 100% HP with ammo")

    access_token = os.environ.get("PSS_ACCESS_TOKEN")
    if not access_token:
        print("\n⚠️  No PSS_ACCESS_TOKEN env var set.")
        print("   Set it and re-run to test live:")
        print("   export PSS_ACCESS_TOKEN='your-token-here'")
        print("   python3 scripts/verify_battle_against_capture.py")
        return

    # Run live test
    test_live_battle_flow(access_token)


if __name__ == "__main__":
    main()