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
