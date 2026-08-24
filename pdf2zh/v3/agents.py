"""Module: Phase 4 / 阶段十一 Multi-Agent Pipeline.

Implements the roadmap's agentified translation loop:

    Parser Agent → Layout Agent → Translate Agent → Typography Agent → Reviewer Agent

The ``AgentPipeline`` drives the agents in that order and supports a bounded
self-feedback loop: when the Reviewer flags a glossary violation (or an empty
/ non-preserved formula) the Translator re-runs for the flagged nodes in
``strict`` mode until the report is clean or the round budget is exhausted.

All agents accept plain data (a DocumentIR + injected callables), so the
whole pipeline is unit-testable headlessly without an LLM endpoint.

Usage::

    from pdf2zh.v3.agents import AgentPipeline
    from pdf2zh.v3.document_ir import IRBuilder

    def stub_translator(text, node_id, strict=False):
        return "译文" if text else ""

    pipeline = AgentPipeline(translator=stub_translator, glossary={"LLM": "大语言模型"})
    report = pipeline.run(ir)
    print(report.final_translations)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from pdf2zh.v3.typography import AdaptiveTypography, GlyphProbe, TypographyMetrics
from pdf2zh.v3.visual_tree import BoundingBox

logger = logging.getLogger(__name__)

# Semantic roles that must never be machine-translated (KEEP_* families).
_KEEP_SEMANTIC = ("formula", "formula_inline", "table", "figure", "code")


# ── Reports ──────────────────────────────────────────────────────────


@dataclass
class ParserReport:
    """Parser Agent verification result over a DocumentIR."""

    node_count: int = 0
    pages: int = 0
    missing_text: List[str] = field(default_factory=list)
    orphan_pages: List[int] = field(default_factory=list)
    unknown_semantic: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.missing_text
            or self.orphan_pages
            or self.unknown_semantic
            or self.issues
        )

    def to_dict(self) -> dict:
        return {
            "node_count": self.node_count,
            "pages": self.pages,
            "missing_text": self.missing_text,
            "orphan_pages": self.orphan_pages,
            "unknown_semantic": self.unknown_semantic,
            "issues": self.issues,
            "ok": self.ok,
        }


@dataclass
class LayoutPlan:
    """Layout Agent plan: solved boxes + collision report."""

    positions: Dict[str, BoundingBox] = field(default_factory=dict)
    collisions: List[Tuple[str, str]] = field(default_factory=list)
    overlap_rate: float = 0.0
    solved: bool = True
    engine: str = "kiwi"

    def to_dict(self) -> dict:
        return {
            "solved": self.solved,
            "engine": self.engine,
            "overlap_rate": round(self.overlap_rate, 4),
            "collisions": [f"{a}<->{b}" for a, b in self.collisions],
            "positions": {
                k: (round(v.x, 1), round(v.y, 1), round(v.width, 1), round(v.height, 1))
                for k, v in self.positions.items()
            },
        }


@dataclass
class TypographyPlan:
    """Typography Agent plan: per-node adaptive metrics."""

    metrics: Dict[str, TypographyMetrics] = field(default_factory=dict)
    resized: Dict[str, float] = field(default_factory=dict)  # node_id → new height
    auto_fit: Dict[str, float] = field(default_factory=dict)  # node_id → new font size

    def to_dict(self) -> dict:
        return {
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "resized": {k: round(v, 2) for k, v in self.resized.items()},
            "auto_fit": {k: round(v, 2) for k, v in self.auto_fit.items()},
        }


@dataclass
class ReviewOutcome:
    """Reviewer Agent output."""

    issues: List[str] = field(default_factory=list)
    flagged_nodes: List[str] = field(default_factory=list)
    reviewed: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {
            "issues": self.issues,
            "flagged_nodes": self.flagged_nodes,
            "reviewed": self.reviewed,
            "ok": self.ok,
        }


@dataclass
class PipelineReport:
    """End-to-end report from the AgentPipeline."""

    rounds: int = 0
    stages: Dict[str, dict] = field(default_factory=dict)
    final_translations: Dict[str, str] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    converged: bool = False

    def to_dict(self) -> dict:
        return {
            "rounds": self.rounds,
            "stages": self.stages,
            "final_translations": self.final_translations,
            "issues": self.issues,
            "converged": self.converged,
        }


# ── Agents ──────────────────────────────────────────────────────────


def _semantic_name(node: Any) -> str:
    s = getattr(node, "semantic", None)
    return getattr(s, "value", None) or str(s or "")


def _node_bbox(node: Any) -> Optional[BoundingBox]:
    bb = getattr(node, "bbox", None)
    if bb is None:
        return None
    if isinstance(bb, BoundingBox):
        return bb
    if hasattr(bb, "x"):
        return BoundingBox(bb.x, bb.y, bb.width, bb.height)
    try:
        x0, y0, x1, y1 = (float(v) for v in bb)
        return BoundingBox(x0, y0, x1 - x0, y1 - y0)
    except (TypeError, ValueError):
        return None


def _ir_nodes(ir: Any) -> List[Any]:
    """Return IR nodes whether ``nodes`` is a list, property or method."""
    nodes = getattr(ir, "nodes", None)
    if callable(nodes):
        return list(nodes())
    return list(nodes or [])


class ParserAgent:
    """阶段零 / 阶段十一 — structural verification of a DocumentIR."""

    def verify(self, ir: Any) -> ParserReport:
        nodes = _ir_nodes(ir)
        report = ParserReport(node_count=len(nodes))
        pages = {getattr(n, "page_num", None) for n in nodes}
        pages.discard(None)
        report.pages = len(pages)
        page_nodes = {
            getattr(n, "page_num", None)
            for n in nodes
            if _semantic_name(n) == "document"
        }
        for n in nodes:
            name = _semantic_name(n)
            if name in ("document", "section"):
                continue
            text = getattr(n, "text", "") or ""
            if not text.strip():
                report.missing_text.append(getattr(n, "id", "?"))
            if name == "unknown":
                report.unknown_semantic.append(getattr(n, "id", "?"))
            if getattr(n, "page_num", None) not in pages:
                report.orphan_pages.append(getattr(n, "page_num", None))
        return report


class LayoutAgent:
    """阶段六 / 阶段十一 — constraint layout planning for an IR.

    Builds a ConstraintGraph from the IR nodes, adds MUST_BELOW edges for the
    reading order (page order + follows order), solves with the Kiwi engine
    and reports residual collisions.
    """

    def __init__(self, page_width: float = 612.0, page_height: float = 792.0) -> None:
        self.page_width = page_width
        self.page_height = page_height

    def plan(
        self,
        ir: Any,
        order_edges: Optional[List[Tuple[str, str]]] = None,
        engine: str = "auto",
    ) -> LayoutPlan:
        from pdf2zh.v3.constraint_graph import ConstraintGraph, ConstraintRelation

        nodes = _ir_nodes(ir)
        content = [n for n in nodes if _semantic_name(n) not in ("document", "section")]
        cg = ConstraintGraph()
        for n in content:
            bb = _node_bbox(n)
            if bb is None:
                continue
            cg.add_node(
                nid := getattr(n, "id", ""),
                _semantic_name(n),
                bbox=bb,
                page_num=getattr(n, "page_num", 0),
            )
        # reading order: explicit edges first, then page/position order
        seen: set = set()
        for src, tgt in order_edges or []:
            if src in cg._nodes and tgt in cg._nodes and (src, tgt) not in seen:
                seen.add((src, tgt))
                cg.add_edge(
                    src, tgt, ConstraintRelation.MUST_BELOW, priority="hard", gap=4.0
                )
        sorted_nodes = sorted(
            content,
            key=lambda n: (
                getattr(n, "page_num", 0),
                (
                    getattr(n, "bbox", (0, 0, 0, 0))[1]
                    if not hasattr(getattr(n, "bbox", None), "y")
                    else getattr(n.bbox, "y", 0)
                ),
            ),
        )
        for prev, nxt in zip(sorted_nodes, sorted_nodes[1:]):
            if (prev.id, nxt.id) not in seen:
                cg.add_edge(
                    prev.id,
                    nxt.id,
                    ConstraintRelation.MUST_BELOW,
                    priority="soft",
                    gap=4.0,
                )

        from pdf2zh.v3.constraint_graph import ConstraintSolver

        solver = ConstraintSolver(cg, self.page_width, self.page_height)
        solved = solver.solve(engine=engine)
        positions: Dict[str, BoundingBox] = {}
        collisions: List[Tuple[str, str]] = []
        laid = list(cg._nodes.values())
        for n in laid:
            positions[n.id] = n.resolved_bbox or n.bbox
        for i in range(len(laid)):
            for j in range(i + 1, len(laid)):
                a, b = laid[i], laid[j]
                if a.page_num != b.page_num:
                    continue
                ab, bb = positions[a.id], positions[b.id]
                if ab.overlaps(bb):
                    collisions.append((a.id, b.id))
        overlap = len(set(c for c, _ in collisions) | {c for _, c in collisions}) / max(
            1, len(laid)
        )
        return LayoutPlan(
            positions=positions,
            collisions=collisions,
            overlap_rate=overlap,
            solved=solved,
            engine="kiwi" if engine in ("auto", "kiwi") else "greedy",
        )


class TypographyAgent:
    """阶段七 / 阶段十一 — adaptive typography plan for translated text."""

    def __init__(self, container_width: float = 450.0, font_size: float = 12.0) -> None:
        self.typography = AdaptiveTypography(container_width, font_size)

    def plan(
        self,
        ir: Any,
        translations: Dict[str, str],
        source_text: Optional[Dict[str, str]] = None,
        max_lines: Optional[int] = None,
    ) -> TypographyPlan:
        plan = TypographyPlan()
        for n in _ir_nodes(ir):
            nid = getattr(n, "id", "")
            if _semantic_name(n) in ("document", "section"):
                continue
            translated = translations.get(nid)
            if translated is None:
                continue
            src = (source_text or {}).get(nid, None)
            m = self.typography.metrics(translated, source=src)
            plan.metrics[nid] = m
            bb = _node_bbox(n)
            if bb is not None and m.block_height > bb.height:
                plan.resized[nid] = m.block_height
            # auto-fit the font when the translation overflows the container
            fit = self.typography.auto_fit_font_size(translated, max_lines=max_lines)
            if fit < self.typography.font_size - 1e-6:
                plan.auto_fit[nid] = fit
        return plan


class TranslateAgent:
    """阶段三 / 阶段十一 — per-semantic-unit translation with glossary guard."""

    def __init__(
        self, translator: Callable[..., str], glossary: Optional[Dict[str, str]] = None
    ) -> None:
        self.translator = translator
        self.glossary = glossary or {}

    def translate(
        self, ir: Any, strict: bool = False, node_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Return ``{node_id: translated_text}`` for the given IR nodes."""
        result: Dict[str, str] = {}
        wanted = set(node_ids or [])
        for n in _ir_nodes(ir):
            nid = getattr(n, "id", "")
            if wanted and nid not in wanted:
                continue
            name = _semantic_name(n)
            text = getattr(n, "text", "") or ""
            if name in _KEEP_SEMANTIC or name in (
                "citation",
                "reference",
                "footnote",
                "bibliography",
            ):
                result[nid] = text
                continue
            if not text.strip():
                result[nid] = text
                continue
            result[nid] = self.translator(text, nid, strict=strict)
        # strict glossary enforcement: substitute remaining raw terms
        if strict and self.glossary:
            for nid in list(result):
                t = result[nid]
                for src_term, tgt_term in self.glossary.items():
                    if src_term in t and tgt_term not in t:
                        result[nid] = t.replace(src_term, tgt_term)
        return result


class ReviewerAgent:
    """阶段十一 — glossary / emptiness / integrity review of translations."""

    def __init__(
        self,
        glossary: Optional[Dict[str, str]] = None,
        source_text: Optional[Dict[str, str]] = None,
    ) -> None:
        self.glossary = glossary or {}
        self.source_text = source_text or {}

    def review(self, ir: Any, translations: Dict[str, str]) -> ReviewOutcome:
        outcome = ReviewOutcome()
        for n in _ir_nodes(ir):
            nid = getattr(n, "id", "")
            name = _semantic_name(n)
            if name in ("document", "section") or nid not in translations:
                continue
            outcome.reviewed += 1
            t = translations.get(nid, "")
            if not t.strip() and (getattr(n, "text", "") or "").strip():
                outcome.issues.append(f"{nid}: empty translation")
                outcome.flagged_nodes.append(nid)
                continue
            if name in _KEEP_SEMANTIC:
                if t != (getattr(n, "text", "") or ""):
                    outcome.issues.append(f"{nid}: kept role {name} was modified")
                    outcome.flagged_nodes.append(nid)
                continue
            src = self.source_text.get(nid, getattr(n, "text", "") or "")
            for src_term, tgt_term in self.glossary.items():
                if src_term in src and tgt_term not in t:
                    outcome.issues.append(
                        f"{nid}: glossary term '{src_term}' not rendered as "
                        f"'{tgt_term}'"
                    )
                    outcome.flagged_nodes.append(nid)
        # deduplicate flagged nodes
        outcome.flagged_nodes = sorted(set(outcome.flagged_nodes))
        return outcome


class AgentPipeline:
    """阶段十一 end-to-end agent pipeline with bounded self-feedback.

    Stages: Parser.verify → Layout.plan → Translate → Typography.plan →
    Reviewer.review → (re-translate flagged nodes in strict mode) → report.
    """

    def __init__(
        self,
        translator: Callable[..., str],
        glossary: Optional[Dict[str, str]] = None,
        source_text: Optional[Dict[str, str]] = None,
        max_feedback_rounds: int = 2,
        page_width: float = 612.0,
        page_height: float = 792.0,
        container_width: float = 450.0,
    ) -> None:
        self.parser = ParserAgent()
        self.layout = LayoutAgent(page_width, page_height)
        self.translator = TranslateAgent(translator, glossary)
        self.typography = TypographyAgent(container_width)
        self.reviewer = ReviewerAgent(glossary, source_text)
        self.max_feedback_rounds = max_feedback_rounds

    def run(self, ir: Any) -> PipelineReport:
        parser_report = self.parser.verify(ir)
        layout_plan = self.layout.plan(ir)
        translations = self.translator.translate(ir)
        typography_plan = self.typography.plan(ir, translations)
        outcome = self.reviewer.review(ir, translations)

        rounds = 1
        while not outcome.ok and rounds < self.max_feedback_rounds:
            rounds += 1
            translations.update(
                self.translator.translate(
                    ir, strict=True, node_ids=outcome.flagged_nodes
                )
            )
            typography_plan = self.typography.plan(ir, translations)
            outcome = self.reviewer.review(ir, translations)

        return PipelineReport(
            rounds=rounds,
            stages={
                "parser": parser_report.to_dict(),
                "layout": layout_plan.to_dict(),
                "typography": typography_plan.to_dict(),
                "review": outcome.to_dict(),
            },
            final_translations=dict(translations),
            issues=list(outcome.issues),
            converged=outcome.ok,
        )


__all__ = [
    "ParserReport",
    "LayoutPlan",
    "TypographyPlan",
    "ReviewOutcome",
    "PipelineReport",
    "ParserAgent",
    "LayoutAgent",
    "TypographyAgent",
    "TranslateAgent",
    "ReviewerAgent",
    "AgentPipeline",
]
