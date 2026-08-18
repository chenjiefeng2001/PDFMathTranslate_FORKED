# ONNX 后端"DML 被选中却实际跑 CPU"问题调查报告

- **日期**：2026-08-17
- **关联日志**：2026-08-17 12:19:21 ~ 12:19:24（GUI 任务，`backend=dml`）
- **调查方式**：代码走查 + 本机 ORT profiling 实测（onnxruntime 1.28.0）

---

## 一、现象

用户在 GUI 中选择 `dml`（DirectML）推理后端后，日志输出如下：

```
2026-08-17 12:19:22,391 - INFO - Loading ONNX model...
2026-08-17 12:19:23,516 - INFO - BabelDOC doclayout ONNX providers=['AzureExecutionProvider', 'CPUExecutionProvider'] (backend=dml)
2026-08-17 12:19:24,695 - INFO - BabelDOC doclayout ONNX providers=['AzureExecutionProvider', 'CPUExecutionProvider'] (backend=dml)
```

从日志看，会话 provider 列表包含 `AzureExecutionProvider`（DirectML 新名），似乎 DML 已生效；
但**实际推理速度仍是 CPU 水平**（1035 页文档版面阶段约 3.65~4.15s/页，与 04:03 那次明确回退 CPU 的速度一致）。
用户感知为"ONNX 模型莫名其妙切换到 CPU"。

与 04:03 日志对比，差异在于：
| 时间 | 后端 | 日志表现 | 是否暴露问题 |
|---|---|---|---|
| 04:03:44 | `cuda` | `Backend 'cuda' was requested but the ONNX session fell back to CPU (...)` **warning** | ✅ 显式暴露 |
| 12:19:23 | `dml` | `providers=['AzureExecutionProvider', 'CPUExecutionProvider'] (backend=dml)` 无任何 warning | ❌ 完全静默 |

---

## 二、核心结论

**这不是"切换到 CPU"，而是"DML 从未真正生效、且被静默掩盖"。**

真实执行链是：用户在 GUI 选了 `dml` → `set_backend("dml")` → 会话以
`[AzureExecutionProvider, CPUExecutionProvider]` 创建 → 但 **735/816 个 ONNX 算子
一个都没有落到 AzureExecutionProvider 上，全部在 CPUExecutionProvider 执行**。
由于 onnxruntime 对 AzureExecutionProvider 的失效是**静默回退**（不抛异常、`get_providers()`
仍返回 Azure），现有全部检测代码都判定"DML 正常"，日志与 GUI 面板全部失真。

根因分四层（详见 §四）：

1. **环境级（主因）**：onnxruntime 1.28.0 中 `AzureExecutionProvider` 已注册，但
   DirectML 设备在本机实际初始化失败 → ORT 静默回退 CPU，并**自动把 CPUExecutionProvider
   附加进有效列表**（即便请求的是纯 Azure）。
2. **检测级（为什么没人发现）**：`_probe_providers()` 用最小 Relu 模型探测"effective
   providers"，而 ORT 的静默回退使最小模型同样返回 `[Azure, CPU]` → 探针失效 →
   `get_runtime_provider_status()['dml'] = True` 误报 → GUI 面板显示"DML 可用"。
3. **图优化级（潜在放大器）**：`_configure_session_options()` 固定
   `graph_optimization_level = ORT_ENABLE_ALL`，其中 NchwcTransformer（CPU 专用块布局优化）
   会把图变换为 NCHWc 节点。**即使 DML 设备可用，NCHWc 布局也会阻止 DirectML 接管算子**
   （onnxruntime 官方对 DirectML 推荐 `ORT_ENABLE_BASIC`）。profiling 实测所有节点均带
   `_nchwc_kernel_time` 后缀。
4. **缓存级（固化剂）**：`.optimized` 缓存文件名与 provider 无关，CPU 环境下生成的
   NCHWc 优化图缓存被后续 DML 会话直接复用（缓存 mtime `12:18:18`，距任务仅 1 分钟），
   把 CPU 执行固化下来。

---

## 三、实测证据链（本机探针）

### 3.1 环境

| 项 | 值 |
|---|---|
| onnxruntime | `1.28.0`（`C:\Python313\Lib\site-packages\onnxruntime`） |
| `get_available_providers()` | `['AzureExecutionProvider', 'CPUExecutionProvider']` |
| GPU | NVIDIA GeForce RTX 3070 Laptop GPU（驱动 32.0.16.1074） |
| doclayout 模型 | `C:\Users\14977\.cache\babeldoc\models\doclayout_yolo_docstructbench_imgsz1024.onnx`（75.3MB） |
| `.optimized` 缓存 | 存在，75.3MB，mtime `Mon Aug 17 12:18:18 2026`（任务前 1 分钟生成） |

> 注：本机同时安装了 `onnxruntime-gpu 1.20.2` 与 `onnxruntime 1.28.0`，二者在
> site-packages 冲突，**当前生效的是 1.28.0（CPU 发行版，Windows 上编译了 DML/EP 名）**，
> `onnxruntime_gpu` 模块不可 import → CUDAExecutionProvider 也不可用（对应 04:03 日志）。

### 3.2 真实 doclayout 模型推理耗时（1024×1024 单页，3 次平均）

| 配置 | provider 列表 | 平均耗时 |
|---|---|---|
| A. `ORT_ENABLE_ALL` + `[Azure, CPU]`（当前代码） | `[Azure, CPU]` | **712.5 ms** |
| B. `ORT_ENABLE_BASIC` + `[Azure, CPU]`（DML 官方推荐） | `[Azure, CPU]` | 978.9 ms |
| C. `ORT_ENABLE_ALL` + `[CPU]`（纯 CPU 基线） | `[CPU]` | **665.4 ms** |
| D. `ORT_ENABLE_ALL` + `[Azure, CPU]` 复用 `.optimized` 缓存 | `[Azure, CPU]` | 689.3 ms |
| E. `ORT_ENABLE_BASIC` + `[Azure, CPU]` 复用 `.optimized` 缓存 | `[Azure, CPU]` | 746.1 ms |

**解读**：所有含 Azure 的配置耗时都与纯 CPU 基线（665ms）同量级（700~980ms），
DML 没有带来任何加速。若 DML 真正生效，1024×1024 YOLO 推理应进入几十~百毫秒量级。


### 3.3 ORT profiling（节点级 provider 分配）

对真实 doclayout 模型（`ORT_ENABLE_ALL` + `[Azure, CPU]`）开启 profiling 后：

- **735 个执行节点，100% 在 `CPUExecutionProvider`**，`AzureExecutionProvider` 0 节点；
- 大量节点名形如 `/model.4/m.0/dilated_block/Conv_output_0_nchwc_kernel_time`——
  证实 NchwcTransformer（CPU 布局优化）已应用；
- 按 provider 聚合耗时：`CPUExecutionProvider = 676.5 ms`。

对 `ORT_ENABLE_BASIC` + `[Azure, CPU]` 复测：**816 个节点仍在 CPU**，排除 NCHWc
作为唯一原因 → 即使移除 NCHWc，DML 依然一个算子都不接管。

### 3.4 最小 Conv 模型（纯 Azure，无 CPU 兜底）

请求 `providers=["AzureExecutionProvider"]`（仅 DML）：

- `get_providers()` 返回 **`['AzureExecutionProvider', 'CPUExecutionProvider']`**
  —— ORT 检测到 DML 无法初始化后**自动附加了 CPU**；
- profiling 显示 Conv 节点 `provider=CPUExecutionProvider`（342us）。

**这是"静默回退"的最直接证据**：连最简单、DML 必然支持的 Conv 算子都无法在
AzureExecutionProvider 上执行。

### 3.5 现有检测机制实测

```python
from pdf2zh.doclayout import get_runtime_provider_status
get_runtime_provider_status()
# {
#   "onnxruntime": "1.28.0",
#   "available": ["AzureExecutionProvider", "CPUExecutionProvider"],
#   "effective":  ["AzureExecutionProvider", "CPUExecutionProvider"],  # 探针失真
#   "cuda": false,
#   "dml":  true,        # ← 误报：GUI 面板据此显示 "DML: 可用"
# }
```

---

## 四、根因分析与相关代码

### 4.1 环境级：ORT 对 DML 的静默回退（外部行为，代码无法捕获）

onnxruntime 1.20+ 将 DirectML EP 更名为 `AzureExecutionProvider`，并在 Windows CPU
发行版中注册。但 **DML 设备（D3D12）初始化失败时 ORT 不抛异常、不回退 provider
列表**，而是把算子静默分配给 CPUExecutionProvider（3.4 节已实测）。这使一切
"看 provider 列表"的检测全部失真。

### 4.2 检测级：`_probe_providers` 探针失效

- `pdf2zh/doclayout.py:247-276` `_probe_providers()`：用最小 Relu 模型做真实会话创建，
  但 ORT 的静默回退让最小模型的 `get_providers()` 同样返回 `[Azure, CPU]` →
  **探测结果与实际算子执行无关**；
- `pdf2zh/doclayout.py:279-302` `get_runtime_provider_status()`：`"dml": has_gpu_provider("dml", effective)` 
  → 恒为 `True`；
- `pdf2zh/gui/components/config_panel.py:71-102` `backend_status_markdown()`：直接渲染
  `status['dml']` → GUI 面板显示"DML: 可用"（误报），用户据此选择 dml；
- `pdf2zh/gui/components/config_panel.py:150-160`：backend Radio 的 `dml` 选项不受
  真实可用性约束（可被无脑选中）。

### 4.3 图优化级：`ORT_ENABLE_ALL` 与 DML 冲突

- `pdf2zh/doclayout.py:338-354` `_configure_session_options()`：固定
  `ORT_ENABLE_ALL`。该级别包含 NchwcTransformer（CPU 专用）。官方对 DirectML EP 的建议是
  `ORT_ENABLE_BASIC`——NCHWc 布局图 DirectML 无法消费；
- 3.3 节实测证实所有节点被 NCHWc 化（`_nchwc_kernel_time` 后缀）。本机因根因 4.1
  未到"DML 试图接管"这一步；但**在 DML 可用的机器上这是必然的第二个坑**。

### 4.4 缓存级：`.optimized` 缓存与 backend 解耦

- `pdf2zh/doclayout.py:633-678` `OnnxModel.__init__`：命中 `model_path + ".optimized"`
  缓存即直接用该文件建会话（行 656-659），**不校验缓存生成的 provider/优化级别**；
- `pdf2zh/doclayout.py:364-490` `_OptimizedCache`：缓存路径唯一，CPU 与 GPU 会话共用；
- `pdf2zh/doclayout.py:540-593` `ensure_model_prewarmed`：主进程预热时用当时 backend
  生成缓存（本次 12:18:18 的缓存即为 CPU/上一任务生成）。
- 后果：即便修复 4.3，旧 CPU 缓存仍会污染后续 DML 会话。

### 4.5 为何没有显式降级警告（对比 cuda 路径）

- `pdf2zh/doclayout.py:209-222` `has_gpu_provider()`：`dml` 分支只要 `effective`
  含非 CPU provider 即判定 GPU 存在 → 恒 True；
- `pdf2zh/doclayout.py:225-244` `_check_session_fallback()`：基于 `has_gpu_provider`
  判定，永远不触发；
- `pdf2zh/babeldoc_onnx_backend.py:188-195` `_session_has_gpu()`：同样的失效判定。
- 唯一显式降级路径是 `pdf2zh/high_level.py:1406-1445` `_degrade_backend_on_crash()`
  （BrokenProcessPool 崩溃后 `mark_cpu_degraded()`），与本次静默回退无关。

### 4.6 后端状态传播链（无泄漏点）

`runtime_service._apply_request_backend`（`pdf2zh/services/runtime_service.py:834-870`）
按任务请求调用 `set_backend(wanted)` 并重置 `ModelInstance.value`、重建 warm pool；
`parallel/worker.py:81-129` `init_worker_process` 通过 initargs 把 `get_backend()` 传给
worker。**这条链路本身没有把 dml 改成 cpu 的代码路径**——进一步证明不是"切换"，
而是"选了但没生效"。


---

## 五、影响评估

| 影响面 | 说明 |
|---|---|
| 性能 | DML 选择实际是 CPU 推理，版面阶段无 GPU 加速（1035 页 ≈ 3.65~4.15s/页） |
| 正确性 | 结果不受影响（CPU 与 DML 数值语义一致） |
| 误导性 | GUI 面板、日志、`get_runtime_provider_status()` 全部误报"DML 可用" |
| 潜在风险 | 若 DML 可用机器上残留 CPU NCHWc 缓存，同样会静默跑 CPU 且无法发现 |

---

## 六、修复建议

### P0-1：执行级 DML 有效性探测（替代 `_probe_providers`）
`pdf2zh/doclayout.py:247-276` 的探针改用**包含真实模型同款算子（Conv/Resize/Sigmoid）
的最小模型 + profiling**：创建会话后对一次推理开启 `enable_profiling()`，解析 profile
中是否存在 `provider == AzureExecutionProvider/DmlExecutionProvider` 的节点；无 → DML 无效。
据此修正 `get_runtime_provider_status()["dml"]` 与 GUI 面板（`config_panel.py:71-102`）。

> 备选（成本更低）：对最小 Conv 模型分别用 `[Azure]` 与 `[CPU]` 各推理 N 次，
> 若耗时差 < 阈值则判 DML 无效（3.2 节数据表明本机两者同量级）。

### P0-2：GUI 不可用后端禁用提示
`config_panel.py:150-160` 的 backend Radio 依据 P0-1 结果禁用 `dml`/`cuda` 选项，
并给出环境提示（如"DirectML 设备初始化失败，请检查显卡驱动"）。

### P1-1：DML 会话专用图优化级别
`_configure_session_options()`（`doclayout.py:338-354`）在 `backend == "dml"` 时使用
`ORT_ENABLE_BASIC`（DML 官方推荐），避免 NCHwC 阻塞 DirectML。CPU/auto 路径保持
`ORT_ENABLE_ALL` 不变（探针 C=665ms < B=979ms，说明 CPU 路径仍应保留高级优化）。

### P1-2：`.optimized` 缓存按 provider/优化级别隔离
缓存文件名附加 provider 指纹（如 `doclayout_yolo...onnx.dml-basic.optimized`），或在
缓存头记录生成时的 `providers` + `graph_optimization_level`，不匹配则忽略重建；
`_OptimizedCache`（`doclayout.py:364-490`）与 `OnnxModel.__init__`（行 656-659）同步改造。

### P1-3：显式静默回退警告
新增"会话创建后对真实模型做一次计时/节点级校验"，DML 静默回退时输出与 cuda 路径
一致的 `warn_gpu_session_fallback` 级警告，避免继续无感知。

### P2：环境清理建议（本机）
`onnxruntime-gpu 1.20.2` 与 `onnxruntime 1.28.0` 混装冲突。若目标是 CUDA：卸载
`onnxruntime`（保留 `onnxruntime-gpu`，注意 Python 3.13 需 ≥1.21 版本）；若目标是 DML：
保留 `onnxruntime 1.28.0` 并排查 D3D12 设备可用性（更新 NVIDIA 驱动、确认 WDDM）。
修好后按 §三 探针复测：DML 生效时 1024×1024 推理应 < 200ms。

---

## 七、附：关键代码位置索引

| 模块 | 行号 | 职责 |
|---|---|---|
| `pdf2zh/doclayout.py` | 338-354 | `_configure_session_options`（ORT_ENABLE_ALL） |
| `pdf2zh/doclayout.py` | 247-276 | `_probe_providers`（探针失效） |
| `pdf2zh/doclayout.py` | 279-302 | `get_runtime_provider_status`（dml 误报） |
| `pdf2zh/doclayout.py` | 305-334 | `resolve_providers`（仅按注册表求交） |
| `pdf2zh/doclayout.py` | 633-678 | `OnnxModel.__init__`（缓存复用 + effective 判定） |
| `pdf2zh/doclayout.py` | 364-490 / 540-593 | `_OptimizedCache` / `ensure_model_prewarmed` |
| `pdf2zh/babeldoc_onnx_backend.py` | 188-195 / 198-243 | `_session_has_gpu` / `_patched_init` |
| `pdf2zh/services/runtime_service.py` | 834-870 | `_apply_request_backend`（后端全局切换） |
| `pdf2zh/high_level.py` | 1406-1445 | `_degrade_backend_on_crash`（唯一显式降级） |
| `pdf2zh/gui/components/config_panel.py` | 71-102 / 150-160 | 诊断面板 / backend Radio |
| `pdf2zh/parallel/worker.py` | 81-129 | worker backend 传播（无泄漏） |
