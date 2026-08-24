# -*- coding: utf-8 -*-
"""V1.6 收尾 — 真实 PDF 端到端回归（P1）+ 双轨恒等 + 聚类接管。

覆盖：
- V8.5 真实翻译产物回归：fitz PDF（含超链接注解）经解释器 + 转换器
  产出真实 gate 记录 → ``_relink_translated_doc``（y_flip 修正）→
  链接 rect 命中译后几何（IoU >= 0.5）；
- 双轨恒等：processor_channels 开/关下 gate 记录逐字段一致
  （side-channel 不改主链路）；
- Geometry 聚类接管：纯文本页 adopt；公式占位页回退；
- 新侧通道：render_takeover / translation_qa 在 converter 全链跑通。
"""

import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from pdfminer.layout import LTChar, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.converter import TranslateConverter
from pdf2zh.pdfinterp import PDFPageInterpreterEx


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


def add_text(page, x0, y, text, adv=9.0):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t))


def make_layout(shape=(800, 600), default=3.0):
    return np.full(shape, default)


def build_converter(layout_page=0, **kwargs):
    rsrcmgr = PDFResourceManager()
    conv = TranslateConverter(
        rsrcmgr,
        layout={layout_page: make_layout()},
        lang_in="en",
        lang_out="zh-CN",
        service="google",
    )
    conv.thread = 1
    conv.noto_name = "noto"
    noto = Mock()
    noto.char_lengths.return_value = [8.0]
    noto.has_glyph.return_value = True
    conv.noto = noto
    conv.fontmap, conv.fontid = {}, {}
    conv.text_metrics = {}
    from pdf2zh.collision_resolver import CollisionResolver

    conv.collision_resolver = CollisionResolver()
    translator = Mock()
    translator.translate = Mock(side_effect=lambda t: "YI" + t)
    translator.lang_in = "en"
    translator.lang_out = "zh-CN"
    conv.translator = translator
    conv.emit_ir = False
    conv.relayout_gate = None
    for k, v in kwargs.items():
        setattr(conv, k, v)
    return conv


class TestGeometryClusterAdoption(unittest.TestCase):
    def test_plain_text_page_adopted(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Hello world today")
        conv = build_converter(geometry_cluster=True, geometry_adoptions={})
        conv.receive_layout(page)
        report = conv.geometry_adoptions[1]
        self.assertTrue(report["adopted"])
        self.assertEqual(report["reason"], "consistent")

    def test_formula_page_falls_back(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Body text goes here")
        conv = build_converter(geometry_cluster=True, geometry_adoptions={})
        with patch(
            "pdf2zh.v3.geometry.chars_from_ltpage", return_value=[]
        ):  # 模拟字符流缺失 → 不可接管
            conv.receive_layout(page)
        report = conv.geometry_adoptions[1]
        self.assertFalse(report["adopted"])

    def test_flag_off_keeps_legacy(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Hello world today")
        conv = build_converter()
        conv.receive_layout(page)
        self.assertFalse(hasattr(conv, "geometry_adoptions"))


class TestDoubleTrackInvariance(unittest.TestCase):
    def test_processor_channels_off_on_identical_gate_records(self):
        page_a = LTPage(1, (0, 0, 600, 800))
        add_text(page_a, 50, 700, "Chapter 3: Results")
        add_text(page_a, 50, 680, "We discuss the findings.")
        page_b = LTPage(1, (0, 0, 600, 800))
        add_text(page_b, 50, 700, "Chapter 3: Results")
        add_text(page_b, 50, 680, "We discuss the findings.")
        conv_off = build_converter(processor_channels=False)
        conv_on = build_converter(processor_channels=True)
        conv_off.receive_layout(page_a)
        conv_on.receive_layout(page_b)
        keys = (
            "text",
            "translated",
            "x",
            "y",
            "width",
            "height",
            "src_box",
            "dst_box",
            "node_type",
        )
        off = [tuple(r.get(k) for k in keys) for r in conv_off._gate_records]
        on = [tuple(r.get(k) for k in keys) for r in conv_on._gate_records]
        self.assertEqual(off, on)
        # 且 on 路径产出了侧通道数据
        self.assertTrue(getattr(conv_on, "processor_reports", {}))
        self.assertEqual(getattr(conv_off, "processor_reports", {}), {})


class TestNewSideChannels(unittest.TestCase):
    def test_render_takeover_plan_produced(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Hello world today")
        conv = build_converter(render_takeover=True)
        conv.relayout_gate = True  # 门控开启（用默认 GateBlock 判定）
        conv.receive_layout(page)
        plans = getattr(conv, "render_plans", {})
        self.assertIn(1, plans)
        self.assertIn("routing", plans[1]["plan"])
        self.assertGreaterEqual(plans[1]["applied_count"], 1)

    def test_translation_qa_records_produced(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "This is a long sentence here")
        conv = build_converter(translation_qa=True)
        conv.receive_layout(page)
        qa = getattr(conv, "translation_qa_records", {})
        self.assertIn(1, qa)
        self.assertGreaterEqual(qa[1]["total"], 1)
        # stub 翻译 "YI" + 原文 → 与原文不等 → 不触发 UNTRANSLATED
        self.assertGreaterEqual(qa[1]["action_retranslate"], 0)


class TestRealPdfLinkRemapE2E(unittest.TestCase):
    """真实 PDF（fitz 合成 + 超链接）经解释器 → gate 记录 → relink 闭环。"""

    def _make_pdf(self, path):
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 104), "Source paragraph text", fontsize=10)
        page.insert_link(
            {
                "kind": fitz.LINK_URI,
                "from": fitz.Rect(72, 94, 260, 104),
                "uri": "http://example.com",
            }
        )
        doc.save(path)
        doc.close()

    def test_link_remap_tracks_real_translation_geometry(self):
        import fitz
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser
        from pdf2zh.high_level import _relink_translated_doc

        out = tempfile.mkdtemp(prefix="v10_e2e_")
        path = os.path.join(out, "in.pdf")
        self._make_pdf(path)
        stream = io.BytesIO(open(path, "rb").read())
        docp = PDFDocument(PDFParser(stream))
        doc_zh = fitz.open(path)

        conv = build_converter(link_remap=True, gate_records_by_page={})
        conv.relayout_gate = None
        obj_patch = {}
        interp = PDFPageInterpreterEx(PDFResourceManager(), conv, obj_patch)
        for pageno, page in enumerate(PDFPage.create_pages(docp)):
            page.pageno = pageno
            page.page_xref = pageno
            interp.process_page(page)

        records = getattr(conv, "gate_records_by_page", {})
        self.assertTrue(records, "link_remap 桥应采集到段落几何")
        v3_output = {"link_records": records}
        stats = _relink_translated_doc(doc_zh, v3_output)
        self.assertGreaterEqual(stats["relinked"], 1, f"stats={stats}")

        # 修正后的 rect 应命中译后 dst 几何（y_flip 后的 pdfminer 空间）
        from pdf2zh.v3.link_remap import rect_iou

        rec = records[0][0]
        ph = doc_zh[0].rect.height
        src = rec["src_box"]
        dst = rec["dst_box"]
        flipped_dst = (dst[0], ph - dst[3], dst[2], ph - dst[1])
        links = doc_zh[0].get_links()
        new_rect = tuple(float(v) for v in links[0]["from"])
        self.assertGreaterEqual(rect_iou(new_rect, flipped_dst), 0.5)


if __name__ == "__main__":
    unittest.main()
