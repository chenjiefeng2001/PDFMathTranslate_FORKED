"""Module: V6.0 Constraint Layout Engine — Constraint Graph.

A proper constraint graph system that replaces the rule-based ParagraphLayout.
Models layout elements as nodes with constraint edges (must_above, must_below,
cannot_overlap, align_left, center, keep_together, same_page).

Inspired by Cassowary / Kiwi constraint solving, but operating at the
document layout level rather than UI widget level.

Usage:
    from pdf2zh.v3.constraint_graph import ConstraintGraph, ConstraintEdge, \
        ConstraintPriority, LayoutNode, ConstraintSolver
    cg = ConstraintGraph()
    a = cg.add_node("para1", "paragraph", bbox=BoundingBox(...))
    b = cg.add_node("fig1", "figure", bbox=BoundingBox(...))
    cg.add_edge(a.id, b.id, relation="must_below", priority=ConstraintPriority.HARD, gap=10.0)
    solver = ConstraintSolver(cg)
    solver.solve()
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from pdf2zh.v3.visual_tree import BoundingBox

logger = logging.getLogger(__name__)


class ConstraintPriority(Enum):
    HARD = "hard"          # Must be satisfied
    SOFT = "soft"          # Should be satisfied if possible
    PREFERRED = "preferred"  # Nice to have
    STRONG = "strong"      # Between hard and soft


class ConstraintRelation(Enum):
    MUST_ABOVE = "must_above"
    MUST_BELOW = "must_below"
    MUST_LEFT = "must_left"
    MUST_RIGHT = "must_right"
    CANNOT_OVERLAP = "cannot_overlap"
    ALIGN_LEFT = "align_left"
    ALIGN_RIGHT = "align_right"
    ALIGN_TOP = "align_top"
    ALIGN_BOTTOM = "align_bottom"
    CENTER_X = "center_x"
    CENTER_Y = "center_y"
    SAME_WIDTH = "same_width"
    SAME_HEIGHT = "same_height"
    KEEP_TOGETHER = "keep_together"
    SAME_PAGE = "same_page"
    KEEP_WITH_NEXT = "keep_with_next"
    FLOAT = "float"


@dataclass
class ConstraintEdge:
    source_id: str
    target_id: str
    relation: ConstraintRelation
    priority: ConstraintPriority = ConstraintPriority.HARD
    gap: float = 0.0
    weight: float = 1.0
    enabled: bool = True
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.relation, str):
            self.relation = ConstraintRelation(self.relation)
        if isinstance(self.priority, str):
            self.priority = ConstraintPriority(self.priority)


@dataclass
class LayoutNode:
    id: str
    node_type: str  # paragraph, figure, table, caption, equation, heading, etc.
    bbox: BoundingBox = field(default_factory=BoundingBox)
    min_width: float = 0.0
    min_height: float = 0.0
    preferred_width: float = 0.0
    preferred_height: float = 0.0
    max_width: float = 99999.0
    max_height: float = 99999.0
    fixed: bool = False
    page_num: int = 0
    column_index: int = 0
    z_index: int = 0
    metadata: dict = field(default_factory=dict)
    _resolved: bool = False
    _resolved_bbox: Optional[BoundingBox] = None

    @property
    def resolved_bbox(self) -> BoundingBox:
        return self._resolved_bbox or self.bbox

    @resolved_bbox.setter
    def resolved_bbox(self, value: BoundingBox) -> None:
        self._resolved_bbox = value
        self._resolved = True

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    def reset(self) -> None:
        self._resolved = False
        self._resolved_bbox = None


class ConstraintGraph:
    """A constraint graph for document layout.

    Models layout elements as nodes with constraint edges between them.
    Supports all common layout relationships found in academic papers.
    """

    def __init__(self) -> None:
        self._nodes: Dict[str, LayoutNode] = {}
        self._edges: Dict[str, ConstraintEdge] = {}
        self._edge_counter: int = 0
        self._adjacency: Dict[str, Set[str]] = {}  # node_id -> set of edge_ids

    def add_node(self, node_id: str, node_type: str, *,
                 bbox: Optional[BoundingBox] = None,
                 min_width: float = 0.0, min_height: float = 0.0,
                 preferred_width: float = 0.0, preferred_height: float = 0.0,
                 max_width: float = 99999.0, max_height: float = 99999.0,
                 page_num: int = 0, column_index: int = 0,
                 fixed: bool = False, **metadata) -> LayoutNode:
        if node_id in self._nodes:
            raise ValueError(f"LayoutNode '{node_id}' already exists")
        n = LayoutNode(
            id=node_id, node_type=node_type,
            bbox=bbox or BoundingBox(),
            min_width=min_width, min_height=min_height,
            preferred_width=preferred_width or (bbox.width if bbox else 0.0),
            preferred_height=preferred_height or (bbox.height if bbox else 0.0),
            max_width=max_width, max_height=max_height,
            page_num=page_num, column_index=column_index,
            fixed=fixed, metadata=metadata,
        )
        self._nodes[node_id] = n
        self._adjacency.setdefault(node_id, set())
        return n

    def get_node(self, node_id: str) -> Optional[LayoutNode]:
        return self._nodes.get(node_id)

    def remove_node(self, node_id: str) -> None:
        if node_id not in self._nodes:
            return
        # Remove all edges connected to this node
        affected_edges = list(self._adjacency.get(node_id, set()))
        for eid in affected_edges:
            self.remove_edge(eid)
        self._nodes.pop(node_id, None)
        self._adjacency.pop(node_id, None)

    def add_edge(self, source_id: str, target_id: str, relation: str,
                 priority: str = "hard", gap: float = 0.0,
                 weight: float = 1.0, **metadata) -> ConstraintEdge:
        if source_id not in self._nodes:
            raise ValueError(f"Source node '{source_id}' not found")
        if target_id not in self._nodes:
            raise ValueError(f"Target node '{target_id}' not found")
        self._edge_counter += 1
        eid = f"ce_{self._edge_counter}"
        e = ConstraintEdge(
            source_id=source_id, target_id=target_id,
            relation=ConstraintRelation(relation),
            priority=ConstraintPriority(priority),
            gap=gap, weight=weight, metadata=metadata,
        )
        self._edges[eid] = e
        self._adjacency.setdefault(source_id, set()).add(eid)
        self._adjacency.setdefault(target_id, set()).add(eid)
        return e

    def get_edge(self, edge_id: str) -> Optional[ConstraintEdge]:
        return self._edges.get(edge_id)

    def remove_edge(self, edge_id: str) -> None:
        e = self._edges.pop(edge_id, None)
        if e is None:
            return
        for nid in (e.source_id, e.target_id):
            if nid in self._adjacency:
                self._adjacency[nid].discard(edge_id)

    def get_edges_for_node(self, node_id: str) -> List[ConstraintEdge]:
        eids = self._adjacency.get(node_id, set())
        return [self._edges[eid] for eid in eids if eid in self._edges]

    def get_outgoing_edges(self, node_id: str) -> List[ConstraintEdge]:
        result = []
        for e in self.get_edges_for_node(node_id):
            if e.source_id == node_id:
                result.append(e)
        return result

    def get_incoming_edges(self, node_id: str) -> List[ConstraintEdge]:
        result = []
        for e in self.get_edges_for_node(node_id):
            if e.target_id == node_id:
                result.append(e)
        return result

    @property
    def nodes(self) -> List[LayoutNode]:
        return list(self._nodes.values())

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edges(self) -> List[ConstraintEdge]:
        return list(self._edges.values())

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def get_nodes_by_type(self, node_type: str) -> List[LayoutNode]:
        return [n for n in self._nodes.values() if n.node_type == node_type]

    def get_nodes_on_page(self, page_num: int) -> List[LayoutNode]:
        return [n for n in self._nodes.values() if n.page_num == page_num]

    def topological_sort(self) -> List[LayoutNode]:
        """Sort nodes by their MUST_BELOW / MUST_ABOVE constraints."""
        in_degree = {nid: 0 for nid in self._nodes}
        adj = {nid: [] for nid in self._nodes}
        for e in self._edges.values():
            if e.relation == ConstraintRelation.MUST_BELOW and e.enabled:
                adj[e.source_id].append(e.target_id)
                in_degree[e.target_id] = in_degree.get(e.target_id, 0) + 1
            elif e.relation == ConstraintRelation.MUST_ABOVE and e.enabled:
                adj[e.target_id].append(e.source_id)
                in_degree[e.source_id] = in_degree.get(e.source_id, 0) + 1
        queue = [nid for nid, d in in_degree.items() if d == 0]
        result = []
        while queue:
            nid = queue.pop(0)
            result.append(self._nodes[nid])
            for neighbor in adj[nid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        remaining = [self._nodes[nid] for nid in self._nodes if nid not in {n.id for n in result}]
        result.extend(remaining)
        return result

    def has_cycle(self) -> bool:
        """Check if the constraint graph has a cycle (infeasible layout)."""
        return len(self.topological_sort()) < len(self._nodes)

    def find_conflicting_constraints(self) -> List[ConstraintEdge]:
        """Find constraints that may conflict (e.g., A above B and B above A)."""
        conflicts = []
        processed = set()
        for eid, e in self._edges.items():
            key = (e.source_id, e.target_id, e.relation)
            if key in processed:
                continue
            # For directional constraints, find opposite-direction conflict
            opposite_relations = {
                ConstraintRelation.MUST_ABOVE: ConstraintRelation.MUST_ABOVE,
                ConstraintRelation.MUST_BELOW: ConstraintRelation.MUST_BELOW,
                ConstraintRelation.MUST_LEFT: ConstraintRelation.MUST_LEFT,
                ConstraintRelation.MUST_RIGHT: ConstraintRelation.MUST_RIGHT,
            }
            if e.relation in opposite_relations:
                for eid2, e2 in self._edges.items():
                    if eid2 == eid:
                        continue
                    # Conflict: A must_above B AND B must_above A (same relation, swapped)
                    if (e2.source_id == e.target_id and e2.target_id == e.source_id
                            and e2.relation == e.relation):
                        conflict_key = tuple(sorted([(e.source_id, e.target_id, e.relation.value),
                                                      (e.target_id, e.source_id, e2.relation.value)]))
                        if conflict_key not in processed:
                            conflicts.append(e)
                            conflicts.append(e2)
                            processed.add(conflict_key)
        return conflicts

    def reset_all(self) -> None:
        for n in self._nodes.values():
            n.reset()

    def to_dict(self) -> dict:
        return {
            "nodes": {nid: {"type": n.node_type, "bbox": {"x": n.bbox.x, "y": n.bbox.y,
                                                           "w": n.bbox.width, "h": n.bbox.height},
                            "page": n.page_num, "fixed": n.fixed}
                      for nid, n in self._nodes.items()},
            "edges": {eid: {"source": e.source_id, "target": e.target_id,
                            "relation": e.relation.value, "priority": e.priority.value,
                            "gap": e.gap}
                      for eid, e in self._edges.items()},
        }


class ConstraintSolver:
    """Simplified constraint solver for layout constraint graphs.

    Uses a greedy approach:
    1. Topological sort for ordering constraints
    2. Apply HARD constraints first
    3. Apply SOFT constraints (if they don't conflict)
    4. Resolve overlaps by pushing elements down/right
    """

    def __init__(self, graph: ConstraintGraph, page_width: float = 612.0,
                 page_height: float = 792.0) -> None:
        self.graph = graph
        self.page_width = page_width
        self.page_height = page_height
        self._solved = False

    def solve(self) -> bool:
        """Run the constraint solving algorithm."""
        self.graph.reset_all()
        # 1. Sort topologically for ordering
        ordered = self.graph.topological_sort()
        nodes_map = {n.id: n for n in ordered}

        # 2. Collect constraints grouped by priority
        hard_edges = [e for e in self.graph.edges
                      if e.priority == ConstraintPriority.HARD and e.enabled]
        soft_edges = [e for e in self.graph.edges
                      if e.priority == ConstraintPriority.SOFT and e.enabled]
        preferred_edges = [e for e in self.graph.edges
                           if e.priority == ConstraintPriority.PREFERRED and e.enabled]
        strong_edges = [e for e in self.graph.edges
                        if e.priority == ConstraintPriority.STRONG and e.enabled]

        # 3. Apply ALL edges (not just hard) in priority order
        for edges in [hard_edges, strong_edges, soft_edges, preferred_edges]:
            self._apply_constraints(edges, nodes_map)

        # 4. Resolve remaining overlaps
        self._resolve_overlaps(nodes_map)

        self._solved = True
        return True

    def _apply_constraints(self, edges: List[ConstraintEdge],
                           nodes_map: Dict[str, LayoutNode]) -> None:
        for e in edges:
            source = nodes_map.get(e.source_id)
            target = nodes_map.get(e.target_id)
            if source is None or target is None:
                continue
            sb = source.resolved_bbox
            tb = target.resolved_bbox

            if e.relation == ConstraintRelation.MUST_ABOVE:
                if sb.y + sb.height + e.gap > tb.y:
                    tb = BoundingBox(tb.x, sb.y + sb.height + e.gap,
                                     tb.width, tb.height)
                    target.resolved_bbox = tb

            elif e.relation == ConstraintRelation.MUST_BELOW:
                if tb.y + tb.height + e.gap > sb.y:
                    sb = BoundingBox(sb.x, tb.y + tb.height + e.gap,
                                     sb.width, sb.height)
                    source.resolved_bbox = sb

            elif e.relation == ConstraintRelation.MUST_LEFT:
                if sb.x + sb.width + e.gap > tb.x:
                    tb = BoundingBox(sb.x + sb.width + e.gap, tb.y,
                                     tb.width, tb.height)
                    target.resolved_bbox = tb

            elif e.relation == ConstraintRelation.MUST_RIGHT:
                if tb.x + tb.width + e.gap > sb.x:
                    sb = BoundingBox(tb.x + tb.width + e.gap, sb.y,
                                     sb.width, sb.height)
                    source.resolved_bbox = sb

            elif e.relation == ConstraintRelation.ALIGN_LEFT:
                tb = BoundingBox(sb.x, tb.y, tb.width, tb.height)
                target.resolved_bbox = tb

            elif e.relation == ConstraintRelation.ALIGN_TOP:
                tb = BoundingBox(tb.x, sb.y, tb.width, tb.height)
                target.resolved_bbox = tb

            elif e.relation == ConstraintRelation.CENTER_X:
                center = sb.x + sb.width / 2
                tb = BoundingBox(center - tb.width / 2, tb.y,
                                 tb.width, tb.height)
                target.resolved_bbox = tb

    def _resolve_overlaps(self, nodes_map: Dict[str, LayoutNode]) -> None:
        """Push overlapping elements down/right to resolve collisions."""
        node_list = list(nodes_map.values())
        changed = True
        max_iterations = 10
        iteration = 0
        while changed and iteration < max_iterations:
            changed = False
            iteration += 1
            for i in range(len(node_list)):
                for j in range(i + 1, len(node_list)):
                    a, b = node_list[i], node_list[j]
                    if a.page_num != b.page_num:
                        continue
                    ab = a.resolved_bbox
                    bb = b.resolved_bbox
                    if ab.overlaps(bb):
                        overlap_y = min(ab.y1, bb.y1) - max(ab.y, bb.y)
                        overlap_x = min(ab.x1, bb.x1) - max(ab.x, bb.x)
                        if overlap_y < overlap_x and overlap_y > 2:
                            # Push the lower element down
                            if ab.y < bb.y:
                                bb = BoundingBox(bb.x, ab.y1 + 2, bb.width, bb.height)
                            else:
                                ab = BoundingBox(ab.x, bb.y1 + 2, ab.width, ab.height)
                        elif overlap_x > 2:
                            if ab.x < bb.x:
                                bb = BoundingBox(ab.x1 + 2, bb.y, bb.width, bb.height)
                            else:
                                ab = BoundingBox(bb.x1 + 2, ab.y, ab.width, ab.height)
                        else:
                            continue
                        a.resolved_bbox = ab
                        b.resolved_bbox = bb
                        changed = True

    @property
    def solved(self) -> bool:
        return self._solved

    def get_layout_result(self) -> Dict[str, BoundingBox]:
        return {n.id: n.resolved_bbox for n in self.graph.nodes}


def build_constraint_graph_from_document(graph, doc_graph) -> ConstraintGraph:
    """Build a ConstraintGraph from a DocumentGraph.

    Args:
        graph: The ConstraintGraph to populate
        doc_graph: DocumentGraph with semantic analysis results

    Returns:
        Populated ConstraintGraph with layout constraints inferred from semantics
    """
    from pdf2zh.v3.graph import NodeType, EdgeType

    for node in doc_graph.nodes:
        if node.node_type in (NodeType.DOCUMENT, NodeType.PAGE):
            continue
        ntype = _map_node_type(node.node_type)
        graph.add_node(
            node.id, ntype,
            bbox=BoundingBox(node.x0, node.y0,
                             node.width, node.height),
            page_num=node.page_num,
            preferred_height=node.height,
        )

    for edge in doc_graph.edges:
        if edge.edge_type == EdgeType.FOLLOWS:
            if (graph.get_node(edge.source_id) is not None
                    and graph.get_node(edge.target_id) is not None):
                graph.add_edge(edge.source_id, edge.target_id,
                              "must_below", priority="soft", gap=2.0)
        elif edge.edge_type == EdgeType.CONTAINS:
            if (graph.get_node(edge.source_id) is not None
                    and graph.get_node(edge.target_id) is not None):
                graph.add_edge(edge.source_id, edge.target_id,
                              "keep_together", priority="soft")

    # Infer figure-caption constraints
    figures = doc_graph.get_nodes_by_type(NodeType.FIGURE)
    for fig in figures:
        for e in doc_graph.get_edges(source_id=fig.id):
            if e.edge_type == EdgeType.CAPTION_OF:
                target = doc_graph.get_node(e.target_id)
                if target and target.node_type == NodeType.CAPTION:
                    graph.add_edge(fig.id, target.id,
                                  "must_below", priority="hard", gap=3.0)

    return graph


def _map_node_type(nt) -> str:
    mapping = {
        "paragraph": "paragraph", "heading": "heading", "caption": "caption",
        "figure": "figure", "table": "table", "formula": "equation",
        "footer": "footer", "header": "header", "footnote": "footnote",
        "reference": "reference", "abstract": "abstract",
        "code": "code", "list": "list", "toc": "toc",
    }
    name = nt.value if hasattr(nt, 'value') else str(nt).lower()
    return mapping.get(name, "paragraph")


__all__ = [
    "ConstraintPriority", "ConstraintRelation", "ConstraintEdge",
    "LayoutNode", "ConstraintGraph", "ConstraintSolver",
    "build_constraint_graph_from_document",
]
