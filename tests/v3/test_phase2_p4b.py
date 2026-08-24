"""Headless tests for Phase 2 - P4b: Diagnostic + IssueMapper + Evaluator."""

from __future__ import annotations
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType

B = (0, 0, 0, 0)


class TestDiagnosticReport(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.evaluator import DiagnosticReport

        self.rpt = DiagnosticReport()

    def test_empty(self):
        self.assertEqual(self.rpt.total, 0)
        self.assertEqual(self.rpt.pass_rate, 0.0)

    def test_add_passed(self):
        self.rpt.add(metric="test", value=95, threshold=80, passed=True)
        self.assertEqual(self.rpt.passed_count, 1)

    def test_add_failed(self):
        self.rpt.add(metric="test", value=50, threshold=80, passed=False)
        self.assertEqual(self.rpt.failed_count, 1)

    def test_pass_rate(self):
        self.rpt.add("a", value=90, passed=True)
        self.rpt.add("b", value=50, passed=False)
        self.assertAlmostEqual(self.rpt.pass_rate, 50.0)

    def test_clear(self):
        self.rpt.add("x", value=100, passed=True)
        self.rpt.clear()
        self.assertEqual(self.rpt.total, 0)

    def test_to_dict(self):
        self.rpt.add("test", value=85, passed=True)
        d = self.rpt.to_dict()
        self.assertIn("total", d)
        self.assertEqual(len(d["records"]), 1)

    def test_to_text_pass(self):
        self.rpt.add("test", value=85, threshold=80, passed=True)
        self.assertIn("PASS", self.rpt.to_text())

    def test_to_text_fail(self):
        self.rpt.add("test", value=40, threshold=80, passed=False)
        self.assertIn("FAIL", self.rpt.to_text())


class TestEvaluationIssueMapper(unittest.TestCase):
    def test_no_issues(self):
        from pdf2zh.v3.evaluator import EvaluationResult, EvaluationIssueMapper

        r = EvaluationResult(
            translation_score=95,
            semantic_score=90,
            typography_score=85,
            layout_score=95,
            consistency_score=90,
        )
        self.assertEqual(EvaluationIssueMapper.map_result(r).total, 0)

    def test_low_translation(self):
        from pdf2zh.v3.evaluator import EvaluationResult, EvaluationIssueMapper

        r = EvaluationResult(
            translation_score=50,
            semantic_score=85,
            typography_score=85,
            layout_score=85,
            consistency_score=85,
        )
        issues = EvaluationIssueMapper.map_result(r).get_by_module("translation")
        self.assertGreater(len(issues), 0)

    def test_low_layout_critical(self):
        from pdf2zh.v3.evaluator import (
            EvaluationResult,
            EvaluationIssueMapper,
            IssueSeverity,
        )

        r = EvaluationResult(
            translation_score=95,
            semantic_score=95,
            typography_score=95,
            layout_score=40,
            consistency_score=95,
        )
        issues = EvaluationIssueMapper.map_result(r).get_by_module("layout")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, IssueSeverity.CRITICAL)


class TestQualityEvaluatorDiagnostic(unittest.TestCase):
    def test_diagnostic_on_result(self):
        from pdf2zh.v3.evaluator import QualityEvaluator, DiagnosticReport

        g = DocumentGraph()
        g.add_node(
            DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="test")
        )
        result = QualityEvaluator().evaluate(g, g)
        self.assertIsNotNone(result._diagnostic)
        self.assertIsInstance(result._diagnostic, DiagnosticReport)

    def test_generate_diagnostic(self):
        from pdf2zh.v3.evaluator import QualityEvaluator, DiagnosticReport

        g = DocumentGraph()
        g.add_node(
            DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="test")
        )
        diag = QualityEvaluator().generate_diagnostic(g, g)
        self.assertIsInstance(diag, DiagnosticReport)

    def test_diagnostic_property(self):
        from pdf2zh.v3.evaluator import QualityEvaluator, DiagnosticReport

        evaluator = QualityEvaluator()
        g = DocumentGraph()
        g.add_node(
            DocumentNode(id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="test")
        )
        evaluator.evaluate(g, g)
        self.assertIsNotNone(evaluator.diagnostic)
        self.assertIsInstance(evaluator.diagnostic, DiagnosticReport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
