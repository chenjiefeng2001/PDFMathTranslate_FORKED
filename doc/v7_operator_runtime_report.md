# V7 Operator-Based Runtime 迭代报告：从「图运行时」走向「文档智能运行时（DIR）」

> 本文档承接 `doc/v6_1_runtime_first_report.md`，落实《Document Intelligence
> Runtime（DIR）—— 面向文档计算（Document Computing）的运行时平台》评审意见：
>
> - 迭代结论：**项目已完成本质性范式转移**——它不再是「PDF 翻译器」或
>   「pdf2zh 的重构版」，而是一个 **Document Intelligence Runtime（DIR）**。
> - 本轮主战场：不再是「再加一个模块」，而是 **把 Runtime 从库（Library）
>   演进为可长期运行的运行时服务（Runtime Service）**。
> - 本轮已落地（V7.0 → V7.3，四层推进）：Property Graph、Operator Graph、
>   State Snapshot（WAL 风格）、Runtime Service。测试全绿。

---

## 一、为什么 V7 不再是「V6 的又一堆模块」

把 V3→V7 的演进画成时间线，可以看到一次**抽象层级**的跳跃：

```
V3     新增的是模块        Parser / Graph / Analyzer / Planner / Runtime / Memory
V4     新增的是模块        RuntimeKernel / KnowledgeCenter / Telemetry / Transaction
V5     新增的是模块        Workflow / ExecutionGraph / RuntimeSupervisor
V6     新增的是模块        RelayoutEngine / RenderAdapter / TransformationPipeline
V6.1   新增的是抽象层       BaseGraph（统一图骨架）+ DocumentRuntime（文档运行时）
V7     新增的是运行模型      Operator Graph（去 Pipeline）+ State Snapshot + Runtime Service
```

V6.1 起系统已经围绕 **Runtime 运转**，而不是 Pipeline。V7 在此基础上把
Runtime 真正「去 Pipeline 化」并「服务化」：

```
V7 目标模型（对齐 Airflow / Ray / Prefect / Dagster / PyTorch FX / LLVM PassManager）：

RuntimeService
  ├── ExecutionGraph        → 依赖调度 / 优先级 / 增量执行（ExecutionScheduler）
  ├── OperatorGraph         → Pipeline 消失，Runtime 自己就是 Pipeline
  │       └── Parse / Analyze / Plan / Translate / Review / Layout / Render
  ├── State Snapshot        → WAL 风格全量状态打点（rollback / diff / restore）
  ├── IncrementalEngine     → 基于 diff 的 Dirty Propagation，只重跑受影响子图
  ├── PersistenceLayer      → Snapshot / 图 / 遥测落盘与恢复
  ├── ResourceManager       → CPU / LLM / 并发配额
  ├── SessionManager        → 文档会话生命周期、上限、驱逐
  └── RuntimeNotificationBus → 统一事件发布 / 订阅 / 历史
```


---

## 三、本轮实现要点

### 3.1 `pdf2zh/v3/graph_property.py` — Property Graph（V7.0）

- `PropertySchema`：声明式节点类型 / 属性 Schema，`strict` 模式拒绝未知属性
- `PropertyGraph`：图数据库风格属性图
  - 节点存储 `nodes: Dict[id, node]`，边存储 `edges: List[PropertyEdge]`，
    完全兼容 `BaseGraph.adapt()` 鸭子类型（统一视图 / 遍历 / 快照）
  - **按类型索引** `_nodes_by_type` + **按属性值索引** `_prop_index`：
    `ids_of_type('Paragraph')` / `lookup('page', 3)` 均为 O(1) 定位
  - `upsert_node` 维护索引一致性（变更即重索引，不留脏索引）
  - `add_edge` 同步维护出/入邻接表，`neighbors()` 支持方向 + 关系过滤
- `PropertyQuery`：`MATCH ... WHERE ...` 声明式查询
  `query().where_type('Paragraph').where(page=3).collect()` —— 先走类型索引
  缩小候选集，再做属性过滤，避免全图 O(N) 扫描
- `create_property_graph_from_document()`：把 DocumentGraph 重建为带
  `page / bbox / font_size / confidence` 属性的类型化属性图；
  RuntimeService 每次 `_commit` 自动注册 `session.graphs["property"]`

### 3.2 `pdf2zh/v3/operators.py` — Operator Graph（V7.1）

- `OperatorContext`：管线中间状态的统一容器（document / document_graph /
  translations / outputs / metrics / graphs / extra）
- `Operator`：Parse / Analyze / Plan / Translate / Review / Layout / Render
  七个一等公民算子，`execute(ctx)` 在 DAG 上下文中读写同一份 Context
- `OperatorGraph`：声明式 DAG
  - `add(op, depends_on=[...])` 声明依赖，`order()` Kahn 拓扑排序
  - `run(ctx, filter_names=...)`：**依赖子图执行**（一次全跑 / 增量子集）
  - `prune_from(name)` / `dependents(name)`：算子级「只重跑受影响子图」裁剪
  - 每次运行记录 `_trace`，供遥测与测试断言
- `OperatorRegistry`：内置算子注册表，`RuntimeService` 用它构建算子图

### 3.3 `pdf2zh/v3/runtime_snapshot.py` — State Snapshot（V7.2）

- `RuntimeSnapshot`：**WAL 风格全量状态打点** —— document / graphs /
  translations / outputs / metrics / metadata（label + 时间戳），
  `to_dict/from_dict` 可序列化，`restore_into(session)` 一次性恢复全组件
- `SnapshotDiff`：组件级结构差异（`updated_components`），
  `between(before, after)` 支撑增量规划与审计
- `IncrementalEngine.plan(before, after)`：
  - 输入为 Document 时：逐块 `fingerprint` 计算 changed set
  - 输入为 RuntimeSnapshot 时：复用 SnapshotDiff 提取变化组件
  - 输出 `IncrementalPlan(changed, affected)`，changed 与 affected 一致
    （文档节点之间尚无跨引用边，后续 KnowledgeGraph 补边后即扩展到 transitive 影响集）

### 3.4 `pdf2zh/v3/runtime_service.py` — Runtime Service（V7.3）

- `SessionManager`：create / get / close / evict / list_ids，全局会话上限
- `ResourceManager`：信号量配额（llm / cpu / memory），`acquire(timeout)` +
  `release`，并发有界
- `ExecutionScheduler`：`plan()` 依赖拓扑规划、`run()` 按算子图执行，
  `stats()` 记录最近一次运行是否 incremental
- `IncrementalEngine`：`plan()` 计算 changed / affected 节点
- `PersistenceLayer`：Snapshot / 图数据落盘（json），`persist()` /
  `restore()`
- `RuntimeNotificationBus`：`publish` / `subscribe` / `history`，
  会话与执行生命周期事件可观测
- `RuntimeService`：服务化外观
  - `open(document, document_id, target_lang)` / `execute` /
    `execute_incremental` / `snapshot` / `rollback` / `persist` / `restore` /
    `status` / `close`
  - `_commit()` 自动注册 document / execution / constraint / property 图，
    并把 property graph 作为图的「数据库视图」与 DocumentGraph 并存
  - `_affected_operators()`：对受影响节点做算子依赖闭合，只重跑受影响子图

---

## 四、验证结果

```text
python -m pytest tests/v3 -q
→ 986 passed, 1 warning        # 含新增 tests/v3/test_v7_2_runtime_service.py 19 项，无回归

python -m pytest tests/v3/test_v7_2_runtime_service.py -v
→ 19 passed
```

端到端演示（已在 REPL 验证）：

```text
open → execute：
  stats: total_nodes=3, translated=2, rendered={html, pdf, text}
  events: [session.opened, execute.started, execute.completed]
  graphs: {document, execution, constraint, property}
  state : completed

snapshot('v1') → mutate translations → rollback：
  translations 恢复为快照值（WAL 语义，全组件回滚）

execute_incremental(['n0'])：
  算子 trace: [parse, translate, layout, review, render]   # analyze/plan 被裁剪
  scheduler.stats().last.incremental == True

persist('v2') → mutate → restore：
  translations 恢复为持久化值（跨进程可用）

status: nodes / translated / formats / snapshots / last_active 完整可观测
```

---

## 五、验收口径（Definition of Done）

1. **去 Pipeline 化**：一次执行 = OperatorGraph 拓扑调度，依赖子图裁剪
   （`prune_from`）与增量执行（`execute_incremental`）共用同一运行模型
2. **State Snapshot**：`rollback()` 是全组件恢复（图 / 翻译 / 输出 / 指标），
   而非部分状态修补；`persist/restore` 跨进程可重建
3. **服务化**：会话 / 调度 / 持久化 / 增量 / 资源 / 事件 六件套齐备，
   RuntimeService 作为长期运行入口而非一次性调用
4. **Property Graph**：图数量增长不再引发遍历退化 —— 类型 / 属性双索引
   + 声明式查询，且与 BaseGraph.adapt 完全兼容
5. **无回归**：v3 全量 986 项通过；既有 CLI（pdf2zh input.pdf）端到端可用

---

## 六、与上一轮迭代（V6.1）的关系

| 评审建议 | V6.1 迭代（已落地） | V7 迭代（本轮落地） |
| --- | --- | --- |
| 图爆炸 → 统一图骨架 | BaseGraph + adapt() | PropertyGraph（数据库化查询/索引） |
| Runtime 中心化 | DocumentRuntime + DocumentSession 状态机 | RuntimeService（服务化六件套） |
| Pipeline 去黑盒 | TransformationPipeline 保留 | OperatorGraph 取代 Pipeline 为执行模型 |
| Checkpoint 升级 | RuntimeCheckpoint | RuntimeSnapshot（WAL 风格全量快照） |
| 命名收敛 | Document OS（讨论稿） | **Document Intelligence Runtime（DIR）** |

下一阶段建议（V7.4+）：算子结果级缓存（cache-aside）、跨会话 KnowledgeGraph
增量传播、以及将 RuntimeService 暴露为进程外服务（gRPC/REST 适配层）。

| 阶段 | 落地模块 | 说明 | 状态 | 无头测试 |
| --- | --- | --- | --- | --- |
| V7.4 算子结果级缓存 | `pdf2zh/v3/operator_cache.py` | OperatorCacheSpec（输入/输出路径声明）+ OperatorResultCache（内容寻址 + LRU + deepcopy 隔离）；`OperatorGraph.run(cache=...)` cache-aside（命中→恢复、未命中→执行）；translate 的图副作用（translated_text）随算子输出一起缓存，保证 review 的输入签名跨会话一致 | ✅ 已落地 | `tests/v3/test_v7_4_operator_cache.py`（17 例） |
| V7.5 跨会话 KnowledgeGraph | `pdf2zh/v3/knowledge_graph.py` | KnowledgeGraph（实体/术语/概念/引用合并 + 序列化）+ KnowledgePropagator（`propagate()` 会话→共享图、`prepare_config()` 图→下一会话术语注入，本地配置优先）；`RuntimeService(knowledge=...)` 发布 `knowledge.propagated` 事件 | ✅ 已落地 | `tests/v3/test_v7_5_knowledge_graph.py`（21 例） |
| V7.6 进程外 Runtime Service | `pdf2zh/v3/remote_runtime.py` | RuntimeTransport（协议基类）+ RuntimeRestServer（线程化 stdlib HTTP 服务，daemon 线程、临时端口、上下文管理）+ RuntimeRestClient（open/execute/status/translations/snapshot/rollback/close/stats/health 全生命周期动词）；404/400/不可达错误语义统一为 `RuntimeRemoteError` | ✅ 已落地 | `tests/v3/test_v7_6_remote_runtime.py`（15 例） |

---

## 二、V7 迭代内容对照

| 评审建议（上一轮迭代报告） | V7 落地 | 状态 |
| --- | --- | --- |
| BaseGraph 还缺一层：Graph → GraphDatabase（Node/Edge/Property/Schema/Query/Traversal/Index） | `pdf2zh/v3/graph_property.py`：PropertyGraph + PropertySchema + PropertyQuery，按类型/属性建立索引，支持 `MATCH Paragraph WHERE page==3` | ✅ V7.0 |
| Runtime 下一步应该去 Pipeline 化（Runtime → TaskGraph → Scheduler → Node Executor → Operator） | `operators.py`：OperatorGraph（声明式 DAG）+ 依赖裁剪 `prune_from()` / `dependents()`，ExecutionScheduler 按拓扑调度 | ✅ V7.1 |
| Checkpoint 升级为 State Snapshot（Graphs/Knowledge/Cache/Memory/Workflow/Telemetry/Diagnostics/Plugins/Execution Queue） | `runtime_snapshot.py`：RuntimeSnapshot（图/翻译/输出/文档/元数据/时间戳）+ SnapshotDiff（组件级差异），真正支持 `rollback()` 而非恢复部分状态 | ✅ V7.2 |
| V6.2 核心 = Runtime 服务化（Session Manager / Execution Scheduler / Persistence / Incremental Engine / Resource Manager / Event Bus） | `runtime_service.py`：RuntimeService 完整服务化六件套 + `execute_incremental()` 增量执行 + `open/execute/snapshot/rollback/persist/restore/close` | ✅ V7.3 |
| V6.3 命名收敛（不再叫 Document Operating System） | 命名统一为 **Document Intelligence Runtime**，不再使用 OS 隐喻 | ✅ 已收敛 |
