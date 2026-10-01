"""TranslationMetrics — 翻译效率指标。

Phase 7 新指标：追踪翻译优化效果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class TranslationMetrics:
    """翻译效率指标。"""

    # Block 统计
    blocks_total: int = 0
    blocks_merged: int = 0
    blocks_skipped: int = 0

    # 批处理
    batch_count: int = 0
    avg_batch_tokens: float = 0.0
    avg_batch_blocks: float = 0.0

    # 缓存
    cache_hits: int = 0
    cache_misses: int = 0

    # 术语
    glossary_hits: int = 0
    memory_hits: int = 0

    # API
    api_calls: int = 0
    api_latency_ms: float = 0.0

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

    @property
    def dedup_ratio(self) -> float:
        return 1.0 - (self.blocks_merged / max(self.blocks_total, 1))

    @property
    def api_calls_saved(self) -> int:
        return self.blocks_total - self.api_calls

    @property
    def savings_ratio(self) -> float:
        return self.api_calls_saved / max(self.blocks_total, 1)

    def summary(self) -> str:
        return (
            f"TranslationMetrics | "
            f"blocks={self.blocks_total} | "
            f"unique={self.blocks_merged} | "
            f"batches={self.batch_count} | "
            f"cache_hit={self.cache_hit_ratio:.1%} | "
            f"glossary={self.glossary_hits} | "
            f"api_calls={self.api_calls} | "
            f"saved={self.api_calls_saved} ({self.savings_ratio:.1%})"
        )

    def to_dict(self) -> Dict:
        return {
            "blocks_total": self.blocks_total,
            "blocks_merged": self.blocks_merged,
            "batch_count": self.batch_count,
            "avg_batch_tokens": self.avg_batch_tokens,
            "cache_hit_ratio": self.cache_hit_ratio,
            "glossary_hits": self.glossary_hits,
            "api_calls": self.api_calls,
            "api_calls_saved": self.api_calls_saved,
            "savings_ratio": self.savings_ratio,
        }
