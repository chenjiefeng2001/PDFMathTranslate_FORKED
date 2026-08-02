"""Module: V6.0 Graph Registry — unified node_id namespace across four graphs.

The report (chapter 4) requires four correlated graphs sharing ONE node_id
namespace instead of a single ever-growing mega graph:

    Document Graph  — structure      (v3/graph.py DocumentGraph)
    Semantic Graph  — semantics      (v3/document_intelligence.py EntityGraph/ConceptGraph/CitationGraph)
    Layout Graph    — spatial layout (v3/constraint_graph.py ConstraintGraph)
    Execution Graph — execution state(v3/execution_graph.py ExecutionGraph)

A node lives in several graphs at once; each graph stores only the facet it
owns. The registry provides the "travel between graphs" query interface:

    node "p42" ──► DocumentGraph: Paragraph(contains in Section 2)
                ──► SemanticGraph: BODY_TEXT, defines term "Transformer"
                ──► LayoutGraph:   CANNOT_OVERLAP with Figure 3
                ──► ExecutionGraph: state=TRANSLATED, depends_on=[p41]

GraphRegistry is duck-typed: it queries whatever graph-like objects are
registered, so it works with all four existing implementations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class GraphKind(Enum):
    DOCUMENT = "document"
    SEMANTIC = "semantic"
    LAYOUT = "layout"
    EXECUTION = "execution"


@dataclass
class GraphMembership:
    """Which graphs contain a given node_id."""

    node_id: str
    graphs: Set[GraphKind] = field(default_factory=set)

    def has(self, kind: GraphKind) -> bool:
        return kind in self.graphs

    @property
    def count(self) -> int:
        return len(self.graphs)

    @property
    def names(self) -> List[str]:
        return sorted(g.value for g in self.graphs)

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "graphs": self.names}


class GraphRegistry:
    """Registry correlating the four graphs through a shared node_id space.

    The registry does not copy data — it holds references to the registered
    graph objects and queries them on demand (duck typing). Graphs are
    registered per GraphKind; registering a second graph of the same kind
    replaces the previous one.
    """

    def __init__(self) -> None:
        self._graphs: Dict[GraphKind, Any] = {}

    # ── Registration ─────────────────────────────────────────────

    def register(self, kind: GraphKind, graph: Any) -> None:
        if isinstance(kind, str):
            kind = GraphKind(kind)
        self._graphs[kind] = graph

    def register_document_graph(self, graph: Any) -> None:
        self.register(GraphKind.DOCUMENT, graph)

    def register_semantic_graph(self, graph: Any) -> None:
        self.register(GraphKind.SEMANTIC, graph)

    def register_layout_graph(self, graph: Any) -> None:
        self.register(GraphKind.LAYOUT, graph)

    def register_execution_graph(self, graph: Any) -> None:
        self.register(GraphKind.EXECUTION, graph)

    def get(self, kind: GraphKind) -> Optional[Any]:
        if isinstance(kind, str):
            kind = GraphKind(kind)
        return self._graphs.get(kind)

    def unregister(self, kind: GraphKind) -> None:
        if isinstance(kind, str):
            kind = GraphKind(kind)
        self._graphs.pop(kind, None)

    @property
    def registered_kinds(self) -> List[GraphKind]:
        return list(self._graphs.keys())

    @property
    def all_registered(self) -> bool:
        """True when all four graphs are registered."""
        return all(k in self._graphs for k in GraphKind)

    # ── Node membership helpers ──────────────────────────────────

    def _has_node(self, graph: Any, node_id: str) -> bool:
        if graph is None:
            return False
        getter = getattr(graph, "get_node", None)
        if getter is not None:
            try:
                return getter(node_id) is not None
            except Exception:  # pragma: no cover - defensive
                return False
        nodes = getattr(graph, "nodes", None)
        if isinstance(nodes, dict):
            return node_id in nodes
        if nodes is not None:
            try:
                return any(getattr(n, "id", None) == node_id for n in nodes)
            except Exception:  # pragma: no cover - defensive
                return False
        return False

    def membership(self, node_id: str) -> GraphMembership:
        """Return which graphs contain node_id."""
        graphs = set()
        for kind, graph in self._graphs.items():
            if self._has_node(graph, node_id):
                graphs.add(kind)
        return GraphMembership(node_id=node_id, graphs=graphs)

    def node_in(self, node_id: str, kind: GraphKind) -> bool:
        if isinstance(kind, str):
            kind = GraphKind(kind)
        return self._has_node(self._graphs.get(kind), node_id)

    def coverage(self, node_id: str) -> int:
        """How many of the four graphs contain node_id."""
        return self.membership(node_id).count

    # ── Cross-graph lookup ───────────────────────────────────────

    def get_node(self, node_id: str, kind: GraphKind) -> Optional[Any]:
        """Fetch a node from one specific graph."""
        if isinstance(kind, str):
            kind = GraphKind(kind)
        graph = self._graphs.get(kind)
        if graph is None:
            return None
        getter = getattr(graph, "get_node", None)
        if getter is not None:
            try:
                return getter(node_id)
            except Exception:  # pragma: no cover - defensive
                return None
        nodes = getattr(graph, "nodes", None)
        if isinstance(nodes, dict):
            return nodes.get(node_id)
        if nodes is not None:
            for n in nodes:
                if getattr(n, "id", None) == node_id:
                    return n
        return None

    def get_all(self, node_id: str) -> Dict[GraphKind, Any]:
        """Return the node record from every graph that contains it."""
        return {
            kind: self.get_node(node_id, kind)
            for kind in GraphKind
            if self.node_in(node_id, kind)
        }

    def all_node_ids(self) -> Set[str]:
        """Union of node ids across all registered graphs."""
        ids: Set[str] = set()
        for graph in self._graphs.values():
            nodes = getattr(graph, "nodes", None)
            if nodes is None:
                continue
            if isinstance(nodes, dict):
                ids.update(nodes.keys())
            else:
                ids.update(getattr(n, "id", None) for n in nodes)
        return {i for i in ids if i is not None}

    def graph_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for kind, graph in self._graphs.items():
            nodes = getattr(graph, "nodes", None)
            if nodes is None:
                counts[kind.value] = 0
            elif isinstance(nodes, dict):
                counts[kind.value] = len(nodes)
            else:
                counts[kind.value] = len(nodes)
        return counts

    def consistency_report(self) -> dict:
        """Summarize graph coherence for the compatibility baseline."""
        all_ids = sorted(self.all_node_ids())
        report = {
            "registered": [k.value for k in self.registered_kinds],
            "counts": self.graph_counts(),
            "total_unique_nodes": len(all_ids),
            "avg_coverage": round(
                sum(self.coverage(i) for i in all_ids) / len(all_ids), 2
            ) if all_ids else 0.0,
            "nodes_missing_in_execution": [
                i for i in all_ids if not self.node_in(i, GraphKind.EXECUTION)
            ][:20],
        }
        return report

    def to_dict(self) -> dict:
        memberships = {
            i: self.membership(i).names for i in sorted(self.all_node_ids())
        }
        return {
            "registered": [k.value for k in self.registered_kinds],
            "counts": self.graph_counts(),
            "memberships": memberships,
        }

    def __len__(self) -> int:
        return len(self.all_node_ids())


__all__ = ["GraphKind", "GraphMembership", "GraphRegistry"]



