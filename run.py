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
    if send_email(email, password, recipient, subject, log_content):
        logging.info("Log file emailed successfully")
        return True
    return False


def send_email(email, password, recipient, subject, body):
    """Send one plain-text message through the SMTP server the log delivery uses.

    Returns ``True`` once the server accepted it, ``False`` after logging why it failed.
    """
    message = EmailMessage()
    message["from"] = email
    message["to"] = recipient
    message["subject"] = subject
    message.set_content(body)

    try:
        # Add timeout to prevent hanging
        session = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
        session.ehlo()
        session.starttls()
        session.login(email, password)
        session.send_message(message)
        session.quit()
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


# Native checksum settings every game client gets (RebuildAmmo3, UserEmailPasswordAuthorize4).
CLIENT_CHECKSUM_SETTINGS = {"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"}
FLEET_JOURNAL_DIR_DEFAULT = "tachikoma-fleet-journals"  # holds <alias>.log files; *.log is gitignored


def _md_cell(value):
    return "n/a" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def run_fleet_dry_run(registry_path, journal_dir=None, concurrent=None, identity_salt_env=None, out=None,
                      environ=None, client_cls=None, device_cls=None, clock=None):
    """Dry-run every enabled registry account and print the fleet report and alerts.

    Each account logs in with the credentials its ``credentials_secret`` names
    in the environment (see ``sdk.fleet.factory``), reads design data and
    snapshots with read-only calls, and journals its plan to
    ``<journal_dir>/<alias>.log``. There is no execute path: the orchestrator
    always runs with ``execute=False``. One account's failure is recorded and
    the others still run. The plan events of an account whose dry run
    completed are journaled with a dry-run stamp (policy id, registry stamp,
    dry-run id), which ``--fleet-execute`` requires (SPEC-05, S05-R3). Returns
    0 once the fleet ran, or 2 when the registry, the identity salt or the
    journal directory is missing or invalid.
    """
    from datetime import datetime, timezone

    from sdk.fleet.accounts import RegistryError, load_registry
    from sdk.fleet.concurrent import ConcurrentFleetOrchestrator
    from sdk.fleet.factory import EnvironmentAccountFactory, LogScrubber, registry_stamp
    from sdk.fleet.orchestrator import FleetOrchestrator
    from sdk.state.snapshot import SnapshotInputError, identity_salt_key

    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    clock = clock or (lambda: datetime.now(timezone.utc))
    journal_dir = Path(journal_dir or FLEET_JOURNAL_DIR_DEFAULT)

    if not Path(registry_path).is_file():
        print(f"error: registry not found: {registry_path}", file=sys.stderr)
        return 2
    try:
        registry = load_registry(registry_path)
    except RegistryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    identity_salt = None
    if identity_salt_env:
        identity_salt = environ.get(identity_salt_env) or None
        if identity_salt is None:
            print(f"error: identity salt variable {identity_salt_env} is not set", file=sys.stderr)
            return 2
        try:
            identity_salt_key(identity_salt)
        except SnapshotInputError as error:  # the message never repeats the salt
            print(f"error: {identity_salt_env}: {error}", file=sys.stderr)
            return 2

    try:
        journal_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"error: cannot create fleet journal directory {journal_dir}: {error.strerror}", file=sys.stderr)
        return 2

    # Credential values, the salt and display names are scrubbed from every log record while the
    # fleet runs (the client logs request URLs and player names), and from the printed reasons.
    scrubber = LogScrubber()
    scrubber.add_secrets([identity_salt])
    factory_options = {"device_cls": device_cls} if device_cls is not None else {}
    # Each account's plan events are held until its outcome is known; only a completed dry run is stamped.
    factory = EnvironmentAccountFactory(
        journal_dir, client_cls=client_cls or Client, environ=environ,
        client_settings=CLIENT_CHECKSUM_SETTINGS, identity_salt=identity_salt, log_scrubber=scrubber,
        dry_run_registry_stamp=registry_stamp(registry), **factory_options,
    )
    if concurrent is not None:
        orchestrator = ConcurrentFleetOrchestrator(registry, factory, max_workers=concurrent, clock=clock)
    else:
        orchestrator = FleetOrchestrator(registry, factory, clock=clock)
    fleet_run = None
    try:
        with scrubber.installed():
            fleet_run = orchestrator.run(execute=False)  # dry run only: this command has no execute path
    finally:
        factory.commit_dry_runs({run.alias: run.status for run in (fleet_run.accounts if fleet_run else ())})
    assert fleet_run.dry_run

    journals = _read_fleet_journals(registry, factory)
    if journals is None:
        return 2
    lines = [
        f"# Fleet dry run ({fleet_run.mode})",
        "",
        f"Registry: {registry_path} ({len(registry.enabled())} of {len(registry)} accounts enabled). "
        f"Journals: {journal_dir}. Dry run only: no action was executed.",
        "",
        # Reasons from later steps carry only redact_secrets; scrub them like the factory's errors.
        *_fleet_accounts_section(fleet_run, factory, journals, lambda text: scrubber.scrub(text, names=False)),
        *_fleet_report_sections(registry, journals, fleet_run, clock()),
    ]
    out.write("\n".join(lines) + "\n")
    return 0


BASELINE_SOURCE = "run.py --baseline"


def run_baseline(registry_path, output_dir, concurrent=None, identity_salt_env=None, summary_path=None,
                 out=None, environ=None, client_cls=None, device_cls=None, clock=None):
    """Write one read-only baseline per enabled registry account (SPEC-04, S04-R1, S04-R3, S04-R6).

    Each enabled account logs in with the credentials its ``credentials_secret``
    names in the environment (``sdk.fleet.factory``), takes one snapshot with
    the snapshot provider's read-only calls, and is written with
    ``sdk.state.baseline.write_baseline`` to ``<output_dir>/<alias>-<UTC time>.json``.
    The game version is the server's latest-version setting
    (``sdk.fleet.factory.server_game_version``), ``None`` when not observed.
    There is no execute path: no orchestrator, policy or action runner is
    built. Two aliases that logged in to the same game account (by
    ``game_identity``) or an identity that differs from the profile's
    ``expected_game_identity`` are refused, and a file that already exists
    for the same alias and time is never overwritten. One account's failure
    is reported and the others are still written.

    Prints the accounts table and a Markdown summary of every baseline in
    ``output_dir`` (alias, observed time, game version, hull bracket, missing
    sections); with ``summary_path`` the summary is also written there.
    Returns 0 when every enabled account was written, 1 when any was not or
    the summary could not be written, and 2 when the registry, the identity
    salt or the output directory is missing or invalid.
    """
    from concurrent.futures import ThreadPoolExecutor

    from sdk.fleet.accounts import RegistryError, load_registry
    from sdk.fleet.factory import EnvironmentAccountFactory, LogScrubber
    from sdk.fleet.orchestrator import FleetClaims, error_reason
    from sdk.state.baseline import BaselineError, BaselineExistsError, scan_text, write_baseline
    from sdk.state.snapshot import SnapshotInputError, identity_salt_key

    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    output_dir = Path(output_dir)

    if not Path(registry_path).is_file():
        print(f"error: registry not found: {registry_path}", file=sys.stderr)
        return 2
    try:
        registry = load_registry(registry_path)
    except RegistryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    identity_salt = None
    if identity_salt_env:
        identity_salt = environ.get(identity_salt_env) or None
        if identity_salt is None:
            print(f"error: identity salt variable {identity_salt_env} is not set", file=sys.stderr)
            return 2
        try:
            identity_salt_key(identity_salt)
        except SnapshotInputError as error:  # the message never repeats the salt
            print(f"error: {identity_salt_env}: {error}", file=sys.stderr)
            return 2

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"error: cannot create baseline directory {output_dir}: {error.strerror}", file=sys.stderr)
        return 2
    if summary_path is not None and Path(summary_path).suffix.lower() == ".json":
        print("error: --baseline-summary must not be a .json file (baseline files are .json)", file=sys.stderr)
        return 2

    scrubber = LogScrubber()
    scrubber.add_secrets([identity_salt])
    factory_options = {"device_cls": device_cls} if device_cls is not None else {}
    factory = EnvironmentAccountFactory(
        output_dir, client_cls=client_cls or Client, environ=environ,
        client_settings=CLIENT_CHECKSUM_SETTINGS, identity_salt=identity_salt, log_scrubber=scrubber,
        source=BASELINE_SOURCE, **factory_options,
    )

    def observe(profile):
        try:
            return factory.open_baseline(profile, clock=clock), None
        except Exception as error:  # one account's failure must not stop the others
            return None, error_reason(error)

    enabled = registry.enabled()
    with scrubber.installed():
        if concurrent is not None and len(enabled) > 1:
            with ThreadPoolExecutor(max_workers=concurrent) as pool:
                observed = list(pool.map(observe, enabled))
        else:
            observed = [observe(profile) for profile in enabled]

    # Accounts never mix: identities are claimed in alias order, as in the fleet orchestrators.
    claims = FleetClaims(registry)
    results = {}  # alias -> (status, file name, game version, reason)
    for profile, (observation, reason) in zip(enabled, observed):
        if observation is None:
            results[profile.alias] = ("error", None, None, reason)
            continue
        refusal = claims.claim_snapshot(profile.alias, observation.snapshot)
        if refusal is not None:
            results[profile.alias] = ("refused", None, observation.game_version, refusal)
            continue
        try:
            path = write_baseline(observation.snapshot, output_dir, observation.game_version)
        except (BaselineExistsError, BaselineError, OSError) as error:
            results[profile.alias] = ("error", None, observation.game_version, error_reason(error))
            continue
        failed = (f"reads that returned no data: {', '.join(observation.failed_reads)}"
                  if observation.failed_reads else None)
        results[profile.alias] = ("written", path.name, observation.game_version, failed)

    notes = factory.notes()
    lines = [
        "# Account baselines",
        "",
        f"Registry: {registry_path} ({len(enabled)} of {len(registry)} accounts enabled). "
        f"Baselines: {output_dir}. Read-only: no action was executed.",
        "",
        "## Accounts",
        "",
        "| Account | Status | Baseline | Game version | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for alias in registry.aliases():
        status, name, version, reason = results.get(alias, ("disabled", None, None, None))
        reason = "; ".join(filter(None, [reason and scrubber.scrub(reason, names=False), *notes.get(alias, ())]))
        lines.append(f"| {alias} | {status} | {_md_cell(name)} | {_md_cell(version)} | {_md_cell(reason or None)} |")
    summary = baseline_summary(output_dir)
    lines += ["", "## Summary", "", summary.rstrip("\n")]

    code = 0 if all(results[profile.alias][0] == "written" for profile in enabled) else 1
    if summary_path is not None:
        findings = scan_text(summary)
        target = Path(summary_path)
        if findings:
            lines += ["", "Summary not written: it fails the secret scan."]
            code = 1
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(summary, encoding="utf-8")
            except OSError as error:
                lines += ["", f"Summary not written: {error.strerror}."]
                code = 1
            else:
                lines += ["", f"Summary written: {target}"]
    out.write("\n".join(lines) + "\n")
    return code


def baseline_summary(directory):
    """Markdown table of every baseline file in ``directory`` (SPEC-04, S04-R6), oldest first per alias.

    Columns: alias, observation time (UTC), game version, hull bracket and the
    sections whose completeness is ``missing``. A file that does not load is
    listed by name with the reason, which never repeats a refused value.
    """
    from sdk.reports.battles import hull_bracket
    from sdk.state.baseline import load_baseline_file
    from sdk.state.snapshot import SECTIONS, Completeness

    lines = [
        "| Account | Observed (UTC) | Game version | Hull bracket | Missing sections |",
        "| --- | --- | --- | --- | --- |",
    ]
    unreadable = []
    rows = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            loaded = load_baseline_file(path)
        except (ValueError, TypeError, OSError) as error:  # BaselineError is a ValueError
            unreadable.append(f"- {_md_cell(path.name)}: {_md_cell(type(error).__name__)}: {_md_cell(str(error))}")
            continue
        snapshot = loaded.snapshot
        missing = [name for name in SECTIONS if snapshot.section(name) is Completeness.MISSING]
        rows.append((snapshot.account_alias, snapshot.observed_at,
                     f"| {snapshot.account_alias} | {snapshot.observed_at.strftime('%Y-%m-%dT%H:%M:%SZ')} | "
                     f"{_md_cell(loaded.game_version)} | {hull_bracket(snapshot.hull_level)} | "
                     f"{', '.join(missing) or 'none'} |"))
    lines += [row for _, _, row in sorted(rows, key=lambda item: (item[0], item[1]))]
    if not rows:
        lines.append("| n/a | n/a | n/a | n/a | n/a |")
    if unreadable:
        lines += ["", "Files that are not valid baselines:", "", *unreadable]
    return "\n".join(lines) + "\n"


def _read_fleet_journals(registry, factory):
    """Every registered alias's journal that exists, or ``None`` after printing why one is unreadable."""
    from sdk.journal import JournalError, read_events

    try:
        return {alias: read_events(factory.journal_path(alias)) for alias in registry.aliases()
                if factory.journal_path(alias).is_file()}
    except JournalError as error:
        print(f"error: {error}", file=sys.stderr)
        return None


def _fleet_accounts_section(fleet_run, factory, journals, scrub_reason):
    """The accounts table of a fleet run, with each reason passed through ``scrub_reason``."""
    notes = factory.notes()
    lines = [
        "## Accounts",
        "",
        "| Account | Status | Planned | Game identity | Journal | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for run in fleet_run.accounts:
        journal = factory.journal_path(run.alias).name if run.alias in journals else None
        reason = "; ".join(filter(None, [run.reason and scrub_reason(run.reason), *notes.get(run.alias, ())])) or None
        lines.append(f"| {run.alias} | {run.status} | {run.planned} | {_md_cell(run.game_identity)} | "
                     f"{_md_cell(journal)} | {_md_cell(reason)} |")
    if fleet_run.stop_reason:
        lines += ["", f"Fleet stopped: {_md_cell(scrub_reason(fleet_run.stop_reason))}"]
    return lines


def _fleet_report_sections(registry, journals, fleet_run, now, evaluated=None):
    """The fleet report and alerts sections, with the isolation audit applied.

    ``evaluated`` is an ``(audit, alerts)`` pair already computed for these journals; without it
    both are computed here at ``now``.
    """
    from sdk.reports import alerts as alert_report
    from sdk.reports import fleet as fleet_report

    audit, alerts = evaluated or _evaluate_fleet_alerts(registry, journals, now)
    report = fleet_report.compute_fleet_report(journals, fleet_run=fleet_run, audit=audit)
    return ["", "## Fleet report", "", fleet_report.to_markdown(report).rstrip("\n"),
            "", "## Alerts", "", alert_report.to_markdown(alerts).rstrip("\n")]


def _evaluate_fleet_alerts(registry, journals, now):
    """The isolation audit of ``journals`` (owner alias -> events) and the default alert rules over both."""
    from sdk.fleet.audit import audit_fleet
    from sdk.reports.alerts import evaluate_alerts

    audit = audit_fleet(registry, journals)
    return audit, evaluate_alerts(journals, now=now, audit=audit)


def _alerts_record(alerts, scrub_text):
    """The run record's ``alerts`` entry: every field of the report, free text scrubbed (S11-R1, S11-R4)."""
    from dataclasses import asdict

    record = asdict(alerts)
    record["by_severity"] = alerts.by_severity()
    for alert in record["alerts"]:
        alert["message"] = scrub_text(alert["message"])
        alert["evidence"]["details"] = [scrub_text(detail) for detail in alert["evidence"]["details"]]
    return record


def _alert_summary(mode, fleet_run, alerts, record_path, dashboard_path, post_run_error):
    """Subject and body of the one summary e-mail of a --fleet-execute run (S11-R3), not yet scrubbed."""
    from sdk.reports.alerts import SEVERITIES

    if alerts is None:
        subject = f"Tachikoma fleet execute ({mode}): alerts not evaluated"
    else:
        counts = alerts.by_severity()
        subject = f"Tachikoma fleet execute ({mode}): " + ", ".join(f"{counts[s]} {s}" for s in SEVERITIES)
    lines = [
        f"Fleet execute ({mode}, {fleet_run.mode}) from {fleet_run.started_at} to {fleet_run.finished_at}.",
        "Accounts: " + ", ".join(f"{run.alias} {run.status}" for run in fleet_run.accounts) + ".",
    ]
    if fleet_run.stop_reason:
        lines.append(f"Fleet stopped: {fleet_run.stop_reason}")
    # File names only: the scrubber takes a long path for a token, and the operator knows the directories.
    lines += [f"Run record: {record_path.name}", f"Dashboard: {dashboard_path.name if dashboard_path else 'not written'}",
              ""]
    if alerts is None:
        lines.append(f"Alerts were not evaluated: {post_run_error}")
    else:
        lines.append(f"Alerts at {alerts.evaluated_at} ({alerts.accounts_checked} accounts, "
                     f"{alerts.events_checked} events checked):")
        for severity in SEVERITIES:
            matching = [alert for alert in alerts.alerts if alert.severity == severity]
            lines += ["", f"{severity.capitalize()} ({len(matching)})"]
            lines += [f"- {alert.account_alias} / {alert.rule_id}: {alert.message}" for alert in matching] or ["- none"]
        if alerts.rules_not_evaluated:
            lines += ["", "Not evaluated: " + ", ".join(alerts.rules_not_evaluated)]
    return subject, "\n".join(lines) + "\n"


FLEET_STOP_FILE = "STOP"  # <journal dir>/STOP halts --fleet-execute between actions and accounts
RUN_RECORD_DIR = "runs"  # <journal dir>/runs/<UTC time>.json: one record per --fleet-execute run
RUN_RECORD_VERSION = 1
EXECUTE_MODES = ("canary", "full")
DASHBOARD_DEFAULT = "dashboard.html"  # <journal dir>/dashboard.html: written after each --fleet-execute run


def _dashboard_path_problems(path, explicit, protected):
    """Why ``path`` cannot take the post-run dashboard: a missing directory, or a file it would replace."""
    if path.is_dir():
        return [f"dashboard path is a directory: {path}"]
    if explicit and not path.parent.is_dir():
        return [f"dashboard directory not found: {path.parent}"]
    resolved = path.resolve()
    if any(resolved == Path(other).resolve() for other in protected):
        return [f"dashboard path would overwrite a journal, the registry, the authorization or the stop file: {path}"]
    return []


def _read_smtp_settings(smtp_email, smtp_password_file, recipient):
    """``(password, problem)`` for the summary e-mail: all three settings or none, as the log delivery needs.

    With none, ``(None, None)``: nothing is sent. The problem never repeats the password.
    """
    given = [value for value in (smtp_email, smtp_password_file, recipient) if value is not None]
    if not given:
        return None, None
    if len(given) < 3:
        return None, "incomplete SMTP configuration: --smtp-email, --smtp-password-file and --recipient go together"
    try:
        password = Path(smtp_password_file).read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
        return None, f"cannot read the SMTP password file: {getattr(error, 'strerror', None) or type(error).__name__}"
    if not password:
        return None, "the SMTP password file is empty"
    return password, None


def _write_fleet_dashboard(registry, journals, path):
    """Write the offline dashboard (``run.py --report dashboard``) of ``journals`` to ``path`` atomically."""
    from sdk.reports import dashboard

    events = [event for alias in sorted(journals) for event in journals[alias]]
    board = dashboard.build_dashboard(events, registry=registry,
                                      journal_names=[f"{alias}.log" for alias in sorted(journals)])
    return dashboard.write_dashboard(dashboard.render_html(board), path)


def _write_run_record(directory, started_at, record):
    """Write ``record`` as ``<directory>/<UTC time>.json`` without replacing an existing record."""
    import json
    import tempfile
    from datetime import datetime

    stem = datetime.fromisoformat(started_at).strftime("%Y%m%dT%H%M%SZ")
    target, suffix = directory / f"{stem}.json", 1
    while target.exists():
        suffix += 1
        target = directory / f"{stem}-{suffix}.json"
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".run-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def run_fleet_execute(registry_path, authorization_path, mode=None, journal_dir=None, concurrent=None,
                      identity_salt_env=None, plan_only=False, out=None, environ=None, client_cls=None,
                      device_cls=None, clock=None, halt_share=None, dashboard_out=None, smtp_email=None,
                      smtp_password_file=None, recipient=None):
    """Execute every enabled registry account's plan under an owner-written authorization (SPEC-05).

    This is the only code path that calls a fleet orchestrator's
    ``run(execute=True)``. Before any login it refuses (exit code 2) when the
    registry or authorization is missing or invalid, the authorization does
    not cover the current time, the mode or every enabled account, the
    identity salt is unset or too short, an enabled profile declares no
    ``expected_game_identity``, an enabled account has no completed
    ``--fleet-dry-run`` of the same registry and policy within 24 hours, the
    fleet stop file ``<journal dir>/STOP`` exists, the dashboard path is
    unusable, the SMTP settings are incomplete or the SMTP password file is
    unreadable or empty, or the run record directory cannot be created.
    ``plan_only`` prints what the run would do from the dry-run plans and
    returns 0 without logging in, writing a dashboard or sending e-mail.

    Otherwise each account runs under ``sdk.fleet.factory.ExecutionGate``:
    ``canary`` mode (the default) executes at most one action per account,
    the first authorized safe-to-retry action of its plan; ``full`` executes
    the authorized part of the plan. Starbux is never spent. Once every
    account has finished (SPEC-11), the alerts are evaluated over the
    journals and the isolation audit, the dashboard is written to
    ``dashboard_out`` (default ``<journal dir>/dashboard.html``), and both go
    into the run record under ``<journal dir>/runs/``. With all three SMTP
    settings, one scrubbed summary e-mail of the alerts is sent after the
    record is written; without them nothing is sent. The command prints the
    accounts table, the reconciliation, the fleet report and the alerts, and
    returns 0 when every enabled account completed, the fleet did not stop,
    the alerts and dashboard were produced and no critical alert fired,
    otherwise 1.
    """
    from datetime import datetime, timezone

    from sdk.compat import DECLARED_CLIENT_VERSION
    from sdk.fleet.accounts import RegistryError, load_registry
    from sdk.fleet.authorization import AuthorizationError, authorization_hash, check_authorization, load_authorization
    from sdk.fleet.factory import (
        DEFAULT_HALT_SHARE,
        EXECUTE_SOURCE,
        ConcurrentExecuteFleetOrchestrator,
        EnvironmentAccountFactory,
        ExecuteFleetOrchestrator,
        ExecutionGate,
        LogScrubber,
        canary_choice,
        latest_dry_run,
        registry_hash,
        registry_stamp,
    )
    from sdk.journal import JournalError, read_events
    from sdk.state.snapshot import SnapshotInputError, identity_salt_key

    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    clock = clock or (lambda: datetime.now(timezone.utc))
    journal_dir = Path(journal_dir or FLEET_JOURNAL_DIR_DEFAULT)
    mode = mode or "canary"
    halt_share = DEFAULT_HALT_SHARE if halt_share is None else halt_share

    def refuse(reasons):
        print("error: --fleet-execute refused before any login:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 2

    if mode not in EXECUTE_MODES:
        return refuse([f"mode must be one of: {', '.join(EXECUTE_MODES)}"])
    if isinstance(halt_share, bool) or not isinstance(halt_share, (int, float)) or not 0 <= halt_share < 1:
        return refuse(["halt share must be at least 0 and below 1"])
    if not Path(registry_path).is_file():
        return refuse([f"registry not found: {registry_path}"])
    try:
        registry = load_registry(registry_path)
    except RegistryError as error:
        return refuse([str(error)])
    if not Path(authorization_path).is_file():
        return refuse([f"authorization not found: {authorization_path}"])
    try:
        authorization = load_authorization(authorization_path)
    except AuthorizationError as error:
        return refuse([f"invalid authorization: {error}"])

    if not identity_salt_env:
        return refuse(["--identity-salt-env NAME is required: the identity check is mandatory in execute mode"])
    identity_salt = environ.get(identity_salt_env) or None
    if identity_salt is None:
        return refuse([f"identity salt variable {identity_salt_env} is not set"])
    try:
        identity_salt_key(identity_salt)
    except SnapshotInputError as error:  # the message never repeats the salt
        return refuse([f"{identity_salt_env}: {error}"])

    now = clock()
    enabled = registry.enabled()
    reasons = list(check_authorization(authorization, registry, now, [profile.alias for profile in enabled], (),
                                       mode))
    stamp = registry_stamp(registry)
    plans, journal_events = {}, {}
    for profile in enabled:
        if profile.expected_game_identity is None:
            reasons.append(f"account {profile.alias!r} declares no expected_game_identity; "
                           "execute mode needs one for every enabled account")
        try:
            events = read_events(journal_dir / f"{profile.alias}.log")
        except JournalError as error:
            reasons.append(f"account {profile.alias!r}: {error}")
            continue
        journal_events[profile.alias] = events
        plan, why = latest_dry_run(events, profile, stamp, now)
        if plan is None:
            reasons.append(f"account {profile.alias!r}: {why}; run --fleet-dry-run with this registry first")
        else:
            plans[profile.alias] = plan
    stop_file = journal_dir / FLEET_STOP_FILE
    if stop_file.exists():
        reasons.append(f"fleet stop file present: {stop_file}")
    dashboard_path = Path(dashboard_out) if dashboard_out else journal_dir / DASHBOARD_DEFAULT
    reasons += _dashboard_path_problems(dashboard_path, dashboard_out is not None, [
        *(journal_dir / f"{alias}.log" for alias in registry.aliases()), registry_path, authorization_path, stop_file])
    smtp_password, smtp_problem = _read_smtp_settings(smtp_email, smtp_password_file, recipient)
    if smtp_problem:
        reasons.append(smtp_problem)
    if reasons:
        return refuse(reasons)

    gate = ExecutionGate(authorization, registry, mode, clock)
    if plan_only:
        lines = [
            f"# Fleet execute plan ({mode}), plan only",
            "",
            f"Registry: {registry_path} ({len(enabled)} of {len(registry)} accounts enabled). "
            f"Authorization by {authorization.owner!r}, valid until {authorization.expires.isoformat()}. "
            "Plan only: nothing was logged in and no action was executed.",
            "",
            "From each account's latest dry run. The execute run plans again from a fresh snapshot, "
            "so its plan can differ.",
            "",
            "| Account | Dry run finished | Would run | Not authorized | Minerals cap | Gas cap |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for profile in enabled:
            planned = [event.action_id for event in plans[profile.alias].would_call()]
            def events(alias=profile.alias):  # the account's journal events, for the battle cap
                return journal_events[alias]

            refused = [action for action in planned if gate.refusal(profile.alias, action, events) is not None]
            allowed = [action for action in planned if action not in refused]
            if gate.canary:
                choice = canary_choice(planned, gate, profile.alias, events)
                allowed = [choice] if choice else []
            budgets = gate.budgets(profile)
            lines.append(f"| {profile.alias} | {plans[profile.alias].finished_at.isoformat()} | "
                         f"{', '.join(allowed) or 'nothing'} | {', '.join(refused) or 'none'} | "
                         f"{budgets['minerals']} | {budgets['gas']} |")
        out.write("\n".join(lines) + "\n")
        return 0

    runs_dir = journal_dir / RUN_RECORD_DIR
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return refuse([f"cannot create run record directory {runs_dir}: {error.strerror}"])

    # The dry run's scrubbing: credential values, the salt and display names never reach a log record,
    # and every printed or recorded reason is scrubbed too.
    scrubber = LogScrubber()
    scrubber.add_secrets([identity_salt])
    factory_options = {"device_cls": device_cls} if device_cls is not None else {}
    factory = EnvironmentAccountFactory(
        journal_dir, client_cls=client_cls or Client, environ=environ,
        client_settings=CLIENT_CHECKSUM_SETTINGS, identity_salt=identity_salt, log_scrubber=scrubber,
        source=EXECUTE_SOURCE, **factory_options,
    )
    options = {"gate": gate, "halt_share": halt_share, "stop_file": stop_file, "clock": clock}
    if concurrent is not None:
        orchestrator = ConcurrentExecuteFleetOrchestrator(registry, factory, max_workers=concurrent, **options)
    else:
        orchestrator = ExecuteFleetOrchestrator(registry, factory, **options)
    with scrubber.installed():
        fleet_run = orchestrator.run(execute=True)  # the only execute path (SPEC-05, S05-R1)

    def scrub_text(text):
        return scrubber.scrub(text) if text else text

    record_accounts = []
    for run in fleet_run.accounts:
        report = orchestrator.reports.get(run.alias)
        plan = plans.get(run.alias)
        record_accounts.append({
            "alias": run.alias,
            "status": run.status,
            "reason": scrub_text(run.reason),
            "policy_version": registry.get(run.alias).policy_id,
            "dry_run_id": plan.dry_run_id if plan else None,
            "dry_run_finished_at": plan.finished_at.isoformat() if plan else None,
            "game_identity": run.game_identity,
            "planned": run.planned,
            "attempted": run.attempted,
            "verified": run.verified,
            "spent_this_period": dict(run.spent_this_period),
            "budget_remaining": dict(run.budget_remaining),
            "results": [{"attempt_id": result.event.attempt_id, "action_id": result.action_id,
                         "attempted": result.attempted, "outcome": result.outcome,
                         "reason": scrub_text(result.reason)}
                        for result in (report.results if report else ())],
            # Why planned candidates were not run: unaffordable, not authorized, over the battle cap, ...
            "rejected": [{"candidate": rejection.candidate, "reason": scrub_text(rejection.reason)}
                         for rejection in (report.plan.rejected if report else ())],
        })

    # SPEC-11: every account has finished. Evaluate the alerts over the journals and the audit and
    # write the dashboard beside the journals; a failure here is recorded, never a lost run record.
    journals = _read_fleet_journals(registry, factory)
    evaluated, dashboard_written, post_run_errors = None, False, []
    if journals is None:
        post_run_errors.append("a journal could not be read, so no alert was evaluated and no dashboard written")
    else:
        try:
            evaluated = _evaluate_fleet_alerts(registry, journals, clock())
        except Exception as error:
            post_run_errors.append(scrub_text(f"alerts not evaluated: {type(error).__name__}: {error}"))
        try:
            _write_fleet_dashboard(registry, journals, dashboard_path)
            dashboard_written = True
        except Exception as error:
            post_run_errors.append(scrub_text(f"dashboard not written: {type(error).__name__}: {error}"))
    alerts = evaluated[1] if evaluated else None
    critical = alerts.by_severity()["critical"] if alerts else 0
    record = {
        "version": RUN_RECORD_VERSION,
        "kind": "fleet-execute",
        "mode": mode,
        "orchestrator": fleet_run.mode,
        "started_at": fleet_run.started_at,
        "finished_at": fleet_run.finished_at,
        "authorization_hash": authorization_hash(authorization),
        "authorization": {"owner": authorization.owner, "not_before": authorization.not_before.isoformat(),
                          "expires": authorization.expires.isoformat(), "actions": list(authorization.actions)},
        "registry_hash": registry_hash(registry),
        "policy_versions": {profile.alias: profile.policy_id for profile in enabled},
        # The client version this build identifies as; the server's game version is not observed.
        "game_version": {"client": DECLARED_CLIENT_VERSION, "server": None},
        "halt_share": halt_share,
        "stop_reason": scrub_text(fleet_run.stop_reason),
        "accounts": record_accounts,
        # SPEC-11 (S11-R1): the post-run alerts (None when not evaluated) and the dashboard written.
        "alerts": _alerts_record(alerts, scrub_text) if alerts else None,
        "dashboard": str(dashboard_path) if dashboard_written else None,
        "post_run_errors": post_run_errors,
    }
    record_path = _write_run_record(runs_dir, fleet_run.started_at, record)

    # S11-R3: one summary e-mail, only with all three SMTP settings, scrubbed like every other output.
    delivery = "disabled (no SMTP settings)"
    if smtp_password is not None:
        scrubber.add_secrets([smtp_email, smtp_password, recipient])
        subject, body = _alert_summary(mode, fleet_run, alerts, record_path,
                                       dashboard_path if dashboard_written else None, "; ".join(post_run_errors))
        with scrubber.installed():
            sent = send_email(smtp_email, smtp_password, recipient, scrub_text(subject), scrub_text(body))
        delivery = "sent" if sent else "failed (see the log)"

    lines = [
        f"# Fleet execute ({mode}, {fleet_run.mode})",
        "",
        f"Registry: {registry_path} ({len(enabled)} of {len(registry)} accounts enabled). "
        f"Journals: {journal_dir}. Run record: {record_path}.",
        "",
        *_fleet_accounts_section(fleet_run, factory, journals or {}, scrub_text),
        "",
        "## Reconciliation",
        "",
        "| Account | Action | Attempted | Outcome | Reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in record_accounts:
        if not entry["results"]:
            lines.append(f"| {entry['alias']} | none | no | n/a | {_md_cell(entry['reason'] or 'no action ran')} |")
        for result in entry["results"]:
            lines.append(f"| {entry['alias']} | {result['action_id']} | {'yes' if result['attempted'] else 'no'} | "
                         f"{result['outcome']} | {_md_cell(result['reason'])} |")
    if evaluated:  # alert text is scrubbed like the record and the e-mail (S11-R4)
        lines += [scrub_text(line) for line in _fleet_report_sections(registry, journals, fleet_run, None, evaluated)]
    lines += ["", "## After the run", "",
              f"- Dashboard: {dashboard_path if dashboard_written else 'not written'}",
              f"- Summary e-mail: {delivery}"]
    lines += [f"- Error: {_md_cell(error)}" for error in post_run_errors]
    if critical:
        lines.append(f"- {critical} critical alert{'s' if critical != 1 else ''}: exit code 1")
    out.write("\n".join(lines) + "\n")
    completed = all(run.status in ("completed", "disabled") for run in fleet_run.accounts)
    # S11-R2: a critical alert fails the run only now, after every account has finished.
    clean = completed and fleet_run.stop_reason is None and not post_run_errors and not critical
    return 0 if clean else 1


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def halt_share_value(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0 <= value < 1:
        raise argparse.ArgumentTypeError("must be at least 0 and below 1")
    return value


REPORT_KINDS = ("progression", "pvp", "fleet", "dashboard")
REPORT_FORMATS = ("markdown", "json")


def run_offline_report(kind, journal_paths, registry_path=None, report_format="markdown", out=None,
                       output_path=None):
    """Print a report built from local journal files. Needs no login.

    Never constructs a Device or Client and makes no network call. For the
    fleet report and the dashboard, events are grouped by their own
    ``account_alias`` across all given journals; the registry is only read for
    those two. The dashboard is written atomically to ``output_path`` as one
    self-contained HTML file and the path is printed. Returns a process exit
    code: 0 on success, 2 when a journal or registry is missing or invalid, or
    the dashboard cannot be written.
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
        if registry_path is not None and kind in ("fleet", "dashboard"):
            if not Path(registry_path).is_file():
                print(f"error: registry not found: {registry_path}", file=sys.stderr)
                return 2
            registry = load_registry(registry_path)
    except (JournalError, RegistryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if kind == "dashboard":
        from sdk.reports import dashboard

        target = Path(output_path)
        resolved = target.resolve()
        if any(resolved == Path(journal_path).resolve() for journal_path in journal_paths):
            print(f"error: --output would overwrite a journal: {output_path}", file=sys.stderr)
            return 2
        if registry_path is not None and resolved == Path(registry_path).resolve():
            print(f"error: --output would overwrite the registry: {output_path}", file=sys.stderr)
            return 2
        board = dashboard.build_dashboard(events, registry=registry,
                                          journal_names=[Path(journal_path).name for journal_path in journal_paths])
        try:
            dashboard.write_dashboard(dashboard.render_html(board), target)
        except OSError as error:
            print(f"error: cannot write dashboard: {error}", file=sys.stderr)
            return 2
        out.write(f"{output_path}\n")
        return 0

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
        help="email for SMTP log delivery (with --fleet-execute: the alert summary e-mail)",
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
        help="print an offline report from --journal files and exit (dashboard writes HTML to --output); "
        "never logs in or touches the network",
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
        help="account registry JSON: required by --fleet-dry-run, --fleet-execute and --baseline; adds the "
        "isolation audit to --report fleet",
    )
    parser.add_argument(
        "--report-format",
        dest="report_format",
        choices=REPORT_FORMATS,
        default="markdown",
        help="output format for --report (default: markdown); not used by the dashboard",
    )
    parser.add_argument(
        "--output",
        dest="output",
        default=None,
        metavar="PATH",
        help="HTML file to write for --report dashboard (required there; replaced atomically), or the "
        "directory --baseline writes <alias>-<UTC time>.json files to (required there; never overwritten)",
    )
    parser.add_argument(
        "--fleet-dry-run",
        dest="fleet_dry_run",
        action="store_true",
        default=False,
        help="dry-run every enabled --registry account (credentials from the environment variables its "
        "credentials_secret names), journal each plan, print the fleet report and alerts, then exit; "
        "never changes an account",
    )
    parser.add_argument(
        "--fleet-execute",
        dest="fleet_execute",
        action="store_true",
        default=False,
        help="execute every enabled --registry account's plan under --authorization (canary by default), "
        "then print the reconciliation, evaluate the alerts, write the dashboard and a run record, and with "
        "the SMTP flags e-mail one alert summary; refused before any login without a valid authorization, "
        "--identity-salt-env, an expected_game_identity per account and a dry run in the last 24 hours",
    )
    parser.add_argument(
        "--authorization",
        dest="authorization",
        default=None,
        metavar="PATH",
        help="with --fleet-execute: the owner-written authorization JSON (docs/baseline/fleet-accounts.md)",
    )
    parser.add_argument(
        "--mode",
        dest="mode",
        choices=EXECUTE_MODES,
        default=None,
        help="with --fleet-execute: canary (default; at most one safe-to-retry action per account) or full",
    )
    parser.add_argument(
        "--plan-only",
        dest="plan_only",
        action="store_true",
        default=False,
        help="with --fleet-execute: print what the run would do from the dry-run plans, without logging in",
    )
    parser.add_argument(
        "--halt-share",
        dest="halt_share",
        type=halt_share_value,
        default=None,
        metavar="SHARE",
        help="with --fleet-execute: halt the fleet once more than this share (0 to below 1) of the enabled "
        "accounts stopped (default: 0, so any stopped account halts the rest)",
    )
    parser.add_argument(
        "--dashboard-out",
        dest="dashboard_out",
        default=None,
        metavar="PATH",
        help=f"with --fleet-execute: HTML dashboard written after the run (default: <journal dir>/"
        f"{DASHBOARD_DEFAULT}); replaced atomically",
    )
    parser.add_argument(
        "--baseline",
        dest="baseline",
        action="store_true",
        default=False,
        help="log in to every enabled --registry account (credentials from the environment variables its "
        "credentials_secret names), take one read-only snapshot each, write one baseline file per alias to "
        "--output DIR with the server's game version, print a summary table, then exit; never changes an "
        "account",
    )
    parser.add_argument(
        "--baseline-summary",
        dest="baseline_summary",
        default=None,
        metavar="PATH",
        help="with --baseline: also write the Markdown summary table of every baseline in --output to PATH",
    )
    parser.add_argument(
        "--concurrent",
        dest="concurrent",
        type=positive_int,
        default=None,
        metavar="N",
        help="with --fleet-dry-run, --fleet-execute or --baseline: run up to N accounts at once (default: one at "
        "a time)",
    )
    parser.add_argument(
        "--fleet-journal-dir",
        dest="fleet_journal_dir",
        default=None,
        metavar="DIR",
        help=f"with --fleet-dry-run or --fleet-execute: directory for the per-account <alias>.log journals "
        f"(default: {FLEET_JOURNAL_DIR_DEFAULT})",
    )
    parser.add_argument(
        "--identity-salt-env",
        dest="identity_salt_env",
        default=None,
        metavar="NAME",
        help="with --fleet-dry-run, --baseline or --fleet-execute (required there): environment variable holding "
        "the game identity salt",
    )
    args = parser.parse_args()

    # The fleet and baseline commands are handled before any SMTP setup or single-account login.
    single_account_flags = (
        ("--report", args.report), ("--journal", args.journals), ("--output", args.output),
        ("--progression-dry-run", args.progression_dry_run), ("--run-battle", args.run_battle),
        ("--auth-file", args.auth_file), ("--login-email", args.login_email),
        ("--password-file", args.password_file), ("--device-key", args.device_key),
    )
    # --fleet-execute takes the SMTP flags for its one alert summary e-mail (SPEC-11); the dry run does not.
    smtp_flags = (
        ("--smtp-email", args.smtp_email), ("--smtp-password-file", args.smtp_password_file),
        ("--recipient", args.recipient),
    )
    if args.baseline:
        # Read-only: none of the execute, dry-run, report or single-account flags apply (SPEC-04, S04-R1).
        other_flags = (
            ("--fleet-dry-run", args.fleet_dry_run), ("--fleet-execute", args.fleet_execute),
            ("--authorization", args.authorization), ("--mode", args.mode), ("--plan-only", args.plan_only),
            ("--halt-share", args.halt_share is not None), ("--dashboard-out", args.dashboard_out),
            ("--fleet-journal-dir", args.fleet_journal_dir),
            *(item for item in single_account_flags if item[0] != "--output"), *smtp_flags,
        )
        conflicting = [flag for flag, value in other_flags if value]
        if conflicting:
            parser.error(f"--baseline cannot be combined with {', '.join(conflicting)}")
        if not args.registry or not args.output:
            parser.error("--baseline needs --registry PATH and --output DIR")
        sys.exit(run_baseline(args.registry, args.output, args.concurrent, args.identity_salt_env,
                              args.baseline_summary))
    if args.baseline_summary:
        parser.error("--baseline-summary is only used with --baseline")
    if args.fleet_execute:
        conflicting = [flag for flag, value in (("--fleet-dry-run", args.fleet_dry_run), *single_account_flags)
                       if value]
        if conflicting:
            parser.error(f"--fleet-execute cannot be combined with {', '.join(conflicting)}")
        if not args.registry or not args.authorization:
            parser.error("--fleet-execute needs --registry PATH and --authorization PATH")
        if not args.identity_salt_env:
            parser.error("--fleet-execute needs --identity-salt-env NAME: the identity check is mandatory")
        sys.exit(run_fleet_execute(args.registry, args.authorization, args.mode, args.fleet_journal_dir,
                                   args.concurrent, args.identity_salt_env, args.plan_only,
                                   halt_share=args.halt_share, dashboard_out=args.dashboard_out,
                                   smtp_email=args.smtp_email, smtp_password_file=args.smtp_password_file,
                                   recipient=args.recipient))
    if args.authorization or args.mode or args.plan_only or args.halt_share is not None:
        parser.error("--authorization, --mode, --plan-only and --halt-share are only used with --fleet-execute")
    if args.dashboard_out:
        parser.error("--dashboard-out is only used with --fleet-execute")
    if args.fleet_dry_run:
        conflicting = [flag for flag, value in (*single_account_flags, *smtp_flags) if value]
        if conflicting:
            parser.error(f"--fleet-dry-run cannot be combined with {', '.join(conflicting)}")
        if not args.registry:
            parser.error("--fleet-dry-run needs --registry PATH")
        sys.exit(run_fleet_dry_run(args.registry, args.fleet_journal_dir, args.concurrent,
                                   args.identity_salt_env))
    if args.concurrent is not None or args.fleet_journal_dir or args.identity_salt_env:
        parser.error("--concurrent, --fleet-journal-dir and --identity-salt-env are only used with --fleet-dry-run "
                     "or --fleet-execute (--concurrent and --identity-salt-env also with --baseline)")

    # Offline reports read local journals only: handled before any SMTP setup,
    # Device/Client construction, credential prompt, login or network access.
    if args.report:
        if not args.journals:
            parser.error("--report needs at least one --journal PATH")
        if args.report == "dashboard" and not args.output:
            parser.error("--report dashboard needs --output PATH")
        if args.output and args.report != "dashboard":
            parser.error("--output is only used with --report dashboard or --baseline")
        sys.exit(run_offline_report(args.report, args.journals, args.registry, args.report_format,
                                    output_path=args.output))
    if args.journals or args.registry or args.output:
        parser.error("--journal, --registry and --output are only used with --report")

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
    settings.update(CLIENT_CHECKSUM_SETTINGS)

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