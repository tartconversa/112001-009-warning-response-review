"""Core analysis semantics: coverage, funnel and capacity."""

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


def dt(text):
    return datetime.fromisoformat(text)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])

    def tearDown(self):
        self._tmp.cleanup()

    def test_late_area_is_not_retroactively_covered(self):
        self.ws.write("warnings.csv", WARNING_HEADER, [
            ["W1", 1, "2026-07-12T09:00", "North", "watch",
             "2026-07-12T09:00", "2026-07-12T18:00"],
            ["W1", 2, "2026-07-12T12:30", "North", "warning",
             "2026-07-12T12:30", "2026-07-12T20:00"],
            ["W1", 2, "2026-07-12T12:30", "East", "warning",
             "2026-07-12T12:30", "2026-07-12T20:00"],
        ])
        # East was targeted at 09:30, before it was covered at 12:30.
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(area_id="East", group_id="all", targeted_at="2026-07-12T09:30",
                         status="delivered",
                         acknowledged_at="2026-07-12T09:45"),
            delivery_row(area_id="East", group_id="all", channel="app",
                         targeted_at="2026-07-12T12:45", status="delivered",
                         acknowledged_at=""),
        ])
        result = analyze(self.ws)
        east = group(result, "East", "all")
        self.assertEqual(east["coverage"]["covered_from"], "2026-07-12T12:30:00")
        self.assertTrue(east["coverage"]["late_addition"])
        # The 09:30/09:45 rows must not count as acknowledgement of this warning.
        self.assertEqual(east["targeting"]["pre_warning_attempts"], 1)
        self.assertEqual(east["targeting"]["attributed_attempts"], 1)
        self.assertEqual(east["funnel_state"], "delivered_not_acknowledged")
        self.assertIsNone(east["delivery"]["first_acknowledged_at"])
        escalation = result["warning"]["escalations"]
        self.assertEqual(len(escalation), 1)
        self.assertEqual(escalation[0]["added_areas"], ["East"])

    def test_coverage_as_of_respects_validity(self):
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        # Before the warning was issued there is no coverage at all.
        early = analyze(self.ws, as_of=dt("2026-07-12T08:00"))
        north = group(early, "North", "older_adults")
        self.assertFalse(north["coverage"]["covered_at_as_of"])
        self.assertEqual(north["funnel_state"], "no_warning_coverage")
        # After the warning expires there is also no current coverage.
        expired = analyze(self.ws, as_of=dt("2026-07-12T23:00"))
        self.assertFalse(group(expired, "North", "older_adults")["coverage"]["covered_at_as_of"])

    def test_upgrade_without_new_area_is_upgrade_only(self):
        self.ws.write("warnings.csv", WARNING_HEADER, [
            ["W1", 1, "2026-07-12T09:00", "North", "watch",
             "2026-07-12T09:00", "2026-07-12T18:00"],
            ["W1", 2, "2026-07-12T12:00", "North", "warning",
             "2026-07-12T12:00", "2026-07-12T18:00"],
        ])
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        result = analyze(self.ws)
        self.assertEqual(len(result["warning"]["escalations"]), 1)
        entry = result["warning"]["escalations"][0]
        self.assertTrue(entry["upgraded"])
        self.assertEqual(entry["added_areas"], [])


class FunnelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, rows, **kwargs):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, rows)
        return analyze(self.ws, **kwargs)

    def test_sent_only_is_not_acknowledged(self):
        result = self._run([delivery_row(status="sent")])
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "targeted_outcome_unknown")
        self.assertFalse(item["delivery"]["acknowledged"])
        self.assertFalse(item["delivery"]["channel_success"])

    def test_delivered_is_not_acknowledged(self):
        result = self._run([delivery_row(status="delivered")])
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "delivered_not_acknowledged")
        self.assertTrue(item["delivery"]["channel_success"])
        self.assertFalse(item["delivery"]["acknowledged"])

    def test_acknowledged_requires_status_or_timestamp(self):
        result = self._run([delivery_row(status="acknowledged",
                                         acknowledged_at="2026-07-12T09:40")])
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "acknowledged")
        self.assertEqual(item["delivery"]["first_acknowledged_at"], "2026-07-12T09:40:00")

    def test_ack_timestamp_counts_even_if_status_lags(self):
        result = self._run([delivery_row(status="sent",
                                         acknowledged_at="2026-07-12T09:40")])
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "acknowledged")

    def test_failed_channel_with_ack_timestamp_is_ambiguous_not_ack(self):
        result = self._run([delivery_row(status="failed",
                                         acknowledged_at="2026-07-12T09:40")])
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "unreachable")
        self.assertFalse(item["delivery"]["acknowledged"])

    def test_all_failed_is_unreachable(self):
        result = self._run([delivery_row(status="failed")])
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "unreachable")

    def test_covered_but_not_targeted(self):
        result = self._run([])
        self.ws.write("exposures.csv", EXPOSURE_HEADER, [
            ["E1", "North", "older_adults", "100", "census", "aggregate",
             "2026-07-12T08:00"],
        ])
        result = analyze(self.ws)
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "covered_not_targeted")

    def test_timeliness_uses_channel_success_and_separate_ack(self):
        self.ws.write("warnings.csv", WARNING_HEADER,
                      base_warnings(issued_at="2026-07-12T09:00"))
        result = self._run(
            [delivery_row(status="delivered", targeted_at="2026-07-12T09:30"),
             delivery_row(channel="app", status="acknowledged",
                          targeted_at="2026-07-12T09:35",
                          acknowledged_at="2026-07-12T11:30")],
            timely_minutes=60,
        )
        item = group(result, "North", "older_adults")
        self.assertTrue(item["delivery"]["timely_channel"])
        self.assertFalse(item["delivery"]["timely_acknowledged"])

    def test_ambiguity_is_reported_as_reason(self):
        result = self._run([delivery_row(status="sent?")])
        item = group(result, "North", "older_adults")
        self.assertEqual(item["funnel_state"], "targeted_outcome_unknown")
        self.assertIn("ambiguous_status", item["reasons"])


class ExposureAggregationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(group_id="all", exposed_estimate=8400),
            delivery_row(channel="app", group_id="older_adults", exposed_estimate=480,
                         targeted_at="2026-07-12T09:20"),
        ])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])

    def tearDown(self):
        self._tmp.cleanup()

    def test_whole_area_and_subgroup_are_not_additive(self):
        result = analyze(self.ws)
        north = [item for item in result["groups"] if item["area_id"] == "North"]
        self.assertTrue(all(item["overlap"] for item in north))
        self.assertTrue(all(not item["additive"] for item in north))
        notes = result["claims"]["aggregation_notes"]
        self.assertTrue(any("不可相加" in note for note in notes))

    def test_individual_identification_is_never_claimed(self):
        result = analyze(self.ws)
        self.assertEqual(result["claims"]["individual_identification"], "unsupported")
        self.assertTrue(all(item["aggregate_only"] for item in result["groups"]))
        self.assertIn("不能声称识别到具体个人", result["claims"]["individual_note"])

    def test_individual_granularity_flips_claim(self):
        self.ws.write("exposures.csv", EXPOSURE_HEADER, [
            ["E1", "North", "older_adults", "1", "roster", "individual",
             "2026-07-12T08:00"],
        ])
        result = analyze(self.ws)
        self.assertEqual(result["claims"]["individual_identification"],
                         "supported_for_exposure_rows")


class ShelterCapacityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, rows, as_of):
        self.ws.write("shelters_response.csv", SHELTER_HEADER, rows)
        return analyze(self.ws, as_of=dt(as_of))["shelters"]

    def test_capacity_counts_admissions_inside_window_only(self):
        shelters = self._run([
            shelter_row(admitted_at="2026-07-12T11:00"),
            shelter_row(admitted_at="2026-07-12T13:00"),
            # Before opening: ignored for occupation.
            shelter_row(admitted_at="2026-07-12T09:59"),
        ], as_of="2026-07-12T14:00")
        window = shelters["shelters"][0]
        self.assertEqual(window["admitted_total"], 2)
        self.assertEqual(window["admitted_by_as_of"], 2)
        self.assertEqual(window["remaining_at_as_of"], 98)
        self.assertEqual(window["status"], "open")

    def test_admission_after_as_of_is_not_counted(self):
        shelters = self._run([shelter_row(admitted_at="2026-07-12T15:00")],
                             as_of="2026-07-12T12:00")
        window = shelters["shelters"][0]
        self.assertEqual(window["admitted_total"], 1)
        self.assertEqual(window["admitted_by_as_of"], 0)
        self.assertEqual(window["remaining_at_as_of"], 100)

    def test_closed_window_has_no_current_occupancy(self):
        shelters = self._run([shelter_row(admitted_at="2026-07-12T11:00")],
                             as_of="2026-07-12T20:00")
        window = shelters["shelters"][0]
        self.assertEqual(window["status"], "closed")
        self.assertIsNone(window["occupancy_at_as_of"])
        self.assertIsNone(window["remaining_at_as_of"])

    def test_not_yet_open_window_does_not_add_capacity(self):
        shelters = self._run([shelter_row(open_from="2026-07-12T16:00",
                                          open_to="2026-07-12T22:00")],
                             as_of="2026-07-12T12:00")
        window = shelters["shelters"][0]
        self.assertEqual(window["status"], "not_yet_open")
        self.assertEqual(shelters["areas"]["North"]["open_capacity"], 0)

    def test_zero_capacity_open_shelter_is_flagged(self):
        shelters = self._run([shelter_row(capacity=0)], as_of="2026-07-12T12:00")
        window = shelters["shelters"][0]
        self.assertEqual(window["status"], "open_no_capacity")
        self.assertEqual(shelters["areas"]["North"]["no_capacity_open"], 1)

    def test_conflicting_capacity_values_are_reported(self):
        shelters = self._run([
            shelter_row(admitted_at="2026-07-12T11:00"),
            shelter_row(admitted_at="2026-07-12T12:00", capacity=50),
        ], as_of="2026-07-12T13:00")
        self.assertEqual(shelters["shelters"][0]["capacity"], 100)
        self.assertTrue(any("多个容量值" in item for item in shelters["uncertainties"]))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        self.ws.write("assistance_requests.csv", REQUEST_HEADER, [
            request_row(request_id="Q1", status="open", need="evacuation",
                        inability_reason="no_transport"),
            request_row(request_id="Q2", status="resolved", need="supplies",
                        resolved_at="2026-07-12T12:00", inability_reason=""),
            request_row(request_id="Q3", status="unresolved", need="medical",
                        requested_at="2026-07-12T12:30", inability_reason="no_transport"),
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_unresolved_requests_grouped_by_reason(self):
        result = analyze(self.ws)
        requests = result["requests"]
        self.assertEqual(requests["unresolved_total"], 2)
        reasons = {item["reason"]: item["count"]
                   for item in requests["reason_categories"]}
        self.assertEqual(reasons["no_transport"], 2)
        self.assertEqual(requests["unresolved"][0]["need"], "evacuation")

    def test_open_request_raises_group_priority(self):
        result = analyze(self.ws)
        item = group(result, "North", "older_adults")
        self.assertIn("unresolved_request", item["reasons"])
        priority = next(p for p in result["priorities"]
                        if p["key"] == "contact:North:older_adults")
        self.assertIn("未解决求助", " ".join(priority["reasons"]))


class TimelineTests(unittest.TestCase):
    def test_timeline_is_chronological_and_includes_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(type("T", (), {"name": tmp})())
            ws.copy_fixtures()
            result = analyze(ws)
            times = [entry["at"] for entry in result["timeline"]]
            self.assertEqual(times, sorted(times))
            kinds = {entry["kind"] for entry in result["timeline"]}
            self.assertIn("warning_issued", kinds)
            self.assertIn("acknowledged", kinds)
            self.assertIn("shelter_admission", kinds)


if __name__ == "__main__":
    unittest.main()
