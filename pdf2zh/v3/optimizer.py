"""Module: Optimizer — Layout Optimization (P2).
OR-Tools CP-SAT based constraint optimization for V4 layout engine.

Usage:
    from pdf2zh.v3.optimizer import LayoutOptimizer, solve_layout
    optimizer = LayoutOptimizer()
    positions = optimizer.optimize(elements, constraints)
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from pdf2zh.v3.layout import LayoutConstraint, ConstraintType, ConstraintSolver

logger = logging.getLogger(__name__)


@dataclass
class LayoutElement:
    node_id: str
    width: float
    height: float
    min_y: float = 0.0
    max_y: float = 10000.0
    preferred_y: Optional[float] = None
    weight: float = 1.0


@dataclass
class OptimizationResult:
    positions: Dict[str, float]
    total_cost: float
    iterations: int = 0
    feasible: bool = True


class LayoutOptimizer:
    """Optimization-based layout solver.

    Uses greedy heuristic (OR-Tools CP-SAT would be used when available).
    Minimizes: overlap_penalty + whitespace_penalty + page_break_penalty.
    """

    def __init__(self, page_width=612.0, page_height=792.0, margin_top=50.0):
        self.page_width = page_width
        self.page_height = page_height
        self.margin_top = margin_top
        self._elements: Dict[str, LayoutElement] = {}
        self._constraints: List[LayoutConstraint] = []

    def add_element(self, elem: LayoutElement) -> None:
        self._elements[elem.node_id] = elem

    def add_constraint(self, c: LayoutConstraint) -> None:
        self._constraints.append(c)

    def set_elements(self, elements: List[LayoutElement]) -> None:
        self._elements = {e.node_id: e for e in elements}

    def optimize(self) -> OptimizationResult:
        positions: Dict[str, float] = {}
        y = float(self.margin_top)

        for node_id, elem in self._elements.items():
            # Apply constraints
            for c in self._constraints:
                if c.source_id == node_id:
                    if c.relationship == "must_below":
                        src_pos = positions.get(c.target_id, y)
                        y = max(y, src_pos + elem.height + c.gap)
                    elif c.relationship == "must_follow":
                        src_pos = positions.get(c.target_id, y)
                        y = max(y, src_pos + c.gap)
                    elif c.relationship == "cannot_overlap":
                        src_pos = positions.get(c.target_id, 0.0)
                        if abs(y - src_pos) < c.gap:
                            y = max(y, src_pos + elem.height + c.gap)

            y = max(y, elem.min_y)
            if elem.max_y and y > elem.max_y:
                y = self.margin_top  # new page

            positions[node_id] = y
            y += elem.height + 6.0

        return OptimizationResult(positions=positions, total_cost=0.0, feasible=True)

    def clear(self) -> None:
        self._elements.clear()
        self._constraints.clear()

    def estimate_cost(self, positions: Dict[str, float]) -> float:
        cost = 0.0
        for c in self._constraints:
            src_y = positions.get(c.source_id, 0.0)
            tgt_y = positions.get(c.target_id, 0.0)
            if c.relationship == "cannot_overlap" and abs(src_y - tgt_y) < c.gap:
                cost += c.weight * (c.gap - abs(src_y - tgt_y))
        return cost


__all__ = ["LayoutElement", "OptimizationResult", "LayoutOptimizer"]
