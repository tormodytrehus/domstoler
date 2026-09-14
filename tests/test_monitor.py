import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "monitor.py"
SPEC = importlib.util.spec_from_file_location("court_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


CONFIG = {
    "matching": {"terms": ["Orkland kommune", "Berkåk"]},
    "monitor": {
        "silent_first_run": True,
        "missing_runs_before_alert": 2,
        "max_feed_items": 20,
    },
}


def make_case(subject="Kontraktstvist", parties="Orkland kommune mot Firma AS"):
    return monitor.CourtCase(
        case_id="Trøndelag tingrett:abc",
        case_number="26-123TVI-TTRO/TTRD",
        court="Trøndelag tingrett",
        subject=subject,
        parties=parties,
        url="https://www.domstol.no/no/nar-gar-rettssaken/?saksid=abc",
        hearings=["20.09.2026"],
    )


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

    def test_first_run_is_silent_and_redacts_parties(self):
        state, events = monitor.compare_snapshots(
            {"id": make_case()}, {"initialized": False}, CONFIG, self.now
        )
        self.assertEqual(events, [])
        stored = next(iter(state["cases"].values()))["summary"]
        self.assertNotIn("parties", stored)

    def test_new_interesting_case_alerts_after_baseline(self):
        state, events = monitor.compare_snapshots(
            {"id": make_case()}, {"initialized": True, "cases": {}, "events": []}, CONFIG, self.now
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "new")
        self.assertIn("Orkland kommune", events[0]["matched_terms"])

    def test_changed_case_alerts(self):
        original = make_case()
        old = {
            "initialized": True,
            "cases": {
                "id": {
                    "fingerprint": original.fingerprint,
                    "interesting": True,
                    "missing_count": 0,
                    "summary": original.safe_summary(["Orkland kommune"]),
                }
            },
            "events": [],
        }
        changed = make_case(subject="Endret kontraktstvist")
        _, events = monitor.compare_snapshots({"id": changed}, old, CONFIG, self.now)
        self.assertEqual(events[0]["kind"], "changed")

    def test_unmatched_case_does_not_alert(self):
        case = make_case(parties="Firma AS mot Staten")
        _, events = monitor.compare_snapshots(
            {"id": case}, {"initialized": True, "cases": {}, "events": []}, CONFIG, self.now
        )
        self.assertEqual(events, [])

    def test_feed_uses_observed_time_and_safe_fields(self):
        event = monitor.make_event(
            "new", make_case().safe_summary(["Orkland kommune"]), self.now
        )
        xml = monitor.build_rss(
            [event], {"title": "Test", "description": "Testfeed", "link": "https://example.no"}
        ).decode()
        self.assertIn("Ny beramming", xml)
        self.assertIn("Orkland kommune", xml)
        self.assertNotIn("Firma AS", xml)


if __name__ == "__main__":
    unittest.main()
