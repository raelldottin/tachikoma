"""scripts/daily_changes.py: the daily change summary from baseline snapshots. Synthetic snapshots only."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts import daily_changes
from scripts.trial_report import growth_units
from sdk.state import build_snapshot, diff_snapshots
from sdk.state.baseline import write_baseline

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
EVENING = datetime(2026, 11, 16, 23, 30, tzinfo=timezone.utc)
ROOM_ID = "room-instance-4471"  # synthetic; room ids must never be printed


def snapshot(at, *, rooms=None, research=None, minerals=500, gas=40, trophies=931, crew_level="3", alias="pss5",
             **overrides):
    fields = dict(
        profile={"@Trophy": str(trophies)},
        ship={"@ShipLevel": "2", "@ShipDesignId": "6"},
        resources={"minerals": str(minerals), "gas": str(gas), "starbux": "164"},
        rooms=rooms if rooms is not None else [{"@RoomId": ROOM_ID, "@RoomDesignId": "100", "@RoomStatus": "Normal"}],
        research=research if research is not None else [{"@ResearchDesignId": "510", "@ResearchState": "Researching"}],
        crew=[{"@CharacterId": "c1", "@Level": crew_level}],
        inventory=[],
    )
    fields.update(overrides)
    return build_snapshot(alias, at, "synthetic", **fields)


DAY1 = snapshot(EVENING)
DAY2 = snapshot(
    EVENING + timedelta(days=1),
    rooms=[{"@RoomId": ROOM_ID, "@RoomDesignId": "101", "@RoomStatus": "Normal"},
           {"@RoomId": "room-instance-4472", "@RoomDesignId": "26", "@RoomStatus": "Upgrading"}],
    research=[{"@ResearchDesignId": "510", "@ResearchState": "Completed"},
              {"@ResearchDesignId": "511", "@ResearchState": "Researching"}],
    minerals=740, gas=0, trophies=943, crew_level="4",
)


class DailyChangesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, *snapshots):
        for item in snapshots:
            write_baseline(item, self.dir, "1.0")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = daily_changes.main([str(self.dir), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_a_day_lists_what_changed_and_the_trials_measure(self):
        self.write(DAY1, DAY2)
        code, out, err = self.run_main("--schedule", str(REPO_ROOT / "docs/experiments/r2-progression-trial/schedule.json"))
        self.assertEqual(code, 0, err)
        self.assertIn("## 2026-11-17: manual (baseline window)", out)
        self.assertIn("- Growth (the trial's measure): 2 (1 room upgrade(s), 1 research completed)", out)
        self.assertIn("- Rooms upgraded: design 100 -> 101", out)
        self.assertIn("- Rooms added: design 26", out)
        self.assertIn("- Room upgrades started: 1", out)
        self.assertIn("- Research completed: 510", out)
        self.assertIn("- Research started: 511", out)
        self.assertIn("- Minerals: +240; gas: -40; starbux: 0", out)
        self.assertIn("- Trophies: +12; ship level: 0", out)
        self.assertIn("- Crew level-ups: 1", out)
        self.assertIn("(24.0 h)", out)

    def test_the_growth_figure_is_the_exit_reports(self):
        units = growth_units(diff_snapshots(DAY1, DAY2))
        line = next(entry for entry in daily_changes.day_lines(DAY1, DAY2, diff_snapshots(DAY1, DAY2))
                    if entry.startswith("- Growth"))
        self.assertIn(str(units["room_design_advances"] + units["research_completions"]), line)

    def test_room_ids_are_never_printed(self):
        self.write(DAY1, DAY2)
        _, out, _ = self.run_main()
        self.assertNotIn("room-instance", out)

    def test_unobserved_sections_are_not_shown_as_no_change(self):
        self.write(DAY1, build_snapshot("pss5", EVENING + timedelta(days=1), "synthetic",
                                        resources={"minerals": "600", "gas": "40", "starbux": "164"}))
        _, out, _ = self.run_main()
        self.assertIn("- Growth (the trial's measure): not observed", out)
        self.assertIn("- Rooms: not observed", out)
        self.assertIn("- Research: not observed", out)
        self.assertIn("- Crew: not observed", out)
        self.assertIn("trophies: not observed", out.lower())
        self.assertIn("- Not observed in a snapshot:", out)

    def test_labels_skips_and_filters(self):
        outside = snapshot(datetime(2026, 12, 22, 23, 30, tzinfo=timezone.utc))
        short = snapshot(datetime(2026, 12, 23, 6, 0, tzinfo=timezone.utc))
        other = snapshot(EVENING, alias="pss4")
        self.write(DAY1, DAY2, snapshot(datetime(2026, 12, 21, 23, 30, tzinfo=timezone.utc)), outside, short, other)
        (self.dir / "run-record.json").write_text(json.dumps({"kind": "fleet-execute"}), encoding="utf-8")
        schedule = str(REPO_ROOT / "docs/experiments/r2-progression-trial/schedule.json")
        code, out, _ = self.run_main("--schedule", schedule, "--alias", "pss5")
        self.assertEqual(code, 0)
        self.assertIn("1 JSON file(s) were not baseline snapshots", out)
        self.assertIn("## 2026-12-22: outside the schedule", out)
        self.assertIn("not a one-day pair, so the exit report leaves it out", out)
        self.assertNotIn("# pss4", out)
        _, ranged, _ = self.run_main("--from", "2026-11-17", "--to", "2026-11-17")
        self.assertIn("## 2026-11-17", ranged)
        self.assertNotIn("## 2026-12-22", ranged)
        _, no_schedule, _ = self.run_main("--alias", "pss5", "--to", "2026-11-17")
        self.assertIn("no schedule given", no_schedule)

    def test_bad_inputs(self):
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("no baseline snapshots", err)
        bad = self.dir / "schedule.json"
        bad.write_text(json.dumps({"blocks": [{"name": "a", "start": "2026-11-16", "end": "2026-11-20", "arm": "x"},
                                              {"name": "b", "start": "2026-11-20", "end": "2026-11-25", "arm": "y"}]}),
                       encoding="utf-8")
        code, _, err = self.run_main("--schedule", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("overlap", err)
        self.write(DAY1)
        code, out, _ = self.run_main()
        self.assertIn("Fewer than two snapshots", out)

    def test_the_trial_schedule_matches_the_approved_plan(self):
        blocks = daily_changes.load_schedule(REPO_ROOT / "docs/experiments/r2-progression-trial/schedule.json")
        self.assertEqual([(block.name, block.start, block.end, block.arm) for block in blocks], [
            ("baseline window", date(2026, 11, 16), date(2026, 11, 22), "manual"),
            ("block 1", date(2026, 11, 23), date(2026, 11, 29), "manual"),
            ("block 2", date(2026, 11, 30), date(2026, 12, 6), "baseline-v1"),
            ("block 3", date(2026, 12, 7), date(2026, 12, 13), "baseline-v1"),
            ("block 4", date(2026, 12, 14), date(2026, 12, 20), "manual"),
        ])
        plan = (REPO_ROOT / "docs/experiments/r2-progression-trial.md").read_text(encoding="utf-8")
        for block in blocks:
            self.assertIn(f"{block.start.isoformat()} to {block.end.isoformat()}", plan)


if __name__ == "__main__":
    unittest.main()
