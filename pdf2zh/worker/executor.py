"""WorkerExecutor — chunk 执行器。

管理 worker 进程，执行 chunk 任务。

用法：
    executor = WorkerExecutor(max_workers=4)

    chunks = scheduler.schedule(pages, complexities)
    results = executor.execute(chunks, worker_fn)
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from pdf2zh.worker.result import ChunkMetrics, ChunkResult, PageResult
from pdf2zh.worker.task import ChunkTask


class WorkerExecutor:
    """Worker 执行器。

    职责：
        1. 管理 worker 进程池
        2. 分发 chunk 任务
        3. 收集结果
        4. 统计指标

    用法：
        executor = WorkerExecutor(max_workers=4)

        def worker_fn(chunk: ChunkTask) -> ChunkResult:
            # 在 worker 进程中执行
            results = []
            for page in chunk.pages:
                result = process_page(page)
                results.append(result)
            return ChunkResult(chunk_id=chunk.chunk_id, pages=results)

        results = executor.execute(chunks, worker_fn)
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        chunk_timeout: float = 300.0,
    ) -> None:
        self.max_workers = max_workers
        self.chunk_timeout = chunk_timeout
        self._stats = {
            "total_chunks": 0,
            "total_pages": 0,
            "completed_chunks": 0,
            "failed_chunks": 0,
            "total_dispatch_ms": 0.0,
            "total_execution_ms": 0.0,
        }

    def execute(
        self,
        chunks: List[ChunkTask],
        worker_fn: Callable[[ChunkTask], ChunkResult],
    ) -> List[ChunkResult]:
        """执行所有 chunks。"""
        if not chunks:
            return []

        self._stats["total_chunks"] = len(chunks)
        self._stats["total_pages"] = sum(c.page_count for c in chunks)

        results = []
        t0 = time.perf_counter()

        with ProcessPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            futures = {}
            for chunk in chunks:
                t_dispatch = time.perf_counter()
                future = executor.submit(worker_fn, chunk)
                futures[future] = chunk
                self._stats["total_dispatch_ms"] += (
                    time.perf_counter() - t_dispatch
                ) * 1000

            # 收集结果
            for future in as_completed(futures, timeout=self.chunk_timeout):
                chunk = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    self._stats["completed_chunks"] += 1
                except Exception as e:
                    # 失败的 chunk
                    failed_result = ChunkResult(
                        chunk_id=chunk.chunk_id,
                        pages=[
                            PageResult(
                                page_index=p.page_index, success=False, error=str(e)
                            )
                            for p in chunk.pages
                        ],
                    )
                    results.append(failed_result)
                    self._stats["failed_chunks"] += 1

        self._stats["total_execution_ms"] = (time.perf_counter() - t0) * 1000

        # 按 chunk_id 排序
        results.sort(key=lambda r: r.chunk_id)

        return results

    def execute_sequential(
        self,
        chunks: List[ChunkTask],
        worker_fn: Callable[[ChunkTask], ChunkResult],
    ) -> List[ChunkResult]:
        """顺序执行（调试用）。"""
        results = []
        for chunk in chunks:
            try:
                result = worker_fn(chunk)
                results.append(result)
            except Exception as e:
                failed_result = ChunkResult(
                    chunk_id=chunk.chunk_id,
                    pages=[
                        PageResult(page_index=p.page_index, success=False, error=str(e))
                        for p in chunk.pages
                    ],
                )
                results.append(failed_result)
        return results

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    def summary(self) -> str:
        s = self._stats
        return (
            f"WorkerExecutor | chunks={s['total_chunks']} | "
            f"pages={s['total_pages']} | "
            f"completed={s['completed_chunks']} | "
            f"failed={s['failed_chunks']} | "
            f"dispatch={s['total_dispatch_ms']:.1f}ms | "
            f"execution={s['total_execution_ms']:.1f}ms"
        )
