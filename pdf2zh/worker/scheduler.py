"""ChunkScheduler — 动态 chunk 调度。

根据页面复杂度动态分配 chunk size。

简单页面 → 大 chunk (64 pages)
复杂页面 → 小 chunk (8 pages)
"""

from __future__ import annotations

from typing import List, Optional

from pdf2zh.worker.task import CacheContext, ChunkTask, PageTask


class ChunkScheduler:
    """动态 chunk 调度器。

    用法：
        scheduler = ChunkScheduler(
            default_chunk_size=32,
            min_chunk_size=4,
            max_chunk_size=64,
        )

        chunks = scheduler.schedule(pages, page_costs)
    """

    def __init__(
        self,
        default_chunk_size: int = 32,
        min_chunk_size: int = 4,
        max_chunk_size: int = 64,
        target_cost: float = 200.0,
    ) -> None:
        self.default_chunk_size = default_chunk_size
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size
        self.target_cost = target_cost

    def estimate_page_cost(self, page_complexity: str) -> float:
        """估算单页代价。"""
        costs = {
            "simple": 1.0,
            "medium": 5.0,
            "complex": 20.0,
        }
        return costs.get(page_complexity, 5.0)

    def compute_chunk_size(self, avg_page_cost: float) -> int:
        """根据平均页面代价计算 chunk size。"""
        if avg_page_cost <= 0:
            return self.default_chunk_size

        size = int(self.target_cost / avg_page_cost)
        return max(self.min_chunk_size, min(self.max_chunk_size, size))

    def schedule(
        self,
        pages: List[PageTask],
        page_costs: Optional[List[float]] = None,
        cache_context: Optional[CacheContext] = None,
    ) -> List[ChunkTask]:
        """将 pages 分配到 chunks。"""
        if not pages:
            return []

        if cache_context is None:
            cache_context = CacheContext()

        # 计算平均代价
        if page_costs:
            avg_cost = sum(page_costs) / len(page_costs)
        else:
            avg_cost = 5.0  # 默认中等代价

        chunk_size = self.compute_chunk_size(avg_cost)

        # 分配
        chunks = []
        for i in range(0, len(pages), chunk_size):
            chunk_pages = pages[i : i + chunk_size]
            chunk = ChunkTask(
                chunk_id=len(chunks),
                pages=chunk_pages,
                cache_context=cache_context,
            )
            chunks.append(chunk)

        return chunks

    def schedule_by_complexity(
        self,
        pages: List[PageTask],
        complexities: List[str],
        cache_context: Optional[CacheContext] = None,
    ) -> List[ChunkTask]:
        """按复杂度分组调度。"""
        if not pages:
            return []

        if cache_context is None:
            cache_context = CacheContext()

        # 按复杂度分组
        simple_pages = []
        complex_pages = []

        for page, complexity in zip(pages, complexities):
            if complexity == "simple":
                simple_pages.append(page)
            else:
                complex_pages.append(page)

        chunks = []

        # Simple pages: 大 chunk
        if simple_pages:
            simple_chunk_size = self.max_chunk_size
            for i in range(0, len(simple_pages), simple_chunk_size):
                chunk_pages = simple_pages[i : i + simple_chunk_size]
                chunks.append(
                    ChunkTask(
                        chunk_id=len(chunks),
                        pages=chunk_pages,
                        cache_context=cache_context,
                    )
                )

        # Complex pages: 小 chunk
        if complex_pages:
            complex_chunk_size = self.min_chunk_size
            for i in range(0, len(complex_pages), complex_chunk_size):
                chunk_pages = complex_pages[i : i + complex_chunk_size]
                chunks.append(
                    ChunkTask(
                        chunk_id=len(chunks),
                        pages=chunk_pages,
                        cache_context=cache_context,
                    )
                )

        return chunks

    def summary(self, chunks: List[ChunkTask]) -> str:
        sizes = [c.page_count for c in chunks]
        return (
            f"ChunkScheduler | chunks={len(chunks)} | "
            f"total_pages={sum(sizes)} | "
            f"avg_chunk_size={sum(sizes)/len(chunks):.1f} | "
            f"min={min(sizes)} | max={max(sizes)}"
        )
