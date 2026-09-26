"""ChunkResult — chunk 执行结果。

包含 PageResult 列表 + 执行指标。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChunkMetrics:
    """Chunk 执行指标。"""

    parse_ms: float = 0.0
    classify_ms: float = 0.0
    layout_ms: float = 0.0
    translation_ms: float = 0.0
    artifact_ms: float = 0.0
    total_ms: float = 0.0

    cache_hits: int = 0
    cache_misses: int = 0
    pages_processed: int = 0

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "parse_ms": self.parse_ms,
            "classify_ms": self.classify_ms,
            "layout_ms": self.layout_ms,
            "translation_ms": self.translation_ms,
            "artifact_ms": self.artifact_ms,
            "total_ms": self.total_ms,
            "cache_hit_rate": self.cache_hit_rate,
            "pages_processed": self.pages_processed,
        }


@dataclass
class PageResult:
    """单页结果。"""

    page_index: int = 0
    source_page_hash: str = ""
    success: bool = True
    error: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkResult:
    """Chunk 执行结果。"""

    chunk_id: int = 0
    pages: List[PageResult] = field(default_factory=list)
    metrics: ChunkMetrics = field(default_factory=ChunkMetrics)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def success_count(self) -> int:
        return sum(1 for p in self.pages if p.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for p in self.pages if not p.success)

    def summary(self) -> str:
        return (
            f"ChunkResult(chunk={self.chunk_id}) | "
            f"pages={self.page_count} | "
            f"success={self.success_count} | "
            f"failed={self.failed_count} | "
            f"cache_hit={self.metrics.cache_hit_rate:.1%} | "
            f"{self.metrics.total_ms:.1f}ms"
        )
