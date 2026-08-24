"""Module: V7.1 Operator Runtime — Runtime 去 Pipeline 化.

Iteration feedback: the V6 TransformationPipeline is still a *black box*
(``execute() → TransformationPipeline.run()``). A Document Intelligence
Runtime should own its execution: the pipeline disappears and the runtime
*is* the pipeline. The execution model becomes::

    Runtime
      └── OperatorGraph            (declarative operator DAG)
            └── Operator           (one composable stage)

Each operator is a pure stage over a shared ``OperatorContext``:

    ParseOperator     blocks      → DocumentGraph
    AnalyzeOperator   graph       → entity / concept / citation knowledge
    PlanOperator      config      → glossary + translation plan
    TranslateOperator graph+plan  → translations
    ReviewOperator    translations→ reviewed final translations
    LayoutOperator    graph       → relayout manifest
    RenderOperator    manifest    → rendered outputs (pdf / html / svg / ...)

Operators are registered in an ``OperatorRegistry`` and composed into an
``OperatorGraph`` whose topological order is derived from declared
dependencies — the same "Runtime → TaskGraph → Scheduler → Executor →
Operator" shape used by Airflow / Ray / Dagster / LLVM PassManager.

Usage::

    from pdf2zh.v3.operators import OperatorGraph, ParseOperator, \
        TranslateOperator, OperatorContext

    graph = OperatorGraph()
    graph.add(ParseOperator())
    graph.add(TranslateOperator(), depends_on=["parse"])
    ctx = graph.run(OperatorContext(document=blocks, provider=provider))
"""

from __future__ import annotations

import copy
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def _as_jsonable(obj: Any, depth: int = 0) -> Any:
    """Best-effort conversion of arbitrary state into JSON-safe values."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if depth > 8:
        return repr(obj)[:200]
    if isinstance(obj, dict):
        return {str(k): _as_jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_as_jsonable(v, depth + 1) for v in obj]
    for attr in ("to_dict", "to_json"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return _as_jsonable(method(), depth + 1)
            except Exception:
                pass
    if hasattr(obj, "value") and not callable(getattr(obj, "value")):
        return obj.value
    if hasattr(obj, "__dict__"):
        return _as_jsonable(vars(obj), depth + 1)
    return str(obj)


@dataclass
class OperatorContext:
    """The shared, mutable execution context flowing through an operator DAG.

    Mirrors the state of one document session so operators stay stateless
    and composable. ``snapshot()`` produces the JSON-safe state consumed by
    the V7.2 RuntimeSnapshot (state snapshot / rollback).
    """

    session_id: str = ""
    document: Any = None  # input blocks / DocumentGraph
    provider: Any = None  # LLM provider
    config: Any = None  # PipelineConfig
    page_width: float = 612.0
    page_height: float = 792.0

    document_graph: Any = None  # DocumentGraph
    translations: Dict[str, str] = field(default_factory=dict)
    outputs: Dict[str, str] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    graphs: Dict[str, Any] = field(default_factory=dict)  # knowledge graphs
    extra: Dict[str, Any] = field(default_factory=dict)  # stage-local state

    # ── Graph registry ────────────────────────────────────────────────

    def register_graph(self, kind: str, graph: Any) -> "OperatorContext":
        self.graphs[kind] = graph
        return self

    def get_graph(self, kind: str) -> Optional[Any]:
        return self.graphs.get(kind)

    # ── State capture ─────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "translations": dict(self.translations),
            "outputs": {k: _as_jsonable(v) for k, v in self.outputs.items()},
            "metrics": _as_jsonable(self.metrics),
            "graphs": {k: _as_jsonable(v) for k, v in self.graphs.items()},
            "extra": _as_jsonable(self.extra),
        }

    def clone(self) -> "OperatorContext":
        return copy.deepcopy(self)


class Operator:
    """Base class for a composable execution stage."""

    name: str = "operator"
    version: str = "1.0"
    inputs: Tuple = ()
    outputs: Tuple = ()

    def validate(self, ctx: OperatorContext) -> None:
        """Optional pre-flight checks; raise before execution if invalid."""

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        raise NotImplementedError(f"Operator '{self.name}' must implement execute()")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name} v{self.version}>"


# ═══════════════════════════════════════════════════════════════════
# Concrete operators — one stage each
# ═══════════════════════════════════════════════════════════════════


class ParseOperator(Operator):
    """blocks / DocumentGraph → DocumentGraph (+ reading-order edges)."""

    name = "parse"

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        from pdf2zh.v3.graph import DocumentGraph
        from pdf2zh.v3.transformation_pipeline import TransformationPipeline

        doc = ctx.document
        if isinstance(doc, DocumentGraph):
            graph = doc
        elif isinstance(doc, list):
            graph = TransformationPipeline.build_graph_from_blocks(doc)
        else:
            raise TypeError(
                "Unsupported document: expected a block list or DocumentGraph"
            )
        ctx.document_graph = graph
        ctx.register_graph("document", graph)
        ctx.metrics["nodes"] = len(graph.nodes)
        return ctx


class AnalyzeOperator(Operator):
    """DocumentGraph → entity / concept / citation knowledge graphs."""

    name = "analyze"

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        from pdf2zh.v3.document_intelligence import DocumentIntelligence

        graph = ctx.document_graph
        if graph is None:
            raise ValueError("AnalyzeOperator requires a document graph")
        di = DocumentIntelligence(graph)
        di.analyze()
        ctx.extra["analysis"] = {
            "entity": di.get_entity_context(),
            "concept": di.get_concept_context(),
            "citation": di.get_citation_context(),
            "summary": di.summary(),
        }
        ctx.metrics["entities"] = di.summary().get("entities", 0)
        return ctx


class PlanOperator(Operator):
    """Config + document graph → glossary and translation plan."""

    name = "plan"

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        from pdf2zh.v3.planner import GlossaryManager, PlannerConfig, TranslationPlanner

        config = ctx.config
        planner = TranslationPlanner(
            PlannerConfig(
                source_lang=getattr(config, "source_lang", "en"),
                target_lang=getattr(config, "target_lang", "zh-CN"),
                model=getattr(config, "model", ""),
            )
        )
        glossary = GlossaryManager()
        for src, tgt in getattr(config, "glossary", {}).items():
            glossary.add_term(src, tgt)
        planner.glossary = glossary
        ctx.extra["planner"] = planner
        terms = 0
        if hasattr(glossary, "terms"):
            terms = len(glossary.terms)
        elif hasattr(glossary, "entries"):
            terms = len(glossary.entries)
        ctx.metrics["glossary_terms"] = terms
        return ctx


class TranslateOperator(Operator):
    """DocumentGraph + planner → translations (per semantic unit)."""

    name = "translate"

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        from pdf2zh.v3.planner import GlossaryManager, PlannerConfig, TranslationPlanner
        from pdf2zh.v3.transformation_pipeline import RuleBasedProvider
        from pdf2zh.v3.translator import TranslationSession, Translator

        graph = ctx.document_graph
        if graph is None:
            raise ValueError("TranslateOperator requires a document graph")
        planner = ctx.extra.get("planner")
        if planner is None:
            config = ctx.config
            planner = TranslationPlanner(
                PlannerConfig(
                    source_lang=getattr(config, "source_lang", "en"),
                    target_lang=getattr(config, "target_lang", "zh-CN"),
                    model=getattr(config, "model", ""),
                )
            )
            glossary = GlossaryManager()
            for src, tgt in getattr(config, "glossary", {}).items():
                glossary.add_term(src, tgt)
            planner.glossary = glossary
        provider = ctx.provider
        if provider is None:
            provider = RuleBasedProvider(getattr(ctx.config, "target_lang", "zh-CN"))
        session = TranslationSession(graph=graph, planner=planner, provider=provider)
        translator = Translator(session)
        incremental_ids = set(ctx.extra.get("incremental_ids") or ())
        if incremental_ids:
            # Incremental: re-translate only the changed nodes and keep the
            # previously translated nodes from the session state.
            for node in graph.nodes:
                if node.id in incremental_ids:
                    translator.translate_node(node.id, force=True)
            existing = dict(ctx.translations)
            existing.update(
                {nid: text for nid, text in session.results.items() if text}
            )
            ctx.translations = existing
        else:
            translator.translate_all()
            ctx.translations = dict(session.results)
        ctx.metrics["translated"] = len(ctx.translations)
        ctx.extra["session_summary"] = session.summary()
        return ctx


class ReviewOperator(Operator):
    """translations → reviewed final translations (formula / glossary / QA)."""

    name = "review"

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        from pdf2zh.v3.graph import NodeType
        from pdf2zh.v3.review_agent import QualityPipeline, ReviewAgent

        graph = ctx.document_graph
        reviewer = ReviewAgent(glossary=getattr(ctx.config, "glossary", {}))
        quality = QualityPipeline(reviewer=reviewer)
        is_formula_map: Dict[str, bool] = {}
        source_map: Dict[str, str] = {}
        for nid, txt in ctx.translations.items():
            node = graph.get_node(nid) if graph is not None else None
            if node is None:
                continue
            source_map[nid] = node.text
            is_formula_map[nid] = node.node_type in (
                NodeType.FORMULA,
                NodeType.FORMULA_INLINE,
            )
        review = quality.run(
            {
                nid: {"source": source_map.get(nid, ""), "translated": txt}
                for nid, txt in ctx.translations.items()
                if nid in source_map
            },
            is_formula_map=is_formula_map,
        )
        ctx.translations = dict(review.get("final_translations", ctx.translations))
        ctx.extra["review"] = _as_jsonable(review)
        ctx.metrics["quality_score"] = review.get("quality_score", 1.0)
        ctx.metrics["review_errors"] = review.get("errors", 0)
        return ctx


class LayoutOperator(Operator):
    """DocumentGraph + translations → relayout manifest (constraint solve)."""

    name = "layout"

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        from pdf2zh.v3.relayout_engine import RelayoutConfig, RelayoutEngine

        graph = ctx.document_graph
        if graph is None:
            raise ValueError("LayoutOperator requires a document graph")
        config = ctx.config
        relayout = RelayoutEngine(
            RelayoutConfig(
                reflow=getattr(config, "reflow", True),
                float_images=getattr(config, "float_images", False),
                overlay=getattr(config, "overlay", False),
                chunk_line_gap=getattr(config, "line_gap", 0.0),
            )
        )
        pages: Dict[int, List[Any]] = {}
        for n in graph.nodes:
            node_type = getattr(n, "node_type", None)
            node_type_value = (
                node_type.value if hasattr(node_type, "value") else node_type
            )
            if node_type_value in ("page", "document"):
                continue
            if not getattr(n, "text", "").strip():
                continue
            pages.setdefault(getattr(n, "page_num", 0), []).append(n)
        for items in pages.values():
            items.sort(key=lambda n: (n.y0, n.x0))
        manifest = relayout.run(
            [{"index": pn, "items": items} for pn, items in pages.items()],
            page_width=ctx.page_width,
            page_height=ctx.page_height,
        ).to_dict()
        ctx.extra["manifest"] = manifest
        ctx.metrics["layout_blocks"] = len(manifest.get("blocks", []))
        return ctx


class RenderOperator(Operator):
    """manifest + translations → rendered outputs (pdf / html / svg / ...)."""

    name = "render"

    def execute(self, ctx: OperatorContext) -> OperatorContext:
        from pdf2zh.v3.render_adapter import RenderAdapter

        manifest = ctx.extra.get("manifest")
        if manifest is None:
            raise ValueError("RenderOperator requires a layout manifest")
        render_translations: Dict[str, str] = {}
        for block in manifest.get("blocks", []):
            chunk_id = block["id"]
            source_ids = block.get("source_ids", [])
            if source_ids and source_ids[0] in ctx.translations:
                render_translations[chunk_id] = ctx.translations[source_ids[0]]
        render_blocks = RenderAdapter.build_blocks(manifest, render_translations)
        formats = tuple(getattr(ctx.config, "formats", ("html",)))
        adapter = RenderAdapter(formats=list(formats))
        rendered: Dict[str, str] = {}
        for fmt in formats:
            try:
                rendered[fmt] = adapter.render(render_blocks, fmt=fmt)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Render failed for %s: %s", fmt, exc)
        ctx.outputs = rendered
        ctx.metrics["formats"] = list(rendered.keys())
        return ctx


# ═══════════════════════════════════════════════════════════════════
# OperatorRegistry + OperatorGraph
# ═══════════════════════════════════════════════════════════════════

_BUILTIN_OPERATORS: List[Operator] = [
    ParseOperator(),
    AnalyzeOperator(),
    PlanOperator(),
    TranslateOperator(),
    ReviewOperator(),
    LayoutOperator(),
    RenderOperator(),
]


class OperatorRegistry:
    """Registry of operators keyed by their ``name``."""

    def __init__(self, builtin: bool = True) -> None:
        self._ops: Dict[str, Operator] = {}
        if builtin:
            for op in _BUILTIN_OPERATORS:
                self.register(op)

    def register(self, op: Operator) -> "OperatorRegistry":
        if op.name in self._ops:
            raise ValueError(f"Operator '{op.name}' already registered")
        self._ops[op.name] = op
        return self

    def get(self, name: str) -> Operator:
        if name not in self._ops:
            raise KeyError(f"Unknown operator '{name}'")
        return self._ops[name]

    def has(self, name: str) -> bool:
        return name in self._ops

    @property
    def names(self) -> List[str]:
        return list(self._ops.keys())

    def all(self) -> List[Operator]:
        return list(self._ops.values())

    @property
    def count(self) -> int:
        return len(self._ops)

    def unregister(self, name: str) -> bool:
        return self._ops.pop(name, None) is not None


class OperatorGraph:
    """A declarative DAG of operators executed in dependency order.

    The runtime schedules operators from this graph — the pipeline is gone,
    the graph *is* the execution. ``run()`` produces the final
    OperatorContext (translations / outputs / metrics) and records a
    per-operator trace.
    """

    def __init__(self) -> None:
        self._ops: Dict[str, Operator] = {}
        self._deps: Dict[str, Set[str]] = {}
        self._trace: List[Dict[str, Any]] = []

    def add(
        self, op: Operator, depends_on: Optional[Iterable[str]] = None
    ) -> "OperatorGraph":
        if op.name in self._ops:
            raise ValueError(f"Operator '{op.name}' already in graph")
        self._ops[op.name] = op
        self._deps[op.name] = set(depends_on or ())
        for dep in self._deps[op.name]:
            if dep not in self._ops:
                raise ValueError(f"Operator '{op.name}' depends on unknown '{dep}'")
        return self

    def has(self, name: str) -> bool:
        return name in self._ops

    def get(self, name: str) -> Operator:
        return self._ops[name]

    def order(self, filter_names: Optional[Iterable[str]] = None) -> List[str]:
        """Topological order of the operator DAG (Kahn's algorithm)."""
        names = set(self._ops.keys())
        if filter_names is not None:
            wanted = set(filter_names)
            extra = wanted - names
            if extra:
                raise KeyError(f"Unknown operators: {sorted(extra)}")
            names = wanted
        deps = {n: self._deps[n] & names for n in names}
        in_deg = {n: len(deps[n]) for n in names}
        queue = sorted(n for n in names if in_deg[n] == 0)
        ordered: List[str] = []
        while queue:
            name = queue.pop(0)
            ordered.append(name)
            for other in sorted(names):
                if name in deps[other]:
                    deps[other].discard(name)
                    in_deg[other] -= 1
                    if in_deg[other] == 0:
                        queue.append(other)
        if len(ordered) != len(names):
            raise ValueError("OperatorGraph contains a cycle — cannot run")
        return ordered

    def dependents(self, name: str) -> Set[str]:
        """All operators transitively depending on ``name`` (downstream set)."""
        result: Set[str] = set()
        stack = [n for n, deps in self._deps.items() if name in deps]
        while stack:
            node = stack.pop()
            if node in result:
                continue
            result.add(node)
            stack.extend(o for o, deps in self._deps.items() if node in deps)
        return result

    def prune_from(self, name: str) -> List[str]:
        """Sub-graph (in topological order) rooted at ``name`` plus all
        downstream operators — the operator-level equivalent of "only re-run
        the affected sub-graph" used by incremental execution.
        """
        if name not in self._ops:
            raise KeyError(f"Unknown operator: {name!r}")
        return self.order(filter_names=self.dependents(name) | {name})

    def run(
        self,
        ctx: OperatorContext,
        filter_names: Optional[Iterable[str]] = None,
        cache: Optional[Any] = None,
    ) -> OperatorContext:
        """Execute the DAG (or the ``filter_names`` sub-graph) over ``ctx``.

        When ``cache`` is provided (V7.4 cache-aside, see
        ``pdf2zh.v3.operator_cache.OperatorResultCache``), each operator is
        first resolved against the cache: on a hit the stored outputs are
        applied to ``ctx`` and the operator itself is not executed; on a miss
        it runs normally and its outputs are stored under the content-addressed
        key computed from its input view. Operators without a declared cache
        spec are always executed. Trace entries gain a ``cached`` flag.
        """
        self._trace.clear()
        for name in self.order(filter_names=filter_names):
            op = self._ops[name]
            started = time.time()
            op.validate(ctx)
            cached = False
            key = cache.key_for(ctx, op) if cache is not None else None
            entry = cache.get(key) if key is not None else None
            if entry is not None:
                cache.apply(entry, ctx)
                cached = True
            else:
                ctx = op.execute(ctx)
                if key is not None:
                    cache.put(key, ctx, op)
            self._trace.append(
                {
                    "operator": name,
                    "version": op.version,
                    "elapsed_ms": round((time.time() - started) * 1000, 4),
                    "cached": cached,
                }
            )
        return ctx

    @property
    def trace(self) -> List[Dict[str, Any]]:
        return list(self._trace)

    def to_dict(self) -> dict:
        return {
            "operators": [
                {"name": n, "depends_on": sorted(self._deps[n])} for n in self.order()
            ],
        }

    def stats(self) -> dict:
        return {
            "operators": len(self._ops),
            "order": self.order(),
            "last_run": self._trace,
        }


class TypographyRule:
    """阶段五 rule: adaptive CJK / Latin typography adjustments.

    The 阶段五 deliverable demanded an explicit ``_typography_rule`` hook in
    the operator runtime. This class provides that hook as a pure, testable
    rule: it inspects the *translated* text and the source geometry and emits
    a manifest of adjustments (font size, line height, letter spacing) that
    the RenderOperator can consume.

    Rule outcomes:

      * ``adopt_source_geometry`` — translation fits the source bbox.
      * ``shrink_font`` — translation overflows horizontally → reduce font.
      * ``expand_block`` — translation is taller → grow the block height.
      * ``baseline_shift`` — mixed CJK/Latin baseline correction needed.
    """

    def __init__(self, max_overflow_ratio: float = 0.0) -> None:
        self.max_overflow_ratio = max_overflow_ratio

    def apply(
        self,
        translated: str,
        source: str = "",
        bbox: Optional[Tuple[float, float, float, float]] = None,
        font_size: float = 12.0,
    ) -> Dict[str, Any]:
        """Return a typography adjustment manifest for one block."""
        from pdf2zh.v3.typography import AdaptiveTypography, GlyphProbe

        width = bbox[2] if bbox else 400.0
        height = bbox[3] if bbox else 20.0
        ty = AdaptiveTypography(container_width=width, font_size=font_size)
        m = ty.metrics(
            translated,
            source=source or None,
            font_size=font_size,
            container_width=width,
        )

        manifest: Dict[str, Any] = {
            "rule": "adopt_source_geometry",
            "font_size": font_size,
            "line_height": m.line_height,
            "letter_spacing": 0.0,
            "block_height": m.block_height,
            "baseline_shift": 0.0,
        }
        expansion = m.expansion_ratio
        total_w = GlyphProbe.text_width(translated, font_size)
        overflow_w = m.estimated_width - width
        if overflow_w > self.max_overflow_ratio * width or total_w > width * 1.5:
            fit = ty.auto_fit_font_size(
                translated, font_size, width, max_lines=None, target_height=height
            )
            manifest.update(
                {
                    "rule": "shrink_font",
                    "font_size": fit,
                    "line_height": ty.line_height_for(translated, fit),
                    "letter_spacing": -0.5 * (font_size - fit),
                }
            )
        elif m.block_height > height * 1.05:
            manifest.update(
                {
                    "rule": "expand_block",
                    "block_height": m.block_height,
                }
            )
        if 0.05 < GlyphProbe.cjk_fraction(translated) < 0.95:
            baseline = ty.baseline_metrics(translated, font_size)
            if baseline["cjk_dominant"]:
                manifest["baseline_shift"] = -font_size * 0.08
        return manifest


__all__ = [
    "OperatorContext",
    "Operator",
    "ParseOperator",
    "AnalyzeOperator",
    "PlanOperator",
    "TranslateOperator",
    "ReviewOperator",
    "LayoutOperator",
    "RenderOperator",
    "OperatorRegistry",
    "OperatorGraph",
    "TypographyRule",
    "_as_jsonable",
]
