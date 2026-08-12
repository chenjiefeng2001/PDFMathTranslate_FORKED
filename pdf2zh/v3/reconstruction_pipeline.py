"""P5–P10 统一编排管线（Semantic Text Unit & Glyph Geometry Layer）。

把规范书第 8 节目录结构的四个子系统串成一条可复用的管道：

    LTChar ──► Glyph ──► StyleRun ──► VisualLine ──► LogicalParagraph
        ──► FormulaObject 抽取（锚点注入）──► TranslationUnit
        ──► Inline Layout / Baseline / Layout Solver（三阶段坐标）
        ──► Dual Patch + QA 校验（§9.1 / §9.2）

作为 side-channel 挂在 mainline_wiring 上（``conv.reconstruction_channel``
开关）；所有失败只进 debug 日志，绝不干扰主链路渲染。消费端把
``v3_output[\"reconstruction\"]`` 回传（high_level）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from pdf2zh.geometry.glyph import extract_glyphs_from_ltpage
from pdf2zh.geometry.line import VisualLineBuilder
from pdf2zh.geometry.paragraph import build_logical_paragraphs
from pdf2zh.formula.confidence import FormulaConfidenceEngine
from pdf2zh.formula.extractor import FormulaExtractor, FormulaObject
from pdf2zh.layout.inline_layout import (
    InlineLayoutEngine,
    TranslationUnit,
    build_translation_unit,
)
from pdf2zh.layout.solver import LayoutSolver, SolvedUnit

log = logging.getLogger(__name__)


@dataclass
class ReconstructionResult:
    """P5–P10 管道对单页的全部输出。"""

    page_id: int = 0
    glyph_count: int = 0
    line_count: int = 0
    paragraph_count: int = 0
    formula_count: int = 0
    ambiguous_count: int = 0
    translation_units: List[TranslationUnit] = field(default_factory=list)
    solved_units: List[SolvedUnit] = field(default_factory=list)
    paragraphs: List = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "glyph_count": self.glyph_count,
            "line_count": self.line_count,
            "paragraph_count": self.paragraph_count,
            "formula_count": self.formula_count,
            "ambiguous_count": self.ambiguous_count,
            "translation_units": [u.to_dict() for u in self.translation_units],
            "solved_units": [s.to_dict() for s in self.solved_units],
            "paragraphs": [p.to_dict() for p in self.paragraphs],
        }


class ReconstructionPipeline:
    """P5–P10 全链路编排（Glyph → Formula → TranslationUnit → Solve）。"""

    def __init__(self, layout_class_fn=None, page_rect_fn=None) -> None:
        """``layout_class_fn(para)`` 返回 DocLayout 区域类别（可选）。

        ``page_rect_fn(page_id)`` 返回页面边界 (x0,y0,x1,y1)（可选）。
        """
        self.line_builder = VisualLineBuilder()
        self.engine = FormulaConfidenceEngine()
        self.extractor = FormulaExtractor(engine=self.engine)
        self.inline_engine = InlineLayoutEngine()
        self.solver = LayoutSolver(inline_engine=self.inline_engine)
        self.layout_class_fn = layout_class_fn
        self.page_rect_fn = page_rect_fn

    # ── 完整管道 ──────────────────────────────────────────────────

    def run(self, ltpage, page_id: Optional[int] = None,
            blocks=None) -> ReconstructionResult:
        """对单个 LTPage 执行 P5–P10 全链路并返回结果。"""
        if page_id is None:
            page_id = int(getattr(ltpage, "pageid", 0) or 0)
        result = ReconstructionResult(page_id=page_id)
        glyphs = extract_glyphs_from_ltpage(ltpage, page_id=page_id)
        result.glyph_count = len(glyphs)
        if not glyphs:
            return result
        # P5: 视觉行 + 逻辑段落
        lines = self.line_builder.build(glyphs, page_id=page_id)
        result.line_count = len(lines)
        paragraphs = build_logical_paragraphs(lines, page_id=page_id,
                                              blocks=blocks)
        result.paragraph_count = len(paragraphs)
        result.paragraphs = paragraphs
        # P6: 公式抽取 + 锚点
        for para in paragraphs:
            layout_cls = None
            if self.layout_class_fn is not None:
                try:
                    layout_cls = self.layout_class_fn(para)
                except Exception:  # noqa: BLE001
                    layout_cls = None
            objects = self.extractor.extract_paragraph(para, layout_class=layout_cls)
            result.formula_count += sum(
                1 for o in objects if isinstance(o, FormulaObject))
            result.ambiguous_count += len(objects) - result.formula_count
            # P7: TranslationUnit（含锚点）
            unit = build_translation_unit(para)
            result.translation_units.append(unit)
        # P9: 三阶段坐标求解
        for unit in result.translation_units:
            page_rect = None
            if self.page_rect_fn is not None:
                try:
                    page_rect = self.page_rect_fn(page_id)
                except Exception:  # noqa: BLE001
                    page_rect = None
            translated = unit.text  # 恒等译文（demo 链路）
            solved = self.solver.solve(unit, translated, page_rect=page_rect)
            result.solved_units.append(solved)
        return result

    # ── 便捷入口 ──────────────────────────────────────────────────

    @staticmethod
    def run_on_glyphs(glyphs: Sequence, page_id: int = 0,
                      blocks=None) -> ReconstructionResult:
        """直接输入 Glyph 序列（测试友好，跳过 LTChar 提取）。"""
        result = ReconstructionResult(page_id=page_id)
        result.glyph_count = len(glyphs)
        if not glyphs:
            return result
        pipe = ReconstructionPipeline()
        lines = pipe.line_builder.build(glyphs, page_id=page_id)
        result.line_count = len(lines)
        paragraphs = build_logical_paragraphs(lines, page_id=page_id,
                                              blocks=blocks)
        result.paragraph_count = len(paragraphs)
        result.paragraphs = paragraphs
        for para in paragraphs:
            objects = pipe.extractor.extract_paragraph(para)
            result.formula_count += sum(
                1 for o in objects if isinstance(o, FormulaObject))
            unit = build_translation_unit(para)
            result.translation_units.append(unit)
        for unit in result.translation_units:
            solved = pipe.solver.solve(unit, unit.text)
            result.solved_units.append(solved)
        return result


__all__ = ["ReconstructionResult", "ReconstructionPipeline"]

