"""Module: V5 Runtime Context — extracted runtime context from RuntimeKernel.

Encapsulates all "context" capabilities that were previously part of RuntimeKernel,
following the MicroKernel pattern. Kernel focuses on lifecycle/scheduling/transaction;
RuntimeContext holds knowledge, memory, telemetry, plugins, config, and cache.

Usage::
    from pdf2zh.v3.runtime_context import RuntimeContext, RuntimeConfig

    ctx = RuntimeContext()
    ctx.knowledge.learn("PDF", "Portable Document Format")
    ctx.memory.put("key", "value")
    ctx.telemetry.record("op", 42.0)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Callable

from pdf2zh.v3.runtime_kernel import (
    KnowledgeCenter,
    MemoryCenter,
    TelemetryCollector,
    PluginManager,
    CapabilityPlugin,
    Capability,
)
from pdf2zh.v3.service import ServiceRegistry

logger = logging.getLogger(__name__)


@dataclass
class RuntimeConfig:
    """Shared configuration for the V5 Runtime."""

    max_memory_entries: int = 10000
    max_cache_entries: int = 5000
    telemetry_enabled: bool = True
    tracing_enabled: bool = True
    auto_recovery: bool = True
    max_retries: int = 3
    log_level: str = "INFO"
    metadata: dict = field(default_factory=dict)


class LRUCache:
    """Simple LRU cache with max size."""

    def __init__(self, max_size: int = 5000) -> None:
        self._data: Dict[str, Any] = {}
        self._order: List[str] = []
        self._max = max_size
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        if key in self._data:
            self._hits += 1
            self._order.remove(key)
            self._order.append(key)
            return self._data[key]
        self._misses += 1
        return None

    def put(self, key: str, value: Any) -> None:
        if key in self._data:
            self._order.remove(key)
        elif len(self._data) >= self._max:
            oldest = self._order.pop(0)
            del self._data[oldest]
        self._data[key] = value
        self._order.append(key)

    def remove(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self._order.remove(key)

    def clear(self) -> None:
        self._data.clear()
        self._order.clear()
        self._hits = 0
        self._misses = 0

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    def stats(self) -> dict:
        return {
            "size": self.size,
            "max": self._max,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 4),
        }


class RuntimeContext:
    """Bound context holding all runtime capabilities.

    The RuntimeKernel owns one RuntimeContext instance. All non-kernel
    capabilities (knowledge, memory, telemetry, plugins, config, cache)
    live here, keeping the Kernel lean.
    """

    def __init__(
        self,
        config: Optional[RuntimeConfig] = None,
        knowledge: Optional[KnowledgeCenter] = None,
        memory: Optional[MemoryCenter] = None,
        telemetry: Optional[TelemetryCollector] = None,
        plugins: Optional[PluginManager] = None,
        cache: Optional[LRUCache] = None,
        service_registry: Optional[ServiceRegistry] = None,
    ) -> None:
        self.config: RuntimeConfig = config or RuntimeConfig()
        self.knowledge: KnowledgeCenter = knowledge or KnowledgeCenter()
        self.memory: MemoryCenter = memory or MemoryCenter()
        self.telemetry: TelemetryCollector = telemetry or TelemetryCollector()
        self.plugins: PluginManager = plugins or PluginManager()
        self.cache: LRUCache = cache or LRUCache(max_size=self.config.max_cache_entries)
        self.service_registry: ServiceRegistry = service_registry or ServiceRegistry()
        self._context_id: str = uuid.uuid4().hex[:12]
        self._created: float = time.time()
        self._labels: Dict[str, str] = {}

    @property
    def context_id(self) -> str:
        return self._context_id

    def set_label(self, key: str, value: str) -> None:
        self._labels[key] = value

    def get_label(self, key: str) -> Optional[str]:
        return self._labels.get(key)

    def find_plugins(self, capability: Capability) -> List[CapabilityPlugin]:
        return self.plugins.find_by_capability(capability)

    def stats(self) -> dict:
        return {
            "context_id": self._context_id,
            "age_seconds": round(time.time() - self._created, 2),
            "knowledge": {"entries": self.knowledge.entry_count()},
            "memory": self.memory.stats(),
            "telemetry": (
                self.telemetry.summary()
                if self.config.telemetry_enabled
                else {"disabled": True}
            ),
            "plugins": {
                "total": self.plugins.plugin_count,
                "running": self.plugins.running_count,
            },
            "cache": self.cache.stats(),
            "labels": dict(self._labels),
        }

    def clear_state(self) -> None:
        """Reset all mutable state (for testing/cleanup)."""
        self.knowledge.clear()
        self.memory.clear_all()
        self.cache.clear()
        self._labels.clear()
        logger.debug("RuntimeContext state cleared [%s]", self._context_id)


__all__ = [
    "RuntimeConfig",
    "LRUCache",
    "RuntimeContext",
]
