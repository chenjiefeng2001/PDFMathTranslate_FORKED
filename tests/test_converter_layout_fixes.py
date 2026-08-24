# -*- coding: utf-8 -*-
"""无头回归测试：TranslateConverter 碰撞管线修复（S1-S6）。

覆盖修复：
- S1: 无条件应用下移避让（删除 `if shift > 0` 门控），形成链式流式重排
- S2: 一次传入全部障碍物
- S3: 解包并应用 resolver 返回的 x / size
- S4: 原文单行段落（brk=False）译文超宽也能换行
- S5: 行高下限采用字形度量（CJK 不低于 1.3），压缩失败输出 QA 溢出标记
- S6: 表格边框 / 公式块边界线条登记为障碍物
"""

import re
import unittest
from unittest.mock import Mock

import numpy as np
from pdfminer.layout import LTChar, LTLine, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.collision_resolver import BoundingBox
from pdf2zh.converter import TranslateConverter


def make_char(x, y, text="A", size=12.0):
    """构造一个字符。pdfminer 的 LTChar 坐标基于字体度量计算，
    测试仅依赖字符间的相对位置关系。"""
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


def add_text(page, x0, y, text, adv=12.0):
    """把一串字符加入页面（同一行，从左到右）。"""
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t))


def make_zone_layout(zone_cls_map, shape=(800, 600), default=3.0):
    """构造 layout：默认类别 default，zone_cls_map 为 {(x0,y0,x1,y1): cls}。"""
    arr = np.full(shape, default)
    for (x0, y0, x1, y1), cls in zone_cls_map.items():
        arr[y0:y1, x0:x1] = cls
    return arr


class ConverterCollisionBase(unittest.TestCase):
    def build_converter(
        self,
        page,
        translations,
        layout_arr,
        noto_width=12.0,
        text_metrics=None,
        collision_resolver=None,
    ):
        rsrcmgr = PDFResourceManager()
        converter = TranslateConverter(
            rsrcmgr,
            layout={page.pageid: layout_arr},
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
        converter.text_metrics = text_metrics or {}
        if collision_resolver is None:
            from pdf2zh.collision_resolver import CollisionResolver

            collision_resolver = CollisionResolver()
        converter.collision_resolver = collision_resolver
        translator = Mock()
        translator.translate = Mock(side_effect=list(translations))
        translator.lang_in = "en"
        translator.lang_out = "zh-CN"
        converter.translator = translator
        return converter

    @staticmethod
    def count_tm(ops):
        return len(re.findall(r"1 0 0 1 [\d.]+ [\d.]+ Tm", ops))


class TestPushDownChain(ConverterCollisionBase):
    """S1/S2/S3：膨胀段落上方，后继段落必须被链式下推且互不重叠。"""

    def test_second_paragraph_pushed_below_expanded_first(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 648, "He")  # 段落1：字符 y0 = 660
        add_text(page, 50, 628, "Wo")  # 段落2：字符 y0 = 640
        layout_arr = make_zone_layout({(45, 630, 70, 650): 4.0})
        conv = self.build_converter(
            page,
            translations=["你好你好你好你好你好你好你好", "世界"],
            layout_arr=layout_arr,
        )
        conv.receive_layout(page)
        self.assertEqual(len(conv._rendered_paragraphs), 2)
        p1, p2 = conv._rendered_paragraphs
        # 段落1 译文膨胀后下探，段落2 必须整体下移避开
        self.assertFalse(p1.overlaps(p2))
        self.assertLess(p2.y1, 640.0)  # 段落2 顶部低于其原文位置

    def test_three_paragraph_chain_push_down(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 678, "AAA")  # 段落1：y0 = 690
        add_text(page, 50, 658, "BBB")  # 段落2：y0 = 670
        add_text(page, 50, 638, "CC")  # 段落3：y0 = 650
        layout_arr = make_zone_layout(
            {
                (45, 685, 80, 700): 4.0,
                (45, 665, 80, 680): 5.0,
                (45, 645, 80, 660): 6.0,
            }
        )
        conv = self.build_converter(
            page,
            translations=[
                "这是一个非常长的中文测试段落用于膨胀",
                "中文段落测试",
                "结束",
            ],
            layout_arr=layout_arr,
        )
        conv.receive_layout(page)
        self.assertEqual(len(conv._rendered_paragraphs), 3)
        p1, p2, p3 = conv._rendered_paragraphs
        for a, b in ((p1, p2), (p1, p3), (p2, p3)):
            self.assertFalse(a.overlaps(b), f"{a} overlaps {b}")
        # 每段都相对原文位置向下移动
        self.assertLess(p2.y1, 670.0)
        self.assertLess(p3.y1, 650.0)


class TestSingleLineWrap(ConverterCollisionBase):
    """S4：原文单行段落（brk=False）译文超宽必须换行。"""

    def test_brk_false_paragraph_translation_wraps(self):
        page = LTPage(1, (0, 0, 600, 800))
        # 25 个字符排成一行，无物理换行 → brk=False
        add_text(page, 50, 648, "ABCDEFGHIJKLMNOPQRSTUVWXY")
        layout_arr = make_zone_layout({})
        conv = self.build_converter(
            page,
            translations=[
                "这是一个非常长的中文句子用来验证换行行为是否正确生效并且不会超出右边界这是第二行内容用来确保产生多行折行效果"
            ],
            layout_arr=layout_arr,
        )
        ops = conv.receive_layout(page)
        # 译文宽度远超原文行宽，必须折成多行（多个 Tm 指令）
        self.assertGreaterEqual(self.count_tm(ops), 3)


class TestTableFormulaObstacles(ConverterCollisionBase):
    """S6：表格边框 / 公式块边界线条登记为障碍物，细线不误报。"""

    def test_table_border_line_registered_as_obstacle(self):
        page = LTPage(1, (0, 0, 600, 800))
        # 粗长线条（表格边框）位于 layout 保留区域（cls=0）
        page.add(LTLine(1.0, (100, 500), (500, 500)))
        # 细线（装饰线/下划线）不应登记
        page.add(LTLine(0.2, (100, 400), (500, 400)))
        layout_arr = make_zone_layout({})
        layout_arr[500, 100:501] = 0.0
        layout_arr[400, 100:501] = 0.0
        conv = self.build_converter(page, translations=[], layout_arr=layout_arr)
        conv.receive_layout(page)
        self.assertTrue(conv._rendered_obstacles)
        # 只有粗线条（>=1.0 且长度 >=30pt）被登记
        self.assertEqual(len(conv._rendered_obstacles), 1)
        obs = conv._rendered_obstacles[0]
        self.assertAlmostEqual(obs.y0, 500.0, delta=0.5)
        self.assertGreater(obs.width, 30.0)


class TestLineHeightFloorAndQAMarks(ConverterCollisionBase):
    """S5：行高下限（CJK>=1.3），压缩失败记录 QA 溢出标记并写入内容流。"""

    def test_cjk_line_height_floor_and_overflow_mark(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 648, "He")
        layout_arr = make_zone_layout({})
        # 提供字形度量：ascent - descent = 1.2
        tm = Mock()
        tm.ascent = 0.9
        tm.descent = -0.3
        tm.char_width.return_value = 12.0
        conv = self.build_converter(
            page,
            translations=["你好你好你好你好你好你好你好你好你好你好"],
            layout_arr=layout_arr,
            text_metrics={"noto": tm},
        )
        ops = conv.receive_layout(page)
        self.assertTrue(conv._overflow_flags, "CJK 行高压缩失败应记录溢出标记")
        flag = conv._overflow_flags[0]
        self.assertIn("required_height", flag)
        # 行高下限不得低于 1.3（CJK），即使压缩失败也不压低
        self.assertGreaterEqual(flag["line_height"], 1.3)
        # QA 标记写入内容流注释，供自动化回归解析
        self.assertIn("% pdf2zh-qa-overflow", ops)


class TestWidthReductionApplied(ConverterCollisionBase):
    """S3：resolver 返回 width 策略时，converter 平移已生成行（x 应用）。"""

    def test_width_adjustment_moves_paragraph_horizontally(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 628, "Wo")
        layout_arr = make_zone_layout({(45, 630, 70, 650): 4.0})
        resolver = Mock()
        # 段落唯一：resolver 返回 width 策略（x 右移 10pt）
        resolver.resolve.return_value = (60.0, 640.0, 12.0, "width")
        conv = self.build_converter(
            page,
            translations=["世界"],
            layout_arr=layout_arr,
            collision_resolver=resolver,
        )
        # 预先放置一个覆盖段落区域的障碍物，确保走碰撞分支
        conv._rendered_obstacles.append(BoundingBox(40.0, 630.0, 300.0, 645.0))
        ops = conv.receive_layout(page)
        resolver.resolve.assert_called_once()
        # 段落文本行首被平移至 x=60
        self.assertRegex(ops, r"1 0 0 1 60\.\d+ [\d.]+ Tm")


if __name__ == "__main__":
    unittest.main()
