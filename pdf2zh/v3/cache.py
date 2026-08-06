"""Module: Cache — Phase 6.4 分层缓存架构。

    DocumentCache
     ├── parse        （页 → 结构哈希，避免 pdfminer 重跑）
     ├── semantic     （页哈希 → 标注结果）
     ├── translation  （文本 → 译文，跨文档复用）
     ├── layout       （页 + 变更 → 排版计划）
     └── render       （页 → 渲染产物）

每层独立容量上限（LRU 语义：超限丢最旧）；``invalidate_page`` 级联
失效同页下游层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

_LAYERS = ("parse", "semantic", "translation", "layout", "render")


class LayerCache:
    """单层缓存（OrderedDict LRU 语义）。"""

    def __init__(self, name: str, capacity: int = 512) -> None:
        self.name = name
        self.capacity = max(1, capacity)
        self._data: "OrderedDict[str, Any]" = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def invalidate(self, prefix: str = "") -> int:
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            self._data.pop(k, None)
        return len(keys)

    def clear(self) -> None:
        self._data.clear()

    @property
    def size(self) -> int:
        return len(self._data)


@dataclass
class CacheStats:
    layers: Dict[str, int] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def to_dict(self) -> dict:
        return {"layers": dict(self.layers), "hits": self.hits,
                "misses": self.misses}

    def summary(self) -> str:
        return (f"Cache {self.hits} hits / {self.misses} misses :: "
                + " ".join(f"{k}={v}" for k, v in self.layers.items()))


class DocumentCache:
    """五层缓存：parse/semantic/translation/layout/render。"""

    def __init__(self, capacities: Optional[Dict[str, int]] = None) -> None:
        caps = capacities or {}
        self.layers: Dict[str, LayerCache] = {
            name: LayerCache(name, int(caps.get(name, 512)))
            for name in _LAYERS
        }
        self.hits = 0
        self.misses = 0

    def get(self, layer: str, key: str) -> Optional[Any]:
        if layer not in self.layers:
            return None
        value = self.layers[layer].get(key)
        if value is not None:
            self.hits += 1
        else:
            self.misses += 1
        return value

    def set(self, layer: str, key: str, value: Any) -> None:
        if layer in self.layers:
            self.layers[layer].set(key, value)

    def translate(self, text: str, fn: Callable[[str], str]) -> str:
        """翻译缓存：同文本直接复用（跨文档共享）。"""
        key = f"t:{text}"
        cached = self.get("translation", key)
        if cached is not None:
            return cached
        result = fn(text)
        self.set("translation", key, result)
        return result

    def invalidate_page(self, pno: int) -> dict:
        """页面变更 → 级联失效该页 parse/semantic/layout/render。"""
        cleared = {}
        for layer in ("parse", "semantic", "layout", "render"):
            cleared[layer] = self.layers[layer].invalidate(f"p{pno}")
        return cleared

    def stats(self) -> CacheStats:
        return CacheStats(
            layers={name: layer.size for name, layer in self.layers.items()},
            hits=self.hits, misses=self.misses)


__all__ = ["LayerCache", "CacheStats", "DocumentCache", "_LAYERS"]