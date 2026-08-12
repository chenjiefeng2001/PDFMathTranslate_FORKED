# P5–P10 语义文本重建与公式几何重构 实现报告

> 版本：v1.0（P5–P10 全链路闭环）
> 日期：2026-08-11
> 依据：《PDF 原生文本几何与数学公式重建技术架构规范书》（P1–P4 已完成，本文为 §7 演进路线 P5–P10 的实现记录）
> 基线：`pdf2zh` V8.3–V11 + Phase D 既有 side-channel 体系

---

## 摘要 / TL;DR

P1–P4（碰撞修复 + 数学字体保护）完成之后，底层建模缺陷仍在：**多字体段落语义割裂**（Font/Style Run 切换被当作翻译单元截断边界）与**数学公式几何重构失效**（公式按普通文本处理导致几何解体）。本报告按规范书 §8 目录规范，将重构重心下沉至**语义文本重建与字形几何层**，新建四个子系统，串成 P5–P10 六阶段链路，并以 side-channel 形式接入真实主链路：

```
LTChar ──► Glyph ──► StyleRun ──► VisualLine ──► LogicalParagraph
    ──► FormulaObject 抽取（<formula_x> 锚点保护）──► TranslationUnit
    ──► Inline Layout / Master Baseline / Layout Solver（三阶段坐标）
    ──► Dual Patch + QA 校验（§9.1 / §9.2）
```

**验收全部达标**：多字体段落聚合不碎裂（font-switch ratio 0.0714 < 0.1）、字符完整性率 0.00%、恒等译文公式零漂移（Δx=0.0pt / Δy≤0.127pt，阈值 0.5pt）、锚点匹配率 100%。全量回归 **2237 passed / 1 skipped**。

---

## 一、架构落地（规范书 §8 目录规范对照）

| 规范书目标目录 | 实际交付 | 核心职责 |
| :--- | :--- | :--- |
| `geometry/glyph.py` | `pdf2zh/geometry/glyph.py` | 不可变 Glyph 数据模型 + 从 pdfminer LTChar/LTPage 原生提取 |
| `geometry/style_run.py` | `pdf2zh/geometry/style_run.py` | 同样式连续流划分（StyleRun）+ 字体名规范化 |
| `geometry/line.py` | `pdf2zh/geometry/line.py` | VisualLine 物理行重构（基线/重叠/间距三重判定） |
| `geometry/paragraph.py` | `pdf2zh/geometry/paragraph.py` | LogicalParagraph 语义段落聚合（忽略字体切换） |
| `formula/confidence.py` | `pdf2zh/formula/confidence.py` | §5.3 公式置信度打分引擎（五维加权） |
| `formula/extractor.py` | `pdf2zh/formula/extractor.py` | FormulaObject 抽取（不可变一等排版对象） |
| `formula/anchor.py` | `pdf2zh/formula/anchor.py` | 翻译占位符 `<formula_x>` 注入 / 还原 / 完整性评分 |
| `layout/inline_layout.py` | `pdf2zh/layout/inline_layout.py` | P7 统一 Inline 布局模型（Text/Formula 并列） |
| `layout/baseline.py` | `pdf2zh/layout/baseline.py` | P8 Master Baseline 几何计算 + 对齐 |
| `layout/solver.py` | `pdf2zh/layout/solver.py` | P9 Layout Solver（§6.2 三阶段坐标） |
| `patch/dual_patcher.py` | `pdf2zh/patch/dual_patcher.py` | P10 双层补丁合成 + §9 QA 校验 |
| （编排层） | `pdf2zh/v3/reconstruction_pipeline.py` | P5–P10 统一编排管道（Glyph 输入 / LTChar 输入双入口） |
| （演示/验收） | `pdf2zh/v3/qa_reconstruction_demo.py` | 确定性合成 + 真实 PDF smoke 验收脚本 |

---

## 二、P5 — 视觉行与逻辑段落重建


---

## 三、P6 — 公式对象重建

### 3.1 置信度引擎（§5.3）
`FormulaConfidenceEngine` 实现五维加权打分，权重 `w = [0.30, 0.25, 0.15, 0.15, 0.15]`：

- **C_font**：字体族名包含 `Math` / `Sym` / `CM` / `STIX` / `AMS` / `EUFM` / `MSBM` / `CMSY` / `CMEX` / `CMMI` / `XITS` / `Cambria Math` / `Asana` / `LMMath` / `MnSymbol` 等关键词（覆盖 `formula_font_investigation_report.md` 附录列出的 Springer/Elsevier/AMS/IEEE 典型数学字体）。命中≥2 个字形即给 0.95，防单字符误判。
- **C_density**：`∫ ∑ √ ≤ → ∈ ⊂ ± × ÷` 等数学符号密度（按非空白字符比例）。
- **C_unicode**：Math Alphanumeric Symbols（U+1D400–U+1D7FF）区间命中。
- **C_baseline**：行内基线微小波动（上下标结构）加权。
- **C_layout**：DocLayout 区域类别（`Equation`/`Formula`）贡献 0.5，预留外部注入接口。

阈值：≥ 0.75 → FormulaObject；0.45–0.75 → ambiguous（数学字体 ≥ 0.85 且含括号/运算符等结构提示时升级为 formula）；< 0.45 → TextRun。

> **本次迭代修复**：`C_font` 原实现要求整行统一数学字体才给高分，多字体混排公式（如正文 Helvetica + 符号 CMSY）会被打成文本。现改为行内任一 ≥ 2 个字形命中即 0.95，并新增结构提示（括号配对 / 运算符 / Unicode 上下标）兜底歧义段。

### 3.2 FormulaObject（不可变公式对象）
`FormulaObject(formula_id, glyphs, bbox, baseline, raw_latex_approx, is_display_mode, confidence_score)`，glyphs 列表整体不可变（dataclass 嵌套 frozen Glyph），仅整体参与行内排版（§2 原则 2）。行级抽取 `extract_line` 按 StyleRun 边界切片、逐段打分、回填 `para.inline_objects` 与 `para._line_objects`（按行分组）。

### 3.3 锚点保护（§6.1）
`AnchorProtector.protect` 把公式替换为语义无关占位符 `<formula_x>`，`restore` 精确还原，`integrity_score` 计算锚点匹配率（§9.2 要求 100%）。LLM 只看到 `"Let <formula_0> be computable."`，几何由 Layout Solver 计算（§2 原则 4）。

---

## 四、P7/P8/P9 — 统一 Inline 布局与三阶段坐标

### 4.1 P7 Inline Layout（`inline_layout.py`）
`InlineLayoutEngine`：`TranslationUnit` → `LayoutLine[]` 换行。`InlineSegment(kind, text, width, formula_id, baseline_offset)` 把 Text/Formula 并列排版；CJK 字符宽度按 1.0 × 字号、西文 0.5 × 字号估算，`text_width` 兼容 `\n` 换行符。`build_translation_unit` 从 `_line_objects` 按行拼接、行间保留 `\n`（源段落行结构），逐公式注入 `<formula_x>` token 并构建 `formula_map`。

### 4.2 P8 Master Baseline（`baseline.py`）
`BaselineComputer.compute` 以字号加权求 master baseline + line_height；`align_baselines` 返回两行基线差。`TranslationUnit.source_line_baselines` 记录源各视觉行基线（P8 行级求解依据）。

### 4.3 P9 Layout Solver（`solver.py`，§6.2 三阶段坐标）
`solve(unit, translated_text)` 走完整三阶段：

1. `source_bbox` —— 原生 PDF 坐标，不可变（只读不写）；
2. `translated_bbox` —— 按译文长度 + wrap 重排计算逻辑目标坐标；

---

## 六、质量保障与验收（规范书 §9）

### 6.1 单元测试
| 测试文件 | 覆盖 | 用例数 |
| :--- | :--- | :--- |
| `tests/test_reconstruction_p5_p10.py` | Glyph/StyleRun 不可变与划分、幽灵行回归、段落聚合/硬截断、置信度阈值、公式抽取、锚点往返、基线计算、Inline 换行、三阶段坐标、多行恒等零漂移、DualPatch QA、全链路 | 34 |
| `tests/v3/test_v19_reconstruction_sidechannel.py` | 主链路接线：records 产出、开关关闭保持空、QA 快照、side-channel 失败不炸主链路 | 4 |

### 6.2 验收指标实测（`python -m pdf2zh.v3.qa_reconstruction_demo`）
- **§9.1 字体切换/单元比**：`font_switch_count=14`，`ratio=0.0714 < 0.1` ✓（多字体混合段落不碎裂）。
- **§9.1 字符完整性**：Loss Rate = 0.00%（恒等直出）✓。
- **§9.2 公式漂移**：恒等译文 `drift_dx ≤ 0.0pt / drift_dy ≤ 0.127pt`，远低于 0.5pt 容差 ✓。
- **§9.2 锚点匹配率**：`anchor_score=1.0`（100%）✓。
- QA 摘要：`TEXT_OK|DRIFT_OK|ANCHOR_OK`。
- 真实 PDF smoke：fitz 生成混合字体页面 → pdfminer LTChar → 管道 89 字形 / 3 视觉行 / 1 逻辑段落，无异常。

### 6.3 全量回归
`python -m pytest tests -q` → **2223 passed, 1 skipped**（含既有 V8.3–V11 全部测试，无回归）。

---

## 七、运行与消费方式

```python
# 库调用（主链路 side-channel）
from pdf2zh.high_level import translate_patch
v3_output = {}
translate_patch(
    ..., reconstruction_channel=True, v3_output=v3_output,
)
records = v3_output["reconstruction"]   # {page_id: ReconstructionResult.to_dict()}
qa      = v3_output["reconstruction_qa"]  # {page_id: DualPatch QA dict}

# 独立验收脚本（确定性合成 + 真实 PDF smoke）
python -m pdf2zh.v3.qa_reconstruction_demo

# 单测
python -m pytest tests/test_reconstruction_p5_p10.py \
    tests/v3/test_v19_reconstruction_sidechannel.py -q
```

---

## 八、遗留与下一步（P7–P10 深化）

| 项 | 说明 | 建议 |
| :--- | :--- | :--- |
| 1 | `raw_latex_approx` 目前返回 None（未引入完整 LaTeX 解析器） | 可接轻量 math-detector 或第三方 LaTeX OCR 结果 |
| 2 | 译文变长时新增行的行内公式水平夹紧采用「源 x0 + 容器夹紧」 | 可与 CollisionResolver 联合做避让（P3 层协作） |
| 3 | `C_layout` 的 DocLayout 区域类别需要真实布局模型注入 | `ReconstructionPipeline(layout_class_fn=...)` 已预留 |
| 4 | 双层补丁目前为逻辑/渲染指令，未直接调用 MuPDF 写 PDF | 供渲染接管（render_takeover）消费 |

### 8.1 本次迭代完成（4 项遗留全部落地，2026-08-11）

| 项 | 完成交付 | 代码位置 |
| :--- | :--- | :--- |
| 1 | `raw_latex_approx` 不再恒为 None：符号映射表近似（不引入完整解析器，几何不受影响） | `formula/latex_approx.py`；`formula/extractor.py::_approx_latex` 接入 |
| 2 | 公式与译文文本重叠时水平避让：行内顺序推进占用区间，重叠右移、不超容器；恒等译文零漂移保持（`collision_evaded` 标记） | `layout/solver.py::translated_box` |
| 3 | C_layout 真实布局类别注入：`conv.layout[pageid]` DocLayout 掩码中心采样 + 文本启发式兜底 | `v3/layout_class.py`；`mainline_wiring.run_reconstruction_channel` 接线 `layout_class_fn` |
| 4 | 双层补丁 MuPDF 直接落位：行级 `lines` 补丁（公式段空格占位）、`apply_to_pdf` y-up→y-down 坐标转换、`to_overlay_segments`、完成 `render_hybrid` stub | `patch/dual_patcher.py`；`overlay_renderer.py` |

验收：新增 `tests/test_p5p10_remaining.py`（14 用例）覆盖四项；全量回归 `2237 passed / 1 skipped`；demo 新增 `[3/3]` 补丁落位 + LaTeX 近似展示（`apply_to_pdf` 落位 2 个文本对象，hybrid PDF 15.6KB）。


3. `render_bbox` —— 最终绘制坐标（补丁消费端）。

行级基线映射：译文行 i 直接采用源行 i 的 `master_baseline`（恒等或近似映射），译文变长新增行沿用上一源行基线递减 line_height。公式对象级落位锁定**源绝对 x0**（§4.3 几何不可变），仅当超出容器宽度时夹紧到行内，杜绝宽度估算累积漂移。

> **本次迭代修复**：① 原实现 `seg.formula_id`（对象级 id，如 `formula_3600`）与 `formula_map` 的 token key（`<formula_0>`）不匹配，公式落位恒为空 —— 现建立对象 id → token 反查表；② 原实现从行首累计估算宽度定位公式，恒等译文也漂移 Δx≈90pt —— 现改为源 x0 锁定。

---

## 五、P10 — 双层 PDF 补丁与 QA 校验

### 5.1 `dual_patcher.py`
- **逻辑补丁**：`compose_render_patch` 把 `SolvedUnit` 序列化为 `op=text_show` 渲染指令（render_bbox + 文本 + 行结构 + 公式锚点清单）。
- **渲染补丁**：patch 指令携带最终 `render_bbox` 供渲染引擎直接落位。
- **QA（§9.1/§9.2）**：`text_qa`（font-switch ratio + 字符完整性 Loss Rate）、`formula_qa`（Δx/Δy ≤ 0.5pt 漂移容差）、`anchor_qa`（锚点 100% 匹配）、`_summary` 生成 `TEXT_OK|DRIFT_OK|ANCHOR_OK` 摘要。

### 5.2 主链路接线（side-channel）
- `translate_patch` / `translate_stream` 新增 `reconstruction_channel: bool = False` 参数（`high_level.py`）。
- `run_mainline_channels` 在开关下调用 `run_reconstruction_channel`（`mainline_wiring.py`），消费同一 LTChar 流跑完整链路，结果写 `conv.reconstruction_records[pageid]` 与 `conv.reconstruction_qa[pageid]`，由 `high_level` 回传 `v3_output["reconstruction"]` / `v3_output["reconstruction_qa"]`。
- 所有失败仅进 debug 日志，**绝不干扰主链路渲染**（side-channel 契约）。

> **本次迭代修复**：QA 快照写入处 `round(len/switches, 4 if switches>0 else 1.0)` 在无字体切换页（switches=0）除零抛错被吞、QA 恒为空 —— 改为 `switches>0` 时除、否则 ratio=1.0。

### 2.1 Glyph（字形元数据）
`Glyph` 为 `frozen=True` 不可变 dataclass，字段与规范书 §4.1 完全一致（char / bbox / baseline / ascent / descent / font_name / font_size / page_id / object_id）。`extract_glyphs_from_ltpage` 从 pdfminer `LTChar` 提取原生 `bbox`、`get_baseline()`、`fontname`、`fontsize`，并做 `cid:(n):` 前缀剥离与字节解码容错。

### 2.2 StyleRun（同样式连续流）
`build_style_runs` 按 `(font_name, font_size)` 连续分组，逐字形扫描生成 `StyleRun(start_index, end_index, font_name, font_size, bbox)`。字体名经 `normalize_font_name` 规范化（小写、去数字后缀、去 `+` 子集前缀），用于跨 PDF 字体引用比对。

### 2.3 VisualLine（视觉物理行重建，§5.1）
`VisualLineBuilder` 采用**两阶段构建**：

1. **锚定阶段**：按 master baseline 聚簇。对候选锚点（当前 `font_size >= 0.7 × 最大字号`，排除上标/下标），以主簇为锚聚合并行内字形，主簇基线与最大字号加权。
2. **补齐阶段**：把未被合并的上标/下标字形（基线偏移 ≤ 0.6 × 主字号、bbox 垂直重叠）回填到所在行，`bbox` 取并集、`master_baseline` 保持主簇基线。

行内合并三重判定（规范书 §5.1）：基线差 < 0.35 × 字号中位数；垂直重叠率 ≥ 0.60；水平间距 ≤ max(2.5 × 字号, gap 阈值)。

> **本次迭代修复**：原实现把上标小字号字形当作独立行锚，导致 `x²+1` 被裂成「幽灵行」（`x` + 单独成行的 `2`）。修复后上标/下标字形并入主行，数学行保持单一物理行。

### 2.4 LogicalParagraph（逻辑自然段聚合，§5.2）
`build_logical_paragraphs` 对连续 VisualLine 聚合：行间距稳定性 + 首行缩进/对齐容差（≤ 2.0pt）正向聚合；块级障碍物分割、垂直间距 > 1.8 × line_height 硬截断。**字体/字号切换永不作为段落边界**（样式与语义解耦，§2 原则 1）。
