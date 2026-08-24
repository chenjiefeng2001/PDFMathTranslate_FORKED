"""P5–P10 遗留项补全测试（报告第八节 4 项遗留的实现验收）。

覆盖：
  1. 遗留项 1：``raw_latex_approx`` —— formula/latex_approx 符号映射
     表 + extractor 接入（不再恒返回 None）；
  2. 遗留项 2：译文变长时公式与文本重叠的水平避让（solver 顺序推进
     占用区间；恒等译文零漂移保持）；
  3. 遗留项 3：C_layout 真实注入 —— v3/layout_class 掩码采样 + 文本
     启发式兜底；
  4. 遗留项 4：双层补丁 MuPDF 直接落位 —— compose_render_patch 行级
     补丁 + apply_to_pdf + to_overlay_segments + render_hybrid。
"""

from __future__ import annotations

import pytest

from pdf2zh.formula.extractor import FormulaExtractor, FormulaObject, InlineTextRun
from pdf2zh.geometry.glyph import Glyph
from pdf2zh.geometry.line import VisualLineBuilder
from pdf2zh.geometry.paragraph import build_logical_paragraphs
from pdf2zh.layout.inline_layout import TranslationUnit
from pdf2zh.layout.solver import LayoutSolver
from pdf2zh.patch.dual_patcher import DualPatcher
from pdf2zh.v3.layout_class import heuristic_layout_class, layout_mask_class_fn


def mk_glyph(char, x, baseline, size, font="Helv"):
    """构造测试字形（与 test_reconstruction_p5_p10 同构）。"""
    return Glyph(
        char=char,
        bbox=(x, baseline - 0.2 * size, x + 0.5 * size, baseline + 0.8 * size),
        baseline=baseline,
        ascent=0.8 * size,
        descent=-0.2 * size,
        font_name=font,
        font_size=size,
        page_id=0,
        object_id=int(x * 100),
    )


def _mk_formula_unit(x0=48, width=300):
    """构造含一个行内公式的 TranslationUnit（源文 'Let <f> be.'）。"""
    formula = FormulaObject(
        formula_id="formula_0_48",
        glyphs=[mk_glyph("x", x0, 100, 14, "CMMI10")],
        bbox=(x0, 97, x0 + 7, 111),
        baseline=100,
    )
    return TranslationUnit(
        unit_id="u0",
        page_id=0,
        source_text_with_anchors="Let <formula_0> be.",
        formula_map={"<formula_0>": formula},
        source_bbox=(10, 96, width, 112),
        inline_structure=[
            InlineTextRun(
                text="Let ", style_runs=[], bbox=(10, 96, 40, 112), font_size=12
            ),
            formula,
            InlineTextRun(
                text="be.", style_runs=[], bbox=(60, 96, 80, 112), font_size=12
            ),
        ],
        master_baseline=100,
        source_line_baselines=[100],
    )


# ── 遗留项 1：raw_latex_approx ───────────────────────


class TestLatexApprox:
    def test_symbol_table(self):
        from pdf2zh.formula.latex_approx import to_latex_approx

        assert "\\int" in to_latex_approx("∫")
        assert to_latex_approx("x ≤ ∞") == "x \\leq \\infty"
        assert "\\rightarrow" in to_latex_approx("→")
        assert to_latex_approx("×") == "\\times"

    def test_greek_and_superscript(self):
        from pdf2zh.formula.latex_approx import to_latex_approx

        assert to_latex_approx("α Δ") == "\\alpha \\Delta"
        assert to_latex_approx("x²₀") == "x^{2}_{0}"

    def test_unknown_char_preserved(self):
        from pdf2zh.formula.latex_approx import to_latex_approx

        assert to_latex_approx("汉字") == "汉字"

    def test_extractor_no_longer_none(self):
        """遗留项 1：FormulaObject.raw_latex_approx 不再恒为 None。"""
        out = FormulaExtractor._approx_latex("x² + α")
        assert out is not None and "x^{2}" in out


# ── 遗留项 2：公式水平避让 ───────────────────────


class TestCollisionAvoidance:
    def test_identity_zero_drift(self):
        """恒等译文：公式锁定源 x0，零漂移（不触发避让）。"""
        solver = LayoutSolver()
        solved = solver.solve(_mk_formula_unit(), _mk_formula_unit().text)
        p = solved.formula_placements[0]
        assert abs(p["render_bbox"][0] - p["source_bbox"][0]) < 1e-6
        assert p.get("collision_evaded") is False

    def test_long_text_same_line_evades(self):
        """译文变长（同一行）文本延伸进公式区域 → 公式右移避让。"""
        unit = _mk_formula_unit(width=300)
        solver = LayoutSolver()
        solved = solver.solve(unit, "Let this longer text <formula_0> be.")
        p = solved.formula_placements[0]
        assert p.get("collision_evaded") is True
        assert p["render_bbox"][0] > p["source_bbox"][0]
        assert p["render_bbox"][2] <= 300  # 不超容器右边界

    def test_evasion_stays_in_container(self):
        """避让后公式不超出容器（防御性夹紧）。"""
        unit = _mk_formula_unit(width=120)
        solver = LayoutSolver()
        solved = solver.solve(unit, "Let a long text <formula_0> be.")
        for p in solved.formula_placements:
            assert p["render_bbox"][2] <= 120 + 1e-6


# ── 遗留项 3：C_layout 真实注入 ───────────────────────


class TestLayoutClassInjection:
    def test_mask_sampling(self):
        """从 conv.layout[pageid] 掩码采样段落中心类别。"""
        import numpy as np
        from types import SimpleNamespace

        glyphs = [mk_glyph(c, x, 100, 12) for x, c in [(0, "A"), (12, "B"), (24, "C")]]
        paras = build_logical_paragraphs(
            VisualLineBuilder().build(glyphs, page_id=0), page_id=0
        )
        mask = np.zeros((200, 200), dtype=np.int32)
        mask[95:105, 0:60] = 0  # abandon/公式保留区
        cls_fn = layout_mask_class_fn(SimpleNamespace(layout={0: mask}), None)
        assert cls_fn(paras[0]) == 0

    def test_mask_missing_returns_none(self):
        from types import SimpleNamespace

        paras = build_logical_paragraphs(
            VisualLineBuilder().build([mk_glyph("A", 0, 100, 12)], page_id=0), page_id=0
        )
        assert layout_mask_class_fn(SimpleNamespace(layout={}), None)(paras[0]) is None

    def test_heuristic_formula_and_title(self):
        math_lines = VisualLineBuilder().build(
            [mk_glyph("f", x, 100, 14, "CMMI10") for x in (0, 12, 24)]
            + [mk_glyph("∫", 36, 100, 16, "CMSY10")],
            page_id=0,
        )
        mparas = build_logical_paragraphs(math_lines, page_id=0)
        assert heuristic_layout_class(mparas[0]) == "formula"

        title_lines = VisualLineBuilder().build(
            [
                mk_glyph(c, x, 100, 24, "Helv")
                for x, c in [(0, "T"), (20, "i"), (40, "t")]
            ],
            page_id=0,
        )
        tparas = build_logical_paragraphs(title_lines, page_id=0)
        assert heuristic_layout_class(tparas[0]) == "title"

        plain_lines = VisualLineBuilder().build(
            [mk_glyph(c, x, 100, 12) for x, c in [(0, "A"), (12, "B")]], page_id=0
        )
        pparas = build_logical_paragraphs(plain_lines, page_id=0)
        assert heuristic_layout_class(pparas[0]) == "plain text"


# ── 遗留项 4：双层补丁 MuPDF 直接落位 ───────────────────────


class TestDualPatchApply:
    def test_render_patch_line_level(self):
        """compose_render_patch 输出行级补丁：公式段以空格占位。"""
        solver = LayoutSolver()
        solved = solver.solve(_mk_formula_unit(), _mk_formula_unit().text)
        patch = DualPatcher().compose_render_patch(solved)
        assert patch["op"] == "text_show"
        assert patch["lines"][0]["formula_ids"] == ["formula_0_48"]
        assert "<formula" not in patch["lines"][0]["text"]
        assert patch["text"] == "Let x be."  # 顶层保留语义文本

    def test_apply_to_pdf(self):
        pymupdf = pytest.importorskip("pymupdf")
        solver = LayoutSolver()
        unit = _mk_formula_unit()
        solved = solver.solve(unit, unit.text)
        patch = DualPatcher().synthesize([solved], unit.text, unit.formula_map)
        doc = pymupdf.open()
        doc.new_page(width=400, height=300)
        n = DualPatcher().apply_to_pdf(doc, 0, patch, fontname="helv")
        assert n == 1
        text = doc[0].get_text().strip()
        assert "be" in text and "x" not in text  # 公式字形未渲染

    def test_to_overlay_segments(self):
        pytest.importorskip("pymupdf")
        solver = LayoutSolver()
        unit = _mk_formula_unit()
        solved = solver.solve(unit, unit.text)
        patch = DualPatcher().synthesize([solved], unit.text, unit.formula_map)
        segs = DualPatcher().to_overlay_segments(patch)
        assert len(segs) == 1
        assert "x" not in segs[0].text

    def test_render_hybrid(self):
        pymupdf = pytest.importorskip("pymupdf")
        from pdf2zh.overlay_renderer import OverlayRenderer, OverlaySegment

        doc = pymupdf.open()
        page = doc.new_page(width=300, height=200)
        page.insert_text((40, 60), "Original", fontsize=12)
        orig = doc.write()
        out = OverlayRenderer(dpi=150).render_hybrid(
            page,
            [OverlaySegment(text="译文文本", bbox=(40, 40, 200, 56), font_size=12)],
            orig,
        )
        assert len(out) > 1000
        out_doc = pymupdf.open(stream=out)
        assert out_doc.page_count == 1
