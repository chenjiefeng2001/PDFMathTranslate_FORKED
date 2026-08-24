"""P5–P10 主链路接管（阶段 3 接线）测试（v21）。

验证：

1. ``reconstruction_adapter`` 归一化/配对/接管：
   * 公式占位符 ``{vN}`` 与锚点 ``<formula_N>`` 归一化一致 → 接管
   * Level 1：逐段文本集一致 → 用 ``SolvedUnit.render_bbox`` 替换 ``pstk``
     几何（``sstk`` 保持 legacy，公式走旧 ``{vN}`` 逐字形还原）
   * Level 2：重建段 = 多个 legacy 段拼接 → 合并为一个渲染段落
     （``sstk/toc_track/pfkstk`` 同步压缩，LLM 获得完整自然段上下文）
   * TOC 段 / 文本分歧 / 通道关闭 → 一律回退 legacy（零回归）
2. ``run_reconstruction_channel`` 幂等 + ``render_source`` 标注：
   接管成功 → ``reconstructed`` / ``legacy_renderer``；否则 ``legacy`` / ``none``。
3. e2e：``receive_layout`` 真实页面跑通接管报告。

Run with:
    python -m pytest tests/v3/test_v21_mainline_reconstruction_adoption.py -v
"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from pdfminer.layout import LTChar, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.converter import TranslateConverter
from pdf2zh.v3.mainline_wiring import run_reconstruction_channel
from pdf2zh.v3.reconstruction_adapter import (
    adopt_reconstruction_cluster,
    normalize_formula_tokens,
    pair_legacy_to_reconstructed,
)
from pdf2zh.v3.reconstruction_pipeline import ReconstructionResult
from pdf2zh.layout.solver import SolvedUnit
from pdf2zh.layout.inline_layout import TranslationUnit
from pdf2zh.geometry.paragraph import LogicalParagraph

# ── 测试辅助 ──────────────────────────────────────────────────────


def make_result(page_id, texts, bbox_base=100.0):
    """由语义文本列表构造 ReconstructionResult（solved/paragraphs 平行数组）。"""
    result = ReconstructionResult(page_id=page_id)
    for i, t in enumerate(texts):
        y = bbox_base - i * 20.0
        bb = (50.0, y, 550.0, y + 16.0)
        unit = TranslationUnit(
            unit_id=f"u{i}",
            page_id=page_id,
            source_text_with_anchors=t,
            source_bbox=bb,
        )
        result.translation_units.append(unit)
        result.solved_units.append(
            SolvedUnit(
                unit_id=f"u{i}",
                source_bbox=bb,
                translated_bbox=bb,
                render_bbox=bb,
                font_size=12.0,
                line_count=1,
            )
        )
        result.paragraphs.append(
            LogicalParagraph(
                paragraph_id=f"p{i}",
                page_id=page_id,
                bbox=bb,
            )
        )
    result.paragraph_count = len(texts)
    return result


def make_pstk(n, base_y=100.0):
    return [
        SimpleNamespace(
            x=50.0,
            y=base_y - i * 16.0,
            x0=50.0,
            x1=550.0,
            y0=base_y - i * 16.0,
            y1=base_y - i * 16.0 + 16.0,
            size=12.0,
            brk=(i == n - 1),
        )
        for i in range(n)
    ]


def make_conv(pageid, result, adopted=True):
    conv = Mock()
    conv.reconstruction_channel = True
    conv.reconstruction_adopt = adopted
    conv.reconstruction_results = {pageid: result}
    conv.reconstruction_adoptions = {}
    conv.reconstruction_records = {}
    return conv


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


# ── 归一化 ─────────────────────────────────────────────────────────


class TestNormalizeFormulaTokens(unittest.TestCase):
    def test_legacy_formula(self):
        self.assertEqual(normalize_formula_tokens("Let {v0} be"), "Let {formula} be")

    def test_anchor_formula(self):
        self.assertEqual(
            normalize_formula_tokens("Let <formula_0> be"), "Let {formula} be"
        )

    def test_mixed(self):
        self.assertEqual(
            normalize_formula_tokens("a {v1} b <formula_2> c"),
            "a {formula} b {formula} c",
        )

    def test_whitespace_collapse(self):
        self.assertEqual(normalize_formula_tokens("a\n b   c"), "a b c")

    def test_none(self):
        self.assertEqual(normalize_formula_tokens(None), "")


# ── 配对 ───────────────────────────────────────────────────────────


class TestPairing(unittest.TestCase):
    def test_level1(self):
        self.assertEqual(
            pair_legacy_to_reconstructed(["A", "B"], ["A", "B"]), [(0, 0, 0), (1, 1, 1)]
        )

    def test_level2_merge(self):
        self.assertEqual(
            pair_legacy_to_reconstructed(["Let", " x be"], ["Let x be"]), [(0, 1, 0)]
        )

    def test_formula_placeholder_match(self):
        self.assertEqual(
            pair_legacy_to_reconstructed(["Let {v0} be"], ["Let <formula_0> be"]),
            [(0, 0, 0)],
        )

    def test_mismatch(self):
        self.assertIsNone(pair_legacy_to_reconstructed(["A", "B"], ["X"]))

    def test_recon_splits_into_more_reverse_merge(self):
        # P5 把一段拆成多段 → 反向合并接管（字符序列一致；原契约整体回退已废弃）
        self.assertEqual(pair_legacy_to_reconstructed(["A B"], ["A", "B"]), [(0, 0, 0)])

    def test_empty(self):
        self.assertIsNone(pair_legacy_to_reconstructed([], ["A"]))
        self.assertIsNone(pair_legacy_to_reconstructed(["A"], []))

    def test_merge_with_formula(self):
        pairs = pair_legacy_to_reconstructed(
            ["Let {v0}", " go"], ["Let <formula_0> go"]
        )
        self.assertEqual(pairs, [(0, 1, 0)])


# ── 接管（单元级）──────────────────────────────────────────────────


class TestAdoptReconstructionCluster(unittest.TestCase):
    def test_level1_keeps_sstk_replaces_geometry(self):
        page = SimpleNamespace(pageid=0)
        sstk = ["Let {v0} be"]
        pstk = make_pstk(1)
        toc_track = [[]]
        pfkstk = [{"F1"}]
        result = make_result(0, ["Let <formula_0> be"])
        conv = make_conv(0, result)
        report = adopt_reconstruction_cluster(
            conv, page, sstk, pstk, [], [], [], [], toc_track, pfkstk
        )
        self.assertTrue(report["adopted"])
        self.assertEqual(report["level"], 1)
        # sstk 保持 legacy（公式占位符原样 → 旧 {vN} 机制逐字形还原）
        self.assertEqual(sstk, ["Let {v0} be"])
        # pstk 几何来自 SolvedUnit.render_bbox
        self.assertAlmostEqual(pstk[0].x0, 50.0)
        self.assertAlmostEqual(pstk[0].x1, 550.0)
        self.assertAlmostEqual(pstk[0].y0, 100.0)
        self.assertAlmostEqual(pstk[0].y1, 116.0)
        self.assertTrue(pstk[0].brk)  # 保持 legacy brk
        self.assertEqual(toc_track, [[]])
        self.assertEqual(pfkstk, [{"F1"}])

    def test_level2_merges_paragraphs(self):
        page = SimpleNamespace(pageid=0)
        sstk = ["Let", " x be"]  # 多字体割裂的两个 legacy 段
        pstk = make_pstk(2)
        toc_track = [[], []]
        pfkstk = [{"F1"}, {"F2"}]
        result = make_result(0, ["Let x be"])  # P5 重建合并为一个自然段
        conv = make_conv(0, result)
        report = adopt_reconstruction_cluster(
            conv, page, sstk, pstk, [], [], [], [], toc_track, pfkstk
        )
        self.assertTrue(report["adopted"])
        self.assertEqual(report["level"], 2)
        self.assertEqual(report["merged_paragraphs"], 1)
        self.assertEqual(sstk, ["Let x be"])
        self.assertEqual(len(pstk), 1)
        self.assertTrue(pstk[0].brk)  # 取最后一段 brk
        self.assertEqual(toc_track, [[]])
        self.assertEqual(pfkstk, [{"F1", "F2"}])

    def test_toc_present_fallback(self):
        page = SimpleNamespace(pageid=0, width=600.0)
        # 真实目录行结构：标题 + 点线 + 页码（detect_toc_line 识别；单行 brk=False）
        sstk = ["Chapter 1 Introduction .... 5"]
        pstk = make_pstk(1)
        pstk[0].brk = False
        toc_track = [
            [
                (".", 480.0, 486.0),
                (".", 486.0, 492.0),
                (".", 492.0, 498.0),
                (".", 498.0, 504.0),
                ("5", 510.0, 516.0),
            ]
        ]
        result = make_result(0, ["Chapter 1 Introduction .... 5"])
        conv = make_conv(0, result)
        report = adopt_reconstruction_cluster(
            conv, page, sstk, pstk, [], [], [], [], toc_track, [set()]
        )
        self.assertFalse(report["adopted"])
        self.assertEqual(report["reason"], "toc_present")
        self.assertEqual(sstk, ["Chapter 1 Introduction .... 5"])

    def test_body_page_with_page_numbers_is_not_toc(self):
        """P1 精判修复：正文页含页码/年份数字（track 有数字）不是目录行，
        不得因旧判据 ``any(toc_track[t])`` 误判 toc_present 而回退。"""
        page = SimpleNamespace(pageid=0, width=600.0)
        sstk = ["Vol. 11 (1989), 263-282"]  # 正文末尾含年份/页码范围
        pstk = make_pstk(1)
        toc_track = [
            [
                ("1", 200.0, 208.0),
                ("1", 220.0, 228.0),
                ("2", 400.0, 408.0),
                ("6", 420.0, 428.0),
                ("3", 500.0, 508.0),
                ("2", 510.0, 518.0),
                ("8", 520.0, 528.0),
                ("2", 530.0, 538.0),
            ]
        ]
        result = make_result(0, ["Vol. 11 (1989), 263-282"])
        conv = make_conv(0, result)
        report = adopt_reconstruction_cluster(
            conv, page, sstk, pstk, [], [], [], [], toc_track, [set()]
        )
        self.assertTrue(report["adopted"])

    def test_text_mismatch_fallback(self):
        page = SimpleNamespace(pageid=0)
        sstk = ["A"]
        pstk = make_pstk(1)
        toc_track = [[]]
        result = make_result(0, ["Completely different"])
        conv = make_conv(0, result)
        before = (pstk[0].x0, pstk[0].y0)
        report = adopt_reconstruction_cluster(
            conv, page, sstk, pstk, [], [], [], [], toc_track, [set()]
        )
        self.assertFalse(report["adopted"])
        self.assertEqual(report["reason"], "text_mismatch")
        self.assertEqual(sstk, ["A"])
        self.assertEqual((pstk[0].x0, pstk[0].y0), before)

    def test_channel_disabled(self):
        page = SimpleNamespace(pageid=0)
        conv = make_conv(0, make_result(0, ["A"]))
        conv.reconstruction_channel = False
        report = adopt_reconstruction_cluster(
            conv, page, ["A"], make_pstk(1), [], [], [], [], [[]], [set()]
        )
        self.assertFalse(report["adopted"])
        self.assertEqual(report["reason"], "adopt_disabled")

    def test_adopt_disabled(self):
        page = SimpleNamespace(pageid=0)
        conv = make_conv(0, make_result(0, ["A"]), adopted=False)
        report = adopt_reconstruction_cluster(
            conv, page, ["A"], make_pstk(1), [], [], [], [], [[]], [set()]
        )
        self.assertFalse(report["adopted"])
        self.assertEqual(report["reason"], "reconstruction_adopt_disabled")

    def test_no_result(self):
        page = SimpleNamespace(pageid=0)
        conv = make_conv(0, None)
        report = adopt_reconstruction_cluster(
            conv, page, ["A"], make_pstk(1), [], [], [], [], [[]], [set()]
        )
        self.assertFalse(report["adopted"])
        self.assertEqual(report["reason"], "no_reconstruction_result")


# ── render_source 标注 ─────────────────────────────────────────────


class TestRenderSourceMarking(unittest.TestCase):
    def test_reconstructed_when_adopted(self):
        conv = Mock()
        conv.reconstruction_records = {0: {"page_id": 0}}
        conv.reconstruction_adoptions = {
            0: {"adopted": True, "level": 2, "merged_paragraphs": 1}
        }
        page = SimpleNamespace(pageid=0)
        run_reconstruction_channel(conv, page)  # 幂等分支：只标注不重算
        rec = conv.reconstruction_records[0]
        self.assertEqual(rec["render_source"], "reconstructed")
        self.assertEqual(rec["render_consumer"], "legacy_renderer")
        self.assertEqual(rec["adopt_level"], 2)

    def test_legacy_when_not_adopted(self):
        conv = Mock()
        conv.reconstruction_records = {0: {"page_id": 0}}
        conv.reconstruction_adoptions = {
            0: {"adopted": False, "reason": "text_mismatch"}
        }
        page = SimpleNamespace(pageid=0)
        run_reconstruction_channel(conv, page)
        rec = conv.reconstruction_records[0]
        self.assertEqual(rec["render_source"], "legacy")
        self.assertEqual(rec["render_consumer"], "none")
        self.assertEqual(rec["adopt_reason"], "text_mismatch")


# ── e2e：receive_layout 真实页面 ───────────────────────────────────


class TestMainlineReconstructionAdoption(unittest.TestCase):
    def build_converter(self, page):
        rsrcmgr = PDFResourceManager()
        conv = TranslateConverter(
            rsrcmgr,
            layout={page.pageid: make_layout()},
            lang_in="en",
            lang_out="zh-CN",
            service="google",
        )
        conv.thread = 1
        conv.noto_name = "noto"
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
        translator.translate = Mock(side_effect=lambda s: "译文")
        translator.lang_in = "en"
        translator.lang_out = "zh-CN"
        conv.translator = translator
        conv.emit_ir = False
        conv.relayout_gate = None
        # 阶段 3 接线开关 + 容器
        conv.reconstruction_channel = True
        conv.reconstruction_adopt = True
        conv.reconstruction_records = {}
        conv.reconstruction_qa = {}
        conv.reconstruction_results = {}
        conv.reconstruction_adoptions = {}
        return conv

    def test_e2e_adopt_report_and_source(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 650, "Let f(x) be continuous.")
        add_text(page, 50, 638, "The sum converges to 2.")
        conv = self.build_converter(page)
        conv.receive_layout(page)
        report = conv.reconstruction_adoptions.get(1)
        self.assertIsNotNone(report, "接管报告应逐页产出")
        self.assertIn("adopted", report)
        rec = conv.reconstruction_records.get(1)
        self.assertIsNotNone(rec)
        if report.get("adopted"):
            self.assertEqual(rec["render_source"], "reconstructed")
            self.assertEqual(rec["render_consumer"], "legacy_renderer")
        else:
            self.assertEqual(rec["render_source"], "legacy")
            self.assertIn("adopt_reason", rec)

    def test_e2e_disabled_adopt_keeps_records_but_no_rendering_change(self):
        page = LTPage(2, (0, 0, 600, 800))
        add_text(page, 50, 650, "Hello world")
        conv = self.build_converter(page)
        conv.reconstruction_adopt = False
        conv.receive_layout(page)
        report = conv.reconstruction_adoptions.get(2)
        self.assertEqual(report["adopted"], False)
        self.assertEqual(report["reason"], "reconstruction_adopt_disabled")
        self.assertEqual(conv.reconstruction_records[2]["render_source"], "legacy")


if __name__ == "__main__":
    unittest.main()
