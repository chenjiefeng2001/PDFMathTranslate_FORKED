"""FeedbackStore — 翻译结果反馈存储。

存储翻译结果，用于策略学习。

用法：
    store = FeedbackStore()
    store.record(outcome)
    best = store.get_best_strategy(block_type="TOC")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TranslationOutcome:
    """翻译结果。"""

    block_type: str = ""
    strategy: str = ""
    text_hash: str = ""
    score: float = 0.0
    overflow: bool = False
    truncated: bool = False
    retry_count: int = 0
    timestamp: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyStats:
    """策略统计。"""

    strategy: str = ""
    block_type: str = ""
    count: int = 0
    avg_score: float = 0.0
    overflow_rate: float = 0.0
    retry_rate: float = 0.0


class FeedbackStore:
    """反馈存储。"""

    def __init__(self, persistence_path: Optional[Path] = None) -> None:
        self._outcomes: List[TranslationOutcome] = []
        self._persistence_path = persistence_path
        self._load()

    def _load(self) -> None:
        if self._persistence_path and self._persistence_path.exists():
            try:
                with open(self._persistence_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("outcomes", []):
                    self._outcomes.append(TranslationOutcome(**item))
            except Exception:
                pass

    def _save(self) -> None:
        if self._persistence_path:
            try:
                self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "outcomes": [
                        {
                            "block_type": o.block_type,
                            "strategy": o.strategy,
                            "text_hash": o.text_hash,
                            "score": o.score,
                            "overflow": o.overflow,
                            "truncated": o.truncated,
                            "retry_count": o.retry_count,
                            "timestamp": o.timestamp,
                        }
                        for o in self._outcomes[-1000:]  # 只保存最近 1000 条
                    ]
                }
                with open(self._persistence_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def record(self, outcome: TranslationOutcome) -> None:
        """记录结果。"""
        if outcome.timestamp == 0:
            outcome.timestamp = time.time()
        self._outcomes.append(outcome)
        self._save()

    def get_best_strategy(self, block_type: str) -> Optional[str]:
        """获取最佳策略。"""
        stats = self.get_strategy_stats(block_type)
        if not stats:
            return None
        # 按平均分排序

    def get_strategy_stats(self, block_type: str) -> List[StrategyStats]:
        """获取策略统计。"""
        relevant = [o for o in self._outcomes if o.block_type == block_type]
        if not relevant:
            return []

        # 按策略分组
        by_strategy: Dict[str, List[TranslationOutcome]] = {}
        for o in relevant:
            if o.strategy not in by_strategy:
                by_strategy[o.strategy] = []
            by_strategy[o.strategy].append(o)

        stats = []
        for strategy, outcomes in by_strategy.items():
            scores = [o.score for o in outcomes]
            overflows = sum(1 for o in outcomes if o.overflow)
            retries = sum(1 for o in outcomes if o.retry_count > 0)
            stats.append(
                StrategyStats(
                    strategy=strategy,
                    block_type=block_type,
                    count=len(outcomes),
                    avg_score=sum(scores) / len(scores) if scores else 0,
                    overflow_rate=overflows / len(outcomes) if outcomes else 0,
                    retry_rate=retries / len(outcomes) if outcomes else 0,
                )
            )

        return stats

    def get_recent_outcomes(
        self,
        block_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[TranslationOutcome]:
        """获取最近结果。"""
        outcomes = self._outcomes
        if block_type:
            outcomes = [o for o in outcomes if o.block_type == block_type]
        return outcomes[-limit:]

    def summary(self) -> str:
        total = len(self._outcomes)
        avg_score = sum(o.score for o in self._outcomes) / total if total > 0 else 0
        overflow_rate = (
            sum(1 for o in self._outcomes if o.overflow) / total if total > 0 else 0
        )
        return (
            f"FeedbackStore: {total} outcomes, "
            f"avg_score={avg_score:.2f}, "
            f"overflow_rate={overflow_rate:.1%}"
        )
