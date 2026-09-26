"""LayoutCascade — cascade 控制器。

按优先级尝试各 stage，直到置信度达标。

用法：
    cascade = LayoutCascade()

    result = cascade.infer(snapshot)
    # 自动选择 rule / column / ml / vision
"""

from __future__ import annotations

import time
from typing import List, Optional

from pdf2zh.layout.result import LayoutResult
from pdf2zh.layout.stage import LayoutConfidence, LayoutStage
from pdf2zh.parser.page_snapshot import PageSnapshot


class LayoutCascade:
    """Layout cascade 控制器。

    逻辑：
        for stage in stages:
            if stage.can_handle(snapshot):
                result = stage.infer(snapshot)
                confidence = stage.confidence(snapshot, result)
                if confidence.total >= threshold:
                    return result

        return fallback(snapshot)
    """

    def __init__(
        self,
        stages: Optional[List[LayoutStage]] = None,
        confidence_threshold: float = 0.7,
    ) -> None:
        if stages is None:
            from pdf2zh.layout.rule_stage import RuleLayoutStage

            stages = [RuleLayoutStage()]

        self.stages = stages
        self.confidence_threshold = confidence_threshold
        self._stats = {
            "total": 0,
            "stage_hits": {s.name: 0 for s in self.stages},
            "fallback": 0,
            "total_ms": 0.0,
        }

    def infer(self, snapshot: PageSnapshot) -> LayoutResult:
        """cascade 推理。"""
        self._stats["total"] += 1
        t0 = time.perf_counter()

        for stage in self.stages:
            if not stage.can_handle(snapshot):
                continue

            result = stage.infer(snapshot)
            confidence = stage.confidence(snapshot, result)

            if confidence.total >= self.confidence_threshold:
                self._stats["stage_hits"][stage.name] = (
                    self._stats["stage_hits"].get(stage.name, 0) + 1
                )
                result.confidence = confidence.total
                self._stats["total_ms"] += (time.perf_counter() - t0) * 1000
                return result

        # Fallback: 使用最后一个 stage 的结果
        if self.stages:
            last_stage = self.stages[-1]
            result = last_stage.infer(snapshot)
            self._stats["fallback"] += 1
            self._stats["total_ms"] += (time.perf_counter() - t0) * 1000
            return result

        # 无 stage 可用
        self._stats["fallback"] += 1
        self._stats["total_ms"] += (time.perf_counter() - t0) * 1000
        return LayoutResult(page_index=snapshot.page_index, source="empty")

    def batch_infer(self, snapshots: List[PageSnapshot]) -> List[LayoutResult]:
        """批量推理。"""
        return [self.infer(snap) for snap in snapshots]

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    def summary(self) -> str:
        s = self._stats
        total = s["total"] or 1
        parts = [f"total={s['total']}"]
        for name, count in s["stage_hits"].items():
            if count > 0:
                parts.append(f"{name}={count}")
        if s["fallback"] > 0:
            parts.append(f"fallback={s['fallback']}")
        parts.append(f"avg_ms={s['total_ms']/total:.1f}")
        return f"LayoutCascade | {' | '.join(parts)}"
