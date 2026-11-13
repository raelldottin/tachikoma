#!/usr/bin/env python3
"""What changed on a trial account each day, from its evening snapshots (SPEC-07 support).

Reads baseline snapshot files (``<alias>-<UTC>.json``, as ``run.py --baseline``
writes them and the trial's ``trial-snapshot-*`` artifacts hold them), pairs
consecutive snapshots of each account, and prints one section per day:

- the day's block and arm, from ``--schedule`` (the trial's ``schedule.json``);
- the trial's growth measure for that day, computed exactly as the exit report
  computes it (``scripts/trial_report.growth_units``: room upgrades plus research
  completions);
- rooms upgraded, added and removed (by room design, never by room id), room
  upgrades started, research completed and started;
- the change in minerals, gas, starbux, trophies and ship level, and crew level-ups;
- the sections a snapshot did not observe, so an unknown is never shown as "no change".

A day is the date (UTC) of the later snapshot of the pair: the 23:30 UTC snapshot
closes that day. It reads files only: no game traffic, no network, no writes.

    python scripts/daily_changes.py DIR_OR_FILE... [--alias pss5] [--schedule PATH]
                                     [--from YYYY-MM-DD] [--to YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

# abspath, not resolve(): under Bazel this file is a runfiles symlink.
ROOT = Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.trial_report import DEFAULT_DAY_PAIR_HOURS, growth_units  # noqa: E402
from sdk.state import AccountSnapshot, Completeness, SnapshotDelta, diff_snapshots  # noqa: E402
from sdk.state.snapshot import SECTIONS  # noqa: E402
from sdk.state.baseline import BaselineError, load_baseline_file  # noqa: E402

RESEARCH_DONE = "Completed"
RESEARCH_RUNNING = "Researching"
UPGRADING = "Upgrading"


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Block:
    name: str
    start: date
    end: date
    arm: str


def load_schedule(path: Path | str) -> tuple[Block, ...]:
    """The trial's blocks: ``{"blocks": [{"name", "start", "end", "arm"}, ...]}``, dates inclusive, no overlaps."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScheduleError(f"cannot read the schedule {path}: {type(error).__name__}") from None
    entries = payload.get("blocks") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise ScheduleError("the schedule needs a non-empty blocks list")
    blocks = []
    for entry in entries:
        try:
            block = Block(str(entry["name"]), date.fromisoformat(entry["start"]), date.fromisoformat(entry["end"]),
                          str(entry["arm"]))
        except (KeyError, TypeError, ValueError):
            raise ScheduleError("each block needs name, start and end (YYYY-MM-DD) and arm") from None
        if block.end < block.start:
            raise ScheduleError(f"block {block.name} ends before it starts")
        blocks.append(block)
    blocks.sort(key=lambda block: block.start)
    for earlier, later in zip(blocks, blocks[1:]):
        if later.start <= earlier.end:
            raise ScheduleError(f"blocks {earlier.name} and {later.name} overlap")
    return tuple(blocks)


def block_on(blocks: Sequence[Block], day: date) -> Optional[Block]:
    return next((block for block in blocks if block.start <= day <= block.end), None)


def load_snapshots(paths: Iterable[Path | str]) -> tuple[dict[str, list[AccountSnapshot]], int]:
    """Every baseline snapshot under ``paths``, by account, oldest first; and how many JSON files were not one."""
    files = []
    for path in map(Path, paths):
        files.extend(sorted(path.rglob("*.json")) if path.is_dir() else [path])
    by_alias: dict[str, dict[datetime, AccountSnapshot]] = {}
    skipped = 0
    for file in files:
        try:
            snapshot = load_baseline_file(file).snapshot
        except (BaselineError, OSError, ValueError):
            skipped += 1  # a run record, manifest or other JSON in the same artifact
            continue
        by_alias.setdefault(snapshot.account_alias, {})[snapshot.observed_at] = snapshot
    return ({alias: [series[moment] for moment in sorted(series)] for alias, series in by_alias.items()}, skipped)


def _signed(value: Optional[int]) -> str:
    if value is None:
        return "not observed"
    return f"{value:+,}" if value else "0"


def _counted(designs: Iterable[str]) -> str:
    counts = Counter(designs)
    return ", ".join(f"design {design}" + (f" (x{count})" if count > 1 else "") for design, count in sorted(counts.items()))


def day_lines(before: AccountSnapshot, after: AccountSnapshot, delta: SnapshotDelta) -> list[str]:
    """The bullet lines for one day; room ids never appear, only room designs."""
    lines = []
    units = growth_units(delta)
    if units is None:
        lines.append("- Growth (the trial's measure): not observed (rooms or research missing from a snapshot)")
    else:
        total = units["room_design_advances"] + units["research_completions"]
        lines.append(f"- Growth (the trial's measure): {total} ({units['room_design_advances']} room upgrade(s), "
                     f"{units['research_completions']} research completed)")
    if "rooms" in delta.unknown_sections:
        lines.append("- Rooms: not observed")
    else:
        upgrades = [f"{old} -> {new}" for old, new in delta.room_design.values() if old is not None and new is not None]
        added = [new for old, new in delta.room_design.values() if old is None and new is not None]
        removed = [old for old, new in delta.room_design.values() if old is not None and new is None]
        started = sum(1 for old, new in delta.room_status.values() if new == UPGRADING and old != UPGRADING)
        lines.append(f"- Rooms upgraded: {', '.join(f'design {item}' for item in sorted(upgrades)) or 'none'}")
        lines.append(f"- Rooms added: {_counted(added) or 'none'}")
        if removed:
            lines.append(f"- Rooms removed: {_counted(removed)}")
        lines.append(f"- Room upgrades started: {started}")
    if "research" in delta.unknown_sections:
        lines.append("- Research: not observed")
    else:
        done = sorted(design for design, (old, new) in delta.research_state.items()
                      if new == RESEARCH_DONE and old != RESEARCH_DONE)
        running = sorted(design for design, (old, new) in delta.research_state.items()
                         if new == RESEARCH_RUNNING and old != RESEARCH_RUNNING)
        lines.append(f"- Research completed: {', '.join(done) or 'none'}")
        lines.append(f"- Research started: {', '.join(running) or 'none'}")
    lines.append(f"- Minerals: {_signed(delta.minerals)}; gas: {_signed(delta.gas)}; starbux: {_signed(delta.starbux)}")
    lines.append(f"- Trophies: {_signed(delta.trophies)}; ship level: {_signed(delta.hull_level)}")
    if "crew" in delta.unknown_sections:
        lines.append("- Crew: not observed")
    else:
        ups = sum(1 for change in delta.crew_level.values() if change is not None and change > 0)
        lines.append(f"- Crew level-ups: {ups}")
    missing = [name for name in SECTIONS
               if Completeness.MISSING in (before.section(name), after.section(name))]
    if missing:
        lines.append(f"- Not observed in a snapshot: {', '.join(missing)}")
    return lines


def summarize(series: Sequence[AccountSnapshot], blocks: Sequence[Block] = (),
              first: Optional[date] = None, last: Optional[date] = None) -> list[str]:
    """Markdown for one account's consecutive snapshot pairs, one section per day."""
    if len(series) < 2:
        return ["Fewer than two snapshots: no day to compare."]
    out = []
    for before, after in zip(series, series[1:]):
        day = after.observed_at.astimezone(timezone.utc).date()
        if (first and day < first) or (last and day > last):
            continue
        block = block_on(blocks, day)
        label = f"{block.arm} ({block.name})" if block else ("outside the schedule" if blocks else "no schedule given")
        hours = (after.observed_at - before.observed_at).total_seconds() / 3600
        out.append(f"## {day.isoformat()}: {label}")
        out.append("")
        out.append(f"Snapshots {before.observed_at.astimezone(timezone.utc):%Y-%m-%d %H:%M} to "
                   f"{after.observed_at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC ({hours:.1f} h)"
                   + ("" if DEFAULT_DAY_PAIR_HOURS[0] <= hours <= DEFAULT_DAY_PAIR_HOURS[1]
                      else "; not a one-day pair, so the exit report leaves it out"))
        out.append("")
        out.extend(day_lines(before, after, diff_snapshots(before, after)))
        out.append("")
    return out or ["No day in the requested range."]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", help="baseline snapshot files, or directories searched for them")
    parser.add_argument("--alias", help="only this account alias")
    parser.add_argument("--schedule", help="the trial's schedule.json, to label each day's block and arm")
    parser.add_argument("--from", dest="first", type=date.fromisoformat, help="first day (YYYY-MM-DD)")
    parser.add_argument("--to", dest="last", type=date.fromisoformat, help="last day (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        blocks = load_schedule(args.schedule) if args.schedule else ()
    except ScheduleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    by_alias, skipped = load_snapshots(args.paths)
    if args.alias:
        by_alias = {alias: series for alias, series in by_alias.items() if alias == args.alias}
    if not by_alias:
        print("error: no baseline snapshots found" + (f" for {args.alias}" if args.alias else ""), file=sys.stderr)
        return 1
    lines = ["# Daily changes from the snapshots", ""]
    if skipped:
        lines += [f"({skipped} JSON file(s) were not baseline snapshots and were skipped.)", ""]
    for alias in sorted(by_alias):
        lines += [f"# {alias}", ""] + summarize(by_alias[alias], blocks, args.first, args.last)
    print("\n".join(lines).rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
