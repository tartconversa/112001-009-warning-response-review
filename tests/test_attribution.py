"""Attribution rules for sends outside the warning window, and evidence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    DELIVERY_HEADER,
    EXPOSURE_HEADER,
    SHELTER_HEADER,
    WARNING_HEADER,
    delivery_row,
    make_workspace,
    shelter_row,
)

from warning_response.analysis import build_analysis
from warning_response.data import load_bundle


def analyze(ws, **kwargs):
    return build_analysis(load_bundle(ws.data_dir), **kwargs)


def group(result, area, group_id):
    return next(item for item in result["groups"]
                if item["area_id"] == area and item["group_id"] == group_id)


class SendAttributionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, [
            ["W1", 1, "2026-07-12T09:00", "North", "watch",
             "2026-07-12T09:00", "2026-07-12T18:00"],
        ])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        self.ws.write("exposures.csv", EXPOSURE_HEADER, [
            ["E1", "North", "older_adults", "120", "census", "aggregate",
             "2026-07-12T08:00"],
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_send_after_expiry_is_not_counted_as_coverage(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(targeted_at="2026-07-12T19:00", status="acknowledged",
                         acknowledged_at="2026-07-12T19:10"),
        ])
        # At 17:00 the warning is still in force, and the 19:00 send does not
        # exist yet.
        result = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T17:00"))
        item = group(result, "North", "older_adults")
        self.assertEqual(item["targeting"]["future_attempts_ignored"], 1)
        self.assertEqual(item["funnel_state"], "covered_not_targeted")

        # At 20:00 the warning has expired; the 19:00 send cannot be counted.
        result = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T20:00"))
        item = group(result, "North", "older_adults")
        self.assertEqual(item["targeting"]["post_expiry_attempts"], 1)
        self.assertEqual(item["funnel_state"], "no_warning_coverage")
        self.assertIn("sent_after_expiry", item["reasons"])

    def test_acknowledgement_after_as_of_is_not_counted(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(targeted_at="2026-07-12T09:00", status="acknowledged",
                         acknowledged_at="2026-07-12T11:00"),
        ])
        result = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T10:00"))
        item = group(result, "North", "older_adults")
        self.assertFalse(item["delivery"]["acknowledged"])
        self.assertEqual(item["funnel_state"], "delivered_not_acknowledged")

    def test_send_before_issue_is_flagged_but_attempt_recorded(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(targeted_at="2026-07-12T08:00", status="delivered"),
        ])
        result = analyze(self.ws)
        item = group(result, "North", "older_adults")
        self.assertEqual(item["targeting"]["pre_warning_attempts"], 1)
        self.assertIn("pre_warning_target", item["reasons"])
        self.assertEqual(item["funnel_state"], "covered_not_targeted")

    def test_coverage_basis_is_explicit(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        result = analyze(self.ws)
        basis = group(result, "North", "older_adults")["coverage"]["basis"]
        self.assertEqual(basis["basis"], "first_version_covering_area")
        self.assertEqual(basis["version"], 1)
        self.assertEqual(basis["issued_at"], "2026-07-12T09:00:00")
        self.assertTrue(basis["in_force_at_as_of"])

    def test_coverage_basis_reports_no_version(self):
        self.ws.write("warnings.csv", WARNING_HEADER, [
            ["W1", 1, "2026-07-12T09:00", "Other", "watch",
             "2026-07-12T09:00", "2026-07-12T18:00"],
        ])
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        result = analyze(self.ws)
        item = group(result, "North", "older_adults")
        self.assertEqual(item["coverage"]["basis"]["basis"],
                         "no_warning_version_names_area")
        self.assertFalse(item["coverage"]["covered_at_as_of"])

    def test_mixed_attributed_and_unattributed_rows(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(targeted_at="2026-07-12T08:00", status="delivered"),
            delivery_row(targeted_at="2026-07-12T09:30", status="delivered"),
        ])
        result = analyze(self.ws)
        item = group(result, "North", "older_adults")
        self.assertEqual(item["targeting"]["attempts"], 2)
        self.assertEqual(item["targeting"]["pre_warning_attempts"], 1)
        self.assertEqual(item["targeting"]["attributed_attempts"], 1)
        self.assertEqual(item["funnel_state"], "delivered_not_acknowledged")


if __name__ == "__main__":
    unittest.main()
