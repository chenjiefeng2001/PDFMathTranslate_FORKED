"""RuntimeBenchmarkResult — 统一 benchmark 结果。

5 个关键指标：
    1. First Page Latency
    2. Steady Throughput
    3. Memory Bound
    4. Translation Efficiency
    5. Backend Efficiency
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PhaseResult:
    """单阶段结果。"""

    time_ms: float = 0.0
    pages_sec: float = 0.0
    items_processed: int = 0

    def summary(self) -> str:
        return f"time={self.time_ms:.0f}ms, pages/sec={self.pages_sec:.1f}"


@dataclass
class TranslationResult:
    """翻译效率结果。"""

    total_blocks: int = 0
    unique_blocks: int = 0
    cache_hits: int = 0
    repeated_regions: int = 0
    api_requests: int = 0
    api_latency_ms: float = 0.0

    @property
    def cache_hit_ratio(self) -> float:
        if self.total_blocks == 0:
            return 0.0
        return self.cache_hits / self.total_blocks

    @property
    def dedup_ratio(self) -> float:
        if self.total_blocks == 0:
            return 0.0
        return 1.0 - (self.unique_blocks / self.total_blocks)

    def summary(self) -> str:
        return (
            f"blocks={self.total_blocks}, unique={self.unique_blocks}, "
            f"cache_hit={self.cache_hit_ratio:.1%}, "
            f"dedup={self.dedup_ratio:.1%}, "
            f"api_requests={self.api_requests}"
        )


@dataclass
class MemoryResult:
    """内存结果。"""

    peak_rss_mb: float = 0.0
    final_rss_mb: float = 0.0
    rss_growth_mb: float = 0.0

    def summary(self) -> str:
        return f"peak={self.peak_rss_mb:.1f}MB, growth={self.rss_growth_mb:.1f}MB"


@dataclass
class BackendResult:
    """Backend 效率结果。"""

    backend_name: str = ""
    objects_before: int = 0
    objects_after: int = 0
    reused_objects: int = 0
    resource_growth: float = 1.0
    write_time_ms: float = 0.0
    valid: bool = False

    @property
    def reuse_ratio(self) -> float:
        if self.objects_before == 0:
            return 0.0
        return self.reused_objects / self.objects_before

    def summary(self) -> str:
        return (
            f"backend={self.backend_name}, "
            f"objects={self.objects_before}->{self.objects_after}, "
            f"reuse={self.reuse_ratio:.1%}, "
            f"growth={self.resource_growth:.2f}x"
        )


@dataclass
class RuntimeBenchmarkResult:
    """统一 benchmark 结果。

    5 个关键指标：
        1. First Page Latency
        2. Steady Throughput
        3. Memory Bound
        4. Translation Efficiency
        5. Backend Efficiency
    """

    # Metadata
    dataset_name: str = ""
    pages: int = 0
    runtime: str = ""  # batch or stream
    backend: str = ""  # pikepdf or mupdf

    # 5 Key Metrics
    first_page_latency_ms: float = 0.0
    steady_throughput_pages_sec: float = 0.0
    total_time_ms: float = 0.0

    # Phase breakdown
    parse: PhaseResult = field(default_factory=PhaseResult)
    layout: PhaseResult = field(default_factory=PhaseResult)
    translation: TranslationResult = field(default_factory=TranslationResult)
    assembly: BackendResult = field(default_factory=BackendResult)
    memory: MemoryResult = field(default_factory=MemoryResult)

    # Validation
    semantic_equal: bool = True
    valid: bool = True

    def summary(self) -> str:
        lines = [
            "Runtime Benchmark Result",
            "=" * 60,
            f"Dataset: {self.dataset_name} ({self.pages} pages)",
            f"Runtime: {self.runtime} | Backend: {self.backend}",
            "",
            "--- 5 Key Metrics ---",
            f"1. First Page Latency:  {self.first_page_latency_ms:.0f} ms",
            f"2. Steady Throughput:    {self.steady_throughput_pages_sec:.1f} pages/s",
            f"3. Memory Peak:         {self.memory.peak_rss_mb:.1f} MB",
            f"4. Translation:         {self.translation.summary()}",
            f"5. Backend:             {self.assembly.summary()}",
            "",
            "--- Phase Breakdown ---",
            f"Parse:    {self.parse.summary()}",
            f"Layout:   {self.layout.summary()}",
            f"Assembly: {self.assembly.write_time_ms:.0f}ms",
            "",
            f"Total: {self.total_time_ms:.0f}ms",
            f"Valid: {self.valid} | Semantic Equal: {self.semantic_equal}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "dataset": self.dataset_name,
            "pages": self.pages,
            "runtime": self.runtime,
            "backend": self.backend,
            "metrics": {
                "first_page_latency_ms": self.first_page_latency_ms,
                "steady_throughput_pages_sec": self.steady_throughput_pages_sec,
                "total_time_ms": self.total_time_ms,
                "memory_peak_mb": self.memory.peak_rss_mb,
            },
            "phase": {
                "parse_ms": self.parse.time_ms,
                "layout_ms": self.layout.time_ms,
                "assembly_ms": self.assembly.write_time_ms,
            },
            "translation": {
                "total_blocks": self.translation.total_blocks,
                "unique_blocks": self.translation.unique_blocks,
                "cache_hit_ratio": self.translation.cache_hit_ratio,
                "api_requests": self.translation.api_requests,
            },
            "backend": {
                "name": self.assembly.backend_name,
                "objects_before": self.assembly.objects_before,
                "objects_after": self.assembly.objects_after,
                "reuse_ratio": self.assembly.reuse_ratio,
            },
            "valid": self.valid,
            "semantic_equal": self.semantic_equal,
        }
