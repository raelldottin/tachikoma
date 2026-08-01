import sys
import getpass
import os
from configparser import ConfigParser
from configparser import NoSectionError
import smtplib
from email.message import EmailMessage
import argparse
import logging
import io
from sdk.client import Client
from sdk.device import Device

# Feature flags for experimental checksum-gated actions.
# These endpoints (CollectMarker2, RebuildAmmo3, AddStarbux2) use an MD5 checksum
# whose construction has not been reproduced from the iOS client.
# Disabled by default to avoid noisy logs and server-side throttling.
ENABLE_COLLECT_MARKER = os.environ.get("ENABLE_COLLECT_MARKER", "false").lower() == "true"
ENABLE_REBUILD_AMMO = os.environ.get("ENABLE_REBUILD_AMMO", "false").lower() == "true"
ENABLE_GRAB_STARBUCKS = os.environ.get("ENABLE_GRAB_STARBUCKS", "false").lower() == "true"

logfilepath = "tachikoma.log"
log_capture_string = io.StringIO()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(logfilepath),
        logging.StreamHandler(sys.stdout),
        logging.StreamHandler(log_capture_string),
    ],
)

def email_logfile(filename, client, email=None, password=None, recipient=None):
    if email and password and recipient:
        pass
    else:
        try:
            config = ConfigParser()
            config.read("./config.secrets")
            email = config.get("MAIL_CONFIG", "SENDER_EMAIL")
            password = config.get("MAIL_CONFIG", "SENDER_PASSWD")
            recipient = config.get("MAIL_CONFIG", "RECIPIENT_EMAIL")
        except NoSectionError:
            logging.error(
                "Unable to email log file because email authentication is not properly setup."
            )
            return None

    try:
        with open(filename, "rb") as f:
            logs = f.read()
    except:
        with open(logfilepath, "rb") as f:
            logs = f.read()

    if not logs:
        return False

    logs = log_capture_string.getvalue()
    subject = f"Pixel Starships Automation Log: {getattr(client, 'user', None) and getattr(client.user, 'name', '') or ''}"
    message = EmailMessage()
    message["from"] = email
    message["to"] = recipient
    message["subject"] = subject
    message.set_content(logs)

    try:
        session = smtplib.SMTP("smtp.gmail.com", 587)
        session.ehlo()
        session.starttls()
        session.login(email, password)
        session.send_message(message)
        session.quit()
    except:
        logging.exception("Exception occurred", exc_info=True)
    log_capture_string.close()
    return True

def main():
    parser = argparse.ArgumentParser(
        description="Automate trivial tasks in Pixel Starships Mobile Starategy Sci-Fi MMORPG"
    )
    parser.add_argument(
        "-a",
        "--auth",
        nargs=1,
        action="store",
        dest="auth",
        default=None,
        help="authentication string (key:name:refreshToken:languageKey)",
    )
    parser.add_argument(
        "-e",
        "--email",
        nargs=1,
        action="store",
        dest="email",
        default=None,
        help="email for game login and SMTP sender",
    )
    parser.add_argument(
        "-p",
        "--password",
        nargs=1,
        action="store",
        dest="password",
        default=None,
        help="password for game login and SMTP sender",
    )
    parser.add_argument(
        "-r",
        "--recipient",
        nargs=1,
        action="store",
        dest="recipient",
        default=None,
        help="recipient for the email log",
    )
    args = parser.parse_args()

    # Set email/password/recipient from args
    email = args.email[0] if args.email else None
    password = args.password[0] if args.password else None
    recipient = args.recipient[0] if args.recipient else None

    # Parse auth string — Device expects "name|key|refreshToken|languageKey"
    # Optional 5th and 6th fields: "name|key|refreshToken|languageKey|accessToken|userId"
    auth_string = args.auth[0] if args.auth else None
    if auth_string:
        device = Device(language="en", authentication_string=auth_string)
    elif email and password:
        # Non-interactive: use provided email/password
        device = Device(language="en")
    else:
        # Interactive mode
        decide = input("Input G to login as guest. Input A to login as user : ")
        if decide == "G":
            device = Device(language="en")
        else:
            email = input("Enter email: ")
            password = getpass.getpass("Enter password: ")
            device = Device(language="en")

    recipient = args.recipient[0] if args.recipient else None

    client = Client(device)

    # Attempt to log in
    if not client.login(email=email, password=password):
        logging.error("Authentication failed. Exiting.")
        # Still attempt to email log (though it may be empty or contain only the error)
        email_logfile(logfilepath, client, email, password, recipient)
        sys.exit(1)

    logging.info(f"[{client.info.get('@Name', 'unknown')}] Authenticated successfully")

    # Task loop with success tracking
    success_counts = {}
    failure_counts = {}

    try:
        client.getLatestVersion3()
        client.getTodayLiveOps2()
        client.listAllDesigns4()
        client.getShipByUserId()
    except Exception as e:
        logging.warning(f"[{client.info.get('@Name', 'unknown')}] Initialization step failed: {e}")
        failure_counts["initialization"] = failure_counts.get("initialization", 0) + 1

    # Main task loop (runs once per account)
    while client:
        # grabFlyingStarbux uses AddStarbux2 which has an incorrect checksum formula
        # (integer arithmetic instead of MD5). Gated behind ENABLE_GRAB_STARBUCKS.
        if ENABLE_GRAB_STARBUCKS:
            try:
                result = client.grabFlyingStarbux()
                if result is True:
                    success_counts["grabFlyingStarbux"] = success_counts.get("grabFlyingStarbux", 0) + 1
                elif result is False:
                    logging.warning(f"[{client.info.get('@Name', 'unknown')}] grabFlyingStarbux returned False (API error or no-op)")
                    failure_counts["grabFlyingStarbux"] = failure_counts.get("grabFlyingStarbux", 0) + 1
                else:
                    success_counts["grabFlyingStarbux"] = success_counts.get("grabFlyingStarbux", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] grabFlyingStarbux failed: {e}")
                failure_counts["grabFlyingStarbux"] = failure_counts.get("grabFlyingStarbux", 0) + 1
        else:
            logging.debug(f"[{client.info.get('@Name', 'unknown')}] grabFlyingStarbux SKIPPED (ENABLE_GRAB_STARBUCKS=false)")

        if client.freeStarbuxToday < client.freeStarbuxMax:
            logging.info(
                f"[{client.info.get('@Name', 'unknown')}] "
                f"Flying Starbux threshold not reached ({client.freeStarbuxToday}/{client.freeStarbuxMax}); "
                f"continuing with other tasks."
            )

        # Remaining tasks — run regardless of Flying Starbux result
        if client.freeStarbuxToday >= client.freeStarbuxMax or True:
            try:
                client.collectTaskReward()
                success_counts["collectTaskReward"] = success_counts.get("collectTaskReward", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] collectTaskReward failed: {e}")
                failure_counts["collectTaskReward"] = failure_counts.get("collectTaskReward", 0) + 1

            try:
                client.getCrewInfo()
                success_counts["getCrewInfo"] = success_counts.get("getCrewInfo", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] getCrewInfo failed: {e}")
                failure_counts["getCrewInfo"] = failure_counts.get("getCrewInfo", 0) + 1

            try:
                client.upgradeResearches()
                success_counts["upgradeResearches"] = success_counts.get("upgradeResearches", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] upgradeResearches failed: {e}")
                failure_counts["upgradeResearches"] = failure_counts.get("upgradeResearches", 0) + 1

            try:
                client.upgradeRooms()
                success_counts["upgradeRooms"] = success_counts.get("upgradeRooms", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] upgradeRooms failed: {e}")
                failure_counts["upgradeRooms"] = failure_counts.get("upgradeRooms", 0) + 1

            try:
                client.collectDailyReward()
                success_counts["collectDailyReward"] = success_counts.get("collectDailyReward", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] collectDailyReward failed: {e}")
                failure_counts["collectDailyReward"] = failure_counts.get("collectDailyReward", 0) + 1

            try:
                client.listActiveMarketplaceMessages()
                success_counts["listActiveMarketplaceMessages"] = success_counts.get("listActiveMarketplaceMessages", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] listActiveMarketplaceMessages failed: {e}")
                failure_counts["listActiveMarketplaceMessages"] = failure_counts.get("listActiveMarketplaceMessages", 0) + 1

            try:
                client.getMessages()
                success_counts["getMessages"] = success_counts.get("getMessages", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] getMessages failed: {e}")
                failure_counts["getMessages"] = failure_counts.get("getMessages", 0) + 1

            try:
                client.infoBux()
                success_counts["infoBux"] = success_counts.get("infoBux", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] infoBux failed: {e}")
                failure_counts["infoBux"] = failure_counts.get("infoBux", 0) + 1

            try:
                client.manageTraining()
                success_counts["manageTraining"] = success_counts.get("manageTraining", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] manageTraining failed: {e}")
                failure_counts["manageTraining"] = failure_counts.get("manageTraining", 0) + 1

            try:
                client.getResourceTotals()
                success_counts["getResourceTotals"] = success_counts.get("getResourceTotals", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] getResourceTotals failed: {e}")
                failure_counts["getResourceTotals"] = failure_counts.get("getResourceTotals", 0) + 1

            # Collect mining drones (experimental — checksum contract unknown)
            if ENABLE_COLLECT_MARKER:
                try:
                    client.listStarSystemMarkersAndUserMarkers()
                    markers = client.starSystemMarkersAndUserMarkers
                    all_markers = markers.get('GalaxyService', {}).get('ListStarSystemMarkersAndUserMarkers', {}).get('StarSystemMarkers', {}).get('StarSystemMarker', [])
                    if not isinstance(all_markers, list):
                        all_markers = [all_markers] if all_markers else []
                    
                    collected_count = 0
                    for marker in all_markers:
                        marker_id = marker.get('@StarSystemMarkerId', '')
                        marker_type = marker.get('@MarkerType', '')
                        is_collected = marker.get('@IsCollected', 'false')
                        if marker_type == 'Mining' and is_collected == 'false' and marker_id:
                            if client.collectMiningDrone(int(marker_id)):
                                collected_count += 1
                    
                    success_counts["collectMiningDrones"] = success_counts.get("collectMiningDrones", 0) + 1
                    logging.info(f'[{client.info.get("@Name", "unknown")}] Collected {collected_count} mining drones')
                except Exception as e:
                    logging.warning(f'[{client.info.get("@Name", "unknown")}] collectMiningDrones failed: {e}')
                    failure_counts["collectMiningDrones"] = failure_counts.get("collectMiningDrones", 0) + 1
            else:
                logging.info(f'[{client.info.get("@Name", "unknown")}] CollectMarker2: SKIPPED — checksum algorithm unavailable')

            # Rearm/recharge ship (experimental — checksum contract unknown)
            if ENABLE_REBUILD_AMMO:
                try:
                    client.rebuildAmmo()
                    success_counts["rebuildAmmo"] = success_counts.get("rebuildAmmo", 0) + 1
                except Exception as e:
                    logging.warning(f'[{client.info.get("@Name", "unknown")}] rebuildAmmo failed: {e}')
                    failure_counts["rebuildAmmo"] = failure_counts.get("rebuildAmmo", 0) + 1
            else:
                logging.info(f'[{client.info.get("@Name", "unknown")}] RebuildAmmo3: SKIPPED — checksum algorithm unavailable')

            try:
                client.upgradeCharacters()
                success_counts["upgradeCharacters"] = success_counts.get("upgradeCharacters", 0) + 1
            except Exception as e:
                logging.warning(f"[{client.info.get('@Name', 'unknown')}] upgradeCharacters failed: {e}")
                failure_counts["upgradeCharacters"] = failure_counts.get("upgradeCharacters", 0) + 1

            logging.info(f'[{client.info.get("@Name", "unknown")}] Finished...')
            break

    # Summary
    name = client.info.get('@Name', 'unknown') if client and hasattr(client, 'info') else 'unknown'
    logging.info(f"[{name}] === Task Execution Summary ===")
    total_success = sum(success_counts.values())
    total_failure = sum(failure_counts.values())
    logging.info(f"[{name}] Total successful actions: {total_success}")
    logging.info(f"[{name}] Total failed actions: {total_failure}")
    if success_counts:
        logging.info(f"[{name}] Successes per action: {success_counts}")
    if failure_counts:
        logging.info(f"[{name}] Failures per action: {failure_counts}")
    overall_success = (total_failure == 0)
    logging.info(f"[{name}] Overall result: {'PASSED' if overall_success else 'FAILED'}")

    # Email log
    email_logfile(logfilepath, client, email, password, recipient)

    sys.exit(0 if overall_success else 1)

if __name__ == "__main__":
    main()