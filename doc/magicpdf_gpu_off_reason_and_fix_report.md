# magic-pdf 未使用 GPU：根因分析与修复报告

- **日期**：2026-08-19
- **问题**：magic-pdf 解析引擎全程在 CPU 上运行，未使用 GPU（BabelDOC 的 ONNX 版面模型已走 CUDA）。
- **结论**：**根本原因是本机 PyTorch 为 CPU 版**（`torch 2.13.0+cpu`，`torch.cuda.is_available() = False`）。magic-pdf 1.3.12 的全部子模型都是 PyTorch 实现，统一从 `~/magic-pdf.json` 的 `device-mode` 取值；CPU 版 torch 无法提供 CUDA，`device-mode` 被强制回退 `cpu` → 全链路 CPU。仅安装 `onnxruntime-gpu` 无法解决。本次已修复项目侧的两处配套缺陷，并新增一键诊断能力。

---

## 1. 摘要

| # | 结论 / 修复 | 级别 | 状态 |
|---|---|---|---|
| 1 | **根因 = 本机 torch 为 CPU 版**：`torch.cuda.is_available()` 为 `False`，magic-pdf 的 `device-mode` 无 GPU 可指向 | 阻断 | 需用户安装 CUDA 版 torch（本次已给出可执行命令与显式诊断） |
| 2 | **配套缺陷 A**：`_ensure_magicpdf_config` 仅在配置缺失时生成，**已存在 `~/magic-pdf.json` 的 `device-mode` 永不更新**——即使以后装好 CUDA torch，配置仍停留 `cpu` | 阻断 | ✅ 已修复：新增 `_sync_magicpdf_device_mode`，请求 `cuda` 且 torch CUDA 可用时自动把已有配置升级为 `cuda` |
| 3 | **配套缺陷 B**：`_normalize_magicpdf_device` 回退 `cpu` 时只给一句无操作指引的警告，且无任何"magic-pdf 实际用何设备"的诊断出口 | 中 | ✅ 已修复：警告附 CUDA torch 安装命令；新增 `get_magicpdf_device_status()`；CLI 启动打印 `[magicpdf] device status: ...`；GUI 状态面板新增「MagicPDF 解析设备」行 |
| 4 | **认识修正**：此前文档称"ONNX / Paddle 模型各自独立走 GPU"——实测 magic-pdf 1.3.12 的 doclayout_yolo/OCR 均为 **PyTorch** 实现（ultralytics / paddleocr2pytorch），不存在独立 ONNX/Paddle 执行链路 | — | ✅ 已修正 |

---

## 2. 问题现象与日志佐证

近期运行 `--parse-engine magicpdf` 时日志出现：

```
2026-08-17 18:24:43,158 - WARNING - [magicpdf] torch 无 CUDA（或导入失败），
                                 magic-pdf device-mode 回退 cpu
2026-08-18 ... INFO - magic_pdf.model.pdf_extract_kit:__init__:82 - using device: cpu
```

其中 `using device: cpu` 即 `CustomPEKModel` 收到的 `device` 参数（来自配置 `device-mode`），magic-pdf 所有子模型按此初始化，因此版面/公式/OCR/阅读序全部在 CPU 执行。

同一时段 BabelDOC 自身的 ONNX 版面模型日志为 `providers=['CUDAExecutionProvider','CPUExecutionProvider']`——**GPU 只作用于 BabelDOC 的 ONNX 链路，magic-pdf 解析链路完全不受影响**，造成"看起来有 GPU、magic-pdf 却不用"的错觉。

本机实测：

```
torch 2.13.0+cpu
cuda available: False
cuda version: None
```

---

## 3. 根因分析

### 3.1 magic-pdf 的设备决策链路（单一事实源：`device-mode`）

magic-pdf 1.3.12 全链路设备均由 `~/magic-pdf.json`（或 `MINERU_TOOLS_CONFIG_JSON`）的 **`device-mode`** 决定：

1. `magic_pdf/libs/config_reader.py::get_device()` → 直接返回配置中的 `device-mode`（缺失时默认 `cpu`）。
2. `doc_analyze_by_custom_model.py::custom_model_init()` → `device = get_device()` 传入 `CustomPEKModel(**model_input)`。
3. `pdf_extract_kit.py::CustomPEKModel.__init__` 打印 `using device: {device}`，随后把 `device` 传给**每一个**原子模型：

| 原子模型 | 实现框架 | 设备入口 |
|---|---|---|
| doclayout_yolo（版面） | ultralytics YOLOv10（PyTorch） | `DocLayoutYOLOModel.predict(..., device=self.device)` |
| MFD（公式检测） | YOLOv8（PyTorch） | `mfd_model_init(weights, device)` |
| MFR（公式识别） | UniMerNet（PyTorch） | `mfr_model_init(weight_dir, cfg, device)` |
| OCR det/rec | paddleocr2pytorch（纯 PyTorch 重实现） | `PytorchPaddleOCR` 内 `device = get_device()` |
| ReadingOrder（阅读序） | transformers LayoutLMv3 | 由 `device` 统一初始化 |

> 实测确认：magic-pdf 1.3.12 **没有独立 ONNX / Paddle 执行链路**（doclayout_yolo 走 ultralytics，OCR 走 paddleocr2pytorch）。因此"装 onnxruntime-gpu / DirectML 就能让 magic-pdf 用 GPU"是不成立的——**必须先有 CUDA 版 PyTorch**。

### 3.2 为什么 `device-mode` 只能是 `cpu`

项目在 `_normalize_magicpdf_device` 中做安全回退：

```python
if device == "cuda":
    if _torch_cuda_available():
        return "cuda"
    logger.warning(...)   # 回退 cpu
    return "cpu"
```

本机 `torch.cuda.is_available()` 为 `False`（torch 为 CPU 构建），因此无论用户请求 `--backend cuda` 还是 GUI 后端 CUDA，`device-mode` 都被强制写成 `cpu`。该回退是**必要保护**：`device-mode=cuda` + CPU torch 会让 ultralytics / YOLOv8 在推理时抛 `CUDA not available` 直接崩溃。

### 3.3 项目侧配套缺陷（本次修复）

1. **已有配置永不更新**：`_ensure_magicpdf_config` 的既有逻辑是 `if os.path.exists(cfg_file): return cfg_file`。用户环境从 CPU 升级为 CUDA torch 后，`~/magic-pdf.json` 的 `device-mode` 仍为 `cpu`，magic-pdf 继续跑 CPU。
2. **诊断缺失**：没有一处出口能直接看到"magic-pdf 实际用什么设备"，用户只能翻日志猜；回退警告也未给出安装命令。

### 3.4 与 BabelDOC ONNX 后端的关系（独立路径）

| 路径 | 设备来源 | 本机状态 |
|---|---|---|
| BabelDOC 版面（doclayout ONNX） | `PDF2ZH_BABELDOC_BACKEND` / `--backend` → ORT providers | ✅ CUDA（`CUDAExecutionProvider` 已生效） |
| magic-pdf 解析（全部子模型） | `~/magic-pdf.json` → `device-mode` | ❌ CPU（torch 无 CUDA） |

两者完全独立：ONNX 用上 GPU **不能**让 magic-pdf 用上 GPU。

---

## 4. 本次修复内容

### 4.1 `pdf2zh/magicpdf_adapter.py`

- **`_normalize_magicpdf_device`** 增强：
  - `auto`：torch CUDA 可用 → `cuda`，否则 `cpu`（新建配置时自动择优）；
  - `cuda`：无 CUDA 时回退 `cpu`，警告附 CUDA torch 安装命令；
  - `dml`：明确提示 magic-pdf 的 torch 模型不支持 DirectML，回退 `cpu`；
  - 修正 docstring 中"ONNX/Paddle 独立走 GPU"的不准确表述。
- **新增 `_torch_cuda_available()` / `_torch_version()`**：懒导入、全异常容错的 torch 探测。
- **新增 `_cuda_torch_install_hint()`**：可执行安装命令（`pip install -U "torch" --index-url https://download.pytorch.org/whl/cu126`，按本机 CUDA 选 cu121/cu124/cu126）。
- **新增 `_sync_magicpdf_device_mode(cfg_file, device)`**：已存在配置时——
  - 显式请求 `cuda` 且 torch CUDA 可用且现有值非 `cuda` → 补写 `cuda`（其余键保留）；
  - 请求 `auto`/`cpu` → 不覆盖用户设置；
  - 配置 `cuda` 但 torch 无 CUDA → 本次按 `cpu` 运行并告警，保留用户配置意图。
- **`_ensure_magicpdf_config`**：已存在配置时调用 `_sync_magicpdf_device_mode` 同步设备，不再"只看不写"。
- **新增 `get_magicpdf_device_status(requested="auto")`**：返回 `installed / torch / torch_cuda / requested / config_file / device_mode / effective / hint` 的诊断 dict。

### 4.2 `pdf2zh/magicpdf_cli.py`

解析前打印 `[magicpdf] device status: requested=... torch=... torch_cuda=... device-mode=... effective=...`，未走 GPU 时追加 `[magicpdf] ...` 安装指引告警。

### 4.3 `pdf2zh/gui/components/config_panel.py` + `pdf2zh/gui/i18n.py`

- `backend_status_markdown()` 在 magic-pdf 已安装时新增「MagicPDF 解析设备 / MagicPDF parse device」行：显示 `effective`、torch 版本、`cuda=True/False`，以及未启用 GPU 时的修复提示。
- i18n 新增键：`backend_status_magicpdf_device`、`backend_status_magicpdf_hint`（中/英）。

### 4.4 文档

- `README.md`：GPU 一节新增 **magic-pdf GPU** bullet（CUDA torch 硬前提、`--backend cuda` 自动升级配置、dml 不适用、诊断出口）。
- `docs/ADVANCED.md`：magicpdf 一节新增 **GPU** 小节（含安装命令与验证步骤）。
- `docs/README_zh-CN.md`：新增 **magic-pdf GPU（独立执行设备）** bullet。

### 4.5 单元测试（`tests/test_magicpdf_adapter.py`）

新增 `TestMagicPdfDevice` 类（8 项），并把既有 `test_ensure_config_keeps_existing` 加固为显式 mock torch 无 CUDA：

| 测试 | 验证点 |
|---|---|
| `test_auto_with_cuda_returns_cuda` / `test_auto_without_cuda_returns_cpu` | `auto` 探测择优 |
| `test_cuda_without_torch_falls_back_cpu` | `cuda` + CPU torch → 回退 `cpu` + 告警 |
| `test_dml_falls_back_cpu` | `dml` 对 magic-pdf 无效 |
| `test_sync_upgrades_existing_cpu_to_cuda` | 请求 cuda + torch CUDA 可用 → 已有配置补写 `cuda`，其余键保留 |
| `test_sync_keeps_user_cpu_on_auto` | `auto` 不覆盖用户手动 `cpu` |
| `test_sync_cuda_config_without_torch_falls_back_cpu` | 配置 `cuda` + torch 无 CUDA → 本次按 `cpu` 且保留配置 |
| `test_get_magicpdf_device_status_fields` | 诊断 dict 字段齐全、值与配置一致 |

---

## 5. 让 magic-pdf 真正使用 GPU：操作步骤

```bash
# 1) 安装 CUDA 版 PyTorch（硬前提；按本机 CUDA 版本选 cu121/cu124/cu126）
python -m pip install -U "torch" --index-url https://download.pytorch.org/whl/cu126

# 2) 验证（必须输出 True）
python -c "import torch; print(torch.cuda.is_available())"

# 3) 用 cuda 后端跑 magicpdf（会自动把 ~/magic-pdf.json 的 device-mode 升级为 cuda）
pdf2zh --parse-engine magicpdf --backend cuda example.pdf
```

GUI：后端单选选 **CUDA（NVIDIA GPU）** 后提交即可；解析前 CLI 会打印
`[magicpdf] device status: ... effective=cuda ...`，GUI 状态面板显示
「MagicPDF 解析设备: `cuda` · torch 2.x.x+cuXXX · cuda=True」。

校验成功的标志（日志）：

```
[magicpdf] device status: requested=cuda torch=2.13.0+cu126 torch_cuda=True device-mode=cuda effective=cuda
magic_pdf.model.pdf_extract_kit:__init__:82 - using device: cuda
```

---

## 6. 验证结果

| 项 | 结果 |
|---|---|
| `tests/test_magicpdf_adapter.py`（含新增 8 项） | ✅ 28 passed, 1 skipped |
| `tests/test_engine_env.py` | ✅ 通过 |
| `tests/test_magicpdf_cli.py` | ✅ 通过 |
| `tests/test_parse_engine_switch.py` | ✅ 通过 |
| `tests/test_gui_modules.py`（i18n + 状态面板 import/渲染） | ✅ 通过 |
| 真实环境诊断输出（本机 torch CPU 版） | ✅ `effective=cpu`、`hint` 含安装命令，与根因一致 |

---

## 7. 遗留风险与注意事项

1. **CUDA torch 依赖冲突**：本项目锁定了 magic-pdf 1.x 依赖组；升级 torch 为 CUDA 版可能影响其它依赖的 wheel 解析（尤其 Windows 下 `pymupdf`/`pdfminer` 冲突），建议用 `uv` + 仓库内置依赖覆写执行，见 `docs/ADVANCED.md`。
2. **NVIDIA 驱动 / CUDA 版本匹配**：`cu126` wheel 需要驱动支持 CUDA 12.x；显存不足时 MFD/MFR 批量推理可能 OOM（magic-pdf 的 `clean_vram` 阈值 6GB 可调）。
3. **transformers 兼容补丁**（`_patch_magicpdf_transformers_compat`）在 CUDA 下同样生效，无需额外处理。
4. **`dml` 对 magic-pdf 无效**：DirectML 仅作用于本项目/ BabelDOC 的 ONNX 会话；magic-pdf 的 torch 模型不认 `DmlExecutionProvider`，指定 `--backend dml` 时 magic-pdf 仍回退 CPU。
5. **`auto` 语义微调**：新建配置时 `auto` 现在会在 torch CUDA 可用时择优 `cuda`（此前固定 `cpu`）；已存在配置不受影响（不覆盖用户设置）。

