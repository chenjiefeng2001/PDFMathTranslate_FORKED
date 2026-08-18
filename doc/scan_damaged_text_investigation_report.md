# 「扫描版但文本层严重损坏」PDF 被当作正常 PDF 处理 —— 调查与根因分析报告

> **调查日期**：2026-08-16
> **调查人**：pdf2zh 解析层工作组
> **触发问题**：扫描版 PDF（底层为扫描图像）叠加了一层**严重损坏的文本层**时，系统不把它当作扫描件处理，而是当作正常文本 PDF 直接翻译，导致整篇译文基于乱码文本输出、全篇翻译错误。
> **结论摘要**：问题根因是**三层扫描/损坏检测机制全部失效或缺失**——BabelDOC 的 SSIM 像素相似度判定只看「文本层是否在像素上可见」，无法识别「可见但语义损坏」的文本层；legacy（pdfminer）内核完全没有扫描检测；magicpdf 的 OCR 开关默认关闭且需用户显式选择引擎。v3 侧已有的 `(cid:N)`/`�` 损坏诊断能力是纯 side-channel，未接入翻译前自动降级决策。**后续已落地缓解**：翻译前文本层质量预检（`pdf2zh/scanned_detection.py` `preflight_scan_check`）+ magicpdf 路径命中扫描信号自动开启 OCR + legacy 路径 `magic-pdf` 可用时自动切换（`PDF2ZH_AUTO_SWITCH_MAGICPDF=0` 关闭）；且 magicpdf 引擎已可从 GUI「解析引擎」下拉直接选择（见 `doc/mineru_integration_implementation_report.md`）。

---

## 1. 问题定义

「扫描版但文本层严重损坏」的 PDF 特征：

- **视觉层**：页面主体是一张扫描图像（像素内容），即用户真正要翻译的内容；
- **文本层**：叠加了一层由 OCR / 转换工具生成的文字对象，但该文本层**无法可靠使用**，典型损坏形态包括：

| 损坏形态 | 渲染表现 | 文本提取表现 |
|---|---|---|
| ToUnicode CMap 缺失 / 损坏 | 字形可能正常渲染 | 提取为 `(cid:N)` / `�` / 错误码点 |
| 字体子集化失败 / 字体对象损坏 | 替换字体渲染出 `.notdef` 方块、错乱字形 | 提取为乱码 / 空 |
| Unicode 映射错误（映射到错误码点） | 字形渲染正常（外观无异常） | 提取为「看起来正常但语义错误」的字符 |
| 文本坐标错乱 / 超页 | 文字渲染但位置错乱 | 提取出乱序文本 |

当前行为：这三类文档均**直接进入文本翻译链路**，损坏文本被当作正文翻译，译文基于乱码 / 错误语义输出，且无任何告警。

---

## 2. 涉及链路（三条解析路径）

| 路径 | 触发方式 | 扫描检测 | OCR 兜底 |
|---|---|---|---|
| legacy（pdfminer）内核 | 默认 CLI / `--parse-engine legacy` | **无任何检测** | 无 |
| BabelDOC（YADT） | `--babeldoc` / `--parse-engine babeldoc` / GUI 默认 | `DetectScannedFile`（SSIM 像素相似度） | `auto` 检测到扫描后自动启用 OCR workaround |
| magicpdf（MinerU） | `--parse-engine magicpdf` | 无 | `--magicpdf-ocr`（默认关闭） |

---

## 3. 根因分析

### 3.1 BabelDOC 引擎：SSIM 判定无法识别「可见但损坏」的文本层

BabelDOC 扫描检测位于 `babeldoc/format/pdf/document_il/midend/detect_scanned_file.py`，由两个方法组成：

**① `fast_check`（快速检测）—— 已被上游注释禁用**

```python
def fast_check(self, doc: pymupdf.Document) -> bool:
    # 统计页面内容流中的 /Artifact、/P << /MCID、3 Tr（OCR 软件生成的
    # 隐藏文本层标记），>80% 页面命中 → 判定扫描件
    ...
```

但当前安装的 BabelDOC 中该调用点已在 `high_level.py` 中被整段注释（`# if not translation_config.skip_scanned_detection and DetectScannedFile(...).fast_check(...)`），**实际只走 `process()` 的 SSIM 路径**。

**② `process` / `detect_page_is_scanned`（SSIM 像素相似度）—— 唯一实际生效的判定**

```python
before_page_image = geometry.image            # 原始整页渲染
pdf_creater.update_page_content_stream(False, page, pdf, config, True)  # 用 IL 重绘、skip_char=True 跳过文本层字符
after_page_image  = geometry.render_at_dpi(pdf[page], ...)              # 重绘（无文本层文字）
similarity = structural_similarity(before, after)
return similarity > 0.95                       # SSIM>0.95 → 该页判定为扫描页
```

全文档扫描页占比 ≥ 80% → 判定为扫描件；本项目 `--babeldoc-ocr auto` 把 `auto_enable_ocr_workaround` 置 True，命中后自动启用 OCR。

**失效本质**：SSIM 比较的实质是「**文本层在像素上是否可见**」，与「文本层内容是否语义有效」**完全无关**：

- 无文本层 / 文本层渲染不可见（白色、字体缺失空白）→ before ≈ after → SSIM 高 → **正确**判为扫描 → 走 OCR；
- 文本层渲染**可见**但内容损坏（乱码字形、`.notdef` 方块、错误字体、大号错误字形）→ before（可见乱码文字）与 after（无文字）像素差异大 → **SSIM 跌破 0.95 → 误判为「正常文本 PDF」** → 不启用 OCR workaround → 损坏文本层被直接送进翻译 → 全篇乱译。

### 3.2 legacy（pdfminer）内核：完全没有扫描检测

`pdf2zh/high_level.py` 的 `translate_patch` 直接用 `PDFConverterEx`（继承 `pdfminer.converter.PDFConverter`）提取文本层：

```python
def render_char(self, matrix, font, fontsize, ...):
    try:
        text = font.to_unichr(cid)
    except PDFUnicodeNotDefined:
        text = self.handle_undefined_char(font, cid)   # → "(cid:%d)"
```

- **无扫描检测阶段、无 OCR 开关、无文本质量门禁**——对损坏文本层 100% 直接翻译；
- 损坏表现 1（提取出 `(cid:N)` / `�`）→ 翻译器把 `(cid:1)` 之类当正文翻译；
- 损坏表现 3（ToUnicode 映射到错误码点）→ 提取出「看起来正常」的错误字符 → **静默错误**，任何信号都没有。

### 3.3 magicpdf（MinerU）路径

`--parse-engine magicpdf`（CLI / Service / GUI「解析引擎」下拉）桥接了 MinerU 的解析能力。解析前经 `preflight_scan_check` 做文本层质量预检，命中扫描/损坏信号时**自动开启 OCR**（无需用户手动加 `--magicpdf-ocr`）；`--magicpdf-ocr` 仍可强制开启：
- 默认无独立扫描检测 → 由 `run_magicpdf_main` 的预检信号驱动；
- `--magicpdf-ocr` 开关默认关闭，预检命中时自动生效；
- legacy 路径在 `magic-pdf` 可用时由 `_try_auto_switch_magicpdf` 自动切换（`PDF2ZH_AUTO_SWITCH_MAGICPDF=0` 关闭）。

### 3.4 已有诊断能力未接入主流程决策

项目 v3 侧已经具备损坏文本检测能力，但全部是 **side-channel（侧通道）**，不参与翻译前决策：

| 能力 | 位置 | 现状 |
|---|---|---|
| `has_replacement()`（`�` / `(cid:N)`） | `pdf2zh/v3/pipeline_dump.py` | 仅用于 dump 诊断，CLI 手动触发 |
| `glyph_dump` 的 `is_replacement` / `decode` / `has_to_unicode` 信号 | `pdf2zh/v3/pipeline_dump.py` | 同上 |
| `unicode_error` 检测器（severity=**error**） | `pdf2zh/v3/diagnostics.py` | 挂在 processor_channels 侧通道，`admissible=False` 仅提示进 Repair Pipeline，**不拦截翻译、不触发 OCR** |
| `translation_dump` 的损坏对比 | `pdf2zh/v3/pipeline_dump.py` | 翻译后对比，为时已晚 |

即：**损坏信号「能看到，但用不上」**。

---

## 4. 实验验证

为量化验证 BabelDOC SSIM 判定的失效，编写了可复现脚本 `tools/scan_detect_experiment.py`（复现 `detect_page_is_scanned` 的 before/after 渲染与 SSIM 判定，阈值取 BabelDOC 的 0.95）：

| 场景 | SSIM | 判定结果 | 是否正确 |
|---|---|---|---|
| A. 真扫描件（仅图像、无文本层） | 1.0000 | SCANNED → 走 OCR | ✅ 正确 |
| B. 正常 PDF（图像背景 + 完好文本层） | 0.8955 | non-scanned → 走文本翻译 | ✅ 正确 |
| C1. 损坏文本层（渲染为**大号可见错误字形**） | **0.8225** | **non-scanned → 走文本翻译** | ❌ **误判** |
| C2. 损坏文本层（渲染为**密集可见乱码**） | **0.8936** | **non-scanned → 走文本翻译** | ❌ **误判** |
| D. 文本层不可见（白色文字） | 0.9710 | SCANNED → 走 OCR | ✅ 正确（但不具代表性） |

pdfminer 提取验证（legacy 内核实际输入）：

- 正常 PDF → `"The quick brown fox jumps over the lazy dog."`（正常）
- 损坏（C1/C2）→ 乱码 / 符号流被直接提取 → 会被当作正文翻译
- 结果印证：**渲染可见的损坏文本层同时骗过 SSIM 判定与文本提取兜底。**

> 复现方式：`python tools/scan_detect_experiment.py`

---

## 5. 影响面与危害

| 入口 | 默认 OCR 模式 | 对「损坏文本层」的行为 | 危害级别 |
|---|---|---|---|
| GUI（BabelDOC 后端） | `auto` | SSIM 误判 → 不 OCR → 乱译 | 🔴 高（用户最常见入口） |
| CLI 默认（legacy pdfminer） | 无 OCR | 无检测 → 乱译 | 🔴 高 |
| CLI `--babeldoc` | `auto` | 同上 SSIM 误判 | 🔴 高 |
| CLI `--babeldoc-ocr on` | 强制 OCR | 全部走 OCR（可缓解） | 🟢 缓解手段，但需用户手动开启 |
| CLI `--parse-engine magicpdf --magicpdf-ocr` | 默认关（预检命中自动开） | 预检自动启用；GUI 也可直接选择引擎 | 🟡→🟢 缓解手段（已接线） |

**危害**：翻译过程无任何报错、无告警，输出 PDF 外观正常但全文内容错误——这是比「报错失败」更危险的**静默错误**，用户很难察觉并纠正。


---

## 6. 改进建议

按「诊断先行 → 决策接线 → 多信号融合」分三阶段，与现有双轨并行战略一致，不破坏稳定主链路。

### 6.1 短期：翻译前「文本层质量预检」+ 自动启用 OCR / 显式告警

在解析出首 N 页字符流后、翻译启动前，新增轻量质量评分：

1. 统计信号（复用 `pipeline_dump.has_replacement` 与新增指标）：
   - `(cid:N)` 占比、`�` 占比；
   - 不可打印 / 控制字符占比；
   - 低信息量符号簇（`\xa5\xa6...` 等乱码符号带）占比；
   - 提取文本与页面渲染图像文字区域的覆盖率差异（可选项）。
2. 决策：
   - 损坏率超过阈值（建议初始 10% 字符 / 或 30% 页面含损坏信号）：
     - **BabelDOC 路径**：自动翻转为 `ocr_workaround=True`（相当于临时 `--babeldoc-ocr on`），并输出 `WARNING: 检测到文本层损坏（(cid:N)/乱码占 X%），已自动启用 OCR`；
     - **legacy 路径**：无 OCR 能力，输出**强警告**并建议用户使用 `--babeldoc` / `--parse-engine magicpdf --magicpdf-ocr`；若 `magic-pdf` 可用则自动切换（`_try_auto_switch_magicpdf` 已实现，`PDF2ZH_AUTO_SWITCH_MAGICPDF=0` 关闭）。
3. 产物：`doc/` 下新增《文本层质量预检设计》小节并入本报告，作为实现规格。

### 6.2 中期：把 v3 损坏诊断升级为主流程 gate

- 将 `glyph_dump` 的 `decode` / `has_to_unicode` 信号在解析阶段采样统计，写入 `v3_output["text_quality"]`；
- 在 `translate_patch` / `RuntimeService` 中加入 gate：`text_quality.broken_ratio > 阈值` → 自动降级到 OCR 引擎（BabelDOC `ocr_workaround` 或 magicpdf `--ocr`），并记录 Decision 供回放；
- `diagnostics.unicode_error` 从「error 级但仅提示」升级为「触发 OCR 降级」的输入。

### 6.3 长期：多信号融合的统一扫描判定

替代单一 SSIM 判据，融合：

| 信号 | 来源 | 判别力 |
|---|---|---|
| SSIM 像素相似度 | BabelDOC `DetectScannedFile` | 判「文本层是否可见」 |
| `(cid:N)` / `�` 比例 | pdfminer / v3 glyph | 判「ToUnicode 是否损坏」 |
| 字体 ToUnicode CMap 缺失率 | `glyph_dump.has_to_unicode` | 判「字体解码可信度」 |
| 提取文本与 OCR 文本的一致性 | magic-pdf / UniMERNet | 交叉验证 |
| 版面色块 / 图像占比 | 布局模型 | 判「是否扫描页面」 |

任一信号命中阈值即触发 OCR，而不是「全部通过才算扫描」——降低漏检。

---

## 7. 结论

1. **根因确认**：当前「扫描版 + 损坏文本层」被当作正常 PDF 的直接原因是 BabelDOC 的 `DetectScannedFile` 只用 **SSIM 像素相似度**判定扫描，该判据**无法识别「渲染可见但语义损坏」的文本层**；legacy 内核**完全没有扫描/损坏检测**；magicpdf OCR 默认关闭。
2. **实验证实**：损坏可见文本层的 SSIM 为 0.82–0.89，稳定跌破 0.95 阈值 → 必然被误判为正常 PDF → 乱码被直接翻译（见第 4 节数据）。
3. **现有诊断资产**（`pipeline_dump.has_replacement` / `glyph_dump.decode` / `diagnostics.unicode_error`）已具备检测损坏的能力，但未接入翻译前决策，属于「看到了但没用上」。
4. **修复方向**：短期在翻译前增加文本层质量预检并自动启用 OCR（BabelDOC 可立即落地）；中期将 v3 诊断升级为主流程 gate；长期用多信号融合替代单一 SSIM。

---

## 附录 A：关键代码定位

| 组件 | 文件 |
|---|---|
| BabelDOC SSIM 扫描检测 | `babeldoc/format/pdf/document_il/midend/detect_scanned_file.py`（`process` / `detect_page_is_scanned`） |
| BabelDOC `fast_check` 被注释禁用 | `babeldoc/format/pdf/high_level.py` 约 884–896 行 |
| 本项目 OCR 三态开关 | `pdf2zh/babeldoc_ocr_mode.py`（`auto`→SSIM 自动检测） |
| legacy 未定义字符处理 | `pdf2zh/converter.py` → `PDFUnicodeNotDefined` → `(cid:N)` |
| 损坏诊断（side-channel） | `pdf2zh/v3/pipeline_dump.py`（`has_replacement` / `glyph_dump`）、`pdf2zh/v3/diagnostics.py`（`unicode_error`） |
| 验证脚本 | `tools/scan_detect_experiment.py` |

## 附录 B：复现步骤

```bash
# 1) 运行 SSIM 误判复现实验（打印各场景判定）
python tools/scan_detect_experiment.py

# 2) 对可疑 PDF 使用既有诊断工具观察文本层损坏信号
python -m pdf2zh.v3.pipeline_dump <suspicious.pdf> --out dump/
# 观察 GlyphDump 中 is_replacement=True / decode=fffd / decode=notdef 的比例
```

