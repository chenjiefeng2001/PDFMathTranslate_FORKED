# 最终产品阶段推进报告：九阶段方案 × 当前实现 差距对照与落地

> 版本：v1.3
> 日期：2026-08-05
> 依据：《Document Understanding 重构方案（九阶段）》、`doc/pdf2zh_next_roadmap_analysis.md`（v3.0）、
> `doc/text_overlap_analysis_report.md`、`doc/toc_layout_investigation_report.md`、`doc/v7_operator_runtime_report.md`、`doc/link_rect_mismatch_report.md`（v1.1）
> 基线：git HEAD `abfad7b` + 本报告对应的全部新增/修改（未提交）

---

## 摘要 / TL;DR

对九阶段方案的逐项核查结论是：**阶段五（TOC Engine）已完整落地并接入主链路；阶段一/二/三/九的组件级能力已就位但此前无真实 PDF 消费端；阶段六/七/八仍为 V4 侧能力、主链路未接管。**

v1.0（第一轮）补齐了「真实 PDF 文档理解闭环」的最后一环。**v1.1（第二轮）随后推进报告「遗留与下一步」清单：V8.3 主链路 IR 快照（阶段一/二/三消费端）、V8.4 写回门控挂到 legacy 写回路径（阶段七）、阶段九 <90 分自动差分快照留存（P1）、真实 PDF 语料 IR 基线（P2）、Geometry/legacy 双轨收敛点（P1）与竖向文本条通用化（P2）——清单中 P0 全部闭环，P1/P2 全部落地。**
**v1.2（第三轮）修复译文页超链接热区框停留在源坐标的结构性缺失（`link_rect_mismatch_report.md`），按方案 A「长线实现」落地：V8.5 复用 `_gate_records` 的段落 src→dst 几何重投影链接 rect，纯逻辑模块 + side-channel 接线 + feature flag，converter 维持 848 行；译文页 rect 与译后文字 IoU≥0.5、mono 原文页零回归、链接总数不变，全量回归 1500 passed。**
**v1.3（第四轮）按「图片翻译重构方案」落地 V8.6：**Image Translation Engine（`image_engine.py`）把图片对象独立成生命周期决策管线 —— 「判断图片里的文字是否该翻、该怎么翻」而不是「OCR 出来全翻」。**新引擎**：ImageObject（bbox/dpi/alpha/source）、统计分析特征（色数/边缘密度/明度方差/灰度判定）、纯规则分类器（Photo/Diagram/Chart/Screenshot/Logo/Equation/QR/Barcode/Map/CAD/Comic，CNN 后端接口预留）、文字区域检测（只框不识别、保留背景）、TranslationDecisionEngine（per-region translation_score，技术词典/品牌/UI/坐标轴/公式 keep）、Router（只把真正需翻译的 region 交给翻译器）。**Content Preservation Engine（`content_preservation.py`）**：在 Document IR 上统一产出 TRANSLATE / PRESERVE / OVERLAY 三种决策（正文翻、图/表/公式/页眉页脚保、题注翻但保编号、UI 图走 Overlay），并可把决策写回 IR 的 TranslationRole。**接线**：side-channel（`emit_preservation`，`translate_stream` 预合并前采集图片决策回填 `v3_output["preservation_records"]`，不触碰 legacy 渲染与像素），feature flag（`use_v4_image_engine` / `use_v4_content_preservation`，默认 off）。翻译器（Google/DeepL）零改动，只消费 `translate` 决策。全量回归 **1546 passed**。

| 新增交付 | 对应阶段 | 说明 |
| :--- | :--- | :--- |
| `pdf2zh/v3/geometry.py` | 阶段二 | 纯算法 Geometry Engine：Char → Word → Line → Paragraph → 栏感知 XY-Cut 阅读顺序，真实 PDF 可消费 |
| `pdf2zh/v3/structure.py` | 阶段三 | 特征向量块角色分类器（标题/正文/题注/目录/脚注/页眉页脚/页码/公式）+ DocumentIR 升级 |
| `pdf2zh/evaluate.py` | 阶段九 | **文档级评测 CLI/库**：几何/结构/翻译/渲染四组指标在真实 PDF 上计算（V8.1 回归基线的真实 PDF 版） |
| `pdf2zh/v3/mainline_wiring.py` | V8.3/V8.4/V8.5 | **本轮新增**：主链路 side-channel 统一入口（IR 快照 + 写回门控 + 链接桥接数据），从 converter 抽出以守住 strangulation 目标 |
| `pdf2zh/v3/link_remap.py` | V8.5 | **v1.2 新增**：译文页超链接 /Rect 重投影（纯逻辑 + guarded fitz 入口），复用 gate 记录 src_box/dst_box |
| `pdf2zh/v3/image_engine.py` | V8.6 | **v1.3 新增**：Image Translation Engine（图片分类/区域检测/翻译决策/Router/四种渲染模式，纯逻辑 + guarded fitz 分析入口） |
| `pdf2zh/v3/content_preservation.py` | V8.6 | **v1.3 新增**：Content Preservation Engine（Document IR 统一 translate/preserve/overlay 决策层，可写回 IR 角色） |
| `pdf2zh/corpus_baseline.py` | 阶段九/P2 | **本轮新增**：真实 PDF 语料 IR 基线 build/diff 工具 |
| RuntimeService 接线 | 阶段九 / V8.2 | `ServiceConfig.run_evaluation` 驱动主链路输出评测；`use_v4_*` + V8.6 图片/保护开关同步 v3 FeatureFlags + 回退遥测 |

---

## 一、九阶段 × 当前实现 差距对照矩阵

状态图例：✅ 已落地且可用 · 🔶 组件就位/未消费 · 🔴 缺失

| 阶段 | 方案要求 | 当前实现 | 状态 | 差距 |
| :--- | :--- | :--- | :---: | :--- |
| **一** | 统一 Document IR（Document/Page/Block + 四类 Role，不直接操作 bbox） | `document_ir.py`（四 Role + JSON schema）+ `IRBuilder`；本轮新增 `structure.to_document_ir()` 把真实 PDF 几何模型升级为 IR | ✅ | IR 建模完备；**v1.1 起主链路 `receive_layout` 直接产出 IR 快照**（V8.3，`ir_snapshots` side-channel） |
| **二** | Geometry Engine（Char→Word→Line→Paragraph→阅读顺序，纯算法） | `v3/geometry.py`：空格切词 + 间距兜底、基线行、栏级行拆分、行距/缩进段落合并、**栏感知 XY-Cut**、竖向文本条剔除（v1.1 通用化） | ✅ | **v1.1 起 `chars_from_ltpage` 消费 pdfminer 字符流**（V8.3 双轨收敛点）；`receive_layout` 自有聚类为并行 legacy 路径 |
| **三** | Structure Engine（特征向量 + 规则分类，不用 LLM） | `v3/structure.py`：font size/weight/indent/alignment/line count/numbering/digit/leader/capital 特征 + 九级判定链；`analyzer.py`（图级）并存 | ✅ | 分类器已落地；与 analyzer 的图级通道尚未融合（遗留） |
| **四** | Relationship Graph（图/表/公式/引用边） | `graph.py` 13 类 EdgeType + `graph_registry.py` 四图 ID 命名空间 + `knowledge_graph.py` 跨会话知识图 | ✅ | V4 侧；主链路未消费（迁移闭环问题） |
| **五** | TOC Engine（Detector / Parser / Renderer） | **已完整落地**：`pdf2zh/toc.py` + converter `toc_mode` + `high_level._apply_bookmarks` | ✅ | 无 |
| **六** | Translation Layer（置信度路由） | `planner_chain.py` + `multi_channel_rewriter.py` + `translation_runtime.py` | 🔶 | V4 侧；legacy 主链路逐段翻译未改 |
| **七** | Adaptive Layout Engine（重新排版而非替换） | `relayout_engine.py` + `constraint_graph.py` + `collision_resolver.py` + **v1.1 起 `MainlineRelayoutGate` 挂到 legacy 写回路径**（`mainline_wiring.run_writeback_gate`，V8.4） | ✅/🔶 | 门控已挂并产出 `gate_verdicts` side-channel（QA 标记 `kind=gate-blocked`）；重排引擎仍由门控按需触发 |
| **八** | LLM Refiner（仅低置信度） | `review_agent.py` + `repair.py` 闭环 | 🔶 | V4 侧；未接入主链路 |
| **九** | 评估体系（几何/结构/翻译/渲染） | `evaluator.py` + `migration_diff.py` + `evaluate.py`；**v1.1 起 <90 分自动留存 IR 差分快照**（`report_dir`/`report_threshold`，P1）与真实语料 IR 基线（`corpus_baseline.py`，P2） | ✅ | 已闭环（见 §2.5/§2.6） |

**结论**：九阶段中「建模类」阶段（一~四）与「质量类」阶段（九）已进入真实 PDF 可运行状态；「接管类」阶段（六~八 + 七的写回 gate）仍受制于 Phase M 迁移闭环 —— 与 `pdf2zh_next_roadmap_analysis.md` v3.0 的判断一致，本轮未改变该结论，但为其提供了**真实 PDF 回归基线**（阶段九落地后，接管与否变成可量化问题）。

---

## 二、本轮落地细节

### 2.1 Geometry Engine（`pdf2zh/v3/geometry.py`，阶段二）

四段式纯算法流水线，全部只依赖 bbox，不绑定 PDF 库：

```
Char（text + bbox + size + font）
  │  ① 按基线分组 + 空格硬切词 / 间距兜底切词
  ▼
Word
  │  ② 基线聚类成行 + 栏级空白带拆分（≥2.5×字号 = 栏间隙）
  ▼
Line
  │  ③ 行距连续性（≤1.9×字号）+ 缩进对齐 + 目录行保护 + 竖向文本条剔除
  ▼
Paragraph
  │  ④ 栏感知 XY-Cut：栏聚簇（y 跨度相交的 ≥2 段簇）→ 横切 → y 退化排序
  ▼
Reading Order（真实阅读顺序）
```

关键算法决策（均有测试固化）：

- **空格字符保留为切词符**：pymupdf rawdict 的空格字形即词边界；对不输出空格的 PDF 用 `0.45×字号` 间距兜底（`test_space_splits_words` / `test_no_space_pdf_falls_back_to_gap_heuristic`）。
- **栏级行拆分**：同一基线上的单词若存在 ≥`2.5×字号` 的空白带，拆为两条物理行 —— 双栏 PDF 同一基线交错排版的直接证据（`test_column_gap_splits_line`）。
- **栏感知 XY-Cut**：纯 XY-Cut 对「目录页 + 底部页码」会误切（页码阻断竖空白带）；本实现先做**栏检测**（按 x 中心聚簇，簇 y 跨度相交且 ≥2 段才算并列栏），整宽块（标题/目录行）按 y 归入栏流前后（`test_mixed_title_two_columns_toc`）。
- **竖向文本条剔除**：旋转 90° 的页边文字（arXiv 侧边编号）表现为「同 x 范围、≤3 字符、纵向堆叠 ≥3 行」，直接剔除，防止污染段落合并（`_drop_vertical_strips`）。

### 2.2 Structure Classifier（`pdf2zh/v3/structure.py`，阶段三）

特征向量按方案清单实现：`font_size / weight_est / indent / alignment / line_count / spacing_ratio / numbering / digit_ratio / punctuation_ratio / leader_ratio / capital_ratio / position_top / position_bottom`。

九级判定链（优先级从高到低）：**页码 → 目录行 → 题注 → 公式 → 脚注 → 标题 → 页眉页脚 → 引用 → 正文**。关键顺序设计：

- 脚注必须先于标题判定（数字脚注标记 "1 See..." 会命中标题编号特征）；
- 页眉页脚要求页面上下文（≥3 段）且后于标题判定，避免大字号标题被误判为页眉（真实论文第 1 页标题 17pt vs 正文 10pt 的实验已验证）。

`to_document_ir()` 把几何模型 + 分类结果升级为 DocumentIR（每页一个 Section 容器、按阅读顺序排列子节点、按角色映射 TranslationRole/RenderingRole），即**阶段一的真实 PDF 消费端**；`snapshot_ir()` 产出 IR 快照基线（V8.1 的 P4 验收产物）。

### 2.3 文档级评测（`pdf2zh/evaluate.py`，阶段九）

四组指标全部在真实 PDF 上计算，无参考译文依赖，无头可测、可入 CI：

| 组 | 指标 | 说明 |
| :--- | :--- | :--- |
| Geometry | `overlap_rate` / `duplicate_rate` / `collision_rate` / `overflow_rate` / `page_drift` / `mean_drift_pt` | 碰撞率 = max(不同基线行 IoU≥0.15 重叠率, 同位置重复绘制率)；溢出 = 越过页边界的行占比；漂移 = 按阅读顺序对应段落的 bbox 位移 |
| Structure | `heading/caption/toc/formula_preservation` / `reading_order_consistency` | 保留率 = 目标/源角色计数下限 |
| Translation | `target_coverage` / `residue_estimate` / `text_coverage` | 目标语字符占比；原文残留估计 = 拉丁主导长行占比；文本覆盖率 = 译文字符/原文字符 |
| Rendering | `collision_rate` / `overflow_rate` / `whitespace_score` / `density_score` | 空白/密度映射为 0~1 得分 |

`overall_score` = 0.3×几何 + 0.2×结构 + 0.25×翻译 + 0.25×渲染。CLI：`python -m pdf2zh.evaluate src.pdf out-mono.pdf [--json report.json]`。

**真实论文验证**（`test/file/2505.05427v1 (1).pdf` 前 3 页，同一文档自比对）：overall=91.6，headings=4、formulas=15、page_numbers=1，阅读顺序正确（标题→摘要→引言→正文），词/行/段恢复正确（13780 字符 / 218 行 / 55 段）。注：该文件为翻译管线加工产物（内容流带 cm 偏移），几何坐标有系统性偏移，属预期。

### 2.4 主链路接线（`pdf2zh/services/runtime_service.py`）

- `ServiceConfig.run_evaluation`：legacy 任务完成后对 mono 输出自动运行文档级评测，结果写入 `TaskState.quality_scores`（overall/geometry/structure/translation/rendering/collision_rate/overflow_rate/residue_estimate）与 `diagnostic_summary`（GUI 诊断面板直接可见）。
- `_sync_feature_flags()`（V8.2 接线）：`ServiceConfig.use_v4_*` 同步进 v3 `FeatureFlags` 单例，并记录 `legacy_mainline`/`v4_enabled` 回退遥测事件。
- **v1.1 新增字段**：`ServiceConfig.emit_ir` / `use_v4_gate` / `evaluation_report_dir`（plus `FeatureFlags.use_v4_gate`）；`TaskState` 增 `ir_snapshots` / `gate_verdicts`（写入 `to_dict`）；`_make_gate(page_w, page_h)` 工厂（带宽域默认的 MainlineRelayoutGate）。译后 `translate_stream` 的 `v3_output` 把 `ir_snapshots` / `gate_verdicts` 回写到 task state，并把 `evaluation_report_dir` 透传给 `evaluate_translation`（<90 留存）。

### 2.5 V8.3 主链路 IR 快照 + V8.4 写回门控（`pdf2zh/v3/mainline_wiring.py`，v1.1）

为守住 `pdf2zh/converter.py` 的 strangulation 目标（<850 行），V8.3/V8.4/V8.5 逻辑全部收敛进 `v3/mainline_wiring.py`，converter 仅保留：
- `_gate_records`：渲染循环内每段记录初始几何，碰撞求解后一次性 `_new_gate_record(x0, y, x1, size, text, translated, toc_mode, lidx, line_height, pstk[id].y0, pstk[id].y1)` 写入最终几何（`y` 碰撞位移后回填，无提前创建残留）。**v1.2 起记录同时携带源几何 `src_y0/src_y1`**，输出 `src_box/dst_box` 供 V8.5 超链接重定位复用。
- 页面末尾 `run_mainline_channels(self, ltpage)`：`emit_ir` 时 `emit_page_ir`（`chars_from_ltpage` 消费 legacy 同一 LTChar 流 → GeometryEngine → `to_document_ir` → `snapshot_ir`）；`relayout_gate` 时 `run_writeback_gate`（GateBlock 复本页段落 → `MainlineRelayoutGate.run` → `gate_verdicts[pageid]`，拦截页追加 `kind=gate-blocked` QA 标记与 `% pdf2zh-qa-overflow` 内容流注释，QA 解析循环已防御化兼容 `flag.get`）；`link_remap` 时按页存档 `conv.gate_records_by_page[pageid]`（V8.5 桥接数据）。

三条通道均为 side-channel：失败只进 debug 日志，任何情况不影响 legacy 翻译烘焙/写回。

### 2.6 V8.5 超链接热区重定位（`pdf2zh/v3/link_remap.py`，v1.2，方案 A 落地）

修复「译文页链接框停留在源坐标」的结构性缺失（详见 `doc/link_rect_mismatch_report.md` v1.1）：
- **新模块** `v3/link_remap.py`：纯逻辑 rect 数学（归一化/中心匹配/IoU/仿射投影/多段兼容），fitz 仅存在于 `remap_document_links` 入口；每页逐条守护，旋转页跳过，无匹配链接保留原 rect（宁不错配）。
- **桥接数据复用 R4**：`_new_gate_record` 扩展 `src_box/dst_box`；`translate_patch` 回传 `v3_output["link_records"]`；`translate_stream` 在 `set_contents` 后、`insert_file` 前调用 `_relink_translated_doc` —— mono 译文副本自动继承修正 rect，原文页不动。
- **开关**：`FeatureFlags.relink_links` / `ServiceConfig.relink_links`（默认 True），可一键回滚。
- 大纲 `pdf2zh/high_level.py` 接线：`translate_patch` 增 `link_remap` 参数 + `device.gate_records_by_page`；`translate_stream` 增 `relink_links` 参数（经 `**dict(locals())` 透传给 `link_remap`）。converter.py 仅改第 807 行（整体替换，**行数维持 848 < 850**）。

### 2.7 <90 分自动留存 + 真实语料 IR 基线（v1.1，阶段九 P1/P2）

- `evaluate_translation(..., report_dir=None, report_threshold=90.0)`：整体 < 阈值时自动落盘 `report_dir/<basename>/`，含 `report.json` + `source-ir.json` + `target-ir.json` + `diff.json`（`_ir_diff` 按 `_IR_BUCKET_KEYS` 桶计数对比）。CLI 增 `--report-dir/--report-threshold`。
- `corpus_baseline.py`：`build(pdf_dir, out_dir, max_pages, target_lang)` 对真实 PDF 语料逐份产出 IR 快照（单份失败不中断、manifest 记错误），`diff(a, b)` 分桶一致性与偏移统计；CLI `build/diff`。

### 2.8 V8.6 图片翻译 + 内容保护引擎（`image_engine.py` / `content_preservation.py`，v1.3）

对应「图片翻译重构方案」核心原则 —— *系统不是判断「图片里有没有文字」，而是判断「图片里的文字是否应该翻译、以及应该如何翻译」*。图片不再作为一个需要 OCR 碰运气处理的整体，而是按对象生命周期走独立决策链：

- **ImageObject（阶段一）**：统一数据结构（id / bbox / dpi / has_alpha / source / image_class / 决策结果）。`analyze_pdf_images(doc)` guarded fitz 入口逐页提取图片对象（失败仅日志）。
- **图片统计特征**：`compute_image_features`（numpy）输出色数、去重色比、边缘密度、明度方差、暗/白占比、灰度判定、宽高宽比 —— 分类与决策共用同一特征向量，可序列化。
- **规则分类器（阶段二）**：`RuleImageClassifier`（Photo/Diagram/Chart/Screenshot/Logo/QR/Barcode/Equation/Map/CAD/Comic/UNKNOWN），确定性可单测；`ImageClassifierBackend` 接口预留 CNN/ViT 后端（默认不掉任何重型依赖，与 `structure.py` 同风格）。
- **文字区域检测（阶段四）**：`detect_text_regions` 用暗像素密度 + 粗网格连通分量**只框不识别**，bbox 归一化，背景完全保留 —— 不做整图 OCR。
- **翻译决策（阶段三/五）**：`TranslationDecisionEngine` 按图片类型默认策略 + OCR 置信度 + 技术词典后计算 per-region `translation_score`；`Router`（技术名词/品牌/UI 控件/坐标轴数值/公式/代码/纯数字 keep）只把真正需要翻译的 region 交给翻译器 —— **Google/DeepL 等翻译器零改动**。
- **渲染模式**：`IMAGE_POLICY` 表给出四类 RenderMode（Preserve / Overlay / RegionReplace / FullRepaint），默认保护真实性（照片/Logo/QR/公式/条形码 → PRESERVE）。
- **Content Preservation Engine（§十一）**：`ROLE_DEFAULT` 把 IR 语义角色收敛为三种动作（正文/标题/目录/脚注 → TRANSLATE，图/表/公式/代码/页眉页脚 → PRESERVE，题注 → TRANSLATE+保编号 NEED_CONTEXT），`apply_to_ir` 可写回 IR 的 TranslationRole；图片委托 `ImagePolicy`，全部走同一 `decide()` 接口（不再逐对象打补丁）。
- **接线（side-channel）**：`translate_stream` 新增 `image_engine` / `content_preservation` / `emit_preservation` 参数，在 `insert_file` 合并前经 `_collect_preservation_side_channel` 采集决策回填 `v3_output["preservation_records"]` / `preservation_stats`，**不修改任何页面像素/链接/内容流**；`FeatureFlags.use_v4_image_engine` / `use_v4_content_preservation`（默认 off）+ `ServiceConfig` 透传，可一键灰度回滚。

---

## 三、验证结果

```text
python -m pytest tests/v3/test_geometry_engine.py      → 19 passed   （词/行/段/阅读顺序/竖向文本）
python -m pytest tests/v3/test_structure_classifier.py → 23 passed   （特征向量/九级判定/IR 升级/快照）
python -m pytest tests/test_document_evaluation.py     → 11 passed   （画像/四组指标/CLI/序列化/留存）
python -m pytest tests/v3/test_final_product_wiring.py → 14 passed   （Flags 同步/评测配置/IR+门控接线/chars_from_ltpage）
python -m pytest tests/v3/test_mainline_gate.py        → 7 passed    （GateBlock/MainlineRelayoutGate 裁决）
python -m pytest tests/v3/test_mainline_wiring.py      → 8 passed    （receive_layout → IR 快照 + 门控裁决 + V8.5 桥接记录 end-to-end）
python -m pytest tests/v3/test_link_remap.py           → 19 passed   （V8.5 rect 数学/IoU 验收/页面偏移/集成）
python -m pytest tests/v3/test_image_engine.py         → 25 passed   （V8.6 特征/分类/区域检测/决策/Router/端到端）
python -m pytest tests/v3/test_content_preservation.py → 21 passed   （V8.6 统一动作/语义默认表/图片委托/IR 写回）
全量回归（tests/ + tests/v3 + test/）                  → 1546 passed, 0 failed
```

- 主链路 side-channel 触发验证：converter 页末 `run_mainline_channels` 仅 1 行调用（逻辑在 `v3/mainline_wiring.py`）；`pdf2zh/converter.py` 严格守住 <850 行 strangulation 目标（848 行）。
- 链接重定位集成探针：`_relink_translated_doc` 直调 → 源 rect `(72,90,260,104)` 重投影为 `(72,48,320,63)`（与译后 span 完全重合，IoU 1.0）；mono 合并后原文偶数页保留原 rect、译文奇数页继承修正 rect（零回归 + 正确重定位同时满足）。
- 图片决策集成探针：`analyze_pdf_images` 对含嵌入式黑白图片的合成 PDF → 正确产出 ImageObject（bbox/dpi），未知类型默认 PRESERVE（保护原像素）。
- `python -m pdf2zh.corpus_baseline build "test/file" <tmp> --max-pages 2` → 4 份快照；`diff` 自比对 → 4 consistent / 0 changed。

基线对照：v1.0 全量 1544 passed；v1.1 全量 1473 passed（部分外部引擎/网络用例跳过）；v1.2 全量 **1500 passed, 0 failed**；v1.3 在 v1.2 基础上新增 46 条 V8.6 定向测试，全量 **1546 passed, 0 failed**（无跳过）。

---

## 四、与九阶段方案的对照结论

1. **阶段一（Document IR）**：建模完备 + 真实 PDF 消费端（`to_document_ir` + IR 快照）；**v1.1 主链路 `receive_layout` 侧通道产出 IR 快照**（V8.3）。legacy `LTChar → obj_patch` 直通保留为并行路径，未砍（迁移闭环范畴）。
2. **阶段二（Geometry Engine）**：完整落地（纯算法、无 LLM）。**v1.1 新增 `chars_from_ltpage` 消化 pdfminer 字符流，是 V8.3 双轨收敛点**；竖向文本条判定通用化（同 x 堆叠 + 旋转字形纵横比，config 化阈值）。
3. **阶段三（Structure Engine）**：完整落地（特征向量 + 规则）。剩余：与 `analyzer.py` 图级通道融合、大规模语料调参。
4. **阶段四（Relationship Graph）**：组件就位（V4 侧），未受本轮影响。
5. **阶段五（TOC Engine）**：已完整落地并接入主链路（此前轮次）。
6. **阶段六（Translation Layer）**：V4 侧就位、主链路未接管（置信度路由仍为后续项）。
7. **阶段七（Adaptive Layout）**：legacy 碰撞避让生效 + **v1.1 `MainlineRelayoutGate` 挂到写回路径**（V8.4，`gate_verdicts` side-channel）；**v1.2 输出侧补上「译文页超链接随段落几何重投影」**（V8.5，复用 gate/IR 同源桥接数据，详见 `link_rect_mismatch_report.md`）。
8. **阶段八（LLM Refiner）**：V4 侧就位，未接入主链路。**v1.3 图片决策按「低置信度才交 LLM 分类」原则设计**（`ImageClassifierBackend` 接口 + 决策分数低于阈值时可接 LLM 判定类别，OCR/重绘仍不在 LLM 职责内）。
9. **阶段九（Evaluation）**：图级 + 块级 + 真实 PDF 文档级三级评测齐备；**v1.1 补 <90 分差分快照留存（P1）与真实语料 IR 基线（P2）**，接管与否已成为可量化问题。

## 五、遗留与下一步（诚实清单）

| 事项 | 级别 | 说明 |
| :--- | :--- | :--- |
| V8.3 后半程：IR 侧通道 → 渲染接管 | P0→P1 | 侧通道已产出 IR/门控裁决；「接管」需在迁移闭环内以 gate 判据切换渲染路径 |
| Geometry 与 `receive_layout` 聚类合并 | P1 | `chars_from_ltpage` 已建立同流收敛点；双轨对比试验后替换 |
| 阶段三 与 `analyzer.py` 图级通道融合 | P1 | 分类器规则流与图级通道的融合验证 |
| 大规模真实语料基线扩充 | P2 | `corpus_baseline` 已可用；语料规模待扩充 |
| 阶段六/八 主链路接管 | P2 | 置信度路由与 LLM Refiner 仍限 V4，未受本轮影响 |
| V8.5 有待真实翻译产物回归 | P1 | rect 数学/接线/IoU 验收已由定向测试与探针覆盖，真实 PDF 端到端待经 CLI 服务路径（含 doclayout 模型）复验 |
| V8.6 图片决策待真正接入 OCR 与渲染后端 | P1 | 决策/分类/Router 已落地；OCR 结果（`TextRegion.text/ocr_confidence`）尚需喂入决策链、RegionReplace/Overlay/FullRepaint 渲染后端尚未接入（当前仅 PRESERVE 生效，其余为决策契约） |
| V8.6 图片分类器真实语料调参 | P2 | 规则阈值基于启发式合成样例；需真实图片集标定（Photo/Diagram/Chart 边界） |

---

*报告完。测试证据：`tests/v3/test_geometry_engine.py`、`tests/v3/test_structure_classifier.py`、`tests/test_document_evaluation.py`、`tests/v3/test_final_product_wiring.py`、`tests/v3/test_mainline_gate.py`、`tests/v3/test_mainline_wiring.py`、`tests/v3/test_link_remap.py`、`tests/v3/test_image_engine.py`、`tests/v3/test_content_preservation.py`。*
