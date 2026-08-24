"""Module: V6.0 Relayout Engine — unified reflow / float / overlay driver.

Implements the report's "约束布局求解" (constraint layout solving) stages on
top of the existing constraint_graph runtime:

    Layer A  ModelSelector    — physical rows -> logical chunks
    Layer B  RelayoutSolver   — constraint graph building + native solve
    Layer C  OutputAssembler  — resolved bboxes -> assembly manifest

Consumed by the TransformationPipeline; keeps external output compatible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.constraint_graph import (
    ConstraintGraph,
    ConstraintSolver as GraphConstraintSolver,
)
from pdf2zh.v3.visual_tree import BoundingBox

logger = logging.getLogger(__name__)


@dataclass
class RelayoutConfig:
    """Tunables for the relayout engine."""

    reflow: bool = True
    float_images: bool = True
    overlay: bool = True
    chunk_line_gap: float = 2.0
    order_gap: float = 2.0


@dataclass
class RelayoutResult:
    """Result of the whole relayout stage for one document."""

    layouts: Dict[str, Dict[str, BoundingBox]] = field(default_factory=dict)
    blocks: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pages: int = 0

    def to_dict(self) -> dict:
        return {
            "pages": self.pages,
            "blocks": list(self.blocks),
            "warnings": list(self.warnings),
            "layouts": {
                k: {nid: bbox_to_tuple(b) for nid, b in v.items()}
                for k, v in self.layouts.items()
            },
        }


def _bbox_of(item: Any) -> BoundingBox:
    """Normalize an item (LayoutNode / node with .bbox) to a BoundingBox."""
    bb = getattr(item, "bbox", None)
    if bb is None:
        bb = getattr(item, "resolved_bbox", None)
    if isinstance(bb, BoundingBox):
        return bb
    if hasattr(bb, "x") and hasattr(bb, "width"):
        return BoundingBox(bb.x, bb.y, bb.width, bb.height)
    x0, y0, x1, y1 = bb[0], bb[1], bb[2], bb[3]
    return BoundingBox(x0, y0, x1 - x0, y1 - y0)


def bbox_to_tuple(bb: BoundingBox) -> Tuple[float, float, float, float]:
    return (round(bb.x, 2), round(bb.y, 2), round(bb.width, 2), round(bb.height, 2))


class ModelSelector:
    """Layer A: convert physical rows into logical translation chunks."""

    def __init__(self, line_gap: float = 2.0) -> None:
        self.line_gap = line_gap

    @staticmethod
    def _union(bboxes: Sequence[BoundingBox]) -> BoundingBox:
        x0 = min(b.x for b in bboxes)
        y0 = min(b.y for b in bboxes)
        x1 = max(b.x + b.width for b in bboxes)
        y1 = max(b.y + b.height for b in bboxes)
        return BoundingBox(x0, y0, x1 - x0, y1 - y0)

    def select(self, items: Sequence[Any]) -> List[List[Any]]:
        """Group items into chunks using vertical proximity + size similarity."""
        if not items:
            return []
        chunks: List[List[Any]] = []
        current: List[Any] = [items[0]]
        prev_bb = _bbox_of(items[0])
        for item in items[1:]:
            bb = _bbox_of(item)
            gap = bb.y - (prev_bb.y + prev_bb.height)
            same_size = abs(bb.height - prev_bb.height) < max(2.0, prev_bb.height * 0.3)
            if gap > self.line_gap or not same_size:
                chunks.append(current)
                current = [item]
            else:
                current.append(item)
            prev_bb = bb
        chunks.append(current)
        return chunks


class RelayoutSolver:
    """Layer B: build a ConstraintGraph from chunks and solve it."""

    def __init__(self, order_gap: float = 2.0) -> None:
        self.order_gap = order_gap

    def build_graph(
        self, chunks: List[List[Any]], page_num: int = 0
    ) -> ConstraintGraph:
        graph = ConstraintGraph()
        prev_id: Optional[str] = None
        for chunk in chunks:
            bboxes = [_bbox_of(i) for i in chunk]
            bb = ModelSelector._union(bboxes)
            cid = f"chunk_{getattr(chunk[0], 'id', '')}"
            graph.add_node(
                cid,
                "text_chunk",
                bbox=bb,
                page_num=page_num,
            )
            if prev_id is not None:
                graph.add_edge(
                    prev_id, cid, "must_below", priority="soft", gap=self.order_gap
                )
            prev_id = cid
        return graph

    def solve(
        self,
        chunks: List[List[Any]],
        page_num: int = 0,
        page_width: Optional[float] = None,
        page_height: Optional[float] = None,
    ) -> Dict[str, BoundingBox]:
        """Build + solve constraints, return {chunk_id: resolved BoundingBox}."""
        graph = self.build_graph(chunks, page_num=page_num)
        solver = GraphConstraintSolver(graph)
        solver.solve()
        return solver.get_layout_result()


class OutputAssembler:
    """Layer C: turn solved chunk bboxes into a compact assembly manifest."""

    @staticmethod
    def assemble(
        layout: Dict[str, BoundingBox], chunks: Optional[Dict[str, List[str]]] = None
    ) -> List[dict]:
        blocks = []
        chunks = chunks or {}
        for cid, bb in layout.items():
            blocks.append(
                {
                    "id": cid,
                    "x": round(bb.x, 2),
                    "y": round(bb.y, 2),
                    "w": round(bb.width, 2),
                    "h": round(bb.height, 2),
                    "source_ids": list(chunks.get(cid, [])),
                }
            )
        blocks.sort(key=lambda b: (b["y"], b["x"]))
        return blocks


class RelayoutEngine:
    """Unified pipeline facade: ModelSelector -> RelayoutSolver -> Assembler."""

    def __init__(self, config: Optional[RelayoutConfig] = None) -> None:
        self.config = config or RelayoutConfig()
        self.selector = ModelSelector(line_gap=self.config.chunk_line_gap)
        self.solver = RelayoutSolver(order_gap=self.config.order_gap)
        self.assembler = OutputAssembler()

    def run(
        self,
        pages: List[dict],
        page_width: Optional[float] = None,
        page_height: Optional[float] = None,
    ) -> RelayoutResult:
        """Run the full relayout over a list of page dicts.

        page dict: {"index": int, "items": [items with .id and .bbox]}
        """
        result = RelayoutResult()
        for page in pages:
            idx = page.get("index", 0)
            items = page.get("items", [])
            chunks = self.selector.select(items)
            layout = self.solver.solve(
                chunks, page_num=idx, page_width=page_width, page_height=page_height
            )
            source_map = {
                f"chunk_{getattr(c[0], 'id', '')}": [getattr(i, "id", "") for i in c]
                for c in chunks
            }
            result.layouts[idx] = layout
            result.blocks.extend(self.assembler.assemble(layout, source_map))
            result.pages += 1
        return result


__all__ = [
    "RelayoutConfig",
    "RelayoutResult",
    "ModelSelector",
    "RelayoutSolver",
    "OutputAssembler",
    "RelayoutEngine",
]
