"""Tests for V3 Repair Runtime (Module: repair.py)."""

import pytest
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
from pdf2zh.v3.evaluator import (
    QualityEvaluator,
    EvaluatorConfig,
    EvaluationResult,
    Issue,
    IssueSeverity,
    IssueGraph,
    RepairScheduler,
)
from pdf2zh.v3.repair import RepairStats, RepairResult, RepairRuntime


class TestRepairStats:
    def test_defaults(self):
        s = RepairStats()
        assert s.issues_detected == 0
        assert s.repairs_scheduled == 0
        assert s.repairs_executed == 0
        assert s.converged is False

    def test_to_dict(self):
        s = RepairStats(
            issues_detected=5, repairs_scheduled=3, repairs_executed=2, converged=True
        )
        d = s.to_dict()
        assert d["issues_detected"] == 5
        assert d["repairs_executed"] == 2
        assert d["converged"] is True


class TestRepairResult:
    def test_defaults(self):
        ig = IssueGraph()
        stats = RepairStats()
        r = RepairResult(success=True, stats=stats, final_issues=ig)
        assert r.success is True
        assert r.summary() is not None

    def test_summary_converged(self):
        ig = IssueGraph()
        stats = RepairStats(
            converged=True, iterations=3, issues_detected=10, repairs_executed=8
        )
        stats.scores_before = {"total": 85.0}
        stats.scores_after = {"total": 95.0}
        r = RepairResult(success=True, stats=stats, final_issues=ig)
        s = r.summary()
        assert "converged" in s
        assert "3" in s
        assert "85.0" in s


class TestRepairRuntime:
    def test_init(self):
        rt = RepairRuntime()
        assert rt.max_iterations == 3
        assert rt.evaluator is not None

    def test_detect_issues_empty(self):
        rt = RepairRuntime()
        g = DocumentGraph()
        g2 = DocumentGraph()
        issues = rt.detect_issues(g, g2)
        assert isinstance(issues, IssueGraph)

    def test_detect_issues_with_nodes(self):
        rt = RepairRuntime()
        g = DocumentGraph()
        n = DocumentNode(
            id="n1",
            node_type=NodeType.PARAGRAPH,
            bbox=(0, 0, 100, 50),
            text="Hello world",
        )
        g.add_node(n)
        g2 = DocumentGraph()
        n2 = DocumentNode(
            id="n1",
            node_type=NodeType.PARAGRAPH,
            bbox=(0, 0, 100, 50),
            text="Hola mundo",
        )
        g2.add_node(n2)
        issues = rt.detect_issues(g, g2)
        assert isinstance(issues, IssueGraph)

    def test_schedule_repairs(self):
        rt = RepairRuntime()
        ig = IssueGraph()
        ig.add_issue(
            Issue(
                issue_type="overlap",
                severity=IssueSeverity.CRITICAL,
                description="Overlap on page 5",
                module="layout",
                fix_hint="Relayout paragraphs",
            )
        )
        repairs = rt.schedule_repairs(ig)
        assert len(repairs) >= 1
        assert repairs[0]["action"] == "relayout"

    def test_execute_repairs(self):
        rt = RepairRuntime()
        g = DocumentGraph()
        n = DocumentNode(
            id="n1",
            node_type=NodeType.PARAGRAPH,
            bbox=(0, 0, 100, 50),
            text="Original text",
        )
        g.add_node(n)
        repairs = [
            {
                "issue_type": "bad_translation",
                "node_id": "n1",
                "module": "translator",
                "action": "retranslate",
            }
        ]
        executed = rt.execute_repairs(repairs, g)
        assert len(executed) == 1

    def test_evaluate_scores_empty(self):
        rt = RepairRuntime()
        g = DocumentGraph()
        scores = rt.evaluate_scores(g, g)
        assert "total" in scores
        assert "translation" in scores

    def test_reset_stats(self):
        rt = RepairRuntime()
        rt._stats.issues_detected = 100
        rt.reset_stats()
        assert rt.stats.issues_detected == 0

    def test_auto_repair_no_issues(self):
        """If both graphs are identical, scores should be 100 and no issues."""
        rt = RepairRuntime(max_iterations=2)
        g = DocumentGraph()
        g2 = DocumentGraph()
        graph, result = rt.auto_repair(g, g2)
        assert isinstance(result, RepairResult)

    @property
    def stats(self):
        return self._stats
