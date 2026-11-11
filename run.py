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


def _fleet_report_sections(registry, journals, fleet_run, now):
    """The fleet report and alerts sections, with the isolation audit applied."""
    from sdk.fleet.audit import audit_fleet
    from sdk.reports import alerts as alert_report
    from sdk.reports import fleet as fleet_report

    audit = audit_fleet(registry, journals)
    report = fleet_report.compute_fleet_report(journals, fleet_run=fleet_run, audit=audit)
    alerts = alert_report.evaluate_alerts(journals, now=now, audit=audit)
    return ["", "## Fleet report", "", fleet_report.to_markdown(report).rstrip("\n"),
            "", "## Alerts", "", alert_report.to_markdown(alerts).rstrip("\n")]


FLEET_STOP_FILE = "STOP"  # <journal dir>/STOP halts --fleet-execute between actions and accounts
RUN_RECORD_DIR = "runs"  # <journal dir>/runs/<UTC time>.json: one record per --fleet-execute run
RUN_RECORD_VERSION = 1
EXECUTE_MODES = ("canary", "full")


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
                      device_cls=None, clock=None, halt_share=None):
    """Execute every enabled registry account's plan under an owner-written authorization (SPEC-05).

    This is the only code path that calls a fleet orchestrator's
    ``run(execute=True)``. Before any login it refuses (exit code 2) when the
    registry or authorization is missing or invalid, the authorization does
    not cover the current time, the mode or every enabled account, the
    identity salt is unset or too short, an enabled profile declares no
    ``expected_game_identity``, an enabled account has no completed
    ``--fleet-dry-run`` of the same registry and policy within 24 hours, the
    fleet stop file ``<journal dir>/STOP`` exists, or the run record directory
    cannot be created. ``plan_only`` prints what the run would do from the
    dry-run plans and returns 0 without logging in.

    Otherwise each account runs under ``sdk.fleet.factory.ExecutionGate``:
    ``canary`` mode (the default) executes at most one action per account,
    the first authorized safe-to-retry action of its plan; ``full`` executes
    the authorized part of the plan. Starbux is never spent. The command
    prints the accounts table, the reconciliation, the fleet report and the
    alerts, writes a run record under ``<journal dir>/runs/``, and returns 0
    when every enabled account completed, otherwise 1.
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
    }
    record_path = _write_run_record(runs_dir, fleet_run.started_at, record)

    journals = _read_fleet_journals(registry, factory)
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
    if journals is not None:
        lines += _fleet_report_sections(registry, journals, fleet_run, clock())
    out.write("\n".join(lines) + "\n")
    completed = all(run.status in ("completed", "disabled") for run in fleet_run.accounts)
    return 0 if journals is not None and completed and fleet_run.stop_reason is None else 1


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
        help="account registry JSON: required by --fleet-dry-run and --fleet-execute; adds the isolation audit "
        "to --report fleet",
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
        help="HTML file to write for --report dashboard (required there); replaced atomically",
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
        "then print the reconciliation and write a run record; refused before any login without a valid "
        "authorization, --identity-salt-env, an expected_game_identity per account and a dry run in the "
        "last 24 hours",
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
        "--concurrent",
        dest="concurrent",
        type=positive_int,
        default=None,
        metavar="N",
        help="with --fleet-dry-run or --fleet-execute: run up to N accounts at once (default: one at a time)",
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
        help="with --fleet-dry-run or --fleet-execute (required there): environment variable holding the game "
        "identity salt",
    )
    args = parser.parse_args()

    # The fleet commands are handled before any SMTP setup or single-account login.
    single_account_flags = (
        ("--report", args.report), ("--journal", args.journals), ("--output", args.output),
        ("--progression-dry-run", args.progression_dry_run), ("--run-battle", args.run_battle),
        ("--auth-file", args.auth_file), ("--login-email", args.login_email),
        ("--password-file", args.password_file), ("--device-key", args.device_key),
        ("--smtp-email", args.smtp_email), ("--smtp-password-file", args.smtp_password_file),
        ("--recipient", args.recipient),
    )
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
                                   halt_share=args.halt_share))
    if args.authorization or args.mode or args.plan_only or args.halt_share is not None:
        parser.error("--authorization, --mode, --plan-only and --halt-share are only used with --fleet-execute")
    if args.fleet_dry_run:
        conflicting = [flag for flag, value in single_account_flags if value]
        if conflicting:
            parser.error(f"--fleet-dry-run cannot be combined with {', '.join(conflicting)}")
        if not args.registry:
            parser.error("--fleet-dry-run needs --registry PATH")
        sys.exit(run_fleet_dry_run(args.registry, args.fleet_journal_dir, args.concurrent,
                                   args.identity_salt_env))
    if args.concurrent is not None or args.fleet_journal_dir or args.identity_salt_env:
        parser.error("--concurrent, --fleet-journal-dir and --identity-salt-env are only used with --fleet-dry-run "
                     "or --fleet-execute")

    # Offline reports read local journals only: handled before any SMTP setup,
    # Device/Client construction, credential prompt, login or network access.
    if args.report:
        if not args.journals:
            parser.error("--report needs at least one --journal PATH")
        if args.report == "dashboard" and not args.output:
            parser.error("--report dashboard needs --output PATH")
        if args.output and args.report != "dashboard":
            parser.error("--output is only used with --report dashboard")
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