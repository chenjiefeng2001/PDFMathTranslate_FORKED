# 「允许从 BabelDOC 切换为 magic-pdf」落地现状与实跑故障根因报告

> **日期**：2026-08-17
> **范围**：PDFMathTranslate_FORKED（pdf2zh 1.9.x 系，GUI 7860 端口）
> **前置文档**：`doc/babeldoc_to_magicpdf_feasibility_report.md`（2026-08-16，可行性初评）；本文是其“实现落地 + 实跑验证”续篇
> **一句话结论**：**代码层面“把解析引擎从 BabelDOC 切换为 magic-pdf/MinerU”的能力已经实现**（CLI `--parse-engine magicpdf` + GUI 下拉 + 服务层路由 + 双后端适配器 + v3 Bridge + 渲染接管 + 单元测试），但 2026-08-17 实际运行暴露 **4 个阻断点**：① magic-pdf 1.x 缺少 `~/magic-pdf.json` 与模型文件，解析必然失败；② 可用性探测只查“能 import”不查“能运行”，导致自动切换误判；③ “熔断→自动切换→再熔断”级联造成重复尝试；④ 任务把 gradio 临时缓存目录既当输入又当输出目录，源文件在翻译中途消失，两个任务均报 `no such file`。前两项属“环境未配置”，后两项属“工程缺陷”，均可修。

---

## 0. TL;DR（结论摘要）

| # | 问题 / 结论 | 级别 | 状态 |
|---|---|---|---|
| 1 | **“切换”能力已实现**：CLI/GUI/服务层四路路由 + `MagicPdfAdapter`（mineru 2.x / magic-pdf 1.x 双后端）+ `MagicPdfBridge`→v3 文档模型→翻译→RenderTakeover 渲染 mono PDF + `tests/test_parse_engine_switch.py` | — | ✅ 已实现 |
| 2 | **magic-pdf 1.x 本机未配置**：无 `C:\Users\14977\magic-pdf.json`、无 `~/.cache/modelscope`（模型未下载）→ `doc_analyze` 首批推理时 `read_config()` 抛 `FileNotFoundError` | 阻断 | ✅ 本次已修（见 `babeldoc_to_magicpdf_switch_report.md` §3 修复 1：解析前自动生成配置） |
| 8 | **18:24 二次实跑新暴露**：配置自动生成后 `KeyError: 'YOLO_v8_MFD'`（公式模型键名错误：应为小写 `yolo_v8_mfd` / `unimernet_small`）+ 模型文件缺失（本机 `~/.cache/magic-pdf/models` 为空） | 阻断 | ✅ 本次已修（`babeldoc_to_magicpdf_switch_report.md` §3 修复 4：键名修正；修复 5：模型存在性预检 + 下载指引，秒级降级 legacy） |
| 3 | **可用性探测误判**：`engine_env.available_backend()` / `MagicPdfAdapter.is_available()` 仅探测模块可导入，不校验配置与模型 → 自动切换误触发，切换后立刻失败 | 缺陷 | 🔧 待修 |
| 4 | **级联重试**：magicpdf 失败 → `_fallback_legacy` → legacy 预检 → 再次自动切回 magicpdf → 再失败 → 才真正跑 legacy；单任务重复解析 2 次、浪费约 20s，日志混乱 | 缺陷 | 🔧 待修 |
| 5 | **源文件在任务中途消失**（`no such file`）：全链路 `out_dir = dirname(source_path)`，把 gradio 临时缓存目录当输出目录；源文件在 04:03:44（legacy 读取成功）到 04:04:24（BabelDOC `do_translate` 失败）之间被回收 | 缺陷 | 🔧 待修 |
| 6 | **GPU 后端选型**：本机 `onnxruntime 1.28.0` 仅有 `AzureExecutionProvider`(DML)+CPU；任务却请求 `cuda`，每次会话创建都打印 CPU 回退告警；正确选择是 `dml` 或 `cpu` | 配置 | ⚙️ 用户操作 |
| 7 | **可行性再评估**：完全替换 BabelDOC 仍不可行（magic-pdf 无翻译/排版/渲染）；**解析层替换/并存**可行且已落地，前提是配置好引擎 + 修掉 3~5 | — | ✅ 可行 |

---

## 1. 背景：为什么“切换”在今天被真正触发

本次会话日志（`python -m pdf2zh.gui.app`）中出现了完整的 **magic-pdf 自动/手动切换链路**，这在此前可行性报告（纯静态分析）之后是首次真机实测：

```
2026-08-17 04:03:18,644 - [task=task_818ce8b80465] ONNX backend switch: auto -> cuda
2026-08-17 04:03:19,430 - [magicpdf] ...Compilers Principles...pdf 预检命中扫描/损坏信号
                          (font_to_unicode: 1.000 >= 0.60)，自动开启 OCR
2026-08-17 04:03:21,978 - magic_pdf.data.dataset:__init__:157 - lang: None
2026-08-17 04:03:29,820 - magic_pdf.model...doc_analyze:162 - Batch 1/6: 200 pages/1035 pages
2026-08-17 04:03:33,261 - [magicpdf] ...解析失败: C:\Users\14977\magic-pdf.json not found
2026-08-17 04:03:33,261 - [magicpdf] ...解析失败 —— 自动降级回 legacy 内核重试。
2026-08-17 04:03:33,350 - ...文本层质量预检命中扫描/损坏信号（font_to_unicode: 1.000 >= 0.60）；
                          magic-pdf/MinerU 可用，已自动切换 --parse-engine magicpdf --magicpdf-ocr。
2026-08-17 04:03:43,489 - magic_pdf...doc_analyze:162 - Batch 1/6: 200 pages/1035 pages   （第二次尝试）
2026-08-17 04:03:43,490 - [magicpdf] ...解析失败: C:\Users\14977\magic-pdf.json not found
2026-08-17 04:03:44,902 - translate_stream: loaded 1035 pages, starting patch phase...  （legacy 兜底终于开跑）
...
2026-08-17 04:04:24,629 - INFO - start to translate: <gradio 缓存目录>\Compilers...Z-Library.pdf
2026-08-17 04:04:24,630 - WARNING - Error in check metadata, continue: no such file: '<同一路径>'
2026-08-17 04:04:24,746 - ERROR - translate error: no such file: '<同一路径>'
2026-08-17 04:04:24,760 - ERROR - [task=task_d9bd31153e8e] BabelDOC failed: no such file: ...
Traceback → babeldoc_next_adapter.py:543 run_babeldoc_next_translation → do_translate
```

日志同时证明了两件事：**切换机制确实被执行了**（预检命中 → 自动切换 → magicpdf 解析 → 熔断降级 legacy）；**但切换后的引擎因环境未配置而失败，且源文件随后消失导致 BabelDOC 任务也失败**。

---
## 2. 现状盘点：切换机制在代码中的完整链路（已实现）

### 2.1 三层入口

| 层 | 位置 | 说明 |
|---|---|---|
| CLI | `pdf2zh/pdf2zh.py:463-472` `main()` + `resolve_parse_engine()`（475-484） | `--parse-engine auto\|legacy\|babeldoc\|magicpdf`；`auto` 保持历史语义（`--babeldoc`→YADT，否则 legacy）；显式 `magicpdf` 走 magicpdf 链路 |
| GUI | `pdf2zh/gui/components/config_panel.py:181-197` | 「解析引擎」Radio：`auto / legacy / babeldoc / magicpdf` + 「MagicPDF OCR」Checkbox（`magicpdf_ocr`） |
| 服务层 | `pdf2zh/services/runtime_service.py:891-907` `_execute_task` | 四路路由：`magicpdf`→`_execute_magicpdf`；`babeldoc`→`_execute_babeldoc`；批量→`_execute_batch`；v4→`_execute_v4`；否则 legacy |

### 2.2 magic-pdf 执行链（`pdf2zh/magicpdf_cli.py` + `pdf2zh/magicpdf_adapter.py`）

```
MagicPdfAdapter.parse(pdf_path, ocr=ocr)            # adapter 双后端选择
  ├─ backend()                                       # mineru 2.x 优先，magic-pdf 1.x 兜底
  ├─ _parse_mineru()                                 # mineru.document.Document.parse（Py3.10~3.12）
  └─ _parse_magicpdf()                               # PymuDocDataset → doc_analyze → pipe_ocr_merge/txt_merge
        （先读字节，再走 magic-pdf 公共管线，产出 middle.json 归一化）
        ↓
MagicPdfBridge.convert_all(results) → to_document_model()   # middle.json → v3 DocumentModel
        ↓
translate_document(doc, translator.translate)                # 复用 build_translator 翻译
        ↓
collect_formula_latex / apply_formula_latex                  # 公式 LaTeX 侧通道回填
render_plan_from_model → fixup_render_plan                    # 渲染计划 + RenderTakeover 修正
render_plan_to_pdf(...) → {output}/magicpdf/{stem}_mono.pdf    # 译后 mono PDF（可 --no-magicpdf-render 关闭）
        ↓
_write_dumps() → {output}/magicpdf/{stem}_magicpdf.json /
                 {stem}_document.json / {stem}_render_plan.json / {stem}_formula_channel.json
```

关键点：
- **解析失败自动降级**：`run_magicpdf_main`（`magicpdf_cli.py:79`）任何异常都走 `_fallback_legacy`（`magicpdf_cli.py:32`）→ 调 `pdf2zh.pdf2zh._run_legacy_kernel` 用 legacy 内核重跑。
- **文本层预检**：`run_magicpdf_main` 与 legacy 内核都会调 `pdf2zh.scanned_detection.preflight_scan_check`，命中扫描/损坏信号时自动开 OCR。
- **产物位置**：`_output_dir(parsed_args)`（`magicpdf_cli.py:25`）= `{output}/magicpdf/`；服务层 `_execute_magicpdf`（`runtime_service.py:1667-1669`）把 `ns.output` 设为 `config.output_dir or dirname(source_path)`。

---

### 2.3 依赖与测试

- `pyproject.toml`：可选依赖组 `magicpdf = ["mineru>=2.0", "magic-pdf>=1.3.12,<2"]`（两包互斥，装其一）。当前环境：**magic-pdf 1.3.12 已装，mineru 未装**。
- 测试：`tests/test_parse_engine_switch.py`（9 项：请求字段默认/回传、四路路由、`_execute_magicpdf` 映射与失败落态、GUI worker 透传）。
- 离线兜底：`MagicPdfAdapter.load_middle_json / from_middle_json`（`magicpdf_adapter.py:180` 起）可在无引擎环境下回归 bridge 层。

### 2.4 与 BabelDOC 的关系（现状）

- **BabelDOC 仍是默认/主推引擎**：`--parse-engine auto` + `--babeldoc` → YADT；GUI 默认 `auto`。
- **magic-pdf 是“并行旁路”**：可选、显式或自动触发，产物是 JSON 转储 + 自渲染 mono PDF，**不依赖 BabelDOC 的任何环节**（翻译器通过 `build_translator` 复用 pdf2zh 的翻译引擎）。
- 二者共享：ONNX 后端开关（`pdf2zh/doclayout.py` → `_sync_babeldoc_backend` 同步到 BabelDOC）、扫描预检（`scanned_detection.py`）、翻译器（`pdf2zh.translator`）。

---

## 3. 今日实跑故障根因分析（对照日志）

### R1（阻断·环境）：magic-pdf 1.x 缺少 `~/magic-pdf.json` 与模型 → 解析必然失败

**日志证据**：
```
04:03:21,978  magic_pdf.data.dataset:__init__:157 - lang: None
04:03:29,820  doc_analyze:162 - Batch 1/6: 200 pages/1035 pages
04:03:33,261  [magicpdf] ...解析失败: C:\Users\14977\magic-pdf.json not found
```

**本机核验**：`C:\Users\14977\magic-pdf.json` 不存在；`C:\Users\14977\.cache` 下无 `modelscope` 目录（模型未下载）；`magic_pdf` 1.3.12 已安装、`mineru` 未安装。

**代码路径**（magic-pdf 1.3.12 包内）：
1. `magic_pdf/libs/config_reader.py:15-27` `read_config()`：读 `~/magic-pdf.json`（或 `MINERU_TOOLS_CONFIG_JSON` 指向的文件），**文件不存在直接 `raise FileNotFoundError`**。
2. `magic_pdf/model/doc_analyze_by_custom_model.py` `custom_model_init()`（full 模式 `MODEL.PEK`）：首批推理时懒加载模型 → `get_local_models_dir()` / `get_device()` / `get_layout_config()` / `get_formula_config()` / `get_table_recog_config()` 全部依赖 `read_config()`。
3. `doc_analyze()`（同文件 133-162 行）先渲染 200 页图像（日志里的 “Batch 1/6” 来自这里），**首个批次进入模型推理时**触发上面的懒加载 → 抛 `FileNotFoundError: C:\Users\14977\magic-pdf.json not found`。
4. 该异常沿 `MagicPdfAdapter._parse_magicpdf`（`magicpdf_adapter.py:301-338`）→ `run_magicpdf_main` 冒泡，命中熔断降级。

**结论**：不是代码逻辑错误，是 magic-pdf 引擎需要一次性的环境初始化（配置文件 + 模型下载，见 §5）。**只要没有 `magic-pdf.json`，本机无论手动还是自动选择 magicpdf 引擎，100% 复现该失败。**

---

### R2（缺陷）：可用性探测只查“能 import”，不查“能运行” → 自动切换误判

**日志证据**：
```
04:03:33,350  ...magic-pdf/MinerU 可用，已自动切换 --parse-engine magicpdf --magicpdf-ocr。
04:03:43,489  第二次 magic-pdf 解析又失败（同样缺配置）
```

**根因**：`engine_env.available_backend()`（`pdf2zh/engine_env.py:78-88`）与 `MagicPdfAdapter.is_available()`（`magicpdf_adapter.py:276-278`）的判断标准**仅有一条**——`import magic_pdf`（或 `import mineru`）是否成功。而 magic-pdf 的“能 import”与“能跑通”之间隔着配置文件与模型文件两座大山。于是：

- 预检命中扫描信号（`font_to_unicode` 缺失率 1.0 ≥ 0.60）后，`pdf2zh.py:487 _try_auto_switch_magicpdf` 询问 `available_backend()` → 返回 `("magicpdf", True)` → 判定“可用” → 自动切换。
- 切换后 `run_magicpdf_main` 立刻因 R1 失败。

**影响**：把“有引擎”误报为“可运行”，既是本次级联重试（R3）的导火索，也会在 GUI 中给用户错误的引擎可用性预期。

---

### R3（缺陷）：熔断→自动切换→再熔断的级联重试（非死循环，但浪费 ~20s 且日志混乱）

**日志证据**（单任务 `task_818ce8b80465` 内的完整轨迹）：
```
04:03:19,430  预检命中 → 自动开启 OCR（run_magicpdf_main 内的预检分支）
04:03:33,261  magicpdf 解析失败 → 自动降级 legacy（第一次）
04:03:33,350  legacy 预检 → 又命中扫描信号 → “magic-pdf/MinerU 可用” → 再次自动切换 magicpdf
04:03:43,490  magicpdf 解析再次失败 → 自动降级 legacy（第二次）
04:03:44,902  translate_stream 终于开跑（legacy 兜底）
```

**代码路径**：
1. `run_magicpdf_main`（`magicpdf_cli.py:79`）解析失败 → `_fallback_legacy`（`magicpdf_cli.py:32`）→ `_run_legacy_kernel`。
2. `_run_legacy_kernel`（`pdf2zh.py:544`）→ `_try_auto_switch_magicpdf`（`pdf2zh.py:487`）→ 预检命中 + `available_backend()` 误判可用（R2）→ **把 `parse_engine` 改回 `magicpdf` 并返回 True** → 再次调用 `run_magicpdf_main`。
3. 第二次 `run_magicpdf_main` 又失败 → `_fallback_legacy` → `_run_legacy_kernel` → `_try_auto_switch_magicpdf` 命中防重入守卫 `_auto_switch_attempted=True` → 返回 False → 这次才真正走 legacy。

**结论**：防重入守卫（`_auto_switch_attempted`）避免了死循环，但**没有阻止“失败后再自动切回失败引擎”这一无意义动作**；单任务出现 2 次 magic-pdf 解析尝试 + 2 次 legacy 入口，浪费约 20s，且日志里“自动切换/自动降级”反复出现，对用户极有误导性。理想语义应当是：**一旦从 magicpdf 熔断降级，本次任务就锁定 legacy，不再自动切回。**

---

### R4（缺陷）：源文件在任务中途消失 → 两个任务均报 `no such file`

**日志证据**：
```
04:03:44,902  translate_stream: loaded 1035 pages, starting patch phase...   ← legacy 已成功读到文件字节
...
04:04:24,629  start to translate: C:\Users\14977\AppData\Local\Temp\gradio\d98ddc...\Compilers...pdf
04:04:24,630  WARNING - Error in check metadata, continue: no such file: '<同一路径>'
04:04:24,746  ERROR - translate error: no such file: '<同一路径>'
04:04:24,760  ERROR - [task=task_d9bd31153e8e] BabelDOC failed: no such file: '<同一路径>'
```

**现场勘察**：
- 源文件路径位于 **gradio 的临时缓存目录** `%TEMP%\gradio\d98ddc7bd2...\`（gradio 上传落盘位置，目录名是文件内容哈希）。
- 该目录下同时出现了本项目 magicpdf 的输出子目录 `magicpdf\`——即 **`_execute_magicpdf` 把 `out_dir` 设成了 `dirname(source_path)`（gradio 缓存目录）**，magic-pdf 链路在此创建了 `magicpdf/` 输出目录。
- 源文件在 **04:03:44（legacy 读到字节）** 与 **04:04:24（BabelDOC `do_translate` 打开失败）** 之间消失；事后复查，整个 `d98ddc...` gradio 缓存目录已被删除。

**根因**：
1. 本项目三个引擎（legacy / babeldoc / magicpdf）在未指定输出目录时都把输出写到 `os.path.dirname(source_path)`（见 `runtime_service.py:1667-1669`（magicpdf）、`1809`（babeldoc）、legacy 同模式）——而 GUI 场景下 `source_path` 恰恰在 **gradio 生命周期托管的 scratch 目录**里。
2. 任务全链路（从提交到翻译完成，长则数十分钟）依赖这条**随时可能被回收/重建的临时路径**：gradio 缓存目录的回收（服务重启/重传同哈希文件复用目录/外部 `%TEMP%` 清理器/Windows 临时清理）都会使任务中途失去输入。
3. 仓库内代码未发现删除源文件的路径（已核对 `pdf2zh/gui`、`runtime_service`、两个 babeldoc 适配器、magicpdf 适配器；gradio 默认 `delete_cache=None` 也不自动回收），因此判定为**依赖了不可靠的临时路径**这一架构性缺陷，而非某行“删除代码”。

**影响**：任务 818ce8（legacy 兜底，已读入内存不受影响）与任务 d9bd31（BabelDOC，翻译启动时才打开文件）命运不同——**凡是“翻译阶段才打开源文件”的路径（BabelDOC 的 `do_translate`）都会在此类回收下硬失败**。

**修复方向（§6.2）**：提交任务时把上传文件快照到**每任务私有工作目录**，输入/输出都放私有目录，与 gradio 缓存目录彻底解耦。

---

### R5（配置）：请求 `cuda` 但本机只有 DML/CPU → 每次会话创建都 CPU 回退

**日志证据**（多次重复出现）：
```
04:03:43,788  Backend 'cuda' requested but no GPU provider is available
              (wanted ['CUDAExecutionProvider','CPUExecutionProvider']; available: ['AzureExecutionProvider','CPUExecutionProvider'])
04:04:20,762  BabelDOC doclayout ONNX providers=['CPUExecutionProvider'] (backend=cuda)
```

**根因**：本机 `onnxruntime 1.28.0` 可用 provider 为 `AzureExecutionProvider`(DirectML) + `CPUExecutionProvider`，**没有 CUDA provider**（未装 `onnxruntime-gpu` 或 CUDA/cuDNN 不匹配）。GUI「后端」选中 `cuda`（日志 `ONNX backend switch: auto -> cuda` 说明请求明确为 cuda），于是：
- `pdf2zh.doclayout.resolve_providers` 静态解析出 `[CUDA, CPU]`；
- `InferenceSession` 创建时 CUDA 不可用 → 自动回退 CPU；
- 每次创建会话（多页版面分析会多次）都打印一对警告。

**影响**：功能不受影响（CPU 可跑），但：① 日志噪音大；② 若用户误以为“cuda 生效”可能困惑；③ CPU 版面分析对大文档是吞吐瓶颈。

**正确做法**：本机 GPU 加速应选 **`dml`**（DirectML 已就绪，无需 CUDA 工具链）；或 `cpu` 保持安静。`auto` 语义下建议先探测 `get_available_providers()`，只在确有 CUDA provider 时才解析为 cuda。

---

## 4. 可行性再评估（相对 2026-08-16 报告的更新）

### 4.1 三个层次的结论更新

| 层次 | 8-16 初评 | 8-17 实测后结论 | 依据 |
|---|---|---|---|
| **完全替换 BabelDOC** | 不可行 | **仍不可行** | magic-pdf 只产 middle.json/Markdown，无翻译、无逐字排版、无 mono/dual PDF 渲染；本仓库的 BabelDOC 主链路（字符级 IR + Typesetting + PDFCreater）无可替代物 |
| **解析层替换/并存** | 有条件可行 | **已实现，尚需“能跑通”** | `--parse-engine magicpdf` 全链路已落地（§2）；缺引擎配置（R1）与可靠输入目录（R4） |
| **解析能力增强** | 最可行 | **仍成立且正在受益** | magic-pdf 侧通道公式 LaTeX（`v3/formula_side_channel.py`）、RenderTakeover（`v3/render_takeover.py` + `v3/magicpdf_renderer.py`）、扫描预检 OCR 自动开启均已接入 |

---

### 4.2 “切换”的完成度核对

**已实现（本次日志已证实执行）**
- [x] GUI/CLI/服务层三层入口（含 `magicpdf_ocr` 开关透传）
- [x] 扫描/损坏文本层预检 → 自动开启 OCR / 自动切换 magicpdf
- [x] magic-pdf 1.x（Py3.13 兜底）/ MinerU 2.x（Py3.10-3.12 优先）双后端选择
- [x] middle.json → v3 文档模型 → 复用翻译器翻译 → 公式 LaTeX 侧通道 → 渲染计划 fixup → 译后 mono PDF
- [x] JSON 转储（`{output}/magicpdf/*.json`）+ 失败熔断降级 legacy + 单元测试

**未完成 / 待办**
- [ ] magic-pdf 1.x 的 `magic-pdf.json` 一键生成/校验（`pdf2zh magicpdf-setup` 类子命令）
- [ ] 可用性探测升级：import + 配置 + 模型三连探针（R2）
- [ ] 级联重试收敛：熔断后锁定 legacy（R3）
- [ ] 输入文件每任务快照 + 输出目录与 gradio 缓存解耦（R4）
- [ ] `auto` 后端按真实 provider 探测解析（R5）
- [ ] 排版质量对比评测（v3 dumps + mono PDF 已具备，缺 20+ 样例 OmniDocBench 类视觉对比）

### 4.3 环境层面的可行性（本机）

| 条件 | 状态 | 说明 |
|---|---|---|
| Python 3.13.1 | ✅ | magic-pdf 1.3.12 支持（torch 2.6 已装）；mineru 2.x 在 Windows 仅 3.10-3.12，本机不可用 |
| magic-pdf 1.3.12 包 | ✅ 已装 | `import magic_pdf` 正常 |
| `~/magic-pdf.json` | ❌ 缺失 | 需创建（§5 模板） |
| 模型文件 | ❌ 缺失 | 无 `~/.cache/modelscope`，需下载（§5） |
| OCR 依赖（PaddleOCR） | ⚠️ 需核验 | magic-pdf OCR 模式依赖 `paddleocr`/`rapidocr`，本机是否就绪未验证 |
| pdfminer-six 版本 | ✅ 已落地宽松化 | 本项目约束 `>=20250416,<20250507`；另经 `[tool.uv] override-dependencies` 强制 `pymupdf>=1.26.7`，`uv lock` 通过（8-16 报告 §12.4 结论已更新） |
| GPU | ⚠️ 仅 DML | 选 `dml` 后端可用 DirectML 加速；`cuda` 无效 |

---

## 5. 让 magic-pdf 在本机真正跑起来（落地步骤）

> 前提：本机 Python 3.13，只能走 **magic-pdf 1.x** 路线（mineru 2.x 不支持 3.13）。

### 5.1 创建 `~/magic-pdf.json`

参考模板（路径按本机调整；模型目录用 ASCII 路径，避免含中文/空格）：

```json
{
  "bucket_info": {},
  "models-dir": "C:/Users/14977/.cache/magicpdf/models",
  "layoutreader-model-dir": "C:/Users/14977/.cache/magicpdf/models/layoutreader",
  "device-mode": "cpu",
  "layout-config": {
    "model": "doclayout_yolo"
  },
  "formula-config": {
    "mfd_model": "yolo_v8_mfd",
    "mfr_model": "unimernet_small",
    "enable": true
  },
  "table-config": {
    "model": "rapid_table",
    "enable": false,
    "max_time": 400
  }
}
```

说明：
- `device-mode` 先填 `cpu` 验证链路；magic-pdf 1.x 对 `cuda` 要求 torch 带 CUDA 且满足显存，本机无 CUDA 环境，**不要**填 cuda。
- 版面模型 `layoutlmv3` 与 `doclayout_yolo` 二选一；`doclayout_yolo` 比 `layoutlmv3` 轻量、对公式/伪代码类文档通常效果更好（本项目伪代码融合模型同源）。
- 表识别 `rapid_table` 若无需求可 `enable: false`（省模型与耗时）。

---

### 5.2 下载模型到 `models-dir`

magic-pdf 1.x 模型从 ModelScope 分发，模型 ID 以官方 MinerU/magic-pdf README 为准（常见为 `ppaanngggg/*` 系列）。可用以下任一方式：

```powershell
# 方式 A：modelscope CLI（推荐）
pip install modelscope
modelscope download --model ppaanngggg/layoutlmv3          --local_dir C:/Users/14977/.cache/magicpdf/models/layoutlmv3
modelscope download --model ppaanngggg/doclayout_yolo      --local_dir C:/Users/14977/.cache/magicpdf/models/doclayout_yolo
modelscope download --model ppaanngggg/YOLO_v8_MFD         --local_dir C:/Users/14977/.cache/magicpdf/models/YOLO_v8_MFD
modelscope download --model ppaanngggg/UniMerNet_v2_Small  --local_dir C:/Users/14977/.cache/magicpdf/models/UniMerNet_v2_Small
modelscope download --model ppaanngggg/layoutreader        --local_dir C:/Users/14977/.cache/magicpdf/models/layoutreader
# （OCR 模式另需 PaddleOCR 模型：pip install paddleocr 后按需下载 ch_PP-OCRv4_det/rec）
```

```powershell
# 方式 B：使用 magic-pdf 官方仓库提供的模型下载脚本（modelscope / hf-mirror 二选一），
#   脚本与模型 ID 以官方 README 为准。
```

> 核验：上述模型应下载到与 `magic-pdf.json` 中 `models-dir` 一致的目录，且各子目录名与 `layout-config.model` / `formula-config.mfd_model` / `mfr_model` / `layoutreader-model-dir` 匹配。

### 5.3 验证链路（不经过 GUI）

```powershell
# 1) 最小冒烟：直接调用 magic-pdf 公共管线解析单页
python -c "from magic_pdf.data.dataset import PymuDocDataset; from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze; ds=PymuDocDataset(open(r'C:\path\to\test.pdf','rb').read()); r=ds.apply(doc_analyze, ocr=False); print('OK', len(r.get_middle_json().get('pdf_info',[])))"

# 2) 走本项目 CLI（显式 magicpdf，不开 OCR，单文件）
pdf2zh --parse-engine magicpdf --output C:\temp\out C:\path\to\test.pdf

# 3) 成功后查看产物
#    C:\temp\out\magicpdf\{stem}_magicpdf.json / {stem}_document.json / {stem}_mono.pdf
```

### 5.4 GUI 侧操作

1. 确认 `~/magic-pdf.json` 存在且模型就位（§5.1-5.2）。
2. GUI「后端」改为 **`dml`**（或 `cpu`），不要再选 `cuda`。
3. 「解析引擎」选 **`magicpdf`**，扫描版文档勾选「MagicPDF OCR」。
4. 提交任务；预期日志出现 `magic_pdf...doc_analyze` → 解析成功 → 翻译 → `[magicpdf] ... mono PDF 已渲染`。

> 若不想手动创建配置文件，可先设置环境变量让 magic-pdf 读自定义路径：`MINERU_TOOLS_CONFIG_JSON=C:/Users/14977/magic-pdf.json`（`config_reader.py:12` 已支持）。

---

## 6. 建议的代码级改进（按优先级）

### 6.1 P0：可用性探测升级为“可运行”三连探针（修复 R2）

- 位置：`pdf2zh/engine_env.py` `available_backend()` / `probe_magicpdf()`；`pdf2zh/magicpdf_adapter.py` `is_available()`。
- 建议：
  1. `import magic_pdf` 成功；
  2. `magic_pdf.libs.config_reader.read_config()` 可读（`~/magic-pdf.json` 或 `MINERU_TOOLS_CONFIG_JSON` 存在）；
  3. `models-dir` 指向的目录存在且非空。
- 三条同时满足才算“可用”；不满足时 `available_backend()` 返回 `(backend, False)` 并在返回中带出缺项原因，供 GUI/CLI 给出可执行提示（“缺少 magic-pdf.json，请运行 pdf2zh magicpdf-setup”）。
- 这样 `_try_auto_switch_magicpdf` 就不会在未配置环境下自动切换（R3 的触发源之一也随之消失）。

### 6.2 P0：任务输入快照 + 输出目录与 gradio 缓存解耦（修复 R4）

- 位置：`pdf2zh/services/runtime_service.py` 的任务入口（`submit_translation_task`/`_execute_task`）与 `pdf2zh/gui/app.py` `on_translate`。
- 建议：
  1. 任务提交时，把上传文件**复制**到每任务私有工作目录（如 `{配置的 jobs 根目录}/tasks/{task_id}/input/<原文件名>`），后续全链路只引用这份快照；
  2. `out_dir` 也指向该私有目录（或用户显式指定的输出目录），**不再默认 `dirname(source_path)`**；
  3. 任务终态后按保留策略清理私有目录（或保留供下载）。
- 收益：无论 gradio 缓存目录何时回收，任务输入/输出都不受影响；同时让“结果文件在服务重启后消失”的隐患一并消除。

---

### 6.3 P1：熔断降级后锁定引擎，不再自动切回（修复 R3）

- 位置：`pdf2zh/magicpdf_cli.py` `_fallback_legacy` / `pdf2zh.py` `_try_auto_switch_magicpdf`。
- 建议：`_fallback_legacy` 传入 `reason` 时，在 `parsed_args` 上打上 `_magicpdf_fallback=True`；`_try_auto_switch_magicpdf` 首行检查该标记，直接返回 False。配合 6.1 后，未配置环境下根本不会发生第一次切换，级联自然消失。

### 6.4 P1：`auto` 后端按真实 provider 探测（修复 R5）

- 位置：`pdf2zh/doclayout.py` 后端解析处。
- 建议：`auto` 时先 `onnxruntime.get_available_providers()`：含 CUDA 才解析 cuda；否则含 Azure/DML 才解析 dml；再否则 cpu。这样本机 `auto` 会自动落到 DML 或 CPU，不再请求无效的 cuda、也不再打满屏告警。

### 6.5 P1：`magicpdf-setup` 子命令（一键生成配置 + 校验模型）

- 建议新增 `pdf2zh` 子命令（或 GUI「MagicPDF 状态」面板）：
  1. 检测 Python 版本 → 给出 mineru/magic-pdf 选择建议；
  2. 缺失时生成 `~/magic-pdf.json` 模板（`device-mode` 按探测结果填 `cpu`/`dml`）；
  3. 校验 `models-dir` 各子模型目录是否存在，缺失时给出 `modelscope download` 命令清单；
  4. 输出“可运行/缺配置/缺模型”三态，供 GUI 展示。

### 6.6 P2：GUI 引擎可用性提示

- `config_panel.py` 的「解析引擎」Radio 旁增加实时状态：`magic-pdf: 已就绪 / 缺少 magic-pdf.json / 缺少模型`（复用 6.1/6.5 的探针），避免用户在不可用状态下提交后才发现失败。

---

## 7. 风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| **字符级坐标缺失** | magic-pdf 的 span 只有行/块级 bbox，无逐字符 bbox；重排精度低于 BabelDOC 字符级 IR（8-16 报告 §3.3 已指出） | magicpdf 产物定位为“旁路解析/自渲染 mono PDF”，不与 BabelDOC 主链路做同指标对比 |
| **公式 LaTeX 质量** | UniMerNet 小模型对复杂多行公式仍有误差 | 公式侧通道 + fixup 已可诊断；正式采用前建议跑 20+ 样例视觉评测 |
| **OCR 依赖** | magic-pdf OCR 模式依赖 PaddleOCR/RapidOCR，本机未核验 | 落地步骤 §5.2 注明；先以非 OCR 文档验证 |
| **pdfminer-six 冲突面** | 已宽松化（`>=20250416,<20250507`）+ pymupdf override 落地，`uv lock` 通过（8-16 报告 §12.4） | magicpdf 与 legacy 共用同一解释器内同一 pdfminer |
| **Py3.13 锁定 magic-pdf 1.x** | mineru 2.x 在 Windows 仅 3.10-3.12 | 本机无解，除非换 Python 3.12 解释器装 mineru |
| **gradio 缓存目录（已实测）** | 上传文件生命周期归 gradio 管，任务不应依赖其持久性 | §6.2 每任务快照 |
| **大量 1035 页文档** | magic-pdf 首批就渲染 200 页图像，CPU 下耗时/内存高（本次峰值 13.7GB） | 建议先 `--pages` 子集验证；或选用 `layoutlmv3` 等轻量版面模型 |

---

## 8. 附录：关键代码位置索引

| 主题 | 位置 |
|---|---|
| CLI 引擎路由 | `pdf2zh/pdf2zh.py` `main()` 463-472、`resolve_parse_engine` 475-484、`_try_auto_switch_magicpdf` 487-541、`_run_legacy_kernel` 544-593 |
| GUI 解析引擎下拉 | `pdf2zh/gui/components/config_panel.py` 181-197（`parse_engine` Radio + `magicpdf_ocr`） |
| 服务层四路路由 | `pdf2zh/services/runtime_service.py` `_execute_task` 891-907；`_execute_magicpdf` 1641-1712；`_execute_babeldoc` 1759+；`_execute_legacy` 1403+；`out_dir` 默认 1667-1669 / 1809 |
| magicpdf 执行器 | `pdf2zh/magicpdf_cli.py` `run_magicpdf_main` 79-201、`_fallback_legacy` 32-37、`_output_dir` 25-29 |
| 双后端适配器 | `pdf2zh/magicpdf_adapter.py` `MagicPdfAdapter.backend/is_available/parse` 262-299、`_parse_magicpdf` 301-338、`_parse_mineru` 340-379、离线 `load_middle_json` 180 |
| 环境探测 | `pdf2zh/engine_env.py` `available_backend` 78-88、`probe_magicpdf/probe_mineru` 51-70 |
| 扫描/损坏预检 | `pdf2zh/scanned_detection.py` `preflight_scan_check`（`font_to_unicode` 信号 574-579） |
| BabelDOC ONNX 后端 | `pdf2zh/babeldoc_onnx_backend.py` 提供 cuda/dml/cpu 解析 + CPU 回退（92-151） |
| doclayout 后端开关 | `pdf2zh/doclayout.py` `set_backend` 50-63、`_sync_babeldoc_backend` 66-81 |
| magic-pdf 配置读取 | `site-packages/magic_pdf/libs/config_reader.py` `read_config` 15-27（缺文件抛 `FileNotFoundError`） |
| magic-pdf 模型懒加载 | `site-packages/magic_pdf/model/doc_analyze_by_custom_model.py` `custom_model_init`（full 模式）与 `doc_analyze` 133-162 |
| 测试 | `tests/test_parse_engine_switch.py`（9 项） |
| 依赖声明 | `pyproject.toml` `[project.optional-dependencies] magicpdf = ["mineru>=2.0", "magic-pdf>=1.3.12,<2"]` |

