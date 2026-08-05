# -*- coding: utf-8 -*-
"""V9.0 — Processor 层主链路接线（P1）+ 真实 PDF e2e 回归。

覆盖：
- ``processor_channels=True`` 时 converter.receive_layout 产出的
  ``processor_reports`` / ``processor_type_counts`` / ``toc_ir_records``；
- 默认关闭时不产出（side-channel 纪律）；
- 失败不影响 legacy 渲染（side-channel 纪律）；
- 真实 fitz 合成 PDF 经 PDFPageInterpreterEx + TranslateConverter
  （stub 翻译器 + stub 布局矩阵）端到端产出 v3 side-channel 数据。
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


class ProcessorChannelsBase(unittest.TestCase):
    def build_converter(self, page, processor_channels=True):
        rsrcmgr = PDFResourceManager()
        converter = TranslateConverter(
            rsrcmgr,
            layout={page.pageid: make_layout()},
            lang_in="en",
            lang_out="zh-CN",
            service="google",
        )
        converter.thread = 1
        converter.noto_name = "noto"
        noto = Mock()
        noto.char_lengths.return_value = [8.0, 8.0]
        noto.has_glyph.return_value = True
        converter.noto = noto
        converter.fontmap = {}
        converter.fontid = {}
        converter.text_metrics = {}
        from pdf2zh.collision_resolver import CollisionResolver
        converter.collision_resolver = CollisionResolver()
        translator = Mock()
        translator.translate = Mock(side_effect=lambda t: "YI" + t)
        translator.lang_in = "en"
        translator.lang_out = "zh-CN"
        converter.translator = translator
        converter.emit_ir = False
        converter.relayout_gate = None
        converter.processor_channels = processor_channels
        return converter


class TestProcessorChannels(ProcessorChannelsBase):
    def test_processor_channels_produce_reports(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Chapter 3: Results")
        add_text(page, 50, 680, "We discuss experimental findings here.")
        add_text(page, 50, 660, "[1, 2] are cited later.")
        conv = self.build_converter(page, processor_channels=True)
        conv.receive_layout(page)
        assert hasattr(conv, "processor_reports") and conv.processor_reports
        assert 1 in conv.processor_reports
        rep = conv.processor_reports[1]
        assert rep["ok"] is True
        assert rep["stages"]  # RAW + SEMANTIC 两阶段
        # 类型分布含语义化结果（TOC/表格/引用/题注…）
        counts = conv.processor_type_counts.get(1, {})
        assert counts

    def test_toc_channel_structured_records(self):
        all_records = []
        for pageid, line in [(2, "1. Introduction .......... 3"),
                             (3, "2. Methods .............. 12")]:
            page = LTPage(pageid, (0, 0, 600, 800))
            add_text(page, 50, 700, line)
            conv = self.build_converter(page, processor_channels=True)
            conv.receive_layout(page)
            all_records.extend(
                getattr(conv, "toc_ir_records", {}).get(pageid, []))
        assert all_records, "TOC 目录行应产出结构化记录"
        kinds = {r["kind"] for r in all_records}
        assert "section" in kinds

    def test_explicit_off_produces_no_side_channel(self):
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "Chapter 3: Results")
        conv = self.build_converter(page, processor_channels=False)
        conv.receive_layout(page)
        assert getattr(conv, "processor_reports", {}) == {}
        assert getattr(conv, "toc_ir_records", {}) == {}

    def test_side_channel_failure_is_silent(self):
        page = LTPage(4, (0, 0, 600, 800))
        add_text(page, 50, 700, "Hello world")
        conv = self.build_converter(page, processor_channels=True)
        with patch("pdf2zh.v3.geometry.chars_from_ltpage",
                   side_effect=RuntimeError("boom")):
            conv.receive_layout(page)  # 不得抛异常


class TestRealPdfE2E(unittest.TestCase):
    """真实 PDF（fitz 合成）经解释器 + TranslateConverter 跑通 P1/P2 通道。"""

    def _make_pdf(self, path):
        import fitz
        doc = fitz.open()
        # page 0：正文段落；page 1：目录行单行（多行会与 stub 布局合并成段）
        for (text, y) in [("Chapter 3: Results", 720),
                          ("1. Introduction .......... 3", 300)]:
            page = doc.new_page(width=612, height=792)
            page.insert_text((72, y), text, fontsize=10)
        doc.save(path)
        doc.close()

    def test_real_pdf_interpreter_channel(self):
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser

        out = tempfile.mkdtemp(prefix="v9_e2e_")
        path = os.path.join(out, "in.pdf")
        self._make_pdf(path)

        stream = io.BytesIO(open(path, "rb").read())
        parser = PDFParser(stream)
        doc = PDFDocument(parser)

        with patch("pdf2zh.converter.build_translator") as bt:
            stub = Mock()
            stub.translate = Mock(side_effect=lambda t: "YI" + t)
            stub.lang_in = "en"
            stub.lang_out = "zh-CN"
            bt.return_value = stub
            rsrcmgr = PDFResourceManager()
            conv = TranslateConverter(
                rsrcmgr,
                layout={0: make_layout()},
                lang_in="en", lang_out="zh-CN", service="stub",
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
            conv.processor_channels = True
            obj_patch = {}
            interp = PDFPageInterpreterEx(rsrcmgr, conv, obj_patch)
            for pageno, page in enumerate(PDFPage.create_pages(doc)):
                page.pageno = pageno
                page.page_xref = pageno
                interp.process_page(page)

        reports = getattr(conv, "processor_reports", {})
        assert reports, "P1 通道应回传处理器报告"
        for rep in reports.values():
            assert rep["ok"] is True
        toc_records = getattr(conv, "toc_ir_records", {})
        assert toc_records, "P1 通道应回传 TOC 结构化记录"
        recs = [r for pg in toc_records.values() for r in pg]
        assert any(r["kind"] == "section" for r in recs)


if __name__ == "__main__":
    unittest.main()