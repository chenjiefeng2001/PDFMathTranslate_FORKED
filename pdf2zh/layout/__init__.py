"""P7/P8/P9 — Layout & Solving Layer（规范书 §4.3 / §6.2 / §7）。

    inline_layout.py   # Inline 混合排版模型（P7）
    baseline.py        # Master Baseline 几何计算（P8）
    solver.py          # Layout Solver 三阶段坐标计算（P9）
"""
from __future__ import annotations

from pdf2zh.layout.baseline import (
    BaselineMetrics,
    BaselineComputer,
    align_baselines,
)
from pdf2zh.layout.inline_layout import (
    TranslationUnit,
    InlineSegment,
    LayoutLine,
    InlineLayoutEngine,
    build_translation_unit,
)
from pdf2zh.layout.solver import (
    SolvedUnit,
    LayoutSolver,
)

__all__ = [
    "BaselineMetrics", "BaselineComputer", "align_baselines",
    "TranslationUnit", "InlineSegment", "LayoutLine",
    "InlineLayoutEngine", "build_translation_unit",
    "SolvedUnit", "LayoutSolver",
]
