"""LayoutBatchScheduler — batch 调度器。

管理 simple/complex 分流 + batch 聚合。

用法：
    scheduler = LayoutBatchScheduler(batch_size=8)

    for snapshot in snapshots:
        result = scheduler.submit(snapshot)
        # result is LayoutResult (from fast_path or model)

    # flush remaining
    remaining = scheduler.flush()
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Dict, List, Optional

from pdf2zh.layout.cache_key import LayoutCacheKey
from pdf2zh.layout.request import LayoutRequest
from pdf2zh.layout.result import LayoutBlock, LayoutResult


class LayoutBatchScheduler:
    """Layout batch 调度器。

    职责：
        1. 分流 simple/complex
        2. 聚合 complex pages 为 batch
        3. 调用 model runner
        4. 缓存结果

    用法：
        scheduler = LayoutBatchScheduler(
            batch_size=8,
            fast_path_fn=rule_based_layout,
            model_fn=yolo_predict,
        )

        results = {}
        for snap in snapshots:
            results[snap.page_index] = scheduler.submit(snap)

        # flush remaining
        for idx, result in scheduler.flush():
            results[idx] = result
    """

    def __init__(
        self,
        batch_size: int = 8,
        fast_path_fn: Optional[Callable] = None,
        model_fn: Optional[Callable] = None,
        cache: Optional[Dict[LayoutCacheKey, LayoutResult]] = None,
    ) -> None:
        self.batch_size = batch_size
        self.fast_path_fn = fast_path_fn
        self.model_fn = model_fn
        self._queue: deque[LayoutRequest] = deque()
        self._cache = cache or {}
        self._stats = {
            "total": 0,
            "fast_path": 0,
            "model": 0,
            "cache_hit": 0,
            "batches": 0,
        }

    def submit(self, request: LayoutRequest, is_simple: bool = True) -> LayoutResult:
        """提交 layout 请求。

        simple 页面 → fast path
        complex 页面 → 入队等 batch
        """
        self._stats["total"] += 1

        # 检查缓存
        cache_key = LayoutCacheKey(
            source_page_hash=request.source_page_hash,
            layout_model_version="v1",
            width=float(request.width or 0.0),
            height=float(request.height or 0.0),
        )
        if cache_key in self._cache:
            self._stats["cache_hit"] += 1
            cached = self._cache[cache_key]
            cached.source = "cache"
            return cached

        # Simple → fast path
        if is_simple:
            result = self._fast_path(request)
            self._stats["fast_path"] += 1
            self._cache[cache_key] = result
            return result

        # Complex → 入队
        self._queue.append(request)
        self._stats["model"] += 1

        # 如果队列满了，flush
        if len(self._queue) >= self.batch_size:
            return self._flush_one_batch()

        # 返回空结果，稍后 flush
        return LayoutResult(
            page_index=request.page_index,
            source="pending",
        )

    def flush(self) -> List[tuple[int, LayoutResult]]:
        """flush 所有剩余队列。"""
        results = []
        while self._queue:
            result = self._flush_one_batch()
            results.append((result.page_index, result))
        return results

    def _fast_path(self, request: LayoutRequest) -> LayoutResult:
        """快速路径（规则）。"""
        t0 = time.perf_counter()

        if self.fast_path_fn:
            blocks = self.fast_path_fn(request)
        else:
            # 默认：单文本块
            blocks = [
                LayoutBlock(
                    block_type="text",
                    bbox=(0, 0, request.width, request.height),
                    confidence=0.9,
                )
            ]

        inference_ms = (time.perf_counter() - t0) * 1000

        return LayoutResult(
            page_index=request.page_index,
            blocks=blocks,
            model_version="rule_v1",
            inference_ms=inference_ms,
            confidence=0.9,
            source="fast_path",
        )

    def _flush_one_batch(self) -> LayoutResult:
        """flush 一个 batch。"""
        batch = []
        while self._queue and len(batch) < self.batch_size:
            batch.append(self._queue.popleft())

        if not batch:
            return LayoutResult()

        self._stats["batches"] += 1

        # 调用模型
        t0 = time.perf_counter()
        if self.model_fn:
            raw_results = self.model_fn(batch)
        else:
            raw_results = [LayoutResult(page_index=req.page_index) for req in batch]
        inference_ms = (time.perf_counter() - t0) * 1000

        # 返回第一个结果
        result = raw_results[0] if raw_results else LayoutResult()
        result.inference_ms = inference_ms
        result.source = "model"

        return result

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    def summary(self) -> str:
        s = self._stats
        total = s["total"] or 1
        return (
            f"LayoutBatchScheduler | total={s['total']} | "
            f"fast={s['fast_path']} ({s['fast_path']/total:.0%}) | "
            f"model={s['model']} ({s['model']/total:.0%}) | "
            f"cache={s['cache_hit']} ({s['cache_hit']/total:.0%}) | "
            f"batches={s['batches']}"
        )
