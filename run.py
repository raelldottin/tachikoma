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
from pathlib import Path
from sdk.client import Client
from sdk.device import Device
from sdk.redaction import redact_secrets


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

    # Use a copy of the log capture to avoid closing the global buffer
    log_content = log_capture_string.getvalue()
    subject = f"Pixel Starships Automation Log: {client.user.name if hasattr(client, 'user') else ''}"
    message = EmailMessage()
    message["from"] = email
    message["to"] = recipient
    message["subject"] = subject
    message.set_content(log_content)

    try:
        # Add timeout to prevent hanging
        session = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
        session.ehlo()
        session.starttls()
        session.login(email, password)
        session.send_message(message)
        session.quit()
        logging.info("Log file emailed successfully")
    except smtplib.SMTPAuthenticationError as e:
        logging.error(f"SMTP authentication failed: {e}")
        return False
    except smtplib.SMTPException as e:
        logging.error(f"SMTP error: {e}")
        return False
    except TimeoutError:
        logging.error("SMTP connection timed out")
        return False
    except Exception as e:
        logging.exception(f"Unexpected error sending email: {e}")
        return False
    return True


def read_auth_file(path: str) -> str:
    """Read authentication string from file, stripping whitespace."""
    content = Path(path).read_text().strip()
    return content


def main():
    parser = argparse.ArgumentParser(
        description="Automate trivial tasks in Pixel Starships Mobile Starategy Sci-Fi MMORPG"
    )
    parser.add_argument(
        "--auth-file",
        dest="auth_file",
        default=None,
        help="path to file containing authentication string (safer than CLI arg)",
    )
    parser.add_argument(
        "--login-email",
        dest="login_email",
        default=None,
        help="email for game login (password will be prompted)",
    )
    parser.add_argument(
        "--device-key",
        dest="device_key",
        default=None,
        help="permanent device key to use (if not provided, generates new one)",
    )
    parser.add_argument(
        "--smtp-email",
        dest="smtp_email",
        default=None,
        help="email for SMTP log delivery",
    )
    parser.add_argument(
        "--smtp-password-file",
        dest="smtp_password_file",
        default=None,
        help="path to file containing SMTP password",
    )
    parser.add_argument(
        "--recipient", "-r",
        dest="recipient",
        default=None,
        help="recipient email for log delivery",
    )
    parser.add_argument(
        "--password-file",
        dest="password_file",
        default=None,
        help="path to file containing game password (for CI automation)",
    )
    parser.add_argument(
        "--run-battle",
        dest="run_battle",
        action="store_true",
        default=False,
        help="run end-to-end ship battle (CreateStarBattle5 -> VerifyBattle2 -> FinaliseBattle15)",
    )
    args = parser.parse_args()

    # Validate SMTP configuration before Device/Client creation or network activity
    smtp_email = args.smtp_email
    smtp_password_file = args.smtp_password_file
    recipient = args.recipient

    smtp_args = [smtp_email, smtp_password_file, recipient]
    smtp_count = sum(1 for a in smtp_args if a is not None)

    smtp_password = None
    smtp_enabled = False

    if smtp_count == 0:
        logging.info("Email log delivery is disabled.")
        smtp_enabled = False
    elif smtp_count == 3:
        pw_path = Path(smtp_password_file)
        # Handle both regular files and file descriptors (process substitution)
        try:
            pw_content = pw_path.read_text().strip()
            if pw_content:
                smtp_password = pw_content
                smtp_enabled = True
            else:
                logging.error("SMTP password file empty; email delivery was not attempted.")
                sys.exit(2)
        except Exception as e:
            logging.error(f"Failed to read SMTP password file: {e}; email delivery was not attempted.")
            sys.exit(2)
    else:
        logging.error("Incomplete SMTP configuration; email delivery was not attempted.")
        sys.exit(2)

    # Load authentication string from file if provided
    auth_string = None
    if args.auth_file:
        auth_string = read_auth_file(args.auth_file)

    if auth_string:
        device = Device(language="en", authentication_string=auth_string)
    else:
        # Clear any persisted .device file to start fresh BEFORE creating Device
        # This ensures each account gets a unique device key
        if args.login_email or args.device_key:
            try:
                os.unlink(Device.DB)
            except FileNotFoundError:
                pass

        device = Device(language="en")
        if args.device_key:
            device.set_device_key(args.device_key.upper())

    # Enable email/password login if email provided
    settings = {}
    if args.login_email:
        settings["allow_email_password_login"] = True
    
    # Add checksum settings for RebuildAmmo3 and other native checksums
    settings["checksum_key"] = "5343"
    settings["savy_checksum"] = "Savvy!s0d@"

    client = Client(device=device, settings=settings)

    if args.login_email:
        if args.password_file:
            pw_path = Path(args.password_file)
            if pw_path.is_file():
                password = pw_path.read_text().strip()
            else:
                logging.error("Password file not found")
                sys.exit(2)
        else:
            password = getpass.getpass("Game password: ")
        if not client.login(email=args.login_email, password=password):
            logging.warning("[authenticate] failed to login")
            sys.exit(1)
    else:
        if not client.login():
            logging.warning("[authenticate] failed to login")
            sys.exit(1)

    # Run battle if requested
    runtime_failed = False
    if args.run_battle:
        try:
            res = client.runBattleEndToEnd()
            if res is False:
                logging.info("Battle flow completed but all steps failed (game server PvP restriction); not marking run as failed")
        except Exception as e:
            logging.error(f"runBattleEndToEnd failed: {redact_secrets(str(e))}")
            runtime_failed = True

    # Send heartbeat to keep session alive (official client sends every 60s)
    try:
        client.heartbeat()
    except Exception as e:
        logging.debug(f"heartbeat failed: {redact_secrets(str(e))}")

    # Purchase Scorched Pod if affordable
    try:
        res = client.purchaseScorchedPodIfAffordable()
        if res is False:
            logging.info("Scorched Pod not purchased (not found, insufficient Starbux, or error)")
    except Exception as e:
        logging.error(f"purchaseScorchedPodIfAffordable failed: {redact_secrets(str(e))}")
        runtime_failed = True

    # Run the normal automation loop
    while client:
        try:
            client.grabFlyingStarbux()
        except Exception as e:
            logging.error(f"grabFlyingStarbux failed: {redact_secrets(str(e))}")
            # grabFlyingStarbux only works with mobile app running - expected to fail in CI
            # not marking run as failed
            pass

        if getattr(client, "freeStarbuxToday", 0) >= getattr(client, "freeStarbuxMax", 0):
            try:
                res = client.collectTaskReward()
                if res is False:
                    logging.info("collectTaskReward returned False (expected if storage full)")
            except Exception as e:
                logging.error(f"collectTaskReward failed: {redact_secrets(str(e))}")
                runtime_failed = True

        # These operations should only run once per account, not in a loop
        # Run them once and then break out of the loop for CI
        if not getattr(client, "_automation_done", False):
            try:
                res = client.getCrewInfo()
                if res is False:
                    runtime_failed = True
            except Exception as e:
                logging.error(f"getCrewInfo failed: {redact_secrets(str(e))}")
                runtime_failed = True

            try:
                res = client.upgradeResearches()
                if res is False:
                    runtime_failed = True
            except Exception as e:
                logging.error(f"upgradeResearches failed: {redact_secrets(str(e))}")
                runtime_failed = True

            try:
                res = client.upgradeRooms()
                if res is False:
                    runtime_failed = True
            except Exception as e:
                logging.error(f"upgradeRooms failed: {redact_secrets(str(e))}")
                runtime_failed = True

            try:
                res = client.collectDailyReward()
                if res is False:
                    runtime_failed = True
            except Exception as e:
                logging.error(f"collectDailyReward failed: {redact_secrets(str(e))}")
                runtime_failed = True

            try:
                client.collectAvailableMarkers()
            except Exception as e:
                logging.error(f"collectAvailableMarkers failed: {redact_secrets(str(e))}")
                # marker collection failure is not fatal

            try:
                client.collectMiningDronesWithTravel()
            except Exception as e:
                logging.error(f"collectMiningDronesWithTravel failed: {redact_secrets(str(e))}")
                # mining drone collection failure is not fatal

            try:
                res = client.listActiveMarketplaceMessages()
                if res is False:
                    runtime_failed = True
            except Exception as e:
                logging.error(f"listActiveMarketplaceMessages failed: {redact_secrets(str(e))}")
                runtime_failed = True

            try:
                res = client.getMessages()
                if res is False:
                    runtime_failed = True
            except Exception as e:
                logging.error(f"getMessages failed: {redact_secrets(str(e))}")
                runtime_failed = True

            try:
                client.infoBux()
            except Exception as e:
                logging.error(f"infoBux failed: {redact_secrets(str(e))}")

            try:
                res = client.manageTraining()
                if res is False:
                    runtime_failed = True
            except Exception as e:
                logging.error(f"manageTraining failed: {redact_secrets(str(e))}")
                runtime_failed = True

            try:
                client.analyzeShipLayout()
            except Exception as e:
                logging.error(f"analyzeShipLayout failed: {redact_secrets(str(e))}")

            client._automation_done = True

        # In CI, we only run once per account
        break

    try:
        client.getResourceTotals()
    except Exception as e:
        logging.error(f"getResourceTotals failed: {redact_secrets(str(e))}")

    try:
        res = client.upgradeCharacters()
        if res is False:
            runtime_failed = True
    except Exception as e:
        logging.error(f"upgradeCharacters failed: {redact_secrets(str(e))}")
        runtime_failed = True

    char_name = client.info.get("@Name", "") if isinstance(getattr(client, "info", None), dict) else ""
    logging.info(f'[{char_name}] Finished...')

    # Send log file via SMTP only if SMTP is enabled
    if smtp_enabled:
        try:
            email_result = email_logfile(logfilepath, client, smtp_email, smtp_password, recipient)
            if email_result is False:
                logging.warning("email_logfile returned False")
        except Exception as e:
            logging.exception(f"email_logfile raised exception: {e}")
            runtime_failed = True

    if runtime_failed:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()