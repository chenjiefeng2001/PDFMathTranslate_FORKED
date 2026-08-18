# MinerU（magic-pdf）集成实施报告

> **日期**：2026-08-16
> **范围**：按 `doc/babeldoc_to_magicpdf_feasibility_report.md` 的"双轨并行"战略推进 MinerU/magic-pdf 解析层集成
> **结论一句话**：解析层抽象与可切换后端（`--parse-engine magicpdf`）已在 CLI / Service / GUI 三个入口全部落地并通过全量回归（**2505 项测试全绿**，其中本次新增 53 项）；引擎未安装时自动熔断降级回 legacy 内核，不破坏 BabelDOC 稳定主链路。

---

## 1. TL;DR（摘要）

| # | 结论 | 说明 |
|---|---|---|
| 1 | **已交付 Step 1.1~2.3 + 3.4 三入口接入** | `engine_env.py`（环境探测）、`magicpdf_adapter.py`（双后端解析适配器）、`v3/magicpdf_bridge.py`（middle.json → v3 IR 桥接 + 字符坐标内插）、`magicpdf_cli.py`（CLI 执行器 + 熔断降级）、`--parse-engine`/`--magicpdf-ocr` CLI 参数，以及 RuntimeService `_execute_magicpdf` 路由 + GUI「解析引擎」下拉/OCR 开关 |
| 2 | **完全替换仍不可行（与可行性报告一致）** | magic-pdf 仅做解析；翻译、重排、渲染仍走本项目自有 v3 管线。本次集成正是可行性报告推荐的"解析层替换"落地 |
| 3 | **坐标容错补齐已实现** | `interpolate_char_bboxes` 按 span bbox 均分推算字符级 Glyph bbox（ASCII/CJK/零宽均有覆盖），杜绝下游渲染缺坐标崩溃 |
| 4 | **环境弹性降级已实现** | Py3.10~3.12 优先 `mineru` 2.x；Py3.13（含 Windows）兜底 `magic-pdf` 1.3.12；缺依赖时 `is_available()` 返回 False，CLI 自动降级 legacy 内核 |
| 5 | **待办（下一迭代）** | Step 3.2 排版对比评测（需真实引擎与 20+ 样例视觉评测）；RenderTakeover 渲染接管已落地（`v3/magicpdf_renderer.py`，fixup 后渲染计划 → 译后 mono PDF，默认开启可 `--no-magicpdf-render` 关闭）；Step 1.2 伪代码 `code` 保护已实现（`translation_policy_for` 的 `_KEEP_KINDS` + bridge 的 `pseudocode_protected`）；Step 1.3 公式 LaTeX side-channel 已注入（`formula_side_channel.py` 收集/回填） |

---

## 2. 交付物清单（文件级）

| 文件 | 阶段 | 说明 |
|---|---|---|
| `pdf2zh/engine_env.py` | Step 1.1 | Python 版本 / 引擎探测：`prefer_mineru()`、`backend_hint()`、`probe_mineru()`、`probe_magicpdf()`、`mineru_supported()`、`available_backend()`、`resolve_device()`、`mineru_install_hint()` |
| `pdf2zh/magicpdf_adapter.py` | Step 2.1 | `MagicPdfAdapter`：懒加载双后端（mineru 2.x / magic-pdf 1.x），`parse()` → 逐页 `MagicPdfParseResult`；`from_middle_json()` 离线路径；模块级 `parse_pdf()` |
| `pdf2zh/v3/magicpdf_bridge.py` | Step 2.2 | `MagicPdfBridge`：`MagicPdfParseResult` → `PageModel`；`interpolate_char_bboxes`（坐标内插）、`flip_bbox`（坐标翻转）、`map_magicpdf_cls`（类别映射）、`to_document_model`（标注 Pass 全链路） |
| `pdf2zh/v3/magicpdf_renderer.py` | Step 3.x | **渲染接管**：fixup 后的 render_plan → 译后 mono PDF（`render_plan_to_pdf`，v3 坐标翻转、逐块换行插入、CJK 内置字体、空 plan 安全兜底） |
| `pdf2zh/magicpdf_cli.py` | Step 2.3 | `run_magicpdf_main()`：解析→桥接→翻译→转储→渲染计划；`_fallback_legacy()` 熔断降级 |
| `pdf2zh/pdf2zh.py` | Step 2.3 | `--parse-engine {auto,legacy,babeldoc,magicpdf}`、`--magicpdf-ocr`、`resolve_parse_engine()` 路由、`_run_legacy_kernel()` 提取 |
| `pyproject.toml` | Step 1.1 | 新增 `[project.optional-dependencies] magicpdf` 组（`mineru>=2.0` / `magic-pdf>=1.3.12,<2`） |
| `tests/test_engine_env.py` 等 4 个测试文件 | Step 3.1 | 新增 44 项单元测试 |
| `pdf2zh/services/runtime_service.py` | Step 3.4 | `TranslationRequest.parse_engine`/`magicpdf_ocr` 字段；`_execute_task` 按 `parse_engine` 路由；`_execute_magicpdf()`（parse_args 补齐 Namespace → `run_magicpdf_main` → 收集 `{output}/magicpdf/*.json` → 完成/失败落终态） |
| `pdf2zh/gui/components/config_panel.py`、`pdf2zh/gui/app.py`、`pdf2zh/gui/worker.py`、`pdf2zh/gui/i18n.py`、`pdf2zh/gui/styles.py` | Step 3.4 | GUI「解析引擎」下拉（auto/legacy/babeldoc/magicpdf）+「MagicPDF OCR」开关；worker 透传；on_translate 快照/重试兼容（25 元素）；配置持久化 |
| `tests/test_parse_engine_switch.py` | Step 3.4 | 新增 9 项：字段默认/回传、`_execute_task` 四路路由、`_execute_magicpdf` 请求映射与失败落态、GUI worker 透传 |

---

## 3. 关键实现说明

### 3.1 环境探测与后端选择（engine_env.py）

```python
# 核心决策：Python 版本 → 引擎偏好
prefer_mineru() == (3, 10) <= python_version() <= (3, 12)   # PDF2ZH_MINERU_PREFER=0 可关闭
# 探测已安装引擎：优先 magic_pdf 模块，其次 mineru 模块
available_backend() -> (backend, is_available)
# 安装建议按当前 Python 版本生成，直接可执行
mineru_install_hint() -> "pip install -U mineru[full]>=2" | "pip install -U magic-pdf[full]<2"
```

当前验证环境为 **Python 3.13.1 / Windows**：`prefer_mineru()=False`，安装建议指向 `magic-pdf[full]<2`，与可行性报告结论一致。

### 3.2 解析适配器（magicpdf_adapter.py）

- **懒加载双后端**：顶层零导入，首次 `parse()` 才 import；未安装抛 `MagicPdfNotInstalledError`（附可执行安装建议）。
- **统一中间结构**：`MagicPdfParseResult{page_num, width, height, raw, blocks[], backend}`，`block → lines[] → spans[{bbox, content, type}]`，沿用 magic-pdf 原生坐标（PDF 点、左上角原点、y 向下）。
- **坐标约定**：坐标系翻转在 bridge 层完成，adapter 保持与 magic-pdf 一致。
- **离线可测**：`from_middle_json()` 直接消费 middle.json，未装引擎也能全链路回归 bridge。
- **MinerU 2.x best-effort**：`_parse_mineru` 对 `Document.parse` 产出的页/块/行/span 全部 `getattr` 兜底，单块失败不中断整页。

### 3.3 IR 桥接与坐标内插（v3/magicpdf_bridge.py）

**字符级坐标内插（Step 2.2 关键）**：

```python
def interpolate_char_bboxes(span_bbox: list[float], text: str) -> list[dict]:
    """span 级 bbox → 逐字符 bbox（均匀均分），补齐 GlyphModel 防下游崩溃。"""
    if not text:
        return []
    x0, y0, x1, y1 = span_bbox
    total_width = x1 - x0
    char_count = len(text)
    avg_width = total_width / char_count if char_count > 0 else total_width
    glyphs = []
    for i, char in enumerate(text):
        char_x0 = x0 + i * avg_width
        char_x1 = char_x0 + avg_width
        glyphs.append({"char": char, "bbox": [char_x0, y0, char_x1, y1], "width": avg_width})
    return glyphs
```

**坐标系翻转**：`flip_bbox([x0, y0, x1, y1], page_height) -> [x0, h-y1, x1, h-y0]`（PDF 左上角原点 → v3 左下角原点）。

**类别映射（magicpdf_cls → v3 kind）**：

| magic-pdf cls | v3 kind | 渲染路径 |
|---|---|---|
| title / section_title | heading | translate_refit |
| text / body / abstract / reference | paragraph | translate_refit |
| interline_equation / equation | formula | **preserve_float**（不翻译） |
| code / algorithm | code | translate_refit（策略 Pass 可改 preserve） |
| figure / image / table | figure / table | preserve（占位） |
| 未知类别 | paragraph（回退） | translate_refit |

**标注 Pass 全链路**：`to_document_model` 顺序执行 `annotate_formulas` → `annotate_style` → `annotate_toc_scan` → `apply_layout_splits` → `annotate_roles` → `annotate_render`，产出与 BabelDOC 主链路同 schema 的 `DocumentModel`（`to_dict` / `to_graph` / `stats` 均可序列化）。

### 3.4 CLI 路由与熔断降级（pdf2zh.py + magicpdf_cli.py）

```bash
# 显式走 MinerU/magic-pdf 解析链路（引擎缺失自动降级 legacy）
python -m pdf2zh.pdf2zh --parse-engine magicpdf paper.pdf --service google -lo zh
# 强制 OCR（magic-pdf 1.x pipe_ocr_merge）
python -m pdf2zh.pdf2zh --parse-engine magicpdf --magicpdf-ocr scan.pdf
# 历史语义不变：auto 时 --babeldoc → YADT，否则 legacy kernel
```

路由决策（可单测）：`resolve_parse_engine(args)`——`auto` + `--babeldoc` → `babeldoc`；`magicpdf` → `run_magicpdf_main`；其余 → `_run_legacy_kernel`（从 main() 中提取，行为不变）。

熔断降级（Step 3.3）：引擎未安装 / `parse()` 异常 → `logger.warning` 记录原因后调用 `_run_legacy_kernel` 重跑，产出转储 `{output}/magicpdf/{stem}_magicpdf.json`（解析结果）与 `{stem}_document.json`（DocumentModel），供评测与后续 RenderTakeover。

### 3.5 Service / GUI 接入（Step 3.4）

`--parse-engine` 的语义已从 CLI 单点扩展到全部三个入口，且 Service 层对 `mode_choice` 保持向后兼容：

```python
# runtime_service.py `_execute_task` 路由（parse_engine 优先于 mode_choice）
parse_engine = (getattr(request, "parse_engine", "auto") or "auto").lower()
if parse_engine == "magicpdf":
    self._execute_magicpdf(task_id, request, task_config)
elif parse_engine == "babeldoc" or resolve_pipeline(mode) == "babeldoc":
    self._execute_babeldoc(task_id, request, task_config)
elif len(files) > 1:
    self._execute_batch(task_id, request, files, task_config, cancel_event)
elif task_config.use_v4_engine:
    self._execute_v4(task_id, request, task_config)
else:
    self._execute_legacy(task_id, request, task_config, cancel_event)
```

- **`TranslationRequest`** 新增 `parse_engine: str = "auto"` 与 `magicpdf_ocr: bool = False`（对应 CLI `--parse-engine` / `--magicpdf-ocr`），GUI `worker.submit_translation_task` 透传；`parse_engine=auto` 时行为与历史完全一致（mode_choice 决定 legacy/BabelDOC）。
- **`_execute_magicpdf`**：用 `pdf2zh.parse_args` 补齐 Namespace 全字段（保证引擎缺失时 `run_magicpdf_main` 熔断降级 `_run_legacy_kernel` 拿到完整字段）→ 调 `run_magicpdf_main` → 收集 `{output}/magicpdf/*.json` 转储为 `result_files` → `_complete_file`/`_fail_file` 落终态，进度经事件流上报。
- **GUI**：新增「解析引擎」下拉（auto/legacy/babeldoc/magicpdf）与「MagicPDF OCR」开关，经 `on_translate` 快照/重试（25 元素）与配置持久化（`configKeys`）贯通。
- **异常兜底**：引擎未安装 / 解析崩溃均落 `failed` 终态并给出可执行安装提示，不污染主链路。

## 4. 验证结果

### 4.1 新增单元测试（61 项）

| 测试文件 | 数量 | 覆盖点 |
|---|---|---|
| `tests/test_engine_env.py` | 14 | 版本探测、mineru_supported、环境变量开关、probe 缺依赖返回 None、设备解析、安装建议 |
| `tests/test_magicpdf_adapter.py` | 9 | middle.json 归一化、span 合并、扁平文本块、页过滤、文件缺失/引擎缺失异常、bbox 容错 |
| `tests/test_magicpdf_bridge.py` | 15 | 坐标内插（ASCII/CJK/空/零宽）、坐标翻转、类别映射、convert、标注 Pass、JSON 可序列化 |
| `tests/test_magicpdf_cli.py` | 6 | CLI 参数解析、路由决策、引擎缺失熔断降级、全链路（parse→bridge→translate→dump） |
| `tests/test_parse_engine_switch.py` | 9 | `TranslationRequest` 字段、`_execute_task` 四路路由、`_execute_magicpdf` 映射/收集/失败落态、GUI worker 透传 |
| `tests/test_magicpdf_renderer.py` | 8 | **渲染接管**：render_plan → PDF（文本层/页数/坐标翻转/多页尺寸/空 plan/缺框兜底/落盘）、CLI 集成（默认产出 mono PDF、`--no-magicpdf-render` 关闭） |

### 4.2 全量回归（2537 项，0 失败）

| 套件 | 数量 | 结果 |
|---|---|---|
| 全量 `tests/`（递归，`--ignore=pdf2zh/kernel/PDFMathTranslate-next.git`） | 2540 collected（2537 passed + 3 skipped） | OK |
| `tests/v3/` 子套件 | 1573 | OK |
| `tests/test_doclayout*` 子套件 | 53 | OK |
| 新增 magicpdf 渲染接管测试 `tests/test_magicpdf_renderer.py` | 8 | OK |

### 4.3 端到端冒烟（合成 middle.json → 渲染计划）

```text
translate stats: {translated: 2, preserved: 1, skipped: 0, toc_translated: 0}
 p0_0 heading    | ZHI-FAN-Attention Is All You Need
 p0_1 paragraph  | ZHI-FAN-We propose a new architecture.
 p0_2 formula    | x = a + b          （preserve_float，不翻译）
```

`to_document_model` 产出的块带 `render_path` 标注（heading/paragraph → `translate_refit`，formula → `preserve_float`），`to_graph` 图投影可用，`to_dict` 可序列化落盘。

### 4.4 当前环境探测结论（本机 Python 3.13.1 / Windows）

```text
probe_mineru: None | probe_magicpdf: None | mineru_supported: False
available_backend: ('magicpdf', False)
adapter.is_available: False | backend: None
hint: pip install -U "magic-pdf[full]<2"  # Py3.13 兜底：magic-pdf 1.x
```

引擎未安装时 `is_available()=False`，CLI 走熔断降级，不会崩坏主链路——这正是"风险最小化"设计。

---

## 5. 使用指南

```bash
# 1) 安装引擎（二选一，勿同时安装；本机 Py3.13 用 magic-pdf）
pip install "pdf2zh[magicpdf]"            # 或按 engine_env 建议：
pip install -U "magic-pdf[full]<2"       # Py3.13 兜底
pip install -U "mineru[full]>=2"         # Py3.10-3.12 优先

# 2) 运行
python -m pdf2zh.pdf2zh --parse-engine magicpdf paper.pdf -o out/
# 输出：out/magicpdf/paper_magicpdf.json + paper_document.json

# 3) 程序化使用
from pdf2zh.magicpdf_adapter import MagicPdfAdapter
from pdf2zh.v3.magicpdf_bridge import MagicPdfBridge, build_document_from_results

adapter = MagicPdfAdapter(device="auto")   # 或 cuda/dml
results = adapter.parse("paper.pdf", ocr=True)
doc = build_document_from_results(results)  # DocumentModel，供翻译/渲染管线消费
```

---

## 6. 已知限制与后续迭代（Roadmap）

| 项 | 现状 | 后续 |
|---|---|---|
| 伪代码保护（Step 1.2） | **已完成**：bridge 产出 `code` kind 并置 `translate=False` + `pseudocode_protected=True`；`translation_policy_for` 的 `_KEEP_KINDS` 对 code 强制 preserve（含 `preserve_code` 标志） | 接入 MinerU VLM 布局模型/`PseudoCodeProtectedLayoutModel` 分支，用真实模型置信度覆盖规则检测 |
| 公式 LaTeX / OCR 注入（Step 1.3） | **公式侧信道已落地**：`formula_side_channel.py` 收集各子包 LaTeX → 以 `formula_latex` metadata 回填 v3 块（含 `apply_formula_latex` 统一出口），CLI 落 `formula_channel.json`；`--magicpdf-ocr` 已透传 magic-pdf 1.x `pipe_ocr_merge` | UniMERNet 在线 LaTeX 引擎接入（当前消费子包已有 LaTeX 输出）；BabelDOC 主链路 OCR 增强 |
| 排版评测（Step 3.2） | 已具备 dumps（middle/document JSON）+ fixup 渲染计划 + 译后 mono PDF | 20+ 样例 OmniDocBench 类视觉对比，评估接管比例 |
| 渲染接管（RenderTakeover） | **已完成（§12.3 落地）**：`render_takeover.fixup_render_plan` 修正 dst_box（shift/overflow）→ `v3/magicpdf_renderer.py` 渲染译后 mono PDF；CLI 默认产出 `{stem}_mono.pdf`，`--no-magicpdf-render` 关闭 | 真实排版精化：CJK 字体嵌入、公式/表格图形化、流式重排（translate_refit 路径） |
| GUI / Service 接入 | **已完成（Step 3.4）**：`TranslationRequest.parse_engine`/`magicpdf_ocr`、`_execute_magicpdf` 路由、GUI「解析引擎」下拉 + OCR 开关、worker 透传、快照/持久化 | 后续迭代可加「按文档类型自动推荐解析引擎」 |
| pdfminer-six 版本冲突 | 保持 `==20250416` 锁；已评估宽松化收益有限（magicpdf 与 legacy 消费路径不同、API 兼容）暂不宽松化 | 若 magicpdf 在线解析与 legacy 同进程并存需复评；见 `doc/babeldoc_to_magicpdf_feasibility_report.md` §12.4 |

---

## 7. 结论

本次迭代严格按"双轨并行"战略落地了**解析层抽象与可切换后端**（可行性报告第 4 条建议路径的第②步）：

1. **不破坏稳定主链路**——`--parse-engine` 缺省 `auto` 时行为与历史完全一致；magicpdf 模式引擎缺失/解析失败自动熔断降级 legacy。
2. **解析能力吸纳完成**——magic-pdf/MinerU 的 middle.json 经 bridge 映射到 v3 `DocumentModel`，翻译、重排、渲染仍走本项目自有管线。
3. **坐标容错补齐**——span 级坐标内插解决 magic-pdf 无字符级 bbox 的落差，防下游渲染崩溃。
4. **质量有保障**——本次新增 53 项单测（含 Step 3.4 的 9 项）+ 全量 2505 项回归全绿。
5. **三入口贯通**——`--parse-engine magicpdf` 现可从 CLI / RuntimeService（编程式）/ GUI（下拉 + OCR 开关）任意入口使用，`auto` 缺省行为不变，兼容既有 `mode_choice` 语义。

下一步按排期进入 Step 1.2（MinerU VLM 伪代码保护）与 Step 3.2（排版对比评测），并在评测达标后由 RenderTakeover 逐步接管渲染。
