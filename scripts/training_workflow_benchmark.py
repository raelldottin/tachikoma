"""SPEC-14 workflow benchmark: frozen, independently authored traces for the training-v2 fleet workflow (S14-R7).

Every case in ``tests/fixtures/training/workflow_benchmark.json`` (the required manifest) and
``tests/fixtures/training/workflow_holdout.json`` (the independent holdout) is a small set of synthetic accounts and
steps. Each step drives the production entry point, the real ``run.main()`` (``--fleet-dry-run``, ``--crew-plan``,
``--fleet-execute --mode full``), against a stateful FakePss (``tests.fakes``) behind the real ``sdk.client.Client``.
The oracle never reads production's own account of what happened first: received requests, applied state, charged
resources and started timers come from the fake's ledger and world; journals, run records and printed output are
compared against the frozen expected trace. Expected traces were written by hand from SPEC-14 and each fixture's
world (see each fixture's ``authored_from``), never by running this code.

Each case runs at three fixed seeds. A seed changes nothing a correct workflow may depend on: it reorders every list
reply (``FakePss(permute_seed=...)``) and relabels every id order-preservingly (``seeded_id``); expected traces name
labels only. Every case also carries a paired ``training-v1`` control with its own frozen trace, which must pass on
the unmodified code, and the red stage replays each case with ``training-v1`` as a probe (reported, never scored) to
show the behavioral cause of each unmet assertion.

Gate (``--gate``): exit 0 only when both suites score 100.000000 (passing traces / required traces), every declared
witness fired, no hard violation (account crossover, premium or budget spend, duplicate write, false verification)
occurred, every control passed, and three runs (two in this process, one in a child under another PYTHONHASHSEED)
produced byte-identical scored reports, each trace's per-step behavior digests included (so a run that makes another
allowed choice is caught even when it passes). Exit 1 otherwise; exit 2 for a fixture error (empty suite, missing,
duplicate or undeclared case, unknown label, skip code or outcome, a fixture whose sha256 is not its pinned
FIXTURE_SHA256), an oracle exception or a defect in the fake. Before running the suite the gate checks the fixture
digests itself and self-checks the fake's strict pricing and refusal rules (``fake_self_check``).

Seeded defects (``--defects``, ``--defect ID`` to select; S14-AC7): each catalogued defect (``SEEDED_DEFECTS``) is
patched into production at call time and replayed against its named targets at every seed, each target first run
unpatched. ``benchmark`` targets are frozen required, holdout or control traces; ``derived`` targets are variants built
in code from a frozen scenario, never written to a fixture. A defect is detected on a trace only when every expected
named assertion and hard code shows, with no harness error and no production exception at any step's exit. Exit 0
when every target passes unpatched and every critical defect is detected on every trace; exit 1 otherwise (a critical
defect with no trace, or no defect at all, included); exit 2 for a fixture or oracle error, a failed fake self-check,
a seeding that cannot be applied, or a harness error in any run, clean or patched. ``--gate`` never runs with a defect active (it exits 2), and neither
the determinism child's argv nor its environment carries one: a defect reaches a processes-mode dispatch child only
through that child's spec JSON.

Concurrency: ``overlap`` steps run several execute dispatches at once. ``processes`` mode is the S14-AC4 interprocess
witness: each dispatch is a child process (``--dispatch-child``) running ``run.main``, synchronised by a file barrier
and a file hold; each child serves its own copy of the world (one world shared across processes would need a socket
server, which the test runner's socket audit forbids), and the journal directory, so any production lock or store in
it, is shared. ``threads`` mode runs the dispatches as threads of this process against one shared fake world through
``run.run_fleet_execute`` (``run.main`` patches process-wide argv and stdout); each thread's stdout and stderr are
captured apart. Threads mode needs the account lock to exclude another thread of the same process too: an
``fcntl.flock`` on a file opened per dispatch does, a POSIX ``lockf``/``fcntl`` record lock (held per process) does
not, and then only the processes scenario can pass. Both scenarios are scored. A threads-mode world sets
``concurrent_sessions``: with the live server's single session, the second dispatch's login would revoke the first
dispatch's token and thread timing, not the lock, would decide the trace (an open SPEC-14 risk, reported, not scored).

Clock: production reads the real wall clock (run.py) for dry-run freshness and the authorization window, while the
fake's clock serves the game. Each scenario's fake clock starts at real now (truncated to the second) and each step
moves it up to real now again, never past it, so the two clocks agree to within a step; traces carry durations, never
timestamps. This real-clock dependence is deliberate: run.py builds its own clock and cannot be handed a fixed one.

Limits: a SimulatedCrash is a BaseException raised from the adapter, so production's ``finally`` blocks still run (a
real crash would not run them). Interfaces SPEC-14 leaves open (the argv of the new commands, where the attempt store
lives, how an intent or settle write is made to fail, the output contract, the run.py surface) are pinned in the
INTERFACE ASSUMPTIONS section
below, the one place ``spec14-workflow-eval`` may retarget them; the fixtures only name abstract step kinds.

No network and no real credentials: every credential, salt, user id and token is synthetic.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import datetime
import fnmatch
import hashlib
import importlib
import io
import itertools
import json
import logging
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # run as a script: python scripts/training_workflow_benchmark.py
    sys.path.insert(0, ROOT)

from tests.fakes import state as fake_state  # noqa: E402
from tests.fakes.pss_server import FakePss, FakePssFleet, SimulatedCrash  # noqa: E402
from tests.synthetic_fixtures import SYNTHETIC_DEVICE_KEY_IOS, SYNTHETIC_DEVICE_KEY_MAC  # noqa: E402

EXIT_OK, EXIT_GATE, EXIT_ERROR = 0, 1, 2
CHILD_ENV = "TRAINING_WORKFLOW_CHILD"
CHILD_TIMEOUT = 900.0
DISPATCH_TIMEOUT = 120.0
PROCESS_EXCHANGE_OFFSET = 1_000_000  # keeps the request indexes of different dispatch children apart
SCHEMA = "tachikoma.training-workflow/1"
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "training")
FIXTURES = {
    "required": os.path.join(FIXTURE_DIR, "workflow_benchmark.json"),
    "holdout": os.path.join(FIXTURE_DIR, "workflow_holdout.json"),
}
REQUIRED_IDS = ("role-coverage", "preview-prestige", "paid-boundary", "stale-preflight", "reconciliation",
                "persisted-intent-restart", "overlapping-dispatch", "distinct-accounts", "store-stop-controls")
REQUIRED_SEEDS = (1401, 1402, 1403)
# The holdout is pinned here too, so a case (or a seed) cannot leave the holdout together with its declaration.
HOLDOUT_IDS = ("tie-break-instance-id", "observed-zero-stat", "gas-priced-course", "malformed-price",
               "unclaimed-course", "room-level-gate", "two-copy-recipe", "retry-different-crew",
               "concurrent-two-accounts")
HOLDOUT_SEEDS = (2401, 2402, 2403)
# Tripwire (checked by --gate and tests.test_training_workflow_benchmark): the sha256 of each frozen fixture file. Only an
# approved oracle change (a reviewed fixture edit before implementation, or an owner-approved correction) may update
# these; an implementation slice never does.
FIXTURE_SHA256 = {
    "required": "a3d4cb493f155a352f5dcc82ddfc73ec6103731a2d19af5c216b7144aa62f082",
    "holdout": "7b93e4091c502125920b38875a4531d0761c6579d7b274c6d7912dc33bed8ed8",
}
SEEDS_PER_SUITE = 3
SALT_ENV = "TWB_SALT"
SALT = "synthetic-workflow-salt-0001"  # synthetic; never a real salt
REGISTRY_FILE, AUTHORIZATION_FILE, JOURNAL_DIR = "registry.json", "authorization.json", "journals"
STEP_KINDS = ("dry-run", "crew-plan", "execute", "provision", "resolve", "mutate", "overlap")
RUN_KINDS = ("dry-run", "crew-plan", "execute", "provision", "resolve")
ACCOUNT_KINDS = ("provision", "resolve")  # one run per named account
OVERLAP_MODES = ("threads", "processes")
ROOM_STATUSES = ("Normal", "Upgrading")  # fixture room status values (strict rules refuse any but "Normal")
RESEARCH_STATES = ("Completed", "Researching")  # fixture research states (strict rules need "Completed")
REGISTRY_KEYS = frozenset({"policy_id", "enabled", "max_actions", "budgets", "legacy_run_account"})  # registry_payload
PRE_KEYS = frozenset({"stop_file", "remove_state", "corrupt_state", "fail_intent_write", "fail_settle_write", "set"})
EXPECTED_KEYS = frozenset({"exit", "requests", "mutations", "applied", "other_mutations", "resource_delta", "timer",
                           "journal", "journal_planned", "crew_plan", "prestige", "skips", "dispatches", "store"})
# Journal outcomes whose observed cost is final; unknown and not-observed attempts may carry no (or an uncertain) cost.
FINAL_OUTCOMES = ("verified", "contradicted", "skipped")
TRAINING_ACTIONS = ("add_training", "finish_training", "use_consumable")
ADD_TRAINING = "POST /TrainingService/AddTraining"
CHARACTERS_READ = "GET /CharacterService/ListAllCharactersOfUser"
LOGIN = "POST /UserService/DeviceLogin17"
# Request parameter -> (label kind, trace key): how a received request is turned back into labels.
PARAM_LABELS = {"characterId": ("crew", "crew"), "trainingDesignId": ("course", "course"),
                "roomId": ("room", "room"), "upgradeRoomDesignId": ("room_design", "design")}
# Journal params -> (label kind, trace key).
JOURNAL_LABELS = {"character_id": ("crew", "crew"), "training_design_id": ("course", "course"),
                  "room_id": ("room", "room"), "upgrade_room_design_id": ("room_design", "design")}
STEP_CAUSES = (
    ("unknown policy 'training-v2'",
     "registry refuses training-v2 before login (sdk/fleet/accounts.py: POLICIES in sdk/policy/progression.py lacks "
     "it), exit 2"),
    ("unrecognized arguments: --crew-plan", "--crew-plan is not implemented (run.py argparse exits 2)"),
    ("unrecognized arguments: --fleet-provision-training",
     "--fleet-provision-training is not implemented (run.py argparse exits 2)"),
    ("unrecognized arguments: --fleet-resolve-training",
     "--fleet-resolve-training is not implemented (run.py argparse exits 2)"),
)

# --- INTERFACE ASSUMPTIONS ----------------------------------------------------------------------------------------
# SPEC-14 fixes behavior, not every interface. The fixtures name abstract step kinds and skip codes; this section is
# the one place that maps them onto concrete interfaces. spec14-crew-policy and spec14-training-execution implement
# these (or spec14-workflow-eval, the only later slice that may edit this file, retargets an entry here, never a
# fixture). Each entry, and what it asks of production:
#
# 1. ARGV_TEMPLATES: the run.py argv of each step kind; {registry}, {authorization}, {journal_dir}, {salt_env} and
#    {alias} are filled in. ``provision`` explicitly provisions a virgin attempt store for one verified account (the
#    S14 safety invariant: before its first execution). ``resolve`` records an operator's durable reconciliation of
#    that account's unresolved attempt from a fresh authoritative read (S14-R5; AC3/AC4 "until reconciled"). Neither
#    makes a gameplay write. An execute dispatch may add ``--concurrent N``.
# 2. STORE_DIR: the attempt (intent) store of account <alias> is the file or directory <journal dir>/STORE_DIR/<alias>
#    or files <journal dir>/STORE_DIR/<alias>.<suffix>; ``*.lock`` files are never the store. Nothing else in the
#    journal directory (journals, runs/, the dashboard, STOP, lock files) is treated as the store. ``store``
#    expectations and the remove/corrupt pre-states act on exactly these paths. A provisioned store holds at least
#    one file (an empty directory is not a store a run could read): ``corrupt_state`` overwrites every file of it and
#    its witness is missing when there is none to overwrite.
# 3. Store hooks, (module, attribute) pairs the attempt store calls through the module attribute at call time:
#    - INTENT_WRITE_HOOK writes an intent durably before a training request is sent. The ``fail_intent_write``
#      pre-state replaces it with one that raises OSError.
#    - SETTLE_WRITE_HOOK durably settles (records the outcome of) an attempt after its request was sent. The
#      ``fail_settle_write`` pre-state replaces it with one that raises OSError once the fake has applied an
#      AddTraining in that step (earlier calls go through), so the pending intent must survive the failed write.
#    If a hook does not exist, or never fails, the step's ``intent_write_failed`` / ``settle_write_failed`` witness
#    is reported missing with that cause (the scenario cannot pass), never silently passed.
# 4. THREADS_ENTRY: threads-mode overlap calls run.run_fleet_execute(registry, authorization, mode="full",
#    journal_dir=Path, identity_salt_env=str, out=stream, environ=dict) in each thread, never run.main(), so the
#    account lock, the attempt store and every training-v2 refusal must be reached from run_fleet_execute itself.
# 5. Output contract (what the oracle reads besides the fake's own ledger and world):
#    - A skip code counts only as a whole word in a structured record tied to the account: a run record
#      (<journal dir>/runs/*.json) account entry for that alias (its status, reason, results[].reason, or a rejected[]
#      candidate and reason), a journal event of that alias (its reason and params), or a pre-login refusal line on
#      stderr, ``  - account '<alias>' ...`` or ``  - account '<alias>': ...`` (the alias quoted, then a space or a
#      colon), naming exactly one vocabulary code. When the expectation names a crew, the code must sit with that
#      crew's instance id: in a record that names no other crew of the scenario, or in a segment of it (split at ``;``,
#      ``|`` or a line break) that names that crew alone, so a string about several candidates never satisfies
#      another crew's expectation. Free stdout never counts, so a printed legend of codes satisfies nothing.
#    - Pre-login codes the training-v2 refusals must name in such a line: ``dry_run_stale`` (no completed matching
#      dry run), ``not_authorized`` (add_training or the account not authorized) and ``legacy_run_not_skipped`` (a
#      training-v2 account with a legacy daily run account that the authorization lists only as
#      legacy_run_serialized, S14-R6). Today's legacy refusal (sdk/fleet/authorization.py) names legacy_run_skipped
#      and legacy_run_serialized, neither a vocabulary code, and does not refuse a serialized account at all.
#    - Overlap dispatches: a line of that dispatch's own output naming the alias (any line, with one account) and
#      exactly one code.
#    - Crew plan: one stdout line per crew holding the crew's instance id, the role word (repair, shields, weapons)
#      and either the course's design name or exactly one skip code; an uncovered role is a line with the role word
#      and ``role_uncovered``. Prestige: one stdout line holding the target and ingredient design names and
#      ``missing <n>``.
#    - Wire fields the fake serves for observations SPEC-14 leaves open (spec14-training-observations parses them):
#      the course price ``CostString="<currency>:<amount>"`` (absent: unavailable), ``RequiredRoomType``,
#      ``RequiredRoomLevel``, ``RequiredResearchDesignId``, ``Rank`` and ``<Stat>Chance`` on training designs; room
#      design ``Level`` and ``Capacity``; Mineral and Gas items in ListItemsOfAShip; Prestige recipes from
#      ``GET /CharacterService/PrestigeCharacterTo?characterDesignId=``.
# 6. The run.py surface the harness drives, and the sdk functions behind it (none exists today):
#    - ``--crew-plan`` (read-only; prints the crew-plan and Prestige lines of item 5, no journal directory);
#    - ``--fleet-provision-training`` and ``--fleet-resolve-training`` with the item 1 argv, ``--account <alias>``
#      naming one registry account, exit 0 on success;
#    - the pre-login refusal lines of item 5 with their codes, legacy_run_not_skipped included, printed by
#      ``--fleet-execute`` before any login;
#    - run_fleet_execute (item 4) reaching the account lock, the attempt store and those refusals itself.
#    Expected sdk entry points: sdk.execution.runner.write_training_intent and settle_training_intent (item 3),
#    provision_training_store(journal_dir, alias) and resolve_training_attempt(journal_dir, alias, observation) in
#    sdk/execution/runner.py; the training-v2 lock, store and pre-login checks wired into the execute orchestration
#    in sdk/fleet/factory.py (spec14-training-execution). SLICE-TABLE NOTE: the SPEC-14 slice table gives run.py only
#    to spec14-crew-policy, so that slice must land this whole CLI surface (the flags and refusal lines, delegating
#    to the sdk functions above, as stubs until spec14-training-execution fills them in), or the owner must amend
#    the slice table to give run.py to spec14-training-execution too. sdk/fleet/authorization.py is in no SPEC-14
#    slice, so legacy_run_not_skipped cannot come from there.
ARGV_TEMPLATES = {
    "dry-run": ("--fleet-dry-run", "--registry", "{registry}", "--fleet-journal-dir", "{journal_dir}",
                "--identity-salt-env", "{salt_env}"),
    "crew-plan": ("--crew-plan", "--registry", "{registry}", "--identity-salt-env", "{salt_env}"),
    "execute": ("--fleet-execute", "--authorization", "{authorization}", "--mode", "full", "--registry", "{registry}",
                "--fleet-journal-dir", "{journal_dir}", "--identity-salt-env", "{salt_env}"),
    "provision": ("--fleet-provision-training", "--registry", "{registry}", "--fleet-journal-dir", "{journal_dir}",
                  "--identity-salt-env", "{salt_env}", "--account", "{alias}"),
    "resolve": ("--fleet-resolve-training", "--registry", "{registry}", "--fleet-journal-dir", "{journal_dir}",
                "--identity-salt-env", "{salt_env}", "--account", "{alias}"),
}
STORE_DIR = "training-attempts"
INTENT_WRITE_HOOK = ("sdk.execution.runner", "write_training_intent")
SETTLE_WRITE_HOOK = ("sdk.execution.runner", "settle_training_intent")
THREADS_ENTRY = "run_fleet_execute"
OVERLAP_TIMEOUT_S = 30  # default barrier and hold bound (an upper bound only: each releases as soon as it can)


class FixtureError(ValueError):
    """The fixture is malformed: the gate cannot score it (exit 2)."""


class OracleError(RuntimeError):
    """The harness or the fake failed, not the candidate (exit 2)."""


# --- fixtures ---------------------------------------------------------------------------------------------------


def load_suite(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise FixtureError(f"{path}: {error}") from None
    validate_suite(data)
    return data


def _labels(catalog: Mapping, kind: str) -> set:
    return set(catalog.get(kind, {}))


def validate_suite(data: Any) -> None:
    """Raise FixtureError naming the first problem that would make the suite unscorable."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise FixtureError(f"schema must be {SCHEMA!r}")
    suite = data.get("suite")
    if suite not in FIXTURES:
        raise FixtureError(f"suite must be one of {sorted(FIXTURES)}")
    if not isinstance(data.get("declared"), str) or not data["declared"]:
        raise FixtureError("declared is required")
    authored = data.get("authored_from")
    if not isinstance(authored, dict) or not isinstance(authored.get("text"), str) or not authored["text"]:
        raise FixtureError("authored_from.text is required")
    interpretations = authored.get("interpretations")
    if not isinstance(interpretations, list) or not interpretations \
            or not all(isinstance(item, str) and item.strip() for item in interpretations) \
            or len(set(interpretations)) != len(interpretations):
        raise FixtureError("authored_from.interpretations must list each declared reading of the spec once")
    seeds = data.get("seeds")
    if not isinstance(seeds, list) or len(seeds) != SEEDS_PER_SUITE or len(set(seeds)) != len(seeds) \
            or not all(isinstance(seed, int) and not isinstance(seed, bool) and seed > 0 for seed in seeds):
        raise FixtureError(f"seeds must be {SEEDS_PER_SUITE} distinct positive integers")
    pinned_seeds = REQUIRED_SEEDS if suite == "required" else HOLDOUT_SEEDS
    if tuple(seeds) != pinned_seeds:
        raise FixtureError(f"{suite} seeds must be the pinned {list(pinned_seeds)}")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FixtureError("empty suite: cases must be a non-empty list")
    declared = data.get("cases_declared")
    if not isinstance(declared, list) or not declared:
        raise FixtureError("cases_declared must be a non-empty list")
    if len(set(declared)) != len(declared):
        raise FixtureError(f"duplicate case in cases_declared: {_duplicates(declared)}")
    ids = [case.get("id") if isinstance(case, dict) else None for case in cases]
    if len(set(ids)) != len(ids):
        raise FixtureError(f"duplicate case id: {_duplicates(ids)}")
    if set(ids) != set(declared):
        missing, extra = sorted(set(declared) - set(ids)), sorted(set(ids) - set(declared), key=str)
        raise FixtureError(f"cases differ from cases_declared: missing {missing}, undeclared {extra}")
    # Each manifest is pinned exactly, order included (it is the report order): no case missing, none added, none
    # duplicated (checked above) and none moved. A case cannot leave together with its declaration.
    if suite == "required" and tuple(declared) != REQUIRED_IDS:
        raise FixtureError(f"required manifest must be exactly {list(REQUIRED_IDS)}: "
                           f"{_manifest_difference(declared, REQUIRED_IDS)}")
    if suite == "holdout" and set(declared) & set(REQUIRED_IDS):
        raise FixtureError(f"holdout reuses required case ids: {sorted(set(declared) & set(REQUIRED_IDS))}")
    if suite == "holdout" and tuple(declared) != HOLDOUT_IDS:
        raise FixtureError(f"holdout manifest must be exactly the pinned {list(HOLDOUT_IDS)}: "
                           f"{_manifest_difference(declared, HOLDOUT_IDS)}")
    vocabulary = data.get("vocabulary")
    if not isinstance(vocabulary, dict):
        raise FixtureError("vocabulary is required")
    from sdk.journal import OUTCOMES

    if set(vocabulary.get("outcomes", ())) != set(OUTCOMES):
        raise FixtureError(f"vocabulary.outcomes must equal sdk.journal.OUTCOMES {sorted(OUTCOMES)}")
    for key in ("skip_codes", "control_skip_codes", "roles"):
        if not vocabulary.get(key):
            raise FixtureError(f"vocabulary.{key} is required")
    catalog = data.get("catalog")
    if not isinstance(catalog, dict):
        raise FixtureError("catalog is required")
    _validate_catalog(catalog)
    for case in cases:
        _validate_case(case, catalog, vocabulary)


def _manifest_difference(declared: Sequence, pinned: Sequence[str]) -> str:
    missing = [case_id for case_id in pinned if case_id not in declared]
    extra = sorted({case_id for case_id in declared if case_id not in pinned}, key=str)
    if missing or extra:
        return f"missing {missing}, unexpected {extra}"
    return f"the same ids out of order: {list(declared)}"


def _duplicates(values: Sequence) -> list:
    return sorted({value for value in values if values.count(value) > 1}, key=str)


def _validate_catalog(catalog: Mapping) -> None:
    for kind in ("room_designs", "research_designs", "character_designs", "training_designs"):
        rows = catalog.get(kind)
        if not isinstance(rows, dict) or not rows:
            raise FixtureError(f"catalog.{kind} must be a non-empty mapping")
        bases = [row.get("id") for row in rows.values()]
        if len(set(bases)) != len(bases) or not all(isinstance(b, int) and 0 < b < 100000 for b in bases):
            raise FixtureError(f"catalog.{kind}: ids must be distinct integers below 100000")
    for label, row in catalog["room_designs"].items():
        if row.get("upgrade_from") is not None and row["upgrade_from"] not in catalog["room_designs"]:
            raise FixtureError(f"room design {label}: unknown upgrade_from {row['upgrade_from']!r}")
    for label, row in catalog["research_designs"].items():
        if row.get("root", label) not in catalog["research_designs"]:
            raise FixtureError(f"research design {label}: unknown root {row.get('root')!r}")
    for label, row in catalog["training_designs"].items():
        if row.get("research") is not None and row["research"] not in catalog["research_designs"]:
            raise FixtureError(f"training design {label}: unknown research {row['research']!r}")
        if row.get("stat") not in fake_state.STATS:
            raise FixtureError(f"training design {label}: unknown stat {row.get('stat')!r}")
        _check_room_type(f"training design {label}", row, catalog)
    for recipe in catalog.get("prestige", ()):
        for label in (recipe.get("to"), *recipe.get("from", ())):
            if label not in catalog["character_designs"]:
                raise FixtureError(f"prestige recipe {recipe}: unknown character design {label!r}")


def _check_room_type(where: str, course: Mapping, catalog: Mapping) -> None:
    """A course's room_type is a room design name (FakePss strict rules compare the crew's room design name)."""
    names = {row.get("name") for row in catalog["room_designs"].values()}
    if course.get("room_type") is not None and course["room_type"] not in names:
        raise FixtureError(f"{where}: room_type {course['room_type']!r} names no room design")


def _validate_case(case: Mapping, catalog: Mapping, vocabulary: Mapping) -> None:
    where = f"case {case.get('id')}"
    for key in ("requirements", "acceptance", "summary", "scenarios"):
        if not case.get(key):
            raise FixtureError(f"{where}: {key} is required")
    control = case.get("control")
    if not isinstance(control, dict) or control.get("policy_id") != "training-v1" or not control.get("scenarios"):
        raise FixtureError(f"{where}: a training-v1 control with scenarios is required")
    if not isinstance(case.get("red"), dict) or not case["red"].get("expected_cause"):
        raise FixtureError(f"{where}: red.expected_cause is required")
    codes = set(vocabulary["skip_codes"])
    _validate_scenarios(where, case["scenarios"], catalog, vocabulary, codes)
    _validate_scenarios(f"{where} control", control["scenarios"], catalog, vocabulary,
                        codes | set(vocabulary["control_skip_codes"]))


def _validate_scenarios(where: str, scenarios: Sequence, catalog: Mapping, vocabulary: Mapping, codes: set) -> None:
    ids = [scenario.get("id") for scenario in scenarios]
    if len(set(ids)) != len(ids) or not all(ids):
        raise FixtureError(f"{where}: scenario ids must be present and distinct")
    for scenario in scenarios:
        _validate_scenario(f"{where}/{scenario['id']}", scenario, catalog, vocabulary, codes)


def _validate_scenario(where: str, scenario: Mapping, catalog: Mapping, vocabulary: Mapping, codes: set) -> None:
    accounts = scenario.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise FixtureError(f"{where}: accounts are required")
    courses = scenario.get("courses")
    if not isinstance(courses, list) or not courses or not set(courses) <= _labels(catalog, "training_designs"):
        raise FixtureError(f"{where}: courses must list catalog training designs")
    for label, override in scenario.get("course_overrides", {}).items():
        if label not in courses:
            raise FixtureError(f"{where}: course_overrides names unknown course {label!r}")
        _check_room_type(f"{where}: course_overrides.{label}", override, catalog)
    crew_labels: dict[str, str] = {}
    room_labels: dict[str, set] = {}
    bases = [(kind, row.get("base_id")) for account in accounts for kind in ("crew", "rooms")
             for row in account.get("world", {}).get(kind, ())]
    if len(set(bases)) != len(bases):
        raise FixtureError(f"{where}: crew and room base_ids must be distinct across accounts")
    for account in accounts:
        alias = account.get("alias")
        if not alias or not account.get("secret") or not account.get("refresh_token") or not account.get("user_id"):
            raise FixtureError(f"{where}: every account needs alias, secret, refresh_token and user_id")
        world = account.get("world", {})
        room_labels[alias] = set()
        unknown_registry = set(account.get("registry", {})) - REGISTRY_KEYS
        if unknown_registry:
            raise FixtureError(f"{where}: account {alias} has unknown registry keys {sorted(unknown_registry)}")
        for room in world.get("rooms", ()):
            if room.get("design") not in catalog["room_designs"]:
                raise FixtureError(f"{where}: unknown room design label {room.get('design')!r}")
            if room.get("status", "Normal") not in ROOM_STATUSES:
                raise FixtureError(f"{where}: room {room.get('label')} has unknown status {room.get('status')!r}")
            if any(room["label"] in labels for labels in room_labels.values()):
                raise FixtureError(f"{where}: room label {room['label']!r} is used twice")
            room_labels[alias].add(room["label"])
        for research in world.get("researches", ()):
            if research.get("design") not in catalog["research_designs"]:
                raise FixtureError(f"{where}: unknown research label {research.get('design')!r}")
            if research.get("state", "Completed") not in RESEARCH_STATES:
                raise FixtureError(f"{where}: research {research.get('design')} has unknown state "
                                   f"{research.get('state')!r}")
        for crew in world.get("crew", ()):
            label = crew.get("label")
            if label in crew_labels:
                raise FixtureError(f"{where}: crew label {label!r} is used twice")
            crew_labels[label] = alias
            if crew.get("design") not in catalog["character_designs"]:
                raise FixtureError(f"{where}: unknown character design label {crew.get('design')!r}")
            if crew.get("room") is not None and crew["room"] not in room_labels[alias]:
                raise FixtureError(f"{where}: crew {label} names unknown room {crew['room']!r}")
            training = crew.get("training")
            if training and training.get("course") not in courses:
                raise FixtureError(f"{where}: crew {label} trains unknown course {training.get('course')!r}")
            for stat in crew.get("stats", {}):
                if f"{stat}Improvement" not in fake_state.STATS:
                    raise FixtureError(f"{where}: crew {label} has unknown stat {stat!r}")
    steps = scenario.get("steps")
    if not isinstance(steps, list) or not steps:
        raise FixtureError(f"{where}: steps are required")
    step_ids = [step.get("id") for step in steps]
    if len(set(step_ids)) != len(step_ids) or not all(step_ids):
        raise FixtureError(f"{where}: step ids must be present and distinct")
    expected = scenario.get("expected")
    if not isinstance(expected, dict):
        raise FixtureError(f"{where}: expected is required")
    for key in expected:
        if key not in step_ids:
            raise FixtureError(f"{where}: expected names step {key!r}, which has no step")
    witnesses = set(scenario.get("witnesses", ()))
    labels = {"crew": set(crew_labels), "course": set(courses), "alias": {a["alias"] for a in accounts},
              "room": set().union(*room_labels.values()), "room_design": _labels(catalog, "room_designs"),
              "character_design": _labels(catalog, "character_designs"),
              "research": _labels(catalog, "research_designs")}
    for index, step in enumerate(steps):
        kind = step.get("kind")
        if kind not in STEP_KINDS:
            raise FixtureError(f"{where}: step {step.get('id')} has unknown kind {kind!r}")
        if "mode" in step and (kind != "overlap" or step["mode"] not in OVERLAP_MODES):
            raise FixtureError(f"{where}: step {step.get('id')} has unknown mode {step['mode']!r} (an overlap step "
                               f"takes one of {list(OVERLAP_MODES)})")
        for alias, spec in (step.get("registry_variant") or {}).items():
            if alias not in labels["alias"] or not isinstance(spec, dict) or set(spec) - REGISTRY_KEYS:
                raise FixtureError(f"{where}: step {step.get('id')} registry_variant names unknown account or keys: "
                                   f"{alias!r}: {spec!r}")
        if kind == "overlap" and step.get("mode") == "processes" and (
                index != len(steps) - 1
                or any(s.get("kind") not in ("dry-run", "crew-plan", "provision") for s in steps[:index])):
            raise FixtureError(f"{where}: a processes overlap must be the last step, after previews and provisioning "
                               "only (each child rebuilds the fixture world)")
        unknown_pre = set(step.get("pre", {})) - PRE_KEYS
        if unknown_pre:
            raise FixtureError(f"{where}: step {step.get('id')} has unknown pre-state keys {sorted(unknown_pre)}")
        for alias in step.get("aliases", ()):
            if alias not in labels["alias"]:
                raise FixtureError(f"{where}: step {step.get('id')} names unknown account {alias!r}")
        if kind == "overlap" and step.get("mode", "threads") == "threads" and not all(
                account.get("world", {}).get("concurrent_sessions") for account in accounts):
            raise FixtureError(f"{where}: a threads overlap needs concurrent_sessions on every account (else the "
                               "second login revokes the first session and thread timing decides the trace)")
        needed = _needed_witnesses(step)
        if not needed <= witnesses:
            raise FixtureError(f"{where}: step {step['id']} needs witnesses {sorted(needed - witnesses)}")
        if kind in RUN_KINDS and "other_mutations" not in expected.get(step["id"], {}):
            raise FixtureError(f"{where}: run step {step['id']} must assert other_mutations")
        if kind == "overlap" and "dispatches" not in expected.get(step["id"], {}):
            raise FixtureError(f"{where}: overlap step {step['id']} must assert dispatches")
        for op in [*step.get("set", ()), *step.get("pre", {}).get("set", ()),
                   *(op for trigger in step.get("triggers", ()) for op in trigger.get("apply", ()))]:
            _validate_op(where, op, labels)
        if step.get("misapply") and step["misapply"].get("course") not in courses:
            raise FixtureError(f"{where}: misapply names unknown course")
    for step_id, entry in expected.items():
        _validate_expected(f"{where}/{step_id}", entry, labels, codes, set(vocabulary["outcomes"]),
                           set(vocabulary["roles"]))


def _needed_witnesses(step: Mapping) -> set:
    sid = step["id"]
    needed = {f"{sid}:fault:{i}" for i in range(len(step.get("faults", ())))}
    needed |= {f"{sid}:trigger:{i}" for i in range(len(step.get("triggers", ())))}
    if step.get("crash"):
        needed.add(f"{sid}:crash")
    if step.get("misapply"):
        needed.add(f"{sid}:misapply")
    pre = step.get("pre", {})
    if pre.get("stop_file"):
        needed.add(f"{sid}:stop_file")
    if pre.get("remove_state") or pre.get("corrupt_state"):
        needed.add(f"{sid}:store_artifacts")
    if pre.get("fail_intent_write"):
        needed.add(f"{sid}:intent_write_failed")
    if pre.get("fail_settle_write"):
        needed.add(f"{sid}:settle_write_failed")
    if step.get("kind") == "overlap":
        needed |= {f"{sid}:barrier", f"{sid}:hold_overlap"}
    return needed


def _validate_op(where: str, op: Mapping, labels: Mapping) -> None:
    if op.get("stop_file") is not None and set(op) != {"stop_file"}:
        raise FixtureError(f"{where}: a stop_file operation takes no other keys: {op}")
    if "status" in op and op["status"] not in ROOM_STATUSES:
        raise FixtureError(f"{where}: operation {op} sets unknown room status {op['status']!r}")
    for key, kind in (("crew", "crew"), ("room", "room"), ("research", "research"), ("course", "course"),
                      ("to_room", "room")):
        if key in op and op[key] not in labels[kind]:
            raise FixtureError(f"{where}: operation {op} names unknown {kind} {op[key]!r}")


def _validate_expected(where: str, entry: Mapping, labels: Mapping, codes: set, outcomes: set, roles: set) -> None:
    unknown = set(entry) - EXPECTED_KEYS
    if unknown:
        raise FixtureError(f"{where}: unknown expected keys {sorted(unknown)}")

    def check(kind: str, value: Any) -> None:
        if value is not None and value not in labels[kind]:
            raise FixtureError(f"{where}: unknown {kind} label {value!r}")

    for rows in entry.get("mutations", {}).values():
        for row in rows:
            check("alias", row.get("alias"))
            check("crew", row.get("crew"))
            check("course", row.get("course"))
            check("room", row.get("room"))
            check("room_design", row.get("design"))
    for alias in [*entry.get("resource_delta", {}), *entry.get("requests", {}), *entry.get("store", {})]:
        check("alias", alias)
    for alias, state in entry.get("store", {}).items():
        if state not in ("present", "absent"):
            raise FixtureError(f"{where}: store.{alias} must be 'present' or 'absent', not {state!r}")
    for crew, timer in entry.get("timer", {}).items():
        check("crew", crew)
        check("course", timer.get("course"))
    rows = [*entry.get("journal", ()), *entry.get("journal_planned", ())]
    for row in list(rows):
        alternatives = row.get("any_of")
        if alternatives is not None:
            if not isinstance(alternatives, list) or len(alternatives) < 2 \
                    or not all(isinstance(alt, dict) and "any_of" not in alt for alt in alternatives):
                raise FixtureError(f"{where}: any_of must list at least two alternative rows")
            rows += alternatives
    for row in rows:
        check("alias", row.get("alias"))
        check("crew", row.get("crew"))
        check("course", row.get("course"))
        for outcome in [row.get("outcome"), *row.get("outcome_in", ())]:
            if outcome is not None and outcome not in outcomes:
                raise FixtureError(f"{where}: unknown outcome {outcome!r}")
    for row in entry.get("crew_plan", ()):
        check("crew", row.get("crew"))
        check("course", row.get("course"))
        if row.get("role") not in roles:
            raise FixtureError(f"{where}: unknown role {row.get('role')!r}")
        if row.get("skip") is not None and row["skip"] not in codes:
            raise FixtureError(f"{where}: unknown skip code {row['skip']!r}")
    for row in entry.get("prestige", ()):
        check("character_design", row.get("target"))
        check("character_design", row.get("ingredient"))
    for row in entry.get("skips", ()):
        check("alias", row.get("alias"))
        check("crew", row.get("crew"))
        if row.get("code") not in codes:
            raise FixtureError(f"{where}: unknown skip code {row.get('code')!r}")
    for dispatch in entry.get("dispatches", ()):
        for row in dispatch:
            check("alias", row.get("alias"))
            for crew in row.get("candidate_one_of", ()):
                check("crew", crew)
            if row.get("code") is not None and row["code"] not in codes:
                raise FixtureError(f"{where}: unknown skip code {row['code']!r}")


def load_suites(which: str = "all") -> dict[str, dict]:
    names = list(FIXTURES) if which == "all" else [which]
    suites = {name: load_suite(FIXTURES[name]) for name in names}
    if len(suites) == 2:
        required, holdout = suites["required"], suites["holdout"]
        if set(required["seeds"]) & set(holdout["seeds"]):
            raise FixtureError("holdout seeds must differ from the required seeds")
    return suites


# --- seeds and worlds -------------------------------------------------------------------------------------------


def seeded_id(base: int, seed: int) -> str:
    """``base`` (below 100000) relabelled for ``seed``: the same digit count for every base, so numeric order,
    string order and length are all preserved and stable tie-breaks on instance ids cannot change with the seed."""
    if not 0 < base < 100000:
        raise FixtureError(f"base id {base} must be between 1 and 99999")
    return str((1 + seed % 9) * 100000 + base)


@dataclass
class IdMap:
    """label <-> seeded id, per label kind (crew and rooms also per alias)."""

    labels: dict[str, dict[str, str]] = field(default_factory=dict)  # kind -> id -> label
    ids: dict[str, dict[str, str]] = field(default_factory=dict)  # kind -> label -> id
    owners: dict[str, str] = field(default_factory=dict)  # "crew:<label>" / "room:<label>" -> alias

    def add(self, kind: str, label: str, value: str) -> None:
        self.labels.setdefault(kind, {})[value] = label
        self.ids.setdefault(kind, {})[label] = value

    def label(self, kind: str, value: Any) -> str:
        """The label of ``value``; ``?<value>`` when it names nothing of that kind (always a mismatch)."""
        text = str(value)
        return self.labels.get(kind, {}).get(text, f"?{text}")

    def id(self, kind: str, label: str) -> str:
        return self.ids[kind][label]


def _catalog_world_parts(catalog: Mapping, scenario: Mapping, seed: int, idmap: IdMap) -> dict:
    duration = datetime.timedelta
    room_designs = []
    for label, row in catalog["room_designs"].items():
        did = seeded_id(row["id"], seed)
        idmap.add("room_design", label, did)
        upgrade = seeded_id(catalog["room_designs"][row["upgrade_from"]]["id"], seed) if row.get("upgrade_from") \
            else "0"
        room_designs.append(fake_state.RoomDesign(did, row["name"], row["type"], upgrade, row.get("price", ""),
                                                  level=row.get("level"), capacity=row.get("capacity")))
    research_designs = []
    for label, row in catalog["research_designs"].items():
        did = seeded_id(row["id"], seed)
        idmap.add("research", label, did)
        root = seeded_id(catalog["research_designs"][row.get("root", label)]["id"], seed)
        research_designs.append(fake_state.ResearchDesign(did, root, row["name"], row.get("gas", 0)))
    character_designs = []
    for label, row in catalog["character_designs"].items():
        did = seeded_id(row["id"], seed)
        idmap.add("character_design", label, did)
        character_designs.append(fake_state.CharacterDesign(did, row["name"], training_capacity=row["capacity"],
                                                            rarity=row.get("rarity", "Common")))
    overrides = scenario.get("course_overrides", {})
    training_designs = []
    for label in scenario["courses"]:
        row = {**catalog["training_designs"][label], **overrides.get(label, {})}
        did = seeded_id(row["id"], seed)
        idmap.add("course", label, did)
        research = row.get("research")
        training_designs.append(fake_state.TrainingDesign(
            did, row["name"], row["stat"], row.get("gain", 2), row.get("fatigue", 10),
            duration(seconds=row["duration_s"]), price=row.get("price"), charge=row.get("charge"),
            rank=row.get("rank"), required_room_type=row.get("room_type"), required_room_level=row.get("room_level"),
            required_research_id=seeded_id(catalog["research_designs"][research]["id"], seed) if research else None))
    return {"room_designs": room_designs, "research_designs": research_designs,
            "character_designs": character_designs, "training_designs": training_designs}


def build_world(catalog: Mapping, scenario: Mapping, account: Mapping, seed: int, epoch: datetime.datetime,
                idmap: IdMap) -> fake_state.World:
    """The account's FakePss world for ``seed``, with every id seeded and recorded in ``idmap``."""
    parts = _catalog_world_parts(catalog, scenario, seed, idmap)
    world_spec = account.get("world", {})
    alias = account["alias"]
    resources = world_spec.get("resources", {})
    world = fake_state.World(
        account=fake_state.Account(user_id=account["user_id"], name=f"Synthetic{alias.title()}",
                                   refresh_token=account["refresh_token"], credits=world_spec.get("credits", 0),
                                   daily_reward_status="1", last_heartbeat=epoch),
        ship=fake_state.Ship(ship_id=str(int(account["user_id"]) + 100000000), name=f"Synthetic{alias.title()}Ship"),
        resources=fake_state.Resources(mineral=resources.get("mineral", 0), gas=resources.get("gas", 0)),
        typed_resource_items=world_spec.get("typed_resource_items", False),
        strict_training_rules=world_spec.get("strict_training_rules", False),
        ship_omits_researches=world_spec.get("ship_omits_researches", False),
        concurrent_sessions=world_spec.get("concurrent_sessions", False),
        **parts)
    for index, room in enumerate(world_spec.get("rooms", ())):
        rid = seeded_id(room["base_id"], seed)
        idmap.add("room", room["label"], rid)
        idmap.owners[f"room:{room['label']}"] = alias
        world.rooms.append(fake_state.Room(rid, idmap.id("room_design", room["design"]), room.get("status", "Normal"),
                                           row=index))
    for research in world_spec.get("researches", ()):
        world.researches.append(fake_state.Research(idmap.id("research", research["design"]),
                                                    research.get("state", "Completed")))
    for crew in world_spec.get("crew", ()):
        cid = seeded_id(crew["base_id"], seed)
        idmap.add("crew", crew["label"], cid)
        idmap.owners[f"crew:{crew['label']}"] = alias
        member = fake_state.Character(
            cid, crew["name"], idmap.id("character_design", crew["design"]),
            idmap.id("room", crew["room"]) if crew.get("room") else "0", level=crew.get("level", 20),
            fatigue=crew.get("fatigue", 0), stats=fake_state.stats(**crew.get("stats", {})),
            omit_stats=tuple(f"{stat}Improvement" for stat in crew.get("omit_stats", ())),
            hidden=bool(crew.get("hidden", False)))
        training = crew.get("training")
        if training:
            course = world.training_design(idmap.id("course", training["course"]))
            end = epoch + datetime.timedelta(seconds=training["end_offset_s"])
            member.session = fake_state.TrainingSession(course.id, end - course.duration, end,
                                                        applied=training.get("applied", False))
        world.characters.append(member)
    if world_spec.get("prestige"):
        for recipe in catalog.get("prestige", ()):
            first, second = (idmap.id("character_design", label) for label in recipe["from"])
            world.prestige_recipes.append((first, second, idmap.id("character_design", recipe["to"])))
    world.validate()
    return world


def apply_ops(world: fake_state.World, ops: Sequence[Mapping], idmap: IdMap, catalog: Mapping) -> None:
    """World edits named by labels: crew stats, room and training; room status; research removal; course prices."""
    for op in ops:
        if "crew" in op:
            crew = world.character(idmap.id("crew", op["crew"]))
            if "stats" in op:
                crew.stats = fake_state.stats(**op["stats"])
            if "to_room" in op:
                crew.room_id = idmap.id("room", op["to_room"])
            if "fatigue" in op:
                crew.fatigue = op["fatigue"]
            if "hidden" in op:  # left out of (or back in) the roster read
                crew.hidden = bool(op["hidden"])
        elif "room" in op:
            room = world.room(idmap.id("room", op["room"]))
            room.status = op.get("status", room.status)
        elif "research" in op:
            design_id = idmap.id("research", op["research"])
            if op.get("remove"):
                world.researches = [r for r in world.researches if r.design_id != design_id]
        elif "course" in op:
            course = world.training_design(idmap.id("course", op["course"]))
            if "price" in op:
                course.price = op["price"]
            if "charge" in op:
                course.charge = op["charge"]
        else:
            raise FixtureError(f"operation {op} names nothing to change")


# --- inputs ---------------------------------------------------------------------------------------------------


def _utc(moment: datetime.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def registry_payload(accounts: Sequence[Mapping], policy_override: str | None = None,
                     variant: Mapping | None = None, only: Sequence[str] | None = None) -> dict:
    from sdk.state.snapshot import game_identity_fingerprint

    entries = []
    for account in accounts:
        if only is not None and account["alias"] not in only:
            continue
        spec = {**account.get("registry", {}), **(variant or {}).get(account["alias"], {})}
        entry = {"alias": account["alias"], "objective": "grow",
                 "policy_id": policy_override or spec.get("policy_id", "training-v2"),
                 "credentials_secret": account["secret"],
                 "expected_game_identity": game_identity_fingerprint(account["user_id"], SALT)}
        for key in ("enabled", "max_actions", "budgets", "legacy_run_account"):
            if spec.get(key) is not None:
                entry[key] = spec[key]
        entries.append(entry)
    return {"version": 1, "accounts": entries}


def authorization_payload(accounts: Sequence[Mapping], now: datetime.datetime) -> dict:
    """The owner authorization (SPEC-05) for every account that declares one; valid for real now."""
    authorized = [account for account in accounts if account.get("authorization") is not None]
    actions = sorted({action for account in authorized for action in account["authorization"].get("actions", ())})
    payload = {"version": 1, "owner": "owner", "purpose": "SPEC-14 workflow benchmark (synthetic accounts)",
               "accounts": [account["alias"] for account in authorized], "actions": actions,
               "not_before": _utc(now - datetime.timedelta(hours=1)), "expires": _utc(now + datetime.timedelta(days=7)),
               "caps": {account["alias"]: account["authorization"].get("caps", {"minerals": 1000, "gas": 1000})
                        for account in authorized},
               "modes": ["full"]}
    for key in ("legacy_run_skipped", "legacy_run_serialized"):
        values = sorted({n for account in authorized for n in account["authorization"].get(key, ())})
        if values:
            payload[key] = values
    return payload


def credentials_env(accounts: Sequence[Mapping]) -> dict[str, str]:
    """Synthetic device-login credentials per account, and the synthetic identity salt."""
    keys = (SYNTHETIC_DEVICE_KEY_IOS, SYNTHETIC_DEVICE_KEY_MAC)
    env = {account["secret"]: f"fleet|{keys[index % 2]}|{account['refresh_token']}|en"
           for index, account in enumerate(accounts)}
    env[SALT_ENV] = SALT
    return env


# --- running production -------------------------------------------------------------------------------------------

_PREIMPORTED = False


def preimport_sdk() -> None:
    """Import every sdk module before any FakePss.install(), so each one's clock globals are swapped (a module first
    imported inside install() would read the real clock, a fake violation)."""
    global _PREIMPORTED
    if _PREIMPORTED:
        return
    import sdk

    for module in pkgutil.walk_packages(sdk.__path__, "sdk."):
        importlib.import_module(module.name)
    _PREIMPORTED = True


def _run_module():
    import run  # imported after the first chdir, so run.py's log file opens in a scratch directory

    return run


def _forbidden(name: str) -> Callable:
    def refuse(*args, **kwargs):
        raise AssertionError(f"{name} must not be called by the workflow benchmark")

    return refuse


def real_now() -> datetime.datetime:
    """Naive UTC, truncated to the second: where each step puts the fake clock (never before where it was)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


@contextlib.contextmanager
def production_patches(env: Mapping[str, str]):
    """The process-wide patches every production call runs under: environment, logging, no e-mail or prompts."""
    run = _run_module()
    capture = logging.StreamHandler(io.StringIO())
    with patch.dict(os.environ, dict(env), clear=True), patch.object(logging.getLogger(), "handlers", [capture]), \
            patch("getpass.getpass", side_effect=_forbidden("getpass.getpass")), \
            patch.object(run, "send_email", side_effect=_forbidden("run.send_email")), \
            patch.object(run, "email_logfile", side_effect=_forbidden("run.email_logfile")), \
            patch.object(run, "Device", side_effect=_forbidden("run.Device")):
        yield


def call_main(argv: Sequence[str], env: Mapping[str, str], driver) -> tuple[Any, str, str]:
    """Run the real ``run.main()`` with ``argv`` against ``driver`` (a FakePss or FakePssFleet).

    Returns (exit code, "crashed" or "error <ExceptionType>", stdout, stderr). Any exception production lets escape
    is recorded as the step's exit and the comparison goes on: one failing step never aborts the report.
    """
    run = _run_module()
    out, err = io.StringIO(), io.StringIO()
    code: Any = 0
    with driver.install(), production_patches(env), patch.object(sys, "argv", ["run.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = run.main()
            code = 0 if result is None else result
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        except SimulatedCrash:
            code = "crashed"
        except Exception as error:  # noqa: BLE001 - a production exception is the step's recorded outcome
            code = f"error {type(error).__name__}"
            err.write(f"production raised {type(error).__name__}\n")
    return code, out.getvalue(), err.getvalue()


# --- scenario execution --------------------------------------------------------------------------------------------


@dataclass
class ScenarioRun:
    suite: str
    case_id: str
    scenario: Mapping
    catalog: Mapping
    seed: int
    policy_override: str | None
    directory: Path
    idmap: IdMap = field(default_factory=IdMap)
    fakes: dict[str, FakePss] = field(default_factory=dict)
    driver: Any = None
    env: dict = field(default_factory=dict)
    witnesses: dict[str, str | None] = field(default_factory=dict)  # name -> None (fired) or why missing
    fake_errors: list[str] = field(default_factory=list)
    epoch: datetime.datetime = fake_state.FAKE_EPOCH  # when the worlds were built: their session times count from it

    @property
    def accounts(self) -> list[Mapping]:
        return list(self.scenario["accounts"])

    @property
    def journals(self) -> Path:
        return self.directory / JOURNAL_DIR

    def caps(self, alias: str) -> dict:
        for account in self.accounts:
            if account["alias"] == alias and account.get("authorization") is not None:
                return dict(account["authorization"].get("caps", {"minerals": 1000, "gas": 1000}))
        return {"minerals": 0, "gas": 0}


def start_scenario(suite: str, case_id: str, scenario: Mapping, catalog: Mapping, seed: int,
                   policy_override: str | None, directory: Path) -> ScenarioRun:
    epoch = real_now()
    ctx = ScenarioRun(suite, case_id, scenario, catalog, seed, policy_override, directory, epoch=epoch)
    for index, account in enumerate(ctx.accounts):
        world = build_world(catalog, scenario, account, seed, epoch, ctx.idmap)
        ctx.fakes[account["alias"]] = FakePss(world, seed=seed, epoch=epoch, token_tag=f"{index + 1:012x}",
                                              permute_seed=seed)
    ctx.driver = FakePssFleet(ctx.fakes) if len(ctx.fakes) > 1 else next(iter(ctx.fakes.values()))
    ctx.env = credentials_env(ctx.accounts)
    ctx.journals.mkdir(parents=True, exist_ok=True)
    _write_json(directory / REGISTRY_FILE, registry_payload(ctx.accounts, policy_override))
    _write_json(directory / AUTHORIZATION_FILE, authorization_payload(ctx.accounts,
                                                                     datetime.datetime.now(datetime.timezone.utc)))
    return ctx


def _write_json(path: Path, payload: Mapping) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _primary(ctx: ScenarioRun) -> FakePss:
    return next(iter(ctx.fakes.values()))


def _fake_for(ctx: ScenarioRun, spec: Mapping) -> FakePss:
    alias = spec.get("alias")
    return ctx.fakes[alias] if alias else _primary(ctx)


def store_artifacts(journals: Path, aliases: Sequence[str]) -> list[Path]:
    """The attempt (intent) store of ``aliases`` where INTERFACE ASSUMPTIONS item 2 puts it: <journals>/STORE_DIR/
    <alias> (a file, or a directory and everything in it) and <alias>.<suffix> files; never a ``*.lock`` file."""
    root = journals / STORE_DIR
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.iterdir()):
        if path.suffix == ".lock" or not any(path.name == alias or path.name.startswith(f"{alias}.")
                                             for alias in aliases):
            continue
        found.append(path)
        if path.is_dir():
            found += [inner for inner in sorted(path.rglob("*")) if inner.suffix != ".lock"]
    return found


def _set_clock(ctx: ScenarioRun) -> None:
    """Move the fake clock up to real now (never back, never ahead of real now): see the module docstring."""
    clock = _primary(ctx).clock
    clock.now = max(clock.now, real_now())


def _step_aliases(ctx: ScenarioRun, step: Mapping) -> list[str]:
    return list(step.get("aliases") or [account["alias"] for account in ctx.accounts])


def _prepare_store(ctx: ScenarioRun, step: Mapping, sid: str) -> None:
    """Apply the step's stop-file and store pre-state (remove or corrupt the named accounts' attempt store)."""
    pre = step.get("pre", {})
    if pre.get("stop_file"):
        (ctx.journals / "STOP").write_text("stop\n", encoding="utf-8")
        ctx.witnesses[f"{sid}:stop_file"] = None
    if pre.get("remove_state") or pre.get("corrupt_state"):
        aliases = _step_aliases(ctx, step)
        artifacts = store_artifacts(ctx.journals, aliases)
        overwritten = 0
        for path in reversed(artifacts):
            if pre.get("remove_state"):
                shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
            elif pre.get("corrupt_state") and path.is_file():
                path.write_bytes(b"\x00not-a-store\xff\n")
                overwritten += 1
        if not artifacts:
            why = f"no attempt store under <journal dir>/{STORE_DIR}/ for {aliases} (INTERFACE ASSUMPTIONS item 2)"
        elif pre.get("corrupt_state") and not overwritten:
            why = (f"the attempt store of {aliases} holds no file to corrupt (INTERFACE ASSUMPTIONS item 2: a "
                   "provisioned store holds at least one file)")
        else:
            why = None
        ctx.witnesses[f"{sid}:store_artifacts"] = why


def _fail_hook(ctx: ScenarioRun, name: str, hook: tuple[str, str], what: str, purpose: str,
               armed: Callable[[], bool] = lambda: True, when: str = "") -> Callable[[], None]:
    """Make the store hook raise OSError for the step whenever ``armed()`` holds (calling through otherwise);
    returns the check that restores it and records witness ``name`` (missing with its cause when the hook does not
    exist or never failed)."""
    module_name, attribute = hook
    where = f"{module_name}.{attribute}"
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        module = None
    original = getattr(module, attribute, None) if module is not None else None
    if not callable(original):
        ctx.witnesses[name] = (f"{what} hook {where} does not exist (INTERFACE ASSUMPTIONS item 3: the attempt store "
                               f"must {purpose} through it)")
        return lambda: None
    calls = {"count": 0}

    def failing(*args, **kwargs):
        if not armed():
            return original(*args, **kwargs)
        calls["count"] += 1
        raise OSError(f"workflow benchmark: injected {what} failure ({where})")

    patcher = patch.object(module, attribute, failing)
    patcher.start()

    def check() -> None:
        patcher.stop()
        _witness(ctx, name, calls["count"] > 0, f"{what} hook {where} was never called{when}")

    return check


def _fail_intent_writes(ctx: ScenarioRun, sid: str) -> Callable[[], None]:
    """Every intent write of the step fails (INTERFACE ASSUMPTIONS item 3)."""
    return _fail_hook(ctx, f"{sid}:intent_write_failed", INTENT_WRITE_HOOK, "intent-write", "write intents")


def _fail_settle_writes(ctx: ScenarioRun, sid: str) -> Callable[[], None]:
    """Every settle write of the step fails once a fake has applied an AddTraining in it (INTERFACE ASSUMPTIONS
    item 3): the request is sent and applied, and only the post-send durable write fails."""
    marks = {alias: len(fake.ledger) for alias, fake in ctx.fakes.items()}

    def applied_start() -> bool:
        return any(entry.applied and f"{entry.method} {entry.path}" == ADD_TRAINING
                   for alias, fake in ctx.fakes.items() for entry in fake.ledger[marks[alias]:])

    return _fail_hook(ctx, f"{sid}:settle_write_failed", SETTLE_WRITE_HOOK, "settle-write", "settle attempts",
                      armed=applied_start, when=" after an applied start")


def _arm(ctx: ScenarioRun, step: Mapping, sid: str) -> list[Callable[[], None]]:
    """Arm the step's faults, triggers, crash and misapply; returns witness checks to run after the step."""
    checks: list[Callable[[], None]] = []
    for index, spec in enumerate(step.get("faults", ())):
        fake = _fake_for(ctx, spec)
        method, _, path = spec["route"].partition(" ")
        holder: dict[str, Any] = {}

        def arm(fake=fake, spec=spec, method=method, path=path, holder=holder) -> None:
            holder["fault"] = fake.fail(path, method=method, status=spec.get("status", 404),
                                        times=spec.get("times", 1), apply=spec.get("apply", False))

        if spec.get("after_route"):
            armer = fake.on_reply(spec["after_route"], lambda f, e, arm=arm: arm(), nth=1, times=1)
            checks.append(lambda armer=armer: setattr(armer, "times", armer.hits))  # never arms a later step
        else:
            arm()
        checks.append(lambda name=f"{sid}:fault:{index}", holder=holder: _witness(
            ctx, name, holder.get("fault") is not None and holder["fault"].hits > 0, "fault never fired"))
        checks.append(lambda holder=holder: _disarm_fault(holder.get("fault")))
    for index, spec in enumerate(step.get("triggers", ())):
        fake = _fake_for(ctx, spec)
        ops = spec.get("apply", ())
        alias = spec.get("alias") or ctx.accounts[0]["alias"]

        def fire(f, e, ops=ops) -> None:
            for op in ops:
                if op.get("stop_file"):  # the operator's stop control, created while the run is under way
                    (ctx.journals / "STOP").write_text("stop\n", encoding="utf-8")
                else:
                    apply_ops(f.state, [op], ctx.idmap, ctx.catalog)

        hook = fake.on_reply(spec["route"], fire, nth=spec.get("after_nth", 1), times=1)
        checks.append(lambda name=f"{sid}:trigger:{index}", hook=hook, alias=alias: _witness(
            ctx, name, hook.hits > 0, f"trigger on {alias} never fired"))
        checks.append(lambda hook=hook: setattr(hook, "times", hook.hits))
    if step.get("crash"):
        fake = _fake_for(ctx, step["crash"])
        crash = fake.crash(step["crash"]["route"], apply=step["crash"].get("apply", False))
        checks.append(lambda crash=crash: _witness(ctx, f"{sid}:crash", crash.hits > 0, "crash never fired"))
        checks.append(lambda crash=crash, fake=fake: fake.crashes.remove(crash))
    if step.get("misapply"):
        fake = _fake_for(ctx, step["misapply"])
        misapply = fake.misapply_training(ctx.idmap.id("course", step["misapply"]["course"]))
        checks.append(lambda m=misapply: _witness(ctx, f"{sid}:misapply", m.hits > 0, "misapply never fired"))
        checks.append(lambda m=misapply: setattr(m, "times", m.hits))
    if step.get("pre", {}).get("fail_intent_write"):
        checks.append(_fail_intent_writes(ctx, sid))
    if step.get("pre", {}).get("fail_settle_write"):
        checks.append(_fail_settle_writes(ctx, sid))
    return checks


def _disarm_fault(fault) -> None:
    if fault is not None:
        fault.times = fault.hits


def _witness(ctx: ScenarioRun, name: str, fired: bool, why: str) -> None:
    ctx.witnesses[name] = None if fired else why


@dataclass
class Before:
    ledger: dict[str, int]
    exchanges: dict[str, int]
    violations: dict[str, int]
    wallets: dict[str, tuple[int, int, int]]
    sessions: dict[str, dict[str, tuple]]
    journal: dict[str, int]
    runs: set


def _sessions(world: fake_state.World) -> dict[str, tuple]:
    return {c.id: (c.session.design_id, fake_state.fmt(c.session.start), fake_state.fmt(c.session.end))
            for c in world.characters if c.session is not None}


def _journal_events(ctx: ScenarioRun, alias: str) -> list | None:
    """The account's journal events; None when the journal exists but cannot be read (never an empty journal)."""
    from sdk.journal import JournalError, read_events

    try:
        return read_events(ctx.journals / f"{alias}.log")
    except (JournalError, OSError):
        return None


def _before(ctx: ScenarioRun) -> Before:
    runs_dir = ctx.journals / "runs"
    return Before(
        ledger={a: len(f.ledger) for a, f in ctx.fakes.items()},
        exchanges={a: len(f.exchanges) for a, f in ctx.fakes.items()},
        violations={a: len(f.violations) for a, f in ctx.fakes.items()},
        wallets={a: f._wallet() for a, f in ctx.fakes.items()},
        sessions={a: _sessions(f.state) for a, f in ctx.fakes.items()},
        journal={a["alias"]: len(_journal_events(ctx, a["alias"]) or ()) for a in ctx.accounts},
        runs=set(runs_dir.glob("*.json")) if runs_dir.is_dir() else set())


def _collect(ctx: ScenarioRun, before: Before, code: Any, stdout: str, stderr: str) -> dict:
    """The raw step record: everything the oracle reads, before normalization."""
    accounts = {}
    for alias, fake in ctx.fakes.items():
        wallet = fake._wallet()
        old_wallet = before.wallets[alias]
        old_sessions = before.sessions[alias]
        started = {cid: session for cid, session in _sessions(fake.state).items()
                   if old_sessions.get(cid) != session}
        first = before.exchanges[alias]
        events = _journal_events(ctx, alias)
        accounts[alias] = {
            "ledger": [entry.to_dict() for entry in fake.ledger[before.ledger[alias]:]],
            "earlier_ledger": [entry.to_dict() for entry in fake.ledger[:before.ledger[alias]]],
            "exchanges": [_exchange_dict(first + offset, exchange)
                          for offset, exchange in enumerate(fake.exchanges[first:])],
            "requests": len(fake.exchanges) - first,
            "violations": fake.violations[before.violations[alias]:],
            "delta": {kind: wallet[i] - old_wallet[i] for i, kind in enumerate(("mineral", "gas", "starbux"))},
            "started": started,
            "journal": [_event_dict(event) for event in (events or [])[before.journal.get(alias, 0):]],
            "journal_unreadable": events is None,
            "store": bool(store_artifacts(ctx.journals, [alias])),
        }
    runs_dir = ctx.journals / "runs"
    records = []
    for path in sorted((set(runs_dir.glob("*.json")) if runs_dir.is_dir() else set()) - before.runs):
        with contextlib.suppress(OSError, json.JSONDecodeError):
            records.append(json.loads(path.read_text(encoding="utf-8")))
    return {"exit": code, "stdout": stdout, "stderr": stderr, "accounts": accounts,
            "records": [row for record in records for row in record_texts(record)]}


def _exchange_dict(index: int, exchange) -> dict:
    return {"index": index, "method": exchange.method, "path": exchange.path, "status": exchange.status,
            "error": exchange.error, "thread": exchange.thread, "roster": exchange.roster}


def record_texts(record: Mapping) -> list[tuple[str, str]]:
    """(alias, text) for every field of a run record's account entries a skip code may be read from."""
    rows = []
    for entry in record.get("accounts", ()) if isinstance(record, Mapping) else ():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("alias"), str):
            continue
        alias = entry["alias"]
        rows += [(alias, f"{key}: {entry[key]}") for key in ("status", "reason") if entry.get(key)]
        rows += [(alias, f"{item.get('candidate')}: {item.get('reason')}") for item in entry.get("rejected", ())
                 if isinstance(item, Mapping)]
        rows += [(alias, f"{item.get('action_id')}: {item.get('reason')}") for item in entry.get("results", ())
                 if isinstance(item, Mapping)]
    return rows


def _event_dict(event) -> dict:
    return {"alias": event.account_alias, "action": event.action_id, "outcome": event.outcome,
            "attempted": event.attempted, "dry_run": event.dry_run, "reason": event.reason,
            "params": dict(event.params), "cost": dict(event.cost), "evidence": dict(event.evidence or {})}


def _argv(ctx: ScenarioRun, step: Mapping, registry: str, alias: str | None = None) -> list[str]:
    """The step kind's ARGV_TEMPLATES entry (INTERFACE ASSUMPTIONS item 1), filled in."""
    values = {"registry": registry, "authorization": AUTHORIZATION_FILE, "journal_dir": JOURNAL_DIR,
              "salt_env": SALT_ENV, "alias": alias or ""}
    argv = [part.format(**values) for part in ARGV_TEMPLATES[step["kind"]]]
    if step["kind"] == "execute" and step.get("concurrent"):
        argv += ["--concurrent", str(step["concurrent"])]
    return argv


def _registry_for(ctx: ScenarioRun, step: Mapping) -> str:
    if not step.get("registry_variant"):
        return REGISTRY_FILE
    name = f"registry-{step['id']}.json"
    _write_json(ctx.directory / name, registry_payload(ctx.accounts, ctx.policy_override, step["registry_variant"]))
    return name


def _ops_for(ctx: ScenarioRun, alias: str, ops: Sequence[Mapping]) -> list[Mapping]:
    """The operations that edit ``alias``'s world: its own crew and rooms; course and research edits everywhere
    (or where an op's ``alias`` says)."""
    keep = []
    for op in ops:
        if op.get("alias") not in (None, alias):
            continue
        if any(key in op and ctx.idmap.owners.get(f"{kind}:{op[key]}") != alias
               for key, kind in (("crew", "crew"), ("room", "room"))):
            continue
        keep.append({key: value for key, value in op.items() if key != "alias"})
    return keep


def run_step(ctx: ScenarioRun, step: Mapping) -> dict | None:
    """Run one step; returns its raw record (None for a mutate step)."""
    sid = step["id"]
    if step["kind"] == "mutate":
        for alias, fake in ctx.fakes.items():
            apply_ops(fake.state, _ops_for(ctx, alias, step.get("set", ())), ctx.idmap, ctx.catalog)
        return None
    pre = step.get("pre", {})
    _set_clock(ctx)
    for alias, fake in ctx.fakes.items():
        apply_ops(fake.state, _ops_for(ctx, alias, pre.get("set", ())), ctx.idmap, ctx.catalog)
    _prepare_store(ctx, step, sid)
    if step["kind"] == "overlap":
        return run_overlap(ctx, step)
    checks = _arm(ctx, step, sid)
    before = _before(ctx)
    try:
        registry = _registry_for(ctx, step)
        if step["kind"] in ACCOUNT_KINDS:  # one run per account; the first nonzero exit is the step's
            runs = [call_main(_argv(ctx, step, registry, alias), ctx.env, ctx.driver)
                    for alias in _step_aliases(ctx, step)]
            code = next((run[0] for run in runs if run[0] != 0), 0)
            stdout, stderr = "".join(run[1] for run in runs), "".join(run[2] for run in runs)
        else:
            code, stdout, stderr = call_main(_argv(ctx, step, registry), ctx.env, ctx.driver)
    finally:
        for check in checks:
            check()
    return _collect(ctx, before, code, stdout, stderr)


# --- overlap (threads and processes) ------------------------------------------------------------------------------


def run_overlap(ctx: ScenarioRun, step: Mapping) -> dict:
    if step.get("mode", "threads") == "processes":
        return run_overlap_processes(ctx, step)
    return run_overlap_threads(ctx, step)


def run_overlap_threads(ctx: ScenarioRun, step: Mapping) -> dict:
    """Several execute dispatches as threads against the shared fake world, overlapped by a real barrier."""
    run = _run_module()
    sid, count = step["id"], int(step.get("dispatches", 2))
    barrier_spec, hold_spec = step["barrier"], step["hold"]
    barrier = threading.Barrier(barrier_spec.get("parties", count),
                                timeout=barrier_spec.get("timeout_s", OVERLAP_TIMEOUT_S))
    done = [threading.Event() for _ in range(count)]
    state = {"barrier_broken": False, "hold": None}

    def wait_barrier(fake, exchange) -> None:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            state["barrier_broken"] = True

    def hold(fake, exchange) -> None:
        me = _dispatch_index(threading.current_thread().name)
        others = [event for index, event in enumerate(done) if index != me]
        deadline = time.monotonic() + hold_spec.get("timeout_s", OVERLAP_TIMEOUT_S)
        released = all(event.wait(max(0.0, deadline - time.monotonic())) for event in others)
        state["hold"] = "event" if released else "timeout"

    barrier_hook = _fake_for(ctx, barrier_spec).on_reply(barrier_spec["route"], wait_barrier, nth=1,
                                                         times=barrier_spec.get("parties", count), blocking=True)
    hold_hook = _fake_for(ctx, hold_spec).on_reply(hold_spec["route"], hold, nth=1, times=1, blocking=True)
    outputs = [io.StringIO() for _ in range(count)]
    codes: list[Any] = [None] * count
    before = _before(ctx)
    entry = getattr(run, THREADS_ENTRY)  # INTERFACE ASSUMPTIONS item 4

    def dispatch(index: int) -> None:
        try:
            codes[index] = entry(REGISTRY_FILE, AUTHORIZATION_FILE, mode="full", journal_dir=Path(JOURNAL_DIR),
                                 identity_salt_env=SALT_ENV, out=outputs[index], environ=dict(ctx.env))
        except BaseException as error:  # noqa: BLE001 - a dispatch failure is a recorded outcome, never a hang
            codes[index] = f"error {type(error).__name__}"
        finally:
            done[index].set()

    stdout, stderr = ThreadStreams(), ThreadStreams()  # each dispatch's printed text kept apart
    with ctx.driver.install(), production_patches(ctx.env), contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        threads = [threading.Thread(target=dispatch, args=(index,), name=f"dispatch-{index}", daemon=True)
                   for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(DISPATCH_TIMEOUT)
    barrier_hook.times = barrier_hook.hits
    hold_hook.times = hold_hook.hits
    if any(thread.is_alive() for thread in threads):
        raise OracleError(f"{sid}: a dispatch thread did not finish within {DISPATCH_TIMEOUT:g} s")
    parties = barrier_spec.get("parties", count)
    _witness(ctx, f"{sid}:barrier", barrier_hook.hits >= parties and not state["barrier_broken"],
             f"barrier crossed by {barrier_hook.hits} of {parties} dispatches" if not state["barrier_broken"]
             else "barrier broken by timeout")
    _witness(ctx, f"{sid}:hold_overlap", state["hold"] == "event",
             "hold never reached" if state["hold"] is None else "hold released by timeout, not by the other dispatch")
    names = [f"dispatch-{index}" for index in range(count)]
    texts = [outputs[index].getvalue() + stdout.text(name) + "\n" + stderr.text(name)
             for index, name in enumerate(names)]
    raw = _collect(ctx, before, codes, "\n".join(texts), "".join(stderr.text(name) for name in ["MainThread", *names]))
    raw["dispatch_outputs"] = texts
    raw["dispatch_codes"] = codes
    return raw


class ThreadStreams(io.TextIOBase):
    """A sys.stdout / sys.stderr stand-in that keeps each thread's text apart (threads-mode overlap)."""

    def __init__(self) -> None:
        super().__init__()
        self._texts: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._texts.setdefault(threading.current_thread().name, []).append(text)
        return len(text)

    def text(self, name: str) -> str:
        with self._lock:
            return "".join(self._texts.get(name, ()))


def _dispatch_index(name: str) -> int:
    match = re.fullmatch(r"dispatch-(\d+)", name)
    return int(match.group(1)) if match else -1


def run_overlap_processes(ctx: ScenarioRun, step: Mapping) -> dict:
    """Several execute dispatches as child processes (``--dispatch-child``) through ``run.main``.

    Each child rebuilds the fixture world from the same scenario, catalog, seed and build time (only previews ran
    before, so the world is unchanged) and serves its own copy; the children share the journal directory and
    synchronise through a file barrier (at the barrier route's reply) and a file hold (at the hold route's first
    reply). Everything is handed over as JSON.
    """
    sid, count = step["id"], int(step.get("dispatches", 2))
    sync = ctx.directory / f"sync-{sid}"
    sync.mkdir()
    before = _before(ctx)
    children = []
    for index in range(count):
        spec = {"index": index, "count": count, "cwd": str(ctx.directory), "scenario": ctx.scenario,
                "catalog": ctx.catalog, "seed": ctx.seed, "world_epoch": ctx.epoch.isoformat(),
                "clock": _primary(ctx).clock.now.isoformat(),
                "argv": _argv(ctx, {"kind": "execute"}, REGISTRY_FILE), "env": ctx.env,
                "barrier": step["barrier"], "hold": step["hold"], "sync": str(sync),
                "out": str(sync / f"result-{index}.json"), **dispatch_defect_field()}
        path = sync / f"spec-{index}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        children.append(subprocess.Popen([sys.executable, os.path.abspath(__file__), "--dispatch-child", str(path)],
                                         cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace"))
    results = []
    for index, child in enumerate(children):
        try:
            _, errors = child.communicate(timeout=DISPATCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise OracleError(f"{sid}: dispatch child {index} timed out") from None
        result_path = sync / f"result-{index}.json"
        if child.returncode != 0 or not result_path.exists():
            tail = (errors or "").strip().splitlines()[-1:] or [""]
            raise OracleError(f"{sid}: dispatch child {index} failed (exit {child.returncode}): {tail[0]}")
        results.append(json.loads(result_path.read_text(encoding="utf-8")))
    return merge_process_results(ctx, step, before, results)


def merge_process_results(ctx: ScenarioRun, step: Mapping, before: Before, results: Sequence[Mapping]) -> dict:
    """The processes overlap's raw record from the children's result JSON (``dispatch_child``): the barrier and hold
    witnesses, each account's ledger and exchanges (indexes offset per child, so they never interleave), the summed
    requests and delta, and each child's output and exit code."""
    sid, count = step["id"], int(step.get("dispatches", 2))
    parties = step["barrier"].get("parties", count)
    arrived = sum(result["barrier"]["hits"] for result in results)
    broken = any(result["barrier"]["broken"] for result in results)
    _witness(ctx, f"{sid}:barrier", arrived >= parties and not broken,
             "file barrier broken by timeout" if broken else f"file barrier reached by {arrived} of {parties}")
    holds = [result["hold"] for result in results if result["hold"] is not None]
    _witness(ctx, f"{sid}:hold_overlap", holds == ["event"],
             "file hold never reached" if not holds else f"file hold released by {holds}")
    raw = _collect(ctx, before, [result["exit"] for result in results],
                   "\n".join(result["stdout"] for result in results), "\n".join(r["stderr"] for r in results))
    for alias in ctx.fakes:  # each child changed its own world copy: the ledgers are the evidence
        entries, exchanges = [], []
        for index, result in enumerate(results):  # exchange indexes are per child: offset them apart
            offset = (index + 1) * PROCESS_EXCHANGE_OFFSET
            for entry in result["ledger"].get(alias, ()):
                entries.append({**entry, "thread": f"dispatch-{index}", "exchange": entry["exchange"] + offset})
            for exchange in result["exchanges"].get(alias, ()):
                exchanges.append({**exchange, "thread": f"dispatch-{index}", "index": exchange["index"] + offset})
        raw["accounts"][alias]["ledger"] = entries
        raw["accounts"][alias]["exchanges"] = exchanges
        raw["accounts"][alias]["requests"] = sum(result["requests"].get(alias, 0) for result in results)
        raw["accounts"][alias]["violations"] = [v for result in results for v in result["violations"].get(alias, ())]
        raw["accounts"][alias]["delta"] = {kind: sum(entry["delta"][kind] for entry in entries)
                                           for kind in ("mineral", "gas", "starbux")}
        raw["accounts"][alias]["started"] = {}
    raw["dispatch_outputs"] = [result["stdout"] + "\n" + result["stderr"] for result in results]
    raw["dispatch_codes"] = [result["exit"] for result in results]
    return raw


def wait_files(sync: Path, names: Sequence[str], timeout: float) -> bool:
    """Whether every named file exists in ``sync`` within ``timeout`` seconds (polled)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all((sync / name).exists() for name in names):
            return True
        time.sleep(0.01)
    return all((sync / name).exists() for name in names)


def file_barrier(sync: Path, index: int, parties: int, timeout: float, status: dict) -> None:
    """Dispatch ``index`` arrives at the processes-mode file barrier and waits for every party's arrive file; the
    barrier is broken (``status["broken"]``) when they are not all there within ``timeout`` seconds."""
    status["hits"] += 1
    (sync / f"arrive-{index}").write_text("1", encoding="utf-8")
    names = [f"arrive-{i}" for i in range(parties)]
    if not wait_files(sync, names, timeout):
        status["broken"] = True


def dispatch_child(spec_path: str) -> int:
    """One processes-mode dispatch: ``run.main`` against its own copy of the world; writes its result JSON."""
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    sync = Path(spec["sync"])
    os.chdir(spec["cwd"])
    preimport_sdk()
    world_epoch = datetime.datetime.fromisoformat(spec["world_epoch"])
    clock = datetime.datetime.fromisoformat(spec["clock"])
    fakes = {}
    for index, account in enumerate(spec["scenario"]["accounts"]):
        world = build_world(spec["catalog"], spec["scenario"], account, spec["seed"], world_epoch, IdMap())
        fakes[account["alias"]] = FakePss(world, seed=spec["seed"], epoch=clock, token_tag=f"{index + 1:012x}",
                                          permute_seed=spec["seed"])
    driver = FakePssFleet(fakes) if len(fakes) > 1 else next(iter(fakes.values()))
    index, count = spec["index"], spec["count"]
    status = {"barrier": {"hits": 0, "broken": False}, "hold": None}

    def barrier(fake, exchange) -> None:
        file_barrier(sync, index, spec["barrier"].get("parties", count),
                     spec["barrier"].get("timeout_s", OVERLAP_TIMEOUT_S), status["barrier"])

    def hold(fake, exchange) -> None:
        names = [f"done-{i}" for i in range(count) if i != index]
        timeout = spec["hold"].get("timeout_s", OVERLAP_TIMEOUT_S)
        status["hold"] = "event" if wait_files(sync, names, timeout) else "timeout"

    barrier_fake = fakes.get(spec["barrier"].get("alias")) or next(iter(fakes.values()))
    hold_fake = fakes.get(spec["hold"].get("alias")) or next(iter(fakes.values()))
    barrier_fake.on_reply(spec["barrier"]["route"], barrier, nth=1, times=1, blocking=True)
    hold_fake.on_reply(spec["hold"]["route"], hold, nth=1, times=1, blocking=True)
    defect_id = spec.get("defect")  # a seeded defect (--defects) reaches a dispatch child only through its spec
    if defect_id is not None and defect_id not in {item.id for item in SEEDED_DEFECTS}:
        raise OracleError(f"dispatch child {index}: unknown seeded defect {defect_id!r}")
    try:
        with defect(defect_id).active() if defect_id else contextlib.nullcontext():
            code, stdout, stderr = call_main(spec["argv"], spec["env"], driver)
    finally:
        (sync / f"done-{index}").write_text("1", encoding="utf-8")
    result = {"exit": code, "stdout": stdout, "stderr": stderr, "barrier": status["barrier"], "hold": status["hold"],
              "ledger": {alias: [entry.to_dict() for entry in fake.ledger] for alias, fake in fakes.items()},
              "exchanges": {alias: [_exchange_dict(i, exchange) for i, exchange in enumerate(fake.exchanges)]
                            for alias, fake in fakes.items()},
              "requests": {alias: len(fake.exchanges) for alias, fake in fakes.items()},
              "violations": {alias: list(fake.violations) for alias, fake in fakes.items()}}
    Path(spec["out"]).write_text(json.dumps(result), encoding="utf-8")
    return EXIT_OK


# --- normalization and comparison ------------------------------------------------------------------------------------


def _code_in(code: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(code)}(?![A-Za-z0-9_])", text or "") is not None


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _label_params(params: Mapping, idmap: IdMap, mapping: Mapping[str, tuple[str, str]]) -> dict:
    return {key: idmap.label(kind, params[name]) for name, (kind, key) in mapping.items() if name in params}


def normalize(raw: Mapping, ctx: ScenarioRun) -> dict:
    """The behavioral trace: ids become labels, environmental values (times, attempt ids, stamps, paths, tokens)
    are dropped; counts, outcomes, costs, aliases and courses are kept as observed."""
    idmap = ctx.idmap
    mutations: dict[str, list] = {}
    applied: dict[str, int] = {}
    resource_delta, requests, timer, journal, planned = {}, {}, {}, [], []
    fake_violations = []
    for alias, account in raw["accounts"].items():
        for entry in account["ledger"]:
            route = f"{entry['method']} {entry['path']}"
            mutations.setdefault(route, []).append({"alias": alias, **_label_params(entry["params"], idmap,
                                                                                     PARAM_LABELS)})
            if entry["applied"]:
                applied[route] = applied.get(route, 0) + 1
        resource_delta[alias] = dict(account["delta"])
        requests[alias] = account["requests"]
        fake_violations += [f"{alias}: {violation}" for violation in account["violations"]]
        for cid, (design_id, start, end) in account["started"].items():
            duration = _seconds_between(start, end)
            timer[idmap.label("crew", cid)] = {"course": idmap.label("course", design_id), "duration_s": duration}
        for event in account["journal"]:
            row = {"alias": event["alias"], "action": event["action"],
                   **_label_params(event["params"], idmap, JOURNAL_LABELS)}
            if event["dry_run"]:
                if event["action"] in TRAINING_ACTIONS and isinstance(event["evidence"].get("dry_run_id"), str):
                    planned.append(row)
                continue
            if event["outcome"] == "skipped" and not event["attempted"]:
                continue  # a deliberate skip record: checked through "skips", free to exist
            cost = event["cost"]
            row.update({"outcome": event["outcome"], "attempted": event["attempted"],
                        "cost": {"minerals": _spent(cost.get("minerals_delta")), "gas": _spent(cost.get("gas_delta"))}})
            journal.append(row)
    for rows in mutations.values():
        rows.sort(key=_canon)
    return {"exit": raw["exit"], "requests": requests, "mutations": mutations, "applied": applied,
            "resource_delta": resource_delta, "timer": timer, "journal": sorted(journal, key=_canon),
            "journal_planned": sorted(planned, key=_canon), "texts": skip_records(raw),
            "store": {alias: "present" if account.get("store") else "absent"
                      for alias, account in raw["accounts"].items()},
            "journal_unreadable": sorted(alias for alias, account in raw["accounts"].items()
                                         if account.get("journal_unreadable")),
            "stdout_lines": raw["stdout"].splitlines(), "fake_violations": fake_violations, "raw": raw}


# ``  - account '<alias>' ...`` or ``  - account '<alias>': ...`` (INTERFACE ASSUMPTIONS item 5).
REFUSAL_LINE = re.compile(r"^\s*-\s*account '([^']+)'(?=[\s:]|$)")
SEGMENT_BREAK = re.compile(r"[;|\n]")


def skip_records(raw: Mapping) -> list[tuple[str, str, str]]:
    """(alias, kind, text) of every structured record a skip code may be read from (INTERFACE ASSUMPTIONS item 5):
    run-record account fields, the account's journal events (reason and params), and pre-login refusal lines naming
    the account. Free stdout is never one of them."""
    rows = [(alias, "record", text) for alias, text in raw.get("records", ())]
    for alias, account in raw["accounts"].items():
        rows += [(alias, "journal", f"{event['action']} {_canon(event['params'])}: {event['reason']}")
                 for event in account["journal"] if event["outcome"] != "verified"]
    for line in raw.get("stderr", "").splitlines():
        match = REFUSAL_LINE.match(line)
        if match:
            rows.append((match.group(1), "refusal", line))
    return rows


def _spent(delta: Any) -> Any:
    """A journal balance delta as a signed cost: a spend is positive, a refund negative (never its magnitude), None
    when the journal recorded no delta. A frozen ``cost`` row names amounts in this convention."""
    return None if delta is None else -delta


def _seconds_between(start: str, end: str) -> int:
    parse = datetime.datetime.strptime
    return int((parse(end, "%Y-%m-%dT%H:%M:%S") - parse(start, "%Y-%m-%dT%H:%M:%S")).total_seconds())


@dataclass(frozen=True)
class Unmet:
    step: str
    assertion: str
    expected: str
    actual: str


def _row_matches(expected: Mapping, actual: Mapping) -> bool:
    """Whether ``actual`` has every key ``expected`` names; ``outcome_in`` lists allowed outcomes and ``any_of`` lists
    alternative rows (each with the row's other keys) the spec equally allows."""
    for key, value in expected.items():
        if key == "outcome_in":
            if actual.get("outcome") not in value:
                return False
        elif key == "any_of":
            if not any(_row_matches(alternative, actual) for alternative in value):
                return False
        elif actual.get(key) != value:
            return False
    return True


def _multiset_diff(expected: Sequence[Mapping], actual: Sequence[Mapping]) -> bool:
    """True when the expected rows and the actual rows pair off one to one, each expected row matching its actual
    row by the keys it names (a bipartite matching, so the order of either list never matters)."""
    if len(expected) != len(actual):
        return False
    candidates = [[index for index, row in enumerate(actual) if _row_matches(want, row)] for want in expected]
    owner: dict[int, int] = {}  # actual index -> expected index

    def assign(want: int, seen: set) -> bool:
        for index in candidates[want]:
            if index in seen:
                continue
            seen.add(index)
            if index not in owner or assign(owner[index], seen):
                owner[index] = want
                return True
        return False

    return all(assign(want, set()) for want in range(len(expected)))


def _codes_in(text: str, codes: Sequence[str]) -> set:
    return {code for code in codes if _code_in(code, text)}


def _crew_ids_in(text: str, ctx: ScenarioRun) -> set[str]:
    """The scenario crew instance ids that ``text`` names (as whole tokens)."""
    return {cid for cid in ctx.idmap.ids.get("crew", {}).values() if _code_in(cid, text)}


def _skip_found(row: Mapping, trace: Mapping, ctx: ScenarioRun, codes: Sequence[str]) -> bool:
    """Whether a structured record of the expected account carries the code. A refusal line must name exactly one
    code. When the expectation names a crew, the code must sit with that crew alone: in a record naming no other
    scenario crew, or in a segment of it (INTERFACE ASSUMPTIONS item 5) naming that crew alone."""
    crew_id = ctx.idmap.ids.get("crew", {}).get(row.get("crew")) if row.get("crew") else None
    for alias, kind, text in trace["texts"]:
        if alias != row["alias"] or not _code_in(row["code"], text):
            continue
        if kind == "refusal" and len(_codes_in(text, codes)) != 1:
            continue
        if crew_id is None:
            return True
        if any(_code_in(row["code"], part) and _crew_ids_in(part, ctx) == {crew_id}
               for part in [text, *SEGMENT_BREAK.split(text)]):
            return True
    return False


def _codes_for(alias: str, trace: Mapping, codes: Sequence[str]) -> list[str]:
    return sorted({code for owner, _, text in trace["texts"] if owner == alias for code in _codes_in(text, codes)})


def compare(expected: Mapping, trace: Mapping, ctx: ScenarioRun, step_id: str, codes: Sequence[str]) -> list[Unmet]:
    """Every unmet behavioral assertion of one step, named; [] when the step matches its frozen trace."""
    unmet: list[Unmet] = []

    def fail(name: str, want: Any, got: Any) -> None:
        unmet.append(Unmet(step_id, name, _canon(want), _canon(got)))

    if "exit" in expected:
        want, got = expected["exit"], trace["exit"]
        ok = (got not in (0, "crashed") and isinstance(got, int)) if want == "nonzero" else got == want
        if not ok:
            fail("exit", want, got)
    for alias, count in expected.get("requests", {}).items():
        if trace["requests"].get(alias, 0) != count:
            fail(f"requests.{alias}", count, trace["requests"].get(alias, 0))
    named = expected.get("mutations", {})
    for route, rows in named.items():
        got = trace["mutations"].get(route, [])
        if sorted(map(_canon, rows)) != sorted(map(_canon, got)):
            fail(f"mutations.{route}", sorted(rows, key=_canon), got)
    if "other_mutations" in expected:
        others = sum(len(rows) for route, rows in trace["mutations"].items() if route not in named)
        if others != expected["other_mutations"]:
            fail("other_mutations", expected["other_mutations"],
                 {route: len(rows) for route, rows in trace["mutations"].items() if route not in named})
    for route, count in expected.get("applied", {}).items():
        if trace["applied"].get(route, 0) != count:
            fail(f"applied.{route}", count, trace["applied"].get(route, 0))
    for alias, delta in expected.get("resource_delta", {}).items():
        if trace["resource_delta"].get(alias) != delta:
            fail(f"resource_delta.{alias}", delta, trace["resource_delta"].get(alias))
    if "timer" in expected:
        for crew in sorted(set(expected["timer"]) | set(trace["timer"])):
            if expected["timer"].get(crew) != trace["timer"].get(crew):
                fail(f"timer.{crew}", expected["timer"].get(crew), trace["timer"].get(crew))
    for key in ("journal", "journal_planned"):
        if key in expected and not _multiset_diff(expected[key], trace[key]):
            fail(key, expected[key], trace[key])
    for alias, state in expected.get("store", {}).items():
        if trace["store"].get(alias) != state:
            fail(f"store.{alias}", state, trace["store"].get(alias))
    for row in expected.get("crew_plan", ()):
        if not _crew_plan_line(row, trace, ctx, codes):
            name = row.get("crew") or row["role"]
            fail(f"crew_plan.{name}", row, "no stdout line has the crew's instance id, role and course or skip code")
    for row in expected.get("prestige", ()):
        if not _prestige_line(row, trace, ctx):
            fail(f"prestige.{row['target']}", row, "no stdout line names the target, ingredient and missing count")
    for row in expected.get("skips", ()):
        if not _skip_found(row, trace, ctx, codes):
            fail(f"skips.{row['alias']}.{row['code']}" + (f".{row['crew']}" if row.get("crew") else ""), row,
                 {"codes_in_records": _codes_for(row["alias"], trace, codes)})
    if "dispatches" in expected:
        ok, shown = compare_dispatches(expected["dispatches"], trace, ctx, codes)
        if not ok:
            fail("dispatches", expected["dispatches"], shown)
    if trace.get("journal_unreadable"):  # never read as an empty journal
        fail("journal_unreadable", [], trace["journal_unreadable"])
    if trace["fake_violations"]:
        fail("fake_violations", [], trace["fake_violations"])
    return unmet


def _crew_plan_line(row: Mapping, trace: Mapping, ctx: ScenarioRun, codes: Sequence[str]) -> bool:
    """One stdout line with the crew's instance id, the role word and the course name or the skip code; a skip
    line names exactly that one code (so a line listing every code satisfies nothing)."""
    crew_id = ctx.idmap.ids["crew"].get(row["crew"]) if row.get("crew") else None
    course = ctx.catalog["training_designs"][row["course"]]["name"] if row.get("course") else None
    for line in trace["stdout_lines"]:
        if crew_id is not None and crew_id not in line:
            continue
        if not _code_in(row["role"], line):
            continue
        if course is not None and course not in line:
            continue
        if row.get("skip") is not None and _codes_in(line, codes) != {row["skip"]}:
            continue
        return True
    return False


def _prestige_line(row: Mapping, trace: Mapping, ctx: ScenarioRun) -> bool:
    designs = ctx.catalog["character_designs"]
    target, ingredient = designs[row["target"]]["name"], designs[row["ingredient"]]["name"]
    return any(target in line and ingredient in line and _code_in(f"missing {row['missing']}", line)
               for line in trace["stdout_lines"])


def dispatch_summaries(trace: Mapping, ctx: ScenarioRun, codes: Sequence[str]) -> list[list[dict]]:
    """Per dispatch, per alias: AddTraining and mutating requests sent, whether one applied, candidates, skip codes."""
    raw = trace["raw"]
    outputs = raw.get("dispatch_outputs", [])
    summaries = []
    for index, output in enumerate(outputs):
        rows = []
        for alias, account in raw["accounts"].items():
            mine = [entry for entry in account["ledger"] if entry["thread"] == f"dispatch-{index}"]
            starts = [entry for entry in mine if f"{entry['method']} {entry['path']}" == ADD_TRAINING]
            single = len(raw["accounts"]) == 1
            found = sorted({code for line in output.splitlines() if single or _code_in(alias, line)
                            for code in _codes_in(line, codes) if len(_codes_in(line, codes)) == 1})
            rows.append({"alias": alias, "add_training": len(starts), "mutations": len(mine),
                         "started": any(entry["applied"] for entry in starts),
                         "candidates": sorted(ctx.idmap.label("crew", entry["params"].get("characterId"))
                                              for entry in starts),
                         "codes": found})
        summaries.append(rows)
    return summaries


def _dispatch_row_ok(want: Mapping, got: Mapping) -> bool:
    if want.get("result") == "started" and not got["started"]:
        return False
    if want.get("result") == "skipped" and got["started"]:
        return False
    for key in ("add_training", "mutations"):
        if key in want and want[key] != got[key]:
            return False
    allowed = set(want.get("candidate_one_of", ()))
    if "candidate_one_of" in want and not (got["candidates"] and set(got["candidates"]) <= allowed):
        return False
    return not ("code" in want and want["code"] not in got["codes"])


def compare_dispatches(expected: Sequence[Sequence[Mapping]], trace: Mapping, ctx: ScenarioRun,
                       codes: Sequence[str]) -> tuple[bool, Any]:
    """Concurrent outcomes compared canonically: any assignment of expected dispatches to actual ones may match,
    while account and candidate identity, request counts and overlap evidence are kept."""
    actual = dispatch_summaries(trace, ctx, codes)
    if len(actual) == len(expected):
        for order in itertools.permutations(range(len(actual))):
            if all(all(any(row["alias"] == want["alias"] and _dispatch_row_ok(want, row) for row in actual[index])
                       for want in wants) for wants, index in zip(expected, order)):
                return True, None
    allowed = {crew for wants in expected for want in wants for crew in want.get("candidate_one_of", ())}
    shown = [[{**row, "candidates": ["*" if crew in allowed else crew for crew in row["candidates"]]}
              for row in rows] for rows in actual]
    return False, sorted(shown, key=_canon)


# --- behavior digest (determinism) -------------------------------------------------------------------------------

ROLE_WORDS = ("repair", "shields", "weapons")  # the role words of the output contract (INTERFACE ASSUMPTIONS item 5)


def behavior(trace: Mapping, ctx: ScenarioRun, codes: Sequence[str]) -> dict:
    """The step's behavior, without environmental values: requests per route, ledger mutations and applied counts,
    resource deltas, timers, journal rows, store presence, skip records (their codes and the crew they name), the
    labels each printed line names, and the overlap dispatch rows in canonical order (which dispatch won is thread
    timing; which crew it started is the candidate's choice, so candidate labels are kept). Raw text, times,
    attempt ids, stamps and paths are dropped."""
    raw = trace["raw"]
    names = {kind: {row["name"]: label for label, row in ctx.catalog[kind].items()}
             for kind in ("training_designs", "character_designs")}

    def crews(text: str) -> list[str]:
        return sorted(label for label, cid in ctx.idmap.ids.get("crew", {}).items() if _code_in(cid, text))

    lines = []
    for line in raw["stdout"].splitlines():
        row = {"crew": crews(line), "course": sorted(label for name, label in names["training_designs"].items()
                                                     if name in line),
               "design": sorted(label for name, label in names["character_designs"].items() if name in line)}
        if any(row.values()):
            row.update({"role": [word for word in ROLE_WORDS if _code_in(word, line)],
                        "codes": sorted(_codes_in(line, codes)), "missing": re.findall(r"missing (\d+)", line)})
            lines.append(row)
    skips = [{"alias": alias, "kind": kind, "codes": sorted(_codes_in(text, codes)), "crew": crews(text)}
             for alias, kind, text in trace["texts"]]
    requests = {alias: dict(sorted(Counter(f"{x['method']} {x['path']}" for x in account["exchanges"]).items()))
                for alias, account in raw["accounts"].items()}
    code = trace["exit"]
    return {"exit": sorted(code, key=_canon) if isinstance(code, list) else code, "requests": requests,
            "mutations": trace["mutations"], "applied": trace["applied"], "resource_delta": trace["resource_delta"],
            "timer": trace["timer"], "journal": trace["journal"], "journal_planned": trace["journal_planned"],
            "store": trace["store"], "journal_unreadable": trace["journal_unreadable"],
            "skips": sorted(skips, key=_canon), "lines": sorted(lines, key=_canon),
            "dispatches": sorted((sorted(rows, key=_canon) for rows in dispatch_summaries(trace, ctx, codes)),
                                 key=_canon),
            "fake_violations": len(trace["fake_violations"])}


def behavior_digest(trace: Mapping, ctx: ScenarioRun, codes: Sequence[str]) -> str:
    """sha256 of the step's canonical behavior (S14-AC6: repeated runs and another hash seed must agree on it)."""
    return hashlib.sha256(_canon(behavior(trace, ctx, codes)).encode("utf-8")).hexdigest()


# --- hard violations ---------------------------------------------------------------------------------------------


def hard_violations(ctx: ScenarioRun, step: Mapping, expected: Mapping, trace: Mapping) -> list[str]:
    """Violations that fail the gate whatever the score: account crossover, premium or budget spend, duplicate
    writes and false verification. Read from the fake's ledger and world, checked against the journal."""
    found = []
    raw = trace["raw"]
    sid = step["id"]
    starts_by_alias: dict[str, list] = {}
    for alias, account in raw["accounts"].items():
        own_crew = {cid for cid, label in ctx.idmap.labels.get("crew", {}).items()
                    if ctx.idmap.owners.get(f"crew:{label}") == alias}
        spent = {"mineral": 0, "gas": 0}
        for entry in account["ledger"]:
            cid = entry["params"].get("characterId")
            if cid is not None and cid not in own_crew:
                found.append(f"{sid}: account_crossover: {alias} received a request for character "
                             f"{ctx.idmap.label('crew', cid)} of another account")
            if entry["delta"]["starbux"] < 0:
                found.append(f"{sid}: premium_spend: {alias} spent {-entry['delta']['starbux']} starbux")
            for kind in spent:
                spent[kind] += max(0, -entry["delta"][kind])
            if f"{entry['method']} {entry['path']}" == ADD_TRAINING:
                starts_by_alias.setdefault(alias, []).append(entry)
        caps = ctx.caps(alias)
        for kind, cap_key in (("mineral", "minerals"), ("gas", "gas")):
            if spent[kind] > caps.get(cap_key, 0):
                found.append(f"{sid}: budget_spend: {alias} spent {spent[kind]} {kind}, above its cap "
                             f"{caps.get(cap_key, 0)}")
            want = expected.get("resource_delta", {}).get(alias)
            if want is not None and spent[kind] > max(0, -want.get(kind, 0)):
                found.append(f"{sid}: budget_spend: {alias} spent {spent[kind]} {kind}, more than the frozen "
                             f"trace allows ({max(0, -want.get(kind, 0))})")
        for event in account["journal"]:
            if event["alias"] != alias:
                found.append(f"{sid}: account_crossover: {alias}'s journal holds an event for {event['alias']}")
            cid = event["params"].get("character_id")
            if cid is not None and str(cid) not in own_crew:
                found.append(f"{sid}: account_crossover: {alias}'s journal names character "
                             f"{ctx.idmap.label('crew', cid)}")
            if event["action"] == "add_training" and event["outcome"] == "verified" and not event["dry_run"]:
                problem = verification_problem(ctx, alias, event, account)
                if problem:
                    found.append(f"{sid}: false_verification: {alias} journaled add_training "
                                 f"{ctx.idmap.label('crew', cid)} verified {problem}")
        # A later run's reconciliation may restate an earlier step's start (and its spend): not charged here.
        attempted = [e for e in account["journal"] if e["attempted"] and not e["dry_run"]
                     and not _restates_earlier(ctx, alias, e, account)]
        for kind, cap_key in (("mineral", "minerals"), ("gas", "gas")):
            # Only a final outcome with an observed cost is compared: an unknown or not-observed attempt may
            # rightly journal no (or an uncertain) spend, and then the observed rest may only be at most the charge.
            observed = [e for e in attempted if e["outcome"] in FINAL_OUTCOMES
                        and e["cost"].get(f"{cap_key}_delta") is not None]
            journaled = sum(_spent(e["cost"][f"{cap_key}_delta"]) for e in observed)
            complete = len(observed) == len(attempted)
            if attempted and (journaled != spent[kind] if complete else journaled > spent[kind]):
                found.append(f"{sid}: budget_spend: {alias} journaled {journaled} {kind} spent, the fake charged "
                             f"{spent[kind]}")
    for alias, starts in starts_by_alias.items():
        if len(starts) > 1:  # sent, applied or not: at most one training start per account per run (S14-R4)
            found.append(f"{sid}: duplicate_write: {len(starts)} AddTraining sent on {alias} in one step")
        if step.get("blocked_after_unresolved") and starts:
            found.append(f"{sid}: duplicate_write: {len(starts)} AddTraining on {alias} after an unresolved attempt")
    return found


def _applies(ctx: ScenarioRun, alias: str, event: Mapping) -> Callable[[Mapping], bool]:
    """Whether a ledger entry applied the journaled course to the journaled crew with the course's full duration."""
    cid, design_id = str(event["params"].get("character_id")), str(event["params"].get("training_design_id"))
    try:
        duration = int(ctx.fakes[alias].state.training_design(design_id).duration.total_seconds())
    except KeyError:
        return lambda entry: False
    return lambda entry: bool(entry["applied"] and entry.get("session") and entry["session"][0] == cid
                              and entry["session"][1] == design_id
                              and _seconds_between(entry["session"][2], entry["session"][3]) == duration)


def _restates_earlier(ctx: ScenarioRun, alias: str, event: Mapping, account: Mapping) -> bool:
    """A journaled add_training whose start the fake applied in an earlier step and not in this one."""
    if event["action"] != "add_training":
        return False
    applies = _applies(ctx, alias, event)
    return not any(applies(e) for e in account["ledger"]) and any(applies(e) for e in account.get("earlier_ledger", ()))


def _good_read(exchange: Mapping) -> bool:
    method, _, path = CHARACTERS_READ.partition(" ")
    return (exchange["method"], exchange["path"]) == (method, path) and exchange["status"] == 200 \
        and not exchange["error"]


def _shows_start(exchange: Mapping, event: Mapping) -> bool:
    """Whether a successful roster read showed the journaled crew on the journaled course with a running timer
    (from what the fake served, FakePss Exchange.roster)."""
    seen = (exchange.get("roster") or {}).get(str(event["params"].get("character_id")))
    return _good_read(exchange) and isinstance(seen, Mapping) and seen.get("running") is True \
        and seen.get("course") == str(event["params"].get("training_design_id"))


def verification_problem(ctx: ScenarioRun, alias: str, event: Mapping, account: Mapping) -> str | None:
    """Why a journaled ``verified`` start is false (S14-R5), or None. The fake must have applied that course to that
    crew with its full duration, and a successful roster read showing the crew on that course with a running timer
    must follow: after the start on the same dispatch when it was sent in this step, or anywhere in this step when
    it reconciles a start from an earlier step."""
    applies = _applies(ctx, alias, event)
    here = [entry for entry in account["ledger"] if applies(entry)]
    if here:
        start = here[-1]
        if any(_shows_start(x, event) and x["index"] > start["exchange"] and x["thread"] == start["thread"]
               for x in account["exchanges"]):
            return None
        return "without a successful roster read after the start showing the course with a running timer"
    if any(applies(entry) for entry in account.get("earlier_ledger", ())):
        return None if any(_shows_start(x, event) for x in account["exchanges"]) else \
            "from an earlier start without a fresh roster read showing the course with a running timer"
    return "without the requested course and timer"


# --- diagnosis --------------------------------------------------------------------------------------------------


def step_cause(raw: Mapping | None) -> str:
    """Why a step behaved as it did, from its own output: a known refusal, a crash, or a behavioral mismatch."""
    if raw is None:
        return "world edit"
    text = raw.get("stderr", "")
    for needle, cause in STEP_CAUSES:
        if needle in text:
            return cause
    if raw.get("exit") == "crashed":
        return "simulated crash at the armed request"
    if isinstance(raw.get("exit"), str) and raw["exit"].startswith("error "):
        return f"production raised {raw['exit'][len('error '):]} (the step's exit records it; comparison went on)"
    lines = text.splitlines()
    if "error: --fleet-execute refused before any login:" in text:
        reasons = [line.strip(" -") for line in lines if line.startswith("  - ")]
        return "execute refused before login: " + "; ".join(_scrub(reason) for reason in reasons[:2])
    return "behavioral mismatch (the step ran; see the assertion)"


def _scrub(text: str) -> str:
    """Drop scratch paths and hex stamps so a cause line is identical in every run."""
    text = re.sub(r"(/[^\s:'\"]+)+", "<path>", text)
    return re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", text)


# --- suite ------------------------------------------------------------------------------------------------------


@dataclass
class TraceResult:
    passed: bool
    unmet: list[dict]
    witnesses_missing: list[str]
    hard_violations: list[str]
    errors: list[str]
    codes_seen: list[str] = field(default_factory=list)  # skip codes in any output: diagnosis only, not scored
    behavior: dict[str, str] = field(default_factory=dict)  # step id -> behavior_digest, compared for determinism

    def to_dict(self) -> dict:
        return {"pass": self.passed, "unmet": self.unmet, "witnesses_missing": self.witnesses_missing,
                "hard_violations": self.hard_violations, "errors": self.errors, "behavior": self.behavior}


def run_scenario(suite: str, case_id: str, scenario: Mapping, catalog: Mapping, seed: int, codes: Sequence[str],
                 policy_override: str | None = None) -> TraceResult:
    """Run every step of one scenario at one seed and compare each against its frozen trace."""
    preimport_sdk()
    unmet: list[dict] = []
    violations: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    digests: dict[str, str] = {}
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="twb-") as scratch:
        directory = Path(scratch)
        os.chdir(directory)
        try:
            ctx = start_scenario(suite, case_id, scenario, catalog, seed, policy_override, directory)
            for step in scenario["steps"]:
                raw = run_step(ctx, step)
                expected = scenario["expected"].get(step["id"])
                if raw is None:
                    continue
                trace = normalize(raw, ctx)
                digests[step["id"]] = behavior_digest(trace, ctx, codes)
                if expected is None:
                    continue
                seen |= {code for code in codes for _, _, text in trace["texts"] if _code_in(code, text)}
                errors += [v for v in trace["fake_violations"] if "FakePss error" in v or "handler error" in v]
                cause = step_cause(raw)
                unmet += [{"step": u.step, "assertion": u.assertion, "expected": u.expected, "actual": u.actual,
                           "cause": cause} for u in compare(expected, trace, ctx, step["id"], codes)]
                violations += hard_violations(ctx, step, expected, trace)
            missing = missing_witnesses(scenario.get("witnesses", ()), ctx.witnesses)
        finally:
            os.chdir(cwd)
    passed = not unmet and not missing and not violations and not errors
    return TraceResult(passed, unmet, missing, violations, errors, sorted(seen), digests)


def missing_witnesses(declared: Sequence[str], fired: Mapping[str, str | None]) -> list[str]:
    """Each declared witness that did not fire, with why: one recorded as not fired (a reason string) or never
    recorded at all (its step never ran) is missing; only a witness recorded as fired (None) is present."""
    return [f"{name}: {fired.get(name) or 'step never ran'}" for name in declared
            if name not in fired or fired[name] is not None]


def _trace_key(scenario: Mapping, seed: int) -> str:
    return f"{scenario['id']}@{seed}"


def run_case(suite: str, case: Mapping, catalog: Mapping, seeds: Sequence[int], codes: Sequence[str],
             control_codes: Sequence[str], *, probe: bool, control_seeds: Sequence[int] | None = None) -> dict:
    traces, controls, probes = {}, {}, {}
    for seed in seeds:
        for scenario in case["scenarios"]:
            result = run_scenario(suite, case["id"], scenario, catalog, seed, codes)
            traces[_trace_key(scenario, seed)] = result.to_dict()
            if probe and not result.passed:
                probes[_trace_key(scenario, seed)] = probe_summary(run_scenario(
                    suite, case["id"], scenario, catalog, seed, sorted(set(codes) | set(control_codes)),
                    policy_override="training-v1"))
    for seed in control_seeds if control_seeds is not None else seeds:
        for scenario in case["control"]["scenarios"]:
            controls[_trace_key(scenario, seed)] = run_scenario(
                suite, case["id"], scenario, catalog, seed, control_codes,
                policy_override=case["control"]["policy_id"]).to_dict()
    result = {"traces": traces, "control": {"pass": all(t["pass"] for t in controls.values()), "traces": controls},
              "expected_red_cause": case["red"]["expected_cause"]}
    if probe:
        result["probe"] = probes
    return result


def probe_summary(result: TraceResult) -> str:
    """The training-v1 replay's behavioral cause: its first unmet assertions and hard violations."""
    behavioral = [u for u in result.unmet if u["step"] != "provision"] or result.unmet  # v1 has no provisioning
    parts = [f"{u['step']} {u['assertion']} expected {u['expected']} actual {u['actual']}" for u in behavioral[:3]]
    parts += result.hard_violations[:2]
    parts += [f"witness missing {w}" for w in result.witnesses_missing[:1]]
    seen = f"(skip codes seen: {', '.join(result.codes_seen) or 'none'}) "
    return "v1 probe " + seen + ("; ".join(parts) if parts else "matches the training-v2 trace")


def run_suite(suites: Mapping[str, Mapping], *, cases: Sequence[str] | None = None, probe: bool = True,
              control_seed_limit: int | None = None) -> dict:
    """Every selected case at every seed of its suite; returns the report dict."""
    report: dict[str, Any] = {"schema": SCHEMA, "suites": {}}
    for name, suite in suites.items():
        vocabulary = suite["vocabulary"]
        codes = list(vocabulary["skip_codes"])
        control_codes = sorted(set(codes) | set(vocabulary["control_skip_codes"]))
        results = {}
        for case in suite["cases"]:
            if cases and case["id"] not in cases:
                continue
            control_seeds = suite["seeds"][:control_seed_limit] if control_seed_limit else None
            results[case["id"]] = run_case(name, case, suite["catalog"], suite["seeds"], codes, control_codes,
                                           probe=probe, control_seeds=control_seeds)
        total = sum(len(result["traces"]) for result in results.values())
        passed = sum(trace["pass"] for result in results.values() for trace in result["traces"].values())
        report["suites"][name] = {"seeds": list(suite["seeds"]), "cases": results, "passed": passed, "total": total,
                                  "score": f"{(100.0 * passed / total) if total else 0.0:.6f}"}
    return report


def scored_view(report: Mapping) -> dict:
    """The report without probe text: what the determinism check compares, each trace's per-step behavior digests
    included, so a run whose behavior differs fails it even when its pass status does not."""
    view = copy.deepcopy(dict(report))
    for suite in view.get("suites", {}).values():
        for case in suite["cases"].values():
            case.pop("probe", None)
    return view


def to_json(report: Mapping) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def gate_reasons(report: Mapping, *, deterministic: bool, subset: bool) -> list[str]:
    reasons = []
    if subset:
        reasons.append("subset: only some cases ran, so the gate cannot pass")
    if not deterministic:
        reasons.append("nondeterministic: repeated runs produced different scored reports")
    for name, suite in report["suites"].items():
        if suite["total"] == 0:
            reasons.append(f"{name}: empty suite")
        failing = sum(not trace["pass"] for case in suite["cases"].values() for trace in case["traces"].values())
        if suite["score"] != "100.000000":
            reasons.append(f"{name}: score {suite['score']} ({suite['passed']} of {suite['total']} traces pass)")
        elif failing:  # never only the score: a failing trace is a reason even if the score says otherwise
            reasons.append(f"{name}: {failing} failing traces under a score of {suite['score']}")
        for case_id, case in suite["cases"].items():
            for key, trace in case["traces"].items():
                if trace["witnesses_missing"]:
                    reasons.append(f"{name}/{case_id}/{key}: missing witness")
                if trace["hard_violations"]:
                    reasons.append(f"{name}/{case_id}/{key}: hard violation")
            if not case["control"]["pass"]:
                reasons.append(f"{name}/{case_id}: training-v1 control failed")
    return reasons


def fake_errors(report: Mapping) -> list[str]:
    return [error for suite in report["suites"].values() for case in suite["cases"].values()
            for trace in [*case["traces"].values(), *case["control"]["traces"].values()] for error in trace["errors"]]


def render_text(report: Mapping, reasons: Sequence[str] = ()) -> str:
    lines = ["# SPEC-14 workflow benchmark", ""]
    for name, suite in report["suites"].items():
        lines.append(f"{name}: score {suite['score']} ({suite['passed']} of {suite['total']} traces pass), "
                     f"seeds {suite['seeds']}")
    lines.append("")
    for name, suite in report["suites"].items():
        for case_id, case in suite["cases"].items():
            control = "PASS" if case["control"]["pass"] else "FAIL"
            for key, trace in case["traces"].items():
                scenario, _, seed = key.rpartition("@")
                where = f"{name}/{case_id}/{scenario} seed={seed}"
                probe = case.get("probe", {}).get(key)
                tail = (f" | {probe}" if probe else "") + f" | control training-v1: {control}"
                for unmet in trace["unmet"]:
                    lines.append(f"FAIL {where} step={unmet['step']} {unmet['assertion']}: expected "
                                 f"{unmet['expected']} actual {unmet['actual']} -- cause: {unmet['cause']}{tail}")
                for witness in trace["witnesses_missing"]:
                    lines.append(f"FAIL {where} witness {witness}{tail}")
                for violation in trace["hard_violations"]:
                    lines.append(f"FAIL {where} hard violation {violation}{tail}")
                if trace["pass"]:
                    lines.append(f"PASS {where}{tail}")
            for key, trace in case["control"]["traces"].items():
                if not trace["pass"]:
                    scenario, _, seed = key.rpartition("@")
                    for unmet in trace["unmet"]:
                        lines.append(f"CONTROL-FAIL {name}/{case_id}/{scenario} seed={seed} step={unmet['step']} "
                                     f"{unmet['assertion']}: expected {unmet['expected']} actual {unmet['actual']}")
                    for item in [*trace["witnesses_missing"], *trace["hard_violations"], *trace["errors"]]:
                        lines.append(f"CONTROL-FAIL {name}/{case_id}/{scenario} seed={seed} {item}")
    if reasons:
        lines += ["", "Gate: FAIL", *[f"- {reason}" for reason in reasons]]
    return "\n".join(lines) + "\n"


# --- the gate's own preconditions: pinned fixtures and the fake's strict rules ------------------------------------


def verify_fixture_digests(names: Sequence[str]) -> None:
    """The gate's own tripwire (not only the tests'): each fixture it scores must hash to its FIXTURE_SHA256."""
    for name in names:
        path = FIXTURES[name]
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError as error:
            raise FixtureError(f"{path}: {error}") from None
        if digest != FIXTURE_SHA256[name]:
            raise FixtureError(f"{name} fixture {path} has sha256 {digest}, which does not match its pinned "
                               f"FIXTURE_SHA256 {FIXTURE_SHA256[name]}: only an approved oracle change may edit it")


SELF_CHECK_BASE = "https://api.pixelstarships.com"  # served by the in-process FakePss adapter; never the network
SELF_CHECK_WORLD = {"mineral": 5000, "gas": 5000, "credits": 7, "price": "mineral:500"}
# (name, world arguments, world edit, the refusal AddTraining must answer (None: it applies), wallet after the start
# as (mineral, gas, starbux)). Every case also checks the AddTraining ledger entry (applied flag, refusal and delta)
# and, for a refusal, the reply's errorMessage text.
def _second_crew_session(hours_left: float, room: str = "11") -> Callable[[fake_state.World], None]:
    """World edit: the second crew trains in ``room`` (a second level 2 Academy "12" is added for it), its session
    ending ``hours_left`` hours after FAKE_EPOCH (negative: ended, not yet claimed)."""
    def edit(world: fake_state.World) -> None:
        if room != "11":
            world.room_designs.append(fake_state.RoomDesign("902", "Academy", "Training", level=2, capacity=1))
            world.rooms.append(fake_state.Room(room, "902"))
        world.characters[1].room_id = room
        end = fake_state.FAKE_EPOCH + datetime.timedelta(hours=hours_left)
        world.characters[1].session = fake_state.TrainingSession("41", end - datetime.timedelta(hours=2), end)
    return edit


def _other_research_completed(world: fake_state.World) -> None:
    """World edit: only another research (not the course's) is completed."""
    world.research_designs.append(fake_state.ResearchDesign("62", "62", "Fitness 102", 1000))
    world.researches = [fake_state.Research("62")]


SELF_CHECKS:tuple[tuple[str, Mapping, Callable | None, str | None, tuple[int, int, int]], ...] = (
    ("mineral price charged as minerals", {"mineral": 500, "gas": 300}, None, None, (0, 300, 7)),
    ("gas price charged as gas", {"mineral": 1000, "gas": 300, "price": "gas:300"}, None, None, (1000, 0, 7)),
    ("starbux price charged from credits", {"price": "starbux:5"}, None, None, (5000, 5000, 2)),
    ("insufficient minerals refused", {"mineral": 499}, None, "Not enough minerals", (499, 5000, 7)),
    ("insufficient gas refused", {"gas": 299, "price": "gas:300"}, None, "Not enough gas", (5000, 299, 7)),
    ("room level refused", {}, lambda world: setattr(world.room_designs[0], "level", 1), "Room level is too low",
     (5000, 5000, 7)),
    ("research refused", {}, lambda world: setattr(world, "researches", []), "Research required", (5000, 5000, 7)),
    ("research in progress refused", {}, lambda world: setattr(world.researches[0], "state", "Researching"),
     "Research required", (5000, 5000, 7)),
    ("another research refused", {}, _other_research_completed, "Research required", (5000, 5000, 7)),
    ("room not ready refused", {}, lambda world: setattr(world.rooms[0], "status", "Upgrading"), "Room is not ready",
     (5000, 5000, 7)),
    ("wrong room refused", {}, lambda world: setattr(world.room_designs[0], "name", "Gym"),
     "Wrong room for this training", (5000, 5000, 7)),
    ("fatigue refused", {}, lambda world: setattr(world.characters[0], "fatigue", fake_state.MAX_TRAINING_FATIGUE),
     "Crew is too fatigued", (5000, 5000, 7)),
    ("room capacity refused", {}, lambda world: setattr(world.characters[1], "session", fake_state.TrainingSession(
        "41", fake_state.FAKE_EPOCH - datetime.timedelta(hours=1), fake_state.FAKE_EPOCH + datetime.timedelta(hours=1))),
     "Room is full", (5000, 5000, 7)),
    ("ended unclaimed session leaves room", {}, _second_crew_session(-1), None, (4500, 5000, 7)),
    ("another room's session leaves room", {}, _second_crew_session(1, room="12"), None, (4500, 5000, 7)),
    ("training point capacity refused", {},
     lambda world: setattr(world.characters[0], "stats", fake_state.stats(Repair=60, Weapon=40)),
     "Training points are at capacity", (5000, 5000, 7)),
)


def _self_check_world(mineral: int, gas: int, credits: int, price: str) -> fake_state.World:
    """A strict world: two repair crew in a level 2 Academy of capacity 1, a priced course that needs that room and a
    completed research."""
    fs = fake_state
    world = fs.World(account=fs.Account(credits=credits), resources=fs.Resources(mineral=mineral, gas=gas),
                     typed_resource_items=True, strict_training_rules=True)
    world.room_designs = [fs.RoomDesign("901", "Academy", "Training", level=2, capacity=1)]
    world.rooms = [fs.Room("11", "901")]
    world.research_designs = [fs.ResearchDesign("61", "61", "Fitness 101", 1000)]
    world.researches = [fs.Research("61")]
    world.character_designs = [fs.CharacterDesign("31", "Synthetic Cadet")]
    world.characters = [fs.Character("1001", "Fixer", "31", "11", stats=fs.stats(Repair=30)),
                        fs.Character("1002", "Second", "31", "11", stats=fs.stats(Repair=10))]
    world.training_designs = [fs.TrainingDesign("41", "Manual", "RepairImprovement", 2, 10, datetime.timedelta(hours=1),
                                                price=price, rank=1, required_room_type="Academy",
                                                required_room_level=2, required_research_id="61")]
    return world


def _self_check_case(world_args: Mapping, edit: Callable | None, refusal: str | None,
                     wallet: tuple[int, int, int]) -> list[str]:
    world = _self_check_world(**{**SELF_CHECK_WORLD, **world_args})
    if edit is not None:
        edit(world)
    before = (world.resources.mineral, world.resources.gas, world.account.credits)
    fake = FakePss(world)
    session = fake.session()
    try:
        login = session.post(f"{SELF_CHECK_BASE}/UserService/DeviceLogin17",
                             json={"RefreshToken": world.account.refresh_token})
        token = re.search(r'accessToken="([^"]+)"', login.text)
        if token is None:
            return ["no access token from DeviceLogin17"]
        query = urllib.parse.urlencode({"trainingDesignId": "41", "characterId": "1001",
                                        "trainingStartDate": fake_state.fmt(fake.now), "accessToken": token.group(1)})
        reply = session.post(f"{SELF_CHECK_BASE}/TrainingService/AddTraining?{query}").text
    finally:
        session.close()
    problems = []
    applies = refusal is None
    if ("errorMessage" not in reply) != applies:
        problems.append(f"AddTraining {'refused' if applies else 'applied'}: {reply[:120]!r}")
    elif not applies and f'errorMessage="{refusal}"' not in reply:
        problems.append(f"AddTraining refusal text {reply[:120]!r}, expected {refusal!r}")
    after = (world.resources.mineral, world.resources.gas, world.account.credits)
    if after != wallet:
        problems.append(f"wallet (mineral, gas, starbux) {after}, expected {wallet}")
    entries = [entry for entry in fake.ledger if entry.path == "/TrainingService/AddTraining"]
    delta = {kind: wallet[index] - before[index] for index, kind in enumerate(("mineral", "gas", "starbux"))}
    if len(entries) != 1:
        problems.append(f"{len(entries)} AddTraining ledger entries, expected 1")
    else:
        got = (entries[0].applied, entries[0].refusal, entries[0].delta)
        if got != (applies, refusal, delta):
            problems.append(f"ledger entry (applied, refusal, delta) {got}, expected {(applies, refusal, delta)}")
    started = world.characters[0].session
    if (started is not None and started.design_id == "41") != applies:
        problems.append("crew session " + ("not started" if applies else "started"))
    problems += [f"fake violation: {violation}" for violation in fake.violations]
    return problems


def fake_self_check() -> list[str]:
    """``<check>: <problem>`` for every way the fake's strict pricing and refusal rules (World.strict_training_rules)
    break: what the gate scores depends on them, and in the red stage no control charges a priced course, so a broken
    rule would leave the suite's results unchanged. [] when every check holds."""
    failures = []
    for name, world_args, edit, refusal, wallet in SELF_CHECKS:
        try:
            failures += [f"{name}: {problem}" for problem in _self_check_case(world_args, edit, refusal, wallet)]
        except Exception as error:  # a broken fake must stop the gate, not crash it
            failures.append(f"{name}: raised {error!r}")
    return failures


# --- determinism and CLI -------------------------------------------------------------------------------------------


def child_hash_seed() -> str:
    """A fixed PYTHONHASHSEED for the determinism child that differs from this process's: 1, or 2 under 1."""
    return "2" if os.environ.get("PYTHONHASHSEED") == "1" else "1"


def child_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, os.path.abspath(__file__), "--report-only", "--json", "-", "--quiet", "--no-probe",
               "--suite", args.suite]
    for case_id in args.case or ():
        command += ["--case", case_id]
    if args.control_seeds:
        command += ["--control-seeds", str(args.control_seeds)]
    return command


class ChildRun:
    """The determinism child (scripts/training_benchmark.py's pattern): the same suite in its own process under
    another fixed PYTHONHASHSEED, started at once so it runs while this process runs the suite."""

    def __init__(self, command: Sequence[str], popen=subprocess.Popen, timeout: float = CHILD_TIMEOUT) -> None:
        self.timeout = timeout
        env = dict(os.environ, PYTHONHASHSEED=child_hash_seed(), PYTHONDONTWRITEBYTECODE="1", **{CHILD_ENV: "1"})
        self.process = popen(list(command), cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    def report(self) -> str:
        try:
            output, errors = self.process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            raise OracleError(f"determinism child: no report after {self.timeout:g} s") from None
        if self.process.returncode not in (EXIT_OK, EXIT_GATE):
            tail = (errors or "").strip().splitlines()[-1:] or [""]
            raise OracleError(f"determinism child: exit {self.process.returncode}: {tail[0]}")
        return output

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.communicate()


def check_determinism(suites: Mapping, *, start_child: Callable[[], ChildRun] | None, **kwargs) -> tuple[bool, dict]:
    """The suite twice in this process (the first with probes) and once in the child: deterministic when every
    scored report, behavior digests included, is byte-identical."""
    child = None if start_child is None else start_child()
    try:
        first = run_suite(suites, **kwargs)
        expected = to_json(scored_view(first))
        same = to_json(scored_view(run_suite(suites, **{**kwargs, "probe": False}))) == expected
        if child is not None:
            same = same and to_json(scored_view(json.loads(child.report()))) == expected
    finally:
        if child is not None:
            child.close()
    return same, first


# --- seeded defects (S14-AC7) --------------------------------------------------------------------------------------
#
# Each catalogued defect is seeded into production at call time (``unittest.mock.patch`` on the attribute production
# looks up when it runs, never a source edit) and replayed against named frozen traces. A defect is detected on a
# trace only when every expected named assertion and hard code shows, with no harness error and no unexpected crash.
# Two channels: ``benchmark`` targets are the frozen required, holdout and control traces; ``derived`` targets are
# variants built here in code from a frozen scenario (never written to a fixture), which must pass unpatched first.
# The gate never runs with a defect active: ``main`` refuses, ``child_command`` and ``ChildRun`` carry none, and a
# defect reaches a processes-mode ``--dispatch-child`` only through its spec JSON while one is active here.

DEFECTS_SCHEMA = "tachikoma.training-workflow-defects/1"
HARD_CODES = ("account_crossover", "premium_spend", "budget_spend", "duplicate_write", "false_verification")
_ACTIVE_DEFECT: str | None = None
_ADD_MUTATION = f"mutations.{ADD_TRAINING}"
_ADD_APPLIED = f"applied.{ADD_TRAINING}"


@dataclass(frozen=True)
class DefectTarget:
    """One trace a seeded defect must change: ``expect`` names ``<step>:<assertion>`` (a trailing ``*`` is an
    fnmatch glob, used only where the label is an in-game name) and ``hard`` names ``<step>:<hard code>``."""

    suite: str
    case_id: str
    scenario_id: str
    expect: tuple[str, ...] = ()
    hard: tuple[str, ...] = ()
    control: bool = False  # the case's training-v1 control scenario (its policy and control skip codes)
    derive: tuple[str, Callable[[dict], None]] | None = None  # (id suffix, in-place edit of a deep copy)

    @property
    def channel(self) -> str:
        return "derived" if self.derive else "benchmark"

    @property
    def label(self) -> str:
        suffix = f"~{self.derive[0]}" if self.derive else ""
        return f"{self.suite}/{self.case_id}/{self.scenario_id}{suffix}"


@dataclass(frozen=True)
class SeededDefect:
    id: str
    title: str  # public text: no in-game names
    critical: bool  # critical defects must be detected on every trace; the others' rates are published only
    seeding: Callable[[], Any]  # zero-argument factory of a context manager; originals are captured on enter
    targets: tuple[DefectTarget, ...]
    note: str = ""

    @contextlib.contextmanager
    def active(self):
        global _ACTIVE_DEFECT
        if _ACTIVE_DEFECT is not None:
            raise OracleError(f"seeded defect {_ACTIVE_DEFECT} is already active; {self.id} cannot join it")
        _ACTIVE_DEFECT = self.id
        try:
            with self.seeding():
                yield self
        finally:
            _ACTIVE_DEFECT = None


def active_defect() -> str | None:
    return _ACTIVE_DEFECT


def dispatch_defect_field() -> dict:
    """The dispatch-child spec's defect field: empty (so the spec JSON is unchanged) unless a defect is active."""
    return {} if _ACTIVE_DEFECT is None else {"defect": _ACTIVE_DEFECT}


def _module(name: str):
    """Import ``name`` for seeding; a first import happens in a scratch directory (run.py opens its log file in
    the working directory) after the sdk pre-import."""
    if name not in sys.modules:
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="twb-import-") as scratch:
            os.chdir(scratch)
            try:
                preimport_sdk()
                importlib.import_module(name)
            finally:
                os.chdir(cwd)
    return sys.modules[name]


def _swap(module_name: str, dotted: str, make: Callable[[Any], Any]) -> Callable[[], Any]:
    """A seeding factory: on enter, replace ``module.Owner.attr`` with ``make(original)``; restore it on exit."""
    @contextlib.contextmanager
    def seeding():
        owner = _module(module_name)
        *path, attr = dotted.split(".")
        for part in path:
            owner = getattr(owner, part)
        with patch.object(owner, attr, make(getattr(owner, attr))):
            yield

    return seeding


def _set(module_name: str, dotted: str, value: Any) -> Callable[[], Any]:
    return _swap(module_name, dotted, lambda original: value)


def _stack(*factories: Callable[[], Any]) -> Callable[[], Any]:
    @contextlib.contextmanager
    def seeding():
        with contextlib.ExitStack() as stack:
            for factory in factories:
                stack.enter_context(factory())
            yield

    return seeding


def _nothing(*args, **kwargs) -> None:
    return None


class _NoLock:
    def release(self) -> None:
        pass


def _send_twice(original):
    def add_training(self, *args, **kwargs):
        original(self, *args, **kwargs)
        return original(self, *args, **kwargs)

    return add_training


@contextlib.contextmanager
def _intent_after_send():
    """The intent written only after the send: the write is held back and made just before the settle."""
    runner = _module("sdk.execution.runner")
    settle, held = runner.settle_training_intent, {}

    def write_later(store, alias, intent):
        held[(alias, intent.intent_id)] = intent

    def settle_after_writing(store, alias, intent_id, settlement):
        intent = held.pop((alias, intent_id), None)
        if intent is not None:
            store.write_intent(alias, intent)
        return settle(store, alias, intent_id, settlement)

    with patch.object(runner, "write_training_intent", write_later), \
            patch.object(runner, "settle_training_intent", settle_after_writing):
        yield


def _fresh_on(error_name: str) -> Callable[[], Any]:
    """Missing or unreadable established state treated as a fresh account's store."""
    def make(original):
        attempts = _module("sdk.fleet.attempts")
        caught, seen = getattr(attempts, error_name), {}

        def load(self, alias, identity=None):
            if identity:
                seen[alias] = identity
            try:
                return original(self, alias, identity)
            except caught:
                return attempts.virgin_store(alias, identity or seen.get(alias),
                                             datetime.datetime.now(datetime.timezone.utc))

        return load

    return _swap("sdk.fleet.attempts", "_StoreBase.load", make)


def _lose_pending(original):
    runner, attempts = _module("sdk.execution.runner"), _module("sdk.fleet.attempts")

    def settle(self, guard, intent, result):
        try:
            return original(self, guard, intent, result)
        except runner.AttemptSettlementError:
            state = guard.store.load(self.account_alias)
            kept = tuple(item for item in state.attempts if item.intent_id != intent.intent_id)
            guard.store._replace(self.account_alias, attempts.dump_store(dataclasses.replace(state, attempts=kept)))
            raise

    return settle


def _guarded(value: bool) -> Callable[[], Any]:
    return _stack(_set("sdk.fleet.factory", "guarded_training", lambda policy: value),
                  _set("sdk.fleet.attempts", "guarded_training", lambda policy: value))


def _blocker(adjust: Callable) -> Callable[[], Any]:
    def make(original):
        return lambda course, room, inputs, policy: adjust(original, course, room, inputs, policy)

    return _swap("sdk.training.planner", "course_blocker", make)


def _premium_allowed(original, course, room, inputs, policy):
    found = original(course, room, inputs, policy)
    return None if found is not None and found[0] == "premium_currency" else found


def _funds(delta: int) -> Callable:
    def adjust(original, course, room, inputs, policy):
        funds = {kind: (None if value is None else value + delta) for kind, value in inputs.funds.items()}
        return original(course, room, dataclasses.replace(inputs, funds=funds), policy)

    return adjust


def _always_verified(original):
    verified = _module("sdk.actions").ReconciliationStatus.VERIFIED
    return dataclasses.replace(original, postcondition=lambda *args: (verified, "seeded"))


def _price(change: Callable) -> Callable[[], Any]:
    def make(original):
        model = _module("sdk.training.model")
        return lambda value: change(model, original(value))

    return _swap("sdk.training.parse", "parse_course_price", make)


def _price_shift(delta: int) -> Callable:
    def change(model, price):
        if price.status != model.PRICE_OK:
            return price
        return model.CoursePrice(model.PRICE_OK, price.currency, max(0, price.amount + delta))

    return change


def _price_as_mineral(model, price):
    return model.CoursePrice(model.PRICE_OK, "mineral", price.amount) if price.status == model.PRICE_OK else price


def _zero_price_unknown(model, price):
    if price.status == model.PRICE_OK and price.amount == 0:
        return model.CoursePrice(model.PRICE_UNAVAILABLE)
    return price


def _zero_stat_unknown(original):
    def observe(snapshot, member):
        found = original(snapshot, member)
        if found.stats is not None and 0 in found.stats.values():
            return dataclasses.replace(found, stats=None)
        return found

    return observe


def _role_assignment(planner, crew, roles: Mapping[str, Any], uncovered: Sequence[str]):
    unknown = tuple(sorted(member.character_id for member in crew if member.stats is None))
    return planner.RoleAssignment(dict(roles), tuple(uncovered), unknown)


def _greedy_roles(original):
    """Global greedy: the highest (score, then lowest id) crew-role pair first, each role and crew member once."""
    planner = _module("sdk.training.planner")

    def assign(crew, policy):
        known = [member for member in crew if member.stats is not None]
        pairs = sorted(((member.stats[stat], word, member.character_id) for word, stat in policy.roles
                        for member in known), key=lambda pair: (-pair[0], pair[2]))
        roles, used = {}, set()
        for _score, word, character_id in pairs:
            if word not in roles and character_id not in used:
                roles[word] = character_id
                used.add(character_id)
        return _role_assignment(planner, crew, roles, [word for word, _ in policy.roles if word not in roles])

    return assign


def _tie_high_id(original):
    def key(roles, choice):
        found = original(roles, choice)
        return (found[0], found[1], sorted((score, -ident) for score, ident in found[2]), found[3],
                tuple(-ident for ident in found[4]))

    return key


def _tie_input_order(original):
    """Equal scores tied by the roster reply's order: the planner's choice key without its instance-id parts."""
    planner = _module("sdk.training.planner")

    def assign(crew, policy):
        known = [member for member in crew if member.stats is not None]
        slots = len(policy.roles)
        candidates = tuple(tuple(sorted(known, key=lambda member, stat=stat: -member.stats[stat])[:slots])
                           for _, stat in policy.roles)

        def key(choice):
            found = planner._assignment_key(policy.roles, choice)
            return (found[0], found[1], [score for score, _ in found[2]], found[3])

        choice = min(planner._assignment_choices(candidates, ()), key=key)
        pairs = tuple(zip(policy.roles, choice))
        return _role_assignment(planner, crew, {word: member.character_id for (word, _), member in pairs
                                                if member is not None},
                                [word for (word, _), member in pairs if member is None])

    return assign


def _one_copy(original):
    """A recipe needing two copies of one design counted as needing one."""
    prestige = _module("sdk.training.prestige")

    def match(recipe, roster):
        ingredients = tuple(sorted((recipe.first_design_id, recipe.second_design_id)))
        if roster is None:
            return prestige.PrestigeRoute(recipe.to_design_id, ingredients, None, None)
        missing = unknown = 0
        for design_id in sorted(set(ingredients)):
            ready = sum(1 for entry in roster if entry.design_id == design_id and entry.eligible is True)
            maybe = sum(1 for entry in roster if entry.design_id == design_id and entry.eligible is None)
            short = max(0, 1 - ready)
            missing += short
            unknown += min(short, maybe)
        undesigned = sum(1 for entry in roster if entry.design_id is None and entry.eligible is not False)
        unknown += min(missing - unknown, undesigned)
        return prestige.PrestigeRoute(recipe.to_design_id, ingredients, missing, unknown)

    return match


def _wrong_stat(original):
    planner = _module("sdk.training.planner")

    def choose(stat, room, inputs, policy):
        for course in sorted((c for c in inputs.courses or () if c.stat != stat), key=lambda c: c.design_id):
            if planner.course_blocker(course, room, inputs, policy) is None:
                return course
        return original(stat, room, inputs, policy)

    return choose


def trigger_after(nth: int) -> Callable[[dict], None]:
    """A derived variant's edit: every step trigger fires after the ``nth`` reply of its route instead."""
    def edit(scenario: dict) -> None:
        triggers = [trigger for step in scenario["steps"] for trigger in step.get("triggers", ())]
        if not triggers:
            raise ValueError(f"{scenario['id']}: no step trigger to move")
        for trigger in triggers:
            trigger["after_nth"] = nth

    return edit


def derived_scenario(scenario: Mapping, derive: tuple[str, Callable[[dict], None]]) -> dict:
    """A deep copy of a frozen scenario with ``derive``'s edit applied and ``~suffix`` on its id; never on disk."""
    suffix, edit = derive
    entry = copy.deepcopy(dict(scenario))
    edit(entry)
    entry["id"] = f"{scenario['id']}~{suffix}"
    return entry


def target_scenario(target: DefectTarget, suites: Mapping[str, Mapping]) -> tuple[dict, list[str], str | None]:
    """(scenario, skip codes, policy override) for a target, exactly as run_suite would score that trace."""
    data = suites[target.suite]
    found = next((item for item in data["cases"] if item["id"] == target.case_id), None)
    if found is None:
        raise FixtureError(f"defect target {target.label}: no such case")
    pool = found["control"]["scenarios"] if target.control else found["scenarios"]
    scenario = next((item for item in pool if item["id"] == target.scenario_id), None)
    if scenario is None:
        raise FixtureError(f"defect target {target.label}: no such scenario")
    codes = list(data["vocabulary"]["skip_codes"])
    policy = None
    if target.control:
        codes = sorted(set(codes) | set(data["vocabulary"]["control_skip_codes"]))
        policy = found["control"]["policy_id"]
    if target.derive:
        scenario = derived_scenario(scenario, target.derive)
    return scenario, codes, policy


@dataclass
class TargetResult(TraceResult):
    """A defect target's trace result plus ``crashes``: every production exception at any step's raw exit, read
    whether or not the step freezes an exit (several execute and every overlap step freeze none)."""

    crashes: list[str] = field(default_factory=list)


def production_exceptions(step_id: str, raw: Mapping) -> list[str]:
    """``<step>:exit error <Type>`` for each exit of a raw step record (an overlap step's exit lists its dispatches'
    exits) that is a production exception. An armed ``crashed`` exit is the scenario's own fault and is compared."""
    exits = raw.get("exit")
    return [f"{step_id}:exit {code}" for code in (exits if isinstance(exits, list) else [exits])
            if isinstance(code, str) and code.startswith("error ")]


def run_target(target: DefectTarget, suites: Mapping[str, Mapping], seed: int) -> TargetResult:
    """One target at one seed; a harness exception (a dispatch child's OracleError included) is an error, so it
    never counts as a detection. Each step's raw record is read for production exceptions on the way through."""
    crashes: list[str] = []
    step = run_step

    def observed_step(ctx: ScenarioRun, entry: Mapping) -> dict | None:
        raw = step(ctx, entry)
        if raw is not None:
            crashes.extend(production_exceptions(entry["id"], raw))
        return raw

    try:
        scenario, codes, policy = target_scenario(target, suites)
        with patch.object(sys.modules[__name__], "run_step", observed_step):  # run_scenario looks it up per step
            result = run_scenario(target.suite, target.case_id, scenario, suites[target.suite]["catalog"], seed,
                                  codes, policy_override=policy)
    except Exception as error:  # noqa: BLE001 - recorded, never detected
        return TargetResult(False, [], [], [], [f"harness raised {type(error).__name__}: {error}"])
    fields = {item.name: getattr(result, item.name) for item in dataclasses.fields(result)}
    return TargetResult(**fields, crashes=crashes)


def _named(result: TraceResult) -> set[str]:
    return {f"{item['step']}:{item['assertion']}" for item in result.unmet}


def _hard(result: TraceResult) -> set[str]:
    return {":".join(violation.split(": ")[:2]) for violation in result.hard_violations}


def detection(target: DefectTarget, result: TraceResult) -> tuple[bool, list[str]]:
    """(detected, what is missing): every expected assertion and hard code shown, no error and no unexpected crash
    or production exception at any step's exit."""
    named, hard = _named(result), _hard(result)
    missing = [want for want in target.expect
               if not (any(fnmatch.fnmatchcase(name, want) for name in named) if want.endswith("*") else want in named)]
    missing += [want for want in target.hard if want not in hard]
    crashed = bool(getattr(result, "crashes", ())) or any(item["assertion"] == "exit" and (
        item["actual"] == "crashed" or str(item["actual"]).startswith("error ")) for item in result.unmet)
    found = missing + (["errors"] if result.errors else []) + (["crashed"] if crashed else [])
    return not found, found


def defect(defect_id: str) -> SeededDefect:
    for item in SEEDED_DEFECTS:
        if item.id == defect_id:
            return item
    raise KeyError(defect_id)


def _required(*args, **kwargs) -> DefectTarget:
    return DefectTarget("required", *args, **kwargs)


def _holdout(*args, **kwargs) -> DefectTarget:
    return DefectTarget("holdout", *args, **kwargs)


_STALE = (("room-upgrading", "nth2", 2, "room_not_ready"), ("crew-moved", "nth2", 2, "not_in_training_room"),
          ("research-removed", "nth2", 2, "research_missing"), ("crew-fatigued", "nth2", 2, "fatigued"),
          ("price-raised", "nth3", 3, "insufficient_funds"))
_RECHECK_OFF = _set("sdk.fleet.factory", "start_refusal", _nothing)
_TIE = _holdout("tie-break-instance-id", "equal-repair", expect=("plan:crew_plan.tia", "dry:journal_planned"))

SEEDED_DEFECTS: tuple[SeededDefect, ...] = (
    SeededDefect("two-starts", "two training starts sent on one account in one run", True,
                 _swap("sdk.client", "Client.addTraining", _send_twice), (
                     _required("paid-boundary", "exact-500", expect=(f"exec:{_ADD_MUTATION}",),
                                  hard=("exec:duplicate_write",)),
                     _holdout("gas-priced-course", "gas-300", expect=(f"exec:{_ADD_MUTATION}",),
                                  hard=("exec:duplicate_write",)))),
    SeededDefect("guard-latch-off", "the guard's one-start latch removed", False,
                 _set("sdk.fleet.attempts", "TrainingGuard.mark_sent", _nothing), (
                     _required("paid-boundary", "exact-500", expect=(f"exec:{_ADD_MUTATION}",),
                                  hard=("exec:duplicate_write",)),
                     _required("overlapping-dispatch", "threads", expect=("overlap:dispatches",))),
                 note="masked by the planner's one-start rule: a crew plan never holds two starts, so the latch "
                      "is unreachable without also seeding the planner"),
    SeededDefect("lock-skipped", "the account lock skipped (threads and processes)", True,
                 _set("sdk.fleet.attempts", "FileAccountLock.try_acquire", lambda *args, **kwargs: _NoLock()), (
                     _required("overlapping-dispatch", "threads", expect=("overlap:dispatches",)),
                     _required("distinct-accounts", "alpha-held", expect=("overlap:dispatches",)),
                     _required("overlapping-dispatch", "processes", expect=("overlap:dispatches",))),
                 note="without the lock, whether the second dispatch's start lands (the duplicate-write and budget "
                      "hard codes) or its store write is refused depends on timing in both modes, so only the "
                      "dispatch summary is expected"),
    SeededDefect("intent-not-persisted", "the intent never written before the send", True,
                 _set("sdk.execution.runner", "write_training_intent", _nothing), (
                     _required("persisted-intent-restart", "crash-after-send",
                                  expect=(f"exec2:{_ADD_MUTATION}", "exec2:resource_delta.alpha"),
                                  hard=("exec2:duplicate_write", "exec2:budget_spend")),),
                 note="on paid-boundary/exact-500 only the exit changes (the settle finds no intent): not specific "
                      "enough to be a target"),
    SeededDefect("intent-after-send", "the intent written only after the send (reorder)", True, _intent_after_send, (
        _required("persisted-intent-restart", "crash-after-send",
                     expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.unresolved_attempt"),
                     hard=("exec2:duplicate_write", "exec2:budget_spend")),
        _required("persisted-intent-restart", "crash-before-apply",
                     expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.unresolved_attempt"),
                     hard=("exec2:duplicate_write", "exec2:budget_spend"))),
                 note="intent-write-fails is not a target: its armed fault replaces the intent write itself"),
    SeededDefect("stop-ignored", "the stop control ignored (run.py and scheduler)", True,
                 _set("run", "FLEET_STOP_FILE", "STOP-IGNORED"), (
                     _required("store-stop-controls", "stop", expect=(f"exec:{_ADD_MUTATION}",
                                                                            "exec:requests.alpha")),
                     _required("store-stop-controls", "stop-mid-run",
                                  expect=(f"exec:{_ADD_MUTATION}", "exec:skips.alpha.stopped"),
                                  hard=("exec:budget_spend",)))),
    SeededDefect("scheduler-stop-ignored", "the scheduler's between-actions stop check ignored", True,
                 _set("sdk.scheduler.daily", "DailyScheduler._stop_requested", lambda self: False), (
                     _required("store-stop-controls", "stop-mid-run",
                                  expect=(f"exec:{_ADD_MUTATION}", "exec:skips.alpha.stopped"),
                                  hard=("exec:budget_spend",)),),
                 note="store-stop-controls/stop is not a target: run.py refuses before login"),
    SeededDefect("missing-store-fresh", "missing established state treated as a fresh account", True,
                 _fresh_on("StoreMissing"), (
                     _required("store-stop-controls", "missing-state",
                                  expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.state_missing",
                                          "exec2:store.alpha"),
                                  hard=("exec2:budget_spend",)),),
                 note="store-stop-controls/unprovisioned is not a target: the dispatch refuses before any load"),
    SeededDefect("unreadable-store-fresh", "unreadable state treated as a fresh account", True,
                 _fresh_on("StoreUnreadable"), (
                     _required("store-stop-controls", "unreadable-state",
                                  expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.state_unreadable"),
                                  hard=("exec2:budget_spend",)),)),
    SeededDefect("settle-loses-pending", "a settlement failure drops the pending record", True,
                 _swap("sdk.execution.runner", "ActionRunner._settle", _lose_pending), (
                     _required("persisted-intent-restart", "settle-write-fails",
                                  expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.unresolved_attempt"),
                                  hard=("exec2:duplicate_write", "exec2:budget_spend")),)),
    SeededDefect("v1-under-v2-path", "a training-v1 account run under the training-v2 lock and store", True,
                 _guarded(True), (
                     _required("paid-boundary", "v1-unpriced-start", control=True,
                                  expect=(f"exec:{_ADD_MUTATION}", f"exec:{_ADD_APPLIED}", "exec:journal")),)),
    SeededDefect("v2-down-v1-path", "a training-v2 account sends writes down the unguarded training-v1 path", True,
                 _guarded(False), (
                     _required("overlapping-dispatch", "threads",
                                  expect=("overlap:dispatches", "provision:store.alpha")),
                     _required("persisted-intent-restart", "crash-after-send",
                                  expect=(f"exec2:{_ADD_MUTATION}", "provision:store.alpha"),
                                  hard=("exec2:duplicate_write",))),
                 note="threads mode: the duplicate-write hard code depends on thread timing, so only the dispatch "
                      "summary and the store assertion are expected there"),
    SeededDefect("unresolved-guard-only", "an unresolved attempt ignored by the guard (the store still refuses)", True,
                 _set("sdk.fleet.attempts", "block_reason", _nothing), (
                     _required("reconciliation", "missing-post-read",
                                  expect=("exec2:skips.alpha.unresolved_attempt",)),
                     _holdout("retry-different-crew", "lost-reply",
                                  expect=("exec2:skips.alpha.unresolved_attempt",)))),
    SeededDefect("unresolved-ignored", "an unresolved attempt ignored; a retry goes out for a different crew member "
                 "after an ambiguous paid call", True, _set("sdk.fleet.attempts", "StoreState.blocking", _nothing), (
                     _holdout("retry-different-crew", "lost-reply",
                                  expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.unresolved_attempt"),
                                  hard=("exec2:duplicate_write", "exec2:budget_spend")),
                     _required("reconciliation", "missing-post-read",
                                  expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.unresolved_attempt"),
                                  hard=("exec2:duplicate_write", "exec2:budget_spend")),
                     _required("persisted-intent-restart", "crash-after-send",
                                  expect=(f"exec2:{_ADD_MUTATION}", "exec2:skips.alpha.unresolved_attempt"),
                                  hard=("exec2:duplicate_write", "exec2:budget_spend")))),
    SeededDefect("premium-spend", "a premium-currency course allowed", True, _blocker(_premium_allowed), (
        _required("paid-boundary", "premium", expect=(f"exec:{_ADD_MUTATION}",
                                                            "exec:skips.alpha.premium_currency.fixer"),
                     hard=("exec:premium_spend",)),)),
    SeededDefect("false-verification", "a start verified without the requested course and timer", True,
                 _swap("sdk.fleet.factory", "STRICT_ADD_TRAINING", _always_verified), (
                     _required("reconciliation", "different-course", expect=("exec1:journal",),
                                  hard=("exec1:false_verification",)),
                     _required("reconciliation", "missing-id", expect=("exec1:journal",),
                                  hard=("exec1:false_verification",)))),
    SeededDefect("funds-plus-one", "499 spendable treated as 500", True, _blocker(_funds(1)), (
        _required("paid-boundary", "short-499", expect=(f"exec:{_ADD_MUTATION}",
                                                              "exec:skips.alpha.insufficient_funds.fixer")),)),
    SeededDefect("funds-minus-one", "exactly 500 spendable refused", True, _blocker(_funds(-1)), (
        _required("paid-boundary", "exact-500", expect=(f"exec:{_ADD_MUTATION}", f"exec:{_ADD_APPLIED}",
                                                              "exec:resource_delta.alpha")),)),
    SeededDefect("reservation-ignored", "an earlier reservation not deducted from the remainder", True,
                 _swap("sdk.policy.training", "remaining_budget",
                       lambda original: lambda snapshot, config, requests: original(snapshot, config, ())), (
                     _required("paid-boundary", "earlier-reservation",
                                  expect=("dry:journal_planned", "exec:skips.alpha.insufficient_funds.fixer")),
                     _required("paid-boundary", "spend-cap-after-earlier",
                                  expect=("exec:skips.alpha.cap_exceeded.fixer",)))),
    SeededDefect("recheck-off", "the under-lock recheck disabled (stale crew, room, research or price)", True,
                 _RECHECK_OFF, tuple(
                     _required("stale-preflight", scenario_id, derive=(suffix, trigger_after(nth)),
                                  expect=(f"exec:{_ADD_MUTATION}", f"exec:skips.alpha.{code}.fixer"))
                     for scenario_id, suffix, nth, code in _STALE),
                 note="derived variants: each frozen trigger moved to a later roster read, after the plan read, so "
                      "only the under-lock recheck can see the change"),
    SeededDefect("recheck-off-frozen", "the recheck disabled, measured on the frozen stale fixtures", False,
                 _RECHECK_OFF, tuple(_required("stale-preflight", scenario_id, expect=(f"exec:{_ADD_MUTATION}",))
                                     for scenario_id, *_ in _STALE),
                 note="the frozen triggers fire at the identity read, so the plan read already sees the change and "
                      "refuses; the derived channel (recheck-off) covers the recheck"),
    SeededDefect("price-under", "the course price read 100 low", True, _price(_price_shift(-100)), (
        _required("paid-boundary", "short-499", expect=(f"exec:{_ADD_MUTATION}",
                                                              "exec:skips.alpha.insufficient_funds.fixer")),),
                 note="paid-boundary/exact-500 is not a target: an underestimate still sends and is charged the real "
                      "price"),
    SeededDefect("price-over", "the course price read 1 high", True, _price(_price_shift(1)), (
        _required("paid-boundary", "exact-500", expect=(f"exec:{_ADD_MUTATION}", f"exec:{_ADD_APPLIED}")),)),
    SeededDefect("currency-swapped", "a gas price read as minerals", True, _price(_price_as_mineral), (
        _holdout("gas-priced-course", "gas-300", expect=(f"exec:{_ADD_MUTATION}", f"exec:{_ADD_APPLIED}",
                                                                "exec:resource_delta.alpha")),)),
    SeededDefect("zero-price-unknown", "an observed zero price treated as unknown", True, _price(_zero_price_unknown), (
        _required("paid-boundary", "explicit-zero", expect=(f"exec:{_ADD_MUTATION}", f"exec:{_ADD_APPLIED}")),)),
    SeededDefect("zero-stat-unknown", "an observed zero stat treated as unknown", True,
                 _swap("sdk.policy.training", "_crew_observation", _zero_stat_unknown), (
                     _holdout("observed-zero-stat", "missing-versus-zero",
                                  expect=("plan:crew_plan.obi", "dry:journal_planned")),)),
    SeededDefect("greedy-roles", "greedy role allocation by highest score leaves the repair specialist unassigned",
                 True, _swap("sdk.training.planner", "assign_roles", _greedy_roles), (
                     _required("role-coverage", "swap", expect=("plan1:crew_plan.ana", "plan1:crew_plan.ben",
                                                                      "plan2:crew_plan.ana", "plan2:crew_plan.ben")),)),
    SeededDefect("tie-high-id", "an equal-score tie broken toward the higher instance id", True,
                 _swap("sdk.training.planner", "_assignment_key", _tie_high_id), (_TIE,)),
    SeededDefect("tie-input-order", "an equal-score tie broken by roster reply order", False,
                 _swap("sdk.training.planner", "assign_roles", _tie_input_order), (
                     _holdout("tie-break-instance-id", "equal-repair", expect=("plan:crew_plan.tia",)),),
                 note="caught only at the seeds whose reply order puts the higher instance id first (the seed "
                      "permutes reply order)"),
    SeededDefect("two-copy-as-one", "a recipe needing two identical designs counted as needing one", True,
                 _swap("sdk.training.prestige", "match_recipe", _one_copy), (
                     _holdout("two-copy-recipe", "one-training", expect=("plan:prestige.*",)),
                     _required("preview-prestige", "one-copy", expect=("plan:prestige.*",))),
                 note="two-copy-recipe/two-idle is not a target: both copies are present there"),
    SeededDefect("wrong-stat-course", "a course training another stat chosen", True,
                 _swap("sdk.training.planner", "choose_course", _wrong_stat), (
                     _required("role-coverage", "swap", expect=("plan1:crew_plan.ana", "dry1:journal_planned")),
                     _required("preview-prestige", "one-copy",
                                  expect=("plan:crew_plan.cora", "dry:journal_planned")))),
)


@contextlib.contextmanager
def _scratch_cwd():
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="twb-defects-") as scratch:
        os.chdir(scratch)
        try:
            yield
        finally:
            os.chdir(cwd)


def _rate(detected: int, total: int) -> str:
    return f"{(100.0 * detected / total) if total else 0.0:.6f}"


def run_defect(item: SeededDefect, suites: Mapping[str, Mapping]) -> dict:
    """The clean run of every target at every seed, then the same under the seeded defect."""
    clean, traces = [], []
    with _scratch_cwd():
        _module("run")
        for target in item.targets:
            for seed in suites[target.suite]["seeds"]:
                result = run_target(target, suites, seed)
                clean.append({"target": target.label, "channel": target.channel, "seed": seed, "pass": result.passed,
                              "unmet": sorted(_named(result)), "hard": sorted(_hard(result)),
                              "witnesses_missing": result.witnesses_missing, "errors": result.errors})
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(item.active())
            except OracleError:
                raise
            except Exception as error:  # a seeding naming a missing attribute: a harness error, never a run
                raise OracleError(f"seeded defect {item.id} could not be applied: {type(error).__name__}: "
                                  f"{error}") from error
            for target in item.targets:
                for seed in suites[target.suite]["seeds"]:
                    result = run_target(target, suites, seed)
                    detected, missing = detection(target, result)
                    traces.append({"target": target.label, "channel": target.channel, "seed": seed,
                                   "detected": detected, "missing": missing, "expect": list(target.expect),
                                   "hard_expected": list(target.hard), "unmet": sorted(_named(result)),
                                   "hard": sorted(_hard(result)), "errors": result.errors,
                                   "crashes": result.crashes})
    detected = sum(trace["detected"] for trace in traces)
    channels: dict[str, dict] = {}
    for trace in traces:
        entry = channels.setdefault(trace["channel"], {"detected": 0, "total": 0})
        entry["detected"] += trace["detected"]
        entry["total"] += 1
    return {"id": item.id, "title": item.title, "critical": item.critical, "note": item.note, "clean": clean,
            "traces": traces, "detected": detected, "total": len(traces), "rate": _rate(detected, len(traces)),
            "channels": channels, "survivor": not traces or detected < len(traces)}  # 0 of 0 measures nothing


def run_defects(ids: Sequence[str] | None = None) -> dict:
    """Every selected catalogued defect (all by default), in catalogue order, against the pinned fixtures."""
    suites = load_suites()
    verify_fixture_digests(list(suites))
    failures = fake_self_check()  # detections rest on the fake's strict rules, exactly as the gate's scores do
    if failures:
        raise OracleError("fake self-check failed: " + "; ".join(failures))
    results = [run_defect(item, suites) for item in SEEDED_DEFECTS if not ids or item.id in ids]
    critical = [result for result in results if result["critical"]]
    survivors = {"critical": [result["id"] for result in critical if result["survivor"]],
                 "non_critical": [result["id"] for result in results if not result["critical"] and result["survivor"]]}
    clean_failures = list(dict.fromkeys(f"{entry['target']}@{entry['seed']}" for result in results
                                        for entry in result["clean"] if not entry["pass"]))
    return {"schema": DEFECTS_SCHEMA, "seeds": {name: list(suite["seeds"]) for name, suite in suites.items()},
            "defects": results,
            "summary": {"critical": {"defects": len(critical),
                                     "detected_all_traces": sum(not result["survivor"] for result in critical)},
                        "survivors": survivors, "clean_failures": clean_failures}}


def defect_reasons(report: Mapping) -> list[str]:
    """Why ``--defects`` fails: a target failing unpatched, or a critical defect missed on any trace."""
    reasons = list(dict.fromkeys(f"clean: {entry['target']} seed={entry['seed']} fails unpatched"
                                 for result in report["defects"] for entry in result["clean"] if not entry["pass"]))
    reasons += [f"critical defect {result['id']} detected on {result['detected']} of {result['total']} traces"
                for result in report["defects"] if result["critical"] and result["survivor"]]
    if not report["defects"]:
        reasons.append("no seeded defect ran")
    return reasons


def defect_harness_errors(report: Mapping) -> list[str]:
    """Every harness error, clean or patched: a run that recorded one measured nothing, so it is never only a miss
    (a non-critical defect's published rate would otherwise hide it)."""
    clean = [f"{entry['target']} seed={entry['seed']}: {entry['errors'][0]}" for result in report["defects"]
             for entry in result["clean"] if entry["errors"]]
    return clean + [f"{result['id']} {trace['target']} seed={trace['seed']}: {trace['errors'][0]}"
                    for result in report["defects"] for trace in result["traces"] if trace["errors"]]


def render_defects(report: Mapping, reasons: Sequence[str]) -> str:
    lines = ["# SPEC-14 seeded defects", ""]
    for result in report["defects"]:
        for entry in result["clean"]:
            if entry["pass"]:
                lines.append(f"CLEAN PASS {entry['target']} seed={entry['seed']}")
            else:
                why = entry["unmet"] + entry["hard"] + entry["witnesses_missing"] + entry["errors"][:1]
                lines.append(f"CLEAN FAIL {entry['target']} seed={entry['seed']}: {', '.join(why)}")
        for trace in result["traces"]:
            where = f"{result['id']} {trace['target']} seed={trace['seed']} [{trace['channel']}]"
            if trace["detected"]:
                hard = f"; hard {', '.join(trace['hard_expected'])}" if trace["hard_expected"] else ""
                lines.append(f"DETECTED {where} by {', '.join(trace['expect'])}{hard}")
            else:
                lines.append(f"MISSED {where} missing {', '.join(trace['missing'])}")
    lines.append("")
    for result in report["defects"]:
        channels = ", ".join(f"{name} {entry['detected']}/{entry['total']}"
                             for name, entry in sorted(result["channels"].items()))
        line = (f"{result['id']} ({'critical' if result['critical'] else 'non-critical'}): detected "
                f"{result['detected']} of {result['total']} traces ({result['rate']}) [{channels}]")
        if result["survivor"]:
            line += " -- survivor" + (f": {result['note']}" if result["note"] else "")
        lines.append(line)
    lines += ["", f"Defects: {'FAIL' if reasons else 'PASS'}"]
    lines += [f"- {reason}" for reason in reasons]
    return "\n".join(lines) + "\n"


def _main_defects(args: argparse.Namespace) -> int:
    try:
        if _ACTIVE_DEFECT is not None:
            raise OracleError(f"a seeded defect is already active ({_ACTIVE_DEFECT})")
        unknown = sorted(set(args.defect or ()) - {item.id for item in SEEDED_DEFECTS})
        if unknown:
            raise FixtureError(f"unknown defect ids: {unknown}")
        report = run_defects(args.defect)
    except FixtureError as error:
        print(f"fixture error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except OracleError as error:
        print(f"oracle error: {error}", file=sys.stderr)
        return EXIT_ERROR
    reasons = defect_reasons(report)
    if args.json == "-":
        sys.stdout.write(to_json(report))
    elif args.json:
        Path(args.json).write_text(to_json(report), encoding="utf-8")
    if not args.quiet and args.json != "-":
        sys.stdout.write(render_defects(report, reasons))
    errors = defect_harness_errors(report)
    if errors:
        print("defect harness error: " + "; ".join(errors[:3]), file=sys.stderr)
        return EXIT_ERROR
    return EXIT_GATE if reasons else EXIT_OK


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gate", action="store_true", help="determinism + gate; exit 1 on any failure")
    mode.add_argument("--report-only", action="store_true", help="run once and print the report (exit 0 or 2)")
    mode.add_argument("--dispatch-child", metavar="SPEC", help=argparse.SUPPRESS)
    mode.add_argument("--defects", action="store_true", help="seed each catalogued defect; exit 1 unless every "
                      "critical one is caught on every trace")
    parser.add_argument("--json", metavar="PATH", help="write the JSON report to PATH ('-' for stdout)")
    parser.add_argument("--suite", choices=("required", "holdout", "all"), default="all")
    parser.add_argument("--case", action="append", metavar="ID", help="run only this case (repeatable); a subset "
                        "never passes the gate")
    parser.add_argument("--no-probe", action="store_true", help="skip the training-v1 probes")
    parser.add_argument("--control-seeds", type=int, metavar="N", help="run each control at its first N seeds only")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--defect", action="append", metavar="ID", help="with --defects: seed only this defect "
                        "(repeatable)")
    args = parser.parse_args(argv)
    if args.defect and not args.defects:
        parser.error("--defect needs --defects")
    if args.defects and (args.case or args.control_seeds or args.no_probe or args.suite != "all"):
        parser.error("--defects runs every catalogued target: --suite, --case, --control-seeds and --no-probe do "
                     "not apply")
    return args


def _emit(args: argparse.Namespace, report: Mapping, reasons: Sequence[str]) -> None:
    if args.json == "-":
        sys.stdout.write(to_json(report))
    elif args.json:
        Path(args.json).write_text(to_json(report), encoding="utf-8")
    if not args.quiet and args.json != "-":
        sys.stdout.write(render_text(report, reasons))


def main(argv: Sequence[str] | None = None, *, child: Callable[[Sequence[str]], Any] = ChildRun) -> int:
    args = parse_args(argv)
    if args.dispatch_child:
        return dispatch_child(args.dispatch_child)
    if args.defects:
        return _main_defects(args)
    try:
        if _ACTIVE_DEFECT is not None:
            raise OracleError(f"a seeded defect is active ({_ACTIVE_DEFECT}); --gate never runs with one")
        suites = load_suites(args.suite)
        known = {case["id"] for suite in suites.values() for case in suite["cases"]}
        unknown = sorted(set(args.case or ()) - known)
        if unknown:
            raise FixtureError(f"unknown case ids: {unknown}")
        kwargs = {"cases": args.case, "probe": not args.no_probe, "control_seed_limit": args.control_seeds}
        if args.gate:
            verify_fixture_digests(list(suites))
            failures = fake_self_check()
            if failures:
                raise OracleError("fake self-check failed: " + "; ".join(failures))
            in_child = os.environ.get(CHILD_ENV) == "1"
            starter = None if in_child else (lambda: child(child_command(args)))
            deterministic, report = check_determinism(suites, start_child=starter, **kwargs)
        else:
            deterministic, report = True, run_suite(suites, **kwargs)
    except FixtureError as error:
        print(f"fixture error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except OracleError as error:
        print(f"oracle error: {error}", file=sys.stderr)
        return EXIT_ERROR
    errors = fake_errors(report)
    if errors:
        print("fake error: " + "; ".join(errors[:3]), file=sys.stderr)
        _emit(args, report, ())
        return EXIT_ERROR
    if not args.gate:
        _emit(args, report, ())
        return EXIT_OK
    subset = bool(args.case) or bool(args.control_seeds) or args.suite != "all"
    reasons = gate_reasons(report, deterministic=deterministic, subset=subset)
    _emit(args, report, reasons)
    return EXIT_GATE if reasons else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
