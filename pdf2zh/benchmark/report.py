"""BenchmarkReport — 生成 benchmark 报告。

用法：
    reporter = BenchmarkReporter()

    report = reporter.compare(batch_result, stream_result)
    print(report)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pdf2zh.benchmark.result import RuntimeBenchmarkResult


@dataclass
class ComparisonResult:
    """A/B 对比结果。"""

    a: RuntimeBenchmarkResult
    b: RuntimeBenchmarkResult

    @property
    def speedup(self) -> float:
        if self.b.total_time_ms <= 0:
            return 0.0
        return self.a.total_time_ms / self.b.total_time_ms

    @property
    def first_page_improvement(self) -> float:
        if self.a.first_page_latency_ms <= 0:
            return 0.0
        return self.a.first_page_latency_ms / self.b.first_page_latency_ms

    @property
    def memory_ratio(self) -> float:
        if self.a.memory.peak_rss_mb <= 0:
            return 0.0
        return self.b.memory.peak_rss_mb / self.a.memory.peak_rss_mb


class BenchmarkReporter:
    """Benchmark 报告生成器。"""

    def compare(
        self,
        result_a: RuntimeBenchmarkResult,
        result_b: RuntimeBenchmarkResult,
    ) -> ComparisonResult:
        """对比两个结果。"""
        return ComparisonResult(a=result_a, b=result_b)

    def format_comparison(
        self,
        comparison: ComparisonResult,
        label_a: str = "batch",
        label_b: str = "stream",
    ) -> str:
        """格式化对比报告。"""
        a = comparison.a
        b = comparison.b

        lines = [
            "Production Readiness Matrix",
            "=" * 70,
            f"Dataset: {a.dataset_name} ({a.pages} pages)",
            f"Backend: {a.assembly.backend_name}",
            "",
            f"{'Metric':<30} {label_a:>15} {label_b:>15} {'Ratio':>10}",
            "-" * 70,
            f"{'First Page Latency (ms)':<30} {a.first_page_latency_ms:>15.0f} {b.first_page_latency_ms:>15.0f} {comparison.first_page_improvement:>9.1f}x",
            f"{'Steady Throughput (p/s)':<30} {a.steady_throughput_pages_sec:>15.1f} {b.steady_throughput_pages_sec:>15.1f} {b.steady_throughput_pages_sec / max(a.steady_throughput_pages_sec, 0.01):>9.1f}x",
            f"{'Total Time (ms)':<30} {a.total_time_ms:>15.0f} {b.total_time_ms:>15.0f} {comparison.speedup:>9.1f}x",
            f"{'Memory Peak (MB)':<30} {a.memory.peak_rss_mb:>15.1f} {b.memory.peak_rss_mb:>15.1f} {comparison.memory_ratio:>9.2f}x",
            "",
            "--- Translation ---",
            f"{'Total Blocks':<30} {a.translation.total_blocks:>15} {b.translation.total_blocks:>15}",
            f"{'Unique Blocks':<30} {a.translation.unique_blocks:>15} {b.translation.unique_blocks:>15}",
            f"{'Cache Hit Ratio':<30} {a.translation.cache_hit_ratio:>14.1%} {b.translation.cache_hit_ratio:>14.1%}",
            f"{'API Requests':<30} {a.translation.api_requests:>15} {b.translation.api_requests:>15}",
            "",
            "--- Backend ---",
            f"{'Objects Before':<30} {a.assembly.objects_before:>15} {b.assembly.objects_before:>15}",
            f"{'Objects After':<30} {a.assembly.objects_after:>15} {b.assembly.objects_after:>15}",
            f"{'Reuse Ratio':<30} {a.assembly.reuse_ratio:>14.1%} {b.assembly.reuse_ratio:>14.1%}",
            f"{'Resource Growth':<30} {a.assembly.resource_growth:>14.2f}x {b.assembly.resource_growth:>14.2f}x",
            f"{'Write Time (ms)':<30} {a.assembly.write_time_ms:>15.0f} {b.assembly.write_time_ms:>15.0f}",
            "",
            "--- Validation ---",
            f"{'Semantic Equal':<30} {str(a.semantic_equal):>15} {str(b.semantic_equal):>15}",
            f"{'Valid':<30} {str(a.valid):>15} {str(b.valid):>15}",
            "",
            "Acceptance Criteria:",
            f"  {'✓' if a.semantic_equal and b.semantic_equal else '✗'} Semantic regression pass",
            f"  {'✓' if comparison.speedup > 1.0 else '✗'} Stream faster than batch ({comparison.speedup:.1f}x)",
            f"  {'✓' if b.memory.peak_rss_mb < a.memory.peak_rss_mb else '✗'} Memory bounded",
            f"  {'✓' if b.first_page_latency_ms < a.first_page_latency_ms else '✗'} First page latency reduced",
            f"  {'✓' if a.assembly.valid and b.assembly.valid else '✗'} Backend valid",
            f"  {'✓' if a.assembly.resource_growth < 1.1 and b.assembly.resource_growth < 1.1 else '✗'} Resource growth controlled",
        ]

        return "\n".join(lines)
