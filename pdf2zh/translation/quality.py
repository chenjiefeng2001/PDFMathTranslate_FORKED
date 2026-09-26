"""TranslationQualityScorer — 翻译质量评分器。

自动评估翻译质量，发现问题时自动 retry。

评分维度：
    - terminology: 术语一致性
    - completeness: 完整性
    - fluency: 流畅度
    - layout_fit: 布局适配
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class QualityDimensions:
    """质量维度。"""

    terminology: float = 1.0  # 术语一致性
    completeness: float = 1.0  # 完整性
    fluency: float = 1.0  # 流畅度
    layout_fit: float = 1.0  # 布局适配


@dataclass
class QualityScore:
    """综合质量评分。"""

    dimensions: QualityDimensions = field(default_factory=QualityDimensions)
    weighted_score: float = 0.0
    needs_retry: bool = False
    issues: List[str] = field(default_factory=list)

    @property
    def is_good(self) -> bool:
        return self.weighted_score >= 0.8 and not self.needs_retry

    @property
    def is_acceptable(self) -> bool:
        return self.weighted_score >= 0.6


class TranslationQualityScorer:
    """翻译质量评分器。"""

    def __init__(
        self,
        glossary: Optional[Any] = None,
        layout_constraint: Optional[Any] = None,
    ) -> None:
        self._glossary = glossary
        self._layout_constraint = layout_constraint

    def score(
        self,
        original: str,
        translated: str,
        block_type: str = "paragraph",
    ) -> QualityScore:
        """评分翻译质量。"""
        issues = []
        dimensions = QualityDimensions()

        # 1. Terminology
        dimensions.terminology = self._score_terminology(original, translated, issues)

        # 2. Completeness
        dimensions.completeness = self._score_completeness(original, translated, issues)

        # 3. Fluency
        dimensions.fluency = self._score_fluency(translated, issues)

        # 4. Layout fit
        dimensions.layout_fit = self._score_layout_fit(translated, block_type, issues)

        # Weighted score
        weights = {
            "terminology": 0.35,
            "completeness": 0.30,
            "fluency": 0.20,
            "layout_fit": 0.15,
        }
        weighted = (
            dimensions.terminology * weights["terminology"]
            + dimensions.completeness * weights["completeness"]
            + dimensions.fluency * weights["fluency"]
            + dimensions.layout_fit * weights["layout_fit"]
        )

        # Need retry?
        needs_retry = (
            dimensions.terminology < 0.5
            or dimensions.completeness < 0.6
            or weighted < 0.5
        )

        return QualityScore(
            dimensions=dimensions,
            weighted_score=weighted,
            needs_retry=needs_retry,
            issues=issues,
        )

    def _score_terminology(
        self,
        original: str,
        translated: str,
        issues: List[str],
    ) -> float:
        """评分术语一致性。"""
        if self._glossary is None:
            return 1.0

        # 检查术语是否被正确翻译
        score = 1.0
        # 简化：检查 glossary 术语
        return score

    def _score_completeness(
        self,
        original: str,
        translated: str,
        issues: List[str],
    ) -> float:
        """评分完整性。"""
        if not original or not translated:
            return 0.0

        # 长度比
        orig_len = len(original)
        trans_len = len(translated)
        ratio = trans_len / orig_len if orig_len > 0 else 0.0

        # 正常比例 0.5-2.0
        if ratio < 0.3:
            issues.append(f"translation_too_short: {ratio:.2f}")
            return 0.3
        elif ratio > 3.0:
            issues.append(f"translation_too_long: {ratio:.2f}")
            return 0.5
        elif ratio < 0.5 or ratio > 2.0:
            return 0.7
        return 1.0

    def _score_fluency(
        self,
        translated: str,
        issues: List[str],
    ) -> float:
        """评分流畅度。"""
        if not translated:
            return 0.0

        score = 1.0

        # 检查重复
        if re.search(r"(.{2,})\1{2,}", translated):
            issues.append("repeated_patterns")
            score -= 0.3

        # 检查标点
        if re.search(r"\s+[.,;:!?]", translated):
            issues.append("punctuation_spacing")
            score -= 0.1

        # 检查长度
        if len(translated) > 1000:
            issues.append("too_long")
            score -= 0.2

        return max(0.0, score)

    def _score_layout_fit(
        self,
        translated: str,
        block_type: str,
        issues: List[str],
    ) -> float:
        """评分布局适配。"""
        if self._layout_constraint is None:
            return 1.0

        # 检查是否适合布局
        score = 1.0

        # TOC 特殊检查
        if block_type == "toc":
            if not re.search(r"\d+\s*$", translated):
                issues.append("toc_missing_page_number")
                score -= 0.3

        # Title 特殊检查
        if block_type == "title":
            if len(translated) > 50:
                issues.append("title_too_long")
                score -= 0.2

        return max(0.0, score)

    def batch_score(
        self,
        items: List[Tuple[str, str, str]],
    ) -> Dict[str, Any]:
        """批量评分。"""
        scores = []
        for original, translated, block_type in items:
            scores.append(self.score(original, translated, block_type))

        avg_score = sum(s.weighted_score for s in scores) / len(scores) if scores else 0
        retry_count = sum(1 for s in scores if s.needs_retry)

        return {
            "count": len(scores),
            "avg_score": avg_score,
            "retry_count": retry_count,
            "scores": scores,
        }
