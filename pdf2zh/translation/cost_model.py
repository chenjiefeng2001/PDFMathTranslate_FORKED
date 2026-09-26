"""TranslationCostModel — 翻译成本预测。

基于历史数据预测翻译成本。

用法：
    model = TranslationCostModel()
    estimate = model.estimate(block_type="TOC", text_length=50)
    # CostEstimate(time_ms=120, cost_usd=0.001, quality=0.89)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CostEstimate:
    """成本估算。"""

    time_ms: float = 0.0
    cost_usd: float = 0.0
    quality: float = 0.9
    overflow_risk: float = 0.0
    strategy: str = "default"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"CostEstimate: {self.time_ms:.0f}ms, "
            f"${self.cost_usd:.4f}, "
            f"quality={self.quality:.2f}, "
            f"overflow={self.overflow_risk:.1%}"
        )


@dataclass
class StrategyProfile:
    """策略画像。"""

    strategy: str = ""
    avg_time_ms: float = 0.0
    avg_quality: float = 0.0
    overflow_rate: float = 0.0
    cost_per_token: float = 0.00002  # USD
    sample_count: int = 0


class TranslationCostModel:
    """翻译成本模型。"""

    def __init__(self) -> None:
        self._profiles: Dict[str, StrategyProfile] = {
            "default": StrategyProfile(
                strategy="default",
                avg_time_ms=320.0,
                avg_quality=0.88,
                overflow_rate=0.05,
            ),
            "fast": StrategyProfile(
                strategy="fast",
                avg_time_ms=150.0,
                avg_quality=0.82,
                overflow_rate=0.12,
            ),
            "toc_constrained": StrategyProfile(
                strategy="toc_constrained",
                avg_time_ms=120.0,
                avg_quality=0.91,
                overflow_rate=0.02,
            ),
            "table_aware": StrategyProfile(
                strategy="table_aware",
                avg_time_ms=400.0,
                avg_quality=0.85,
                overflow_rate=0.08,
            ),
        }

    def estimate(
        self,
        block_type: str = "paragraph",
        text_length: int = 100,
        strategy: str = "default",
    ) -> CostEstimate:
        """估算成本。"""
        profile = self._profiles.get(strategy, self._profiles["default"])

        # 时间估算
        time_ms = profile.avg_time_ms * (text_length / 100)

        # 成本估算（假设 GPT-4 价格）
        tokens = text_length // 4  # 粗略估算
        cost_usd = tokens * profile.cost_per_token * 2  # input + output

        # 质量估算
        quality = profile.avg_quality

        # overflow 风险
        overflow_risk = profile.overflow_rate
        if block_type == "toc":
            overflow_risk *= 0.5  # TOC 用专门策略
        elif block_type == "caption":
            overflow_risk *= 1.5  # caption 更容易 overflow

        return CostEstimate(
            time_ms=time_ms,
            cost_usd=cost_usd,
            quality=quality,
            overflow_risk=overflow_risk,
            strategy=strategy,
        )

    def select_strategy(
        self,
        block_type: str,
        text_length: int,
        quality_target: float = 0.9,
        cost_budget_usd: float = 0.001,
    ) -> str:
        """选择策略。"""
        candidates = []

        for strategy, profile in self._profiles.items():
            estimate = self.estimate(block_type, text_length, strategy)
            if (
                estimate.quality >= quality_target
                and estimate.cost_usd <= cost_budget_usd
            ):
                candidates.append((strategy, estimate))

        if not candidates:
            return "fast"  # fallback

        # 选择成本最低的
        candidates.sort(key=lambda x: x[1].cost_usd)
        return candidates[0][0]

    def update_profile(
        self,
        strategy: str,
        time_ms: float,
        quality: float,
        overflow: bool,
    ) -> None:
        """更新策略画像。"""
        if strategy not in self._profiles:
            self._profiles[strategy] = StrategyProfile(strategy=strategy)

        profile = self._profiles[strategy]
        n = profile.sample_count

        # 滑动平均
        profile.avg_time_ms = (profile.avg_time_ms * n + time_ms) / (n + 1)
        profile.avg_quality = (profile.avg_quality * n + quality) / (n + 1)
        profile.overflow_rate = (
            profile.overflow_rate * n + (1.0 if overflow else 0.0)
        ) / (n + 1)
        profile.sample_count = n + 1

    def summary(self) -> str:
        lines = ["TranslationCostModel:"]
        for name, profile in self._profiles.items():
            lines.append(
                f"  {name}: time={profile.avg_time_ms:.0f}ms, "
                f"quality={profile.avg_quality:.2f}, "
                f"overflow={profile.overflow_rate:.1%}, "
                f"samples={profile.sample_count}"
            )
        return "\n".join(lines)
