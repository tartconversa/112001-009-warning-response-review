"""Data loading and validation rules."""

from __future__ import annotations

import tempfile
import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (
    DELIVERY_HEADER,
    FIXTURE_DIR,
    EXPOSURE_HEADER,
    GROUP_HEADER,
    SHELTER_HEADER,
    WARNING_HEADER,
    base_warnings,
    delivery_row,
    make_workspace,
    shelter_row,
)

from warning_response.data import load_bundle
from warning_response.errors import InputDataError


class FixtureLoadingTests(unittest.TestCase):
    def test_bundled_fixtures_load(self):
        bundle = load_bundle(FIXTURE_DIR)
        self.assertEqual(len(bundle.warnings), 3)
        self.assertEqual(len(bundle.delivery), 3)
        self.assertEqual(len(bundle.shelters), 3)
        self.assertTrue(bundle.exposures)
        # The fixtures have no exposure file, so exposure is derived and must
        # be flagged as such.
        self.assertIn("synthesized_exposures", bundle.datasets_found)
        self.assertTrue(any("推导" in item.detail for item in bundle.uncertainties))

    def test_derived_exposure_is_aggregate(self):
        bundle = load_bundle(FIXTURE_DIR)
        for exposure in bundle.exposures:
            self.assertEqual(exposure.granularity, "aggregate")
            self.assertFalse(exposure.individually_identified)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = make_workspace(self._tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _minimal(self):
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        self.ws.write("population_delivery.csv", DELIVERY_HEADER, [delivery_row()])
        self.ws.write("shelters_response.csv", SHELTER_HEADER, [shelter_row()])

    def test_minimal_bundle_loads(self):
        self._minimal()
        bundle = load_bundle(self.ws.data_dir)
        self.assertEqual(bundle.datasets_found[:3],
                         ["warnings", "population_delivery", "shelters_response"])

    def test_missing_required_file_is_reported(self):
        self.ws.write("warnings.csv", WARNING_HEADER, base_warnings())
        with self.assertRaises(InputDataError) as ctx:
            load_bundle(self.ws.data_dir)
        self.assertEqual(ctx.exception.code, "missing_file")
        self.assertIn("delivery", str(ctx.exception))

    def test_ragged_row_rejected_with_line_number(self):
        self._minimal()
        path = self.ws.data_dir / "population_delivery.csv"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("North,all,10\n")
        with self.assertRaises(InputDataError) as ctx:
            load_bundle(self.ws.data_dir)
        self.assertEqual(ctx.exception.code, "ragged_row")
        self.assertIn("第 3 行", str(ctx.exception))

    def test_duplicate_column_rejected(self):
        self._minimal()
        self.ws.write(
            "warnings.csv",
            WARNING_HEADER + ["severity"],
            [base_warnings()[0] + ["warning"]],
        )
        with self.assertRaises(InputDataError) as ctx:
            load_bundle(self.ws.data_dir)
        self.assertEqual(ctx.exception.code, "bad_header")

    def test_duplicate_warning_version_area_rejected(self):
        self._minimal()
        self.ws.write("warnings.csv", WARNING_HEADER,
                      base_warnings() + base_warnings())
        with self.assertRaises(InputDataError) as ctx:
            load_bundle(self.ws.data_dir)
        self.assertEqual(ctx.exception.code, "duplicate_record")

    def test_timezone_aware_timestamp_rejected(self):
        self._minimal()
        self.ws.write("warnings.csv", WARNING_HEADER,
                      base_warnings(issued_at="2026-07-12T09:00+08:00"))
        with self.assertRaises(InputDataError) as ctx:
            load_bundle(self.ws.data_dir)
        self.assertEqual(ctx.exception.code, "invalid_timestamp")

    def test_invalid_interval_rejected(self):
        self._minimal()
        self.ws.write("warnings.csv", WARNING_HEADER,
                      base_warnings(valid_from="2026-07-12T18:00",
                                    valid_to="2026-07-12T10:00"))
        with self.assertRaises(InputDataError) as ctx:
            load_bundle(self.ws.data_dir)
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_negative_capacity_rejected(self):
        self._minimal()
        self.ws.write("shelters_response.csv", SHELTER_HEADER,
                      [shelter_row(capacity="-5")])
        with self.assertRaises(InputDataError) as ctx:
            load_bundle(self.ws.data_dir)
        self.assertEqual(ctx.exception.code, "invalid_number")

    def test_unknown_delivery_status_becomes_uncertainty_not_success(self):
        self._minimal()
        self.ws.write("population_delivery.csv", DELIVERY_HEADER,
                      [delivery_row(status="teleported")])
        bundle = load_bundle(self.ws.data_dir)
        self.assertEqual(bundle.delivery[0].status, "unknown")
        self.assertTrue(any("无法识别" in item.detail for item in bundle.uncertainties))

    def test_acknowledged_before_targeted_is_discarded_with_warning(self):
        self._minimal()
        self.ws.write(
            "population_delivery.csv", DELIVERY_HEADER,
            [delivery_row(status="acknowledged",
                          targeted_at="2026-07-12T09:30",
                          acknowledged_at="2026-07-12T09:00")],
        )
        bundle = load_bundle(self.ws.data_dir)
        self.assertIsNone(bundle.delivery[0].acknowledged_at)
        self.assertTrue(any("确认时间早于发送时间" in item.detail
                            for item in bundle.uncertainties))

    def test_admission_before_shelter_open_discarded_with_warning(self):
        self._minimal()
        self.ws.write("shelters_response.csv", SHELTER_HEADER,
                      [shelter_row(open_from="2026-07-12T12:00",
                                   admitted_at="2026-07-12T11:00")])
        bundle = load_bundle(self.ws.data_dir)
        self.assertIsNone(bundle.shelters[0].admitted_at)
        self.assertTrue(any("早于避难点开放时间" in item.detail
                            for item in bundle.uncertainties))

    def test_existing_exposure_file_is_used_over_derivation(self):
        self._minimal()
        self.ws.write("exposures.csv", EXPOSURE_HEADER, [
            ["E1", "North", "older_adults", "500", "census", "aggregate",
             "2026-07-12T08:00"],
        ])
        self.ws.write("groups.csv", GROUP_HEADER, [
            ["older_adults", "老年人", "500", "census", "aggregate"],
        ])
        bundle = load_bundle(self.ws.data_dir)
        self.assertEqual(len(bundle.exposures), 1)
        self.assertEqual(bundle.exposures[0].count, 500)
        self.assertNotIn("synthesized_exposures", bundle.datasets_found)
        self.assertEqual(bundle.groups["older_adults"].label, "老年人")

    def test_legacy_towns_alias_still_loads(self):
        self._minimal()
        self.ws.write("towns.csv", ["area_id", "group_id", "count", "source"], [
            ["North", "all", "8400", "census"],
        ])
        bundle = load_bundle(self.ws.data_dir)
        self.assertEqual(len(bundle.exposures), 1)
        self.assertIn("towns", bundle.datasets_found)

    def test_header_aliases_are_normalised(self):
        self._minimal()
        self.ws.write(
            "population_delivery.csv",
            ["region", "population_group", "population", "route", "sent_at",
             "delivery_status", "confirmed_at"],
            [["North", "older_adults", "480", "sms", "2026-07-12T09:15",
              "delivered", "2026-07-12T09:40"]],
        )
        bundle = load_bundle(self.ws.data_dir)
        self.assertEqual(bundle.delivery[0].area_id, "North")
        self.assertEqual(bundle.delivery[0].status, "delivered")

    def test_utf8_bom_header_is_accepted(self):
        self._minimal()
        path = self.ws.data_dir / "warnings.csv"
        text = path.read_text(encoding="utf-8")
        path.write_text("﻿" + text, encoding="utf-8")
        bundle = load_bundle(self.ws.data_dir)
        self.assertEqual(len(bundle.warnings), 1)


if __name__ == "__main__":
    unittest.main()
