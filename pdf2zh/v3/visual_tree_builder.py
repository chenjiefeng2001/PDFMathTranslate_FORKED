"""Module: VisualTreeBuilder — Deep Converter from DocumentGraph to VisualTree.

Phase 2, Step 2.1 implementation that builds a VisualTree
from a semantically analyzed DocumentGraph.

Usage:
    from pdf2zh.v3.visual_tree_builder import VisualTreeBuilder
    builder = VisualTreeBuilder()
    tree = builder.build_from_graph(graph)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType, EdgeType
from pdf2zh.v3.visual_tree import (
    VisualTree, VisualNode, VisualNodeType,
    BoundingBox, Page, Paragraph, Line, TextRun,
    Image, Formula,
)

logger = logging.getLogger(__name__)

DEFAULT_PAGE_WIDTH = 612.0
DEFAULT_PAGE_HEIGHT = 792.0
DEFAULT_MARGIN = 50.0

# Node type mapping for visual tree conversion
_NODE_TYPE_TO_SKIP = {
    NodeType.DOCUMENT, NodeType.PAGE,
}


class VisualTreeBuilder:
    """Build a VisualTree from a DocumentGraph.

    Maps DocumentNode instances to VisualNode subclasses,
    groups them by page, preserves reading order, and assigns initial BBox.
    """

    def __init__(
        self,
        page_width: float = DEFAULT_PAGE_WIDTH,
        page_height: float = DEFAULT_PAGE_HEIGHT,
        margin: float = DEFAULT_MARGIN,
    ) -> None:
        self._page_width = page_width
        self._page_height = page_height
        self._margin = margin
        self._node_map: Dict[str, str] = {}

    @property
    def node_map(self) -> Dict[str, str]:
        return dict(self._node_map)

    def build_from_graph(self, graph: DocumentGraph) -> VisualTree:
        tree = VisualTree()
        self._node_map.clear()

        page_groups: Dict[int, List[DocumentNode]] = {}
        for node in graph.nodes:
            if node.node_type in (NodeType.DOCUMENT,):
                continue
            pn = node.page_num
            page_groups.setdefault(pn, []).append(node)

        if not page_groups:
            logger.warning("No pageable nodes found in DocumentGraph")
            return tree

        for page_num in sorted(page_groups.keys()):
            page_node = self._build_page(graph, page_num, page_groups[page_num])
            tree.add_page(page_node)

        logger.info(
            "Built VisualTree: %d pages from DocumentGraph (%d nodes mapped)",
            tree.page_count, len(self._node_map),
        )
        return tree

    # ── Private Builders ─────────────────────────────────────────────

    def _build_page(
        self,
        graph: DocumentGraph,
        page_num: int,
        nodes: List[DocumentNode],
    ) -> Page:
        page = Page(
            id=f"page_{page_num}",
            width=self._page_width,
            height=self._page_height,
            page_num=page_num,
        )
        page.bbox = BoundingBox(0, 0, self._page_width, self._page_height)

        sorted_nodes = self._sort_by_reading_order(graph, nodes)

        for doc_node in sorted_nodes:
            if doc_node.node_type in _NODE_TYPE_TO_SKIP:
                continue
            visual_node = self._convert_node(doc_node)
            if visual_node is not None:
                page.add_child(visual_node)

        return page

    def _sort_by_reading_order(
        self,
        graph: DocumentGraph,
        nodes: List[DocumentNode],
    ) -> List[DocumentNode]:
        """Topological sort using FOLLOWS edges; fallback to Y-order."""
        if not nodes:
            return nodes

        node_ids = {n.id for n in nodes}
        children: Dict[str, List[str]] = {n.id: [] for n in nodes}
        in_degree: Dict[str, int] = {n.id: 0 for n in nodes}

        for n in nodes:
            for edge in graph.get_edges(source_id=n.id):
                if edge.edge_type == EdgeType.FOLLOWS and edge.target_id in node_ids:
                    children[n.id].append(edge.target_id)
                    in_degree[edge.target_id] = in_degree.get(edge.target_id, 0) + 1

        edge_count = sum(len(v) for v in children.values())
        if edge_count < len(nodes) * 0.3:
            # Fall back to Y-order top-to-bottom, X left-to-right
            return sorted(
                nodes,
                key=lambda n: (n.y0 or 0, n.x0 or 0),
            )

        # Topological sort
        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        sorted_ids: List[str] = []
        while queue:
            queue.sort()
            nid = queue.pop(0)
            sorted_ids.append(nid)
            for nb in children.get(nid, []):
                in_degree[nb] -= 1
                if in_degree[nb] == 0:
                    queue.append(nb)

        # Add remaining (orphaned) nodes at end
        remaining = sorted(
            [n for n in nodes if n.id not in sorted_ids],
            key=lambda n: (n.y0 or 0, n.x0 or 0),
        )
        sorted_ids.extend(n.id for n in remaining)

        node_by_id = {n.id: n for n in nodes}
        return [node_by_id[nid] for nid in sorted_ids if nid in node_by_id]

    def _convert_node(self, doc_node: DocumentNode) -> Optional[VisualNode]:
        """Convert a DocumentNode to the appropriate VisualNode subclass."""
        ntype = doc_node.node_type
        bbox = self._bbox_from_node(doc_node)

        if ntype == NodeType.FIGURE:
            visual = Image(
                id=doc_node.id, bbox=bbox,
                alt_text=(doc_node.text or "")[:200],
            )
            self._node_map[doc_node.id] = visual.id
            return visual

        if ntype in (NodeType.FORMULA, NodeType.FORMULA_INLINE):
            visual = Formula(
                id=doc_node.id, bbox=bbox,
                latex=doc_node.text or "",
                is_inline=(ntype == NodeType.FORMULA_INLINE),
            )
            self._node_map[doc_node.id] = visual.id
            return visual

        if ntype == NodeType.TABLE:
            visual = Paragraph(
                id=doc_node.id, bbox=bbox,
                language=doc_node.language or "",
            )
            self._node_map[doc_node.id] = visual.id
            line = Line(
                id=f"{doc_node.id}_l0", y=bbox.y,
                baseline=bbox.y + 12,
            )
            line.bbox = BoundingBox(bbox.x, bbox.y, bbox.width, 14)
            run = TextRun(
                id=f"{doc_node.id}_r0",
                text=doc_node.translated_text or doc_node.text or "",
                font=doc_node.font or "",
            )
            line.add_run(run)
            visual.add_line(line)
            return visual

        # Default: Paragraph-based node (PARAGRAPH, HEADING, CAPTION,
        # ABSTRACT, REFERENCE, FOOTNOTE, LIST, CODE, etc.)
        text = doc_node.translated_text or doc_node.text or ""
        if not text.strip():
            return None

        visual = Paragraph(
            id=doc_node.id, bbox=bbox,
            language=doc_node.language or "",
            spacing_before=2.0, spacing_after=4.0,
        )

        font_size = doc_node.font_size or 12.0
        line_height = font_size * 1.4
        raw_lines = text.split("\n")
        y_pos = bbox.y

        for li, line_text in enumerate(raw_lines):
            if not line_text.strip() and li > 0:
                y_pos += line_height * 0.5
                continue
            line = Line(
                id=f"{doc_node.id}_l{li}", y=y_pos,
                baseline=y_pos + font_size * 0.8,
                line_height=line_height,
            )
            line.bbox = BoundingBox(bbox.x, y_pos, bbox.width, line_height)
            run = TextRun(
                id=f"{doc_node.id}_r{li}",
                text=line_text,
                font=doc_node.font or "",
                font_size=font_size,
            )
            line.add_run(run)
            visual.add_line(line)
            y_pos += line_height

        if visual.lines:
            last_line = visual.lines[-1]
            visual.bbox.height = last_line.bbox.y1 - bbox.y + 2

        self._node_map[doc_node.id] = visual.id
        return visual

    @staticmethod
    def _bbox_from_node(doc_node: DocumentNode) -> BoundingBox:
        """Extract or estimate BoundingBox from DocumentNode attributes."""
        try:
            if doc_node.bbox is not None:
                x0, y0, x1, y1 = (
                    float(doc_node.bbox[0]), float(doc_node.bbox[1]),
                    float(doc_node.bbox[2]), float(doc_node.bbox[3]),
                )
                return BoundingBox(x0, y0, x1 - x0, y1 - y0)
        except (TypeError, ValueError, IndexError):
            pass
        return BoundingBox(
            float(doc_node.x0 or 0), float(doc_node.y0 or 0),
            float(doc_node.width or 100), float(doc_node.height or 20),
        )


__all__ = ["VisualTreeBuilder"]