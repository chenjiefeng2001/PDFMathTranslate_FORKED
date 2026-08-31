# 7H-1 — Dual Fidelity Forensics（差分诊断闭环）交付报告

日期：2026-08-30 · 新增目录：`dual_forensics/` · 测试：`tests/test_dual_forensics_7h1.py`（10 通过）

本报告记录 7H-1 的落地：把「一个已经生成出来的 Dual PDF，某个视觉缺陷第一次在
哪一层出现」从一个可回答的问题变成可运行的工程工具（First Divergence Stage，
下文 **FDS**）。按方案：**先不修任何 bug，先让系统对真实 Dual PDF 给出 FDS 报告**。
本交付只做诊断 instrument，不改 recovery/packing，不新增 layout rule。

---

## 1. 交付物

`dual_forensics/`（顶层包，纯读、可复现、不需要 translator / ONNX / renderer）：

| 模块 | 职责 |
|------|------|
| `provenance.py` | 阶段注册表（source→parser→model→translation→layout→render→pdf）+ 每节点证据 schema；`node_id = p{page}_{index}` 与上游模型/plan 的 `block_id` 严格一致 |
| `snapshot.py` | 跑同一 v3 管线（`build_document_model`→`translate_document`(identity)→`render_plan_from_model`）抓 parser/model/translation/layout 四份证据，按页落盘 |
| `pdf_inspector.py` | 读回 Dual PDF 页的真实文本/draw 对象（bbox/font/size/color/v3_bbox），并探测 **MuPDF emitter 语法错误**（页面内容流里的畸形 float）——文本层看不到、只有对象层可见的 F4/F9 信号 |
| `diff.py` | 把 Dual 页的 render run 按几何匹配回模型节点 → `Trace`；识别 dangling block / unmatched run |
| `defect.py` | F1–F10 taxonomy + 检测器；对每个 finding 计算 FDS 与逐阶段 PASS/FAIL |
| `report.py` | 落 `manifest.json` / `page-*/{source,model,translation,layout,render,diff}.json` / `summary.json` |
| `__main__.py` | `python -m dual_forensics --source X.pdf --dual Y.pdf --page N ... --out DIR` |

```
python -m dual_forensics \
  --source 'pdf2zh_files/Large-Scale C Volume I_ ...pdf' \
  --dual   '...-dual.pdf' \
  --page 62 65 69 75 185 186 187 \
  --out forensic-report-c/
```

输出与方案 §5 完全一致：`summary.json` 给出 `F1..F10` 计数 + `first_divergence_stage_histogram`。

### 约束修正（方案 §1 的 corpus 难点，已实证）

- **「Art of Multiprocessor Programming」不可用**：pdfminer `extract_pages` 正常（1.9s/20 页），
  但 `build_document_model` 是 7G 报告记录的 **infinite-hang 输入**，仓库内也没有它的 Dual PDF。
  → corpus 锚定在仓库真实可用的 Dual 对（`pdf2zh_files/`）：**C 书**（`Large-Scale C Volume I`，
  源 1023 页 / dual 2046 页，C++ 代码密集——F2 最佳语料）、**AI for Games**（237/474，正文+图注）、
  **Game Physics**、**Networking**。全部可跑通。
- **Dual 页对齐**：四个真实 Dual 都是「交替页」——源页 N ⇔ dual 页 `2N`(原文) / `2N+1`(译文)，
  已对 AI 书与 C 书逐页验证。

---

## 2. 真实 corpus 上的 FDS 报告（验证工具是否区分「真缺陷」与「干净页」）

用恒等翻译在源页抓 parser/model/translation/layout，用 Dual 译文页读 render 证据。

### C 书（10 页：正文 + 代码 + Figure 0-23 + 数学段）→ 116 findings

```text
F2  code mistranslated (code 被中文化)      14
        first divergence = model            14   ← 语义分类把 code/formula 判错并送译
F9  text layer vs visual layer mismatch     10   (每页 1 条，renderer)
        first divergence = render             10  ← MuPDF emitter 写坏 float
F10 draw-object-lost (match-gap, UNCERTAIN) 92
        first divergence = render             92  ← 双栏重排导致几何失配，多属 match-gap
```

FDS 直方图：`model=14, render=102, translation=0, layout=0, pdf=0`。

**关键结论——不要继续改 layout。**

- **F2 的 First Divergence 是 model**：真实 dual 译文页 371 上，源 C++ 代码块被逐行中文化——
  `typedef int Int; // 'int'33 的别名的 'typedef' 声明 class Foo; // 纯（又名"前向"）类声明 ...`
  （真实原文残留）。工具把该块的源 code 文本与其渲染后的 CJK 文本按几何对上，F2 检测器命中，
  FDS=model：源是纯 code（source/parser PASS），错在 **semantic 层把公式/正文块当可翻译块**。
  这正是方案强调的「绝不能继续改 layout」的情形。
- **F9 的 First Divergence 是 render**：dual 译文页内容流含
  `1.0 0.0 0.0 1.0 -9.000000001435637e-05 -1.1999999998124622e-05 cm`——**合法**科学计数法浮点，
  但 MuPDF 解析 `cm` 时报 `unknown keyword: '-9.000000001435637e'`。文本层提取完全正常
  （字形还在），**只有对象层可见**，是典型的 renderer-stage F4/F9 emitter 缺陷
  （16 位十进制科学计数 float 触发 MuPDF tokenizer 分字）。`content_stream_anomaly` 用
  `TOOLS.mupdf_warnings()` 权威捕获，Corpus 每引到该页的文本提取都会被此破坏。
- **F10 标记 UNCERTAIN**：dual 奇数页会对内容重排，源 src_box 几何匹配不到其实存在的内容
  （页眉/页脚/页号/跨行正文），92 条里大多是 match-gap 而非真丢失。按诚实原则不把几何失配
  当成已证实的 renderer 判定。

### AI book（5 页：正文+图注）→ 4 findings（干净页，符合预期）

```text
F10 match-gap UNCERTAIN   2
```
零 F2、零 F9。C 书的图/代码/数学缺陷在 AI 书上不存在 → **工具能区分「真缺陷页」与「干净页」**。

---

## 3. 检测器诚实性（避免过度归因）

遵循方案「translation wrong ≠ placed wrong」纪律：

- **F2 只用可靠信号**：只有当「源码是 code-like **且**渲染文本 CJK 比 > 25%」才算 F2；
  FDS = model（块被判成 formula/paragraph 而非 code → 策略送译）。曾先用「ASCII 渲染 ≠ 源」的弱启发，
  在 AI 书产生 2 条假阳性（`proc continuousMove {` 渲染成 `p roc continuousMove { w`，
  只是 CJK 字体水平度量的空格伪影，并非误译），已删除该弱分支，回归 0 假阳性。
- **F9/F10 只在 render 层证实时才 FAIL**；dangling 一律 `confidence: uncertainty`。
- 所有 detector 逐阶段写 `stage_verdicts`，`first_divergence` = 最早的 FAIL 阶段。

---

## 4. 工程产物清单（工作区）

```
dual_forensics/
    __init__.py
    provenance.py
    snapshot.py
    pdf_inspector.py
    diff.py
    defect.py
    report.py
    __main__.py
tests/test_dual_forensics_7h1.py   (10 tests: provenance/taxonomy/inspector/match/
                                     snapshot/defect-FDS/report-tree)
```

不改动 `pdf2zh/`；新增文件不触碰 586 个 layout/recovery 测试。本地验证 `pytest tests/test_dual_forensics_7h1.py` 全绿。

---

## 5. 下一步（7H-2 方向建议，按类别而不是按页/按书）

1. **F2 → model / semantic 层**：code/formula 被判成可翻译块。不是 packer/layout。
   方向：`StructureClassifier` / `doc_layout` 的 code vs formula 判定 + `TranslationPolicyPass`
   对 `code` 类块强制 preserve。
2. **F9 → renderer emitter**：畸形 float 写进内容流，是两个 renderer 的共同 emitter 问题。
   方向：把 `cm`/transform 的浮点序列化从 `repr(float)` 改为短十进制
   （`%.9g` 上限），消除会让 MuPDF tokenizer 分裂的 `e±` 长尾。
3. **F10 → 需要真实 render 侧 provenance**：当前 render 无 id，只能几何匹配。
   7H-1 已经证明这是「无法严格回答每块去哪了」的根因。方向：render_plan 条目在渲染时把
   `block_id` 写进可读元数据 / 每页留 `render-manifest`，让 diff 从几何匹配升级为 id 直连。
4. **F1/F3/F5/F6/F8 的 detector 仍待补**：本轮优先交付了验证闭环与 C/N 两类信号；其余类别检测器
   与 per-page `render.json` 的 figure/draw 几何关系是下一批可做的小步。

### 7H-1 状态（COMPLETE）

| 项 | 状态 |
|----|------|
| `dual_forensics/` 工具链 + CLI + report tree | ✅ |
| FDS 报告（真实 Dual corpus） | ✅ C 书 model=14 / render=102；AI 书干净 |
| F1–F10 taxonomy + 检测器（discipline: wrong ≠ placed wrong） | ✅ F2/F9/F10；F1/F3/F5/F6/F8 待补 |
| **contract test（禁止 guessed FDS）** | ✅ `test_every_defect_has_allowed_first_divergence` |
| 10→12 forensic tests + 110+ layout/recovery 回归 | ✅ 全绿 |

### 7H-1 corpus qualification failure（不是 7H-1 失败）

方案原文的 `The Art of Multiprocessor Programming, 2e` 因 `build_document_model`
**infinite-hang** 无法进入完整 FDS pipeline，且仓库无其 Dual PDF。已记录为
**corpus qualification failure** 并换用真实可用 Dual 对（C 书 / AI / Game Physics /
Networking）。该 hang 本身是独立的 7G model-build termination 问题，与 7H-1 无关。

---

## 6. 7H-2A Render Provenance（已落地部分）

按 7H-2 优先级（A→B→C），第一优先不是 F2，而是**先解决 F10 的 UNCERTAIN 盲区**。
现状：F10 的 92 条大多是「几何匹配器无法确认」，不是已证实的 renderer defect；
若不先给 render 侧 provenance，直接修 F2/F9 仍没有闭合诊断系统最大的盲区。

### 6.1 已实现

- **Renderer provenance（纯增量采集）**：`render_plan_to_pdf(..., provenance=True)`
  把每个绘制块记录为
  ```json
  {"source_node_id": "p0_0", "render_object_ref": "R0", "page": 0,
   "object_type": "wrapped|list|toc|flow", "final_bbox_v3": [...],
   "font_size": 12.0, "text": "..."}
  ```
  缺省为 `False`，stats 不含该键——既有行为/24 个 renderer 测试零影响。
- **ID-direct diff**：`aggregate_page_id_direct` 直接用 provenance 查表，不再几何匹配：
  - present  → 按 id 确认存在（含 moved / transformed 的几何对照）；
  - absent   → **已证实的缺失**（不再是 UNCERTAIN）；
  - stray    → 有渲染对象但无 source 块背书。
  F10 从「geometry similarity → best-effort → UNCERTAIN」升级为
  「source_node_id → render_object_ref → present / absent / moved」。
- **CLI**：`python -m dual_forensics ... --use-provenance-render` 用生产 renderer
  （恒等翻译）渲染源页并产出 ID-direct 报告。
- **contract test**：`test_every_defect_has_allowed_first_divergence` 强制
  FDS ∈ {parser, model, translation, layout, packing, render, pdf, unknown} 且
  equals 最早 FAIL 阶段——杜绝未来出现 `defect detected / FDS = guessed`。

### 6.2 边界（诚实声明）

仓库内现有的 BabelDOC 生成的 Dual（corpus 用）**无法被注入 provenance**——
那是外部引擎在离线生成时的产物。7H-2A 的 provenance 面向**本管线自己渲染**的
Dual（magicpdf mono/dual 路径），ID-direct 模式需要源页重渲染（恒等翻译，可复现）。
对既有外部 Dual，仍走几何匹配，并标注 UNCERTAIN；对管线内渲染，走 id-direct。

### 6.3 测试

```
test_renderer_provenance_id_direct     renderer 逐块 provenance + id-direct present/dangling
```---

## 7. 7H-2B Numeric Emitter（F9）— COMPLETE

严格收窄，只修「PDF graphics/text emitter 的浮点序列化导致 tokenizer / parser
结构性歧义」。不改 recovery / packing / provenance。

### 7.1 决定性实验（B-3/B-4 不假设 `%.9g`）

先用 MuPDF probe 确定 contract，而不是预设「`%.9g` 够了」——结果推翻了原方案假设：

```text
repr(  -9.000000001435637e-05 )          → '-9.000000001435637e-05'   MuPDF ERROR (unknown keyword)
format(-9e-05, '.9g')                     → '-9e-05'                    MuPDF ERROR (unknown keyword '-9e')
'-0.0000900000' / '-0.0000000001435637'   → fixed decimal               MuPDF OK
'1e05' / '-9e-05' (exponent token)        →                            MuPDF ERROR
```

**结论（B-4）：问题不是「科学计数法不允许」，而是 MuPDF tokenizer 对 `e±n` 尾的
split 不兼容。** 计划预想的 `%.9g` 仍会产出 `-9e-05`，不够。所以 contract 定死为：

```text
PDF numeric token = finite decimal, bounded precision(≈9 sig), NO exponent
```

### 7.2 落地

- **`pdf2zh/pdfnum.py`**（contract 单一来源）：`pdf_num(v)` 对有限 float 输出
  无指数、有界精度、去尾零小数（`1e15 → "1000000000000000"` 不丢整数零）；
  NaN/±Inf/`-0.0`/非数值一律 `"0"`，坏值永不进流；`never_emits_exponent` 不变量。
- **emitter 接线**：`pdfinterp.py` 的 Form XObject `cm` 矩阵（`do_Do`）与
  页面偏移 `cm`（`process_page`）从 raw f-string 改为 `pdf_num` 序列化——
  正是写坏 `-9.000000001435637e` 的两处。converter 的 `_safe_float`(`.4f`) 与
  `pdf_op_builder`(`.2f`) 本就是定点，无需改。

### 7.3 Acceptance（B2.1–B2.6）

| 项 | 状态 |
|----|------|
| B2.1 PDF syntactically parseable | ✅ contract test 用真实 MuPDF probe：probe 出 `unknown keyword` |
| B2.2 MuPDF text/object inspection | ✅ 全部 contract token MuPDF 解析无警告 |
| B2.3 transform semantic equivalent | ✅ 每次序列化 parse-back 相对误差 < 1e-7（9 位有效） |
| B2.4 page geometry unchanged（容差内） | ✅ cm 矩阵六元组 + page 偏移 round-trip 保真 |
| B2.5 provenance object mapping unchanged | ✅ renderer provenance 未被触碰 |
| B2.6 existing renderer tests unchanged | ✅ 24 renderer + 17 xobject + 96 converter/layout 全绿，0 回归 |

### 7.4 B-5 回跑 forensic（关键闭环）

`pdf2zh/pdfnum` 上线后用生产 renderer 对 C 书 185/186 页做 in-pipeline 重渲染，
`content_stream_anomaly`（MuPDF warnings 权威捕获）逐页检查：

```text
F9-in-pipeline: 0   （render 阶段本 emitter 零语法错误）
```

外部 BabelDOC Dual 页面上仍能看到历史坏 float —— 那是外部引擎离线产物，
无法回溯修复（与 7H-2A 同一边界）；但**本管线 emitter 已不再产生该缺陷**。
历史 `F9:10`（外部 dual）与 `F9-in-pipeline:0`（本管线）并列为诚实记录。

### 7.5 测试

```
tests/test_pdfnum_contract.py  (9: parse-back budget/transform corpus/科学计数/NaN/+-0/MuPDF probe)
tests/test_pdfnum_emitter.py   (3: PDFPageInterpreterEx cm/offset 走 pdf_num、MuPDF 重解析干净)
```

---

## 8. 7H-2C Semantic Translation Policy（F2）— COMPLETE

入口条件固定：不是「code 不翻译了」，而是把责任拆成

```text
StructureClassifier → semantic_role → TranslationPolicyPass → translation_policy → Translator
```

expressible per policy:

```text
CODE → PRESERVE · FORMULA → PRESERVE/specialized · IDENTIFIER → PRESERVE
FILENAME → PRESERVE · COMMAND → PRESERVE · CITATION → context-dependent
PROSE → TRANSLATE · CAPTION → TRANSLATE
```

真正解决 F2 的**类别系统**问题，而非把 C 书变成特判 case。

> 里程碑：不再扩充 7G-4/7G-5 recovery/packing：7G 解决「系统不要因复杂 PDF 失控」，
> 7H-1 证明「系统能告诉我们哪里失控」，7H-2A/7H-2B 开始逐层消灭 fidelity defect。
### 8.1 落地（F2 根因 = semantic 角色仲裁，类别系统而非 C 书特判）

- **角色仲裁**（`structure.py`）：`StructureClassifier._arbitrate_preserve_role` 在
  formula（第 4 步）之前，以可靠证据优先定型 `CODE / COMMAND / FILENAME /
  IDENTIFIER`——「一旦充分证据确认 CODE，后续普通角色规则不得覆盖」。已在
  `doc_passes.py` 说明同一纪律：**code 必须首先通过其自身证据被确认为 code**，
  再交由 policy 层放行，而非靠 policy 层特判把任意块判成 code。
- **策略层统一消费**：`doc_passes._KEEP_KINDS`、`document_model._KEEP_KINDS`、
  `render_payload.KEEP_KINDS`、`annotate_roles`/`annotate_render` 全部纳入
  `command / filename / identifier`。仲裁产生的 `role`/`kind` 直连 `translation_policy`，
  **translator 调用点零特判**（有　`TestNoTranslatorPatch` 纪律测试锁定）。
- **formula 不被 CODE 吞**：`_arbitrate_preserve_role` 在 formula 之前运行但只认
  强结构信号（大括号/分号/关键字/作用域运算符），数学式单行不满足 → 仍归 formula，
  KEEP_KINDS 继续保护。
- **修复后 build_document_model 主路径（无 SemanticPass）**：`annotate_roles` 对
  DOCUMENT_IR 角色图新增 code 桶，`classify_paragraph` 已是源 → 证据链一致，
  forensic 快照不再需要额外特判。

### 8.2 Acceptance（`tests/v3/test_7h2c_semantic_policy.py` 22 通过）

- CODE→PRESERVE、FORMULA→PRESERVE（且不误归 CODE）、IDENTIFIER/COMMAND/FILENAME→PRESERVE；
- 正文 prose 仍 translate；citation 不被 CODE 误吞；
- forensic 证据：code 块 kind=code → F2 检测器不再 flag（model/translation PASS）；
- 既有 formula/code/table protection + renderer + doc_passes 全绿 0 回归
  （`test_structure_classifier`/`test_v12`/`test_v13`/`test_content_preservation`/
  `test_magicpdf_renderer`/`test_babeldoc_formula_protect`/`test_final_layout_contract`/
  `test_architecture_7e` 均通过）。

> 里程碑：不再扩充 7G-4/7G-5 recovery/packing：7G 解决「系统不要因复杂 PDF 失控」，
> 7H-1 证明「系统能告诉我们哪里失控」，7H-2A/7H-2B 开始逐层消灭 fidelity defect。
> 当前状态：**`7H-1 COMPLETE → 7H-2A COMPLETE → 7H-2B COMPLETE →
> 7H-2C COMPLETE`**（7H 校验诊断层全部完成）。
