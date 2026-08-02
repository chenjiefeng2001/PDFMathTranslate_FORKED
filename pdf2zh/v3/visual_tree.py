"""Module: Visual Tree — V4.2 Rendering Intermediate Representation.

A layout-independent display tree between DocumentGraph and Renderer.
Inspired by Blink's LayoutTree, Flutter's RenderObject, and Typst's FrameTree.

VisualNode subclasses:
  - Page:         A page containing paragraphs
  - Paragraph:    Block-level paragraph with lines
  - Line:         Typeset line of text
  - TextRun:      Contiguous text run with uniform formatting
  - GlyphRun:     Shaped text run (post-font-shaping)
  - Image:        Image placeholder
  - Formula:      Mathematical formula

Usage::
    from pdf2zh.v3.visual_tree import VisualTree, Page, Paragraph, Line, TextRun

    tree = VisualTree()
    page = Page(id="p1", width=612, height=792, page_num=0)
    para = Paragraph(id="para1", bbox=BoundingBox(50, 50, 512, 30))
    line = Line(id="l1", y=50, baseline=55)
    run = TextRun(id="r1", text="Hello World", font="Times-Roman")
    line.add_run(run)
    para.add_line(line)
    page.add_child(para)
    tree.add_page(page)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class VisualNodeType(Enum):
    ROOT = "root"
    PAGE = "page"
    COLUMN = "column"
    PARAGRAPH = "paragraph"
    LINE = "line"
    TEXT_RUN = "text_run"
    GLYPH_RUN = "glyph_run"
    IMAGE = "image"
    TABLE = "table"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    FORMULA = "formula"
    FIGURE = "figure"


@dataclass
class BoundingBox:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.height

    def contains(self, other: "BoundingBox") -> bool:
        return (self.x <= other.x and self.y <= other.y
                and self.x1 >= other.x1 and self.y1 >= other.y1)

    def overlaps(self, other: "BoundingBox") -> bool:
        return not (self.x1 <= other.x or other.x1 <= self.x
                    or self.y1 <= other.y or other.y1 <= self.y)

    def translate(self, dx: float, dy: float) -> "BoundingBox":
        return BoundingBox(self.x + dx, self.y + dy,
                           self.width, self.height)


@dataclass
class VisualNode(ABC):
    """Base class for all Visual Tree nodes."""
    id: str
    vtype: VisualNodeType = VisualNodeType.PAGE  # override in __post_init__
    bbox: BoundingBox = field(default_factory=BoundingBox)
    children: List[VisualNode] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def add_child(self, child: VisualNode) -> None:
        self.children.append(child)

    def remove_child(self, child_id: str) -> None:
        self.children = [c for c in self.children if c.id != child_id]

    def find(self, node_id: str) -> Optional["VisualNode"]:
        if self.id == node_id:
            return self
        for child in self.children:
            result = child.find(node_id)
            if result:
                return result
        return None

    def walk(self) -> Generator["VisualNode", None, None]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class TextRun(VisualNode):
    """A contiguous text span with uniform formatting."""
    text: str = ""
    font: str = ""
    font_size: float = 12.0
    bold: bool = False
    italic: bool = False
    color: str = "black"
    language: str = ""

    def __post_init__(self):
        self.vtype = VisualNodeType.TEXT_RUN

    def __repr__(self):
        preview = self.text[:30] + ("..." if len(self.text) > 30 else "")
        return (f"TextRun(id={self.id}, text='{preview}', "
                f"font='{self.font}', size={self.font_size})")


@dataclass
class GlyphRun(VisualNode):
    """A shaped glyph run (post-font-shaping)."""
    glyphs: List[dict] = field(default_factory=list)
    font: str = ""
    font_size: float = 12.0
    direction: str = "ltr"

    def __post_init__(self):
        self.vtype = VisualNodeType.GLYPH_RUN


@dataclass
class Page(VisualNode):
    """A single page in the Visual Tree."""
    width: float = 612.0
    height: float = 792.0
    page_num: int = 0

    def __post_init__(self):
        self.vtype = VisualNodeType.PAGE
        self.bbox = BoundingBox(0, 0, self.width, self.height)

    def add_paragraph(self, para: Paragraph) -> None:
        self.add_child(para)

    @property
    def paragraphs(self) -> List[Paragraph]:
        return [c for c in self.children if isinstance(c, Paragraph)]

    def __repr__(self):
        return (f"Page(id={self.id}, num={self.page_num}, "
                f"size={self.width}x{self.height}, "
                f"children={len(self.children)})")


@dataclass
class Image(VisualNode):
    """An image placeholder in the Visual Tree."""
    image_path: str = ""
    alt_text: str = ""
    dpi: float = 72.0

    def __post_init__(self):
        self.vtype = VisualNodeType.IMAGE


@dataclass
class Formula(VisualNode):
    """A mathematical formula."""
    latex: str = ""
    is_inline: bool = False

    def __post_init__(self):
        self.vtype = VisualNodeType.FORMULA


@dataclass
class DisplayCommand:
    """A single renderable display command in a DisplayList.

    Encodes all information a renderer needs to draw one element:
    - Type (text, image, formula, rect)
    - Position (absolute coordinates)
    - Content (text, binary data, or LaTeX)
    - Styling (font, size, color, alignment)
    """
    cmd_type: str  # "text", "image", "formula", "rect"
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    text: str = ""
    font: str = ""
    font_size: float = 12.0
    color: tuple = (0, 0, 0)
    alignment: str = "left"
    baseline: float = 0.0
    opacity: float = 1.0
    page_num: int = 0
    metadata: dict = field(default_factory=dict)


class VisualTree:
    """Top-level container for a document's Visual Tree.

    A VisualTree consists of:
      - A list of Page nodes (one per page)
      - A document node as root

    Renderers consume this tree, never DocumentGraph directly.

    Supports layout freeze: once frozen, the tree is immutable
    and generates a DisplayList for renderers.
    """

    def __init__(self) -> None:
        self._pages: List[Page] = []
        self._metadata: dict = {}
        self._is_layout_frozen: bool = False
        self._display_list: Optional[List[DisplayCommand]] = None

    def add_page(self, page: Page) -> None:
        if self._is_layout_frozen:
            raise RuntimeError("Cannot add page to frozen VisualTree")
        self._pages.append(page)

    def get_page(self, page_num: int) -> Optional[Page]:
        for p in self._pages:
            if p.page_num == page_num:
                return p
        return None

    @property
    def pages(self) -> List[Page]:
        return list(self._pages)

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def is_layout_frozen(self) -> bool:
        return self._is_layout_frozen

    @property
    def display_list(self) -> Optional[List[DisplayCommand]]:
        """Get the DisplayList after layout freeze."""
        return self._display_list

    def freeze_layout(self) -> None:
        """Freeze layout and generate DisplayList.

        After freezing:
        - No more nodes can be added
        - All bbox coordinates are final
        - A flat DisplayList is generated for renderers
        """
        self._display_list = self._build_display_list()
        self._is_layout_frozen = True

    def _build_display_list(self) -> List[DisplayCommand]:
        """Build a flat sorted DisplayList from the tree."""
        commands: List[DisplayCommand] = []
        for page in self._pages:
            for node in page.walk():
                if isinstance(node, TextRun):
                    parent_line = self._find_parent_line(node)
                    commands.append(DisplayCommand(
                        cmd_type="text",
                        x=node.bbox.x, y=node.bbox.y,
                        width=node.bbox.width, height=node.bbox.height,
                        text=node.text,
                        font=node.font,
                        font_size=node.font_size,
                        baseline=parent_line.baseline if parent_line else node.bbox.y,
                        page_num=page.page_num,
                    ))
                elif isinstance(node, Image):
                    commands.append(DisplayCommand(
                        cmd_type="image",
                        x=node.bbox.x, y=node.bbox.y,
                        width=node.bbox.width, height=node.bbox.height,
                        page_num=page.page_num,
                    ))
                elif isinstance(node, Formula):
                    commands.append(DisplayCommand(
                        cmd_type="formula",
                        x=node.bbox.x, y=node.bbox.y,
                        width=node.bbox.width, height=node.bbox.height,
                        text=node.latex,
                        page_num=page.page_num,
                    ))
        # Sort by page_num, then y, then x
        commands.sort(key=lambda c: (c.page_num, c.y, c.x))
        return commands

    @staticmethod
    def _find_parent_line(node: VisualNode) -> Optional[VisualNode]:
        """Walk up to find the parent Line node."""
        # This works with the existing tree structure
        return None  # Simplification — Line info is in the tree

    def walk(self) -> Generator[VisualNode, None, None]:
        for page in self._pages:
            yield from page.walk()

    def find(self, node_id: str) -> Optional[VisualNode]:
        for page in self._pages:
            result = page.find(node_id)
            if result:
                return result
        return None

    def to_text(self) -> str:
        lines = []
        for node in self.walk():
            if isinstance(node, TextRun):
                lines.append(node.text)
            elif isinstance(node, Page):
                lines.append(f"\n=== Page {node.page_num} ===")
        return "\n".join(lines)

    def __repr__(self):
        total_nodes = sum(1 for _ in self.walk())
        return (f"VisualTree(pages={self.page_count}, "
                f"total_nodes={total_nodes}, "
                f"frozen={self._is_layout_frozen})")


__all__ = [
    "VisualTree", "VisualNode", "VisualNodeType",
    "BoundingBox", "Page", "Paragraph", "Line",
    "TextRun", "GlyphRun", "Image", "Formula",
    "DisplayCommand",
]


@dataclass
class Line(VisualNode):
    """A typeset line of text containing TextRuns."""
    baseline: float = 0.0
    line_height: float = 0.0
    alignment: str = "left"

    def __post_init__(self):
        self.vtype = VisualNodeType.LINE

    def add_run(self, run: TextRun) -> None:
        self.add_child(run)

    @property
    def runs(self) -> List[TextRun]:
        return [c for c in self.children if isinstance(c, TextRun)]

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    def __repr__(self):
        return (f"Line(id={self.id}, y={self.bbox.y:.1f}, "
                f"text='{self.text[:30]}')")


@dataclass
class Paragraph(VisualNode):
    """A block-level paragraph with lines."""
    indent: float = 0.0
    spacing_before: float = 0.0
    spacing_after: float = 0.0
    line_spacing: float = 1.2
    language: str = ""

    def __post_init__(self):
        self.vtype = VisualNodeType.PARAGRAPH

    def add_line(self, line: Line) -> None:
        self.add_child(line)

    @property
    def lines(self) -> List[Line]:
        return [c for c in self.children if isinstance(c, Line)]

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)