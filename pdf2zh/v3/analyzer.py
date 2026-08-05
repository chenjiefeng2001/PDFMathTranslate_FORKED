"""Module 4: Semantic Analyzer.

Unified document analysis pipeline that operates on the DocumentGraph and:

  1. Refines NodeType labels (heading levels, section structure, caption types)
  2. Detects paragraph boundaries and merges split fragments
  3. Identifies formula, footnote, header/footer elements
  4. Reconstructs reading order using multi-feature analysis
  5. Adds cross-reference edges (caption→figure, citation→reference)

All analysis logic is centralized here rather than scattered across
converter.py, doclayout.py, and layout_graph.py.
"""


from __future__ import annotations

__all__ = ["AnalyzerConfig", "SemanticAnalyzer"]

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from pdf2zh.font_resolver import FontStyle
from pdf2zh.v3.graph import (
    ConstraintPriority,
    DocumentGraph,
    DocumentNode,
    Edge,
    EdgeType,
    NodeType,
)



# ── Configuration ───────────────────────────────────────────────────────


@dataclass
class AnalyzerConfig:
    """Configuration for SemanticAnalyzer."""

    lang_in: str = "auto"
    refine_heading_levels: bool = True
    detect_paragraph_boundaries: bool = True
    detect_formulas: bool = True
    detect_footnotes: bool = True
    detect_headers_footers: bool = True
    detect_captions: bool = True
    detect_sections: bool = True
    detect_references: bool = True
    merge_fragments: bool = True
    heading_font_ratio: float = 1.15
    footnote_font_ratio: float = 0.85
    header_footer_margin: float = 0.1
    use_rule_classifier: bool = True
    """阶段三融合：先用 ``structure.StructureClassifier`` 规则流给未定型
    节点打分，高置信度（>= rule_confidence_threshold）结果直接采纳。"""


# ── Regex patterns ──────────────────────────────────────────────────────

_RE_CITATION = re.compile(
    r"\[\d+\]|\[\d+[,\s–-]+\d+\]|\[\d+–\d+\]|\b(?:et\s+al\.?|vol\.|pp\.|no\.)"
)
_RE_FIGURE_REF = re.compile(r"(?:Fig\.|Figure|FIG\.)\s*\d+")
_RE_TABLE_REF = re.compile(r"(?:Tab\.|Table|TAB\.)\s*\d+")
_RE_EQUATION = re.compile(
    r"^[\s\d+\-*/=(){}^_\\]+$|"
    r"(?:\beq\.?\b|\bequation\b|\bformula\b)",
    re.IGNORECASE,
)
_RE_SECTION_NUM = re.compile(
    r"^(?:I|II|III|IV|V|VI|VII|VIII|IX|X|"
    r"\d+\.?\d*|"
    r"[A-Z]\.)\s+"
)
_RE_FOOTNOTE_MARK = re.compile(r"^[\d†‡*§¶‖#†‡§¶†‡•◊○●]")


class _RuleParagraphAdapter:
    """DocumentNode → ``structure.compute_features`` 所需 Paragraph 鸭子类型。

    只读字段适配（text / lines[].size / words[0].font / alignment /
    line_count / first_line_indent / x0/x1/y0/y1 / size），不创建新对象。
    """

    class _Word:
        def __init__(self, font: str = ""):
            self.font = font

    class _Line:
        def __init__(self, size: float, font: str):
            self.size = size
            self.words = [_RuleParagraphAdapter._Word(font)]

    def __init__(self, node: DocumentNode):
        self.text = node.text or ""
        self.x0, self.y0, self.x1, self.y1 = node.bbox
        self.size = float(node.font_size or 0.0)
        self.first_line_indent = 0.0
        self.alignment = "left"
        self.line_count = max(1, (self.text or "").count("\n") + 1)
        font = node.metadata.get("font_name", "") if node.metadata else ""
        self.lines = [_RuleParagraphAdapter._Line(self.size, font)]
        for _ in range(self.line_count - 1):
            self.lines.append(_RuleParagraphAdapter._Line(self.size, font))



# ── SemanticAnalyzer ────────────────────────────────────────────────────


class SemanticAnalyzer:
    """Centralized document analysis on the DocumentGraph.

    This replaces the scattered analysis logic currently spread across:
      - converter.py (receive_layout, paragraph grouping)
      - layout_graph.py (reading order)
      - doclayout.py (YOLO classification)
    """

    def __init__(self, config: Optional[AnalyzerConfig] = None):
        self.config = config or AnalyzerConfig()

    def analyze(self, graph: DocumentGraph) -> DocumentGraph:
        """Run all enabled analysis passes on the graph.

        Args:
            graph: Input DocumentGraph (from Module 3).

        Returns:
            Same graph instance, modified in-place.
        """
        # 阶段三融合：规则分类器（structure.py）先行打分，高置信度直接采纳
        if self.config.use_rule_classifier:
            self._apply_rule_classifier(graph)
        if self.config.refine_heading_levels:
            self._refine_headings(graph)
        if self.config.detect_captions:
            self._refine_captions(graph)
        if self.config.detect_formulas:
            self._detect_formulas(graph)
        if self.config.detect_footnotes:
            self._detect_footnotes(graph)
        if self.config.detect_headers_footers:
            self._detect_headers_footers(graph)
        if self.config.detect_references:
            self._detect_references(graph)
        if self.config.detect_sections:
            self._detect_sections(graph)
        if self.config.merge_fragments:
            self._merge_fragments(graph)
        if self.config.detect_paragraph_boundaries:
            self._refine_paragraphs(graph)
        return graph

    # 阶段三融合：规则分类器先行（structure.py 规则流 + 图级通道合并）
    _RULE_ROLE_TO_TYPE: Dict["BlockRole", Optional[NodeType]] = None

    @classmethod
    def _rule_type_map(cls) -> Dict["BlockRole", Optional[NodeType]]:
        if cls._RULE_ROLE_TO_TYPE is None:
            from pdf2zh.v3.structure import BlockRole
            cls._RULE_ROLE_TO_TYPE = {
                BlockRole.PAGE_NUMBER: None,
                BlockRole.HEADER: NodeType.HEADER,
                BlockRole.FOOTER: NodeType.FOOTER,
                BlockRole.TOC_ENTRY: NodeType.TOC_ENTRY,
                BlockRole.HEADING: NodeType.HEADING,
                BlockRole.CAPTION: NodeType.CAPTION,
                BlockRole.FOOTNOTE: NodeType.FOOTNOTE,
                BlockRole.FORMULA: NodeType.FORMULA,
                BlockRole.CITATION: NodeType.CITATION,
                BlockRole.BODY_TEXT: None,
                BlockRole.UNKNOWN: None,
            }
        return cls._RULE_ROLE_TO_TYPE

    def _apply_rule_classifier(self, graph: DocumentGraph) -> None:
        """结构规则分类器先行：未定型节点按高置信度规则采纳角色。

        融合点（阶段三"与 analyzer 图级通道融合"）：规则流产出角色与
        置信度写进 ``metadata["analysis.rule_role"]`` /
        ``["analysis.rule_confidence"]``；图级通道（本类其余 pass）只
        在规则未定型或置信度不足时兜底。规则置信度低于阈值不覆盖。
        """
        try:
            from pdf2zh.v3.structure import StructureClassifier
        except Exception:  # noqa: BLE001 — 融合失败即跳过（side-channel 纪律）
            return
        threshold = 0.65
        body_size = self._estimate_graph_body_size(graph)
        type_map = self._rule_type_map()
        for node in graph.nodes:
            if node.node_type not in (NodeType.PARAGRAPH, NodeType.UNKNOWN):
                continue
            if not (node.text or "").strip():
                continue
            try:
                para = _RuleParagraphAdapter(node)
                classified = StructureClassifier(
                    heading_font_ratio=self.config.heading_font_ratio,
                    body_font_size=body_size,
                ).classify_paragraph(para, page=None,
                                     body_font_size=body_size)
            except Exception:  # noqa: BLE001
                continue
            role = classified.role
            conf = classified.confidence
            node.metadata["analysis.rule_role"] = role.value
            node.metadata["analysis.rule_confidence"] = round(conf, 4)
            target = type_map.get(role)
            if target is not None and conf >= threshold:
                node.node_type = target

    @staticmethod
    def _estimate_graph_body_size(graph: DocumentGraph) -> float:
        sizes = [n.font_size for n in graph.nodes
                 if (n.font_size or 0) > 0 and n.text]
        if not sizes:
            return 12.0
        sizes.sort()
        return sizes[len(sizes) // 2]


    def _refine_headings(self, graph: DocumentGraph) -> None:
        """Refine heading detection: assign heading levels (H1-H4)."""
        body_size = self._estimate_body_font_size(graph)
        if body_size <= 0:
            return
        for node in graph.get_nodes_by_type(NodeType.HEADING):
            ratio = node.font_size / body_size if body_size > 0 else 1.0
            if ratio >= 1.8:
                node.metadata["heading_level"] = 1
            elif ratio >= 1.4:
                node.metadata["heading_level"] = 2
            elif ratio >= 1.15:
                node.metadata["heading_level"] = 3
            else:
                node.metadata["heading_level"] = 4
            node.metadata["analysis"] = "font_ratio_heading"
        # Section-number patterns → heading
        for node in graph.get_nodes_by_type(NodeType.PARAGRAPH):
            text = node.text.strip()
            if _RE_SECTION_NUM.match(text) and len(text) <= 80:
                node.node_type = NodeType.HEADING
                node.metadata["heading_level"] = 2
                node.metadata["analysis"] = "section_number_heading"

    def _refine_captions(self, graph: DocumentGraph) -> None:
        """Link captions to figures/tables and detect refs."""
        for cap in graph.get_nodes_by_type(NodeType.CAPTION):
            text_lower = cap.text.lower()
            target_type = None
            if "figure" in text_lower or "fig." in text_lower:
                target_type = NodeType.FIGURE
            elif "table" in text_lower:
                target_type = NodeType.TABLE
            if target_type is None:
                continue
            candidates = [
                n for n in graph.get_nodes_on_page(cap.page_num)
                if n.node_type == target_type
            ]
            if not candidates:
                fig_node = DocumentNode(
                    id=f"syn_{cap.id}",
                    node_type=target_type,
                    bbox=cap.bbox,
                    text=f"[{target_type.value}]",
                    page_num=cap.page_num,
                    metadata={"synthetic": True},
                )
                graph.add_node(fig_node)
                candidates = [fig_node]
            nearest = min(candidates, key=lambda n: abs(n.y0 - cap.y0))
            graph.add_edge(Edge(
                source_id=cap.id, target_id=nearest.id,
                edge_type=EdgeType.CAPTION_OF,
                priority=ConstraintPriority.HARD,
            ))
            nearest.metadata["has_caption"] = True
        for node in graph.get_nodes_by_type(NodeType.PARAGRAPH):
            for m in _RE_FIGURE_REF.finditer(node.text):
                graph.add_edge(Edge(
                    source_id=node.id,
                    target_id=f"ref_{node.id}_{m.start()}",
                    edge_type=EdgeType.REFERENCE,
                    metadata={"ref_text": m.group(), "offset": m.start()},
                ))


    def _detect_formulas(self, graph: DocumentGraph) -> None:
        """Detect formula blocks by symbol density."""
        for node in list(graph.nodes):
            text = node.text.strip()
            if not text:
                continue
            symbols = "+-*/=(){}^_\\∑∫∏∂∆∇√∞≈≠≤≥∈"
            density = sum(1 for c in text if c in symbols)
            sym_ratio = density / max(len(text), 1)
            if sym_ratio > 0.2 and len(text) < 200:
                node.node_type = NodeType.FORMULA
                node.metadata["analysis"] = "symbol_density_formula"
            elif re.search(r'\$.*\$|\\\(.*\\\)', text):
                node.metadata["has_inline_formula"] = True

    def _detect_footnotes(self, graph: DocumentGraph) -> None:
        """Detect footnotes by font size and marker pattern."""
        body_size = self._estimate_body_font_size(graph)
        if body_size <= 0:
            return
        for node in list(graph.nodes):
            if node.node_type != NodeType.PARAGRAPH:
                continue
            if (node.font_size > 0
                    and node.font_size < body_size * 0.9
                    and _RE_FOOTNOTE_MARK.match(node.text.strip())):
                node.node_type = NodeType.FOOTNOTE
                node.metadata["analysis"] = "font_size_footnote"

    def _detect_headers_footers(self, graph: DocumentGraph) -> None:
        """Detect headers/footers by page position."""
        for page_num in sorted(set(n.page_num for n in graph.nodes)):
            pn = graph.get_nodes_on_page(page_num)
            if not pn:
                continue
            pb = self._compute_page_bbox(pn)
            ph = pb[3] - pb[1]
            margin = ph * self.config.header_footer_margin
            for node in pn:
                if node.node_type not in (NodeType.PARAGRAPH, NodeType.HEADING):
                    continue
                if node.y1 >= pb[3] - margin and node.y1 <= pb[3]:
                    node.node_type = NodeType.HEADER
                    node.metadata["analysis"] = "page_position_header"
                elif node.y0 >= pb[1] and node.y0 < pb[1] + margin:
                    node.node_type = NodeType.FOOTER
                    node.metadata["analysis"] = "page_position_footer"


    def _detect_references(self, graph: DocumentGraph) -> None:
        """Detect bibliography/reference sections."""
        for node in list(graph.nodes):
            text = node.text.strip()
            if not text:
                continue
            if text.lower() in ("references", "bibliography", "works cited"):
                node.node_type = NodeType.HEADING
                node.metadata["heading_level"] = 1
                node.metadata["analysis"] = "reference_section"
            elif _RE_CITATION.match(text) and len(text) > 20:
                node.node_type = NodeType.REFERENCE
                node.metadata["analysis"] = "citation_pattern_reference"

    def _detect_sections(self, graph: DocumentGraph) -> None:
        """Build section hierarchy from heading nodes."""
        headings = sorted(
            graph.get_nodes_by_type(NodeType.HEADING),
            key=lambda n: (n.page_num, n.y0),
        )
        stack: List[DocumentNode] = []
        for h in headings:
            level = h.metadata.get("heading_level", 3)
            while stack and stack[-1].metadata.get("heading_level", 0) >= level:
                stack.pop()
            if stack:
                graph.add_edge(Edge(
                    source_id=stack[-1].id, target_id=h.id,
                    edge_type=EdgeType.CONTAINS,
                ))
            stack.append(h)

    def _merge_fragments(self, graph: DocumentGraph) -> None:
        """Merge consecutive same-page fragments into single paragraphs."""
        merged: Set[str] = set()
        for page_num in sorted(set(n.page_num for n in graph.nodes)):
            pn = sorted(
                [n for n in graph.get_nodes_on_page(page_num)
                 if n.node_type in (NodeType.PARAGRAPH, NodeType.CAPTION)
                 and n.id not in merged],
                key=lambda n: n.y0,
            )
            i = 0
            while i < len(pn) - 1:
                a, b = pn[i], pn[i + 1]
                size_ok = (a.font_size == 0 or b.font_size == 0
                           or abs(a.font_size - b.font_size)
                           / max(a.font_size, 1) < 0.1)
                gap = abs(b.y0 - a.y0)
                avg_h = (a.height + b.height) / 2
                gap_ok = gap < avg_h * 1.5
                h_ov = min(a.x1, b.x1) - max(a.x0, b.x0)
                avg_w = (a.width + b.width) / 2
                overlap_ok = h_ov > avg_w * (-0.2)
                if size_ok and gap_ok and overlap_ok:
                    a.text = (a.text + " " + b.text).strip()
                    a.bbox = (min(a.x0, b.x0), min(a.y0, b.y0),
                              max(a.x1, b.x1), max(a.y1, b.y1))
                    a.metadata["merged_from"] = (
                        a.metadata.get("merged_from", []) + [b.id]
                    )
                    merged.add(b.id)
                    pn.pop(i + 1)
                else:
                    i += 1
        graph.nodes = [n for n in graph.nodes if n.id not in merged]

    def _refine_paragraphs(self, graph: DocumentGraph) -> None:
        """Mark paragraph boundaries for translation context."""
        for node in graph.get_nodes_by_type(NodeType.PARAGRAPH):
            text = node.text.rstrip()
            node.metadata["is_paragraph_end"] = bool(
                text and text[-1] in ".?!。？！)"
            )

    @staticmethod
    def _estimate_body_font_size(graph: DocumentGraph) -> float:
        paras = graph.get_nodes_by_type(NodeType.PARAGRAPH)
        if not paras:
            return 12.0
        sizes = sorted(n.font_size for n in paras if n.font_size > 0)
        return sizes[len(sizes) // 2] if sizes else 12.0

    @staticmethod
    def _compute_page_bbox(nodes: List[DocumentNode]) -> Tuple[float, float, float, float]:
        if not nodes:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(n.x0 for n in nodes), min(n.y0 for n in nodes),
                max(n.x1 for n in nodes), max(n.y1 for n in nodes))

logger = logging.getLogger(__name__)
