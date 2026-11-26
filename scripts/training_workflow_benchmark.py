"""Offline red gate for the first SPEC-14 workflow acceptance cases."""

import argparse
import importlib
import io
import json
import os
import sys
from contextlib import chdir, redirect_stderr, redirect_stdout
from datetime import timedelta, timezone
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures" / "training"
REQUIRED = frozenset({
    "role-coverage", "preview-prestige", "paid-boundary", "stale-preflight", "reconciliation",
    "persisted-intent-restart", "overlapping-dispatch", "distinct-accounts", "store-stop-controls",
})
ADD_TRAINING = "/TrainingService/AddTraining"
SALT = "synthetic-spec14-workflow-salt"


def _valid_seeds(seeds):
    return (isinstance(seeds, list) and bool(seeds)
            and all(type(seed) is int and seed >= 0 for seed in seeds)
            and len(seeds) == len(set(seeds)))


def _valid_vector(vector):
    return (isinstance(vector, dict)
            and all(isinstance(vector.get(key), dict) and bool(vector[key]) for key in ("input", "expected")))


def load_fixtures(directory=FIXTURES):
    fixtures = json.loads((directory / "workflow_benchmark.json").read_text(encoding="utf-8"))
    holdout = json.loads((directory / "workflow_holdout.json").read_text(encoding="utf-8"))
    for package in (fixtures, holdout):
        if not isinstance(package, dict) or type(package.get("schema_version")) is not int or package["schema_version"] != 1:
            raise ValueError("unsupported SPEC-14 fixture schema")
        cases = package.get("cases")
        if not isinstance(cases, list) or not cases or not all(_valid_vector(case) for case in cases):
            raise ValueError("every package needs complete case vectors")
        ids = [case.get("id") for case in cases]
        if not all(isinstance(case_id, str) and case_id for case_id in ids) or len(ids) != len(set(ids)):
            raise ValueError("fixture IDs must be unique nonempty strings")
    if not _valid_seeds(fixtures.get("seeds")) or len(fixtures["seeds"]) != 3:
        raise ValueError("workflow fixtures require exactly three distinct seeds")
    cases = fixtures["cases"]
    if {case["id"] for case in cases} != REQUIRED or any(case.get("kind") != case["id"] for case in cases):
        raise ValueError("workflow fixtures must contain every required case and no extras")
    if not _valid_vector(fixtures.get("legacy_control")):
        raise ValueError("missing legacy control vector")
    if any(not case["id"].startswith("holdout-") or case.get("kind") not in REQUIRED
           or not _valid_seeds(case.get("seeds")) or set(case["seeds"]) & set(fixtures["seeds"])
           for case in holdout["cases"]):
        raise ValueError("holdout cases require independent seeds, IDs and a recognized case kind")
    if not any(case["kind"] == "paid-boundary" for case in holdout["cases"]):
        raise ValueError("missing independently authored paid-boundary witness")
    return fixtures, holdout


def _run_main(run, *options):
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", ["run.py", *options]), redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            run.main()
        except SystemExit as error:
            return error.code, stdout.getvalue() + stderr.getvalue()
    raise RuntimeError("fleet CLI returned without a status")


def observe_legacy_training(vector, seed):
    """Exercise the existing CLI over fake HTTP and read effects from server-owned state."""
    from sdk.fleet import factory as fleet_factory
    from sdk.fleet import orchestrator as fleet_orchestrator
    from sdk.journal import read_events
    from sdk.policy.progression import PolicyConfig
    from sdk.state.snapshot import game_identity_fingerprint
    from tests.fakes.pss_server import FakePss
    from tests.fakes.state import Research, ResearchDesign, minimal_world
    from tests.synthetic_fixtures import SYNTHETIC_DEVICE_KEY_IOS

    mineral_reserve = vector["mineral_reserve"]
    if type(mineral_reserve) is not int or mineral_reserve < 0:
        raise ValueError("fixture mineral reserve must be a nonnegative integer")
    policy_config = PolicyConfig(mineral_reserve=mineral_reserve)
    world = minimal_world()
    world.resources.mineral = vector["minerals"]
    world.resources.gas = vector["gas"]
    world.report_resource_items = True
    crew = world.add_crew(vector["character_name"], room="Academy", rarity="Legendary")
    room = world.room(crew.room_id)
    world.rooms = [room]
    room.id = crew.room_id = str(vector["room_id"])
    world.character_designs[0].id = crew.design_id = str(vector["character_design_id"])
    crew.id = str(vector["character_id"])
    course = world.training_design(str(vector["course_id"]))
    if course.name != vector["course_name"]:
        raise ValueError("fixture course ID and name identify different courses")
    course.price = f"mineral:{vector['course_mineral_cost']}"
    # The manifest names semantic training tiers. The game reports course numbers.
    research_courses = {1: 101, 2: 201, 3: 203}
    for index, (family, tier) in enumerate(vector["research"].items(), start=10):
        if family not in ("Education", "Fitness") or type(tier) is not int or tier not in research_courses:
            raise ValueError(f"unknown training research fixture: {family!r} tier {tier!r}")
        name = f"{family} {research_courses[tier]}"
        world.research_designs.append(ResearchDesign(str(index), str(index), name, 0))
        world.researches.append(Research(str(index)))

    fake = FakePss(world, seed=seed)
    now = fake.now.replace(tzinfo=timezone.utc)
    alias = vector["account"]
    env = {
        "S14_FAKE_ALPHA": f"fleet|{SYNTHETIC_DEVICE_KEY_IOS}|{world.account.refresh_token}|en",
        "S14_FAKE_SALT": SALT,
    }
    registry = {"version": 1, "accounts": [{
        "alias": alias, "objective": "offline training check", "policy_id": "training-v1",
        "credentials_secret": "S14_FAKE_ALPHA", "max_actions": 20,
        "expected_game_identity": game_identity_fingerprint(world.account.user_id, SALT),
    }]}
    authorization = {
        "version": 1, "owner": "offline-fixture", "purpose": "synthetic training workflow",
        "accounts": [alias], "actions": ["add_training"], "modes": ["full"],
        "not_before": (now - timedelta(hours=1)).isoformat(),
        "expires": (now + timedelta(hours=1)).isoformat(),
        "caps": {alias: {"minerals": 1000, "gas": 0}},
    }
    common = ("--registry", "registry.json", "--fleet-journal-dir", "journals", "--identity-salt-env", "S14_FAKE_SALT")
    with TemporaryDirectory(prefix="tachikoma-spec14-") as directory, chdir(directory):
        import run

        # Load lazy fleet modules before the fake replaces their clock bindings.
        for module in (
            "sdk.compat.ledger", "sdk.fleet.audit", "sdk.fleet.authorization", "sdk.fleet.factory",
            "sdk.fleet.orchestrator", "sdk.reports.alerts", "sdk.reports.dashboard",
            "sdk.reports.fleet", "sdk.scheduler.daily",
        ):
            importlib.import_module(module)

        Path("registry.json").write_text(json.dumps(registry), encoding="utf-8")
        Path("authorization.json").write_text(json.dumps(authorization), encoding="utf-8")
        # The current CLI has no reserve flag. Set it through the existing policy
        # configuration seam while leaving the actual dry-run and execute paths intact.
        with (
            fake.install(),
            patch.dict(os.environ, env, clear=True),
            patch.object(run, "run_fleet_dry_run", partial(run.run_fleet_dry_run, clock=lambda: now)),
            patch.object(run, "run_fleet_execute", partial(run.run_fleet_execute, clock=lambda: now)),
            patch.object(
                fleet_orchestrator, "FleetOrchestrator",
                partial(fleet_orchestrator.FleetOrchestrator, base_policy_config=policy_config),
            ),
            patch.object(
                fleet_factory, "ExecuteFleetOrchestrator",
                partial(fleet_factory.ExecuteFleetOrchestrator, base_policy_config=policy_config),
            ),
            patch.object(
                fleet_factory, "budgeted_policy_config", wraps=fleet_factory.budgeted_policy_config,
            ) as execute_budget,
        ):
            dry_status, dry_output = _run_main(run, "--fleet-dry-run", *common)
            dry_writes = [path for _, path, _ in fake.mutations()]
            if dry_status != 0 or dry_writes:
                raise RuntimeError(f"dry run status={dry_status}, writes={dry_writes}: {dry_output}")
            start = len(fake.exchanges)
            status, output = _run_main(run, "--fleet-execute", "--mode", "full", "--authorization",
                                       "authorization.json", *common)
        if not fake.exchanges[start:] or fake.violations or Path(".device").exists():
            raise RuntimeError(f"fleet CLI missed the fake or violated its contract: {fake.violations!r}")
        if execute_budget.call_count != 1 or execute_budget.call_args.args[2] != policy_config:
            raise RuntimeError("execute did not evaluate the fixture's policy configuration")
        events = read_events(Path("journals", f"{alias}.log"))
        writes = fake.mutations(fake.exchanges[start:])
        unexpected_writes = [path for _, path, _ in writes if path != ADD_TRAINING]
        if unexpected_writes:
            raise RuntimeError(f"fleet execute sent writes outside AddTraining: {unexpected_writes}")
        wrong_request = any(
            params.get("characterId") != crew.id or params.get("trainingDesignId") != course.id
            for _, _, params in writes
        )
        if wrong_request:
            raise RuntimeError("fleet CLI requested an unexpected character or training design")
        if status not in (0, 1) or not any(event.action_id == "add_training" for event in events):
            raise RuntimeError(f"fleet CLI failed before evaluating a planned training action: {output}")
        started = crew.session is not None
        if started and (crew.session.design_id != course.id or crew.session.end != fake.now + course.duration):
            raise RuntimeError("fake settled a different course or unexpected timer")
        return {
            "add_training_requests": len(writes),
            "mineral_delta": world.resources.mineral - vector["minerals"],
            "gas_delta": world.resources.gas - vector["gas"],
            "verified_starts": sum(event.action_id == "add_training" and event.outcome == "verified"
                                   and not event.dry_run for event in events),
        }


def evaluate():
    fixtures, holdout = load_fixtures()
    control = fixtures["legacy_control"]
    mismatches = []
    for seed in fixtures["seeds"]:
        actual = observe_legacy_training(control["input"], seed)
        if actual != control["expected"]:
            raise RuntimeError(f"legacy-control seed={seed}: expected {control['expected']!r}, observed {actual!r}")
    print(f"legacy-control: {len(fixtures['seeds'])} traces passed")
    cases = [(item, fixtures["seeds"]) for item in fixtures["cases"]]
    cases.extend((item, item["seeds"]) for item in holdout["cases"])
    for case, seeds in cases:
        if case["kind"] != "paid-boundary":
            print(f"{case['id']}: pending SPEC-14 implementation ({len(seeds)} frozen traces)")
            continue
        for seed in seeds:
            actual = observe_legacy_training(case["input"], seed)
            if actual != case["expected"]:
                mismatches.append((case["id"], seed, case["expected"], actual))
    return mismatches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", required=True, action="store_true")
    parser.parse_args(argv)
    try:
        mismatches = evaluate()
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"workflow benchmark could not evaluate its required witness: {error}", file=sys.stderr)
        return 2
    if not mismatches:
        print("workflow benchmark found no behavioral red witness", file=sys.stderr)
        return 2
    for case, seed, expected, actual in mismatches:
        print(f"{case} seed={seed}: expected {expected!r}, observed {actual!r}")
    print("SPEC-14 workflow gate incomplete. Unexecuted cases do not count toward a score.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
