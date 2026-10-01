"""Pipeline — streaming compiler pipeline 控制器。

Producer-consumer DAG 的核心协调器。

用法：
    pipeline = Pipeline()

    pipeline.add_stage("parser", parser_fn)
    pipeline.add_stage("layout", layout_fn)
    pipeline.add_stage("translator", translate_fn)

    pipeline.connect("parser", "layout")
    pipeline.connect("layout", "translator")

    pipeline.run(source_pdf, output_pdf)
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from pdf2zh.runtime.metrics import PipelineMetrics
from pdf2zh.runtime.queue import PipelineQueue


class PipelineStage:
    """Pipeline 阶段。"""

    def __init__(
        self,
        name: str,
        fn: Callable,
        input_queue: Optional[PipelineQueue] = None,
        output_queue: Optional[PipelineQueue] = None,
    ) -> None:
        self.name = name
        self.fn = fn
        self.input_queue = input_queue
        self.output_queue = output_queue
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """启动阶段线程。"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止阶段。"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        """阶段运行循环。"""
        while self._running:
            if self.input_queue is None:
                break

            item = self.input_queue.get(timeout=0.1)
            if item is None:
                continue

            # 处理
            result = self.fn(item)

            # 输出
            if self.output_queue and result is not None:
                self.output_queue.put(result)

    def wait(self) -> None:
        """等待阶段完成。"""
        if self._thread:
            self._thread.join()


class Pipeline:
    """Streaming compiler pipeline。

    DAG 结构：
        parser → layout → translator → assembler

    用法：
        pipeline = Pipeline()

        @pipeline.stage("parser")
        def parse(item):
            return snapshot

        @pipeline.stage("layout")
        def layout(snapshot):
            return result

        pipeline.run(source, output)
    """

    def __init__(self, queue_size: int = 64) -> None:
        self._queue_size = queue_size
        self._stages: Dict[str, PipelineStage] = {}
        self._connections: Dict[str, str] = {}
        self._queues: Dict[str, PipelineQueue] = {}
        self._metrics = PipelineMetrics()
        self._running = False

    def stage(self, name: str):
        """装饰器：注册阶段。"""

        def decorator(fn):
            self.add_stage(name, fn)
            return fn

        return decorator

    def add_stage(self, name: str, fn: Callable) -> None:
        """添加阶段。"""
        queue = PipelineQueue(max_size=self._queue_size)
        self._queues[name] = queue
        self._stages[name] = PipelineStage(name, fn, input_queue=queue)

    def connect(self, from_stage: str, to_stage: str) -> None:
        """连接阶段。"""
        if from_stage not in self._stages:
            raise ValueError(f"Stage {from_stage} not found")
        if to_stage not in self._stages:
            raise ValueError(f"Stage {to_stage} not found")

        self._stages[from_stage].output_queue = self._queues[to_stage]
        self._connections[from_stage] = to_stage

    def run(
        self,
        items: List[Any],
        output_fn: Optional[Callable] = None,
    ) -> PipelineMetrics:
        """运行 pipeline。"""
        self._metrics = PipelineMetrics(total_pages=len(items))
        self._metrics.start_time = time.perf_counter()

        # 启动所有阶段
        for stage in self._stages.values():
            stage.start()

        # 获取第一个阶段的队列
        first_stage = list(self._stages.values())[0] if self._stages else None
        if not first_stage:
            return self._metrics

        # Feed items
        for item in items:
            first_stage.input_queue.put(item)

        # 等待所有阶段完成
        for stage in self._stages.values():
            stage.wait()

        self._metrics.end_time = time.perf_counter()

        return self._metrics

    def summary(self) -> str:
        lines = ["Pipeline:"]
        for name, stage in self._stages.items():
            q = self._queues.get(name)
            q_info = q.summary() if q else "no queue"
            lines.append(f"  {name}: {q_info}")
        lines.append("")
        lines.append(self._metrics.summary())
        return "\n".join(lines)
