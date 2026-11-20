"""Review output and cross-version comparison."""

from __future__ import annotations

import tempfile
import unittest

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

from warning_response.analysis import build_analysis, compare_analyses
from warning_response.data import load_bundle
from warning_response.review import build_review, render_review_markdown
from warning_response.store import Store


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, [
            ["W1", 1, "2026-07-12T09:00", "North", "watch",
             "2026-07-12T09:00", "2026-07-12T18:00"],
            ["W1", 2, "2026-07-12T12:30", "North", "warning",
             "2026-07-12T12:30", "2026-07-12T20:00"],
            ["W1", 2, "2026-07-12T12:30", "East", "warning",
             "2026-07-12T12:30", "2026-07-12T20:00"],
        ])
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(area_id="North", group_id="older_adults",
                         status="delivered", targeted_at="2026-07-12T09:15",
                         acknowledged_at="2026-07-12T09:40"),
            delivery_row(area_id="East", group_id="all", exposed_estimate=5000,
                         status="sent", targeted_at="2026-07-12T12:45",
                         acknowledged_at=""),
        ])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [
            shelter_row(admitted_at="2026-07-12T13:00"),
        ])
        self.ws.write("assistance_requests.csv", REQUEST_HEADER, [
            request_row(status="unresolved", inability_reason="no_transport"),
        ])
        self.store = Store.create(self.ws.store_path, event_id="W1", event_name="暴雨")
        self.store.import_snapshot(self.ws.data_dir, label="初始")

    def tearDown(self):
        self._tmp.cleanup()

    def test_review_has_all_required_sections(self):
        review = build_review(self.store)
        for key in ("event", "snapshot_history", "gaps", "unresolved_reasons",
                    "tasks", "resources", "timeline", "uncertainty", "claims",
                    "completeness", "recommendations"):
            self.assertIn(key, review)

    def test_review_marks_late_area_and_unacknowledged(self):
        review = build_review(self.store)
        gaps = {(gap["area_id"], gap["group_id"]): gap for gap in review["gaps"]}
        east = gaps[("East", "all")]
        self.assertEqual(east["coverage"]["covered_from"], "2026-07-12T12:30:00")
        self.assertTrue(east["coverage"]["late_addition"])
        self.assertFalse(east["acknowledged"])
        north = gaps[("North", "older_adults")]
        self.assertTrue(north["acknowledged"])

    def test_review_records_unresolved_reason_categories(self):
        review = build_review(self.store)
        self.assertEqual(review["unresolved_reasons"]["total"], 1)
        self.assertEqual(review["unresolved_reasons"]["by_reason"]["no_transport"], 1)
        self.assertEqual(review["unresolved_reasons"]["by_need"]["evacuation"], 1)

    def test_markdown_contains_claim_boundaries(self):
        text = render_review_markdown(build_review(self.store))
        self.assertIn("复盘结论边界", text)
        self.assertIn("不能把发送条数当作居民已知悉的证明", text)
        self.assertIn("不能声称对聚合人口资料识别到了具体个人", text)
        self.assertIn("不能把预警扩区后新增的区域算作提前收到通知", text)

    def test_task_drift_is_surfaced_after_revision(self):
        self.store.register_task(area_id="East", group_id="all",
                                 action="二次联络", priority_key="contact:East:all")
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(area_id="North", group_id="older_adults",
                         status="delivered", targeted_at="2026-07-12T09:15",
                         acknowledged_at="2026-07-12T09:40"),
            delivery_row(area_id="East", group_id="all", exposed_estimate=5000,
                         status="acknowledged", targeted_at="2026-07-12T12:45",
                         acknowledged_at="2026-07-12T14:00"),
        ])
        self.store.import_snapshot(self.ws.data_dir, label="补充回执")
        review = build_review(self.store)
        task = review["tasks"][0]
        self.assertTrue(task["drift"]["changed"])
        self.assertEqual(task["funnel_state_at_registration"],
                         "targeted_outcome_unknown")
        self.assertIn("已发送，结果未知", task["drift"]["reasons"][0])
        text = render_review_markdown(review)
        self.assertIn("需复核", text)

    def test_review_shows_superseded_snapshot_retained(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(area_id="North", group_id="older_adults",
                         status="delivered", targeted_at="2026-07-12T09:15",
                         acknowledged_at=""),
            delivery_row(area_id="East", group_id="all", exposed_estimate=5000,
                         status="sent", targeted_at="2026-07-12T12:45",
                         acknowledged_at=""),
        ])
        self.store.import_snapshot(self.ws.data_dir, label="回执修订")
        review = build_review(self.store)
        self.assertEqual(review["snapshot_count"], 2)
        self.assertTrue(review["snapshot_history"][0]["superseded"])
        self.assertFalse(review["snapshot_history"][1]["superseded"])
        text = render_review_markdown(review)
        self.assertIn("已被后续修订取代", text)

    def test_completeness_reports_open_work(self):
        review = build_review(self.store)
        self.assertFalse(review["completeness"]["fully_closed"])
        self.assertGreater(review["completeness"]["counts"]["groups_not_acknowledged"], 0)

    def test_task_impact_explainswhatchanged(self):
        from warning_response.review import task_impact

        self.store.register_task(area_id="North", group_id="older_adults",
                                 action="复核转移", priority_key="contact:North:older_adults")
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [
            shelter_row(admitted_at="2026-07-12T13:00", capacity=5, open_to=""),
        ])
        self.store.import_snapshot(self.ws.data_dir, label="容量变化")
        impact = task_impact(self.store)
        impacted = next(item for item in impact if item["task_id"] == "T001")
        self.assertTrue(impacted["needs_review"])
        self.assertTrue(any("避难点" in reason for reason in impacted["reasons"]))

    def test_task_impact_ignores_unrelated_changes(self):
        from warning_response.review import task_impact

        self.store.register_task(area_id="North", group_id="older_adults",
                                 action="复核转移", priority_key="contact:North:older_adults")
        # A change confined to East must not raise the North task.
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(area_id="North", group_id="older_adults",
                         status="delivered", targeted_at="2026-07-12T09:15",
                         acknowledged_at="2026-07-12T09:40"),
            delivery_row(area_id="East", group_id="all", exposed_estimate=5000,
                         status="acknowledged", targeted_at="2026-07-12T12:45",
                         acknowledged_at="2026-07-12T14:00"),
        ])
        self.store.import_snapshot(self.ws.data_dir, label="East 回执")
        self.assertEqual(task_impact(self.store), [])

    def test_impact_command_open_only(self):
        self.store.register_task(area_id="North", group_id="older_adults",
                                 action="复核转移", priority_key="contact:North:older_adults")
        self.store.update_task("T001", status="done", outcome="已处理")
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [
            shelter_row(admitted_at="2026-07-12T13:00", capacity=5, open_to=""),
        ])
        self.store.import_snapshot(self.ws.data_dir, label="容量变化")
        from warning_response.review import task_impact

        self.assertTrue(task_impact(self.store))  # still reported for the record
        self.assertFalse(task_impact(self.store)[0]["needs_review"])

    def test_review_log_is_ordered(self):
        self.store.register_task(area_id="East", group_id="all", action="联络")
        self.store.update_task("T001", status="done")
        self.store.add_note("handover", "交接")
        review = build_review(self.store)
        entries = review["lifecycle_entries"]
        self.assertEqual(entries, [])


class ComparisonTests(unittest.TestCase):
    def _analysis(self, ws):
        return build_analysis(load_bundle(ws.data_dir))

    def test_new_area_coverage_is_high_severity_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(type("T", (), {"name": tmp})())
            ws.write("warnings.csv", WARNING_HEADER, base_warnings())
            ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
            ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
            ws.write("exposures.csv", EXPOSURE_HEADER, [
                ["E1", "East", "all", "100", "census", "aggregate", "2026-07-12T08:00"],
            ])
            before = self._analysis(ws)
            ws.write("warnings.csv", WARNING_HEADER, [
                base_warnings()[0],
                ["W1", 2, "2026-07-12T12:00", "East", "warning",
                 "2026-07-12T12:00", "2026-07-12T18:00"],
            ])
            after = self._analysis(ws)
            changes = compare_analyses(before, after)
            new_area = [c for c in changes if c["category"] == "new_area_coverage"]
            self.assertEqual(len(new_area), 1)
            self.assertEqual(new_area[0]["severity"], "high")
            self.assertIn("不能视为已提前收到通知", new_area[0]["detail"])

    def test_capacity_and_priority_changes_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_workspace(type("T", (), {"name": tmp})())
            ws.write("warnings.csv", WARNING_HEADER, base_warnings())
            ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
            ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
            before = self._analysis(ws)
            ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row(capacity=20)])
            after = self._analysis(ws)
            changes = compare_analyses(before, after)
            self.assertTrue(any(c["category"] == "capacity_changed" for c in changes))


if __name__ == "__main__":
    unittest.main()
