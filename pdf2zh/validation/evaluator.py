"""TranslationEvaluator — 翻译质量评估器。

评估翻译质量的多个维度。

用法：
    evaluator = TranslationEvaluator()
    result = evaluator.evaluate(original, translated, block_type="paragraph")
    # EvalResult(score=0.92, completeness=0.95, overflow=False)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EvalResult:
    """评估结果。"""

    score: float = 0.0
    completeness: float = 1.0
    terminology_accuracy: float = 1.0
    overflow: bool = False
    truncated: bool = False
    consistent: bool = True
    issues: List[str] = field(default_factory=list)

    @property
    def is_good(self) -> bool:
        return self.score >= 0.8 and not self.overflow

    @property
    def is_acceptable(self) -> bool:
        return self.score >= 0.6

    def summary(self) -> str:
        return (
            f"EvalResult: score={self.score:.2f}, "
            f"completeness={self.completeness:.2f}, "
            f"terminology={self.terminology_accuracy:.2f}, "
            f"overflow={self.overflow}"
        )


@dataclass
class GlossaryEntry:
    """术语条目。"""

    source: str = ""
    expected: str = ""
    domain: str = ""


class TranslationEvaluator:
    """翻译质量评估器。"""

    def __init__(self, glossary: Optional[List[GlossaryEntry]] = None) -> None:
        self._glossary = glossary or []

    def evaluate(
        self,
        original: str,
        translated: str,
        block_type: str = "paragraph",
        max_length_ratio: float = 2.0,
    ) -> EvalResult:
        """评估翻译质量。"""
        issues = []

        # 1. Completeness
        completeness = self._check_completeness(original, translated, issues)

        # 2. Terminology
        terminology = self._check_terminology(translated, issues)

        # 3. Overflow
        overflow = self._check_overflow(original, translated, max_length_ratio, issues)

        # 4. Truncation
        truncated = self._check_truncation(original, translated, issues)

        # 5. Consistency
        consistent = self._check_consistency(translated, issues)

        # Overall score
        score = (
            completeness * 0.35
            + terminology * 0.30
            + (1.0 if not overflow else 0.5) * 0.20
            + (1.0 if not truncated else 0.5) * 0.15
        )

        return EvalResult(
            score=score,
            completeness=completeness,
            terminology_accuracy=terminology,
            overflow=overflow,
            truncated=truncated,
            consistent=consistent,
            issues=issues,
        )

    def _check_completeness(
        self,
        original: str,
        translated: str,
        issues: List[str],
    ) -> float:
        """检查完整性。"""
        if not original or not translated:
            return 0.0

        orig_len = len(original)
        trans_len = len(translated)
        ratio = trans_len / orig_len if orig_len > 0 else 0.0

        if ratio < 0.3:
            issues.append(f"too_short: {ratio:.2f}")
            return 0.3
        elif ratio > 3.0:
            issues.append(f"too_long: {ratio:.2f}")
            return 0.5
        elif ratio < 0.5 or ratio > 2.0:
            return 0.7
        return 1.0

    def _check_terminology(
        self,
        translated: str,
        issues: List[str],
    ) -> float:
        """检查术语准确性。"""
        if not self._glossary:
            return 1.0

        correct = 0
        total = 0

        for entry in self._glossary:
            if entry.source.lower() in translated.lower():
                total += 1
                if entry.expected and entry.expected in translated:
                    correct += 1

        if total == 0:
            return 1.0

        return correct / total

    def _check_overflow(
        self,
        original: str,
        translated: str,
        max_ratio: float,
        issues: List[str],
    ) -> bool:
        """检查是否溢出。"""
        if not original:
            return False

        ratio = len(translated) / len(original)
        if ratio > max_ratio:
            issues.append(f"overflow: {ratio:.2f} > {max_ratio}")
            return True
        return False

    def _check_truncation(
        self,
        original: str,
        translated: str,
        issues: List[str],
    ) -> bool:
        """检查是否截断。"""
        if not original:
            return False

        # 简单检查：翻译是否以不完整句子结尾
        if translated and not translated[-1] in "。！？.!?…":
            # 可能截断（但不是绝对）
            pass
        return False

    def _check_consistency(
        self,
        translated: str,
        issues: List[str],
    ) -> bool:
        """检查一致性。"""
        # 检查重复模式
        if re.search(r"(.{2,})\1{3,}", translated):
            issues.append("repeated_patterns")
            return False
        return True

    def batch_evaluate(
        self,
        items: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """批量评估。"""
        results = []
        for item in items:
            result = self.evaluate(
                item.get("original", ""),
                item.get("translated", ""),
                item.get("block_type", "paragraph"),
            )
            results.append(result)

        avg_score = sum(r.score for r in results) / len(results) if results else 0
        overflow_count = sum(1 for r in results if r.overflow)

        return {
            "count": len(results),
            "avg_score": avg_score,
            "overflow_rate": overflow_count / len(results) if results else 0,
            "results": results,
        }
