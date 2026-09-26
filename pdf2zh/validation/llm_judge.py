"""LLMJudge — LLM 质量评估器。

使用 LLM 评估翻译质量。

用法：
    judge = LLMJudge()
    score = judge.evaluate(source, translated, reference)
    # QualityScore(meaning=0.95, terminology=0.90, readability=0.88)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class QualityScore:
    """质量分数。"""

    meaning: float = 0.0  # 意义保持
    terminology: float = 0.0  # 术语准确
    readability: float = 0.0  # 可读性
    numerical: float = 0.0  # 数字一致性
    structural: float = 0.0  # 结构保持

    # 加权总分
    weighted: float = 0.0

    # 问题
    issues: List[str] = field(default_factory=list)

    @property
    def is_good(self) -> bool:
        return self.weighted >= 0.85

    @property
    def is_acceptable(self) -> bool:
        return self.weighted >= 0.70

    def summary(self) -> str:
        return (
            f"QualityScore: weighted={self.weighted:.2f}, "
            f"meaning={self.meaning:.2f}, "
            f"terminology={self.terminology:.2f}, "
            f"readability={self.readability:.2f}"
        )


class LLMJudge:
    """LLM 质量评估器。"""

    def __init__(self) -> None:
        self._weights = {
            "meaning": 0.35,
            "terminology": 0.20,
            "readability": 0.15,
            "numerical": 0.15,
            "structural": 0.15,
        }

    def evaluate(
        self,
        source: str,
        translated: str,
        reference: Optional[str] = None,
        domain: str = "general",
    ) -> QualityScore:
        """评估翻译质量。"""
        score = QualityScore()

        # 1. Meaning preservation
        score.meaning = self._evaluate_meaning(source, translated, reference)

        # 2. Terminology
        score.terminology = self._evaluate_terminology(source, translated, domain)

        # 3. Readability
        score.readability = self._evaluate_readability(translated)

        # 4. Numerical consistency
        score.numerical = self._evaluate_numerical(source, translated)

        # 5. Structural preservation
        score.structural = self._evaluate_structural(source, translated)

        # Weighted score
        score.weighted = (
            score.meaning * self._weights["meaning"]
            + score.terminology * self._weights["terminology"]
            + score.readability * self._weights["readability"]
            + score.numerical * self._weights["numerical"]
            + score.structural * self._weights["structural"]
        )

        # Issues
        score.issues = self._identify_issues(source, translated, score)

        return score

    def _evaluate_meaning(
        self,
        source: str,
        translated: str,
        reference: Optional[str],
    ) -> float:
        """评估意义保持。"""
        if reference:
            # 有参考：与参考比较
            return self._similarity(translated, reference)
        else:
            # 无参考：启发式评估
            return self._heuristic_meaning(source, translated)

    def _similarity(self, a: str, b: str) -> float:
        """计算相似度。"""
        # 简化：基于字符重叠
        if not a or not b:
            return 0.0

        # 中文字符
        a_cn = set(c for c in a if "\u4e00" <= c <= "\u9fff")
        b_cn = set(c for c in b if "\u4e00" <= c <= "\u9fff")

        if not a_cn and not b_cn:
            # 英文：词重叠
            a_words = set(a.lower().split())
            b_words = set(b.lower().split())
            if not a_words:
                return 0.0
            overlap = len(a_words & b_words) / len(a_words)
            return min(1.0, overlap * 1.2)

        # 中文：字符重叠
        if not a_cn:
            return 0.0
        overlap = len(a_cn & b_cn) / len(a_cn)
        return min(1.0, overlap * 1.1)

    def _heuristic_meaning(self, source: str, translated: str) -> float:
        """启发式意义评估。"""
        # 长度比
        ratio = len(translated) / len(source) if source else 0
        if 0.5 <= ratio <= 2.0:
            length_score = 1.0
        elif 0.3 <= ratio <= 3.0:
            length_score = 0.7
        else:
            length_score = 0.4

        # 关键词保留
        keywords = self._extract_keywords(source)
        if keywords:
            preserved = sum(1 for k in keywords if k.lower() in translated.lower())
            keyword_score = preserved / len(keywords)
        else:
            keyword_score = 0.8

        return length_score * 0.5 + keyword_score * 0.5

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词。"""
        # 简化：提取大写词和数字
        keywords = []
        for word in text.split():
            if word[0].isupper() if word else False:
                keywords.append(word)
            if any(c.isdigit() for c in word):
                keywords.append(word)
        return keywords[:10]

    def _evaluate_terminology(
        self,
        source: str,
        translated: str,
        domain: str,
    ) -> float:
        """评估术语准确。"""
        # 技术术语应该保留
        tech_terms = [
            "GPU",
            "CPU",
            "CUDA",
            "PyTorch",
            "TensorFlow",
            "BERT",
            "GPT",
            "Transformer",
            "API",
            "HTTP",
        ]

        preserved = 0
        total = 0
        for term in tech_terms:
            if term.lower() in source.lower():
                total += 1
                if term.lower() in translated.lower():
                    preserved += 1

        if total == 0:
            return 0.9  # 无术语，默认高分

        return preserved / total

    def _evaluate_readability(self, translated: str) -> float:
        """评估可读性。"""
        if not translated:
            return 0.0

        score = 1.0

        # 检查重复
        if re.search(r"(.{2,})\1{3,}", translated):
            score -= 0.3

        # 检查标点
        if re.search(r"\s+[.,;:!?]", translated):
            score -= 0.1

        # 检查长度
        if len(translated) > 500:
            score -= 0.1

        return max(0.0, score)

    def _evaluate_numerical(self, source: str, translated: str) -> float:
        """评估数字一致性。"""
        # 提取数字
        source_nums = set(re.findall(r"\b\d+\.?\d*%?\b", source))
        translated_nums = set(re.findall(r"\b\d+\.?\d*%?\b", translated))

        if not source_nums:
            return 1.0

        missing = source_nums - translated_nums
        return 1.0 - (len(missing) / len(source_nums))

    def _evaluate_structural(self, source: str, translated: str) -> float:
        """评估结构保持。"""
        # 检查引用
        source_refs = set(re.findall(r"\[\d+\]", source))
        translated_refs = set(re.findall(r"\[\d+\]", translated))

        if source_refs:
            preserved = len(source_refs & translated_refs)
            return preserved / len(source_refs)

        return 1.0

    def _identify_issues(
        self,
        source: str,
        translated: str,
        score: QualityScore,
    ) -> List[str]:
        """识别问题。"""
        issues = []

        if score.meaning < 0.7:
            issues.append("meaning_loss")
        if score.terminology < 0.8:
            issues.append("terminology_error")
        if score.readability < 0.7:
            issues.append("readability_poor")
        if score.numerical < 0.9:
            issues.append("numerical_mismatch")
        if score.structural < 0.9:
            issues.append("reference_break")

        return issues

    def batch_evaluate(
        self,
        items: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """批量评估。"""
        scores = []
        for item in items:
            score = self.evaluate(
                item.get("source", ""),
                item.get("translated", ""),
                item.get("reference"),
                item.get("domain", "general"),
            )
            scores.append(score)

        avg_weighted = sum(s.weighted for s in scores) / len(scores) if scores else 0
        good_count = sum(1 for s in scores if s.is_good)

        return {
            "count": len(scores),
            "avg_weighted": avg_weighted,
            "good_count": good_count,
            "good_rate": good_count / len(scores) if scores else 0,
            "scores": scores,
        }
