"""Evaluation moments (`as_of`) make every data source point-in-time."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    DELIVERY_HEADER,
    REQUEST_HEADER,
    SHELTER_HEADER,
    WARNING_HEADER,
    base_warnings,
    delivery_row,
    make_workspace,
    request_row,
    shelter_row,
)

from warning_response.analysis import build_analysis
from warning_response.data import load_bundle


def analyze(ws, **kwargs):
    return build_analysis(load_bundle(ws.data_dir), **kwargs)


def group(result, area, group_id):
    return next(item for item in result["groups"]
                if item["area_id"] == area and item["group_id"] == group_id)


class PointInTimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())

    def tearDown(self):
        self._tmp.cleanup()

    def test_as_of_defaults_to_latest_input_moment(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(targeted_at="2026-07-12T09:15", status="delivered",
                         acknowledged_at="2026-07-12T09:40"),
        ])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        result = analyze(self.ws)
        self.assertEqual(result["as_of"], "2026-07-12T18:00:00")

    def test_request_received_after_as_of_is_excluded(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        self.ws.write(
            "assistance_requests.csv",
            REQUEST_HEADER + ["received_at"],
            [request_row(request_id="Q1", received_at="2026-07-12T12:00",
                         requested_at="2026-07-12T11:00", status="open")
             + ["2026-07-12T12:00"]],
        )
        early = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T11:30"))
        self.assertEqual(early["requests"]["unresolved_total"], 0)
        late = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T13:00"))
        self.assertEqual(late["requests"]["unresolved_total"], 1)

    def test_request_resolved_after_as_of_still_open(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        self.ws.write("assistance_requests.csv", REQUEST_HEADER, [
            request_row(request_id="Q1", received_at="2026-07-12T11:00",
                        status="resolved", resolved_at="2026-07-12T15:00",
                        inability_reason=""),
        ])
        midday = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T12:00"))
        self.assertEqual(midday["requests"]["unresolved_total"], 1)
        self.assertEqual(midday["requests"]["unresolved"][0]["status"],
                         "resolved_after_as_of")
        evening = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T16:00"))
        self.assertEqual(evening["requests"]["unresolved_total"], 0)

    def test_ack_after_as_of_not_counted(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(targeted_at="2026-07-12T09:15", status="acknowledged",
                         acknowledged_at="2026-07-12T11:00"),
        ])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        result = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T10:00"))
        item = group(result, "North", "older_adults")
        self.assertFalse(item["delivery"]["acknowledged"])

    def test_shelter_admission_after_as_of_excluded(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [
            shelter_row(admitted_at="2026-07-12T13:00"),
        ])
        result = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T12:00"))
        window = result["shelters"]["shelters"][0]
        self.assertEqual(window["admitted_by_as_of"], 0)

    def test_explicit_as_of_is_recorded(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        result = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T11:00"))
        self.assertEqual(result["as_of"], "2026-07-12T11:00:00")

    def test_coverage_issued_after_as_of_counts_as_uncovered(self):
        self.ws.write("warnings.csv", WARNING_HEADER, [
            ["W1", 1, "2026-07-12T09:00", "North", "watch",
             "2026-07-12T09:00", "2026-07-12T18:00"],
            ["W1", 2, "2026-07-12T14:00", "East", "warning",
             "2026-07-12T14:00", "2026-07-12T20:00"],
        ])
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(area_id="East", group_id="all"),
        ])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        result = analyze(self.ws, as_of=datetime.fromisoformat("2026-07-12T12:00"))
        item = group(result, "East", "all")
        self.assertFalse(item["coverage"]["covered_at_as_of"])
        self.assertEqual(item["coverage"]["covered_from"], "2026-07-12T14:00:00")
        self.assertFalse(item["coverage"]["basis"]["in_force_at_as_of"])
        self.assertEqual(item["funnel_state"], "no_warning_coverage")


if __name__ == "__main__":
    unittest.main()
