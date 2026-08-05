"""Module: StructureFusion — Structure Classifier 与 analyzer.py 图级通道的融合。

阶段三的规则分类器（``structure.StructureClassifier``）在 **块级** 产出角色
（heading/caption/toc_entry/...），而 ``analyzer.SemanticAnalyzer`` 在
**图级** 用上下文（页面位置 / 字号比 / FOLLOWS 边）细化节点。两者此前
并存但没有数据流：分类器不知道图，analyzer 不消费分类器输出。

本模块实现**融合通道**：

    PageGeometry ──StructureClassifier──▶ 块级角色（含置信度）
                                                │  fuse（按文本/bbox 匹配到节点）
                                                ▼
    DocumentGraph ───────────────────────▶ 节点语义化（node_type + metadata）
                                                │
                                                ▼
    SemanticAnalyzer（图级细化）──────────▶ 关系/层级/段落边界最终化

输出 ``FusionReport``：分类器命中数、分析器细化数、融合后按角色的节点统计。
融合是幂等的：重复运行不改变已定角色（避免分类器与分析器互相覆盖）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType

# BlockRole → NodeType 映射（BlockRole 枚举名即 value，直接查 _BLOCK_ROLE_VALUES）
_BLOCK_ROLE_TO_TYPE: Dict[str, NodeType] = {
    "page_number": NodeType.HEADER,       # 页码归入页眉流
    "header": NodeType.HEADER,
    "footer": NodeType.FOOTER,
    "toc_entry": NodeType.TOC_ENTRY,
    "heading": NodeType.HEADING,
    "caption": NodeType.CAPTION,
    "footnote": NodeType.FOOTNOTE,
    "formula": NodeType.FORMULA,
    "citation": NodeType.CITATION,
    "body_text": NodeType.PARAGRAPH,
    "unknown": NodeType.UNKNOWN,
}

# 分类器已定角色后，分析器不再改写 node_type 的角色集合（避免互踩）
_FROZEN_TYPES = frozenset({
    NodeType.TOC_ENTRY, NodeType.CAPTION, NodeType.FOOTNOTE,
    NodeType.HEADER, NodeType.FOOTER, NodeType.FORMULA, NodeType.CITATION,
})


@dataclass
class FusionReport:
    """融合过程统计。"""

    classified: int = 0
    refined: int = 0
    skipped: int = 0
    role_counts: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "classified": self.classified,
            "refined": self.refined,
            "skipped": self.skipped,
            "role_counts": dict(self.role_counts),
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        return (f"StructureFusion classified={self.classified} "
                f"refined={self.refined} skipped={self.skipped} "
                f"roles={self.role_counts}")


class StructureFusion:
    """把分类器角色融合进 DocumentGraph，再交给图级分析器细化。

    ``fuse`` 的分类器输入可以是 ``PageGeometry`` 列表（走完整
    ``classify_page``），也可以是轻量 ``(text, node)`` 直接计算特征
    （无几何模型时），保持纯逻辑可测。
    """

    def __init__(self, classifier=None, min_confidence: float = 0.55) -> None:
        from pdf2zh.v3.structure import StructureClassifier
        self.classifier = classifier or StructureClassifier()
        self.min_confidence = min_confidence

    # ── 融合入口 ───────────────────────────────────────────────────

    def fuse(self, graph: DocumentGraph,
             pages: Optional[Sequence] = None) -> FusionReport:
        """把分类器（块级）角色融合进图（节点级），再跑图级细化。"""
        report = FusionReport()
        if pages:
            self._fuse_from_pages(graph, pages, report)
        else:
            self._fuse_lightweight(graph, report)
        self._refine_graph(graph, report)
        for node in graph.nodes:
            key = node.node_type.value if hasattr(node.node_type, "value") \
                else str(node.node_type)
            report.role_counts[key] = report.role_counts.get(key, 0) + 1
        return report

    # ── 块级 → 图级融合 ─────────────────────────────────────────────

    def _fuse_from_pages(self, graph: DocumentGraph, pages: Sequence,
                         report: FusionReport) -> None:
        body = self.classifier.estimate_body_font_size(list(pages))
        for page in pages:
            for block in self.classifier.classify_page(page, body_font_size=body):
                if block.confidence < self.min_confidence:
                    continue
                node = self._match_node(graph, block.paragraph.text,
                                        block.paragraph.page_num)
                if node is None:
                    report.skipped += 1
                    continue
                self._apply_role(node, block.role.value, block.confidence,
                                 report, f"classifier:{block.role.value}")

    def _fuse_lightweight(self, graph: DocumentGraph, report: FusionReport) -> None:
        """无几何模型时：直接用节点文本跑特征 + 判定（退化融合）。"""
        for node in graph.nodes:
            if node.node_type not in (NodeType.PARAGRAPH, NodeType.UNKNOWN):
                continue
            if not (node.text or "").strip():
                continue
            from pdf2zh.v3.structure import compute_features
            proxy = _NodeParagraphProxy(node)
            features = compute_features(proxy, body_font_size=None)
            classified = self.classifier.classify_paragraph(
                proxy, body_font_size=None)
            if classified.confidence < self.min_confidence:
                continue
            self._apply_role(node, classified.role.value, classified.confidence,
                             report, "classifier:lightweight")

    # ── 图级细化（analyzer 通道融合） ───────────────────────────────

    def _refine_graph(self, graph: DocumentGraph, report: FusionReport) -> None:
        from pdf2zh.v3.analyzer import AnalyzerConfig, SemanticAnalyzer
        before = {(n.id, n.node_type) for n in graph.nodes}
        analyzer = SemanticAnalyzer(AnalyzerConfig(
            refine_heading_levels=True,
            detect_captions=False,       # 分类器已定 CAPTION，避免重复
            detect_footnotes=False,
            detect_headers_footers=True,
            detect_sections=True,
            detect_references=False,
            merge_fragments=False,
            detect_formulas=False,
        ))
        analyzer.analyze(graph)
        for n in graph.nodes:
            if (n.id, n.node_type) not in before:
                report.refined += 1

    # ── 工具 ────────────────────────────────────────────────────────

    @staticmethod
    def _match_node(graph: DocumentGraph, text: str,
                    page_num: int) -> Optional[DocumentNode]:
        for node in graph.get_nodes_on_page(page_num):
            if node.text.strip() == text.strip():
                return node
        return None

    def _apply_role(self, node: DocumentNode, role_value: str,
                    confidence: float, report: FusionReport, note: str) -> None:
        ntype = _BLOCK_ROLE_TO_TYPE.get(role_value)
        if ntype is None:
            report.skipped += 1
            return
        # 不覆盖更专门类型（图片/表格/代码等已由 RAW 处理器定型的节点）
        if node.node_type not in (NodeType.PARAGRAPH, NodeType.UNKNOWN) \
                and node.node_type != ntype:
            report.skipped += 1
            return
        if node.node_type != ntype:
            node.node_type = ntype
        node.metadata["semantic"] = dict(
            node.metadata.get("semantic", {}),
            structure={"role": role_value, "confidence": round(confidence, 4)},
        )
        node.metadata.setdefault("policy_reasons", [])
        node.metadata["policy_reasons"].append(note)
        report.classified += 1


class _NodeParagraphProxy:
    """把 DocumentNode 伪装成 geometry.Paragraph（轻量融合用）。

    只提供 compute_features 需要的属性：text / lines(size, font, words) /
    first_line_indent / alignment / line_count / x0..y1 / page_num。
    """

    def __init__(self, node: DocumentNode) -> None:
        self.text = node.text
        self.page_num = node.page_num
        bbox = getattr(node, "bbox", None) or (0, 0, 0, 0)
        self.x0, self.y0 = float(bbox[0]), float(bbox[1])
        self.x1, self.y1 = float(bbox[2]), float(bbox[3])
        size = float(getattr(node, "font_size", 12.0) or 12.0)
        self._line = _LineProxy(size)

    @property
    def lines(self) -> List["_LineProxy"]:
        return [self._line]

    @property
    def first_line_indent(self) -> float:
        return 0.0

    @property
    def alignment(self) -> str:
        return "left"

    @property
    def line_count(self) -> int:
        return 1


class _LineProxy:
    """Line 的最小代理（只给 compute_features 读 size/words）。"""

    def __init__(self, size: float) -> None:
        self.size = size
        self.words = [_WordProxy()]


class _WordProxy:
    """Word 的最小代理（compute_features 读 words[0].font）。"""

    @property
    def font(self) -> str:
        return ""


__all__ = [
    "FusionReport", "StructureFusion", "_BLOCK_ROLE_TO_TYPE",
]