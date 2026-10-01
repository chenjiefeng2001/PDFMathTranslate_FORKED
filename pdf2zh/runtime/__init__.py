"""Runtime — streaming compiler pipeline + production runtime。

Phase 10 + Phase 12:
    - PipelineQueue: 流式队列 + backpressure
    - Pipeline: DAG 协调器
    - StreamingAssembler: 流式组装
    - PipelineMetrics: 指标跟踪
    - RuntimeMode: batch vs stream
    - TranslationManifest: checkpoint/resume
    - DependencyGraph: artifact invalidation
    - RuntimeOptimizer: decision engine
    - MemoryController: memory pressure

用法：
    from pdf2zh.runtime import PipelineQueue, RuntimeOptimizer, MemoryController

    optimizer = RuntimeOptimizer()
    decision = optimizer.choose_from_snapshot(snapshot)

    controller = MemoryController(max_rss_mb=4096)
    controller.check_pressure()
"""

from pdf2zh.runtime.assembler import StreamingAssembler
from pdf2zh.runtime.metrics import PipelineMetrics, StageMetrics
from pdf2zh.runtime.pipeline import Pipeline, PipelineStage
from pdf2zh.runtime.queue import PipelineQueue
from pdf2zh.runtime.mode import RuntimeMode
from pdf2zh.runtime.manifest import TranslationManifest, PageState
from pdf2zh.runtime.dependency_graph import DependencyGraph, InvalidationResult
from pdf2zh.runtime.optimizer import RuntimeOptimizer, PageFeatures, PipelinePlan
from pdf2zh.runtime.memory_controller import MemoryController, MemoryPolicy, MemoryState

__all__ = [
    # Streaming
    "PipelineQueue",
    "Pipeline",
    "PipelineStage",
    "StreamingAssembler",
    # Metrics
    "PipelineMetrics",
    "StageMetrics",
    # Mode
    "RuntimeMode",
    # Manifest
    "TranslationManifest",
    "PageState",
    # Dependency
    "DependencyGraph",
    "InvalidationResult",
    # Optimizer
    "RuntimeOptimizer",
    "PageFeatures",
    "PipelinePlan",
    # Memory
    "MemoryController",
    "MemoryPolicy",
    "MemoryState",
]
