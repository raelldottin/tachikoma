from __future__ import annotations

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.client import Client
from sdk.device import Device


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def main() -> int:
    auth_string = sys.argv[1] if len(sys.argv) > 1 else None
    if not auth_string:
        print("Usage: python scripts/debug_get_ship_by_user_id.py '<auth-string>'")
        return 2

    device = Device(authentication_string=auth_string)
    client = Client(device=device)

    if not client.login():
        logging.error("Authentication failed")
        return 1

    logging.info("self.user.id=%s", client.user.id)
    result = client.getShipByUserId()
    logging.info("getShipByUserId result=%r", result)

    if result and hasattr(client, "shipByUserId"):
        ship = client.shipByUserId.get("ShipService", {}).get("GetShipByUserId", {}).get("Ship", {})
        logging.info("ShipId=%s, ShipStatus=%s", ship.get("@ShipId"), ship.get("@ShipStatus"))

    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
