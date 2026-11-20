"""Parsing of the five input datasets.

The loaders normalise header names, validate types and reject contradictory
rows early.  They never silently drop a record: every skipped or doubtful
input is reported as an ``Uncertainty`` so the review can state what was
excluded and why.

Input shapes (all CSV unless noted):

``warnings.csv``
    warning_id, version, issued_at, area_id, severity, valid_from, valid_to [, source]
``population_delivery.csv``
    area_id, group_id, exposed_estimate, channel, targeted_at, status,
    acknowledged_at, [, resolution, unsubscribe_reason]
``shelters_response.csv``
    shelter_id, area_id, open_from, open_to, capacity, admitted_at, event_ref
``assistance_requests.csv`` (optional)
    request_id, area_id, requested_at, need, group_id, related_warning_id,
    person_ref, status, resolved_at, inability_reason [, received_at]
``population_groups.csv`` (optional)
    group_id, label, count, source, granularity [, risk_notes]
``exposures.csv`` (optional, preferred)
    exposure_id, area_id, group_id, count, source, granularity, as_of
``towns.csv`` (optional, legacy alias of exposures.csv)
    area_id, group_id, count, source
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .errors import InputDataError
from .timeutil import parse_time
from .values import (
    STATUS_AMBIGUOUS,
    STATUS_UNKNOWN,
    resolve_delivery_status,
    resolve_granularity,
    resolve_request_status,
    resolve_severity,
)

REQUIRED_COLUMNS = {
    "warnings": (
        "warning_id", "version", "issued_at", "area_id", "severity",
        "valid_from", "valid_to",
    ),
    "delivery": (
        "area_id", "group_id", "exposed_estimate", "channel", "targeted_at",
        "status", "acknowledged_at",
    ),
    "shelters": (
        "shelter_id", "area_id", "open_from", "open_to", "capacity",
        "admitted_at", "event_ref",
    ),
    "requests": (
        "request_id", "area_id", "requested_at", "need", "group_id",
        "related_warning_id", "person_ref", "status", "resolved_at",
        "inability_reason",
    ),
    "groups": ("group_id", "label", "count", "source", "granularity"),
    "exposures": ("exposure_id", "area_id", "group_id", "count", "source", "granularity", "as_of"),
}

HEADER_ALIASES = {
    "warning_id": ("warning_id", "warning", "id"),
    "version": ("version", "warning_version"),
    "issued_at": ("issued_at", "issued", "sent_at"),
    "area_id": ("area_id", "area", "region", "community"),
    "severity": ("severity", "level"),
    "valid_from": ("valid_from", "effective_from", "starts_at"),
    "valid_to": ("valid_to", "effective_to", "ends_at"),
    "channels": ("channels", "channel_list", "routes"),
    "source": ("source", "publisher"),
    "group_id": ("group_id", "group", "population_group"),
    "exposed_estimate": ("exposed_estimate", "exposed", "population", "count"),
    "channel": ("channel", "route", "medium"),
    "targeted_at": ("targeted_at", "targeted", "sent_at"),
    "status": ("status", "delivery_status", "request_status"),
    "acknowledged_at": ("acknowledged_at", "acknowledged", "confirmed_at"),
    "resolution": ("resolution", "outcome"),
    "unsubscribe_reason": ("unsubscribe_reason", "opt_out_reason"),
    "shelter_id": ("shelter_id", "shelter", "facility_id"),
    "open_from": ("open_from", "opened_at", "open"),
    "open_to": ("open_to", "closes_at", "close"),
    "capacity": ("capacity", "beds", "max_occupancy"),
    "admitted_at": ("admitted_at", "admitted", "arrived_at"),
    "event_ref": ("event_ref", "event", "event_id"),
    "request_id": ("request_id", "request", "call_id"),
    "requested_at": ("requested_at", "requested", "call_at"),
    "received_at": ("received_at", "received", "logged_at"),
    "need": ("need", "need_type", "request_need"),
    "related_warning_id": ("related_warning_id", "related_warning", "warning_ref"),
    "person_ref": ("person_ref", "person", "case_ref"),
    "resolved_at": ("resolved_at", "resolved", "closed_at"),
    "inability_reason": ("inability_reason", "no_help_reason", "failure_reason"),
    "label": ("label", "name"),
    "count": ("count", "population", "exposed_estimate"),
    "granularity": ("granularity", "level", "resolution_level"),
    "as_of": ("as_of", "observed_at", "measured_at"),
    "risk_notes": ("risk_notes", "notes", "description"),
    "exposure_id": ("exposure_id", "id"),
}

# Which canonical columns each dataset may contain.  Restricting aliases per
# dataset avoids collisions such as ``sent_at`` meaning ``issued_at`` in
# warnings but ``targeted_at`` in delivery.
ALLOWED_COLUMNS = {
    "warnings": {"warning_id", "version", "issued_at", "area_id", "severity",
                 "valid_from", "valid_to", "channels", "source"},
    "delivery": {"area_id", "group_id", "exposed_estimate", "channel",
                 "targeted_at", "status", "acknowledged_at", "resolution",
                 "unsubscribe_reason"},
    "shelters": {"shelter_id", "area_id", "open_from", "open_to", "capacity",
                 "admitted_at", "event_ref"},
    "requests": {"request_id", "area_id", "requested_at", "received_at", "need",
                 "group_id", "related_warning_id", "person_ref", "status",
                 "resolved_at", "inability_reason"},
    "groups": {"group_id", "label", "count", "source", "granularity", "risk_notes"},
    "exposures": {"exposure_id", "area_id", "group_id", "count", "source",
                  "granularity", "as_of"},
}


class Row(dict):
    """A CSV row with field access that reports the offending column."""

    def __init__(self, data: dict, *, dataset: str, line: int) -> None:
        super().__init__(data)
        self.dataset = dataset
        self.line = line

    def raw(self, name: str) -> str:
        return (self.get(name) or "").strip()

    def required(self, name: str) -> str:
        value = self.raw(name)
        if not value:
            raise InputDataError(
                f"{self.dataset} 第 {self.line} 行缺少必填字段 {name}",
                code="missing_field",
            )
        return value

    def time(self, name: str, *, allow_empty: bool = False):
        return parse_time(
            self.raw(name),
            field=f"{self.dataset} 第 {self.line} 行 {name}",
            allow_empty=allow_empty,
        )

    def integer(self, name: str, *, minimum: int | None = None) -> int:
        text = self.required(name)
        try:
            value = int(text)
        except ValueError as exc:
            raise InputDataError(
                f"{self.dataset} 第 {self.line} 行 {name} 不是整数: {text!r}",
                code="invalid_number",
            ) from exc
        if minimum is not None and value < minimum:
            raise InputDataError(
                f"{self.dataset} 第 {self.line} 行 {name} 不能小于 {minimum}: {value}",
                code="invalid_number",
            )
        return value

    def where(self) -> str:
        return f"{self.dataset} 第 {self.line} 行"


@dataclass
class Uncertainty:
    """A record-level doubt that must survive into the review."""

    dataset: str
    detail: str
    severity: str = "info"
    row: str = ""

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "row": self.row,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass
class Observation:
    """An immutable fact from one version of one dataset.

    ``event_seq`` is the review-wide monotonic sequence of the version this
    observation belongs to; it is assigned by the repository.
    """

    dataset: str
    key: str
    value: dict
    event_seq: int = 0

    def as_dict(self) -> dict:
        return {"dataset": self.dataset, "key": self.key, "value": self.value}


@dataclass
class WarningRow:
    warning_id: str
    version: int
    issued_at: object
    area_id: str
    severity_raw: str
    severity: str
    severity_unknown: bool
    valid_from: object
    valid_to: object
    channels: tuple[str, ...] = ()
    source: str = ""
    row: str = ""

    def key(self) -> str:
        return f"{self.warning_id}|{self.version}|{self.area_id}"


@dataclass
class DeliveryRow:
    area_id: str
    group_id: str
    exposed_estimate: int
    channel: str
    targeted_at: object
    status_raw: str
    status: str
    acknowledged_at: object
    resolution: str = ""
    unsubscribe_reason: str = ""
    row: str = ""

    def key(self) -> str:
        return (
            f"{self.area_id}|{self.group_id}|{self.channel}|"
            f"{self.targeted_at.isoformat()}"
        )


@dataclass
class ShelterRow:
    shelter_id: str
    area_id: str
    open_from: object
    open_to: object
    capacity: int
    admitted_at: object
    event_ref: str
    row: str = ""

    def key(self) -> str:
        # A shelter window may appear on several rows, once per admission or
        # capacity revision.  Only fully identical rows are duplicates; the
        # analysis reports conflicting capacities explicitly.
        admitted = self.admitted_at.isoformat() if self.admitted_at else ""
        return (f"{self.shelter_id}|{self.event_ref}|{self.open_from.isoformat()}"
                f"|{admitted}|{self.capacity}")


@dataclass
class RequestRow:
    request_id: str
    area_id: str
    requested_at: object
    received_at: object
    need: str
    group_id: str
    related_warning_id: str
    person_ref: str
    status_raw: str
    status: str
    resolved_at: object
    inability_reason: str
    row: str = ""

    def key(self) -> str:
        return self.request_id


@dataclass
class PopulationGroup:
    group_id: str
    label: str
    count: int
    source: str
    granularity: str
    risk_notes: str = ""
    row: str = ""


@dataclass
class Exposure:
    exposure_id: str
    area_id: str
    group_id: str
    count: int
    source: str
    granularity: str
    as_of: object
    row: str = ""

    def key(self) -> str:
        if self.exposure_id:
            return self.exposure_id
        return f"{self.area_id}|{self.group_id}|{self.as_of.isoformat() if self.as_of else ''}"

    @property
    def aggregate(self) -> bool:
        """True when the count describes a group, never an individual."""
        return self.granularity in {"aggregate", "street", "district", "neighbourhood"}

    @property
    def individually_identified(self) -> bool:
        return self.granularity in {"individual", "roster", "named"}


@dataclass
class DatasetBundle:
    warnings: list[WarningRow] = field(default_factory=list)
    delivery: list[DeliveryRow] = field(default_factory=list)
    shelters: list[ShelterRow] = field(default_factory=list)
    requests: list[RequestRow] = field(default_factory=list)
    groups: dict[str, PopulationGroup] = field(default_factory=dict)
    exposures: list[Exposure] = field(default_factory=list)
    warnings_from_manifest: bool = False
    uncertainties: list[Uncertainty] = field(default_factory=list)
    datasets_found: list[str] = field(default_factory=list)


def read_csv_rows(path: Path, dataset: str) -> tuple[list[Row], list[str]]:
    """Read a CSV with header normalisation.

    Duplicate headers are an error because they make column mapping
    ambiguous.  Blank rows are ignored.  Two different headers mapping to the
    same canonical name is also an error.
    """

    try:
        stream = path.open(encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise InputDataError(f"无法读取 {dataset} 文件 {path}: {exc}", code="unreadable") from exc
    allowed = ALLOWED_COLUMNS.get(dataset, set(HEADER_ALIASES))
    with stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            raise InputDataError(f"{dataset} 文件为空: {path}", code="empty_file") from None
        normalised = [_canonical_header(cell, allowed=allowed) for cell in header]
        if any(not cell for cell in normalised):
            raise InputDataError(f"{dataset} 表头包含空列名: {path}", code="bad_header")
        duplicates = {name for name in normalised if normalised.count(name) > 1}
        if duplicates:
            raise InputDataError(
                f"{dataset} 表头包含重复或同义重复的列: {', '.join(sorted(duplicates))}",
                code="bad_header",
            )
        rows: list[Row] = []
        for line, values in enumerate(reader, start=2):
            if not any(cell.strip() for cell in values):
                continue
            if len(values) != len(header):
                raise InputDataError(
                    f"{dataset} 第 {line} 行有 {len(values)} 列，表头有 {len(header)} 列",
                    code="ragged_row",
                )
            rows.append(Row(dict(zip(normalised, values)), dataset=dataset, line=line))
    return rows, normalised


def _canonical_header(cell: str, *, allowed: set[str] | None = None) -> str:
    value = (cell or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not value:
        return ""
    candidates = [name for name, aliases in HEADER_ALIASES.items()
                  if value in aliases and (allowed is None or name in allowed)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise InputDataError(
            f"表头 {cell!r} 有多个含义（{', '.join(sorted(candidates))}），无法确定列用途",
            code="ambiguous_header",
        )
    return value


def _require_columns(columns: list[str], required: tuple[str, ...], dataset: str, path: Path) -> None:
    missing = [name for name in required if name not in columns]
    if missing:
        raise InputDataError(
            f"{dataset} 缺少必要列: {', '.join(missing)}（文件 {path.name}）",
            code="missing_column",
        )


def _check_duplicate_keys(rows: list, dataset: str, key_name: str) -> None:
    seen: dict[str, str] = {}
    for row in rows:
        key = row.key()
        if key in seen:
            raise InputDataError(
                f"{dataset} 存在重复记录键 {key_name}={key!r}"
                f"（{seen[key]} 与 {row.row}）",
                code="duplicate_record",
            )
        seen[key] = row.row


def load_warnings(path: Path) -> tuple[list[WarningRow], list[Uncertainty]]:
    rows, columns = read_csv_rows(path, "warnings")
    _require_columns(columns, REQUIRED_COLUMNS["warnings"], "warnings", path)
    uncertainties: list[Uncertainty] = []
    parsed: list[WarningRow] = []
    for row in rows:
        severity_raw = row.required("severity")
        severity = resolve_severity(severity_raw)
        unknown = severity == STATUS_UNKNOWN
        if unknown:
            uncertainties.append(
                Uncertainty(
                    "warnings",
                    f"预警 {row.raw('warning_id')} 的强度 {severity_raw!r} 无法识别，"
                    "按未知强度处理，相关覆盖不作已确认结论。",
                    severity="warning",
                    row=row.where(),
                )
            )
        item = WarningRow(
            warning_id=row.required("warning_id"),
            version=row.integer("version", minimum=1),
            issued_at=row.time("issued_at"),
            area_id=row.required("area_id"),
            severity_raw=severity_raw,
            severity=severity,
            severity_unknown=unknown,
            valid_from=row.time("valid_from"),
            valid_to=row.time("valid_to"),
            channels=tuple(
                c.strip() for c in row.raw("channels").split(";") if c.strip()
            ),
            source=row.raw("source"),
            row=row.where(),
        )
        if item.valid_to < item.valid_from:
            raise InputDataError(
                f"{item.row} 的有效结束时间早于开始时间", code="invalid_interval"
            )
        if item.issued_at > item.valid_to:
            uncertainties.append(
                Uncertainty(
                    "warnings",
                    f"预警 {item.warning_id} v{item.version} 的发布时间晚于失效时间，"
                    "时间线需人工核对。",
                    severity="warning",
                    row=item.row,
                )
            )
        parsed.append(item)
    _check_duplicate_keys(parsed, "warnings", "warning_id+version+area_id")
    return parsed, uncertainties


def load_delivery(path: Path) -> tuple[list[DeliveryRow], list[Uncertainty]]:
    rows, columns = read_csv_rows(path, "delivery")
    _require_columns(columns, REQUIRED_COLUMNS["delivery"], "delivery", path)
    uncertainties: list[Uncertainty] = []
    parsed: list[DeliveryRow] = []
    for row in rows:
        status_raw = row.required("status")
        status = resolve_delivery_status(status_raw)
        if status in {STATUS_AMBIGUOUS, STATUS_UNKNOWN}:
            detail = (
                f"回执状态 {status_raw!r} 含义不明确，按「未确认」处理，不视为送达成功。"
                if status == STATUS_AMBIGUOUS
                else f"回执状态 {status_raw!r} 无法识别，按「未确认」处理。"
            )
            uncertainties.append(
                Uncertainty(
                    "delivery",
                    f"{row.where()}：{detail}",
                    severity="warning",
                    row=row.where(),
                )
            )
        acknowledged = row.time("acknowledged_at", allow_empty=True)
        targeted = row.time("targeted_at")
        if acknowledged is not None and acknowledged < targeted:
            uncertainties.append(
                Uncertainty(
                    "delivery",
                    f"{row.where()}：确认时间早于发送时间，确认记录存疑，按未确认处理。",
                    severity="warning",
                    row=row.where(),
                )
            )
            acknowledged = None
        item = DeliveryRow(
            area_id=row.required("area_id"),
            group_id=row.required("group_id"),
            exposed_estimate=row.integer("exposed_estimate", minimum=0),
            channel=row.required("channel"),
            targeted_at=targeted,
            status_raw=status_raw,
            status=status,
            acknowledged_at=acknowledged,
            resolution=row.raw("resolution"),
            unsubscribe_reason=row.raw("unsubscribe_reason"),
            row=row.where(),
        )
        parsed.append(item)
    _check_duplicate_keys(parsed, "delivery", "area+group+channel+targeted_at")
    return parsed, uncertainties


def load_shelters(path: Path) -> tuple[list[ShelterRow], list[Uncertainty]]:
    rows, columns = read_csv_rows(path, "shelters")
    _require_columns(columns, REQUIRED_COLUMNS["shelters"], "shelters", path)
    uncertainties: list[Uncertainty] = []
    parsed: list[ShelterRow] = []
    for row in rows:
        open_from = row.time("open_from")
        open_to = row.time("open_to", allow_empty=True)
        if open_to is not None and open_to < open_from:
            raise InputDataError(
                f"{row.where()} 的关闭时间早于开放时间", code="invalid_interval"
            )
        admitted = row.time("admitted_at", allow_empty=True)
        if admitted is not None and admitted < open_from:
            uncertainties.append(
                Uncertainty(
                    "shelters",
                    f"{row.where()}：接纳时间早于避难点开放时间，容量占用存疑，"
                    "本次不计入已接纳人数。",
                    severity="warning",
                    row=row.where(),
                )
            )
            admitted = None
        item = ShelterRow(
            shelter_id=row.required("shelter_id"),
            area_id=row.required("area_id"),
            open_from=open_from,
            open_to=open_to,
            capacity=row.integer("capacity", minimum=0),
            admitted_at=admitted,
            event_ref=row.raw("event_ref"),
            row=row.where(),
        )
        parsed.append(item)
    _check_duplicate_keys(parsed, "shelters", "shelter_id+event_ref+open_from")
    return parsed, uncertainties


def load_requests(path: Path) -> tuple[list[RequestRow], list[Uncertainty]]:
    rows, columns = read_csv_rows(path, "requests")
    _require_columns(columns, REQUIRED_COLUMNS["requests"], "requests", path)
    uncertainties: list[Uncertainty] = []
    parsed: list[RequestRow] = []
    for row in rows:
        status_raw = row.required("status")
        status = resolve_request_status(status_raw)
        if status == STATUS_UNKNOWN:
            uncertainties.append(
                Uncertainty(
                    "requests",
                    f"{row.where()}：求助状态 {status_raw!r} 无法识别，按「未解决」处理。",
                    severity="warning",
                    row=row.where(),
                )
            )
        requested = row.time("requested_at")
        received = row.time("received_at", allow_empty=True) or requested
        resolved = row.time("resolved_at", allow_empty=True)
        if resolved is not None and resolved < received:
            uncertainties.append(
                Uncertainty(
                    "requests",
                    f"{row.where()}：解决时间早于受理时间，按未解决处理。",
                    severity="warning",
                    row=row.where(),
                )
            )
            resolved = None
            status = STATUS_UNKNOWN
        parsed.append(
            RequestRow(
                request_id=row.required("request_id"),
                area_id=row.required("area_id"),
                requested_at=requested,
                received_at=received,
                need=row.required("need"),
                group_id=row.raw("group_id"),
                related_warning_id=row.raw("related_warning_id"),
                person_ref=row.raw("person_ref"),
                status_raw=status_raw,
                status=status,
                resolved_at=resolved,
                inability_reason=row.raw("inability_reason"),
                row=row.where(),
            )
        )
    _check_duplicate_keys(parsed, "requests", "request_id")
    return parsed, uncertainties


def load_groups(path: Path) -> tuple[dict[str, PopulationGroup], list[Uncertainty]]:
    rows, columns = read_csv_rows(path, "groups")
    _require_columns(columns, REQUIRED_COLUMNS["groups"], "groups", path)
    uncertainties: list[Uncertainty] = []
    groups: dict[str, PopulationGroup] = {}
    for row in rows:
        group_id = row.required("group_id")
        if group_id in groups:
            raise InputDataError(
                f"groups 中人口分组 {group_id!r} 重复", code="duplicate_record"
            )
        granularity = resolve_granularity(row.raw("granularity"))
        if granularity == STATUS_UNKNOWN:
            uncertainties.append(
                Uncertainty(
                    "groups",
                    f"{row.where()}：人口粒度 {row.raw('granularity')!r} 无法识别，"
                    "按聚合人口处理，不能声称识别到个人。",
                    severity="warning",
                    row=row.where(),
                )
            )
        groups[group_id] = PopulationGroup(
            group_id=group_id,
            label=row.required("label"),
            count=row.integer("count", minimum=0),
            source=row.raw("source"),
            granularity=granularity,
            risk_notes=row.raw("risk_notes"),
            row=row.where(),
        )
    return groups, uncertainties


def load_exposures(path: Path) -> tuple[list[Exposure], list[Uncertainty]]:
    rows, columns = read_csv_rows(path, "exposures")
    if "exposure_id" not in columns:
        legacy = ("area_id", "group_id", "count", "source")
        _require_columns(columns, legacy, "exposures", path)
    else:
        _require_columns(columns, REQUIRED_COLUMNS["exposures"], "exposures", path)
    uncertainties: list[Uncertainty] = []
    exposures: list[Exposure] = []
    for row in rows:
        granularity = resolve_granularity(row.raw("granularity"))
        if granularity == STATUS_UNKNOWN:
            granularity = "aggregate"
            uncertainties.append(
                Uncertainty(
                    "exposures",
                    f"{row.where()}：暴露人口粒度 {row.raw('granularity')!r} 无法识别，"
                    "按聚合人口处理。",
                    severity="warning",
                    row=row.where(),
                )
            )
        item = Exposure(
            exposure_id=row.raw("exposure_id"),
            area_id=row.required("area_id"),
            group_id=row.required("group_id"),
            count=row.integer("count", minimum=0),
            source=row.raw("source"),
            granularity=granularity,
            as_of=row.time("as_of", allow_empty=True),
            row=row.where(),
        )
        exposures.append(item)
    _check_duplicate_keys(exposures, "exposures", "exposure_id")
    return exposures, uncertainties


def load_bundle(data_dir: Path) -> DatasetBundle:
    """Load one snapshot directory of all supported datasets."""

    if not data_dir.is_dir():
        raise InputDataError(f"资料目录不存在: {data_dir}", code="missing_directory")
    bundle = DatasetBundle()
    aliases = {
        "warnings": ("warnings.csv",),
        "delivery": ("delivery.csv", "population_delivery.csv"),
        "shelters": ("shelters.csv", "shelters_response.csv"),
        "requests": ("requests.csv", "assistance_requests.csv"),
    }
    mapping = {
        "warnings": load_warnings,
        "delivery": load_delivery,
        "shelters": load_shelters,
        "requests": load_requests,
    }
    for name, loader in mapping.items():
        path = next((data_dir / candidate for candidate in aliases[name]
                     if (data_dir / candidate).exists()), None)
        if path is None:
            if name in {"requests", "shelters"}:
                continue
            raise InputDataError(
                f"缺少必要资料文件: 需要 {aliases[name][0]}", code="missing_file"
            )
        values, doubts = loader(path)
        setattr(bundle, name, values)
        bundle.uncertainties.extend(doubts)
        bundle.datasets_found.append(path.stem)

    # Exposures: prefer exposures.csv, accept the legacy towns.csv alias.
    exposure_path = data_dir / "exposures.csv"
    if not exposure_path.exists():
        exposure_path = data_dir / "towns.csv"
    has_exposure_file = exposure_path.exists()
    if has_exposure_file:
        bundle.exposures, doubts = load_exposures(exposure_path)
        bundle.uncertainties.extend(doubts)
        bundle.datasets_found.append(exposure_path.stem)

    # groups.csv / population_groups.csv provide labels and risk notes.
    group_path = data_dir / "groups.csv"
    if not group_path.exists():
        group_path = data_dir / "population_groups.csv"
    if group_path.exists():
        bundle.groups, doubts = load_groups(group_path)
        bundle.uncertainties.extend(doubts)
        bundle.datasets_found.append(group_path.stem)

    if not has_exposure_file:
        # With no exposure file we derive group-level exposure from the
        # delivery targets, and every derived row carries a warning.
        doubts, derived = synthesize_exposures(bundle)
        bundle.exposures.extend(derived)
        bundle.uncertainties.extend(doubts)
        if doubts:
            bundle.datasets_found.append("synthesized_exposures")
    return bundle


def synthesize_exposures(bundle: DatasetBundle) -> tuple[list[Uncertainty], list[Exposure]]:
    """Derive group-level exposure from delivery rows when exposure data is absent.

    Delivery rows state how many people were targeted.  They therefore give a
    lower bound on exposure, always aggregated per area/group and never an
    individual record.  The uncertainty is attached to every derived row so
    the review cannot present it as a measured exposure.
    """

    derived: list[Exposure] = []
    doubts: list[Uncertainty] = []
    seen: set[tuple[str, str]] = set()
    for row in bundle.delivery:
        pair = (row.area_id, row.group_id)
        if pair in seen:
            continue
        seen.add(pair)
        derived.append(
            Exposure(
                exposure_id=f"derived:{row.area_id}:{row.group_id}",
                area_id=row.area_id,
                group_id=row.group_id,
                count=row.exposed_estimate,
                source="delivery 目标人数（推导值）",
                granularity="aggregate",
                as_of=row.targeted_at,
                row=row.row,
            )
        )
        doubts.append(
            Uncertainty(
                "exposures",
                f"未提供独立暴露人口资料；{row.area_id}/{row.group_id} 的暴露人数"
                f"由发送目标人数 {row.exposed_estimate} 推导，仅为下限估计。",
                severity="warning",
                row=row.row,
            )
        )
    return doubts, derived
