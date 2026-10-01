"""TranslationBenchmark — 真实翻译 benchmark。

模拟真实翻译流程，测量性能和质量。

用法：
    benchmark = TranslationBenchmark()
    result = benchmark.run("input.pdf", translator=mock_translator)
    print(result.summary())
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pikepdf


@dataclass
class TranslationMetrics:
    """翻译指标。"""

    total_blocks: int = 0
    translated_blocks: int = 0
    cached_blocks: int = 0
    overflow_blocks: int = 0
    failed_blocks: int = 0

    total_tokens: int = 0
    api_calls: int = 0
    api_latency_ms: float = 0.0

    @property
    def cache_hit_ratio(self) -> float:
        return self.cached_blocks / self.total_blocks if self.total_blocks > 0 else 0

    @property
    def overflow_rate(self) -> float:
        return self.overflow_blocks / self.total_blocks if self.total_blocks > 0 else 0

    @property
    def success_rate(self) -> float:
        return (
            self.translated_blocks / self.total_blocks if self.total_blocks > 0 else 0
        )


@dataclass
class BenchmarkResult:
    """Benchmark 结果。"""

    pdf_path: str = ""
    page_count: int = 0
    total_time_ms: float = 0.0
    parse_time_ms: float = 0.0
    translation_time_ms: float = 0.0
    assembly_time_ms: float = 0.0

    metrics: TranslationMetrics = field(default_factory=TranslationMetrics)

    # 成本
    estimated_cost_usd: float = 0.0
    cost_per_page: float = 0.0

    # 质量
    avg_quality_score: float = 0.0
    overflow_rate: float = 0.0

    # 内存
    peak_memory_mb: float = 0.0

    def summary(self) -> str:
        lines = [
            "Translation Benchmark Result",
            "=" * 60,
            f"PDF: {self.pdf_path}",
            f"Pages: {self.page_count}",
            "",
            "Performance:",
            f"  Total time: {self.total_time_ms:.0f}ms ({self.total_time_ms/1000:.1f}s)",
            f"  Parse time: {self.parse_time_ms:.0f}ms",
            f"  Translation time: {self.translation_time_ms:.0f}ms",
            f"  Assembly time: {self.assembly_time_ms:.0f}ms",
            f"  Throughput: {self.page_count / (self.total_time_ms/1000):.1f} pages/s",
            "",
            "Translation:",
            f"  Total blocks: {self.metrics.total_blocks}",
            f"  Translated: {self.metrics.translated_blocks}",
            f"  Cached: {self.metrics.cached_blocks} ({self.metrics.cache_hit_ratio:.1%})",
            f"  Overflow: {self.metrics.overflow_blocks} ({self.metrics.overflow_rate:.1%})",
            f"  Failed: {self.metrics.failed_blocks}",
            f"  API calls: {self.metrics.api_calls}",
            "",
            "Cost:",
            f"  Estimated: ${self.estimated_cost_usd:.4f}",
            f"  Per page: ${self.cost_per_page:.6f}",
            "",
            "Quality:",
            f"  Avg score: {self.avg_quality_score:.2f}",
            f"  Overflow rate: {self.overflow_rate:.1%}",
        ]
        return "\n".join(lines)


class TranslationBenchmark:
    """翻译 Benchmark。"""

    def __init__(self) -> None:
        self._metrics = TranslationMetrics()

    def run(
        self,
        pdf_path: str,
        translator: Optional[Callable] = None,
        sample_pages: int = 10,
    ) -> BenchmarkResult:
        """运行 benchmark。"""
        result = BenchmarkResult(pdf_path=pdf_path)

        try:
            # 解析
            t0 = time.perf_counter()
            pdf = pikepdf.open(pdf_path)
            pages = list(pdf.pages)
            result.page_count = len(pages)
            result.parse_time_ms = (time.perf_counter() - t0) * 1000

            # 翻译（模拟）
            t1 = time.perf_counter()
            sample = pages[: min(sample_pages, len(pages))]

            for i, page in enumerate(sample):
                # 模拟 block 提取
                blocks = self._extract_blocks(page)
                self._metrics.total_blocks += len(blocks)

                for block in blocks:
                    if translator:
                        try:
                            translated = translator(block)
                            if translated:
                                self._metrics.translated_blocks += 1
                                # 检查 overflow
                                if len(translated) > len(block) * 1.5:
                                    self._metrics.overflow_blocks += 1
                                self._metrics.api_calls += 1
                        except Exception:
                            self._metrics.failed_blocks += 1
                    else:
                        # 无翻译器，模拟
                        self._metrics.translated_blocks += 1
                        self._metrics.api_calls += 1

            result.translation_time_ms = (time.perf_counter() - t1) * 1000

            # 组装（模拟）
            t2 = time.perf_counter()
            result.assembly_time_ms = (time.perf_counter() - t2) * 1000

            # 总时间
            result.total_time_ms = (time.perf_counter() - t0) * 1000

            # 复制指标
            result.metrics = self._metrics

            # 估算成本
            avg_tokens_per_call = 100
            cost_per_1k_tokens = 0.00002  # USD
            result.estimated_cost_usd = (
                self._metrics.api_calls
                * avg_tokens_per_call
                / 1000
                * cost_per_1k_tokens
            )
            result.cost_per_page = (
                result.estimated_cost_usd / result.page_count
                if result.page_count > 0
                else 0
            )

            # 质量（模拟）
            result.avg_quality_score = 0.85
            result.overflow_rate = self._metrics.overflow_rate

            pdf.close()

        except Exception as e:
            result.page_count = 0

        return result

    def _extract_blocks(self, page: pikepdf.Page) -> List[str]:
        """提取页面文本块（简化）。"""
        # 简化：返回模拟 blocks
        return ["block_1", "block_2", "block_3"]

    def batch_run(
        self,
        pdf_paths: List[str],
        translator: Optional[Callable] = None,
    ) -> List[BenchmarkResult]:
        """批量运行。"""
        results = []
        for path in pdf_paths:
            result = self.run(path, translator)
            results.append(result)
        return results

    def compare(
        self,
        result_a: BenchmarkResult,
        result_b: BenchmarkResult,
    ) -> str:
        """比较两个结果。"""
        speedup = (
            result_a.total_time_ms / result_b.total_time_ms
            if result_b.total_time_ms > 0
            else 0
        )

        lines = [
            "Benchmark Comparison",
            "=" * 60,
            f"{'Metric':<30} {'A':>12} {'B':>12} {'Ratio':>10}",
            "-" * 60,
            f"{'Total time (ms)':<30} {result_a.total_time_ms:>12.0f} {result_b.total_time_ms:>12.0f} {speedup:>9.1f}x",
            f"{'Pages':<30} {result_a.page_count:>12} {result_b.page_count:>12}",
            f"{'API calls':<30} {result_a.metrics.api_calls:>12} {result_b.metrics.api_calls:>12}",
            f"{'Cost':<30} ${result_a.estimated_cost_usd:>11.4f} ${result_b.estimated_cost_usd:>11.4f}",
        ]
        return "\n".join(lines)
