"""GPU 并发调控器与 CUDA 生命周期隔离工具。

背景
----
BabelDOC 内部 ONNX（以及 pdf2zh 主链路 doclayout）的布局推理一旦开启
CUDA，伴随多线程/多任务并发时会出现两类故障：

1. **cuDNN 并发崩溃**：多个线程同时在同一个进程里 Execute 两个 CUDA 会话会
   触发 ``CUDNN_BACKEND_API_FAILED``。旧的规避是把所有 CUDA ``session.run``
   用一个全局互斥锁（``pdf2zh.doclayout._CUDA_RUN_LOCK``）串行化——稳定，但
   把 GPU 推理变成串行，并让 BabelDOC 与 pdf2zh 共享同一个跨框架的锁。

2. **fork 后共享 CUDA 状态死锁**：POSIX 下若在父进程已初始化 CUDA Context /
   ORT 会话后再 ``fork()``，子进程复制了带锁状态的 CUDA Driver 内存，任何
   CUDA 调用都会永久阻塞（典型 “卡死“）。

本模块主张的核心不变量（见
``doc/babeldoc_cuda_engine_fix.md``）：

> **线程可以共享 CUDA Session；进程不能继承/共享 CUDA Session。**

据此提供：

- :class:`GPUConcurrencyGovernor`：**有界并发**（``BoundedSemaphore``，并发度
  可由环境变量配置），替换“全局互斥锁串行化”。并发度 = 1 时语义与旧互斥锁
  完全一致（向后兼容），调到 N 时允许同一进程内 N 个 CUDA ``session.run``
  并发（配合 ORT 自身 intra-op 并发），而不是把所有推理钉死串行。
- :func:`get_governor`：以 ``scope`` 为键的**进程内单例注册表**，并绑定创建
  时的 PID。fork 出的子进程 PID 变化后会自动获得**全新、彼此独立**的
  governor —— 绝不会把父进程的信号量/会话“继承”到子进程，从根上切断
  “fork 后共享 CUDA 同步原语”。
- CUDA 生命周期跟踪：:func:`mark_cuda_initialized` / :func:`cuda_initialized`
  记录“*当前进程* 是否已初始化 CUDA”，fork 子进程继承的标记因为 PID 不同
  会自动失效 —— 让上层能判断“本进程里 CUDA 是否安全可用”。
- 进程本地线程预算：:func:`apply_process_local_thread_budget` 在**每个独立
  进程**（spawn worker / BabelDOC 子进程）入口用 ``setdefault`` 设定 OMP /
  MKL / ORT 线程数，避免两个生态互相改全局环境；默认是“有界并发”而非
  “把线程关死”。
"""

from __future__ import annotations

import logging
import multiprocessing as _mp
import os
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: 全局并发度环境变量（对所有 GPU scope 生效的默认值）。
_ENV_GPU_CONCURRENCY = "PDF2ZH_GPU_CONCURRENCY"

#: 作用域专项覆盖：``PDF2ZH_GPU_CONCURRENCY_<SCOPE>``。
_ENV_SCOPE_CONCURRENCY_PREFIX = "PDF2ZH_GPU_CONCURRENCY_"

#: 严格 fork 守卫开关：在确实是 fork 产生的子进程里，若检测到父进程可能
#: 已初始化 CUDA（本子进程编号与继承标记不一致），强制把请求的 GPU 后端
#: 降级为 CPU，避免 CUDA Driver 内部锁死锁造成“卡死”。
#: 默认关闭 —— 不改变现有的显式 ``cuda`` 行为；需要强隔离时再开启。
_ENV_STRICT_FORK_CUDA = "PDF2ZH_STRICT_FORK_CUDA"

#: 进程本地默认线程预算（有界并发；可用 ``PDF2ZH_PROCESS_THREADS`` 覆盖）。
_ENV_PROCESS_THREADS = "PDF2ZH_PROCESS_THREADS"
_DEFAULT_PROCESS_THREADS = 4

#: 每个进程默认允许的并发 CUDA ``session.run`` 数量（=1 保持向后兼容串行，
#: 但可调高为有界并发；建议不超过显存能承受的量）。
_DEFAULT_CONCURRENCY = 1


def _resolve_concurrency(scope: str) -> int:
    """解析指定 scope 的 CUDA 并发上限。

    优先级：``PDF2ZH_GPU_CONCURRENCY_<SCOPE>`` > ``PDF2ZH_GPU_CONCURRENCY`` > 1。
    非法取值回退到并发度 1（保守、稳定）。
    """
    name = scope.replace("-", "_").upper()
    for env_name in (
        f"{_ENV_SCOPE_CONCURRENCY_PREFIX}{name}",
        _ENV_GPU_CONCURRENCY,
    ):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            val = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r (expected an integer >= 1)", env_name, raw
            )
            return _DEFAULT_CONCURRENCY
        if val < 1:
            logger.warning("Ignoring %s=%r (must be >= 1; keeping 1)", env_name, raw)
            return _DEFAULT_CONCURRENCY
        return val
    return _DEFAULT_CONCURRENCY


class GPUConcurrencyGovernor:
    """有界并发调控器：限制同一作用域内并发的 GPU 推理数量，而非串行化。

    与全局互斥锁的本质区别：

    - ``Lock`` / 并发度 1 → 同时只有 1 个 ``session.run``（完全串行）；
    - ``BoundedSemaphore(N)`` → 最多 N 个 ``session.run`` 并发，其余排队，
      让 ORT / CUDA 自身在可承受并发内并行，而不是把所有推理钉死。
    """

    def __init__(self, scope: str, max_concurrent: Optional[int] = None) -> None:
        self.scope = scope
        self.max_concurrent = (
            _resolve_concurrency(scope) if max_concurrent is None else max_concurrent
        )
        if self.max_concurrent < 1:
            raise ValueError(
                f"max_concurrent for {scope!r} must be >= 1 "
                f"(got {self.max_concurrent})"
            )
        self._sem: threading.BoundedSemaphore = threading.BoundedSemaphore(
            self.max_concurrent
        )

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取一个并发名额。返回是否成功（超时可能失败）。"""
        if timeout is None:
            self._sem.acquire()
            return True
        return self._sem.acquire(timeout=timeout)

    def release(self) -> None:
        """归还一个并发名额。"""
        self._sem.release()

    def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在有界并发名额内执行 ``fn``。推荐的调用方式。"""
        with self._sem:
            return fn(*args, **kwargs)

    def __enter__(self) -> "GPUConcurrencyGovernor":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class _GovernorEntry:
    __slots__ = ("pid", "governor")

    def __init__(self, governor: GPUConcurrencyGovernor) -> None:
        self.pid = os.getpid()
        self.governor = governor


#: 进程内 governor 注册表。条目绑定创建时的 PID，以识别从 fork 继承的实例。
_GOVERNORS: Dict[str, _GovernorEntry] = {}


def get_governor(
    scope: str, max_concurrent: Optional[int] = None
) -> GPUConcurrencyGovernor:
    """返回指定作用域的进程内单例 governor。

    **进程不隔离**：若当前 PID 与注册条目的 PID 不一致（典型：POSIX fork
    出的子进程），会为该进程重建一个全新、彼此独立的 governor —— 绝不继承
    父进程的信号量，从而切断“fork 后共享 CUDA 同步原语”。

    Args:
        scope: 作用域名（如 ``"babeldoc"``、``"pdf2zh"``）。不同 scope 之间
            相互独立，不共享并发名额 —— 让 BabelDOC 与 pdf2zh 主链路彼此隔离。
        max_concurrent: 显式指定并发上限；``None`` 时从环境变量解析。
    """
    entry = _GOVERNORS.get(scope)
    if entry is not None and entry.pid == os.getpid():
        return entry.governor
    gov = GPUConcurrencyGovernor(scope, max_concurrent=max_concurrent)
    _GOVERNORS[scope] = _GovernorEntry(gov)
    logger.debug(
        "GPU governor %r ready in pid=%d (concurrency=%d)",
        scope,
        os.getpid(),
        gov.max_concurrent,
    )
    return gov


#: 本进程是否已初始化 CUDA（绑定 PID，fork 子进程继承的标记自动失效）。
_cuda_initialized_pid: Optional[int] = None


def mark_cuda_initialized() -> None:
    """记录**本进程**已初始化 CUDA runtime / ONNX CUDA 会话。"""
    global _cuda_initialized_pid
    _cuda_initialized_pid = os.getpid()


def cuda_initialized() -> bool:
    """**本进程**是否已初始化 CUDA。

    由于标记绑定 PID，fork 出子进程后继承的 ``_cuda_initialized_pid`` 指向
    父进程 PID，与本进程 PID 不同，因此返回 ``False`` —— 引导子进程重新
    初始化自己的 CUDA 上下文（“进程独立 CUDA 上下文”的要求）。
    """
    return _cuda_initialized_pid == os.getpid()


def reset_cuda_process_guard() -> None:
    """在全新（spawn）进程中重置 CUDA 初始化标记。

    供 worker bootstrap 使用：spawn 子进程没有父进程的 CUDA 上下文，这里
    显式置 ``None``，让后续判断都从“未初始化”开始，避免残留状态干扰。
    """
    global _cuda_initialized_pid
    _cuda_initialized_pid = None


def in_multiprocessing_process() -> bool:
    """当前是否运行在 multiprocessing 子进程中（fork 或 spawn）。"""
    try:
        return _mp.parent_process() is not None
    except Exception:  # noqa: BLE001 -- 老版本 / 受限环境
        return False


def current_start_method() -> str:
    """返回当前 multiprocessing 启动方式（``fork``/``spawn``/``forkserver``）。

    在子进程内获取可能受限，失败时保守返回空串。
    """
    try:
        return _mp.get_start_method()
    except Exception:  # noqa: BLE001
        return ""


def strict_fork_cuda_degrade_enabled() -> bool:
    """是否启用“严格 fork-CUDA 守卫”（默认关闭，不改变现有行为）。"""
    return os.environ.get(_ENV_STRICT_FORK_CUDA, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def fork_cuda_degrade_backend(backend: Optional[str]) -> Optional[str]:
    """fork 子进程 + 严格守卫下的后端降级守卫。

    规则（全部命中才降级）：
    - 指定的后端必须是 GPU（``cuda``/``dml``）；
    - 当前必须正运行在 multiprocessing 子进程中；
    - 启动方式必须是 ``fork``（spawn 天然安全）；
    - ``PDF2ZH_STRICT_FORK_CUDA=1`` 已开启。

    未开启严格开关时仅打日志（帮助诊断“卡死”），不改变后端。

    返回：
        需要降级时返回 ``cpu``；否则原样返回 ``backend``。
    """
    if backend not in ("cuda", "dml"):
        return backend
    if not in_multiprocessing_process():
        return backend
    if current_start_method() != "fork":
        return backend
    if not strict_fork_cuda_degrade_enabled():
        logger.warning(
            "Detected a fork child process (pid=%d) with a pending GPU backend %r; "
            "set %s=1 to force CPU here and avoid CUDA driver deadlocks inherited "
            "from the parent.",
            os.getpid(),
            backend,
            _ENV_STRICT_FORK_CUDA,
        )
        return backend
    logger.warning(
        "Strict fork-CUDA guard: degrading backend %r -> cpu in fork child pid=%d "
        "(CUDA context must live independently per process).",
        backend,
        os.getpid(),
    )
    return "cpu"


def suggest_concurrency_for_vram(available_mb: int) -> int:
    """按显存给出保守的并发建议（纯引导，不自动生效）。

    经验值（含保底余量）：
    - 8 GB   → 1~2
    - 12 GB  → 2~3
    - 24 GB  → 3~4
    - 更大  → 由调用方决定
    """
    if available_mb <= 0:
        return _DEFAULT_CONCURRENCY
    if available_mb <= 8 * 1024:
        return 1
    if available_mb <= 12 * 1024:
        return 2
    if available_mb <= 24 * 1024:
        return 3
    return 4


def resolve_process_thread_budget() -> int:
    """解析进程本地的默认线程预算（``PDF2ZH_PROCESS_THREADS``，默认 4）。"""
    raw = os.environ.get(_ENV_PROCESS_THREADS, "").strip()
    if raw:
        try:
            budget = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r (expected an integer)",
                _ENV_PROCESS_THREADS,
                raw,
            )
            budget = _DEFAULT_PROCESS_THREADS
        if budget < 1:
            budget = 1
    else:
        budget = _DEFAULT_PROCESS_THREADS
    return budget


def apply_process_local_thread_budget(scope: str, budget: Optional[int] = None) -> None:
    """在每个**独立进程**入口设定进程本地线程预算（有界并发，而非串行）。

    - 用 ``setdefault``：调用进程内已显式设置的值优先，不粗暴覆盖；
    - OMP/MKL 是“进程级全局”的，真正的进程隔离依赖这些环境是被独立进程
      （spawn worker / BabelDOC 子进程）读取；本函数不修改别的进程的全局；
    - 并发乘积 = 进程数 × (ORT intra-op + ORT inter-op + OMP + 应用线程)，
      由 budget 统一封顶，而非把线程全部关为 1。
    """
    if budget is None:
        budget = resolve_process_thread_budget()
    for env_name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
    ):
        if env_name not in os.environ:
            os.environ[env_name] = str(budget)
    os.environ.setdefault("PDF2ZH_PROCESS_THREAD_BUDGET", str(budget))
    logger.debug(
        "Process-local thread budget for %r set to %d (pid=%d)",
        scope,
        budget,
        os.getpid(),
    )


__all__ = [
    "GPUConcurrencyGovernor",
    "get_governor",
    "mark_cuda_initialized",
    "cuda_initialized",
    "reset_cuda_process_guard",
    "in_multiprocessing_process",
    "current_start_method",
    "strict_fork_cuda_degrade_enabled",
    "fork_cuda_degrade_backend",
    "suggest_concurrency_for_vram",
    "resolve_process_thread_budget",
    "apply_process_local_thread_budget",
]
