"""Module: V6.1 Translation Runtime — Translation as a Workflow.

A proper translation runtime that handles:
- Translation Plan routing (different models for different content types)
- Chunk scheduling with dependency management
- Context fusion from DocumentMemory + KnowledgeCenter
- Terminology consistency checking across chunks
- Auto-retry with fallback strategies
- Review and repair loop

Replaces the simple translator.translate(text) pattern with:
    session = TranslationRuntime(kernel)
    session.execute(plan)
    session.review()
    session.repair()

Usage:
    from pdf2zh.v3.translation_runtime import TranslationRuntime, TranslationWorkflow
    runtime = TranslationRuntime(kernel)
    result = runtime.execute(graph, plan)
"""

from __future__ import annotations
import logging, time, uuid, hashlib, json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Generator
from abc import ABC, abstractmethod
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType, Edge, EdgeType
from pdf2zh.v3.memory import DocumentMemory, EntityEntry
from pdf2zh.v3.planner import TranslationPlan, TranslationPlanner, PlannerConfig
from pdf2zh.v3.translator import TranslationSession, Translator, PromptComposer

logger = logging.getLogger(__name__)


class ChunkStatus(Enum):
    PENDING = "pending"
    ROUTED = "routed"
    TRANSLATING = "translating"
    DONE = "done"
    FAILED = "failed"
    REVIEWED = "reviewed"
    REPAIRED = "repaired"


class ConsistencyLevel(Enum):
    NONE = "none"
    TERMINOLOGY = "terminology"
    ENTITY = "entity"
    FULL = "full"


@dataclass
class TranslationChunkResult:
    node_id: str
    source_text: str
    translated_text: str = ""
    model: str = ""
    status: ChunkStatus = ChunkStatus.PENDING
    consistency_score: float = 1.0
    latency_ms: float = 0.0
    retry_count: int = 0
    error: str = ""
    review_notes: List[str] = field(default_factory=list)


@dataclass
class TranslationRoute:
    node_type: str
    model: str = "gpt-4o"
    temperature: float = 0.3
    max_tokens: int = 4096
    prompt_template: str = "general"
    retry_policy: str = "fixed"
    max_retries: int = 2
    priority: int = 100

    def matches(self, node: DocumentNode) -> bool:
        nt = (
            node.node_type.value
            if hasattr(node.node_type, "value")
            else str(node.node_type)
        )
        return nt == self.node_type


class Router:
    """Route translation requests to appropriate models/strategies."""

    DEFAULT_ROUTES = [
        TranslationRoute("paragraph", "gpt-4o", 0.3, 4096, "general", "fixed", 2, 100),
        TranslationRoute(
            "heading", "gpt-4o-mini", 0.1, 1024, "heading", "fixed", 1, 200
        ),
        TranslationRoute(
            "caption", "gpt-4o-mini", 0.2, 2048, "caption", "fixed", 1, 150
        ),
        TranslationRoute("abstract", "gpt-4o", 0.1, 4096, "abstract", "fixed", 2, 100),
        TranslationRoute(
            "formula", "gpt-4o-mini", 0.0, 1024, "formula", "fixed", 1, 50
        ),
        TranslationRoute(
            "reference", "gpt-4o-mini", 0.0, 2048, "reference", "fixed", 1, 50
        ),
        TranslationRoute("code", "gpt-4o-mini", 0.0, 4096, "code", "fixed", 1, 50),
        TranslationRoute(
            "footnote", "gpt-4o-mini", 0.2, 1024, "footnote", "fixed", 1, 80
        ),
        TranslationRoute("table", "gpt-4o", 0.2, 4096, "table", "fixed", 2, 100),
    ]

    def __init__(self, custom_routes: Optional[List[TranslationRoute]] = None) -> None:
        self._routes = custom_routes or list(self.DEFAULT_ROUTES)
        self._fallback_route = TranslationRoute("unknown", "gpt-4o-mini", 0.3, 2048)

    def route(self, node: DocumentNode) -> TranslationRoute:
        for r in self._routes:
            if r.matches(node):
                return r
        return self._fallback_route

    def add_route(self, route: TranslationRoute) -> None:
        self._routes.append(route)

    def remove_route(self, node_type: str) -> None:
        self._routes = [r for r in self._routes if r.node_type != node_type]


class ChunkScheduler:
    """Schedule translation chunks respecting dependencies."""

    def __init__(self, graph: DocumentGraph) -> None:
        self.graph = graph
        self._results: Dict[str, TranslationChunkResult] = {}

    def schedule(self, plan: TranslationPlan) -> List[str]:
        """Return ordered node IDs respecting dependency order."""
        if plan.node_ids:
            return plan.node_ids
        # Fallback: topological sort by reading order
        return [
            n.id
            for n in sorted(
                [
                    n
                    for n in self.graph.nodes
                    if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE)
                ],
                key=lambda n: (n.page_num, n.y0),
            )
        ]

    def mark_done(self, node_id: str, result: TranslationChunkResult) -> None:
        self._results[node_id] = result

    def mark_failed(self, node_id: str, error: str) -> None:
        if node_id not in self._results:
            self._results[node_id] = TranslationChunkResult(
                node_id=node_id,
                source_text="",
                translated_text="",
                status=ChunkStatus.FAILED,
                error=error,
            )
        else:
            self._results[node_id].status = ChunkStatus.FAILED
            self._results[node_id].error = error
            if not self._results[node_id].translated_text:
                self._results[node_id].translated_text = ""

    def get_result(self, node_id: str) -> Optional[TranslationChunkResult]:
        return self._results.get(node_id)

    @property
    def results(self) -> Dict[str, TranslationChunkResult]:
        return dict(self._results)


class ConsistencyChecker:
    """Check terminology and entity consistency across chunks."""

    def __init__(self, memory: Optional[DocumentMemory] = None) -> None:
        self.memory = memory

    def check(
        self,
        node_id: str,
        source: str,
        translation: str,
        previous_results: Dict[str, TranslationChunkResult],
    ) -> float:
        """Score consistency (0.0 = inconsistent, 1.0 = perfect)."""
        if not self.memory:
            return 1.0
        score = 1.0
        issues: List[str] = []
        # Check glossary terms
        for entry in self.memory.get_all_glossary():
            if entry.source.lower() in source.lower():
                if entry.target.lower() not in translation.lower():
                    score -= 0.1
                    issues.append(
                        f"Glossary term '{entry.source}' -> '{entry.target}' not found"
                    )
        # Check entity consistency
        for entry in self.memory.get_all_entities():
            if entry.name.lower() in source.lower():
                aliases = [a.lower() for a in entry.aliases] if entry.aliases else []
                has_alias = any(a in translation.lower() for a in aliases)
                if not has_alias and entry.name.lower() not in translation.lower():
                    score -= 0.05
        # Check abbreviation consistency
        for entry in self.memory.get_all_abbreviations():
            if entry.short.lower() in source.lower():
                if (
                    entry.long.lower() not in translation.lower()
                    and entry.short.lower() not in translation.lower()
                ):
                    score -= 0.05
        return max(0.0, score)

    def report(self, score: float) -> str:
        if score >= 0.95:
            return "excellent"
        elif score >= 0.8:
            return "acceptable"
        elif score >= 0.6:
            return "needs_review"
        else:
            return "needs_repair"


class RetryPolicy:
    """Retry policy with exponential backoff."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay_ms: float = 100.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay_ms = base_delay_ms
        self.backoff_factor = backoff_factor

    def should_retry(self, attempt: int, error: str) -> bool:
        return attempt < self.max_retries

    def delay_ms(self, attempt: int) -> float:
        return self.base_delay_ms * (self.backoff_factor**attempt)


class TranslationWorkflow:
    """A full translation workflow for a single document."""

    def __init__(
        self,
        graph: DocumentGraph,
        memory: Optional[DocumentMemory] = None,
        router: Optional[Router] = None,
    ) -> None:
        self.graph = graph
        self.memory = memory or DocumentMemory()
        self.router = router or Router()
        self.scheduler = ChunkScheduler(graph)
        self.checker = ConsistencyChecker(memory)
        self.retry_policy = RetryPolicy()
        self._results: Dict[str, TranslationChunkResult] = {}
        self._start_time: float = 0.0
        self._end_time: float = 0.0
        self._translator: Optional[Translator] = None
        self._session: Optional[TranslationSession] = None

    def execute(
        self,
        plan: TranslationPlan,
        translate_fn: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> Dict[str, TranslationChunkResult]:
        """Execute the full translation workflow.

        Args:
            plan: The TranslationPlan to execute
            translate_fn: Optional function(node_id, text) -> translated_text.
                          If not provided, uses the built-in Translator.

        Returns:
            Dict of node_id -> TranslationChunkResult
        """
        self._start_time = time.time()
        ordered = self.scheduler.schedule(plan)
        logger.info("TranslationWorkflow: executing %d chunks", len(ordered))

        for node_id in ordered:
            node = self.graph.get_node(node_id)
            if node is None or not node.text.strip():
                empty_result = TranslationChunkResult(
                    node_id=node_id,
                    source_text="",
                    translated_text="",
                    status=ChunkStatus.DONE,
                )
                self.scheduler.mark_done(node_id, empty_result)
                self._results[node_id] = empty_result
                continue

            route = self.router.route(node)
            result = TranslationChunkResult(
                node_id=node_id,
                source_text=node.text,
                model=route.model,
                status=ChunkStatus.ROUTED,
            )
            translated = None
            for attempt in range(route.max_retries + 1):
                start_t = time.time()
                try:
                    if translate_fn:
                        translated = translate_fn(node_id, node.text)
                    elif self._translator:
                        translated = self._translator.translate_node(node_id)
                    else:
                        translated = self._fallback_translate(node.text, route)
                    elapsed = (time.time() - start_t) * 1000
                    if translated:
                        result.translated_text = translated
                        result.latency_ms = elapsed
                        result.status = ChunkStatus.DONE
                        result.retry_count = attempt
                        break
                    else:
                        result.error = "Empty translation"
                except Exception as e:
                    elapsed = (time.time() - start_t) * 1000
                    result.error = str(e)
                    result.latency_ms = elapsed
                    if not self.retry_policy.should_retry(attempt, str(e)):
                        break
                    delay = self.retry_policy.delay_ms(attempt)
                    time.sleep(delay / 1000.0)

            if translated:
                # Check consistency
                score = self.checker.check(
                    node_id, node.text, translated, self.scheduler.results
                )
                result.consistency_score = score
                if score < 0.6:
                    result.status = ChunkStatus.FAILED
                    result.review_notes.append(f"Low consistency: {score:.2f}")
                else:
                    self.scheduler.mark_done(node_id, result)
            else:
                self.scheduler.mark_failed(node_id, result.error)

            self._results[node_id] = result

        self._end_time = time.time()
        return self._results

    def _fallback_translate(self, text: str, route: TranslationRoute) -> str:
        """Simple mock translation for testing."""
        return f"[{route.model}] {text[:200]}"

    def review(self) -> List[str]:
        """Post-translation review: identify issues for repair."""
        issues = []
        for nid, result in self._results.items():
            if result.status == ChunkStatus.FAILED:
                issues.append(f"Node {nid}: {result.error}")
            elif result.consistency_score < 0.8:
                issues.append(
                    f"Node {nid}: low consistency ({result.consistency_score:.2f})"
                )
        if self.memory:
            # Check all terms in the document are present
            all_text = " ".join(r.translated_text for r in self._results.values())
            for entry in self.memory.get_all_glossary():
                if entry.target.lower() not in all_text.lower():
                    issues.append(
                        f"Glossary term '{entry.source}' -> '{entry.target}' missing from output"
                    )
        return issues

    def repair(
        self,
        node_id: str,
        translate_fn: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> Optional[TranslationChunkResult]:
        """Re-translate a single node."""
        node = self.graph.get_node(node_id)
        if node is None:
            return None
        route = self.router.route(node)
        result = TranslationChunkResult(
            node_id=node_id,
            source_text=node.text,
            model=route.model,
            status=ChunkStatus.REPAIRED,
        )
        try:
            if translate_fn:
                translated = translate_fn(node_id, node.text)
            else:
                translated = self._fallback_translate(node.text, route)
            if translated:
                result.translated_text = translated
                result.status = ChunkStatus.REPAIRED
                self._results[node_id] = result
                self.scheduler.mark_done(node_id, result)
                return result
        except Exception as e:
            result.error = str(e)
        return None

    def stats(self) -> dict:
        total = len(self._results)
        done = sum(
            1
            for r in self._results.values()
            if r.status
            in (ChunkStatus.DONE, ChunkStatus.REVIEWED, ChunkStatus.REPAIRED)
        )
        failed = sum(
            1 for r in self._results.values() if r.status == ChunkStatus.FAILED
        )
        total_time = (
            (self._end_time - self._start_time) * 1000
            if self._end_time > self._start_time
            else 0
        )
        return {
            "total": total,
            "done": done,
            "failed": failed,
            "total_time_ms": round(total_time, 1),
            "avg_latency_ms": round(
                sum(r.latency_ms for r in self._results.values()) / max(total, 1), 1
            ),
            "avg_consistency": round(
                sum(r.consistency_score for r in self._results.values())
                / max(total, 1),
                3,
            ),
        }

    def get_results(self) -> Dict[str, TranslationChunkResult]:
        return dict(self._results)

    def apply_to_graph(self, use_transaction: bool = False) -> None:
        """Write translation results back to DocumentGraph nodes.

        Phase 1, Step 1.4: When use_transaction=True, uses GraphRuntime
        transaction for atomic write-back with version tracking.
        """
        if use_transaction:
            try:
                from pdf2zh.v3.runtime import GraphRuntime

                runtime = GraphRuntime(self.graph)
                with runtime.transaction("translation_workflow_apply"):
                    for nid, result in self._results.items():
                        if (
                            result.status
                            in (
                                ChunkStatus.DONE,
                                ChunkStatus.REVIEWED,
                                ChunkStatus.REPAIRED,
                            )
                            and result.translated_text
                        ):
                            node = self.graph.get_node(nid)
                            if node:
                                old_text = node.translated_text
                                node.translated_text = result.translated_text
                                node.metadata["model_used"] = result.model
                                node.metadata["translated_at"] = time.time()
                                node.metadata["consistency_score"] = (
                                    result.consistency_score
                                )
                                node.metadata["token_cost"] = len(
                                    result.translated_text.split()
                                )
                                runtime.mark_dirty(nid)
                return
            except Exception as e:
                logger.warning(
                    "Transaction-based apply failed: %s. Falling back to direct apply.",
                    e,
                )

        # Fallback: direct apply without transaction
        for nid, result in self._results.items():
            if (
                result.status
                in (ChunkStatus.DONE, ChunkStatus.REVIEWED, ChunkStatus.REPAIRED)
                and result.translated_text
            ):
                node = self.graph.get_node(nid)
                if node:
                    node.translated_text = result.translated_text
                    node.metadata["model_used"] = result.model
                    node.metadata["translated_at"] = time.time()


class TranslationRuntime:
    """Top-level translation runtime that orchestrates the full pipeline.

    Usage:
        runtime = TranslationRuntime(kernel)
        workflow = runtime.create_workflow(graph)
        results = workflow.execute(plan)
        issues = workflow.review()
        for issue in issues: workflow.repair(...)
        workflow.apply_to_graph()
    """

    def __init__(self, kernel=None, memory: Optional[DocumentMemory] = None) -> None:
        self.kernel = kernel
        self.memory = memory
        self._router = Router()
        self._workflows: Dict[str, TranslationWorkflow] = {}
        self._total_translated: int = 0
        self._total_time_ms: float = 0.0

    def create_workflow(self, graph: DocumentGraph) -> TranslationWorkflow:
        wf = TranslationWorkflow(graph, memory=self.memory, router=self._router)
        wid = f"wf_{len(self._workflows)}"
        self._workflows[wid] = wf
        return wf

    def execute(
        self,
        graph: DocumentGraph,
        plan: TranslationPlan,
        translate_fn: Optional[Callable] = None,
    ) -> Dict[str, TranslationChunkResult]:
        wf = self.create_workflow(graph)
        results = wf.execute(plan, translate_fn=translate_fn)
        self._total_translated += len(results)
        stats = wf.stats()
        self._total_time_ms += stats.get("total_time_ms", 0)
        return results

    def batch_translate(
        self, graphs: List[DocumentGraph], planner: TranslationPlanner
    ) -> List[Dict[str, TranslationChunkResult]]:
        all_results = []
        for g in graphs:
            # Create a plan for the entire graph by planning all nodes
            plans = planner.plan_all(g)
            # Build a merged plan with all node IDs
            node_ids = list(plans.keys())
            plan = TranslationPlan(node_ids=node_ids)
            results = self.execute(g, plan)
            all_results.append(results)
        return all_results

    def stats(self) -> dict:
        return {
            "total_translated": self._total_translated,
            "total_time_ms": round(self._total_time_ms, 1),
            "workflow_count": len(self._workflows),
        }

    @property
    def router(self) -> Router:
        return self._router


# --- Legacy Engine Bridge ---

_ENGINE_CACHE = {}


def discover_legacy_engines():
    if _ENGINE_CACHE:
        return _ENGINE_CACHE
    try:
        import importlib

        mod = importlib.import_module("pdf2zh.translator")
        for attr in dir(mod):
            cls = getattr(mod, attr)
            if isinstance(cls, type) and hasattr(cls, "name") and cls.name:
                _ENGINE_CACHE[cls.name.lower()] = cls
        import logging

        logging.getLogger(__name__).info(
            f"Discovered {len(_ENGINE_CACHE)} legacy engines: {list(_ENGINE_CACHE.keys())}"
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning(f"Cannot discover legacy engines: {e}")
    return _ENGINE_CACHE


class LegacyEngineAdapter:
    def __init__(self, engine_name, lang_in="", lang_out="", envs=None, prompt=None):
        engines = discover_legacy_engines()
        key = engine_name.lower()
        if key not in engines:
            avail = ", ".join(sorted(engines.keys())) if engines else "none"
            raise ValueError(f"Unknown engine {engine_name!r}. Available: {avail}")
        self._cls = engines[key]
        self._name = engine_name
        self._lang_in = lang_in
        self._lang_out = lang_out
        self._envs = envs or {}
        self._prompt = prompt
        self._inst = None

    def _get(self):
        if self._inst is None:
            self._inst = self._cls(
                self._lang_in,
                self._lang_out,
                model=self._envs.get("model"),
                envs=self._envs,
                prompt=self._prompt,
            )
        return self._inst

    @property
    def engine_name(self):
        return self._name

    def translate(self, text, **kwargs):
        inst = self._get()
        if hasattr(inst, "translate") and callable(inst.translate):
            r = inst.translate(text)
            return str(r) if r is not None else text
        if callable(inst):
            r = inst(text)
            return str(r) if r is not None else text
        raise RuntimeError(f"Engine {self._name!r} has no translate method")

    def batch_translate(self, texts):
        inst = self._get()
        if hasattr(inst, "batch_translate") and callable(inst.batch_translate):
            rs = inst.batch_translate(texts)
            return [str(r) if r is not None else t for r, t in zip(rs, texts)]
        return [self.translate(t) for t in texts]

    def __call__(self, text, **kwargs):
        return self.translate(text, **kwargs)


__all__ = [
    "ChunkStatus",
    "ConsistencyLevel",
    "TranslationChunkResult",
    "TranslationRoute",
    "Router",
    "ChunkScheduler",
    "ConsistencyChecker",
    "RetryPolicy",
    "TranslationWorkflow",
    "TranslationRuntime",
    "discover_legacy_engines",
    "LegacyEngineAdapter",
]
