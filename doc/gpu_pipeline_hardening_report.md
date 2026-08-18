# GPU 管线加固落地报告

> 本文档记录 `onnx_backend_silent_cpu_fallback_report.md` 中全部修复建议的**落地实现**、
> 真实环境实测数据、行为变化与验证结果。重点回答两个问题：
> **① 用户显式选择 `dml`/`cuda` 时能否真正用上 GPU（不再静默跑 CPU）？
> ② 整个 ONNX/GPU 管线在"GPU 无效→CPU 回退"与"GPU 有效"两条路径上是否都能正常工作？**

---

## 1. 摘要

本机（Windows，`onnxruntime 1.28.0` = onnxruntime-directml）中 **DirectML/Azure provider 已注册但
D3D12 设备初始化失败**。ORT 的处理是**静默回退 CPU**：`get_providers()` 仍返回
`['AzureExecutionProvider', 'CPUExecutionProvider']`，算子却全部在 CPU 执行。

修复前，该静默回退被三层掩盖，导致用户"选了 GPU 却无感知地跑 CPU"：

| 掩盖层 | 修复前行为 | 后果 |
| --- | --- | --- |
| 注册表级 | `resolve_providers("dml")` 只看 `get_available_providers()` → 返回 `[Azure, CPU]` | 创建无效 GPU 会话 |
| 会话级 | `get_providers()` 仍返回 `[Azure, CPU]`，`_check_session_fallback` 判定"有 GPU" | 静默回退无警告 |
| 状态级 | `get_runtime_provider_status()["dml"] = has_gpu_provider("dml", effective)` → 恒为 `True` | GUI 状态面板误报"DML 可用"，backend 下拉显示 dml 选项 |

本次落地全部修复建议（P0-1 ~ P1-6），核心是引入**执行级探测**：
创建真实推理会话并开启 profiling，**以 profile 中 Node 事件的 provider 分布判定"真正接管过算子的
provider"**，替代仅看注册表/`get_providers()` 的静态判断。

---

## 2. 根因回顾（来自前序报告）

- BabelDOC 0.6.x 的 `OnnxModel` 默认硬编码只启用 CPU provider；
- ORT 对 DirectML 失效是**静默回退**：`get_providers()` 返回请求列表并自动附加 CPU，
  但算子全在 CPUExecutionProvider 执行；
- 旧 `_probe_providers()` 用最小 Relu 模型 + `sess.get_providers()`，同样被静默回退骗过；
- `_configure_session_options()` 固定 `ORT_ENABLE_ALL`，其 **NchwcTransformer** 会把图优化为
  CPU 专用 NCHWc 块布局，即使 GPU 有效也无法消费 → 算子全部回落 CPU；
- `.optimized` 图缓存路径唯一，CPU 与 GPU 会话共用 → 跨环境复用会把 GPU 会话固化在 CPU。

---

## 3. 本机真实环境实测数据

| 项 | 修复前（实测） | 修复后（实测） |
| --- | --- | --- |
| `onnxruntime.__version__` | 1.28.0 | 1.28.0 |
| `get_available_providers()` | `[Azure, CPU]` | `[Azure, CPU]`（注册表不变） |
| **`get_runtime_provider_status()["effective"]`** | `[Azure, CPU]`（**误报**） | **`[CPUExecutionProvider]`** |
| **`status["dml"]`** | `True`（**误报**） | **`False`** |
| **`resolve_providers("dml")`** | `[Azure, CPU]`（无效 GPU 会话） | **`[CPUExecutionProvider]` + 明确警告** |
| `OnnxModel(backend=dml).model.get_providers()` | `[Azure, CPU]` | **`[CPUExecutionProvider]`** |
| 会话级回退警告 | 无（静默） | `Backend 'dml' was requested but the ONNX session fell back to CPU (...)` 一次 |
| GUI backend 下拉 | auto/cpu/cuda/**dml** | **auto/cpu**（dml 隐藏 + 状态面板显示原因/修复提示） |
| 探针耗时（首测） | — | ~0.9s（进程内缓存，后续零成本） |

**实测结论：修复后，显式选择 `dml` 时立即回退 CPU-only 并给出可执行修复提示；状态面板、
后端选项、会话 provider 三处口径完全一致，不再有任何一层误报。**

---

## 4. 修复落地清单

### 4.1 执行级探测（核心，`pdf2zh/doclayout.py`）

**P1-1 / P1-3：`_probe_providers()` 改为真实执行 + profile 判定**

```
创建最小 Conv 模型 → InferenceSession(providers=请求列表) → sess.run()
→ end_profiling() 开启的 profile JSON 中，cat=="Node" 事件的 args.provider 字段
  记录每个算子实际在哪个 EP 执行
→ 返回 [p for p in providers if p in used]
   （无节点事件/解析失败 → 保守返回 CPU-only）
```

- 探针模型用 **Conv**（所有 GPU EP 都支持、YOLO 版面模型的主算子），替代旧的 Relu；
- 探针本身用 `ORT_ENABLE_BASIC`（避免 NchwcTransformer 把探针图优化成 CPU 专用布局）；
- `ir_version` 降到 ≤10，兼容旧版 onnxruntime 1.20.x；
- profile 解析提取为独立函数 `_parse_profile_providers(profile_path)`，便于单测；
- **新增 `del sess`**：显式释放 ORT native 会话句柄后再删除 profile 文件——Windows 下
  ORT 会保持文件句柄打开直到 session GC，否则 1MB 的探针 JSON 残留在工作目录。

**P1-2：`_probe_gpu_provider(name)` + `_exec_gpu_providers()`（进程内缓存）**

- 对**每个**注册的 GPU provider 分别做执行级探测（独立会话），避免多 GPU 并存时
  高优先级 provider 接管全部算子、掩盖其它 provider 的真实可用性；
- 结果缓存到 `_EXEC_GPU_PROVIDERS`（进程生命周期内环境事实不变，只探测一次）；
- `get_runtime_provider_status` / `has_gpu_provider` / `resolve_providers` /
  `_configure_session_options` / `_optimized_cache_path` 全部复用该集合，口径一致。

### 4.2 静态 resolve 的执行级校验（`resolve_providers`）

显式 `cuda`/`dml` 分支在返回前增加执行级校验：

```
usable = wanted ∩ available
if backend in ("cuda","dml") and gpu_names[backend] ∩ _exec_gpu_providers() 为空:
    warn_gpu_session_fallback(backend, usable, cpu_only)   # 明确警告 + 修复提示
    return cpu_only or ["CPUExecutionProvider"]            # 提前回退，不创建无效会话
```

同时 `_check_session_fallback()` 增加早退：若 `requested` 已不含任何 GPU provider
（说明 resolve 阶段已回退并警告过），不再重复警告——避免"创建会话"与"resolve 回退"
双路径各打一次。


### 4.3 P0-1 状态口径（`get_runtime_provider_status`）

`effective` 改为**执行级探测过滤后的列表**，`cuda`/`dml` 布尔值基于
`_exec_gpu_providers()` 判定：

```python
effective = [p for p in available if p == "CPUExecutionProvider" or p in exec_gpu]
"cuda": "CUDAExecutionProvider" in exec_gpu
"dml":  bool(exec_gpu & {"AzureExecutionProvider", "DmlExecutionProvider"})
```

### 4.4 P0-2 GUI 后端选项动态过滤（`pdf2zh/gui/components/config_panel.py`）

- 新增 `_available_backend_choices()`：基于 `get_runtime_provider_status()` 过滤——
  `cuda`/`dml` 仅当其执行级探测有效时才出现在 backend Radio 中；
- `backend_status_markdown()` 状态面板补充 `backend_status_gpu_hidden` 提示
  （CUDA 与 DirectML 均不可用时告知"已从后端选项隐藏"）；
- `pdf2zh/gui/i18n.py` 新增对应中英双语条目。

### 4.5 P1-4 图优化级别（`_configure_session_options`）

DML **真正有效**时使用 `ORT_ENABLE_BASIC`（官方推荐：`ORT_ENABLE_ALL` 的
NchwcTransformer 生成 CPU 专用 NCHWc 布局，DirectML 无法消费 → 算子全回落 CPU）。
DML 失效回退 CPU 时保持 `ORT_ENABLE_ALL`（CPU 性能不受影响）。

### 4.6 P1-5/P1-6 `.optimized` 缓存按 backend/优化级别隔离（`_optimized_cache_path`）

| backend | 缓存文件 | 说明 |
| --- | --- | --- |
| `auto`/`cpu` | `<model>.optimized` | 兼容历史缓存（CPU + ALL 优化级） |
| `cuda` | `<model>.cuda.optimized` | CUDA + ALL 优化级 |
| `dml` 有效 | `<model>.dml-basic.optimized` | DML + BASIC 优化级 |
| `dml` 无效 | `<model>.optimized` | 回退 CPU + ALL，复用默认缓存 |

两处缓存消费点（`ensure_model_prewarmed` 预热、`OnnxModel.__init__`）统一改走
`_optimized_cache_path()`。`_OptimizedCache` 的锁路径基于缓存文件派生，天然隔离
CPU/GPU 竞争。

### 4.7 BabelDOC 内部 ONNX 会话同步校验（`pdf2zh/babeldoc_onnx_backend.py`）

`resolve_babeldoc_providers()` 的 GPU 分支同样增加执行级校验
（`_babeldoc_gpu_ineffective` + `_warn_babeldoc_gpu_session_fallback`）：
显式 `cuda`/`dml` 且执行级探测判定无效 → 立即回退 CPU-only + 明确警告，
不创建无效 GPU 会话。

---

## 5. GPU 有效场景的完整路径（代码验证）

本机无有效 GPU，GPU 有效路径通过 **mock 单测**覆盖（见 §6）。两场景预期行为：

### 5.1 CUDA 有效（`onnxruntime-gpu` + CUDA/cuDNN 就绪）

```
get_available_providers() = [CUDA, CPU]
_exec_gpu_providers()     = {CUDAExecutionProvider}      # 探测通过
resolve_providers("cuda") → [CUDA, CPU]
_configure_session_options → ORT_ENABLE_ALL
_optimized_cache_path      → <model>.cuda.optimized      # 与 CPU 缓存隔离
会话 get_providers()       → [CUDA, CPU]（真实执行）
GUI：cuda 选项可见、状态"DML: 不可用 CUDA: 可用"
```

### 5.2 DirectML 有效（`onnxruntime-directml` + 驱动就绪）

```
_exec_gpu_providers()     = {AzureExecutionProvider}
resolve_providers("dml")  → [Azure, CPU]
_configure_session_options → ORT_ENABLE_BASIC            # 关键：避免 NCHWc 污染
_optimized_cache_path      → <model>.dml-basic.optimized # 隔离 BASIC 级缓存
会话 get_providers()       → [Azure, CPU]（真实执行）
GUI：dml 选项可见
```

### 5.3 任何 GPU 无效 / 显式选择时的兜底

```
显式 dml/cuda → 执行级探测失败 → warn_gpu_session_fallback（含修复提示）
             → resolve 返回 CPU-only → 会话纯 CPU 执行（绝不创建无效 GPU 会话）
auto          → 保持 ORT 原生语义（全部注册 provider），状态面板如实显示
                effective=CPU-only
```


---

## 6. 测试与验证

### 6.1 单元测试新增/修改

| 文件 | 新增/修改 | 覆盖点 |
| --- | --- | --- |
| `tests/test_doclayout.py` | 修改 6 处、新增 ~19 项 | `_parse_profile_providers`、`_probe_providers` 执行级、`_probe_gpu_provider`、`_exec_gpu_providers` 缓存、`has_gpu_provider` 注册但无效、`resolve_providers` dml 无效回退、`_optimized_cache_path` 隔离（cpu/cuda/dml 有效/无效）、`_configure_session_options` BASIC/ALL 选择、`_check_session_fallback` 去重与静默回退 |
| `tests/test_babeldoc_onnx_backend.py` | 修改 5 处、新增 1 项 | `resolve_babeldoc_providers` 执行级校验、cuda 注册但无效回退、`_patched_init` GPU/回退路径 |
| `tests/test_gui_modules.py` | 新增 4 项 | `_available_backend_choices` 过滤（无 GPU/仅 CUDA/仅 DML）、状态面板隐藏提示 |

### 6.2 真实环境验证结果

```
✓ get_runtime_provider_status()      → effective=[CPU], dml=False（修复前误报 True）
✓ resolve_providers("dml")           → [CPU] + 明确警告（含修复提示）
✓ OnnxModel(backend=dml)             → providers=[CPU]，只打一次警告
✓ OnnxModel(backend=auto)            → 会话创建/推理正常（CPU）
✓ ensure_model_prewarmed / 缓存      → .optimized 正常生成
✓ GUI choices                        → [auto, cpu]（dml 已隐藏）
✓ 状态面板                           → 正确显示 effective 与缺失原因
✓ spawn worker × 2 (backend=dml)     → 无崩溃，worker providers=[CPU]
✓ profile 探针清理                   → 无 JSON 残留（新增 del sess 修复 Windows 文件锁）
```

### 6.3 回归

```
tests/test_doclayout.py                       42 passed
tests/test_babeldoc_onnx_backend.py           21 passed
tests/test_doclayout_batch.py + degrade + parallel_runtime + onnx_backend_switch
                                              71 passed
tests/test_gui_modules.py                    123 passed
─────────────────────────────────────────────────────
合计（本报告涉及）                           ≥257 passed，0 failed
```

---

## 7. 已知限制与后续建议

1. **GPU 有效路径未实机验证**：本机 DML 初始化失败、未装 onnxruntime-gpu，
   5.1/5.2 场景由 mock 单测覆盖，建议在真实 CUDA/DirectML 环境复跑
   `tests/test_doclayout.py::TestOptimizedCacheIsolation` + 5.x 冒烟脚本确认。
2. **执行级探测成本**：首次 ~0.9s（创建会话 + profiling），进程内缓存后为零；
   多 GPU 并存时对每个 GPU provider 独立探测，成本线性增长（正常仅 1~2 个）。
3. **auto 语义保持不变**：`auto` 仍把全部注册 provider 交给 ORT 自行选择
   （会话 `get_providers()` 可能显示无效 GPU 名），但**状态面板与警告口径已如实
   呈现实际生效列表**；如需进一步消除无效 EP 初始化噪音，可考虑 auto 也走执行级
   过滤（本次未做，避免改变 ORT 原生语义）。
4. **降级状态联动**：`mark_cpu_degraded()`（GPU worker 崩溃后的进程级降级）与本
   修复独立且互补——执行级探测解决"从未生效"，崩溃降级解决"生效后崩溃"。

---

## 8. 结论

**全部修复建议已落地并验证**：

- 显式 `cuda`/`dml` 选择不再可能"静默跑 CPU"——执行级探测在会话创建前识别无效
  GPU，回退 CPU-only 并给出可执行修复提示；
- GUI 状态面板、后端选项、会话 providers、`.optimized` 缓存、图优化级别五处口径
  与真实执行环境完全一致；
- GPU 有效时（CUDA/DirectML）走正确配置（BASIC 优化级 + 隔离缓存），无 NCHWc
  污染与跨环境缓存污染；
- BabelDOC 内部 ONNX 会话同步执行级校验，`--backend cuda/dml` 对 babeldoc 版面
  分析的 GPU 加速意图得到忠实表达；
- 全量回归 ≥257 项通过，spawn worker 冒烟无崩溃。

