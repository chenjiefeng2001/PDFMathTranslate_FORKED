"""
DAG-based reading order analysis for pdf2zh 2.0.

Builds a directed acyclic graph (DAG) of text blocks on each page
and performs topological sort to determine correct reading order,
handling multi-column layouts and complex document structures.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class TextNode:
    """A text block node in the reading-order DAG."""
    id: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str = ""
    font_size: float = 0.0
    page_num: int = 0


@dataclass
class LayoutGraph:
    """DAG representing the reading order of text blocks on a page."""
    nodes: List[TextNode] = field(default_factory=list)
    edges: Dict[int, List[int]] = field(default_factory=dict)

    def add_node(self, node: TextNode) -> None:
        """Add a text block node."""
        self.nodes.append(node)
        if node.id not in self.edges:
            self.edges[node.id] = []

    def add_edge(self, from_id: int, to_id: int) -> None:
        """Add a reading order edge (from_id -> to_id)."""
        if from_id not in self.edges:
            self.edges[from_id] = []
        if to_id not in self.edges[from_id]:
            self.edges[from_id].append(to_id)

    def topological_sort(self) -> List[TextNode]:
        """Return nodes sorted by reading order via topological sort.

        Falls back to spatial sort (top-to-bottom, left-to-right) if
        no explicit edges are defined.

        Returns:
            List of TextNode in reading order
        """
        if not self.edges or all(len(v) == 0 for v in self.edges.values()):
            return self._spatial_sort()

        # Kahn's algorithm
        in_degree: Dict[int, int] = {n.id: 0 for n in self.nodes}
        for edges in self.edges.values():
            for to_id in edges:
                in_degree[to_id] = in_degree.get(to_id, 0) + 1

        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        sorted_ids = []

        while queue:
            nid = queue.pop(0)
            sorted_ids.append(nid)
            for neighbor in self.edges.get(nid, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # If cycle detected, fall back to spatial sort
        if len(sorted_ids) != len(self.nodes):
            logger.warning("Cycle detected in reading order DAG; falling back to spatial sort")
            return self._spatial_sort()

        node_map = {n.id: n for n in self.nodes}
        return [node_map[nid] for nid in sorted_ids]

    def _spatial_sort(self) -> List[TextNode]:
        """Sort nodes by spatial position (top-to-bottom, left-to-right).

        Multi-column detection: groups nodes by horizontal overlap,
        then sorts each group top-to-bottom, and groups left-to-right.
        """
        if not self.nodes:
            return []

        # Detect columns by x-overlap
        sorted_by_x = sorted(self.nodes, key=lambda n: n.x0)
        columns: List[List[TextNode]] = [[sorted_by_x[0]]]

        for node in sorted_by_x[1:]:
            # Check if node overlaps with any existing column
            placed = False
            for col in columns:
                if self._horizontal_overlap(node, col[0]):
                    col.append(node)
                    placed = True
                    break
            if not placed:
                columns.append([node])

        # Sort each column top-to-bottom
        for col in columns:
            col.sort(key=lambda n: n.y0, reverse=True)

        # Sort columns left-to-right
        columns.sort(key=lambda c: c[0].x0)

        # Flatten
        result = []
        for col in columns:
            result.extend(col)
        return result

    def detect_multi_column(self) -> int:
        """Detect number of text columns on the page.

        Returns:
            Number of columns detected (1=single, 2=two-column, etc.)
        """
        if len(self.nodes) < 2:
            return 1

        # Count gaps in x-projection
        x_intervals = sorted(
            [(n.x0, n.x1) for n in self.nodes],
            key=lambda x: x[0]
        )

        # Merge overlapping intervals
        merged = [list(x_intervals[0])]
        for start, end in x_intervals[1:]:
            if start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        return len(merged)

    @staticmethod
    def _horizontal_overlap(a: TextNode, b: TextNode, threshold: float = 0.3) -> bool:
        """Check if two text nodes horizontally overlap."""
        overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
        if overlap <= 0:
            return False
        min_width = min(a.x1 - a.x0, b.x1 - b.x0)
        return overlap / min_width >= threshold
