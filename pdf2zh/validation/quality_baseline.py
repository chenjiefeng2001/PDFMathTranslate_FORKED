"""QualityBaseline — 质量基线报告。

综合评估翻译质量，生成基线报告。

用法：
    baseline = QualityBaseline()
    report = baseline.evaluate(test_cases)
    print(report.summary())
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pdf2zh.validation.llm_judge import LLMJudge, QualityScore
from pdf2zh.validation.reference_corpus import ReferenceCorpus
from pdf2zh.validation.semantic_errors import SemanticError, SemanticErrorTaxonomy


@dataclass
class QualityBaselineReport:
    """质量基线报告。"""

    timestamp: float = 0.0
    total_cases: int = 0

    # 质量分数
    avg_weighted: float = 0.0
    avg_meaning: float = 0.0
    avg_terminology: float = 0.0
    avg_readability: float = 0.0
    avg_numerical: float = 0.0
    avg_structural: float = 0.0

    # 通过率
    good_rate: float = 0.0  # >= 0.85
    acceptable_rate: float = 0.0  # >= 0.70

    # 错误统计
    total_errors: int = 0
    error_by_type: Dict[str, int] = field(default_factory=dict)
    error_by_severity: Dict[str, int] = field(default_factory=dict)

    # 按领域
    domain_scores: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            "Quality Baseline Report",
            "=" * 60,
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.timestamp))}",
            f"Total cases: {self.total_cases}",
            "",
            "Quality Scores:",
            f"  Weighted:   {self.avg_weighted:.2f}",
            f"  Meaning:    {self.avg_meaning:.2f}",
            f"  Terminology: {self.avg_terminology:.2f}",
            f"  Readability: {self.avg_readability:.2f}",
            f"  Numerical:  {self.avg_numerical:.2f}",
            f"  Structural: {self.avg_structural:.2f}",
            "",
            "Pass Rates:",
            f"  Good (>=0.85):     {self.good_rate:.1%}",
            f"  Acceptable (>=0.70): {self.acceptable_rate:.1%}",
            "",
            f"Errors: {self.total_errors}",
        ]

        if self.error_by_type:
            lines.append("  By Type:")
            for t, count in sorted(self.error_by_type.items(), key=lambda x: -x[1]):
                lines.append(f"    {t}: {count}")

        if self.domain_scores:
            lines.append("")
            lines.append("By Domain:")
            for domain, score in sorted(self.domain_scores.items()):
                lines.append(f"  {domain}: {score:.2f}")

        # Overall assessment
        lines.append("")
        if self.avg_weighted >= 0.85:
            lines.append("Assessment: GOOD - Production ready")
        elif self.avg_weighted >= 0.70:
            lines.append("Assessment: ACCEPTABLE - Needs improvement")
        else:
            lines.append("Assessment: POOR - Significant issues")

        return "\n".join(lines)


class QualityBaseline:
    """质量基线评估器。"""

    def __init__(self) -> None:
        self._judge = LLMJudge()
        self._taxonomy = SemanticErrorTaxonomy()
        self._corpus = ReferenceCorpus()

    def evaluate(
        self,
        test_cases: Optional[List[Dict[str, str]]] = None,
    ) -> QualityBaselineReport:
        """评估质量基线。"""
        report = QualityBaselineReport(timestamp=time.time())

        # 使用参考语料库
        if test_cases is None:
            pairs = self._corpus.get_pairs()
            test_cases = [
                {
                    "source": p.source,
                    "translated": p.reference,  # 模拟：用参考作为翻译
                    "reference": p.reference,
                    "domain": p.domain,
                }
                for p in pairs
            ]

        report.total_cases = len(test_cases)

        # 评估每个案例
        all_scores = []
        all_errors = []
        domain_scores: Dict[str, List[float]] = {}

        for case in test_cases:
            # 质量评分
            score = self._judge.evaluate(
                case.get("source", ""),
                case.get("translated", ""),
                case.get("reference"),
                case.get("domain", "general"),
            )
            all_scores.append(score)

            # 按领域统计
            domain = case.get("domain", "general")
            if domain not in domain_scores:
                domain_scores[domain] = []
            domain_scores[domain].append(score.weighted)

            # 错误分类
            errors = self._taxonomy.classify(
                case.get("source", ""),
                case.get("translated", ""),
                case.get("reference"),
            )
            all_errors.extend(errors)

        # 计算平均分
        if all_scores:
            report.avg_weighted = sum(s.weighted for s in all_scores) / len(all_scores)
            report.avg_meaning = sum(s.meaning for s in all_scores) / len(all_scores)
            report.avg_terminology = sum(s.terminology for s in all_scores) / len(
                all_scores
            )
            report.avg_readability = sum(s.readability for s in all_scores) / len(
                all_scores
            )
            report.avg_numerical = sum(s.numerical for s in all_scores) / len(
                all_scores
            )
            report.avg_structural = sum(s.structural for s in all_scores) / len(
                all_scores
            )

            # 通过率
            report.good_rate = sum(1 for s in all_scores if s.is_good) / len(all_scores)
            report.acceptable_rate = sum(
                1 for s in all_scores if s.is_acceptable
            ) / len(all_scores)

        # 错误统计
        report.total_errors = len(all_errors)
        for error in all_errors:
            t = error.type.value
            report.error_by_type[t] = report.error_by_type.get(t, 0) + 1
            sev = error.severity.value
            report.error_by_severity[sev] = report.error_by_severity.get(sev, 0) + 1

        # 领域分数
        for domain, scores in domain_scores.items():
            report.domain_scores[domain] = sum(scores) / len(scores) if scores else 0

        return report
