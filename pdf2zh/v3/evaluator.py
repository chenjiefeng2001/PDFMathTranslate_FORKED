"""Module 9: Quality Evaluator.
Automated quality assessment for translated DocumentGraphs.

Provides multi-dimensional scoring:
  - Translation Score: Completeness and fluency
  - Semantic Score: Semantic structure preservation
  - Typography Score: Font and spacing quality
  - Layout Score: Spatial integrity (no overlaps)
  - Consistency Score: Glossary term consistency
"""

from __future__ import annotations

import re
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pdf2zh.v3.graph import (
    DocumentGraph,
    DocumentNode,
    EdgeType,
    NodeType,
)

# --- Scoring Weights ---

WEIGHTS: Dict[str, float] = {
    "translation": 0.25,
    "semantic": 0.25,
    "typography": 0.15,
    "layout": 0.20,
    "consistency": 0.15,
}


# --- Diagnostic Report (Phase 2) ---


@dataclass
class DiagnosticRecord:
    page_num: int = 0
    node_id: str = ""
    metric: str = ""
    value: float = 0.0
    threshold: float = 70.0
    message: str = ""
    passed: bool = True


class DiagnosticReport:
    def __init__(self):
        self._records: List[DiagnosticRecord] = []

    def add_record(self, record: DiagnosticRecord) -> None:
        self._records.append(record)

    def add(self, page_num=0, node_id="", metric="",
            value=100.0, threshold=70.0, message="", passed=True):
        self._records.append(DiagnosticRecord(
            page_num=page_num, node_id=node_id,
            metric=metric, value=value, threshold=threshold,
            message=message, passed=passed,
        ))

    @property
    def records(self): return list(self._records)
    @property
    def total(self) -> int: return len(self._records)
    @property
    def passed_count(self) -> int:
        return sum(1 for r in self._records if r.passed)
    @property
    def failed_count(self) -> int: return self.total - self.passed_count
    @property
    def pass_rate(self) -> float:
        return (self.passed_count / max(self.total, 1)) * 100.0
    def to_dict(self) -> dict:
        return {
            "total": self.total, "passed": self.passed_count,
            "failed": self.failed_count,
            "pass_rate": round(self.pass_rate, 1),
            "records": [{"page": r.page_num, "node": r.node_id,
                         "metric": r.metric, "value": r.value,
                         "threshold": r.threshold, "message": r.message,
                         "passed": r.passed} for r in self._records],
        }
    def to_text(self) -> str:
        lines = [
            "=== Diagnostic Report ===",
            f"Total: {self.total} | Passed: {self.passed_count} | Failed: {self.failed_count} | Rate: {self.pass_rate:.1f}%",
            "",
        ]
        for r in self._records:
            s = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{s}] {r.metric} ({r.value:.1f}/{r.threshold:.1f}) page={r.page_num} node={r.node_id} {r.message}")
        return "\n".join(lines)
    def clear(self): self._records.clear()


class EvaluationIssueMapper:
    @staticmethod
    def map_result(result: EvaluationResult) -> "IssueGraph":
        from pdf2zh.v3.evaluator import IssueGraph, Issue, IssueSeverity
        graph = IssueGraph()
        thresholds = {"translation": 80.0, "semantic": 75.0,
                      "typography": 70.0, "layout": 80.0, "consistency": 75.0}
        smap = {"translation": result.translation_score,
                "semantic": result.semantic_score,
                "typography": result.typography_score,
                "layout": result.layout_score,
                "consistency": result.consistency_score}
        smap2 = {"translation": IssueSeverity.MAJOR,
                 "semantic": IssueSeverity.MAJOR,
                 "typography": IssueSeverity.MINOR,
                 "layout": IssueSeverity.CRITICAL,
                 "consistency": IssueSeverity.MAJOR}
        for metric, score in smap.items():
            thresh = thresholds.get(metric, 70.0)
            if score < thresh:
                impact = (thresh - score) * 0.5
                graph.add_issue(Issue(
                    issue_type=metric + "_low_score",
                    severity=smap2.get(metric, IssueSeverity.MINOR),
                    description=f"{metric} score ({score:.1f}) below threshold ({thresh:.1f})",
                    module=metric, score_impact=impact,
                    fix_hint=f"Improve {metric} quality",
                ))
        return graph



# --- Evaluation Result ---


@dataclass
class EvaluationResult:
    """Scores in [0, 100] for a single evaluation run."""
    translation_score: float = 100.0
    semantic_score: float = 100.0
    typography_score: float = 100.0
    layout_score: float = 100.0
    consistency_score: float = 100.0
    total_score: float = 100.0
    details: dict = field(default_factory=dict)
    per_page_scores: Dict[int, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "translation_score": self.translation_score,
            "semantic_score": self.semantic_score,
            "typography_score": self.typography_score,
            "layout_score": self.layout_score,
            "consistency_score": self.consistency_score,
            "total_score": self.total_score,
            "details": self.details,
        }


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def weighted_penalty(base: float, penalties: List[float]) -> float:
    score = base
    for p in penalties:
        score -= p
    return clamp(score)

# --- Translation Evaluator ---


class TranslationEvaluator:
    """Evaluate translation completeness and fluency."""

    @staticmethod
    def evaluate(original: DocumentGraph,
                 translated: DocumentGraph) -> Tuple[float, dict]:
        details: dict = {}
        penalties: List[float] = []

        orig_content = [n for n in original.nodes
                        if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE)]
        trans_content = [n for n in translated.nodes
                         if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE)]
        trans_ids = {n.id for n in trans_content}

        if orig_content:
            missing = sum(1 for n in orig_content if n.id not in trans_ids)
            missing_ratio = missing / len(orig_content)
            penalties.append(missing_ratio * 30)
            details["missing_node_ratio"] = round(missing_ratio, 4)

        if trans_content:
            empty = sum(1 for n in trans_content if not n.text.strip())
            empty_ratio = empty / len(trans_content)
            penalties.append(empty_ratio * 20)
            details["empty_text_ratio"] = round(empty_ratio, 4)

        # Number preservation check (sample first 50 nodes)
        num_checks = 0
        num_missing = 0
        for orig_node in orig_content[:50]:
            trans_node = translated.get_node(orig_node.id)
            if (trans_node and orig_node.text.strip()
                    and trans_node.text.strip()):
                orig_nums = set(re.findall(r"\b\d{2,}\b", orig_node.text))
                key_nums = {n for n in orig_nums if len(n) >= 2}
                if key_nums:
                    trans_nums = set(re.findall(r"\b\d{2,}\b", trans_node.text))
                    num_checks += 1
                    if not key_nums.issubset(trans_nums):
                        num_missing += 1

        if num_checks > 0:
            penalties.append((num_missing / num_checks) * 15)
            details["num_missing_ratio"] = round(num_missing / num_checks, 4)

        score = weighted_penalty(100.0, penalties)
        details["penalties"] = [round(p, 2) for p in penalties]
        return score, details


# --- Semantic Evaluator ---


class SemanticEvaluator:
    """Evaluate semantic structure preservation."""

    @staticmethod
    def evaluate(original: DocumentGraph,
                 translated: DocumentGraph) -> Tuple[float, dict]:
        details: dict = {}
        penalties: List[float] = []

        sem_edge_types = {EdgeType.CAPTION_OF, EdgeType.FOOTNOTE_OF,
                          EdgeType.SAME_SECTION, EdgeType.REFERENCE}
        orig_sem = [e for e in original.edges
                    if e.edge_type in sem_edge_types]
        trans_sem_keys = {
            f"{e.source_id}->{e.target_id}|{e.edge_type.value}"
            for e in translated.edges if e.edge_type in sem_edge_types
        }
        if orig_sem:
            preserved = sum(
                1 for e in orig_sem
                if f"{e.source_id}->{e.target_id}|{e.edge_type.value}"
                in trans_sem_keys
            )
            ratio = preserved / len(orig_sem)
            penalties.append((1 - ratio) * 15)
            details["edge_preservation"] = round(ratio, 4)

        orig_hids = {n.id for n in original.nodes
                     if n.node_type == NodeType.HEADING}
        if orig_hids:
            kept = sum(1 for hid in orig_hids
                       if translated.get_node(hid) is not None)
            ratio = kept / len(orig_hids)
            penalties.append((1 - ratio) * 10)
            details["heading_preservation"] = round(ratio, 4)

        score = weighted_penalty(100.0, penalties)
        details["penalties"] = [round(p, 2) for p in penalties]
        return score, details


# --- Typography Evaluator ---


class TypographyEvaluator:
    """Evaluate typography quality."""

    @staticmethod
    def evaluate(translated: DocumentGraph) -> Tuple[float, dict]:
        details: dict = {}
        penalties: List[float] = []

        type_sizes: Dict[NodeType, List[float]] = {}
        for n in translated.nodes:
            if n.font_size > 0:
                type_sizes.setdefault(n.node_type, []).append(n.font_size)

        for nt, sizes in type_sizes.items():
            if len(sizes) >= 3:
                avg = sum(sizes) / len(sizes)
                var_ = sum((s - avg) ** 2 for s in sizes) / len(sizes)
                if var_ > 4.0:
                    penalties.append(min(var_ * 1.5, 10))
                    details.setdefault("font_variance", []).append({
                        "type": nt.value, "variance": round(var_, 2),
                    })
                    break

        pages: Dict[int, List[DocumentNode]] = {}
        for n in translated.nodes:
            if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE):
                pages.setdefault(n.page_num, []).append(n)
        for pnum, nodes in pages.items():
            if len(nodes) >= 5:
                heights = [n.height for n in nodes if n.height > 0]
                if heights:
                    avg_h = sum(heights) / len(heights)
                    tiny = sum(1 for h in heights if h < avg_h * 0.3)
                    if tiny > len(heights) * 0.5:
                        penalties.append(5.0)
                        details["tiny_ratio"] = round(tiny / len(heights), 3)
                        break

        score = weighted_penalty(100.0, penalties)
        details["penalties"] = [round(p, 2) for p in penalties]
        return score, details


# --- Layout Evaluator ---


class LayoutEvaluator:
    """Evaluate spatial layout quality."""

    @staticmethod
    def evaluate(translated: DocumentGraph) -> Tuple[float, dict]:
        details: dict = {}
        penalties: List[float] = []

        page_nodes: Dict[int, List[DocumentNode]] = {}
        for n in translated.nodes:
            if n.node_type in (NodeType.DOCUMENT, NodeType.PAGE):
                continue
            page_nodes.setdefault(n.page_num, []).append(n)

        total_overlaps = 0
        for nodes in page_nodes.values():
            sorted_n = sorted(nodes, key=lambda x: x.y0)
            for i in range(len(sorted_n)):
                for j in range(i + 1, len(sorted_n)):
                    a, b = sorted_n[i], sorted_n[j]
                    if a.y1 > b.y0:
                        h_overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
                        if h_overlap > 0 and h_overlap > min(a.width, b.width) * 0.3:
                            total_overlaps += 1

        if total_overlaps > 0:
            penalties.append(min(total_overlaps * 5, 25))
            details["overlap_count"] = total_overlaps

        gaps: List[float] = []
        for nodes in page_nodes.values():
            sorted_n = sorted(nodes, key=lambda x: x.y0)
            for i in range(len(sorted_n) - 1):
                gap = sorted_n[i + 1].y0 - sorted_n[i].y1
                if 0 < gap < 50:
                    gaps.append(gap)
        if gaps:
            mean_gap = sum(gaps) / len(gaps)
            var_gap = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
            if var_gap > 100:
                penalties.append(min(var_gap / 50, 10))
                details["gap_variance"] = round(var_gap, 2)

        score = weighted_penalty(100.0, penalties)
        details["penalties"] = [round(p, 2) for p in penalties]
        return score, details


# --- Consistency Evaluator ---


class ConsistencyEvaluator:
    """Evaluate term/glossary consistency."""

    @staticmethod
    def evaluate(translated: DocumentGraph,
                 glossary: Optional[Dict[str, str]] = None) -> Tuple[float, dict]:
        details: dict = {}
        penalties: List[float] = []

        if not glossary:
            return 100.0, {"note": "no glossary provided"}

        text_nodes = [
            n for n in translated.nodes
            if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE,
                                   NodeType.FIGURE, NodeType.TABLE)
            and n.text.strip()
        ]

        violations = 0
        for src, expected in glossary.items():
            if not src.strip():
                continue
            for node in text_nodes:
                if src.lower() in node.text.lower():
                    if expected.lower() not in node.text.lower():
                        words = src.split()
                        if len(words) == 1:
                            violations += 1  # single-word term not translated
                        else:
                            all_present = all(w.lower() in node.text.lower()
                                              for w in words)
                            if all_present:
                                violations += 1

        if violations > 0:
            penalties.append(min(violations * 10, 30))
            details["term_violations"] = violations

        empty_ids = [n.id for n in text_nodes if not n.text.strip()]
        if empty_ids:
            penalties.append(min(len(empty_ids) * 2, 10))
            details["empty_node_count"] = len(empty_ids)

        score = weighted_penalty(100.0, penalties)
        details["penalties"] = [round(p, 2) for p in penalties]
        return score, details


# --- Main Quality Evaluator ---


@dataclass
class EvaluatorConfig:
    """Configuration for the Quality Evaluator."""
    enable_translation: bool = True
    enable_semantic: bool = True
    enable_typography: bool = True
    enable_layout: bool = True
    enable_consistency: bool = True
    weights: Dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    glossary: Optional[Dict[str, str]] = None
    per_page: bool = False


class QualityEvaluator:
    """Main quality evaluator: runs sub-evaluators and produces weighted total."""

    def __init__(self, config: Optional[EvaluatorConfig] = None):
        self.config = config or EvaluatorConfig()

    def evaluate(self, original: DocumentGraph,
                 translated: DocumentGraph) -> EvaluationResult:
        result = EvaluationResult()
        scores: Dict[str, float] = {}
        details: dict = {}

        diagnostic = DiagnosticReport()
        if self.config.enable_translation:
            sc, det = TranslationEvaluator.evaluate(original, translated)
            scores["translation_score"] = sc
            details["translation"] = det
        else:
            scores["translation_score"] = 100.0

        if self.config.enable_semantic:
            sc, det = SemanticEvaluator.evaluate(original, translated)
            scores["semantic_score"] = sc
            details["semantic"] = det
        else:
            scores["semantic_score"] = 100.0

        if self.config.enable_typography:
            sc, det = TypographyEvaluator.evaluate(translated)
            scores["typography_score"] = sc
            details["typography"] = det
        else:
            scores["typography_score"] = 100.0

        if self.config.enable_layout:
            sc, det = LayoutEvaluator.evaluate(translated)
            scores["layout_score"] = sc
            details["layout"] = det
        else:
            scores["layout_score"] = 100.0

        if self.config.enable_consistency:
            sc, det = ConsistencyEvaluator.evaluate(translated, self.config.glossary)
            scores["consistency_score"] = sc
            details["consistency"] = det
        else:
            scores["consistency_score"] = 100.0

        w = self.config.weights
        total = (
            scores["translation_score"] * w.get("translation", 0.25)
            + scores["semantic_score"] * w.get("semantic", 0.25)
            + scores["typography_score"] * w.get("typography", 0.15)
            + scores["layout_score"] * w.get("layout", 0.20)
            + scores["consistency_score"] * w.get("consistency", 0.15)
        )

        result.translation_score = round(scores["translation_score"], 1)
        result.semantic_score = round(scores["semantic_score"], 1)
        result.typography_score = round(scores["typography_score"], 1)
        result.layout_score = round(scores["layout_score"], 1)
        result.consistency_score = round(scores["consistency_score"], 1)
        result.total_score = round(clamp(total), 1)
        result.details = details
        result._diagnostic = diagnostic
        self._diagnostic = diagnostic
        return result

    @property
    def diagnostic(self):
        return getattr(self, "_diagnostic", DiagnosticReport())

    def generate_diagnostic(self, original, translated):
        self.evaluate(original, translated)
        return self.diagnostic



# ── Issue Graph (P2) ──────────────────────────────────────────────────────

class IssueSeverity(Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"

@dataclass
class Issue:
    """A quality issue detected during evaluation."""
    issue_type: str
    severity: IssueSeverity
    description: str
    node_id: str = ""
    module: str = ""
    fix_hint: str = ""
    score_impact: float = 0.0
    details: dict = field(default_factory=dict)

class IssueGraph:
    """Collection of quality issues organized by module and severity."""
    def __init__(self):
        self._issues = {}
    def add_issue(self, issue):
        module = issue.module or "general"
        if module not in self._issues:
            self._issues[module] = []
        self._issues[module].append(issue)
    def add_issues(self, issues):
        for i in issues: self.add_issue(i)
    def get_by_module(self, module):
        return list(self._issues.get(module, []))
    def get_by_severity(self, severity):
        return [i for il in self._issues.values() for i in il if i.severity == severity]
    def get_critical(self): return self.get_by_severity(IssueSeverity.CRITICAL)
    def get_major(self): return self.get_by_severity(IssueSeverity.MAJOR)
    @property
    def total(self): return sum(len(v) for v in self._issues.values())
    @property
    def modules(self): return list(self._issues.keys())
    @property
    def critical_count(self): return len(self.get_critical())
    @property
    def major_count(self): return len(self.get_major())
    def clear(self): self._issues.clear()
    def summary(self):
        return {"total": self.total, "critical": self.critical_count, "major": self.major_count,
                "by_module": {m: len(v) for m, v in self._issues.items()}}

class RepairScheduler:
    """Schedules repair tasks based on issues found."""
    def __init__(self):
        self._repairs = []
    def schedule(self, issue):
        repair = {"issue_type": issue.issue_type, "node_id": issue.node_id, "module": issue.module,
                  "action": {"overlap": "relayout", "bad_translation": "retranslate",
                             "missing_node": "retranslate", "empty_text": "retranslate",
                             "term_inconsistency": "retranslate", "font_mismatch": "reformat",
                             "overflow": "relayout"}.get(issue.issue_type, "reinspect"),
                  "priority": {"critical": 1, "major": 2, "minor": 3, "info": 4}.get(issue.severity.value, 5)}
        self._repairs.append(repair)
        return repair
    def schedule_all(self, issues):
        for m in issues.modules:
            for i in issues.get_by_module(m): self.schedule(i)
        return self.list_repairs()
    def list_repairs(self): return list(self._repairs)
    def clear(self): self._repairs.clear()


__all__ = [
    "EvaluationResult",
    "EvaluatorConfig",
    "QualityEvaluator",
    "TranslationEvaluator",
    "SemanticEvaluator",
    "TypographyEvaluator",
    "LayoutEvaluator",
    "ConsistencyEvaluator",
    "WEIGHTS",
    "Issue", "IssueSeverity", "IssueGraph", "RepairScheduler",
]

