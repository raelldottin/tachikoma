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
from sdk.client import Client, _extract_collection
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


PROGRESSION_JOURNAL_DEFAULT = "tachikoma-progression.log"  # JSONL; *.log is gitignored


def run_progression_dry_run(client, account_alias, journal_path):
    """Plan with baseline-v1 and journal a dry run. Makes no mutating call.

    Reads design data and a fresh snapshot through read-only Client methods,
    plans, and records each planned action as a dry-run journal event.
    Returns a process exit code.
    """
    from sdk.execution import ActionRunner
    from sdk.journal import GameplayJournal
    from sdk.policy import DesignData, PolicyConfig, get_policy
    from sdk.scheduler import DailyScheduler
    from sdk.state import ClientSnapshotProvider

    client.listRoomDesigns2()
    client.listAllResearchDesigns2()
    design = DesignData.from_records(
        _extract_collection(getattr(client, "roomDesigns", None), "RoomDesign"),
        _extract_collection(getattr(client, "allResearchDesigns", None), "ResearchDesign"),
    )
    provider = ClientSnapshotProvider(client, account_alias, source="run.py --progression-dry-run")
    runner = ActionRunner(account_alias, client, provider, GameplayJournal(journal_path))
    report = DailyScheduler(runner, get_policy("baseline-v1"), design, PolicyConfig()).run(execute=False)

    print(f"Progression dry run for {account_alias} ({report.plan.policy}, observed {report.plan.observed_at})")
    if provider.failed_reads:
        print("  reads that returned no data: " + ", ".join(provider.failed_reads))
    print(f"  planned actions: {len(report.plan.requests)}")
    for index, request in enumerate(report.plan.requests, start=1):
        params = ", ".join(f"{key}={value}" for key, value in request.params.items())
        print(f"    {index}. {request.action_id}({params}): {request.reason}")
    print(f"  rejected candidates: {len(report.plan.rejected)}")
    for rejection in report.plan.rejected:
        print(f"    - {rejection.candidate}: {rejection.reason}")
    for skip in report.skipped:
        print(f"  skipped {skip.request.action_id}: {skip.reason}")
    if report.stop_reason:
        print(f"  stopped: {report.stop_reason}")
    print(f"  journal: {journal_path}")
    return 0


REPORT_KINDS = ("progression", "pvp", "fleet")
REPORT_FORMATS = ("markdown", "json")


def run_offline_report(kind, journal_paths, registry_path=None, report_format="markdown", out=None):
    """Print a report built from local journal files. Needs no login.

    Never constructs a Device or Client and makes no network call. For the
    fleet report, events are grouped by their own ``account_alias`` across all
    given journals; the registry is only read for the fleet report. Returns a process exit code: 0 on success, 2 when a journal
    or registry is missing or invalid.
    """
    from sdk.fleet.accounts import RegistryError, load_registry
    from sdk.journal import JournalError, read_events

    out = out or sys.stdout
    events = []
    try:
        for journal_path in journal_paths:
            path = Path(journal_path)
            if not path.is_file():
                print(f"error: journal not found: {journal_path}", file=sys.stderr)
                return 2
            events.extend(read_events(path))
        registry = None
        if registry_path is not None and kind == "fleet":
            if not Path(registry_path).is_file():
                print(f"error: registry not found: {registry_path}", file=sys.stderr)
                return 2
            registry = load_registry(registry_path)
    except (JournalError, RegistryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if kind == "progression":
        from sdk.reports import progression

        metrics = progression.compute_progression_metrics(events)
        text = progression.to_json(metrics) if report_format == "json" else progression.to_markdown(metrics)
    elif kind == "pvp":
        from sdk.reports import pvp
        from sdk.reports.battles import battles_from_events

        rows = pvp.compute_scorecard(battles_from_events(events))
        text = pvp.to_json(rows) if report_format == "json" else pvp.to_markdown(rows)
    else:
        from sdk.fleet.audit import audit_fleet
        from sdk.reports import fleet

        journals = {}
        for event in events:
            journals.setdefault(event.account_alias, []).append(event)
        audit = audit_fleet(registry, journals) if registry is not None else None
        report = fleet.compute_fleet_report(journals, audit=audit)
        text = fleet.to_json(report) if report_format == "json" else fleet.to_markdown(report)

    out.write(text if text.endswith("\n") else text + "\n")
    return 0


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
    parser.add_argument(
        "--progression-dry-run",
        dest="progression_dry_run",
        action="store_true",
        default=False,
        help="log in, plan the day with baseline-v1 and journal it without changing the account, then exit",
    )
    parser.add_argument(
        "--account-alias",
        dest="account_alias",
        default="primary",
        help="lowercase account alias used in the progression journal (default: primary)",
    )
    parser.add_argument(
        "--progression-journal",
        dest="progression_journal",
        default=PROGRESSION_JOURNAL_DEFAULT,
        help=f"progression journal path (default: {PROGRESSION_JOURNAL_DEFAULT})",
    )
    parser.add_argument(
        "--report",
        dest="report",
        choices=REPORT_KINDS,
        default=None,
        help="print an offline report from --journal files and exit; never logs in or touches the network",
    )
    parser.add_argument(
        "--journal",
        dest="journals",
        action="append",
        default=[],
        metavar="PATH",
        help="journal file to read for --report (repeatable)",
    )
    parser.add_argument(
        "--registry",
        dest="registry",
        default=None,
        metavar="PATH",
        help="account registry JSON for --report fleet; adds the isolation audit",
    )
    parser.add_argument(
        "--report-format",
        dest="report_format",
        choices=REPORT_FORMATS,
        default="markdown",
        help="output format for --report (default: markdown)",
    )
    args = parser.parse_args()

    # Offline reports read local journals only: handled before any SMTP setup,
    # Device/Client construction, credential prompt, login or network access.
    if args.report:
        if not args.journals:
            parser.error("--report needs at least one --journal PATH")
        sys.exit(run_offline_report(args.report, args.journals, args.registry, args.report_format))
    if args.journals or args.registry:
        parser.error("--journal and --registry are only used with --report")

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

    if args.progression_dry_run:
        sys.exit(run_progression_dry_run(client, args.account_alias, args.progression_journal))

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