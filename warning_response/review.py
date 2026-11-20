"""Post-event review assembly.

The review answers the questions the duty team must be able to defend:

* What was the key timeline?
* What evidence supports each coverage claim?
* Which groups were not reached, and for what recorded reason?
* What data was uncertain, revised or derived?
* What did the team see when they made each decision, and what changed after?

It renders from the immutable snapshot history plus the append-only action log,
so a later data revision cannot rewrite what was done at the time.
"""

from __future__ import annotations

from copy import deepcopy

from .analysis import (
    FUNNEL_ACKNOWLEDGED,
    FUNNEL_COVERED_NOT_TARGETED,
    FUNNEL_DELIVERED_NOT_ACKNOWLEDGED,
    FUNNEL_LABELS,
    FUNNEL_NO_WARNING_COVERAGE,
    FUNNEL_TARGETED_OUTCOME_UNKNOWN,
    FUNNEL_UNREACHABLE,
    REASON_LABELS,
    compare_analyses,
)
from .errors import ConflictError
from .store import RESOURCE_KINDS, TASK_OPEN

GAP_ACTIONS = {
    FUNNEL_NO_WARNING_COVERAGE: "确认该区域是否应纳入预警，并在下一轮联络中补发。",
    FUNNEL_COVERED_NOT_TARGETED: "安排首次联络，并记录渠道与回执。",
    FUNNEL_UNREACHABLE: "改用户外广播、上门等替代方式，并记录不可达原因。",
    FUNNEL_TARGETED_OUTCOME_UNKNOWN: "核对渠道回执含义，必要时重新发送并跟踪确认。",
    FUNNEL_DELIVERED_NOT_ACKNOWLEDGED: "二次联络确认居民已知悉并可执行避险。",
    FUNNEL_ACKNOWLEDGED: "保持跟踪，关注避险执行与后续求助。",
}

STATUS_LABELS = {
    "open": "待处理",
    "in_progress": "处理中",
    "done": "已完成",
    "dropped": "已取消",
    "active": "可用",
    "released": "已释放",
}


def task_impact(store) -> list[dict]:
    """For each open task, what changed since it was registered.

    This is the "what needs adjusting after an escalation, capacity change or
    new receipt" view.  It only reports differences; it never changes a task
    status on its own.
    """

    if not store.snapshots():
        raise ConflictError("尚未导入资料，无法评估任务影响。", code="no_snapshot")
    state = store.current_state()
    current = store.latest_snapshot()
    groups = {(item["area_id"], item["group_id"]): item
              for item in current["analysis"]["groups"]}
    areas = {item["area_id"]: item for item in current["analysis"]["areas"]}
    shelters = current["analysis"]["shelters"]["areas"]
    priorities = {item["key"]: item for item in current["analysis"]["priorities"]}

    impact = []
    for task in state["tasks"]:
        seen = task.get("seen", {})
        reasons: list[str] = []
        severity = "info"
        group_now = groups.get((task["area_id"], task["group_id"]))
        group_seen = seen.get("group")
        if task["priority_key"]:
            prior = priorities.get(task["priority_key"])
            seen_priority = seen.get("priority")
            if seen_priority and prior is None:
                reasons.append("原优先级事项已从当前清单消失（可能已不再需要或资料已变）。")
                severity = "high"
            elif seen_priority and prior and seen_priority["urgency"] != prior["urgency"]:
                reasons.append(
                    f"优先级由 {seen_priority['urgency']} 变为 {prior['urgency']}。"
                )
                severity = "high" if prior["urgency"] in {"critical", "high"} else "info"
        if group_seen and group_now is None:
            reasons.append("该分组在当前资料中已不存在，需核对任务是否仍然适用。")
            severity = "high"
        elif group_seen and group_now:
            if group_seen["funnel_state"] != group_now["funnel_state"]:
                reasons.append(
                    f"响应状态由「{group_seen['funnel_label']}」"
                    f"变为「{group_now['funnel_label']}」。"
                )
            before_cover = group_seen["coverage"]["covered_from"]
            after_cover = group_now["coverage"]["covered_from"]
            if before_cover != after_cover:
                reasons.append(
                    f"覆盖起点由 {before_cover or '未覆盖'} 变为 "
                    f"{after_cover or '未覆盖'}；后来覆盖不能视为已提前通知。"
                )
                severity = "high"
            before_ack = group_seen["delivery"]["first_acknowledged_at"]
            after_ack = group_now["delivery"]["first_acknowledged_at"]
            if before_ack != after_ack:
                reasons.append(
                    f"居民确认由 {before_ack or '无'} 变为 {after_ack or '无'}。"
                )
        area_seen = seen.get("area") or {}
        area_now = areas.get(task["area_id"])
        shelter_now = shelters.get(task["area_id"], {})
        if area_now and (
            area_now.get("shelter_open_remaining", 0) != area_seen.get("shelter_open_remaining", 0)
            or area_now.get("shelter_no_capacity_open", 0)
            != area_seen.get("shelter_no_capacity_open", 0)
        ):
            reasons.append(
                "辖区避难点开放容量发生变化：开放剩余 "
                f"{area_seen.get('shelter_open_remaining', 0)} → "
                f"{shelter_now.get('open_remaining', 0)}。"
            )
        if reasons:
            impact.append(
                {
                    "task_id": task["task_id"],
                    "area_id": task["area_id"],
                    "group_id": task["group_id"],
                    "action": task["action"],
                    "status": task["status"],
                    "severity": severity,
                    "registered_against_snapshot": task.get("registered_against_snapshot"),
                    "needs_review": task["status"] not in {"done", "dropped"},
                    "reasons": reasons,
                }
            )
    impact.sort(key=lambda item: ({"high": 0, "info": 1}[item["severity"]], item["task_id"]))
    return impact


def build_review(store) -> dict:
    """Assemble the full, traceable review document."""

    if not store.snapshots():
        raise ConflictError("尚未导入资料，无法生成复盘。", code="no_snapshot")
    snapshots = store.snapshots()
    current = snapshots[-1]
    analysis = current["analysis"]
    state = store.current_state()

    snapshot_history = []
    for index, snapshot in enumerate(snapshots):
        entry = {
            "seq": snapshot["seq"],
            "label": snapshot["label"],
            "imported_at": snapshot["imported_at"],
            "source_dir": snapshot["source_dir"],
            "datasets": snapshot["datasets"],
            "as_of": snapshot["analysis"]["as_of"],
            "input_digest": snapshot["input_digest"],
            "analysis_digest": snapshot["analysis_digest"],
            "uncertainties": len(snapshot["uncertainties"]),
            "superseded": index < len(snapshots) - 1,
        }
        if index > 0:
            entry["changes"] = compare_analyses(
                snapshots[index - 1]["analysis"], snapshot["analysis"]
            )
        snapshot_history.append(entry)

    revision_changes = [
        change
        for entry in snapshot_history[1:]
        for change in entry.get("changes", [])
    ]

    task_review = []
    current_groups = {
        (item["area_id"], item["group_id"]): item for item in analysis["groups"]
    }
    for task in state["tasks"]:
        seen = task.get("seen", {})
        seen_group = seen.get("group") or {}
        now_group = current_groups.get((task["area_id"], task["group_id"]))
        drift = _task_drift(task, seen_group, now_group, snapshot_history)
        task_review.append(
            {
                "task_id": task["task_id"],
                "area_id": task["area_id"],
                "group_id": task["group_id"],
                "action": task["action"],
                "owner": task["owner"],
                "due_at": task["due_at"],
                "status": task["status"],
                "status_label": STATUS_LABELS.get(task["status"], task["status"]),
                "note": task["note"],
                "outcome": task["outcome"],
                "registered_at": task["registered_at"],
                "registered_seq": task["registered_seq"],
                "registered_against_snapshot": task.get("registered_against_snapshot"),
                "seen": seen,
                "current_group_state": (
                    {
                        "funnel_state": now_group["funnel_state"],
                        "funnel_label": now_group["funnel_label"],
                        "covered_from": now_group["coverage"]["covered_from"],
                        "first_acknowledged_at": now_group["delivery"]["first_acknowledged_at"],
                    }
                    if now_group else None
                ),
                "funnel_state_at_registration": seen_group.get("funnel_state"),
                "funnel_label_at_registration": seen_group.get("funnel_label"),
                "drift": drift,
                "history": task["history"],
            }
        )

    resource_review = []
    for resource in state["resources"]:
        resource_review.append(
            {
                "resource_id": resource["resource_id"],
                "area_id": resource["area_id"],
                "kind": resource["kind"],
                "kind_label": RESOURCE_KINDS.get(resource["kind"], resource["kind"]),
                "capacity": resource["capacity"],
                "window_from": resource["window_from"],
                "window_to": resource["window_to"],
                "status": resource["status"],
                "status_label": STATUS_LABELS.get(resource["status"], resource["status"]),
                "note": resource["note"],
                "registered_at": resource["registered_at"],
                "history": resource["history"],
            }
        )

    gaps = _gap_table(analysis)
    unresolved_reasons = _unresolved_reasons(analysis)
    uncertainty = _uncertainty_summary(snapshots, analysis)
    closed = state["event"]["status"] == "closed"
    completeness = _completeness(analysis, task_review, unresolved_reasons)
    lifecycle_entries = [
        {
            "seq": entry["seq"],
            "at": entry["at"],
            "actor": entry["actor"],
            "summary": (
                "事件结束：" + (entry["payload"].get("summary") or "未填写总结")
                if entry["kind"] == "event_closed"
                else "事件重新打开：" + (entry["payload"].get("reason") or "未填写原因")
            ),
        }
        for entry in store.data["log"]
        if entry["kind"] in {"event_closed", "event_reopened"}
    ]

    return {
        "event": deepcopy(state["event"]),
        "generated_from_seq": state["seq"],
        "snapshot_count": len(snapshots),
        "snapshot_history": snapshot_history,
        "revision_changes": revision_changes,
        "latest_snapshot": {
            "seq": current["seq"],
            "label": current["label"],
            "as_of": analysis["as_of"],
        },
        "analysis": analysis,
        "gaps": gaps,
        "unresolved_reasons": unresolved_reasons,
        "tasks": task_review,
        "resources": resource_review,
        "notes": state["notes"],
        "timeline": analysis["timeline"],
        "uncertainty": uncertainty,
        "claims": analysis["claims"],
        "completeness": completeness,
        "closed": closed,
        "lifecycle_entries": lifecycle_entries,
        "task_impact": task_impact(store),
        "recommendations": _recommendations(gaps, task_review, analysis),
    }


def _task_drift(task: dict, seen_group: dict, now_group: dict | None,
                snapshot_history: list[dict]) -> dict:
    """Whether the evidence behind a task changed after registration."""

    if not seen_group:
        return {"changed": False, "reasons": []}
    if now_group is None:
        return {
            "changed": True,
            "reasons": ["当前资料中已找不到该社区/人口分组的记录，任务需人工复核。"],
        }
    reasons = []
    before_state = seen_group.get("funnel_state")
    now_state = now_group["funnel_state"]
    if before_state and before_state != now_state:
        reasons.append(
            f"响应状态由「{seen_group.get('funnel_label', before_state)}」"
            f"变为「{now_group['funnel_label']}」。"
        )
    before_covered = seen_group.get("coverage", {}).get("covered_from")
    now_covered = now_group["coverage"]["covered_from"]
    if before_covered != now_covered:
        reasons.append(
            f"预警覆盖起点由 {before_covered or '无'} 变为 {now_covered or '无'}；"
            "后来覆盖的区域不能算作提前收到通知。"
        )
    before_ack = seen_group.get("delivery", {}).get("first_acknowledged_at")
    now_ack = now_group["delivery"]["first_acknowledged_at"]
    if before_ack != now_ack:
        reasons.append(
            f"居民确认时间由 {before_ack or '无'} 变为 {now_ack or '无'}。"
        )
    later = [
        entry["label"]
        for entry in snapshot_history
        if entry["seq"] > task.get("registered_against_snapshot", 0)
    ]
    if reasons and later:
        reasons.append(f"涉及资料修订：{', '.join(later)}。")
    return {"changed": bool(reasons), "reasons": reasons}


def _gap_table(analysis: dict) -> list[dict]:
    gaps = []
    for group in analysis["groups"]:
        state = group["funnel_state"]
        gaps.append(
            {
                "area_id": group["area_id"],
                "group_id": group["group_id"],
                "group_label": group["group_label"],
                "exposure_count": group["exposure_count"],
                "aggregate_only": group["aggregate_only"],
                "coverage": group["coverage"],
                "funnel_state": state,
                "funnel_label": FUNNEL_LABELS[state],
                "channel_success": group["delivery"]["channel_success"],
                "acknowledged": group["delivery"]["acknowledged"],
                "first_channel_success_at": group["delivery"]["first_channel_success_at"],
                "first_acknowledged_at": group["delivery"]["first_acknowledged_at"],
                "latency_minutes": group["delivery"]["latency_minutes"],
                "ack_latency_minutes": group["delivery"]["ack_latency_minutes"],
                "timely_channel": group["delivery"]["timely_channel"],
                "timely_acknowledged": group["delivery"]["timely_acknowledged"],
                "pre_warning_attempts": group["targeting"]["pre_warning_attempts"],
                "reasons": [REASON_TEXT.get(reason, reason) for reason in group["reasons"]],
                "next_action": GAP_ACTIONS[state],
                "additive": group["additive"],
            }
        )
    return gaps


REASON_TEXT = REASON_LABELS


def _unresolved_reasons(analysis: dict) -> dict:
    by_need: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for item in analysis["requests"]["unresolved"]:
        by_need[item["need"]] = by_need.get(item["need"], 0) + 1
        reason = item["inability_reason"].strip() or "未登记原因"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "total": analysis["requests"]["unresolved_total"],
        "by_need": by_need,
        "by_reason": by_reason,
        "items": analysis["requests"]["unresolved"],
    }


def _uncertainty_summary(snapshots: list[dict], analysis: dict) -> dict:
    current = [
        item for item in analysis["uncertainties"]
        if item.get("severity") != "info"
    ]
    return {
        "current": current,
        "current_count": len(current),
        "by_dataset": _count_by(current, "dataset"),
        "superseded_inputs_preserved": len(snapshots) - 1,
        "note": (
            "资料修订不会覆盖历史快照；复盘中的任务仍保留登记当时所见证据。"
            if len(snapshots) > 1
            else "当前只有一个资料版本。"
        ),
    }


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.get(key) or "未归类"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _completeness(analysis: dict, tasks: list[dict], unresolved: dict) -> dict:
    """What the evidence can and cannot prove."""

    aggregate_only = all(group["aggregate_only"] for group in analysis["groups"])
    unacknowledged = [
        group for group in analysis["groups"]
        if group["funnel_state"] != FUNNEL_ACKNOWLEDGED
    ]
    open_tasks = [task for task in tasks if task["status"] in {TASK_OPEN, "in_progress"}]
    return {
        "can_claim": [
            "各社区在评估时点的预警覆盖起点（按发布版本）。",
            "渠道发送、渠道送达、居民确认三个状态分别的记录。",
            "按开放时段和接纳人数计算的避难点容量。",
            "未解决求助及其登记的原因类别。",
        ],
        "cannot_claim": [
            "不能把发送条数当作居民已知悉的证明。",
            "不能声称对聚合人口资料识别到了具体个人。" if aggregate_only
            else "不能超出个人资料来源范围推断其他个人。",
            "不能把预警扩区后新增的区域算作提前收到通知。",
            "不能推断没有回执的居民是否实际避险。",
        ],
        "counts": {
            "groups": len(analysis["groups"]),
            "groups_not_acknowledged": len(unacknowledged),
            "unresolved_requests": unresolved["total"],
            "open_tasks": len(open_tasks),
            "priorities": len(analysis["priorities"]),
        },
        "fully_closed": bool(
            analysis["groups"]
            and not unacknowledged
            and unresolved["total"] == 0
            and not open_tasks
        ),
    }


def _recommendations(gaps: list[dict], tasks: list[dict],
                     analysis: dict) -> list[str]:
    tips = []
    for gap in gaps:
        if gap["funnel_state"] != FUNNEL_ACKNOWLEDGED:
            tips.append(
                f"{gap['area_id']}/{gap['group_label'] or gap['group_id']}："
                f"{gap['funnel_label']} → {gap['next_action']}"
            )
    for task in tasks:
        if task["drift"]["changed"] and task["status"] in {TASK_OPEN, "in_progress"}:
            tips.append(
                f"任务 {task['task_id']} 的依据资料已变化，需复核："
                + " ".join(task["drift"]["reasons"])
            )
    no_capacity = [
        area for area in analysis["shelters"]["areas"].values()
        if area["no_capacity_open"]
    ]
    for area in no_capacity:
        tips.append(f"{area['area_id']} 有避难点开放但容量为 0，需调配临时空间。")
    return tips


# --------------------------------------------------------------------- render


def render_review_markdown(review: dict) -> str:
    lines: list[str] = []
    event = review["event"]
    latest = review["latest_snapshot"]
    lines.append(f"# 预警响应复盘：{event['name']}")
    lines.append("")
    lines.append(f"- 事件编号：{event['id']}")
    lines.append(f"- 状态：{'已结束' if review['closed'] else '进行中'}"
                 f"{'（结束于 ' + event['closed_at'] + '）' if event['closed_at'] else ''}")
    lines.append(f"- 依据资料版本：第 {latest['seq']} 次导入「{latest['label']}」，"
                 f"评估时点 {latest['as_of']}")
    lines.append(f"- 资料版本数：{review['snapshot_count']}；"
                 f"行动日志位置：{review['generated_from_seq']}")
    lines.append("")

    completeness = review["completeness"]
    lines.append("## 复盘结论边界")
    lines.append("")
    lines.append("可以说明：")
    for item in completeness["can_claim"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("不能说明：")
    for item in completeness["cannot_claim"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 关键时间线")
    lines.append("")
    lines.append("| 时间 | 类型 | 区域 | 说明 |")
    lines.append("| --- | --- | --- | --- |")
    for entry in review["timeline"]:
        lines.append(
            f"| {entry['at']} | {_KIND_LABELS.get(entry['kind'], entry['kind'])} "
            f"| {entry['area_id']} | {entry['detail']} |"
        )
    lines.append("")

    lines.append("## 风险覆盖与响应缺口")
    lines.append("")
    lines.append("| 社区 | 人口分组 | 覆盖起点 | 覆盖强度 | 渠道成功 | 居民确认 | 判定 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for gap in review["gaps"]:
        lines.append(
            f"| {gap['area_id']} | {gap['group_label']} | "
            f"{gap['coverage']['covered_from'] or '未覆盖'} | "
            f"{gap['coverage']['severity']} | "
            f"{gap['first_channel_success_at'] or '无'} | "
            f"{gap['first_acknowledged_at'] or '无'} | {gap['funnel_label']} |"
        )
    lines.append("")
    if review["gaps"]:
        lines.append(
            "> 覆盖起点按该区域首次被预警版本包含的发布时间计算；"
            "渠道成功不等于居民确认，确认也不等于已抵达避难点。"
        )
        lines.append("")
        lines.append("覆盖依据：")
        for gap in review["gaps"]:
            basis = gap["coverage"].get("basis", {})
            if basis.get("explanation"):
                lines.append(f"- {gap['area_id']}/{gap['group_label']}：{basis['explanation']}")
        lines.append("")
        lines.append(f"> {review['claims'].get('shelter_arrival_note', '')}")
        lines.append("")

    lines.append("## 未响应原因与后续动作")
    lines.append("")
    for gap in review["gaps"]:
        if gap["funnel_state"] == FUNNEL_ACKNOWLEDGED:
            continue
        lines.append(f"- **{gap['area_id']} / {gap['group_label']}**：{gap['funnel_label']}")
        for reason in gap["reasons"]:
            lines.append(f"  - 原因：{reason}")
        if gap["pre_warning_attempts"]:
            lines.append(
                f"  - 另有 {gap['pre_warning_attempts']} 次联络发生在该区域被纳入预警之前，"
                "已单独列出，未计入本次通知。"
            )
        lines.append(f"  - 建议：{gap['next_action']}")
    if all(gap["funnel_state"] == FUNNEL_ACKNOWLEDGED for gap in review["gaps"]):
        lines.append("- 所有已登记分组均获得居民确认（依据见上表）。")
    lines.append("")

    unresolved = review["unresolved_reasons"]
    lines.append("## 现场求助与未解决原因")
    lines.append("")
    if unresolved["total"] == 0:
        lines.append("- 资料中没有未解决求助记录。")
    else:
        lines.append(f"- 未解决求助共 {unresolved['total']} 条。")
        for reason, count in sorted(unresolved["by_reason"].items(),
                                   key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  - {reason}：{count} 条")
        for item in unresolved["items"]:
            lines.append(
                f"  - {item['request_id']}（{item['area_id']}，{item['need']}，"
                f"{item['requested_at']}）状态 {item['status']}："
                f"{item['inability_reason'] or '未登记原因'}"
            )
    lines.append("")

    lines.append("## 避难点容量（按开放时段与已接纳人数）")
    lines.append("")
    lines.append("| 避难点 | 社区 | 开放时段 | 容量 | 已接纳 | 评估时点占用 | 状态 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for shelter in review["analysis"]["shelters"]["shelters"]:
        window = f"{shelter['open_from']} – {shelter['open_to'] or '未登记关闭时间'}"
        lines.append(
            f"| {shelter['shelter_id']} | {shelter['area_id']} | {window} | "
            f"{shelter['capacity']} | {shelter['admitted_total']} | "
            f"{shelter['occupancy_at_as_of'] if shelter['occupancy_at_as_of'] is not None else '—'} | {_SHELTER_LABELS.get(shelter['status'], shelter['status'])} |"
        )
    lines.append("")

    lines.append("## 待联络事项与任务")
    lines.append("")
    if not review["tasks"]:
        lines.append("- 未登记任务。")
    for task in review["tasks"]:
        lines.append(
            f"- {task['task_id']} [{task['status_label']}] {task['area_id']}"
            f"{'/' + task['group_id'] if task['group_id'] else ''}：{task['action']}"
            f"（负责人 {task['owner'] or '未指派'}）"
        )
        lines.append(
            f"  - 登记于 {task['registered_at']}，依据第 "
            f"{task['registered_against_snapshot']} 次导入资料；登记时状态："
            f"{task['funnel_label_at_registration'] or '未知'}"
        )
        if task["current_group_state"]:
            lines.append(f"  - 当前资料状态：{task['current_group_state']['funnel_label']}")
        if task["drift"]["changed"]:
            lines.append("  - **需复核**：" + " ".join(task["drift"]["reasons"]))
        if task["outcome"]:
            lines.append(f"  - 处理结果：{task['outcome']}")
        elif task["note"]:
            lines.append(f"  - 备注：{task['note']}")
    lines.append("")

    lines.append("## 资料变化对已登记任务的影响")
    lines.append("")
    if not review["task_impact"]:
        lines.append("- 已登记任务所依据的情况没有变化。")
    for item in review["task_impact"]:
        flag = "需调整" if item["needs_review"] else "已结束任务（仅留档）"
        lines.append(
            f"- {item['task_id']} {item['area_id']}"
            f"{'/' + item['group_id'] if item['group_id'] else ''}"
            f"（{STATUS_LABELS.get(item['status'], item['status'])}，{flag}）："
        )
        for reason in item["reasons"]:
            lines.append(f"  - {reason}")
    lines.append("")

    lines.append("## 临时资源安排")
    lines.append("")
    if not review["resources"]:
        lines.append("- 未登记临时资源。")
    for resource in review["resources"]:
        window = ""
        if resource["window_from"] or resource["window_to"]:
            window = f"，时段 {resource['window_from'] or '未定'} – {resource['window_to'] or '未定'}"
        lines.append(
            f"- {resource['resource_id']} {resource['area_id']} {resource['kind_label']} "
            f"容量 {resource['capacity']}{window}，状态 {resource['status_label']}"
            + (f"，备注：{resource['note']}" if resource["note"] else "")
        )
    lines.append("")

    if review["snapshot_history"]:
        lines.append("## 资料修订与处置时所见")
        lines.append("")
        for entry in review["snapshot_history"]:
            marker = "（已被后续修订取代，但原始内容保留）" if entry["superseded"] else ""
            lines.append(
                f"- 第 {entry['seq']} 次导入「{entry['label']}」"
                f"于 {entry['imported_at']}，资料 {', '.join(entry['datasets'])}，"
                f"输入摘要 {entry['input_digest']}{marker}"
            )
            for change in entry.get("changes", []):
                lines.append(f"  - 变化：{change['detail']}")
        lines.append("")

    uncertainty = review["uncertainty"]
    lines.append("## 资料不确定性")
    lines.append("")
    if not uncertainty["current"]:
        lines.append("- 当前版本未发现需提示的不确定性。")
    else:
        lines.append(f"- 当前版本共 {uncertainty['current_count']} 条提示：")
        for item in uncertainty["current"]:
            lines.append(f"  - [{item['dataset']}] {item['detail']}")
    lines.append(f"- {uncertainty['note']}")
    lines.append("")

    if review["recommendations"]:
        lines.append("## 下一轮联络与资源调整建议")
        lines.append("")
        for tip in review["recommendations"]:
            lines.append(f"- {tip}")
        lines.append("")

    if review["notes"]:
        lines.append("## 值班记录")
        lines.append("")
        for note in review["notes"]:
            lines.append(f"- {note['at']} {note['actor']}（{note['key']}）：{note['text']}")
        lines.append("")

    lines.append("## 行动日志（可追溯）")
    lines.append("")
    lines.append("| 序号 | 时间 | 操作人 | 事件 |")
    lines.append("| --- | --- | --- | --- |")
    for entry in _log_entries(review):
        lines.append(
            f"| {entry['seq']} | {entry['at']} | {entry['actor']} | {entry['summary']} |"
        )
    lines.append("")
    return "\n".join(lines)


_KIND_LABELS = {
    "warning_issued": "预警发布/升级",
    "first_delivery": "首次联络",
    "acknowledged": "居民确认",
    "shelter_opened": "避难点开放",
    "shelter_admission": "避难点接纳",
    "request_received": "现场求助",
}

_SHELTER_LABELS = {
    "open": "开放中",
    "open_no_capacity": "开放但容量为 0",
    "closed": "已关闭",
    "not_yet_open": "尚未开放",
}


def _log_entries(review: dict) -> list[dict]:
    entries = []
    for snapshot in review["snapshot_history"]:
        entries.append(
            {
                "seq": snapshot["seq"],
                "at": snapshot["imported_at"],
                "actor": "资料导入",
                "summary": f"导入资料「{snapshot['label']}」",
            }
        )
    for task in review["tasks"]:
        for item in task["history"]:
            entries.append(
                {
                    "seq": item["seq"],
                    "at": item["at"],
                    "actor": item["actor"],
                    "summary": f"{task['task_id']} {item['kind']} → "
                               f"{STATUS_LABELS.get(item['status'], item['status'])}",
                }
            )
    for resource in review["resources"]:
        for item in resource["history"]:
            entries.append(
                {
                    "seq": item["seq"],
                    "at": item["at"],
                    "actor": item["actor"],
                    "summary": f"{resource['resource_id']} {item['kind']} → "
                               f"{STATUS_LABELS.get(item['status'], item['status'])}",
                }
            )
    for note in review["notes"]:
        entries.append(
            {
                "seq": note["seq"],
                "at": note["at"],
                "actor": note["actor"],
                "summary": f"值班记录：{note['text']}",
            }
        )
    entries.extend(review.get("lifecycle_entries", []))
    entries.sort(key=lambda item: (item["seq"], item["at"]))
    return entries
