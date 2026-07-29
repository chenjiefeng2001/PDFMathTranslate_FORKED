"""Module: Layout Runtime — V4 Epic B.
Unified layout engine: Measure -> Flow -> Constraint -> Solve -> Render.

Phase 2 upgrades:
  - InlineLayout (character-level letter/word spacing, kerning)
  - ColumnLayout (multi-column detection and layout)
  - CollisionEngine (sweep-line based collision detection)
  - Overflow / pagination improvements
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
from pdf2zh.v3.visual_tree import VisualTree, BoundingBox, Page, Paragraph, Line, TextRun
logger = logging.getLogger(__name__)


# -- CollisionEngine (Phase 2) --

@dataclass
class CollisionRecord:
    node_id_a: str
    node_id_b: str
    overlap_x: float = 0.0
    overlap_y: float = 0.0
    severity: str = "minor"


class CollisionEngine:
    OVERLAP_THRESHOLD = 2.0

    @classmethod
    def _bbox_rect(cls, node):
        """Normalize bbox to (x, y, w, h) whether node.bbox is tuple or BoundingBox."""
        bb = node.bbox
        if hasattr(bb, "x"):
            return (bb.x, bb.y, bb.width, bb.height)
        x0, y0, x1, y1 = bb[0], bb[1], bb[2], bb[3]
        return (x0, y0, x1 - x0, y1 - y0)

    @classmethod
    def detect(cls, nodes):
        """Sweep-line collision detection."""
        events = []
        for n in nodes:
            if not hasattr(n, "bbox") or not n.bbox:
                continue
            x, y, w, h = cls._bbox_rect(n)
            events.append((x, "start", n))
            events.append((x + w, "end", n))
        events.sort(key=lambda e: (e[0], 0 if e[1] == "start" else 1))
        active = set()
        collisions = []
        active_nodes = {}
        for pos, etype, node in events:
            if etype == "start":
                nx, ny, nw, nh = cls._bbox_rect(node)
                for oid in list(active):
                    other = active_nodes[oid]
                    ox, oy, ow, oh = cls._bbox_rect(other)
                    ovx = min(nx + nw, ox + ow) - max(nx, ox)
                    ovy = min(ny + nh, oy + oh) - max(ny, oy)
                    if ovx > cls.OVERLAP_THRESHOLD and ovy > cls.OVERLAP_THRESHOLD:
                        sev = "critical" if ovy > 10 else ("major" if ovy > 5 else "minor")
                        collisions.append(CollisionRecord(node.id, other.id, ovx, ovy, sev))
                active.add(node.id)
                active_nodes[node.id] = node
            else:
                active.discard(node.id)
                active_nodes.pop(node.id, None)
        return collisions

    @classmethod
    def has_collisions(cls, nodes):
        return len(cls.detect(nodes)) > 0

    @classmethod
    def count_critical(cls, nodes):
        return sum(1 for c in cls.detect(nodes) if c.severity == "critical")



class ConstraintType(Enum):
    HARD = "hard"
    SOFT = "soft"
    PREFERRED = "preferred"

@dataclass
class LayoutConstraint:
    constraint_type: ConstraintType
    source_id: str
    target_id: str
    relationship: str
    priority: int = 100
    weight: float = 1.0
    gap: float = 5.0

class Measure:
    CJK_WIDTH = 12.0
    ASCII_WIDTH = 7.0
    LINE_HEIGHT = 16.0
    PARAGRAPH_SPACING = 6.0

    @classmethod
    def measure_text(cls, text: str, font_size: float = 12.0) -> float:
        scale = font_size / 12.0
        width = 0.0
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f':
                width += cls.CJK_WIDTH * scale
            else:
                width += cls.ASCII_WIDTH * scale
        return width

    @classmethod
    def estimate_lines(cls, text: str, font_size: float, container_width: float) -> int:
        if container_width <= 0:
            return len(text) // 10 + 1
        char_w = cls.measure_text("W", font_size)
        chars_per_line = max(1, int(container_width / char_w))
        return max(1, (len(text) + chars_per_line - 1) // chars_per_line)

    @classmethod
    def measure_height(cls, lines: int, font_size: float = 12.0) -> float:
        return lines * cls.LINE_HEIGHT * (font_size / 12.0)


# -- InlineLayout (Phase 2) --

@dataclass
class GlyphMetric:
    char: str = ""
    width: float = 0.0
    height: float = 0.0
    advance: float = 0.0
    is_cjk: bool = False


class InlineLayout:
    """Character-level inline layout with letter/word spacing."""
    LETTER_SPACING = 0.5
    WORD_SPACING = 3.0
    CJK_WIDTH = 12.0
    ASCII_WIDTH = 7.0

    @classmethod
    def measure_char(cls, ch, font_size=12.0):
        scale = font_size / 12.0
        is_cjk = bool("一" <= ch <= "鿿" or "　" <= ch <= "〿")
        w = cls.CJK_WIDTH if is_cjk else cls.ASCII_WIDTH
        return GlyphMetric(char=ch, width=w * scale, height=font_size,
                          advance=(w + cls.LETTER_SPACING) * scale, is_cjk=is_cjk)

    @classmethod
    def measure_word(cls, word, font_size=12.0):
        total = 0.0
        for ch in word:
            total += cls.measure_char(ch, font_size).advance
        return total

    @classmethod
    def break_line(cls, text, max_width, font_size=12.0):
        """Break text into lines at word boundaries."""
        words = text.split(" ")
        lines = []
        current_line = []
        current_w = 0.0
        for word in words:
            ww = cls.measure_word(word, font_size)
            sep_w = cls.WORD_SPACING * (font_size / 12.0) if current_line else 0
            if current_w + sep_w + ww > max_width and current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
                current_w = ww
            else:
                current_line.append(word)
                current_w += sep_w + ww
        if current_line:
            lines.append(" ".join(current_line))
        return lines if lines else [text]


@dataclass
class ColumnRegion:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    def __init__(self, x=0.0, y=0.0, width=None, w=None, height=None):
        self.x = x
        self.y = y
        self.width = width if width is not None else (w if w is not None else 0.0)
        self.height = height if height is not None else 0.0


class ColumnLayout:
    """Multi-column detection and layout."""
    MIN_COL_GAP = 20.0

    @staticmethod
    def _node_bbox_x(node):
        bb = node.bbox
        if hasattr(bb, "x"):
            return bb.x
        return bb[0]

    @staticmethod
    def _node_bbox_x1(node):
        bb = node.bbox
        if hasattr(bb, "x"):
            return bb.x + bb.width
        return bb[2]

    @classmethod
    def detect_columns(cls, nodes, page_width, margin_l=50, margin_r=50):
        """Detect column count from node x-coordinates."""
        if not nodes:
            return 1, [ColumnRegion(x=margin_l, y=0, width=page_width - margin_l - margin_r, height=1000)]
        ranges = []
        for n in nodes:
            ranges.append((cls._node_bbox_x(n), cls._node_bbox_x1(n)))
        ranges.sort()
        gaps = []
        for i in range(len(ranges) - 1):
            gap = ranges[i + 1][0] - ranges[i][1]
            if gap > cls.MIN_COL_GAP:
                gaps.append((ranges[i][1], ranges[i + 1][0]))
        ncols = len(gaps) + 1 if gaps else 1
        cw = (page_width - margin_l - margin_r) / ncols
        cols = []
        for i in range(ncols):
            cols.append(ColumnRegion(x=margin_l + i * cw, y=0, width=cw, height=1000))
        return ncols, cols

    @classmethod
    def assign_to_column(cls, node, columns):
        """Assign a node to the best column based on x-centroid."""
        cx = cls._node_bbox_x(node) + (cls._node_bbox_x1(node) - cls._node_bbox_x(node)) / 2
        best = 0
        best_dist = float("inf")
        for i, col in enumerate(columns):
            col_cx = col.x + col.width / 2
            d = abs(cx - col_cx)
            if d < best_dist:
                best_dist = d; best = i
        return best



class FlowResult:
    def __init__(self, node_id: str, lines: int, width: float, height: float, overflow: bool = False):
        self.node_id = node_id
        self.lines = lines
        self.width = width
        self.height = height
        self.overflow = overflow

class Flow:
    def __init__(self, page_width=612.0, page_height=792.0, margin_left=50.0, margin_right=50.0, margin_top=50.0, margin_bottom=50.0):
        self.page_width = page_width
        self.page_height = page_height
        self.margin_left = margin_left
        self.margin_right = margin_right
        self.margin_top = margin_top
        self.margin_bottom = margin_bottom

    @property
    def content_width(self):
        return self.page_width - self.margin_left - self.margin_right

    @property
    def content_height(self):
        return self.page_height - self.margin_top - self.margin_bottom

    def flow_paragraph(self, text, font_size=12.0):
        w = self.content_width
        lines = Measure.estimate_lines(text, font_size, w)
        h = Measure.measure_height(lines, font_size)
        return FlowResult(node_id="", lines=lines, width=w, height=h, overflow=h > self.content_height)

    def flow_paragraphs(self, nodes, font_size=12.0):
        y = self.margin_top
        results = []
        for node in nodes:
            if not node.text.strip():
                continue
            fr = self.flow_paragraph(node.text, font_size or node.font_size)
            fr.node_id = node.id
            if y + fr.height > self.page_height - self.margin_bottom:
                y = self.margin_top
            y += fr.height + Measure.PARAGRAPH_SPACING
            results.append(fr)
        return results

class ConstraintBuilder:
    @classmethod
    def build_from_graph(cls, graph):
        constraints = []
        for edge in graph.edges:
            et = edge.edge_type.value if hasattr(edge.edge_type, 'value') else str(edge.edge_type)
            if et == "reading_order":
                constraints.append(LayoutConstraint(ConstraintType.SOFT, edge.source_id, edge.target_id, "must_follow", gap=5.0))
            elif et in ("contain", "contains"):
                constraints.append(LayoutConstraint(ConstraintType.HARD, edge.source_id, edge.target_id, "must_below", gap=0.0))
        return constraints

class ConstraintSolver:
    def __init__(self):
        self._constraints = []
        self._positions = {}
    def add_constraint(self, c): self._constraints.append(c)
    def add_constraints(self, cs):
        self._constraints.extend(cs)
    def solve(self):
        for c in self._constraints:
            src_y = self._positions.get(c.source_id, 0.0)
            tgt_y = self._positions.get(c.target_id, 0.0)
            if c.relationship == "must_follow":
                self._positions[c.target_id] = max(tgt_y, src_y + c.gap)
            elif c.relationship == "must_above":
                self._positions[c.target_id] = max(tgt_y, src_y + 10.0 + c.gap)
            elif c.relationship == "must_below":
                self._positions[c.target_id] = max(tgt_y, src_y + c.gap)
            elif c.relationship == "cannot_overlap":
                if abs(src_y - tgt_y) < c.gap:
                    self._positions[c.target_id] = max(tgt_y, src_y + c.gap + 10.0)
        return dict(self._positions)
    def clear(self):
        self._constraints.clear(); self._positions.clear()
    @property
    def constraint_count(self): return len(self._constraints)

class LayoutEngine:
    def __init__(self, page_width=612.0, page_height=792.0):
        self.measure = Measure()
        self.flow = Flow(page_width=page_width, page_height=page_height)
        self.constraint_solver = ConstraintSolver()
        self.page_width = page_width
        self.page_height = page_height
        self._tree = None
        self._constraints = []

    def layout(self, graph):
        tree = VisualTree()
        page = Page(id="page_1", width=self.page_width, height=self.page_height, page_num=0)
        cw = self.flow.content_width
        y = self.flow.margin_top
        nodes = [n for n in graph.nodes if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE)]
        for node in nodes:
            if not node.text.strip(): continue
            para = Paragraph(id=node.id, bbox=BoundingBox(self.flow.margin_left, y, cw, 0))
            lines_n = Measure.estimate_lines(node.text, node.font_size or 12.0, cw)
            para_h = Measure.measure_height(lines_n, node.font_size or 12.0)
            for li in range(lines_n):
                line_baseline = y + li * Measure.LINE_HEIGHT + 2
                line = Line(id=f"{node.id}_l{li}", baseline=line_baseline)
                line.bbox = BoundingBox(self.flow.margin_left, y + li * Measure.LINE_HEIGHT, cw, Measure.LINE_HEIGHT)
                run = TextRun(id=f"{node.id}_r{li}", text=node.text[li::max(1, lines_n)], font="")
                line.add_run(run)
                para.add_line(line)
            para.bbox.height = para_h
            page.add_child(para)
            y += para_h + Measure.PARAGRAPH_SPACING
            if y > self.page_height - self.flow.margin_bottom:
                tree.add_page(page)
                page = Page(id=f"page_{tree.page_count + 1}", width=self.page_width, height=self.page_height, page_num=tree.page_count)
                y = self.flow.margin_top
        if page.children:
            tree.add_page(page)
        self._tree = tree
        return tree

    @property
    def tree(self):
        return self._tree

    def layout_with_columns(self, graph):
        """Layout with automatic column detection."""
        nodes = [n for n in graph.nodes if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE)]
        ncols, columns = ColumnLayout.detect_columns(nodes, self.page_width)
        col_nodes = [[] for _ in range(ncols)]
        for n in nodes:
            ci = ColumnLayout.assign_to_column(n, columns)
            col_nodes[ci].append(n)
        tree = VisualTree()
        page = Page(id="page_1", width=self.page_width, height=self.page_height, page_num=0)
        for ci, col in enumerate(columns):
            y = self.flow.margin_top
            for node in col_nodes[ci]:
                if not node.text.strip():
                    continue
                lines_n = Measure.estimate_lines(node.text, node.font_size or 12.0, col.width)
                para_h = Measure.measure_height(lines_n, node.font_size or 12.0)
                para = Paragraph(id=node.id, bbox=BoundingBox(col.x, y, col.width, para_h))
                for li in range(lines_n):
                    line = Line(id=node.id + "_l" + str(li), y=y + li * Measure.LINE_HEIGHT,
                                baseline=y + li * Measure.LINE_HEIGHT + 2)
                    line.bbox = BoundingBox(col.x, y + li * Measure.LINE_HEIGHT, col.width, Measure.LINE_HEIGHT)
                    run = TextRun(id=node.id + "_r" + str(li), text=node.text[li::max(1, lines_n)], font="")
                    line.add_run(run)
                    para.add_line(line)
                page.add_child(para)
                y += para_h + Measure.PARAGRAPH_SPACING
                if y > self.page_height - self.flow.margin_bottom:
                    tree.add_page(page)
                    page = Page(id="page_" + str(tree.page_count + 1),
                                width=self.page_width, height=self.page_height,
                                page_num=tree.page_count)
                    y = self.flow.margin_top
        if page.children:
            tree.add_page(page)
        self._tree = tree
        return tree

    def detect_collisions(self):
        if self._tree is None:
            return []
        nodes = []
        for p in self._tree.pages:
            for child in p.children:
                if hasattr(child, "bbox"):
                    nodes.append(child)
        return CollisionEngine.detect(nodes)

    def rebuild(self, graph):
        self._constraints = ConstraintBuilder.build_from_graph(graph)
        self.constraint_solver.add_constraints(self._constraints)
        self.constraint_solver.solve()
        return self.layout(graph)

__all__ = ["ConstraintType", "LayoutConstraint", "Measure", "FlowResult",
           "Flow", "ConstraintBuilder", "ConstraintSolver", "LayoutEngine",
           "GlyphMetric", "InlineLayout", "ColumnRegion", "ColumnLayout",
           "CollisionRecord", "CollisionEngine"]
