"""Observability — 全链路可观测性。

Phase 9.3 + Phase 11.1:
    - PipelineTrace: 追踪每页执行
    - Span: 单阶段追踪
    - PipelineProfiler: 瓶颈分析
    - PipelineCostModel: 成本模型

用法：
    from pdf2zh.observability import PipelineTrace, PipelineProfiler

    # 追踪
    trace = PipelineTrace()
    with trace.span("parse", page=0) as s:
        result = parse_page(pdf, 0)

    # 分析
    profiler = PipelineProfiler()
    report = profiler.estimate(pages=730)
    print(report.summary())
"""

from pdf2zh.observability.trace import PageTrace, PipelineTrace, Span
from pdf2zh.observability.profiler import (
    BottleneckReport,
    PipelineCostModel,
    PipelineProfiler,
    StageMetrics,
)

__all__ = [
    "PipelineTrace",
    "PageTrace",
    "Span",
    "PipelineProfiler",
    "PipelineCostModel",
    "BottleneckReport",
    "StageMetrics",
]
