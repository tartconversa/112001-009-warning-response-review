# 气象灾害预警响应复盘

值班用的本地分析应用：导入预警事件、匿名化人口暴露、消息回执、避难点容量与
现场求助记录，看清「风险覆盖 → 居民确认 → 实际避险」之间的缺口；在事件进行中
登记待联络事项和临时资源，资料修订后仍能说明每个决定当时依据了什么。

仅使用 Python 标准库（Python 3.11+），无需安装依赖。

## 快速开始

```bash
python -m warning_response --store work.json init --event W100 --name "7·12 暴雨"
python -m warning_response --store work.json import data --label 初始资料
python -m warning_response --store work.json overview
python -m warning_response --store work.json review --markdown review.md
```

`data/` 是随附的合成样例，可直接导入。

## 命令

| 命令 | 作用 |
| --- | --- |
| `init --event ID [--name 名称]` | 新建事件工作区 |
| `import 目录 [--label 说明] [--as-of 时间]` | 导入或修订一次资料，生成不可变快照 |
| `overview` | 当前态势、优先级、不确定性与相比上一版的变化 |
| `warnings [--snapshot N]` | 预警版本、扩区/升级事件、各区域覆盖起点 |
| `groups [--area 社区]` | 每个社区/人口分组的响应漏斗与覆盖依据 |
| `shelters` | 按开放时段与已接纳人数计算的避难点容量 |
| `requests` | 现场求助与未解决原因分类 |
| `priorities [--urgency …]` | 当前需处理的优先级（含评分理由） |
| `diff [--from-seq N] [--to-seq N]` | 两版资料之间发生了什么变化 |
| `tasks [--status …] [--at-seq N]` | 列出任务；可按日志位置回放当时状态 |
| `task-add --area A --action 动作 [--group G] [--priority-key K]` | 登记待联络事项 |
| `task-update ID [--status …] [--outcome …]` | 更新任务状态与结果 |
| `resources` / `resource-add` / `resource-update` | 登记与调整临时资源 |
| `note --key 类别 --text 内容` | 班次交接、决定等值班记录 |
| `impact [--open-only]` | 资料变化后哪些已登记任务需要调整 |
| `review [--markdown 文件]` | 生成完整可追溯复盘 |
| `close` / `reopen` | 结束事件 / 为补录重新打开 |

`--actor` 用于记录操作人，默认 `duty`。错误以 `错误 [原因码]: 说明` 输出并返回退出码 2。

## 输入资料

每次 `import` 读取一个目录。文件名或列名可用常见同义词（如
`shelters_response.csv`、`region`、`confirmed_at`），但每列只能有一种含义。

| 文件 | 必需列 |
| --- | --- |
| `warnings.csv` | warning_id, version, issued_at, area_id, severity, valid_from, valid_to |
| `population_delivery.csv` | area_id, group_id, exposed_estimate, channel, targeted_at, status, acknowledged_at |
| `shelters_response.csv` | shelter_id, area_id, open_from, open_to, capacity, admitted_at, event_ref |
| `assistance_requests.csv`（可选） | request_id, area_id, requested_at, need, group_id, related_warning_id, person_ref, status, resolved_at, inability_reason |
| `exposures.csv` 或 `towns.csv`（可选） | area_id, group_id, count, source, granularity[, as_of] |
| `groups.csv`（可选） | group_id, label, count, source, granularity[, risk_notes] |

- 时间统一使用现场本地时间（带时区的值会被拒绝），格式为 ISO 8601。
- 没有 `exposures.csv` 时，暴露人数由发送目标人数推导，并明确标注为下限估计。
- 记录级别的问题（无法识别的状态、早于发送的确认、未登记关闭时间等）不会被
  静默丢弃，而是进入复盘的「资料不确定性」。

## 关键口径

这些口径直接决定复盘结论，代码中有对应测试。

1. **覆盖按版本与区域计算。** 覆盖起点取首个包含该区域的预警版本的发布时间。
   后续版本新增的区域不算「已提前收到通知」；早于该时间或晚于有效期的联络
   单独列出，不计入本次通知。
2. **发送、送达、确认、抵达是四种状态。** 渠道成功只说明消息发出；居民确认需
   明确的确认状态或确认时间；避难点接纳按设施统计，不能与发送条数相减。
3. **容量按开放时段计算。** 只有评估时点处于开放时段内的容量计入可用容量；
   已接纳人数只统计开放时段内的登记，且只计评估时点之前已发生的接纳。
4. **聚合人口不识别个人。** 输入只有聚合人口时，复盘不会声称识别到具体个人；
   全体人口与子分组并存时，各组人数不可相加。
5. **资料修订不覆盖历史。** 每次导入生成不可变快照；任务登记时复制当时的
   优先级与分组证据，之后修订只会产生对比，不会改写当时所见。
6. **评估时点一致。** `--as-of`（默认取资料中最晚时间）同时作用于预警、回执、
   避难点与求助；评估时点之后才发生的记录不参与结论。

## 结构与扩展

```
warning_response/
  data.py      资料解析、列名归一化、不确定性与推导
  values.py    受控词表（状态、强度、需求、人口粒度）
  analysis.py  单版本分析：覆盖、漏斗、容量、优先级、时间线、版本对比
  store.py     追加式工作区：快照、任务、资源、按日志位置回放
  review.py    复盘装配与 Markdown 渲染、任务影响评估
  cli.py       命令行入口
```

`build_analysis()`、`compare_analyses()`、`build_review()` 都是纯函数，可直接
在脚本或界面层调用。

## 本地验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q warning_response tests
```
