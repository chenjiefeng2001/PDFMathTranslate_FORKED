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
    BlockModel, LineModel, PageModel, SpanModel, annotate_style,
    apply_layout_splits,
)
from pdf2zh.v3.document_inspector import build_layout_report, inspect_layout
from pdf2zh.v3.document_model import render_plan_from_model


def _span(text, size, font="Body", x0=None, x1=None):
    return SpanModel(font=font, size=size, text=text,
                     x0=x0 if x0 is not None else 0.0,
                     y0=0.0, x1=x1 if x1 is not None else 0.0, y1=10.0)


def _line(text, spans, x0=0.0, x1=100.0):
    return LineModel(text=text, x0=x0, y0=0.0, x1=x1, y1=10.0, spans=spans)


def _page_with(blocks):
    page = PageModel(page_num=1, blocks=list(blocks))
    annotate_style(page)
    return page


class TestFontResolution(unittest.TestCase):
    def test_major_font_not_max(self):
        # 混入少量大字 span：font_size 应为 major（12）而非 max（24）
        line = _line("ABCdx", [
            _span("ABCd", 12, "Body"), _span("x", 24, "Sup"),
        ])
        block = BlockModel(text="ABCdx", kind="paragraph",
                           x0=0, x1=100, lines=[line])
        page = _page_with([block])
        b = page.blocks[0]
        self.assertEqual(b.metadata["font_size"], 12.0)
        self.assertEqual(b.metadata["font_size_max"], 24.0)
        self.assertEqual(b.metadata["font_major"], "Body")
        self.assertAlmostEqual(b.metadata["font_size_ratio"], 2.0, places=2)
        self.assertFalse(b.metadata["font_uniform"])

    def test_uniform_small_block(self):
        line = _line("hello", [
            _span("hello", 12, "Body"),
        ])
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
        self.assertEqual(_line_alignment(_line("body", [], 0.0, 100.0),
                                         0.0, 100.0), "left")
        self.assertEqual(_line_alignment(_line("t", [], 40.0, 60.0),
                                         0.0, 100.0), "center")
        self.assertEqual(_line_alignment(_line("r", [], 80.0, 100.0),
                                         0.0, 100.0), "right")


class TestLayoutSplits(unittest.TestCase):
    def _merged_block(self, lines):
        block = BlockModel(lines=lines, x0=min(l.x0 for l in lines),
                           x1=max(l.x1 for l in lines))
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
        block = BlockModel(kind="paragraph",
                           x0=0, x1=300, lines=[title, body])
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        apply_layout_splits(page)
        report = build_layout_report(DocumentModel(pages=[page]))
        self.assertTrue(any(i["kind"] == "split" for i in report["issues"]))


class TestConsumption(unittest.TestCase):
    def test_render_plan_uses_resolved_font(self):
        # Resolved font_size=12（major）；旧行为 font_size(max)=24 会把整段抬大
        line = _line("ABCdx", [_span("ABCd", 12, "Body"), _span("x", 24, "Sup")])
        block = BlockModel(kind="paragraph", text=line.text,
                           x0=0, y0=0, x1=100, y1=10, lines=[line])
        page = PageModel(page_num=1, blocks=[block])
        annotate_style(page)
        plan = render_plan_from_model(
            __import__("pdf2zh.v3.document_model", fromlist=["DocumentModel"])
            .DocumentModel(pages=[page]))
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
            "errors": 0, "warnings": 0, "admissible": True, "issues": [],
        }
        svc = RuntimeService()
        diag, heal, recs, conf = svc._collect_legacy_diagnostics(
            {"document_model": dm})
        self.assertIsNotNone(diag)
        self.assertIn("layout", diag)
        self.assertIsInstance(diag["layout"]["stats"]["blocks"], int)

    def test_healing_markdown_renders_layout(self):
        from pdf2zh.gui.components.diagnostic_panel import build_healing_markdown
        md = build_healing_markdown(diagnostic_report={
            "errors": 0, "warnings": 1, "admissible": True,
            "layout": {
                "paragraphs": [{
                    "block_id": "p1_0", "kind": "paragraph", "text": "Hi",
                    "lines": 1, "font_size": 12.0, "font_size_max": 24.0,
                    "font_size_ratio": 2.0, "alignment": "left",
                    "layout_split": True,
                }],
                "issues": [{"kind": "size_blend", "node": "p1_0", "why": "x"}],
                "stats": {"blocks": 1, "issues": 1},
            },
        })
        self.assertIn("Layout", md)
        self.assertIn("p1_0", md)


if __name__ == "__main__":
    unittest.main()