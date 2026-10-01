"""PageClassifier — 页面分类：simple vs complex。

Phase Priority 2 核心：简单页面跳过 YOLO，直接用规则。

分类规则：
    Simple page（快速路径）：
        - text blocks > 95%
        - no images
        - no tables
        - no formulas
        - single column

    Complex page（完整路径）：
        - 以上任一条件不满足

预期收益：
    论文 70-80% 页面属于 simple
    2-5x layout speed improvement
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pdf2zh.assembler.page_snapshot import PageSnapshot


class PageComplexity(str, Enum):
    """页面复杂度。"""

    SIMPLE = "simple"
    COMPLEX = "complex"
    UNKNOWN = "unknown"


@dataclass
class ClassificationResult:
    """分类结果。"""

    complexity: PageComplexity = PageComplexity.UNKNOWN
    reason: str = ""
    text_ratio: float = 0.0
    image_ratio: float = 0.0
    has_formula: bool = False
    has_table: bool = False
    column_count: int = 1
    confidence: float = 0.0

    @property
    def is_simple(self) -> bool:
        return self.complexity == PageComplexity.SIMPLE


class PageClassifier:
    """页面分类器。

    根据 PageSnapshot 判断页面复杂度，
    决定是否需要完整 YOLO 推理。

    用法：
        classifier = PageClassifier()
        result = classifier.classify(snapshot)
        if result.is_simple:
            fast_path(snapshot)
        else:
            full_layout(snapshot)
    """

    def __init__(
        self,
        text_ratio_threshold: float = 0.95,
        max_images: int = 0,
        max_tables: int = 0,
        max_columns: int = 1,
    ) -> None:
        self.text_ratio_threshold = text_ratio_threshold
        self.max_images = max_images
        self.max_tables = max_tables
        self.max_columns = max_columns

    def classify(self, snapshot: PageSnapshot) -> ClassificationResult:
        """分类页面。"""
        result = ClassificationResult()

        # 1. 文本比例
        total_area = snapshot.width * snapshot.height
        if total_area > 0:
            text_area = sum(
                (s.bbox[2] - s.bbox[0]) * (s.bbox[3] - s.bbox[1])
                for s in snapshot.text_spans
            )
            result.text_ratio = text_area / total_area

        # 2. 图片数量
        result.image_ratio = snapshot.image_count / max(len(snapshot.raw_blocks), 1)

        # 3. 检测公式（简单规则）
        formula_indicators = ["=", "∑", "∫", "∂", "√", "∞", "≈", "≠", "≤", "≥"]
        for span in snapshot.text_spans:
            if any(ind in span.text for ind in formula_indicators):
                if len(span.text) < 50:  # 短公式
                    result.has_formula = True
                    break

        # 4. 检测表格（简单规则）
        if len(snapshot.drawings) > 5:
            # 多个绘图可能是表格边框
            horizontal_lines = sum(
                1 for d in snapshot.drawings if d.rect[1] == d.rect[3]  # 水平线
            )
            if horizontal_lines >= 3:
                result.has_table = True

        # 5. 检测列数（基于文本块 x 坐标分布）
        if snapshot.text_spans:
            x_coords = [s.bbox[0] for s in snapshot.text_spans]
            if x_coords:
                min_x = min(x_coords)
                max_x = max(x_coords)
                page_width = snapshot.width
                if page_width > 0:
                    x_range = max_x - min_x
                    if x_range > page_width * 0.6:
                        result.column_count = 2
                    else:
                        result.column_count = 1

        # 6. 综合判断
        reasons = []

        if result.text_ratio >= self.text_ratio_threshold:
            reasons.append(f"text_ratio={result.text_ratio:.1%}")
        else:
            reasons.append(
                f"text_ratio={result.text_ratio:.1%} < {self.text_ratio_threshold:.0%}"
            )

        if snapshot.image_count <= self.max_images:
            reasons.append(f"images={snapshot.image_count}")
        else:
            reasons.append(f"images={snapshot.image_count} > {self.max_images}")

        if result.has_table:
            reasons.append("has_table")

        if result.has_formula:
            reasons.append("has_formula")

        if result.column_count > self.max_columns:
            reasons.append(f"columns={result.column_count}")

        # 判断
        is_simple = (
            result.text_ratio >= self.text_ratio_threshold
            and snapshot.image_count <= self.max_images
            and not result.has_table
            and not result.has_formula
            and result.column_count <= self.max_columns
        )

        result.complexity = (
            PageComplexity.SIMPLE if is_simple else PageComplexity.COMPLEX
        )
        result.reason = " | ".join(reasons)
        result.confidence = result.text_ratio if is_simple else (1 - result.text_ratio)

        return result

    def batch_classify(
        self,
        snapshots: list[PageSnapshot],
    ) -> dict[PageComplexity, list[int]]:
        """批量分类，返回按复杂度分组的页码。"""
        result: dict[PageComplexity, list[int]] = {
            PageComplexity.SIMPLE: [],
            PageComplexity.COMPLEX: [],
        }

        for snap in snapshots:
            cr = self.classify(snap)
            result[cr.complexity].append(snap.page_index)

        return result

    def summary(self, snapshots: list[PageSnapshot]) -> str:
        """批量分类摘要。"""
        groups = self.batch_classify(snapshots)
        total = len(snapshots)
        simple = len(groups[PageComplexity.SIMPLE])
        complex_ = len(groups[PageComplexity.COMPLEX])

        return (
            f"PageClassifier | total={total} | "
            f"simple={simple} ({simple/total:.0%}) | "
            f"complex={complex_} ({complex_/total:.0%})"
        )
