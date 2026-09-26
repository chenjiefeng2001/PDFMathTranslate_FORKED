"""MemoryController — 内存压力控制器。

防止大文档处理时 OOM。

用法：
    controller = MemoryController(max_rss_mb=4096)
    controller.check_pressure()
    if controller.is_pressure:
        controller.release_cache()
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


def get_rss_mb() -> float:
    """获取当前 RSS (MB)。"""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


@dataclass
class MemoryPolicy:
    """内存策略。"""

    max_rss_mb: float = 4096.0
    warning_threshold: float = 0.8  # 80%
    critical_threshold: float = 0.9  # 90%
    snapshot_cache_max: int = 100
    translation_cache_max: int = 10000


@dataclass
class MemoryState:
    """内存状态。"""

    current_rss_mb: float = 0.0
    max_rss_mb: float = 4096.0
    usage_ratio: float = 0.0
    is_warning: bool = False
    is_critical: bool = False
    pressure_level: str = "normal"  # normal, warning, critical

    def summary(self) -> str:
        return (
            f"Memory: {self.current_rss_mb:.0f}MB / {self.max_rss_mb:.0f}MB "
            f"({self.usage_ratio:.0%}) [{self.pressure_level}]"
        )


class MemoryController:
    """内存压力控制器。"""

    def __init__(self, policy: Optional[MemoryPolicy] = None) -> None:
        self._policy = policy or MemoryPolicy()
        self._release_callbacks: List[Callable] = []
        self._state = MemoryState(max_rss_mb=self._policy.max_rss_mb)

    def register_release_callback(self, callback: Callable) -> None:
        """注册释放回调。"""
        self._release_callbacks.append(callback)

    def check_pressure(self) -> MemoryState:
        """检查内存压力。"""
        rss = get_rss_mb()
        self._state.current_rss_mb = rss
        self._state.usage_ratio = (
            rss / self._policy.max_rss_mb if self._policy.max_rss_mb > 0 else 0
        )

        self._state.is_warning = (
            self._state.usage_ratio >= self._policy.warning_threshold
        )
        self._state.is_critical = (
            self._state.usage_ratio >= self._policy.critical_threshold
        )

        if self._state.is_critical:
            self._state.pressure_level = "critical"
        elif self._state.is_warning:
            self._state.pressure_level = "warning"
        else:
            self._state.pressure_level = "normal"

        return self._state

    def release_cache(self) -> int:
        """释放缓存。"""
        released = 0
        for callback in self._release_callbacks:
            try:
                callback()
                released += 1
            except Exception:
                pass
        return released

    def should_throttle(self) -> bool:
        """是否应该节流。"""
        state = self.check_pressure()
        return state.is_warning

    def get_adaptive_batch_size(self, base_batch_size: int) -> int:
        """获取自适应批大小。"""
        state = self.check_pressure()
        if state.is_critical:
            return max(1, base_batch_size // 4)
        elif state.is_warning:
            return max(1, base_batch_size // 2)
        return base_batch_size

    @property
    def state(self) -> MemoryState:
        return self._state

    def summary(self) -> str:
        self.check_pressure()
        return self._state.summary()
