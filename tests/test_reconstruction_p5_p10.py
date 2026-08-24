"""P5–P10 实现单元测试（规范书 §5 / §6 / §9）。

覆盖：
  * P5: Glyph → StyleRun → VisualLine → LogicalParagraph 逻辑链路
      - 字体切换不产生语义单元碎裂（样式与语义解耦）
      - 基线/重叠/水平间距三重行合并判定
  * P6: 公式置信度打分 + FormulaObject 抽取 + 锚点保护
  * P7/P8/P9: Inline 换行 + Master Baseline + 三阶段坐标
  * P10: DualPatch 双层补丁合成 + QA（§9.1/§9.2）
"""

from __future__ import annotations

from pdf2zh.geometry.glyph import Glyph
from pdf2zh.geometry.line import VisualLineBuilder
from pdf2zh.geometry.paragraph import build_logical_paragraphs
from pdf2zh.geometry.style_run import build_style_runs, normalize_font_name
from pdf2zh.formula.anchor import AnchorProtector
from pdf2zh.formula.confidence import FormulaConfidenceEngine
from pdf2zh.formula.extractor import FormulaExtractor, FormulaObject, InlineTextRun
from pdf2zh.layout.baseline import BaselineComputer, align_baselines
from pdf2zh.layout.inline_layout import InlineLayoutEngine, build_translation_unit
from pdf2zh.layout.solver import LayoutSolver
from pdf2zh.patch.dual_patcher import DualPatcher
from pdf2zh.v3.reconstruction_pipeline import ReconstructionPipeline


def mk_glyph(char, x, baseline, size, font="Helv", y0=None, y1=None):
    """构造测试字形（y-up：baseline 为基线 y，bbox 由基线推算）。"""
    y0 = y0 if y0 is not None else baseline - 0.2 * size
    y1 = y1 if y1 is not None else baseline + 0.8 * size
    return Glyph(
        char=char,
        bbox=(x, y0, x + 0.5 * size, y1),
        baseline=baseline,
        ascent=0.8 * size,
        descent=-0.2 * size,
        font_name=font,
        font_size=size,
        page_id=0,
        object_id=int(x * 100),
    )


# ── P5: Glyph / StyleRun / VisualLine / LogicalParagraph ──────────────


class TestGlyphAndStyleRun:
    def test_glyph_immutable_fields(self):
        g = mk_glyph("A", 10, 100, 12)
        assert g.baseline == 100
        assert g.font_name == "Helv"
        assert g.page_id == 0

    def test_font_name_normalization(self):
        assert normalize_font_name("Helvetica-Bold") == "helveticabold"
        assert normalize_font_name("STIXTwoMath") == "stixtwomath"

    def test_style_runs_split_on_font_switch(self):
        glyphs = (
            [mk_glyph("H", x, 100, 12, "Helv") for x in range(0, 20, 10)]
            + [mk_glyph("x", 30, 100, 12, "CMMI10")]
            + [mk_glyph("e", 40, 100, 12, "Helv")]
        )
        runs = build_style_runs(glyphs)
        assert len(runs) == 3  # Helv / CMMI10 / Helv

    def test_style_runs_subscript_new_size(self):
        glyphs = [
            mk_glyph("a", 0, 100, 12, "Helv"),
            mk_glyph("1", 12, 96, 8, "Helv"),  # 下标：字号 8
            mk_glyph("b", 20, 100, 12, "Helv"),
        ]
        runs = build_style_runs(glyphs)
        assert len(runs) == 3
        assert runs[1].font_size == 8


class TestVisualLineReconstruction:
    def test_same_baseline_joins(self):
        glyphs = [
            mk_glyph(c, x, 100, 12)
            for x, c in [(0, "H"), (12, "e"), (24, "l"), (36, "l"), (48, "o")]
        ]
        line = VisualLineBuilder().build(glyphs, page_id=0)[0]
        assert line.text == "Hello"
        assert abs(line.master_baseline - 100) < 1.0

    def test_font_switch_within_line_stays_one_line(self):
        glyphs = [mk_glyph("H", x, 100, 12, "Helv") for x in range(0, 30, 10)] + [
            mk_glyph("x", x, 100, 12, "CMMI10") for x in range(30, 60, 10)
        ]
        line = VisualLineBuilder().build(glyphs, page_id=0)[0]
        assert line.text == "HHHxxx"
        assert len(line.style_runs) == 2

    def test_baseline_drift_separates_lines(self):
        glyphs = [mk_glyph(c, x, 100, 12) for x, c in [(0, "A"), (12, "B"), (24, "C")]]
        glyphs += [mk_glyph(c, x, 85, 12) for x, c in [(0, "D"), (12, "E"), (24, "F")]]
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        assert len(lines) == 2
        assert lines[0].text == "ABC"
        assert lines[1].text == "DEF"

    def test_superscript_joins_main_line_no_ghost_line(self):
        """回归：上标小字号字形不得成为行锚导致「幽灵行」分裂。

        构造 "x2"（x 主字形 14pt + 上标 2 为 8pt、基线 +4），整行必须是
        一条视觉行且文本顺序保持 "x2"，而不是 2 孤立成行。
        """
        glyphs = [
            mk_glyph("x", 120, 100, 14, "CMMI10"),
            mk_glyph("2", 132, 104, 8, "CMR10"),  # 上标（先按基线排最前）
            mk_glyph("+", 144, 100, 14, "CMSY10"),
            mk_glyph("1", 158, 100, 14, "CMR10"),
        ]
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        assert len(lines) == 1, f"幽灵行分裂: {[l.text for l in lines]}"
        assert lines[0].text == "x2+1"

    def test_subscript_joins_main_line(self):
        """回归：下标字形并入主行（基线 -4，字号 8pt）。"""
        glyphs = [
            mk_glyph("a", 0, 100, 12, "Helv"),
            mk_glyph("1", 12, 96, 8, "Helv"),  # 下标
            mk_glyph("b", 20, 100, 12, "Helv"),
        ]
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        assert len(lines) == 1
        assert lines[0].text == "a1b"


class TestLogicalParagraph:
    def test_font_switch_does_not_split_paragraph(self):
        """§9.1：字体切换不应导致单元碎裂 —— 多字体段落必须聚合为一个段落。"""
        rows = []
        for base_y in (100.0, 87.0):
            for x, c in [(0, "H"), (12, "x"), (24, "e")]:
                font = "CMMI10" if c == "x" else "Helv"
                rows.append(mk_glyph(c, x, base_y, 12, font))
        lines = VisualLineBuilder().build(rows, page_id=0)
        assert len(lines) == 2
        paras = build_logical_paragraphs(lines, page_id=0)
        assert len(paras) == 1  # 字体切换不碎裂段落
        assert paras[0].line_count == 2

    def test_large_gap_splits_paragraph(self):
        """§5.2 硬截断：垂直间距 > 1.8 × line_height。"""
        rows = []
        for base_y in (100.0, 60.0):  # 40pt 间距 >> 1.8×12
            rows.extend(mk_glyph(c, x, base_y, 12) for x, c in [(0, "A"), (12, "B")])
        lines = VisualLineBuilder().build(rows, page_id=0)
        paras = build_logical_paragraphs(lines, page_id=0)
        assert len(paras) == 2


# ── P6: Formula Confidence / Extraction / Anchor ──────────────────────


class TestFormulaConfidence:
    def test_weights_are_spec(self):
        eng = FormulaConfidenceEngine()
        assert eng.WEIGHTS == (0.30, 0.25, 0.15, 0.15, 0.15)

    def test_font_keyword_hit(self):
        eng = FormulaConfidenceEngine()
        assert eng.font_score("STIXTwoMath-Regular") > 0.5

    def test_density_detects_math_symbols(self):
        eng = FormulaConfidenceEngine()
        assert eng.density_score("∫ x² dx = 2") > 0.5
        assert eng.density_score("plain english text") < 0.3

    def test_unicode_region(self):
        eng = FormulaConfidenceEngine()
        assert eng.unicode_score("𝑓𝑥") >= 0.9  # Math Alphanumeric
        assert eng.unicode_score("hello") < 0.2

    def test_baseline_spread(self):
        eng = FormulaConfidenceEngine()
        glyphs = [
            mk_glyph("x", 0, 100, 12),
            mk_glyph("1", 12, 95, 8),  # 下标
            mk_glyph("2", 20, 104, 8),  # 上标
        ]
        assert eng.baseline_score(glyphs) > 0.3

    def test_score_verdict_thresholds(self):
        eng = FormulaConfidenceEngine()
        math_glyphs = [
            mk_glyph("∫", 0, 100, 14, "CMMI10"),
            mk_glyph("x", 15, 100, 14, "CMMI10"),
        ]
        s = eng.score("∫x", math_glyphs, font_name="CMMI10")
        assert s.verdict == "formula"
        assert s.total >= 0.75

    def test_plain_text_verdict(self):
        eng = FormulaConfidenceEngine()
        plain = [mk_glyph(c, x, 100, 12) for x, c in [(0, "h"), (12, "e"), (24, "l")]]
        s = eng.score("hel", plain, font_name="Helvetica")
        assert s.verdict == "text"


class TestFormulaExtraction:
    def _mk_mixed_paragraph(self):
        glyphs = (
            [
                mk_glyph(c, x, 100, 12)
                for x, c in [(0, "L"), (12, "e"), (24, "t"), (36, " ")]
            ]
            + [
                mk_glyph("f", 48, 100, 14, "CMMI10"),
                mk_glyph("(", 64, 100, 14, "CMMI10"),
                mk_glyph("x", 76, 100, 14, "CMMI10"),
                mk_glyph(")", 88, 100, 14, "CMMI10"),
            ]
            + [mk_glyph("b", 104, 100, 12), mk_glyph("e", 116, 100, 12)]
        )
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        return build_logical_paragraphs(lines, page_id=0)[0]

    def test_extract_formula_object_in_paragraph(self):
        para = self._mk_mixed_paragraph()
        objs = FormulaExtractor().extract_paragraph(para)
        kinds = {type(o).__name__ for o in objs}
        assert "FormulaObject" in kinds
        assert "InlineTextRun" in kinds

    def test_translation_unit_anchors(self):
        para = self._mk_mixed_paragraph()
        FormulaExtractor().extract_paragraph(para)
        unit = build_translation_unit(para)
        assert "<formula_0>" in unit.source_text_with_anchors
        assert "Let" in unit.source_text_with_anchors
        assert unit.formula_map

    def test_anchor_protect_restore_roundtrip(self):
        prot = AnchorProtector()
        text = "Let <formula_0> be computable."
        anchored, fmap = prot.protect(text)
        assert anchored == text
        assert fmap == {"<formula_0>": "0"}
        restored = prot.restore("设 <formula_0> 为连续函数。", fmap)
        assert "<formula_0>" in restored

    def test_anchor_integrity_100_percent(self):
        prot = AnchorProtector()
        _, fmap = prot.protect("A <formula_0> B <formula_1> C")
        assert prot.integrity_score("X <formula_0> Y <formula_1> Z", fmap) == 1.0


# ── P7/P8/P9: Inline Layout / Baseline / Solver ───────────────────────


class TestBaseline:
    def test_compute_weighted(self):
        glyphs = [mk_glyph("a", 0, 100, 12), mk_glyph("b", 12, 99, 11)]
        m = BaselineComputer.compute(glyphs)
        assert abs(m.master_baseline - 99.5) < 0.6
        assert m.line_height > 0

    def test_align_baselines(self):
        a = BaselineComputer.compute([mk_glyph("x", 0, 100, 12)])
        b = BaselineComputer.compute([mk_glyph("y", 0, 90, 12)])
        assert abs(align_baselines(a, b) - 10.0) < 1e-6


class TestInlineLayout:
    def test_text_width_cjk_larger(self):
        eng = InlineLayoutEngine()
        assert eng.text_width("你好", 10) > eng.text_width("abc", 10)

    def test_wrap_multiline(self):
        eng = InlineLayoutEngine()
        objs = [
            InlineTextRun(text="a" * 30, style_runs=[], bbox=(0, 0, 0, 0), font_size=10)
        ]
        lines = eng.wrap(objs, container_width=100, font_size=10)
        assert len(lines) >= 2


class TestLayoutSolver:
    def _mk_unit(self):
        glyphs = (
            [mk_glyph(c, x, 100, 12) for x, c in [(0, "L"), (12, "e"), (24, "t")]]
            + [mk_glyph("x", 36, 100, 14, "CMMI10")]
            + [mk_glyph("b", 48, 100, 12), mk_glyph("e", 60, 100, 12)]
        )
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        paras = build_logical_paragraphs(lines, page_id=0)
        FormulaExtractor().extract_paragraph(paras[0])
        return build_translation_unit(paras[0])

    def test_three_stage_coordinates(self):
        unit = self._mk_unit()
        solver = LayoutSolver()
        solved = solver.solve(unit, "Let <formula_0> be", page_rect=(0, 0, 600, 800))
        assert solved.translated_bbox[0] == solved.source_bbox[0]
        assert solved.line_count >= 1
        assert solved.to_dict()["unit_id"]

    def test_translated_bbox_preserves_source(self):
        unit = self._mk_unit()
        src = unit.source_bbox
        solver = LayoutSolver()
        solver.translated_box(unit, "Let <formula_0> be long translation")
        assert unit.source_bbox == src  # §6.2 source immutable

    def test_multiline_identity_solve_zero_drift(self):
        """回归：多行段落恒等译文时公式对象级漂移必须为 0。

        两行混合段落（行基线 100/85），恒等译文（unit.text）应保持
        行级基线映射 → 公式 render_bbox == source_bbox（§9.2 容差）。"""
        glyphs = (
            [
                mk_glyph(c, x, 100, 12)
                for x, c in [(0, "L"), (12, "e"), (24, "t"), (36, " ")]
            ]
            + [
                mk_glyph(c, x, 100, 14, "CMMI10")
                for x, c in [(48, "f"), (64, "("), (76, "x"), (88, ")")]
            ]
            + [mk_glyph(c, x, 100, 12) for x, c in [(104, "b"), (116, "e")]]
            + [mk_glyph(c, x, 85, 12) for x, c in [(0, "T"), (12, "h"), (24, "e")]]
            + [mk_glyph("∫", 36, 85, 18, "CMSY10"), mk_glyph("x", 58, 85, 14, "CMMI10")]
        )
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        assert len(lines) == 2, f"期望 2 视觉行: {[l.text for l in lines]}"
        paras = build_logical_paragraphs(lines, page_id=0)
        assert len(paras) == 1
        FormulaExtractor().extract_paragraph(paras[0])
        unit = build_translation_unit(paras[0])
        assert len(unit.source_line_baselines) == 2
        assert len(unit.formula_map) == 2, f"期望 2 公式: {unit.formula_map}"
        solver = LayoutSolver()
        solved = solver.solve(unit, unit.text)
        assert len(solved.formula_placements) == 2, solved.formula_placements
        for p in solved.formula_placements:
            dx = abs(p["render_bbox"][0] - p["source_bbox"][0])
            dy = abs(p["render_bbox"][1] - p["source_bbox"][1])
            assert (
                dx <= 0.5 and dy <= 0.5
            ), f"公式 {p['formula_id']} 漂移 dx={dx:.3f} dy={dy:.3f}"


# ── P10: Dual Patch + QA ──────────────────────────────────────────────


class TestDualPatchQA:
    def test_font_switch_ratio_low(self):
        """§9.1：多字体混合段落 unit_count/switch_count 接近段落占比。"""
        patcher = DualPatcher()
        rows = []
        for base_y in (100.0, 87.0, 74.0):
            for x, c in [(0, "H"), (12, "x"), (24, "e"), (36, "x"), (48, "o")]:
                font = "CMMI10" if c == "x" else "Helv"
                rows.append(mk_glyph(c, x, base_y, 12, font))
        lines = VisualLineBuilder().build(rows, page_id=0)
        paras = build_logical_paragraphs(lines, page_id=0)
        assert len(paras) == 1  # 1 段落（不碎裂）
        switches = patcher.count_font_switches(paras)
        assert switches >= 4  # 每行至少 2 次切换 × 3 行
        ratio = 1 / switches if switches else 1.0
        assert ratio < 0.1

    def test_character_retention_zero_loss(self):
        """§9.1：直出/渲染文本与译文一致时，字符丢失率必须为 0.00%。"""
        patcher = DualPatcher()
        # 译文 == 源文（未翻译直出）：锚点剥离后无丢失无重复
        qa = patcher.text_qa(
            unit_count=1,
            font_switch_count=0,
            source_chars=6,
            translated_text="设 <formula_0> 为连续函数。",
            source_text="设 <formula_0> 为连续函数。",
        )
        assert qa["retention_ok"] is True
        assert qa["loss_rate"] == 0.0

    def test_drift_tolerance(self):
        """§9.2：非翻译公式位置偏差 Δx/Δy <= 0.5pt。"""
        unit = TestLayoutSolver()._mk_unit()
        solver = LayoutSolver()
        solved = solver.solve(unit, unit.text)
        qa = DualPatcher().formula_qa([solved])
        assert qa["drift_ok"] is True
        assert qa["max_dx"] <= 0.5 and qa["max_dy"] <= 0.5

    def test_anchor_score_in_synthesis(self):
        patcher = DualPatcher()
        unit = TestLayoutSolver()._mk_unit()
        solver = LayoutSolver()
        solved = solver.solve(unit, unit.text)
        patch = patcher.synthesize([solved], unit.text, unit.formula_map)
        assert patch.qa["formula"]["anchor"]["anchor_ok"] is True
        assert "ANCHOR_OK" in patch.qa["summary"]


# ── P5–P10 全链路（run_on_glyphs 便捷入口）───────────────────────────


class TestReconstructionPipeline:
    def test_full_pipeline_on_glyphs(self):
        glyphs = (
            [
                mk_glyph(c, x, 100, 12)
                for x, c in [(0, "L"), (12, "e"), (24, "t"), (36, " ")]
            ]
            + [
                mk_glyph("f", 48, 100, 14, "CMMI10"),
                mk_glyph("(", 64, 100, 14, "CMMI10"),
                mk_glyph("x", 76, 100, 14, "CMMI10"),
                mk_glyph(")", 88, 100, 14, "CMMI10"),
            ]
            + [
                mk_glyph("b", 104, 100, 12),
                mk_glyph("e", 116, 100, 12),
                mk_glyph("c", 128, 100, 12),
                mk_glyph("o", 140, 100, 12),
                mk_glyph("n", 152, 100, 12),
            ]
        )
        result = ReconstructionPipeline.run_on_glyphs(glyphs, page_id=3)
        assert result.glyph_count == len(glyphs)
        assert result.line_count == 1
        assert result.paragraph_count == 1
        assert result.formula_count >= 1
        assert len(result.translation_units) == 1
        unit = result.translation_units[0]
        assert "<formula_0>" in unit.source_text_with_anchors
        assert result.to_dict()["page_id"] == 3
