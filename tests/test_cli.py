"""End-to-end CLI behaviour."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

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

from warning_response.cli import main


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)
        self.store = str(self.ws.store_path)
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])

    def tearDown(self):
        self._tmp.cleanup()

    def init_and_import(self):
        self.assertEqual(run("--store", self.store, "init", "--event", "W1")[0], 0)
        code, out, _ = run("--store", self.store, "import", str(self.ws.data_dir),
                           "--label", "初始")
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_full_flow(self):
        imported = self.init_and_import()
        self.assertEqual(imported["label"], "初始")

        code, out, _ = run("--store", self.store, "overview")
        self.assertEqual(code, 0)
        overview = json.loads(out)
        self.assertEqual(overview["snapshot"]["label"], "初始")

        code, out, _ = run("--store", self.store, "priorities", "--urgency", "high")
        self.assertEqual(code, 0)
        self.assertTrue(all(item["urgency"] == "high" for item in json.loads(out)))

        code, out, _ = run(
            "--store", self.store, "task-add",
            "--area", "North", "--group", "older_adults",
            "--action", "上门确认", "--owner", "一组",
            "--priority-key", "contact:North:older_adults",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["task"]["task_id"], "T001")

        code, out, _ = run("--store", self.store, "task-update", "T001",
                           "--status", "done", "--outcome", "已确认")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "done")

        code, out, _ = run(
            "--store", self.store, "resource-add", "--area", "North",
            "--kind", "transport", "--capacity", "2",
            "--window-from", "2026-07-12T14:00",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["resource_id"], "R001")

        review_path = self.ws.root / "review.md"
        code, out, _ = run("--store", self.store, "review", "--markdown", str(review_path))
        self.assertEqual(code, 0)
        text = review_path.read_text(encoding="utf-8")
        self.assertIn("预警响应复盘", text)
        self.assertIn("T001", text)
        self.assertIn("R001", text)

    def test_snapshot_query_by_seq(self):
        imported = self.init_and_import()
        seq = imported["snapshot_seq"]
        code, out, _ = run("--store", self.store, "groups", "--snapshot", str(seq))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out))

    def test_error_paths_return_exit_code_two_and_stderr(self):
        code, out, err = run("--store", self.store, "overview")
        self.assertEqual(code, 2)
        self.assertIn("工作区不存在", err)

        self.init_and_import()
        code, out, err = run("--store", self.store, "task-update", "T999", "--status", "done")
        self.assertEqual(code, 2)
        self.assertIn("[task_not_found]", err)

    def test_bad_data_import_reports_reason(self):
        run("--store", self.store, "init", "--event", "W1")
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings(valid_to="oops"))
        code, out, err = run("--store", self.store, "import", str(self.ws.data_dir))
        self.assertEqual(code, 2)
        self.assertIn("错误 [", err)
        self.assertIn("invalid_timestamp", err)

    def test_close_then_review_marks_closed(self):
        self.init_and_import()
        code, _, _ = run("--store", self.store, "close", "--summary", "结束")
        self.assertEqual(code, 0)
        code, out, _ = run("--store", self.store, "review", "--markdown",
                           str(self.ws.root / "r.md"))
        self.assertEqual(code, 0)
        self.assertIn("已结束", (self.ws.root / "r.md").read_text(encoding="utf-8"))
        code, out, err = run("--store", self.store, "task-add", "--area", "North",
                             "--action", "补录")
        self.assertEqual(code, 2)
        self.assertIn("event_closed", err)

    def test_actor_is_recorded(self):
        self.init_and_import()
        run("--store", self.store, "task-add", "--area", "North", "--action", "联络",
            "--actor", "值班员甲")
        code, out, _ = run("--store", self.store, "tasks")
        task = json.loads(out)[0]
        self.assertEqual(task["history"][0]["actor"], "值班员甲")

    def test_diff_requires_two_versions(self):
        self.init_and_import()
        code, out, err = run("--store", self.store, "diff")
        self.assertEqual(code, 2)
        self.assertIn("no_revision", err)


if __name__ == "__main__":
    unittest.main()
