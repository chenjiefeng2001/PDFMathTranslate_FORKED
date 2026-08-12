"""Parallel engine sub-package (V3 iteration).

对外仅暴露 ``TaskCoordinator`` 与错误分类；任务/结果/worker 为内部契约。
"""

from pdf2zh.parallel.coordinator import TaskCoordinator
from pdf2zh.parallel.errors import (
    PageProcessingError,
    ParallelError,
    ProtocolViolationError,
    WorkerBootstrapError,
    WorkerProcessError,
)

__all__ = [
    "TaskCoordinator",
    "ParallelError",
    "WorkerBootstrapError",
    "WorkerProcessError",
    "PageProcessingError",
    "ProtocolViolationError",
]
