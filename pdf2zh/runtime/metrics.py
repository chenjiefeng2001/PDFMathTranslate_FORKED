"""PipelineMetrics — 流式 pipeline 指标。

跟踪每个阶段的 throughput、latency、memory。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class StageMetrics:
    """单阶段指标。"""

    name: str = ""
    pages_processed: int = 0
    total_ms: float = 0.0
    queue_wait_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.pages_processed if self.pages_processed > 0 else 0

    @property
    def throughput(self) -> float:
        """pages/sec"""
        if self.total_ms <= 0:
            return 0.0
        return self.pages_processed / (self.total_ms / 1000)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pages_processed": self.pages_processed,
            "total_ms": self.total_ms,
            "avg_ms": self.avg_ms,
            "throughput": self.throughput,
        }


@dataclass
class PipelineMetrics:
    """完整 pipeline 指标。"""

    total_pages: int = 0
    start_time: float = 0.0
    end_time: float = 0.0

    stages: Dict[str, StageMetrics] = field(default_factory=dict)

    memory_peak_mb: float = 0.0
    first_page_latency_ms: float = 0.0

    def stage(self, name: str) -> StageMetrics:
        """获取或创建阶段指标。"""
        if name not in self.stages:
            self.stages[name] = StageMetrics(name=name)
        return self.stages[name]

    def record_stage(
        self,
        name: str,
        pages: int = 1,
        ms: float = 0.0,
        queue_wait_ms: float = 0.0,
    ) -> None:
        """记录阶段指标。"""
        s = self.stage(name)
        s.pages_processed += pages
        s.total_ms += ms
        s.queue_wait_ms += queue_wait_ms

    @property
    def total_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000 if self.end_time > 0 else 0

    @property
    def end_to_end_throughput(self) -> float:
        """端到端 throughput (pages/sec)"""
        if self.total_ms <= 0:
            return 0.0
        return self.total_pages / (self.total_ms / 1000)

    def summary(self) -> str:
        lines = [
            f"PipelineMetrics | pages={self.total_pages} | total={self.total_ms:.0f}ms",
            f"  throughput={self.end_to_end_throughput:.1f} pages/s",
            f"  first_page={self.first_page_latency_ms:.0f}ms",
            f"  memory_peak={self.memory_peak_mb:.1f}MB",
            "",
            "  Stage breakdown:",
        ]
        for name, stage in sorted(self.stages.items()):
            lines.append(
                f"    {name:<20} {stage.pages_processed:>5} pages "
                f"{stage.total_ms:>8.0f}ms "
                f"({stage.throughput:.1f} pages/s)"
            )
        return "\n".join(lines)
