"""Module: V6.0 Document IR — Multi-Role Semantic Intermediate Representation.

The single source of truth (Phase M / M2) between PDF parsing, translation,
and rendering. Upgrades the Rendering Tree (v3/visual_tree.py) into a Semantic
IR where each node carries FOUR independent roles:

    SemanticRole      — what it IS (body / heading / caption / figure / table / ...)
    ReadingRole       — which reading flow it belongs to (main flow / column 2 / sidebar / ...)
    TranslationRole   — how the translator should treat it (translate / keep term / keep formula / ...)
    RenderingRole     — how the renderer should place it (block / inline / float / anchored)

Hierarchy: Document -> Section -> SemanticBlock -> VisualBlock -> TextRun.
Each node references children by ID (not embedded objects), matching the
four-graph unified node_id namespace (see v3/graph_registry.py).

The IR is fully serializable: to_json() / from_json() produce a stable JSON
schema that can be passed across processes and across the Legacy/V4 runtimes.

Usage::

    from pdf2zh.v3.document_ir import (
        DocumentIR, IRNode, SemanticRole, ReadingRole,
        TranslationRole, RenderingRole, IRBuilder,
    )
    ir = DocumentIR()
    node = ir.add_node("p42", semantic=SemanticRole.BODY_TEXT,
                       translation=TranslationRole.NEED_CONTEXT)
    text = ir.to_json()
    restored = DocumentIR.from_json(text)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SemanticRole(Enum):
    """What a node IS in the document semantics."""

    DOCUMENT = "document"
    SECTION = "section"
    SUBSECTION = "subsection"
    BODY_TEXT = "body_text"
    HEADING = "heading"
    CAPTION = "caption"
    FIGURE = "figure"
    IMAGE = "image"
    TABLE = "table"
    FORMULA = "formula"
    FORMULA_INLINE = "formula_inline"
    TOC_ENTRY = "toc_entry"
    REFERENCE = "reference"
    CITATION = "citation"
    FOOTNOTE = "footnote"
    CODE = "code"
    LIST = "list"
    LIST_ITEM = "list_item"
    ABSTRACT = "abstract"
    KEYWORDS = "keywords"
    HEADER = "header"
    FOOTER = "footer"
    BIBLIOGRAPHY = "bibliography"
    UNKNOWN = "unknown"


class ReadingRole(Enum):
    """Which reading flow a node belongs to."""

    MAIN_FLOW = "main_flow"
    COLUMN_1 = "column_1"
    COLUMN_2 = "column_2"
    SIDEBAR = "sidebar"
    HEADER_FLOW = "header_flow"
    FOOTER_FLOW = "footer_flow"
    FOOTNOTE_FLOW = "footnote_flow"
    FOLLOWS_FIGURE = "follows_figure"
    UNKNOWN = "unknown"


class TranslationRole(Enum):
    """How the translator should treat a node."""

    TRANSLATE = "translate"
    NEED_CONTEXT = "need_context"
    KEEP_TERM = "keep_term"
    KEEP_FORMULA = "keep_formula"
    KEEP_NUMBER = "keep_number"
    SKIP = "skip"
    TRACK = "track"


class RenderingRole(Enum):
    """How the renderer should place a node."""

    BLOCK = "block"
    INLINE = "inline"
    FLOAT = "float"
    ANCHORED = "anchored"
    OVERLAY = "overlay"
    TABLE_CELL = "table_cell"
    TABLE_ROW = "table_row"


# ── IR Node ─────────────────────────────────────────────────────────────


@dataclass
class IRNode:
    """A single node in the Document IR.

    Attributes:
        id: Globally unique node id shared by all four graphs.
        semantic: What this node IS.
        reading: Which reading flow it belongs to.
        translation: How the translator treats it.
        rendering: How the renderer places it.
        parent_id: Parent node id (None for the document root).
        bbox: (x0, y0, x1, y1) in PDF coordinate space.
        text: Extracted text.
        page_num: 0-based page number.
        confidence: Layout/semantic analysis confidence in [0, 1].
        children: Child node ids (reference by id, not embedded).
        metadata: Arbitrary extra attributes.
    """

    id: str
    semantic: SemanticRole = SemanticRole.UNKNOWN
    reading: ReadingRole = ReadingRole.UNKNOWN
    translation: TranslationRole = TranslationRole.TRANSLATE
    rendering: RenderingRole = RenderingRole.BLOCK
    parent_id: Optional[str] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    text: str = ""
    page_num: int = 0
    confidence: float = 1.0
    children: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Coerce string role values to enum members for ergonomic construction.
        if isinstance(self.semantic, str):
            self.semantic = SemanticRole(self.semantic)
        if isinstance(self.reading, str):
            self.reading = ReadingRole(self.reading)
        if isinstance(self.translation, str):
            self.translation = TranslationRole(self.translation)
        if isinstance(self.rendering, str):
            self.rendering = RenderingRole(self.rendering)
        if isinstance(self.bbox, (list, tuple)) and len(self.bbox) == 4:
            self.bbox = tuple(float(v) for v in self.bbox)

    @property
    def x0(self) -> float:
        return self.bbox[0] if self.bbox else 0.0

    @property
    def y0(self) -> float:
        return self.bbox[1] if self.bbox else 0.0

    @property
    def x1(self) -> float:
        return self.bbox[2] if self.bbox else 0.0

    @property
    def y1(self) -> float:
        return self.bbox[3] if self.bbox else 0.0

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    def add_child(self, child_id: str) -> None:
        if child_id not in self.children:
            self.children.append(child_id)

    def remove_child(self, child_id: str) -> None:
        if child_id in self.children:
            self.children.remove(child_id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "semantic": self.semantic.value,
            "reading": self.reading.value,
            "translation": self.translation.value,
            "rendering": self.rendering.value,
            "parent_id": self.parent_id,
            "bbox": list(self.bbox) if self.bbox else None,
            "text": self.text,
            "page_num": self.page_num,
            "confidence": self.confidence,
            "children": list(self.children),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IRNode":
        return cls(
            id=data["id"],
            semantic=data.get("semantic", "unknown"),
            reading=data.get("reading", "unknown"),
            translation=data.get("translation", "translate"),
            rendering=data.get("rendering", "block"),
            parent_id=data.get("parent_id"),
            bbox=tuple(data["bbox"]) if data.get("bbox") else None,
            text=data.get("text", ""),
            page_num=data.get("page_num", 0),
            confidence=data.get("confidence", 1.0),
            children=list(data.get("children", [])),
            metadata=dict(data.get("metadata", {})),
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"IRNode(id={self.id!r}, semantic={self.semantic.value}, "
            f"text={self.text[:24]!r})"
        )


# ── Document IR Container ───────────────────────────────────────────────


class DocumentIR:
    """Container for a full document's semantic IR.

    The hierarchy is:
        Document (root) -> Section -> SemanticBlock -> VisualBlock -> TextRun

    Every node is stored in a flat dict keyed by id; children are references.
    """

    def __init__(
        self, title: str = "", source_lang: str = "en", target_lang: str = "zh-cn"
    ) -> None:
        self.title = title
        self.source_lang = source_lang
        self.target_lang = target_lang
        self._nodes: Dict[str, IRNode] = {}
        self._root_ids: List[str] = []

    # ── Node management ──────────────────────────────────────────

    def add_node(
        self,
        node_id: str,
        semantic: SemanticRole = SemanticRole.UNKNOWN,
        reading: ReadingRole = ReadingRole.UNKNOWN,
        translation: TranslationRole = TranslationRole.TRANSLATE,
        rendering: RenderingRole = RenderingRole.BLOCK,
        parent_id: Optional[str] = None,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        text: str = "",
        page_num: int = 0,
        confidence: float = 1.0,
        metadata: Optional[dict] = None,
    ) -> IRNode:
        """Add (or replace) a node, maintaining the parent/child linkage."""
        node = IRNode(
            id=node_id,
            semantic=semantic,
            reading=reading,
            translation=translation,
            rendering=rendering,
            parent_id=parent_id,
            bbox=bbox,
            text=text,
            page_num=page_num,
            confidence=confidence,
            metadata=dict(metadata or {}),
        )
        existed = node_id in self._nodes
        self._nodes[node_id] = node
        if parent_id is not None:
            parent = self._nodes.get(parent_id)
            if parent is not None:
                parent.add_child(node_id)
            if not existed:
                self._root_ids = [r for r in self._root_ids if r != node_id]
        else:
            if not existed:
                self._root_ids.append(node_id)
        return node

    def get_node(self, node_id: str) -> Optional[IRNode]:
        return self._nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def remove_node(self, node_id: str) -> None:
        node = self._nodes.pop(node_id, None)
        if node is None:
            return
        if node.parent_id and node.parent_id in self._nodes:
            self._nodes[node.parent_id].remove_child(node_id)
        for child_id in list(node.children):
            child = self._nodes.get(child_id)
            if child is not None:
                child.parent_id = None
                self._root_ids.append(child_id)
        self._root_ids = [r for r in self._root_ids if r != node_id]

    def children_of(self, node_id: str) -> List[IRNode]:
        node = self._nodes.get(node_id)
        if node is None:
            return []
        return [self._nodes[c] for c in node.children if c in self._nodes]

    def parent_of(self, node_id: str) -> Optional[IRNode]:
        node = self._nodes.get(node_id)
        if node is None or node.parent_id is None:
            return None
        return self._nodes.get(node.parent_id)

    def set_roles(
        self,
        node_id: str,
        *,
        semantic: Optional[SemanticRole] = None,
        reading: Optional[ReadingRole] = None,
        translation: Optional[TranslationRole] = None,
        rendering: Optional[RenderingRole] = None,
    ) -> Optional[IRNode]:
        node = self._nodes.get(node_id)
        if node is None:
            return None
        if semantic is not None:
            node.semantic = (
                semantic
                if isinstance(semantic, SemanticRole)
                else SemanticRole(semantic)
            )
        if reading is not None:
            node.reading = (
                reading if isinstance(reading, ReadingRole) else ReadingRole(reading)
            )
        if translation is not None:
            node.translation = (
                translation
                if isinstance(translation, TranslationRole)
                else TranslationRole(translation)
            )
        if rendering is not None:
            node.rendering = (
                rendering
                if isinstance(rendering, RenderingRole)
                else RenderingRole(rendering)
            )
        return node

    # ── Traversal ─────────────────────────────────────────────────

    def walk(self, node_id: Optional[str] = None) -> Generator[IRNode, None, None]:
        """Depth-first walk of the whole IR (or a subtree rooted at node_id)."""
        if node_id is not None:
            node = self._nodes.get(node_id)
            if node is None:
                return
            yield node
            for child_id in node.children:
                yield from self.walk(child_id)
            return
        for root_id in list(self._root_ids):
            yield from self.walk(root_id)

    def nodes(self) -> List[IRNode]:
        return list(self._nodes.values())

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def root_ids(self) -> List[str]:
        return [
            n.id
            for n in self._nodes.values()
            if n.parent_id is None or n.parent_id not in self._nodes
        ]

    def find_by_semantic(self, role: SemanticRole) -> List[IRNode]:
        if isinstance(role, str):
            role = SemanticRole(role)
        return [n for n in self._nodes.values() if n.semantic == role]

    def find_by_translation(self, role: TranslationRole) -> List[IRNode]:
        if isinstance(role, str):
            role = TranslationRole(role)
        return [n for n in self._nodes.values() if n.translation == role]

    def nodes_on_page(self, page_num: int) -> List[IRNode]:
        return [n for n in self._nodes.values() if n.page_num == page_num]

    def find(self, text: str, case_sensitive: bool = False) -> List[IRNode]:
        needle = text if case_sensitive else text.lower()
        return [
            n
            for n in self._nodes.values()
            if needle in (n.text if case_sensitive else n.text.lower())
        ]

    def to_text(self) -> str:
        return "\n".join(n.text for n in self.walk() if n.text)

    # ── Serialization ─────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "title": self.title,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "nodes": [n.to_dict() for n in self._nodes.values()],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentIR":
        ir = cls(
            title=data.get("title", ""),
            source_lang=data.get("source_lang", "en"),
            target_lang=data.get("target_lang", "zh-cn"),
        )
        for nd in data.get("nodes", []):
            node = IRNode.from_dict(nd)
            ir._nodes[node.id] = node
        ir._root_ids = list(ir.root_ids)
        return ir

    @classmethod
    def from_json(cls, text: str) -> "DocumentIR":
        return cls.from_dict(json.loads(text))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"DocumentIR(nodes={self.node_count}, roots={len(self._root_ids)})"


# ── IR Builder ──────────────────────────────────────────────────────────


_SEMANTIC_MAP = {
    "document": SemanticRole.DOCUMENT,
    "page": SemanticRole.SECTION,
    "section": SemanticRole.SECTION,
    "subsection": SemanticRole.SUBSECTION,
    "paragraph": SemanticRole.BODY_TEXT,
    "heading": SemanticRole.HEADING,
    "caption": SemanticRole.CAPTION,
    "figure": SemanticRole.FIGURE,
    "image": SemanticRole.IMAGE,
    "table": SemanticRole.TABLE,
    "formula": SemanticRole.FORMULA,
    "formula_inline": SemanticRole.FORMULA_INLINE,
    "toc_entry": SemanticRole.TOC_ENTRY,
    "reference": SemanticRole.REFERENCE,
    "bibliography": SemanticRole.BIBLIOGRAPHY,
    "citation": SemanticRole.CITATION,
    "footnote": SemanticRole.FOOTNOTE,
    "code": SemanticRole.CODE,
    "list": SemanticRole.LIST,
    "list_item": SemanticRole.LIST_ITEM,
    "abstract": SemanticRole.ABSTRACT,
    "keywords": SemanticRole.KEYWORDS,
    "header": SemanticRole.HEADER,
    "footer": SemanticRole.FOOTER,
}

_TRANSLATION_MAP = {
    SemanticRole.FORMULA: TranslationRole.KEEP_FORMULA,
    SemanticRole.FORMULA_INLINE: TranslationRole.KEEP_FORMULA,
    SemanticRole.REFERENCE: TranslationRole.TRACK,
    SemanticRole.CITATION: TranslationRole.KEEP_NUMBER,
    SemanticRole.CODE: TranslationRole.KEEP_FORMULA,
    SemanticRole.CAPTION: TranslationRole.NEED_CONTEXT,
    SemanticRole.ABSTRACT: TranslationRole.NEED_CONTEXT,
    SemanticRole.IMAGE: TranslationRole.SKIP,
    SemanticRole.TOC_ENTRY: TranslationRole.TRANSLATE,
}

_RENDERING_MAP = {
    SemanticRole.FIGURE: RenderingRole.FLOAT,
    SemanticRole.IMAGE: RenderingRole.FLOAT,
    SemanticRole.TABLE: RenderingRole.FLOAT,
    SemanticRole.HEADER: RenderingRole.OVERLAY,
    SemanticRole.FOOTER: RenderingRole.OVERLAY,
    SemanticRole.FOOTNOTE: RenderingRole.ANCHORED,
    SemanticRole.FORMULA: RenderingRole.BLOCK,
}


class IRBuilder:
    """Build a DocumentIR from a DocumentGraph (and optionally a Page tree).

    The builder maps DocumentNode.node_type onto SemanticRole and derives the
    remaining roles via the role maps above. It preserves page containers as
    Section nodes so the IR hierarchy stays close to the original document.
    """

    def __init__(
        self, title: str = "", source_lang: str = "en", target_lang: str = "zh-cn"
    ) -> None:
        self.title = title
        self.source_lang = source_lang
        self.target_lang = target_lang

    @staticmethod
    def semantic_for(node_type) -> SemanticRole:
        name = node_type.value if hasattr(node_type, "value") else str(node_type)
        return _SEMANTIC_MAP.get(name.lower(), SemanticRole.UNKNOWN)

    def build(self, graph, use_page_sections: bool = True) -> DocumentIR:
        """Build an IR from a DocumentGraph-like object.

        Args:
            graph: DocumentGraph with .nodes / .get_node(node_id).
            use_page_sections: wrap page nodes as Section containers.
        """
        ir = DocumentIR(
            title=self.title, source_lang=self.source_lang, target_lang=self.target_lang
        )
        nodes = list(getattr(graph, "nodes", []) or [])

        page_section: Dict[int, str] = {}
        for n in nodes:
            semantic = self.semantic_for(n.node_type)
            translation = _TRANSLATION_MAP.get(semantic, TranslationRole.TRANSLATE)
            rendering = _RENDERING_MAP.get(semantic, RenderingRole.BLOCK)
            parent_id = None
            if use_page_sections and semantic not in (
                SemanticRole.DOCUMENT,
                SemanticRole.SECTION,
            ):
                page_id = f"page_{n.page_num}"
                if not ir.has_node(page_id):
                    ir.add_node(
                        page_id,
                        semantic=SemanticRole.SECTION,
                        reading=ReadingRole.MAIN_FLOW,
                        parent_id=None,
                        bbox=n.bbox,
                        text=f"Page {n.page_num + 1}",
                        page_num=n.page_num,
                    )
                page_section.setdefault(n.page_num, page_id)
                parent_id = page_id

            ir.add_node(
                n.id,
                semantic=semantic,
                reading=ReadingRole.MAIN_FLOW,
                translation=translation,
                rendering=rendering,
                parent_id=parent_id,
                bbox=n.bbox,
                text=n.text,
                page_num=n.page_num,
                confidence=n.confidence,
                metadata={"node_type": getattr(n.node_type, "value", str(n.node_type))},
            )

        # Link follow edges into reading role hints for captions following figures.
        edges = list(getattr(graph, "edges", []) or [])
        for e in edges:
            etype = (
                e.edge_type.value if hasattr(e.edge_type, "value") else str(e.edge_type)
            )
            if etype in ("caption_of", "reference"):
                target = ir.get_node(e.target_id)
                if target is not None:
                    target.reading = ReadingRole.FOLLOWS_FIGURE
        return ir

    @staticmethod
    def from_graph(
        graph, title: str = "", source_lang: str = "en", target_lang: str = "zh-cn"
    ) -> DocumentIR:
        return IRBuilder(title, source_lang, target_lang).build(graph)


__all__ = [
    "SemanticRole",
    "ReadingRole",
    "TranslationRole",
    "RenderingRole",
    "IRNode",
    "DocumentIR",
    "IRBuilder",
]
