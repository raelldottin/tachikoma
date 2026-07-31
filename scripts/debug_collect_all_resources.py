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
        print("Usage: python scripts/debug_collect_all_resources.py '<auth-string>'")
        return 2

    device = Device(authentication_string=auth_string)
    client = Client(device=device)

    if not client.login():
        logging.error("Authentication failed")
        return 1

    # Record state before
    logging.info("Pre-collect: credits=%s, gasTotal=%s, mineralTotal=%s",
                 getattr(client, 'credits', '?'),
                 getattr(client, 'gasTotal', '?'),
                 getattr(client, 'mineralTotal', '?'))

    result = client.collectAllResources()
    logging.info("collectAllResources result=%r", result)

    # Record state after
    logging.info("Post-collect: credits=%s, gasTotal=%s, mineralTotal=%s",
                 getattr(client, 'credits', '?'),
                 getattr(client, 'gasTotal', '?'),
                 getattr(client, 'mineralTotal', '?'))

    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
