"""Module: Graph Runtime — V4.0 Runtime Core.

Provides runtime services for the DocumentGraph:

  1. GraphTransaction  — atomic commit / rollback support
  2. GraphVersion      — version tracking with RevisionID
  3. GraphSnapshot     — point-in-time serialization
  4. GraphObserver     — dirty-flag and change-notification

Usage::
    from pdf2zh.v3.runtime import GraphRuntime

    rt = GraphRuntime(graph)
    with rt.transaction("translate_page_3"):
        node = graph.get_node("n42")
        node.text = "translated text"
        rt.mark_dirty(node.id)
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from pdf2zh.v3.graph import DocumentGraph, DocumentNode
from pdf2zh.v3.memory import DocumentMemory

logger = logging.getLogger(__name__)


class TransactionStatus(Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


@dataclass
class GraphVersion:
    """A version identifier in the runtime."""
    revision_id: str
    timestamp: float
    description: str = ""
    parent_revision: Optional[str] = None

    @classmethod
    def create(cls, description: str = "",
               parent: Optional[str] = None) -> "GraphVersion":
        return cls(
            revision_id=uuid.uuid4().hex[:12],
            timestamp=time.time(),
            description=description,
            parent_revision=parent,
        )


@dataclass
class ChangeRecord:
    """Record of a single change in a transaction."""
    node_id: str
    field: str
    old_value: Any
    new_value: Any


class GraphTransaction:
    """A transaction on a DocumentGraph.

    Supports:
      - Record all changes during the transaction
      - Commit: apply changes and finalize
      - Rollback: undo all changes
    """

    def __init__(self, runtime: "GraphRuntime", description: str = ""):
        self._runtime = runtime
        self.description = description
        self.status = TransactionStatus.ACTIVE
        self._changes: Dict[str, List[ChangeRecord]] = {}

    def record_change(
        self, node_id: str, field: str,
        old_value: Any, new_value: Any,
    ) -> None:
        if self.status != TransactionStatus.ACTIVE:
            raise RuntimeError(
                f"Cannot record change on {self.status.value} transaction"
            )
        if node_id not in self._changes:
            self._changes[node_id] = []
        self._changes[node_id].append(
            ChangeRecord(node_id, field, old_value, new_value)
        )

    def commit(self) -> str:
        if self.status != TransactionStatus.ACTIVE:
            raise RuntimeError(f"Cannot commit {self.status.value} transaction")
        parent = self._runtime._current_version.revision_id
        new_version = GraphVersion.create(
            description=self.description, parent=parent,
        )
        self._runtime._current_version = new_version
        self._runtime._revision_history.append(new_version)
        for node_id in self._changes:
            self._runtime._dirty_nodes.add(node_id)
        self.status = TransactionStatus.COMMITTED
        logger.debug(
            "Committed '%s' (rev=%s, %d changes)",
            self.description, new_version.revision_id,
            sum(len(c) for c in self._changes.values()),
        )
        return new_version.revision_id

    def rollback(self) -> None:
        if self.status != TransactionStatus.ACTIVE:
            raise RuntimeError(
                f"Cannot rollback {self.status.value} transaction"
            )
        graph = self._runtime._graph
        for node_id, changes in self._changes.items():
            node = graph.get_node(node_id)
            if node is None:
                continue
            for change in reversed(changes):
                setattr(node, change.field, change.old_value)
        self.status = TransactionStatus.ROLLED_BACK
        logger.debug(
            "Rolled back '%s' (%d changes undone)",
            self.description,
            sum(len(c) for c in self._changes.values()),
        )

    def get_changes(self) -> Dict[str, List[ChangeRecord]]:
        return dict(self._changes)

    def change_count(self) -> int:
        return sum(len(c) for c in self._changes.values())

    def __enter__(self) -> "GraphTransaction":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            self.rollback()
        elif self.status == TransactionStatus.ACTIVE:
            self.commit()


class GraphObserver:
    """Observes changes via callbacks and dirty-flag querying."""

    def __init__(self) -> None:
        self._callbacks: Dict[str, List[Callable]] = {}

    def on(self, event: str, callback: Callable) -> None:
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(callback)

    def off(self, event: str, callback: Callable) -> None:
        if event in self._callbacks:
            self._callbacks[event] = [
                cb for cb in self._callbacks[event] if cb is not callback
            ]

    def emit(self, event: str, **data: Any) -> None:
        for cb in self._callbacks.get(event, []):
            try:
                cb(**data)
            except Exception as e:
                logger.warning("Observer callback error for %s: %s", event, e)


@dataclass
class GraphSnapshot:
    """A point-in-time snapshot of a DocumentGraph."""
    revision_id: str
    timestamp: float
    nodes: List[dict]
    description: str = ""

    def describe(self) -> str:
        return (
            f"Snapshot(rev={self.revision_id[:8]}, "
            f"nodes={len(self.nodes)}, "
            f"desc='{self.description}')"
        )


class GraphRuntime:
    """Runtime layer for a DocumentGraph.

    Provides:
      - Transaction-based changes (commit/rollback)
      - Version tracking with revision history
      - Dirty-node tracking for incremental updates
      - Observer pattern for change notification
      - Snapshot serialization
    """

    def __init__(self, graph: Optional[DocumentGraph] = None):
        self._graph: DocumentGraph = graph or DocumentGraph()
        self._current_version = GraphVersion.create(description="initial")
        self._revision_history: List[GraphVersion] = [self._current_version]
        self._dirty_nodes: Set[str] = set()
        self._observer = GraphObserver()
        self._active_transaction: Optional[GraphTransaction] = None

    @property
    def graph(self) -> DocumentGraph:
        return self._graph

    @property
    def current_revision(self) -> str:
        return self._current_version.revision_id

    @property
    def revision_history(self) -> List[GraphVersion]:
        return list(self._revision_history)

    @property
    def revision_count(self) -> int:
        return len(self._revision_history)

    @property
    def dirty_nodes(self) -> Set[str]:
        return set(self._dirty_nodes)

    @property
    def observer(self) -> GraphObserver:
        return self._observer

    def begin_transaction(self, description: str = "") -> GraphTransaction:
        if self._active_transaction is not None:
            raise RuntimeError("A transaction is already active")
        tx = GraphTransaction(self, description=description)
        self._active_transaction = tx
        return tx

    def transaction(self, description: str = ""):
        return self.begin_transaction(description)

    def mark_dirty(self, node_id: str) -> None:
        self._dirty_nodes.add(node_id)
        self._observer.emit("node_changed", node_id=node_id)

    def mark_clean(self, node_id: str) -> None:
        self._dirty_nodes.discard(node_id)

    def is_dirty(self, node_id: str) -> bool:
        return node_id in self._dirty_nodes

    def clear_dirty(self) -> None:
        self._dirty_nodes.clear()

    def take_snapshot(self, description: str = "") -> GraphSnapshot:
        nodes_data = []
        for node in self._graph.nodes:
            nodes_data.append({
                "id": node.id,
                "node_type": node.node_type.value,
                "bbox": list(node.bbox),
                "text": node.text,
                "page_num": node.page_num,
                "font_size": node.font_size,
            })
        return GraphSnapshot(
            revision_id=self._current_version.revision_id,
            timestamp=time.time(),
            nodes=nodes_data,
            description=description or f"Snapshot rev {self._current_version.revision_id[:8]}",
        )

    def restore_snapshot(self, snapshot: GraphSnapshot) -> None:
        self._graph = DocumentGraph()
        for nd in snapshot.nodes:
            node_type = nd["node_type"]
            if isinstance(node_type, str):
                from pdf2zh.v3.graph import NodeType
                node_type = NodeType(node_type)
            node = DocumentNode(
                id=nd["id"], node_type=node_type,
                bbox=tuple(nd["bbox"]),
                text=nd.get("text", ""),
                page_num=nd.get("page_num", 0),
                font_size=nd.get("font_size", 0.0),
            )
            self._graph.add_node(node)
        self._current_version = GraphVersion.create(
            description=f"restored from {snapshot.revision_id[:8]}",
        )
        self._dirty_nodes.clear()
        logger.info("Restored snapshot: %s", snapshot.describe())

    def get_version(self, revision_id: str) -> Optional[GraphVersion]:
        for v in self._revision_history:
            if v.revision_id == revision_id:
                return v
        return None

    def has_revision(self, revision_id: str) -> bool:
        return self.get_version(revision_id) is not None



# ── Runtime Facade (P0) ────────────────────────────────────────────────────


class RuntimeFacade:
    """Unified Runtime Facade — V4 top-level entry point.

    Provides a single lifecycle: load → analyze → plan → translate → layout → render → evaluate.

    Usage:
        rt = RuntimeFacade()
        rt.load(pdf_path)
        rt.analyze()
        rt.plan()
        rt.translate()
        tree = rt.layout()
        output = rt.render(fmt="pdf")
        result = rt.evaluate()
    """

    def __init__(self, config: Optional[dict] = None,
                 feature_flags: Optional["FeatureFlags"] = None):

        self.config = config or {}
        from pdf2zh.v3.feature_flags import FeatureFlags, get_feature_flags
        self.feature_flags = feature_flags if feature_flags is not None else get_feature_flags()
        self.source: str = ""
        self.graph: Optional[DocumentGraph] = None
        self.memory: Optional[DocumentMemory] = None
        self.plans: Optional[dict] = None
        self.translator: Optional[Any] = None
        self.layout_engine: Optional[Any] = None
        self.tree: Optional[VisualTree] = None
        self.output: Optional[bytes] = None
        self.evaluation: Optional[Any] = None
        self._parser: Any = None
        self._normalizer: Any = None
        self._analyzer: Any = None
        self._planner: Any = None
        self._diagnostic_report: Optional[Any] = None

    def load(self, path: str) -> "RuntimeFacade":
        """Parse a PDF into DocumentGraph."""
        self.source = path
        from pdf2zh.v3.parser import PDFParser
        from pdf2zh.v3.normalizer import Normalizer, NormalizerConfig
        from pdf2zh.v3.graph import DocumentGraphBuilder

        self._parser = PDFParser()
        raw = self._parser.parse(path)

        cfg = NormalizerConfig(lang_in=self.config.get("lang_in", "auto"))
        self._normalizer = Normalizer(cfg)
        normalized = self._normalizer.normalize(raw)

        builder = DocumentGraphBuilder()
        self.graph = builder.build(normalized)
        return self

    def analyze(self) -> "RuntimeFacade":
        """Run semantic analysis."""
        from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig

        self._analyzer = SemanticAnalyzer(AnalyzerConfig(
            lang_in=self.config.get("lang_in", "auto"),
        ))
        self.graph = self._analyzer.analyze(self.graph)
        return self

    def plan(self) -> "RuntimeFacade":
        """Generate translation plans."""
        from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig

        self._planner = TranslationPlanner(PlannerConfig(
            source_lang=self.config.get("lang_in", "auto"),
            target_lang=self.config.get("lang_out", "zh-cn"),
        ))
        self.plans = self._planner.plan_all(self.graph)
        return self

    def translate(self) -> "RuntimeFacade":
        """Translate all nodes (uses placeholder by default)."""
        from pdf2zh.v3.translator import TranslationSession, Translator

        self.memory = DocumentMemory()
        session = TranslationSession(
            graph=self.graph,
            memory=self.memory,
            planner=self._planner,
        )
        self.translator = Translator(session)
        self.translator.translate_all()
        return self

    def layout(self) -> Any:
        """Run layout engine and return VisualTree.

        Phase 2, Step 2.1: When use_v4_visual_tree_builder flag is
        enabled, builds via VisualTreeBuilder from DocumentGraph.
        """
        ff = self.feature_flags
        if ff.use_v4_visual_tree_builder:
            from pdf2zh.v3.visual_tree_builder import VisualTreeBuilder
            builder = VisualTreeBuilder(
                page_width=self.config.get("page_width", 612),
                page_height=self.config.get("page_height", 792),
            )
            self.tree = builder.build_from_graph(self.graph)
        else:
            from pdf2zh.v3.layout import LayoutEngine
            self.layout_engine = LayoutEngine(
                page_width=self.config.get("page_width", 612),
                page_height=self.config.get("page_height", 792),
            )
            self.tree = self.layout_engine.layout(self.graph)

        # Phase 2, Step 2.4: Freeze layout
        if self.tree is not None and not getattr(self.tree, 'is_layout_frozen', False):
            self.tree.freeze_layout()
        return self.tree

    def render(self, fmt: str = "pdf") -> bytes:
        """Render VisualTree to target format."""
        from pdf2zh.v3.renderer import RendererFactory

        renderer = RendererFactory.create(fmt)
        self.output = renderer.render(self.tree)
        return self.output

    def evaluate(self) -> Any:
        """Run quality evaluation.

        Phase 3, Step 3.1: Generates DiagnosticReport when
        use_v4_diagnostic flag is enabled.
        """
        from pdf2zh.v3.evaluator import QualityEvaluator, EvaluatorConfig

        evaluator = QualityEvaluator(EvaluatorConfig())
        self.evaluation = evaluator.evaluate(self.graph, self.graph)

        if self.feature_flags.use_v4_diagnostic:
            try:
                from pdf2zh.v3.evaluator import (
                    DiagnosticReport, EvaluationIssueMapper,
                )
                mapper = EvaluationIssueMapper()
                self._diagnostic_report = mapper.map_result(
                    self.evaluation, self.graph,
                )
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "DiagnosticReport failed: %s", exc,
                )

        return self.evaluation

    def pipeline(self, path: str, fmt: str = "pdf") -> bytes:
        """Run end-to-end pipeline: load → analyze → plan → translate → layout → render → evaluate.

        Phase 3, Step 3.4: Fix-Validate loop when use_v4_fix_validate_loop
        flag is enabled. Iterates up to max_repair_passes times.
        """
        self.load(path).analyze().plan().translate().layout()

        if self.feature_flags.use_v4_fix_validate_loop:
            max_p = max(1, self.feature_flags.max_repair_passes)
            for lp in range(max_p):
                self.evaluate()
                if self._diagnostic_report is not None:
                    try:
                        from pdf2zh.v3.evaluator import (
                            IssueSeverity, RepairScheduler,
                        )
                        critical = (
                            self._diagnostic_report.get_issues_by_severity(
                                IssueSeverity.CRITICAL,
                            )
                            + self._diagnostic_report.get_issues_by_severity(
                                IssueSeverity.BLOCKER,
                            )
                        )
                        if not critical:
                            logger.info(
                                "No critical issues (pass %d/%d)",
                                lp + 1, max_p,
                            )
                            break

                        logger.info(
                            "Fix pass %d/%d: %d critical issues",
                            lp + 1, max_p, len(critical),
                        )

                        scheduler = RepairScheduler()
                        repairs = scheduler.schedule(
                            self._diagnostic_report,
                        )
                        if not repairs:
                            break

                        # Repairs: RE_TRANSLATE or RE_LAYOUT
                        if self.feature_flags.use_v4_translator:
                            from pdf2zh.v3.translation_runtime import (
                                TranslationRuntime,
                            )
                            tr = TranslationRuntime()
                            re_tn_ids = [
                                r["node_id"] for r in repairs
                                if r.get("action") == "RE_TRANSLATE"
                                and r.get("node_id")
                            ]
                            for nid in re_tn_ids:
                                node = self.graph.get_node(nid)
                                if node:
                                    node.translated_text = None
                            if re_tn_ids:
                                from pdf2zh.v3.planner import TranslationPlan
                                plan = TranslationPlan(node_ids=re_tn_ids)
                                tr.execute(self.graph, plan)

                        if self.feature_flags.use_v4_layout:
                            self.layout()

                    except Exception as exc:
                        logger.warning(
                            "Fix-validate loop error: %s", exc,
                        )
                        break

        return self.render(fmt)

    def summary(self) -> dict:
        return {
            "source": self.source,
            "graph_nodes": len(self.graph.nodes) if self.graph else 0,
            "graph_edges": len(self.graph.edges) if self.graph else 0,
            "plans": len(self.plans) if self.plans else 0,
            "tree_pages": self.tree.page_count if self.tree else 0,
            "output_size": len(self.output) if self.output else 0,
            "evaluation": self.evaluation.to_dict() if self.evaluation else None,
        }


__all__ = [
    "RuntimeFacade", "GraphRuntime", "GraphTransaction", "GraphVersion",
    "GraphSnapshot", "GraphObserver", "ChangeRecord",
    "TransactionStatus",
]
