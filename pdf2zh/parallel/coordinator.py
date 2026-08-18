"""Bounded in-flight 窗口调度 + 有限重试 + 增量降级 + KeyboardInterrupt 短路。

取代旧 ``_translate_parallel`` 的一次性全提交模式：
- 初始提交 ``min(total, max_workers * in_flight_multiplier)`` 个 chunk；
  每完成一个补一个（结果合并内存上限 = 窗口大小，不再随 chunk 数线性增长）。
- chunk 级失败策略：
  - 进程崩溃（``BrokenProcessPool``）：所有未完成 chunk 记 failed → 串行补跑
    （启动即崩则升格为 ``WorkerBootstrapError`` → 外部整体串行）；
  - 单 chunk 异常（``PageProcessingError`` / 非空 ``error_message``）：
    原地重试 ``retry_limit`` 次，仍失败 → 串行补跑；
  - pickle 违例（``ProtocolViolationError``）：任务契约不合法 → 整体串行。
- ``KeyboardInterrupt``：``_force_terminate_workers`` 硬杀所有存活 worker 后
  ``shutdown(wait=False, cancel_futures=True)`` 并重抛 —— 正在运行的 chunk 也会
  被立即中断（不再跑完才退出），绝不进入任何串行兜底。除直接异常传播外，还经
  ``pdf2zh.parallel.interrupt`` 的进程级 Ctrl+C 旗标在提交/等待/池崩三处主动短路
  （GUI 场景主线程的 KeyboardInterrupt 被 gradio 吞掉、翻译线程收不到时尤其关键）。

返回 ``(obj_patch, obs_bundles, serial_patch_indices)`` —— 增量降级的
内存态 manifest（不落盘）。池整体不可用时抛 ``ParallelError`` 子类。
"""

from __future__ import annotations

import concurrent.futures
import logging
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from concurrent.futures.process import BrokenProcessPool

from pdf2zh.parallel.chunk import ChunkManifest, ChunkResult, ChunkTask
from pdf2zh.parallel.errors import (
    PageProcessingError,
    ProtocolViolationError,
    WorkerBootstrapError,
    WorkerProcessError,
)
from pdf2zh.parallel.interrupt import is_interrupted
from pdf2zh.parallel.worker import execute_chunk

logger = logging.getLogger(__name__)

_PICKLE_HINTS = ("cannot pickle", "not picklable", "PicklingError")


def _is_pickle_error(exc: Exception) -> bool:
    import pickle

    if isinstance(exc, (pickle.PicklingError, TypeError)):
        msg = str(exc).lower()
        return any(h in msg for h in _PICKLE_HINTS) or "cannot serialize" in msg
    return isinstance(exc, AttributeError)


class TaskCoordinator:
    """Bounded in-flight 调度器（生产路径 = 进程池；测试可注入假 executor/fn）。"""

    def __init__(
        self,
        max_workers: int = 4,
        in_flight_multiplier: int = 2,
        retry_limit: int = 1,
    ) -> None:
        self.max_workers = max(1, int(max_workers))
        self.in_flight_multiplier = max(1, int(in_flight_multiplier))
        self.retry_limit = max(0, int(retry_limit))

    # ── 公共 API ──────────────────────────────────────────────────────────
    def run(
        self,
        chunk_tasks: List[ChunkTask],
        progress_cb: Optional[Callable[[float, str], None]] = None,
        initializer: Optional[Callable[..., None]] = None,
        initargs: tuple = (),
        executor_factory: Optional[Callable[..., Any]] = None,
        task_fn: Optional[Callable[[ChunkTask], ChunkResult]] = None,
        # 8.2.1 Warm Process Pool：reuse_executor=True 时任务结束后不
        # shutdown 池（常驻复用）；pool_owner 收到中断/异常后标记 broken
        # 供下次重建（worker 被硬杀后池内部状态已不可信）。
        reuse_executor: bool = False,
        pool_owner: Optional[Any] = None,
    ) -> Tuple[dict, list, List[int]]:
        """窗口调度执行全部 chunk，返回 ``(obj_patch, obs_bundles, serial_indices)``。

        参数 ``executor_factory`` / ``task_fn`` 供测试注入（默认进程池 + 真任务）。
        """
        total = len(chunk_tasks)
        obj_patch: dict = {}
        obs_bundles: list = []
        if total == 0:
            return obj_patch, obs_bundles, []

        max_in_flight = min(total, self.max_workers * self.in_flight_multiplier)
        manifest = ChunkManifest(total)
        retry_left = {i: self.retry_limit for i in range(total)}
        serial_indices: List[int] = []

        run_task = task_fn or execute_chunk
        factory = executor_factory or self._default_executor_factory
        executor = factory(self.max_workers, initializer, initargs)

        pending: List[int] = list(range(total))
        inflight: Dict[Any, int] = {}

        def _submit(idx: int) -> None:
            # Ctrl+C 已请求：绝不提交新 chunk（GPU 会话、翻译线程等都不该再启动）。
            if is_interrupted():
                raise KeyboardInterrupt(
                    "Parallel engine aborted: Ctrl+C received before chunk "
                    f"{idx} submission"
                )
            manifest.mark_running(idx)
            try:
                inflight[executor.submit(run_task, chunk_tasks[idx])] = idx
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 -- pickle 违例等 submit 期异常
                if _is_pickle_error(exc):
                    raise ProtocolViolationError(
                        f"chunk {idx} not picklable: {type(exc).__name__}: {exc}"
                    ) from exc
                raise

        try:
            for _ in range(max_in_flight):
                if not pending:
                    break
                _submit(pending.pop(0))

            while inflight:
                # Ctrl+C 轮询：chunk 运行中也能在 ≤0.5s 内感知并短路，
                # 不必等当前 chunk 跑完（Windows GUI 场景见 interrupt 模块）。
                if is_interrupted():
                    raise KeyboardInterrupt(
                        "Parallel engine aborted: Ctrl+C received while "
                        "chunks in flight"
                    )
                done, _ = concurrent.futures.wait(
                    list(inflight),
                    timeout=0.5,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    continue
                # 第一遍：先合并所有成功结果；失败按类收集（两阶段保证
                # "先看到成功、再判池崩" —— 避免同批 done 中 broken 先被
                # 处理时把"部分成功后的池崩"误判为 bootstrap）。
                pool_broken = False
                broken_chunks: List[int] = []
                for future in done:
                    idx = inflight.pop(future)
                    try:
                        result = future.result()
                    except KeyboardInterrupt:
                        raise
                    except BrokenProcessPool as bpp:
                        pool_broken = True
                        broken_chunks.append(idx)
                        logger.warning(
                            "Parallel worker pool crashed (%s); chunk %d "
                            "queued for serial fallback", str(bpp)[:120], idx,
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001
                        self._handle_chunk_failure(
                            idx, exc, manifest, serial_indices, retry_left, pending
                        )
                        continue

                    if isinstance(result, ChunkResult) and not result.ok:
                        self._handle_chunk_failure(
                            idx,
                            PageProcessingError(result.error_message),
                            manifest,
                            serial_indices,
                            retry_left,
                            pending,
                        )
                    else:
                        manifest.mark_ok(idx)
                        if result.obj_patch:
                            obj_patch.update(result.obj_patch)
                        if result.obs_bundle:
                            obs_bundles.append(result.obs_bundle)
                        if callable(progress_cb):
                            done_chunks = manifest.ok_count + len(serial_indices)
                            try:
                                progress_cb(
                                    done_chunks * 100.0 / total,
                                    f"Translating {done_chunks}/{total} chunk(s)",
                                )
                            except Exception:  # noqa: BLE001
                                pass

                if pool_broken:
                    # Ctrl+C 恰与 worker 崩溃同刻：一律按“用户中断”短路 ——
                    # 绝不把中断误判为 bootstrap/协议失败而触发整文档串行兜底。
                    if is_interrupted():
                        raise KeyboardInterrupt(
                            "Parallel engine aborted by Ctrl+C during worker crash"
                        )
                    # 池启动即死（尚无任何成功成果）→ bootstrap/protocol 失败 → 整体串行；
                    # 已有成功成果 → 增量降级：本批 + 其余在途 chunk 全部进串行补跑。
                    if manifest.ok_count == 0 and not obj_patch:
                        raise WorkerBootstrapError(
                            "worker pool failed before any chunk succeeded "
                            f"({len(broken_chunks)} chunk(s) crashed)"
                        )
                    for idx in broken_chunks:
                        manifest.mark_failed(idx)
                        serial_indices.append(idx)
                    for fidx in list(inflight.values()):
                        manifest.mark_failed(fidx)
                        serial_indices.append(fidx)
                    inflight.clear()
                    break

                # 窗口补充：每完成一个补一个（v2 Lazy 提交思想）
                while len(inflight) < max_in_flight and pending:
                    _submit(pending.pop(0))
        finally:
            # Ctrl+C 短路（旗标或异常传播任一触发）：直接 terminate 所有存活
            # worker —— ``shutdown(cancel_futures=True)`` 只能取消未开始的 future，
            # 正在运行的 chunk 会让 worker 继续跑完才退出（真实场景一个 chunk
            # 54 页 ≈ 2 分钟，日志里 Ctrl+C 后残留的 4 条 tqdm 进度条即是）。
            # 正常完成 / 串行兜底路径不 terminate（worker 自然结束或随池崩退出）。
            interrupted = is_interrupted() or sys.exc_info()[0] is KeyboardInterrupt
            if interrupted:
                try:
                    self._force_terminate_workers(executor)
                except Exception:  # noqa: BLE001 -- 清理不阻塞主流程
                    pass
                if pool_owner is not None:
                    try:
                        pool_owner.mark_broken()  # 硬杀后共享池内部状态不可信
                    except Exception:  # noqa: BLE001
                        pass
            elif pool_owner is not None and sys.exc_info()[0] is not None:
                # 异常传播（非中断，如 bootstrap/protocol 失败）：保守标记
                # 共享池失效，下次任务重建（成本仅一次 spawn）。
                try:
                    pool_owner.mark_broken()
                except Exception:  # noqa: BLE001
                    pass
            if not reuse_executor:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:  # noqa: BLE001 -- 清理不阻塞主流程
                    pass

        return obj_patch, obs_bundles, serial_indices

    # ── 内部 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _default_executor_factory(max_workers: int, initializer, initargs: tuple):
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            initializer=initializer,
            initargs=initargs or (),
        )

    @staticmethod
    def _force_terminate_workers(executor) -> int:
        """强制终止所有存活 worker 进程（Ctrl+C 短路专用），返回终止数量。

        ``ProcessPoolExecutor.shutdown(cancel_futures=True)`` 只能取消未开始的
        future，正在运行的 chunk 会由 worker 继续跑完才退出。这里直接 terminate
        底层 ``multiprocessing.Process``（``executor._processes``，CPython 私有
        结构，3.8~3.13 稳定；缺失时静默跳过，兼容测试注入的假 executor）。

        terminate 是硬杀（Windows ``TerminateProcess`` / POSIX SIGTERM），
        不等待 chunk 结束；已死亡的进程跳过。调用方随后仍需 ``shutdown`` 让
        后台清理线程 join 这些进程。
        """
        processes = getattr(executor, "_processes", None)
        if not isinstance(processes, dict):
            return 0
        terminated = 0
        for proc in list(processes.values()):
            try:
                alive = bool(proc.is_alive())
            except Exception:  # noqa: BLE001 -- 进程对象可能已失效
                alive = True
            if not alive:
                continue
            try:
                proc.terminate()
                terminated += 1
            except Exception:  # noqa: BLE001 -- 单个失败不阻塞其余终止
                pass
        if terminated:
            logger.warning(
                "Ctrl+C: force-terminated %d parallel worker process(es)", terminated
            )
        return terminated

    def _handle_chunk_failure(
        self,
        idx: int,
        exc: Exception,
        manifest: ChunkManifest,
        serial_indices: List[int],
        retry_left: Dict[int, int],
        pending: List[int],
    ) -> None:
        """chunk 级失败：先原地重试（新提交），再失败则进串行补跑清单。"""
        if isinstance(exc, BrokenProcessPool):
            kind = "worker process crashed"
        elif isinstance(exc, (WorkerProcessError, PageProcessingError)):
            kind = "chunk processing failed"
        else:
            kind = f"{type(exc).__name__}"
        if retry_left.get(idx, 0) > 0:
            retry_left[idx] -= 1
            logger.warning(
                "Parallel chunk %d %s (%s); retrying (%d left)",
                idx, kind, str(exc)[:120], retry_left[idx],
            )
            pending.insert(0, idx)
            manifest.chunk_status[idx] = "pending"
            return
        manifest.mark_failed(idx)
        serial_indices.append(idx)
        logger.warning(
            "Parallel chunk %d %s permanently (%s); queued for serial fallback",
            idx, kind, str(exc)[:120],
        )

