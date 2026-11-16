# CohortLab · 有约束的群体分析引擎

在指定租户内按主体属性及事件先后顺序筛选群体，仅输出经过小样本策略控制的聚合结果，保留可重复使用的内部群体快照及查询审计。

## 已实现

- 主体和事件的租户复合主键，跨租户外键隔离，事件 ID 幂等；同 ID 内容冲突拒绝。
- 有界声明式条件树和字段白名单，无用户 SQL 或表达式执行。
- 半开时间窗口、有序事件序列、连续步骤间最大时间差；同一条事件不重复满足多个步骤。
- 分组维度/分组数、主体/事件扫描量、序列长度和时间范围限制。
- 小于 k 的单元格置空；只有一个小单元格时额外抑制一个互补单元格，存在抑制时隐藏总数。
- SQLite 事务内预留每日用户查询额度，缓存命中和失败请求也收费；拒绝请求写审计。
- 缓存键包含租户、用户、策略内容哈希、数据修订和查询定义。
- 内部快照保存当时的主体属性副本；后续可按当前策略重新聚合，用户隔离并保留原始数据修订号。
- 内部样本计划：从冻结快照选出恰好 N 位主体，同时满足两个属性维度的非负整数上下界配额；下界可行流/最小费用环流精确求解，小规模枚举 oracle 交叉校验，资源超限独立拒绝，选择可复现。

## 运行

Python 3.11+，项目目录执行：

```bash
export PYTHONPATH="$PWD/src"
python3 -m cohortlab.cli --db /tmp/cohortlab-demo.sqlite --tenant demo ingest examples/population.json
python3 -m cohortlab.cli --db /tmp/cohortlab-demo.sqlite --tenant demo query examples/query.json --principal analyst
python3 -m unittest discover -s tests -v
python3 examples/smoke.py
```

样例 4 位主体中 3 位满足专业版用户、先注册再激活的事件序列；按地区聚合返回 east=3。重复查询命中持久缓存；对全量 4 人按地区统计时，west=1 与互补的 east=3 均被隐藏。

提供 `--policy policy.json` 可覆盖 `QueryPolicy` 配置，如 `k/daily_queries/group_fields/max_candidates`。查询示例见 `examples/query.json`；冻结查询仅允许 `{"snapshot":"...","group_by":["region"]}`，不能再指定事件条件。

## 内部样本计划（可复现配额选择）

群体筛选完成后，研究作业可在**已冻结的 snapshot** 上再做一次精确的配额选择。该能力是独立的进程内 Python 作业边界（`CohortEngine.build_sample_plan`），不新增 CLI、公开查询或鉴权层；入选主体 ID 只返回给已通过租户/principal 认证的受信调用方。

输入只接受现有租户/principal 可读的冻结成员及其属性副本，加上 `n`、`seed` 与两张配额表；不接受新事件、筛选条件或时间窗口，也不重新执行时序查询。方法整体只读：无论成功或失败都不创建 snapshot、不写查询 cache/audit、不消耗查询预算。

### 三个分离的模块

| 模块 | 职责 | 是否接触存储 |
| --- | --- | --- |
| `planning/quota.py` | 请求规范化、类别确定语义、单维可行性诊断与聚合 inventory | 否，纯函数 |
| `planning/solver.py` | 小规模枚举全部 N 元子集；大规模下界最小费用环流；节点/边/计算上限 | 否 |
| `planning/planner.py` | 计划身份、稳定整数成本、并列 tie-break 与编排 | 否 |
| `engine.build_sample_plan` | 冻结成员只读适配：租户/principal 校验、只读事务、成员上限 | 是，仅 SELECT |

### 为什么不能逐项比较库存

一个主体同时挂在两侧类别上（例如 `east/young`），两侧配额通过主体耦合。成员为 `east/young、east/young、west/old`，要求 `N=2`、east 至少 2 且 old 至少 1 时，每个维度单独看库存都够，整体却无解。流网络把这种耦合显式建模：

```
S -> 地区类别 c          下界/上界 = 地区配额 [lo, hi]
c -> 主体 i             [0, 1]（i 的地区侧属于 c）
主体 i -> 年龄段类别 d    [0, 1]，费用 = i 的确定性整数权重
d -> T                  [lo, hi] = 年龄段配额
T -> S                  [N, N] 固定回流边
```

每个单位流量都是一位且仅一位入选主体：从其地区节点进入、从其年龄段节点离开。下界用标准环流变换处理（保留每条边下界，节点平衡由超级源/汇补足）。阶段一用 SPFA 推送全部强制单位证明可行性，不可行时从残量可达集生成 min-cut 争用证据（`joint_quota_contention`，含 `forced_supply/routed/cut_capacity` 与只含类别名的可达侧摘要），**绝不静默放松任何配额**。阶段二在核心网络上用 Bellman-Ford 精确负环消去降费用：饱和 `[N,N]` 回流边的残量反向边（容量 N 的 `S→T`）闭合每个改进交换并把循环总量钉死为 N，所以每个负环取消与选入的主体数相等，只交换人选、不改变总数，最终得到总费用最小解。

### 稳定成本与并列语义

主体 `i` 的主成本为 `sha256(canonical([协议版本, seed, 主体ID]))` 的前 8 字节大端整数；哈希输入不含 set/dict 遍历或受 hash 种子影响的容器，因此与 `PYTHONHASHSEED` 无关（测试用多个 hash seed 子进程验证）。这是确定性键控排序，**不声称均匀随机，也不是差分隐私**。

成本总和相等时，结果必须是入选 ID 有序列表的字典序唯一最小值。设 ID 按序排名 `r=0..m-1`、`SCALE=2^m`，主体权重编码为 `primary*SCALE - 2^(m-1-r)`；固定 N 元子集的 tie 项之和落在 `[0, SCALE)` 内，主成本相差 1 也主导任何并列重排，而最小化 tie 项恰好等价于字典序最小。Python 任意精度整数保证编码精确。

### 确定语义与资源边界

- 两个维度的每个类别声明非负整数 `min/max`（也接受 `[lo, hi]` 数组）；缺省 `min=0`、`max=N`；`min>max` 或 `max>N` 拒绝计划。
- 配额里出现冻结人口不存在的类别 → 整计划请求拒绝（未知类别）；人口存在但配额未声明的类别隐式 `[0,N]`。
- 属性缺失或为 `null` 归入显式缺失类别 `"__none__"`，可在配额中显式约束；旧式歧义标记 `"__missing__"` 保留并拒绝；`"east,west"` 这类多类别字符串是**一个不透明标签**，不会被拆分；数值 `1` 与字符串 `"1"` 是不同类别；布尔/列表/字典属性值拒绝。
- `N=0`、空群体、重复主体 ID、非字符串 seed、未知字段（事件/过滤/时间窗口）均有确定拒绝语义；空群体与人口小于 N 走可行性诊断而非请求错误。
- 所有遍历基于排序列表与插入序 dict，不受进程 hash 随机化影响。
- 计划身份 `plan_id` 绑定：冻结内容摘要（成员 ID 与属性副本的规范化哈希）、两侧配额、N、seed、求解协议版本。之后 ingest 修改当前人口属性不会改变同一计划的选择（测试覆盖属性更新复算）。
- 大规模在策略声明的 `max_plan_members/max_plan_nodes/max_plan_edges/flow_steps_budget` 内求解；超限抛 `PlanCapacityError`（**不是** `PlanInfeasible` 的子类），不与配额无解混淆，也不做静默近似。枚举子集数超过 `enumeration_limit` 时自动切换到精确流方法；`enumeration_limit=0` 可强制走流路径用于对照。
- 普通诊断只给配额冲突与聚合 inventory（类别名、可用量、上下界、cut 安全摘要），不含主体 ID。

### 用法

```python
plan = engine.build_sample_plan("tenant-a", "analyst-1", {
    "snapshot": frozen_snapshot_id,
    "n": 100,
    "seed": "research-job-2026-11",
    "region": {"east": {"min": 40, "max": 70}, "west": {"max": 30}},
    "age_band": {"30-39": {"min": 20}, "40-49": {"max": 60}},
})
plan["selected_ids"]   # 仅受信进程内作业可见
plan["plan_id"]        # 含冻结摘要/配额/N/seed/协议版本
```

策略中的计划限额可通过 `QueryPolicy(max_plan_members=..., max_plan_nodes=..., max_plan_edges=..., flow_steps_budget=..., enumeration_limit=..., plan_dimensions=("region","age_band"))` 声明；两个计划维度必须是已声明的 group 字段。示例见 `examples/sample_plan.py`。

## 模块与扩展

`policy.py` 提供不可变限额配置；`query.py` 负责校验、筛选、序列算法和抑制；`engine.py` 负责事务、租户数据、快照、缓存及预算；`cli.py` 是 JSON 入口。

```python
from cohortlab import CohortEngine, QueryPolicy

policy = QueryPolicy(k=5, daily_queries=50,
                     filter_fields=("region", "plan"), group_fields=("region",))
engine = CohortEngine("cohort.sqlite", policy=policy)
try:
    # query 是带 start/end 的声明式查询，不是 SQL。
    result = engine.query("tenant-a", "analyst-1", query)
    frozen = engine.query("tenant-a", "analyst-1", {
        "snapshot": result["snapshot"], "group_by": ["region"]})
finally:
    engine.close()
```

将当前聚合层抽象为策略可继续增加统计指标；将查询规划与存储适配拆分可支持列式引擎。服务端接入时，租户/用户必须由受信任认证上下文提供，不能直接信任终端参数。

## 语义与当前边界

事件排序为 `(发生时间, 事件ID)`，同时刻不同事件可按此顺序推进序列。序列约束是相邻步骤最大间隔，整体范围由查询时间窗口限制；当前不支持整个序列的额外总时长或事件属性谓词。

没有序列条件时，时间窗口不筛掉主体，仅限定事件查询范围。数据集超过候选/事件扫描限额时会拒绝，当前不是分布式分析系统。所有查询的预算和本地计算在一个写事务中完成，优先保证额度与结果一致，吞吐受 SQLite 单写者限制。

小样本和互补抑制不等同差分隐私，无法保证抵抗多次重叠群体查询或外部辅助信息推断。快照、缓存和 SQLite 文件包含受控数据，需要本地访问保护；当前没有 HTTP 鉴权层、跨查询披露分析、加密存储或备份清理策略。

内部样本计划是确定性配额选择，不是均匀随机抽样也不是差分隐私：seed 只决定稳定成本排序，不提供任何概率或隐私保证。计划不改变时序匹配、披露规则、缓存生命周期与备份的现有行为；它只读既有 snapshot，失败不留任何写入痕迹。
