"""P5 — Semantic Text Unit & Glyph Geometry Layer.

底层几何抽象层（对应规范书第 8 节目录结构）：

    glyph.py       # Glyph 数据结构与提取
    style_run.py   # StyleRun 划分逻辑
    line.py        # VisualLine 物理行重构
    paragraph.py   # LogicalParagraph 语义段落重构

链路：LTChar → Glyph → StyleRun → VisualLine → LogicalParagraph。

所有模块纯 Python 实现，无外部依赖，可直接单测。
"""

from __future__ import annotations

from pdf2zh.geometry.glyph import (
    Glyph,
    GlyphBBox,
    extract_glyphs_from_ltpage,
    extract_glyphs_from_page,
)
from pdf2zh.geometry.style_run import (
    StyleRun,
    build_style_runs,
    style_runs_text,
)
from pdf2zh.geometry.line import (
    VisualLine,
    VisualLineConfig,
    VisualLineBuilder,
)
from pdf2zh.geometry.paragraph import (
    LogicalParagraph,
    ParagraphConfig,
    build_logical_paragraphs,
)

__all__ = [
    "Glyph",
    "GlyphBBox",
    "extract_glyphs_from_ltpage",
    "extract_glyphs_from_page",
    "StyleRun",
    "build_style_runs",
    "style_runs_text",
    "VisualLine",
    "VisualLineConfig",
    "VisualLineBuilder",
    "LogicalParagraph",
    "ParagraphConfig",
    "build_logical_paragraphs",
]
