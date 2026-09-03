# OCR 模块实现调查报告

> **日期**：2026-09-01
> **范围**：PDFMathTranslate_FORKED（pdf2zh 1.9.x 系）—— `pdf2zh/` 主代码、`pdf2zh/kernel/`（pdf2zh_next 子模块与 mineru_worker）、`vendor/MinerU/`、site-packages 中 babeldoc 内核、`doc/` 历史报告、`tests/`
> **调查方法**：全仓正则检索（`ocr`/`OCR`/`paddleocr`/`tesseract`/`rapidocr`/`easyocr` 等）+ 关键文件逐行阅读 + OCR 相关测试套件实跑验证（54 passed, 1 skipped）
> **结论摘要**：本项目**没有自研 OCR 识别引擎**，OCR 能力完全由外部解析引擎（MinerU/magic-pdf 的 `PytorchPaddleOCR`：DB 文本检测 + CRNN 识别）提供；本项目自身实现的是围绕 OCR 的**「判定 + 开关 + 接线」三层控制结构**。BabelDOC 内核**不含任何 OCR 引擎代码**，其 `ocr_workaround` 只是排版配合（译文白底覆盖、丢弃图像字符），依赖 PDF 已有隐形 OCR 文本层。legacy 内核无 OCR 能力，预检命中仅警告或自动切换 magicpdf 引擎。

---

## 1. 总体架构：三层控制结构

| 层 | 模块 | 职责 |
|---|---|---|
| 判定层 | `pdf2zh/scanned_detection.py` | 多信号融合判定「该不该 OCR」（扫描件/损坏文本层检测） |
| 开关层 | `pdf2zh/babeldoc_ocr_mode.py` + CLI/GUI/Service 三态透传 | 三态 `auto/on/off`，让用户能显式关闭 OCR |
| 接线层 | `babeldoc_adapter.py` / `babeldoc_next_adapter.py` / `magicpdf_adapter.py` / `magicpdf_cli.py` | 把 OCR 开关翻译成下游引擎参数 |

真正执行 OCR 识别（文本检测+识别模型）的只有 **MinerU/magic-pdf 链路**。

---

## 2. 三条翻译链路的 OCR 行为

### 2.1 magicpdf/MinerU 链路 —— 唯一真正跑 OCR 模型的链路 ✅

**用户入口**（2026-08-19 由二态升级为三态，见 `doc/magicpdf_ocr_cannot_disable_report.md`）：

- CLI：`--magicpdf-ocr`（历史 bool，等价 `on` 且优先）/ `--magicpdf-ocr-mode {auto,on,off}`
- 解析函数：`pdf2zh/pdf2zh.py:628 resolve_magicpdf_ocr_mode()`
- GUI：`config_panel.py:249` 三态 Radio（`magicpdf_ocr`）
- Service：`runtime_service.py:335-345` `magicpdf_ocr: bool`（兼容）+ `magicpdf_ocr_mode: str`；另有 `mineru_parse_method`（`auto/ocr/txt` 显式模式，**优先于 OCR 开关**）

**执行流**（`magicpdf_cli.py:296-320`）：

```
on  → ocr=True（无条件）
off → ocr=False（预检命中也绝不开启 —— 修复"OCR 无法关闭"的关键）
auto→ 预检 preflight_scan_check 命中扫描/损坏信号才 ocr=True
```

**OCR 参数下传**（`magicpdf_adapter.py`）：

- MinerU 路径（:1636）：`parse_method = mineru_parse_method or ("ocr" if ocr else "auto")` → 隔离 venv 子进程 `kernel/mineru_worker.py` → `mineru.cli.common.do_parse`
- magic-pdf 1.x 路径（:1529-1542）：`ds.apply(doc_analyze, ocr=ocr)`，ocr 为真走 `pipe_ocr_mode/pipe_ocr_merge`，否则 `pipe_txt_mode/pipe_txt_merge`

**MinerU 内核的 OCR 实现**（`vendor/MinerU/`）：

- `parse_method='auto'` 时 `mineru/utils/pdf_classify.py::classify()` 用启发式自动判 ocr/txt：极端页宽高比、PDFium Unicode map 错误、高 CID 字体无 ToUnicode、Latin 字体解码出 CJK、可疑跨脚本文本等 7 类信号
- OCR 引擎：`mineru/model/ocr/pytorch_paddle.py::PytorchPaddleOCR` —— **PaddleOCR 的 PyTorch 移植**（DB 文本检测 + CRNN 识别），多语言模型由 `models_config.yml` 配置、按需自动下载；`pipeline_analyze.py:133 ocr_model_init` 经 `AtomModelSingleton` 单例加载，`model_init.py:26` 有推理锁
- OCR 同时服务于表格（`MineruTableOrientationClsModel`/`UnetTableModel`/`PaddleTableModel` 都消费 ocr_engine）
- GPU：OCR 属 torch 模型（`magicpdf_adapter.py:184`：OCR=paddleocr2pytorch），**必须 CUDA 版 torch**；`mineru_worker.py` 解析前注入 `MINERU_DEVICE_MODE` 并按显存做保守 batch 预算（防 OCR 批处理 OOM）

### 2.2 BabelDOC 链路 —— 只有"OCR workaround"，无 OCR 引擎 ⚠️

**开关模块** `pdf2zh/babeldoc_ocr_mode.py`（三态，优先级：`PDF2ZH_BABELDOC_OCR` 环境变量 > 显式参数 > `auto`）：

| 模式 | ocr_workaround | auto_enable | skip_detection | 语义 |
|---|---|---|---|---|
| `auto` | F | **T** | F | 检测到扫描才启用（pdf2zh 默认） |
| `on` | **T** | F | F | 强制所有 PDF 走 OCR 路径 |
| `off` | F | F | **T** | 跳过扫描检测，不做 OCR |

`resolve_ocr_flags()` 在 `auto` + 有 `source_path` 时先跑预检：

- 命中扫描/损坏 → 强制 `(True, False, False)`；
- 判定健康文本层且**每页**都有文本（`_all_pages_have_text_layer`，pymupdf 逐页 ≥32 字符）→ `(False, False, True)` 跳过 BabelDOC 内部 SSIM 二次检测提速（`PDF2ZH_BABELDOC_TRUST_PREFLIGHT=0` 可恢复双重检测）。

**消费方**：`babeldoc_adapter.py:417`（CLI legacy `TranslationConfig`）+ `babeldoc_next_adapter.py:385`（GUI 主路径 → kernel `SettingsModel`）。

**内核实际行为**（site-packages `babeldoc/format/pdf/document_il/midend/detect_scanned_file.py` + 消费点）：

- 检测：`fast_check`（content stream 找 `/Artifact`/`/MCID`/`3 Tr` 隐形文本标记）+ `detect_page_is_scanned`（72dpi 渲染原图 vs 去文本层重渲染，SSIM>0.95 判扫描）；扫描页 ≥80% 时启用 workaround（`auto_enable` 开）或抛 `ScannedPDFError`（关）
- **关键事实：BabelDOC 内核不含任何 OCR 识别引擎代码**。`docvision/table_detection/rapidocr.py::RapidOCRModel` 是已退役的 no-op 兼容桩（predict 恒返回空 boxes）；全包搜 `tesseract|ocrmypdf|paddle` 无 OCR 执行代码
- `ocr_workaround=True` 的真实效果是**排版配合**：`paragraph_finder.py:296` 给文本加白色背景填充（`add_text_fill_background`）并清空 `page.pdf_character`；`pdf_creater.py:892` 矩形描边宽 0.4→0.1、:1472 字体子集 gc_level 提到 4；`styles_and_formulas.py:372` 跳过 contained elements 收集
- 因此 BabelDOC 的 OCR 语义是：**假定输入 PDF 已有可提取文本层**（如 OCR 工具生成的隐形文本层，即 `3 Tr`），内核只负责「译文盖底、丢弃图像字符」。`babeldoc_ocr_mode.py` 模块 docstring 中「需要 OCR 模型」的描述与内核实现不符（详见 §6 问题清单）

### 2.3 legacy 内核（pdfminer）—— 无 OCR 能力，只有警告与自动切引擎

- `_run_text_quality_gate`（`high_level.py:528`）：翻译前预检，判定写入 `v3_output["text_quality"]`；命中只输出强警告「legacy 内核无 OCR 兜底，建议 --parse-engine magicpdf --magicpdf-ocr 或 --babeldoc-ocr on」
- `_try_auto_switch_magicpdf`（`pdf2zh.py:641`）：预检命中且 MinerU 可用 → 自动切 `parse_engine=magicpdf` + `magicpdf_ocr=True`。防护：`off` 模式不切换（:665）、`PDF2ZH_AUTO_SWITCH_MAGICPDF=0` 关闭、`_auto_switch_attempted`/`_magicpdf_fallback` 防乒乓循环
- `runtime_service.py:1177` 服务层同样有 auto-switch（且不覆盖用户显式选择的 `mode_choice=babeldoc`）

---

## 3. 判定核心：`scanned_detection.py` 多信号融合

「任一信号命中阈值即触发 OCR」（降低漏检），默认采样前 3 页（`DEFAULT_MAX_PAGES=3`），预检只读不写：

| # | 信号 | 阈值 | 判什么 | 状态 |
|---|---|---|---|---|
| 1 | `pixel_ssim` | 0.95 | 文本层是否像素可见（≈BabelDOC SSIM） | ✅ 生效 |
| 2 | `text_cid_fffd` / `text_broken_pages` | 0.10 / 0.30 | `(cid:N)`/``/控制字符/噪声符号占比；页级损坏率 | ✅ 生效 |
| 3 | `font_to_unicode` | 0.60 | 复合（CID）字体 ToUnicode 缺失率 | ✅ 生效（2026-08-22 修复：从页面字体字典判读并区分复合/简单字体，此前恒 1.000 导致**预检恒触发 OCR**） |
| 4 | `ocr_crosscheck` | 0.5 | 提取文本 vs OCR 文本 Jaccard 差异 | ⚠️ **未接线**（需外部注入 ocr_texts，当前无调用方） |
| 5 | `image_ratio` | 0.60 | 图像块面积占比 | 可选（需传 blocks） |

提取用 `PDFConverterEx` 复用主链路渲染语义（`_extract_pdf_samples`）；`recommend_ocr_flags()` 把判定映射回 BabelDOC 三开关。

## 4. 历史演进脉络（`doc/` 佐证）

1. `scan_damaged_text_investigation_report.md`（2026-08-16）：BabelDOC SSIM 只能判「可见性」不能判「语义损坏」→ 引入多信号预检
2. `magicpdf_ocr_cannot_disable_report.md`（2026-08-19）：用户反馈「MinerU OCR 无法关闭」——bool=False 只是「不主动开」而非「禁止开」，预检命中仍强制改写 → **三态化修复**（15 项专项测试）
3. `mineru_babeldoc_starting_stall_fix_report.md`：新增 `mineru_parse_method`（auto/ocr/txt）显式模式，不受 OCR 开关间接决定

---

## 5. 用户入口汇总

| 入口 | BabelDOC 链路 | magicpdf 链路 |
|---|---|---|
| CLI | `--babeldoc-ocr {auto,on,off}` | `--magicpdf-ocr` / `--magicpdf-ocr-mode` |
| GUI | `ocr_mode` Radio（config_panel.py:224） | `magicpdf_ocr` 三态 Radio（:249） |
| API | — | `TranslationRequest.magicpdf_ocr(_mode)`、`mineru_parse_method` |
| 环境变量 | `PDF2ZH_BABELDOC_OCR`、`PDF2ZH_BABELDOC_TRUST_PREFLIGHT` | `PDF2ZH_AUTO_SWITCH_MAGICPDF`、`MINERU_DEVICE_MODE` 等 |

---

## 6. 发现的问题与风险

1. **死桩**：`pdf2zh/scan_pdf_processor.py` 的 `ScanPDFProcessor`（投影法分栏 + 阅读序）设计了完整 OCR 提取骨架，但 `_ocr_region` 仅 `logger.warning("OCR engine not connected.")` 返回空 —— 历史遗留，从未接线，也无调用方。
2. **`ocr_crosscheck` 信号未接线**：预检 5 信号之一永远不参与判定。
3. **文档滞后**：`docs/README_GUI.md:20`、`docs/ADVANCED.md:450` 仍描述 MagicPDF OCR 为 checkbox（实际已是三态 Radio）；`README_zh-CN.md:267/318` 只提 `--magicpdf-ocr` 未提 `--magicpdf-ocr-mode` 三态。
4. **描述失真**：`babeldoc_ocr_mode.py` docstring 称 ocr_workaround「最可靠但最慢，需要 OCR 模型」，但 BabelDOC 内核无 OCR 引擎——`on` 模式对**真无文本层**的扫描件不会产生文本（无译文来源），该模式仅对「已有隐形 OCR 文本层」的 PDF 有意义。
5. **采样盲区**：预检默认只看前 3 页，长文档中后部损坏页不会触发 auto 强制 OCR（MinerU 内部 `classify` 同为采样式启发，可部分兜底）。
6. **双开关心智负担**：GUI 同时存在 BabelDOC `ocr_mode` 与 MagicPDF `magicpdf_ocr` 两个三态 Radio，语义独立（一个管排版 workaround、一个管识别模型），用户易混淆。

---

## 7. 测试验证

```
tests/test_babeldoc_ocr_mode.py / test_magicpdf_ocr_mode.py /
test_text_quality_gate.py / test_parse_engine_switch.py
→ 54 passed, 1 skipped (17.12s) ✅
```

覆盖：三态归一化与优先级、三态↔互斥字段映射、`off` 时不强制 OCR、legacy 自动切换尊重 `off`、预检字体信号等。另相关：`tests/test_scanned_font_signal.py`（font_to_unicode 修复回归）。

---

## 8. 后续建议（按投入产出排序）

1. **低成本高收益**：修正 `babeldoc_ocr_mode.py` docstring 与 `docs/README_GUI.md`、`docs/ADVANCED.md` 的滞后描述（OCR 语义失真会直接误导用户对 `--babeldoc-ocr on` 的预期——它对纯无文本层扫描件不会产生译文）。
2. **接线现成能力**：`scan_pdf_processor.py` 的骨架与 `scanned_detection.py` 的 `ocr_crosscheck` 信号都已就位，只差一个真实 OCR 引擎注入点。最自然的做法是复用 MinerU 隔离 venv 的 `PytorchPaddleOCR`（`PDF2ZH_MINERU_PYTHON` 子进程机制已有成熟先例），为 legacy 链路补上 OCR 兜底。
3. **预检收紧**（`magicpdf_ocr_cannot_disable_report.md` §7.2 已记录在案）：区分「纯扫描无文本层」与「有文本层但 ToUnicode 损坏」，减少 auto 模式对后者的误判强制 OCR。
4. **判定盲区**：预检 3 页采样 + MinerU classify 采样式启发对长文档中后部损坏页存在漏检，可考虑页数自适应采样（如前 3 + 均匀抽 3）。
5. **开关整合**：GUI 两个 OCR Radio 增加 i18n 说明互链（或在文档中给出「哪个场景用哪个」决策表），降低混淆。


