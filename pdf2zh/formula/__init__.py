"""P6 — Formula Object Reconstruction 子系统（规范书 §5.3 / §6.1）。

confidence.py   # 公式置信度打分引擎（五特征加权）
extractor.py    # FormulaObject 抽取与解析
anchor.py       # 翻译占位符注入与还原
"""

from __future__ import annotations

from pdf2zh.formula.confidence import (
    FormulaConfidenceEngine,
    FormulaScore,
    is_math_unicode,
)
from pdf2zh.formula.extractor import (
    FormulaExtractor,
    FormulaObject,
    InlineTextRun,
)
from pdf2zh.formula.anchor import (
    AnchorProtector,
    anchors_in_text,
    anchors_in_text_loose,
    extract_anchors_loose,
    normalize_anchor_token,
    repair_anchors,
)

__all__ = [
    "FormulaConfidenceEngine",
    "FormulaScore",
    "is_math_unicode",
    "FormulaExtractor",
    "FormulaObject",
    "InlineTextRun",
    "AnchorProtector",
    "anchors_in_text",
    "anchors_in_text_loose",
    "extract_anchors_loose",
    "normalize_anchor_token",
    "repair_anchors",
]
