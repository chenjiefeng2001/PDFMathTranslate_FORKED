# BabelDOC 大文档翻译进度缓慢问题分析报告

> **修复落地记录（2026-08-24）**：P0 四项已实施——
> ① 伪代码保护三态开关 + 页数门控：`PDF2ZH_BABELDOC_PSEUDO_PROTECT`（auto/on/off，默认 auto），
>   `auto` 仅对 ≤ `PDF2ZH_BABELDOC_PSEUDO_PROTECT_MAX_PAGES`（默认 30）页的文档启用，MinerU 整本
>   预解析分支随之被门控收紧（`pdf2zh/doclayout_pseudocode.py`）；
> ② 健康文本层信任预检、跳过 BabelDOC 内部 SSIM 二次扫描检测，混合扫描文档（任一页无文本层）
>   绝不跳过，`PDF2ZH_BABELDOC_TRUST_PREFLIGHT=0` 可恢复旧行为（`pdf2zh/babeldoc_ocr_mode.py`）；
> ③ CPU 回退时一次性 GPU 加速提示（`pdf2zh/babeldoc_onnx_backend.py`）；
> ④ 大文档 + LLM 引擎 + 低 qps 的提速日志提示（`runtime_service._hint_babeldoc_qps`）。
> 回归测试见 `tests/test_babeldoc_perf_gates.py`。

> **结论摘要**：大文档走 BabelDOC 模式「进度过于缓慢」的主因不在翻译本身，而在**翻译前的解析（parsing）与布局分析（analyzing）阶段逐页串行、随页数线性膨胀**——基线实测 10 页任务中这两段占 74% 墙钟时间，200 页文档在第一个段落开始翻译前就要等待约 15–50 分钟。在此之上，本项目默认注入的伪代码保护融合模型使布局推理成本进一步放大（每页双模型串行；装有 magic-pdf/MinerU 的环境下更是先对整本 PDF 跑一遍完整 MinerU 解析再进 BabelDOC）。翻译段并发受 `qps`（GUI threads，默认 4）限制，LLM 引擎下是第二瓶颈。进度观感被阶段权重错配放大：漫长的 analyzing 段只占权重 20%，条爬得极慢，用户感知为「卡死」。

## 一、问题定义

- 症状：`mode_choice=babeldoc` / `parse_engine=babeldoc` 翻译大 PDF 时总耗时远超 legacy 模式，且进度条长时间停滞在低位。
- 范围：`RuntimeService._execute_babeldoc` 驱动的两条管线（pdf2zh_next 内核优先、legacy BabelDOC 兜底），二者共享同一套 BabelDOC 核心（babeldoc 0.6.4）。

## 二、证据链

### 2.1 基线实测：非翻译段占绝对大头

`script/bench/RESULTS.md` B3（google 免费引擎、ignore_cache=true、10 页）：

| config | wall | translating | parsing+analyzing |
|---|---|---|---|
| legacy_t1 | 44.5s | 38.7s | — |
| **babeldoc_t4** | **194.9s** | **26.7s** | **145.0s（parsing 54.3 + analyzing 90.7）** |

BabelDOC 路径中翻译只占 13.7%，parsing+analyzing 占 74%；RESULTS.md 结论 #4 已确认「analyzing/parsing 段位于 BabelDOC 库内部（document_il midend，单线程设计），本地无并行切入点」。该成本随页数线性增长，**页数越多越慢**——这正是大文档症状的来源。

> 注意：bench 环境装了伪代码保护模型（见 §2.3），analyzing 数值含双模型推理；即使剔除该因素，原生单模型布局仍是逐页串行。

### 2.2 BabelDOC 核心是「阶段串行 + 页级串行」设计

管线顺序（`babeldoc/format/pdf/high_level.py:836-1071` `_do_translate_single`）：

```
parse IR → DetectScannedFile → LayoutParser → ParagraphFinder
→ StylesAndFormulas → ILTranslator(翻译) → Typesetting → PDFCreater
```

各阶段间严格串行；关键的页级并行点全部缺失：

| 环节 | 事实 | 出处 |
|---|---|---|
| 布局推理批量 | `batch_size = 1` **硬编码**，逐页一次 ONNX 调度 | `babeldoc/docvision/doclayout.py:208-236` |
| 布局阶段消费 | `LayoutParser.process` for 循环逐页消费 `handle_document` 生成器 | `document_il/midend/layout_parser.py:119-133`、`doclayout.py:259-284` |
| 进程池 | `_ENABLE_PROCESS_POOL = False` 默认关闭，`enable_process_pool()` 注释明言 dev/testing only；layout 完成即 `close_process_pool()` | `babeldoc/const.py:49-84`、`high_level.py:956` |
| 解析（PDF→IR） | 单进程逐页（extract_char/mupdf_helper 的池路径同样依赖上述禁用的池） | `document_il/utils/extract_char.py`、`mupdf_helper.py` |

即：**上游没有给本仓库留下任何页级并行切入点**，warm worker pool 等 legacy 加速手段也不适用（`RESULTS.md:70`：「babeldoc 路径不经过进程池」）。

### 2.3 本项目叠加项：伪代码保护把布局成本放大

两条适配器都会注入融合布局模型：

- next 内核：`babeldoc_next_adapter.py:513-526`，`build_pseudo_code_protected_layout_model(pdf_path=work_path)`
- legacy 兜底：`babeldoc_adapter.py:349`

融合模型 `PseudoCodeProtectedLayoutModel.handle_document`（`doclayout_pseudocode.py:288-318`）**每个页面串行做两次完整推理**：

1. 持有 `base_model.lock` 渲染整页（1024px 长边栅格化）+ 默认 doclayout YOLO 推理；
2. 再跑 PP-DocLayoutV2（204 MB ONNX）检测 algorithm 框。

→ analyzing 段推理次数 ×2、栅格化 ×1（原本只有第 1 步）。

**更重的隐藏分支**：next 内核传了 `pdf_path` → `_build_with_mineru_or_paddle`（`doclayout_pseudocode.py:553-584`）。当环境安装了 magic-pdf/MinerU 时，优先构造 `MinerUAlgorithmDetector(pdf_path)`（`:395-424`）——其 `__init__` 直接执行 `MagicPdfAdapter.parse(pdf_path)`，即 **magic-pdf 全管线（PymuDocDataset → doc_analyze 版面/公式/OCR PyTorch 模型 → middle.json）对整本文档先解析一遍**（`magicpdf_adapter.py:763-827`），然后 BabelDOC 再自己 parse/layout 一遍。同一份文档被两套完全独立的解析栈各处理一次，且 per-document 不缓存。

### 2.4 翻译段并发上限：qps = GUI threads（默认 4）

- ILTranslator 用 `PriorityThreadPoolExecutor(max_workers=pool_max_workers)`（`il_translator.py:412-416`）；`pool_max_workers` 缺省 = `qps`（`translation_config.py:243-246`）。
- 服务层 `qps=request.threads or 4`（`runtime_service.py:2022-2028`）；GUI threads 滑条默认 4（`gui/components/config_panel.py:261-263`）。
- 内核的 `pdf2zh_next_recommended_qps` 机制只回填 **term_qps**（术语抽取），主翻译 qps/pool 不变（`pdf2zh_next/high_level.py:513-527`）。

→ LLM 引擎（openai/deepseek 等，单请求 2–5s）大文档的翻译墙钟 ≈ `段落数 ÷ 4 × 单请求延迟`；数千段时为小时级量级，且调大并发只能靠用户手动拉滑条。

### 2.5 次要因素

| 因素 | 说明 | 出处 |
|---|---|---|
| ONNX 后端默认 CPU | `PDF2ZH_BABELDOC_BACKEND=auto` 保持 BabelDOC 原生 CPU 行为；GPU 需显式设 cuda/dml | `babeldoc_onnx_backend.py`（apply_babeldoc_backend 幂等传播已实现） |
| 扫描检测重复执行 | OCR auto 模式：本项目 `preflight_scan_check` 先检一遍；未命中强制 OCR 时仍返回 `(False, True, False)`，BabelDOC 内部 SSIM 检测继续跑——文本层健康的 PDF 也要对 ~20% 页面做双次栅格化 | `babeldoc_ocr_mode.py:101-126`、`detect_scanned_file.py:112-123,151-172` |
| 扫描件 OCR workaround | 真·扫描大文档逐页 OCR，属于本质性开销（质量换时间），非回归 | 同上 |

### 2.6 进度观感错配：「慢」也被感知放大

服务端阶段权重表（`runtime_service.py:188-196`）：parsing 10%、analyzing 20%、translating 30%……按 legacy 工作量假设设计。BabelDOC 大文档实际分布接近 parsing 28% / analyzing 47% / translating 14%（§2.1 折算），于是：

- analyzing 漫长阶段在 UI 上只对应 10%→30% 的窄区间，条爬极慢；
- `_update_aggregator_weights` 仅按页数重排 translating/layouting/rendering 权重，不感知 BabelDOC 各 stage 的真实 total；
- 用户看到的是「卡在 20% 很久」，叠加实际就慢，形成「过于缓慢」的直接观感。

## 三、大文档场景推演（量级估算）

以 10 页实测线性外推（含一次性成本的摊入，±30% 引擎方差）：

| 文档规模 | parsing+analyzing（进入翻译前等待） | 翻译段（google, qps=4） |
|---|---|---|
| 10 页（实测） | ~145 s | ~27 s |
| 50 页 | ~12 min | ~2 min |
| 200 页 | **~48 min** | ~9 min |
| 200 页 + MinerU 已安装 | 上表基础上再加一次完整 MinerU 解析（分钟~十分钟级） | 同上 |
| 200 页 + LLM 引擎（2–5s/请求） | 同上 | 5000 段 ÷ 4 × 3s ≈ **60 min+** |

结论：页数越大，「翻译前的串行预处理」占比越高；MinerU 分支与 LLM 低 QPS 是两个乘法级放大器。

## 四、优化建议

### P0 —— 本地可落地、不动上游

1. **伪代码保护加开关与页数自适应**（预期收益最大、改动最小）
   - 现状无任何开关，两条适配器无条件注入（`babeldoc_next_adapter.py:513`、`babeldoc_adapter.py:349`）。
   - 建议 `PDF2ZH_BABELDOC_PSEUDO_PROTECT=auto/on/off`：auto 时小文档（如 ≤30 页）启用、大文档跳过并在日志/UI 提示；on/off 强制。
   - **MinerU 分支单独收紧**：仅小文档或显式 opt-in 时走 `MinerUAlgorithmDetector`（整本预解析代价过高），否则退 PP-DocLayoutV2 或纯 base 模型。
2. **复用预检结果跳过二次扫描检测**：preflight 判定「健康文本层」时直接 `skip_scanned_detection=True`（扫描件仍走强制 OCR 分支），省掉 BabelDOC 内部 ~20% 页面的 SSIM 双栅格化。
3. **GPU 引导**：检测到 onnxruntime-gpu/dml 可用时，日志/UI 提示设 `PDF2ZH_BABELDOC_BACKEND=cuda/dml`（传播机制已就绪，纯引导项）。
4. **qps 透明化**：大文档 + LLM 引擎时提示调高 threads 滑条；或内核对已知高吞吐引擎自动提高主 qps（现有 recommended_qps 机制目前只覆盖 term_qps）。

### P1 —— monkey-patch / 上游贡献

5. **布局批量推理**：参照 `apply_babeldoc_list_split` / `apply_babeldoc_toc_protect` 的幂等 patch 先例，把 `DocLayoutModel.predict` 的硬编码 `batch_size=1` 提升为可配（GPU 下收益显著；CPU 收益有限）。
6. **handle_document 页级并行**：多线程喂页 + 会话锁推理，或推动上游启用预留的进程池（`enable_process_pool` 目前 dev-only）。
7. 解析段（extract_char/mupdf_helper）上游并行化——代价高，列为长期项。

### P2 —— 观测性

8. **阶段权重动态化**：BabelDOC `progress_start` 事件携带各 stage 的真实 total（ProgressMonitor per-stage），可用实际工作量重排当前任务的阶段权重，消除「卡在 20%」的错觉；至少在 UI 显示当前阶段名 + 阶段内百分比。

## 五、附：关键代码索引

| 主题 | 位置 |
|---|---|
| 服务入口/进度转发 | `pdf2zh/services/runtime_service.py:1942-2095` |
| next 内核适配器（模型注入、asyncio 驱动） | `pdf2zh/babeldoc_next_adapter.py:484-583` |
| legacy 适配器（TranslationConfig 组装） | `pdf2zh/babeldoc_adapter.py:204-433` |
| 融合布局模型（逐页双推理） | `pdf2zh/doclayout_pseudocode.py:252-390` |
| MinerU 全文档预解析分支 | `pdf2zh/doclayout_pseudocode.py:395-424,553-584`；`pdf2zh/magicpdf_adapter.py:742-827` |
| OCR 三态映射/预检 | `pdf2zh/babeldoc_ocr_mode.py:101-141` |
| BabelDOC 主流程 | `babeldoc/format/pdf/high_level.py:836-1071` |
| 布局 batch_size=1 | `babeldoc/docvision/doclayout.py:182-284` |
| 翻译线程池/qps | `babeldoc/format/pdf/document_il/midend/il_translator.py:406-481`；`translation_config.py:243-253` |
| 进程池默认禁用 | `babeldoc/const.py:49-84` |
| 基线数据 | `script/bench/RESULTS.md` B3 |
