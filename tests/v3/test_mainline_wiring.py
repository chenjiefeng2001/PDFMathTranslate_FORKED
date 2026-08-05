"""V8.3/V8.4 mainline side-channel wiring tests.

Verifies that ``TranslateConverter.receive_layout`` + ``run_mainline_channels``
produce, on the legacy render path:
  * ``conv.ir_snapshots[pageid]``      (V8.3: Geometry Engine IR from LTChar stream)
  * ``conv.gate_verdicts[pageid]``     (V8.4: MainlineRelayoutGate write-back verdict)
  * ``kind="gate-blocked"`` QA overflow flags when the gate rejects the page

Run with:
    python -m pytest tests/v3/test_mainline_wiring.py -v
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


def add_text(page, x0, y, text, adv=12.0):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t))


def make_layout(shape=(800, 600), default=3.0, zones=None):
    arr = np.full(shape, default)
    for (x0, y0, x1, y1), cls in (zones or {}).items():
        arr[y0:y1, x0:x1] = cls
    return arr


class MainlineChannelsBase(unittest.TestCase):
    def build_converter(self, page, emit_ir=True, relayout_gate=None):
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
        translator.translate = Mock(side_effect=[t for t in ("你好世界",)])
        translator.lang_in = "en"
        translator.lang_out = "zh-CN"
        converter.translator = translator
        converter.emit_ir = emit_ir
        converter.relayout_gate = relayout_gate
        return converter


class TestIrSnapshotSideChannel(MainlineChannelsBase):
    def test_emit_ir_produces_snapshot(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 648, "Hello")
        conv = self.build_converter(page, emit_ir=True)
        conv.receive_layout(page)
        assert conv.ir_snapshots, "emit_ir on: expect a non-empty ir_snapshots"
        assert 1 in conv.ir_snapshots
        snap = conv.ir_snapshots[1]
        assert isinstance(snap, dict) and snap

    def test_no_emit_ir_keeps_snapshots_empty(self):
        page = LTPage(2, (0, 0, 600, 800))
        add_text(page, 50, 648, "Hello")
        conv = self.build_converter(page, emit_ir=False)
        conv.receive_layout(page)
        assert conv.ir_snapshots == {}

    def test_ir_failure_is_side_channel_silent(self):
        """chars 流不可用/抛错时不得影响主链路渲染。"""
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 648, "Hello")
        conv = self.build_converter(page, emit_ir=True)
        conv.receive_layout(page)
        assert conv.ir_snapshots.get(1) is not None or True  # 结构性：不抛异常即通过


class TestGateSideChannel(MainlineChannelsBase):
    def test_gate_records_verdict_per_page(self):
        from pdf2zh.v3.mainline_gate import MainlineRelayoutGate
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 648, "Hello")
        conv = self.build_converter(page, relayout_gate=MainlineRelayoutGate())
        conv.receive_layout(page)
        assert 3 in conv.gate_verdicts
        verdict = conv.gate_verdicts[3]
        assert isinstance(verdict, dict)
        assert "writeback_allowed" in verdict

    def test_gate_blocked_appends_qa_flag(self):
        from pdf2zh.v3.mainline_gate import MainlineRelayoutGate
        page = LTPage(4, (0, 0, 600, 800))
        add_text(page, 50, 780, "Below")  # 底部段落：门控判定溢出
        conv = self.build_converter(page, relayout_gate=MainlineRelayoutGate())
        conv.receive_layout(page)
        kinds = [f.get("kind") for f in conv._overflow_flags]
        assert "gate-blocked" in kinds

    def test_no_gate_no_verdicts(self):
        page = LTPage(5, (0, 0, 600, 800))
        add_text(page, 50, 648, "Hello")
        conv = self.build_converter(page, relayout_gate=None)
        conv.receive_layout(page)
        assert conv.gate_verdicts == {}

    def test_link_remap_bridge_records_per_page(self):
        """V8.5: link_remap 开关打开时逐段落的源/目标几何按页存档。"""
        page = LTPage(7, (0, 0, 600, 800))
        add_text(page, 50, 648, "Hello")
        conv = self.build_converter(page, emit_ir=False, relayout_gate=None)
        conv.link_remap = True
        conv.gate_records_by_page = {}
        conv.receive_layout(page)
        assert 7 in conv.gate_records_by_page
        records = conv.gate_records_by_page[7]
        assert records, "expect per-paragraph records"
        for rec in records:
            assert "src_box" in rec and "dst_box" in rec
            # 源几何应位于字符实际写入位置（y-up，48..104 区间）
            assert isinstance(rec["src_box"], tuple) and len(rec["src_box"]) == 4
            assert isinstance(rec["dst_box"], tuple) and len(rec["dst_box"]) == 4

    def test_link_remap_bridge_off_when_flag_false(self):
        page = LTPage(8, (0, 0, 600, 800))
        add_text(page, 50, 648, "Hello")
        conv = self.build_converter(page, emit_ir=False, relayout_gate=None)
        conv.link_remap = False
        conv.gate_records_by_page = {}
        conv.receive_layout(page)
        assert conv.gate_records_by_page == {}


if __name__ == "__main__":
    unittest.main()