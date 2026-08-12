import unittest
from unittest.mock import Mock

import numpy as np
from pdfminer.layout import LTChar, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.converter import TranslateConverter
from pdf2zh.formula.anchor import (
    AnchorProtector,
    extract_anchors_loose,
    normalize_anchor_token,
    repair_anchors,
)


class TestLooseAnchorExtraction(unittest.TestCase):
    """失效点 2：宽松锚点提取 + 规范化。"""

    def test_canonical(self):
        self.assertEqual(extract_anchors_loose('<formula_0>'), ['<formula_0>'])

    def test_space_pollution(self):
        self.assertEqual(extract_anchors_loose('< formula_0 >'), ['<formula_0>'])
        self.assertEqual(extract_anchors_loose('<formula 0>'), ['<formula_0>'])

    def test_case_pollution(self):
        self.assertEqual(extract_anchors_loose('<FORMULA_0>'), ['<formula_0>'])
        self.assertEqual(extract_anchors_loose('< Formula_1 >'), ['<formula_1>'])

    def test_missing_underscore(self):
        self.assertEqual(extract_anchors_loose('<formula0>'), ['<formula_0>'])

    def test_no_false_positive_on_f(self):
        self.assertEqual(extract_anchors_loose('x <f_0> y'), [])
        self.assertIsNone(normalize_anchor_token('<f_0>'))

    def test_normalize(self):
        self.assertEqual(normalize_anchor_token(' < FORMULA 7 > '), '<formula_7>')
        self.assertIsNone(normalize_anchor_token('plain text'))

    def test_ordered_extraction(self):
        out = extract_anchors_loose('<formula_2> a <FORMULA_0> b <formula 1>')
        self.assertEqual(out, ['<formula_2>', '<formula_0>', '<formula_1>'])


class TestAnchorRepairFallback(unittest.TestCase):
    """失效点 2：缺失/污染锚点回退兜底（几何绝不丢弃）。"""

    FORMULA_MAP = {'<formula_0>': {}, '<formula_1>': {}}

    def test_clean_text_unchanged(self):
        t = 'the value is <formula_0> <formula_1> here'
        self.assertEqual(repair_anchors(t, self.FORMULA_MAP), t)

    def test_pollution_normalized(self):
        out = repair_anchors('the value is <FORMULA_0> < formula_1 >', self.FORMULA_MAP)
        self.assertEqual(out, 'the value is <formula_0> <formula_1>')

    def test_deleted_anchor_fallback(self):
        out = repair_anchors('the value is ', self.FORMULA_MAP)
        self.assertIn('<formula_0>', out)
        self.assertIn('<formula_1>', out)

    def test_merged_anchor_fallback(self):
        out = repair_anchors('ab <formula_0>', self.FORMULA_MAP)
        self.assertEqual(out, 'ab <formula_0> <formula_1>')

    def test_no_map_just_normalize(self):
        self.assertEqual(repair_anchors('x < FORMULA_0 >', {}), 'x <formula_0>')

    def test_integrity_loose_vs_strict(self):
        p = AnchorProtector()
        polluted = 'x < FORMULA_0 > y'
        self.assertAlmostEqual(p.integrity_score(polluted, self.FORMULA_MAP), 0.5)
        self.assertAlmostEqual(
            p.integrity_score(polluted, self.FORMULA_MAP, loose=False), 0.0)
        self.assertAlmostEqual(
            p.integrity_score('x <formula_0> <formula_1>', self.FORMULA_MAP), 1.0)

    def test_protector_repair_delegate(self):
        out = AnchorProtector().repair('ab <formula_0>', self.FORMULA_MAP)
        self.assertEqual(out, 'ab <formula_0> <formula_1>')


class TestBlockSplitIoU(unittest.TestCase):
    """失效点 3：_block_split IoU 阈值加固。"""

    def _line(self, bb):
        from pdf2zh.geometry.line import VisualLine
        return VisualLine(line_id='l', bbox=bb, master_baseline=bb[3])

    def test_edge_overflow_no_split(self):
        from pdf2zh.geometry.paragraph import _block_split
        line = self._line((100, 600, 400, 612))
        self.assertFalse(_block_split(line, [(100, 599, 400, 601)]))

    def test_big_block_still_splits(self):
        from pdf2zh.geometry.paragraph import _block_split
        line = self._line((100, 600, 400, 612))
        self.assertTrue(_block_split(line, [(100, 590, 400, 610)]))

    def test_threshold_tunable(self):
        from pdf2zh.geometry.paragraph import _block_split
        line = self._line((100, 600, 400, 612))
        self.assertTrue(_block_split(line, [(100, 599, 400, 601)], 0.05))
        self.assertFalse(_block_split(line, [(100, 599, 400, 601)], 0.3))

    def test_paragraph_aggregation_survives_edge_block(self):
        from pdf2zh.geometry.paragraph import ParagraphConfig, build_logical_paragraphs
        lines = [self._line((100, 600, 400, 612)), self._line((100, 588, 400, 600))]
        paras = build_logical_paragraphs(
            lines, page_id=0, blocks=[(100, 599, 400, 601)], config=ParagraphConfig())
        self.assertEqual(len(paras), 1)
        self.assertEqual(paras[0].line_count, 2)


class TestApplyToPdfCjkFont(unittest.TestCase):
    """失效点 4：CJK 字体注册 + 中文落位可提取。"""

    def test_chinese_text_roundtrip(self):
        import pymupdf
        from pdf2zh.patch.dual_patcher import DualPatch, DualPatcher

        doc = pymupdf.open()
        doc.new_page(width=612, height=792)
        patch = DualPatch(patches=[{
            'op': 'text_show',
            'bbox': [72, 650, 300, 670],
            'font_size': 12,
            'lines': [{'text': '中文译文 abc', 'baseline': 660.0,
                       'font_size': 12, 'formula_ids': []}],
        }])
        count = DualPatcher().apply_to_pdf(doc, 0, patch)
        self.assertGreaterEqual(count, 1)
        self.assertIn('中文译文', doc[0].get_text())
        doc.close()

    def test_apply_to_pdf_bad_index_safe(self):
        import pymupdf
        from pdf2zh.patch.dual_patcher import DualPatch, DualPatcher
        doc = pymupdf.open()
        doc.new_page(width=612, height=792)
        self.assertEqual(DualPatcher().apply_to_pdf(doc, 99, DualPatch(patches=[])), 0)
        doc.close()


class TestAnchorQaLoose(unittest.TestCase):
    """失效点 2：DualPatcher.anchor_qa 宽松匹配。"""

    def test_polluted_anchor_ok(self):
        from pdf2zh.patch.dual_patcher import DualPatcher
        qa = DualPatcher().anchor_qa(
            'x < FORMULA_0 > < formula_1 >', {'<formula_0>': {}, '<formula_1>': {}})
        self.assertTrue(qa['anchor_ok'])
        self.assertEqual(qa['anchor_matcher'], 'loose')
        self.assertEqual(qa['found_anchors'], 2)

    def test_missing_anchor_reported(self):
        from pdf2zh.patch.dual_patcher import DualPatcher
        qa = DualPatcher().anchor_qa(
            'x <formula_0>', {'<formula_0>': {}, '<formula_1>': {}})
        self.assertFalse(qa['anchor_ok'])
        self.assertEqual(qa['missing'], ['<formula_1>'])


class TestMainlineRenderSourceMark(unittest.TestCase):
    """失效点 1：主链路通道开启后记录 render_source=legacy 标注。"""

    def _build_converter(self, page):
        rsrcmgr = PDFResourceManager()
        conv = TranslateConverter(
            rsrcmgr, layout={page.pageid: np.full((800, 600), 3.0)},
            lang_in='en', lang_out='zh-CN', service='google')
        conv.thread = 1
        conv.noto_name = 'noto'
        noto = Mock()
        noto.char_lengths.return_value = [12.0]
        noto.has_glyph.return_value = True
        conv.noto = noto
        conv.fontmap = {}
        conv.fontid = {}
        conv.text_metrics = {}
        from pdf2zh.collision_resolver import CollisionResolver
        conv.collision_resolver = CollisionResolver()
        translator = Mock()
        translator.translate = Mock(side_effect=['译文'])
        translator.lang_in = 'en'
        translator.lang_out = 'zh-CN'
        conv.translator = translator
        conv.emit_ir = False
        conv.relayout_gate = None
        conv.reconstruction_channel = True
        conv.reconstruction_records = {}
        conv.reconstruction_qa = {}
        return conv

    def test_channel_on_marks_render_source_legacy(self):
        page = LTPage(11, (0, 0, 600, 800))
        for i, t in enumerate('Let f(x) be continuous.'):
            font = Mock()
            font.fontname = 'Helvetica'
            font.get_descent.return_value = -0.25
            page.add(LTChar((1, 0, 0, 1, 50 + i * 12, 650), font, 12.0, 1.0,
                            0.0, t, textwidth=0.5, textdisp=(0.0, 0.0),
                            ncs=Mock(), graphicstate=Mock()))
        conv = self._build_converter(page)
        conv.receive_layout(page)
        rec = conv.reconstruction_records[11]
        self.assertEqual(rec['render_source'], 'legacy')
        self.assertEqual(rec['render_consumer'], 'none')
        self.assertTrue(rec['channel_enabled'])
        self.assertGreaterEqual(rec['paragraph_count'], 1)


if __name__ == '__main__':
    unittest.main()
