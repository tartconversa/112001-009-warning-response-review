"""Workspace persistence, revision history and point-in-time replay."""

from __future__ import annotations

import json
import tempfile
import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    DELIVERY_HEADER,
    SHELTER_HEADER,
    WARNING_HEADER,
    base_warnings,
    delivery_row,
    make_workspace,
    shelter_row,
)

from warning_response.errors import ConflictError, InputDataError, NotFoundError, StorageError
from warning_response.store import Store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])
        self.store = Store.create(self.ws.store_path, event_id="W1", event_name="暴雨")
        self.snapshot = self.store.import_snapshot(self.ws.data_dir, label="初始")

    def tearDown(self):
        self._tmp.cleanup()


class SnapshotTests(StoreTestCase):
    def test_snapshot_is_immutable_across_revisions(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(status="acknowledged", acknowledged_at="2026-07-12T09:40"),
        ])
        self.store.import_snapshot(self.ws.data_dir, label="补充确认")
        snapshots = self.store.snapshots()
        self.assertEqual(len(snapshots), 2)
        old = snapshots[0]["analysis"]["groups"][0]
        new = snapshots[1]["analysis"]["groups"][0]
        self.assertEqual(old["funnel_state"], "delivered_not_acknowledged")
        self.assertEqual(new["funnel_state"], "acknowledged")

    def test_identical_reimport_is_rejected(self):
        with self.assertRaises(ConflictError) as ctx:
            self.store.import_snapshot(self.ws.data_dir, label="重复")
        self.assertEqual(ctx.exception.code, "no_change")

    def test_snapshot_at_replays_earlier_version(self):
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(status="acknowledged", acknowledged_at="2026-07-12T09:40"),
        ])
        newer = self.store.import_snapshot(self.ws.data_dir, label="补充确认")
        before = self.store.snapshot_at(self.snapshot["seq"])
        self.assertEqual(before["seq"], self.snapshot["seq"])
        self.assertEqual(self.store.snapshot_at(newer["seq"])["seq"], newer["seq"])

    def test_snapshot_records_input_and_analysis_digests(self):
        self.assertRegex(self.snapshot["input_digest"], r"^[0-9a-f]{16}$")
        self.assertRegex(self.snapshot["analysis_digest"], r"^[0-9a-f]{16}$")
        self.assertNotEqual(self.snapshot["input_digest"],
                            self.snapshot["analysis_digest"])

    def test_invalid_input_does_not_mutate_workspace(self):
        before = self.ws.read_store()
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings(valid_to="bogus"))
        with self.assertRaises(InputDataError):
            self.store.import_snapshot(self.ws.data_dir, label="坏资料")
        self.assertEqual(self.ws.read_store()["seq"], before["seq"])
        self.assertEqual(len(self.store.snapshots()), 1)


class TaskTests(StoreTestCase):
    def test_task_captures_evidence_at_registration(self):
        task = self.store.register_task(
            priority_key="contact:North:older_adults",
            area_id="North",
            group_id="older_adults",
            action="上门确认",
            owner="一组",
        )
        self.assertEqual(task["task_id"], "T001")
        self.assertEqual(task["registered_against_snapshot"], self.snapshot["seq"])
        self.assertEqual(task["seen"]["group"]["funnel_state"],
                         "delivered_not_acknowledged")
        self.assertEqual(task["seen"]["priority"]["key"], "contact:North:older_adults")

    def test_evidence_survives_later_revision(self):
        self.store.register_task(area_id="North", group_id="older_adults",
                                 action="上门确认")
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [
            delivery_row(status="acknowledged", acknowledged_at="2026-07-12T09:40"),
        ])
        self.store.import_snapshot(self.ws.data_dir, label="补充确认")
        state = self.store.current_state()
        task = state["tasks"][0]
        # What the duty team saw when they registered is still there...
        self.assertEqual(task["seen"]["group"]["funnel_state"],
                         "delivered_not_acknowledged")
        # ...and the current snapshot has moved on.
        self.assertEqual(
            self.store.latest_snapshot()["analysis"]["groups"][0]["funnel_state"],
            "acknowledged",
        )

    def test_task_status_history_replays(self):
        self.store.register_task(area_id="North", group_id="older_adults",
                                 action="上门确认")
        self.store.update_task("T001", status="in_progress", owner="二组")
        self.store.update_task("T001", status="done", outcome="已当面确认")
        current = self.store.current_state()["tasks"][0]
        self.assertEqual(current["status"], "done")
        self.assertEqual(len(current["history"]), 3)
        # A replay before the final update shows the earlier state.
        done_seq = current["history"][-1]["seq"]
        earlier = self.store.state_at(done_seq - 1)["tasks"][0]
        self.assertEqual(earlier["status"], "in_progress")
        self.assertEqual(earlier["owner"], "二组")

    def test_no_snapshot_means_no_task(self):
        empty = Store.create(self.ws.store_path.parent / "empty.json", event_id="X")
        with self.assertRaises(ConflictError) as ctx:
            empty.register_task(area_id="North", action="联络")
        self.assertEqual(ctx.exception.code, "no_snapshot")

    def test_missing_area_and_action_are_rejected(self):
        with self.assertRaises(InputDataError):
            self.store.register_task(area_id="", action="联络")
        with self.assertRaises(InputDataError):
            self.store.register_task(area_id="North", action="")

    def test_unknown_task_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.store.update_task("T999", status="done")

    def test_invalid_status_is_rejected(self):
        self.store.register_task(area_id="North", action="联络")
        with self.assertRaises(InputDataError):
            self.store.update_task("T001", status="finished")


class ResourceTests(StoreTestCase):
    def test_resource_lifecycle_is_logged(self):
        resource = self.store.register_resource(
            area_id="North", kind="transport", capacity=2,
            window_from="2026-07-12T14:00", window_to="2026-07-12T20:00",
        )
        self.assertEqual(resource["resource_id"], "R001")
        updated = self.store.update_resource("R001", status="released")
        self.assertEqual(updated["status"], "released")
        state = self.store.state_at(updated["history"][-1]["seq"] - 1)
        self.assertEqual(state["resources"][0]["status"], "active")

    def test_invalid_resource_kind_rejected(self):
        with self.assertRaises(InputDataError):
            self.store.register_resource(area_id="North", kind="teleporter", capacity=1)

    def test_negative_capacity_rejected(self):
        with self.assertRaises(InputDataError):
            self.store.register_resource(area_id="North", kind="transport", capacity=-1)


class ReplayRangeTests(StoreTestCase):
    def test_seq_out_of_range_is_rejected(self):
        with self.assertRaises(NotFoundError):
            self.store.state_at(self.store.data["seq"] + 1)
        with self.assertRaises(NotFoundError):
            self.store.state_at(-1)

    def test_state_at_zero_has_no_tasks(self):
        self.store.register_task(area_id="North", action="联络")
        self.assertEqual(self.store.state_at(0)["tasks"], [])


class LifecycleTests(StoreTestCase):
    def test_close_blocks_further_registration_and_reopen_allows_it(self):
        self.store.close_event(summary="结束")
        with self.assertRaises(ConflictError) as ctx:
            self.store.register_task(area_id="North", action="补录")
        self.assertEqual(ctx.exception.code, "event_closed")
        self.store.reopen_event(reason="补充回执")
        task = self.store.register_task(area_id="North", action="补录")
        self.assertEqual(task["status"], "open")
        self.assertEqual(self.store.current_state()["event"]["status"], "active")

    def test_double_close_rejected(self):
        self.store.close_event()
        with self.assertRaises(ConflictError):
            self.store.close_event()

    def test_reopen_when_active_rejected(self):
        with self.assertRaises(ConflictError):
            self.store.reopen_event()


class StoreFileTests(StoreTestCase):
    def test_store_round_trips(self):
        self.store.add_note("handover", "交接班")
        reopened = Store.open(self.ws.store_path)
        self.assertEqual(reopened.data["seq"], self.store.data["seq"])
        self.assertEqual(reopened.current_state()["notes"][0]["text"], "交接班")

    def test_corrupt_log_detected(self):
        data = json.loads(self.ws.store_path.read_text(encoding="utf-8"))
        data["log"] = data["log"][:-1]
        self.ws.store_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(StorageError):
            Store.open(self.ws.store_path)

    def test_unknown_schema_version_rejected(self):
        data = json.loads(self.ws.store_path.read_text(encoding="utf-8"))
        data["schema_version"] = 99
        self.ws.store_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(StorageError):
            Store.open(self.ws.store_path)

    def test_missing_store_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            Store.open(self.ws.store_path.parent / "nope.json")

    def test_create_over_existing_store_rejected(self):
        with self.assertRaises(ConflictError):
            Store.create(self.ws.store_path, event_id="W1")


if __name__ == "__main__":
    unittest.main()
