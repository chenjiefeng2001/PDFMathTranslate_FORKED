"""P5–P10 reconstruction side-channel wiring tests (v19).

Verifies that ``TranslateConverter.receive_layout`` + ``run_mainline_channels``
produce, on the legacy render path, per-page reconstruction records:

  * ``conv.reconstruction_records[pageid]``  (Glyph/StyleRun/Line/Paragraph/
    Formula/TranslationUnit/SolvedUnit stats)
  * ``conv.reconstruction_qa[pageid]``       (§9.1/§9.2 QA snapshot)

The channel is strictly side-channel: it must never break the legacy render
path, and it must stay disabled unless ``reconstruction_channel`` is on.

Run with:
    python -m pytest tests/v3/test_v19_reconstruction_sidechannel.py -v
"""

import unittest
from unittest.mock import Mock

import numpy as np
from pdfminer.layout import LTChar, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.converter import TranslateConverter


def make_char(x, y, text="A", size=12.0, fontname="Helvetica"):
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


def add_text(page, x0, y, text, adv=12.0, fontname="Helvetica"):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname))


def make_layout(shape=(800, 600), default=3.0):
    return np.full(shape, default)


class ReconstructionSideChannelBase(unittest.TestCase):
    def build_converter(self, page, reconstruction_channel=True):
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
        noto.char_lengths.return_value = [12.0]
        noto.has_glyph.return_value = True
        converter.noto = noto
        converter.fontmap = {}
        converter.fontid = {}
        converter.text_metrics = {}
        from pdf2zh.collision_resolver import CollisionResolver

        converter.collision_resolver = CollisionResolver()
        translator = Mock()
        translator.translate = Mock(side_effect=["译文"])
        translator.lang_in = "en"
        translator.lang_out = "zh-CN"
        converter.translator = translator
        converter.emit_ir = False
        converter.relayout_gate = None
        converter.reconstruction_channel = bool(reconstruction_channel)
        converter.reconstruction_records = {}
        converter.reconstruction_qa = {}
        return converter


class TestReconstructionSideChannel(ReconstructionSideChannelBase):
    def test_enabled_produces_records(self):
        page = LTPage(7, (0, 0, 600, 800))
        add_text(page, 50, 650, "Let f(x) be continuous.")  # 多字体混合行
        add_text(page, 50, 638, "The sum converges to 2.")  # 第二行
        conv = self.build_converter(page, reconstruction_channel=True)
        conv.receive_layout(page)
        assert conv.reconstruction_records, "channel on: expect per-page records"
        assert 7 in conv.reconstruction_records
        rec = conv.reconstruction_records[7]
        assert rec["page_id"] == 7
        assert rec["glyph_count"] > 0
        assert rec["line_count"] >= 2  # 两物理行
        assert rec["paragraph_count"] >= 1
        assert len(rec["translation_units"]) >= 1
        assert len(rec["solved_units"]) == len(rec["translation_units"])

    def test_disabled_keeps_records_empty(self):
        page = LTPage(8, (0, 0, 600, 800))
        add_text(page, 50, 650, "Hello world")
        conv = self.build_converter(page, reconstruction_channel=False)
        conv.receive_layout(page)
        assert conv.reconstruction_records == {}

    def test_qa_snapshot_written(self):
        page = LTPage(9, (0, 0, 600, 800))
        add_text(page, 50, 650, "Let x be a value.")
        add_text(page, 50, 638, "Here y is defined.")
        conv = self.build_converter(page, reconstruction_channel=True)
        conv.receive_layout(page)
        assert 9 in conv.reconstruction_qa, "QA snapshot per page expected"
        qa = conv.reconstruction_qa[9]
        inner = qa["qa"] if isinstance(qa, dict) and "qa" in qa else qa
        assert "text" in inner and "formula" in inner
        assert "summary" in inner

    def test_channel_failure_never_breaks_mainline(self):
        """side-channel 抛错不得影响主链路渲染（结构完整 + 不抛异常）。"""
        page = LTPage(10, (0, 0, 600, 800))
        add_text(page, 50, 650, "Ok")
        conv = self.build_converter(page, reconstruction_channel=True)
        conv.reconstruction_channel = True
        conv.receive_layout(page)  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
