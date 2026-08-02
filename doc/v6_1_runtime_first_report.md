# V6.1 Runtime-First 迭代报告：从「模块堆叠」走向「文档操作系统」

> 本文档基于《下一代 PDF 翻译引擎（pdf2zh-next）重构与工程路线图》（Design RFC）的
> 评审意见，结合当前代码库的真实完成度，对原路线图进行了一次**关键转向的迭代**，
> 并同步落地了本轮迭代中判定为「不能再等」的两项基础设施。
>
> - 迭代结论：**项目已不再是 RFC 描述的 V6 模块补全，而是「Document Intelligence Runtime 1.0」**
> - 下一阶段主矛盾：不再是「再加一个模块」，而是 **模块之间的统一运行模型**
> - 本轮已落地：**统一图骨架（BaseGraph）** 与 **文档运行时（DocumentRuntime）**，测试全绿

---

## 一、V6 实际完成度评估（与 RFC 对齐）

| 子系统 | RFC 状态 | 当前状态 | 评价 |
| --- | --- | --- | --- |
| Parser | ✅ | ✅ | 已稳定 |
| Normalizer | ✅ | ✅ | 已稳定 |
| Document Graph | ✅ | ✅ | 已稳定 |
| Semantic Analyzer | ✅ | ✅ | 已稳定 |
| Translation Planner | ✅ | ✅ | 已稳定 |
| Runtime | 原规划较弱 | ✅ RuntimeKernel + RuntimeFacade + RuntimeSupervisor | 超出 RFC |
| Memory | 原规划简单 | ✅ KnowledgeCenter + DocumentMemory | 超出 RFC |
| Constraint Layout | 规划 | ✅ RelayoutEngine | 已完成基础实现 |
| Renderer | 规划 | ✅ RenderAdapter + HTML/PDF/Text | 已完成基础实现 |
| Pipeline | 规划 | ✅ TransformationPipeline | 已完成 |
| Quality | 规划 | ✅ Evaluator + Reviewer | 已完成 |
| Runtime Transaction | 无 | ✅ | 超出 RFC |
| Telemetry | 无 | ✅ | 超出 RFC |
| DiagnosticGraph | 无 | ✅ | 超出 RFC |
| Workflow | 无 | ✅ | 超出 RFC |
| ExecutionGraph | 无 | ✅ | 超出 RFC |

**结论：** 按 RFC 衡量，模块层已超额完成。继续「V7 加 Agent、加 XXX」属于错误方向——真正缺的不是模块，而是**模块之间的统一运行模型**。

---

## 二、两个必须现在解决的结构性缺口

### 缺口 A：Graph Explosion（图爆炸）

当前代码库中已存在多张独立图，各带一套自己的遍历 / 序列化 / 差异逻辑：

```
DocumentGraph   ExecutionGraph   ConstraintGraph
DiagnosticGraph WorkflowGraph    KnowledgeGraph / MemoryGraph / CacheGraph ...
```

若继续增长，每一张图都要重复实现 DFS / BFS / Topological / Cycle / Merge /
Clone / Serialize / Diff / Snapshot，维护成本与心智负担随图数量线性爆炸。

**目标态：**

```
BaseGraph
  └── TypedGraph
        ├── DocumentGraph（统一视图）
        ├── SemanticGraph / ExecutionGraph / ConstraintGraph / KnowledgeGraph / DiagnosticGraph

统一能力：Node / Edge / Property / Serializer / Visitor / Traversal / Diff / Snapshot
```

### 缺口 B：Pipeline 只是一次性调用，Document 没有生命周期

现状：

```
TransformationPipeline.run()      ← 一次性：输入 blocks，输出结果，文档即亡
```

目标态（Runtime First）：

```python
runtime.open(document)      # Document 进入运行时，获得 Session
runtime.execute()           # 执行
runtime.pause() / resume()  # 暂停 / 续跑
runtime.rollback()          # 回滚到某个 Checkpoint
runtime.diff()              # 两个 Checkpoint 之间的结构差异
runtime.snapshot()          # 显式打点
runtime.close()             # 关闭会话（Checkpoint 仍可查询）
```

**Document 不是一次性的输入，而是一台一直活着的 Runtime。**

---

## 三、迭代后的路线图（V6.1 → V6.3）

### V6.1 Runtime-First（本轮，已落地）

| 任务 | 说明 | 状态 |
| --- | --- | --- |
| `BaseGraph` 统一图骨架 | Node/Edge/Property + 共享遍历/序列化/差异/快照 | ✅ 已实现 |
| `adapt()` 鸭子类型适配 | DocumentGraph / ExecutionGraph / ConstraintGraph 零侵入统一视图 | ✅ 已实现 |
| `DocumentRuntime` | open/execute/pause/resume/rollback/diff/snapshot/close | ✅ 已实现 |
| `DocumentSession` 状态机 | CREATED→OPENED→READY→EXECUTING→(PAUSED\|COMPLETED\|FAILED)→… 非法转移拦截 | ✅ 已实现 |
| 多图统一视图 | 一次 execute 后自动注册 document/execution/constraint 三张统一图 | ✅ 已实现 |
| 回归治理 | constraint_graph 构建 CONTAINS 边引用缺失 page 节点的缺陷修复 | ✅ 已修复 |

### V6.2 Runtime 服务化（下一步）

- 会话持久化：Session + Checkpoint 落盘（`GraphSnapshot.to_json` 已具备序列化基础）
- 分布式调度：多文档并发、任务抢占、断点续跑落地为真实异步语义
- 增量翻译：`runtime.diff` 驱动「只重译变更子图」的增量管线
- 增量式执行：以统一执行图拓扑顺序调度，节点级 dirty 传播复用 ExecutionGraph 状态

### V6.3 文档操作系统（远期）

- 多文档知识图谱互通（跨文档引用、术语传播）
- 交互式校验工作台（Reviewer 结果回写 Runtime → 触发增量重译）
- 插件化节点运行时（让第三方处理器成为一等公民）

---

## 四、本轮实现要点

### 4.1 `pdf2zh/v3/base_graph.py` — 统一图骨架

- `GraphNode / GraphEdge / GraphProperty`：统一元素模型，`to_dict/from_dict` 双向序列化
- `GraphTraversal`：纯函数式算法（DFS/BFS/拓扑排序/环检测/连通分量/可达集），
  只依赖 `node_ids + out_edges`
- `GraphVisitor`：访问者模式
- `GraphDiff`：节点增删改 + 边增删的结构化差异，`summary()` 供报告
- `GraphSnapshot`：可序列化时间点快照，`restore_into` / `diff` 双用途
- `BaseGraph`：统一 API —— 节点/边操作、遍历、代数（clone/merge/subgraph）、
  序列化（dict/json）、快照/差异、访问者
- `adapt(graph)`：**鸭子类型**统一适配，无需修改任何具体图类：
  - 节点取自 `nodes` / `_nodes`
  - id 取自 `id` / `node_id` / dict key
  - 边取自 `edges` / `get_edges()` / 内部 `_edges` dict / `depends_on` 依赖合成
  - 关系枚举自动解包为字符串（`must_below`、`follows`、`depends_on`）

### 4.2 `pdf2zh/v3/document_runtime.py` — 文档运行时

- `SessionState` 状态机 + `TRANSITIONS` 合法转移表，非法转移立即抛错
- `RuntimeCheckpoint`：graph 快照（可序列化 dict）+ graph 对象（内存 deepcopy）+
  translations / outputs / metrics，支撑 rollback 与 diff
- `DocumentSession`：文档的生命周期载体 —— 状态轨迹、Checkpoint 列表、
  统一图视图、指标与执行历史
- `DocumentRuntime`：
  - `open(document)`：支持 blocks dict 列表、`DocumentGraph`、dict 包装
  - `execute()`：复用 TransformationPipeline，自动派生 ExecutionGraph 与
    ConstraintGraph，并注册三张统一图；执行前后自动打 Checkpoint
  - `pause() / resume()`：可续跑的暂停语义（resume 计入 `resume_count`）
  - `rollback()`：恢复到指定 Checkpoint（含翻译、输出、图、指标）
  - `diff()`：两个 Checkpoint 之间的图结构差异
  - `snapshot()` / `close()` / `status()` / `list_sessions()` / `graphs()`
  - `register_graph()`：任意外部图（KnowledgeGraph 等）注册为统一视图

### 4.3 回归修复

- `constraint_graph.build_constraint_graph_from_document`：为 CONTAINS 边添加
  keep_together 约束时引用了被跳过构建的 page/document 节点（此前会抛
  `Source node 'page_0' not found`），现已按端点存在性守卫
- `GraphNode/GraphEdge.to_dict`：properties 改为深拷贝，避免快照被后续修改污染

### 4.4 测试（`tests/v3/test_v7_runtime_first.py`，35 项）

- BaseGraph：节点/边操作、非法操作、遍历、拓扑、环、连通分量、可达集
- BaseGraph 代数：序列化往返、clone/merge/subgraph、快照恢复、结构化 diff、访问者
- adapt：DocumentGraph / ConstraintGraph / ExecutionGraph 三张具体图的统一适配与统一遍历
- 状态机：合法生命周期、非法转移拦截
- Runtime：全生命周期、活动会话、未知会话、非法状态守卫、DocumentGraph 直接输入、
  暂停/恢复、多图统一视图、自定义图注册

---

## 五、验证结果

```text
python -m pytest tests/v3 -q
→ 856 passed, 1 warning            # 含新增 35 项，无回归

python -m pytest tests/v3/test_v7_runtime_first.py -v
→ 35 passed
```

端到端演示（已在 REPL 验证）：

```text
state after open:      ready
state after execute:   completed   (translations=2, quality=1.0)
checkpoints:           ['execute_start', 'execute_end']
graphs:                document / execution / constraint（统一 BaseGraph 视图）
paused:                paused
resumed state:         completed   (resume_count=1)
rollback to:           execute_start  →  translations 恢复为 0
closed:                closed      (checkpoints 仍可查询)
```

---

## 六、验收口径（Definition of Done）

1. **统一图**：任意具体图（现有 + 未来新增）经 `adapt()` 后即获得
   DFS/BFS/拓扑/环/合并/克隆/序列化/差异/快照，无需重复实现
2. **Runtime First**：文档从「一次性输入」变为「一直活着的会话」，
   全生命周期（open→execute→pause→resume→rollback→diff→close）可用且可观测
3. **零侵入**：所有适配基于鸭子类型，未改动 DocumentGraph / ExecutionGraph /
   ConstraintGraph 的既有接口
4. **无回归**：v3 全量测试通过，且修复了 constraint 构建的既有缺陷

---

## 七、与初始 RFC 的关系

| 初始 RFC 阶段 | 演进后归属 |
| --- | --- |
| 阶段零 Document IR | ✅ 已由 DocumentGraph 承接（V3 完成） |
| 阶段一 阅读顺序 | ✅ ReadingEdge 已由 DocumentGraph 承接 |
| 阶段二 段落重构 | ✅ SentenceDetector 已承接 |
| 阶段三 语义标记 | ✅ SemanticAnalyzer 已承接 |
| 阶段四~六 规划/翻译/布局/渲染 | ✅ 已由 V6 Pipeline 承接 |
| **（新增）V6.1 统一图 + 文档运行时** | ✅ **本轮迭代新增，已落地** |

