# -*- coding: utf-8 -*-
"""V1.17 — Phase 5.2 修复：TOCAnalyzer（目录块边界恢复 + 条目结构化）。

覆盖（对应「TOC Entry 分割失败 → Block Boundary 恢复失败」的诊断结论）：
- parse_entry_text：编号 + 标题 + 页码（点线/空列两种）逐行解析；非目录行 None；
- split_merged_block：被压成一个块的多条目录行按 ≥0.5 命中率重切（纯语义，
  不改 geometry）；普通段落块返回空；
- 页码独立检测：几何列（x > 0.8×width 的纯数字 span）优先，文本回退；
- rebuild_toc_page / analyze_toc_result：条目 → 层级树（build_toc_tree）；
- split_toc_blocks：Semantic Pass 就地重切 page.blocks → 逐条 kind="toc"，
  与 annotate_toc_scan 元数据格式一致；
- 集成：build_document_model 对多行目录块先拆分再自扫描；
- render_toc_entry：目录条目专用渲染（title --- page，页码不翻译）。
"""

import unittest
from unittest.mock import Mock

from pdfminer.layout import LTChar, LTPage

from pdf2zh.v3.canonical_page import BlockModel, LineModel, PageModel, SpanModel
from pdf2zh.v3.document_model import build_document_model
from pdf2zh.v3.toc_analyzer import (
    analyze_toc_blocks,
    analyze_toc_result,
    parse_entry_text,
    rebuild_toc_page,
    render_toc_entry,
    split_merged_block,
    split_toc_blocks,
)


def make_char(x, y, text="A", size=10.0, fontname="Helvetica"):
    font = Mock()
    font.fontname = fontname
    font.get_descent.return_value = -0.25
    ch = LTChar(
        (1, 0, 0, 1, x, y),
        font,
        size,
        1.0,
        0.0,
        text,
        textwidth=0.5,
        textdisp=(0.0, 0.0),
        ncs=Mock(),
        graphicstate=Mock(),
    )
    ch.cid = ord(text[0])
    ch.font = font
    return ch


def add_text(page, x0, y, text, adv=9.0, fontname="Helvetica", size=10.0):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname, size=size))


def merged_block(
    text="2.3 Continuous Random Variables 31\n"
    "2.3.1 Uniform Random Variables 32\n"
    "2.3.2 Expectations 33",
):
    lines = [LineModel(text=ln) for ln in text.split("\n")]
    b = BlockModel(text=text, lines=lines)
    return b


def page_with_merged():
    page = PageModel(page_num=3, width=600.0, height=800.0)
    page.blocks.append(merged_block())
    page.blocks.append(
        BlockModel(
            text="Some normal paragraph that spans",
            lines=[LineModel(text="Some normal " "paragraph")],
        )
    )
    return page


class TestParseEntryText(unittest.TestCase):
    def test_dotted_page(self):
        r = parse_entry_text("2.3 Continuous Random Variables 31")
        self.assertIsNotNone(r)
        self.assertEqual(r.number, "2.3")
        self.assertEqual(r.title, "Continuous Random Variables")
        self.assertEqual(r.page, "31")

    def test_space_page_column(self):
        r = parse_entry_text("2.3.1 Uniform Random Variables 32")
        self.assertIsNotNone(r)
        self.assertEqual(r.page, "32")
        self.assertEqual(r.title, "Uniform Random Variables")

    def test_leader_dots(self):
        r = parse_entry_text("2.3 Continuous Random Variables....31")
        self.assertEqual(r.page, "31")
        self.assertEqual(r.title, "Continuous Random Variables")

    def test_flat_chapter(self):
        r = parse_entry_text("2 Introduction 1")
        self.assertEqual(r.number, "2")
        self.assertEqual(r.page, "1")

    def test_rejects_body_text(self):
        self.assertIsNone(parse_entry_text("The kernel scheduler runs " "threads."))
        self.assertIsNone(parse_entry_text("31"))
        self.assertIsNone(parse_entry_text("2.3"))


class TestSplitMergedBlock(unittest.TestCase):
    def test_merged_toc_split(self):
        entries = split_merged_block(merged_block(), 600.0)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["number"], "2.3")
        self.assertEqual(entries[0]["page"], "31")
        self.assertEqual(entries[2]["number"], "2.3.2")

    def test_single_line_not_split(self):
        b = BlockModel(
            text="2.3 Continuous Random Variables 31",
            lines=[LineModel(text="2.3 Continuous Random " "Variables 31")],
        )
        self.assertEqual(split_merged_block(b, 600.0), [])

    def test_body_block_not_split(self):
        b = BlockModel(
            text="First line of paragraph.\nSecond line with 42.",
            lines=[
                LineModel(text="First line of paragraph."),
                LineModel(text="Second line with 42."),
            ],
        )
        self.assertEqual(split_merged_block(b, 600.0), [])

    def test_geometric_page_column(self):
        def line(text, px0):
            return LineModel(
                text=text,
                spans=[
                    SpanModel(text=text, x0=50.0, x1=300.0),
                    SpanModel(text="31", x0=px0, x1=px0 + 20.0),
                ],
            )

        b = BlockModel(
            text="2.3 Continuous Random Variables\n" "2.3.1 Uniform Random Variables",
            lines=[
                line("2.3 Continuous Random Variables", 520.0),
                line("2.3.1 Uniform Random Variables", 540.0),
            ],
        )
        entries = split_merged_block(b, 600.0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["page"], "31")
        self.assertEqual(entries[1]["page"], "31")

    def test_geometric_ignores_inner_digits(self):
        def line(text):
            return LineModel(text=text, spans=[SpanModel(text=text, x0=50.0, x1=300.0)])

        b = BlockModel(
            text="2.3 Data 2024 Report\n2.3.1 Appendix B",
            lines=[line("2.3 Data 2024 Report"), line("2.3.1 Appendix B")],
        )
        entries = split_merged_block(b, 600.0)
        self.assertEqual(len(entries), 0)


class TestTocTreeAndResult(unittest.TestCase):
    def test_rebuild_toc_page(self):
        page = page_with_merged()
        tree = rebuild_toc_page(page)
        self.assertIn("roots", tree)
        self.assertIn("nodes", tree)
        nodes = tree["nodes"]
        self.assertGreaterEqual(len(nodes), 3)

    def test_analyze_toc_result(self):
        res = analyze_toc_result(page_with_merged())
        self.assertEqual(res["count"], 3)
        self.assertEqual(res["entries"][0]["number"], "2.3")
        self.assertIn("tree", res)

    def test_analyze_toc_blocks_marks_indices(self):
        page = page_with_merged()
        be = analyze_toc_blocks(page.blocks, page.width)
        merged = [x for x in be if x["entries"]]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["index"], 0)


class TestSplitTocBlocks(unittest.TestCase):
    def test_restructures_blocks(self):
        page = page_with_merged()
        before = len(page.blocks)
        splits = split_toc_blocks(page)
        self.assertEqual(splits, 1)
        # 3 条目录逐行独立 + 1 个正文块
        self.assertEqual(len(page.blocks), before + 2)
        toc_blocks = [b for b in page.blocks if b.kind == "toc"]
        self.assertEqual(len(toc_blocks), 3)
        self.assertEqual(toc_blocks[0].metadata["toc_number"], "2.3")
        self.assertEqual(toc_blocks[0].metadata["toc_page"], "31")
        self.assertTrue(toc_blocks[0].metadata["toc_scan"])
        self.assertEqual(toc_blocks[1].metadata["toc_number"], "2.3.1")

    def test_body_untouched(self):
        page = page_with_merged()
        split_toc_blocks(page)
        body = [b for b in page.blocks if b.kind == "paragraph"]
        self.assertEqual(len(body), 1)
        self.assertIn("normal", body[0].text)

    def test_no_toc_page_untouched(self):
        page = PageModel(page_num=1, width=600.0, height=800.0)
        page.blocks.append(
            BlockModel(
                text="Just a paragraph with no toc.",
                lines=[LineModel(text="Just a paragraph with no " "toc.")],
            )
        )
        self.assertEqual(split_toc_blocks(page), 0)
        self.assertEqual(len(page.blocks), 1)


class TestIntegrationBuildDocumentModel(unittest.TestCase):
    def test_merged_toc_split_in_model(self):
        # 真实失败场景：无点线引导的目录行（空格页码列）被 Geometry 并成一个块，
        # split_toc_blocks 在语义层重切为独立条目。
        page = LTPage(2, (0, 0, 800, 800))
        add_text(page, 50, 740, "5.1 Data Collection 10")
        add_text(page, 50, 735, "5.2 Processing 12")
        add_text(page, 50, 730, "5.3 Results 21")
        add_text(page, 50, 700, "The kernel scheduler runs threads.", fontname="Times")
        model = build_document_model([page])
        pm = model.pages[0]
        toc_blocks = [b for b in pm.blocks if b.kind == "toc"]
        self.assertEqual(len(toc_blocks), 3)
        nums = [b.metadata["toc_number"] for b in toc_blocks]
        self.assertIn("5.1", nums)
        self.assertIn("5.2", nums)
        self.assertIn("5.3", nums)
        pages = [b.metadata["toc_page"] for b in toc_blocks]
        self.assertEqual(sorted(pages), ["10", "12", "21"])


class TestRenderTocEntry(unittest.TestCase):
    def test_render_line(self):
        line = render_toc_entry("2.3.1", "Uniform Random Variables", "32", level=1)
        self.assertEqual(line, "    2.3.1 Uniform Random Variables ... 32")

    def test_render_page_not_translated(self):
        line = render_toc_entry("2.3", "Continuous Random Variables", "31")
        self.assertIn("... 31", line)
        self.assertIn("31", line)


if __name__ == "__main__":
    unittest.main()
