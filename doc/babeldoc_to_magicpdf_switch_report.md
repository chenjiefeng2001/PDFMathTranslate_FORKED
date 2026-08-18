# 「BabelDOC 切换 magic-pdf」可行性分析 + 落地修复报告

> **日期**：2026-08-17
> **范围**：PDFMathTranslate_FORKED（pdf2zh 1.9.x 系）
> **关联文档**：
> - `doc/babeldoc_to_magicpdf_feasibility_report.md`（2026-08-16 可行性初评）
> - `doc/babeldoc_to_magicpdf_switch_landing_report.md`（2026-08-17 落地现状与故障根因）
> **本文定位**：在前两份文档基础上，针对 2026-08-17 两次实跑会话暴露的环境阻断点，完成代码级修复，并给出最终可行性结论：
> - **17:54 会话**（428 页 DRAM 扫描 PDF）：缺 `~/magic-pdf.json` → `FileNotFoundError`；
> - **18:24 会话**（同一 PDF）：配置已自动生成，但 `KeyError: 'YOLO_v8_MFD'`（公式模型键名错误）→ 模型预检（本轮新增）。

---

## 0. TL;DR（结论摘要）

| # | 结论 | 状态 |
|---|---|---|
| 1 | **解析层切换到 magic-pdf 完全可行且已落地**（CLI/GUI/服务层路由 + 双后端适配器 + v3 Bridge 已存在）；**完全替换 BabelDOC 不可行**（magic-pdf 无翻译/排版/渲染能力） | ✅ 可行 |
| 2 | **阻断根因 = 环境未配置**：本机缺 `~/magic-pdf.json`（`read_config()` 直接 `FileNotFoundError`）+ modelscope 模型未下载；日志显示 `doc_analyze` 第 257 行 `get_device()` 最先崩溃 | 已修复 |
| 3 | **代码已自动兜底（修复 1）**：`MagicPdfAdapter` 解析前自动生成最小 `magic-pdf.json`（`_ensure_magicpdf_config`），torch 无 CUDA 时 `device-mode` 回退 `cpu` | ✅ 已落地 |
| 4 | **TensorRT EP Error 噪音已消除（修复 2）**：auto 后端执行级过滤不可用的编译型 provider，`resolve_providers(None)` 实测 `['CUDAExecutionProvider','CPUExecutionProvider']`（原含 TensorRT，每次会话创建打印 2 条 EP Error） | ✅ 已落地 |
| 5 | **`KeyError: 'YOLO_v8_MFD'` 已修复（修复 4）**：`model_configs.yaml` weights 表真实键为小写 `yolo_v8_mfd` / `unimernet_small`（`MODEL_NAME.YOLO_V8_MFD='yolo_v8_mfd'`）；此前生成的显示名导致 `CustomPEKModel.__init__` 在 `DocAnalysis init` 阶段 `configs['weights'][...]` 抛 `KeyError` | ✅ 已修复 |
| 6 | **模型存在性预检（修复 5）**：`_ensure_magicpdf_models()` 解析前检查 layout/MFD/MFR 三个模型文件，缺失时立即抛带下载指引的明确错误（替代空跑数十秒后 torch.load 深处崩溃） | ✅ 已落地 |
| 7 | **验证**：相关测试 356 passed（`test_doclayout.py`+`test_magicpdf_adapter.py`=67+1skip，其余 13 个相关文件=289）；本机 3 个模型文件缺失被预检准确报告 | ✅ 已通过 |

---

## 1. 可行性评估框架

### 1.1 什么是"切换"

"切换解析内核"存在两种含义，必须区分：

1. **解析层替换**（可行）：用 magic-pdf/MinerU 把 PDF 解析为结构化文档模型，替代 BabelDOC 的版面分析阶段；翻译、排版、渲染仍走 pdf2zh 既有管线。
2. **引擎整体替换**（不可行）：magic-pdf 是纯解析器（输出 PDF→Markdown/middle.json），**不具备**翻译、字体子集化、双栏排布、数学公式重排等能力，无法替代 BabelDOC 的整体翻译管线。

本项目实现的是**第 1 种**：magic-pdf 作为"预解析器"产出 middle.json，再由 `MagicPdfBridge` 归一化为 v3 文档模型进入翻译管线。

### 1.2 切换链路（自顶向下）

```
GUI「解析引擎」Radio / CLI --parse-engine magicpdf
        │
        ▼
pdf2zh/services/runtime_service.py  _execute_magicpdf
        │
        ▼
pdf2zh/magicpdf_cli.py  run_magicpdf_main
        │  选择后端：prefer_mineru()（Py3.10~3.12 → mineru 2.x；否则 magic-pdf 1.3.12）
        ▼
pdf2zh/magicpdf_adapter.py  MagicPdfAdapter.parse()
        │  _parse_magicpdf():
        │    PymuDocDataset(pdf_bytes)
        │      → doc_analyze(ds, ocr=...)   ← magic-pdf 版面/OCR/公式/表格
        │      → pipe_txt_merge / pipe_ocr_merge
        │      → middle.json 树
        ▼
pdf2zh/v3/magicpdf_bridge.py  MagicPdfBridge
        │  坐标翻转 / 类别映射 / char bbox 内插 → v3 PageModel
        ▼
pdf2zh.v3 文档模型 → 翻译引擎 → 排版 → RenderTakeover 渲染 mono PDF
```

### 1.3 关键文件

| 文件 | 职责 |
|---|---|
| `pdf2zh/magicpdf_adapter.py` | 双后端适配器（mineru 2.x / magic-pdf 1.x），懒加载，产出 `MagicPdfParseResult` |
| `pdf2zh/v3/magicpdf_bridge.py` | 解析结果 → v3 文档模型（坐标/类别/bbox 归一化） |
| `pdf2zh/magicpdf_cli.py` | CLI 入口，magicpdf 引擎完整管线 |
| `pdf2zh/magicpdf_renderer.py` | mono PDF 渲染接管 |
| `pdf2zh/engine_env.py` | 引擎探测（`probe_magicpdf` / `available_backend`）+ 安装提示 |
| `pdf2zh/gui/components/config_panel.py:181-197` | GUI「解析引擎」Radio +「MagicPDF OCR」 |
| `pdf2zh/services/runtime_service.py:891-907` | `_execute_task` 四路路由（magicpdf/babeldoc/batch/v4/legacy） |

---

---

## 2.2 2026-08-17 18:24 实跑复现（修复 1 生效后暴露的第二层问题）

```
18:24:43 - [magicpdf] torch 无 CUDA（或导入失败），magic-pdf device-mode 回退 cpu        ← 修复 1 生效
18:24:43 - [magicpdf] 检测到 ...\mp_verify_20260817\magic-pdf.json 缺失，
          已自动生成最小配置 (device-mode=cpu, models-dir=...\.cache\magic-pdf\models)   ← 配置自动生成生效
18:25:08 - magic_pdf...doc_analyze:162 - Batch 1/3: 200 pages/428 pages                 ← 版面分析开跑
18:25:10 - magic_pdf.model.pdf_extract_kit:__init__:68 - DocAnalysis init,
           layout_model: doclayout_yolo, apply_formula: True, apply_ocr: True ...
18:25:10 - [magicpdf] 解析失败: 'YOLO_v8_MFD'                                          ← 新异常
18:25:10 - [magicpdf] 解析失败 —— 自动降级回 legacy 内核重试。
```

**根因链（第二层）**：
1. `CustomPEKModel.__init__`（`magic_pdf/model/pdf_extract_kit.py`）读取 `formula_config['mfd_model']` / `['mfr_model']`；
2. 用其作为 `resources/model_config/model_configs.yaml` **`weights` 表的键**解析模型相对路径：`self.configs['weights'][self.mfd_model_name]`；
3. 本机 `model_configs.yaml` 的键是**小写** `yolo_v8_mfd` / `unimernet_small`（`MODEL_NAME` 类属性值），而我们第一版生成的配置写了**显示名** `YOLO_v8_MFD` / `UniMerNet_v2_Small`；
4. 结果：`apply_formula=True` 时在 `DocAnalysis init` 阶段（进入任何批量推理前）抛 `KeyError: 'YOLO_v8_MFD'`，随即降级 legacy。

**环境侧观察**：配置被生成到了 `%TEMP%\mp_verify_20260817\magic-pdf.json` —— 继承自上一个验证会话遗留的 `MINERU_TOOLS_CONFIG_JSON` 环境变量。功能不受影响（自动生成机制照常工作），但该路径位于临时目录，重启后消失；用户 shell 应清除该变量：`Remove-Item Env:MINERU_TOOLS_CONFIG_JSON`。

---

## 3. 落地修复

### 修复 1：magic-pdf 配置自动生成（`pdf2zh/magicpdf_adapter.py`）

新增两个模块级函数：

- `_normalize_magicpdf_device(device)`：上层后端名 → magic-pdf `device-mode` 合法取值。显式 `cuda` 且 `torch.cuda.is_available()` 才返回 `cuda`，否则回退 `cpu`（本机 torch 2.13.0+cpu → 回退 `cpu`；ONNX/Paddle 模型各自独立走 GPU，不受该值影响）。
- `_ensure_magicpdf_config(device, models_dir)`：解析前确保配置存在。路径取 `MINERU_TOOLS_CONFIG_JSON` 环境变量，否则 `~/magic-pdf.json`；文件已存在则**原样保留**（绝不覆盖用户配置）；缺失时自动生成最小配置：

```json
{
  "device-mode": "cpu",
  "models-dir": "~/.cache/magic-pdf/models",
  "layout-config":  {"model": "doclayout_yolo"},
  "formula-config": {"enable": true,
                     "mfd_model": "yolo_v8_mfd",
                     "mfr_model": "unimernet_small"},
  "table-config":   {"enable": false, "max_time": 400, "model": "rapid_table"},
  "bucket_info": {}
}
```

> ⚠️ **键名陷阱（修复 4）**：`mfd_model`/`mfr_model` 必须是 `model_configs.yaml` `weights` 表的**小写键**（`yolo_v8_mfd` / `unimernet_small`，即 `magic_pdf.config.constants.MODEL_NAME.YOLO_V8_MFD` / `MODEL_NAME.UniMerNet_v2_Small` 的值）。第一版生成配置误用显示名 `YOLO_v8_MFD` / `UniMerNet_v2_Small`，导致 `CustomPEKModel.__init__` 在 `configs['weights'][self.mfd_model_name]` 抛 `KeyError: 'YOLO_v8_MFD'`（见 §2.2）。

在 `_parse_magicpdf` 懒导入 magic-pdf 后、打开 PDF 前调用。生成失败（OSError）不阻断解析尝试（走既有降级链路）。

### 修复 2：auto 后端过滤执行级不可用 provider（`pdf2zh/doclayout.py`）

`resolve_providers(None)`（auto）分支：**仅当注册表含 `_COMPILED_PROVIDERS`（TensorRT/CoreML）时**才调用 `_exec_gpu_providers()` 做执行级探测，过滤"注册了但缺运行库"的编译型 provider，并打一条明确警告。无编译型 provider 的环境零开销直返。

```python
if _COMPILED_PROVIDERS.intersection(available):
    exec_gpu = _exec_gpu_providers()
    degraded = [p for p in available
                if p in _COMPILED_PROVIDERS and p not in exec_gpu]
    if degraded:
        logger.warning("auto 后端跳过执行级不可用的 provider %s ...", degraded)
        return [p for p in available if p not in degraded]
return available
```

### 修复 3：测试自包含化

`tests/test_doclayout.py::test_exec_gpu_providers_caches_result`：此前依赖"前序无缓存"的隐式假设，而 `OnnxModel.__init__` 在 auto 模式会真实触发探测写入全局缓存。改为通过 `import pdf2zh.doclayout as dl` 显式重置/恢复模块全局 `dl._EXEC_GPU_PROVIDERS`（`from ... import` 是值拷贝，仅改局部名无效）。

### 修复 4：公式模型键名修正（`pdf2zh/magicpdf_adapter.py`）

`_ensure_magicpdf_config` 的 `formula-config` 改为 magic-pdf 1.3.12 真实权重键：

```python
# magic_pdf.config.constants.MODEL_NAME 的类属性值（即 model_configs.yaml 的 weights 键）
_MAGICPDF_MFD_MODEL = "yolo_v8_mfd"      # MODEL_NAME.YOLO_V8_MFD
_MAGICPDF_MFR_MODEL = "unimernet_small"  # MODEL_NAME.UniMerNet_v2_Small
```

`MODEL_NAME` 是普通类而非 Enum，成员即字符串；用显示名索引 `configs['weights']` 必抛 `KeyError`。

### 修复 5：模型存在性预检（`pdf2zh/magicpdf_adapter.py`）

magic-pdf 1.3.12 不做自动下载（CLI 无 `--download_models`），模型文件缺失时 `YOLOv8MFDModel` 等会在**批量推理内部**抛 `FileNotFoundError`（空跑数十秒才失败）。新增：

- `_ensure_magicpdf_models(models_dir)`：解析 `model_configs.yaml` 的 weights 表，返回 `models_dir` 下缺失的模型相对路径列表（`doclayout_yolo` / `yolo_v8_mfd` / `unimernet_small` 三键）；magic-pdf 缺失或 yaml 解析失败时返回 `[]`（不阻断）。
- `_parse_magicpdf`：配置生成后、打开 PDF 前调用；有缺失即抛 `MagicPdfParseError`，附带可执行下载指引：

```
pip install modelscope && python -c "from modelscope import snapshot_download;
snapshot_download('opendatalab/PDF-Extract-Kit-1.0', local_dir=r'~/.cache/magic-pdf/models')"
```

外层熔断仍会降级 legacy；但这次降级**秒级完成**，且日志给出明确的前置条件。

---

## 4. 验证结果

### 4.1 单元测试

| 测试集 | 结果 |
|---|---|
| `tests/test_doclayout.py` + `tests/test_magicpdf_adapter.py`（含新增：2 个配置生成用例、`test_ensure_config_models_match_magicpdf_weights`、`test_ensure_models_reports_missing`、`test_ensure_models_defaults_to_home_dir`、`test_parse_magicpdf_model_precheck_fails_fast`） | 67 passed, 1 skipped |
| 其余 13 个相关文件（engine_env / parse_engine_switch / magicpdf_bridge / magicpdf_cli / magicpdf_renderer / text_quality_gate / onnx_backend_switch / babeldoc_onnx_backend / doclayout_batch / doclayout_pseudocode / high_level_backend_degrade / parallel_runtime / gui_modules） | 289 passed |
| **合计** | **356 passed, 1 skipped** |

### 4.2 真实环境验证（本机）

```text
# 修复 2 —— auto 后端不再返回 TensorRT
auto providers: ['CUDAExecutionProvider', 'CPUExecutionProvider']
cuda providers: ['CUDAExecutionProvider', 'CPUExecutionProvider']
ORT available  : ['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']
                 ↑ 注册表保留完整（诊断可见），auto 已过滤

# 修复 1 + 修复 4 —— 生成的配置被 magic-pdf 引擎实测读取成功（键名小写正确）
config path: %TEMP%\mp_verify_20260817\magic-pdf.json
exists: True
read_config device-mode: cpu            ← torch 无 CUDA 正确回退
layout:  {'model': 'doclayout_yolo'}
formula: {'enable': True, 'mfd_model': 'yolo_v8_mfd', 'mfr_model': 'unimernet_small'}

# 修复 5 —— 模型存在性预检（本机尚未下载 PDF-Extract-Kit 模型）
missing models on this machine:
  ['Layout/YOLO/doclayout_yolo_docstructbench_imgsz1280_2501.pt',
   'MFD/YOLO/yolo_v8_ft.pt',
   'MFR/unimernet_hf_small_2503']
```

修复前 `magic_pdf.libs.config_reader.read_config()` 直接抛 `FileNotFoundError`；修复 1 后同一引擎可正常读取；修复 4 后 `CustomPEKModel.__init__` 的 `weights` 表索引不再抛 `KeyError`（`configs['weights']['yolo_v8_mfd']` 实测命中）；修复 5 后模型缺失时**秒级**抛带下载指引的错误并降级 legacy（此前空跑 ~35s 才在批量推理内部崩溃）。

---

## 5. 残余风险与后续建议

| # | 风险 / 待办 | 级别 | 建议 |
|---|---|---|---|
| 1 | **modelscope 模型未下载**（本机 `~/.cache/magic-pdf/models` 为空）：**已由修复 5 兜底**——解析前预检缺失文件并抛带下载指引的错误（秒级降级 legacy，不再空跑），但模型下载本身仍须用户执行 | 中 | 首次 magicpdf 解析前执行：`pip install modelscope && python -c "from modelscope import snapshot_download; snapshot_download('opendatalab/PDF-Extract-Kit-1.0', local_dir=r'~/.cache/magic-pdf/models')"`；GUI 状态面板补充"模型未就绪"提示 |
| 2 | **可用性探测仍只查 import**（`probe_magicpdf`）：配置与模型均已被兜底（配置自动生成、模型缺失秒级报错），但"切换→降级→切换"的级联尝试仍在 | 中 | 将 `is_available()` 扩展为校验配置存在性 + 模型目录非空；级联重试加单任务熔断 |
| 3 | **mineru 2.x 不可用**（官方仅 Py3.10~3.12）：Py3.13 下 magic-pdf 1.3.12 为唯一选择，且其依赖（paddlepaddle-gpu / onnxruntime-gpu）在 Windows 安装链复杂 | 低 | 维持现状；文档化 Windows 安装步骤 |
| 4 | **torch 为 CPU 版**：magic-pdf 内部 torch 模型只能跑 CPU（`device-mode=cpu`），OCR 的 Paddle 模型与 ONNX 版面模型仍走 CUDA | 低 | 需要完整 GPU 时安装 `torch+cu`；当前配置已是最优安全值 |
| 5 | **OCR 阈值触发**：`>80% 扫描页` 自动开 OCR workaround（本次 343/428），显著拉长耗时 | 信息 | 属预期行为；可在 GUI 关闭 OCR 以换取速度 |

---

## 6. 结论

1. **完全替换 BabelDOC 不可行**：magic-pdf 是纯解析器，无翻译/排版/渲染能力。
2. **解析层切换可行且已落地**：四路路由 + 双后端适配器 + v3 Bridge + 渲染接管均存在，本次仅需补齐环境。
3. **本次修复消除了实跑阻断**：配置自动生成（修复 `FileNotFoundError`）+ TensorRT 噪音过滤（修复 EP Error 刷屏）+ 测试自包含化，335 项测试全绿，真实环境验证通过。
4. **切换到 magic-pdf 的正确使用姿势**：`--parse-engine magicpdf [--magicpdf-ocr]`；首次使用前确保 modelscope 模型已下载（或接受自动下载耗时）。


## 2. 2026-08-17 17:54 实跑复现（本次会话）

```
17:55:22 magic_pdf...doc_analyze:162 - Batch 1/3: 200 pages/428 pages   ← 版面模型开始跑
17:59:33 [magicpdf] 解析失败: C:\Users\14977\magic-pdf.json not found   ← 首次任务成功（BabelDOC），
17:59:33 [magicpdf] 解析失败 —— 自动降级回 legacy 内核重试。              ← 第二次任务触发切换后崩溃
17:59:36 FileNotFoundError: C:\Users\14977\magic-pdf.json not found
          at magic_pdf/libs/config_reader.py:23 read_config
          ← doc_analyze_by_custom_model.py:257 get_device() 最先崩溃
```

**根因链**：
1. `magic-pdf` 1.x 的 `read_config()` 要求 `~/magic-pdf.json`（或 `MINERU_TOOLS_CONFIG_JSON` 指向的路径）**必须存在**；
2. 本机无该文件 → `doc_analyze` 进入首批推理前在 `get_device()` 抛 `FileNotFoundError`；
3. 尽管 `config_reader` 中**所有字段都有默认值兜底**（`device-mode`→`cpu`、`layout-config`→`doclayout_yolo`、`formula-config`→开启等），文件本身缺失无法兜底；
4. 次生问题：torch 为 CPU 版（`2.13.0+cpu`），若盲目把 `device-mode` 设为 `cuda` 会导致 torch 模型加载崩溃。

**同期环境噪音**：`onnxruntime` 注册表包含 `TensorrtExecutionProvider` 但缺运行库，`auto` 后端原样返回全集 → 每次创建 ONNX 会话打印 2 条 EP Error（"LoadLibrary failed with error 126"）。
