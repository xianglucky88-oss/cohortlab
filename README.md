# CohortLab · 有约束的群体分析引擎

在指定租户内按主体属性及事件先后顺序筛选群体，仅输出经过小样本策略控制的聚合结果，保留可重复使用的内部群体快照及查询审计。

## 已实现

- 主体和事件的租户复合主键，跨租户外键隔离，事件 ID 幂等；同 ID 内容冲突拒绝，**冲突判定包含事件属性**。
- 事件携带标量属性入库（SQLite `events.attributes`，JSON 规范化存储）；旧库自动迁移，历史事件视为空属性，属性谓词对其按缺失字段处理。
- 两代声明式时序语言，均无用户 SQL 或表达式执行：
  - **v1（兼容）**：`"sequence": ["signup", "activate"]` 字符串列表，仅事件类型与相邻 gap。
  - **v2（当前）**：`"sequence": {"steps": [...], "not": [...]}`，正向步骤可带白名单属性谓词并声明 `optional`；`not` 否定条件带可选属性谓词；`max_total_seconds` 约束第一个到最后一个已消费事件的总时长。
- 有界规范化 AST：深度、节点数、序列长度、`in` 取值数、时间窗口等沿用 `QueryPolicy` 限制；拒绝全可选表达式，以及缺少两侧必需边界的否定条件。
- 有界生产执行器（Pareto 前沿状态机）与独立参考穷举解释器，属性测试以穷举结果为 oracle。
- 分组维度/分组数、主体/事件扫描量、序列长度和时间范围限制，外加每主体执行状态数上限 `max_states`，超限**可解释拒绝而非静默剪掉合法路径**。
- 小于 k 的单元格置空；只有一个小单元格时额外抑制一个互补单元格，存在抑制时隐藏总数。
- SQLite 事务内预留每日用户查询额度，缓存命中和失败请求也收费；拒绝请求写审计。
- 缓存键包含租户、用户、策略内容哈希、数据修订、**语言版本**和**规范化查询定义**；JSON 键顺序不同可复用，事件条件或语言版本变化不会串用旧结果。
- 内部快照保存当时的主体属性副本与语言版本；冻结查询仅使用冻结成员重新聚合，不重跑时序筛选。

## 时序语言语义（v2）

- 事件按 `(发生时间, 事件 ID)` 全序处理；同刻事件由事件 ID 决定先后。
- 每个正向步骤至多消费一个事件，同一事件不会被重复消费；跳过可选步骤不消费事件、不虚构事件，也不重置任何计时器。
- 正向步骤可声明 `optional: true`，但整条表达式至少保留一个必需步骤。
- `max_gap_seconds` 约束**相邻两个已消费事件**；`max_total_seconds` 约束从第一个到最后一个已消费事件的跨度（单个已消费事件跨度为零）。
- 否定条件只在**两个已消费正向事件之间的开区间**内判定：`last_consumed < negative < next_consumed`（按全序）。没有两侧边界的否定在规范化阶段被拒绝；尚未被后续正向事件封闭的间隔不会提前判定为“无退款”。
- 同一事件可同时匹配否定类型与某个正向步骤：先做正向匹配（该事件可能正是右边界，封闭旧间隔），再毒化仍未封闭的间隔。
- 执行器为每个已消费步骤数保留一条 Pareto 前沿：相邻 gap 只关心最后消费时间（越晚越占优），启用总时长时再叠加起点约束；互相权衡的替代路径都保留，因此“注册在第 0、90 秒，激活在第 100 秒，总时长上限 30 秒”能正确选用第二次注册；若第 95 秒出现退款，该路径因开放间隔被毒化而不成立。

## 运行

Python 3.11+，项目目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m cohortlab.cli --db /tmp/cohortlab-demo.sqlite --tenant demo ingest examples/population.json
python3 -m cohortlab.cli --db /tmp/cohortlab-demo.sqlite --tenant demo query examples/query.json --principal analyst
python3 -m cohortlab.cli --db /tmp/cohortlab-demo.sqlite --tenant demo query examples/query_v2.json --principal analyst
python3 -m unittest discover -s tests -v
python3 examples/smoke.py
```

v2 查询示例（注册 → 可选验证 → 激活，期间无 `status=ok` 的退款）：

```json
{
  "start": "2026-11-01T00:00:00Z",
  "end": "2026-11-03T00:00:00Z",
  "sequence": {
    "steps": [
      {"type": "signup", "where": {"field": "channel", "op": "in", "value": ["web", "app"]}},
      {"type": "verify", "optional": true, "where": {"field": "status", "op": "eq", "value": "ok"}},
      {"type": "activate", "where": {"field": "method", "op": "in", "value": ["email", "sms"]}}
    ],
    "not": [
      {"type": "refund", "where": {"field": "status", "op": "eq", "value": "ok"}}
    ]
  },
  "max_gap_seconds": 86400,
  "max_total_seconds": 172800,
  "group_by": ["region"]
}
```

提供 `--policy policy.json` 可覆盖 `QueryPolicy` 配置，如 `k/daily_queries/group_fields/max_candidates/max_states`。冻结查询仅允许 `{"snapshot":"...","group_by":["region"]}`，不能再指定事件条件。

## 模块与职责

- `policy.py`：不可变限额配置与策略摘要，新增 `max_states` 与事件属性白名单 `event_filter_fields`。
- `query.py`：规范化 AST（`prepare`）、有界谓词校验、Pareto 前沿生产执行器（`temporal_matches`）与聚合抑制；`sequence_matches` 保留为旧入口。
- `reference.py`：**参考穷举解释器**，独立枚举全部严格有序的事件选择并逐条核验 gap、总时长、开区间否定与不重复消费，仅共享 AST 形状和叶子谓词，作为测试 oracle。
- `engine.py`：事务、租户数据、属性入库与旧库迁移、快照、缓存、预算与审计；缓存/审计/快照身份统一绑定语言版本与规范化定义。
- `cli.py`：JSON 入口。

### 参考解释器与生产执行器的取舍

| 方案 | 正确性 | 成本 |
| --- | --- | --- |
| 保留全部路径（参考穷举） | 语义基准，直观可审计 | 状态/回溯随事件数组合增长，仅适合小流作 oracle |
| Pareto 支配消除（生产） | 仅丢弃被严格支配、不可能再成功的状态；替代路径完整保留 | 每事件前沿维护有界，受 `max_states` 硬上限保护 |
| 超限拒绝 | 不牺牲正确性，返回可解释错误并照常计费/审计 | 需要调用方收窄窗口或申请更高限额 |

## 语义与当前边界

没有序列条件时，时间窗口不筛掉主体，仅限定事件查询范围。数据集超过候选/事件扫描限额、或单主体执行状态超过 `max_states` 时会拒绝，当前不是分布式分析系统。所有查询的预算和本地计算在一个写事务中完成，优先保证额度与结果一致，吞吐受 SQLite 单写者限制。

小样本和互补抑制不等同差分隐私，无法保证抵抗多次重叠群体查询或外部辅助信息推断。快照、缓存和 SQLite 文件包含受控数据，需要本地访问保护；当前没有 HTTP 鉴权层、跨查询披露分析、加密存储或备份清理策略。
