"""Warm Process Pool —— 进程级常驻并行 worker（报告 §8.2.1）。

实测依据（``doc/performance_bottleneck_report.md`` §6.3）：并行模式每次任务
新建 spawn 子进程池，冷启动 ``import pdf2zh`` + doclayout ONNX 模型加载实测
8.2s（约占总耗时 29%）。本模块提供进程级共享的常驻 ``ProcessPoolExecutor``：

- 懒创建 + 任务间复用（服务层 / 多文件批量任务收益最大）；
- worker 崩溃（``BrokenProcessPool``）或 Ctrl+C 硬杀后经 ``mark_broken()``
  标记失效，下一次 ``get()`` 自动重建新池；
- 后端（CPU/GPU/DML）或 worker 数变化时自动重建（worker 侧 ``set_backend``
  只在 ``initializer`` 建池时执行一次）；
- ``PDF2ZH_WARM_POOL=1`` 环境变量启用；未启用时 ``get_shared_pool`` 返回
  ``None``（调用方回落旧行为 —— 每次任务新建池，CLI 单次任务零影响）。

风险缓解：
- ``initializer=init_worker_process`` 只在建池时执行一次，doclayout 模型加载
  进 ``ModelInstance`` 全局单例在任务间复用（与任务内复用语义一致）；
- 服务层需要重置模型/后端时调用 :func:`shutdown_shared_pool` 触发重建。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from typing import Optional

from pdf2zh.parallel.worker import init_worker_process

logger = logging.getLogger(__name__)

#: 启用开关（8.2.1）。服务层/批量任务设置 ``PDF2ZH_WARM_POOL=1`` 生效。
_ENV_ENABLE = "PDF2ZH_WARM_POOL"


class SharedProcessPool:
    """进程级共享的常驻 ``ProcessPoolExecutor``（Warm Pool）。"""

    def __init__(self, max_workers: int = 4, backend: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._executor: Optional[ProcessPoolExecutor] = None
        self._max_workers = max(2, int(max_workers))
        self._backend = backend
        self._broken = False

    # ── 生命周期 ─────────────────────────────────────────────────────────
    def get(self) -> ProcessPoolExecutor:
        """返回可用池；缺失/broken 时重建。worker 已在建池时经 initializer 初始化。"""
        with self._lock:
            if self._executor is None or self._broken:
                if self._executor is not None:
                    try:
                        self._executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:  # noqa: BLE001 -- 清理不阻塞重建
                        pass
                self._executor = ProcessPoolExecutor(
                    max_workers=self._max_workers,
                    initializer=init_worker_process,
                    initargs=(self._backend,),
                )
                self._broken = False
                logger.info(
                    "Warm pool created: %d worker(s), backend=%s",
                    self._max_workers, self._backend,
                )
            return self._executor

    def mark_broken(self) -> None:
        """标记池失效（worker 被硬杀/崩溃后），下次 ``get()`` 重建。幂等。"""
        with self._lock:
            if self._executor is not None:
                self._broken = True
                logger.warning("Warm pool marked broken; will rebuild on next use")

    def shutdown(self) -> None:
        """关闭并释放当前池（服务层关停 / 后端切换时调用）。幂等。"""
        with self._lock:
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                except Exception:  # noqa: BLE001 -- 清理不阻塞
                    pass
                self._executor = None
                self._broken = False
                logger.info("Warm pool shut down")


#: 模块级共享池（进程内唯一）。
_shared_pool: Optional[SharedProcessPool] = None
_shared_pool_lock = threading.Lock()


def warm_pool_enabled() -> bool:
    """Warm Pool 是否启用（``PDF2ZH_WARM_POOL=1``）。"""
    return os.environ.get(_ENV_ENABLE, "") == "1"


def get_shared_pool(
    workers: int, backend: Optional[str] = None
) -> Optional[SharedProcessPool]:
    """返回共享池；未启用时返回 ``None``（调用方回落旧行为）。

    backend / workers 与池不一致时自动重建（保证 worker 侧环境正确）。
    """
    global _shared_pool
    if not warm_pool_enabled():
        return None
    with _shared_pool_lock:
        if _shared_pool is None:
            _shared_pool = SharedProcessPool(max_workers=workers, backend=backend)
        elif (
            _shared_pool._backend != backend
            or _shared_pool._max_workers != max(2, int(workers))
        ):
            _shared_pool.shutdown()
            _shared_pool = SharedProcessPool(max_workers=workers, backend=backend)
        return _shared_pool


def shutdown_shared_pool() -> None:
    """关闭全局共享池（服务层关停 / 后端热切换时调用）。幂等。"""
    global _shared_pool
    with _shared_pool_lock:
        if _shared_pool is not None:
            _shared_pool.shutdown()
            _shared_pool = None
