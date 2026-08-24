"""Module: Repair Runtime — Self-healing auto-repair loop (P2).

Completes the self-healing Runtime: IssueGraph → RepairScheduler → execute
→ re-evaluate → converge.  Integrates with the V3 Scheduler for async repair.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pdf2zh.v3.graph import DocumentGraph
from pdf2zh.v3.evaluator import (
    QualityEvaluator,
    EvaluatorConfig,
    EvaluationResult,
    Issue,
    IssueSeverity,
    IssueGraph,
    RepairScheduler,
    EvaluationIssueMapper,
)

logger = logging.getLogger(__name__)


# ── Repair Statistics ────────────────────────────────────────────────────


@dataclass
class RepairStats:
    """Statistics for a single repair run."""

    issues_detected: int = 0
    repairs_scheduled: int = 0
    repairs_executed: int = 0
    repairs_failed: int = 0
    iterations: int = 0
    converged: bool = False
    scores_before: Dict[str, float] = field(default_factory=dict)
    scores_after: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "issues_detected": self.issues_detected,
            "repairs_scheduled": self.repairs_scheduled,
            "repairs_executed": self.repairs_executed,
            "repairs_failed": self.repairs_failed,
            "iterations": self.iterations,
            "converged": self.converged,
            "scores_before": self.scores_before,
            "scores_after": self.scores_after,
        }


@dataclass
class RepairResult:
    """Result of a full repair loop."""

    success: bool
    stats: RepairStats
    final_issues: IssueGraph
    final_evaluation: Optional[EvaluationResult] = None
    per_iteration: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        s = self.stats
        conv = "converged" if s.converged else "did not converge"
        parts = [
            f"Repair {conv}",
            f"  Iterations: {s.iterations}",
            f"  Issues: {s.issues_detected} -> repairs: {s.repairs_executed} (failed: {s.repairs_failed})",
        ]
        if s.scores_before:
            before_total = s.scores_before.get("total", 0)
            after_total = (
                s.scores_after.get("total", 0) if s.scores_after else before_total
            )
            parts.append(f"  Score: {before_total:.1f} -> {after_total:.1f}")
        return "\n".join(parts)


# ── Repair Runtime ────────────────────────────────────────────────────────


class RepairRuntime:
    """Self-healing runtime: detect -> schedule -> execute -> re-evaluate.

    Parameters
    ----------
    evaluator : QualityEvaluator, optional
        Evaluator instance. Built with default config if omitted.
    scheduler : RepairScheduler, optional
        Repair scheduler. Created fresh if omitted.
    max_iterations : int
        Maximum repair iterations (default 3).
    improve_threshold : float
        Minimum score improvement to continue iterating (default 0.5).
    """

    def __init__(
        self,
        evaluator: Optional[QualityEvaluator] = None,
        scheduler: Optional[RepairScheduler] = None,
        max_iterations: int = 3,
        improve_threshold: float = 0.5,
    ):
        self.evaluator = evaluator or QualityEvaluator(EvaluatorConfig())
        self.repair_scheduler = scheduler or RepairScheduler()
        self.max_iterations = max_iterations
        self.improve_threshold = improve_threshold
        self._stats = RepairStats()

    def detect_issues(
        self,
        graph: DocumentGraph,
        original_graph: Optional[DocumentGraph] = None,
    ) -> IssueGraph:
        """Run evaluation and map results to an IssueGraph."""
        orig = original_graph or graph
        result = self.evaluator.evaluate(graph, orig)
        issues = EvaluationIssueMapper.map_result(result)
        self._stats.issues_detected = issues.total
        return issues

    def schedule_repairs(self, issues: IssueGraph) -> List[Dict[str, Any]]:
        """Schedule repairs from an IssueGraph."""
        self.repair_scheduler.clear()
        repairs = self.repair_scheduler.schedule_all(issues)
        self._stats.repairs_scheduled = len(repairs)
        return repairs

    def execute_repairs(
        self,
        repairs: List[Dict[str, Any]],
        graph: DocumentGraph,
    ) -> List[Dict[str, Any]]:
        """Execute scheduled repairs on a graph."""
        self.repair_scheduler.clear()
        self.repair_scheduler._repairs = list(repairs)
        executed = self.repair_scheduler.execute_all(graph)
        self._stats.repairs_executed += len(executed)
        return executed

    def evaluate_scores(
        self,
        graph: DocumentGraph,
        original_graph: Optional[DocumentGraph] = None,
    ) -> Dict[str, float]:
        """Run full evaluation and return score dict."""
        orig = original_graph or graph
        result = self.evaluator.evaluate(graph, orig)
        return {
            "total": result.total_score,
            "translation": result.translation_score,
            "semantic": result.semantic_score,
            "typography": result.typography_score,
            "layout": result.layout_score,
            "consistency": result.consistency_score,
        }

    def repair_loop(
        self,
        graph: DocumentGraph,
        original_graph: Optional[DocumentGraph] = None,
        max_iterations: Optional[int] = None,
    ) -> RepairResult:
        """Run the full self-healing loop.

        Detects issues -> schedules -> executes -> re-evaluates.
        Loops until convergence or max_iterations is reached.
        """
        orig = original_graph or graph
        max_it = max_iterations if max_iterations is not None else self.max_iterations
        self._stats = RepairStats()
        self._stats.scores_before = self.evaluate_scores(graph, orig)

        per_iteration: List[Dict[str, Any]] = []

        for iteration in range(1, max_it + 1):
            logger.info("Repair iteration %d/%d", iteration, max_it)

            issues = self.detect_issues(graph, orig)
            self._stats.issues_detected += issues.total

            repairs = self.schedule_repairs(issues)
            if not repairs:
                logger.info("No repairs needed; converged at iteration %d", iteration)
                self._stats.converged = True
                self._stats.iterations = iteration - 1
                break

            executed = self.execute_repairs(repairs, graph)
            self._stats.repairs_executed += len(executed)
            self._stats.repairs_failed += max(0, len(repairs) - len(executed))

            scores_after = self.evaluate_scores(graph, orig)
            per_iteration.append(
                {
                    "iteration": iteration,
                    "repairs_executed": len(executed),
                    "repairs_failed": max(0, len(repairs) - len(executed)),
                    "scores": scores_after,
                }
            )

            if iteration >= max_it:
                self._stats.converged = False
                self._stats.iterations = iteration
            elif iteration < max_it and issues.total == 0:
                self._stats.converged = True
                self._stats.iterations = iteration
                break

        self._stats.scores_after = self.evaluate_scores(graph, orig)
        if self._stats.iterations == 0:
            self._stats.iterations = 1

        final_issues = self.detect_issues(graph, orig)
        final_eval = self.evaluator.evaluate(graph, orig)
        success = self._stats.converged or self._stats.repairs_failed == 0
        return RepairResult(
            success=success,
            stats=self._stats,
            final_issues=final_issues,
            final_evaluation=final_eval,
            per_iteration=per_iteration,
        )

    def auto_repair(
        self,
        graph: DocumentGraph,
        original: Optional[DocumentGraph] = None,
    ):
        """Convenience: run repair_loop and return (graph, result)."""
        result = self.repair_loop(graph, original)
        return graph, result

    @property
    def stats(self) -> RepairStats:
        return self._stats

    def reset_stats(self) -> None:
        self._stats = RepairStats()


__all__ = [
    "RepairStats",
    "RepairResult",
    "RepairRuntime",
]
