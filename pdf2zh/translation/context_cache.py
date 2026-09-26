"""ContextCache — 上下文感知缓存。

考虑 block_type、policy_version、model_version 的缓存。

用法：
    cache = ContextCache()
    cache.set(text, block_type="TOC", translation="介绍")
    result = cache.get(text, block_type="TOC")
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class CacheKey:
    """缓存 key。"""

    text_hash: str = ""
    block_type: str = ""
    glossary_version: int = 0
    policy_version: int = 0
    model_version: int = 0

    @property
    def key(self) -> str:
        """生成唯一 key。"""
        parts = [
            self.text_hash,
            self.block_type,
            str(self.glossary_version),
            str(self.policy_version),
            str(self.model_version),
        ]
        return "|".join(parts)

    @classmethod
    def from_text(
        cls,
        text: str,
        block_type: str = "paragraph",
        glossary_version: int = 0,
        policy_version: int = 0,
        model_version: int = 0,
    ) -> CacheKey:
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        return cls(
            text_hash=text_hash,
            block_type=block_type,
            glossary_version=glossary_version,
            policy_version=policy_version,
            model_version=model_version,
        )


@dataclass
class CacheEntry:
    """缓存条目。"""

    translation: str = ""
    score: float = 0.0
    timestamp: float = 0.0
    hits: int = 0


class ContextCache:
    """上下文感知缓存。"""

    def __init__(self, max_size: int = 10000, ttl: int = 3600) -> None:
        self._cache: Dict[str, CacheEntry] = {}
        self._max_size = max(1, int(max_size))
        self._ttl = max(1, int(ttl))
        self._hits = 0
        self._misses = 0

    def get(
        self,
        text: str,
        block_type: str = "paragraph",
        glossary_version: int = 0,
        policy_version: int = 0,
        model_version: int = 0,
    ) -> Optional[str]:
        """获取缓存。"""
        key = CacheKey.from_text(
            text, block_type, glossary_version, policy_version, model_version
        )
        entry = self._cache.get(key.key)
        if entry and time.time() - entry.timestamp < self._ttl:
            entry.hits += 1
            self._hits += 1
            return entry.translation
        if entry is not None:
            self._cache.pop(key.key, None)
        self._misses += 1
        return None

    def set(
        self,
        text: str,
        translation: str,
        block_type: str = "paragraph",
        glossary_version: int = 0,
        policy_version: int = 0,
        model_version: int = 0,
        score: float = 1.0,
    ) -> None:
        """设置缓存。"""
        key = CacheKey.from_text(
            text, block_type, glossary_version, policy_version, model_version
        )
        self._cache[key.key] = CacheEntry(
            translation=translation,
            score=score,
            timestamp=time.time(),
        )
        # LRU 淘汰
        if len(self._cache) > self._max_size:
            # 移除最早且 hits 最少的
            oldest_key = min(
                self._cache.keys(),
                key=lambda k: (self._cache[k].timestamp, -self._cache[k].hits),
            )
            del self._cache[oldest_key]

    def invalidate_glossary(self) -> None:
        """使术语表相关缓存失效。"""
        # 简化：清空所有缓存
        # 实际应该只清空 glossary_version 相关的
        self._cache.clear()

    @property
    def hit_ratio(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def size(self) -> int:
        return len(self._cache)

    def summary(self) -> str:
        return f"ContextCache: {self.size} entries, " f"hit_ratio={self.hit_ratio:.1%}"
