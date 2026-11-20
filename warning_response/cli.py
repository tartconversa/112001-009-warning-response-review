"""Command line entry point.

Examples::

    python -m warning_response --store work.json init --event W100 --name "暴雨"
    python -m warning_response --store work.json import data --label 初始资料
    python -m warning_response --store work.json overview
    python -m warning_response --store work.json tasks
    python -m warning_response --store work.json task-add --area North \\
        --group older_adults --action "上门确认" --priority-key contact:North:older_adults
    python -m warning_response --store work.json task-update T001 --status done \\
        --outcome "已当面确认"
    python -m warning_response --store work.json resource-add --area North \\
        --kind transport --capacity 2
    python -m warning_response --store work.json review --markdown review.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analysis import compare_analyses
from .errors import WarningResponseError
from .review import build_review, render_review_markdown, task_impact
from .store import RESOURCE_KINDS, RESOURCE_STATUSES, Store, TASK_STATUSES

DEFAULT_STORE = "work.json"


def _emit(value) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _load(args) -> Store:
    return Store.open(Path(args.store))


def _snapshot_analysis(args, snapshot_seq: int | None) -> dict:
    store = _load(args)
    snapshot = (
        store.snapshot_at(snapshot_seq) if snapshot_seq is not None
        else store.latest_snapshot()
    )
    return {"snapshot": snapshot, "analysis": snapshot["analysis"]}


# ------------------------------------------------------------------- commands
def cmd_init(args) -> int:
    store = Store.create(args.store, event_id=args.event, event_name=args.name,
                         actor=args.actor)
    _emit({"created": str(store.path), "event": store.data["event"]})
    return 0


def cmd_import(args) -> int:
    store = _load(args)
    snapshot = store.import_snapshot(
        args.data_dir, label=args.label or "", actor=args.actor, as_of=args.as_of
    )
    analysis = snapshot["analysis"]
    _emit(
        {
            "snapshot_seq": snapshot["seq"],
            "label": snapshot["label"],
            "as_of": analysis["as_of"],
            "datasets": snapshot["datasets"],
            "summary": analysis["summary"],
            "uncertainties": len(analysis["uncertainties"]),
        }
    )
    return 0


def cmd_overview(args) -> int:
    store = _load(args)
    snapshot = store.latest_snapshot()
    analysis = snapshot["analysis"]
    state = store.current_state()
    output = {
        "event": state["event"],
        "snapshot": {"seq": snapshot["seq"], "label": snapshot["label"],
                     "as_of": analysis["as_of"]},
        "summary": analysis["summary"],
        "warning": analysis["warning"],
        "areas": analysis["areas"],
        "priorities": analysis["priorities"],
        "uncertainties": analysis["uncertainties"],
        "open_tasks": [task for task in state["tasks"]
                       if task["status"] not in {"done", "dropped"}],
    }
    if snapshot["seq"] > store.snapshots()[0]["seq"]:
        previous = store.snapshots()[-2]["analysis"]
        output["changes_since_previous"] = compare_analyses(previous, analysis)
        output["task_impact"] = task_impact(store)
    _emit(output)
    return 0


def cmd_warnings(args) -> int:
    result = _snapshot_analysis(args, args.snapshot)
    _emit(result["analysis"]["warning"])
    return 0


def cmd_groups(args) -> int:
    result = _snapshot_analysis(args, args.snapshot)
    groups = result["analysis"]["groups"]
    if args.area:
        groups = [item for item in groups if item["area_id"] == args.area]
    _emit(groups)
    return 0


def cmd_shelters(args) -> int:
    result = _snapshot_analysis(args, args.snapshot)
    _emit(result["analysis"]["shelters"])
    return 0


def cmd_requests(args) -> int:
    result = _snapshot_analysis(args, args.snapshot)
    _emit(result["analysis"]["requests"])
    return 0


def cmd_priorities(args) -> int:
    result = _snapshot_analysis(args, args.snapshot)
    priorities = result["analysis"]["priorities"]
    if args.urgency:
        priorities = [item for item in priorities if item["urgency"] == args.urgency]
    _emit(priorities)
    return 0


def cmd_diff(args) -> int:
    store = _load(args)
    snapshots = store.snapshots()
    if args.from_seq is not None:
        before = store.snapshot_at(args.from_seq)["analysis"]
    else:
        if len(snapshots) < 2:
            raise WarningResponseError("只有一个资料版本，无法比较。", code="no_revision")
        before = snapshots[-2]["analysis"]
    after = (
        store.snapshot_at(args.to_seq)["analysis"] if args.to_seq is not None
        else snapshots[-1]["analysis"]
    )
    _emit(compare_analyses(before, after))
    return 0


def cmd_impact(args) -> int:
    store = _load(args)
    our_impact = task_impact(store)
    if args.open_only:
        our_impact = [item for item in our_impact if item["needs_review"]]
    _emit(our_impact)
    return 0


def cmd_tasks(args) -> int:
    store = _load(args)
    state = store.state_at(args.at_seq)
    tasks = state["tasks"]
    if args.status:
        tasks = [task for task in tasks if task["status"] == args.status]
    _emit(tasks)
    return 0


def cmd_task_add(args) -> int:
    store = _load(args)
    task = store.register_task(
        priority_key=args.priority_key,
        area_id=args.area,
        group_id=args.group,
        action=args.action,
        owner=args.owner,
        due_at=args.due,
        note=args.note,
        actor=args.actor,
        task_id=args.task_id,
    )
    _emit({"task": task, "evidence_snapshot": task["registered_against_snapshot"]})
    return 0


def cmd_task_update(args) -> int:
    store = _load(args)
    task = store.update_task(
        args.task_id,
        status=args.status,
        owner=args.owner,
        due_at=args.due,
        note=args.note,
        outcome=args.outcome,
        actor=args.actor,
    )
    _emit(task)
    return 0


def cmd_resources(args) -> int:
    store = _load(args)
    _emit(store.state_at(args.at_seq)["resources"])
    return 0


def cmd_resource_add(args) -> int:
    store = _load(args)
    resource = store.register_resource(
        area_id=args.area,
        kind=args.kind,
        capacity=args.capacity,
        window_from=args.window_from,
        window_to=args.window_to,
        note=args.note,
        actor=args.actor,
        resource_id=args.resource_id,
    )
    _emit(resource)
    return 0


def cmd_resource_update(args) -> int:
    store = _load(args)
    resource = store.update_resource(
        args.resource_id,
        status=args.status,
        capacity=args.capacity,
        window_from=args.window_from,
        window_to=args.window_to,
        note=args.note,
        actor=args.actor,
    )
    _emit(resource)
    return 0


def cmd_note(args) -> int:
    store = _load(args)
    _emit(store.add_note(args.key, args.text, actor=args.actor))
    return 0


def cmd_state(args) -> int:
    store = _load(args)
    _emit(store.state_at(args.at_seq))
    return 0


def cmd_review(args) -> int:
    store = _load(args)
    review = build_review(store)
    if args.markdown:
        path = Path(args.markdown)
        text = render_review_markdown(review)
        path.write_text(text, encoding="utf-8")
        _emit(f"复盘已写入 {path}")
    else:
        _emit(render_review_markdown(review))
    return 0


def cmd_close(args) -> int:
    store = _load(args)
    event = store.close_event(summary=args.summary, actor=args.actor)
    _emit(event)
    return 0


def cmd_reopen(args) -> int:
    store = _load(args)
    event = store.reopen_event(reason=args.reason, actor=args.actor)
    _emit(event)
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warning_response",
        description="气象灾害预警响应分析：资料导入、缺口分析、任务登记与复盘。",
    )
    parser.add_argument("--store", default=DEFAULT_STORE,
                        help=f"工作区 JSON 路径（默认 {DEFAULT_STORE}）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_actor(p):
        p.add_argument("--actor", default="duty", help="操作人")

    p = sub.add_parser("init", help="新建事件工作区")
    p.add_argument("--event", required=True, help="事件编号")
    p.add_argument("--name", default="", help="事件名称")
    add_actor(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="导入/修订资料目录")
    p.add_argument("data_dir")
    p.add_argument("--label", default="", help="资料版本说明")
    p.add_argument("--as-of", default=None, help="评估时点（ISO 8601，默认取资料中最晚时间）")
    add_actor(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("overview", help="显示当前态势与变化")
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser("warnings", help="显示预警版本与扩区/升级")
    p.add_argument("--snapshot", type=int, default=None, help="资料快照序号")
    p.set_defaults(func=cmd_warnings)

    p = sub.add_parser("groups", help="显示各社区人口分组的响应漏斗")
    p.add_argument("--area", default=None)
    p.add_argument("--snapshot", type=int, default=None)
    p.set_defaults(func=cmd_groups)

    p = sub.add_parser("shelters", help="显示避难点容量与占用")
    p.add_argument("--snapshot", type=int, default=None)
    p.set_defaults(func=cmd_shelters)

    p = sub.add_parser("requests", help="显示现场求助与未解决原因")
    p.add_argument("--snapshot", type=int, default=None)
    p.set_defaults(func=cmd_requests)

    p = sub.add_parser("priorities", help="显示当前优先级")
    p.add_argument("--urgency", default=None,
                   choices=["critical", "high", "medium", "low"])
    p.add_argument("--snapshot", type=int, default=None)
    p.set_defaults(func=cmd_priorities)

    p = sub.add_parser("diff", help="比较两个资料版本带来的变化")
    p.add_argument("--from-seq", type=int, default=None)
    p.add_argument("--to-seq", type=int, default=None)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("impact", help="资料变化后哪些已登记任务需要调整")
    p.add_argument("--open-only", action="store_true", help="只看仍需处理的任务")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("tasks", help="列出任务")
    p.add_argument("--status", default=None, choices=sorted(TASK_STATUSES))
    p.add_argument("--at-seq", type=int, default=None, help="按日志位置回放")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("task-add", help="登记待联络事项/任务")
    p.add_argument("--area", required=True)
    p.add_argument("--group", default="")
    p.add_argument("--action", required=True)
    p.add_argument("--priority-key", default="",
                   help="关联优先级键，如 contact:North:older_adults")
    p.add_argument("--owner", default="")
    p.add_argument("--due", default=None)
    p.add_argument("--note", default="")
    p.add_argument("--task-id", default=None)
    add_actor(p)
    p.set_defaults(func=cmd_task_add)

    p = sub.add_parser("task-update", help="更新任务状态/结果")
    p.add_argument("task_id")
    p.add_argument("--status", default=None, choices=sorted(TASK_STATUSES))
    p.add_argument("--owner", default=None)
    p.add_argument("--due", default=None)
    p.add_argument("--note", default="")
    p.add_argument("--outcome", default=None)
    add_actor(p)
    p.set_defaults(func=cmd_task_update)

    p = sub.add_parser("resources", help="列出临时资源")
    p.add_argument("--at-seq", type=int, default=None)
    p.set_defaults(func=cmd_resources)

    p = sub.add_parser("resource-add", help="登记临时资源安排")
    p.add_argument("--area", required=True)
    p.add_argument("--kind", required=True, choices=sorted(RESOURCE_KINDS))
    p.add_argument("--capacity", type=int, required=True)
    p.add_argument("--window-from", default="")
    p.add_argument("--window-to", default="")
    p.add_argument("--note", default="")
    p.add_argument("--resource-id", default=None)
    add_actor(p)
    p.set_defaults(func=cmd_resource_add)

    p = sub.add_parser("resource-update", help="更新临时资源")
    p.add_argument("resource_id")
    p.add_argument("--status", default=None, choices=sorted(RESOURCE_STATUSES))
    p.add_argument("--capacity", type=int, default=None)
    p.add_argument("--window-from", default=None)
    p.add_argument("--window-to", default=None)
    p.add_argument("--note", default="")
    add_actor(p)
    p.set_defaults(func=cmd_resource_update)

    p = sub.add_parser("note", help="登记值班记录")
    p.add_argument("--key", required=True, help="记录类别，如 handover/decision")
    p.add_argument("--text", required=True)
    add_actor(p)
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("state", help="回放某一日志位置的任务与资源")
    p.add_argument("--at-seq", type=int, default=None)
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("review", help="生成可追溯复盘")
    p.add_argument("--markdown", default=None, help="写入 Markdown 文件")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("close", help="结束事件")
    p.add_argument("--summary", default="")
    add_actor(p)
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("reopen", help="重新打开事件以补充记录")
    p.add_argument("--reason", default="")
    add_actor(p)
    p.set_defaults(func=cmd_reopen)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WarningResponseError as exc:
        print(f"错误 [{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
