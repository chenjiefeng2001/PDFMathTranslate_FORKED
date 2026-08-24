import abc
import glob
import json
import logging
import os
import platform
import time

import cv2
import numpy as np
import ast
from babeldoc.assets.assets import get_doclayout_onnx_model_path

try:
    import onnx
    import onnxruntime
except ImportError as e:
    if "DLL load failed" in str(e):
        raise OSError(
            "Microsoft Visual C++ Redistributable is not installed. "
            "Download it at https://aka.ms/vs/17/release/vc_redist.x64.exe"
        ) from e
    raise

logger = logging.getLogger(__name__)

_BACKEND_PROVIDERS = {
    "cpu": ["CPUExecutionProvider"],
    "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "dml": ["AzureExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
}

_preferred_backend: str | None = None

#: BrokenProcessPool 崩溃（GPU worker 被终止）后置 True，表示本进程已把后端
#: 降级为 CPU。显式 set_backend("auto"/"cuda"/"dml") 会重新清零，允许后续任务
#: 重新尝试 GPU；避免"降级一次、永久 CPU、无法恢复"。
_cpu_degraded_flag: bool = False

#: 连续崩溃次数。GPU worker 崩溃通常是瞬态环境故障（驱动瞬时故障/显存竞争/
#: D3D12 上下文被杀），之后往往自动恢复；但也可能是持续性的。策略：
#:   第 1 次崩溃 → 当前任务降级 CPU；下一任务自动重新尝试 GPU 一次；
#:   再崩 1 次  → 后续任务保持 CPU（不再自动重试），由用户显式
#:                 ``set_backend("auto")`` / 重启服务来恢复。
_crash_streak: int = 0

#: ``get_runtime_provider_status`` 的进程级缓存：provider 可用性在进程生命周期内
#: 不变，真实探针（会触发 onnxruntime 打印缺失 CUDA/TensorRT 运行库的 EP Error）
#: 只需执行一次，避免 GUI 每次更新诊断面板都重复打印噪音。
_runtime_provider_status_cache: dict | None = None

#: 执行级探测的"真正接管过算子的非 CPU provider"集合（进程级缓存）。None =
#: 未探测。独立于 ``_runtime_provider_status_cache``，供会话创建后的
#: ``has_gpu_provider``/``resolve_providers`` 复用——ORT 对 DirectML/CUDA 的
#: 失效是静默回退（``get_providers()`` 仍返回 GPU 名且自动附加 CPU），仅凭
#: provider 列表无法判断 GPU 是否真正执行
#: （见 doc/onnx_backend_silent_cpu_fallback_report.md）。
_EXEC_GPU_PROVIDERS: set | None = None


def set_backend(name: str) -> None:
    """Set the ONNX Runtime execution provider backend.

    Args:
        name: One of 'auto', 'cpu', 'cuda', 'dml'.
    """
    global _preferred_backend, _cpu_degraded_flag, _crash_streak
    _preferred_backend = None if name == "auto" else name
    if name != "cpu":
        # 显式要求 GPU/自动探测 = 用户主动恢复尝试，清除降级标记。
        _cpu_degraded_flag = False
        _crash_streak = 0
    # 同一开关同步到 BabelDOC 内部 ONNX 会话（幂等、静默失败）。
    _sync_babeldoc_backend()


def _sync_babeldoc_backend() -> None:
    """把 pdf2zh 的后端选择同步到 BabelDOC 内部 ONNX 推理。

    BabelDOC 0.6.x 的 ``OnnxModel`` 默认硬编码只启用 CPU provider；这里
    通过 :func:`pdf2zh.babeldoc_onnx_backend.apply_babeldoc_backend` 打上
    幂等补丁，使 ``--backend cuda``/``dml`` 对 BabelDOC 的版面分析同样生效。
    babeldoc 缺失 / 补丁失败均静默跳过，绝不干扰主流程。
    """
    try:
        from pdf2zh.babeldoc_onnx_backend import (  # noqa: PLC0415
            apply_babeldoc_backend,
        )

        apply_babeldoc_backend()
    except Exception:  # noqa: BLE001 -- 同步失败不影响主流程
        logger.debug("babeldoc backend patch skipped", exc_info=True)


def is_cpu_degraded() -> bool:
    """Return True if the process previously degraded to CPU after a GPU worker crash.

    供降级逻辑做一次性/幂等判断，也便于上层（GUI/服务）发现当前进程处于
    CPU-only 状态并向用户展示恢复入口。
    """
    return _cpu_degraded_flag


def mark_cpu_degraded() -> bool:
    """Record a BrokenProcessPool crash and mark the backend degraded to CPU.

    Returns True if this call performed the degradation (first time), False if
    the backend is already CPU / already degraded.
    """
    global _preferred_backend, _cpu_degraded_flag, _crash_streak
    if _preferred_backend == "cpu" or _cpu_degraded_flag:
        return False
    _preferred_backend = "cpu"
    _cpu_degraded_flag = True
    _crash_streak += 1
    return True


def try_rearm_gpu() -> bool:
    """Auto-rearm the GPU backend after a crash, at most once per process.

    GPU worker crashes are usually transient (driver hiccup / VRAM contention),
    so the task *after* a crash gets one automatic GPU retry; a second crash in
    the same process keeps the backend on CPU until ``set_backend()`` is called
    explicitly (CLI ``--backend auto`` or a service restart).

    Returns True when the backend was re-armed to auto-detection.
    """
    global _preferred_backend, _cpu_degraded_flag, _crash_streak
    if not _cpu_degraded_flag:
        return False
    if _crash_streak > 1:
        return False
    _preferred_backend = None
    _cpu_degraded_flag = False
    return True


def get_backend() -> str | None:
    """Return the current backend override (``None`` means auto-detection).

    供并行 worker 进程传播父进程的后端选择：``ProcessPoolExecutor`` 的
    ``initargs`` 需要把该值传给 ``_init_worker_process``，避免 worker 在
    父进程显式 ``--backend cpu`` 时仍自动探测出 DirectML/CUDA 等 GPU
    provider，从而在 GPU 推理中把 worker 进程搞崩（BrokenProcessPool）。
    """
    return _preferred_backend


def warn_gpu_unavailable(
    backend: str, wanted: list[str], available: list[str],
) -> None:
    """Log a clear warning when an explicit GPU backend silently falls back to CPU.

    显式请求 ``cuda``/``dml`` 时，CPU 兜底项会让 provider 交集非空，若不加
    检查会“选 GPU 却静默跑 CPU”。这里统一给出可执行的修复提示。
    """
    if backend == "cuda":
        if "CUDAExecutionProvider" in available:
            # 已装 onnxruntime-gpu，但 CUDA 运行库初始化失败（缺 cublas/cuDNN）。
            hint = (
                "onnxruntime-gpu is installed but the CUDA runtime failed to "
                "initialize (missing cublasLt/cuDNN DLLs). Install matching "
                "CUDA + cuDNN (see onnxruntime GPU requirements) and add them "
                "to PATH, or downgrade onnxruntime-gpu to a version matching "
                "your installed CUDA; on Windows you can also use DirectML "
                "('dml') without a CUDA toolkit."
            )
        else:
            hint = (
                "install 'onnxruntime-gpu' matching your CUDA/cuDNN versions "
                "(pip uninstall onnxruntime && pip install onnxruntime-gpu), then "
                "restart; on Windows you can alternatively use DirectML ('dml') "
                "without a CUDA toolkit."
            )
    else:
        hint = (
            "install 'onnxruntime-directml' / check your GPU driver; on NVIDIA "
            "you can also use CUDA ('cuda') with onnxruntime-gpu."
        )
    logger.warning(
        "Backend '%s' requested but no GPU provider is available "
        "(wanted %s; available: %s); falling back to CPU. To enable GPU: %s",
        backend, wanted, available, hint,
    )


def warn_gpu_session_fallback(
    backend: str, requested: list[str], effective: list[str],
) -> None:
    """Log a clear warning when an ONNX session fell back to CPU at creation.

    与 ``warn_gpu_unavailable`` 的静态检测不同，这里基于**实际创建会话后**
    生效的 providers：``get_available_providers()`` 是编译期注册表，即使
    CUDA 运行库缺失也照常列出 ``CUDAExecutionProvider``，但 ORT 在真正创建
    会话时会因缺 DLL（如 ``cublasLt64_*.dll``）而静默回退 CPU。
    """
    if backend == "cuda":
        hint = (
            "the CUDAExecutionProvider could not be initialized at session "
            "creation (usually missing CUDA/cuDNN runtime DLLs or a driver "
            "issue). Install matching CUDA + cuDNN (see onnxruntime GPU "
            "requirements), or downgrade onnxruntime-gpu to match your installed "
            "CUDA; on Windows you can also use DirectML ('dml') without a CUDA "
            "toolkit."
        )
    else:
        hint = (
            "the DirectML/Azure provider could not be initialized at session "
            "creation (driver or onnxruntime-directml issue); on NVIDIA GPUs "
            "you can also use CUDA ('cuda') with onnxruntime-gpu."
        )
    logger.warning(
        "Backend '%s' was requested but the ONNX session fell back to CPU "
        "(requested %s; effective %s): %s",
        backend, requested, effective, hint,
    )


def has_gpu_provider(backend: str, effective: list[str]) -> bool:
    """Return True when ``effective`` contains a GPU provider for ``backend``.

    ``backend`` 取 ``cuda``/``dml``/``auto``：``auto`` 只要出现任一非 CPU
    provider 即视为有 GPU（无法确定用户意图时按宽松判断）。
    """
    if backend in ("cuda", "dml"):
        wanted = {
            "cuda": {"CUDAExecutionProvider"},
            "dml": {"AzureExecutionProvider", "DmlExecutionProvider"},
        }[backend] & set(effective)
        if not wanted:
            return False
        return bool(wanted & _exec_gpu_providers())
    if backend is None or backend == "auto":
        wanted = set(effective) - {"CPUExecutionProvider"}
        if not wanted:
            return False
        return bool(wanted & _exec_gpu_providers())
    return False


def _check_session_fallback(
    backend: str | None,
    requested: list[str],
    effective: list[str],
) -> None:
    """Warn when an explicit GPU backend ended up running on CPU.

    静态检测（``resolve_providers``）只能看到编译期注册表；真正的回退发生在
    ``InferenceSession`` 创建时（缺 CUDA/cuDNN DLL 等），此时生效的 provider
    里没有 GPU。此函数在每次会话创建后核对实际生效结果并给出明确警告，
    覆盖“选 GPU 却静默跑 CPU”的最后一道关口。

    ``resolve_providers`` 已按执行级探测过滤无效 GPU（显式 ``cuda``/``dml``
    回退 CPU-only 时已调用 :func:`warn_gpu_session_fallback`），此时 ``requested``
    不含任何 GPU provider，直接返回避免重复警告。
    """
    if not any(p != "CPUExecutionProvider" for p in requested):
        return
    if not has_gpu_provider(backend, effective):
        if backend in ("cuda", "dml"):
            warn_gpu_session_fallback(backend, requested, effective)
        elif backend is None or backend == "auto":
            logger.info(
                "ONNX session running on CPU (no GPU provider effective: %s)",
                effective,
            )


def _probe_providers(providers: list[str]) -> list[str]:
    """用最小 Conv 模型做**执行级**探测：真正执行过算子的 provider 才算可用。

    旧实现用 Relu 模型 + ``sess.get_providers()``——但 ORT 对 DirectML/CUDA
    的失效是**静默回退**：``get_providers()`` 仍返回请求列表（且自动附加 CPU），
    算子却全部在 CPUExecutionProvider 执行（见
    ``doc/onnx_backend_silent_cpu_fallback_report.md`` 3.4 节）。

    新实现创建最小 Conv 会话后开启 profiling 跑一次真实推理，解析 profile 中
    Node 事件的 provider 分布——只有真正执行过算子的 provider 才进入结果。
    """
    try:
        from onnx import TensorProto, helper  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- 无法构造探针时回退静态判断
        return list(providers)
    try:
        x = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, 64, 64])
        y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 3, 64, 64])
        w = helper.make_tensor(
            "w", TensorProto.FLOAT, [3, 3, 3, 3], [0.1] * 81,
        )
        node = helper.make_node("Conv", ["x", "w"], ["y"])
        graph = helper.make_graph([node], "probe_conv", [x], [y], [w])
        model = helper.make_model(
            graph, opset_imports=[helper.make_opsetid("", 11)]
        )
        # 旧版 onnxruntime（1.20.x）只支持 IR version <= 10；新版 onnx 默认
        # IR 可能更高，若不降级探针本身会创建失败，误报 GPU 不可用。
        model.ir_version = min(model.ir_version, 10)
        opts = onnxruntime.SessionOptions()
        # BASIC：避免 NchwcTransformer 把探针图优化为 CPU 专用 NCHWc 布局而
        # 掩盖 GPU 本应接管算子的能力（与真实 DML 会话的优化级别一致）。
        opts.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_BASIC
        )
        opts.enable_profiling = True
        sess = onnxruntime.InferenceSession(
            model.SerializeToString(), opts, providers=list(providers),
        )
        sess.run(None, {"x": np.zeros((1, 3, 64, 64), dtype=np.float32)})
        profile_path = sess.end_profiling()
        # 显式释放 native 会话句柄：Windows 下 ORT 可能保持 profile 文件句柄
        # 打开直到 session 被 GC，否则 os.unlink 在 _parse_profile_providers
        # 内静默失败 → 探针 JSON 残留在工作目录（~1MB）。
        del sess
        used = _parse_profile_providers(profile_path)
        if not used:
            # profile 解析失败/无节点事件：保守按 get_providers 去 CPU 兜底
            return [p for p in providers if p == "CPUExecutionProvider"]
        return [p for p in providers if p in used]
    except Exception:  # noqa: BLE001 -- 探针失败兜底：仅保留 CPU
        return [p for p in providers if p == "CPUExecutionProvider"]


def _parse_profile_providers(profile_path: str) -> set[str]:
    """从 ORT profiling JSON 提取真正执行过算子的 provider 集合。

    ORT 的 ``end_profiling()`` 输出 Chrome trace 格式 JSON，其中每个 Node 事件
    （``cat == "Node"``）的 ``args.provider`` 字段标明该算子在哪个 EP 执行。
    CPU 兜底时会记录 ``CPUExecutionProvider``；GPU 真正接管时记录对应的
    GPU provider 名。profile 文件读取后立即删除（探针临时产物）。
    """
    used: set[str] = set()
    try:
        with open(profile_path, "r", encoding="utf-8") as fh:
            prof = json.load(fh)
        events = prof.get("traceEvents", []) if isinstance(prof, dict) else prof
        for ev in events:
            if isinstance(ev, dict) and ev.get("cat") == "Node":
                used.add(ev.get("args", {}).get("provider", "?"))
    except Exception:  # noqa: BLE001 -- profile 解析失败视为无节点
        return set()
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass
    return used


def _ort_available_providers() -> list[str]:
    """``onnxruntime.get_available_providers()`` 的兼容封装。

    onnxruntime 1.20.x（含 Windows 上 Py3.13 可用的 ``onnxruntime-gpu
    1.20.2``）顶层缺少 ``get_available_providers`` 属性（AttributeError，
    1.21+ 修复）；但 ``onnxruntime.capi._pybind_state`` 一直提供同名绑定，
    这里做双层兜底。任何失败返回空列表，调用方按“无 provider”处理，
    绝不向上层抛异常。
    """
    try:
        return list(onnxruntime.get_available_providers())
    except AttributeError:
        try:
            from onnxruntime.capi._pybind_state import (  # noqa: PLC0415
                get_available_providers as _pybind_get_available,
            )

            return list(_pybind_get_available())
        except Exception:  # noqa: BLE001 -- 兜底失败视为无 provider
            return []
    except Exception:  # noqa: BLE001 -- 非 AttributeError 也视为无 provider
        return []


def _probe_gpu_provider(name: str) -> bool:
    """单 GPU provider 执行级探测：该 provider 能否真正执行算子。"""
    try:
        available = _ort_available_providers()
    except Exception:  # noqa: BLE001 -- 探测环境异常视为不可用
        return False
    if name not in available:
        return False
    providers = [name]
    if "CPUExecutionProvider" in available:
        providers.append("CPUExecutionProvider")
    return name in _probe_providers(providers)


#: 永不探测/启用的 provider：pdf2zh 的后端开关（auto/cpu/cuda/dml）没有
#: TensorRT 对应项，它永远不会被主动选用；而执行级探测会创建 TRT 测试会话，
#: 在缺 TensorRT 运行库的机器上（绝大多数环境）ORT 的 C++ 层会直接向 stderr
#: 打印整段 "EP Error ... Please install TensorRT libraries ... Falling back"
#: 噪音（LoadLibrary error 126，绕过 Python logging），sidecar/服务形态下
#: 用户会误以为翻译出错。TRT 唯一的现实效果就是被 auto 全量列表带进请求，
#: 因此这里直接跳过探测 —— ``_COMPILED_PROVIDERS`` 过滤会把它从 auto 列表
#: 剔除（与库缺失时的既有行为一致），显式 cuda/dml 路径本就不含 TRT。
_NEVER_PROBE_PROVIDERS = frozenset({"TensorrtExecutionProvider"})


def _exec_gpu_providers() -> set[str]:
    """真正可用的非 CPU provider 集合（执行级探测，进程内缓存）。

    对每个注册的 GPU provider 分别做执行级探测（独立会话），避免多 GPU 并存时
    高优先级 provider 接管全部算子、掩盖其它 provider 的真实可用性。
    """
    global _EXEC_GPU_PROVIDERS
    if _EXEC_GPU_PROVIDERS is not None:
        return _EXEC_GPU_PROVIDERS
    result: set[str] = set()
    try:
        available = _ort_available_providers()
    except Exception:  # noqa: BLE001 -- 探测环境异常视为无 GPU
        available = []
    for name in available:
        if name != "CPUExecutionProvider" and name not in _NEVER_PROBE_PROVIDERS:
            try:
                if _probe_gpu_provider(name):
                    result.add(name)
            except Exception:  # noqa: BLE001 -- 单 provider 探测失败不影响其他
                logger.debug("GPU provider %r probe failed", name, exc_info=True)
    _EXEC_GPU_PROVIDERS = result
    return result


def _executable_alternative_providers(backend: str) -> tuple[str, list[str]] | None:
    """请求的 GPU 后端不可用时，探测另一个 GPU 后端是否执行级可用。

    典型场景（用户实测）：Windows 上装了 onnxruntime-directml（编译期注册表
    只有 ``AzureExecutionProvider``）却显式请求 ``cuda``——CUDA provider 根本
    未注册，原逻辑只能回退 CPU；而 DML 明明执行级可用（``torch.cuda.is_
    available()=True`` 的 NVIDIA 机器同样能走 DirectML）。跨后端兜底让
    「要 GPU 加速」的用户意图在错选后端名时仍能达成，并以 WARNING 说明
    实际选择、如何显式固定。

    Returns:
        ``(alt_backend, providers)``：替代后端名 + 执行级可用的 provider 列表
        （GPU 优先、CPU 兜底）；无可执行替代时 ``None``。
    """
    alt = {"cuda": "dml", "dml": "cuda"}.get(backend)
    if not alt:
        return None
    wanted = _BACKEND_PROVIDERS.get(alt) or []
    available = _ort_available_providers()
    exec_gpu = _exec_gpu_providers()
    for name in wanted:
        if name in available and name in exec_gpu:
            out = [name]
            if "CPUExecutionProvider" in available and (
                "CPUExecutionProvider" not in out
            ):
                out.append("CPUExecutionProvider")
            return alt, out
    return None


def _warn_gpu_substituted(
    backend: str, wanted: list[str], available: list[str],
    alt_backend: str, providers: list[str],
) -> None:
    """请求的 GPU 后端不可用、已切换到另一可执行 GPU 后端时的统一警告。"""
    logger.warning(
        "Backend '%s' requested but its GPU provider is not usable "
        "(wanted %s; available: %s); falling back to the executable "
        "'%s' backend (%s). Pin '--backend %s' to make this choice explicit.",
        backend, wanted, available, alt_backend, providers, alt_backend,
    )


def get_runtime_provider_status() -> dict:
    """Probe the current ONNX Runtime environment for GUI diagnostics.

    Returns:
        dict: ``onnxruntime`` 版本；``available`` 编译期注册的全部 provider；
        ``effective`` 用最小模型真实创建会话后实际生效的 provider；以及
        ``cuda``/``dml`` 是否真正可用（基于 ``effective``，而非仅凭编译期
        注册表——例如 onnxruntime-gpu 已装但缺 CUDA 运行库时注册表里有
        ``CUDAExecutionProvider``，实际却创建失败回退 CPU）。
    """
    global _runtime_provider_status_cache
    if _runtime_provider_status_cache is not None:
        return dict(_runtime_provider_status_cache)
    available = _ort_available_providers()
    exec_gpu = _exec_gpu_providers()
    effective = [
        p for p in available if p == "CPUExecutionProvider" or p in exec_gpu
    ]
    result = {
        "onnxruntime": getattr(onnxruntime, "__version__", "unknown"),
        "available": list(available),
        "effective": effective,
        "cuda": "CUDAExecutionProvider" in exec_gpu,
        "dml": bool(exec_gpu & {"AzureExecutionProvider", "DmlExecutionProvider"}),
    }
    _runtime_provider_status_cache = result
    return dict(result)


def resolve_providers(backend: str | None) -> list[str]:
    """把后端名解析为“实际可用”的 onnxruntime provider 列表。

    显式请求的 providers 会与 ``_ort_available_providers()``
    求交集；若后端名过时/缺失（例如 DirectML 在 onnxruntime >= 1.20 更名为
    ``AzureExecutionProvider``），不会静默退化为 CPU-only（这会导致父进程
    跑 CPU、spawn 出的 worker 却自动探测到 GPU 的不一致状态），而是带警告
    回退到自动探测。

    显式请求 ``cuda``/``dml`` 但对应 GPU provider 未安装（如缺少
    ``onnxruntime-gpu``）时，CPU 兜底项仍会使交集非空 —— 此时返回 CPU-only
    并记录明确警告（:func:`warn_gpu_unavailable`），避免“选 GPU 却静默跑
    CPU”。
    """
    available = _ort_available_providers()
    if backend and backend in _BACKEND_PROVIDERS:
        wanted = _BACKEND_PROVIDERS[backend]
        usable = [p for p in wanted if p in available]
        if usable:
            if backend in ("cuda", "dml") and all(
                p == "CPUExecutionProvider" for p in usable
            ):
                alt = _executable_alternative_providers(backend)
                if alt is not None:
                    _warn_gpu_substituted(
                        backend, wanted, available, alt[0], alt[1],
                    )
                    return alt[1]
                warn_gpu_unavailable(backend, wanted, available)
                return usable
            # 执行级校验：GPU provider 已注册但设备/运行库初始化失败时 ORT 会
            # 静默回退 CPU（get_providers() 仍返回 GPU 名）。这里在创建会话前
            # 提前识别并回退 CPU-only + 明确警告，避免"选 GPU 却无感知跑 CPU"。
            if backend in ("cuda", "dml"):
                gpu_names = {
                    "cuda": {"CUDAExecutionProvider"},
                    "dml": {"AzureExecutionProvider", "DmlExecutionProvider"},
                }
                if not (gpu_names[backend] & _exec_gpu_providers()):
                    cpu_only = [p for p in usable if p == "CPUExecutionProvider"]
                    alt = _executable_alternative_providers(backend)
                    if alt is not None:
                        _warn_gpu_substituted(
                            backend, wanted, available, alt[0], alt[1],
                        )
                        return alt[1]
                    warn_gpu_session_fallback(
                        backend, usable, cpu_only or ["CPUExecutionProvider"],
                    )
                    return cpu_only or ["CPUExecutionProvider"]
            return usable
        logger.warning(
            "Backend '%s' requested but no matching provider is available "
            "(available: %s); falling back to auto-detection.",
            backend, available,
        )
    # auto / None：返回全部注册 provider 交由 ORT 自选，但过滤掉“执行级确认
    # 不可用”的编译型 provider（典型：TensorRT 已注册但缺运行库）。否则每次
    # 会话创建 ORT 都会尝试加载缺失 GPU 库并打印 EP Error 噪音，且使
    # ``_COMPILED_PROVIDERS`` 命中导致优化缓存被跳过。执行级可用（如 CUDA
    # 真正能跑）的 provider 原样保留。
    if _COMPILED_PROVIDERS.intersection(available):
        exec_gpu = _exec_gpu_providers()
        degraded = [
            p for p in available
            if p in _COMPILED_PROVIDERS and p not in exec_gpu
        ]
        if degraded:
            logger.warning(
                "auto 后端跳过执行级不可用的 provider %s（缺运行库，会话创建将失败）",
                degraded,
            )
            return [p for p in available if p not in degraded]
    return available



def _configure_session_options() -> "onnxruntime.SessionOptions":
    """构造统一 ORT SessionOptions（含并行 worker 线程门控）。

    ``PDF2ZH_WORKER_ORT_THREADS=1`` 时把 intra/inter-op 线程限制为 1 并切
    ORT_SEQUENTIAL，避免多 worker × 全核导致的 CPU 争抢（默认行为不变，
    串行路径完全不受影响）。worker bootstrap 通过
    ``parallel.worker.init_worker_process`` 在 spaw 前设置该环境变量。
    """
    opts = onnxruntime.SessionOptions()
    if get_backend() == "dml" and _dml_effective():
        # DirectML EP 官方推荐 ORT_ENABLE_BASIC：ORT_ENABLE_ALL 的
        # NchwcTransformer 会把图优化为 CPU 专用 NCHWc 布局，DirectML 无法
        # 消费 → 算子全部回落 CPU（静默性能塌陷）。仅 DML 真正有效时降级；
        # DML 不可用回退 CPU 时保持 ORT_ENABLE_ALL。
        opts.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_BASIC
        )
    else:
        opts.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
    if os.environ.get("PDF2ZH_WORKER_ORT_THREADS", "") == "1":
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    if os.environ.get("PDF2ZH_ORT_NO_ARENA", "") == "1":
        # 关闭 CPU 内存 arena：arena 会预分配并只增不减，常驻多 worker 的
        # 服务形态下每个 worker 实测 ~490MB RSS（模型文件仅 72MB）。关 arena
        # 换直接 malloc/free，通常显著降低峰值 RSS，延迟影响个位数百分比。
        opts.enable_cpu_mem_arena = False
    return opts


#: NchwcTransformer（CPU 布局优化）相关的指令集特征：这些 flag 决定 CPU 版
#: 优化图（NCHWc 内核）的可用性（AVX/AVX2/AVX512）。指纹中出现这些 flag
#: 变化时缓存必须失效——ORT 官方文档明确 layout optimizations only usable
#: on compatible hardware。
_LAYOUT_CPU_FLAGS = frozenset({
    "sse", "sse2", "sse3", "ssse3", "sse4_1", "sse4_2",
    "avx", "avx2", "avx512f", "avx512cd", "avx512bw", "avx512dq",
    "avx512vl", "avx512vnni", "avx512_bf16", "avx_vnni",
    "fma", "f16c", "vnni",
})

#: 无法序列化优化图的 provider 集合（缓存只会为 CPU-only 生效）
_COMPILED_PROVIDERS = {"CoreMLExecutionProvider", "TensorrtExecutionProvider"}


def _dml_effective() -> bool:
    """DirectML 是否真正有效（执行级探测，进程内缓存）。"""
    return bool(
        _exec_gpu_providers() & {"AzureExecutionProvider", "DmlExecutionProvider"}
    )


def _cache_fingerprint_key() -> str:
    """计算 ORT 优化图缓存的环境指纹（12 位 sha1）。

    ORT 的离线优化模型与 ExecutionProvider、优化级别及硬件强绑定：CPU 的
    NCHWc 布局优化绑定指令集（AVX/AVX2/AVX512）、CUDA fused/contrib 内核绑定
    GPU、DirectML 图优化发生在 DML 内部——跨环境复用会静默把 GPU 会话固化在
    CPU 甚至崩溃（官方文档明确 ``cannot run a model pre-optimized for a GPU
    execution provider on a machine that is equipped only with CPU``，见
    doc/onnx_cpu_cuda_model_incompatibility_report.md）。

    指纹 = ort 版本 + backend/优化级别 + 架构 + CPU 型号/指令集 + provider
    集合；任何一项变化都会得到不同的缓存文件名，杜绝跨机器/跨指令集复用。
    """
    import hashlib

    backend = get_backend()
    if backend == "cuda":
        mode = "cuda"
    elif backend == "dml" and _dml_effective():
        mode = "dml-basic"
    else:
        mode = "cpu-all"
    parts = [
        "ort", onnxruntime.__version__, "mode", mode, "arch", platform.machine(),
    ]
    try:
        proc = platform.processor()
        if proc:
            parts += ["cpu", proc]
    except Exception:  # noqa: BLE001 -- 指纹容错
        pass
    try:
        import cpuinfo  # noqa: PLC0415 -- py-cpuinfo 可选

        flags = sorted(
            f
            for f in cpuinfo.get_cpu_info().get("flags", [])
            if f in _LAYOUT_CPU_FLAGS
        )
        if flags:
            parts += ["flags", ",".join(flags)]
    except Exception:  # noqa: BLE001 -- py-cpuinfo 缺失/失败时降级
        pass
    try:
        parts += [
            "providers", ",".join(sorted(_ort_available_providers())),
        ]
    except Exception:  # noqa: BLE001
        pass
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def _optimized_cache_path(model_path: str) -> str:
    """按 backend/优化级别/**环境指纹**隔离 ``.optimized`` ORT 图缓存文件。

    ORT 优化图包含与 provider/优化级别/硬件强相关的图变换（如 CPU 专用 NCHWc
    布局），跨环境复用会把 GPU 会话固化在 CPU 执行。指纹见
    :func:`_cache_fingerprint_key`；历史无指纹 ``<model>.optimized`` 无法验证
    来源，不再复用（首次启动重新生成一次，此后稳定命中）。

    - ``cpu`` / ``auto`` / DML 失效 → ``<model>.cpu-<fp>.optimized``（ALL 优化）
    - ``cuda`` → ``<model>.cuda-<fp>.optimized``（ALL 优化，CUDA 内核）
    - ``dml`` 有效 → ``<model>.dml-basic-<fp>.optimized``（BASIC，避开 NCHWc）
    """
    backend = get_backend()
    fp = _cache_fingerprint_key()
    if backend == "cuda":
        return f"{model_path}.cuda-{fp}.optimized"
    if backend == "dml" and _dml_effective():
        return f"{model_path}.dml-basic-{fp}.optimized"
    return f"{model_path}.cpu-{fp}.optimized"


def _should_generate_optimized_cache() -> bool:
    """GPU 显式后端（cuda/dml）不落盘缓存。

    指纹化缓存只在“存在同指纹缓存”时被复用（``state == cached``）；若不存在，
    GPU 会话直接走内存在线优化（不设置 ``optimized_model_filepath``），避免
    生成任何可能被其他环境复用/误读的磁盘优化图。CPU/auto 维持单写者落盘。
    """
    return get_backend() not in ("cuda", "dml")


def cleanup_stale_optimized_cache_tmp(model_path: str) -> int:
    """清理 ``<model>.*optimized.*.tmp`` 孤儿文件（生成进程已亡的残留）。

    旧版 ``_OptimizedCache`` 在缓存生成中途被强杀/崩溃时不会执行 ``os.replace``，
    导致 tmp 文件永久残留（本机实测 13 个 ≈980MB）。只删除：1) mtime 超过
    60 秒（排除仍在生成的 tmp）；2) 文件名中 pid 已不存活。绝不触碰正在生成
    的 tmp 或锁文件。

    Returns:
        本次清理的文件数。
    """
    removed = 0
    try:
        now = time.time()
        for tmp in glob.glob(model_path + ".*optimized.*.tmp"):
            try:
                if now - os.path.getmtime(tmp) < 60.0:
                    continue  # 可能仍在生成中（单次约 1-3s，60s 足够宽松）
                pid = int(os.path.basename(tmp).rsplit(".", 2)[-2])
                if pid > 0 and _pid_alive(pid):
                    continue  # 生成进程仍存活
                os.unlink(tmp)
                removed += 1
            except (OSError, ValueError, IndexError):
                continue
    except Exception:  # noqa: BLE001 -- 清理失败不阻断加载
        return removed
    return removed


#: 进程内 tmp 清理只执行一次（每个翻译任务都会创建 OnnxModel，避免重复全目录扫描）
_tmp_cleanup_done = False


def _cleanup_stale_tmp_once(model_path: str) -> None:
    """进程内只执行一次的孤儿 optimized tmp 清理（幂等、静默）。"""
    global _tmp_cleanup_done
    if _tmp_cleanup_done:
        return
    _tmp_cleanup_done = True
    try:
        n = cleanup_stale_optimized_cache_tmp(model_path)
        if n:
            logger.info(
                "Cleaned %d stale optimized-cache tmp file(s) for %s",
                n, os.path.basename(model_path),
            )
    except Exception:  # noqa: BLE001 -- 清理失败不阻断加载
        pass



#: .optimized 缓存并发锁：多进程同时生成同一缓存会互相截断，导致 ORT
#: 读取损坏文件时原生崩溃（无 traceback，worker 瞬时死亡 → BrokenProcessPool）。
class _OptimizedCache:
    """Manage the ``<model>.optimized`` ORT graph cache with cross-process safety.

    Exactly one process generates the cache (holding ``<path>.lock`` + writing to
    ``<path>.tmp`` followed by an atomic ``os.replace``); everyone else waits for
    the completed file. Stale locks from dead owners are reclaimed.
    """

    def __init__(self, optimized_path: str, timeout: float = 15.0):
        self.final = optimized_path
        self.lock_path = optimized_path + ".lock"
        self.tmp_path = f"{optimized_path}.{os.getpid()}.tmp"
        self.timeout = timeout
        self.state = "idle"  # idle | busy(本进程生成中) | cached(复用现成缓存)

    # ── 公共 API ──────────────────────────────────────────────────────────
    def acquire(self) -> str | None:
        """Try to obtain a usable cache path.

        Returns:
            the cache file path when a valid cache is (or becomes) available;
            None when the caller should handle caching in this process
            (either as the lock owner, or falling back to uncached loading).
        """
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass
        if self._try_lock():
            self.state = "busy"
            return None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._valid_cache():
                self.state = "cached"
                return self.final
            if not self._lock_held_by_owner():
                break  # 锁消失：owner 已完成或已亡，最后再确认一次缓存
            time.sleep(0.1)
        if self._valid_cache():
            self.state = "cached"
            return self.final
        if self._try_lock():
            self.state = "busy"
            return None
        logger.warning(
            "Optimized cache busy/locked; loading model without file cache (cached=%s)",
            os.path.basename(self.final),
        )
        self.state = "idle"
        return None

    def publish(self) -> None:
        """Atomically move the rendered cache into place (lock owner only)."""
        if self.state != "busy":
            return  # cached/idle 复用者无权发布，更不得触碰他人锁
        try:
            os.replace(self.tmp_path, self.final)
        except OSError as exc:
            logger.warning("Optimized cache publish failed: %s", exc)
        finally:
            self._release()

    def abort(self) -> None:
        """Roll back generation and release the lock (lock holder only)."""
        if self.state != "busy":
            return
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass
        self._release()

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _try_lock(self) -> bool:
        for _ in range(3):
            if self._lock_held_by_owner():
                return False
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    os.unlink(self.lock_path)  # 残留/无主锁 → 清除重试
                except OSError:
                    return False  # 锁不可删除（占用中）→ 视为他人持有
                continue
            except OSError:
                return False
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        return False

    def _lock_held_by_owner(self) -> bool:
        try:
            with open(self.lock_path, "rb") as fh:
                data = fh.read(32)
        except OSError:
            return False
        data = data.strip()
        if not data:
            return False
        try:
            pid = int(data)
        except ValueError:
            return False
        return pid > 0 and _pid_alive(pid)

    def _valid_cache(self) -> bool:
        try:
            if not os.path.exists(self.final):
                return False
            if os.path.getsize(self.final) < 1024:
                return False
            onnx.load(self.final, load_external_data=False)
            return True
        except Exception:
            return False

    def _release(self) -> None:
        try:
            os.unlink(self.lock_path)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Best-effort existence check for ``pid`` (cross-platform, signal-free).

    .. important::
       On Windows, ``os.kill(pid, 0)`` is **not** a pure probe: ``SIGINT == 0``
       (``signal.CTRL_C_EVENT``), so CPython maps it to
       ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``, which **broadcasts a
       Ctrl+C to the whole console** whenever ``pid`` lives in the same console
       / process group as the caller. A stale lock file written by the current
       process (or by an earlier run attached to the same console) then fires a
       phantom ``KeyboardInterrupt`` into every console process — including the
       GUI main thread — while a translation is running. Use a handle-open
       probe on Windows instead (POSIX keeps the standard ``os.kill(pid, 0)``).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # EPERM → 进程存在（仅权限不足）
    except OSError:
        return False


class DocLayoutModel(abc.ABC):
    @staticmethod
    def load_onnx():
        model = OnnxModel.from_pretrained()
        return model

    @staticmethod
    def load_available():
        return DocLayoutModel.load_onnx()


    @classmethod
    def ensure_model_prewarmed(cls) -> str | None:
        """主进程单写者预热入口：确保 doclayout 模型文件存在并生成/校验 optimized 缓存。

        并行启动前调用一次：worker 的 ``OnnxModel.load_available()`` →
        ``_OptimizedCache.acquire()`` 将直接命中 ``state=="cached"``，绝无并发
        写竞争（多 worker 同时生成同一缓存会互相截断，导致 ORT 读损坏文件时
        原生崩溃 → BrokenProcessPool）。

        Returns:
            模型路径（``str``）表示预热成功/模型可用；``None`` 表示模型不可用
            或预热失败（调用方应跳过并行，等价于整体串行兜底）。
        """
        try:
            pth = get_doclayout_onnx_model_path()
            if not pth:
                logger.warning("doclayout model path unavailable; prewarm skipped")
                return None
            pth = str(pth)
            if not os.path.exists(pth):
                logger.warning(
                    "doclayout model file missing (%s); prewarm skipped", pth
                )
                return None
            providers = resolve_providers(_preferred_backend)
            if not _COMPILED_PROVIDERS.intersection(providers):
                cache_holder = _OptimizedCache(_optimized_cache_path(pth))
                resolved = cache_holder.acquire()
                if resolved is not None:
                    return resolved  # 已有可用缓存：直接命中 cached
                if cache_holder.state == "busy":
                    if _should_generate_optimized_cache():
                        # 本进程持锁：生成 optimized 缓存并原子发布（单写者）
                        try:
                            opts = _configure_session_options()
                            opts.optimized_model_filepath = cache_holder.tmp_path
                            onnxruntime.InferenceSession(
                                pth, opts, providers=providers
                            )
                        except Exception as exc:  # noqa: BLE001 -- 缓存失败不阻断加载
                            cache_holder.abort()
                            logger.warning(
                                "prewarm cache generation failed (%s); "
                                "continuing without optimized cache", exc,
                            )
                        else:
                            cache_holder.publish()
                    else:
                        # GPU 显式后端：指纹不匹配时不落盘，释放锁直接在线优化
                        cache_holder.abort()
                # 锁竞争超时等场景：不生成缓存，直接返回模型路径（worker 安全降级）
            logger.info("doclayout model prewarmed: %s", pth)
            return pth
        except Exception as exc:  # noqa: BLE001 -- 预热失败只影响并行优化，不致命
            logger.warning(
                "ensure_model_prewarmed failed (%s); continuing without prewarm",
                exc,
            )
            return None


    @property
    @abc.abstractmethod
    def stride(self) -> int:
        """Stride of the model input."""
        pass

    @abc.abstractmethod
    def predict(self, image, imgsz=1024, **kwargs) -> list:
        """
        Predict the layout of a document page.

        Args:
            image: The image of the document page.
            imgsz: Resize the image to this size. Must be a multiple of the stride.
            **kwargs: Additional arguments.
        """
        pass


class YoloResult:
    """Helper class to store detection results from ONNX model."""

    def __init__(self, boxes, names):
        self.boxes = [YoloBox(data=d) for d in boxes]
        self.boxes.sort(key=lambda x: x.conf, reverse=True)
        self.names = names


class YoloBox:
    """Helper class to store detection results from ONNX model."""

    def __init__(self, data):
        self.xyxy = data[:4]
        self.conf = data[-2]
        self.cls = data[-1]


class OnnxModel(DocLayoutModel):
    def __init__(self, model_path: str):
        model_path = str(model_path)
        self.model_path = model_path
        _cleanup_stale_tmp_once(model_path)
        #: 动态 batch 支持检测结果缓存：None=未检测，True/False=已检测
        self._supports_batch = None

        # Extract metadata without full model deserialization
        model = onnx.load(model_path, load_external_data=False)
        metadata = {d.key: d.value for d in model.metadata_props}
        self._stride = ast.literal_eval(metadata["stride"])
        self._names = ast.literal_eval(metadata["names"])
        del model  # free memory before creating session

        sess_options = _configure_session_options()

        providers = resolve_providers(_preferred_backend)

        # Providers like CoreML generate compiled nodes that cannot be
        # serialized, so only cache the optimized graph for CPU-only.
        can_cache = not _COMPILED_PROVIDERS.intersection(providers)
        cache_holder = None
        if can_cache:
            cache_holder = _OptimizedCache(_optimized_cache_path(model_path))
            resolved = cache_holder.acquire()
            if resolved is not None:
                model_path = resolved  # state == "cached"：复用同指纹缓存
            elif cache_holder.state == "busy":
                if _should_generate_optimized_cache():
                    # 本进程持锁：加载原模型让 ORT 写 tmp，成功后原子发布
                    sess_options.optimized_model_filepath = cache_holder.tmp_path
                else:
                    # GPU 显式后端：指纹不匹配时绝不落盘，直接在线优化
                    cache_holder.abort()
                    cache_holder = None
            else:
                cache_holder = None  # 锁竞争超时：本次不写缓存（安全降级）
        try:
            self.model = onnxruntime.InferenceSession(
                model_path, sess_options, providers=providers
            )
        except Exception:
            # 仅锁持有者（busy）回滚/释放；cached 复用者不得动他人锁
            if cache_holder is not None and cache_holder.state == "busy":
                cache_holder.abort()
            raise
        if cache_holder is not None and cache_holder.state == "busy":
            cache_holder.publish()
        effective = self.model.get_providers()
        logger.info("ONNX Runtime providers: %s", effective)
        _check_session_fallback(_preferred_backend, providers, effective)

    @staticmethod
    def from_pretrained():
        pth = get_doclayout_onnx_model_path()
        return OnnxModel(pth)

    @property
    def stride(self):
        return self._stride

    def resize_and_pad_image(self, image, new_shape):
        """
        Resize and pad the image to the specified size, ensuring dimensions are multiples of stride.

        Parameters:
        - image: Input image
        - new_shape: Target size (integer or (height, width) tuple)
        - stride: Padding alignment stride, default 32

        Returns:
        - Processed image
        """
        if isinstance(new_shape, int):
            new_shape = (new_shape, new_shape)

        h, w = image.shape[:2]
        new_h, new_w = new_shape

        # Calculate scaling ratio
        r = min(new_h / h, new_w / w)
        resized_h, resized_w = int(round(h * r)), int(round(w * r))

        # Resize image
        image = cv2.resize(
            image, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR
        )

        # Calculate padding size and align to stride multiple
        pad_w = (new_w - resized_w) % self.stride
        pad_h = (new_h - resized_h) % self.stride
        top, bottom = pad_h // 2, pad_h - pad_h // 2
        left, right = pad_w // 2, pad_w - pad_w // 2

        # Add padding
        image = cv2.copyMakeBorder(
            image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114)
        )

        return image

    def scale_boxes(self, img1_shape, boxes, img0_shape):
        """
        Rescales bounding boxes (in the format of xyxy by default) from the shape of the image they were originally
        specified in (img1_shape) to the shape of a different image (img0_shape).

        Args:
            img1_shape (tuple): The shape of the image that the bounding boxes are for,
                in the format of (height, width).
            boxes (torch.Tensor): the bounding boxes of the objects in the image, in the format of (x1, y1, x2, y2)
            img0_shape (tuple): the shape of the target image, in the format of (height, width).

        Returns:
            boxes (torch.Tensor): The scaled bounding boxes, in the format of (x1, y1, x2, y2)
        """

        # Calculate scaling ratio
        gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])

        # Calculate padding size
        pad_x = round((img1_shape[1] - img0_shape[1] * gain) / 2 - 0.1)
        pad_y = round((img1_shape[0] - img0_shape[0] * gain) / 2 - 0.1)

        # Remove padding and scale boxes
        boxes[..., :4] = (boxes[..., :4] - [pad_x, pad_y, pad_x, pad_y]) / gain
        return boxes

    def predict(self, image, imgsz=1024, **kwargs):
        # Preprocess input image
        orig_h, orig_w = image.shape[:2]
        pix = self.resize_and_pad_image(image, new_shape=imgsz)
        pix = np.transpose(pix, (2, 0, 1))  # CHW
        pix = np.expand_dims(pix, axis=0)  # BCHW
        pix = pix.astype(np.float32) / 255.0  # Normalize to [0, 1]
        new_h, new_w = pix.shape[2:]

        # Run inference
        preds = self.model.run(None, {"images": pix})[0]

        # Postprocess predictions
        preds = preds[preds[..., 4] > 0.25]
        preds[..., :4] = self.scale_boxes(
            (new_h, new_w), preds[..., :4], (orig_h, orig_w)
        )
        return [YoloResult(boxes=preds, names=self._names)]

    @property
    def supports_batch(self) -> bool:
        """模型输入 ``batch`` 维是否动态（支持一次 ONNX 调度推理多页）。

        DocLayout-YOLO 以 ``dynamic_axes`` 导出（轴定义形如
        ``['batch', 3, 'height', 'width']``），因此 batch 维为 ``str`` 占位符
        （而非固定整数）—— 检测 ``input.shape[0]`` 是否为字符串/``None``
        即知可否 stack 多页。检测结果缓存于 ``_supports_batch``。
        """
        if self._supports_batch is None:
            try:
                dim = self.model.get_inputs()[0].shape[0]
                self._supports_batch = isinstance(dim, str) or dim is None
            except Exception:  # noqa: BLE001 -- 检测失败按不支持处理（安全降级）
                self._supports_batch = False
        return self._supports_batch

    def predict_batch(self, images, imgsz=None) -> list:
        """一次 ONNX 调度批量推理多页版面（动态 Batch 并行，V3 iteration）。

        将多张页面图片按逐页语义 letterbox（各自 ``int(h / 32) * 32``），
        左上角锚定放入公共 canvas ``[N, 3, H, W]`` 后单次 ``session.run``，
        让 ORT 底层（CPU SIMD/AVX512 或 GPU Tensor Core）并行处理 N 页 ——
        相比逐页推理大幅减少调度开销，且无需多进程/多线程（0 IPC、0 锁）。

        坐标语义与 ``predict`` 完全一致：每页用其实际输入尺寸
        ``(h1, w1)`` 做 ``scale_boxes``（canvas 空白填充不影响该页内容区，
        越界 box 由下游 clip）。同尺寸文档下 canvas 尺寸与逐页输入完全
        相同，逐页/批量结果逐位一致。

        当模型不支持动态 batch（``supports_batch is False``）时自动降级为
        逐页 ``predict``（行为与现状完全等价，逐页 imgsz 语义不变）。

        Args:
            images: 页面图像列表（HxWx3，uint8，BGR，与 ``predict`` 一致）。
            imgsz: 兼容参数；本实现按各页 ``int(h / 32) * 32`` 独立 letterbox
                （与逐页 predict 完全一致），无需调用方指定。

        Returns:
            ``List[YoloResult]``，长度等于 ``len(images)``，顺序一一对应。
        """
        if not images:
            return []
        if not self.supports_batch:
            # 降级：逐张 predict，imgsz 语义与逐页路径一致（每页按自身高度）。
            return [
                self.predict(img, imgsz=int(img.shape[0] / 32) * 32)[0]
                for img in images
            ]

        # 逐页 letterbox（与 predict 相同：aspect-preserve + stride 对齐填充），
        # 记录每页实际输入尺寸 (h1, w1) 用于后处理坐标还原。
        pre = []
        input_shapes = []
        orig_shapes = []
        for image in images:
            orig_shapes.append(image.shape[:2])
            page_imgsz = int(image.shape[0] / 32) * 32
            pix = self.resize_and_pad_image(image, new_shape=page_imgsz)
            pix = np.transpose(pix, (2, 0, 1))  # CHW
            pix = pix.astype(np.float32) / 255.0  # Normalize to [0, 1]
            pre.append(pix)
            input_shapes.append(pix.shape[1:])  # (h1, w1)

        # 公共 canvas：取 batch 内最大尺寸，左上角锚定放置各页内容。
        # 空白区域填 letterbox 同款 114 灰，避免引入模型未见的边缘噪声。
        canvas_h = max(h1 for h1, _ in input_shapes)
        canvas_w = max(w1 for _, w1 in input_shapes)
        batch = np.full(
            (len(pre), 3, canvas_h, canvas_w), 114.0 / 255.0, dtype=np.float32
        )
        for k, pix in enumerate(pre):
            h1, w1 = input_shapes[k]
            batch[k, :, :h1, :w1] = pix

        preds = self.model.run(None, {"images": batch})[0]  # [N, 300, 6]

        results = []
        for k, (orig_h, orig_w) in enumerate(orig_shapes):
            h1, w1 = input_shapes[k]
            p = preds[k]
            p = p[p[..., 4] > 0.25]
            p[..., :4] = self.scale_boxes((h1, w1), p[..., :4], (orig_h, orig_w))
            results.append(YoloResult(boxes=p, names=self._names))
        return results


class ModelInstance:
    value: OnnxModel = None
