"""Parallel engine error taxonomy (V3 iteration).

语义边界即兜底策略边界：

- ``ParallelError``（基类）→ 整体串行兜底
- ``WorkerBootstrapError`` → worker 冷启动失败 → 整体串行兜底
- ``WorkerProcessError`` → 进程崩溃（死 worker）→ 该 chunk 失败 → 串行补跑
- ``PageProcessingError`` → 单 chunk 内计算异常 → 失败 → 串行补跑
- ``ProtocolViolationError`` → pickle 违例（如混入 Lock/Condition）→ 整体串行兜底

约定：``KeyboardInterrupt`` 不落入本体系 —— 它必须直接传播给上层
（GUI 优雅关闭 / CLI 退出），绝不进入任何串行兜底。
"""

from __future__ import annotations

__all__ = [
    "ParallelError",
    "WorkerBootstrapError",
    "WorkerProcessError",
    "PageProcessingError",
    "ProtocolViolationError",
]


class ParallelError(Exception):
    """并行引擎错误基类；语义 = 需要整体串行兜底。

    仅 ``WorkerProcessError`` / ``PageProcessingError`` 可在 coordinator
    内部转为 chunk 级增量降级，其余子类一律向上传播触发整体串行。
    """


class WorkerBootstrapError(ParallelError):
    """Worker 冷启动失败（模型/onnxruntime 不可用、initializer 崩溃）。

    兜底策略：整体串行（池从未可用，重试无意义）。
    """


class WorkerProcessError(ParallelError):
    """Worker 进程崩溃（死 worker / BrokenProcessPool）。

    兜底策略：该 chunk 失败 → 串行补跑（其余已成功 chunk 保留）。
    """


class PageProcessingError(ParallelError):
    """单个 chunk 内的计算异常。

    兜底策略：失败 → 串行补跑。
    """


class ProtocolViolationError(ParallelError):
    """任务/结果跨越进程边界时的 pickle 违例（如混入 Lock/Condition）。

    兜底策略：整体串行（任务契约本身不合法，重试同样会失败）。
    """
