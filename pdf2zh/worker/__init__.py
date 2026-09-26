"""Worker chunk execution — pipeline execution unit。

Priority 7:
    - ChunkTask: pipeline execution unit
    - ChunkResult: batch result + metrics
    - ChunkScheduler: dynamic chunk sizing
    - WorkerExecutor: process pool management

数据流：
    PDF pages
        ↓
    ChunkScheduler
        ↓
    ChunkTask[]
        ↓
    WorkerExecutor
        ↓
    ChunkResult[]
        ↓
    Merge → TranslationArtifact
"""

from pdf2zh.worker.executor import WorkerExecutor
from pdf2zh.worker.result import ChunkMetrics, ChunkResult, PageResult
from pdf2zh.worker.scheduler import ChunkScheduler
from pdf2zh.worker.task import CacheContext, ChunkTask, PageTask

__all__ = [
    "ChunkTask",
    "ChunkResult",
    "ChunkMetrics",
    "ChunkScheduler",
    "WorkerExecutor",
    "PageTask",
    "CacheContext",
    "PageResult",
]
