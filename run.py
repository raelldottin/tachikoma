import sys
import random
from configparser import ConfigParser
import smtplib
from email.message import Message
import argparse
from sdk.client import Client
from sdk.device import Device


class LogFile:
    def __init__(self, filename):
        try:
            self.out_file = open(filename, "w")
        except:
            self.out_file = open("collectrss.log", "w")
        self.old_stdout = sys.stdout
        sys.stdout = self

    def write(self, text):
        self.old_stdout.write(text)
        self.out_file.write(text)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        sys.stdout = self.old_stdout


def email_logfile(filename, client, email=None, password=None, recipient=None):
    if email and password and recipient:
        pass
    else:
        config = ConfigParser()
        config.read("./config.secrets")

        try:
            email = config.get("MAIL_CONFIG", "SENDER_EMAIL")
            password = config.get("MAIL_CONFIG", "SENDER_PASSWD")
            recipient = config.get("MAIL_CONFIG", "RECIPIENT_EMAIL")
        except:
            print(
                "Unable to email log file because email authentication is not properly setup."
            )
            return None

    try:
        with open(filename, "rb") as f:
            logs = f.read()
    except:
        with open("collectrss.log", "rb") as f:
            logs = f.read()

    message = Message()
    message.set_payload(logs)
    subject = f"Pixel Starships Automation Log: {client.user.name}"

    try:
        session = smtplib.SMTP("smtp.gmail.com", 587)
        session.ehlo()
        session.starttls()
        session.ehlo()
        session.login(email, password)
        data = f"Subject: {subject} \n {message}"
        session.sendmail(email, recipient, data)
        session.quit()
    except Exception as e:
        print(e)


def authenticate(device, email=None, password=None):
    client = Client(device=device)

    if device.refreshToken:
        # print("# This device is already authorized, no need to input credentials.")
        if client.login():
            return client
        return False

    if not client.login(email=email, password=password):
        print("[authenticate]", "failed to login")
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
    logfilepath = "./collectrss.log"
    with LogFile(logfilepath):
        if type(args.auth) == list:
            device = Device(language="en", authentication_string=args.auth[0])
        else:
            device = Device(language="en")

        client = None

        if device.refreshToken:
            client = authenticate(device)
            if client:
                client.getLatestVersion3()
                client.getTodayLiveOps2()
                client.listRoomDesigns2()


        else:
            decide = input("Input G to login as guest. Input A to login as user : ")
            if decide == "G":
                client = authenticate(device)
            else:
                email = input("Enter email: ")
                password = input("Enter password: ")
                client = authenticate(device, email, password)


        while client:
            # time.sleep(random.uniform(5.0, 10.0))
            client.heartbeat()
            client.grabFlyingStarbux(random.randint(1, 2))
            if client.freeStarbuxToday >= 10:
                client.getCrewInfo()
                # Check if the ammo needs to be restock before attempting to restock the ammo
                client.rebuildAmmo()
                client.rushResearchOrConstruction()
                client.upgradeResearchorRoom()
                client.collectDailyReward()

                # if client.collectMiningDrone(11638355):
                #    print("Collected a mine drone.")
                # time.sleep(random.uniform(5.0, 10.0))
                # if client.collectMiningDrone(11638356):
                #    print("Collected a mine drone.")
                # time.sleep(random.uniform(5.0, 10.0))
                # if client.collectMiningDrone(11638362):
                #    print("Collected a mine drone.")
                # time.sleep(random.uniform(5.0, 10.0))
                # if client.placeMiningDrone("299", "1651"):
                #    print("Successfully placed mining drone.")
                # time.sleep(random.uniform(5.0, 10.0))
                # if client.placeMiningDrone("299", "1651"):
                #    print("Successfully placed mining drone.")
                # time.sleep(random.uniform(5.0, 10.0))
                # if client.placeMiningDrone("299", "1651"):
                #    print("Successfully placed mining drone.")
                # time.sleep(random.uniform(5.0, 10.0))

                client.collectAllResources()
                client.listActiveMarketplaceMessages()
                client.infoBux()
                client.listUpgradingRooms()
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
