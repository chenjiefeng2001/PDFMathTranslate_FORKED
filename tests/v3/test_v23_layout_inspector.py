# -*- coding: utf-8 -*-
"""V1.23 — Layout Inspector + Font Resolution + L2 段拆（排版级联修复）。

覆盖：
- L3 Font Resolution：字号来源 major（加权众数）而非 max/avg；
- L4 对齐检测：center/left/right + 逐行 line_alignments；
- L2 Paragraph 拆块：字号跳变 ≥1.6× / 对齐翻转 → 独立块 + provenance；
- Layout Inspector：inspect_layout / build_layout_report 逐段证据输出；
- 消费端：render_plan_from_model / to_graph 用 resolved font_size；
- Runtime 侧通道：diagnostic_report 挂 layout；
- GUI：build_healing_markdown 渲染 Layout Inspector 段落。
"""

import unittest

from pdf2zh.v3.canonical_page import (
    BlockModel,
    LineModel,
    PageModel,
    SpanModel,
    annotate_style,
    apply_layout_splits,
)
from pdf2zh.v3.document_inspector import build_layout_report, inspect_layout
from pdf2zh.v3.document_model import render_plan_from_model


def _span(text, size, font="Body", x0=None, x1=None):
    return SpanModel(
        font=font,
        size=size,
        text=text,
        x0=x0 if x0 is not None else 0.0,
        y0=0.0,
        x1=x1 if x1 is not None else 0.0,
        y1=10.0,
    )


def _line(text, spans, x0=0.0, x1=100.0):
    return LineModel(text=text, x0=x0, y0=0.0, x1=x1, y1=10.0, spans=spans)


def _page_with(blocks):
    page = PageModel(page_num=1, blocks=list(blocks))
    annotate_style(page)
    return page


class TestFontResolution(unittest.TestCase):
    def test_major_font_not_max(self):
        # 混入少量大字 span：font_size 应为 major（12）而非 max（24）
        line = _line(
            "ABCdx",
            [
                _span("ABCd", 12, "Body"),
                _span("x", 24, "Sup"),
            ],
        )
        block = BlockModel(text="ABCdx", kind="paragraph", x0=0, x1=100, lines=[line])
        page = _page_with([block])
        b = page.blocks[0]
        self.assertEqual(b.metadata["font_size"], 12.0)
        self.assertEqual(b.metadata["font_size_max"], 24.0)
        self.assertEqual(b.metadata["font_major"], "Body")
        self.assertAlmostEqual(b.metadata["font_size_ratio"], 2.0, places=2)
        self.assertFalse(b.metadata["font_uniform"])

    def test_uniform_small_block(self):
        line = _line(
            "hello",
            [
                _span("hello", 12, "Body"),
            ],
        )
        block = BlockModel(lines=[line])
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        b = page.blocks[0]
        self.assertEqual(b.metadata["font_size"], 12.0)
        self.assertEqual(b.metadata["font_size_max"], 12.0)
        self.assertEqual(b.metadata["font_size_ratio"], 1.0)
        self.assertTrue(b.metadata["font_uniform"])

    def test_line_alignment_detected(self):
        # 占满块宽的行 → left；两侧余量相等 → center
        from pdf2zh.v3.canonical_page import _line_alignment

        self.assertEqual(
            _line_alignment(_line("body", [], 0.0, 100.0), 0.0, 100.0), "left"
        )
        self.assertEqual(
            _line_alignment(_line("t", [], 40.0, 60.0), 0.0, 100.0), "center"
        )
        self.assertEqual(
            _line_alignment(_line("r", [], 80.0, 100.0), 0.0, 100.0), "right"
        )


class TestLayoutSplits(unittest.TestCase):
    def _merged_block(self, lines):
        block = BlockModel(
            lines=lines, x0=min(l.x0 for l in lines), x1=max(l.x1 for l in lines)
        )
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        return page

    def test_size_jump_splits(self):
        title = _line("Title", [_span("Title", 20, "Head")], 200.0, 300.0)
        body_a = _line("Body one", [_span("Body one", 10, "Body")], 0.0, 100.0)
        body_b = _line("Body two", [_span("Body two", 10, "Body")], 0.0, 100.0)
        page = self._merged_block([title, body_a, body_b])
        splits = apply_layout_splits(page)
        self.assertEqual(splits, 1)
        self.assertEqual(len(page.blocks), 2)
        self.assertTrue(page.blocks[0].metadata.get("layout_split"))
        self.assertIn("size:", page.blocks[0].metadata.get("layout_provenance", ""))
        self.assertEqual(page.blocks[0].lines[0].text, "Title")
        self.assertEqual(page.blocks[1].metadata["font_size"], 10.0)

    def test_alignment_flip_splits(self):
        title = _line("Abstract", [_span("Abstract", 12, "Body")], 45.0, 65.0)
        body = _line("Body text", [_span("Body text", 12, "Body")], 10.0, 100.0)
        page = self._merged_block([title, body])
        splits = apply_layout_splits(page)
        self.assertEqual(splits, 1)
        self.assertEqual(len(page.blocks), 2)
        self.assertIn("align:", page.blocks[0].metadata.get("layout_provenance", ""))

    def test_no_spurious_split(self):
        a = _line("Line one", [_span("Line one", 12, "Body")], 10.0, 100.0)
        b = _line("Line two", [_span("Line two", 12, "Body")], 10.0, 100.0)
        c = _line("Line three", [_span("Line three", 11.5, "Body")], 10.0, 100.0)
        page = self._merged_block([a, b, c])
        self.assertEqual(apply_layout_splits(page), 0)
        self.assertEqual(len(page.blocks), 1)


class TestJustifiedParagraphNotShattered(unittest.TestCase):
    """真实书籍排版下的对齐判定（回归护栏）。

    缺陷
    ----
    ``_line_alignment`` 曾用「两侧余量 > 2pt」判居中。实测 325 页英文书
    （Harvard 出版社正文）的行盒余量只有 **2–3pt** —— 那是两端对齐的自然抖动。
    门槛低于它，「是否居中」由亚点取整噪声决定，于是正文行被随机判成 center，
    而 ``apply_layout_splits`` 把「相邻行对齐不同」当段落边界，把一段 18 行正文
    切成 **11** 段。后果：每段被孤立翻译（丢上下文，产出「白人It的出现」这类
    碎片），且重排后长度对不上原行盒，渲染出空洞与叠字。50 页实测 292 次切分。

    本类全部使用**实测几何**（块宽 329pt、行余量 2–3pt），而不是随手编的数字 ——
    编出来的数字恰好落在阈值另一侧，就复现不了缺陷。
    """

    # 实测：block bbox x=[45, 374]，18 行正文，行余量在 2–3pt 之间浮动。
    BOX_X0, BOX_X1 = 45.0, 374.0
    #: 第 5 行实测余量 3.0/3.0 —— 修复前被判成 center 并在此处切开。
    BALANCED_INSET = 3.0

    def _justified_line(self, idx):
        inset = 2.0 + (idx % 3) * 0.5  # 2.0 / 2.5 / 3.0 抖动
        return _line(
            f"line {idx}",
            [_span(f"line {idx}", 9.35, "Body")],
            self.BOX_X0 + inset,
            self.BOX_X1 - inset,
        )

    def test_sub_point_insets_are_not_centred(self):
        """核心断言：2–3pt 的余量是两端对齐噪声，不是居中。"""
        from pdf2zh.v3.canonical_page import _line_alignment

        verdict = _line_alignment(
            _line(
                "x",
                [],
                self.BOX_X0 + self.BALANCED_INSET,
                self.BOX_X1 - self.BALANCED_INSET,
            ),
            self.BOX_X0,
            self.BOX_X1,
        )
        self.assertEqual(
            verdict,
            "left",
            f"行余量仅 {self.BALANCED_INSET}pt（块宽 329pt）却判成 {verdict!r}；"
            "居中门槛必须高于两端对齐的自然抖动余量",
        )

    def test_justified_paragraph_survives_intact(self):
        lines = [self._justified_line(i) for i in range(18)]
        page = _page_with(
            [
                BlockModel(
                    kind="paragraph",
                    x0=self.BOX_X0,
                    x1=self.BOX_X1,
                    y0=0,
                    y1=200,
                    lines=lines,
                )
            ]
        )
        splits = apply_layout_splits(page)
        self.assertEqual(
            splits,
            0,
            f"18 行两端对齐正文被切了 {splits} 段（修复前为 11）；"
            f"provenance={[b.metadata.get('layout_provenance') for b in page.blocks]}",
        )
        self.assertEqual(len(page.blocks), 1)
        self.assertEqual(len(page.blocks[0].lines), 18)

    def test_genuinely_centred_title_still_splits(self):
        """收紧门槛不能把真正的级联防护一起关掉。

        标题被并入正文段会引发字号级联放大（旧缺陷），所以「窄标题 + 宽正文」
        必须仍然切开。
        """
        centred = _line(
            "Abstract",
            [_span("Abstract", 12, "Body")],
            self.BOX_X0 + 64.0,
            self.BOX_X1 - 64.0,
        )
        body = _line(
            "Body text",
            [_span("Body text", 12, "Body")],
            self.BOX_X0,
            self.BOX_X1,
        )
        page = _page_with(
            [
                BlockModel(
                    kind="paragraph",
                    x0=self.BOX_X0,
                    x1=self.BOX_X1,
                    y0=0,
                    y1=200,
                    lines=[centred, body],
                )
            ]
        )
        self.assertEqual(apply_layout_splits(page), 1)
        self.assertIn("align:", page.blocks[0].metadata.get("layout_provenance", ""))

    def test_centre_gate_scales_with_block_width(self):
        """门槛取「固定下限」与「块宽比例」的**较大者**，两个项都得起作用。

        同一段 10pt 余量：600pt 块里只占 1.7%（两端对齐噪声），60pt 块里占
        17%（明显居中）。若只看固定下限，两种宽度会给出同一个结论，比例项
        就是死代码。
        """
        from pdf2zh.v3.canonical_page import _line_alignment

        inset = 10.0  # clears the 8pt floor, so the ratio term decides
        wide = _line_alignment(_line("x", [], inset, 600.0 - inset), 0.0, 600.0)
        narrow = _line_alignment(_line("x", [], inset, 60.0 - inset), 0.0, 60.0)
        self.assertEqual(wide, "left", "600pt 块里的 10pt 余量不该算居中")
        self.assertEqual(
            narrow, "center", "60pt 块里的 10pt 余量（两侧各 17%）应算居中"
        )

    def test_full_width_line_is_left_not_right(self):
        """两端对齐的满宽行两侧余量为 0，必须判 left（修复前的平衡分支保证）。"""
        from pdf2zh.v3.canonical_page import _line_alignment

        self.assertEqual(
            _line_alignment(_line("x", [], 45.0, 374.0), 45.0, 374.0), "left"
        )

    def test_flush_left_line_with_short_tail_is_left(self):
        """左顶格但收尾短的行不是居中 —— 两边余量可以都非零且悬殊。"""
        from pdf2zh.v3.canonical_page import _line_alignment

        verdict = _line_alignment(_line("x", [], 48.0, 320.0), 45.0, 374.0)
        self.assertEqual(verdict, "left", f"顶格行被判成 {verdict!r}")

    def test_line_overhanging_the_block_box_is_left(self):
        """行盒越出块 bbox（负余量）时按「没有留白」算，而不是翻转判定。

        实测：某段块 bbox x=[45,372]，而其中多行 x1 到 390 —— 右边余量 -18。
        不夹的话负号会翻转「哪侧更空」的比较，且平衡分支里 abs(39.0) 与
        tol(39.2) 只差 0.2pt 就把同一段正文判成 right 又判回 left，于是被
        「对齐翻转」切开。
        """
        from pdf2zh.v3.canonical_page import _line_alignment

        # 行从 x=68 起、伸到 390，块只有 45..372 -> left=23, right=-18
        verdict = _line_alignment(_line("x", [], 68.0, 390.0), 45.0, 372.0)
        self.assertEqual(
            verdict,
            "left",
            f"越界行被判成 {verdict!r}；负余量必须夹到 0 再比较",
        )

    def test_line_overhanging_both_sides_is_left_not_centred(self):
        """两侧都越界的满宽行是正文，不是居中。

        夹到 0 给出 (0, 0) → 非居中。若改用 ``abs()``，同样这行会得到
        (25, 28) → 「大致对称且两侧都超过门槛」→ 被判成居中，于是整段正文
        在「对齐翻转」名下被切开。夹取与取绝对值在这里结论相反，必须钉住。
        """
        from pdf2zh.v3.canonical_page import _line_alignment

        verdict = _line_alignment(_line("x", [], 20.0, 400.0), 45.0, 372.0)
        self.assertEqual(
            verdict,
            "left",
            f"两侧都越界的满宽行被判成 {verdict!r}；"
            "负余量应夹到 0（=没有留白），不能取绝对值（会伪装成对称留白）",
        )

    def test_overhanging_paragraph_is_not_split(self):
        """实测 page 15 的那段正文：块 bbox 比行盒窄，修复前被切成 4 段。"""
        lines = [
            _line(
                f"l{i}",
                [_span(f"l{i}", 9.35, "Body")],
                68.0 if i >= 2 else 47.0,
                390.0 if i >= 2 else 372.0,
            )
            for i in range(12)
        ]
        page = _page_with(
            [
                BlockModel(
                    kind="paragraph",
                    x0=45.0,
                    x1=372.0,
                    y0=0,
                    y1=200,
                    lines=lines,
                )
            ]
        )
        splits = apply_layout_splits(page)
        self.assertEqual(
            splits,
            0,
            f"块 bbox 窄于行盒的段落被切了 {splits} 段；"
            f"provenance={[b.metadata.get('layout_provenance') for b in page.blocks]}",
        )

    def test_asymmetric_indent_is_left_not_centred(self):
        """两侧余量必须**都**过门槛，只看较大的一侧会把缩进行判成居中。

        实测坐标是整数量化的（2.0 / 3.0 / 15.0），所以「靠一边空得多」是
        缩进（引文首行、列表项），不是居中。这类误判同样会触发段落边界切分。
        """
        from pdf2zh.v3.canonical_page import _line_alignment

        # 块宽 329pt：左缩进 30pt、右仅 9pt，明显偏左。
        verdict = _line_alignment(_line("x", [], 30.0, 320.0), 0.0, 329.0)
        self.assertEqual(
            verdict,
            "left",
            f"左缩进 30pt / 右余 9pt 的行被判成 {verdict!r}；"
            "居中要求两侧同时留出足够宽度",
        )

    def test_sub_point_inset_in_narrow_block_is_not_centred(self):
        """固定下限（而非只看块宽比例）在窄块里才起作用。

        窄块（20pt）的 6% 只有 1.2pt，而 MinerU 坐标是整数量化的 —— 1–2pt 的
        余量不含任何对齐信息。若去掉固定下限，这种噪声会被判成居中。
        """
        from pdf2zh.v3.canonical_page import (
            _ALIGN_CENTER_MIN_PT,
            _line_alignment,
        )

        narrow_w = 20.0
        inset = 1.5  # clears 6% of 20pt (1.2pt) but is below the 8pt floor
        self.assertLess(inset, _ALIGN_CENTER_MIN_PT, "样本必须落在下限之下")
        self.assertGreaterEqual(
            inset,
            0.06 * narrow_w,
            "样本必须落在比例门槛之上，否则测的是别的东西",
        )
        verdict = _line_alignment(
            _line("x", [], inset, narrow_w - inset), 0.0, narrow_w
        )
        self.assertEqual(
            verdict, "left", f"{inset}pt 余量在 {narrow_w}pt 块里被判成 {verdict!r}"
        )


class TestInspector(unittest.TestCase):
    def test_inspect_layout_rows(self):
        from pdf2zh.v3.document_model import DocumentModel

        line = _line("Hello world", [_span("Hello world", 12, "Body")])
        block = BlockModel(kind="heading", lines=[line], x0=0, x1=100)
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        doc = DocumentModel(pages=[page])
        rows = inspect_layout(doc)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["block_id"], "p1_0")
        self.assertEqual(r["kind"], "heading")
        self.assertEqual(r["font_size"], 12.0)
        self.assertEqual(r["alignment"], "left")
        self.assertIn("line_sizes", r)

    def test_layout_report_flags_size_blend(self):
        from pdf2zh.v3.document_model import DocumentModel

        line = _line("ABCdx", [_span("ABCd", 12, "Body"), _span("x", 24, "Sup")])
        block = BlockModel(lines=[line], x0=0, x1=100)
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        report = build_layout_report(DocumentModel(pages=[page]))
        self.assertIsNotNone(report)
        kinds = {i["kind"] for i in report["issues"]}
        self.assertIn("size_blend", kinds)
        self.assertGreaterEqual(report["stats"]["size_blends"], 1)

    def test_layout_report_captures_split(self):
        from pdf2zh.v3.document_model import DocumentModel

        title = _line("Title", [_span("Title", 20, "Head")], 200.0, 300.0)
        body = _line("Body", [_span("Body", 10, "Body")], 0.0, 100.0)
        block = BlockModel(kind="paragraph", x0=0, x1=300, lines=[title, body])
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        apply_layout_splits(page)
        report = build_layout_report(DocumentModel(pages=[page]))
        self.assertTrue(any(i["kind"] == "split" for i in report["issues"]))


class TestConsumption(unittest.TestCase):
    def test_render_plan_uses_resolved_font(self):
        # Resolved font_size=12（major）；旧行为 font_size(max)=24 会把整段抬大
        line = _line("ABCdx", [_span("ABCd", 12, "Body"), _span("x", 24, "Sup")])
        block = BlockModel(
            kind="paragraph", text=line.text, x0=0, y0=0, x1=100, y1=10, lines=[line]
        )
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        plan = render_plan_from_model(
            __import__(
                "pdf2zh.v3.document_model", fromlist=["DocumentModel"]
            ).DocumentModel(pages=[page])
        )
        self.assertEqual(plan[0]["font_size"], 12.0)

    def test_graph_uses_resolved_font(self):
        from pdf2zh.v3.document_model import DocumentModel

        line = _line("ABCdx", [_span("ABCd", 12, "Body"), _span("x", 24, "Sup")])
        block = BlockModel(kind="paragraph", lines=[line], x0=0, x1=100)
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        g = DocumentModel(pages=[page]).to_graph()
        node = next(n for n in g.nodes if n.id == "p1_0")
        self.assertEqual(node.font_size, 12.0)


class TestRuntimeAndGui(unittest.TestCase):
    def test_legacy_diagnostics_attach_layout(self):
        from pdf2zh.services.runtime_service import RuntimeService
        from pdf2zh.v3.document_model import DocumentModel

        line = _line("Hello", [_span("Hello", 12, "Body")])
        block = BlockModel(kind="paragraph", lines=[line], x0=10, x1=60)
        page = PageModel(page_num=1, blocks=[block])
        dm = DocumentModel(pages=[page])
        dm.metadata["diagnostics"] = {
            "errors": 0,
            "warnings": 0,
            "admissible": True,
            "issues": [],
        }
        svc = RuntimeService()
        diag, heal, recs, conf = svc._collect_legacy_diagnostics({"document_model": dm})
        self.assertIsNotNone(diag)
        self.assertIn("layout", diag)
        self.assertIsInstance(diag["layout"]["stats"]["blocks"], int)

    def test_healing_markdown_renders_layout(self):
        from pdf2zh.gui.components.diagnostic_panel import build_healing_markdown

        md = build_healing_markdown(
            diagnostic_report={
                "errors": 0,
                "warnings": 1,
                "admissible": True,
                "layout": {
                    "paragraphs": [
                        {
                            "block_id": "p1_0",
                            "kind": "paragraph",
                            "text": "Hi",
                            "lines": 1,
                            "font_size": 12.0,
                            "font_size_max": 24.0,
                            "font_size_ratio": 2.0,
                            "alignment": "left",
                            "layout_split": True,
                        }
                    ],
                    "issues": [{"kind": "size_blend", "node": "p1_0", "why": "x"}],
                    "stats": {"blocks": 1, "issues": 1},
                },
            }
        )
        self.assertIn("Layout", md)
        self.assertIn("p1_0", md)


if __name__ == "__main__":
    unittest.main()
