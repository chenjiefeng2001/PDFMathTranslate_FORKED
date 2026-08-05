# -*- coding: utf-8 -*-
"""无头回归测试：目录行（TOC）结构感知与排版（P0-1/P0-2/P1）。

覆盖：
- detect_toc_line：识别"标题 + 点线 + 页码"，排除普通文本误报
- 标题单独翻译（翻译器只收到标题，不含点线/页码）
- 目录行禁折行 + 点线原位填充 + 页码右对齐
- 目录行不做行高压缩、不产生 QA 溢出标记
"""
import re
import unittest
from unittest.mock import Mock

import numpy as np
from pdfminer.layout import LTChar, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.converter import TranslateConverter, detect_toc_line


def make_char(x, y, text="A", size=12.0):
    """构造一个字符（与 test_converter_layout_fixes 相同）。"""
    font = Mock()
    font.fontname = "Helvetica"
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


def add_tight(page, x0, y, text, adv=6.0):
    """紧贴排布一串字符（字符宽 6pt，字距 6pt，无空格）。"""
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t))


def make_zone_layout(shape=(800, 600), default=3.0):
    return np.full(shape, default)


class TocDetectionTest(unittest.TestCase):
    def test_detect_valid_toc_line(self):
        text = "Intro....3"
        track = [(".", 74, 80), (".", 80, 86), (".", 86, 92), (".", 92, 98), ("3", 98, 104)]
        spec = detect_toc_line(text, brk=False, track=track, page_right=104.0)
        self.assertIsNotNone(spec)
        self.assertEqual(spec["title"], "Intro")
        self.assertEqual(spec["page_digits"], "3")
        self.assertAlmostEqual(spec["page_start_x"], 98.0)
        self.assertAlmostEqual(spec["page_right_x"], 104.0)

    def test_detect_toc_with_space_leader(self):
        text = "Chapter 1 .... 42"
        track = [(".", 90, 96), (".", 96, 102), (".", 102, 108), (".", 108, 114),
                 ("4", 120, 126), ("2", 126, 132)]
        spec = detect_toc_line(text, brk=False, track=track, page_right=132.0)
        self.assertIsNotNone(spec)
        self.assertEqual(spec["title"], "Chapter 1")
        self.assertEqual(spec["page_digits"], "42")
        self.assertAlmostEqual(spec["page_start_x"], 120.0)

    def test_body_text_without_leader_not_detected(self):
        self.assertIsNone(detect_toc_line("A plain sentence ending in 42", False, [], page_right=200.0))
        self.assertIsNone(detect_toc_line("No dots 123", False, [], page_right=200.0))
        self.assertIsNone(detect_toc_line("Section 1.2.3", False, [], page_right=200.0))

    def test_multiline_paragraph_not_detected(self):
        self.assertIsNone(detect_toc_line("Intro....3", brk=True, track=[], page_right=200.0))

    def test_title_too_short_not_detected(self):
        self.assertIsNone(detect_toc_line("....3", False, [], page_right=200.0))

    def test_single_dot_leader_not_detected(self):
        self.assertIsNone(detect_toc_line("Intro. 3", False, [], page_right=200.0))

    def test_page_digit_trailing_track_required(self):
        # 字符串形态匹配但无字符几何记录（track 为空）→ 不检测
        self.assertIsNone(detect_toc_line("Intro....3", False, [], page_right=200.0))


class TocRenderBase(unittest.TestCase):
    def build_converter(self, page, translations, noto_width=4.0):
        rsrcmgr = PDFResourceManager()
        converter = TranslateConverter(
            rsrcmgr,
            layout={page.pageid: make_zone_layout()},
            lang_in="en",
            lang_out="zh-CN",
            service="google",
        )
        converter.thread = 1
        converter.noto_name = "noto"
        noto = Mock()
        noto.char_lengths.return_value = [noto_width]
        noto.has_glyph.return_value = True
        converter.noto = noto
        converter.fontmap = {}
        converter.fontid = {}
        converter.text_metrics = {}
        from pdf2zh.collision_resolver import CollisionResolver
        converter.collision_resolver = CollisionResolver()
        translator = Mock()
        translator.translate = Mock(side_effect=list(translations))
        translator.lang_in = "en"
        translator.lang_out = "zh-CN"
        converter.translator = translator
        return converter, translator

    @staticmethod
    def tm_positions(ops):
        return [float(x) for x in re.findall(r"1 0 0 1 ([\d.]+) [\d.]+ Tm", ops)]


class TestTocTitleOnlyTranslation(TocRenderBase):
    """P0-1：目录行只把标题交给翻译器，点线/页码不入翻译文本。"""

    def test_translator_receives_title_only(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_tight(page, 50, 648, "Intro....3")
        conv, translator = self.build_converter(page, translations=["介绍"])
        conv.receive_layout(page)
        args = translator.translate.call_args_list
        self.assertEqual(len(args), 1)
        self.assertEqual(args[0][0][0], "Intro")


class TestTocLineRendering(TocRenderBase):
    """P0-2：目录行禁折行、点线填充、页码右对齐。"""

    def test_toc_line_renders_title_dots_and_right_aligned_page(self):
        page = LTPage(1, (0, 0, 600, 800))
        # 10 字符：Intro(5) + ....(4) + 3(1)，紧贴排布
        # 字符基线 y=648，LTChar y0 = baseline + 字号 = 660
        # 原始页码 '3' 位于 x0=104、x1=116（= 段落右边界 page_right）
        add_tight(page, 50, 648, "Intro....3")
        conv, _ = self.build_converter(page, translations=["介绍"])
        ops = conv.receive_layout(page)

        # 标题译文单行渲染在行首 x=50（未折行：标题两个字符合成一个 Tm）
        self.assertIn("/noto 12.0000 Tf 1 0 0 1 50.0000 660.0000 Tm [<00010001>] TJ", ops)
        # 页码右对齐：渲染宽度 4pt，右对齐起始 = 116 - 4 = 112，右边缘贴住 116
        self.assertIn("1 0 0 1 112.0000 660.0000 Tm", ops)
        # 点线在标题与页码之间原位填充（多个 '.' 依次右移）
        xs = self.tm_positions(ops)
        dot_xs = [x for x in xs if 60.0 < x < 110.0]
        self.assertGreaterEqual(len(dot_xs), 5)
        self.assertEqual(dot_xs, sorted(dot_xs))

    def test_toc_line_no_overflow_flag_no_compression(self):
        # 目录行高度远小于译文所需行高，但仍不应触发行高压缩/QA 溢出标记
        page = LTPage(1, (0, 0, 600, 800))
        add_tight(page, 50, 648, "Intro....3")
        conv, _ = self.build_converter(page, translations=["介绍"])
        ops = conv.receive_layout(page)
        self.assertEqual(conv._overflow_flags, [])
        self.assertNotIn("pdf2zh-qa-overflow", ops)


if __name__ == "__main__":
    unittest.main()
