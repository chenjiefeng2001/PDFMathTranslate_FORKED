"""Headless tests for the Geometry Engine (阶段二：字符→词→行→段→阅读顺序)。

全部用例使用合成 Char 数据或合成 Paragraph（不依赖真实 PDF），
覆盖：词聚类（空格切词 / 无空格兜底）、栏级行拆分、段落合并、
双栏阅读顺序（XY-Cut + 栏检测）、目录页 + 页码的阅读顺序、
pymupdf 字符提取烟雾测试。
"""

import os
import tempfile

import pytest

from pdf2zh.v3.geometry import (
    Char,
    GeometryConfig,
    GeometryEngine,
    Line,
    PageGeometry,
    Paragraph,
    Word,
    extract_chars_from_page,
)


def _mk_chars(text, x, y0, size=10.0, gap=1.0, font="Helvetica", page=0, char_w=None):
    """生成一行文本的合成字符流（每字符固定宽 char_w，字间距 gap）。"""
    chars = []
    cx = x
    cw = char_w if char_w is not None else size * 0.5
    for ch in text:
        chars.append(
            Char(
                text=ch,
                x0=cx,
                y0=y0,
                x1=cx + cw,
                y1=y0 + size,
                size=size,
                font=font,
                page_num=page,
            )
        )
        cx += cw + gap
    return chars


def _fake_para(text, x0, y0, x1, y1, page=0):
    """构造只带 bbox 语义的 Paragraph（用于阅读顺序测试）。"""
    ch = Char(text=text, x0=x0, y0=y0, x1=x1, y1=y1, page_num=page)
    return Paragraph([Line([Word([ch])])], page_num=page)


class TestWordClustering:
    def test_space_splits_words(self):
        chars = _mk_chars("Hello world", 100.0, 700.0)
        words = GeometryEngine().build_words(chars)
        assert [w.text for w in words] == ["Hello", "world"]

    def test_no_space_pdf_falls_back_to_gap_heuristic(self):
        # 无空格字符的 PDF：跨 4 倍字宽的间隙应切词
        chars = _mk_chars("AB", 100.0, 700.0)
        far = Char(
            text="C",
            x0=100.0 + 8 * 10,
            y0=700.0,
            x1=100.0 + 8 * 10 + 5,
            y1=700.0 + 10,
            size=10.0,
        )
        words = GeometryEngine().build_words(chars + [far])
        assert [w.text for w in words] == ["AB", "C"]

    def test_single_word_kept_whole(self):
        chars = _mk_chars("Transformer", 100.0, 700.0)
        words = GeometryEngine().build_words(chars)
        assert len(words) == 1
        assert words[0].text == "Transformer"

    def test_word_bbox_is_char_union(self):
        chars = _mk_chars("AB", 100.0, 700.0)
        word = GeometryEngine().build_words(chars)[0]
        assert word.x0 == 100.0
        assert word.x1 == pytest.approx(100.0 + 5.0 * 2 + 1.0)
        assert word.y0 == 700.0

    def test_two_baselines_never_merge(self):
        line1 = _mk_chars("AB", 100.0, 700.0)
        line2 = _mk_chars("CD", 100.0, 640.0)
        words = GeometryEngine().build_words(line1 + line2)
        # 行按基线分组，组间互不合并（顺序无要求）
        assert sorted(w.text for w in words) == ["AB", "CD"]


class TestLineBuilding:
    def test_words_same_baseline_join_line(self):
        words = [
            Word([Char("A", 100, 700, 105, 710)]),
            Word([Char("B", 120, 700, 125, 710)]),
        ]
        lines = GeometryEngine().build_lines(words)
        assert len(lines) == 1
        assert lines[0].text == "A B"

    def test_column_gap_splits_line(self):
        # 同一基线两栏：左栏 100-250，右栏 350-500
        left = _mk_chars("Left", 100.0, 700.0)
        right = _mk_chars("Right", 350.0, 700.0)
        lines = GeometryEngine().build_lines(GeometryEngine().build_words(left + right))
        assert len(lines) == 2
        assert lines[0].text == "Left"
        assert lines[1].text == "Right"

    def test_different_baselines_separate_lines(self):
        words = [
            Word([Char("A", 100, 700, 105, 710)]),
            Word([Char("B", 100, 660, 105, 670)]),
        ]
        lines = GeometryEngine().build_lines(words)
        assert len(lines) == 2


class TestParagraphBuilding:
    def test_consecutive_lines_merge(self):
        line1 = Line([Word([Char("A", 100, 700, 120, 710)])])
        line2 = Line([Word([Char("B", 100, 685, 120, 695)])])  # 行距 15 < 19
        paras = GeometryEngine().build_paragraphs([line1, line2])
        assert len(paras) == 1
        assert paras[0].line_count == 2

    def test_double_spacing_breaks_paragraph(self):
        line1 = Line([Word([Char("A", 100, 700, 120, 710)])])
        line2 = Line([Word([Char("B", 100, 620, 120, 630)])])  # 行距 80 > 19
        paras = GeometryEngine().build_paragraphs([line1, line2])
        assert len(paras) == 2

    def test_cross_column_lines_never_merge(self):
        # 同一基线两栏行（栏级拆分后）不合并为同一段落
        l = _mk_chars("Left", 100.0, 700.0)
        r = _mk_chars("Right", 350.0, 700.0)
        engine = GeometryEngine()
        words = engine.build_words(l + r)
        lines = engine.build_lines(words)
        paras = engine.build_paragraphs(lines)
        assert len(paras) == 2


class TestReadingOrder:
    def _page(self, paras):
        page = PageGeometry(page_num=0, paragraphs=list(paras))
        page._reading_order = GeometryEngine().reading_order(paras)
        return page

    def test_single_column_is_y_sorted(self):
        paras = [
            _fake_para("top", 100, 700, 300, 720),
            _fake_para("middle", 100, 500, 300, 520),
            _fake_para("bottom", 100, 300, 300, 320),
        ]
        page = self._page(paras)
        assert [p.text for p in page.reading_order()] == ["top", "middle", "bottom"]

    def test_two_column_interleaved_reading_order(self):
        # 双栏论文：两栏段落 y 交错
        paras = [
            _fake_para("L1", 100, 700, 250, 720),
            _fake_para("R1", 350, 700, 500, 720),
            _fake_para("L2", 100, 660, 250, 680),
            _fake_para("R2", 350, 660, 500, 680),
            _fake_para("L3", 100, 620, 250, 640),
            _fake_para("R3", 350, 620, 500, 640),
        ]
        page = self._page(paras)
        assert [p.text for p in page.reading_order()] == [
            "L1",
            "L2",
            "L3",
            "R1",
            "R2",
            "R3",
        ]

    def test_toc_page_with_page_number(self):
        # 目录页：整宽目录行 + 底部居中页码（单栏，但 y 分离）
        paras = [
            _fake_para("Chapter 1 Intro...3", 100, 185, 500, 205),
            _fake_para("Chapter 2 Methods...12", 100, 170, 500, 190),
            _fake_para("42", 280, 30, 300, 42),
        ]
        page = self._page(paras)
        assert [p.text for p in page.reading_order()] == [
            "Chapter 1 Intro...3",
            "Chapter 2 Methods...12",
            "42",
        ]

    def test_mixed_title_two_columns_toc(self):
        # 标题(整宽顶部) + 双栏 + 目录行 + 页码 的混合页
        paras = [
            _fake_para("Title", 100, 720, 500, 740),
            _fake_para("L1", 100, 690, 250, 710),
            _fake_para("R1", 350, 690, 500, 710),
            _fake_para("L2", 100, 660, 250, 680),
            _fake_para("R2", 350, 660, 500, 680),
            _fake_para("1. Intro ... 3", 100, 200, 400, 215),
            _fake_para("2. Methods ... 12", 100, 185, 400, 200),
            _fake_para("42", 280, 30, 300, 42),
        ]
        page = self._page(paras)
        order = [p.text for p in page.reading_order()]
        assert order[0] == "Title"
        # 双栏：L 全部先于 R
        assert (
            order.index("L1")
            < order.index("L2")
            < order.index("R1")
            < order.index("R2")
        )
        # 目录与页码在栏之后
        assert order.index("1. Intro ... 3") > order.index("R2")
        assert order[-1] == "42"

    def test_reading_order_with_real_pdf(self):
        """pymupdf 合成页烟雾测试：rawdict 提取 → 阅读顺序。"""
        pytest.importorskip("fitz")
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 720), "Heading One", fontsize=16)
        page.insert_text((72, 690), "Body paragraph line.", fontsize=10)
        page.insert_text((340, 690), "Right column line.", fontsize=10)
        tmp = os.path.join(tempfile.gettempdir(), "geometry_smoke.pdf")
        doc.save(tmp)
        doc.close()
        doc = fitz.open(tmp)
        try:
            engine = GeometryEngine()
            chars = extract_chars_from_page(doc.load_page(0), 0)
            model = engine.build_page(chars)
            texts = [p.text for p in model.reading_order()]
            assert any("Heading" in t for t in texts)
            assert any("Body" in t for t in texts)
            assert any("Right" in t for t in texts)
        finally:
            doc.close()
            if os.path.exists(tmp):
                os.remove(tmp)

    def test_empty_page_is_safe(self):
        page = PageGeometry(page_num=0, paragraphs=[])
        assert page.reading_order() == []
        assert page.text == ""


class TestConfig:
    def test_custom_word_gap(self):
        cfg = GeometryConfig(gap_word_ratio=5.0)  # 极宽松：不按间距切词
        chars = _mk_chars("AB", 100.0, 700.0)
        far = Char(
            text="C",
            x0=100.0 + 2 * 10,
            y0=700.0,
            x1=100.0 + 2 * 10 + 5,
            y1=700.0 + 10,
            size=10.0,
        )
        words = GeometryEngine(cfg).build_words(chars + [far])
        assert len(words) == 1  # 无空格字符时全部合并为一个词

    def test_column_split_threshold_is_font_relative(self):
        engine = GeometryEngine()
        # 8pt 字号：15pt 间隙 < 2.5*8=20 → 仍是同一行
        words = [
            Word([Char("A", 100, 700, 104, 708, size=8.0)]),
            Word([Char("B", 115, 700, 119, 708, size=8.0)]),
        ]
        assert len(engine.build_lines(words)) == 1
        # 8pt 字号：24pt 间隙 >= 20 → 拆为两行（栏）
        words = [
            Word([Char("A", 100, 700, 104, 708, size=8.0)]),
            Word([Char("B", 124, 700, 128, 708, size=8.0)]),
        ]
        assert len(engine.build_lines(words)) == 2
        # 16pt 字号：24pt 间隙 < 2.5*16=40 → 不拆
        words = [
            Word([Char("A", 100, 700, 108, 716, size=16.0)]),
            Word([Char("B", 124, 700, 132, 716, size=16.0)]),
        ]
        assert len(engine.build_lines(words)) == 1
