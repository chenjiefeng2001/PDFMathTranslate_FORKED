"""RuntimePlanner — 运行时规划器。

根据 PDFProfile 选择最优运行策略。

用法：
    planner = RuntimePlanner()
    plan = planner.create(profile)
    # RuntimePlan(parser="columnar", layout="rule", workers=8, ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class RuntimePlan:
    """运行时计划。"""

    # Parser
    parser: str = "columnar"
    parser_options: Dict[str, Any] = field(default_factory=dict)

    # Layout
    layout: str = "rule"
    layout_options: Dict[str, Any] = field(default_factory=dict)

    # Translation
    translator: str = "default"
    batch_size: int = 32
    max_tokens: int = 4096

    # Workers
    workers: int = 4
    use_streaming: bool = True

    # Quality
    quality_level: str = "normal"  # low, normal, high
    enable_retry: bool = True
    max_retries: int = 2

    # Cache
    cache_policy: str = "aggressive"  # none, normal, aggressive

    # Memory
    memory_limit_mb: int = 4096

    # Cost
    cost_budget_usd: float = 1.0

    def summary(self) -> str:
        lines = [
            "Runtime Plan:",
            f"  Parser: {self.parser}",
            f"  Layout: {self.layout}",
            f"  Translator: {self.translator}",
            f"  Workers: {self.workers}",
            f"  Batch size: {self.batch_size}",
            f"  Quality: {self.quality_level}",
            f"  Streaming: {self.use_streaming}",
            f"  Cache: {self.cache_policy}",
        ]
        return "\n".join(lines)


class RuntimePlanner:
    """运行时规划器。"""

    def create(self, profile: Any) -> RuntimePlan:
        """根据 PDFProfile 创建计划。"""
        plan = RuntimePlan()

        # 根据难度调整
        difficulty = getattr(profile, "difficulty", "medium")
        page_count = getattr(profile, "page_count", 100)
        image_ratio = getattr(profile, "image_ratio", 0)
        has_toc = getattr(profile, "has_toc", False)

        # Parser 选择
        if image_ratio > 3:
            plan.parser = "full"
        else:
            plan.parser = "columnar"

        # Layout 选择
        if image_ratio > 5:
            plan.layout = "ml"
        elif has_toc:
            plan.layout = "rule"
        else:
            plan.layout = "rule"

        # Workers
        if page_count > 500:
            plan.workers = 8
        elif page_count > 100:
            plan.workers = 4
        else:
            plan.workers = 2

        # Batch size
        if difficulty == "hard":
            plan.batch_size = 16
        elif difficulty == "extreme":
            plan.batch_size = 8
        else:
            plan.batch_size = 32

        # Quality
        if difficulty == "extreme":
            plan.quality_level = "high"
            plan.max_retries = 3
        elif difficulty == "hard":
            plan.quality_level = "normal"
            plan.max_retries = 2
        else:
            plan.quality_level = "normal"
            plan.max_retries = 1

        # Streaming
        plan.use_streaming = page_count > 50

        # Cache
        if page_count > 200:
            plan.cache_policy = "aggressive"
        else:
            plan.cache_policy = "normal"

        # Memory
        if page_count > 500:
            plan.memory_limit_mb = 8192
        else:
            plan.memory_limit_mb = 4096

        return plan
