# BabelDOC → magic-pdf（MinerU）替换可行性分析报告

> **日期**：2026-08-16
> **范围**：PDFMathTranslate_FORKED（pdf2zh 1.9.12，BabelDOC 0.6.4）允许切换 BabelDOC 为 magic-pdf / MinerU 的可行性分析
> **结论一句话**：BabelDOC 是"解析 + 翻译 + 排版 + 渲染"一体化引擎，magic-pdf / MinerU 是纯"文档解析"引擎（PDF → Markdown/JSON），**两者不具备对等替换关系**——完全替换不可行；可行的边界是"解析层"替换（magic-pdf 输出喂给本项目自有翻译/排版管线），以及"解析能力增强"（公式 LaTeX / OCR / 伪代码 `code` 类别）。

---

## 1. TL;DR（摘要）

| # | 结论 | 说明 |
|---|---|---|
| 1 | **完全替换 BabelDOC 不可行** | magic-pdf 只产出 Markdown/JSON，无翻译、无 PDF 排版渲染能力；BabelDOC 的核心价值（翻译后 mono/dual PDF、逐字排版回填）无法被 magic-pdf 替代 |
| 2 | **解析层替换有条件可行** | magic-pdf 的 `middle.json`（`block → line → span` + bbox + type）可映射到本项目自有 IR（`v3/canonical_page.py` 的 `PageModel→BlockModel→LineModel→SpanModel`）或 legacy 字符流，但存在**字符级坐标缺失**这一关键落差 |
| 3 | **解析能力增强最可行** | MinerU 2.x VLM 后端自带 `code`/`algorithm` 类别（根治伪代码误翻译问题）、UniMERNet 公式 LaTeX、PaddleOCR 109 语言——恰好覆盖本项目当前用 monkey-patch + 融合模型补偿的三大痛点 |
| 4 | **工程阻力集中在依赖与环境** | 本机 Python 3.13.1：`magic-pdf` 1.3.12 支持 3.10–3.13（需 torch≥2.6 for py3.13），`mineru` 2.x 在 Windows 仅支持 3.10–3.12（`ray` 依赖）；且 `pdfminer-six` 版本冲突（本项目锁 `==20250416`，magic-pdf 要求 `==20250506`） |
| 5 | **建议路径** | 三步走：① 先落地"解析增强"（不动 BabelDOC 主链路）→ ② 实现 `--parse-engine magicpdf` 可切换解析后端，输出映射到 v3 IR，翻译/排版走自有管线 → ③ 用 OmniDocBench 类评测决定是否整体接管解析阶段 |

---

## 2. 背景与目标

本仓库（PDFMathTranslate_FORKED）在 pdf2zh 1.9.x 之上深度集成了 BabelDOC 0.6.4（即 PDFMathTranslate-Next / YADT 引擎）作为可选布局引擎，并围绕它做了大量 fork 侧修复（GPU 后端、OCR 三态、列表拆分、目录保护、伪代码保护等）。

目标：评估"把 BabelDOC 切换为 magic-pdf（MinerU）"的可行性——包括完全替换、解析层替换、解析能力增强三个层次，并给出落地建议。

---

## 3. 现状盘点：BabelDOC 在本项目中的集成深度

### 3.1 BabelDOC 承担的角色

BabelDOC 在本项目中被当作**完整的文档翻译引擎**：

1. **解析**：PDF → 字符级中间表示（document_il，`PdfCharacter/PdfLine/PdfParagraph`，含每字符 bbox / 字体 / 字号 / advance）；
2. **版面分析**：DocLayout-YOLO ONNX（10 类 docstructbench 模型）输出布局类别；
3. **表格 / 公式 / 段落**：TableParser、StylesAndFormulas、ParagraphFinder；
4. **翻译**：ILTranslator 逐段调用 pdf2zh 的翻译引擎（经 `make_babeldoc_translator` 桥接）；
5. **排版 + 渲染**：Typesetting（重排）→ FontMapper（加字体）→ PDFCreater（生成绘制指令）→ 子集化字体 → 保存 mono/dual PDF。

### 3.2 管线阶段（BabelDOC `high_level.py` `TRANSLATE_STAGES`，权重为进度占比）

| # | 阶段 | 占比 | 说明 |
|---|---|---|---|
| 1 | Parse PDF and Create Intermediate Representation | 14.12% | 字符级 IR |
| 2 | DetectScannedFile | 2.45% | 扫描版检测 |
| 3 | Parse Page Layout | 14.03% | doclayout ONNX 推理 |
| 4 | Parse Table | 1.0% | 表格 |
| 5 | Parse Paragraphs | 6.26% | 段落分组 |
| 6 | Parse Formulas and Styles | 1.66% | 公式/样式 |
| 7 | Extract Terms | 30.0% | 术语抽取（本项目关闭）|
| 8 | Translate Paragraphs | 46.96% | 翻译（大头）|
| 9 | Typesetting | 4.71% | 排版重排 |
| 10 | Add Fonts | 0.61% | 字体映射 |
| 11 | Generate drawing instructions | 1.96% | 绘制指令 |
| 12 | Subset font | 0.92% | 字体子集化 |
| 13 | Save PDF | 6.34% | 输出 mono/dual |

### 3.3 数据模型（本项目排版质量的关键）

- `babeldoc/format/pdf/document_il/il_version_1.py`：
  - `PdfCharacter`：`pdf_style`（font_id/font_size/graphic_state）、`box`、`visual_bbox`、`char_unicode`、`advance`、`vertical`、`scale`、`formula_layout_id`、`render_order`…
  - `PdfLine`：`box` + `pdf_character[]`；
  - `PdfParagraphComposition`：`pdf_line | pdf_formula | pdf_same_style_characters | pdf_character…`；
  - `PdfParagraph`：`box` + `pdf_paragraph_composition[]` + `layout_label` / `layout_id`。
- **字符级 bbox 是"保排版"能力的根基**：翻译后重排以每个字符为锚点。

### 3.4 fork 侧集成点（文件清单）

| 文件 | 作用 | 与 BabelDOC 的耦合 |
|---|---|---|
| `pdf2zh/babeldoc_adapter.py` | legacy 适配器：`make_babeldoc_translator`（翻译桥）+ `run_babeldoc_translation`（事件流驱动）+ `_build_doclayout_model` | 直连 `babeldoc.format.pdf.high_level.async_translate` / `TranslationConfig` |
| `pdf2zh/babeldoc_next_adapter.py` | GUI 主路径：映射到 kernel `pdf2zh_next.SettingsModel` + `create_babeldoc_config` | 直连 kernel submodule `pdf2zh/kernel/PDFMathTranslate-next.git` |
| `pdf2zh/babeldoc_onnx_backend.py` | monkey-patch BabelDOC 内部 `OnnxModel.__init__`，使版面推理走 CUDA/DML | 直连 `babeldoc.docvision.doclayout.OnnxModel` |
| `pdf2zh/babeldoc_ocr_mode.py` | `auto/on/off` 三态 → `ocr_workaround / auto_enable_ocr_workaround / skip_scanned_detection` | 直连 `TranslationConfig` 字段 |
| `pdf2zh/babeldoc_list_split.py` | monkey-patch `ParagraphFinder.process_independent_paragraphs`，编号列表拆段 | 直连 `babeldoc...midend.paragraph_finder` |
| `pdf2zh/babeldoc_toc_protect.py` | monkey-patch `ParagraphFinder.process_page`，目录点线/页码公式保护 | 直连 `babeldoc...midend.paragraph_finder` + `pdf2zh.toc` |
| `pdf2zh/doclayout.py` | 独立 doclayout ONNX 推理器（后端开关、GPU 降级、动态 batch） | 复用 `babeldoc.assets.get_doclayout_onnx_model_path` |
| `pdf2zh/doclayout_pseudocode.py` | PP-DocLayoutV2 融合模型（`algorithm` 保护）实现 `DocLayoutModel` 接口 | 直连 `babeldoc.docvision.doclayout` |

### 3.5 入口点

- **CLI**：`pdf2zh --babeldoc` → `yadt_main()`（`pdf2zh/pdf2zh.py:435-436`）；
- **GUI / 服务**：`RuntimeService._execute_babeldoc()`（`pdf2zh/services/runtime_service.py:1649`）→ 优先 `run_babeldoc_next_translation`（next kernel），失败回退 `run_babeldoc_translation`（legacy）；
- **测试**：`tests/test_babeldoc_*.py`、`tests/test_doclayout*.py`、`tests/test_onnx_backend_switch.py` 等约 10 个文件直接依赖 babeldoc 内部符号。

### 3.6 现有痛点（"为什么想换"的动机）

1. **伪代码被翻译**：默认 10 类模型无 `algorithm` 类 → 伪代码被识别为 `plain text`（见 `doc/babeldoc_pseudocode_mistranslation_report.md`），靠 `doclayout_pseudocode.py` 额外引入 204MB 的 PP-DocLayoutV2 融合模型兜底；
2. **版面推理 CPU-only**：BabelDOC 内部 `OnnxModel` 硬编码 CPU，靠 `babeldoc_onnx_backend.py` monkey-patch 补救；
3. **解析细节需持续打补丁**：编号列表拆段、TOC 点线保护都是 fork 侧 monkey-patch，上游升级即失效风险；
4. **扫描 PDF 依赖 OCR workaround**：`TranslationConfig.ocr_workaround` 机制较重，OCR 质量与速度受制于外部；
5. **单一模型依赖**：`babeldoc/assets/assets.py` 固定 10 类 docstructbench 模型，公式/表格识别能力一般。

---

## 4. magic-pdf / MinerU 能力盘点

### 4.1 项目定位与版本

| 项目 | 定位 | 版本 | 维护状态 | 许可 |
|---|---|---|---|---|
| `magic-pdf`（MinerU 1.x 核心库） | PDF → Markdown/JSON 解析 | 1.3.12（2025-05-24） | **已停更**（转向 MinerU 2.x） | AGPL-3.0 |
| `mineru`（MinerU 2.x） | 文档解析引擎（PDF/DOCX/PPTX/XLSX/图片） | 2.x（活跃） | 活跃，77.7k stars | MinerU Open Source License（Apache-2.0 + 附加条款） |

magic-pdf 是 MinerU 1.x 的核心解析库；MinerU 2.x 将包名改成了 `mineru`。用户所说"magic-pdf"指代的是 MinerU 解析体系，下文同时覆盖两代。

### 4.2 Python API

**magic-pdf 1.x**（MinerU 1.x 时代，`magic_pdf/data/dataset.py` + `model/doc_analyze_by_custom_model.py`）：

```python
from magic_pdf.data.data_reader_writer import FileBasedDataWriter, FileBasedDataReader
from magic_pdf.data.dataset import PymuDocDataset
from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze

data_reader = FileBasedDataReader("")
pdf_bytes = data_reader.read(src_pdf)
dataset = PymuDocDataset(pdf_bytes)
model_json = doc_analyze(dataset, ocr=False, lang="", backend="onnx")  # 布局+OCR+公式+表格
pipe_result = dataset.pipe_txt_meta(model_json)
pipe_result.dump_content_list(image_writer, "content_list.json", out_dir)
pipe_result.dump_middle_json(image_writer, "middle.json")
pipe_result.dump_model_json(image_writer, "model.json")
```

**MinerU 2.x**（`mineru` 包）：

```python
from mineru import MinerU
m = MinerU(output_dir="out", model="pipeline-paddle", method="auto")  # method: auto/txt/ocr
m.predict(pdf_path)
```

后端模型：`pipeline-paddle`（纯 CPU 可跑、精度 86.47）、`pipeline-mine-pdf`（需 GPU ≥4GB）、`pipeline-vl`（VLM，GPU ≥8GB，精度最高 95+）。

### 4.3 输出 JSON 结构（MinerU 2.x / magic-pdf 1.x）

| 文件 | 内容 |
|---|---|
| `*_model.json` | 布局模型原始输出：`[{cls_id, label, score, bbox, index}]` |
| `*_middle.json` | `{pdf_info:[{preproc_blocks, page_idx, page_size, images, tables, interline_equations, discarded_blocks, para_blocks}]}`，块结构见下 |
| `*_content_list.json` | Markdown 结构化最终结果（`text/image/table/equation`，VLM 后端含 `code(sub_type=algorithm)`、`list`、`header/footer/page_number/aside_text/page_footnote` 等） |

**块结构层次（middle.json）**：

```
一级块 (table | image | chart)         bbox + blocks[]
└── 二级块 (text|title|index|list|interline_equation|image_body|image_caption|…)
    └── 行 line                        bbox + spans[]
        └── 片段 span                  bbox + type(content: text|inline_equation|interline_equation) + content/image_path
```

- 页面级别：`page_idx`（从 0）、`page_size [width, height]`；
- **所有坐标只有 span 级 bbox，无逐字符坐标**。

### 4.4 模型体系

| 能力 | 模型 | 说明 |
|---|---|---|
| 版面检测 | DocLayout-YOLO（magic-pdf full）/ 各 pipeline 自带 | 类别含 `text/title/figure/table/equation/image/abandon/header/footer/page_number/aside_text/page_footnote`；VLM 后端另有 `code(sub_type=algorithm)`、`list` |
| OCR | PaddleOCR（PP-OCRv4/v5，109 语言） | 扫描件、手写体；`ch/ch_server/ch_lite/...` |
| 公式识别 | UniMERNet → LaTeX | 行内/行间公式，输出 LaTeX（对"公式占位保护"是强项） |
| 表格 | rapid-table / TableStructureRec → HTML | |

### 4.5 关键结论（能力边界）

- ✅ **解析强**：布局、OCR、公式 LaTeX、表格结构化、阅读顺序、多栏；
- ❌ **无翻译**：不调用任何翻译引擎；
- ❌ **无 PDF 排版渲染**：不产出翻译后的 PDF（只输出 `*_layout.pdf`/`*_span.pdf` 这类可视化调试 PDF）；
- ⚠️ **无字符级坐标**：只有 span 级 bbox + text + type。

---

## 5. 关键能力对比矩阵

| 维度 | BabelDOC 0.6.4（本项目现状） | magic-pdf 1.3.12 | MinerU 2.x（mineru） |
|---|---|---|---|
| 定位 | 翻译 + 排版引擎 | 解析引擎 | 解析引擎 |
| 最终产物 | 翻译后 mono/dual PDF | Markdown / JSON | Markdown / JSON |
| 字符级坐标（排版锚点） | ✅ `PdfCharacter`（bbox/字体/字号/advance） | ❌ 无 | ❌ 无 |
| 版面检测模型 | 10 类 docstructbench ONNX（缺 algorithm） | DocLayout-YOLO 多类 | pipeline 系列 / VLM 多类 |
| 伪代码/代码块保护 | 需 PP-DocLayoutV2 融合补丁 | — | VLM 后端有 `code/algorithm` 类别 ✅ |
| 公式识别 | `isolate_formula` 类别 + 占位保护（无 LaTeX） | UniMERNet → LaTeX ✅ | 同左 ✅ |
| 扫描 PDF/OCR | 扫描检测 + `ocr_workaround`（较重） | PaddleOCR 109 语言 ✅ | 同左 ✅ |
| 表格 | TableParser | rapid-table → HTML ✅ | 同左 ✅ |
| 阅读顺序/多栏 | ParagraphFinder | ✅ | ✅ |
| 翻译 | ✅（经 pdf2zh 引擎） | ❌ | ❌ |
| PDF 排版渲染 | ✅ Typesetting+PDFCreater+子集化 | ❌ | ❌ |
| 字体处理 | ✅ FontMapper / 远程字体 / 子集化 | ❌ | ❌ |
| Python 支持 | ≥3.11,<3.14（本项目锁定） | ≥3.10,<3.14 | **Windows 仅 3.10–3.12**（ray 依赖），Linux/macOS 3.10–3.13 |
| 依赖栈 | onnxruntime / pymupdf / pdfminer（轻） | torch / transformers / paddle（重） | 更重（full 磁盘 20GB+，内存 16–32GB） |
| 许可 | AGPL | AGPL-3.0 | Apache-2.0 + 附加条款 |
| 维护状态 | 活跃 | 停更 | 活跃 |

---

## 6. 可行性分析（三个层次）

### 6.1 完全替换 —— ❌ 不可行

- magic-pdf/MinerU 无 `ILTranslator`、无 `Typesetting`、无 `PDFCreater`、无 `FontMapper`。BabelDOC 中占比最高的阶段（Translate 46.96% + Typesetting 4.71% + 渲染 10%）在 magic-pdf 中不存在；
- 输出形态完全不同（PDF vs Markdown/JSON），无法满足"翻译后保留排版 PDF"这一产品核心；
- **结论**：magic-pdf 不是 BabelDOC 的同类替代品，不存在"把 BabelDOC 换成 magic-pdf"这一等价切换。

### 6.2 解析层替换 —— ⚠️ 有条件可行（工作量集中在数据映射）

目标：用 magic-pdf 替代 BabelDOC 的**解析阶段**（管线 1–6：IR 构建 / 扫描检测 / 版面 / 表格 / 段落 / 公式），翻译 + 排版 + 渲染继续走本项目自有管线。

**三个候选接入点**：

| 接入点 | 映射目标 | 适配难度 | 说明 |
|---|---|---|---|
| **A. v3 管线** | `v3/canonical_page.py` 的 `PageModel→BlockModel→LineModel→SpanModel` | 中 | 结构与 magic-pdf `block→line→span` 天然同构；但 `GlyphModel`（字符级）为空，且 v3 管线默认 `use_v4_engine=False` 未接管主链路 |
| **B. legacy converter** | `TranslateConverter.receive_layout()` 消费的 LTChar 流 | 中-高 | `converter.py` 依赖每字符 font/size/bbox 判定公式（`vflag`）、做段落栈（sstk/pstk/vstk），从 span 反推字符会损失精度 |
| **C. BabelDOC document_il** | `PdfCharacter/PdfLine/PdfParagraph` | 高 | 需重建逐字 bbox + `PdfStyle`，等于重写 BabelDOC 的 frontend（`il_creater`），仅当想保留 BabelDOC 排版渲染时才值得 |

**关键落差（为什么不是"改个接口就能换"）**：

1. **字符级坐标缺失（最核心）**：BabelDOC 排版以字符为锚点（`PdfCharacter.visual_bbox`）；magic-pdf 只有 span 级 bbox。从 span 均分/推断字符位置，公式、角标、混排场景排版精度必然下降——这正是本 fork 大量报告（`text_overlap_analysis_report.md`、`p1p4_ineffective_rootcause_report.md` 等）在攻克的领域；
2. **字体名缺失**：MinerU 2.x `middle.json` 的 span 只有 `content`/`type`，无字体名；magic-pdf 1.x span 有 `font` 字段但不保证与 PDF 内部字体一致。legacy `vflag()` 公式判定依赖字体名正则 + unicode 分类；
3. **类别映射表**：BabelDOC `is_text_layout` / `layout_priority` 的白名单（`plain text/title/paragraph/…`）与 magic-pdf 类别（`text/title/index/list/…`）需建立双向映射；
4. **阅读顺序差异**：BabelDOC 的 `ParagraphFinder` 与 magic-pdf 的分段/排序策略不同，段落切分会变化，影响现有 TOC/列表/伪代码补丁的触发面；
5. **公式占位**：magic-pdf 输出 LaTeX（对占位是增强），但 legacy/BabelDOC 的公式占位是"原文原样保留"，LaTeX 需先还原成原文或直接作为占位文本，涉及翻译/渲染两端的协议调整。

### 6.3 解析能力增强替换 —— ✅ 最可行（低风险高收益）

| 痛点 | 现状方案 | magic-pdf/MinerU 可替代方案 | 收益 |
|---|---|---|---|
| 伪代码误翻译 | `doclayout_pseudocode.py` 融合 PP-DocLayoutV2 | MinerU VLM 后端 `code/algorithm` 类别；或 magic-pdf `middle.json` 的 `code` 块 | 干掉 204MB 融合模型与 monkey-patch |
| 版面推理 CPU-only | `babeldoc_onnx_backend.py` monkey-patch | MinerU 自带 GPU/NPU/MPS 后端 | 无需再打补丁 |
| 公式 LaTeX | 无（仅占位保护） | UniMERNet 输出 LaTeX | 公式翻译质量提升（新能力） |
| 扫描 PDF OCR | `ocr_workaround` 较重 | PaddleOCR 109 语言内置 | OCR 质量/语言覆盖提升 |

> 注意：增强方案是"新能力注入"，**不是替换 BabelDOC**；BabelDOC 仍是主引擎，magic-pdf 作为其解析能力的补充/竞争者共存。

---

## 7. 落地改造方案（文件级）

### 方案 A：`--parse-engine magicpdf` 可选解析后端（推荐的中期路线）

1. **新增** `pdf2zh/magicpdf_adapter.py`：
   - 封装 MinerU 2.x（首选）/ magic-pdf 1.x（兜底）编程式调用；
   - 输出规整为 `MagicPdfParseResult`（逐页 `page_idx/page_size/blocks/lines/spans`）；
2. **新增映射器** `pdf2zh/v3/magicpdf_bridge.py`：`MagicPdfParseResult → canonical_page.PageModel`（span→`SpanModel`、block type→`kind`，`GlyphModel` 为空并用 span 级度量兜底）；
3. **改造入口**：
   - CLI：`pdf2zh.py create_parser()` 增加 `--parse-engine {babeldoc,legacy,magicpdf}`，在 `main()` 路由（当前 435 行 `--babeldoc` 开关附近）；
   - GUI：`runtime_service.py _execute_babeldoc()` 附近增加引擎选择分支（复用 `_engine_key` 机制）；
4. **翻译/排版复用**：解析结果直接进 v3 管线（`document_pipeline.py` RAW→SEMANTIC→TRANSLATION→RENDER）或 legacy converter 的 `receive_layout`。

### 方案 B：magic-pdf → BabelDOC document_il（保留 BabelDOC 渲染）

- 实现 `MagicPdfDocLayoutModel`（实现 BabelDOC `DocLayoutModel.handle_document` 接口），在解析阶段把 magic-pdf 的 `middle.json` 转成 `PdfCharacter/PdfLine/PdfParagraph`；
- **难点**：逐字 bbox 重建（可用 `pymupdf` 的 `page.get_text("rawdict")` 与 magic-pdf span 交叉验证）；工作量最大，仅在"magic-pdf 解析质量显著优于 BabelDOC"且已评测确认后才值得投入。

### 方案 C：增强点落地（先做，短期）

1. **伪代码保护**：在 `doclayout_pseudocode.py` 的 `PseudoCodeProtectedLayoutModel` 中，把 PP-DocLayoutV2 换成 MinerU 的 `code/algorithm` 检测（或并排比较）；
2. **公式 LaTeX**：在 `styles_and_formulas` 阶段前用 UniMERNet 对 `isolate_formula` 区域出 LaTeX，作为公式占位/翻译的 side-channel（`v3/formula` 子包已有占位机制）；
3. **OCR**：在 `babeldoc_ocr_mode.py` 的 `on` 模式下，用 magic-pdf 的 OCR 结果替换 `ocr_workaround` 的识别路径。

---

## 8. 依赖、许可与工程风险

### 8.1 依赖冲突与重量

| 项 | 本项目现状 | magic-pdf 1.3.12 要求 | MinerU 2.x 要求 | 结论 |
|---|---|---|---|---|
| `pdfminer-six` | `==20250416`（pyproject） | `==20250506` | 版本不同 | **直接冲突**，需解耦（virtualenv 隔离或版本协商） |
| `pymupdf` | 未锁 / Dockerfile `<1.25.3` | `>=1.24.9,<1.25.0` | 同向 | 可兼容（落在 1.24.x） |
| torch | ❌ 未引入 | `>=2.2.2,<3`（py3.13 需 ≥2.6） | 有 | 新增大块依赖（数百 MB–GB） |
| transformers / paddle / rapid-table | ❌ | full: transformers+doclayout-yolo+ultralytics；lite: paddlepaddle+paddleocr | 更重 | 磁盘 full ≥20GB、内存 16–32GB |
| onnxruntime GPU 后端（本项目已有 `--backend cuda/dml`） | ✅ | 无对应 | pipeline 后端支持 GPU | 能力重叠，注意共存 |

### 8.2 Python 版本（本机实测）

- 本机 **Python 3.13.1**、已装 babeldoc 0.6.4、未装 magic-pdf/mineru；
- `magic-pdf` 1.3.12：`requires-python <3.14, >=3.10` → py3.13 可装，但 torch 需 ≥2.6（Windows py3.13 有 wheel）；
- `mineru` 2.x：官方文档明确 **Windows 平台仅支持 Python 3.10–3.12**（关键依赖 `ray` 未支持 py3.13）→ 本机 py3.13 下 MinerU 2.x 不可用，需降 Python 或用 `magic-pdf` 1.x。

### 8.3 其他风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| magic-pdf 1.x 停更 | 1.3.12（2025-05）后无新版本，模型/API 不再演进 | 选型优先 MinerU 2.x；若锁 py3.13 则 magic-pdf 1.x 为过渡 |
| 模型分发 | MinerU 首次运行需从 HF/ModelScope 下载模型（GB 级），本项目已有 `PDF2ZH_PP_DOCLAYOUT_MODEL` 等模型路径管理经验 | 复用 hf-mirror 下载、缓存目录、`PDF2ZH_*_MODEL` 覆盖 |
| 速度 | OCR/公式识别显著慢于纯文本解析 | 与 BabelDOC 并存，按需切换 |
| 许可 | MinerU 2.x 为 Apache-2.0+附加条款，需评估商业/再分发条款；本项目 AGPL-3.0 | 已有 AGPL 基础，风险低但需法务确认 |
| 回归面 | 现有 10 个 babeldoc 相关测试、pseudocode 融合模型、GUI 工作图阶段映射（`_BABELDOC_STAGE_MAP`）都需覆盖新解析后端 | 保留 BabelDOC 默认，magic-pdf 为可选 |

---

## 9. 工作量与风险矩阵

| 方案 | 范围 | 预估工作量 | 风险 | 收益 |
|---|---|---|---|---|
| C. 增强（伪代码/公式/OCR） | `doclayout_pseudocode.py`、新增公式/OCR side-channel | 0.5–1.5 周/点 | 低 | 高（解决三大痛点）|
| A. `--parse-engine magicpdf` → v3 IR | 新增 adapter + bridge + CLI/GUI 路由 | 1.5–3 周 | 中（排版精度依赖 span 级坐标） | 高（可切换、可评测）|
| B. magic-pdf → document_il | 新增 `MagicPdfDocLayoutModel` + 逐字重建 | 3–4 周 | 高（字符级重建） | 中（保留 BabelDOC 渲染）|
| 完全替换 | — | — | — | 不可行 |

---

## 10. 建议路线图（三步走）

1. **阶段一（短期，1–2 周）——增强共存**
   - MinerU VLM `code/algorithm` 类别接入伪代码保护（替代 PP-DocLayoutV2 融合模型）；
   - UniMERNet 公式 LaTeX 作为公式 side-channel；PaddleOCR 接入 OCR 模式；
   - BabelDOC 仍是主引擎，magic-pdf 能力以"增强"形态存在。

2. **阶段二（中期，2–4 周）——可切换解析后端**
   - 新增 `pdf2zh/magicpdf_adapter.py` + `pdf2zh/v3/magicpdf_bridge.py`；
   - CLI `--parse-engine magicpdf`、GUI 引擎选项；
   - 用 OmniDocBench / 既有回归（`tests/` 全量 1989+）建立 legacy/BabelDOC/magicpdf 三路 diff 基线，量化接管风险。

3. **阶段三（长期，按评测决定）**
   - 若 magicpdf 解析质量在目标语料上显著优于 BabelDOC（尤其扫描件/复杂版面/伪代码），再投入"解析阶段整体替换 + 字符级坐标重建"（方案 B）；
   - 期间 BabelDOC 与 magic-pdf 双引擎共存、按文档类型灰度切换。

---

## 11. 结论

1. **"把 BabelDOC 换成 magic-pdf"不是等价替换**：magic-pdf/MinerU 只有解析能力，缺翻译、排版、渲染三块核心——完全替换不成立；
2. **可行且推荐**：把 magic-pdf 作为**可切换的解析后端**（映射到本项目 v3 IR）与**解析能力增强**（公式 LaTeX / OCR / `code` 类别）两条线推进，保留 BabelDOC 默认主引擎；
3. **必须先解决**：字符级坐标缺失（排版精度）、依赖冲突（`pdfminer-six` 版本、torch/paddle 体积）、Python 版本（MinerU 2.x Windows 不支持 3.13，本机 3.13.1 需用 magic-pdf 1.x 或降级解释器）、维护选型（magic-pdf 1.x 已停更 vs mineru 2.x 活跃）；
4. **建议第一步动作**：搭建 magic-pdf/MinerU 最小解析 demo（本机 py3.13 用 `magic-pdf` 1.3.12，Linux/降级解释器用 `mineru` 2.x），拿 3–5 份真实 PDF 跑出 `middle.json`，与 BabelDOC 的 IR 逐页 diff，用数据驱动后续决策。

## 12. 实施进度（2026-08-17 更新）

> 本报告（2026-08-16）发布后，按"建议路线图"阶段二推进，**可切换解析后端已落地到全部三个入口**。

### 12.1 已交付

| 入口 | 落地内容 | 文件 |
|---|---|---|
| CLI | `--parse-engine {auto,legacy,babeldoc,magicpdf}` + `--magicpdf-ocr`，`resolve_parse_engine()` 路由，`_try_auto_switch_magicpdf()` 自动切换，`run_magicpdf_main()` 熔断降级 | `pdf2zh/pdf2zh.py`、`pdf2zh/magicpdf_cli.py` |
| Service | `TranslationRequest.parse_engine` / `magicpdf_ocr` 字段；`_execute_task` 按 `parse_engine` 路由（magicpdf 优先于 mode_choice）；新增 `_execute_magicpdf()`（parse_args 补齐 Namespace → `run_magicpdf_main` → 收集 `{output}/magicpdf/*.json` 转储 → 完成/失败落终态） | `pdf2zh/services/runtime_service.py` |
| GUI | 「解析引擎」下拉（auto/legacy/babeldoc/magicpdf）+「MagicPDF OCR」开关；`worker.submit_translation_task` 透传；`on_translate` 快照/重试兼容（25 元素）；配置持久化 | `pdf2zh/gui/components/config_panel.py`、`pdf2zh/gui/app.py`、`pdf2zh/gui/worker.py`、`pdf2zh/gui/i18n.py`、`pdf2zh/gui/styles.py` |

### 12.2 测试与回归

- 新增 `tests/test_parse_engine_switch.py`（9 项）：`TranslationRequest` 字段默认/回传、`_execute_task` 四路路由（magicpdf/babeldoc/auto/legacy）、`_execute_magicpdf` 请求映射与结果收集/失败落态、GUI worker 透传。
- 全量回归：**2505 passed, 3 skipped**（`--ignore=pdf2zh/kernel/PDFMathTranslate-next.git`，该 submodule 测试为本机预存收集错误）。

### 12.3 尚未完成（阶段二收尾 / 阶段三前置）

| 项 | 说明 |
|---|---|
| 排版对比评测（原 Step 3.2） | 已具备 dumps（`{output}/magicpdf/*_magicpdf.json`、`*_document.json`、`*_render_plan.json`、`*_formula_channel.json`）+ **译后 mono PDF**；需真实引擎 + 20+ 样例 OmniDocBench 类视觉对比，评估接管比例 |
| RenderTakeover 渲染接管 | **已完成**：`v3/magicpdf_renderer.py` 的 `render_plan_to_pdf` 把 `fixup_render_plan` 修正后的渲染计划渲染为 PDF（v3 坐标翻转、逐块换行插入、CJK 内置字体、空 plan 兜底）；CLI 默认产出 `{stem}_mono.pdf`（`--no-magicpdf-render` 关闭）；新增 `--magicpdf-render` 参数（BooleanOptionalAction，默认开） |
| 依赖冲突宽松化 | **已落地**：`pdfminer-six` 放宽为 `>=20250416,<20250507`（§12.4）；新增 `[tool.uv] override-dependencies` 强制 `pymupdf>=1.26.7` 化解与 babeldoc 的硬冲突；`uv lock` 全量解析通过（270 包） |

### 12.4 依赖冲突处理（已落地：pdfminer-six 宽松化 + pymupdf override）

可行性报告 §4 记录了本项目锁 `pdfminer-six==20250416`、magic-pdf 要求 `==20250506` 的冲突；落地阶段又发现 `babeldoc>=0.6.4` 依赖 `pymupdf>=1.26.7`，与 magic-pdf 的 `pymupdf<1.25.0`（历史 pin）在严格解析器下硬冲突。处理如下：

1. **同一进程双版本不可共存**：magic-pdf 1.x 在 `import magic_pdf` 时即 `import pdfminer`，若已装本项目锁定版本会因版本断言失败（或行为差异）而中断，因此 magicpdf 路径与 legacy/BabelDOC 主链路**必须在同一解释器内复用同一份 pdfminer-six**。
2. **宽松化的收益有限**：magic-pdf 的 pdfminer 使用集中在解析层的 `PDFParser`/`PDFDocument` 文本提取，与本项目 legacy 内核的 `pdfminer.high_level`/`converter` 消费路径不同；`20250416→20250506` 差异属安全修复与 bugfix，API 面兼容（`extract_pages`、`LT*` 对象结构未变）。
3. **结论（已落地）**：`pdfminer-six` 放宽为 `>=20250416,<20250507`（20250506 属安全修复 + bugfix，`extract_pages`/`LT*` API 兼容，回归面仅 pdfminer 消费子集）；pymupdf 通过 `[tool.uv] override-dependencies = ["pymupdf>=1.26.7"]` 强制高版本（magic-pdf 1.3.12 在 pymupdf 1.28 下实测正常）。`uv lock` / `uv sync --dry-run` 均通过（270 包），CI `uv sync` 解析问题解决；pip 用户如遇冲突按 `docs/ADVANCED.md` 的 `--no-deps` 指引安装引擎。

---

## 附录 A：参考资料

- BabelDOC 0.6.4 源码：`C:\Python313\Lib\site-packages\babeldoc\`（`high_level.py`、`format/pdf/document_il/il_version_1.py`、`format/pdf/translation_config.py`）
- MinerU 官方：<https://github.com/opendatalab/MinerU>、<https://opendatalab.github.io/MinerU/>（`docs/zh/reference/output_files.md`、`docs/zh/quick_start/index.md`）
- magic-pdf PyPI：<https://pypi.org/project/magic-pdf/>（1.3.12，wheel 已本地解压核验：`magic_pdf/data/dataset.py`、`model/doc_analyze_by_custom_model.py`、`pdf_parse_union_core_v2.py`、METADATA）
- 本仓库关联报告：`doc/babeldoc_pseudocode_mistranslation_report.md`、`doc/p5_p10_reconstruction_implementation_report.md`、`doc/pdf2zh_next_roadmap_analysis.md`

## 附录 B：关键源码位置索引

| 主题 | 位置 |
|---|---|
| BabelDOC 管线阶段表 | `babeldoc/format/pdf/high_level.py` `TRANSLATE_STAGES` |
| BabelDOC 字符级 IR | `babeldoc/format/pdf/document_il/il_version_1.py` `PdfCharacter/PdfLine/PdfParagraph` |
| fork legacy 适配器 | `pdf2zh/babeldoc_adapter.py` |
| fork next-kernel 适配器 | `pdf2zh/babeldoc_next_adapter.py` |
| GPU 后端补丁 | `pdf2zh/babeldoc_onnx_backend.py` |
| OCR 三态 | `pdf2zh/babeldoc_ocr_mode.py` |
| 列表/目录补丁 | `pdf2zh/babeldoc_list_split.py`、`pdf2zh/babeldoc_toc_protect.py` |
| 伪代码融合模型 | `pdf2zh/doclayout_pseudocode.py` |
| v3 统一页面模型 | `pdf2zh/v3/canonical_page.py` |
| legacy 字符流消费 | `pdf2zh/converter.py` `TranslateConverter.receive_layout` |
| CLI 引擎路由 | `pdf2zh/pdf2zh.py`（`--babeldoc` 435–436；`--babeldoc-ocr` 220–229） |
| GUI 引擎执行 | `pdf2zh/services/runtime_service.py` `_execute_babeldoc`（1649） |
| magic-pdf API 核验 | 本地 wheel（已清理）：`magic_pdf/data/dataset.py`、`model/doc_analyze_by_custom_model.py`、`pdf_parse_union_core_v2.py`、METADATA |












