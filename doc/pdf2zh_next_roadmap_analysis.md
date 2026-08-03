# 下一代 PDF 翻译引擎（pdf2zh-next）重构与工程路线图 —— 现状对照报告 v3.0

> **版本：** v3.0（2026-08-03）
> **基线：** git HEAD `dcebfbc`（V7.6 之后）+ 当前工作区（GUI 模块化改造、`services/runtime_service.py` 更新未提交）
> **依据：** 《下一代 PDF 翻译引擎（pdf2zh-next）重构与工程路线图》（十二阶段 Design RFC）与 `doc/v3_architecture_analysis_report.md`、`doc/V4_MIGRATION_REPORT.md`、`doc/v6_1_runtime_first_report.md`、`doc/_archive/pdf2zh_next_roadmap_analysis.v1.md / .v2.md` 历次迭代报告
> **核查范围：** `pdf2zh/v3/`（53 个模块）、`pdf2zh/services/runtime_service.py`、`pdf2zh/gui/`、`tests/v3/*` 无头测试套件

---

## 摘要 / TL;DR

v2.0 报告（2026-07-31）的核心判断——*"先 Phase M 收运行时，再谈排版引擎""Document IR 仍是渲染树""Memory 缺 Style/Reasoning 两层""没有文档级会话 Runtime"*——在 v2.0 之后已被 V6.0 → V7.6 共六轮迭代逐一兑现：

| 版本 | 交付 | 回应 v2.0 哪一章 |
| :--- | :--- | :--- |
| **V6.0** | `document_ir.py`（四 Role 语义 IR + JSON 序列化）、`graph_registry.py`（四图统一 node_id）、`sentence_detector.py`（段落语义重构）、`planner_chain.py`（语言/领域/置信度决策链）、`memory_layers.py`（Style + Reasoning 记忆）、`multi_channel_rewriter.py`（多通道翻译）、`relayout_engine.py`（约束重排）、`constraint_graph.py`（四档优先级约束图）、`review_agent.py`（评审 Agent + 质量门） | 三、四、五、六、七、十一 |
| **V6.1** | `document_runtime.py`（DocumentSession 文档级生命周期 + 断点）、`translation_runtime.py`（翻译工作流） | 八 |
| **V6.2** | `document_intelligence.py`（Entity / Concept / Citation 知识图） | 三、六 |
| **V7.1** | `operators.py`（OperatorGraph 声明式算子 DAG，Runtime 去 Pipeline 化） | 八、十一 |
| **V7.2** | `runtime_snapshot.py`（12 组件完整状态快照，rollback 成为真实操作） | 八 |
| **V7.3** | `services/runtime_service.py`（GUI / Flask / MCP 三端统一 RuntimeService）+ GUI 模块化 | Phase M / M1 |
| **V7.4** | `operator_cache.py`（算子结果级缓存，cache-aside） | 八 |
| **V7.5** | `knowledge_graph.py`（跨会话知识图，增量传播） | 六、八 |
| **V7.6** | `remote_runtime.py`（REST 远程运行时适配层） | 八、Phase M |

`pdf2zh/v3/` 模块数 **34 → 53**。十二阶段对照矩阵的状态从"大面积 🔴/🔶"整体上移为 **"架构底座 ✅、迁移闭环 🔴"**。

**v3.0 最重要的判断：**

> 路线图的架构底座（统一 IR、四图关联、四层记忆、约束重排、文档级 Runtime、算子化执行、质量闭环）已从"规划"变成"可用代码 + 无头测试"。系统当前的真正瓶颈**不再是缺组件，而是迁移未闭环**：
>
> 1. **Phase M 未闭环**：`use_v4_engine=False` 仍是全有全无的布尔开关，无按页/按文档类型/按用户的灰度维度，无 legacy↔V4 回归 diff 基线，无回退率遥测；
> 2. **DocumentIR 未被主链路消费**：legacy 仍 `LTChar → obj_patch` 直达渲染；`RuntimeFacade`/`ParseOperator` 仍产出 `DocumentGraph + VisualTree`，`DocumentIR` 与它们三套模型并存；
> 3. **主链路仍走贪心推挤**：约束布局（阶段六）、段落重构（阶段二）、四层记忆注入（阶段五）在 V4 侧完备，但 `use_v4_engine=False` 使主链路用户完全看不到。

**优先级因此从 v2.0 的"补齐 IR / 记忆 / 约束组件"升级为"关迁移闭环 + 建回归基线 + 接管主链路"。**

---

## 一、视角转变：从"缺组件"到"闭环缺失"

v2.0 把 V4-V5 基础设施当作"功能模块清单"来核查，核心结论是"缺统一 IR、缺四图 ID 关联、缺 Style/Reasoning 记忆、缺文档级会话"。本轮核查（v3.0）的结论是：**这些组件已经全部补齐，且每一轮都带有配套无头测试**（`tests/v3/test_v6_design_rfc.py`、`test_v7_runtime_first.py`、`test_v7_2_runtime_service.py`、`test_v7_4_operator_cache.py`、`test_v7_5_knowledge_graph.py`、`test_v7_6_remote_runtime.py`）。

因此视角必须再次升级——从"评估组件齐不齐"变为"评估**迁移闭环通不通**"：

- **组件就位 ≠ 主链路已接管**：`ServiceConfig.use_v4_engine=False`（默认），GUI/CLI 实际执行仍走 legacy `translate_stream`；
- **组件就位 ≠ 数据流已统一**：`DocumentIR` 与 legacy 的 `LTChar`、`DocumentGraph+VisualTree` 是三套事实来源，互不消费；
- **组件就位 ≠ 质量可证明**：`QualityEvaluator` 五维打分只作用于 V4 图，legacy 主链路产出没有任何"与 V4 同源 diff"的回归基线。

一句话：**V4 侧已经造好了一台完整的"车间"，但主链路流水线还没有把工件送进去。**

---

## 二、Phase M：运行时迁移层核查（v3.0 最重要的更新）

### 2.1 架构不变：Strangler 模式三层结构

```text
┌─────────────────────────────────────┐
│ ① Adapter 层                          │
│    LegacyAdapter / TranslateConverter│
│    Strangler（双向：把 legacy 组件包成 │
│    V4 Runtime 兼容接口）              │
└───────────────┬─────────────────────┘
                ▼
┌─────────────────────────────────────┐
│ ② V4 Runtime（唯一执行体）            │
│    DocumentRuntime + DocumentSession │
│    + DocumentIR（Single Source of Truth）│
└───────────────┬─────────────────────┘
                ▼
┌─────────────────────────────────────┐
│ ③ Compatibility Layer                │
│    CLI / API / GUI / MCP 对外契约保持 │
└───────────────┬─────────────────────┘
                ▼
    Progressive Cutover（灰度接管：按页 / 按文档类型逐步把 use_v4_engine 置为默认，legacy 降级为兜底）
```

### 2.2 M1 出口统一：🔶 明显推进，但仍未闭环

| 维度 | v2.0 现状 | v3.0 现状 |
| :--- | :--- | :--- |
| GUI / Flask / MCP 三端 | 🔶 部分统一 | ✅ `services/runtime_service.py`（V7.3）`RuntimeService` 已是三端唯一服务层；GUI 经 `gui/worker.py → get_runtime_service()` 提交任务，事件经 `gui/event_bridge.py` 转发 |
| 任务模型 | 21 参数元组 | ✅ `TranslationRequest` 强类型 dataclass + `TaskStage`（13 阶段，含 REPAIRING）+ `TaskProgressEvent` 流式事件 |
| 远程出口 | 无 | ✅ `v3/remote_runtime.py`（V7.6）`RuntimeRestServer / RuntimeRestClient`，`open / execute / status / translations / snapshot / rollback / close / stats / health` 与 `RuntimeService` 生命周期动词一一对应；transport 接口可替换为 gRPC |
| 旁路 | 🔴 `high_level.translate_stream` 直走 legacy | 🔴 **仍存在**：`runtime_service._execute_legacy` 内部直接调用 `high_level.translate_stream`；V4 路径是"同一服务上的两个引擎"而非"同一运行时上的两个引擎" |
| CLI / 公共 API | 🔴 整体旁路 | 🔴 **仍未统一**：`pdf2zh/pdf2zh.py`（CLI）与 `pdf2zh.high_level.translate / translate_stream`（公共 API 契约）完全绕过 `RuntimeService` |
| 会话模型 | 🔴 无文档级会话 | 🔶 `DocumentSession`（V6.1）已存在，但 `RuntimeService` 的任务级 `_TaskStore` 与它仍是两套状态模型，未合并 |

### 2.3 M2 数据统一：组件就位（✅），主链路未接入（🔴）

- **✅ IR 组件已就位**：`document_ir.py`（V6.0）正是 v2.0 第三章的目标态——四 Role（`SemanticRole` / `ReadingRole` / `TranslationRole` / `RenderingRole`）+ `IRBuilder` + `to_json()/from_json()` 稳定 JSON schema。
- **🔴 接入缺失**：没有任何主链路入口消费 `DocumentIR`：
  - `ParseOperator` 仍产出 `DocumentGraph`（`TransformationPipeline.build_graph_from_blocks`），`RuntimeFacade.load()` 仍是 `PDFParser → Normalizer → DocumentGraphBuilder`；
  - legacy 仍 `LTChar → obj_patch` 直达渲染，"解析直通渲染"的双轨未砍掉；
  - `graph_registry.py` 的四图关联建立在 `DocumentGraph` 节点上，与 `DocumentIR` 的 node_id 命名空间尚未合并。

### 2.4 M3 流量切换：🔴 未开始

- `feature_flags.py`：`use_v4_engine=False` 全量关闭，**没有"哪些页/哪些文档类型/哪些用户走 V4、哪些走 legacy"的规则引擎**，开关是全有全无的；
- **无兼容性回归基线**：对同一份 PDF，legacy 与 V4 输出没有自动 diff（页数、文本内容、bbox、重叠率），无法量化"接管了会不会更差"；
- **无迁移遥测**：`runtime_kernel.py` 的 `TelemetryCollector` 未采集回退率 / 灰度维度指标。

> **Phase M 的排期理由在 v3.0 下比 v2.0 更紧迫**：V6.0–V7.6 已把约束求解、多通道翻译、知识图、文档级 Runtime 全部做好并挂在 V4 侧，但 `use_v4_engine=False` 让这些能力对主链路用户完全不可见——**组件侧的投入产出比正在被迁移闭环拖累**。下一步的最高杠杆是关 Phase M，而不是再写组件。

---

## 三、Document IR：v2.0 的最大缺口已被 V6.0 补齐，现存缺口是"消费"

### 3.1 现状：`document_ir.py` 就是 v2.0 第三章的目标态

| 维度 | v2.0 判断 | v3.0 现状 |
| :--- | :--- | :--- |
| IR 模型 | 🔴 `visual_tree.py`（渲染树）+ `graph.py`（DocumentNode），均非唯一事实来源 | ✅ `document_ir.py`（V6.0）：`DocumentIR`（`Document → Section → SemanticBlock → VisualBlock → TextRun` 层级），子节点按 **ID 引用**（非内嵌对象） |
| 语义 Role | 🔴 只有 `node_type` 一个维度 | ✅ 四 Role 独立可叠加：`SemanticRole`（22 值）、`ReadingRole`（9 值：MAIN_FLOW / COLUMN_1 / COLUMN_2 / SIDEBAR / HEADER_FLOW / FOOTER_FLOW / FOOTNOTE_FLOW / FOLLOWS_FIGURE / UNKNOWN）、`TranslationRole`、`RenderingRole` |
| 序列化 | 🔴 无 IR JSON | ✅ `to_json() / from_json()` 稳定 JSON schema，可跨进程 / 跨 Runtime 传递（这是 M2 "receive_layout 出口统一为 IR 序列化结构"的直接支撑） |
| 100 份测试集 | 🔴 无规模化语料与 IR 快照基线 | 🔶 `tests/v3/*` 有 V4–V7 无头套件，但**无 IR 序列化快照基线**（round-trip 等价性无人验证）、无规模化语料 |

### 3.2 现存差距（从"建模"转为"接入"）

1. **没有消费者**：`IRBuilder` 未被 `ParseOperator` / `RuntimeFacade.load()` 调用——解析产出仍是 `DocumentGraph`；
2. **三模型并存**：`DocumentIR` 与 `DocumentGraph + VisualTree` 并行存在；`ParseOperator` 注释自称产出 `DocumentGraph`，而 legacy 产出 `LTChar`；
3. **无 round-trip 基线**：`from_json` 还原后无人校验与原始 IR 等价。

---

## 四、多图体系：统一 ID 关联（v2.0 的最大差距）已实现

### 4.1 `graph_registry.py`（V6.0）——四图共享 node_id 命名空间

v2.0 第四章的"通过 ID 关联，而不是一张大图"已落地：

```text
node "p42"  ──► DocumentGraph: Paragraph(contains in Section 2)
          ──► SemanticGraph:  BODY_TEXT, defines term "Transformer"
          ──► LayoutGraph:    CANNOT_OVERLAP with Figure 3
          ──► ExecutionGraph: state=TRANSLATED, depends_on=[p41]
```

- `GraphKind`（DOCUMENT / SEMANTIC / LAYOUT / EXECUTION）四图注册；
- `GraphMembership` 提供"一个 node_id 属于哪几张图"的查询接口（即四图间穿梭）；
- 鸭子类型实现：对 `DocumentGraph`、`EntityGraph/ConceptGraph/CitationGraph`、`ConstraintGraph`、`ExecutionGraph` 四个既有实现**零侵入**；
- `base_graph.py`（`GraphKind` + 统一 traversal / serialization / diff / snapshot）提供统一图视图，`DocumentRuntime` 用它把多图收敛到同一个 backbone 上。

### 4.2 四图各自现状

| 图 | v2.0 状态 | v3.0 现状 |
| :--- | :---: | :--- |
| **Document Graph** | ✅ 已成型 | ✅ `graph.py`：`DocumentGraph` + 22 类 `NodeType` + 13 类 `EdgeType`（CONTAINS / FOLLOWS / PRECEDES / REFERENCE / CAPTION_OF / FOOTNOTE_OF / CITATION_OF / MUST_ABOVE / MUST_FOLLOW / CANNOT_OVERLAP / SAME_BASELINE / SAME_SECTION / DEPENDS_ON）+ 阅读边 + DAG 拓扑排序 |
| **Semantic Graph** | 🔶 实体级知识图 | 🔶 `document_intelligence.py`（V6.2）`EntityGraph / ConceptGraph / CitationGraph / KnowledgeFuser` 已成型，但仍为**实体级**，未与节点级语义图统一 |
| **Layout Graph** | 🔶 约束散落 | 🔶 `constraint_graph.py`（V6.0）已独立：17 类 `ConstraintRelation`（MUST_ABOVE/BELOW/LEFT/RIGHT、CANNOT_OVERLAP、ALIGN_*、CENTER_*、SAME_*、KEEP_TOGETHER、KEEP_WITH_NEXT、SAME_PAGE、FLOAT）+ 四档优先级；但 `graph.py` 的 `EdgeType` 仍含空间边，两处约束模型并存 |
| **Execution Graph** | 🔶 雏形 | ✅ `execution_graph.py`（8 态状态机 + dirty 级联）+ `workflow_engine.py`（条件/并行/合并/循环）+ `scheduler.py` 配套 |

### 4.3 现存差距

- 四图仍未通过**同一份 `DocumentIR.id`** 关联（注册对象仍是 `DocumentGraph` 系节点）；
- `ConstraintPriority` 双处定义（`graph.py`：HARD/SOFT；`constraint_graph.py`：HARD/SOFT/PREFERRED/STRONG），存在不一致风险。

---

## 五、Planner 升级：决策链已补齐，但计划仍未在主链路执行

### 5.1 v2.0 的三处差距 → v3.0 全部有实现

| v2.0 差距 | v3.0 实现 |
| :--- | :--- |
| 缺 Language / Domain / Confidence 检测 | ✅ `planner_chain.py`（V6.0）：`LanguageDetector`（脚本启发式，zh/ja/ko/en/...）、`DomainDetector`（学科域打分 → Reasoning Memory）、`ConfidenceDetector`（版面置信度 → 人工介入 / 回退决策）+ `PlannerChain` 决策链编排，产出完整 plan payload |
| 缺 Style / Reasoning 记忆输入 | ✅ `memory_layers.py`（V6.0）：`StyleMemory` + `ReasoningMemory` + `MemoryHub`（见第六章） |
| 未接入 V4 主链路 | 🔶 `translation_runtime.py`（V6.1）`TranslationWorkflow` 已按 plan 路由、chunk 依赖调度、记忆融合、术语一致性检查、失败自动重试、review/repair 闭环——**但仅存在于 V4 侧**；`RuntimeService._execute_v4` 走 `RuntimeFacade`（默认 `RuleBasedProvider` 占位翻译器），`_execute_legacy` 完全不经 Planner |

### 5.2 多通道翻译（阶段三"精细化 Prompt"的执行器）

`multi_channel_rewriter.py`（V6.0）已实现"多模态多通道翻译器"：

- `TextChannel`（正文 LLM 翻译）、`FormulaChannel`（公式直通，绝不改动公式文本）、`VerbatimChannel`（数字标识、代码、保持原样）、`CatalogChannel`（题注/参考文献/摘要经 `PromptManager` 路由）；
- 按 chunk 选通道，输出合并回文档级翻译字典——正是路线图"针对 Figure Caption 保编号、Formula 不拆行"的执行载体。

### 5.3 现存差距

- 决策链（PlannerChain）与执行链（TranslationWorkflow）都未接到 `use_v4_engine=True` 之外；
- `TranslationPlan` 无序列化/回放/审计文件输出，"计划可审计、可 diff"尚未产品化。

---

## 六、Memory 升级：四层记忆（v2.0 缺的后两层）已实现

### 6.1 四层记忆现状

| 层 | v2.0 状态 | v3.0 现状 |
| :--- | :--- | :--- |
| **Document Memory**（术语规范映射） | ✅ `memory.py` | ✅ `memory.py`：`EntityEntry / GlossaryEntry / AbbreviationEntry` 别名→规范名映射 |
| **Entity Memory**（编号实体） | ✅ `EntityGraph` | ✅ `document_intelligence.py`（V6.2）：实体别名、`occurrence_count`、`first_occurrence_page`、定义、跨页知识融合（`KnowledgeFuser`） |
| **Style Memory**（语体/时态/风格） | 🔴 缺失 | ✅ `memory_layers.py`（V6.0）：`StyleEntry` 结构化风格规则（key / value / source / confidence），`StyleMemory` 支持手动/检测/继承 |
| **Reasoning Memory**（领域/方法/主题） | 🔴 缺失 | ✅ `memory_layers.py`（V6.0）：`ReasoningMemory` 领域推断；`planner_chain.DomainDetector` 为其提供输入 |
| **汇聚/继承** | 🔴 无 | ✅ `MemoryHub`（DocumentMemory + Style + Reasoning 聚合）；继承链 Reasoning → Style → Document/Entity 语义已在模块注释中明确 |

### 6.2 Knowledge Center（v2.0 第八章"跨 Session 持久化知识库"）

`knowledge_graph.py`（V7.5）已实现**跨会话共享知识图**：

- entities / glossary / concepts / citations 四类知识，别名→规范名，`occurrence_count` 累计、`sessions` 归属跟踪；
- `KnowledgePropagator`：单会话知识**增量传播**进共享图，返回 `PropagationReport`（added / updated / total 计数）——受控学习的雏形；
- 与 `DocumentIntelligence`、`MemoryHub` 组合即为 Knowledge Center 的进程内实现。

### 6.3 现存差距

- **记忆仍未实际注入翻译 Prompt**：legacy `translation_cache.py` 仍是哈希缓存；V4 侧仅 `TranslationRuntime.consistency` 检查与 `ReviewAgent.glossary` 校验在消费记忆，Prompt 模板（`prompt_manager.py` / `planner.py`）未接入四层记忆 + 跨会话知识图；
- `KnowledgeGraph` 的持久化（save/load）与 `storage.py`（三档 Memory / Cache / Persistent-SQLite）未打通——**知识库仍是进程内对象，跨进程任务重启即失**；
- 术语漂移案例（LLM→大语言模型）所需的"首次出现形态固化"指令未落进任何 Prompt 模板。

---

## 七、Constraint 升级：约束重排已落地，但主链路仍是贪心推挤

### 7.1 v2.0 → v3.0 能力对照

| 能力 | v2.0 现状 | v3.0 现状 |
| :--- | :--- | :--- |
| 约束图 | 🔶 `constraint_graph.py` 自研简化版 | ✅ `constraint_graph.py`（V6.0）：17 类 `ConstraintRelation`（MUST_ABOVE/BELOW/LEFT/RIGHT、CANNOT_OVERLAP、ALIGN_LEFT/RIGHT/TOP/BOTTOM、CENTER_X/Y、SAME_WIDTH/HEIGHT、KEEP_TOGETHER、KEEP_WITH_NEXT、SAME_PAGE、FLOAT），`ConstraintSolver` 自研求解 |
| 约束优先级 | 🔶 `HARD/SOFT` 两级 | 🔶 升为 `HARD/SOFT/PREFERRED/STRONG` 四档；距路线图六档（+TYPOGRAPHY / READING / SEMANTIC 语义绑定档）还差两级，且与 `graph.py` 的 `ConstraintPriority`（两级）定义不统一 |
| 重排引擎 | 🔴 无 | ✅ `relayout_engine.py`（V6.0）：`ModelSelector`（物理行→逻辑块）→ `RelayoutSolver`（约束图构建 + 原生求解）→ `OutputAssembler`（bbox→assembly manifest） |
| 布局优化 | 🔴 无 | ✅ `optimizer.py`：`LayoutOptimizer`（OR-Tools CP-SAT，当前贪心启发式），目标 = overlap_penalty + whitespace_penalty + page_break_penalty |
| 碰撞检测 | ✅ R-Tree / PushDown | ✅ `collision_resolver.py`（R-Tree / PushDown）+ `layout.py` `CollisionEngine`（Sweep-Line），仍是仓库完成度最高组件（`tests/test_collision_resolver.py` 通过） |
| 自适应排版 | 🔶 | 🔶 `layout.py`：`InlineLayout`（字符级字距 / kerning）、`ColumnLayout`（分栏检测）已实现；但**缺"按译文长度动态计算行高 / 段落间距"**、缺 CJK 与拉丁字体基线对齐排印约束 |
| legacy 排版 | 🔴 贪心推挤 | 🔴 默认路径仍未接管：`layout_graph.py` + `paragraph_layout.py` 仍是主链路排版实现 |

### 7.2 关键判断

阶段六"约束布局求解"在 V4 侧已相当完整（约束图 + 四档优先级 + 重排引擎 + 优化器 + 碰撞检测四件套齐备）。**其价值取决于是否接管主链路**——建议下一步将 `relayout_engine + collision_resolver` 作为 **legacy 写回之前的约束重排 gate**（M2 数据统一后即可实施，不必等 V4 全量接管）。

---

## 八、Document Intelligence Runtime：v2.0 判断的"最大缺口"已被 V6.1 / V7.x 整体填平

### 8.1 v2.0 → v3.0 对照

| 能力 | v2.0 现状 | v3.0 现状 |
| :--- | :--- | :--- |
| 文档级会话 | 🔴 无 `DocumentSession` | ✅ `document_runtime.py`（V6.1）：`DocumentSession` + 9 态状态机（CREATED→OPENED→READY→EXECUTING→PAUSED/COMPLETED/FAILED→ROLLED_BACK→CLOSED，含合法迁移表 `TRANSITIONS`）+ `RuntimeCheckpoint` + `DocumentRuntime`（open / execute / pause / resume / rollback / diff / snapshot / close） |
| 状态快照 / 回滚 | 🔴 无 | ✅ `runtime_snapshot.py`（V7.2）：12 组件（graphs / knowledge / cache / memory / workflow / telemetry / diagnostics / plugins / queue / translations / outputs / metrics）完整快照；`capture / restore_into / save / load / diff`——rollback 成为真实操作而非部分恢复 |
| 算子化执行 | 🔴 流水线黑盒 | ✅ `operators.py`（V7.1）：`OperatorGraph`（声明式算子 DAG，Kahn 拓扑排序）+ `OperatorRegistry`（7 内置算子：Parse / Analyze / Plan / Translate / Review / Layout / Render）+ `OperatorContext.snapshot()`（JSON 化状态）+ `incremental_ids` 增量重译 + `prune_from` 子图复用 |
| 算子级缓存 | 🔴 无 | ✅ `operator_cache.py`（V7.4）：cache-aside，按算子声明输入/输出路径做内容摘要指纹，上游任何变更自动失效，无需维护失效列表 |
| 执行图 / 调度 | 🔶 雏形 | ✅ `execution_graph.py`（8 态 + dirty 级联）+ `workflow_engine.py`（条件 / 并行 / 合并 / 循环）+ `scheduler.py` + `transformation_pipeline.py` |
| 监控 / 自愈 / 追踪 | 🔶 未绑定生命周期 | ✅ `runtime_kernel.py`（EventBus / TelemetryCollector / KnowledgeCenter / MemoryCenter / PluginManager / Capability）+ `runtime_supervisor.py`（ResourceManager / RecoveryManager / 健康监控）+ `tracing.py`（嵌套 span 分布式追踪）+ `causal_graph.py`（根因诊断图）+ `runtime_context.py`（MicroKernel RuntimeContext 拆分） |
| 存储 | 🔴 无 | ✅ `storage.py`：三档存储（MemoryGraph / CacheGraph LRU+TTL / PersistentGraph SQLite）+ `StorageRuntime` 统一门面 |
| 远程运行时 | 🔴 无 | ✅ `remote_runtime.py`（V7.6）：`RuntimeTransport` 协议 + `RuntimeRestServer`（stdlib http.server）+ `RuntimeRestClient`（stdlib urllib），REST 路由覆盖 RuntimeService 全部生命周期动词 |
| 质量闭环 | 🔶 无 | ✅ `evaluator.py`（五维打分）+ `repair.py`（IssueGraph → RepairScheduler → 局部重译/重排 → 收敛）+ `review_agent.py`（ReviewAgent + QualityPipeline） |

### 8.2 分水岭判断（更新 v2.0 第八章结论）

> v2.0 说"把 V4 基础设施做扎实与做成下一代文档平台的分水岭，是有没有管理 Document 生命周期的 Runtime"。**v3.0 判断：这个分水岭已经跨过。** V6.1 提供了"车间主任"（DocumentRuntime + DocumentSession），V7.1–V7.6 提供了可声明（OperatorGraph）、可缓存（OperatorResultCache）、可快照（RuntimeSnapshot）、可远程（RuntimeRest）的执行底座。
>
> 现在的问题**不是缺运行时，而是运行时与主链路之间缺一条受控的迁移路径**。`DocumentSession`、`OperatorGraph`、`KnowledgeGraph` 全部是"V4 侧的好代码"，`use_v4_engine=False` 让它们对真实翻译任务零贡献。

---

## 九、十二阶段路线图 × 当前实现 对照矩阵（v3.0）

> 状态图例：✅ 已覆盖 · 🔶 部分覆盖 · 🔴 缺失/未接管。迁移依赖列沿用 Phase M 依赖关系。

| 阶段 | 路线图要求（基准） | v2.0 状态 | v3.0 现状 | 关键依据 / 迁移依赖 |
| :--- | :--- | :---: | :--- | :--- |
| **零** | 统一 Document IR（`Document/Page/Block/Line/Span`），PDF→IR 单向提取，JSON 序列化，100 份 PDF 测试集 | 🔶 | **✅/🔶** | ✅ `document_ir.py`（V6.0）：四 Role IR + `IRBuilder` + `to_json/from_json`；🔴 无消费方、无规模化语料与 round-trip 基线。**依赖 M2** |
| **一** | 阅读顺序重建（Reading Tree：分栏 / 留白 / 字号 / 基线 / 拓扑排序） | 🔶 | **✅/🔶** | ✅ `graph.py` 阅读边（FOLLOWS/PRECEDES）+ `analyzer.py` + `layout.py` `ColumnLayout` + `visual_tree_builder.py` 保序建树；🔶 分栏/阅读排序仍启发式、无评价集。**依赖 M2** |
| **二** | 段落语义重构（物理行→句子→段落，`A. B.`/`e.g.`/`Fig.` 误断防护） | 🔶 | **✅/🔶** | ✅ `sentence_detector.py`（V6.0）：缩写表 + 姓名首字母 + 敬称识别，`SentenceDetector` + `ParagraphReconstructor`；🔴 未接入主链路。**依赖 M2** |
| **三** | 结构与语义标记（Title/Caption/Formula/Citation/Code/Table/List/Footnote 精细化 Prompt） | ✅ | **✅** | ✅ `analyzer.py` 全通道 + `graph.py` `NodeType`（22 类）+ `prompt_manager.py` + `multi_channel_rewriter.py`（V6.0 按角色分通道） |
| **四** | 上下文构建器（前文/后文/标题/摘要/术语全局上下文） | 🔶 | **✅/🔶** | ✅ `planner.py` context window + `translation_runtime.py`（V6.1）上下文融合 + `planner_chain.py`（V6.0）；🔴 未在主链路生效 |
| **五** | 动态术语库与翻译记忆（实体别名→规范名，长文档术语一致） | 🔶 | **✅** | ✅ `memory.py` + `document_intelligence.py`（V6.2）+ `memory_layers.py`（V6.0，Style/Reasoning）+ `knowledge_graph.py`（V7.5，跨会话增量传播） |
| **六** | 约束布局求解（Cassowary/Kiwi 弹性推拉） | 🔶 | **✅/🔶** | ✅ `constraint_graph.py`（V6.0，四档优先级）+ `relayout_engine.py`（V6.0）+ `optimizer.py`；🔶 自研求解器未达 Kiwi、优先级未达六档。**依赖 M2** |
| **七** | 自适应排版引擎（译文长度→动态行高/段距、CJK/拉丁基线） | 🔶 | **🔶** | 🔶 `layout.py` `InlineLayout`（字符级字距/kerning）+ `ColumnLayout` 已实现；🔴 缺"按译文长度动态计算行高/段距"与基线对齐约束 |
| **八** | 空间碰撞检测与重排（Sweep-Line / R-Tree） | ✅ | **✅** | ✅ `collision_resolver.py`（R-Tree / PushDown）+ `layout.py` `CollisionEngine`（Sweep-Line） |
| **九** | 多目标渲染（PDF/SVG/HTML/Markdown/DOCX 统一渲染） | 🔶 | **✅/🔶** | ✅ `renderer.py`（PDF/HTML/SVG/DOCX 渲染器）+ `render_adapter.py`（html/text/pdf）+ `pdf_renderer.py`；🔶 PDF 为文本层/基础版，正式 PDF 仍靠 legacy 合并写回 |
| **十** | 视觉与翻译 QA（五维评分 0–100，<90 报警 + 差分快照） | 🔶 | **✅/🔶** | ✅ `evaluator.py`（translation/semantic/typography/layout/consistency 五维，阈值 80/75/70/80/75）+ `IssueGraph` + `DiagnosticReport`；`runtime_service` 已采集 `quality_scores`；🔴 无 <90 自动差分快照留存 |
| **十一** | AI Agent 自校验流水线（Parser/Layout/Translate/Typography 多 Agent） | 🔶 | **🔶** | 🔶 `review_agent.py`（V6.0，评审 Agent + 质量门）+ `repair.py`（自愈闭环）已实现；🔴 Parser/Layout/Typography Agent 未实现，legacy 仍单 LLM 调用 |

### 9.1 矩阵解读（v2.0 → v3.0 状态迁移总结）

- **🔴 → ✅ / 🔶✅**：阶段零（IR 建模）、阶段一（阅读顺序）、阶段二（段落重构）、阶段四（上下文）、阶段五（术语库）、阶段六（约束布局）、阶段九（多格式渲染）、阶段十（QA 评分）——**八个阶段从"缺组件"变为"组件就位"**；
- **仍然 🔴 的只有一件事的两种表现**：组件未接入主链路（阶段二/四/六）与正式 PDF 渲染未接管（阶段九）。它们的根因同源——**Phase M（M2 数据统一 + M3 流量切换）未闭环**。

---

## 十、核心痛点根因分析（v3.0 实证更新）

| 痛点 | v2.0 判断 | v3.0 实证 |
| :--- | :--- | :--- |
| **单句割裂、频繁换行** | 需阶段二段落语义重构 | ✅ `sentence_detector.py` 已实现（物理行→句子→段落，防 `e.g./Fig./A. B.` 误断）；🔴 **主链路未使用**，legacy 仍逐行切分翻译 |
| **术语漂移（LLM→大语言模型→大型语言模型）** | 需四层记忆 + 首次形态固化 | ✅ 四层记忆（`memory_layers.py`）+ 跨会话知识图（`knowledge_graph.py`）已实现；🔴 **未注入任何翻译 Prompt 模板**，术语漂移在真实输出中仍会发生 |
| **上下文断层（段落孤立翻译）** | 需全局+局部复合上下文 | ✅ planner context window + `translation_runtime` 上下文融合 + `planner_chain` 决策链已实现；🔴 **未在主链路生效** |
| **文本重叠（译文膨胀/收缩）** | 需约束布局求解 | ✅ V4 侧四件套（约束图/重排引擎/优化器/碰撞检测）齐备；🔴 **legacy 主链路仍是 `layout_graph.py` + `paragraph_layout.py` 贪心推挤** |
| **质量与回退不可观测** | 需 QA 闭环 + 快照 | ✅ `evaluator.py` 五维打分 + `runtime_service` 采集 `quality_scores` + `IssueGraph`/`DiagnosticReport` 已实现；🔴 **无 legacy↔V4 回归 diff 基线、无迁移回退率遥测** |


> **文本重叠补充调查（v3.0 追加，详见 `text_overlap_analysis_report.md` 附录 D）**：
> 用真实英文文献（arXiv 双栏论文）端到端复现，新增定位到**第 8 类独立根因**——`receive_layout`
> 角标判定以"整段历史最大字号"（`pstk[-1].size` 只增不减）为基准，双栏/布局合并使标题（13.63pt）
> 与正文（9.96pt）落入同一"伪段落"时，正文被误判为角标公式，**原文以原字体重绘、与译文重叠**。
> 已用"行内字号基准（`cur_line_size`）"修复并合入 `converter.py`（验证：译文页原文残留 842→3 字符，
> `tests/` 1376 passed 无回归）。此类根因的**根除**仍依赖阶段零 Document IR 的栏级/阅读顺序拆分。

> **根因收敛**：v1.0 的五个痛点，在 v3.0 的组件层**全部有解**；卡点高度收敛到同一个位置——**Phase M 迁移闭环未关**。v2.0 还分列"模块缺失"与"未接管"两类，v3.0 只剩"未接管"一类。

---

## 十一、新路线图：V8 阶段——从"组件完备"到"主链路接管"

### 11.1 已完成：V6.0 → V7.6 迭代历史（v2.0 之后）

| 版本 | 关键交付 | 验证 |
| :--- | :--- | :--- |
| V6.0 | IR / 四图关联 / 段落重构 / 决策链 / 四层记忆 / 多通道 / 约束重排 / 评审 Agent | `tests/v3/test_v6_design_rfc.py`、`test_v6.py` |
| V6.1 | DocumentRuntime（会话 + 断点）/ TranslationWorkflow | `test_v7_runtime_first.py` |
| V6.2 | Entity / Concept / Citation 知识图 + KnowledgeFuser | `test_phase2_p9_integration.py` 等 |
| V7.1 | OperatorGraph 算子 DAG（Runtime 去 Pipeline） | `test_v7_runtime_first.py` |
| V7.2 | RuntimeSnapshot（12 组件完整快照 / 真回滚） | 同套件 |
| V7.3 | RuntimeService 三端统一 + GUI 模块化 | `test_v7_2_runtime_service.py`、`tests/test_gui_modules.py` |
| V7.4 | OperatorResultCache（算子级缓存） | `test_v7_4_operator_cache.py` |
| V7.5 | KnowledgeGraph（跨会话增量传播） | `test_v7_5_knowledge_graph.py` |
| V7.6 | RemoteRuntime（REST 适配层） | `test_v7_6_remote_runtime.py` |

### 11.2 V8 阶段（迁移闭环，建议 6–10 周）

| 子阶段 | 目标 | 关键动作 |
| :--- | :--- | :--- |
| **V8.1 回归基线** | legacy ↔ V4 在同一份 PDF 上自动 diff（页数 / 文本内容 / bbox / 重叠率） | 在 `tests/v3` 增加 `MigrationDiffHarness`，产出可量化回归报告（回应 v2.0 差距 2） |
| **V8.2 灰度排程** | `use_v4_engine` 从布尔变为策略（按页 / 按文档类型 / 按用户） | 扩展 `feature_flags.py` 为规则引擎；回退事件写入 `TelemetryCollector`（回应差距 1、3） |
| **V8.3 IR 接管** | `ParseOperator` / `RuntimeFacade.load()` 产出 `DocumentIR`；legacy 输出先转 IR 再入下游 | 砍掉 `LTChar → obj_patch` 直通；`graph_registry` 迁移到 IR node_id 命名空间（M2 闭环） |
| **V8.4 主链路排版 gate** | 把 `relayout_engine + collision_resolver` 挂在 legacy 写回前做约束重排 | 保留 babeldoc 字体子集与正式 PDF 写回，仅替换"坐标写回"段（阶段六接管主链路） |
| **V8.5 记忆与段落注入** | 四层记忆 + `KnowledgeGraph` 实际注入 Prompt；`SentenceDetector` 产出段落级翻译单元 | `TranslationRuntime` 接管 `_execute_v4` 翻译阶段；legacy 主链路经 V8.3 转 IR 后共享同一条翻译链（阶段二/四/五接管） |

> **V8 的验证口径**：当 `use_v4_engine` 从布尔开关变成可观测的灰度策略、且默认路径经过 IR + 约束求解时，路线图才从"设计 RFC"变为"生产引擎"。V8.1 的回归基线与 V8.2 的灰度排程必须先于 V8.3–V8.5，否则接管没有安全网。

---

## 十二、综合评价与落地优先级（v3.0）

### 12.1 六维成熟度（v2.0 → v3.0）

| 维度 | v2.0 | v3.0 | 依据 |
| :--- | :---: | :---: | :--- |
| 架构完整性 | 6.0 | **8.5** | 统一 IR、四图 ID 关联、四层记忆、约束重排、文档级 Runtime、算子化执行、远程运行时全部落地 |
| 组件完成度 | 7.0 | **8.0** | 53 模块 + V4–V7 无头测试套件（`tests/v3/*`）全绿；`translation_runtime` / `optimizer` / PDF 正式渲染仍为占位级 |
| 主链路接管率 | 3.0 | **4.0** | GUI / Flask / MCP 三端已统一到 RuntimeService（M1 前 2/3）；CLI / `high_level` 仍旁路，`use_v4_engine=False` |
| 质量可观测性 | 5.0 | **7.0** | `evaluator.py` 五维打分 + `runtime_service` 诊断采集 + `IssueGraph` 已实现；无迁移回退率遥测、无回归基线 |
| 文档与工程 | 7.0 | **7.5** | V6/V7 迭代报告齐备、模块注释与版本号规范；缺规模化语料与 IR 序列化快照基线 |
| 运维可交付性 | 6.0 | **7.0** | 任务生命周期/事件流/暂停恢复/取消/远程调用齐备；无灰度维度与回退监控 |

**综合成熟度：5.7 → 7.0（+1.3，主要增量来自 V6.0–V7.6 的组件层）。**

### 12.2 优先级建议（P0–P4，已随 v3.0 状态重排）

| 优先级 | 事项 | 对应路线图阶段 |
| :---: | :--- | :--- |
| **P0** | V8.1 legacy↔V4 回归基线 + V8.2 灰度排程（迁移闭环，决定所有组件投入的可见性） | Phase M / M3 |
| **P1** | V8.3 IR 接管（M2 数据统一）+ V8.5 记忆/段落注入主链路 | 阶段零/二/四/五 |
| **P2** | V8.4 主链路约束重排 gate（legacy 写回前约束求解） | 阶段六/八 |
| **P3** | 六档约束优先级（+TYPOGRAPHY/READING/SEMANTIC）、统一双处 `ConstraintPriority`、Kiwi 求解器替换 | 阶段六 |
| **P4** | 100 份 PDF 语料 + IR 序列化快照基线、按译文长度动态行高/段距、Parser/Layout/Typography Agent、正式多格式 PDF 渲染 | 阶段零/七/九/十一 |

> **与 v2.0 的优先级对比**：v2.0 的 P0 是"IR 序列化 + 阅读树 + 回归基线"，重心在补组件；v3.0 把 P0 收敛为**迁移闭环（回归基线 + 灰度）**，P1 才是组件接入——因为组件已齐，接入必须先有安全网。

---

## 结论

pdf2zh-next 的十二阶段路线图在 v2.0 之后经历了 V6.0 → V7.6 六轮快速迭代。**架构底座（Document IR、四图关联、四层记忆、约束重排、文档级 Runtime、算子化执行、质量闭环、远程运行时）已从"规划"变为"可用代码 + 无头测试"**，`pdf2zh/v3/` 从 34 个模块扩展到 53 个，v2.0 报告列出的差距在组件层逐条关闭。

系统当前的真实状态是**"组件领先于迁移"**：V4 侧能力完备，但 `use_v4_engine=False` 使主链路用户（GUI 默认执行、CLI、`high_level` 公共 API）与这些能力完全隔离。`DocumentSession`、`OperatorGraph`、`KnowledgeGraph`、`relayout_engine` 都是"V4 侧的好代码"，对真实翻译任务零贡献。

**下一步的最高杠杆不是再写组件，而是关闭 Phase M**：

1. **先建安全网**（V8.1 回归基线 + V8.2 灰度排程）——让"接管了会不会更差"成为可量化问题；
2. **再用 IR 统一数据**（V8.3）——砍掉 legacy 的 `LTChar → obj_patch` 直通双轨；
3. **最后接管主链路**（V8.4 约束重排 gate + V8.5 记忆/段落注入）——让阶段二/四/五/六的能力对真实用户生效。

当 `use_v4_engine` 从布尔开关变成可观测的灰度策略、且默认路径经过 IR + 约束求解时，这份路线图才真正从"设计 RFC"变为"生产引擎"。**组件层的"纸面能力"到此为止，接下来的每一行代码都应当服务于迁移闭环。**

---

*报告 v3.0 完。历史版本：`doc/_archive/pdf2zh_next_roadmap_analysis.v1.md`（功能模块清单视角）、`doc/_archive/pdf2zh_next_roadmap_analysis.v2.md`（运行时迁移视角）；本文档（v3.0）为"迁移闭环缺失"视角。*







