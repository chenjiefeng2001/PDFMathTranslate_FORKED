"""BabelDOC 内部 ONNX 推理后端的 GPU（CUDA/DML）适配开关。

背景
----
BabelDOC 0.6.x 包内 ``babeldoc/docvision/doclayout.py`` 的 ``OnnxModel``
把 provider 选择硬编码为仅 CPU（源码注释 "disable dml|cuda; directml/cuda
may encounter problems under special circumstances"）。因此即使 pdf2zh 主链路
已经通过 ``--backend cuda`` / ``pdf2zh.doclayout.set_backend("cuda")`` 选中
CUDA 后端，BabelDOC 的版面分析（doclayout ONNX）仍会在 CPU 上执行 —— 大文档
的版面阶段成为吞吐瓶颈。

本模块通过进程内、幂等的运行时补丁（monkey-patch）把 BabelDOC 内部 ONNX 会话
的 provider 选择接到 pdf2zh 的后端开关上，完成 GPU/CUDA 后端适配：

=============  ==============================================================
开关（优先级）  取值
=============  ==============================================================
环境变量         ``PDF2ZH_BABELDOC_BACKEND`` ∈ ``auto``/``cpu``/``cuda``/``dml``
                （最高优先级，可单独控制 BabelDOC 而不影响 pdf2zh 版面后端）
``set_backend``  ``pdf2zh.doclayout.set_backend()``（CLI ``--backend``）的选择
默认            ``auto`` = 保持 BabelDOC 原生行为（CPU-only / macOS CoreML）
=============  ==============================================================

显式后端解析：
- ``cuda`` → ``CUDAExecutionProvider`` + ``CPUExecutionProvider``；
- ``dml``  → ``AzureExecutionProvider``（onnxruntime>=1.20 新名，含旧名
  ``DmlExecutionProvider`` 兜底）+ ``CPUExecutionProvider``；
- ``cpu``  → 仅 ``CPUExecutionProvider``。

请求的 GPU provider 在当前环境不可用（未安装 ``onnxruntime-gpu`` /
``onnxruntime-directml``，或驱动/显存缺失）时，带 warning 回退为 CPU-only，
并让 ``OnnxModel`` 走 BabelDOC 原生 CPU 初始化 —— 绝不会让 BabelDOC 崩溃。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

#: 显式后端 → onnxruntime provider 候选（顺序即优先级；与
#: ``pdf2zh.doclayout._BACKEND_PROVIDERS`` 保持一致）。
_BACKEND_PROVIDERS = {
    "cpu": ["CPUExecutionProvider"],
    "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "dml": ["AzureExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
}

#: 允许的后端名（含 auto）。
_VALID_BACKENDS = frozenset({"auto", "cpu", "cuda", "dml"})

#: ``PDF2ZH_BABELDOC_BACKEND`` 环境变量名。
_ENV_BACKEND = "PDF2ZH_BABELDOC_BACKEND"

#: 补丁锁 + 原始 ``__init__`` 引用（None 表示未打补丁）。
_PATCH_LOCK = threading.Lock()
_ORIGINAL_INIT: Optional[object] = None

#: CPU 回退提示只打一次（进程级），避免多任务刷屏。
_GPU_HINT_LOGGED = False


def _log_gpu_acceleration_hint(providers: list[str]) -> None:
    """布局推理落在 CPU 时的一次性加速引导（P0-3，纯日志无行为变化）。

    大文档的墙钟大头是逐页版面分析（见
    doc/babeldoc_large_doc_slow_progress_report.md §2.1）；GPU 版 onnxruntime
    缺失/未生效时给出可操作的安装与开关指引。
    """
    global _GPU_HINT_LOGGED
    if _GPU_HINT_LOGGED:
        return
    _GPU_HINT_LOGGED = True
    logger.info(
        "BabelDOC layout inference is running on CPU (providers=%s). Large "
        "documents spend most of their wall time in per-page layout analysis; "
        "consider 'pip install onnxruntime-gpu' (NVIDIA) or "
        "'pip install onnxruntime-directml' (Windows GPU) and setting "
        "PDF2ZH_BABELDOC_BACKEND=cuda|dml to accelerate it.",
        providers,
    )


def get_babeldoc_backend() -> Optional[str]:
    """解析 BabelDOC 内部 ONNX 的有效后端（``None``/``auto`` = 原生行为）。

    优先级：``PDF2ZH_BABELDOC_BACKEND`` 环境变量 > pdf2zh ``set_backend()``。
    ``auto`` 返回 ``None``，与 ``pdf2zh.doclayout.get_backend()`` 语义一致。
    """
    override = os.environ.get(_ENV_BACKEND, "").strip().lower()
    if override:
        if override not in _VALID_BACKENDS:
            logger.warning(
                "Ignoring invalid %s=%r (expected one of %s); "
                "falling back to the pdf2zh backend selection",
                _ENV_BACKEND, override, sorted(_VALID_BACKENDS),
            )
        else:
            return None if override == "auto" else override
    try:
        from pdf2zh.doclayout import get_backend  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- 无 pdf2zh.doclayout 时按原生行为
        return None
    return get_backend()


def _cpu_only(available) -> list[str]:
    """取 CPU-only provider（保持 BabelDOC 原生 CPU 语义）。"""
    cpu = [p for p in available if p == "CPUExecutionProvider"]
    return cpu or list(available)


def _babeldoc_gpu_ineffective(backend: str, gpu: list[str]) -> bool:
    """GPU provider 已注册但执行级探测判定不可用（ORT 静默回退 CPU）。

    对 DirectML 而言，``onnxruntime >= 1.20`` 注册了 ``AzureExecutionProvider``
    但 D3D12 设备初始化失败时，ORT 不抛异常、``get_providers()`` 仍返回
    ``Azure``，算子却在 CPU 执行——仅凭注册表无法判断 GPU 是否真正生效。
    这里复用 :func:`pdf2zh.doclayout._exec_gpu_providers` 的执行级探测结果。
    """
    try:
        from pdf2zh.doclayout import _exec_gpu_providers  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- 探测不可用时宽松放行（回退到会话级校验）
        return False
    names = {
        "cuda": {"CUDAExecutionProvider"},
        "dml": {"AzureExecutionProvider", "DmlExecutionProvider"},
    }
    expected = names.get(backend, set())
    if not expected:
        return False
    return not (expected & _exec_gpu_providers())


def _warn_babeldoc_gpu_session_fallback(
    backend: str, requested: list[str], effective: list[str],
) -> None:
    """GPU provider 注册但无法真正执行时的统一警告（复用 doclayout 提示文案）。"""
    try:
        from pdf2zh.doclayout import warn_gpu_session_fallback  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- 降级为本地警告
        warn_gpu_session_fallback = None
    if warn_gpu_session_fallback is not None:
        warn_gpu_session_fallback(backend, requested, effective)
    else:
        logger.warning(
            "BabelDOC backend '%s' was requested but the ONNX session fell back "
            "to CPU (requested %s; effective %s)",
            backend, requested, effective,
        )


def resolve_babeldoc_providers(backend: Optional[str] = None) -> list[str]:
    """把 BabelDOC 后端名解析为“实际可用”的 onnxruntime provider 列表。

    - ``auto``/``None`` → 仅 CPU（与 BabelDOC 原生行为一致，不静默启用 GPU）；
    - 显式 ``cuda``/``dml`` → 与 ``_ort_available_providers()``
      求交集，GPU provider 优先，CPU 兜底；
    - 请求的 GPU provider 不可用 → 带 warning 回退 CPU-only。

    Args:
        backend: 后端名（不传则取 ``get_babeldoc_backend()``）。

    Returns:
        可直接传给 ``onnxruntime.InferenceSession(providers=...)`` 的列表。
    """
    from pdf2zh.doclayout import _ort_available_providers  # noqa: PLC0415
    from pdf2zh.doclayout import (  # noqa: PLC0415
        _executable_alternative_providers,
        _warn_gpu_substituted,
    )

    if backend is None:
        backend = get_babeldoc_backend()
    available = _ort_available_providers()
    if backend is None or backend == "auto":
        return _cpu_only(available)

    name = str(backend).lower()
    wanted = _BACKEND_PROVIDERS.get(name)
    if wanted is None:
        logger.warning(
            "Unknown BabelDOC backend %r (expected one of %s); using CPU",
            backend, sorted(_BACKEND_PROVIDERS),
        )
        return _cpu_only(available)

    usable = [p for p in wanted if p in available]
    if not usable:
        logger.warning(
            "BabelDOC backend '%s' requested but none of %s is available "
            "(available: %s); using CPU",
            name, wanted, available,
        )
        return _cpu_only(available)

    gpu = [p for p in usable if p != "CPUExecutionProvider"]
    if not gpu:
        # 显式请求 GPU 但 GPU provider 缺失（CPU 兜底项使交集非空）：
        # 先尝试跨后端兜底（如请求 cuda 但本机只有可执行的 DirectML），
        # 仍无解才给出可执行修复提示，而非静默回退 CPU。
        alt = _executable_alternative_providers(name)
        if alt is not None:
            _warn_gpu_substituted(name, wanted, available, alt[0], alt[1])
            return alt[1]
        try:
            from pdf2zh.doclayout import warn_gpu_unavailable  # noqa: PLC0415
        except Exception:  # noqa: BLE001 -- 降级为本地警告
            warn_gpu_unavailable = None
        if warn_gpu_unavailable is not None:
            warn_gpu_unavailable(name, wanted, available)
        else:
            logger.warning(
                "BabelDOC backend '%s' requested but no GPU provider is available "
                "(wanted %s; available: %s); using CPU",
                name, wanted, available,
            )
    elif _babeldoc_gpu_ineffective(name, gpu):
        # 执行级校验：GPU provider 已注册但设备/运行库初始化失败时 ORT 会
        # 静默回退 CPU（get_providers() 仍返回 GPU 名）。与 pdf2zh.doclayout 一致，
        # 提前识别并回退 CPU-only，避免创建无效 GPU 会话后毫无感知。
        alt = _executable_alternative_providers(name)
        if alt is not None:
            _warn_gpu_substituted(name, wanted, available, alt[0], alt[1])
            return alt[1]
        _warn_babeldoc_gpu_session_fallback(name, usable, gpu)
        cpu_only = [p for p in usable if p == "CPUExecutionProvider"]
        return cpu_only or ["CPUExecutionProvider"]
    result = list(gpu)
    if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in result:
        result.append("CPUExecutionProvider")
    return result or _cpu_only(available)



def _init_with_providers(self, model_path: str, providers: list[str]) -> None:
    """复制 babeldoc 0.6.x ``OnnxModel.__init__`` 主体，仅替换 provider 选择。

    与原实现一致的属性集：``model_path``/``_stride``/``_names``/``model``/
    ``lock``；SessionOptions 复用 pdf2zh 的统一配置（图优化 + worker 线程门控）。
    """
    import ast  # noqa: PLC0415
    import threading  # noqa: PLC0415

    import onnx  # noqa: PLC0415
    import onnxruntime  # noqa: PLC0415

    self.model_path = model_path
    model = onnx.load(model_path)
    metadata = {d.key: d.value for d in model.metadata_props}
    self._stride = ast.literal_eval(metadata["stride"])
    self._names = ast.literal_eval(metadata["names"])

    try:
        from pdf2zh.doclayout import _configure_session_options  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- 统一 SessionOptions 不可用时用默认
        _configure_session_options = None
    opts = (
        _configure_session_options()
        if _configure_session_options is not None
        else onnxruntime.SessionOptions()
    )
    self.model = onnxruntime.InferenceSession(
        model.SerializeToString(), opts, providers=providers,
    )
    self.lock = threading.Lock()


def _session_has_gpu(backend: str, effective: list[str]) -> bool:
    """Reuse :func:`pdf2zh.doclayout.has_gpu_provider` for a created session."""
    try:
        from pdf2zh.doclayout import has_gpu_provider  # noqa: PLC0415

        return has_gpu_provider(backend, effective)
    except Exception:  # noqa: BLE001 -- babeldoc 可选依赖不可用时宽松判断
        return any(p != "CPUExecutionProvider" for p in effective)


def _patched_init(self, model_path: str) -> None:
    """替换后的 ``OnnxModel.__init__``。

    - ``auto`` → 复用 pdf2zh 主链路的 auto 语义（:func:`pdf2zh.doclayout
      .resolve_providers` 的执行级探测）：GPU（CUDA/DML/CoreML）真正可用时
      自动启用，避免"主链路 doclayout 已走 GPU、BabelDOC 内部 ONNX 仍 CPU"
      的撕裂；无可执行 GPU 时回退原始 ``__init__``（保持 BabelDOC 原生
      CPU / macOS CoreML 行为）；
    - 显式 ``cuda``/``dml``/``cpu`` → 按开关解析 provider 创建会话；
      任何异常（如 CUDA 会话创建失败）自动回退原始 CPU 初始化，绝不抛出。
    """
    if _ORIGINAL_INIT is None:
        raise RuntimeError(
            "BabelDOC backend patch not applied (call apply_babeldoc_backend)"
        )
    backend = get_babeldoc_backend()
    if backend is None or backend == "auto":
        try:
            from pdf2zh.doclayout import resolve_providers as _main_resolve  # noqa: PLC0415

            providers = list(_main_resolve(None))
        except Exception:  # noqa: BLE001 -- 主链路解析失败按原生 CPU 兜底
            providers = resolve_babeldoc_providers("auto")
        if not any(p != "CPUExecutionProvider" for p in providers):
            # 无执行级可用 GPU：保持 BabelDOC 原生行为（含 macOS CoreML 特判）。
            _log_gpu_acceleration_hint(providers)
            return _ORIGINAL_INIT(self, model_path)
    else:
        providers = resolve_babeldoc_providers(backend)
    try:
        _init_with_providers(self, model_path, providers)
    except Exception as exc:  # noqa: BLE001 -- GPU 不可用/损坏时回退 CPU
        logger.warning(
            "BabelDOC ONNX init failed with providers=%s (%s: %s); "
            "falling back to the original CPU init",
            providers, type(exc).__name__, str(exc)[:160],
        )
        return _ORIGINAL_INIT(self, model_path)
    effective = list(self.model.get_providers())
    logger.info(
        "BabelDOC doclayout ONNX providers=%s (backend=%s)", effective, backend,
    )
    if (
        backend in ("cuda", "dml")
        # 解析阶段已把「provider 缺失/注册但不可执行」降级为 CPU-only 时，
        # resolve_babeldoc_providers 已给出针对性警告——这里不再重复报
        # 「session 回退」，避免同一根因刷两条吓人消息（用户实测反馈）。
        and any(p != "CPUExecutionProvider" for p in providers)
        and not _session_has_gpu(backend, effective)
    ):
        # 注册表里有 GPU provider 但真实创建会话时回退 CPU（缺 CUDA/cuDNN
        # 运行库 DLL 等）：给出与 pdf2zh.doclayout 一致的明确警告。
        try:
            from pdf2zh.doclayout import (  # noqa: PLC0415
                warn_gpu_session_fallback,
            )
        except Exception:  # noqa: BLE001 -- 降级为本地警告
            warn_gpu_session_fallback = None
        if warn_gpu_session_fallback is not None:
            warn_gpu_session_fallback(backend, providers, effective)
        else:
            logger.warning(
                "BabelDOC backend '%s' requested but the ONNX session fell "
                "back to CPU (requested %s; effective %s)",
                backend, providers, effective,
            )


def apply_babeldoc_backend() -> bool:
    """把 BabelDOC 内部 ``OnnxModel`` 接到 pdf2zh 后端开关（幂等）。

    幂等：同一进程重复调用只补丁一次。补丁在会话*创建时*读取当前后端，
    因此调用方后续 ``set_backend("cuda")`` 对新会话依然生效。

    Returns:
        True 表示补丁已生效（或原本已生效）；False 表示 babeldoc 不可用。
    """
    global _ORIGINAL_INIT
    try:
        from babeldoc.docvision.doclayout import (
            OnnxModel as BabelOnnxModel,  # noqa: PLC0415
        )
    except Exception:  # noqa: BLE001 -- babeldoc 可选依赖
        logger.debug("babeldoc not importable; backend patch skipped")
        return False
    with _PATCH_LOCK:
        if _ORIGINAL_INIT is not None:
            return True
        _ORIGINAL_INIT = BabelOnnxModel.__init__
        BabelOnnxModel.__init__ = _patched_init
        logger.info(
            "BabelDOC internal ONNX backend patched (effective backend=%s)",
            get_babeldoc_backend(),
        )
        return True


def reset_babeldoc_backend() -> bool:
    """恢复 BabelDOC ``OnnxModel.__init__`` 原始实现（主要供测试使用）。

    Returns:
        True 表示已恢复；False 表示 babeldoc 不可用。
    """
    global _ORIGINAL_INIT
    try:
        from babeldoc.docvision.doclayout import (
            OnnxModel as BabelOnnxModel,  # noqa: PLC0415
        )
    except Exception:  # noqa: BLE001
        return False
    with _PATCH_LOCK:
        if _ORIGINAL_INIT is None:
            return True
        BabelOnnxModel.__init__ = _ORIGINAL_INIT
        _ORIGINAL_INIT = None
        logger.info("BabelDOC internal ONNX backend patch restored")
        return True
