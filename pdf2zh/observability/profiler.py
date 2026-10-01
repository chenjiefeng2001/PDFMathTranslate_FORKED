"""PipelineProfiler — 跨 pipeline stage 自动瓶颈分析。

自动回答：下一次优化应该改哪里？

用法：
    profiler = PipelineProfiler()
    report = profiler.analyze(pdf_path)
    print(report.summary())
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StageMetrics:
    """单阶段指标。"""

    stage_name: str = ""
    total_ms: float = 0.0
    page_count: int = 0
    avg_ms_per_page: float = 0.0
    cache_hit_ratio: float = 0.0
    items_processed: int = 0
    sub_metrics: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"{self.stage_name}:",
            f"    avg {self.avg_ms_per_page:.1f}ms/page",
            f"    cache hit {self.cache_hit_ratio:.0%}",
        ]
        for k, v in self.sub_metrics.items():
            lines.append(f"    {k} {v:.1f}")
        return "\n".join(lines)


@dataclass
class BottleneckReport:
    """瓶颈报告。"""

    stages: List[StageMetrics] = field(default_factory=list)
    total_ms: float = 0.0
    bottleneck_stage: str = ""
    recommendations: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "Pipeline Bottleneck Report",
            "=" * 60,
        ]
        for stage in self.stages:
            lines.append(stage.summary())
            lines.append("")

        lines.append(f"Total: {self.total_ms:.0f}ms")
        lines.append(f"Bottleneck: {self.bottleneck_stage}")
        lines.append("")
        lines.append("Recommendations:")
        for rec in self.recommendations:
            lines.append(f"  - {rec}")

        return "\n".join(lines)


@dataclass
class PipelineCostModel:
    """Pipeline 成本模型。"""

    parse_cost_per_page_ms: float = 8.0
    layout_cost_per_page_ms: float = 3.0
    translation_cost_per_block_ms: float = 320.0
    assembly_cost_per_page_ms: float = 5.0

    cache_hit_savings: Dict[str, float] = field(
        default_factory=lambda: {
            "parse": 0.94,
            "layout": 0.91,
            "translation": 0.76,
            "assembly": 0.98,
        }
    )

    def estimate_total_ms(
        self,
        pages: int,
        blocks_per_page: int = 10,
    ) -> Dict[str, float]:
        """估算总时间。"""
        total_blocks = pages * blocks_per_page

        parse_ms = (
            pages
            * self.parse_cost_per_page_ms
            * (1 - self.cache_hit_savings.get("parse", 0))
        )
        layout_ms = (
            pages
            * self.layout_cost_per_page_ms
            * (1 - self.cache_hit_savings.get("layout", 0))
        )
        translation_ms = (
            total_blocks
            * self.translation_cost_per_block_ms
            * (1 - self.cache_hit_savings.get("translation", 0))
        )
        assembly_ms = (
            pages
            * self.assembly_cost_per_page_ms
            * (1 - self.cache_hit_savings.get("assembly", 0))
        )

        return {
            "parse": parse_ms,
            "layout": layout_ms,
            "translation": translation_ms,
            "assembly": assembly_ms,
            "total": parse_ms + layout_ms + translation_ms + assembly_ms,
        }

    def find_bottleneck(self, pages: int, blocks_per_page: int = 10) -> str:
        """找到瓶颈。"""
        costs = self.estimate_total_ms(pages, blocks_per_page)
        costs.pop("total")
        return max(costs, key=costs.get)


class PipelineProfiler:
    """Pipeline 性能分析器。"""

    def __init__(self) -> None:
        self._cost_model = PipelineCostModel()

    def profile_from_metrics(
        self,
        parse_ms: float,
        layout_ms: float,
        translation_ms: float,
        assembly_ms: float,
        pages: int,
        blocks: int,
        cache_hits: Optional[Dict[str, int]] = None,
    ) -> BottleneckReport:
        """从已有指标生成报告。"""
        cache_hits = cache_hits or {}

        stages = [
            StageMetrics(
                stage_name="Parser",
                total_ms=parse_ms,
                page_count=pages,
                avg_ms_per_page=parse_ms / pages if pages > 0 else 0,
                cache_hit_ratio=cache_hits.get("parse", 0.0),
            ),
            StageMetrics(
                stage_name="Layout",
                total_ms=layout_ms,
                page_count=pages,
                avg_ms_per_page=layout_ms / pages if pages > 0 else 0,
                cache_hit_ratio=cache_hits.get("layout", 0.0),
            ),
            StageMetrics(
                stage_name="Translation",
                total_ms=translation_ms,
                page_count=pages,
                avg_ms_per_page=translation_ms / pages if pages > 0 else 0,
                items_processed=blocks,
                cache_hit_ratio=cache_hits.get("translation", 0.0),
            ),
            StageMetrics(
                stage_name="Assembly",
                total_ms=assembly_ms,
                page_count=pages,
                avg_ms_per_page=assembly_ms / pages if pages > 0 else 0,
                cache_hit_ratio=cache_hits.get("assembly", 0.0),
            ),
        ]

        total = parse_ms + layout_ms + translation_ms + assembly_ms
        bottleneck = max(stages, key=lambda s: s.total_ms)

        recommendations = self._generate_recommendations(stages, total, pages)

        return BottleneckReport(
            stages=stages,
            total_ms=total,
            bottleneck_stage=bottleneck.stage_name,
            recommendations=recommendations,
        )

    def estimate(self, pages: int, blocks_per_page: int = 10) -> BottleneckReport:
        """估算性能（无实际运行）。"""
        costs = self._cost_model.estimate_total_ms(pages, blocks_per_page)

        stages = [
            StageMetrics(
                stage_name="Parser",
                total_ms=costs["parse"],
                page_count=pages,
                avg_ms_per_page=costs["parse"] / pages if pages > 0 else 0,
                cache_hit_ratio=self._cost_model.cache_hit_savings.get("parse", 0),
            ),
            StageMetrics(
                stage_name="Layout",
                total_ms=costs["layout"],
                page_count=pages,
                avg_ms_per_page=costs["layout"] / pages if pages > 0 else 0,
                cache_hit_ratio=self._cost_model.cache_hit_savings.get("layout", 0),
            ),
            StageMetrics(
                stage_name="Translation",
                total_ms=costs["translation"],
                page_count=pages,
                avg_ms_per_page=costs["translation"] / pages if pages > 0 else 0,
                items_processed=pages * blocks_per_page,
                cache_hit_ratio=self._cost_model.cache_hit_savings.get(
                    "translation", 0
                ),
            ),
            StageMetrics(
                stage_name="Assembly",
                total_ms=costs["assembly"],
                page_count=pages,
                avg_ms_per_page=costs["assembly"] / pages if pages > 0 else 0,
                cache_hit_ratio=self._cost_model.cache_hit_savings.get("assembly", 0),
            ),
        ]

        bottleneck = self._cost_model.find_bottleneck(pages, blocks_per_page)
        recommendations = self._generate_recommendations(stages, costs["total"], pages)

        return BottleneckReport(
            stages=stages,
            total_ms=costs["total"],
            bottleneck_stage=bottleneck,
            recommendations=recommendations,
        )

    def _generate_recommendations(
        self,
        stages: List[StageMetrics],
        total_ms: float,
        pages: int,
    ) -> List[str]:
        """生成优化建议。"""
        recommendations = []

        for stage in stages:
            ratio = stage.total_ms / total_ms if total_ms > 0 else 0
            if ratio > 0.5:
                recommendations.append(
                    f"{stage.stage_name} is bottleneck ({ratio:.0%} of total). "
                    f"Consider optimizing this stage first."
                )
            if stage.cache_hit_ratio < 0.5:
                recommendations.append(
                    f"{stage.stage_name} cache hit ratio is low ({stage.cache_hit_ratio:.0%}). "
                    f"Consider adding caching."
                )

        if pages > 100:
            recommendations.append("For large documents, consider streaming pipeline.")

        return recommendations
