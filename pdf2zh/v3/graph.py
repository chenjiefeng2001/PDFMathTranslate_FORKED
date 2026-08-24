"""Module 3: Document Graph Builder.

Constructs a DocumentGraph — the core semantic backbone of V3 architecture.

A DocumentGraph is a directed graph of DocumentNode objects connected by
typed Edge objects. Unlike a flat list or tree, a graph natively supports:
  - Cross-references (Figure → Caption, Reference → Citation)
  - Reading order (ReadingEdge)
  - Containment (ContainEdge)
  - Layout constraints (ConstraintEdge)
"""

from __future__ import annotations

__all__ = [
    "NodeType",
    "EdgeType",
    "ConstraintPriority",
    "Edge",
    "DocumentNode",
    "DocumentGraph",
    "GraphBuildConfig",
    "DocumentGraphBuilder",
]

import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from pdf2zh.font_resolver import FontStyle
from pdf2zh.v3.normalizer import NormalizedBlock

logger = logging.getLogger(__name__)


class NodeType(Enum):
    """Semantic node types in the DocumentGraph."""

    UNKNOWN = "unknown"
    DOCUMENT = "document"
    PAGE = "page"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    HEADER = "header"
    FOOTER = "footer"
    FIGURE = "figure"
    IMAGE = "image"
    TABLE = "table"
    FORMULA = "formula"
    FORMULA_INLINE = "formula_inline"
    CODE = "code"
    LIST = "list"
    LIST_ITEM = "list_item"
    REFERENCE = "reference"
    BIBLIOGRAPHY = "bibliography"
    CITATION = "citation"
    ABSTRACT = "abstract"
    KEYWORDS = "keywords"
    SECTION = "section"
    SUBSECTION = "subsection"
    TOC_ENTRY = "toc_entry"


class EdgeType(Enum):
    """Typed edges in the DocumentGraph."""

    CONTAINS = "contains"
    FOLLOWS = "follows"
    PRECEDES = "precedes"
    REFERENCE = "reference"
    CAPTION_OF = "caption_of"
    FOOTNOTE_OF = "footnote_of"
    CITATION_OF = "citation_of"
    MUST_ABOVE = "must_above"
    MUST_FOLLOW = "must_follow"
    CANNOT_OVERLAP = "cannot_overlap"
    SAME_BASELINE = "same_baseline"
    SAME_SECTION = "same_section"
    DEPENDS_ON = "depends_on"


class ConstraintPriority(Enum):
    HARD = "hard"
    SOFT = "soft"


# ── Core Data Structures ────────────────────────────────────────────────


@dataclass
class Edge:
    """A typed, directed edge in the DocumentGraph."""

    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0
    priority: ConstraintPriority = ConstraintPriority.HARD
    metadata: dict = field(default_factory=dict)


@dataclass
class DocumentNode:
    """A node in the DocumentGraph — one document element."""

    id: str
    node_type: NodeType
    bbox: Tuple[float, float, float, float]
    text: str = ""
    page_num: int = 0
    font_size: float = 0.0
    font_style: FontStyle = FontStyle.SANS_SERIF
    confidence: float = 1.0
    metadata: dict = field(default_factory=dict)
    out_edges: List[Edge] = field(default_factory=list)
    in_edges: List[Edge] = field(default_factory=list)

    @property
    def width(self) -> float:
        return abs(self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return abs(self.bbox[3] - self.bbox[1])

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]


# ── DocumentGraph ───────────────────────────────────────────────────────


@dataclass
class DocumentGraph:
    """The core semantic graph of a document.

    All V3 modules operate on this graph:
      - SemanticAnalyzer adds semantic labels (NodeType, semantic edges)
      - Translation Planner associates plans with nodes
      - Layout Engine reads constraints from edges
      - Renderer traverses the graph
      - Quality Evaluator scores the graph
    """

    nodes: List[DocumentNode] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    _node_map: Dict[str, DocumentNode] = field(default_factory=dict)

    def add_node(self, node: DocumentNode) -> None:
        self.nodes.append(node)
        self._node_map[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
        src = self._node_map.get(edge.source_id)
        tgt = self._node_map.get(edge.target_id)
        if src:
            src.out_edges.append(edge)
        if tgt:
            tgt.in_edges.append(edge)

    def get_node(self, node_id: str) -> Optional[DocumentNode]:
        return self._node_map.get(node_id)

    def get_nodes_by_type(self, node_type: NodeType) -> List[DocumentNode]:
        return [n for n in self.nodes if n.node_type == node_type]

    def get_nodes_on_page(self, page_num: int) -> List[DocumentNode]:
        return [n for n in self.nodes if n.page_num == page_num]

    def get_edges(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        edge_type: Optional[EdgeType] = None,
    ) -> List[Edge]:
        result = self.edges
        if source_id is not None:
            result = [e for e in result if e.source_id == source_id]
        if target_id is not None:
            result = [e for e in result if e.target_id == target_id]
        if edge_type is not None:
            result = [e for e in result if e.edge_type == edge_type]
        return result

    def to_dot(self) -> str:
        """Export graph to Graphviz DOT format for visualization."""
        lines = ["digraph DocumentGraph {"]
        for node in self.nodes:
            label = f"{node.node_type.value}:{node.text[:30]}"
            lines.append(f'  "{node.id}" [label="{label}", shape=box];')
        for edge in self.edges:
            lines.append(
                f'  "{edge.source_id}" -> "{edge.target_id}" '
                f'[label="{edge.edge_type.value}"];'
            )
        lines.append("}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self):
        # 必须显式迭代 nodes：__getitem__ 以 node_id(str) 为键且永不抛
        # IndexError，若无 __iter__，Python 会退回按整型索引迭代
        # (graph[0], graph[1], ...) 并因永不终止而无限循环，导致上层
        # `for node in graph` 死锁（曾使 _collect_node_overview 挂起，
        # 整个任务卡在 analyzing、任务队列被锁死）。
        return iter(self.nodes)

    def __getitem__(self, node_id: str) -> Optional[DocumentNode]:
        return self._node_map.get(node_id)


# ── Graph Build Configuration ───────────────────────────────────────────


@dataclass
class GraphBuildConfig:
    """Configuration for the DocumentGraphBuilder."""

    generate_node_ids: bool = True
    add_reading_edges: bool = True
    reading_edge_margin: float = 5.0
    merge_same_line: bool = True


# ── Graph Builder ───────────────────────────────────────────────────────


class DocumentGraphBuilder:
    """Build a DocumentGraph from normalized blocks.

    Initial node type assignment is heuristic-based:
      - Blocks with large font and short text → HEADING
      - Blocks starting with "Figure", "Table" → CAPTION
      - All others → PARAGRAPH
    """

    HEADING_MIN_SIZE = 14.0
    HEADING_MAX_CHARS = 60
    CAPTION_PREFIXES = ("figure", "fig.", "table", "fig", "tableau")

    def __init__(self, config: Optional[GraphBuildConfig] = None):
        self.config = config or GraphBuildConfig()

    def build(self, blocks: List[NormalizedBlock]) -> DocumentGraph:
        """Build a DocumentGraph from normalized parser output."""
        graph = DocumentGraph()

        for i, block in enumerate(blocks):
            node_id = self._make_id(block, i)
            node_type = self._infer_initial_type(block)

            node = DocumentNode(
                id=node_id,
                node_type=node_type,
                bbox=block.bbox,
                text=block.text,
                page_num=block.page_num,
                font_size=block.font_size_avg,
                font_style=block.font_style,
                confidence=block.confidence,
                metadata={"index": i, "source": "normalizer"},
            )
            graph.add_node(node)

        # Add page containers
        pages = sorted(set(b.page_num for b in blocks))
        for page_num in pages:
            page_id = f"page_{page_num}"
            page_nodes = graph.get_nodes_on_page(page_num)
            if not page_nodes:
                continue
            x0 = min(n.x0 for n in page_nodes)
            y0 = min(n.y0 for n in page_nodes)
            x1 = max(n.x1 for n in page_nodes)
            y1 = max(n.y1 for n in page_nodes)

            page_node = DocumentNode(
                id=page_id,
                node_type=NodeType.PAGE,
                bbox=(x0, y0, x1, y1),
                text=f"Page {page_num + 1}",
                page_num=page_num,
            )
            graph.add_node(page_node)
            for node in page_nodes:
                graph.add_edge(
                    Edge(
                        source_id=page_id,
                        target_id=node.id,
                        edge_type=EdgeType.CONTAINS,
                    )
                )

        # Add reading-order edges
        if self.config.add_reading_edges:
            self._add_reading_edges(graph)

        return graph

    def _make_id(self, block: NormalizedBlock, index: int) -> str:
        raw = f"{block.page_num}_{index}_{block.text[:40]}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def _infer_initial_type(self, block: NormalizedBlock) -> NodeType:
        text = block.text.strip()
        if not text:
            return NodeType.PARAGRAPH
        if (
            block.font_size_avg >= self.HEADING_MIN_SIZE
            and len(text) <= self.HEADING_MAX_CHARS
            and "\n" not in text
        ):
            return NodeType.HEADING
        if text.lower().startswith(self.CAPTION_PREFIXES):
            return NodeType.CAPTION
        return NodeType.PARAGRAPH

    def _add_reading_edges(self, graph: DocumentGraph) -> None:
        """Add FOLLOWS edges between content nodes in reading order.

        Uses existing LayoutGraph for spatial sort within each page.
        """
        from pdf2zh.layout_graph import LayoutGraph, TextNode

        for page_num in sorted(set(n.page_num for n in graph.nodes)):
            content_nodes = [
                n
                for n in graph.get_nodes_on_page(page_num)
                if n.node_type not in (NodeType.PAGE, NodeType.DOCUMENT)
            ]
            if len(content_nodes) < 2:
                continue

            lg = LayoutGraph()
            for i, node in enumerate(content_nodes):
                lg.add_node(
                    TextNode(
                        id=i,
                        x0=node.x0,
                        y0=node.y0,
                        x1=node.x1,
                        y1=node.y1,
                        text=node.text,
                        font_size=node.font_size,
                        page_num=node.page_num,
                    )
                )

            sorted_nodes = lg.topological_sort()
            sorted_ids = [content_nodes[sn.id].id for sn in sorted_nodes]

            for i in range(len(sorted_ids) - 1):
                graph.add_edge(
                    Edge(
                        source_id=sorted_ids[i],
                        target_id=sorted_ids[i + 1],
                        edge_type=EdgeType.FOLLOWS,
                        weight=1.0,
                        priority=ConstraintPriority.SOFT,
                    )
                )

    PREFERRED = "preferred"
