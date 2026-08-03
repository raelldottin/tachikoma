import sys
import getpass
from configparser import ConfigParser
from configparser import NoSectionError
import smtplib
from email.message import EmailMessage
import argparse
import logging
import io
from sdk.client import Client
from sdk.device import Device


logfilepath = "tachikoma.log"
log_catpure_string = io.StringIO()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(logfilepath),
        logging.StreamHandler(sys.stdout),
        logging.StreamHandler(log_catpure_string),
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

    logs = log_catpure_string.getvalue()
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
    log_catpure_string.close()
    return True


def authenticate(device, email=None, password=None):
    client = Client(device=device)

    if email and password:
        if not client.login(email=email, password=password):
            logging.warning("[authenticate] failed to login")
            return False
    else:
        if not client.login():
            logging.warning("[authenticate] failed to login")
            return False

    return client


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
        help="authentication string",
    )
    parser.add_argument(
        "--login-email",
        dest="login_email",
        default=None,
        help="email for game login (password will be prompted)",
    )
    parser.add_argument(
        "-e",
        "--email",
        nargs=1,
        action="store",
        dest="email",
        default=None,
        help="username for smtp",
    )
    parser.add_argument(
        "-p",
        "--password",
        nargs=1,
        action="store",
        dest="password",
        default=None,
        help="password for smtp",
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

    if type(args.auth) == list:
        device = Device(language="en", authentication_string=args.auth[0])
    else:
        device = Device(language="en")

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
    if (
        type(args.email) == list
        and type(args.password) == list
        and type(args.recipient) == list
    ):
        email_logfile(
            logfilepath, client, args.email[0], args.password[0], args.recipient[0]
        )
    else:
        email_logfile(logfilepath, client)


if __name__ == "__main__":
    main()