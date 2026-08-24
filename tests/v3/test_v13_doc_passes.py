# -*- coding: utf-8 -*-
"""V1.14 — Phase 2/3：Pass 框架 + PassManager + Normalize/Semantic/Policy/
Typography + Inspector + PassDiff（统一模型上的编译式流水线）。

覆盖：
- PassManager：运行/容错（坏 Pass 不中断）/PassDiff（kind/policy 变化）；
- NormalizePass：Unicode NFC/零宽/多空格折叠/阅读序/异常节点；
- SemanticPass：code/table 检测 + roles/toc/formula；
- TranslationPolicyPass：kind → 策略（toc partial / formula preserve…）；
- translate_document 遵循策略（source_text/partial）；
- TypographyPass：度量/断行/溢出/孤立段；
- DocumentInspector：节点全貌 + TOC 视图。
"""

import unittest
from unittest.mock import Mock

from pdfminer.layout import LTChar, LTPage

from pdf2zh.v3.document_model import (
    DocumentModel,
    build_document_model,
    translate_document,
)


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


def add_text(page, x0, y, text, adv=9.0, fontname="Helvetica"):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname))


def build_model():
    page = LTPage(1, (0, 0, 600, 800))
    add_text(page, 50, 740, "5.2.1 Parser ...... 292")
    add_text(page, 50, 720, "if x > 0: return x", fontname="Courier")
    add_text(page, 50, 700, "x^2 + y^2 = z^2")
    add_text(page, 50, 680, "Body text here", fontname="Times")
    add_text(page, 50, 660, "A|B|C")
    add_text(page, 50, 645, "1|2|3")
    return build_document_model([page])


class TestPassManager(unittest.TestCase):
    def test_run_and_report(self):
        from pdf2zh.v3.doc_passes import (
            NormalizePass,
            PassManager,
            SemanticPass,
            TranslationPolicyPass,
        )

        model = build_model()
        mgr = PassManager([NormalizePass(), SemanticPass(), TranslationPolicyPass()])
        report = mgr.run(model)
        self.assertEqual(report.ok_count, 3)
        self.assertEqual(report.failed_count, 0)
        self.assertEqual(len(report.results), 3)

    def test_failed_pass_does_not_abort(self):
        from pdf2zh.v3.doc_passes import DocumentPass, PassManager

        class BoomPass(DocumentPass):
            name = "boom"

            def run(self, doc):
                raise RuntimeError("kaboom")

        model = build_model()
        mgr = PassManager([BoomPass(), BoomPass()])
        report = mgr.run(model)
        self.assertEqual(report.ok_count, 0)
        self.assertEqual(report.failed_count, 2)
        self.assertIn("kaboom", report.results[0].errors[0])

    def test_pass_diff_records_changes(self):
        from pdf2zh.v3.doc_passes import PassManager, SemanticPass

        model = build_model()
        before_kind = model.pages[0].blocks[1].kind
        report = PassManager([SemanticPass()]).run(model)
        diff = report.results[0].diff
        self.assertTrue(diff)
        kinds = {d.block_id: d for d in diff if d.field == "kind"}
        # code 块 kind 变化应被记录
        changed = [d for d in diff if d.field == "kind" and d.before != d.after]
        self.assertTrue(changed)


class TestNormalizePass(unittest.TestCase):
    def test_normalize_text(self):
        from pdf2zh.v3.doc_passes import normalize_text

        self.assertEqual(normalize_text("A  B\t\tC"), "A B C")
        self.assertEqual(normalize_text("x\u200bx"), "xx")
        self.assertEqual(normalize_text("e\u0301"), "é")  # NFC

    def test_normalize_pass_writes_reading_order_and_anomaly(self):
        from pdf2zh.v3.doc_passes import NormalizePass
        from pdf2zh.v3.canonical_page import BlockModel, LineModel

        model = build_model()
        model.pages[0].blocks.append(
            BlockModel(
                text="Bad  text\u200b here", kind="paragraph", x0=0, y0=0, x1=100, y1=10
            )
        )
        model.pages[0].blocks[-1].lines = [LineModel(text="Bad  text\u200b here")]
        stats = NormalizePass().run(model)
        self.assertGreaterEqual(stats["reading_order"], 6)
        self.assertGreaterEqual(stats["normalized_blocks"], 1)
        p1 = model.pages[0]
        self.assertEqual(p1.blocks[0].metadata["reading_order"], 0)
        # 脏文本被清洗
        dirty = p1.blocks[-1]
        self.assertEqual(dirty.text, "Bad text here")
        # 空文本块 → 异常标记
        p1.blocks.append(
            BlockModel(text="", kind="paragraph", x0=0, y0=0, x1=10, y1=10)
        )
        NormalizePass().run(model)
        anomalies = [b.metadata.get("anomaly") for b in p1.blocks]
        self.assertIn("empty_text", anomalies)


class TestSemanticPass(unittest.TestCase):
    def test_code_and_table_detection(self):
        from pdf2zh.v3.doc_passes import SemanticPass

        model = build_model()
        stats = SemanticPass().run(model)
        self.assertGreaterEqual(stats["code"], 1)
        self.assertGreaterEqual(stats["tables"], 1)
        kinds = [b.kind for b in model.pages[0].blocks]
        self.assertIn("code", kinds)
        self.assertIn("table", kinds)
        table = [b for b in model.pages[0].blocks if b.kind == "table"][0]
        self.assertGreaterEqual(table.metadata["table_cols"], 3)

    def test_detect_helpers(self):
        from pdf2zh.v3.doc_passes import detect_code_block, detect_table_block
        from pdf2zh.v3.canonical_page import (
            BlockModel,
            LineModel,
            SpanModel,
            GlyphModel,
        )

        code = BlockModel(
            text="def foo(): pass", kind="paragraph", x0=0, y0=0, x1=100, y1=10
        )
        code.lines = [LineModel(text="def foo(): pass")]
        self.assertTrue(detect_code_block(code))
        tbl = BlockModel(
            text="a  b  c\n1  2  3", kind="paragraph", x0=0, y0=0, x1=200, y1=20
        )
        tbl.lines = [LineModel(text="a  b  c"), LineModel(text="1  2  3")]
        self.assertGreaterEqual(detect_table_block(tbl), 3)


class TestTranslationPolicyPass(unittest.TestCase):
    def test_policy_per_kind(self):
        from pdf2zh.v3.doc_passes import (
            SemanticPass,
            TranslationPolicyPass,
            translation_policy_for,
        )

        model = build_model()
        SemanticPass().run(model)
        TranslationPolicyPass().run(model)
        p1 = model.pages[0]
        by_kind = {}
        for b in p1.blocks:
            if b.metadata.get("translation_policy"):
                by_kind.setdefault(b.kind, b.metadata["translation_policy"])
        # toc → partial（只翻标题）
        self.assertFalse(by_kind["toc"]["translate"] is False)
        self.assertTrue(by_kind["toc"]["partial"])
        # formula/code → preserve
        self.assertFalse(by_kind["formula"]["translate"])
        self.assertFalse(by_kind["code"]["translate"])
        # table → preserve
        self.assertFalse(by_kind["table"]["translate"])
        # 正文 → translate
        body = [p for p in by_kind.values() if p.get("reason") == "body"]
        self.assertTrue(body and body[0]["translate"])

    def test_toc_source_text_is_title(self):
        from pdf2zh.v3.doc_passes import translation_policy_for
        from pdf2zh.v3.canonical_page import BlockModel

        toc = BlockModel(
            text="5.2.1 Parser ...... 292", kind="toc", x0=0, y0=0, x1=200, y1=10
        )
        toc.metadata["toc_title"] = "Parser"
        pol = translation_policy_for(toc)
        self.assertTrue(pol["partial"])
        self.assertEqual(pol["source_text"], "Parser")

    def test_translate_document_honors_policy(self):
        from pdf2zh.v3.doc_passes import (
            SemanticPass,
            TranslationPolicyPass,
        )

        model = build_model()
        SemanticPass().run(model)
        TranslationPolicyPass().run(model)
        stats = translate_document(model, lambda t: "译_" + t)
        self.assertGreaterEqual(stats["translated"], 2)
        self.assertGreaterEqual(stats["preserved"], 2)  # formula + code + table
        p1 = model.pages[0]
        formula = [b for b in p1.blocks if b.kind == "formula"][0]
        self.assertFalse(formula.metadata["translate"])
        toc = [b for b in p1.blocks if b.kind == "toc"][0]
        # partial：只翻 source_text（标题），组合不含号段原文
        self.assertTrue(toc.metadata["translated"].startswith("译_"))


class TestTypographyPass(unittest.TestCase):
    def test_typography_engine_functions(self):
        from pdf2zh.v3.typography_engine import (
            line_break,
            measure,
            justify_advances,
        )

        widths = {"a": 5.0, "b": 5.0, " ": 2.0, "中": 10.0, "文": 10.0}

        def mfn(s):
            return measure(s, widths, 4.0)

        lines = line_break("abcdefghij", 20, mfn)
        self.assertGreaterEqual(len(lines), 3)
        self.assertTrue(all(len(l) > 0 for l in lines))
        adv = justify_advances("中文", 30, mfn)
        self.assertAlmostEqual(sum(adv), 30, places=3)
        # 中文行逐字断
        zh_lines = line_break("中文排版测试", 20, mfn)
        self.assertGreaterEqual(len(zh_lines), 2)

    def test_typography_pass_marks_overflow(self):
        from pdf2zh.v3.doc_passes import TypographyPass

        model = build_model()
        stats = TypographyPass(overflow_ratio=0.9).run(model)
        self.assertGreaterEqual(stats["measured"], 5)
        self.assertGreaterEqual(stats["overflow_blocks"], 1)
        p1 = model.pages[0]
        typed = [
            b.metadata["typography"] for b in p1.blocks if b.metadata.get("typography")
        ]
        self.assertTrue(any(t["overflow"] for t in typed))
        self.assertIn("line_count", typed[0])


class TestDocumentInspector(unittest.TestCase):
    def test_inspect_node_view(self):
        from pdf2zh.v3.document_inspector import inspect
        from pdf2zh.v3.doc_passes import default_pass_manager

        model = build_model()
        default_pass_manager().run(model)
        p1 = model.pages[0]
        bid = "p1_0"  # toc 块
        view = inspect(model, bid)
        self.assertIsNotNone(view)
        self.assertEqual(view["block_id"], bid)
        self.assertEqual(view["kind"], "toc")
        self.assertIn("bbox", view)
        self.assertIn("translation_policy", view)
        self.assertIn("typography", view)
        self.assertIn("relations", view)
        self.assertIn("reading_order", view)
        self.assertIsNone(inspect(model, "p9_99"))

    def test_inspect_all_and_toc(self):
        from pdf2zh.v3.doc_passes import default_pass_manager
        from pdf2zh.v3.document_inspector import inspect_all, inspect_toc

        model = build_model()
        default_pass_manager().run(model)
        rows = inspect_all(model)
        self.assertGreaterEqual(len(rows), 5)
        toc_rows = inspect_toc(model)
        self.assertGreaterEqual(len(toc_rows), 1)
        self.assertEqual(toc_rows[0]["number"], "5.2.1")


class TestMainlinePipeline(unittest.TestCase):
    def test_pass_report_in_model_metadata(self):
        from pdf2zh.v3.mainline_wiring import run_document_model
        from pdf2zh.converter import TranslateConverter
        from pdf2zh.collision_resolver import CollisionResolver
        from pdfminer.pdfinterp import PDFResourceManager
        from unittest.mock import patch

        translator = Mock()
        translator.translate = Mock(side_effect=lambda t: "YI" + t)
        translator.lang_in = "en"
        translator.lang_out = "zh-CN"
        with patch("pdf2zh.converter.build_translator") as bt:
            bt.return_value = translator
            conv = TranslateConverter(
                PDFResourceManager(),
                layout={},
                lang_in="en",
                lang_out="zh-CN",
                service="stub",
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
        page = LTPage(1, (0, 0, 600, 800))
        add_text(page, 50, 700, "5.2.1 Parser ...... 292")
        conv._gate_records = []
        run_document_model(conv, page)
        dm = conv.document_model
        self.assertIn("pass_report", dm.metadata)
        report = dm.metadata["pass_report"]
        self.assertEqual(report["ok"], 4)  # Normalize/Semantic/Policy/Typography
        self.assertEqual(report["failed"], 0)


if __name__ == "__main__":
    unittest.main()
