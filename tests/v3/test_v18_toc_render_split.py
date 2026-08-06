# -*- coding: utf-8 -*-
"""V1.17-3 — 渲染路径目录重切：legacy 合并目录段按物理行拆分 + 空列页码识别。

根因链（用户截图确认）：无点线页码列的目录行被字符循环并成一段
（``brk=True``），``detect_toc_line`` 对 brk 段落直接放弃 → 整段走普通
Paragraph→Translate→Layout 渲染，后半段目录挤成一段。

V1.17-3 修复（side-channel，不改 Parser）：
- ``toc.detect_toc_line`` 新增「空格列页码」分支：标题以编号开头 +
  页码在页面右缘列（几何 x > 0.8×页宽），点线/页码原位渲染；
- ``toc_analyzer.split_merged_toc_paragraphs``：legacy 渲染路径钩子，
  用原始字符流的物理行重切合并目录段（每行独立 sstk/pstk/toc_track），
  之后既有的 detect_toc_line 逐行识别走 toc_mode。

覆盖：
- detect_toc_line：点线页码 / 空格列页码 / 各种误报拒绝；
- split_merged_toc_paragraphs：合并段 → 逐行段（bbox/brk/track）、
  非目录段不动、含公式占位符不拆、正文多行段不拆；
- 与 converter 段落循环串联后每行被识别为 toc_line。
"""
import unittest
from unittest.mock import Mock

from pdfminer.layout import LTChar, LTPage

from pdf2zh.converter import Paragraph
from pdf2zh.toc import detect_toc_line
from pdf2zh.v3.geometry import chars_from_ltpage
from pdf2zh.v3.toc_analyzer import split_merged_toc_paragraphs


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


def add_text(page, x0, y, text, adv=9.0, size=10.0, fontname="Helvetica"):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, size=size, fontname=fontname))


def build_toc_page():
    """3 条空列页码目录行：标题(50) + 右侧页码列(520)。"""
    page = LTPage(2, (0, 0, 600, 800))
    add_text(page, 50, 740, "2.3 Continuous Random Variables")
    add_text(page, 520, 740, "31")
    add_text(page, 50, 735, "2.3.1 Uniform Random Variables")
    add_text(page, 520, 735, "32")
    add_text(page, 50, 730, "2.3.2 Expectations")
    add_text(page, 520, 730, "34")
    return page


def page_digit_track(page, x_min=0.0):
    """模拟 converter 的 toc_track：点线 + 数字字符（含几何位置）。"""
    out = []
    for child in page:
        if hasattr(child, "size"):
            t = child.get_text()
            if t in ".·…‥" or t.isdigit():
                out.append((t, child.x0, child.x1))
    return out


def merged_sstk(page):
    """模拟 converter 把 3 行目录并入一段后的 sstk/pstk/toc_track。"""
    sstk = ["2.3 Continuous Random Variables 31 2.3.1 Uniform Random Variables 32 2.3.2 Expectations 34"]
    pstk = [Paragraph(y=745.0, x=50.0, x0=50.0, x1=539.0, y0=725.0, y1=745.0, size=10.0, brk=True)]
    toc_track = [page_digit_track(page)]
    return sstk, pstk, toc_track


class TestDetectTocLineSpaceColumn(unittest.TestCase):
    """detect_toc_line：空格列页码 + 误报拒绝。"""

    def test_space_column_page(self):
        # 无点线、空列页码，标题以编号开头，页码在右缘列
        spec = detect_toc_line(
            "2.3.1 Uniform Random Variables 32", False,
            [("2", 50, 60), ("3", 68, 78), ("1", 86, 96),
             ("3", 520, 529), ("2", 529, 539)],
            539, page_width=600,
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["page_digits"], "32")
        self.assertEqual(spec["page_start_x"], 520)
        self.assertEqual(spec["page_right_x"], 539)
        self.assertEqual(spec["title"], "2.3.1 Uniform Random Variables")

    def test_space_column_flat_number(self):
        spec = detect_toc_line(
            "12 Estimation 45", False,
            [("1", 50, 60), ("2", 60, 70), ("4", 520, 530), ("5", 530, 540)],
            540, page_width=600,
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["page_digits"], "45")

    def test_reject_plain_heading(self):
        spec = detect_toc_line("5 Methodology", False, [("5", 50, 58)], 500, page_width=600)
        self.assertIsNone(spec)

    def test_reject_body_text(self):
        spec = detect_toc_line(
            "The result is 42", False,
            [("4", 200, 208), ("2", 210, 218)],
            540, page_width=600,
        )
        self.assertIsNone(spec)

    def test_reject_page_not_right_column(self):
        spec = detect_toc_line(
            "2.3 Estimation 24", False,
            [("2", 50, 60), ("3", 68, 78), ("2", 300, 310), ("4", 310, 320)],
            540, page_width=600,
        )
        self.assertIsNone(spec)

    def test_reject_brk_paragraph(self):
        spec = detect_toc_line(
            "2.3 A 31 2.3.1 B 32", True,
            [("3", 520, 530), ("1", 529, 539)], 539, page_width=600,
        )
        self.assertIsNone(spec)

    def test_leader_dot_still_works(self):
        spec = detect_toc_line(
            "Chapter 2 Abc...............45", False,
            [(".", 200, 201), (".", 210, 211), (".", 220, 221),
             ("4", 520, 530), ("5", 530, 540)],
            540, page_width=600,
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["page_right_x"], 540)
        self.assertEqual(spec["page_digits"], "45")


class TestSplitMergedTocParagraphs(unittest.TestCase):
    """split_merged_toc_paragraphs：legacy 渲染路径重切。"""

    def setUp(self):
        self.page = build_toc_page()

    def test_split_merged_paragraph(self):
        sstk, pstk, toc_track = merged_sstk(self.page)
        report = split_merged_toc_paragraphs(
            Mock(), self.page, sstk, pstk, toc_track, page_width=600.0)
        self.assertEqual(report["split"], 1)
        self.assertEqual(len(sstk), 3)
        self.assertTrue(all(p.brk is False for p in pstk))
        # 每行 bbox 与页码列一致（页码在 x=520..539）
        self.assertEqual(pstk[0].x0, 50)
        self.assertEqual(pstk[0].x1, 539)
        self.assertEqual(pstk[2].x1, 539)
        # track per row：只含该行内的数字（标题编号 + 页码）
        digits_seq = "".join(ch for ch, _, _ in toc_track[1])
        self.assertIn("32", digits_seq)
        self.assertNotIn("34", digits_seq)

    def test_no_split_when_brk_false(self):
        sstk = ["2.3 Continuous Random Variables 31"]
        pstk = [Paragraph(745, 50, 50, 539, 725, 745, 10.0, False)]
        toc_track = [page_digit_track(self.page)[:2]]
        report = split_merged_toc_paragraphs(
            Mock(), self.page, sstk, pstk, toc_track, page_width=600.0)
        self.assertEqual(report["split"], 0)
        self.assertEqual(len(sstk), 1)

    def test_no_split_formula_placeholder(self):
        sstk = ["2.3 Critical {v0} 31 2.3.1 Uniform 32"]
        pstk = [Paragraph(745, 50, 50, 539, 725, 745, 10.0, True)]
        toc_track = [page_digit_track(self.page)]
        report = split_merged_toc_paragraphs(
            Mock(), self.page, sstk, pstk, toc_track, page_width=600.0)
        self.assertEqual(report["split"], 0)

    def test_no_split_body_paragraph(self):
        body = build_toc_page()
        add_text(body, 50, 720, "This is a normal paragraph")
        add_text(body, 50, 715, "continuing on, no page numbers")
        sstk = ["This is a normal paragraph <NOPAGE>"]
        pstk = [Paragraph(745, 50, 50, 260, 715, 725, 10.0, True)]
        toc_track = [[]]
        report = split_merged_toc_paragraphs(
            Mock(), body, sstk, pstk, toc_track, page_width=600.0)
        self.assertEqual(report["split"], 0)

    def test_integration_detect_after_split(self):
        sstk, pstk, toc_track = merged_sstk(self.page)
        split_merged_toc_paragraphs(
            Mock(), self.page, sstk, pstk, toc_track, page_width=600.0)
        specs = [detect_toc_line(s, p.brk, t, p.x1, page_width=600.0)
                 for s, p, t in zip(sstk, pstk, toc_track)]
        self.assertTrue(all(sp is not None for sp in specs))
        self.assertEqual(specs[1]["page_digits"], "32")
        self.assertEqual(specs[2]["page_start_x"], 520)

    def test_split_with_non_numbered_preface_rows(self):
        """目录块混入无编号前缀行（Preface/Contents 等）仍须切分。

        回归根因：ESL 等书籍 TOC 页以 "Preface to the Second Edition vii"
        这类无编号行开头，旧逻辑 ``any(e is None)`` 导致整块放弃重切，
        整页目录并成一段被当普通段落翻译 → TOC 混乱。
        """
        page = build_toc_page()
        add_text(page, 50, 750, "Preface to the Second Edition")
        add_text(page, 520, 750, "vii")
        add_text(page, 50, 745, "Preface to the First Edition")
        add_text(page, 520, 745, "xi")
        sstk = ["Preface to the Second Edition vii Preface to the First Edition xi "
                "2.3 Continuous Random Variables 31 2.3.1 Uniform Random Variables 32 "
                "2.3.2 Expectations 34"]
        pstk = [Paragraph(765.0, 50.0, 50.0, 539.0, 725.0, 765.0, 10.0, True)]
        toc_track = [page_digit_track(page)]
        report = split_merged_toc_paragraphs(
            Mock(), page, sstk, pstk, toc_track, page_width=600.0)
        self.assertEqual(report["split"], 1)
        self.assertEqual(len(sstk), 5)
        # 无编号的 Preface 行保留为独立物理行段落（不丢弃）
        self.assertEqual(sstk[0], "Preface to the Second Edition vii")
        self.assertEqual(sstk[1], "Preface to the First Edition xi")
        self.assertTrue(all(p.brk is False for p in pstk))
        # 编号行仍可被 detect_toc_line 识别为目录行
        specs = [detect_toc_line(s, p.brk, t, p.x1, page_width=600.0)
                 for s, p, t in zip(sstk, pstk, toc_track)]
        self.assertIsNone(specs[0])   # Preface 行非目录行
        self.assertEqual(specs[2]["page_digits"], "31")
        self.assertEqual(specs[3]["page_digits"], "32")


if __name__ == "__main__":
    unittest.main()