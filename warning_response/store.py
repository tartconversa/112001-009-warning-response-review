"""Persistent event workspace.

The store is an append-only log plus a list of immutable input snapshots.
Two consequences matter for the review:

* **Nothing is overwritten.**  Registering a task, changing its status or
  importing a revised dataset adds an event.  ``state_at(seq)`` replays the
  log to reconstruct exactly what the duty team saw at any point.
* **A task remembers what it was based on.**  When a task is registered the
  current priority evidence is copied into the event.  If the data is later
  revised, the review can show both the original evidence and the revision
  instead of silently rewriting history.

File format is a single JSON document written atomically.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .analysis import build_analysis
from .data import load_bundle
from .errors import ConflictError, InputDataError, NotFoundError, StorageError
from .timeutil import format_time

SCHEMA_VERSION = 1
ANALYSIS_ENGINE = "warning-response/1"

TASK_OPEN = "open"
TASK_IN_PROGRESS = "in_progress"
TASK_DONE = "done"
TASK_DROPPED = "dropped"
TASK_STATUSES = {TASK_OPEN, TASK_IN_PROGRESS, TASK_DONE, TASK_DROPPED}

RESOURCE_ACTIVE = "active"
RESOURCE_RELEASED = "released"
RESOURCE_STATUSES = {RESOURCE_ACTIVE, RESOURCE_RELEASED}

RESOURCE_KINDS = {
    "shelter_space": "临时避难点空间",
    "transport": "转运车辆",
    "personnel": "联络/上门人员",
    "supplies": "物资",
    "medical": "医疗支持",
}


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _fingerprint(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _serialise_bundle(bundle, analysis) -> dict:
    """Snapshot the parsed inputs as plain data.

    Two digests are kept: one of the *normalised input* (so a revised file can
    be recognised as identical) and one of the *analysis output* (so a change
    caused by this application's logic is distinguishable from a data
    revision).
    """

    return {
        "raw": {
            "warnings": [_row_dict(row, keys=(
                "warning_id", "version", "issued_at", "area_id", "severity_raw",
                "severity", "valid_from", "valid_to", "channels", "source")) for row in bundle.warnings],
            "delivery": [_row_dict(row, keys=(
                "area_id", "group_id", "exposed_estimate", "channel", "targeted_at",
                "status_raw", "status", "acknowledged_at", "resolution",
                "unsubscribe_reason")) for row in bundle.delivery],
            "shelters": [_row_dict(row, keys=(
                "shelter_id", "area_id", "open_from", "open_to", "capacity",
                "admitted_at", "event_ref")) for row in bundle.shelters],
            "requests": [_row_dict(row, keys=(
                "request_id", "area_id", "requested_at", "received_at", "need",
                "group_id", "related_warning_id", "person_ref", "status_raw",
                "status", "resolved_at", "inability_reason")) for row in bundle.requests],
            "exposures": [_row_dict(row, keys=(
                "exposure_id", "area_id", "group_id", "count", "source",
                "granularity", "as_of")) for row in bundle.exposures],
        },
        "datasets": list(bundle.datasets_found),
        "uncertainties": [item.as_dict() for item in bundle.uncertainties],
        "input_digest": _fingerprint({
            "warnings": sorted(
                (row.warning_id, row.version, format_time(row.issued_at), row.area_id,
                 row.severity, format_time(row.valid_from), format_time(row.valid_to))
                for row in bundle.warnings
            ),
            "delivery": sorted(
                (row.area_id, row.group_id, row.channel, format_time(row.targeted_at),
                 row.status, format_time(row.acknowledged_at))
                for row in bundle.delivery
            ),
            "shelters": sorted(
                (row.shelter_id, row.area_id, format_time(row.open_from),
                 format_time(row.open_to), row.capacity, format_time(row.admitted_at),
                 row.event_ref)
                for row in bundle.shelters
            ),
            "requests": sorted(
                (row.request_id, row.area_id, format_time(row.requested_at),
                 row.need, row.status, format_time(row.resolved_at),
                 row.inability_reason)
                for row in bundle.requests
            ),
            "exposures": sorted(
                (row.area_id, row.group_id, row.count, row.granularity,
                 format_time(row.as_of))
                for row in bundle.exposures
            ),
        }),
        "analysis_digest": _fingerprint(analysis),
    }


def _row_dict(row, *, keys) -> dict:
    out = {}
    for key in keys:
        value = getattr(row, key)
        if isinstance(value, datetime):
            out[key] = format_time(value)
        elif isinstance(value, tuple):
            out[key] = list(value)
        else:
            out[key] = value
    return out


class Store:
    """An append-only analysis workspace on disk."""

    def __init__(self, path: str | Path, data: dict) -> None:
        self.path = Path(path)
        self.data = data

    # ---------------------------------------------------------------- loading
    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        event_id: str,
        event_name: str = "",
        actor: str = "duty",
    ) -> "Store":
        path = Path(path)
        if path.exists():
            raise ConflictError(f"工作区已存在: {path}", code="already_exists")
        data = {
            "schema_version": SCHEMA_VERSION,
            "analysis_engine": ANALYSIS_ENGINE,
            "event": {
                "id": event_id,
                "name": event_name or event_id,
                "created_at": format_time(_now()),
                "closed_at": None,
                "status": "active",
                "created_by": actor,
            },
            "seq": 0,
            "snapshots": [],
            "log": [],
        }
        store = cls(path, data)
        store._append("event_created", {"event_id": event_id, "name": data["event"]["name"]},
                      actor=actor, at=data["event"]["created_at"])
        store.save()
        return store

    @classmethod
    def open(cls, path: str | Path) -> "Store":
        path = Path(path)
        try:
            with path.open(encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            raise NotFoundError(f"工作区不存在: {path}", code="missing_store") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"工作区无法读取: {path}: {exc}") from exc
        cls._validate(data, path)
        return cls(path, data)

    @staticmethod
    def _validate(data: dict, path: Path) -> None:
        if not isinstance(data, dict):
            raise StorageError(f"工作区格式错误（顶层不是对象）: {path}")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise StorageError(
                f"工作区 schema 版本 {data.get('schema_version')!r} 不受支持"
                f"（需要 {SCHEMA_VERSION}）: {path}"
            )
        for key in ("event", "seq", "snapshots", "log"):
            if key not in data:
                raise StorageError(f"工作区缺少字段 {key}: {path}")
        seqs = [item.get("seq") for item in data["log"]]
        if any(not isinstance(seq, int) for seq in seqs):
            raise StorageError(f"工作区日志 seq 不是整数: {path}")
        if sorted(seqs) != list(range(1, len(seqs) + 1)):
            raise StorageError(f"工作区日志序号不连续，可能被损坏: {path}")
        if data["seq"] != len(seqs):
            raise StorageError(
                f"工作区计数器 seq={data['seq']} 与日志长度 {len(seqs)} 不一致，"
                f"可能被截断或手工修改: {path}"
            )

    # ------------------------------------------------------------------ saving
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise StorageError(f"工作区写入失败: {self.path}: {exc}") from exc

    # ---------------------------------------------------------------- appending
    def _append(self, kind: str, payload: dict, *, actor: str, at: str | None = None,
                context: dict | None = None) -> dict:
        self.data["seq"] += 1
        event = {
            "seq": self.data["seq"],
            "at": at or format_time(_now()),
            "kind": kind,
            "actor": actor or "unknown",
            "payload": payload,
        }
        if context is not None:
            event["context"] = context
        self.data["log"].append(event)
        return event

    def _require_active(self) -> None:
        if self.data["event"]["status"] == "closed":
            raise ConflictError("事件已结束，不能继续登记；如需更正请新建复盘工作区。",
                                code="event_closed")

    # ---------------------------------------------------------------- snapshots
    def import_snapshot(
        self,
        data_dir: str | Path,
        *,
        label: str = "",
        actor: str = "duty",
        as_of: str | None = None,
    ) -> dict:
        """Parse a data directory and append it as a new immutable snapshot."""

        self._require_active()
        bundle = load_bundle(Path(data_dir))
        parsed_as_of = None
        if as_of:
            from .timeutil import parse_time

            parsed_as_of = parse_time(as_of, field="--as-of")
        analysis = build_analysis(bundle, as_of=parsed_as_of)
        snapshot = {
            "seq": self.data["seq"] + 1,
            "imported_at": format_time(_now()),
            "label": label or f"资料修订 {len(self.data['snapshots']) + 1}",
            "source_dir": str(Path(data_dir)),
            "datasets": list(bundle.datasets_found),
            "analysis_engine": ANALYSIS_ENGINE,
            **_serialise_bundle(bundle, analysis),
            "analysis": analysis,
        }
        previous = self.data["snapshots"][-1] if self.data["snapshots"] else None
        if previous and previous["input_digest"] == snapshot["input_digest"]:
            raise ConflictError(
                "新导入的资料与上一版本内容相同，未产生修订。", code="no_change"
            )
        self._append(
            "snapshot_imported",
            {
                "snapshot_seq": snapshot["seq"],
                "label": snapshot["label"],
                "source_dir": snapshot["source_dir"],
                "input_digest": snapshot["input_digest"],
                "previous_snapshot_seq": previous["seq"] if previous else None,
                "revision": self._revision_summary(previous, snapshot),
            },
            actor=actor,
        )
        # Snapshots are stored outside the log but share the sequence space.
        self.data["snapshots"].append(snapshot)
        self.save()
        return snapshot

    @staticmethod
    def _revision_summary(previous: dict | None, current: dict) -> dict:
        if previous is None:
            return {"initial": True, "datasets": current["datasets"]}
        changed = []
        before = previous.get("raw", {})
        after = current.get("raw", {})
        for dataset in sorted(set(before) | set(after)):
            if before.get(dataset) != after.get(dataset):
                changed.append(dataset)
        return {
            "initial": False,
            "changed_datasets": changed,
            "uncertainty_added": max(
                0, len(current["uncertainties"]) - len(previous["uncertainties"])
            ),
        }

    def snapshots(self) -> list[dict]:
        return self.data["snapshots"]

    def latest_snapshot(self) -> dict:
        if not self.data["snapshots"]:
            raise ConflictError("尚未导入任何资料，请先导入预警资料目录。",
                                code="no_snapshot")
        snapshot = self.data["snapshots"][-1]
        engine = snapshot.get("analysis_engine")
        if engine and engine != ANALYSIS_ENGINE:
            raise ConflictError(
                f"最新资料快照由分析引擎 {engine} 生成，当前为 {ANALYSIS_ENGINE}；"
                "分析口径可能不同，请重新导入资料后再继续。",
                code="engine_mismatch",
            )
        return snapshot

    def snapshot_at(self, seq: int) -> dict:
        """The latest snapshot that existed at log position ``seq``."""

        candidates = [item for item in self.data["snapshots"] if item["seq"] <= seq]
        if not candidates:
            raise NotFoundError(f"序号 {seq} 之前没有资料快照", code="snapshot_not_found")
        return candidates[-1]

    # -------------------------------------------------------------- replaying
    def state_at(self, seq: int | None = None) -> dict:
        """Reconstruct tasks, resources and notes at a log position."""

        if seq is None:
            seq = self.data["seq"]
        if seq < 0 or seq > self.data["seq"]:
            raise NotFoundError(f"序号 {seq} 超出工作区日志范围 0..{self.data['seq']}",
                                code="seq_out_of_range")
        tasks: dict[str, dict] = {}
        resources: dict[str, dict] = {}
        notes: list[dict] = []
        event_meta = deepcopy(self.data["event"])
        for entry in self.data["log"]:
            if entry["seq"] > seq:
                break
            kind = entry["kind"]
            payload = entry["payload"]
            if kind == "task_registered":
                task = deepcopy(payload["task"])
                task["registered_seq"] = entry["seq"]
                task["registered_at"] = entry["at"]
                task["history"] = [{
                    "seq": entry["seq"],
                    "at": entry["at"],
                    "kind": "registered",
                    "actor": entry["actor"],
                    "status": task["status"],
                    "note": task.get("note", ""),
                }]
                tasks[task["task_id"]] = task
            elif kind == "task_updated":
                task = tasks.get(payload["task_id"])
                if task is None:
                    raise StorageError(
                        f"日志在序号 {entry['seq']} 更新了不存在的任务 {payload['task_id']}"
                    )
                for field in ("status", "owner", "due_at", "note", "outcome"):
                    if field in payload["changes"]:
                        task[field] = payload["changes"][field]
                task["history"].append({
                    "seq": entry["seq"],
                    "at": entry["at"],
                    "kind": "updated",
                    "actor": entry["actor"],
                    "status": task["status"],
                    "note": payload.get("note", ""),
                    "changes": payload["changes"],
                })
            elif kind == "resource_registered":
                resource = deepcopy(payload["resource"])
                resource["registered_seq"] = entry["seq"]
                resource["registered_at"] = entry["at"]
                resource["history"] = [{
                    "seq": entry["seq"], "at": entry["at"], "kind": "registered",
                    "actor": entry["actor"], "status": resource["status"],
                }]
                resources[resource["resource_id"]] = resource
            elif kind == "resource_updated":
                resource = resources.get(payload["resource_id"])
                if resource is None:
                    raise StorageError(
                        f"日志在序号 {entry['seq']} 更新了不存在的资源 {payload['resource_id']}"
                    )
                for field in ("status", "capacity", "window_from", "window_to", "note"):
                    if field in payload["changes"]:
                        resource[field] = payload["changes"][field]
                resource["history"].append({
                    "seq": entry["seq"], "at": entry["at"], "kind": "updated",
                    "actor": entry["actor"], "status": resource["status"],
                    "note": payload.get("note", ""),
                    "changes": payload["changes"],
                })
            elif kind == "note_added":
                notes.append({
                    "seq": entry["seq"], "at": entry["at"], "actor": entry["actor"],
                    "key": payload["key"], "text": payload["text"],
                    "about_snapshot_seq": payload.get("about_snapshot_seq"),
                })
            elif kind == "event_closed":
                event_meta["status"] = "closed"
                event_meta["closed_at"] = entry["at"]
            elif kind == "event_reopened":
                event_meta["status"] = "active"
                event_meta["closed_at"] = None
        return {
            "seq": seq,
            "event": event_meta,
            "tasks": [tasks[key] for key in sorted(tasks)],
            "resources": [resources[key] for key in sorted(resources)],
            "notes": notes,
        }

    def current_state(self) -> dict:
        return self.state_at(self.data["seq"])

    # ------------------------------------------------------------------- tasks
    def register_task(
        self,
        *,
        priority_key: str = "",
        area_id: str,
        group_id: str = "",
        action: str,
        owner: str = "",
        due_at: str | None = None,
        note: str = "",
        actor: str = "duty",
        task_id: str | None = None,
    ) -> dict:
        self._require_active()
        snapshot = self.latest_snapshot()
        if not area_id.strip():
            raise InputDataError("登记任务必须指定社区（area_id）", code="missing_area")
        if not action.strip():
            raise InputDataError("登记任务必须写明动作（action）", code="missing_action")
        task_id = task_id or self._next_task_id()
        if any(item["task_id"] == task_id for item in self.current_state()["tasks"]):
            raise ConflictError(f"任务编号已存在: {task_id}", code="duplicate_task")
        seen = self._evidence_for(snapshot, priority_key, area_id, group_id)
        task = {
            "task_id": task_id,
            "priority_key": priority_key,
            "area_id": area_id,
            "group_id": group_id,
            "action": action,
            "owner": owner,
            "due_at": due_at or "",
            "status": TASK_OPEN,
            "note": note,
            "outcome": "",
            "registered_against_snapshot": snapshot["seq"],
            "seen": seen,
        }
        self._append(
            "task_registered",
            {"task": task},
            actor=actor,
            context={"snapshot_seq": snapshot["seq"], "evidence": seen},
        )
        self.save()
        return task

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        due_at: str | None = None,
        note: str = "",
        outcome: str | None = None,
        actor: str = "duty",
    ) -> dict:
        self._require_active()
        current = {item["task_id"]: item for item in self.current_state()["tasks"]}
        task = current.get(task_id)
        if task is None:
            raise NotFoundError(f"任务不存在: {task_id}", code="task_not_found")
        changes: dict[str, str] = {}
        if status is not None:
            if status not in TASK_STATUSES:
                raise InputDataError(
                    f"任务状态 {status!r} 无效，可选: {', '.join(sorted(TASK_STATUSES))}",
                    code="invalid_task_status",
                )
            changes["status"] = status
        if owner is not None:
            changes["owner"] = owner
        if due_at is not None:
            changes["due_at"] = due_at
        if outcome is not None:
            changes["outcome"] = outcome
        if note:
            changes["note"] = note
        if not changes:
            raise InputDataError("没有需要更新的字段", code="no_changes")
        self._append(
            "task_updated",
            {"task_id": task_id, "changes": changes, "note": note},
            actor=actor,
        )
        self.save()
        return {item["task_id"]: item for item in self.current_state()["tasks"]}[task_id]

    def _next_task_id(self) -> str:
        existing = {item["task_id"] for item in self.current_state()["tasks"]}
        index = 1
        while f"T{index:03d}" in existing:
            index += 1
        return f"T{index:03d}"

    @staticmethod
    def _evidence_for(snapshot: dict, priority_key: str, area_id: str,
                      group_id: str) -> dict:
        """Copy the evidence the duty team could see when they acted."""

        analysis = snapshot["analysis"]
        priority = next(
            (item for item in analysis["priorities"] if item["key"] == priority_key), None
        )
        group = next(
            (item for item in analysis["groups"]
             if item["area_id"] == area_id and item["group_id"] == group_id), None
        )
        area = next(
            (item for item in analysis["areas"] if item["area_id"] == area_id), None
        )
        return {
            "snapshot_seq": snapshot["seq"],
            "snapshot_label": snapshot["label"],
            "as_of": analysis["as_of"],
            "priority": deepcopy(priority),
            "group": deepcopy(group),
            "area": deepcopy(area),
        }

    # ---------------------------------------------------------------- resources
    def register_resource(
        self,
        *,
        area_id: str,
        kind: str,
        capacity: int,
        window_from: str = "",
        window_to: str = "",
        note: str = "",
        actor: str = "duty",
        resource_id: str | None = None,
    ) -> dict:
        self._require_active()
        if kind not in RESOURCE_KINDS:
            raise InputDataError(
                f"临时资源类型 {kind!r} 无效，可选: {', '.join(sorted(RESOURCE_KINDS))}",
                code="invalid_resource_kind",
            )
        if not area_id.strip():
            raise InputDataError("临时资源必须指定社区", code="missing_area")
        if capacity < 0:
            raise InputDataError("临时资源容量不能为负", code="invalid_capacity")
        resource_id = resource_id or self._next_resource_id()
        if any(item["resource_id"] == resource_id
               for item in self.current_state()["resources"]):
            raise ConflictError(f"资源编号已存在: {resource_id}", code="duplicate_resource")
        resource = {
            "resource_id": resource_id,
            "area_id": area_id,
            "kind": kind,
            "kind_label": RESOURCE_KINDS[kind],
            "capacity": capacity,
            "window_from": window_from,
            "window_to": window_to,
            "note": note,
            "status": RESOURCE_ACTIVE,
        }
        self._append("resource_registered", {"resource": resource}, actor=actor)
        self.save()
        return resource

    def update_resource(
        self,
        resource_id: str,
        *,
        status: str | None = None,
        capacity: int | None = None,
        window_from: str | None = None,
        window_to: str | None = None,
        note: str = "",
        actor: str = "duty",
    ) -> dict:
        self._require_active()
        resources = {item["resource_id"]: item
                     for item in self.current_state()["resources"]}
        if resource_id not in resources:
            raise NotFoundError(f"资源不存在: {resource_id}", code="resource_not_found")
        changes: dict = {}
        if status is not None:
            if status not in RESOURCE_STATUSES:
                raise InputDataError(
                    f"资源状态 {status!r} 无效，可选: {', '.join(sorted(RESOURCE_STATUSES))}",
                    code="invalid_resource_status",
                )
            changes["status"] = status
        if capacity is not None:
            if capacity < 0:
                raise InputDataError("临时资源容量不能为负", code="invalid_capacity")
            changes["capacity"] = capacity
        if window_from is not None:
            changes["window_from"] = window_from
        if window_to is not None:
            changes["window_to"] = window_to
        if note:
            changes["note"] = note
        if not changes:
            raise InputDataError("没有需要更新的字段", code="no_changes")
        self._append(
            "resource_updated",
            {"resource_id": resource_id, "changes": changes, "note": note},
            actor=actor,
        )
        self.save()
        return {item["resource_id"]: item
                for item in self.current_state()["resources"]}[resource_id]

    def _next_resource_id(self) -> str:
        existing = {item["resource_id"] for item in self.current_state()["resources"]}
        index = 1
        while f"R{index:03d}" in existing:
            index += 1
        return f"R{index:03d}"

    # -------------------------------------------------------------------- notes
    def add_note(self, key: str, text: str, *, actor: str = "duty") -> dict:
        self._require_active()
        if not text.strip():
            raise InputDataError("记录内容不能为空", code="empty_note")
        snapshot = self.latest_snapshot()
        event = self._append(
            "note_added",
            {"key": key, "text": text, "about_snapshot_seq": snapshot["seq"]},
            actor=actor,
        )
        self.save()
        return event

    # ------------------------------------------------------------- lifecycle
    def close_event(self, *, summary: str = "", actor: str = "duty") -> dict:
        if self.data["event"]["status"] == "closed":
            raise ConflictError("事件已经结束", code="already_closed")
        self.latest_snapshot()
        event = self._append("event_closed", {"summary": summary}, actor=actor)
        self.data["event"]["status"] = "closed"
        self.data["event"]["closed_at"] = event["at"]
        self.save()
        return event

    def reopen_event(self, *, reason: str = "", actor: str = "duty") -> dict:
        if self.data["event"]["status"] != "closed":
            raise ConflictError("事件尚未结束，无需重新打开", code="not_closed")
        event = self._append("event_reopened", {"reason": reason}, actor=actor)
        self.data["event"]["status"] = "active"
        self.data["event"]["closed_at"] = None
        self.save()
        return event
