# 《下一代 PDF 翻译引擎（pdf2zh-next）重构与工程路线图》架构迁移评审

> **文件版本：** v2.0（Architecture Transition Review）
> **日期：** 2026-07-31
> **前置版本：** v1.0（落地差距分析报告，存档于 `doc/_archive/pdf2zh_next_roadmap_analysis.v1.md`）
> **评审对象：** 以《pdf2zh-next 重构与工程路线图》（十二阶段 + 核心设计哲学）为战略基准，以本仓库（PDFMathTranslate_FORKED）当前实现为实证对象。
> **本版核心变化：** 视角从 **“功能模块清单（Module Checklist）”** 升级为 **“运行时迁移（Runtime Migration）”**。v1.0 回答的是“哪些模块缺、哪些已建”；v2.0 回答的是“**两套 Runtime 如何并存、如何把 Legacy 一步步杀死、IR 与知识体系如何收敛为唯一事实来源**”。

---

## 摘要 / TL;DR

1. **V4 已不是 Feature Development，而是 Runtime Migration。** v1.0 报告实际已隐含此事实（`use_v4_engine=False`），但未点破其架构后果：**本仓库当前存在两套完整 Runtime 并行运行**。它们是两个世界——生命周期、数据结构、Scheduler、错误恢复、Cache 全部不同。**双 Runtime 长期共存**是大型软件迁移（Visual Studio、LLVM、Chrome、Firefox Quantum）中最危险的阶段，风险等级高于“缺一个模块”。

2. **新增 Phase M（Migration Runtime）作为横切架构层**，专门负责把 Legacy Runtime 渐进杀死：`Legacy Runtime → Adapter → V4 Runtime → Compatibility Layer → Progressive Cutover`。**Phase M 应排在 Constraint Layout 之前**——排版再精美，若运行时分裂，系统依旧无法长期演进。

3. **Document IR 需要从 Rendering Tree 升级为 Semantic IR。** `v3/visual_tree.py` 的 `VisualTree / Page / Paragraph / Line / TextRun` 本质是**渲染树**（其注释自述受 Blink LayoutTree / Flutter RenderObject / Typst FrameTree 启发），它“看到”的是三个并列 Paragraph，却不知道其中一个是 Figure 3 的 Caption、另一个是 Reference。真正的 IR 应具备 `Document → Section → SemanticBlock → VisualBlock → TextRun` 层级，且**一个节点同时携带 Semantic / Reading / Translation / Rendering 多个 Role**。

4. **Graph 应从“一张大图”演进为“四张关联图”。** Document Graph（结构）、Semantic Graph（语义）、Layout Graph（空间约束）、Execution Graph（执行状态）通过**统一的 node_id 命名空间**关联，而不是把一切塞进一个日益难维护的巨型图。

5. **Planner 应从“Prompt 规划”升级为 TranslationPlan。** 规划链为：`Node → Language Detection → Domain Detection → Confidence → Translator Route → Prompt Template → Glossary → Memory → Chunk → TranslationPlan`。**Translator 只负责 `Execute(plan)`，而不是自己决定怎么翻。**

6. **Memory 应从“术语库”升级为四层记忆体系。** Document Memory（术语映射）、Entity Memory（图/表/算法编号）、Style Memory（语体/时态/风格）、Reasoning Memory（领域/方法/主题）。缺失后两层，LLM 必然出现“第一页 Large Language Model → 第二页 大语言模型 → 第三页 LLM → 第四页 大型语言模型”的术语漂移。

7. **Constraint 应从 Constraint Solver 升级为 Document Constraint Engine。** 约束不再只是布局，而是 `Hard / Soft / Preferred / Typography / Reading / Semantic` 多级文档级约束（如“Caption 必须紧跟 Figure”是 Hard，“参考文献不能断页”是 Semantic）。

8. **最大的架构缺口是 Document Intelligence Runtime。** 现有 `v3/runtime.py` 是 **Graph 的事务层**（Transaction / Version / Snapshot），不是 **Document 的生命周期层**。未来 Agent 协同、自愈、增量翻译、断点恢复、流式处理全部依赖 `DocumentRuntime → Document Session → Knowledge Center → Planner → Execution Graph → Constraint Engine → Telemetry → Repair → Evaluator` 的会话生命周期。

9. **建议的演进路线从“十二阶段线性推进”改为 V5 → V6 三阶段：** ① **Runtime Consolidation（运行时收敛）**——统一双 Runtime、建立唯一 `DocumentRuntime` 与 `DocumentIR` 唯一事实来源；② **Document Intelligence（文档智能）**——多图解耦、四层 Memory、TranslationPlan；③ **Autonomous Document System（自治文档系统）**——Planner/Translator/Reviewer/Repairer 多 Agent 协作、Telemetry 驱动的自动诊断与局部重译重排。

10. **v1.0 的工程实证保留并结转为“已完成项”：** 并行 `bad xref → 空白 PDF` 与 `thread=0` 线程池崩溃已从根因修复，19 页真实 PDF 并行翻译 `updated stream 80/80`、无 bad xref、merge 成功（详见第十章）。这意味着 **Phase M 的“出口统一（M1）”前置条件已经具备**——主链路现在是“可稳定交付、不产空文件”的工程。

---

## 一、视角转变：从“功能模块清单”到“运行时迁移”

### 1.1 核心事实：V4 已进入 Runtime Migration 阶段

v1.0 报告被评审为“已经不再是重构计划，而更接近 Architecture Transition Review”，这个判断是正确的，但当时没有把它的架构含义讲透：

| 维度 | 模块开发（Feature Development） | 运行时迁移（Runtime Migration） |
| :--- | :--- | :--- |
| 问题 | 某个能力怎么做（Parser？Graph？Renderer？） | 怎么把 Legacy 一步一步杀死 |
| 主线 | 功能清单：Parser → Graph → Analyzer → Planner… | 迁移架构：Legacy → Adapter → V4 → 兼容层 → Cutover |
| 主要风险 | 功能未实现 | **双 Runtime 长期共存**、行为漂移、状态/缓存/错误处理分裂 |
| 成功判据 | 模块完成 | **唯一的 DocumentRuntime + 唯一的 DocumentIR** |

仓库已经跨过了“怎么做模块”的阶段——`pdf2zh/v3/` 现有 **34 个模块**（V4 基础设施），它们不是“规划”，而是“已存在但未接管主链路的运行时”。真正的问题从“堆模块”变成了“**把 Legacy 逐步迁移到 V4 Runtime 上**”。

### 1.2 两套 Runtime 并存的现状

```
Legacy Runtime（当前默认路径）
------------------------------
receive_layout()  →  worker()  →  layout  →  PDF operator
数据：LTChar/LTLine（pdfminer 物理级）
调度：ProcessPoolExecutor / ThreadPoolExecutor
恢复：失败回退串行（fallback serial）
缓存：translation_cache（哈希，key=原文）

V4 Runtime（注入完成，use_v4_engine=False）
------------------------------
Parser → Normalizer → Graph → Planner → Memory → Scheduler → Runtime → ConstraintSolver → VisualTree → PDFRenderer
数据：DocumentGraph / VisualTree / DocumentMemory / ExecutionGraph
调度：v3/scheduler.py + workflow_engine.py
恢复：runtime_supervisor.py + repair.py（自愈/重排）
缓存：GraphVersion / GraphSnapshot / DocumentMemory（版本化）
```

### 1.3 双 Runtime 共存的风险矩阵

| 维度 | Legacy Runtime | V4 Runtime | 共存风险 |
| :--- | :--- | :--- | :--- |
| **生命周期** | 单次 `translate_stream()` 调用 | Graph 事务 / 版本 / 快照 | 迁移遗漏：V4 侧的中间态无法映射回 legacy 的“一步到位”流程 |
| **数据结构** | LTChar / obj_patch（PDF operator） | DocumentGraph / VisualTree | 同一份源文档被两套模型解释，语义漂移 |
| **Scheduler** | 进程/线程池，按 chunk 切页 | workflow_engine 图驱动 | 并行策略不一致 → “V4 并行成功、legacy 并行失败”类双份维护 |
| **错误恢复** | fallback serial + catch-skip | Supervisor + Repair | 同一错误两条处理路径，行为分裂 |
| **Cache** | 哈希翻译缓存 | 版本化 Graph 快照 | 一份内容两份缓存，一致性无保证 |

> **结论：** 双 Runtime 共存本身不是问题，**无管理的长期共存**才是。这正是 Phase M 存在的理由。

---

## 二、Phase M：运行时迁移层（本报告最重要的新增架构）

### 2.1 为什么需要 Phase M

Strangulation（绞杀者模式）只解决了“入口分流”（`use_v4_engine` 开关），**没有解决状态、错误恢复、缓存、进度的一致性**。当 Legacy 与 V4 各管一段时，任何跨 Runtime 的数据流都会在三处暴露问题：

1. **适配缺失**：legacy 输出的 `obj_patch` 无法被 V4 的 Constraint Engine 直接消费，反之亦然；
2. **状态分裂**：同一文档在 Legacy 侧是一次调用，在 V4 侧是 Graph 版本序列，二者没有统一的会话对象；
3. **行为漂移**：用户在不同入口（CLI/GUI/MCP）得到不同行为，因为底层走的 Runtime 不同。

Phase M 是**专门负责“把 Legacy Runtime 渐进杀死”的横切架构层**，它不实现任何业务功能（翻译/排版都不是它的事），只管理迁移本身。

### 2.2 Phase M 分层架构

```
    Legacy Runtime
          │
          ▼
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

### 2.3 Phase M 的三个子阶段

| 子阶段 | 目标 | 关键动作 | 仓库现状 |
| :--- | :--- | :--- | :--- |
| **M1 出口统一** | 所有入口（CLI/GUI/MCP/API）必须经过**同一个** `RuntimeService → DocumentRuntime`，不允许旁路 | 移除 `gui.py / backend.py / mcp_server.py` 中绕过 RuntimeService 的直调 | 🔶 部分：`services/runtime_service.py` 已统一三端入口，但 `high_level.translate_stream` 仍在内部直走 legacy |
| **M2 数据统一** | 建立 `DocumentIR` 唯一事实来源；legacy 的 `LTChar` 输出**先转 IR 再入下游**，砍掉“解析直通渲染”的双轨 | `receive_layout` 出口统一为 IR 序列化结构；下游（字体/排版/碰撞/写回）只消费 IR | 🔴 未开始：legacy 仍 `LTChar → obj_patch` 直达 |
| **M3 流量切换** | `use_v4_engine` 从 `False` 逐步走向默认；失败自动回退 legacy，并用 M1 的统一会话记录回退率 | 灰度开关：按页/按文档类型/按用户；回退事件写入 Telemetry | 🔴 未开始：`ServiceConfig.use_v4_engine=False` 全量关闭，无灰度维度 |

### 2.4 仓库现状与差距

- **已有底座：** `pdf2zh/v3/legacy_adapter.py`（`LegacyTranslatorAdapter` 包装 24 个翻译引擎、`V4PipelineRunner`、`TranslateConverterStrangler`）、`services/runtime_service.py`（任务级生命周期 + 事件流）、`gui/`（模块化组件）。
- **差距 1：没有“迁移排程”。** 没有“哪些页/哪些文档类型走 V4、哪些走 legacy”的规则引擎，开关是全有全无的。
- **差距 2：没有“兼容性回归基线”。** 对同一份 PDF，legacy 与 V4 输出没有自动 diff（页数、文本内容、bbox、重叠率），无法量化“接管了会不会更差”。
- **差距 3：M1 未闭环。** `runtime_service.py` 里 `_execute_legacy` 直接调用 `translate_stream`，V4 路径是平行存在而非“同一运行时上的两个引擎”。

> **Phase M 的排期理由：** 路线图把 Constraint Layout（阶段六）视为排版关键升级，但它必须在**统一运行时之上**才有意义——否则约束求解器只在 V4 侧生效，legacy 主链路用户永远看不到。**先 Phase M 收运行时，再谈排版引擎。**

---

## 三、Document IR 升级：从 Rendering Tree 到 Semantic IR

### 3.1 VisualTree 的定位：它是 Rendering Tree，不是 Semantic IR

`pdf2zh/v3/visual_tree.py` 的模块注释自我定位为“A layout-independent display tree between DocumentGraph and Renderer”，灵感来自 **Blink 的 LayoutTree、Flutter 的 RenderObject、Typst 的 FrameTree**——这三位都是**渲染树**。其节点类型 `Page / Column / Paragraph / Line / TextRun / Image / Table / Formula` 也是渲染层级。

对于一篇论文：

```
Figure 3
   ↓
Caption
   ↓
Paragraph
```

VisualTree 看到的是三个并列的 `Paragraph`（或 `Paragraph + Image + Paragraph`），**它不知道**：
- 哪个是 Figure（`Image` 节点没有语义角色绑定）；
- 哪个是 Caption（与 Figure 的 `caption_of` 关系在渲染树中不存在）；
- 哪个是 Reference / Citation（文本内容无法表达引用关系）。

### 3.2 真正的 Document IR：层级 + 多 Role 节点

IR 应显式建模语义与渲染的分离，并且**一个节点同时携带多个 Role**：

```python
# 目标态（示意，非当前实现）
class SemanticRole(Enum):
    BODY_TEXT, HEADING, CAPTION, FIGURE, TABLE,
    FORMULA, REFERENCE, CITATION, FOOTNOTE, CODE = range(10)

class ReadingRole(Enum):
    # 阅读顺序维度
    COLUMN_2, FOLLOWS_FIGURE_3, SIDEBAR, MAIN_FLOW = range(4)

class TranslationRole(Enum):
    NEED_CONTEXT, KEEP_TERM, KEEP_FORMULA, KEEP_NUMBER = range(4)

class RenderingRole(Enum):
    BLOCK, INLINE, FLOAT, ANCHORED = range(4)

@dataclass
class IRNode:
    id: str                       # 全局唯一，被四张图共用
    parent_id: str | None
    semantic: SemanticRole        # 它“是什么”
    reading: ReadingRole          # 它“在哪条阅读流里”
    translation: TranslationRole  # 翻译器“怎么对待它”
    rendering: RenderingRole      # 渲染器“怎么摆放它”
    bbox: BoundingBox
    text: str
    children: list[str]           # 引用子节点 ID（非内嵌对象）

# 层级：Document → Section → SemanticBlock → VisualBlock → TextRun
```

一个 `Paragraph` 节点可以同时是：`Semantic=BODY_TEXT`、`Reading=COLUMN_2`、`Translation=NEED_CONTEXT`、`Rendering=BLOCK`。四个维度**彼此独立、可叠加**，这正是语义分析、阅读排序、翻译规划、约束排版四类下游各自只消费自己那个 Role 的接口。

### 3.3 现状与差距

| 维度 | 当前实现 | 差距 |
| :--- | :--- | :--- |
| IR 模型 | `v3/visual_tree.py`（渲染树）+ `v3/graph.py` `DocumentNode`（`node_type` + bbox + text） | 两个模型并存，均未成为唯一事实来源 |
| 语义 Role | `graph.py NodeType` 枚举很完整（HEADING/CAPTION/REFERENCE/CITATION…） | 只有 `node_type` **一个**维度，没有 Reading/Translation/Rendering 多 Role 视图 |
| 序列化 | 无 IR JSON / Protobuf 序列化 | 缺 `to_json / from_json`，跨进程/跨 Runtime 传递只能靠 PDF operator |
| 100 份测试集 | `tests/v3/*` 有零散 PDF 用例 | 无规模化语料与 IR 序列化快照基线 |

> **影响：** 只要 IR 是“渲染树 + 节点类型”而非“多 Role 语义 IR”，阶段三的精细化 Prompt（对 Caption 说“保留编号”、对 Formula 说“不要拆”）就只能靠 `node_type` 猜，一旦遇到“Caption 里含公式、正文里含引用”的复合节点就退化为统一模板。

---

## 四、多图体系：从单一 DocumentGraph 到四张关联图

### 4.1 四张图，各司其职

把“一个巨大的 DocumentGraph”拆成四张**通过 ID 关联**的图，是 V4 演进的必然方向——单图在节点类型、关系类型不断膨胀后（`graph.py` 的 `EdgeType` 已含 CONTAINS/FOLLOWS/REFERENCE/CAPTION_OF/MUST_ABOVE/CANNOT_OVERLAP/SAME_BASELINE/SAME_SECTION/DEPENDS_ON 十类），遍历、缓存、事务、失效传播都会失去边界：

| 图 | 承载维度 | 典型关系 | 消费者 |
| :--- | :--- | :--- | :--- |
| **Document Graph** | 结构 | `Document → Section → Paragraph`（Contain / Follows） | 大纲、章节跳转、页眉页脚剔除 |
| **Semantic Graph** | 语义 | `Equation → Reference → Definition`（Reference / Definition / CaptionOf） | 术语抽取、上下文构建、精细化 Prompt |
| **Layout Graph** | 空间 | `Caption MUST_FOLLOW Figure`、`A CANNOT_OVERLAP B`、`Anchor` | 约束求解、碰撞检测、重排 |
| **Execution Graph** | 执行 | `Node → State`（DEPENDS_ON / 状态迁移 NEW→PLANNED→TRANSLATED→LAYOUTED→RENDERED→VERIFIED） | 调度、增量重译、断点恢复 |

### 4.2 通过 ID 关联，而不是一张大图

四张图共享**同一个 node_id 命名空间**（即第三章 `IRNode.id`）。一个节点在四张图里各有一条记录：

```
node "p42"  ──► DocumentGraph: Paragraph(contains in Section 2)
          ──► SemanticGraph:  BODY_TEXT, defines term "Transformer"
          ──► LayoutGraph:    CANNOT_OVERLAP with Figure 3
          ──► ExecutionGraph: state=TRANSLATED, depends_on=[p41]
```

好处：语义分析只遍历 Semantic Graph，约束求解只遍历 Layout Graph，互不污染；任何一图的失效（如布局冲突）可以只标脏 Layout Graph 中对应节点，而不会级联失效整个文档。

### 4.3 仓库现状与差距

- **Document Graph：✅ 已成型。** `v3/graph.py` 的 `DocumentGraph` + `DocumentNode` + 10 类 `EdgeType` + `ConstraintPriority(HARD/SOFT)`，DAG + 拓扑排序。
- **Execution Graph：🔶 雏形。** `v3/execution_graph.py` 已有 `ExecutionNode`（8 态状态机 + dirty 级联），与 `v3/workflow_engine.py`、`v3/scheduler.py` 配套。
- **Semantic Graph：🔶 部分。** `v3/document_intelligence.py` 已实现 `EntityGraph / ConceptGraph / CitationGraph`（知识图），`v3/causal_graph.py` 提供因果边——但它们是**实体级知识图**，尚未与文档节点级 `SemanticGraph` 统一。
- **Layout Graph：🔶 部分。** 约束散落在 `v3/constraint_graph.py` 与 `graph.py` 的 `EdgeType`（MUST_ABOVE / CANNOT_OVERLAP / SAME_BASELINE）里，没有独立的 LayoutGraph 边界。
- **最大差距：四图未通过统一 ID 关联。** `DocumentNode.id`、`ExecutionNode.node_id`、`EntityNode.id` 各用各的键；没有“一个节点在四图间穿梭”的查询接口。

---

## 五、Planner 升级：从“Prompt 规划”到 TranslationPlan

### 5.1 现状：Planner 已优于“统一模板”，但仍以 NodeType 驱动

`pdf2zh/v3/planner.py` 的 `TranslationPlanner` 已经能按 `NodeType` 生成差异化计划（`TranslationPlan` 含 prompt template / context window / glossary / temperature / chunk strategy），`PROMPT_TEMPLATES` 覆盖 PARAGRAPH / HEADING / CAPTION / ABSTRACT 等角色——这比 legacy 的统一模板进步巨大。

但它的规划输入**主要是 `NodeType`**，缺少“这篇文档是什么领域、这个节点可信度多高、该走哪个翻译器、要用哪层记忆”的决策链。

### 5.2 升级方向：从“选模板”到“生成翻译计划”

新一代 Planner 应是一条**显式决策链**，产出可审计、可复用的 `TranslationPlan`：

```
Node
  │
  ├─ Language Detection   ──► 源/目标语言，是否跳过（图内文本不翻）
  ├─ Domain Detection     ──► 学科域（Diffusion / NLP / Optimization）→ 注入 Reasoning Memory
  ├─ Confidence           ──► 版面置信度 → 决定人工介入/回退 legacy
  ├─ Translator Route     ──► 按节点类型 × 领域 × 成本选模型
  ├─ Prompt Template      ──► 按 SemanticRole 选模板（Caption 保编号 / Formula 不拆）
  ├─ Glossary             ──► 注入 Document + Entity 记忆
  ├─ Memory               ──► 继承 Style + Reasoning 记忆
  └─ Chunk                ──► 段落级/句级切分策略
          │
          ▼
   TranslationPlan（唯一输出）
```

```yaml
# TranslationPlan 示意
translator: "gpt-5"
temperature: 0.0
domain: ["diffusion", "optimization"]
context:
  preceding: ["p41", "p40"]
  title: "..."
memory:
  entity: ["Transformer=Transformer(保持)", "LLM=大语言模型"]
  style: "academic-ieee"
  reasoning: "本篇属 Diffusion + Optimization 交叉领域"
glossary:
  - "attention mechanism -> 注意力机制"
chunk: "sentence"
quality: "high"
constraints:
  keep_numbers: true
  keep_formulas: true
```

### 5.3 Translator 只负责 `Execute(plan)`

架构语义：**Planner 决定一切策略，Translator 只执行计划**。Translator 不再自己“决定怎么翻”，它拿到 plan 后：取 `translator` 路由、注入 `context`/`glossary`/`memory`、按 `chunk` 切分、按 `temperature` 调用模型、按 `constraints` 校验输出。这样做的好处：

1. **策略可审计**：一份 `TranslationPlan` 可序列化、可回放、可 diff——出问题时定位到“是规划错了还是执行错了”；
2. **策略可复用**：同一 plan 可切换 translator 重试（route 升级），不必重做语义分析；
3. **策略可学习**：`Reviewer`（见第十一章）发现术语漂移时，反馈修正的是 **plan 的 glossary/memory 字段**，而不是去改 Prompt 模板字符串。

> **仓库差距：** `planner.py` 已产出 `TranslationPlan` 数据结构（✅），但缺少 Language/Domain/Confidence 检测环节、缺少 Style/Reasoning 记忆输入、且未接入 V4 主链路（use_v4_engine=False，计划从未被执行）。

---

## 六、Memory 升级：从“术语库”到四层记忆体系

### 6.1 Memory ≈ 术语库？不，是四层记忆

v1.0 报告把“Memory 缺失”等同于“术语库缺失”，这在架构上是不完整的。未来 Memory 至少分四层，每层回答一个不同的问题：

| 层 | 记录什么 | 示例 | 消费时机 |
| :--- | :--- | :--- | :--- |
| **Document Memory** | 术语/别名的规范映射 | `Transformer → Transformer（保持）`、`LLM → 大语言模型` | 全局 Prompt 注入 |
| **Entity Memory** | 编号实体 | `Figure 1`、`Algorithm 3`、`Table 2` 的编号与指代关系 | 保编号指令、跨段指代消解 |
| **Style Memory** | 语体/时态/风格 | `Academic Tone`、`Passive Voice`、`IEEE Style` | 翻译指令约束、术语一致性 |
| **Reasoning Memory** | 领域/方法/主题 | `本文属 Diffusion + Machine Learning + Optimization` | 上下文构建、专业语境继承 |

四层之间是**继承关系**：Reasoning 决定“这篇论文讲什么”→ Style 决定“用什么语体”→ Document/Entity 决定“哪些词固定怎么翻”。后续所有 Prompt **直接继承**这几层，而不是每次从零构造。

### 6.2 术语漂移案例（为什么必须四层）

没有四层记忆的典型长文档后果：

```
第 1 页： Large Language Model（LLM）
第 2 页： 大语言模型
第 3 页： LLM
第 4 页： 大型语言模型
```

原因不是“模型不够好”，而是**每次调用都没有继承**：没有 Document Memory 固化 `LLM=大语言模型`，没有 Reasoning Memory 告诉模型“这是一篇 NLP 论文”，没有 Style Memory 约束“保持术语首次出现时的形态”。

### 6.3 仓库现状与差距

| 层 | 当前实现 | 差距 |
| :--- | :--- | :--- |
| Document Memory | ✅ `v3/memory.py`：`DocumentMemory`（`EntityEntry` / `GlossaryEntry` / `AbbreviationEntry`，别名→规范名映射已内置） | 未注入实际翻译 Prompt；`translation_cache.py`（legacy）仍是哈希缓存 |
| Entity Memory | ✅ `v3/document_intelligence.py`：`EntityGraph`（实体、别名、`occurrence_count`、`first_occurrence_page`） | 未与 Prompt 的“保编号”指令打通 |
| Style Memory | ❌ 缺失 | `DocumentMemory.language_style` 仅一个字符串字段，无结构化风格规则 |
| Reasoning Memory | ❌ 缺失 | `_topics` 列表仅存关键词，无领域推断与继承链 |

> **结论：** 第一、二层（术语/实体）已有扎实雏形，第三、四层（风格/推理）基本空白；而“跨页术语一致”恰恰主要依赖后两层。这是“技术文档严密感”无法再上一个台阶的根因之一。

---

## 七、Constraint 升级：从 Constraint Solver 到 Document Constraint Engine

### 7.1 Constraint 已经不是 Layout，而是 Document Constraint

v1.0 把阶段六的目标简化为“Cassowary/Kiwi 求解器”，但仅解决“文本框弹性推拉”只是**布局**维度。评审意见的正确方向是：**约束必须升级为文档级（Document Constraint）多级体系**：

| 约束类型 | 语义 | 示例 | 违反后果 |
| :--- | :--- | :--- | :--- |
| **Hard** | 不可违反 | `Caption 必须紧跟 Figure 3`、`文字不得超出页面边界` | 输出作废，必须重排 |
| **Soft** | 可加权违反 | `段落尽量保持原高度` | 加权扣分，允许以空间换质量 |
| **Preferred** | 偏好，能实现最好 | `段落尽量不跨页` | 无惩罚，仅作为求解目标 |
| **Typography** | 排印底线 | `行高 ≥ 1.5 × 字号`、`CJK 与拉丁字体基线对齐` | 排版分扣减 |
| **Reading** | 阅读流约束 | `同一段落的两行不得被分栏割裂`、`脚注跟随正文` | 阅读顺序损坏 |
| **Semantic** | 语义约束 | `参考文献条目不能断页`、`公式不拆行` | 语义完整度扣分 |

关键点：**约束的优先级体系（Hard/Soft/Preferred）与语义绑定**，让求解器在“空间不足”时能区分“绝不能压的”（Caption 与 Figure 的相对关系）和“可以让步的”（段落原高度）。这才是出版级排版与“贪心推挤”的本质区别。

### 7.2 仓库现状

| 能力 | 当前实现 | 状态 |
| :--- | :--- | :--- |
| 约束图 | `v3/constraint_graph.py`：`ConstraintGraph` + `ConstraintSolver`（自研简化版） | 🔶 自研未达 Cassowary 级 |
| 约束优先级 | `graph.py ConstraintPriority`：`HARD / SOFT` 两级 | 🔶 缺少 Preferred/Typography/Reading/Semantic |
| 空间约束边 | `EdgeType`：`MUST_ABOVE / CANNOT_OVERLAP / SAME_BASELINE / SAME_SECTION` | ✅ 语义化空间边已定义 |
| 碰撞检测 | `collision_resolver.py`：R-Tree / PushDown，`tests/test_collision_resolver.py` 通过 | ✅ 仓库完成度最高组件 |
| legacy 排版 | `layout_graph.py` + `paragraph_layout.py`（贪心推挤） | 🔴 默认路径仍是贪心推挤 |

### 7.3 升级路径

1. **求解引擎**：以 `collision_resolver.py` 为底座，将 `ConstraintSolver` 替换为 Kiwi（Cassowary 的 C 实现）驱动，支持 `strength` 权重链；
2. **约束分级**：`ConstraintPriority` 从两级扩展到 `HARD / SOFT / PREFERRED / TYPOGRAPHY / READING / SEMANTIC` 六档；
3. **语义绑定**：把第三章 `IRNode.semantic` 与第七章约束关联——`semantic=CAPTION` 的节点自动生成 `MUST_FOLLOW(figure)` 硬约束，`semantic=BIBLIOGRAPHY` 自动生成 `CANNOT_BREAK_PAGE` 语义约束；
4. **统一运行时**：Constraint Engine 必须挂在 Phase M 统一后的 `DocumentRuntime` 上（而不是只服务 V4），否则 legacy 用户永远看不到效果。

---

## 八、最大的缺口：Document Intelligence Runtime

### 8.1 现有 Runtime 是“Graph 的事务层”，不是“Document 的生命周期层”

`pdf2zh/v3/runtime.py` 的 `GraphRuntime` 提供了非常扎实的事务能力：`GraphTransaction`（原子提交/回滚）、`GraphVersion`（修订历史）、`GraphSnapshot`（快照序列化）、`GraphObserver`（脏标记与变更通知）。但它的作用域是 **Graph 节点变更**，而不是**一篇文档从“被载入”到“被交付”的完整生命周期**。

路线图要支撑的下一阶段能力——Agent 协同、自愈、增量翻译、断点恢复、流式处理——全部要求“一次会话贯穿文档的一生”：

```
 DocumentRuntime（文档生命周期所有者）
      │
      ├─ Document Session   （一次翻译任务的会话：状态、上下文、断点）
      ├─ Knowledge Center   （Document / Entity / Style / Reasoning 四层记忆）
      ├─ Planner            （生成 TranslationPlan）
      ├─ Execution Graph    （节点级状态机与增量调度）
      ├─ Constraint Engine  （六档 Document Constraint）
      ├─ Telemetry          （指标：术语漂移率、重叠率、回退率）
      ├─ Repair             （局部重译 / 局部重排）
      └─ Evaluator          （评分 + 触发 Review/Repair）
```

- **Document Session**：一篇 PDF 的一次翻译 = 一个 Session。Session 持有 `DocumentIR`、四层 Memory、ExecutionGraph 状态、断点信息——这是“卡死可续传”“增量翻译只翻改动页”“流式输出先出已翻译页”的前提。
- **Knowledge Center**：跨 Session 可持久化、可检索的知识库（术语库 + 风格 + 领域），受控学习（见第十一章第三阶段）。
- **Repair**：由 `Evaluator`/`Reviewer` 触发，对局部节点重新规划（改 plan 的 glossary/memory 后重译），或对局部版面重新求解（改约束后重排）——而不是整篇重跑。

### 8.2 仓库现状与差距

| 能力 | 当前实现 | 差距 |
| :--- | :--- | :--- |
| Graph 事务/版本/快照 | ✅ `v3/runtime.py`（GraphRuntime） | 作用域仅限 Graph，无文档级 Session |
| 运行监控 | ✅ `v3/runtime_kernel.py`、`v3/runtime_supervisor.py`、`v3/tracing.py` | 未与文档生命周期绑定 |
| 工作流 | ✅ `v3/workflow_engine.py`、`v3/scheduler.py`、`v3/service.py`（ServiceRegistry DI） | 是“管线编排”，不是“会话生命周期” |
| 会话/断点 | ❌ 无 `DocumentSession` 概念 | GUI/服务层用 `task_id` 做任务级状态，无文档级会话 |
| Knowledge Center | ❌ 无持久化知识库 | `DocumentMemory` 是进程内内存对象，无法跨任务继承 |

### 8.3 为什么这是 V6 的入场券

> **“把 V4 基础设施做扎实”与“做成下一代文档平台”的分水岭，就是有没有一个管理 Document 生命周期的 Runtime。** 现有模块集合是一台“流水线”（数据流过各工位）；`DocumentRuntime` 是一个“车间主任”（知道每件工件在哪、下一步该做什么、出问题了怎么返工）。**流水线只能批量处理，车间才能 Agent 协同、自愈、断点续传、增量翻译。**

---

## 九、十二阶段路线图 × 当前实现 对照矩阵（v2.0 迁移视角）

> 状态图例：✅ 已覆盖 · 🔶 部分覆盖 · 🔴 缺失/未接管。新增“迁移依赖”列说明该阶段完成需要依赖 Phase M 中的哪个子阶段。

| 阶段 | 路线图要求（基准） | 当前实现 | 落地状态 | 迁移依赖 |
| :--- | :--- | :--- | :---: | :--- |
| **零** | 统一 Document IR（`Document/Page/Block/Line/Span`），PDF→IR 单向提取，可序列化为 JSON，100 份 PDF 测试集 | `v3/visual_tree.py`（Rendering Tree）、`v3/graph.py`（DocumentNode）；legacy 无 IR（LTChar 直通） | 🔶 | **M2**（数据统一是 IR 接管的前提） |
| **一** | Reading Order Graph（分栏/留白/字号/基线/邻近距离 → 拓扑排序阅读树） | `v3/graph.py` 有 DAG+拓扑；legacy 仍按 `y0/x0` 贪心聚行 | 🔶 | M2 |
| **二** | 段落语义重构（句检测：`A. B.`/`e.g.`/`Fig.`） | 无句检测器；legacy 按物理行翻译 | 🔴 | M2 |
| **三** | 结构与语义标记（Title/Caption/Formula/Ref/Table/List/Footnote → 精细化 Prompt） | `v3/analyzer.py` `SemanticAnalyzer`；`v3/graph.py NodeType` 枚举完整 | 🔶 | —（V4 内部即可完成） |
| **四** | Context Builder（前文+当前+后文+标题+摘要+术语表 → 复合上下文） | `v3/planner.py` 的 `TranslationPlan.context` 已定义窗口；未接入主链路 | 🔶 | M3 |
| **五** | 术语数据库 + 翻译记忆（别名→规范名，全局一致） | `v3/memory.py` DocumentMemory + `document_intelligence.py` EntityGraph；未注入 Prompt | 🔶 | M3 |
| **六** | 约束布局求解（Cassowary/Kiwi 弹性推拉） | `v3/constraint_graph.py`（自研简化）；legacy 贪心推挤 | 🔶 | **Phase M 前置**（见 2.4） |
| **七** | 自适应排版（字符宽度/动态行高段距） | `text_metrics.py`（fontTools 度量）、`paragraph_style.py`、`overflow_policy.py` | 🔶 | Phase M 前置 |
| **八** | 空间碰撞检测（Sweep Line / R-Tree） | `collision_resolver.py`（R-Tree/PushDown）已通过测试 | ✅ | — |
| **九** | 多目标渲染（PDF/SVG/HTML/MD/DOCX） | `v3/pdf_renderer.py`（PDF）；SVG/HTML 未实现 | 🔶 | M2 |
| **十** | 自动化 QA（Translation/Layout/Semantic/Overlap 四维评分） | `v3/evaluator.py`（模块存在，未接入） | 🔶 | M3 |
| **十一** | AI Agent 自校验流水线（Reviewer 触发局部重译/重排） | `v3/runtime_supervisor.py`、`repair.py`（模块存在，未接入） | 🔶 | **DocumentRuntime**（第八章） |
| **十二** | 保持对外接口兼容（CLI/API/GUI/MCP） | `pdf2zh.py`、`backend.py`、`mcp_server.py`、`gui/` 契约保持 | ✅ | **M1**（出口统一已基本达成） |

> **v2.0 视角的核心修正：** 该矩阵揭示的不是“12 个待做模块”，而是 **一个迁移序列**——阶段零/一/二依赖 M2（数据统一）、阶段四/五/十/十一依赖 M3（流量切换）与 DocumentRuntime，阶段六/七必须排在 Phase M 之后。**排期应服从迁移依赖，而非按阶段号线性执行。**

---

## 十、核心痛点根因分析（v1.0 实证保留，结转为“已完成/待办”）

### 10.1 `thread=0` → `ThreadPoolExecutor(max_workers=0)` 崩溃（✅ 已修复）

**现象：** `WARNING Parallel page processing failed (ValueError): max_workers must be greater than 0`。
**根因：** `translate_stream(thread=0)` 默认值为 0（CLI 默认才是 4），并行 worker 中 `ThreadPoolExecutor(max_workers=0)` 直接抛异常，整页失败后回退串行。
**修复：** `pdf2zh/converter.py`：`max_workers=max(1, self.thread or 4)`；`pdf2zh/high_level.py`：`translate_stream` 入口与 `_translate_parallel` 的 `scalar_args` 双处归一化。

### 10.2 并行 xref 错位 → `bad xref` → 空白 PDF（✅ 已修复）

**现象：** `WARNING - Skipping obj_id 611 update_stream error: bad xref`（连续多条）→ 生成一堆空白 PDF。
**根因链：** 并行路径每个 worker 进程对同一 `fp` 快照执行 `translate_patch`，各自 `get_new_xref()` 从相同偏移分配页面 xref，`obj_patch` 键与父进程 `doc_zh` 的真实 xref 表**错位**；`update_stream` 命中无效 xref 抛 `bad xref` → catch 后仅跳过 → 内容流不写回 → 空白页。
**修复：** 父进程并行前**预创建 `page_xref_map={pageno: get_new_xref()}` 并同步给 worker**（worker 只引用不新建），`apply_page_xrefs` 由父进程统一 `set_contents`；并行参数全部收敛为标量/字节串，消除 worker 内不可 pickle 的 `Document/Font` 句柄（呼应本任务的“传递轻量参数，而非对象句柄”方案）。
**验证（`_diag_integration_parallel.py`，19 页真实 PDF + 真实 doclayout 模型 + 假翻译器）：**
```
updated stream 80/80 (100%)
insert_file OK, reordering 19 pages
subsetting fonts... doc_zh write OK (9982051 bytes)
translate_stream done in 28.4s
dual pages: 19, mono pages: 38   → 无 bad xref、无空白页
```

### 10.3 “翻译完成后卡死在页面合并”（⚠️ 部分缓解）

- `insert_file` 与 `move_page` 对 19 页文档仅耗时 0.0s；真实耗时在 `subset_fonts()` 与 `write(deflate, garbage=3, use_objstms=1)`（大型多字体 PDF 可达分钟级）。
- **观感卡死根因：** GUI 在“合并页（80%）→ 子集化（82%）→ 写文件（85%）”之间**没有中间进度事件**，`sync_status` 轮询只能显示同一 stage 文案。
- **待办：** `translate_stream` 增加可选 `progress_cb(stage, pct, msg)`，merge/subset 按页/按字体数上报；GUI 将事件推进与真实耗时绑定。

### 10.4 GUI 功能性问题清单（v1.0 状态结转）

| 问题 | 根因 | 状态 |
| :--- | :--- | :--- |
| 重复点击提交多次 | `on_translate` 通过 `current_task_id` 守卫 + `worker._IN_FLIGHT` 客户端防重；`effective_cid` 依赖浏览器 JS 变量（Python 端取不到），跨标签页防重失效 | 🔶 已部分缓解（按钮 disabled 兜底） |
| 组件占位未连接 | 上传/配置/进度/诊断/预览均已接入 `t_inputs`（22 输入）与 `sync_outputs`（19 输出）；`gr.PDF` 启动崩溃改为 `gr.HTML + iframe`；布局升级为 App Shell + StepBar 双栏 | ✅ 已修复 |
| 预览 `{"detail":"Not Found"}` | Gradio 5 `launch()` 重建 FastAPI app，`launch()` 前注册的 `/pdf-preview/` 被丢弃 | ✅ 已修复（launch 后注册） |
| 刷新导致下载失败/选项重置 | `SESSION_JS` 用 `localStorage` 持久化任务/预览/结果、配置项与主题（含系统深色跟随） | 🔶 已实现，待浏览器回归 |
| 无法选择 dual/mono | 结果选择由 `Dropdown` 升级为 `Radio` 双模式切换器，预览与下载均跟随选择（`_on_select` 更新 `selected_file`，预览优先取选中文件） | ✅ 已修复 |
| 单色/双语模式快捷切换 | 与"无法选择 dual/mono"合并为同一 `result_selector` 组件，避免双控件状态漂移 | ✅ 已修复 |
| 页面加载与整体主题割裂 | 引入 `styles.py` Design Token（24 项 Light/Dark 对齐）+ `TOGGLE_THEME_JS` 前端热切换，Header 徽章动态渲染运行时状态 | ✅ 已修复 |
| 翻译流程不可见 | 新增 4 阶段 StepBar（上传→版面分析→翻译→渲染），`build_stepbar_html` 由 `sync_status` 驱动 | ✅ 已修复 |
| 任务状态不更新/卡住 | `_execute_legacy` 阻塞期间无事件推进（同 10.3） | 🔶 待增强 |

### 10.5 “单句割裂与频繁换行”（路线图阶段一/二的直接靶点）

- legacy 主链路以 **LTChar 物理行**为单位聚行、按坐标排序、按行翻译，**没有句边界检测**（`A. B.`、`e.g.`、`Fig.`、公式内嵌 `{vN}` 占位符均无感知）；
- 译文以“行”为单位写回固定 bbox → 中英长度差异产生空行/重叠/割裂；
- V4 管线具备 `analyzer.py` 与 `constraint_graph.py`，但未接管主链路，用户看到的仍是 legacy 行为。
- **v2.0 判断：** 该痛点本质是 **M2（数据统一）未完成**——只要解析输出不是“语义完整、阅读顺序正确的 IR”，阶段一/二就永远只是 V4 侧的原型。

---

## 十一、新路线图：V5 → V6 三阶段演进

> 相比 v1.0 的“十二阶段线性推进”，本评审建议将未来划分为**三个演进阶段**。十二阶段是**能力维度**的清单，三阶段是**时序维度**的编排——二者不是替代关系，而是“做什么”与“先后做什么”的关系（映射见 11.4）。

### 第一阶段：Runtime Consolidation（运行时收敛）—— 对应 Phase M 落地

**目标：** 彻底统一 Legacy Runtime 与 V4 Runtime，建立唯一的 `DocumentRuntime` 与 `DocumentIR` 唯一事实来源。

1. **唯一运行时**：所有入口（CLI、GUI、MCP、API）共享同一个 `DocumentRuntime`；`translate_stream` 内部不再“直走 legacy”，而是 `DocumentRuntime.execute(task)`。
2. **唯一 IR**：以 `DocumentIR`（第三章多 Role 语义模型）为 Single Source of Truth；legacy `LTChar` 输出先转 IR 再入下游；IR 补齐 `to_json/from_json`。
3. **迁移排程**：`use_v4_engine` 从全有全无开关升级为**灰度策略**（按页/按文档类型/按用户），legacy 降级为兜底，回退率写入 Telemetry。
4. **兼容性基线**：同一 PDF 上 legacy 与 V4 输出自动 diff（页数/文本/bbox/重叠率），作为“接管不更差”的量化闸门。
5. **交付门槛**：平行并存的 Strangulation 阶段结束；任何新功能默认在唯一 Runtime 上实现。

### 第二阶段：Document Intelligence（文档智能）

**目标：** 把 V4 从“模块集合”升级为“文档理解 + 知识驱动”的智能管线。

1. **多图解耦**：将 Semantic Graph、Layout Graph、Execution Graph、Knowledge Graph 从单一大图中拆出，通过统一 `node_id` 关联（第四章）。
2. **四层 Memory**：在 Document/Entity 基础上补齐 Style Memory 与 Reasoning Memory，并注入 `TranslationPlan`（第六章）。
3. **Planner → TranslationPlan**：加入 Language/Domain/Confidence 检测链，Translator 只执行 `Execute(plan)`（第五章）。
4. **句检测与段落重构**：补齐阶段二（`A. B.`/`e.g.`/`Fig.` 边界），以语义完整段落为翻译单元。
5. **100 份 PDF 语料**：建成规模化测试集与 IR 序列化快照基线，CI 回归开始以“IR diff + 渲染 diff”为准。

### 第三阶段：Autonomous Document System（自治文档系统）

**目标：** 引入真正的 Agent 协作与受控自学习，把“流水线”升级为“车间”。

1. **多 Agent 协作**：Planner / Translator / Reviewer / Repairer 四个角色（可为同一模型的不同 plan，也可为独立 Agent）：
   - **Reviewer Agent**：全局巡检，识别术语不一致、版面重叠、漏翻，产出结构化问题清单；
   - **Repairer Agent**：基于问题清单对**局部节点**重新规划（改 plan 的 glossary/memory）与局部重排（改约束重解），而非整篇重跑。
2. **Telemetry 驱动自愈**：`DiagnosticGraph` 记录每个节点的评分/修复/重译历史；`Constraint Engine` 提供空间约束校验；三者联合触发局部修复。
3. **受控 Knowledge Center**：跨 Session 持久化知识库（术语/风格/领域），新增知识必须经过 `Reviewer` 确认才进入全局层，保证“持续学习但可控”。
4. **增量/断点/流式**：`DocumentSession` 支持断点续传（崩溃后从未完成节点续跑）、增量翻译（只重译改动页）、流式交付（先出已通过 Reviewer 的页）。

### 11.4 三阶段与十二阶段的映射

| 演进阶段 | 覆盖的十二阶段 | 核心交付物 |
| :--- | :--- | :--- |
| 一、Runtime Consolidation | 十二（契约）+ 零（IR 唯一化）+ 六/七前置 | `DocumentRuntime` + `DocumentIR` + 迁移排程 + 兼容性基线 |
| 二、Document Intelligence | 一、二、三、四、五、九 | 四图体系 + 四层 Memory + TranslationPlan + 语料基线 |
| 三、Autonomous Document System | 十、十一 | Agent 流水线 + Telemetry 自愈 + Knowledge Center + Session 生命周期 |

> **注意：** 阶段八（碰撞检测）与既有 2.0 增强组件（`collision_resolver.py`/`text_metrics.py`）在三个阶段内**持续增强并迁入唯一 Runtime**，不单独占用一个阶段——它们已经从“路线图要求”变为“已存在组件”。

---

## 十二、综合评价与落地优先级

### 12.1 工业级架构成熟度评价（六维）

| 层级 | 当前状态 | 评价 |
| :--- | :--- | :--- |
| **Legacy 工程化** | ★★★★★ | 已达到成熟生产级。`bad xref→空白 PDF`、`thread=0` 崩溃等并行路径硬伤已从根因修复并通过 19 页真实文档验证，核心稳定性显著提升。 |
| **V4 基础设施** | ★★★★★ | Runtime、Memory、Scheduler、Supervisor、Telemetry、Execution Graph 等基础能力已经相当完整（`pdf2zh/v3/` 34 模块），DI（ServiceRegistry）、事务（GraphRuntime）等工程基建优于多数同类项目。 |
| **V4 主链路接管** | ★★☆☆☆ | 已具备接管条件（Adapter/Strangler/统一服务层齐全），但默认执行路径仍由 Legacy 主导（`use_v4_engine=False`）。**这是当前最大的迁移任务，即 Phase M。** |
| **Document Intelligence** | ★★★☆☆ | 已拥有 Graph、Analyzer、Planner、DocumentIntelligence（实体/概念/引用图）等基础，但仍缺统一的多 Role 语义 IR、四图关联体系、深层（Style/Reasoning）Memory。 |
| **Constraint & Typography** | ★★☆☆☆ | 已有 R-Tree 碰撞检测（完成度高）、fontTools 字体度量与自研约束图，但距离六档 Document Constraint 与出版级动态排版仍有明显差距，且未迁入唯一 Runtime。 |
| **下一代架构（V6）** | ★☆☆☆☆ | 需要从“模块集合”演进为“DocumentRuntime + Multi-Graph + Agent System”。当前仅有雏形（`runtime_supervisor`/`repair`/`evaluator` 已存在但未闭环）。 |

> **定位判断：** 项目已脱离“pdf2zh 的重构版”范畴，正在构建**以 Document Runtime 为核心、面向 Document Intelligence 的通用文档处理平台**。未来的重点不是增加模块数量，而是**运行时统一、统一 IR、统一执行图、统一知识体系**——让整个系统围绕一个一致的数据模型与生命周期运转。

### 12.2 落地优先级（P0 → P4）

| 优先级 | 主题 | 关键交付 | 对应章节 |
| :--- | :--- | :--- | :--- |
| **P0** | 稳定主链路收尾 | `progress_cb` 细化 merge/subset 进度上报；GUI 双栏切换与刷新恢复回归；`update_stream` 失败页级重试 | 10.3/10.4 |
| **P1** | **Phase M（运行时收敛）** | `DocumentRuntime` 单一入口、`DocumentIR` 唯一事实来源、迁移排程（灰度）+ 兼容性回归基线 | 二、三、11.1 |
| **P2** | **Document Intelligence** | 四图体系、四层 Memory、TranslationPlan（Planner 决策链）、句检测与段落重构、100 份语料 | 四、五、六、11.2 |
| **P3** | **Constraint & Typography 升级** | Kiwi 约束引擎 + 六档 Document Constraint + 语义绑定；动态行高/段距闭环 | 七、11.3 |
| **P4** | **Autonomous Document System** | Agent 流水线（Planner/Translator/Reviewer/Repairer）、Telemetry 自愈、Knowledge Center、DocumentSession | 八、11.3 |

### 12.3 关键风险提示

1. **迁移半途而废风险（最高）**：双 Runtime 若一直“平行存在”，修复成本翻倍（一处 bug 改两遍）、行为漂移不可避免。**P1 必须设硬性完成时限**，而不是无限期并行。
2. **IR 过度设计风险**：多 Role 语义 IR 若一步到位可能拖延落地。建议**增量演进**：先以现有 `DocumentNode` 增加 `reading/translation/rendering` 三个可选 Role 字段 + `to_json/from_json`，再逐步补足语义推导。
3. **Agent 成本风险**：第三阶段的 Reviewer/Repairer 会引入额外 LLM 调用，必须设置**每节点预算上限与回退策略**（如 Reviewer 仅抽检置信度 < 阈值或重叠率 > 0 的节点）。
4. **知识与隐私风险**：Knowledge Center 跨文档学习需“受控确认”机制，避免把 A 文档的术语硬套到 B 文档（同形不同义术语）。

---

## 结论

1. **V4 已经从 Feature Development 进入 Runtime Migration 阶段**，本报告随之从“模块差距清单”升级为“架构迁移评审”。当前项目真正的风险不是“缺一个 Document IR”，而是**两套 Runtime（Legacy/V4）无管理的长期共存**。
2. **新增 Phase M（运行时迁移层）**是本次评审最重要的架构决策：以 `Adapter → V4 Runtime → Compatibility Layer → Progressive Cutover` 为骨架，通过 M1 出口统一、M2 数据统一、M3 流量切换三个子阶段，把 Legacy 渐进杀死。**Phase M 优先于 Constraint Layout 排期。**
3. **V4 基础设施已达到生产级（★★★★★），但主链路接管仅 ★★☆☆☆**：从“注入完成”到“接管默认路径”，中间差的是迁移排程、兼容性基线与唯一 `DocumentRuntime`——这正好是 Phase M 的交付物。
4. **Document Intelligence 的四项升级**（多 Role 语义 IR、四图体系、TranslationPlan、四层 Memory）是把“技术文档严密感”从口号变成工程能力的**第二优先投入**；其中 Style/Reasoning 记忆层当前完全空白，是最立竿见影的增量。
5. **演进节奏从“十二阶段线性推进”调整为“V5→V6 三阶段”**：Runtime Consolidation（运行时收敛）→ Document Intelligence（文档智能）→ Autonomous Document System（自治文档系统）。十二阶段是能力清单，三阶段是时序编排，二者按“迁移依赖”对齐。
6. **主链路已经“可稳定交付、不产空文件”**（80/80 流更新、merge 成功），这使 Phase M 不必从“救火”开始，而是可以从容地做“收敛”——这是当前工程状态相对路线图的最佳起点。

---

*报告依据：用户提供的《pdf2zh-next 重构与工程路线图》（十二阶段）、评审意见（Phase M / 多 Role IR / 四图体系 / TranslationPlan / 四层 Memory / Document Constraint Engine / Document Intelligence Runtime / V5→V6 三阶段）、本仓库源码（`pdf2zh/v3/*` 34 模块、`services/runtime_service.py`、`gui/*`、`high_level.py`、`converter.py`）、以及 `_diag_integration_parallel.py` 实测日志。v1.0 版本存档于 `doc/_archive/pdf2zh_next_roadmap_analysis.v1.md`。*












