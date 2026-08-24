"""Module: V6.1 Unified Graph Infrastructure — BaseGraph.

Resolves the *Graph Explosion* (DocumentGraph / ExecutionGraph /
ConstraintGraph / KnowledgeGraph / DiagnosticGraph ...) by providing a single
generic graph backbone shared by every concrete graph:

    BaseGraph
      ├── GraphNode / GraphEdge / GraphProperty   — unified element model
      ├── GraphTraversal                          — DFS / BFS / Topological / Cycle
      ├── GraphVisitor                            — visitor pattern
      ├── GraphDiff                               — structural diff (added/removed/updated)
      └── GraphSnapshot                           — point-in-time snapshot + restore

`adapt()` wraps any existing concrete graph (DocumentGraph, ConstraintGraph,
ExecutionGraph, ...) behind this same interface, so that every graph natively
shares DFS / BFS / Topological / Cycle / Merge / Clone / Serialize / Diff /
Snapshot — without touching the concrete classes.

Usage::

    from pdf2zh.v3.base_graph import BaseGraph, GraphNode, GraphEdge, adapt
    from pdf2zh.v3.graph import DocumentGraph

    bg = adapt(document_graph)          # unified view over a DocumentGraph
    topo = bg.topological_sort()        # shared traversal
    snap = bg.snapshot("checkpoint_1")  # shared snapshot
    diff = bg.diff(adapt(other_graph))  # shared diff
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class GraphKind(str, Enum):
    """Kind of a unified graph view."""

    DOCUMENT = "document"
    SEMANTIC = "semantic"
    EXECUTION = "execution"
    CONSTRAINT = "constraint"
    KNOWLEDGE = "knowledge"
    DIAGNOSTIC = "diagnostic"
    WORKFLOW = "workflow"
    TASK = "task"
    CUSTOM = "custom"


@dataclass
class GraphNode:
    """A unified node in the graph backbone."""

    id: str
    kind: GraphKind = GraphKind.CUSTOM
    label: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    version: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "label": self.label,
            "properties": dict(self.properties),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GraphNode":
        return cls(
            id=data["id"],
            kind=GraphKind(data.get("kind", GraphKind.CUSTOM.value)),
            label=data.get("label", ""),
            properties=dict(data.get("properties", {})),
            version=int(data.get("version", 0)),
        )

    def bump(self) -> None:
        self.version += 1


@dataclass
class GraphEdge:
    """A unified directed edge in the graph backbone."""

    source_id: str
    target_id: str
    relation: str = "related"
    properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GraphEdge":
        return cls(
            source_id=data["source_id"],
            target_id=data["target_id"],
            relation=data.get("relation", "related"),
            properties=dict(data.get("properties", {})),
        )


class GraphProperty(dict):
    """Typed property bag attached to nodes and edges."""


class GraphVisitor:
    """Visitor base class for walking a unified graph."""

    def visit_node(self, node: GraphNode, graph: "BaseGraph") -> None:
        pass

    def visit_edge(self, edge: GraphEdge, graph: "BaseGraph") -> None:
        pass


class GraphTraversal:
    """Pure-function traversal algorithms shared by every graph.

    Each static method only needs:
      - `node_ids`: an iterable of node ids
      - `out_edges`: a callable ``node_id -> Iterable[GraphEdge]``

    so any concrete graph (or a plain adjacency dict) can reuse them.
    """

    @staticmethod
    def dfs(
        start_id: str, out_edges: Callable[[str], Iterable[GraphEdge]]
    ) -> List[str]:
        visited: Set[str] = set()
        order: List[str] = []

        def _visit(nid: str) -> None:
            if nid in visited:
                return
            visited.add(nid)
            order.append(nid)
            for e in out_edges(nid):
                _visit(e.target_id)

        _visit(start_id)
        return order

    @staticmethod
    def bfs(
        start_id: str, out_edges: Callable[[str], Iterable[GraphEdge]]
    ) -> List[str]:
        from collections import deque

        visited = {start_id}
        order: List[str] = []
        queue = deque([start_id])
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for e in out_edges(nid):
                if e.target_id not in visited:
                    visited.add(e.target_id)
                    queue.append(e.target_id)
        return order

    @staticmethod
    def topological_sort(
        node_ids: Iterable[str],
        out_edges: Callable[[str], Iterable[GraphEdge]],
    ) -> List[str]:
        """Kahn's algorithm. Raises ValueError on a cycle."""
        ids = list(node_ids)
        in_deg: Dict[str, int] = {nid: 0 for nid in ids}
        for nid in ids:
            for e in out_edges(nid):
                if e.target_id in in_deg:
                    in_deg[e.target_id] += 1
        queue = [nid for nid, d in in_deg.items() if d == 0]
        ordered: List[str] = []
        while queue:
            nid = queue.pop(0)
            ordered.append(nid)
            for e in out_edges(nid):
                if e.target_id in in_deg:
                    in_deg[e.target_id] -= 1
                    if in_deg[e.target_id] == 0:
                        queue.append(e.target_id)
        if len(ordered) != len(ids):
            raise ValueError("Graph contains a cycle — topological sort impossible")
        return ordered

    @staticmethod
    def has_cycle(
        node_ids: Iterable[str],
        out_edges: Callable[[str], Iterable[GraphEdge]],
    ) -> bool:
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {nid: WHITE for nid in node_ids}

        def _visit(nid: str) -> bool:
            color[nid] = GRAY
            for e in out_edges(nid):
                if e.target_id not in color:
                    continue
                if color[e.target_id] == GRAY:
                    return True
                if color[e.target_id] == WHITE and _visit(e.target_id):
                    return True
            color[nid] = BLACK
            return False

        return any(_visit(nid) for nid in list(color) if color[nid] == WHITE)

    @staticmethod
    def find_cycle(
        node_ids: Iterable[str],
        out_edges: Callable[[str], Iterable[GraphEdge]],
    ) -> Optional[List[str]]:
        """Return one cycle (list of node ids) or None if acyclic."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {nid: WHITE for nid in node_ids}
        stack: List[str] = []

        def _visit(nid: str) -> Optional[List[str]]:
            color[nid] = GRAY
            stack.append(nid)
            for e in out_edges(nid):
                if e.target_id not in color:
                    continue
                if color[e.target_id] == GRAY:
                    return stack[stack.index(e.target_id) :] + [e.target_id]
                if color[e.target_id] == WHITE:
                    cyc = _visit(e.target_id)
                    if cyc:
                        return cyc
            stack.pop()
            color[nid] = BLACK
            return None

        for nid in list(color):
            if color[nid] == WHITE:
                cyc = _visit(nid)
                if cyc:
                    return cyc
        return None

    @staticmethod
    def connected_components(
        node_ids: Iterable[str],
        out_edges: Callable[[str], Iterable[GraphEdge]],
    ) -> List[List[str]]:
        seen: Set[str] = set()
        components: List[List[str]] = []
        for nid in node_ids:
            if nid in seen:
                continue
            comp = GraphTraversal.dfs(nid, out_edges)
            seen.update(comp)
            components.append(comp)
        return components

    @staticmethod
    def reachable(
        start_ids: Iterable[str],
        out_edges: Callable[[str], Iterable[GraphEdge]],
    ) -> Set[str]:
        seen: Set[str] = set()
        stack = list(start_ids)
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            stack.extend(e.target_id for e in out_edges(nid))
        return seen


class GraphDiff:
    """Structural difference between two graphs (or two snapshots)."""

    def __init__(self) -> None:
        self.added_nodes: List[GraphNode] = []
        self.removed_nodes: List[GraphNode] = []
        self.updated_nodes: List[Tuple[str, Dict[str, Any]]] = []
        self.added_edges: List[GraphEdge] = []
        self.removed_edges: List[GraphEdge] = []

    @classmethod
    def between(cls, before: "BaseGraph", after: "BaseGraph") -> "GraphDiff":
        diff = cls()
        before_nodes = {n.id: n for n in before.nodes}
        after_nodes = {n.id: n for n in after.nodes}
        for nid, node in after_nodes.items():
            if nid not in before_nodes:
                diff.added_nodes.append(node)
            elif node.properties != before_nodes[nid].properties:
                diff.updated_nodes.append((nid, node.properties))
        for nid, node in before_nodes.items():
            if nid not in after_nodes:
                diff.removed_nodes.append(node)

        before_edges = {(e.source_id, e.target_id, e.relation) for e in before.edges}
        after_edges = {(e.source_id, e.target_id, e.relation) for e in after.edges}
        for e in after.edges:
            if (e.source_id, e.target_id, e.relation) not in before_edges:
                diff.added_edges.append(e)
        for e in before.edges:
            if (e.source_id, e.target_id, e.relation) not in after_edges:
                diff.removed_edges.append(e)
        return diff

    @property
    def is_empty(self) -> bool:
        return not (
            self.added_nodes
            or self.removed_nodes
            or self.updated_nodes
            or self.added_edges
            or self.removed_edges
        )

    def to_dict(self) -> dict:
        return {
            "added_nodes": [n.to_dict() for n in self.added_nodes],
            "removed_nodes": [n.to_dict() for n in self.removed_nodes],
            "updated_nodes": [
                {"id": nid, "properties": props} for nid, props in self.updated_nodes
            ],
            "added_edges": [e.to_dict() for e in self.added_edges],
            "removed_edges": [e.to_dict() for e in self.removed_edges],
        }

    def summary(self) -> dict:
        return {
            "added_nodes": len(self.added_nodes),
            "removed_nodes": len(self.removed_nodes),
            "updated_nodes": len(self.updated_nodes),
            "added_edges": len(self.added_edges),
            "removed_edges": len(self.removed_edges),
            "changed": not self.is_empty,
        }


class GraphSnapshot:
    """A serializable point-in-time snapshot of a unified graph."""

    def __init__(
        self,
        kind: GraphKind,
        name: str,
        data: dict,
        created_at: Optional[float] = None,
        snapshot_id: Optional[str] = None,
    ) -> None:
        self.kind = kind
        self.name = name
        self.data = data
        self.created_at = created_at or time.time()
        self.snapshot_id = snapshot_id or uuid.uuid4().hex[:12]

    @classmethod
    def capture(cls, graph: "BaseGraph", name: str = "") -> "GraphSnapshot":
        return cls(kind=graph.kind, name=name, data=graph.to_dict())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "created_at": self.created_at,
            "snapshot_id": self.snapshot_id,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GraphSnapshot":
        return cls(
            kind=GraphKind(data.get("kind", GraphKind.CUSTOM.value)),
            name=data.get("name", ""),
            data=data.get("data", {"nodes": [], "edges": []}),
            created_at=data.get("created_at"),
            snapshot_id=data.get("snapshot_id"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def restore_into(self, graph: "BaseGraph") -> None:
        graph.clear()
        restored = BaseGraph.from_dict(self.data)
        for n in restored.nodes:
            graph.add_node(n)
        for e in restored.edges:
            graph.add_edge(e)

    def diff(self, other: "GraphSnapshot") -> GraphDiff:
        before = BaseGraph.from_dict(self.data)
        after = BaseGraph.from_dict(other.data)
        return GraphDiff.between(before, after)


class BaseGraph:
    """The unified graph backbone shared by all document runtime graphs."""

    def __init__(self, kind: GraphKind = GraphKind.CUSTOM, name: str = "") -> None:
        self.kind = kind
        self.name = name
        self._nodes: Dict[str, GraphNode] = {}
        self._edges: List[GraphEdge] = []
        self._out: Dict[str, List[GraphEdge]] = {}
        self._in: Dict[str, List[GraphEdge]] = {}

    # ── Node operations ────────────────────────────────────────────────

    def add_node(self, node: GraphNode) -> "BaseGraph":
        if node.id in self._nodes:
            raise ValueError(f"Node '{node.id}' already exists")
        self._nodes[node.id] = node
        self._out.setdefault(node.id, [])
        self._in.setdefault(node.id, [])
        return self

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        return self._nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def remove_node(self, node_id: str) -> bool:
        if node_id not in self._nodes:
            return False
        self._edges = [
            e for e in self._edges if e.source_id != node_id and e.target_id != node_id
        ]
        self._nodes.pop(node_id, None)
        self._out.pop(node_id, None)
        self._in.pop(node_id, None)
        return True

    def set_property(self, node_id: str, key: str, value: Any) -> "BaseGraph":
        node = self._nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        node.properties[key] = value
        node.bump()
        return self

    @property
    def nodes(self) -> List[GraphNode]:
        return list(self._nodes.values())

    @property
    def node_ids(self) -> List[str]:
        return list(self._nodes.keys())

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    # ── Edge operations ────────────────────────────────────────────────

    def add_edge(self, edge: GraphEdge) -> "BaseGraph":
        if edge.source_id not in self._nodes:
            raise KeyError(f"Source node '{edge.source_id}' not found")
        if edge.target_id not in self._nodes:
            raise KeyError(f"Target node '{edge.target_id}' not found")
        self._edges.append(edge)
        self._out.setdefault(edge.source_id, []).append(edge)
        self._in.setdefault(edge.target_id, []).append(edge)
        return self

    def remove_edge(
        self, source_id: str, target_id: str, relation: Optional[str] = None
    ) -> int:
        removed = 0
        remaining = []
        for e in self._edges:
            if (
                e.source_id == source_id
                and e.target_id == target_id
                and (relation is None or e.relation == relation)
            ):
                removed += 1
            else:
                remaining.append(e)
        if removed:
            self._edges = remaining
            self._rebuild_index()
        return removed

    def get_edges(
        self, source_id: Optional[str] = None, target_id: Optional[str] = None
    ) -> List[GraphEdge]:
        result = []
        for e in self._edges:
            if source_id is not None and e.source_id != source_id:
                continue
            if target_id is not None and e.target_id != target_id:
                continue
            result.append(e)
        return result

    def out_edges(self, node_id: str) -> List[GraphEdge]:
        return list(self._out.get(node_id, []))

    def in_edges(self, node_id: str) -> List[GraphEdge]:
        return list(self._in.get(node_id, []))

    @property
    def edges(self) -> List[GraphEdge]:
        return list(self._edges)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def _rebuild_index(self) -> None:
        self._out.clear()
        self._in.clear()
        for nid in self._nodes:
            self._out.setdefault(nid, [])
            self._in.setdefault(nid, [])
        for e in self._edges:
            self._out.setdefault(e.source_id, []).append(e)
            self._in.setdefault(e.target_id, []).append(e)

    # ── Traversal (shared by every graph) ──────────────────────────────

    def dfs(self, start_id: str) -> List[str]:
        return GraphTraversal.dfs(start_id, self.out_edges)

    def bfs(self, start_id: str) -> List[str]:
        return GraphTraversal.bfs(start_id, self.out_edges)

    def topological_sort(self) -> List[str]:
        return GraphTraversal.topological_sort(self.node_ids, self.out_edges)

    def has_cycle(self) -> bool:
        return GraphTraversal.has_cycle(self.node_ids, self.out_edges)

    def find_cycle(self) -> Optional[List[str]]:
        return GraphTraversal.find_cycle(self.node_ids, self.out_edges)

    def connected_components(self) -> List[List[str]]:
        return GraphTraversal.connected_components(self.node_ids, self.out_edges)

    def reachable_from(self, start_id: str) -> Set[str]:
        return GraphTraversal.reachable([start_id], self.out_edges)

    # ── Graph algebra ──────────────────────────────────────────────────

    def clone(self) -> "BaseGraph":
        return BaseGraph.from_dict(self.to_dict(), kind=self.kind, name=self.name)

    def merge(self, other: "BaseGraph", relation: str = "related") -> "BaseGraph":
        merged = self.clone()
        for n in other.nodes:
            if not merged.has_node(n.id):
                merged.add_node(
                    GraphNode(
                        id=n.id,
                        kind=n.kind,
                        label=n.label,
                        properties=dict(n.properties),
                    )
                )
        for e in other.edges:
            if merged.has_node(e.source_id) and merged.has_node(e.target_id):
                merged.add_edge(
                    GraphEdge(
                        source_id=e.source_id,
                        target_id=e.target_id,
                        relation=e.relation,
                        properties=dict(e.properties),
                    )
                )
        return merged

    def subgraph(self, node_ids: Iterable[str]) -> "BaseGraph":
        keep = set(node_ids)
        sub = BaseGraph(kind=self.kind, name=f"{self.name}.sub")
        for n in self._nodes.values():
            if n.id in keep:
                sub.add_node(
                    GraphNode(
                        id=n.id,
                        kind=n.kind,
                        label=n.label,
                        properties=dict(n.properties),
                        version=n.version,
                    )
                )
        for e in self._edges:
            if e.source_id in keep and e.target_id in keep:
                sub.add_edge(
                    GraphEdge(
                        source_id=e.source_id,
                        target_id=e.target_id,
                        relation=e.relation,
                        properties=dict(e.properties),
                    )
                )
        return sub

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()
        self._out.clear()
        self._in.clear()

    # ── Serialization ──────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges],
        }

    @classmethod
    def from_dict(
        cls, data: dict, kind: Optional[GraphKind] = None, name: Optional[str] = None
    ) -> "BaseGraph":
        graph = cls(
            kind=kind or GraphKind(data.get("kind", GraphKind.CUSTOM.value)),
            name=name if name is not None else data.get("name", ""),
        )
        for nd in data.get("nodes", []):
            graph.add_node(GraphNode.from_dict(nd))
        for ed in data.get("edges", []):
            edge = GraphEdge.from_dict(ed)
            if graph.has_node(edge.source_id) and graph.has_node(edge.target_id):
                graph.add_edge(edge)
        return graph

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, payload: str) -> "BaseGraph":
        return cls.from_dict(json.loads(payload))

    # ── Snapshot / diff ────────────────────────────────────────────────

    def snapshot(self, name: str = "") -> GraphSnapshot:
        return GraphSnapshot.capture(self, name=name)

    def restore(self, snapshot: GraphSnapshot) -> None:
        snapshot.restore_into(self)

    def diff(self, other: "BaseGraph") -> GraphDiff:
        return GraphDiff.between(self, other)

    # ── Visitor ────────────────────────────────────────────────────────

    def accept(self, visitor: GraphVisitor) -> None:
        for n in self._nodes.values():
            visitor.visit_node(n, self)
        for e in self._edges:
            visitor.visit_edge(e, self)


def _node_id_of(node: Any, fallback_key: Optional[str] = None) -> Optional[str]:
    """Extract a stable id from an arbitrary node object."""
    for attr in ("id", "node_id", "task_id"):
        value = getattr(node, attr, None)
        if isinstance(value, str) and value:
            return value
    return fallback_key


def _edge_parts(edge: Any) -> Optional[Tuple[str, str, str]]:
    """Extract (source_id, target_id, relation) from an arbitrary edge."""
    src = getattr(edge, "source_id", None) or getattr(edge, "source", None)
    tgt = getattr(edge, "target_id", None) or getattr(edge, "target", None)
    if not src or not tgt:
        return None
    src, tgt = str(src), str(tgt)
    relation = getattr(edge, "relation", None)
    if relation is None:
        edge_type = getattr(edge, "edge_type", None)
        relation = (
            edge_type.value
            if hasattr(edge_type, "value")
            else (edge_type if isinstance(edge_type, str) else None)
        )
    elif hasattr(relation, "value"):
        relation = relation.value
    return src, tgt, str(relation) if relation else "related"


def adapt(graph: Any, kind: Optional[GraphKind] = None, name: str = "") -> BaseGraph:
    """Wrap any concrete graph object as a unified BaseGraph view.

    Duck-typing rules (zero knowledge of the concrete classes required):
      - nodes are read from ``graph.nodes`` (list or dict); graphs that only
        expose ``_nodes`` are handled as well.
      - node ids come from ``node.id`` / ``node.node_id`` / dict key.
      - edges are read from ``graph.edges`` or ``graph.get_edges()``; graphs
        without an edge list (e.g. ExecutionGraph) fall back to synthesizing
        edges from per-node ``dependencies`` / ``depends_on`` sets.
      - edge endpoints use ``source_id``/``target_id`` (or ``source``/``target``);
        the relation uses ``relation`` or ``edge_type``.
    """
    selected_kind = kind or _infer_kind(graph)
    bg = BaseGraph(
        kind=selected_kind,
        name=name or getattr(graph, "name", "") or type(graph).__name__,
    )

    raw_nodes = getattr(graph, "nodes", None)
    if isinstance(raw_nodes, dict):
        node_items = list(raw_nodes.items())
    elif raw_nodes is None:
        raw_nodes = getattr(graph, "_nodes", None)
        node_items = list(raw_nodes.items()) if isinstance(raw_nodes, dict) else []
    else:
        node_items = [(None, n) for n in raw_nodes]

    for key, node in node_items:
        nid = _node_id_of(node, fallback_key=key)
        if not nid:
            continue
        label = getattr(node, "label", None) or getattr(node, "text", None) or ""
        props = dict(getattr(node, "metadata", {}) or {})
        props["__class__"] = type(node).__name__
        state = getattr(node, "state", None)
        if state is not None:
            props["state"] = state.value if hasattr(state, "value") else str(state)
        bg.add_node(
            GraphNode(
                id=str(nid), kind=selected_kind, label=str(label), properties=props
            )
        )

    edges = getattr(graph, "edges", None)
    if edges is None and hasattr(graph, "get_edges"):
        try:
            edges = graph.get_edges()
        except Exception:  # pragma: no cover - defensive
            edges = None
    if edges is None:
        # Graphs storing edges in an internal ``_edges`` dict (ConstraintGraph).
        internal = getattr(graph, "_edges", None)
        if isinstance(internal, dict) and internal:
            edges = [
                e
                for e in internal.values()
                if getattr(e, "source_id", None) and getattr(e, "target_id", None)
            ]
    if edges is None:
        # Synthesize edges from dependency fields (ExecutionGraph etc.).
        edges = []
        for key, node in node_items:
            nid = _node_id_of(node, fallback_key=key)
            if not nid:
                continue
            deps = getattr(node, "dependencies", None) or getattr(
                node, "depends_on", None
            )
            if deps:
                for dep in deps:
                    edges.append(
                        GraphEdge(
                            source_id=str(dep),
                            target_id=str(nid),
                            relation="depends_on",
                        )
                    )
    for e in edges:
        parts = _edge_parts(e)
        if parts is None and isinstance(e, GraphEdge):
            parts = (e.source_id, e.target_id, e.relation)
        if parts is None:
            continue
        src, tgt, rel = parts
        if bg.has_node(src) and bg.has_node(tgt):
            bg.add_edge(GraphEdge(source_id=src, target_id=tgt, relation=rel))
    return bg


def _infer_kind(graph: Any) -> GraphKind:
    name = type(graph).__name__.lower()
    if "document" in name:
        return GraphKind.DOCUMENT
    if "execution" in name:
        return GraphKind.EXECUTION
    if "constraint" in name or "layout" in name:
        return GraphKind.CONSTRAINT
    if "knowledge" in name or "memory" in name or "entity" in name:
        return GraphKind.KNOWLEDGE
    if "diagnostic" in name or "issue" in name:
        return GraphKind.DIAGNOSTIC
    if "workflow" in name:
        return GraphKind.WORKFLOW
    if "task" in name:
        return GraphKind.TASK
    return GraphKind.CUSTOM


__all__ = [
    "GraphKind",
    "GraphNode",
    "GraphEdge",
    "GraphProperty",
    "GraphTraversal",
    "GraphVisitor",
    "GraphDiff",
    "GraphSnapshot",
    "BaseGraph",
    "adapt",
]
