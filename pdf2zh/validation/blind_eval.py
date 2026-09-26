"""BlindEvaluator — 盲评评估器。

不看 reference，只比较 source → translation 的质量。

用法：
    evaluator = BlindEvaluator()
    result = evaluator.evaluate(source, translation_a, translation_b)
    # BlindResult(winner="A", score_a=0.85, score_b=0.72)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BlindResult:
    """盲评结果。"""

    winner: str = ""  # "A", "B", or "tie"
    score_a: float = 0.0
    score_b: float = 0.0
    confidence: float = 0.0
    reasoning: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_clear(self) -> bool:
        return abs(self.score_a - self.score_b) > 0.15


@dataclass
class PairwiseComparison:
    """成对比较。"""

    source: str = ""
    translation_a: str = ""
    translation_b: str = ""
    label_a: str = "A"
    label_b: str = "B"
    result: Optional[BlindResult] = None


class BlindEvaluator:
    """盲评评估器。"""

    def __init__(self) -> None:
        self._comparisons: List[PairwiseComparison] = []

    def evaluate(
        self,
        source: str,
        translation_a: str,
        translation_b: str,
        shuffle: bool = True,
    ) -> BlindResult:
        """盲评。"""
        # 随机打乱顺序
        if shuffle:
            if random.random() > 0.5:
                translation_a, translation_b = translation_b, translation_a
                flipped = True
            else:
                flipped = False
        else:
            flipped = False

        # 评估
        score_a = self._score_translation(source, translation_a)
        score_b = self._score_translation(source, translation_b)

        # 判断赢家
        if abs(score_a - score_b) < 0.05:
            winner = "tie"
        elif score_a > score_b:
            winner = "A"
        else:
            winner = "B"

        # 如果打乱了，还原
        if flipped:
            winner = {"A": "B", "B": "A", "tie": "tie"}[winner]

        result = BlindResult(
            winner=winner,
            score_a=score_a if not flipped else score_b,
            score_b=score_b if not flipped else score_a,
            confidence=abs(score_a - score_b),
        )

        # 保存比较
        comparison = PairwiseComparison(
            source=source,
            translation_a=translation_a,
            translation_b=translation_b,
            result=result,
        )
        self._comparisons.append(comparison)

        return result

    def _score_translation(
        self,
        source: str,
        translation: str,
    ) -> float:
        """评分翻译质量。"""
        if not translation:
            return 0.0

        score = 0.5  # 基础分

        # 长度比
        ratio = len(translation) / len(source) if source else 0
        if 0.5 <= ratio <= 2.0:
            score += 0.2
        elif 0.3 <= ratio <= 3.0:
            score += 0.1

        # 关键词保留
        keywords = self._extract_keywords(source)
        if keywords:
            preserved = sum(1 for k in keywords if k.lower() in translation.lower())
            score += (preserved / len(keywords)) * 0.2

        # 流畅度（简化）
        if len(translation) > 10:
            score += 0.1

        return min(1.0, score)

    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词。"""
        keywords = []
        for word in text.split():
            if word[0].isupper() if word else False:
                keywords.append(word)
            if any(c.isdigit() for c in word):
                keywords.append(word)
        return keywords[:10]

    def batch_evaluate(
        self,
        pairs: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """批量盲评。"""
        results = []
        for pair in pairs:
            result = self.evaluate(
                pair.get("source", ""),
                pair.get("translation_a", ""),
                pair.get("translation_b", ""),
            )
            results.append(result)

        # 统计
        a_wins = sum(1 for r in results if r.winner == "A")
        b_wins = sum(1 for r in results if r.winner == "B")
        ties = sum(1 for r in results if r.winner == "tie")

        return {
            "count": len(results),
            "a_wins": a_wins,
            "b_wins": b_wins,
            "ties": ties,
            "results": results,
        }

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计。"""
        if not self._comparisons:
            return {"total": 0}

        results = [c.result for c in self._comparisons if c.result]
        a_wins = sum(1 for r in results if r.winner == "A")
        b_wins = sum(1 for r in results if r.winner == "B")

        return {
            "total": len(self._comparisons),
            "a_wins": a_wins,
            "b_wins": b_wins,
            "win_rate_a": a_wins / len(results) if results else 0,
        }
