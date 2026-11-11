"""Tests for sdk.fleet.audit: cross-account isolation incidents. Synthetic journals, offline."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sdk.fleet import registry_from_dict
from sdk.fleet.audit import audit_fleet
from sdk.journal import GameplayEvent

T0 = datetime(2027, 4, 2, 10, 0, tzinfo=timezone.utc)
REGISTRY = registry_from_dict({"version": 1, "accounts": [
    {"alias": "alpha", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "ALPHA_AUTH",
     "budgets": {"minerals": 500}},
    {"alias": "bravo", "objective": "grow", "policy_id": "baseline-v1", "credentials_secret": "BRAVO_AUTH"},
]})


def event(alias="alpha", start=0, length=1, attempt_id=None, attempted=True, minerals=None, action_id="upgrade_room"):
    begin = T0 + timedelta(minutes=start)
    return GameplayEvent.create(
        account_alias=alias, action_id=action_id, started_at=begin, finished_at=begin + timedelta(minutes=length),
        outcome="verified" if attempted else "skipped", reason="synthetic", attempted=attempted,
        attempt_id=attempt_id, cost={} if minerals is None else {"minerals_delta": minerals},
    )


class AuditTests(unittest.TestCase):
    def test_clean_fleet_reports_what_was_checked(self):
        report = audit_fleet(REGISTRY, {"alpha": [event(), event(start=5)], "bravo": [event("bravo")]})
        self.assertTrue(report.clean)
        self.assertEqual((report.accounts_checked, report.events_checked), (2, 3))
        self.assertEqual(json.loads(report.to_json())["incidents"], [])

    def test_foreign_and_unregistered_events(self):
        report = audit_fleet(REGISTRY, {"alpha": [event("bravo"), event("charlie", start=5)], "zulu": [event("zulu")]})
        rules = report.by_rule()
        self.assertEqual(rules["foreign-event"], 2)
        self.assertEqual(rules["unregistered-account"], 2)  # charlie's event and the zulu journal
        foreign = [i for i in report.incidents if i.rule == "foreign-event"]
        self.assertEqual({i.account_alias for i in foreign}, {"alpha"})

    def test_attempt_id_reuse_across_and_within_journals(self):
        report = audit_fleet(REGISTRY, {"alpha": [event(attempt_id="a1"), event(start=5, attempt_id="dup"),
                                                  event(start=9, attempt_id="dup")],
                                        "bravo": [event("bravo", attempt_id="a1")]})
        reused = sorted(i.attempt_ids[0] for i in report.incidents if i.rule == "attempt-id-reused")
        self.assertEqual(reused, ["a1", "dup"])

    def test_overlapping_attempts_on_one_account(self):
        report = audit_fleet(REGISTRY, {"alpha": [event(start=0, length=10), event(start=5, length=1),
                                                  event(start=20), event(start=5, attempted=False)],
                                        "bravo": [event("bravo", start=5)]})
        overlaps = [i for i in report.incidents if i.rule == "overlapping-attempts"]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0].account_alias, "alpha")

    def test_touching_windows_do_not_overlap(self):
        report = audit_fleet(REGISTRY, {"alpha": [event(start=0, length=5), event(start=5, length=5)]})
        self.assertTrue(report.clean)

    def test_budget_exceeded_per_period(self):
        journals = {"alpha": [event(start=0, minerals=-300), event(start=5, minerals=-300),
                              event(start=24 * 60, minerals=-400), event(start=30, minerals=50)],
                    "bravo": [event("bravo", minerals=-99999)]}
        report = audit_fleet(REGISTRY, journals)
        [incident] = [i for i in report.incidents if i.rule == "budget-exceeded"]
        self.assertEqual(incident.account_alias, "alpha")
        self.assertEqual(len(incident.attempt_ids), 2)
        self.assertIn("spent 600 minerals", incident.detail)

    def test_starbux_default_budget_is_enforced(self):
        spend = GameplayEvent.create(account_alias="bravo", action_id="x", started_at=T0, finished_at=T0,
                                     outcome="verified", reason="r", attempted=True, cost={"starbux_delta": -5})
        report = audit_fleet(REGISTRY, {"bravo": [spend]})
        self.assertEqual(report.by_rule(), {"budget-exceeded": 1})


if __name__ == "__main__":
    unittest.main()
