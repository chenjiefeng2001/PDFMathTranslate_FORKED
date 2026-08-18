# onnxruntime-gpu 1.20.2 安装损坏与顶层 API 缺失修复报告

- **日期**：2026-08-17
- **关联环境**：Windows（win32），Python 3.13.1，`C:\Python313`
- **关联现象**：`python -m pdf2zh.gui.app` 启动即崩溃：`AttributeError: module 'onnxruntime' has no attribute 'get_available_providers'`

---

## 1. 现象

用户将 ONNX 运行时从 CPU 版（1.28.0）切换为 GPU 版后，GUI 启动失败：

```
pip uninstall onnxruntime && pip install onnxruntime-gpu   # 实际命中 onnxruntime-gpu 1.20.2
python -m pdf2zh.gui.app
...
AttributeError: module 'onnxruntime' has no attribute 'get_available_providers'
  File ".../pdf2zh/gui/components/config_panel.py", line 86, in _available_backend_choices
    status = get_runtime_provider_status()
  File ".../pdf2zh/doclayout.py", line 402, in get_runtime_provider_status
    available = onnxruntime.get_available_providers()
```

同时 `onnxruntime.__version__` 也缺失，`_ort_available_providers()` 返回空列表。

## 2. 根因分析（两层）

### 2.1 表象层：1.20.x 顶层 `get_available_providers` 缺失的误判

`onnxruntime.get_available_providers()` 在顶层报 `AttributeError`，容易让人误判为 **onnxruntime 1.20.x 的固有缺陷**（该问题在 1.21 修复，1.20.x 仅存在于 `onnxruntime.capi._pybind_state`）。

**实测结论：正常安装的 onnxruntime-gpu 1.20.2（cp313 win_amd64）顶层 `get_available_providers` 与 `__version__` 都存在。**

### 2.2 真根因层：`site-packages/onnxruntime` 包损坏

`pip uninstall onnxruntime`（1.28.0）时，pip 输出：

```
Would not remove (might be manually added):
    ...\onnxruntime\capi\onnxruntime_providers_cuda.dll
    ...\onnxruntime\capi\onnxruntime_providers_tensorrt.dll
    ...
```

该安装历史为：先装了 onnxruntime-gpu 1.20.2 → 后被 onnxruntime 1.28.0（CPU）**覆盖同名目录** → 卸载 1.28.0 时**整目录删除**，仅保留"手动添加"的 2 个残留 DLL；而 onnxruntime-gpu 1.20.2 的 dist-info 仍在，导致 pip 判定"已安装"。

最终 `site-packages/onnxruntime` 目录只剩：

```
capi/onnxruntime_providers_cuda.dll
capi/onnxruntime_providers_tensorrt.dll
quantization/   transformers/
```

**核心文件全部丢失**：`__init__.py`、`onnxruntime_pybind11_state.pyd`、`onnxruntime.dll` 等。于是 `import onnxruntime` 得到的是一个空的 **namespace package** —— 顶层没有任何属性，`__version__`、`get_available_providers` 自然全部缺失。

## 3. 修复内容

### 3.1 环境修复（首要）

强制重装 GPU 版运行时（从 pip 本地缓存，279.7MB wheel）：

```
pip install --force-reinstall --no-deps onnxruntime-gpu==1.20.2
```

重装后验证：`onnxruntime.__version__ = 1.20.2`，顶层 `get_available_providers()` 正常返回 `['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']`。

### 3.2 代码健壮性修复（防御层）

新增兼容封装 `pdf2zh.doclayout._ort_available_providers()`，对"顶层属性缺失"做双层兜底（顶层 → `onnxruntime.capi._pybind_state`），任何失败返回空列表而不向上抛异常：

```python
def _ort_available_providers() -> list[str]:
    try:
        return list(onnxruntime.get_available_providers())
    except AttributeError:
        try:
            from onnxruntime.capi._pybind_state import (
                get_available_providers as _pybind_get_available,
            )
            return list(_pybind_get_available())
        except Exception:
            return []
    except Exception:
        return []
```

替换全部直接调用点（共 7 处）：

| 文件 | 位置 | 说明 |
|---|---|---|
| `pdf2zh/doclayout.py` | `_probe_gpu_provider` / `_exec_gpu_providers` / `get_runtime_provider_status` / `resolve_providers` / `_cache_fingerprint_key` | 主链路探测与解析 |
| `pdf2zh/babeldoc_onnx_backend.py` | `resolve_babeldoc_providers` | BabelDOC 内部 provider 解析 |
| `pdf2zh/parallel/worker.py` | `init_worker_process` | 并行 worker bootstrap 探测 |

### 3.3 测试 mock 更新

`onnxruntime.get_available_providers` 不再被直接依赖，测试统一改为 patch `pdf2zh.doclayout._ort_available_providers`（6 处）：

- `tests/test_doclayout.py`：`_patch_available` + 3 处 `_probe_gpu_provider`/`_exec_gpu_providers` 用例
- `tests/test_babeldoc_onnx_backend.py`：`_patch_providers`
- `tests/test_parallel_runtime.py`：worker bootstrap 失败用例

## 4. 验证结果

| 验证项 | 结果 |
|---|---|
| 相关测试套件（doclayout / babeldoc_onnx_backend / onnx_backend_switch / high_level_backend_degrade / engine_cooldown / engine_env / parse_engine_switch / parallel_runtime / gui_modules / engine_optimizations） | **293 passed** |
| `get_runtime_provider_status()` | `{'onnxruntime': '1.20.2', 'available': [TensorRT, CUDA, CPU], 'effective': [CUDA, CPU], 'cuda': True, 'dml': False}` |
| GUI 完整启动（`python -m pdf2zh.gui.app`） | 正常监听 `http://0.0.0.0:7860`，25s 无崩溃；后端下拉框已出现 **CUDA（NVIDIA GPU）** 选项 |
| `tools/diag_gpu_probe.py` 各后端真实会话 | `backend=cuda: providers=[CUDA, CPU] load=1.84s infer=1.56s detections=1` —— **CUDA 会话真实推理成功** |
| 优化缓存指纹隔离 | `cpu-<fp>.optimized` 与 `cuda-<fp>.optimized` 指纹不同；GPU 显式后端不落盘，无 GPU 指纹缓存残留 |
| 孤儿 `.optimized.*.tmp` 清理 | 0 残留 |

## 5. GPU 使用现状与建议

1. **CUDA 已真正可用**：onnxruntime-gpu 1.20.2 + RTX 3070 + CUDA 运行库在 PATH，执行级探测确认 `CUDAExecutionProvider` 能真实执行算子。
2. **TensorRT 未安装**：注册表含 `TensorrtExecutionProvider`，缺 TensorRT 库时 ORT 每次创建会话打印一段 `EP Error` 并自动 fallback（无害噪音）。`auto` 后端语义为"返回全部注册 provider 由 ORT 自选"（有测试 `test_auto_returns_all_available` 锁定），显式 `cuda` 后端不包含 TensorRT，无此噪音。若需消除：安装匹配 CUDA 12.x 的 TensorRT 运行库，或始终显式 `--backend cuda`。
3. **DirectML 不可用**：GPU 发行版不含 DML provider。如需 DML 路线：`pip uninstall onnxruntime-gpu` 后 `pip install onnxruntime-directml`（provider 名为 `AzureExecutionProvider`）。
4. **推荐用法**：默认 `auto` 即可自动使用 CUDA；如需强制或回退，用 `--backend cuda` / `--backend cpu`（GUI 设置面板等价）。
