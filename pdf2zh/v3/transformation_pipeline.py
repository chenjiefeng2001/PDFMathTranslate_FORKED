"""Module: V6.0 Transformation Pipeline — end-to-end pdf2zh-next pipeline.

Composes the whole V6 architecture into one headless-runnable facade:

    Parse -> Normalize -> DocumentGraph -> Plan -> Translate
        -> Quality Review -> Relayout -> Render

Keep external contract (translate a document, get a translated document) while
internally using Document IR + constraint layout + quality gate.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from pdf2zh.v3.graph import (
    DocumentGraph,
    DocumentNode,
    NodeType,
    Edge,
    EdgeType,
    DocumentGraphBuilder,
    GraphBuildConfig,
)
from pdf2zh.v3.planner import PlannerConfig, TranslationPlanner, GlossaryManager
from pdf2zh.v3.translator import (
    LLMProvider,
    LLMResponse,
    TranslationSession,
    Translator,
    TranslationStats,
    MockLLMProvider,
)
from pdf2zh.v3.review_agent import QualityPipeline, ReviewAgent, ReviewResult
from pdf2zh.v3.relayout_engine import RelayoutEngine, RelayoutConfig
from pdf2zh.v3.render_adapter import (
    RenderAdapter,
    RenderBlock,
    HTMLFloatRenderer,
    TextRenderer,
)
from pdf2zh.v3.visual_tree import BoundingBox

logger = logging.getLogger(__name__)

MOCK_PREFIX = "【译】"


@dataclass
class PipelineConfig:
    """End-to-end pipeline tunables."""

    source_lang: str = "auto"
    target_lang: str = "zh-CN"
    reflow: bool = True
    float_images: bool = True
    overlay: bool = True
    formats: List[str] = field(default_factory=lambda: ["html", "text", "pdf"])
    glossary: Dict[str, str] = field(default_factory=dict)
    model: str = "mock"
    line_gap: float = 2.0


@dataclass
class PipelineStats:
    total_nodes: int = 0
    translated: int = 0
    review_errors: int = 0
    quality_score: float = 1.0
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_nodes": self.total_nodes,
            "translated": self.translated,
            "review_errors": self.review_errors,
            "quality_score": self.quality_score,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


class RuleBasedProvider(LLMProvider):
    """Deterministic, dependency-free pseudo-translator.

    Marks text as translated (so the identity check passes) while preserving
    every formula token, number and glossary term verbatim — ideal for
    headless end-to-end tests of the quality gate and the pipeline.
    """

    def __init__(self, target_lang: str = "zh-CN") -> None:
        self.target_lang = target_lang

    def complete(self, messages, **kwargs):
        last = messages[-1]["content"] if messages else ""
        if "Text to translate:" in last:
            text = last.split("Text to translate:", 1)[-1].strip()
        else:
            text = last
        # Strip prompt scaffolding markers if present.
        text = text.replace("```", "").strip()
        return LLMResponse(
            text=f"{MOCK_PREFIX}{text}",
            model=kwargs.get("model", "mock"),
            provider="rule-based",
            finish_reason="stop",
        )


@dataclass
class PipelineOutput:
    """Final result of a full transformation run."""

    graph: Optional[DocumentGraph] = None
    translations: Dict[str, str] = field(default_factory=dict)
    review: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    rendered: Dict[str, bytes] = field(default_factory=dict)
    stats: PipelineStats = field(default_factory=PipelineStats)
    session_summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "translations": dict(self.translations),
            "review": self.review,
            "manifest": self.manifest,
            "formats": {k: len(v) for k, v in self.rendered.items()},
            "stats": self.stats.to_dict(),
            "session": self.session_summary,
        }


class TransformationPipeline:
    """Compose parse -> graph -> plan -> translate -> review -> relayout -> render."""

    def __init__(
        self,
        config: Optional[PipelineConfig] = None,
        provider: Optional[LLMProvider] = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.provider = provider or RuleBasedProvider(self.config.target_lang)
        self._reviewer = ReviewAgent(glossary=self.config.glossary)
        self._quality = QualityPipeline(reviewer=self._reviewer)
        self._relayout = RelayoutEngine(
            RelayoutConfig(
                reflow=self.config.reflow,
                float_images=self.config.float_images,
                overlay=self.config.overlay,
                chunk_line_gap=self.config.line_gap,
            )
        )
        self._render = RenderAdapter(formats=self.config.formats)

    # ── Graph construction ───────────────────────────────────────────

    @staticmethod
    def build_graph_from_blocks(blocks: List[Dict]) -> DocumentGraph:
        """Build a DocumentGraph from [{text, x, y, w, h, page}] dicts."""
        graph = DocumentGraph()
        for i, b in enumerate(blocks):
            nid = b.get("id", f"node_{i}")
            x0, y0, w, h = (
                b.get("x", 0.0),
                b.get("y", 0.0),
                b.get("w", 100.0),
                b.get("h", 14.0),
            )
            node = DocumentNode(
                id=nid,
                node_type=NodeType(b.get("type", "paragraph")),
                bbox=(x0, y0, x0 + w, y0 + h),
                text=b.get("text", ""),
                page_num=b.get("page", 0),
                font_size=b.get("font_size", 12.0),
            )
            graph.add_node(node)
        # Page containers + reading order.
        pages = sorted({n.page_num for n in graph.nodes})
        for pn in pages:
            page_id = f"page_{pn}"
            page_nodes = graph.get_nodes_on_page(pn)
            if not page_nodes:
                continue
            x0 = min(n.x0 for n in page_nodes)
            y0 = min(n.y0 for n in page_nodes)
            x1 = max(n.x1 for n in page_nodes)
            y1 = max(n.y1 for n in page_nodes)
            graph.add_node(
                DocumentNode(
                    id=page_id,
                    node_type=NodeType.PAGE,
                    bbox=(x0, y0, x1, y1),
                    text=f"Page {pn + 1}",
                    page_num=pn,
                )
            )
            for n in page_nodes:
                graph.add_edge(
                    Edge(source_id=page_id, target_id=n.id, edge_type=EdgeType.CONTAINS)
                )
        ordered = sorted(graph.nodes, key=lambda n: (n.page_num, n.y0, n.x0))
        prev = None
        for n in ordered:
            if n.node_type in (NodeType.PAGE, NodeType.DOCUMENT):
                continue
            if prev is not None:
                graph.add_edge(
                    Edge(source_id=prev.id, target_id=n.id, edge_type=EdgeType.FOLLOWS)
                )
            prev = n
        return graph

    def _graph_to_items(self, graph: DocumentGraph) -> Dict[int, List[DocumentNode]]:
        """Group content nodes per page for the relayout engine."""
        pages: Dict[int, List[DocumentNode]] = {}
        for n in graph.nodes:
            if n.node_type in (NodeType.PAGE, NodeType.DOCUMENT):
                continue
            if not n.text.strip():
                continue
            pages.setdefault(n.page_num, []).append(n)
        for items in pages.values():
            items.sort(key=lambda n: (n.y0, n.x0))
        return pages

    # ── Main entry points ────────────────────────────────────────────

    def run_text(
        self, texts: List[str], page_width: float = 612.0, page_height: float = 792.0
    ) -> PipelineOutput:
        """Run the pipeline over a list of plain-text blocks (headless friendly)."""
        blocks = []
        for i, text in enumerate(texts):
            blocks.append(
                {
                    "id": f"n{i}",
                    "text": text,
                    "type": "paragraph",
                    "x": 72.0,
                    "y": 100.0 + i * 20.0,
                    "w": 468.0,
                    "h": 14.0,
                    "page": 0,
                }
            )
        return self.run(blocks, page_width=page_width, page_height=page_height)

    def run(
        self,
        blocks: List[Dict],
        page_width: float = 612.0,
        page_height: float = 792.0,
        provider: Optional[LLMProvider] = None,
    ) -> PipelineOutput:
        """Run the pipeline over a list of content block dicts."""
        started = time.time()
        graph = self.build_graph_from_blocks(blocks)
        session = self._translate(graph, provider)
        translations = dict(session.results)
        review = self._quality.run(
            {
                nid: {"source": n.text, "translated": txt}
                for nid, txt in translations.items()
                if (n := graph.get_node(nid)) is not None
            },
            is_formula_map={
                nid: graph.get_node(nid).node_type
                in (NodeType.FORMULA, NodeType.FORMULA_INLINE)
                for nid in translations
                if graph.get_node(nid) is not None
            },
        )
        final_translations = review["final_translations"]
        manifest = self._relayout.run(
            [
                {"index": pn, "items": items}
                for pn, items in self._graph_to_items(graph).items()
            ],
            page_width=page_width,
            page_height=page_height,
        ).to_dict()
        # Map chunk ids -> node translations for rendering.
        render_translations = {}
        for block in manifest.get("blocks", []):
            chunk_id = block["id"]
            source_ids = block.get("source_ids", [])
            if source_ids:
                nid = source_ids[0]
                if nid in final_translations:
                    render_translations[chunk_id] = final_translations[nid]
        render_blocks = RenderAdapter.build_blocks(manifest, render_translations)
        rendered = {}
        for fmt in self.config.formats:
            try:
                rendered[fmt] = self._render.render(render_blocks, fmt=fmt)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Render failed for %s: %s", fmt, exc)

        elapsed_ms = (time.time() - started) * 1000.0
        stats = PipelineStats(
            total_nodes=sum(
                1
                for n in graph.nodes
                if n.node_type not in (NodeType.PAGE, NodeType.DOCUMENT)
            ),
            translated=len(translations),
            review_errors=review["errors"],
            quality_score=review["quality_score"],
            elapsed_ms=elapsed_ms,
        )
        return PipelineOutput(
            graph=graph,
            translations=final_translations,
            review=review,
            manifest=manifest,
            rendered=rendered,
            stats=stats,
            session_summary=session.summary(),
        )

    def _translate(
        self, graph: DocumentGraph, provider: Optional[LLMProvider] = None
    ) -> TranslationSession:
        planner = TranslationPlanner(
            PlannerConfig(
                source_lang=self.config.source_lang,
                target_lang=self.config.target_lang,
                model=self.config.model,
            )
        )
        glossary_mgr = GlossaryManager()
        for src, tgt in self.config.glossary.items():
            glossary_mgr.add_term(src, tgt)
        planner.glossary = glossary_mgr
        session = TranslationSession(
            graph=graph,
            planner=planner,
            provider=provider or self.provider,
        )
        translator = Translator(session)
        translator.translate_all()
        return session


__all__ = [
    "PipelineConfig",
    "PipelineStats",
    "RuleBasedProvider",
    "PipelineOutput",
    "TransformationPipeline",
]
