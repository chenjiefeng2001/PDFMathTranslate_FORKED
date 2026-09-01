"""Semantic PDF data model — phase 0 of the semantic parser plan.

Defines the span/block/region vocabulary that the downstream detectors
(:mod:`pdf2zh.semantic.code_detector`, :mod:`pdf2zh.semantic.style_detector`,
:mod:`pdf2zh.semantic.list_detector`) produce, plus the **semantic node**
hierarchy (:class:`SemanticNode` and subclasses) that is the final structure
of a parsed document:

    Document
     ├── Paragraph
     ├── Heading
     ├── CodeBlock
     ├── List
     │    ├── ListItem
     │    └── ListItem
     ├── Table
     ├── Figure
     ├── Formula
     └── TOC

``RegionType`` is the *recognition result*; a ``SemanticNode`` is the
*structure* that carries it together with its :class:`ProtectionPolicy` —
what the pipeline is allowed to do with it. Translation may only ever modify
``span.text``; everything else (font, size, style, geometry, region type,
bookmark/TOC structure) is decided here and carried through untouched.

The legacy converter currently feeds :class:`TextBlock` objects derived from
its existing character stacks; future work migrates more of the pipeline
onto this node model (layout regions, TOC, bookmarks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class RegionType(Enum):
    """Semantic region classification for a block.

    ``resolve_region`` (planned) raises more-specific labels (Formula >
    Code > Table > Figure > TOC > Caption > Title > Text); this enum is the
    shared vocabulary so labels can never drift between detectors.
    """

    TEXT = "text"
    TITLE = "title"
    SECTION = "section"
    CODE = "code"
    FORMULA = "formula"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    TOC = "toc"
    HEADER = "header"
    FOOTER = "footer"
    LIST = "list"


class ProtectionPolicy(Enum):
    """What the translation pipeline may do with a semantic node.

    Central policy vocabulary so new region kinds (caption, footnote,
    header/footer, algorithm, equation label, reference, …) can be added
    without touching the converter:
    """

    TRANSLATE = "translate"  # normal paragraph
    PRESERVE = "preserve"  # never translated (code/formula/figure)
    TRANSLATE_KEEP_STYLE = "translate_keep_style"  # translate + restore bold/italic
    TRANSLATE_KEEP_GEOMETRY = (
        "translate_keep_geometry"  # translate + keep markers/alignment
    )


#: Default policy per region (the plan's policy table).
REGION_POLICY: dict[RegionType, ProtectionPolicy] = {
    RegionType.TEXT: ProtectionPolicy.TRANSLATE,
    RegionType.TITLE: ProtectionPolicy.TRANSLATE_KEEP_STYLE,
    RegionType.SECTION: ProtectionPolicy.TRANSLATE_KEEP_STYLE,
    RegionType.CODE: ProtectionPolicy.PRESERVE,
    RegionType.FORMULA: ProtectionPolicy.PRESERVE,
    RegionType.TABLE: ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY,
    RegionType.FIGURE: ProtectionPolicy.PRESERVE,
    RegionType.CAPTION: ProtectionPolicy.TRANSLATE,
    RegionType.FOOTNOTE: ProtectionPolicy.TRANSLATE,
    RegionType.TOC: ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY,
    RegionType.HEADER: ProtectionPolicy.PRESERVE,
    RegionType.FOOTER: ProtectionPolicy.PRESERVE,
    RegionType.LIST: ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY,
}


@dataclass(frozen=True)
class SpanStyle:
    """Bold / italic weight of a span (derived from font name + flags)."""

    bold: bool = False
    italic: bool = False

    @property
    def styled(self) -> bool:
        return self.bold or self.italic


@dataclass
class TextSpan:
    """An atomic run of text with its full style + geometry provenance.

    ``translatable=False`` marks regions that must never enter the
    translator (code, formula, figure); ``translate()`` must leave them
    byte-for-byte identical.
    """

    text: str
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    font_name: str = ""
    font_size: float = 12.0
    style: SpanStyle = field(default_factory=SpanStyle)
    color: int | None = None
    page: int = 0
    block_id: int | None = None
    region_type: RegionType = RegionType.TEXT
    translatable: bool = True


@dataclass
class TextBlock:
    """Pre-node container (legacy compatibility).

    Kept for the current converter bridge; new code should build semantic
    nodes (:class:`SemanticNode` subclasses) instead of accumulating boolean
    flags on this dataclass.
    """

    spans: list[TextSpan] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    region_type: RegionType = RegionType.TEXT
    translatable: bool = True

    @property
    def text(self) -> str:
        return "".join(sp.text for sp in self.spans)

    def must_translate(self) -> bool:
        """True when the whole block should be fed to the translator."""
        return self.translatable and any(sp.translatable for sp in self.spans)


# ── Semantic node hierarchy ────────────────────────────────────────────


@dataclass
class SemanticNode:
    """Base node: a region together with what the pipeline may do with it.

    ``region_type`` is the recognition result; ``policy`` is the processing
    contract. ``children`` holds nested nodes (e.g. ``List → ListItem →
    List``). Use :meth:`walk` for a depth-first traversal.
    """

    region_type: RegionType = RegionType.TEXT
    policy: ProtectionPolicy = ProtectionPolicy.TRANSLATE
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    children: list["SemanticNode"] = field(default_factory=list)

    def walk(self) -> Iterator["SemanticNode"]:
        """Depth-first traversal including self."""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class ParagraphNode(SemanticNode):
    """A normal text paragraph (policy: TRANSLATE)."""

    text: str = ""
    spans: list[TextSpan] = field(default_factory=list)
    region_type: RegionType = RegionType.TEXT
    policy: ProtectionPolicy = ProtectionPolicy.TRANSLATE


@dataclass
class HeadingNode(ParagraphNode):
    """A title/heading (policy: TRANSLATE_KEEP_STYLE)."""

    level: int = 1
    region_type: RegionType = RegionType.TITLE
    policy: ProtectionPolicy = ProtectionPolicy.TRANSLATE_KEEP_STYLE


@dataclass
class CodeBlockNode(SemanticNode):
    """A protected code region (policy: PRESERVE, never translated)."""

    lines: list[str] = field(default_factory=list)
    region_type: RegionType = RegionType.CODE
    policy: ProtectionPolicy = ProtectionPolicy.PRESERVE

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class ListItemNode(SemanticNode):
    """One list item: marker + content + continuation lines.

    ``children`` may hold a nested :class:`ListNode` (deeper indent).

    Geometry (Commit 4): the renderer needs *where* the marker sits and
    *where* the content starts — ``indent`` alone is not enough. All values
    come from the original PDF (never recomputed from ``level``):

    - ``marker_x``    — x of the marker glyphs (≈ indent / leading x0)
    - ``marker_width``— measured marker advance width
    - ``content_x``   — x where content (and continuation lines) start
    - ``content_width``— available wrap width for the content
    - ``y``           — baseline of the first line
    """

    marker: str = ""
    marker_type: str = ""
    content: str = ""
    continuation: list[str] = field(default_factory=list)
    level: int = 0
    indent: float = 0.0
    marker_x: float = 0.0
    marker_width: float = 0.0
    content_x: float = 0.0
    content_width: float = 0.0
    y: float = 0.0
    region_type: RegionType = RegionType.LIST
    policy: ProtectionPolicy = ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY

    def to_dict(self) -> dict:
        return {
            "marker": self.marker,
            "marker_type": self.marker_type,
            "content": self.content,
            "continuation": list(self.continuation),
            "level": self.level,
            "indent": round(self.indent, 1),
            "marker_x": round(self.marker_x, 1),
            "content_x": round(self.content_x, 1),
            "continuation_x": round(self.content_x, 1),
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class ListNode(SemanticNode):
    """An ordered run of list items at one indent level."""

    items: list[ListItemNode] = field(default_factory=list)
    level: int = 0
    region_type: RegionType = RegionType.LIST
    policy: ProtectionPolicy = ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY

    def walk(self) -> Iterator["SemanticNode"]:
        """Depth-first traversal: the list, then each item (and its subtree)."""
        yield self
        for item in self.items:
            yield from item.walk()

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "items": [it.to_dict() for it in self.items],
        }


@dataclass
class TOCEntryNode(SemanticNode):
    """One visual TOC entry (Commit 6A — see :mod:`pdf2zh.semantic.toc_parser`).

    ``page_number`` (the printed number in the right column) and
    ``destination_page`` (an optional resolved *document* page index) are
    deliberately **two independent fields**: the printed label is never
    assumed to equal the final target page.

    Geometry comes from the original PDF: ``indent`` / ``title_x`` (left edge
    of the title), ``page_x`` (right-aligned page-number column), ``bbox``.
    ``dot_leader`` / ``leader_present`` carry the leader evidence verbatim.
    ``continuation`` holds extra follow-on lines of a multi-line entry.
    """

    title: str = ""
    title_spans: list[TextSpan] = field(default_factory=list)
    level: int = 0
    page_number: str = ""
    destination_page: int | None = None
    indent: float = 0.0
    title_x: float = 0.0
    page_x: float = 0.0
    dot_leader: str = ""
    leader_present: bool = False
    continuation: list[str] = field(default_factory=list)
    region_type: RegionType = RegionType.TOC
    policy: ProtectionPolicy = ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "level": self.level,
            "page_number": self.page_number,
            "destination_page": self.destination_page,
            "indent": round(self.indent, 1),
            "title_x": round(self.title_x, 1),
            "page_x": round(self.page_x, 1),
            "dot_leader": self.dot_leader,
            "leader_present": self.leader_present,
            "continuation": list(self.continuation),
        }


@dataclass
class TOCNode(SemanticNode):
    """A visual Table of Contents region on a page (Commit 6A).

    ``entries`` is the ordered list of :class:`TOCEntryNode`. ``is_toc_page``
    / ``has_header`` record the page-level evidence that promoted this region.
    """

    entries: list[TOCEntryNode] = field(default_factory=list)
    is_toc_page: bool = False
    has_header: bool = False
    header_text: str | None = None
    region_type: RegionType = RegionType.TOC
    policy: ProtectionPolicy = ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY

    def walk(self) -> Iterator["SemanticNode"]:
        """Depth-first traversal: the TOC, then each entry (and its subtree)."""
        yield self
        for entry in self.entries:
            yield from entry.walk()

    def to_dict(self) -> dict:
        return {
            "is_toc_page": self.is_toc_page,
            "has_header": self.has_header,
            "header_text": self.header_text,
            "entries": [e.to_dict() for e in self.entries],
        }


__all__ = [
    "RegionType",
    "ProtectionPolicy",
    "REGION_POLICY",
    "SpanStyle",
    "TextSpan",
    "TextBlock",
    "SemanticNode",
    "ParagraphNode",
    "HeadingNode",
    "CodeBlockNode",
    "ListItemNode",
    "ListNode",
    "TOCEntryNode",
    "TOCNode",
]
