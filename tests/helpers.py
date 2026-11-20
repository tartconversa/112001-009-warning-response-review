"""Shared helpers for tests."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "data"

WARNING_HEADER = ["warning_id", "version", "issued_at", "area_id", "severity",
                  "valid_from", "valid_to"]
DELIVERY_HEADER = ["area_id", "group_id", "exposed_estimate", "channel", "targeted_at",
                   "status", "acknowledged_at"]
SHELTER_HEADER = ["shelter_id", "area_id", "open_from", "open_to", "capacity",
                  "admitted_at", "event_ref"]
REQUEST_HEADER = ["request_id", "area_id", "requested_at", "need", "group_id",
                  "related_warning_id", "person_ref", "status", "resolved_at",
                  "inability_reason"]
EXPOSURE_HEADER = ["exposure_id", "area_id", "group_id", "count", "source",
                   "granularity", "as_of"]
GROUP_HEADER = ["group_id", "label", "count", "source", "granularity"]


class Workspace:
    """A temp directory holding editable datasets and a store path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_path = root / "work.json"

    def write(self, name: str, header: list[str], rows: list[list]) -> Path:
        path = self.data_dir / name
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def copy_fixtures(self) -> None:
        for path in FIXTURE_DIR.glob("*.csv"):
            (self.data_dir / path.name).write_bytes(path.read_bytes())

    def read_store(self) -> dict:
        return json.loads(self.store_path.read_text(encoding="utf-8"))


def make_workspace(tmp: tempfile.TemporaryDirectory) -> Workspace:
    return Workspace(Path(tmp.name))


def base_warnings(**overrides) -> list[list]:
    row = {
        "warning_id": "W1",
        "version": 1,
        "issued_at": "2026-07-12T09:00",
        "area_id": "North",
        "severity": "watch",
        "valid_from": "2026-07-12T10:00",
        "valid_to": "2026-07-12T18:00",
    }
    row.update(overrides)
    return [[row[key] for key in WARNING_HEADER]]


def delivery_row(**overrides) -> list:
    row = {
        "area_id": "North",
        "group_id": "older_adults",
        "exposed_estimate": 100,
        "channel": "sms",
        "targeted_at": "2026-07-12T09:15",
        "status": "delivered",
        "acknowledged_at": "",
    }
    row.update(overrides)
    return [row[key] for key in DELIVERY_HEADER]


def shelter_row(**overrides) -> list:
    row = {
        "shelter_id": "SH01",
        "area_id": "North",
        "open_from": "2026-07-12T10:00",
        "open_to": "2026-07-12T18:00",
        "capacity": 100,
        "admitted_at": "",
        "event_ref": "W1",
    }
    row.update(overrides)
    return [row[key] for key in SHELTER_HEADER]


def request_row(**overrides) -> list:
    row = {
        "request_id": "Q1",
        "area_id": "North",
        "requested_at": "2026-07-12T11:00",
        "need": "evacuation",
        "group_id": "older_adults",
        "related_warning_id": "W1",
        "person_ref": "anonymised-case-1",
        "status": "open",
        "resolved_at": "",
        "inability_reason": "no_transport",
    }
    row.update(overrides)
    return [row[key] for key in REQUEST_HEADER]
