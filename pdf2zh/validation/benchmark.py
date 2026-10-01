"""RuntimeBenchmark — 双模式 benchmark 对比。

比较 batch vs streaming 的性能差异。

用法：
    benchmark = RuntimeBenchmark()

    result = benchmark.compare(input_pdf, output_dir)
    # RuntimeComparison(batch_metrics, stream_metrics, semantic_diff)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import pikepdf

from pdf2zh.runtime.metrics import PipelineMetrics
from pdf2zh.validation.regression import SemanticComparator, SemanticDiff


@dataclass
class RuntimeComparison:
    """双模式对比结果。"""

    batch_metrics: PipelineMetrics = field(default_factory=PipelineMetrics)
    stream_metrics: PipelineMetrics = field(default_factory=PipelineMetrics)
    semantic_diff: SemanticDiff = field(default_factory=SemanticDiff)

    batch_output_size: int = 0
    stream_output_size: int = 0

    @property
    def is_semantically_equal(self) -> bool:
        return self.semantic_diff.is_equal

    @property
    def speedup(self) -> float:
        batch_ms = self.batch_metrics.total_ms
        stream_ms = self.stream_metrics.total_ms
        if stream_ms <= 0:
            return 0.0
        return batch_ms / stream_ms

    def summary(self) -> str:
        lines = [
            "Runtime Benchmark Comparison",
            "=" * 60,
            "",
            f"{'Metric':<35} {'Batch':>12} {'Stream':>12}",
            "-" * 60,
            f"{'Total time (ms)':<35} {self.batch_metrics.total_ms:>12.0f} {self.stream_metrics.total_ms:>12.0f}",
            f"{'Throughput (pages/s)':<35} {self.batch_metrics.end_to_end_throughput:>12.1f} {self.stream_metrics.end_to_end_throughput:>12.1f}",
            f"{'First page latency (ms)':<35} {self.batch_metrics.first_page_latency_ms:>12.0f} {self.stream_metrics.first_page_latency_ms:>12.0f}",
            f"{'Memory peak (MB)':<35} {self.batch_metrics.memory_peak_mb:>12.1f} {self.stream_metrics.memory_peak_mb:>12.1f}",
            "",
            f"{'Output size (MB)':<35} {self.batch_output_size/1024/1024:>12.2f} {self.stream_output_size/1024/1024:>12.2f}",
            f"{'Speedup':<35} {self.speedup:>12.2f}x",
            "",
            f"Semantic Equal: {self.is_semantically_equal}",
            f"Semantic Errors: {self.semantic_diff.error_count}",
            f"Semantic Warnings: {self.semantic_diff.warning_count}",
        ]

        # Stage breakdown
        if self.batch_metrics.stages or self.stream_metrics.stages:
            lines.append("")
            lines.append("Stage Breakdown:")
            all_stages = set(self.batch_metrics.stages.keys()) | set(
                self.stream_metrics.stages.keys()
            )
            for stage in sorted(all_stages):
                batch_ms = self.batch_metrics.stages.get(stage)
                stream_ms = self.stream_metrics.stages.get(stage)
                b = batch_ms.total_ms if batch_ms else 0
                s = stream_ms.total_ms if stream_ms else 0
                lines.append(f"  {stage:<20} batch={b:>8.0f}ms  stream={s:>8.0f}ms")

        return "\n".join(lines)


class RuntimeBenchmark:
    """双模式 benchmark。"""

    def __init__(self) -> None:
        self._comparator = SemanticComparator()

    def benchmark_batch(
        self,
        input_pdf: Path,
        output_pdf: Path,
        mock_translate=None,
    ) -> PipelineMetrics:
        """benchmark batch 模式。"""
        metrics = PipelineMetrics()
        metrics.start_time = time.perf_counter()
        metrics.total_pages = 0

        pdf = pikepdf.open(str(input_pdf))
        pages = list(pdf.pages)
        metrics.total_pages = len(pages)

        # 模拟翻译
        for i, page in enumerate(pages):
            t0 = time.perf_counter()
            if mock_translate:
                mock_translate(page, i)
            metrics.record_stage(
                "translate", pages=1, ms=(time.perf_counter() - t0) * 1000
            )

        pdf.save(str(output_pdf))
        metrics.end_time = time.perf_counter()

        return metrics

    def benchmark_stream(
        self,
        input_pdf: Path,
        output_pdf: Path,
        mock_translate=None,
    ) -> PipelineMetrics:
        """benchmark stream 模式。"""
        from pdf2zh.runtime.queue import PipelineQueue

        metrics = PipelineMetrics()
        metrics.start_time = time.perf_counter()

        pdf = pikepdf.open(str(input_pdf))
        pages = list(pdf.pages)
        metrics.total_pages = len(pages)

        queue = PipelineQueue(max_size=32)

        # 模拟 streaming
        for i, page in enumerate(pages):
            t0 = time.perf_counter()
            queue.put(page)
            item = queue.get()
            if mock_translate:
                mock_translate(item, i)
            metrics.record_stage(
                "translate", pages=1, ms=(time.perf_counter() - t0) * 1000
            )

        pdf.save(str(output_pdf))
        metrics.end_time = time.perf_counter()

        return metrics

    def compare(
        self,
        input_pdf: Path,
        output_dir: Path,
        mock_translate=None,
    ) -> RuntimeComparison:
        """运行双模式对比。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        batch_output = output_dir / "batch_output.pdf"
        stream_output = output_dir / "stream_output.pdf"

        # Batch
        batch_metrics = self.benchmark_batch(input_pdf, batch_output, mock_translate)

        # Stream
        stream_metrics = self.benchmark_stream(input_pdf, stream_output, mock_translate)

        # Semantic comparison
        semantic_diff = self._comparator.compare(str(batch_output), str(stream_output))

        return RuntimeComparison(
            batch_metrics=batch_metrics,
            stream_metrics=stream_metrics,
            semantic_diff=semantic_diff,
            batch_output_size=(
                batch_output.stat().st_size if batch_output.exists() else 0
            ),
            stream_output_size=(
                stream_output.stat().st_size if stream_output.exists() else 0
            ),
        )
