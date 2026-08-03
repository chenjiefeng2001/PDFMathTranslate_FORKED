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
    """Six-band constraint priority ladder (report P3).

    The first four members are the original strength bands; the last three
    are the semantic/advisory bands added by the P3 requirement
    "+TYPOGRAPHY/READING/SEMANTIC". Lower ``rank()`` values bind harder.

        HARD        0  must be satisfied
        STRONG      1  nearly mandatory (structural ordering)
        SOFT        2  should be satisfied if possible (reading order)
        PREFERRED   3  nice to have (whitespace / paragraph spacing)
        TYPOGRAPHY  4  typography guidance (baseline / line-height)
        READING     5  reading-order preference (weak hint)
        SEMANTIC    6  semantic association preference (weakest)
    """

    HARD = "hard"
    STRONG = "strong"
    SOFT = "soft"
    PREFERRED = "preferred"
    TYPOGRAPHY = "typography"
    READING = "reading"
    SEMANTIC = "semantic"

    def rank(self) -> int:
        """0 (HARD) .. 6 (SEMANTIC); lower ranks bind harder."""
        return _PRIORITY_RANK[self]

    @property
    def is_advisory(self) -> bool:
        """True for the three advisory semantic bands."""
        return self.rank() >= _PRIORITY_RANK[ConstraintPriority.TYPOGRAPHY]


_PRIORITY_RANK: Dict["ConstraintPriority", int] = {
    ConstraintPriority.HARD: 0,
    ConstraintPriority.STRONG: 1,
    ConstraintPriority.SOFT: 2,
    ConstraintPriority.PREFERRED: 3,
    ConstraintPriority.TYPOGRAPHY: 4,
    ConstraintPriority.READING: 5,
    ConstraintPriority.SEMANTIC: 6,
}


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


class KiwiSolver:
    """Weighted-stay linear constraint solver in the spirit of Kiwi / Cassowary.

    Solves the vertical (y) axis of a layout with three kinds of constraints:

        stays         — minimize Σ wᵢ·(yᵢ − preferredᵢ)²        (elasticity)
        inequalities  — y_below ≥ y_above + height_above + gap   (must_below)
        equalities    — y_b == y_a + offset                      (align / center)

    Constraints are processed in priority tiers (HARD first ... SEMANTIC last),
    so a weaker band can never break a stronger one. Feasibility is reached by
    deterministic Gauss–Seidel projection; stays are then relaxed toward the
    preferred positions without violating the hard inequalities (the classic
    "push/pull" behaviour of Cassowary-class solvers). No external deps.

    Usage::

        ks = KiwiSolver()
        ks.add_variable("a", preferred=50.0, height=30.0)
        ks.add_variable("b", preferred=100.0, height=30.0)
        ks.add_inequality(below="b", above="a", gap=10.0)
        y = ks.solve()          # {"a": 50.0, "b": 90.0}
    """

    def __init__(self, max_iterations: int = 300,
                 tolerance: float = 1e-3,
                 tier_max_iterations: int = 120) -> None:
        self.max_iterations = max_iterations
        self.tolerance = tolerance
        self.tier_max_iterations = tier_max_iterations
        self._variables: Dict[str, Dict[str, float]] = {}
        self._inequalities: List[Dict[str, Any]] = []
        self._equalities: List[Dict[str, Any]] = []
        self._fixed_y: Dict[str, float] = {}

    # ── Variable declaration ──────────────────────────────────────────

    def add_variable(self, name: str, preferred: float = 0.0,
                     weight: float = 1.0, lower: float = 0.0,
                     upper: float = 1e9, height: float = 0.0,
                     fixed: bool = False) -> None:
        """Register a vertical variable (y of the box top)."""
        self._variables[name] = {
            "preferred": float(preferred),
            "weight": float(weight),
            "lower": float(lower),
            "upper": float(upper),
            "height": float(height),
            "y": float(preferred),
            "fixed": bool(fixed),
        }

    def add_inequality(self, below: str, above: str,
                       gap: float = 0.0, priority: str = "soft",
                       weight: float = 1.0) -> None:
        """Require ``y_below ≥ y_above + height_above + gap``."""
        rank = self._rank(priority)
        self._inequalities.append({
            "below": below, "above": above, "gap": float(gap),
            "rank": rank, "weight": float(weight),
        })

    def add_equality(self, a: str, b: str, offset: float = 0.0,
                     priority: str = "soft", weight: float = 1.0) -> None:
        """Require ``y_b == y_a + offset`` (align_top / align_bottom / center)."""
        self._equalities.append({
            "a": a, "b": b, "offset": float(offset),
            "rank": self._rank(priority), "weight": float(weight),
        })

    @staticmethod
    def _rank(priority: Any) -> int:
        if isinstance(priority, ConstraintPriority):
            return priority.rank()
        try:
            return ConstraintPriority(str(priority)).rank()
        except ValueError:
            return ConstraintPriority.SOFT.rank()

    # ── Solve ─────────────────────────────────────────────────────────

    def solve(self) -> Dict[str, float]:
        """Run feasibility + stay relaxation and return ``{name: y}``."""
        # Phase A — feasibility, tier by tier (hard bands bind first).
        tiers = sorted({ine["rank"] for ine in self._inequalities})
        for tier in tiers:
            active = [ine for ine in self._inequalities if ine["rank"] <= tier]
            for _ in range(self.tier_max_iterations):
                changed = self._project_inequalities(active)
                if not changed:
                    break
        self._apply_equalities()
        # Re-establish feasibility disturbed by equality assignment.
        for _ in range(self.tier_max_iterations):
            if not self._project_inequalities(self._inequalities):
                break
        # Phase B — stay relaxation (elastic pull toward preferred positions).
        self._relax_stays()
        return {name: var["y"] for name, var in self._variables.items()}

    def get_y(self, name: str) -> float:
        return self._variables[name]["y"]

    # ── Internals ─────────────────────────────────────────────────────

    def _project_inequalities(self, active: List[Dict[str, Any]]) -> bool:
        changed = False
        for ine in active:
            below = self._variables.get(ine["below"])
            above = self._variables.get(ine["above"])
            if below is None or above is None or below["fixed"]:
                continue
            required = above["y"] + above["height"] + ine["gap"]
            if below["y"] < required - self.tolerance:
                below["y"] = min(required, below["upper"])
                changed = True
        return changed

    def _apply_equalities(self) -> None:
        for eq in self._equalities:
            a = self._variables.get(eq["a"])
            b = self._variables.get(eq["b"])
            if a is None or b is None or b["fixed"]:
                continue
            target = a["y"] + eq["offset"]
            if eq["rank"] <= ConstraintPriority.STRONG.rank():
                # Strong alignment: snap the follower onto the anchor.
                b["y"] = target
            else:
                # Soft alignment: average so both sides yield slightly.
                b["y"] = (b["y"] + target) / 2.0
            b["y"] = max(b["lower"], min(b["upper"], b["y"]))

    def _relax_stays(self) -> None:
        """Elastic relaxation toward preferred positions (Kiwi stays).

        Only the hard inequalities (HARD / STRONG) constrain the window the
        stays may move in, so soft / advisory bands never lock the layout.
        """
        hard = [ine for ine in self._inequalities
                if ine["rank"] <= ConstraintPriority.STRONG.rank()]
        ordered = sorted(self._variables.items(),
                         key=lambda kv: -kv[1]["weight"])
        for name, var in ordered:
            if var["fixed"]:
                continue
            max_increase = var["upper"] - var["y"]
            max_decrease = var["y"] - var["lower"]
            for ine in hard:
                if ine["below"] == name:
                    required = (self._variables[ine["above"]]["y"]
                                + self._variables[ine["above"]]["height"]
                                + ine["gap"])
                    max_decrease = min(max_decrease, var["y"] - required)
                if ine["above"] == name:
                    upper = (self._variables[ine["below"]]["y"]
                             - var["height"] - ine["gap"])
                    max_increase = min(max_increase, upper - var["y"])
            max_increase = max(0.0, max_increase)
            max_decrease = max(0.0, max_decrease)
            delta = var["preferred"] - var["y"]
            var["y"] += max(-max_decrease, min(max_increase, delta))


class ConstraintSolver:
    """Constraint solver for layout constraint graphs (P3 Kiwi upgrade).

    Default ``engine="auto"`` uses the weighted-stay Kiwi solver for the
    vertical axis (elastic push/pull), then the horizontal pass and a final
    overlap-resolution safety net. ``engine="greedy"`` preserves the original
    greedy behaviour for debugging / A-B comparison.
    """

    def __init__(self, graph: ConstraintGraph, page_width: float = 612.0,
                 page_height: float = 792.0) -> None:
        self.graph = graph
        self.page_width = page_width
        self.page_height = page_height
        self._solved = False

    def solve(self, engine: str = "auto") -> bool:
        """Run the constraint solving algorithm.

        Args:
            engine: "auto" / "kiwi" → weighted-stay Kiwi vertical solve plus
                    horizontal pass and overlap safety net (P3 default);
                    "greedy" → the original priority-grouped greedy pass.
        """
        self.graph.reset_all()
        if engine in ("auto", "kiwi"):
            return self._solve_kiwi()
        return self._solve_greedy()

    def _solve_kiwi(self) -> bool:
        """Kiwi-style vertical solve with elastic stays (report P3)."""
        ordered = self.graph.topological_sort()
        nodes_map = {n.id: n for n in ordered}
        for page in sorted({n.page_num for n in nodes_map.values()}):
            page_nodes = [n for n in ordered if n.page_num == page]
            ks = KiwiSolver()
            free = {n.id for n in page_nodes
                    if not self.graph.get_edges_for_node(n.id)}
            for n in page_nodes:
                ks.add_variable(
                    n.id, preferred=n.bbox.y,
                    weight=2.0 if n.id in free else 1.0,
                    lower=0.0,
                    upper=max(0.0, self.page_height - n.bbox.height),
                    height=n.bbox.height, fixed=n.fixed)
            for e in self.graph.edges:
                if not e.enabled or e.priority.is_advisory:
                    # Advisory bands feed the stays only; they do not take
                    # part in the feasibility projection.
                    continue
                r = e.relation
                if r in (ConstraintRelation.MUST_BELOW,
                         ConstraintRelation.MUST_ABOVE):
                    below, above = ((e.target_id, e.source_id)
                                    if r == ConstraintRelation.MUST_BELOW
                                    else (e.source_id, e.target_id))
                    ks.add_inequality(below=below, above=above,
                                      gap=e.gap, priority=e.priority)
                elif r == ConstraintRelation.ALIGN_TOP:
                    ks.add_equality(a=e.source_id, b=e.target_id,
                                    priority=e.priority)
                elif r == ConstraintRelation.ALIGN_BOTTOM:
                    src = nodes_map.get(e.source_id)
                    tgt = nodes_map.get(e.target_id)
                    if src is not None and tgt is not None:
                        ks.add_equality(a=e.source_id, b=e.target_id,
                                        offset=src.bbox.height - tgt.bbox.height,
                                        priority=e.priority)
                elif r == ConstraintRelation.CENTER_Y:
                    tgt = nodes_map.get(e.target_id)
                    if tgt is not None:
                        ks.add_variable(f"{e.target_id}__center",
                                        preferred=0.0, fixed=True,
                                        upper=self.page_height)
                        ks.add_equality(a=f"{e.target_id}__center",
                                        b=e.target_id,
                                        offset=(self.page_height
                                                - tgt.bbox.height) / 2.0,
                                        priority=e.priority)
                elif r == ConstraintRelation.CANNOT_OVERLAP:
                    src = nodes_map.get(e.source_id)
                    tgt = nodes_map.get(e.target_id)
                    if (src is not None and tgt is not None
                            and src.page_num == tgt.page_num):
                        if src.bbox.y <= tgt.bbox.y:
                            ks.add_inequality(below=e.target_id,
                                              above=e.source_id, gap=2.0,
                                              priority=e.priority)
                        else:
                            ks.add_inequality(below=e.source_id,
                                              above=e.target_id, gap=2.0,
                                              priority=e.priority)
                elif r == ConstraintRelation.KEEP_WITH_NEXT:
                    ks.add_inequality(below=e.target_id, above=e.source_id,
                                      gap=0.0, priority="soft")
                # Horizontal relations (MUST_LEFT / MUST_RIGHT / ALIGN_LEFT /
                # CENTER_X / SAME_*) are applied by the horizontal pass below.
            if ks._variables:
                for nid, y in ks.solve().items():
                    n = nodes_map.get(nid)
                    if n is not None:
                        n.resolved_bbox = BoundingBox(
                            n.bbox.x, y, n.bbox.width, n.bbox.height)
        # Horizontal pass + overlap safety net (shared with greedy engine).
        # Vertical relations were already resolved by the Kiwi stage above, so
        # re-applying them here would corrupt the elastic solution (the legacy
        # pass applies MUST_BELOW with inverse push semantics).
        horizontal = (ConstraintRelation.MUST_LEFT, ConstraintRelation.MUST_RIGHT,
                      ConstraintRelation.ALIGN_LEFT, ConstraintRelation.ALIGN_RIGHT,
                      ConstraintRelation.CENTER_X)
        self._apply_constraints(
            [e for e in self.graph.edges if e.relation in horizontal], nodes_map)
        self._resolve_overlaps(nodes_map)
        self._solved = True
        return True
        self.graph.reset_all()
    def _solve_greedy(self) -> bool:
        """Original greedy solve (priority-grouped application)."""
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
    "LayoutNode", "ConstraintGraph", "ConstraintSolver", "KiwiSolver",
    "build_constraint_graph_from_document",
]
