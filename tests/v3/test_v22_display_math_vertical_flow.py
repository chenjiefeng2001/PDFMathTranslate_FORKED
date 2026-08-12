"""P1–P4 Display Math 垂直排版与 Redact 覆盖验收测试（用户驱动修复）。

覆盖三大致命病灶的修复：

1. **Display / Inline 公式二分（P6）**：整行公式 / 超宽公式 / 居中公式
   → ``FormulaObject.is_display_mode``；混合行内公式与单符号（``=``/``≠``）
   保持 Inline —— 不再把「独立符号拉成块级展示公式」。
2. **垂直流堆叠（P9 LayoutSolver）**：含 Display 公式的段落按
   Vertical Flow Stacking 动态推进 y 轴（公式物理高度 + margin 累加），
   后续文本基线一定在展示公式之下 —— 杜绝「译文覆盖展示公式」。
3. **Redact 覆盖（P10 DualPatcher）**：``apply_to_pdf`` 绘制前按源
   bbox 强制 ``add_redact_annot`` + ``apply_redactions`` 清空旧图层，
   禁止在未擦除的物理图层上叠加新译文。
"""
from __future__ import annotations

import pytest

from pdf2zh.formula.extractor import (
    FormulaExtractor,
    FormulaObject,
    InlineTextRun,
)
from pdf2zh.geometry.glyph import Glyph
from pdf2zh.geometry.line import VisualLineBuilder
from pdf2zh.geometry.paragraph import build_logical_paragraphs
from pdf2zh.layout.inline_layout import InlineLayoutEngine, build_translation_unit
from pdf2zh.layout.solver import LayoutSolver
from pdf2zh.patch.dual_patcher import DualPatcher


def mk_glyph(char, x, baseline, size, font="Helv"):
    """构造测试字形（与 test_p5p10_remaining 同构）。"""
    return Glyph(char=char, bbox=(x, baseline - 0.2 * size,
                                  x + 0.5 * size, baseline + 0.8 * size),
                 baseline=baseline, ascent=0.8 * size, descent=-0.2 * size,
                 font_name=font, font_size=size, page_id=0,
                 object_id=int(x * 100))


def _iou(a, b):
    """2D 矩形 IoU（规范 §4.1：译文文本框 vs 公式框）。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    union = (max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
             + max(0.0, bx1 - bx0) * max(0.0, by1 - by0) - inter)
    return inter / union if union > 1e-9 else 0.0


def _mk_whole_line_formula_para():
    """整行公式段：两行均为 CMMI10 数学字体（模拟 R1/R2 独立成行）。"""
    glyphs = (
        [mk_glyph(c, x, 100, 14, "CMMI10") for x, c in
         [(0, "R"), (12, "1"), (24, "="), (36, "{"), (48, "("), (60, "1")]]
        + [mk_glyph(c, x, 85, 14, "CMMI10") for x, c in
           [(0, "R"), (12, "2"), (24, "="), (36, "{"), (48, "("), (60, "1")]]
    )
    lines = VisualLineBuilder().build(glyphs, page_id=0)
    return build_logical_paragraphs(lines, page_id=0)[0]


def _mk_mixed_inline_para():
    """混合段：``Let f(x) be`` —— 公式嵌在文本流中（Inline）。"""
    glyphs = (
        [mk_glyph(c, x, 100, 12) for x, c in
         [(0, "L"), (12, "e"), (24, "t"), (36, " ")]]
        + [mk_glyph("f", 48, 100, 14, "CMMI10"),
           mk_glyph("(", 64, 100, 14, "CMMI10"),
           mk_glyph("x", 76, 100, 14, "CMMI10"),
           mk_glyph(")", 88, 100, 14, "CMMI10")]
        + [mk_glyph("b", 104, 100, 12), mk_glyph("e", 116, 100, 12)]
    )
    lines = VisualLineBuilder().build(glyphs, page_id=0)
    return build_logical_paragraphs(lines, page_id=0)[0]


def _mk_display_flow_para():
    """用户反馈场景：文本行 + 两个展示公式行 + 后续文本行。"""
    glyphs = (
        [mk_glyph(c, x, 100, 12) for x, c in
         [(0, "("), (6, "b"), (12, ")"), (24, "D"), (36, "e"), (48, "f")]]
        + [mk_glyph(c, x, 85, 14, "CMMI10") for x, c in
           [(0, "R"), (12, "1"), (24, "="), (36, "{"), (48, "("), (60, "1")]]
        + [mk_glyph(c, x, 70, 14, "CMMI10") for x, c in
           [(0, "R"), (12, "2"), (24, "="), (36, "{"), (48, "("), (60, "1")]]
        + [mk_glyph(c, x, 55, 12) for x, c in
           [(0, "F"), (12, "o"), (24, "r"), (36, " "), (48, "e"), (60, "a")]]
    )
    lines = VisualLineBuilder().build(glyphs, page_id=0)
    return build_logical_paragraphs(lines, page_id=0)[0]

# ── P6：Display / Inline 二分 ──────────────────────────────


class TestDisplayExtraction:
    def test_whole_line_formula_is_display(self):
        """整行公式（无文本）→ 所有公式对象 is_display_mode=True。"""
        para = _mk_whole_line_formula_para()
        objs = FormulaExtractor().extract_paragraph(para)
        formulas = [o for o in objs if isinstance(o, FormulaObject)]
        assert formulas, "期望整行被判为公式对象"
        assert all(o.is_display_mode for o in formulas)

    def test_mixed_inline_formula_not_display(self):
        """``Let f(x) be`` 中的行内公式 → 非 display（参与流式排版）。"""
        para = _mk_mixed_inline_para()
        objs = FormulaExtractor().extract_paragraph(para)
        formulas = [o for o in objs if isinstance(o, FormulaObject)]
        assert formulas
        assert all(not o.is_display_mode for o in formulas)

    def test_single_symbol_kept_inline(self):
        """单符号公式（``=``）回退普通文本（模块 4 降级）而非抽为公式。

        病灶三回归：``Is = reflexive?`` 中的孤立 ``=``（数学字体）不再被
        提升为 FormulaObject 锁定绝对坐标 —— 保持普通文本参与翻译，
        杜绝译文行首撕裂成「= 是自反吗？」。
        """
        glyphs = (
            [mk_glyph(c, x, 100, 12) for x, c in
             [(0, "I"), (12, "s"), (24, " "), (36, "a")]]
            + [mk_glyph("=", 48, 100, 12, "CMSY10")]
            + [mk_glyph(c, x, 100, 12) for x, c in
               [(66, "r"), (78, "e")]]
        )
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        para = build_logical_paragraphs(lines, page_id=0)[0]
        objs = FormulaExtractor().extract_paragraph(para)
        formulas = [o for o in objs if isinstance(o, FormulaObject)]
        assert not formulas, "孤立 = 必须回退普通文本，不再抽为 FormulaObject"
        joined = "".join(getattr(o, "text", "") for o in objs)
        assert "=" in joined, "孤立 = 必须作为普通文本参与翻译"

    def test_mark_display_flags_unit(self):
        """判定函数单元级：整行/超宽/居中 → display；窄公式 → inline。"""
        mk = FormulaExtractor._mark_display_flags

        def _obj(width, x0=0.0):
            return FormulaObject(formula_id=f"f{x0}", glyphs=[],
                                 bbox=(x0, 90, x0 + width, 110),
                                 baseline=100)

        class _Line:
            x0, x1, font_size = 0.0, 200.0, 12.0

        class _Para:
            x0, x1 = 0.0, 200.0

        # 整行公式（无文本）
        objs = [_obj(40)]
        mk(objs, _Para(), _Line())
        assert objs[0].is_display_mode
        # 超宽公式（> 0.6 × 段宽）
        objs = [_obj(140), InlineTextRun(text="t", style_runs=[],
                                         bbox=(0, 0, 0, 0), font_size=12)]
        mk(objs, _Para(), _Line())
        assert objs[0].is_display_mode
        # 居中公式（占行宽主体 >= 50% 且居中）
        objs = [_obj(110, x0=45.0), InlineTextRun(text="t", style_runs=[],
                                                  bbox=(0, 0, 0, 0),
                                                  font_size=12)]
        mk(objs, _Para(), _Line())
        assert objs[0].is_display_mode
        # 窄公式（占行宽 < 50%，非居中非超宽）→ inline
        objs = [_obj(40, x0=80.0), InlineTextRun(text="t", style_runs=[],
                                                 bbox=(0, 0, 0, 0),
                                                 font_size=12)]
        mk(objs, _Para(), _Line())
        assert not objs[0].is_display_mode


# ── P7：Display 公式独占一行 ───────────────────────────────


class TestDisplayWrap:
    def test_display_formula_own_line(self):
        """Display 公式 break before/after：独占一行，两侧文本不在同一行。"""
        eng = InlineLayoutEngine()
        f = FormulaObject(formula_id="f0", glyphs=[],
                          bbox=(0, 90, 50, 110), baseline=100,
                          is_display_mode=True)
        objs = [
            InlineTextRun(text="文本", style_runs=[], bbox=(0, 0, 0, 0),
                          font_size=12),
            f,
            InlineTextRun(text="后续", style_runs=[], bbox=(0, 0, 0, 0),
                          font_size=12),
        ]
        lines = eng.wrap(objs, container_width=300, font_size=12)
        assert len(lines) == 3
        mid = lines[1]
        assert len(mid.segments) == 1
        assert mid.segments[0].display
        assert mid.segments[0].formula_id == "f0"

    def test_whole_line_formula_keeps_single_line(self):
        """整行公式被 style_runs 切成多段时，连续 display 公式合并一行。"""
        eng = InlineLayoutEngine()
        fs = [
            FormulaObject(formula_id=f"f{i}", glyphs=[],
                          bbox=(i * 20, 90, i * 20 + 15, 110), baseline=100,
                          is_display_mode=True)
            for i in range(3)
        ]
        lines = eng.wrap(fs, container_width=300, font_size=12)
        assert len(lines) == 1
        assert len(lines[0].segments) == 3


# ── P9：垂直流堆叠 ─────────────────────────────────────────


class TestDisplayVerticalFlow:
    def test_display_flow_stacks_vertical_height(self):
        """含 Display 公式的段落：后续文本基线必须位于公式块下方。"""
        para = _mk_display_flow_para()
        FormulaExtractor().extract_paragraph(para)
        unit = build_translation_unit(para)
        solver = LayoutSolver()
        solved = solver.solve(unit, unit.text)
        displays = [p for p in solved.formula_placements if p.get("display")]
        assert len(displays) == 2, solved.formula_placements
        # 后续文本行（文本段行）基线在全部公式底边之下
        formula_bottom = min(p["render_bbox"][1] for p in displays)
        text_bls = [
            l.master_baseline
            for l in solved.lines
            if not any(getattr(s, "display", False) for s in l.segments)
        ]
        assert text_bls, "期望存在纯文本行"
        assert min(text_bls) < formula_bottom, \
            "译文文本基线必须下推到展示公式下方（垂直流堆叠）"
        # translated_bbox 必须覆盖公式物理高度
        assert solved.translated_bbox[1] <= formula_bottom + 1e-6
        # 水平：display 公式保持源 x0（零水平漂移）
        for p in displays:
            assert abs(p["render_bbox"][0] - p["source_bbox"][0]) < 1e-6

    def test_no_display_keeps_source_baseline_mapping(self):
        """无 Display 公式的段落仍走源行基线映射（回归零漂移契约）。"""
        para = _mk_mixed_inline_para()
        FormulaExtractor().extract_paragraph(para)
        unit = build_translation_unit(para)
        solver = LayoutSolver()
        solved = solver.solve(unit, unit.text)
        assert not any(p.get("display") for p in solved.formula_placements)
        p = solved.formula_placements[0]
        assert abs(p["render_bbox"][0] - p["source_bbox"][0]) < 1e-6
        assert abs(p["render_bbox"][1] - p["source_bbox"][1]) < 1e-6

# ── P10：Redact 覆盖 ───────────────────────────────────────


class TestRedactCoverage:
    def _mk_solved(self):
        from pdf2zh.layout.inline_layout import InlineSegment, LayoutLine
        from pdf2zh.layout.solver import SolvedUnit
        return SolvedUnit(
            unit_id="u0",
            source_bbox=(10.0, 96.0, 300.0, 112.0),
            translated_bbox=(10.0, 96.0, 300.0, 112.0),
            render_bbox=(10.0, 96.0, 300.0, 112.0),
            font_size=12.0,
            line_count=1,
            lines=[LayoutLine(
                segments=[InlineSegment(kind="text", text="translated text",
                                        width=90.0)],
                master_baseline=104.0,
            )],
            formula_placements=[],
        )

    def test_apply_to_pdf_redacts_source_layer(self):
        """绘制新译文前，源 bbox 区域被 Redact 清空（旧图层不残留）。"""
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=300)
        page.insert_text((20, 196), "Original English Text", fontsize=12)
        patch = DualPatcher().synthesize([self._mk_solved()],
                                         "translated text", {})
        n = DualPatcher().apply_to_pdf(doc, 0, patch, fontname="helv")
        assert n == 1
        text = doc[0].get_text()
        assert "Original" not in text, "源图层必须被 redact 擦除"
        assert "translated" in text, "新译文必须写入"
        # 补丁携带 source_bbox（供上层消费）
        assert patch.patches[0]["source_bbox"] == [10.0, 96.0, 300.0, 112.0]

    def test_render_no_text_formula_overlap(self):
        """真实渲染落位（规范 §4.1）：译文文本行与公式框 2D 不相交。

        多行 display 公式 + 后续文本经 LayoutSolver 垂直流求解后，文本行
        必须位于展示公式块之下 —— 用 solver 的 render 几何（同一 y-up
        坐标系、同源字形度量）逐对断言文本行 bbox 与公式 render_bbox
        垂直分离（IoU == 0）。修复前 vertical flow 未接线时译文直接画在
        展示公式上方（IoU > 0）。
        """
        para = _mk_display_flow_para()
        FormulaExtractor().extract_paragraph(para)
        unit = build_translation_unit(para)
        solver = LayoutSolver()
        solved = solver.solve(unit, unit.text)
        displays = [p for p in solved.formula_placements if p.get("display")]
        assert len(displays) == 2, "两行展示公式必须被识别"
        fs = solved.font_size
        text_lines = [l for l in solved.lines
                      if not any(getattr(s, "display", False)
                                 for s in l.segments)]
        assert text_lines, "必须存在文本行"
        for line in text_lines:
            tb = (unit.source_bbox[0], line.master_baseline - fs * 0.2,
                  unit.source_bbox[2], line.master_baseline + fs * 0.8)
            for p in displays:
                fb = p["render_bbox"]
                # y-up：行框与公式框垂直分离 → 2D IoU == 0
                assert tb[3] <= fb[1] or tb[1] >= fb[3], \
                    f"文本行 {[round(x, 1) for x in tb]} 与公式 " \
                    f"{[round(x, 1) for x in fb]} 2D 重叠"
        # 渲染冒烟：DualPatcher 真实落位到 PDF，页面产出译文文本
        pymupdf = pytest.importorskip("pymupdf")
        patch = DualPatcher().synthesize([solved], "", {})
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=300)
        DualPatcher().apply_to_pdf(doc, 0, patch, fontname="helv")
        assert any(w[4].strip() for w in page.get_text("words")), \
            "渲染页必须含译文文本"

    def test_iou_utility(self):
        """IoU 计算（规范 §4.1 判据）：分离框=0，重叠框>0。"""
        a = (0, 0, 10, 10)
        assert _iou(a, (20, 0, 30, 10)) == 0.0       # 水平分离
        assert _iou(a, (0, 20, 10, 30)) == 0.0       # 垂直分离
        assert _iou(a, (0, 0, 10, 10)) == 1.0        # 完全重合
        assert 0.0 < _iou(a, (5, 5, 15, 15)) < 1.0   # 部分重叠

    def test_single_symbol_suppressed_to_text(self):
        """孤立基础运算符（= / ≠ / +）强制回退普通文本（规范 §3.4 模块 4）。

        修复前：数学字体下 ``=``（CMSY10）得分 0.577 → ambiguous，经
        ``font>=0.85 + structure_hint`` 提升为 FormulaObject 并锁定坐标，
        译文行首被撕裂成「= 是自反吗？」；修复后必须判为普通 TextRun。
        """
        from pdf2zh.formula.confidence import (
            FormulaConfidenceEngine, is_single_operator)
        eng = FormulaConfidenceEngine()
        # 数学字体孤立运算符：总分必须 < 0.45（回退 text）
        for ch, font in [("=", "CMSY10"), ("≠", "CMSY10"), ("+", "CMSY10"),
                         ("=", "CMMI10"), ("<", "CMSY10")]:
            g = [mk_glyph(ch, 0, 100, 14, font)]
            s = eng.score(ch, g, font)
            assert s.verdict == "text", (ch, s.verdict)
            assert s.total < 0.45, (ch, s.total)
            assert is_single_operator(ch)
        # 组合公式不受影响：``a = b`` 多字符组合不命中单运算符白名单
        text = "a=b"
        glyphs = [mk_glyph(c, i * 12, 100, 14, "CMMI10") for i, c in
                  enumerate(text)]
        assert not is_single_operator(text), "组合公式不得被误伤为孤立运算符"
        # 且提取层仍把「整行组合公式」提升为公式对象（含 ``=`` 的结构信号）
        lines = VisualLineBuilder().build(glyphs, page_id=0)
        para = build_logical_paragraphs(lines, page_id=0)[0]
        objs = FormulaExtractor().extract_paragraph(para)
        assert any(isinstance(o, FormulaObject) for o in objs), \
            "整行组合公式仍是公式强信号"

    def test_whole_line_math_hint_rejects_lone_operator(self):
        """整行判定：独占一行的孤立 ``=`` 不再被提升为公式对象（病灶三）。"""
        glyphs = [mk_glyph("=", 0, 100, 14, "CMSY10")]
        ext = FormulaExtractor()
        assert not ext._whole_line_math_hint(glyphs, "=")
        # 含变量/文字组合仍提升（``R_1 = {(1,1)}`` 整行公式强信号）
        g2 = [mk_glyph(c, i * 12, 100, 14, "CMMI10") for i, c in
              enumerate("R1={(1,1)}")]
        assert ext._whole_line_math_hint(g2, "R1={(1,1)}")
