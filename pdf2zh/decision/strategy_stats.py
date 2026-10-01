"""StrategyStats — 策略统计。

追踪每个翻译策略的效果。

用法：
    stats = StrategyStats()
    stats.record("toc_constrained", quality=0.93, errors=["overflow"])
    summary = stats.get_summary("toc_constrained")
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StrategyRecord:
    """策略记录。"""

    strategy: str = ""
    quality: float = 0.0
    errors: List[str] = field(default_factory=list)
    cost_tokens: int = 0
    latency_ms: float = 0.0
    timestamp: float = 0.0
    block_type: str = "paragraph"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategySummary:
    """策略汇总。"""

    strategy: str = ""
    sample_count: int = 0

    # 质量统计
    quality_mean: float = 0.0
    quality_std: float = 0.0
    quality_min: float = 0.0
    quality_max: float = 0.0

    # 错误统计
    error_counts: Dict[str, int] = field(default_factory=dict)
    total_errors: int = 0
    error_rate: float = 0.0

    # 成本统计
    avg_tokens: float = 0.0
    avg_latency_ms: float = 0.0

    # 通过率
    pass_rate: float = 0.0  # quality >= 0.85

    def summary(self) -> str:
        lines = [
            f"Strategy: {self.strategy}",
            f"  Samples: {self.sample_count}",
            f"  Quality: {self.quality_mean:.2f} (std={self.quality_std:.2f})",
            f"  Pass rate: {self.pass_rate:.1%}",
            f"  Error rate: {self.error_rate:.1%}",
            f"  Avg tokens: {self.avg_tokens:.0f}",
            f"  Avg latency: {self.avg_latency_ms:.0f}ms",
        ]
        if self.error_counts:
            lines.append("  Top errors:")
            for err, count in sorted(self.error_counts.items(), key=lambda x: -x[1])[
                :3
            ]:
                lines.append(f"    {err}: {count}")
        return "\n".join(lines)


class StrategyStats:
    """策略统计。"""

    def __init__(self) -> None:
        self._records: List[StrategyRecord] = []
        self._by_strategy: Dict[str, List[StrategyRecord]] = defaultdict(list)

    def record(
        self,
        strategy: str,
        quality: float,
        errors: Optional[List[str]] = None,
        cost_tokens: int = 0,
        latency_ms: float = 0.0,
        block_type: str = "paragraph",
    ) -> None:
        """记录策略结果。"""
        record = StrategyRecord(
            strategy=strategy,
            quality=quality,
            errors=errors or [],
            cost_tokens=cost_tokens,
            latency_ms=latency_ms,
            timestamp=time.time(),
            block_type=block_type,
        )
        self._records.append(record)
        self._by_strategy[strategy].append(record)

    def get_summary(self, strategy: str) -> StrategySummary:
        """获取策略汇总。"""
        records = self._by_strategy.get(strategy, [])
        if not records:
            return StrategySummary(strategy=strategy)

        # 质量统计
        qualities = [r.quality for r in records]
        quality_mean = sum(qualities) / len(qualities)
        quality_var = sum((q - quality_mean) ** 2 for q in qualities) / len(qualities)
        quality_std = quality_var**0.5

        # 错误统计
        all_errors = []
        for r in records:
            all_errors.extend(r.errors)
        error_counts = {}
        for err in all_errors:
            error_counts[err] = error_counts.get(err, 0) + 1

        # 成本统计
        tokens = [r.cost_tokens for r in records if r.cost_tokens > 0]
        latencies = [r.latency_ms for r in records if r.latency_ms > 0]

        # 通过率
        pass_count = sum(1 for q in qualities if q >= 0.85)

        return StrategySummary(
            strategy=strategy,
            sample_count=len(records),
            quality_mean=quality_mean,
            quality_std=quality_std,
            quality_min=min(qualities),
            quality_max=max(qualities),
            error_counts=error_counts,
            total_errors=len(all_errors),
            error_rate=len(all_errors) / len(records) if records else 0,
            avg_tokens=sum(tokens) / len(tokens) if tokens else 0,
            avg_latency_ms=sum(latencies) / len(latencies) if latencies else 0,
            pass_rate=pass_count / len(records) if records else 0,
        )

    def compare_strategies(
        self,
        strategy_a: str,
        strategy_b: str,
    ) -> Dict[str, Any]:
        """比较两个策略。"""
        summary_a = self.get_summary(strategy_a)
        summary_b = self.get_summary(strategy_b)

        return {
            "strategy_a": strategy_a,
            "strategy_b": strategy_b,
            "quality_diff": summary_a.quality_mean - summary_b.quality_mean,
            "error_diff": summary_a.error_rate - summary_b.error_rate,
            "cost_diff": summary_a.avg_tokens - summary_b.avg_tokens,
            "recommendation": self._recommend(summary_a, summary_b),
        }

    def _recommend(
        self,
        a: StrategySummary,
        b: StrategySummary,
    ) -> str:
        """推荐策略。"""
        if a.quality_mean > b.quality_mean + 0.05:
            return a.strategy
        elif b.quality_mean > a.quality_mean + 0.05:
            return b.strategy
        elif a.error_rate < b.error_rate:
            return a.strategy
        else:
            return "either"

    def get_best_strategy(
        self,
        block_type: str = "paragraph",
        min_samples: int = 10,
    ) -> Optional[str]:
        """获取最佳策略。"""
        best_strategy = None
        best_score = -1

        for strategy, records in self._by_strategy.items():
            # 过滤 block_type
            filtered = [r for r in records if r.block_type == block_type]
            if len(filtered) < min_samples:
                continue

            # 计算综合分数
            qualities = [r.quality for r in filtered]
            avg_quality = sum(qualities) / len(qualities)
            error_count = sum(len(r.errors) for r in filtered)
            error_penalty = error_count / len(filtered) * 0.1

            score = avg_quality - error_penalty

            if score > best_score:
                best_score = score
                best_strategy = strategy

        return best_strategy

    def to_dict(self) -> Dict[str, Any]:
        """导出为字典。"""
        return {
            strategy: {
                "sample_count": len(records),
                "quality_mean": (
                    sum(r.quality for r in records) / len(records) if records else 0
                ),
                "error_rate": (
                    sum(len(r.errors) for r in records) / len(records) if records else 0
                ),
            }
            for strategy, records in self._by_strategy.items()
        }
