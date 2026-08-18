# ONNX Runtime CPU 与 CUDA 优化模型不兼容性调查报告

- **日期**：2026-08-17
- **调查方式**：代码走查（`pdf2zh/doclayout.py`、`pdf2zh/babeldoc_onnx_backend.py`、BabelDOC 0.6.x 原始实现）+ 本机缓存实测 + ONNX Runtime 官方文档核对
- **结论速览**：**"CPU 模型 / CUDA 模型"不兼容是 ONNX Runtime 的图优化产物（`.optimized` 缓存 / 内存优化图）与 ExecutionProvider 强绑定造成的，而非 `.onnx` 源文件本身**。ORT 官方文档明确禁止跨 EP / 跨硬件复用离线优化模型。本项目已通过"缓存按 backend 隔离 + 优化级别差异化"落地缓解；本报告解释其机理、给出实测证据，并列出残余风险与后续建议。

---

## 一、术语澄清：什么才是"CPU 模型 / CUDA 模型"

| 概念 | 是否绑定 EP | 说明 |
| --- | --- | --- |
| `.onnx` 源文件 | ❌ 否 | 标准 ONNX 图（算子 + 权重），EP 无关，任何 EP 都能加载 |
| **`SessionOptions.graph_optimization_level` 在线优化图** | ✅ 是 | 会话创建时基于**当前启用的 EP 集合**做图变换（融合、布局优化、常量折叠），结果存在内存中，随会话销毁 |
| **`optimized_model_filepath` 离线优化图（`.optimized` 缓存）** | ✅ 是 | 把在线优化图**序列化到磁盘**复用。ORT 文档要求"使用与生成时完全相同的 options（EP、优化级别）与硬件" |

> 用户口语中的"CPU 模型 / CUDA 模型"，在 ONNX Runtime 语境下实际指向**第三种**：被图优化固化后的模型文件。本项目 `doclayout` 的 `<model>.optimized` 缓存即属此类。

---

## 二、不兼容的根本机制

### 2.1 图优化以"启用的 EP 集合"为前提

ORT 在创建 `InferenceSession` 时执行一次图优化流水线（`GraphTransformer` 列表）。同一张 ONNX 图，CPU-only 与 CUDA/DML 会话看到的优化器集合**不同**：

- CPU：`NchwcTransformer`（布局）、`ConvAddFusion`/`ConvActivationFusion`（融合到 MLAS 内核）、`GeluFusion` 等；
- CUDA：`ConvActivationFusion`（融合到 **cuDNN/cuBLAS 内核**）、fp16 转换（自动混合精度）、`CUDNN` 专用 contrib op；
- DirectML：ORT 层只做基础优化，**主要图优化发生在 DirectML 内部**（见 2.4）。

因此优化后的图里出现的是"特定 EP 内核的专属节点"。换一个 EP 加载时，这些节点在内核表里**查不到**——ORT 不会报错，而是**把该节点分派给 CPUExecutionProvider**（静默降级）。

### 2.2 NchwcTransformer：CPU 专用布局优化（本项目的直接根因）

`ORT_ENABLE_ALL` 会启用 **NchwcTransformer**，把普通 NCHW 张量布局重排为 **NCHWc（channel-blocked）** 布局，并重写 Conv/BN/Pool 等算子为 MLAS 的 `_nchwc_` 变体内核：

```
Conv → /model.4/.../Conv_output_0_nchwc_kernel_time   （实测节点名）
```

- NCHWc 布局**只为 CPU（MLAS/oneDNN）设计**，CUDA/DML 的 GPU 内核无法消费这类节点；
- 结果：即使会话里同时启用了 CUDA/DML，凡是带 NCHWc 布局的节点**必然回落 CPU 执行**；
- 官方文档对此有明确警告："When **layout optimizations** are enabled, the offline mode can only be used on compatible hardware … if model has layout optimized for AVX2, the offline model would require CPUs that support AVX2"——即布局优化**不仅绑定 EP，还绑定 CPU 指令集**。

### 2.3 CUDA EP：融合内核 + contrib op 不可移植

CUDA EP 的图优化会生成：
- **`Conv+Add+Relu` 融合**等 fused kernels，绑定到 cuDNN/cuBLAS 的 CUDA 实现；
- 部分 contrib op（如 `CudaFusedConv` 类节点 / `FusedConv`）只存在于 CUDA 内核库；
- 自动 **fp16 / mixed-precision** 变换后的权重与算子，在 CPU 上要么无内核、要么精度不一致。

所以"为 CUDA 预优化的模型在只有 CPU 的机器上跑"是官方文档点名的反例：

> *"you **cannot** run a model pre-optimized for a GPU execution provider on a machine that is equipped only with CPU"*

### 2.4 DirectML EP：独立图优化 + 会话限制

DirectML 官方文档明确 **"Graph optimization occurs within DirectML"**——DML 在设备侧自己做算子融合与张量布局选择，ORT 层的 CPU 布局优化（NCHWc）对它**无用甚至有害**。此外 DML 还有两条硬约束：

- **不支持 memory pattern optimization**（必须 `DisableMemPattern`）；
- **不支持 parallel execution**（必须 `ExecutionMode::ORT_SEQUENTIAL`，且同一会话同一时刻只能一个线程调用 `Run`）。

### 2.5 官方文档原文（关键证据）

来自 *Graph Optimizations in ONNX Runtime*（onnxruntime.ai/docs）：

> "When running in **offline mode**, make sure to use the **exact same options (e.g., execution providers, optimization level) and hardware** as the target machine that the model inference will run on (e.g., you cannot run a model pre-optimized for a GPU execution provider on a machine that is equipped only with CPU)."
>
> "When **layout optimizations** are enabled, the offline mode can only be used on **compatible hardware** to the environment when the offline model is saved. For example, if model has layout optimized for AVX2, the offline model would require CPUs that support AVX2."

这条规则直接对应本项目此前的 bug：**CPU(ALL) 环境生成的 `<model>.optimized`（带 NCHWc）被 DML 会话直接复用 → 735/816 个算子全部在 CPU 执行**（见 `doc/onnx_backend_silent_cpu_fallback_report.md` §3.3）。

---

## 三、跨 EP 复用优化模型的后果分级

| 复用场景 | 结果 | 危害 |
| --- | --- | --- |
| CPU 优化图（含 NCHWc）→ DML 会话 | NCHWc 节点无 DML 内核 → **全部算子回落 CPU** | 静默性能塌陷（本机实测 712.5ms vs CPU 基线 665ms，无任何加速） |
| CPU 优化图 → CUDA 会话 | NCHWc / fused 节点无 CUDA 内核 → 回落 CPU；含 contrib op 时可能**会话创建失败** | 静默降级或崩溃 |
| CUDA 优化图 → CPU-only 机器 | 官方明确 "cannot run" | 报错 / 内核缺失 |
| CUDA 优化图 → DML 会话 | fused contrib op 无对应内核 | 回落 CPU 或失败 |
| 同一 CPU 图跨指令集（AVX2 → 非 AVX2） | 布局优化绑定指令集 | 非法指令 / 崩溃 |

**要点：ORT 对"优化图与 EP 不匹配"的处理不是友好的报错，而是"静默分派到 CPU"**——这正是本系列 GPU 管线问题难以被发现的原因。

---

## 四、项目现状：已落地的隔离与剩余问题

### 4.1 缓存文件名按 backend/优化级别/**环境指纹**隔离（已落地 + 已测试）

`pdf2zh/doclayout.py:_optimized_cache_path()`：缓存键 = backend + 优化级别 +
**环境指纹**（`_cache_fingerprint_key`：ort 版本 + 架构 + CPU 型号 + NCHWc 相关
指令集 flag + provider 集合的 12 位 sha1）。任何环境变化自动得到独立缓存文件。

| backend | 缓存文件 | 优化级别 |
| --- | --- | --- |
| `cpu` / `auto` / DML 失效 | `<model>.cpu-<fp>.optimized` | `ORT_ENABLE_ALL` |
| `cuda` | `<model>.cuda-<fp>.optimized` | `ORT_ENABLE_ALL`（CUDA 内核） |
| `dml` 有效 | `<model>.dml-basic-<fp>.optimized` | `ORT_ENABLE_BASIC` |

历史无指纹 `<model>.optimized` 无法验证来源，不再复用（首次启动重新生成一次，
此后稳定命中）。配套测试 `tests/test_doclayout.py`：
`test_cache_path_cpu_fingerprinted`、`test_cache_path_cuda_fingerprinted`、
`test_cache_path_dml_effective_fingerprinted`、
`test_cache_path_dml_ineffective_uses_cpu_fingerprint`、
`test_cache_fingerprint_stable_within_environment`、
`test_cache_fingerprint_differs_across_backends`。

### 4.2 优化级别差异化（`_configure_session_options`）

DML **真正有效**（`_dml_effective()` 执行级探测）时降为 `ORT_ENABLE_BASIC`，
从源头避开 NchwcTransformer 的 NCHWc 布局——即使 DML 会话偶然复用了错误缓存，
BASIC 级图里也没有 CPU 专用布局节点可被误加载。DML 失效回退 CPU 时保持 `ORT_ENABLE_ALL`（CPU 性能不受影响）。

### 4.3 不可序列化 EP 跳过文件缓存

`_COMPILED_PROVIDERS = {CoreMLExecutionProvider, TensorrtExecutionProvider}`：
这类 EP 生成的优化图含**编译型节点**（不能序列化），项目已在预热与 `OnnxModel.__init__`
两处消费点跳过 `.optimized` 文件缓存，只走内存在线优化。

### 4.4 BabelDOC 原始实现不产生 `.optimized` 文件缓存

走查 `C:\Python313\Lib\site-packages\babeldoc\docvision\doclayout.py` 确认：
BabelDOC 0.6.x 的 `OnnxModel` 用 `model.SerializeToString()`（**内存字节**）+ 默认 SessionOptions，
**不设置 `optimized_model_filepath`** → 它始终走"在线优化、随会话销毁"，本身不制造跨 EP
磁盘缓存污染（代价是每次启动重新优化）。同时其源码注释明确

```python
# disable dml|cuda|
# directml/cuda may encounter problems under special circumstances
```

即 BabelDOC 原生**默认只启用 CPU**——本项目 `pdf2zh/babeldoc_onnx_backend.py` 的 patch
正是为解除该限制并让 `--backend cuda/dml` 忠实表达 GPU 意图。

### 4.5 本机缓存实测

`C:\Users\14977\.cache\babeldoc\models\`：

| 文件 | 大小 | 时间 | 说明 |
| --- | --- | --- | --- |
| `doclayout_yolo_docstructbench_imgsz1024.onnx.cpu-781a47de4eb9.optimized` | 75.3MB | 2026/8/17 12:58 | **指纹化 CPU(ALL) 缓存**（首次探针生成） |
| `doclayout_yolo_74cls_imgsz1024.onnx.optimized` | 95.0MB | 2026/8/13 15:59 | 历史无指纹缓存（保留但不再复用） |
| `doclayout_yolo_docstructbench_imgsz1024.onnx.optimized` | 75.3MB | 2026/8/17 12:42 | 历史无指纹缓存（保留但不再复用） |
| `doclayout_yolo_docsynth300k_imgsz1024.onnx.optimized` | 95.0MB | 2026/8/13 15:46 | 历史无指纹缓存（保留但不再复用） |
| `.cuda-*.optimized` / `.dml-basic-*.optimized` | **0 个** | — | GPU 显式后端不落盘 + 本机无有效 GPU，从未生成 GPU 缓存（符合预期） |
| `*.optimized.*.tmp` | **0 个**（已清理） | — | 原 13 个 ≈980MB 孤儿 tmp 已由 `_cleanup_stale_tmp_once` 在首次会话创建时清除 |

> 结论：指纹化后本机自动命中 `cpu-781a47de4eb9`；用户未来安装 onnxruntime-gpu 并
> 切 `cuda` 时使用独立的 `cuda-97f055d2bffa`，天然隔离、互不复用。

---

## 五、残余风险与后续建议

### 5.1 残余风险

| 风险 | 等级 | 说明 |
| --- | --- | --- |
| **同型号 CPU 共享缓存** | 低 | 指纹含 CPU 型号 + 指令集 + provider 集合，跨机器/跨指令集已隔离；仅“完全相同型号 CPU + 同指令集 + 同 ORT 版本”的机器会共享同一缓存——此时指令集一致，属安全复用 |
| **auto 模式的 ORT 原生语义** | 低 | `auto` 仍把全部已注册 provider 交给 ORT 自行选择；若注册表含无效 GPU（如本机 Azure 注册但 DML 失效），会话创建时 ORT 也会在线做优化，其产物不落盘，无持久污染 |
| **fp16 / CUDA graph / TensorRT 固化** | 低 | 若未来引入 `prefer_nhwc`、CUDA graph、TensorRT 等，优化图将更进一步绑定设备与驱动，届时缓存键需再扩维（当前指纹键已含 ort/backend/provider 维度，扩展点预留） |
| **第三方引擎（magic-pdf/MinerU）** | 低 | MinerU 有自己的 ONNX 加载路径，不经本项目 `.optimized` 缓存，互不污染；但同一进程内若以 GPU 会话在线优化，其内存图同样遵循"EP 绑定"规则 |
| **GPU 显式会话无缓存加速** | 低（有意为之） | 指纹不匹配时 GPU 会话不落盘、每次在线优化（数秒）；换取“绝不产生跨 EP 磁盘图”的硬保证 |

### 5.2 后续建议落地状态（2026-08-17 全部完成）

| # | 建议 | 状态 | 落地说明 |
| --- | --- | --- | --- |
| 1 | 缓存键加入环境指纹 | ✅ 已落地 | `_cache_fingerprint_key()` 生成 `<model>.<backend>-<fp>.optimized`（含 ort 版本/架构/CPU 型号/指令集/provider 集合）；配套 6 个单测 |
| 2 | 清理历史 `.tmp` 残留 | ✅ 已落地 | `cleanup_stale_optimized_cache_tmp()` + `_cleanup_stale_tmp_once()`：进程内一次，删除 mtime>60s 且 pid 已亡的 `.optimized.*.tmp`；本机 13 个 ≈980MB 已清除，实测剩余 0 |
| 3 | GPU 会话默认走"内存在线优化" | ✅ 已落地 | `_should_generate_optimized_cache()`：显式 `cuda`/`dml` 在无同指纹缓存时 `abort()` 释放锁并不落盘；`OnnxModel` 与 `ensure_model_prewarmed` 两处消费点均已接线；配套 3 个单测 |
| 4 | 文档化限制 | ✅ 已落地 | 本报告 + §7 本机 GPU 测试结果；与 `onnx_backend_silent_cpu_fallback_report.md`、`gpu_pipeline_hardening_report.md` 闭环 |

> 落地共新增/更新 13 个测试（指纹路径 4 + 指纹稳定性/差异 2 + 不落盘开关 1 +
> tmp 清理 3 + GPU 不落盘 3）；相关 229 项测试全部通过。

---

## 六、结论

1. **ONNX 源模型本身 EP 无关；"CPU/CUDA 模型不兼容"指的是图优化产物（`.optimized` 缓存/内存优化图）与 ExecutionProvider、优化级别、硬件指令集强绑定**，ORT 官方文档明确禁止跨环境复用。
2. 不兼容的实质后果是**静默分派到 CPU**（而非友好报错）：CPU 的 NCHWc 布局节点、CUDA 的 fused/contrib 内核、DML 的内部图优化三方互不消费，跨 EP 复用即性能塌陷或会话失败。
3. 本项目已落地四道防线：**缓存按 backend/优化级别隔离、DML→BASIC 优化级别、不可序列化 EP 跳过文件缓存、执行级探测**；BabelDOC 原生走内存在线优化，不产生 `.optimized` 文件缓存。
4. 报告 §5.2 的四项建议已于 2026-08-17 全部落地（环境指纹缓存、孤儿 tmp 清理、GPU 不落盘、文档化），残余风险收敛为“同型号 CPU 共享缓存（指令集一致，属安全复用）”与“GPU 显式会话无缓存加速（换取硬隔离保证）”两类低风险项；本机 GPU 测试结果见 §七。

---

## 七、本机 GPU 测试结果（2026-08-17 12:58，`tools/diag_gpu_probe.py`）

运行：`python tools/diag_gpu_probe.py`（本机：onnxruntime 1.28.0 CPU 版，未安装
onnxruntime-gpu / onnxruntime-directml）。

| 测试项 | 结果 | 判定 |
| --- | --- | --- |
| ORT 可用 provider | `[AzureExecutionProvider, CPUExecutionProvider]` | — |
| 执行级探测 | `runtime status: cuda=False, dml=False`；`exec GPU providers = set()` | ✅ 无静默假 GPU |
| `resolve_providers` | `auto=[Azure,CPU]`（注册表级）；`cpu=[CPU]`；`cuda=[CPU]`；`dml=[CPU]`（执行级校验回落） | ✅ GPU 不可用时正确回落 |
| 环境指纹 | `781a47de4eb9`（cpu-all）；cuda 路径独立为 `cuda-97f055d2bffa` | ✅ backend 变化自动换缓存 |
| 真实会话 | `cpu`/`cuda`/`dml` 三后端全部 `['CPUExecutionProvider']`，无 GPU 会话崩溃 | ✅ 静默回退已被显式化 |
| 推理计时 | load≈2.1s（复用 CPU 指纹缓存）；512×512 单页 infer≈0.16s，detections=1 | ✅ 功能正常 |
| GPU 不落盘 | `.cuda-*.optimized` / `.dml-basic-*.optimized` = **0 个** | ✅ 显式 GPU 后端未生成任何磁盘图 |
| CPU 指纹缓存 | `doclayout_yolo_docstructbench_imgsz1024.onnx.cpu-781a47de4eb9.optimized` 首次生成 | ✅ 指纹化缓存路径生效 |
| 孤儿 tmp | 原 13 个 ≈980MB → 首次会话创建时清理，实测剩余 **0 个** | ✅ `_cleanup_stale_tmp_once` 生效 |

**结论**：本机（无有效 GPU）GPU 管线全链路行为正确：GPU 请求 → 执行级探测判不可用
→ 显式告警 + 回落 CPU → 不落任何 GPU 磁盘缓存；CPU 指纹缓存正常命中。若需真正启用
GPU：`pip uninstall onnxruntime && pip install onnxruntime-gpu`（CUDA）或
`onnxruntime-directml`（无 CUDA toolkit），重启后 `auto` 会自动探测到 GPU 并生成
对应的 `.cuda-*` / `.dml-basic-*` 指纹缓存。

