"""RuntimeOptimizer — 运行时决策引擎。

自动决定每页走哪条 pipeline 路径。

用法：
    optimizer = RuntimeOptimizer()
    decision = optimizer.choose(snapshot)
    # PipelinePlan(parser="columnar_fast", layout="rule", translator="fast")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PageFeatures:
    """页面特征。"""

    text_ratio: float = 0.0
    image_count: int = 0
    block_count: int = 0
    repeated_ratio: float = 0.0
    language: str = "en"
    has_formula: bool = False
    has_table: bool = False
    has_toc: bool = False
    font_count: int = 0
    avg_font_size: float = 10.0

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> PageFeatures:
        """从 snapshot 提取特征。"""
        return cls(
            text_ratio=getattr(snapshot, "text_ratio", 0.9),
            image_count=getattr(snapshot, "image_count", 0),
            block_count=getattr(snapshot, "block_count", 10),
            repeated_ratio=getattr(snapshot, "repeated_ratio", 0.0),
            language=getattr(snapshot, "language", "en"),
        )


@dataclass
class PipelinePlan:
    """Pipeline 计划。"""

    parser: str = "columnar"
    layout: str = "rule"
    translator: str = "default"
    cache_policy: str = "normal"
    skip_translation: bool = False
    estimated_cost_ms: float = 0.0
    expected_quality: float = 0.95
    reason: str = ""

    def summary(self) -> str:
        return (
            f"PipelinePlan: parser={self.parser}, layout={self.layout}, "
            f"translator={self.translator}, cache={self.cache_policy} "
            f"[{self.reason}]"
        )


class RuntimeOptimizer:
    """运行时优化器。"""

    def __init__(self) -> None:
        self._rules = self._build_rules()

    def _build_rules(self) -> List[Dict]:
        """构建决策规则。"""
        return [
            # 纯文本快速路径
            {
                "condition": lambda f: (
                    f.text_ratio > 0.95
                    and f.image_count == 0
                    and f.repeated_ratio < 0.3
                ),
                "plan": PipelinePlan(
                    parser="columnar_fast",
                    layout="rule",
                    translator="fast",
                    cache_policy="aggressive",
                    reason="pure_text_fast_path",
                ),
            },
            # 重复内容跳过
            {
                "condition": lambda f: f.repeated_ratio > 0.8,
                "plan": PipelinePlan(
                    parser="columnar",
                    layout="skip",
                    translator="template",
                    cache_policy="aggressive",
                    skip_translation=True,
                    reason="repeated_content_skip",
                ),
            },
            # TOC 特殊处理
            {
                "condition": lambda f: f.has_toc,
                "plan": PipelinePlan(
                    parser="columnar",
                    layout="rule",
                    translator="toc_constrained",
                    cache_policy="normal",
                    reason="toc_structure_preserve",
                ),
            },
            # 公式页面
            {
                "condition": lambda f: f.has_formula and f.text_ratio < 0.3,
                "plan": PipelinePlan(
                    parser="full",
                    layout="rule",
                    translator="skip",
                    cache_policy="none",
                    skip_translation=True,
                    reason="formula_bypass",
                ),
            },
            # 图片密集
            {
                "condition": lambda f: f.image_count > 5,
                "plan": PipelinePlan(
                    parser="full",
                    layout="ml",
                    translator="default",
                    cache_policy="normal",
                    reason="image_heavy_layout",
                ),
            },
            # 表格页面
            {
                "condition": lambda f: f.has_table,
                "plan": PipelinePlan(
                    parser="columnar",
                    layout="rule",
                    translator="table_aware",
                    cache_policy="normal",
                    reason="table_preserve",
                ),
            },
            # 默认路径
            {
                "condition": lambda f: True,
                "plan": PipelinePlan(
                    parser="columnar",
                    layout="rule",
                    translator="default",
                    cache_policy="normal",
                    reason="default_path",
                ),
            },
        ]

    def choose(self, features: PageFeatures) -> PipelinePlan:
        """选择 pipeline 路径。"""
        for rule in self._rules:
            if rule["condition"](features):
                plan = rule["plan"]
                plan.estimated_cost_ms = self._estimate_cost(features, plan)
                return plan
        return PipelinePlan(reason="fallback")

    def choose_from_snapshot(self, snapshot: Any) -> PipelinePlan:
        """从 snapshot 选择路径。"""
        features = PageFeatures.from_snapshot(snapshot)
        return self.choose(features)

    def _estimate_cost(self, features: PageFeatures, plan: PipelinePlan) -> float:
        """估算成本。"""
        base_cost = 50.0  # ms

        # Parser cost
        if plan.parser == "columnar_fast":
            base_cost += 5.0
        elif plan.parser == "full":
            base_cost += 15.0

        # Layout cost
        if plan.layout == "skip":
            base_cost += 0.0
        elif plan.layout == "rule":
            base_cost += 3.0
        elif plan.layout == "ml":
            base_cost += 20.0

        # Translation cost
        if plan.skip_translation:
            base_cost += 0.0
        elif plan.translator == "fast":
            base_cost += features.block_count * 20.0
        elif plan.translator == "default":
            base_cost += features.block_count * 50.0
        elif plan.translator == "toc_constrained":
            base_cost += features.block_count * 30.0

        return base_cost

    def batch_plan(
        self,
        features_list: List[PageFeatures],
    ) -> Dict[str, int]:
        """批量规划，统计路径分布。"""
        path_counts: Dict[str, int] = {}
        for features in features_list:
            plan = self.choose(features)
            key = f"{plan.parser}/{plan.layout}/{plan.translator}"
            path_counts[key] = path_counts.get(key, 0) + 1
        return path_counts
