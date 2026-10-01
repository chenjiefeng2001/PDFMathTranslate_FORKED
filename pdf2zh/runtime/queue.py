"""PipelineQueue — 流式队列 + backpressure。

Producer-consumer DAG 的核心组件。
支持 backpressure 防止内存爆炸。

用法：
    queue = PipelineQueue(max_size=64)

    # Producer
    queue.put(item)

    # Consumer
    item = queue.get()

    # Backpressure
    if queue.should_pause_producer:
        wait()
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")


class PipelineQueue(Generic[T]):
    """带 backpressure 的队列。

    特性：
        - max_size: 队列最大容量
        - high_watermark: 暂停 producer 阈值
        - low_watermark: 恢复 producer 阈值
        - timeout: get 超时
    """

    def __init__(
        self,
        max_size: int = 64,
        high_watermark_ratio: float = 0.8,
        low_watermark_ratio: float = 0.2,
        timeout: float = 1.0,
    ) -> None:
        self._max_size = max_size
        self._high_watermark = int(max_size * high_watermark_ratio)
        self._low_watermark = int(max_size * low_watermark_ratio)
        self._timeout = timeout

        self._queue: deque[T] = deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Event()
        self._not_full = threading.Event()
        self._not_full.set()

        self._stats = {
            "puts": 0,
            "gets": 0,
            "pauses": 0,
            "wait_time_ms": 0.0,
        }

    def put(self, item: T, timeout: Optional[float] = None) -> bool:
        """放入项目。如果队列满，等待。"""
        if timeout is None:
            timeout = self._timeout

        t0 = time.perf_counter()
        deadline = t0 + timeout

        while True:
            with self._lock:
                if len(self._queue) < self._max_size:
                    self._queue.append(item)
                    self._stats["puts"] += 1
                    self._not_empty.set()

                    # 检查是否需要通知 consumer
                    if len(self._queue) >= self._low_watermark:
                        self._not_full.set()

                    return True

            # 队列满，等待
            self._not_full.wait(timeout=min(0.1, deadline - time.perf_counter()))

            if time.perf_counter() > deadline:
                return False

    def get(self, timeout: Optional[float] = None) -> Optional[T]:
        """获取项目。如果队列空，等待。"""
        if timeout is None:
            timeout = self._timeout

        t0 = time.perf_counter()
        deadline = t0 + timeout

        while True:
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
                    self._stats["gets"] += 1
                    self._not_full.set()

                    if len(self._queue) <= self._high_watermark:
                        self._not_empty.set()

                    return item

            # 队列空，等待
            self._not_empty.wait(timeout=min(0.1, deadline - time.perf_counter()))

            if time.perf_counter() > deadline:
                return None

    def get_nowait(self) -> Optional[T]:
        """非阻塞获取。"""
        with self._lock:
            if self._queue:
                return self._queue.popleft()
            return None

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def is_empty(self) -> bool:
        return self.size == 0

    @property
    def is_full(self) -> bool:
        return self.size >= self._max_size

    @property
    def should_pause_producer(self) -> bool:
        """是否应该暂停 producer（backpressure）。"""
        return self.size >= self._high_watermark

    @property
    def should_resume_producer(self) -> bool:
        """是否应该恢复 producer。"""
        return self.size <= self._low_watermark

    @property
    def fill_ratio(self) -> float:
        return self.size / self._max_size if self._max_size > 0 else 0

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    def summary(self) -> str:
        return (
            f"PipelineQueue | size={self.size}/{self._max_size} | "
            f"fill={self.fill_ratio:.0%} | "
            f"puts={self._stats['puts']} | gets={self._stats['gets']}"
        )
