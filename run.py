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
    subject = f"Pixel Starships Automation Log: {client.user.name if hasattr(client, 'user') else ''}"
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
        "-r",
        "--recipient",
        dest="recipient",
        default=None,
        help="recipient email for log delivery",
    )
    args = parser.parse_args()

    # Load authentication string from file if provided
    auth_string = None
    if args.auth_file:
        auth_string = read_auth_file(args.auth_file)

    if auth_string:
        device = Device(language="en", authentication_string=auth_string)
    else:
        # Fresh device: don't load from .device file when doing email/password login
        device = Device(language="en")
        if args.login_email:
            # Clear any persisted .device file to start fresh
            try:
                os.unlink(device.DB)
            except FileNotFoundError:
                pass

    # Enable email/password login if email provided
    settings = {}
    if args.login_email:
        settings["allow_email_password_login"] = True

    client = Client(device=device, settings=settings)

    if args.login_email:
        password = getpass.getpass("Game password: ")
        if not client.login(email=args.login_email, password=password):
            logging.warning("[authenticate] failed to login")
            sys.exit(1)
    else:
        if not client.login():
            logging.warning("[authenticate] failed to login")
            sys.exit(1)

    while client:
        client.grabFlyingStarbux()
        if client.freeStarbuxToday >= client.freeStarbuxMax:
            client.collectTaskReward()
            client.getCrewInfo()
            client.upgradeResearches()
            client.upgradeRooms()
            client.collectDailyReward()
            client.listActiveMarketplaceMessages()
            client.getMessages()
            client.infoBux()
            client.manageTraining()
            client.getResourceTotals()
            client.upgradeCharacters()
            logging.info(f'[{client.info["@Name"]}] Finished...')
            break

    # SMTP log delivery
    smtp_email = args.smtp_email
    smtp_password = None
    if args.smtp_password_file:
        smtp_password = Path(args.smtp_password_file).read_text().strip()
    recipient = args.recipient

    if smtp_email and smtp_password and recipient:
        email_logfile(logfilepath, client, smtp_email, smtp_password, recipient)
    else:
        email_logfile(logfilepath, client)


if __name__ == "__main__":
    main()