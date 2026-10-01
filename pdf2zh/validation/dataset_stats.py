"""DatasetStatistics — 数据集统计。

分析数据集分布、覆盖率、质量。

用法：
    stats = DatasetStatistics(dataset)
    print(stats.summary())
    # Quality Dataset:
    #   Samples: 1200
    #   Domains: academic=35%, technical=30%, ...
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DomainStats:
    """领域统计。"""

    name: str = ""
    count: int = 0
    percentage: float = 0.0
    avg_difficulty: float = 0.0


@dataclass
class ErrorStats:
    """错误统计。"""

    error_type: str = ""
    count: int = 0
    percentage: float = 0.0
    severity_dist: Dict[str, int] = field(default_factory=dict)


@dataclass
class DatasetStatistics:
    """数据集统计。"""

    # 基本信息
    total_samples: int = 0
    total_documents: int = 0

    # 领域分布
    domain_stats: List[DomainStats] = field(default_factory=list)

    # 难度分布
    difficulty_dist: Dict[str, int] = field(default_factory=dict)

    # 错误统计
    error_stats: List[ErrorStats] = field(default_factory=list)
    total_errors: int = 0

    # 质量分数
    avg_quality_score: float = 0.0
    quality_distribution: Dict[str, int] = field(default_factory=dict)

    # 标注统计
    annotated_count: int = 0
    annotator_count: int = 0
    avg_agreement: float = 0.0

    def summary(self) -> str:
        lines = [
            "Quality Dataset Statistics",
            "=" * 60,
            f"Total samples: {self.total_samples}",
            f"Total documents: {self.total_documents}",
            "",
            "Domain Distribution:",
        ]

        for domain in sorted(self.domain_stats, key=lambda x: -x.count):
            lines.append(f"  {domain.name}: {domain.count} ({domain.percentage:.1f}%)")

        lines.append("")
        lines.append("Difficulty Distribution:")
        for diff, count in sorted(self.difficulty_dist.items()):
            lines.append(f"  {diff}: {count}")

        if self.error_stats:
            lines.append("")
            lines.append("Error Distribution:")
            for error in sorted(self.error_stats, key=lambda x: -x.count):
                lines.append(f"  {error.error_type}: {error.count}")

        lines.append("")
        lines.append("Quality:")
        lines.append(f"  Average score: {self.avg_quality_score:.2f}")
        for level, count in self.quality_distribution.items():
            lines.append(f"  {level}: {count}")

        if self.annotated_count > 0:
            lines.append("")
            lines.append("Annotation:")
            lines.append(f"  Annotated: {self.annotated_count}/{self.total_samples}")
            lines.append(f"  Annotators: {self.annotator_count}")
            lines.append(f"  Avg agreement: {self.avg_agreement:.2f}")

        return "\n".join(lines)


class StatisticsCalculator:
    """统计计算器。"""

    def calculate(self, entries: List[Dict[str, Any]]) -> DatasetStatistics:
        """计算数据集统计。"""
        stats = DatasetStatistics()
        stats.total_samples = len(entries)

        if not entries:
            return stats

        # 领域统计
        domain_counts = Counter(e.get("domain", "general") for e in entries)
        total = len(entries)
        for domain, count in domain_counts.most_common():
            stats.domain_stats.append(
                DomainStats(
                    name=domain,
                    count=count,
                    percentage=count / total * 100,
                )
            )

        # 难度统计
        stats.difficulty_dist = dict(
            Counter(e.get("difficulty", "unknown") for e in entries)
        )

        # 错误统计
        all_errors = []
        for e in entries:
            all_errors.extend(e.get("errors", []))

        stats.total_errors = len(all_errors)
        error_counts = Counter(err.get("error_type", "unknown") for err in all_errors)
        for error_type, count in error_counts.most_common():
            severity_dist = Counter(
                err.get("severity", "medium")
                for err in all_errors
                if err.get("error_type") == error_type
            )
            stats.error_stats.append(
                ErrorStats(
                    error_type=error_type,
                    count=count,
                    percentage=count / len(all_errors) * 100 if all_errors else 0,
                    severity_dist=dict(severity_dist),
                )
            )

        # 质量分数
        scores = [e.get("quality_score", 0) for e in entries if "quality_score" in e]
        if scores:
            stats.avg_quality_score = sum(scores) / len(scores)

        # 标注统计
        annotated = [e for e in entries if e.get("annotated", False)]
        stats.annotated_count = len(annotated)
        annotators = set(e.get("annotator_id", "") for e in annotated)
        stats.annotator_count = len(annotators)

        return stats
