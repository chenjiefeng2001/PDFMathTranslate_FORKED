# -*- coding: utf-8 -*-
"""V1.16 — Phase 5：文档智能与自动修复。

覆盖：
- 5.1/5.2 Diagnostics：unicode_error/toc_merged_lines/toc_low_confidence/
  formula/overflow/empty 检测 + admissible 判定；
- 5.2 Confidence Model：节点 confidence/source/uncertainty（替换字符惩罚）；
- 5.3 Evidence Fusion：一致加成 / 矛盾惩罚；
- 5.3 RepairEngine：TOC 拆分真实修复、Unicode/Math/Empty 策略、
  repair_loop 验证改善；
- 5.4 LLM Planner：规则回退 + LLM JSON 决策；
- 5.5 Corpus Regression：Expected IR 桶对比；
- 主链路：diagnostics/confidence 进 model.metadata。
"""
import unittest
from unittest.mock import Mock

from pdfminer.layout import LTChar, LTPage

from pdf2zh.v3.document_model import build_document_model


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


def add_text(page, x0, y, text, adv=9.0, fontname="Helvetica", size=10.0):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname,
                           size=size))


def build_model():
    page = LTPage(1, (0, 0, 600, 800))
    add_text(page, 50, 760, "5 Methodology", size=16)
    add_text(page, 50, 740, "5.1 Data Collection")
    add_text(page, 50, 720, "Plain body text here")
    add_text(page, 50, 700, "x^2 + y^2 = z^2")
    return build_document_model([page])


class TestDiagnostics(unittest.TestCase):
    def test_clean_model_admissible(self):
        from pdf2zh.v3.diagnostics import analyze_document
        model = build_model()
        report = analyze_document(model)
        self.assertTrue(report.admissible)
        self.assertEqual(report.error_count, 0)

    def test_unicode_error_detected(self):
        from pdf2zh.v3.diagnostics import analyze_document
        model = build_model()
        page = model.pages[0]
        page.blocks[2].lines[0].spans[0].glyphs[0].decode = "fffd"
        report = analyze_document(model)
        codes = {i.code for i in report.issues}
        self.assertIn("unicode_error", codes)
        self.assertFalse(report.admissible)

    def test_toc_merged_lines_detected(self):
        from pdf2zh.v3.diagnostics import analyze_document
        from pdf2zh.v3.canonical_page import BlockModel, LineModel
        model = build_model()
        page = model.pages[0]
        page.blocks.append(BlockModel(
            text="5.1 Intro ...... 291 5.2 Arch ...... 292\n5.3 Summary ...... 293",
            kind="paragraph", x0=0, y0=0, x1=300, y1=20))
        page.blocks[-1].lines = [
            LineModel(text="5.1 Intro ...... 291 5.2 Arch ...... 292"),
            LineModel(text="5.3 Summary ...... 293"),
        ]
        report = analyze_document(model)
        codes = {i.code for i in report.issues}
        self.assertIn("toc_merged_lines", codes)
        self.assertFalse(report.admissible)

    def test_toc_low_confidence_warning(self):
        from pdf2zh.v3.diagnostics import analyze_document
        from pdf2zh.v3.canonical_page import BlockModel
        model = build_model()
        page = model.pages[0]
        page.blocks.append(BlockModel(
            text="5.1 Intro ...... 291", kind="toc",
            x0=0, y0=0, x1=200, y1=10))
        page.blocks[-1].metadata.update({"kind": "toc",
                                         "toc_confidence": 0.3})
        report = analyze_document(model)
        codes = {i.code for i in report.issues}
        self.assertIn("toc_low_confidence", codes)

    def test_report_dict_and_summary(self):
        from pdf2zh.v3.diagnostics import analyze_document
        model = build_model()
        d = analyze_document(model).to_dict()
        self.assertIn("errors", d)
        self.assertIn("warnings", d)
        self.assertTrue(d["admissible"])


class TestConfidenceModel(unittest.TestCase):
    def test_confidence_by_kind_and_penalty(self):
        from pdf2zh.v3.diagnostics import annotate_confidence, node_confidence
        model = build_model()
        annotate_confidence(model)
        p1 = model.pages[0]
        # V1.23：Lv2 段拆已把标题（16pt）与正文（10pt）拆开，固定下标不稳；
        # 取公式块（role_confidence 高 → base 高）验证置信度模型。
        body = next(b for b in p1.blocks if b.kind == "formula")
        self.assertGreaterEqual(body.metadata["confidence"], 0.8)
        self.assertEqual(body.metadata["uncertainty"],
                         round(1.0 - body.metadata["confidence"], 3))
        self.assertIn("confidence_source", body.metadata)
        # 替换字符 → 强惩罚
        body.lines[0].spans[0].glyphs[0].decode = "fffd"
        conf_bad, _, _ = node_confidence(body)
        self.assertLess(conf_bad, 0.3)

    def test_empty_block_min_confidence(self):
        from pdf2zh.v3.diagnostics import node_confidence
        from pdf2zh.v3.canonical_page import BlockModel
        b = BlockModel(text="", kind="paragraph")
        b.metadata["anomaly"] = "empty_text"
        conf, _, _ = node_confidence(b)
        self.assertLessEqual(conf, 0.1)


class TestEvidenceFusion(unittest.TestCase):
    def test_consistent_boost(self):
        from pdf2zh.v3.evidence import fuse_evidence, fuse_verdict
        fused = fuse_evidence({"ocr": 0.95, "layout": 0.9, "math": 0.98})
        self.assertGreaterEqual(fused, 0.93)
        v = fuse_verdict({"ocr": 0.95, "layout": 0.9})
        self.assertTrue(v.consistent)
        self.assertGreaterEqual(v.confidence, 0.9)

    def test_conflict_penalty(self):
        from pdf2zh.v3.evidence import fuse_evidence, fuse_verdict
        fused = fuse_evidence({"ocr": 0.95, "math": 0.1})
        self.assertLess(fused, 0.8)
        v = fuse_verdict({"ocr": 0.95, "math": 0.1})
        self.assertFalse(v.consistent)

    def test_empty_scores_default(self):
        from pdf2zh.v3.evidence import fuse_evidence
        self.assertEqual(fuse_evidence({}), 0.5)


class TestRepairEngine(unittest.TestCase):
    def _merged_model(self):
        from pdf2zh.v3.canonical_page import BlockModel, LineModel
        model = build_model()
        page = model.pages[0]
        page.blocks.append(BlockModel(
            text="5.1 Intro ...... 291 5.2 Arch ...... 292\n5.3 Summary ...... 293",
            kind="paragraph", x0=0, y0=0, x1=300, y1=20))
        page.blocks[-1].lines = [
            LineModel(text="5.1 Intro ...... 291 5.2 Arch ...... 292"),
            LineModel(text="5.3 Summary ...... 293"),
        ]
        return model

    def test_toc_split_repair_rebuilds_blocks(self):
        from pdf2zh.v3.diagnostics import analyze_document
        from pdf2zh.v3.repair_engine import RepairEngine
        model = self._merged_model()
        before = len(model.pages[0].blocks)
        report = analyze_document(model)
        rr = RepairEngine().repair(model, report)
        self.assertGreaterEqual(rr.repaired_count, 1)
        self.assertGreater(len(model.pages[0].blocks), before)
        toc = [b for b in model.pages[0].blocks if b.kind == "toc"]
        self.assertGreaterEqual(len(toc), 2)

    def test_repair_loop_improves(self):
        from pdf2zh.v3.repair_engine import repair_loop
        model = self._merged_model()
        result = repair_loop(model, max_iterations=2)
        self.assertTrue(result["improved"])
        self.assertLess(result["after_errors"], result["before_errors"])

    def test_unicode_repair_marks_plan(self):
        from pdf2zh.v3.diagnostics import analyze_document
        from pdf2zh.v3.repair_engine import RepairEngine
        model = build_model()
        page = model.pages[0]
        page.blocks[2].lines[0].spans[0].glyphs[0].decode = "fffd"
        report = analyze_document(model)
        rr = RepairEngine().repair(model, report)
        actions = {r.action for r in rr.results}
        self.assertIn("ocr_fallback", actions)
        block = page.blocks[2]
        self.assertEqual(block.metadata["repair"]["action"], "ocr_fallback")

    def test_math_recovery_marks_plan(self):
        from pdf2zh.v3.diagnostics import analyze_document
        from pdf2zh.v3.repair_engine import RepairEngine
        model = build_model()
        formula = [b for b in model.pages[0].blocks if b.kind == "formula"][0]
        formula.metadata["formula_density"] = 0.1
        report = analyze_document(model, formula_threshold=0.5)
        rr = RepairEngine().repair(model, report)
        actions = {r.action for r in rr.results}
        self.assertIn("latex_ocr", actions)


class TestLLMPlanner(unittest.TestCase):
    def test_rule_fallback(self):
        from pdf2zh.v3.llm_planner import RuleRepairPlanner
        p = RuleRepairPlanner()
        self.assertEqual(p.plan("toc_merged_lines", {}), "toc_split")
        self.assertEqual(p.plan("unicode_error", {}), "unicode_repair")

    def test_llm_planner_parses_json(self):
        from pdf2zh.v3.llm_planner import LLMRepairPlanner
        provider = Mock()
        resp = Mock()
        resp.text = '{"repair": "toc_split", "reason": "merged lines"}'
        provider.complete.return_value = resp
        p = LLMRepairPlanner(provider=provider)
        self.assertEqual(p.plan("toc_merged_lines", {"lines": 2}),
                         "toc_split")

    def test_llm_failure_falls_back(self):
        from pdf2zh.v3.llm_planner import LLMRepairPlanner
        provider = Mock()
        provider.complete.side_effect = RuntimeError("net down")
        p = LLMRepairPlanner(provider=provider)
        self.assertEqual(p.plan("unicode_error", {}), "unicode_repair")

    def test_no_provider_rule(self):
        from pdf2zh.v3.llm_planner import LLMRepairPlanner
        p = LLMRepairPlanner(provider=None)
        self.assertEqual(p.plan("formula_low_confidence", {}),
                         "math_recovery")


class TestCorpusRegression(unittest.TestCase):
    def test_expected_and_compare(self):
        from pdf2zh.v3.corpus_regression import (
            compare_expected, expected_from_model,
        )
        model = build_model()
        expected = expected_from_model(model)
        self.assertGreaterEqual(expected["blocks"], 3)
        self.assertGreaterEqual(expected["headings"], 1)
        diffs = compare_expected(expected, dict(expected))
        self.assertEqual(diffs, {})
        bad = dict(expected)
        bad["blocks"] += 1
        self.assertIn("blocks", compare_expected(expected, bad))

    def test_run_regression(self):
        from pdf2zh.v3.corpus_regression import (
            expected_from_model, run_regression,
        )
        model = build_model()
        expected = expected_from_model(model)
        report = run_regression([("academic/paper", model)],
                                {"academic/paper": expected})
        self.assertEqual(report.passed_count, 1)
        self.assertEqual(report.failed_count, 0)
        # 未登记基线 → 失败
        report2 = run_regression([("academic/paper", model)], {})
        self.assertEqual(report2.failed_count, 1)


class TestMainlineDiagnostics(unittest.TestCase):
    def test_channel_adds_diagnostics_and_confidence(self):
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
            conv = TranslateConverter(PDFResourceManager(), layout={},
                                      lang_in="en", lang_out="zh-CN",
                                      service="stub")
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
        add_text(page, 50, 760, "5 Methodology", size=16)
        add_text(page, 50, 740, "Plain body text here")
        conv._gate_records = []
        run_document_model(conv, page)
        dm = conv.document_model
        self.assertIn("diagnostics", dm.metadata)
        self.assertIn("confidence_stats", dm.metadata)
        diag = dm.metadata["diagnostics"]
        self.assertIn("errors", diag)
        self.assertIn("issues", diag)


if __name__ == "__main__":
    unittest.main()
