"""Response analysis.

This module turns one immutable input snapshot into a structured assessment.
It is a pure function of the snapshot plus a few explicit parameters, which
makes it safe to re-run and to compare snapshots.

Honesty rules encoded here
--------------------------

1. **Coverage is version- and area-specific.**  A warning that is extended to
   a new area in version 2 does not retroactively cover that area.  Coverage
   starts at the ``issued_at`` of the first version that names the area.
2. **Sending is not delivery, delivery is not acknowledgement.**  Channel
   success, resident confirmation and shelter arrival are separate states and
   are never merged into one "reached" number.
3. **Aggregate data cannot identify individuals.**  Exposure rows describe
   groups.  The analysis states this explicitly and never emits a person-level
   claim from aggregate input.
4. **Shelter capacity is window- and admission-aware.**  Capacity is only
   counted while the shelter window is open, and occupancy is the number of
   admissions inside that window.
5. **Uncertainty is reported, not erased.**  Unknown statuses, ambiguous
   timestamps, derived exposure and overlapping groups all surface in the
   output.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .data import DatasetBundle, DeliveryRow, Exposure, RequestRow, ShelterRow, WarningRow
from .timeutil import format_time, minutes_between
from .values import (
    DELIVERY_FAILED,
    DELIVERY_REACHED_CHANNEL,
    DELIVERY_SENT,
    DELIVERY_UNREACHABLE,
    DELIVERY_UNSUBSCRIBED,
    REQUEST_RESOLVED,
    STATUS_AMBIGUOUS,
    STATUS_UNKNOWN,
    URGENCY_CRITICAL,
    URGENCY_HIGH,
    URGENCY_LOW,
    URGENCY_MEDIUM,
    URGENCY_RANK,
    is_aggregate,
    need_rank,
    resolve_need,
    severity_rank,
)

# Funnel states, ordered from best evidence to worst.  "best" here means the
# strongest evidence we hold, not that the response was adequate.
FUNNEL_ACKNOWLEDGED = "acknowledged"
FUNNEL_DELIVERED_NOT_ACKNOWLEDGED = "delivered_not_acknowledged"
FUNNEL_TARGETED_OUTCOME_UNKNOWN = "targeted_outcome_unknown"
FUNNEL_UNREACHABLE = "unreachable"
FUNNEL_COVERED_NOT_TARGETED = "covered_not_targeted"
FUNNEL_NO_WARNING_COVERAGE = "no_warning_coverage"

FUNNEL_LABELS = {
    FUNNEL_ACKNOWLEDGED: "居民已确认（至少一条渠道）",
    FUNNEL_DELIVERED_NOT_ACKNOWLEDGED: "渠道已送达，但无居民确认",
    FUNNEL_TARGETED_OUTCOME_UNKNOWN: "已发送，结果未知",
    FUNNEL_UNREACHABLE: "已尝试联系但不可达",
    FUNNEL_COVERED_NOT_TARGETED: "在预警范围内，但没有联络记录",
    FUNNEL_NO_WARNING_COVERAGE: "所在区域在评估时点未被预警覆盖",
}

FUNNEL_ORDER = [
    FUNNEL_ACKNOWLEDGED,
    FUNNEL_DELIVERED_NOT_ACKNOWLEDGED,
    FUNNEL_TARGETED_OUTCOME_UNKNOWN,
    FUNNEL_UNREACHABLE,
    FUNNEL_COVERED_NOT_TARGETED,
    FUNNEL_NO_WARNING_COVERAGE,
]

# Reasons used in the review.  These are deliberately concrete so the duty
# team can act on them.
REASON_LABELS = {
    "no_warning_coverage": "评估时点该区域无有效预警",
    "covered_not_targeted": "预警覆盖但无联络记录（或联络都在覆盖之外）",
    "pre_warning_target": "联络早于该区域被纳入预警，不计为本次通知",
    "sent_after_expiry": "发送晚于预警有效期，不计为有效期内通知",
    "channel_failed": "渠道失败或退订",
    "ambiguous_status": "回执状态不明或结果未知",
    "delivered_no_ack": "渠道成功但居民未确认",
    "unresolved_request": "辖区有未解决求助",
    "opt_out": "居民退订",
    "no_shelter_capacity": "开放避难点无可用容量",
}

WHOLE_AREA_GROUP_NAMES = {"all", "total", "overall", "everyone", "全部", "总人口", "全体"}

RISK_KEYWORDS = {
    "older": 3,
    "elder": 3,
    "senior": 3,
    "老年": 3,
    "长": 3,
    "child": 3,
    "infant": 3,
    "儿童": 3,
    "婴": 3,
    "pregnant": 3,
    "孕": 3,
    "disab": 3,
    "wheelchair": 3,
    "残": 3,
    "失能": 3,
    "chronic": 2,
    "medical": 2,
    "病": 2,
    "dialysis": 3,
    "lone": 2,
    "alone": 2,
    "独居": 3,
    "孤": 3,
    "homeless": 3,
    "无家": 3,
    "outdoor": 1,
    "worker": 1,
    "migrant": 1,
    "户外的": 2,
    "流浪": 3,
}


def _size_points(count: int) -> int:
    if count >= 10000:
        return 30
    if count >= 5000:
        return 25
    if count >= 1000:
        return 20
    if count >= 500:
        return 15
    if count >= 100:
        return 10
    if count > 0:
        return 5
    return 0


def _risk_points(group_id: str, label: str, notes: str) -> int:
    text = f"{group_id} {label} {notes}".lower()
    points = 0
    for keyword, weight in RISK_KEYWORDS.items():
        if keyword in text:
            points = max(points, weight)
    return points


_STATE_PENALTY = {
    FUNNEL_NO_WARNING_COVERAGE: 40,
    FUNNEL_COVERED_NOT_TARGETED: 35,
    FUNNEL_UNREACHABLE: 30,
    FUNNEL_TARGETED_OUTCOME_UNKNOWN: 25,
    FUNNEL_DELIVERED_NOT_ACKNOWLEDGED: 20,
    FUNNEL_ACKNOWLEDGED: 0,
}


def _urgency(score: int) -> str:
    if score >= 100:
        return URGENCY_CRITICAL
    if score >= 75:
        return URGENCY_HIGH
    if score >= 50:
        return URGENCY_MEDIUM
    return URGENCY_LOW


class WarningIndex:
    """Coverage and escalation facts derived from one snapshot's warnings."""

    def __init__(self, warnings: list[WarningRow]) -> None:
        self.rows = warnings
        self.versions: dict[str, list[dict]] = defaultdict(list)
        self.areas: dict[str, list[dict]] = defaultdict(list)
        self._build()

    def _build(self) -> None:
        # Collapse rows of the same warning version into one entry.  All rows
        # of a version should share an issue time; if not we keep the latest
        # and record the discrepancy as uncertainty separately.
        grouped: dict[tuple[str, int], list[WarningRow]] = defaultdict(list)
        for row in self.rows:
            grouped[(row.warning_id, row.version)].append(row)
        for (warning_id, version), rows in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            issued = max(row.issued_at for row in rows)
            areas = sorted({row.area_id for row in rows})
            severities = sorted({row.severity for row in rows})
            entry = {
                "warning_id": warning_id,
                "version": version,
                "issued_at": issued,
                "areas": areas,
                "severities": severities,
                "valid_from": min(row.valid_from for row in rows),
                "valid_to": max(row.valid_to for row in rows),
                "source": next((row.source for row in rows if row.source), ""),
            }
            self.versions[warning_id].append(entry)
            for area_id in areas:
                area_rows = [row for row in rows if row.area_id == area_id]
                self.areas[area_id].append(
                    {
                        "warning_id": warning_id,
                        "version": version,
                        "issued_at": issued,
                        "severity": max(
                            (row.severity for row in area_rows), key=severity_rank
                        ),
                        "valid_from": min(row.valid_from for row in area_rows),
                        "valid_to": max(row.valid_to for row in area_rows),
                        "unknown_severity": any(row.severity_unknown for row in area_rows),
                    }
                )
        for warning_id in self.versions:
            self.versions[warning_id].sort(key=lambda item: (item["issued_at"], item["version"]))
        for area_id in self.areas:
            self.areas[area_id].sort(key=lambda item: (item["issued_at"], item["version"]))

    def first_coverage(self, area_id: str) -> dict | None:
        """Earliest issued version that names this area, ever."""
        entries = self.areas.get(area_id)
        return entries[0] if entries else None

    def coverage_at(self, area_id: str, moment: datetime) -> dict | None:
        """Latest version that was issued and valid at ``moment``."""
        candidate = None
        for entry in self.areas.get(area_id, []):
            if entry["issued_at"] <= moment <= entry["valid_to"]:
                if candidate is None or entry["issued_at"] >= candidate["issued_at"]:
                    candidate = entry
        return candidate

    def pre_warning_sends(self, area_id: str, moment: datetime) -> bool:
        first = self.first_coverage(area_id)
        return bool(first and moment < first["issued_at"])

    def all_areas(self) -> list[str]:
        return sorted(self.areas)


def _warning_summary(index: WarningIndex) -> dict:
    versions = []
    for warning_id, entries in index.versions.items():
        for entry in entries:
            versions.append(
                {
                    "warning_id": warning_id,
                    "version": entry["version"],
                    "issued_at": format_time(entry["issued_at"]),
                    "areas": entry["areas"],
                    "severities": entry["severities"],
                    "valid_from": format_time(entry["valid_from"]),
                    "valid_to": format_time(entry["valid_to"]),
                    "source": entry["source"],
                }
            )
    versions.sort(key=lambda item: (item["issued_at"], item["warning_id"], item["version"]))
    escalations = []
    # Escalation is computed per warning id across versions.  A warning that
    # covers several areas in one version is a single issue, not one issue
    # per area.
    for warning_id, entries in index.versions.items():
        previous_areas: set[str] = set()
        previous_rank = 0
        has_previous = False
        for entry in entries:
            areas = set(entry["areas"])
            rank = max((severity_rank(s) for s in entry["severities"]), default=0)
            added = sorted(areas - previous_areas)
            upgraded = has_previous and rank > previous_rank
            newly_covered = has_previous and bool(added)
            if newly_covered or upgraded:
                escalations.append(
                    {
                        "warning_id": warning_id,
                        "version": entry["version"],
                        "issued_at": format_time(entry["issued_at"]),
                        "added_areas": added if newly_covered else [],
                        "severity": "/".join(entry["severities"]),
                        "upgraded": upgraded,
                        "note": (
                            "新增区域从该版本发布时间起才受覆盖，"
                            "不能视为已提前收到通知。"
                            if newly_covered
                            else "强度上调。"
                        ),
                    }
                )
            previous_areas |= areas
            previous_rank = max(previous_rank, rank)
            has_previous = True
    area_coverage = {}
    for area_id in index.all_areas():
        first = index.first_coverage(area_id)
        area_coverage[area_id] = {
            "covered_from": format_time(first["issued_at"]),
            "first_version": first["version"],
            "severity_at_end": index.areas[area_id][-1]["severity"],
            "versions": [
                {
                    "version": entry["version"],
                    "issued_at": format_time(entry["issued_at"]),
                    "severity": entry["severity"],
                }
                for entry in index.areas[area_id]
            ],
            "late_addition": first["version"] > 1,
        }
    return {
        "versions": versions,
        "escalations": escalations,
        "area_coverage": area_coverage,
        "area_count": len(area_coverage),
    }


def _shelter_summary(shelters: list[ShelterRow], as_of: datetime) -> dict:
    """Capacity by open window, with admissions counted inside the window."""

    windows: dict[tuple, dict] = {}
    uncertainties = []
    for row in shelters:
        key = (row.shelter_id, row.area_id, row.open_from, row.open_to, row.event_ref)
        window = windows.get(key)
        if window is None:
            window = {
                "shelter_id": row.shelter_id,
                "area_id": row.area_id,
                "open_from": row.open_from,
                "open_to": row.open_to,
                "event_ref": row.event_ref,
                "capacities": {row.capacity},
                "admissions": [],
            }
            windows[key] = window
        else:
            window["capacities"].add(row.capacity)
        if row.admitted_at is not None:
            window["admissions"].append(row.admitted_at)

    result = []
    for window in windows.values():
        capacities = sorted(window["capacities"])
        capacity = capacities[-1]
        if len(capacities) > 1:
            uncertainties.append(
                f"避难点 {window['shelter_id']} 在同一开放时段出现多个容量值 "
                f"{capacities}，按最大值 {capacity} 计算，需核对。"
            )
        open_from = window["open_from"]
        open_to = window["open_to"]
        is_open = open_from <= as_of and (open_to is None or as_of <= open_to)
        if open_to is None:
            uncertainties.append(
                f"避难点 {window['shelter_id']} 未登记关闭时间，按持续开放处理。"
            )
        admitted_in_window = sorted(
            moment
            for moment in window["admissions"]
            if moment >= open_from and (open_to is None or moment <= open_to)
        )
        admitted_by_as_of = sum(1 for moment in admitted_in_window if moment <= as_of)
        total_ever = len(admitted_in_window)
        opened = open_from <= as_of
        status = "not_yet_open"
        if opened and (open_to is not None and as_of > open_to):
            status = "closed"
        elif opened:
            status = "open"
        if opened and capacity == 0 and status == "open":
            status = "open_no_capacity"
        is_open = status in {"open", "open_no_capacity"}
        # Occupancy only describes a shelter that is open at the evaluation
        # moment.  For a closed window the historical admission count is kept
        # in admitted_total, but a "remaining capacity" number would be
        # meaningless.
        occupancy_at_as_of = admitted_by_as_of if is_open else None
        remaining = (
            max(0, capacity - admitted_by_as_of) if is_open else None
        )
        result.append(
            {
                "shelter_id": window["shelter_id"],
                "area_id": window["area_id"],
                "event_ref": window["event_ref"],
                "open_from": format_time(open_from),
                "open_to": format_time(open_to),
                "open_ended": open_to is not None,
                "capacity": capacity,
                "admitted_total": total_ever,
                "admitted_by_as_of": admitted_by_as_of,
                "occupancy_at_as_of": occupancy_at_as_of,
                "remaining_at_as_of": remaining,
                "utilization": (
                    round(admitted_by_as_of / capacity, 3)
                    if is_open and capacity else None
                ),
                "status": status,
                "is_open": is_open,
                "over_capacity": is_open and admitted_by_as_of > capacity,
                "throughout_event": {
                    "open_from": format_time(open_from),
                    "open_to": format_time(open_to),
                    "capacity": capacity,
                    "admitted_total": total_ever,
                },
            }
        )
    result.sort(key=lambda item: (item["area_id"], item["open_from"], item["shelter_id"]))
    areas: dict[str, dict] = {}
    for item in result:
        area = areas.setdefault(
            item["area_id"],
            {
                "area_id": item["area_id"],
                "open_capacity": 0,
                "open_admitted": 0,
                "open_remaining": 0,
                "open_shelters": 0,
                "no_capacity_open": 0,
                "total_capacity": 0,
                "total_admitted": 0,
            },
        )
        area["total_capacity"] += item["capacity"]
        area["total_admitted"] += item["admitted_total"]
        if item["is_open"]:
            area["open_shelters"] += 1
            area["open_capacity"] += item["capacity"]
            area["open_admitted"] += item["admitted_by_as_of"]
            area["open_remaining"] += item["remaining_at_as_of"] or 0
            if item["capacity"] == 0:
                area["no_capacity_open"] += 1
    return {
        "as_of": format_time(as_of),
        "shelters": result,
        "areas": areas,
        "uncertainties": uncertainties,
    }


def _request_summary(requests: list[RequestRow], as_of: datetime) -> dict:
    by_area: dict[str, dict] = {}
    unresolved = []
    for request in requests:
        if request.received_at > as_of:
            # The request did not exist yet at the evaluation moment.
            continue
        # A request resolved after the evaluation moment was still open then.
        resolved_in_time = (
            request.status == REQUEST_RESOLVED
            and request.resolved_at is not None
            and request.resolved_at <= as_of
        )
        area = by_area.setdefault(
            request.area_id,
            {"area_id": request.area_id, "total": 0, "resolved": 0, "unresolved": 0,
             "needs": defaultdict(int), "reasons": defaultdict(int), "late_receipt": 0},
        )
        area["total"] += 1
        area["needs"][resolve_need(request.need)] += 1
        if request.received_at > request.requested_at:
            area["late_receipt"] += 1
        if resolved_in_time:
            area["resolved"] += 1
        else:
            area["unresolved"] += 1
            reason = request.inability_reason.strip() or "未登记原因"
            area["reasons"][reason] += 1
            unresolved.append(
                {
                    "request_id": request.request_id,
                    "area_id": request.area_id,
                    "group_id": request.group_id,
                    "need": resolve_need(request.need),
                    "need_rank": need_rank(resolve_need(request.need)),
                    "requested_at": format_time(request.requested_at),
                    "received_at": format_time(request.received_at),
                    "status": "resolved_after_as_of" if request.status == REQUEST_RESOLVED
                    else request.status,
                    "inability_reason": request.inability_reason,
                    "person_ref": request.person_ref,
                }
            )
    for area in by_area.values():
        area["needs"] = dict(area["needs"])
        area["reasons"] = dict(area["reasons"])
    unresolved.sort(key=lambda item: (-item["need_rank"], item["requested_at"]))
    return {
        "by_area": by_area,
        "unresolved": unresolved,
        "unresolved_total": len(unresolved),
        "reason_categories": _reason_counts(unresolved),
    }


def _reason_counts(unresolved: list[dict]) -> list[dict]:
    counts: dict[str, int] = defaultdict(int)
    for item in unresolved:
        counts[item["inability_reason"].strip() or "未登记原因"] += 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _exposure_index(
    exposures: list[Exposure],
) -> tuple[dict[str, list[Exposure]], list[str]]:
    by_area: dict[str, list[Exposure]] = defaultdict(list)
    remarks: list[str] = []
    for exposure in exposures:
        by_area[exposure.area_id].append(exposure)
    for area_id, rows in by_area.items():
        rows.sort(key=lambda item: (item.group_id, item.as_of or datetime.min))
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row.group_id] += 1
        duplicated = sorted(group for group, count in counts.items() if count > 1)
        if duplicated:
            # Several snapshots of the same group cannot be added up.  We keep
            # both and say so, rather than picking one silently.
            remarks.append(
                f"{area_id} 的 {'、'.join(duplicated)} 有多条暴露记录，"
                "已分别呈现，未自动相加。"
            )
    return dict(by_area), remarks


def _delivery_index(delivery: list[DeliveryRow]) -> dict[tuple[str, str], list[DeliveryRow]]:
    by_group: dict[tuple[str, str], list[DeliveryRow]] = defaultdict(list)
    for row in delivery:
        by_group[(row.area_id, row.group_id)].append(row)
    for key in by_group:
        by_group[key].sort(key=lambda item: (item.targeted_at, item.channel))
    return dict(by_group)


def _group_label(bundle_groups: dict, group_id: str) -> str:
    group = bundle_groups.get(group_id)
    return group.label if group else group_id


def _group_notes(bundle_groups: dict, group_id: str) -> str:
    group = bundle_groups.get(group_id)
    return group.risk_notes if group else ""


def _is_whole_area(group_id: str, label: str) -> bool:
    text = f"{group_id} {label}".strip().lower()
    return text in WHOLE_AREA_GROUP_NAMES or group_id.strip().lower() in WHOLE_AREA_GROUP_NAMES


def _build_group_assessment(
    *,
    area_id: str,
    exposure: Exposure | None,
    group_id: str,
    group_label: str,
    rows: list[DeliveryRow],
    index: WarningIndex,
    as_of: datetime,
    timely_minutes: int,
    unresolved_requests: list[dict],
) -> dict:
    label = group_label
    coverage_first = index.first_coverage(area_id)
    coverage_now = index.coverage_at(area_id, as_of)
    covered = coverage_now is not None
    coverage_start = coverage_first["issued_at"] if coverage_first else None
    coverage_severity = coverage_now["severity"] if coverage_now else (
        coverage_first["severity"] if coverage_first else STATUS_UNKNOWN
    )

    # Point-in-time view: a delivery record created after the evaluation
    # moment did not exist yet and must not influence this assessment.
    future_rows = [row for row in rows if row.targeted_at > as_of]
    rows = [row for row in rows if row.targeted_at <= as_of]

    pre_warning_rows = []
    post_expiry_rows = []
    unattributed_rows = []
    attributed_rows = []
    for row in rows:
        if coverage_start is None:
            unattributed_rows.append(row)
        elif row.targeted_at < coverage_start:
            pre_warning_rows.append(row)
        elif index.coverage_at(area_id, row.targeted_at) is None:
            # Targeted after the last version that covered this area had
            # expired.  It cannot be attributed to an in-force warning.
            post_expiry_rows.append(row)
        else:
            attributed_rows.append(row)

    channels: dict[str, dict] = {}
    confirmed_rows = []
    success_rows = []
    for row in rows:
        # An explicit confirmation is only evidence if it happened by the
        # evaluation moment.
        has_ack = (
            row.acknowledged_at is not None
            and row.acknowledged_at <= as_of
            and row.status not in {DELIVERY_FAILED, DELIVERY_UNSUBSCRIBED}
        )
        if has_ack:
            confirmed_rows.append(row)
        if has_ack or row.status in DELIVERY_REACHED_CHANNEL:
            success_rows.append(row)
        entry = channels.setdefault(
            row.channel,
            {
                "channel": row.channel,
                "attempts": 0,
                "statuses": defaultdict(int),
                "first_targeted_at": format_time(row.targeted_at),
                "first_channel_success_at": None,
                "first_acknowledged_at": None,
                "status_ambiguous": False,
            },
        )
        entry["attempts"] += 1
        entry["statuses"][row.status] += 1
        if row.status == STATUS_AMBIGUOUS:
            entry["status_ambiguous"] = True
        if has_ack or row.status in DELIVERY_REACHED_CHANNEL:
            moment = format_time(row.acknowledged_at or row.targeted_at)
            if entry["first_channel_success_at"] is None:
                entry["first_channel_success_at"] = moment
        if has_ack:
            moment = format_time(row.acknowledged_at)
            if entry["first_acknowledged_at"] is None:
                entry["first_acknowledged_at"] = moment

    attributed_success = [row for row in attributed_rows if row in success_rows]
    attributed_ack = [row for row in attributed_rows if row in confirmed_rows]
    attributed_unreachable = [
        row for row in attributed_rows
        if row.status in DELIVERY_UNREACHABLE and row not in confirmed_rows
    ]
    attributed_unknown = [
        row for row in attributed_rows if row.status in {STATUS_UNKNOWN, STATUS_AMBIGUOUS}
    ]

    first_success = min(
        (row.acknowledged_at or row.targeted_at for row in attributed_success),
        default=None,
    )
    first_ack = min((row.acknowledged_at for row in attributed_ack), default=None)
    latency = (
        minutes_between(coverage_start, first_success)
        if coverage_start is not None and first_success is not None
        else None
    )
    ack_latency = (
        minutes_between(coverage_start, first_ack)
        if coverage_start is not None and first_ack is not None
        else None
    )

    reasons: list[str] = []
    if not covered:
        state = FUNNEL_NO_WARNING_COVERAGE
        reasons.append("no_warning_coverage")
    elif not rows:
        state = FUNNEL_COVERED_NOT_TARGETED
        reasons.append("covered_not_targeted")
    elif attributed_ack:
        state = FUNNEL_ACKNOWLEDGED
    elif attributed_success:
        state = FUNNEL_DELIVERED_NOT_ACKNOWLEDGED
        reasons.append("delivered_no_ack")
    elif attributed_unreachable and not attributed_unknown:
        # Every attributable attempt failed or the resident opted out.
        state = FUNNEL_UNREACHABLE
        reasons.extend(_unreachable_reasons(attributed_unreachable))
    elif attributed_rows:
        # Sent but no reliable outcome: never counted as reached.
        state = FUNNEL_TARGETED_OUTCOME_UNKNOWN
        reasons.append("ambiguous_status")
    else:
        # Attempts exist, but none happened while a warning covering this area
        # was in force.  That is not evidence of contact for this warning, so
        # it is reported as untargeted rather than as an unknown outcome.
        state = FUNNEL_COVERED_NOT_TARGETED
        reasons.append("covered_not_targeted")
    if post_expiry_rows:
        reasons.append("sent_after_expiry")
    if pre_warning_rows:
        reasons.append("pre_warning_target")
    area_requests = [item for item in unresolved_requests if item["area_id"] == area_id]
    if area_requests:
        reasons.append("unresolved_request")

    exposure_count = exposure.count if exposure else (rows[0].exposed_estimate if rows else 0)
    source = exposure.source if exposure else ("delivery 目标人数（推导值）" if rows else "")
    granularity = exposure.granularity if exposure else "aggregate"

    coverage_basis = _coverage_basis(area_id, coverage_first, coverage_now, as_of)

    return {
        "area_id": area_id,
        "group_id": group_id,
        "group_label": label,
        "granularity": granularity,
        "aggregate_only": is_aggregate(granularity),
        "exposure_count": exposure_count,
        "exposure_source": source,
        "coverage": {
            "covered_at_as_of": covered,
            "covered_from": format_time(coverage_start),
            "first_version": coverage_first["version"] if coverage_first else None,
            "severity": coverage_severity,
            "late_addition": bool(coverage_first and coverage_first["version"] > 1),
            "basis": coverage_basis,
        },
        "targeting": {
            "attempts": len(rows),
            "attributed_attempts": len(attributed_rows),
            "pre_warning_attempts": len(pre_warning_rows),
            "post_expiry_attempts": len(post_expiry_rows),
            "unattributed_attempts": len(unattributed_rows),
            "future_attempts_ignored": len(future_rows),
            "first_targeted_at": format_time(rows[0].targeted_at) if rows else None,
            "channels": [
                {
                    **entry,
                    "statuses": dict(entry["statuses"]),
                }
                for entry in sorted(channels.values(), key=lambda item: item["channel"])
            ],
        },
        "delivery": {
            "first_channel_success_at": format_time(first_success) or None,
            "first_acknowledged_at": format_time(first_ack) or None,
            "latency_minutes": round(latency, 1) if latency is not None else None,
            "ack_latency_minutes": round(ack_latency, 1) if ack_latency is not None else None,
            "timely_channel": latency is not None and latency <= timely_minutes,
            "timely_acknowledged": ack_latency is not None and ack_latency <= timely_minutes,
            "acknowledged": bool(attributed_ack),
            "channel_success": bool(attributed_success),
        },
        "funnel_state": state,
        "funnel_label": FUNNEL_LABELS[state],
        "reasons": sorted(set(reasons)),
        "request_count_unresolved": len(area_requests),
        "overlap": False,
        "additive": True,
    }


def _coverage_basis(area_id: str, first: dict | None,
                    now: dict | None, as_of: datetime) -> dict:
    """The evidence supporting the coverage start used for this group.

    The review must be able to show *why* a start time was chosen: which
    warning version, when it was issued, and when it became valid.  Coverage
    begins at the issue time, which is never earlier than the first version
    that names the area.
    """

    if first is None:
        return {
            "basis": "no_warning_version_names_area",
            "explanation": f"{area_id} 在全部资料版本中都没有被任何预警版本包含。",
        }
    basis = {
        "basis": "first_version_covering_area",
        "warning_id": first["warning_id"],
        "version": first["version"],
        "issued_at": format_time(first["issued_at"]),
        "valid_from": format_time(first["valid_from"]),
        "valid_to": format_time(first["valid_to"]),
        "severity": first["severity"],
        "explanation": (
            f"覆盖起点取 {first['warning_id']} v{first['version']} 的发布时间 "
            f"{format_time(first['issued_at'])}；该版本是首个包含 {area_id} 的版本。"
        ),
    }
    if first["version"] > 1:
        basis["note"] = (
            "该区域在后续版本才被纳入，" "早于此时间的联络不能算作本次预警通知。"
        )
    if now is None:
        basis["in_force_at_as_of"] = False
        basis["note_as_of"] = (
            f"评估时点 {format_time(as_of)} 没有处于有效期内的版本，"
            "因此判定为当前无覆盖。"
        )
    else:
        basis["in_force_at_as_of"] = True
        basis["version_in_force"] = now["version"]
        basis["severity_in_force"] = now["severity"]
    return basis


def _unreachable_reasons(rows: list[DeliveryRow]) -> list[str]:
    reasons = set()
    for row in rows:
        if row.status == STATUS_AMBIGUOUS:
            reasons.add("ambiguous_status")
        elif row.status == STATUS_UNKNOWN:
            reasons.add("ambiguous_status")
        else:
            reasons.add("channel_failed")
        if row.unsubscribe_reason:
            reasons.add("opt_out")
    return sorted(reasons)


def _priority_for_group(item: dict, groups: dict, area_unresolved: list[dict]) -> dict:
    score = severity_rank(item["coverage"]["severity"]) * 10
    score += _size_points(item["exposure_count"])
    score += min(20, _risk_points(item["group_id"], item["group_label"],
                                  _group_notes(groups, item["group_id"])) * 6)
    score += _STATE_PENALTY[item["funnel_state"]]
    high_need = [r for r in area_unresolved
                 if r["area_id"] == item["area_id"] and r["need_rank"] >= need_rank("medical")]
    score += min(25, 8 * len(high_need))
    if item["funnel_state"] == FUNNEL_NO_WARNING_COVERAGE:
        reasons = ["所在区域在评估时点没有有效预警", "需要确认是否应纳入下一轮联络"]
    elif item["funnel_state"] == FUNNEL_COVERED_NOT_TARGETED:
        reasons = ["预警已覆盖但没有联络记录"]
    elif item["funnel_state"] == FUNNEL_DELIVERED_NOT_ACKNOWLEDGED:
        reasons = ["渠道已送达但居民未确认，需二次联络确认可执行避险"]
    elif item["funnel_state"] == FUNNEL_UNREACHABLE:
        reasons = ["已尝试联系但不可达，需要替代渠道或上门"]
    elif item["funnel_state"] == FUNNEL_TARGETED_OUTCOME_UNKNOWN:
        reasons = ["发送结果不明确，不能算作已收到"]
    else:
        reasons = ["已获居民确认；仍需关注后续避险执行"]
    if item["coverage"]["late_addition"]:
        reasons.append("该区域在预警升级后才被纳入，不能视为提前通知")
    if high_need:
        reasons.append(f"辖区有 {len(high_need)} 条医疗及以上未解决求助")
    return {
        "key": f"contact:{item['area_id']}:{item['group_id']}",
        "type": "contact_group",
        "area_id": item["area_id"],
        "group_id": item["group_id"],
        "group_label": item["group_label"],
        "score": score,
        "urgency": _urgency(score),
        "reasons": reasons,
        "evidence": {
            "funnel_state": item["funnel_state"],
            "covered_from": item["coverage"]["covered_from"],
            "severity": item["coverage"]["severity"],
            "exposure_count": item["exposure_count"],
        },
    }


def build_analysis(
    bundle: DatasetBundle,
    *,
    as_of: datetime | None = None,
    timely_minutes: int = 60,
) -> dict:
    """Produce the full assessment for one snapshot."""

    index = WarningIndex(bundle.warnings)
    if as_of is None:
        as_of = _latest_moment(bundle)
    request_summary = _request_summary(bundle.requests, as_of)
    unresolved = request_summary["unresolved"]
    shelter_summary = _shelter_summary(bundle.shelters, as_of)
    exposure_by_area, exposure_remarks = _exposure_index(bundle.exposures)
    delivery_by_group = _delivery_index(bundle.delivery)

    areas = sorted(
        set(exposure_by_area)
        | {area_id for area_id, _ in delivery_by_group}
        | set(index.all_areas())
        | set(shelter_summary["areas"])
    )

    groups_out: list[dict] = []
    for area_id in areas:
        exposures = exposure_by_area.get(area_id)
        group_ids: list[str] = []
        labels: dict[str, str] = {}
        for exposure in exposures or []:
            if exposure.group_id not in group_ids:
                group_ids.append(exposure.group_id)
            labels.setdefault(exposure.group_id, _group_label(bundle.groups, exposure.group_id))
        for (delivery_area, group_id) in delivery_by_group:
            if delivery_area == area_id and group_id not in group_ids:
                group_ids.append(group_id)
                labels[group_id] = _group_label(bundle.groups, group_id)
        if not group_ids:
            group_ids = ["all"]
            labels["all"] = "全部人口"
        for group_id in group_ids:
            exposure = next((row for row in (exposures or []) if row.group_id == group_id), None)
            rows = delivery_by_group.get((area_id, group_id), [])
            item = _build_group_assessment(
                area_id=area_id,
                exposure=exposure,
                group_id=group_id,
                group_label=labels.get(group_id, group_id),
                rows=rows,
                index=index,
                as_of=as_of,
                timely_minutes=timely_minutes,
                unresolved_requests=unresolved,
            )
            if item["exposure_count"] == 0 and exposure is None and rows:
                item["exposure_count"] = rows[0].exposed_estimate
            groups_out.append(item)

    # Mark overlapping (non-additive) groups per area.
    by_area_groups: dict[str, list[dict]] = defaultdict(list)
    for item in groups_out:
        by_area_groups[item["area_id"]].append(item)
    overlap_remarks = []
    for area_id, items in by_area_groups.items():
        whole = [item for item in items if _is_whole_area(item["group_id"], item["group_label"])]
        if whole and len(items) > 1:
            for item in items:
                item["overlap"] = True
                item["additive"] = False
            overlap_remarks.append(
                f"{area_id} 同时存在全体人口与分组人口记录，各组人数不可相加。"
            )

    priorities = []
    for item in groups_out:
        priorities.append(_priority_for_group(item, bundle.groups, unresolved))
    for area_id, area in shelter_summary["areas"].items():
        exposed = [item for item in groups_out if item["area_id"] == area_id]
        uncovered = [item for item in exposed if item["funnel_state"] != FUNNEL_ACKNOWLEDGED]
        full = area["open_capacity"] > 0 and area["open_remaining"] <= 0
        if area["no_capacity_open"] or full or (area["open_shelters"] == 0 and exposed):
            score = 55 + min(20, 5 * len(uncovered))
            reasons = []
            if area["no_capacity_open"]:
                reasons.append("有避难点已开放但容量为 0")
            if full:
                reasons.append("开放避难点已满，无剩余容量")
            if area["open_shelters"] == 0 and exposed:
                reasons.append("评估时点辖区没有开放避难点")
            priorities.append(
                {
                    "key": f"shelter:{area_id}",
                    "type": "shelter_capacity",
                    "area_id": area_id,
                    "group_id": "",
                    "group_label": "",
                    "score": score,
                    "urgency": _urgency(score),
                    "reasons": reasons,
                    "evidence": {
                        "open_shelters": area["open_shelters"],
                        "open_capacity": area["open_capacity"],
                        "open_admitted": area["open_admitted"],
                        "open_remaining": area["open_remaining"],
                    },
                }
            )
    for request in unresolved:
        score = 60 + request["need_rank"] * 8
        priorities.append(
            {
                "key": f"assist:{request['request_id']}",
                "type": "assist_request",
                "area_id": request["area_id"],
                "group_id": request["group_id"],
                "group_label": _group_label(bundle.groups, request["group_id"]),
                "score": score,
                "urgency": _urgency(score),
                "reasons": [f"未解决求助：{request['need']}",
                            request["inability_reason"].strip() or "未登记原因"],
                "evidence": {
                    "request_id": request["request_id"],
                    "need": request["need"],
                    "person_ref": request["person_ref"],
                },
            }
        )
    priorities.sort(key=lambda item: (-item["score"], item["type"], item["key"]))

    timeline = _build_timeline(bundle, index, as_of)

    uncertainties = [item.as_dict() for item in bundle.uncertainties]
    for remark in shelter_summary["uncertainties"] + exposure_remarks + overlap_remarks:
        uncertainties.append({"dataset": "derived", "row": "", "severity": "warning",
                              "detail": remark})

    individual_rows = [row for row in bundle.exposures if row.individually_identified]
    claims = {
        "individual_identification": (
            "supported_for_exposure_rows" if individual_rows else "unsupported"
        ),
        "individual_note": (
            "输入仅含聚合人口资料，复盘不能声称识别到具体个人；"
            "现场求助中的 person_ref 为匿名个案编号，不等同于身份识别。"
            if not individual_rows
            else "部分暴露记录标记为个人粒度，但仍不得超出其来源范围推断其他个人。"
        ),
        "shelter_arrival_note": (
            "避难点接纳记录按设施统计，未与个人或分组身份绑定；"
            "已接纳人数不能证明某个分组已全部抵达，也不能与发送条数直接相减。"
        ),
        "aggregation_notes": overlap_remarks + exposure_remarks,
        "timely_definition": (
            f"“及时”指渠道成功时间距该区域首次被预警覆盖的时间不超过 "
            f"{timely_minutes} 分钟；居民确认时间单独计算。"
        ),
        "additivity": (
            "不同分组的暴露人数仅在资料标记为可加时才能汇总；"
            "全体人口与子分组并存时，汇总数字没有意义。"
        ),
    }

    summary = _summary(groups_out, priorities, request_summary, shelter_summary)
    return {
        "as_of": format_time(as_of),
        "timely_minutes": timely_minutes,
        "datasets": bundle.datasets_found,
        "warning": _warning_summary(index),
        "groups": groups_out,
        "areas": _area_rollup(groups_out, shelter_summary, request_summary),
        "shelters": shelter_summary,
        "requests": request_summary,
        "priorities": priorities,
        "timeline": timeline,
        "uncertainties": uncertainties,
        "claims": claims,
        "summary": summary,
    }


def _latest_moment(bundle: DatasetBundle) -> datetime:
    moments: list[datetime] = []
    for row in bundle.warnings:
        moments.extend([row.issued_at, row.valid_from, row.valid_to])
    for row in bundle.delivery:
        moments.append(row.targeted_at)
        if row.acknowledged_at:
            moments.append(row.acknowledged_at)
    for row in bundle.shelters:
        moments.append(row.open_from)
        if row.open_to:
            moments.append(row.open_to)
        if row.admitted_at:
            moments.append(row.admitted_at)
    for row in bundle.requests:
        moments.extend([row.requested_at, row.received_at])
        if row.resolved_at:
            moments.append(row.resolved_at)
    for row in bundle.exposures:
        if row.as_of:
            moments.append(row.as_of)
    return max(moments) if moments else datetime.min


def _build_timeline(bundle: DatasetBundle, index: WarningIndex, as_of: datetime) -> list[dict]:
    events: list[dict] = []
    for warning_id, entries in index.versions.items():
        for entry in entries:
            events.append(
                {
                    "at": format_time(entry["issued_at"]),
                    "kind": "warning_issued",
                    "area_id": ", ".join(entry["areas"]),
                    "detail": f"{warning_id} v{entry['version']} 发布（{'/'.join(entry['severities'])}），"
                              f"覆盖 {', '.join(entry['areas'])}",
                }
            )
    first_delivery: dict[tuple[str, str], datetime] = {}
    for row in bundle.delivery:
        if row.status in DELIVERY_REACHED_CHANNEL:
            key = (row.area_id, row.group_id)
            if key not in first_delivery or row.targeted_at < first_delivery[key]:
                first_delivery[key] = row.targeted_at
        elif row.status in {DELIVERY_SENT, STATUS_AMBIGUOUS}:
            key = (row.area_id, row.group_id)
            if key not in first_delivery:
                first_delivery[key] = row.targeted_at
    for (area_id, group_id), moment in sorted(first_delivery.items(), key=lambda kv: kv[1]):
        if moment > as_of:
            continue
        events.append(
            {
                "at": format_time(moment),
                "kind": "first_delivery",
                "area_id": area_id,
                "detail": f"{area_id}/{group_id} 首次渠道送达或发出",
            }
        )
    for row in bundle.delivery:
        if row.acknowledged_at and row.acknowledged_at <= as_of:
            events.append(
                {
                    "at": format_time(row.acknowledged_at),
                    "kind": "acknowledged",
                    "area_id": row.area_id,
                    "detail": f"{row.area_id}/{row.group_id} 经 {row.channel} 获得居民确认",
                }
            )
    for row in bundle.shelters:
        if row.open_from <= as_of:
            events.append(
                {
                    "at": format_time(row.open_from),
                    "kind": "shelter_opened",
                    "area_id": row.area_id,
                    "detail": f"避难点 {row.shelter_id} 开放（容量 {row.capacity}）",
                }
            )
        if row.admitted_at and row.admitted_at <= as_of:
            events.append(
                {
                    "at": format_time(row.admitted_at),
                    "kind": "shelter_admission",
                    "area_id": row.area_id,
                    "detail": f"避难点 {row.shelter_id} 登记接纳",
                }
            )
    for row in bundle.requests:
        if row.received_at > as_of:
            continue
        events.append(
            {
                "at": format_time(row.received_at),
                "kind": "request_received",
                "area_id": row.area_id,
                "detail": f"求助 {row.request_id}（{row.need}）{row.status_raw}",
            }
        )
    events.sort(key=lambda item: (item["at"], item["kind"]))
    return events


def _area_rollup(groups: list[dict], shelters: dict, requests: dict) -> list[dict]:
    by_area: dict[str, dict] = {}
    for item in groups:
        area = by_area.setdefault(
            item["area_id"],
            {
                "area_id": item["area_id"],
                "covered_at_as_of": item["coverage"]["covered_at_as_of"],
                "covered_from": item["coverage"]["covered_from"],
                "late_addition": item["coverage"]["late_addition"],
                "severity": item["coverage"]["severity"],
                "group_count": 0,
                "funnel": {},
                "acknowledged_groups": [],
                "unacknowledged_groups": [],
                "non_additive": False,
            },
        )
        area["group_count"] += 1
        area["funnel"][item["funnel_state"]] = area["funnel"].get(item["funnel_state"], 0) + 1
        target = "acknowledged_groups" if item["funnel_state"] == FUNNEL_ACKNOWLEDGED else "unacknowledged_groups"
        area[target].append(item["group_id"])
        if not item["additive"]:
            area["non_additive"] = True
    for area_id, area in by_area.items():
        shelter = shelters["areas"].get(area_id, {})
        request = requests["by_area"].get(area_id, {})
        area["shelter_open_capacity"] = shelter.get("open_capacity", 0)
        area["shelter_open_remaining"] = shelter.get("open_remaining", 0)
        area["shelter_total_admitted"] = shelter.get("total_admitted", 0)
        area["shelter_open_shelters"] = shelter.get("open_shelters", 0)
        area["shelter_no_capacity_open"] = shelter.get("no_capacity_open", 0)
        area["requests_unresolved"] = request.get("unresolved", 0)
    return [by_area[key] for key in sorted(by_area)]


def _summary(groups: list[dict], priorities: list[dict], requests: dict, shelters: dict) -> dict:
    funnel_counts: dict[str, int] = defaultdict(int)
    for item in groups:
        funnel_counts[item["funnel_state"]] += 1
    return {
        "group_rows": len(groups),
        "funnel_counts": {state: funnel_counts.get(state, 0) for state in FUNNEL_ORDER},
        "priorities": {
            "critical": sum(1 for item in priorities if item["urgency"] == URGENCY_CRITICAL),
            "high": sum(1 for item in priorities if item["urgency"] == URGENCY_HIGH),
            "medium": sum(1 for item in priorities if item["urgency"] == URGENCY_MEDIUM),
            "low": sum(1 for item in priorities if item["urgency"] == URGENCY_LOW),
        },
        "unresolved_requests": requests["unresolved_total"],
        "open_no_capacity_shelters": sum(
            area["no_capacity_open"] for area in shelters["areas"].values()
        ),
    }


def compare_analyses(previous: dict, current: dict) -> list[dict]:
    """Describe material changes between two snapshots' analyses.

    Each change is a plain dict with ``category``, ``key``, ``severity`` and
    human readable ``detail`` so the duty team can see what needs adjusting.
    """

    changes: list[dict] = []

    prev_coverage = previous["warning"]["area_coverage"]
    curr_coverage = current["warning"]["area_coverage"]
    for area_id, coverage in curr_coverage.items():
        before = prev_coverage.get(area_id)
        if before is None:
            changes.append(
                {
                    "category": "new_area_coverage",
                    "key": f"area:{area_id}",
                    "severity": "high",
                    "detail": f"{area_id} 新增预警覆盖（自 {coverage['covered_from']} 起）。"
                              "该区域此前未被覆盖，不能视为已提前收到通知。",
                }
            )
        elif coverage["covered_from"] != before["covered_from"]:
            changes.append(
                {
                    "category": "coverage_changed",
                    "key": f"area:{area_id}",
                    "severity": "high",
                    "detail": f"{area_id} 覆盖起点由 {before['covered_from']} 改为 "
                              f"{coverage['covered_from']}。",
                }
            )
        if before is not None and coverage["severity_at_end"] != before["severity_at_end"]:
            changes.append(
                {
                    "category": "severity_changed",
                    "key": f"area:{area_id}",
                    "severity": "medium",
                    "detail": f"{area_id} 预警强度由 {before['severity_at_end']} 变为 "
                              f"{coverage['severity_at_end']}。",
                }
            )

    prev_groups = {(item["area_id"], item["group_id"]): item for item in previous["groups"]}
    curr_groups = {(item["area_id"], item["group_id"]): item for item in current["groups"]}
    for key, item in curr_groups.items():
        before = prev_groups.get(key)
        if before is None:
            changes.append(
                {
                    "category": "new_group_record",
                    "key": f"group:{key[0]}:{key[1]}",
                    "severity": "info",
                    "detail": f"{key[0]}/{key[1]} 新增人口或回执记录。",
                }
            )
            continue
        if before["funnel_state"] != item["funnel_state"]:
            better = FUNNEL_ORDER.index(item["funnel_state"]) < FUNNEL_ORDER.index(before["funnel_state"])
            changes.append(
                {
                    "category": "delivery_state_improved" if better else "delivery_state_regressed",
                    "key": f"group:{key[0]}:{key[1]}",
                    "severity": "info" if better else "high",
                    "detail": f"{key[0]}/{key[1]} 响应状态由「{before['funnel_label']}」"
                              f"变为「{item['funnel_label']}」。",
                }
            )

    prev_shelters = {item["shelter_id"] + "|" + item["open_from"]: item
                     for item in previous["shelters"]["shelters"]}
    for item in current["shelters"]["shelters"]:
        shelter_key = item["shelter_id"] + "|" + item["open_from"]
        before = prev_shelters.get(shelter_key)
        if before is None:
            changes.append(
                {
                    "category": "new_shelter_window",
                    "key": f"shelter:{shelter_key}",
                    "severity": "info",
                    "detail": f"避难点 {item['shelter_id']} 新增开放时段 "
                              f"{item['open_from']}–{item['open_to'] or '未定'}，容量 {item['capacity']}。",
                }
            )
        elif before["capacity"] != item["capacity"]:
            changes.append(
                {
                    "category": "capacity_changed",
                    "key": f"shelter:{shelter_key}",
                    "severity": "medium",
                    "detail": f"避难点 {item['shelter_id']} 容量由 {before['capacity']} "
                              f"变为 {item['capacity']}。",
                }
            )
        elif before["admitted_total"] != item["admitted_total"]:
            changes.append(
                {
                    "category": "occupancy_changed",
                    "key": f"shelter:{shelter_key}",
                    "severity": "info",
                    "detail": f"避难点 {item['shelter_id']} 已接纳由 {before['admitted_total']} "
                              f"变为 {item['admitted_total']}。",
                }
            )

    prev_priorities = {item["key"]: item for item in previous["priorities"]}
    curr_priorities = {item["key"]: item for item in current["priorities"]}
    for key, item in curr_priorities.items():
        before = prev_priorities.get(key)
        if before is None:
            changes.append(
                {
                    "category": "new_priority",
                    "key": key,
                    "severity": "high" if item["urgency"] in {URGENCY_CRITICAL, URGENCY_HIGH} else "info",
                    "detail": f"新增待处理事项：{_priority_title(item)}（{item['urgency']}）。",
                }
            )
        elif before["urgency"] != item["urgency"]:
            rank_before = URGENCY_RANK[before["urgency"]]
            rank_now = URGENCY_RANK[item["urgency"]]
            changes.append(
                {
                    "category": "priority_raised" if rank_now > rank_before else "priority_lowered",
                    "key": key,
                    "severity": "high" if rank_now > rank_before else "info",
                    "detail": f"{_priority_title(item)} 优先级由 {before['urgency']} "
                              f"变为 {item['urgency']}。",
                }
            )
    for key, before in prev_priorities.items():
        if key not in curr_priorities:
            changes.append(
                {
                    "category": "priority_closed",
                    "key": key,
                    "severity": "info",
                    "detail": f"事项已不再列入优先级：{_priority_title(before)}。",
                }
            )
    return changes


def _priority_title(item: dict) -> str:
    if item["group_id"]:
        return f"{item['area_id']}/{item['group_label'] or item['group_id']} 联络"
    if item["type"] == "shelter_capacity":
        return f"{item['area_id']} 避难点容量"
    return f"{item['area_id']} 待处理事项"
