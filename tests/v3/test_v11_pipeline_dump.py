# -*- coding: utf-8 -*-
"""V1.9 — 可观测层（PipelineDump）：逐阶段 dump 定位字符流损坏。

覆盖：
- has_replacement：� / (cid:N) 信号识别；
- glyph_dump：替换字符标记 is_replacement、cid/font/bbox 记录；
- line/block_dump：geometry 行/段文本 + 几何 + replacement 信号；
- toc_dump：解析字段 + 置信度 + title_has_replacement（不改 TOC 引擎）；
- translation_dump：source/translated 对 + same + 损坏对比；
- layout_dump：gate 记录 + 门控裁决；
- dump_page / run_pipeline_dump 侧通道端到端；
- dump_pdf_pipeline：真实 PDF 恒等翻译 dump（回答「提取层是否已坏」）。
"""
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from pdfminer.layout import LTChar, LTPage
from pdfminer.pdfinterp import PDFResourceManager

from pdf2zh.v3.pipeline_dump import has_replacement


def make_char(x, y, text="A", size=10.0, fontname="Helvetica", cid=None):
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
    ch.cid = ord(text[0]) if cid is None else cid
    ch.font = font
    return ch


def add_text(page, x0, y, text, adv=9.0, fontname="Helvetica"):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname))


def build_converter(**kwargs):
    from pdf2zh.converter import TranslateConverter
    from pdf2zh.collision_resolver import CollisionResolver
    translator = Mock()
    translator.translate = Mock(side_effect=lambda t: "YI" + t)
    translator.lang_in = "en"
    translator.lang_out = "zh-CN"
    with patch("pdf2zh.converter.build_translator") as bt:
        bt.return_value = translator
        conv = TranslateConverter(
            PDFResourceManager(),
            layout={},
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
    conv.collision_resolver = CollisionResolver()
    conv.translator = translator
    conv.emit_ir = False
    conv.relayout_gate = None
    for k, v in kwargs.items():
        setattr(conv, k, v)
    return conv


class TestHasReplacement(unittest.TestCase):
    def test_fffd_detected(self):
        self.assertTrue(has_replacement("Per\uFFFDev\uFFFDuation"))
        self.assertTrue(has_replacement("\ufffd"))

    def test_cid_notdef_detected(self):
        self.assertTrue(has_replacement("(cid:53) x"))
        self.assertFalse(has_replacement("(cid)"))

    def test_clean_text(self):
        self.assertFalse(has_replacement("Performance Evaluation 7.13"))


class TestGlyphDump(unittest.TestCase):
    def test_replacement_glyph_flagged(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Per")           # 正常字形
        add_text(page, 50 + 3 * 9, 700, "\ufffd")  # 损坏字形（同字体名）
        from pdf2zh.v3.pipeline_dump import glyph_dump
        glyphs = glyph_dump(page)
        chars = [g for g in glyphs if g["char"]]
        self.assertEqual(len(chars), 4)
        broken = [g for g in chars if g["is_replacement"]]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["decode"], "fffd")

    def test_cid_notdef_glyph_flagged(self):
        page = LTPage(1, (0, 0, 600, 800))
        page.add(make_char(50, 700, "(cid:53)", cid=53))
        from pdf2zh.v3.pipeline_dump import glyph_dump
        glyphs = glyph_dump(page)
        notdefs = [g for g in glyphs if g["decode"] == "notdef"]
        self.assertEqual(len(notdefs), 1)
        self.assertTrue(notdefs[0]["is_replacement"])

    def test_glyph_fields_recorded(self):
        page = LTPage(1, (0, 0, 600, 800))
        page.add(make_char(50, 700, "A", size=12.0, fontname="XYZ-Bold"))
        from pdf2zh.v3.pipeline_dump import glyph_dump
        g = [x for x in glyph_dump(page) if x["char"] == "A"][0]
        self.assertEqual(g["font"], "XYZ-Bold")
        self.assertEqual(g["size"], 12.0)
        self.assertGreater(g["x1"], g["x0"])


class TestBlockLineDump(unittest.TestCase):
    def test_line_and_block_text(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Performance Evaluation")
        add_text(page, 50, 680, "7.13")
        from pdf2zh.v3.geometry import chars_from_ltpage
        from pdf2zh.v3.pipeline_dump import block_dump, line_dump
        chars = chars_from_ltpage(page, page_num=1)
        blocks = block_dump(chars, page_num=1)
        lines = line_dump(chars, page_num=1)
        self.assertTrue(any("Performance Evaluation" in b["text"]
                            for b in blocks))
        self.assertTrue(any("7.13" in l["text"] for l in lines))
        self.assertFalse(any(b["has_replacement"] for b in blocks))

    def test_replacement_signal_in_blocks(self):
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "Bad \ufffd title")
        from pdf2zh.v3.geometry import chars_from_ltpage
        from pdf2zh.v3.pipeline_dump import block_dump
        chars = chars_from_ltpage(page, page_num=1)
        blocks = block_dump(chars, page_num=1)
        self.assertTrue(any(b["has_replacement"] for b in blocks))


class TestTocDump(unittest.TestCase):
    def _conv_with_toc_record(self, raw, translated="第7.13节 YI intro"):
        from pdf2zh.v3.mainline_wiring import _new_gate_record
        conv = build_converter()
        conv._gate_records = [
            _new_gate_record(50, 700, 300, 10, raw, translated, toc_mode=True),
            _new_gate_record(50, 660, 300, 10,
                             "Normal body paragraph", "YI body",
                             toc_mode=False),
        ]
        return conv

    def test_toc_dump_fields_and_confidence(self):
        from pdf2zh.v3.pipeline_dump import toc_dump
        # 真实路径：gate 文本 = 标题余量（号段被剥离），PLAIN → 回退组合译头
        conv = self._conv_with_toc_record("Performance Evaluation",
                                          "第7.13节 YI Performance Evaluation")
        entries = toc_dump(conv, 1)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["kind"], "section")
        self.assertEqual(e["number"], "7.13")
        self.assertIn("Performance Evaluation", e["title"])
        self.assertGreaterEqual(e["confidence"], 0.6)
        self.assertFalse(e["raw_has_replacement"])
        self.assertFalse(e["title_has_replacement"])

    def test_bare_multi_number_parse_not_eating_title(self):
        # v1.9 回归修复：多点编号 + 空格标题不再被单点规则吃掉标题
        from pdf2zh.v3.toc_semantics import parse_toc_entry
        e = parse_toc_entry("7.13 Performance Evaluation")
        self.assertEqual(e.kind.value, "section")
        self.assertEqual(e.number, "7.13")
        self.assertEqual(e.title, "Performance Evaluation")

    def test_toc_dump_flags_corrupt_title(self):
        from pdf2zh.v3.pipeline_dump import toc_dump
        conv = self._conv_with_toc_record("\ufffd\ufffd\ufffd",
                                          "第7.13节 YI \ufffd\ufffd")
        entries = toc_dump(conv, 1)
        e = entries[0]
        self.assertTrue(e["raw_has_replacement"])
        # 损坏信号压低置信度（组合译头命中但 raw 含替换字符）
        self.assertLess(e["confidence"], 0.6)

    def test_translation_dump_pairs(self):
        from pdf2zh.v3.pipeline_dump import translation_dump
        conv = self._conv_with_toc_record("Performance Evaluation",
                                          "第7.13节 YI Performance Evaluation")
        pairs = translation_dump(conv, 1)
        self.assertEqual(len(pairs), 2)
        toc = [p for p in pairs if p["node_type"] == "toc"][0]
        self.assertEqual(toc["source"], "Performance Evaluation")
        self.assertEqual(toc["translated"], "第7.13节 YI Performance Evaluation")
        self.assertFalse(toc["same"])

    def test_layout_dump_includes_verdict(self):
        from pdf2zh.v3.pipeline_dump import layout_dump
        conv = self._conv_with_toc_record("Performance Evaluation")
        conv.gate_verdicts = {1: {"writeback_allowed": True, "overlap_rate": 0.0}}
        ld = layout_dump(conv, 1)
        self.assertEqual(ld["page"], 1)
        self.assertEqual(len(ld["blocks"]), 2)
        self.assertEqual(ld["gate_verdict"]["writeback_allowed"], True)


class TestRunDump(unittest.TestCase):
    def test_runs_group_same_style_and_split_on_font_change(self):
        from pdf2zh.v3.geometry import chars_from_ltpage
        from pdf2zh.v3.pipeline_dump import run_dump
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 ", fontname="ABCDE+Times")
        add_text(page, 50 + 6 * 9, 700, "Parser", fontname="ABCDE+Times-Bold")
        add_text(page, 50, 680, "292", fontname="ABCDE+Times")
        chars = chars_from_ltpage(page, page_num=1)
        runs = run_dump(chars, page_num=1)
        self.assertEqual(len(runs), 3)  # 3 个 run（字体/字号变化即拆分）
        bold = [r for r in runs if "Parser" in r["text"]][0]
        self.assertEqual(bold["font"], "ABCDE+Times-Bold")
        self.assertFalse(any(r["has_replacement"] for r in runs))

    def test_runs_flag_replacement(self):
        from pdf2zh.v3.geometry import chars_from_ltpage
        from pdf2zh.v3.pipeline_dump import run_dump
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "\ufffd\ufffd")
        chars = chars_from_ltpage(page, page_num=1)
        runs = run_dump(chars, page_num=1)
        self.assertTrue(runs[0]["has_replacement"])


class TestGlyphFontDecodeSignals(unittest.TestCase):
    def test_cid_font_without_to_unicode_flagged(self):
        from pdf2zh.v3.pipeline_dump import glyph_dump
        page = LTPage(1, (0, 0, 600, 800))
        ch = make_char(50, 700, "\ufffd", fontname="ABCDE+F1")
        font = Mock()
        font.is_multibyte = True
        font.get_toUnicode.return_value = None
        ch.font = font
        ch.cid = 145
        page.add(ch)
        g = [x for x in glyph_dump(page) if x["char"]][0]
        self.assertEqual(g["font_type"], "cid")
        self.assertFalse(g["has_to_unicode"])
        self.assertTrue(g["is_replacement"])
        self.assertEqual(g["cid"], 145)

    def test_simple_font_with_to_unicode_ok(self):
        from pdf2zh.v3.pipeline_dump import glyph_dump
        page = LTPage(1, (0, 0, 600, 800))
        ch = make_char(50, 700, "P", fontname="Helvetica")
        font = Mock()
        font.is_multibyte = False
        font.get_toUnicode.return_value = "P"
        ch.font = font
        page.add(ch)
        g = [x for x in glyph_dump(page) if x["char"] == "P"][0]
        self.assertEqual(g["font_type"], "simple")
        self.assertTrue(g["has_to_unicode"])
        self.assertEqual(g["decode"], "ok")


class TestMergedLineDetection(unittest.TestCase):
    def test_merged_toc_lines_flagged(self):
        from pdf2zh.v3.geometry import chars_from_ltpage
        from pdf2zh.v3.pipeline_dump import line_dump
        # 复现「5.1 xx 291 5.2 xx 292 5.2.1 xx 292」被压成一行
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700,
                 "5.1 Intro 291 5.2 Arch 292 5.2.1 Parser 292")
        chars = chars_from_ltpage(page, page_num=1)
        lines = line_dump(chars, page_num=1)
        self.assertTrue(any(l["suspected_merged_entries"] for l in lines))

    def test_single_toc_line_not_flagged(self):
        from pdf2zh.v3.geometry import chars_from_ltpage
        from pdf2zh.v3.pipeline_dump import line_dump
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 Parser ...... 292")
        chars = chars_from_ltpage(page, page_num=1)
        lines = line_dump(chars, page_num=1)
        self.assertFalse(any(l["suspected_merged_entries"] for l in lines))


class TestTocTree(unittest.TestCase):
    def _entries(self):
        return [
            {"line": 0, "number": "5", "title": "Results", "page": "290",
             "level": 1, "kind": "chapter"},
            {"line": 1, "number": "5.1", "title": "Intro", "page": "291",
             "level": 2, "kind": "section"},
            {"line": 2, "number": "5.2", "title": "Arch", "page": "292",
             "level": 2, "kind": "section"},
            {"line": 3, "number": "5.2.1", "title": "Parser", "page": "292",
             "level": 3, "kind": "section"},
            {"line": 4, "number": "5.2.2", "title": "Renderer", "page": "293",
             "level": 3, "kind": "section"},
            {"line": 5, "number": "5.3", "title": "Summary", "page": "294",
             "level": 2, "kind": "section"},
        ]

    def test_hierarchy_and_depths(self):
        from pdf2zh.v3.toc_tree import build_toc_tree
        tree = build_toc_tree(self._entries())
        by_line = {n["line"]: n for n in tree["nodes"]}
        self.assertEqual(tree["roots"], [0])
        self.assertEqual(by_line[0]["depth"], 0)
        self.assertEqual(by_line[1]["parent"], 0)
        self.assertEqual(by_line[1]["depth"], 1)
        self.assertEqual(by_line[3]["parent"], 2)
        self.assertEqual(by_line[3]["depth"], 2)
        self.assertEqual(by_line[5]["parent"], 0)
        self.assertEqual(tree["max_depth"], 2)
        # 渲染缩进 = depth
        self.assertEqual(by_line[3]["indent"], 2)

    def test_non_dotted_fallback_chain(self):
        from pdf2zh.v3.toc_tree import build_toc_tree
        entries = [
            {"line": 0, "number": "第5章", "title": "结果", "page": "290",
             "level": 1, "kind": "chapter"},
            {"line": 1, "number": "5.1", "title": "引言", "page": "291",
             "level": 2, "kind": "section"},
            {"line": 2, "number": "5.2", "title": "架构", "page": "292",
             "level": 2, "kind": "section"},
            {"line": 3, "number": "附录A", "title": "附录", "page": "300",
             "level": 1, "kind": "appendix"},
        ]
        tree = build_toc_tree(entries)
        by_line = {n["line"]: n for n in tree["nodes"]}
        # 5.1 挂在 第5章 下；附录A 与第5章平级
        self.assertEqual(by_line[1]["parent"], 0)
        self.assertEqual(by_line[3]["parent"], None)
        self.assertEqual(by_line[3]["depth"], 0)


class TestCanonicalPageModel(unittest.TestCase):
    def test_tree_nesting_glyph_span_line_block(self):
        from pdf2zh.v3.canonical_page import build_page_model
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 ", fontname="ABCDE+Times")
        add_text(page, 50 + 6 * 9, 700, "Parser", fontname="ABCDE+Times-Bold")
        add_text(page, 50, 680, "292", fontname="ABCDE+Times")
        pm = build_page_model(page, page_num=3)
        stats = pm.stats()
        self.assertGreaterEqual(stats["blocks"], 1)
        self.assertGreaterEqual(stats["lines"], 2)
        self.assertGreaterEqual(stats["spans"], 2)  # 字体变化 → span 拆分
        self.assertGreaterEqual(stats["glyphs"], 12)
        # 任一 span 含字形，字形可序列化
        spans = [s for b in pm.blocks for l in b.lines for s in l.spans]
        self.assertTrue(all(s.glyphs for s in spans))
        d = pm.to_dict()
        self.assertIn("blocks", d)
        self.assertIn("stats", d)
        self.assertIn("unassigned_glyphs", d)

    def test_font_span_split(self):
        from pdf2zh.v3.canonical_page import build_page_model
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "AB", fontname="F1")
        add_text(page, 50 + 2 * 9, 700, "CD", fontname="F2")
        pm = build_page_model(page, page_num=3)
        spans = [s for b in pm.blocks for l in b.lines for s in l.spans]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0].font, "F1")
        self.assertEqual(spans[1].font, "F2")

    def test_replacement_glyph_decode_fffd(self):
        from pdf2zh.v3.canonical_page import build_page_model
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "\ufffdX")
        pm = build_page_model(page, page_num=3)
        self.assertGreaterEqual(pm.stats()["replacement_glyphs"], 1)

    def test_annotate_toc_marks_block_metadata(self):
        from pdf2zh.v3.canonical_page import annotate_toc, build_page_model
        from pdf2zh.v3.toc_semantics import parse_toc_entry
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 Parser ...... 292")
        pm = build_page_model(page, page_num=3)
        e = parse_toc_entry("5.2.1 Parser ...... 292")
        hits = annotate_toc(pm, [{
            "line": 0, "number": e.number, "title": e.title,
            "page": "292", "confidence": 0.9,
        }])
        self.assertGreaterEqual(hits, 1)
        toc_blocks = [b for b in pm.blocks if b.metadata.get("kind") == "toc"]
        self.assertTrue(toc_blocks)
        b = toc_blocks[0]
        self.assertEqual(b.metadata["toc_number"], "5.2.1")
        self.assertEqual(b.metadata["toc_page"], "292")

    def test_annotate_toc_scan_from_tree_when_legacy_missing(self):
        # legacy 段落合并导致 gate 无 TOC 记录时，树内自扫描仍可标注
        from pdf2zh.v3.canonical_page import annotate_toc_scan, build_page_model
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 Parser ...... 292")
        add_text(page, 50, 680, "Plain body text")
        pm = build_page_model(page, page_num=3)
        hits = annotate_toc_scan(pm)
        self.assertGreaterEqual(hits, 1)
        toc = [b for b in pm.blocks if b.metadata.get("kind") == "toc"]
        self.assertTrue(toc)
        self.assertEqual(toc[0].metadata["toc_number"], "5.2.1")
        self.assertEqual(toc[0].metadata["toc_page"], "292")
        self.assertTrue(toc[0].metadata["toc_scan"])
        # 两行被几何正确拆为两个块 → 两条都被标注（树结构未失效）
        page2 = LTPage(3, (0, 0, 600, 800))
        add_text(page2, 50, 700, "5.2.1 Parser ...... 292")
        add_text(page2, 50, 690, "5.2.2 Renderer ...... 293")
        pm2 = build_page_model(page2, page_num=3)
        hits2 = annotate_toc_scan(pm2)
        self.assertGreaterEqual(hits2, 2)
        numbers = [b.metadata["toc_number"] for b in pm2.blocks
                   if b.metadata.get("kind") == "toc"]
        self.assertIn("5.2.1", numbers)
        self.assertIn("5.2.2", numbers)
        # 多行块（含换行）不判定：疑似合并交行级诊断
        pm3 = build_page_model(page2, page_num=3)
        for blk in pm3.blocks:
            blk.text = (blk.text or "") + "\n extra"
        self.assertEqual(annotate_toc_scan(pm3), 0)

    def test_annotate_formulas_marks_math_spans(self):
        from pdf2zh.v3.canonical_page import annotate_formulas, build_page_model
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "x^2 + y^2 = z^2")
        add_text(page, 50, 660, "Plain body text here")
        pm = build_page_model(page, page_num=3)
        marked = annotate_formulas(pm)
        self.assertGreaterEqual(marked, 1)
        math_spans = [s for b in pm.blocks for l in b.lines
                      for s in l.spans if s.metadata.get("math")]
        self.assertTrue(math_spans)
        self.assertIn("formula_density",
                      [b.metadata for b in pm.blocks][0])

    def test_annotate_style_multifont_signal(self):
        from pdf2zh.v3.canonical_page import annotate_style, build_page_model
        page = LTPage(3, (0, 0, 600, 800))
        add_text(page, 50, 700, "AB", fontname="F1")
        add_text(page, 50 + 2 * 9, 700, "CD", fontname="F2")
        pm = build_page_model(page, page_num=3)
        annotate_style(pm)
        b = pm.blocks[0]
        self.assertTrue(b.metadata["multifont"])
        self.assertEqual(sorted(b.metadata["fonts"]), ["F1", "F2"])

    def test_dump_page_includes_page_model(self):
        from pdf2zh.v3.mainline_wiring import run_pipeline_dump
        page = LTPage(2, (0, 0, 600, 800))
        add_text(page, 50, 700, "7.13 Performance Evaluation")
        conv = build_converter()
        conv._gate_records = []
        run_pipeline_dump(conv, page)
        dump = conv.pipeline_dumps[2]
        self.assertIn("page_model", dump)
        self.assertIsNotNone(dump["page_model"])
        self.assertIn("blocks", dump["page_model"])


class TestDumpPageChannel(unittest.TestCase):
    def test_run_pipeline_dump_end_to_end(self):
        from pdf2zh.v3.mainline_wiring import run_pipeline_dump
        page = LTPage(2, (0, 0, 600, 800))
        add_text(page, 50, 700, "7.13 Performance Evaluation ...... 479")
        conv = build_converter()
        conv._gate_records = [
            {"x": 50, "y": 700, "width": 300, "height": 20, "size": 10,
             "text": "7.13 Performance Evaluation",
             "translated": "第7.13节 YI Performance Evaluation",
             "node_type": "toc"},
        ]
        run_pipeline_dump(conv, page)
        dump = conv.pipeline_dumps[2]
        self.assertIn("glyphs", dump)
        self.assertIn("runs", dump)
        self.assertIn("blocks", dump)
        self.assertIn("toc", dump)
        self.assertIn("toc_tree", dump)
        self.assertIn("translations", dump)
        self.assertIn("layout", dump)
        self.assertEqual(len(dump["toc"]), 1)
        self.assertEqual(dump["toc"][0]["number"], "7.13")


class TestDumpPdfPipeline(unittest.TestCase):
    def test_real_pdf_dump_detects_corruption(self):
        import fitz
        from pdf2zh.v3.pipeline_dump import dump_pdf_pipeline
        out = tempfile.mkdtemp(prefix="v11_dump_")
        path = os.path.join(out, "in.pdf")
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 700), "7.13 Performance Evaluation", fontsize=10)
        page.insert_text((72, 680), "479", fontsize=10)
        doc.save(path)
        doc.close()
        manifest = dump_pdf_pipeline(path, out_dir=out, max_pages=1)
        self.assertEqual(len(manifest), 2)  # 1 页 + document_model 条目
        m = [x for x in manifest if x.get("page") != "all"][0]
        self.assertIn("dump", m)
        self.assertTrue(os.path.exists(m["dump"]))
        # 标准字体 → 提取层应无替换字符
        self.assertEqual(m["replacement_glyphs"], 0)
        self.assertGreater(m["blocks"], 0)
        # 文档统一模型落盘
        dm_entry = [x for x in manifest if x.get("page") == "all"][0]
        self.assertIn("document_model", dm_entry)
        self.assertTrue(os.path.exists(dm_entry["document_model"]))


if __name__ == "__main__":
    unittest.main()
