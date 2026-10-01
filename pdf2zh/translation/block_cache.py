"""BlockCacheKey — block 级翻译缓存 key。

PDF 翻译真正重复的是 block 级别：
    - header/footer 高度重复
    - 章节标题重复
    - 参考文献格式重复
    - 作者信息重复

用法：
    cache = BlockCache()

    for block in blocks:
        key = BlockCacheKey.from_block(block, "en", "zh")
        cached = cache.get(key)
        if cached:
            translated = cached
        else:
            translated = translate(block.text)
            cache.set(key, translated)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class BlockCacheKey:
    """Block 级翻译缓存 key。

    content_hash: block 内容 hash
    semantic_type: 语义类型 (text, title, header, footer, etc.)
    source_language: 源语言
    target_language: 目标语言
    translator_version: 翻译器版本
    layout_policy_version: 布局策略版本
    """

    content_hash: str = ""
    semantic_type: str = "text"
    source_language: str = "en"
    target_language: str = "zh"
    translator_version: str = "v1"
    layout_policy_version: str = "v1"

    @classmethod
    def from_text(
        cls,
        text: str,
        semantic_type: str = "text",
        source_language: str = "en",
        target_language: str = "zh",
        translator_version: str = "v1",
        layout_policy_version: str = "v1",
    ) -> BlockCacheKey:
        """从文本创建 key。"""
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        return cls(
            content_hash=content_hash,
            semantic_type=semantic_type,
            source_language=source_language,
            target_language=target_language,
            translator_version=translator_version,
            layout_policy_version=layout_policy_version,
        )

    @classmethod
    def from_block(cls, block, *args, **kwargs) -> BlockCacheKey:
        """从 LayoutBlock 创建 key。"""
        text = getattr(block, "text", "") or ""
        block_type = getattr(block, "block_type", "text")
        return cls.from_text(text, semantic_type=block_type, *args, **kwargs)

    def __str__(self) -> str:
        return (
            f"{self.content_hash[:8]}|{self.semantic_type}|"
            f"{self.source_language}->{self.target_language}|"
            f"{self.translator_version}|{self.layout_policy_version}"
        )


@dataclass
class BlockCacheEntry:
    """缓存条目。"""

    key: BlockCacheKey
    translated_text: str
    hit_count: int = 0
    source_pages: list[int] = None

    def __post_init__(self):
        if self.source_pages is None:
            self.source_pages = []


class BlockCache:
    """Block 级翻译缓存。

    用法：
        cache = BlockCache()

        # 查询
        key = BlockCacheKey.from_text("Abstract")
        entry = cache.get(key)
        if entry:
            return entry.translated_text

        # 设置
        translated = translate_api("Abstract")
        cache.set(key, translated, page_index=0)

        # 统计
        cache.summary()
    """

    def __init__(self, max_size: int = 10000) -> None:
        self._cache: dict[BlockCacheKey, BlockCacheEntry] = {}
        self._max_size = max(1, int(max_size))
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
        }

    def get(self, key: BlockCacheKey) -> Optional[BlockCacheEntry]:
        """查询缓存。"""
        entry = self._cache.get(key)
        if entry:
            entry.hit_count += 1
            self._stats["hits"] += 1
            return entry
        self._stats["misses"] += 1
        return None

    def set(
        self,
        key: BlockCacheKey,
        translated_text: str,
        page_index: int = 0,
    ) -> None:
        """设置缓存。"""
        if key in self._cache:
            entry = self._cache[key]
            entry.translated_text = translated_text
            if page_index not in entry.source_pages:
                entry.source_pages.append(page_index)
            return

        # LRU 简单实现：满了就清一半
        if len(self._cache) >= self._max_size:
            self._evict()

        entry = BlockCacheEntry(
            key=key,
            translated_text=translated_text,
            source_pages=[page_index],
        )
        self._cache[key] = entry

    def _evict(self) -> None:
        """简单 LRU 淘汰。"""
        if not self._cache:
            return

        # 按 hit_count 排序，淘汰最少的
        sorted_entries = sorted(
            self._cache.values(),
            key=lambda e: e.hit_count,
        )
        to_remove = len(self._cache) // 2
        for entry in sorted_entries[:to_remove]:
            del self._cache[entry.key]
            self._stats["evictions"] += 1

    @property
    def size(self) -> int:
        return len(self._cache)

    @property
    def hit_rate(self) -> float:
        total = self._stats["hits"] + self._stats["misses"]
        return self._stats["hits"] / total if total > 0 else 0.0

    def summary(self) -> str:
        return (
            f"BlockCache | size={self.size} | "
            f"hit_rate={self.hit_rate:.1%} | "
            f"hits={self._stats['hits']} | "
            f"misses={self._stats['misses']} | "
            f"evictions={self._stats['evictions']}"
        )

    def stats_detail(self) -> dict:
        return {
            "size": self.size,
            "max_size": self._max_size,
            "hit_rate": self.hit_rate,
            **self._stats,
        }
