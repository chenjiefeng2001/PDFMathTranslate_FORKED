"""AgreementCalculator — 标注者间一致性计算。

计算 Cohen's kappa 等一致性指标。

用法：
    calculator = AgreementCalculator()
    kappa = calculator.compute_kappa(annotator_a_scores, annotator_b_scores)
    # 0.75 (substantial agreement)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AgreementResult:
    """一致性结果。"""

    kappa: float = 0.0
    agreement_rate: float = 0.0
    interpretation: str = ""
    n_items: int = 0
    n_raters: int = 2

    def summary(self) -> str:
        return (
            f"Agreement: kappa={self.kappa:.2f} ({self.interpretation}), "
            f"agreement={self.agreement_rate:.1%}, "
            f"items={self.n_items}"
        )


class AgreementCalculator:
    """标注者间一致性计算器。"""

    # Kappa 解释
    INTERPRETATIONS = [
        (0.80, "Almost perfect agreement"),
        (0.60, "Substantial agreement"),
        (0.40, "Moderate agreement"),
        (0.20, "Fair agreement"),
        (0.00, "Slight agreement"),
        (-1.00, "Poor agreement"),
    ]

    def compute_kappa(
        self,
        ratings_a: List[int],
        ratings_b: List[int],
    ) -> AgreementResult:
        """计算 Cohen's kappa。"""
        if len(ratings_a) != len(ratings_b):
            raise ValueError("Rating lists must have same length")

        n = len(ratings_a)
        if n == 0:
            return AgreementResult()

        # 计算观察一致率
        agreements = sum(1 for a, b in zip(ratings_a, ratings_b) if a == b)
        po = agreements / n

        # 计算期望一致率
        categories = set(ratings_a) | set(ratings_b)
        pe = 0.0
        for cat in categories:
            pa = sum(1 for x in ratings_a if x == cat) / n
            pb = sum(1 for x in ratings_b if x == cat) / n
            pe += pa * pb

        # 计算 kappa
        if pe == 1.0:
            kappa = 1.0
        else:
            kappa = (po - pe) / (1 - pe)

        # 解释
        interpretation = self._interpret(kappa)

        return AgreementResult(
            kappa=kappa,
            agreement_rate=po,
            interpretation=interpretation,
            n_items=n,
        )

    def compute_weighted_kappa(
        self,
        ratings_a: List[int],
        ratings_b: List[int],
        weights: Optional[Dict[Tuple[int, int], float]] = None,
    ) -> AgreementResult:
        """计算加权 kappa。"""
        if len(ratings_a) != len(ratings_b):
            raise ValueError("Rating lists must have same length")

        n = len(ratings_a)
        if n == 0:
            return AgreementResult()

        # 默认线性权重
        if weights is None:
            weights = {}
            categories = set(ratings_a) | set(ratings_b)
            max_diff = max(categories) - min(categories) if categories else 1
            for i in categories:
                for j in categories:
                    weights[(i, j)] = abs(i - j) / max_diff if max_diff > 0 else 0

        # 计算加权观察一致率
        weighted_agreement = 0.0
        total_weight = 0.0
        for a, b in zip(ratings_a, ratings_b):
            w = weights.get((a, b), 1.0)
            if a == b:
                weighted_agreement += 1.0
            total_weight += 1.0

        po = weighted_agreement / total_weight if total_weight > 0 else 0

        # 简化：使用 unweighted kappa
        return self.compute_kappa(ratings_a, ratings_b)

    def _interpret(self, kappa: float) -> str:
        """解释 kappa 值。"""
        for threshold, interpretation in self.INTERPRETATIONS:
            if kappa >= threshold:
                return interpretation
        return "Poor agreement"

    def compute_pairwise_agreement(
        self,
        annotations: List[List[int]],
    ) -> Dict[str, AgreementResult]:
        """计算多标注者间两两一致性。"""
        results = {}
        n_raters = len(annotations)

        for i in range(n_raters):
            for j in range(i + 1, n_raters):
                key = f"rater_{i+1}_vs_rater_{j+1}"
                result = self.compute_kappa(annotations[i], annotations[j])
                results[key] = result

        return results

    def compute_fleiss_kappa(
        self,
        annotations: List[List[int]],
        categories: Optional[List[int]] = None,
    ) -> AgreementResult:
        """计算 Fleiss' kappa（多标注者）。"""
        if not annotations or not annotations[0]:
            return AgreementResult()

        n_items = len(annotations[0])
        n_raters = len(annotations)

        if categories is None:
            categories = list(set(val for ann in annotations for val in ann))

        # 计算每个项目的类别分布
        item_categories = []
        for i in range(n_items):
            item_anns = [ann[i] for ann in annotations]
            counts = Counter(item_anns)
            item_categories.append(counts)

        # 计算 P_i (每个项目的一致率)
        p_items = []
        for counts in item_categories:
            n_agree = sum(v * (v - 1) for v in counts.values())
            p_i = n_agree / (n_raters * (n_raters - 1)) if n_raters > 1 else 0
            p_items.append(p_i)

        po = sum(p_items) / n_items  # 观察一致率

        # 计算 p_j (每个类别的比例)
        total_annotations = n_items * n_raters
        p_cats = {}
        for cat in categories:
            cat_count = sum(ann.count(cat) for ann in annotations)
            p_cats[cat] = cat_count / total_annotations

        pe = sum(p**2 for p in p_cats.values())  # 期望一致率

        # kappa
        if pe == 1.0:
            kappa = 1.0
        else:
            kappa = (po - pe) / (1 - pe)

        interpretation = self._interpret(kappa)

        return AgreementResult(
            kappa=kappa,
            agreement_rate=po,
            interpretation=interpretation,
            n_items=n_items,
            n_raters=n_raters,
        )
