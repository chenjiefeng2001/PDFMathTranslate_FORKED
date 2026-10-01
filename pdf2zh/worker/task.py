"""ChunkTask — pipeline execution unit。

不再以 page 为单位调度，而是以 chunk 为单位。
chunk 是 snapshot + layout + translation + artifact 的完整执行单元。

用法：
    chunk = ChunkTask(
        chunk_id=0,
        pages=[PageTask(0), PageTask(1), ...],
        cache_context=CacheContext(translator_version="v1"),
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CacheContext:
    """缓存上下文 — worker 共享。"""

    translator_version: str = "v1"
    font_version: str = "v1"
    layout_version: str = "v1"
    source_language: str = "en"
    target_language: str = "zh"


@dataclass
class PageTask:
    """单页任务（轻量级）。"""

    page_index: int = 0
    source_page_hash: str = ""
    priority: int = 0


@dataclass
class ChunkTask:
    """Chunk 任务 — pipeline execution unit。

    包含：
        - chunk_id: 唯一标识
        - pages: 页面列表
        - cache_context: 共享缓存上下文
    """

    chunk_id: int = 0
    pages: List[PageTask] = field(default_factory=list)
    cache_context: CacheContext = field(default_factory=CacheContext)

    def __post_init__(self):
        if not self.pages:
            raise ValueError("ChunkTask must contain at least one PageTask")

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def page_indices(self) -> List[int]:
        return [p.page_index for p in self.pages]

    def validate(self) -> bool:
        return len(self.pages) > 0
