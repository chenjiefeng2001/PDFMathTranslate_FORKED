"""Benchmark — 730 页 production benchmark。

Phase 6: Production Readiness Matrix。

用法：
    from pdf2zh.benchmark import BenchmarkRunner, BenchmarkReporter, default_suite

    # 运行 benchmark
    suite = default_suite()
    dataset = suite.find_by_name("itbook-export")
    runner = BenchmarkRunner()
    result = runner.run(dataset, runtime="stream", backend="pikepdf")

    # 生成报告
    reporter = BenchmarkReporter()
    print(result.summary())
"""

from pdf2zh.benchmark.dataset import BenchmarkDataset, BenchmarkSuite, default_suite
from pdf2zh.benchmark.result import (
    BackendResult,
    MemoryResult,
    PhaseResult,
    RuntimeBenchmarkResult,
    TranslationResult,
)
from pdf2zh.benchmark.report import BenchmarkReporter, ComparisonResult
from pdf2zh.benchmark.runner import BenchmarkRunner

__all__ = [
    "BenchmarkDataset",
    "BenchmarkSuite",
    "default_suite",
    "RuntimeBenchmarkResult",
    "PhaseResult",
    "TranslationResult",
    "MemoryResult",
    "BackendResult",
    "BenchmarkRunner",
    "BenchmarkReporter",
    "ComparisonResult",
]
