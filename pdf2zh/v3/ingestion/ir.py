"""Canonical ingestion IR — the unified block tree produced by *any* PDF-understanding backend.

The v3 translation chain already treats one thing as canonical downstream
(``document_model`` / ``document_ir`` semantic roles).  This module is the
canonical **ingestion** layer *upstream* of that chain: it answers "what did
the PDF parser actually see?" without caring whether the blocks came from the
existing pdfminer path or from a Marker backend.

Design rules (from the Marker-ingestion plan):

- ``IngestBlock`` carries **source provenance** (``source_backend`` /
  ``source_id``) so any downstream block can be traced back to the raw block
  the backend emitted ("which original block did this translation come
  from?"), instead of guessing.
- Every bbox is a :class:`IngestBox`: raw numbers are meaningless alone, so a
  box always declares ``space / origin / unit / meaning`` (the MECH-4 lesson
  applied at the ingestion boundary).  ``v3_box`` on the block is the
  normalized projection into the v3 canonical frame (PDF points, lower-left
  origin, y up); ``None`` when the normalization was not derivable, so a
  consumer can never silently mistake foreign coordinates for canonical ones.
- Text blocks keep the backend's reading order (per-page ``block_ids``).
- Fully serializable (``to_json`` / ``from_json``), stable schema version.

This IR is deliberately flat & structural: semantic roles
(heading/caption/...), translation policy and rendering hints are *not*
decided here — they belong to the ``normalize`` stage and the semantic
``DocumentIR`` (``pdf2zh/v3/document_ir.py``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Coordinate frames / origins / units used by ingestion backends.
#: v3 规范坐标系 —— PDF 点、左下原点、y 向上（pdfminer 惯例，plan 层）。
SPACE_V3 = "v3"
#: Marker JSON 的原始坐标系 —— 渲染页图像像素、左上原点、y 向下。
#: Marker 的 bbox/polygon 是**图像像素**，不是 PDF 点；只有经过显式
#: normalization（带 scale + flip）之后才能与 v3 数值比较（见 adapter）。
SPACE_MARKER_IMAGE = "marker_image"

ORIGIN_LOWER_LEFT = "lower-left"
ORIGIN_TOP_LEFT = "top-left"
UNIT_PT = "pt"
UNIT_PX = "px"

#: box 的语义（这个框“是什么”）：块外框。
MEANING_BLOCK = "block"

#: 顶层块类别词汇表 —— 所有 backend 归一化到同一集合。
KIND_PARAGRAPH = "paragraph"
KIND_HEADING = "heading"
KIND_CAPTION = "caption"
KIND_TABLE = "table"
KIND_TABLE_ROW = "table_row"
KIND_TABLE_CELL = "table_cell"
KIND_FIGURE = "figure"
KIND_IMAGE = "image"
KIND_FORMULA = "formula"
KIND_FORMULA_INLINE = "formula_inline"
KIND_LIST = "list"
KIND_LIST_ITEM = "list_item"
KIND_HEADER = "header"
KIND_FOOTER = "footer"
KIND_FOOTNOTE = "footnote"
KIND_BIBLIOGRAPHY = "bibliography"
KIND_REFERENCE = "reference"
KIND_TOC = "toc"
KIND_CODE = "code"
KIND_OTHER = "other"

#: 文本承载类别（比较器/覆盖度统计时只在这些类别上做文本比对 —— 表格/公式/
#: 图片类块在源 PDF 之间本来就没有逐字对齐的语义）。
TEXT_KINDS = frozenset(
    {
        KIND_PARAGRAPH,
        KIND_HEADING,
        KIND_CAPTION,
        KIND_LIST,
        KIND_LIST_ITEM,
        KIND_FOOTNOTE,
        KIND_HEADER,
        KIND_FOOTER,
        KIND_TOC,
        KIND_CODE,
        KIND_OTHER,
    }
)


def _norm_text(text: str) -> str:
    """Normalized text used for equality / coverage (whitespace-insensitive)."""
    return " ".join((text or "").split()).strip()


@dataclass(frozen=True)
class IngestBox:
    """A declared box — raw numbers + explicit semantics (MECH-4 at ingest time).

    ``space``/``origin``/``unit`` pin the frame the numbers live in;
    ``meaning`` states what the box *is*.  ``semantics`` may carry per-axis
    notes (e.g. ``{"y1": "box_top"}`` for v3 lower-left boxes) so an auditor
    never has to guess what a number means.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    space: str = SPACE_V3
    origin: str = ORIGIN_LOWER_LEFT
    unit: str = UNIT_PT
    meaning: str = MEANING_BLOCK
    semantics: Dict[str, str] = field(default_factory=dict)

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x0": round(float(self.x0), 3),
            "y0": round(float(self.y0), 3),
            "x1": round(float(self.x1), 3),
            "y1": round(float(self.y1), 3),
            "space": self.space,
            "origin": self.origin,
            "unit": self.unit,
            "meaning": self.meaning,
            "semantics": dict(self.semantics),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IngestBox":
        return cls(
            x0=float(data.get("x0", 0.0)),
            y0=float(data.get("y0", 0.0)),
            x1=float(data.get("x1", 0.0)),
            y1=float(data.get("y1", 0.0)),
            space=data.get("space", SPACE_V3),
            origin=data.get("origin", ORIGIN_LOWER_LEFT),
            unit=data.get("unit", UNIT_PT),
            meaning=data.get("meaning", MEANING_BLOCK),
            semantics=dict(data.get("semantics") or {}),
        )


@dataclass
class IngestBlock:
    """One block as the ingest backend saw it (pre-normalize, structural).

    Attributes:
        block_id: stable id within the document (backend-specific prefix).
        page_no: 0-based page number.
        block_type: normalized kind from the shared vocabulary above.
        text: extracted plain text (empty for non-text blocks).
        box: the block's bbox **as the backend emitted it** (declared frame).
        v3_box: ``(x0, y0, x1, y1)`` projected into v3 space (PDF points,
            lower-left, y up) — None when not derivable.
        parent_id / children: containment links (by id, not embedded).
        source_backend: which ingestion backend produced this block.
        source_id: the backend's own id for the block (e.g. Marker
            ``/page/3/Text/17``) — the provenance key.
        confidence: backend confidence in [0, 1] when available.
        metadata: backend-specific extras (marker_block_type, html, ...).
    """

    block_id: str
    page_no: int
    block_type: str = KIND_PARAGRAPH
    text: str = ""
    box: Optional[IngestBox] = None
    v3_box: Optional[Tuple[float, float, float, float]] = None
    parent_id: Optional[str] = None
    children: List[str] = field(default_factory=list)
    source_backend: str = ""
    source_id: str = ""
    confidence: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def trace_id(self) -> str:
        """Eternal identity in the same ``<page>/<block_id>`` shape as v3."""
        return f"{self.page_no}/{self.block_id}"

    def is_text_block(self) -> bool:
        return self.block_type in TEXT_KINDS and bool(_norm_text(self.text))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "block_id": self.block_id,
            "page_no": self.page_no,
            "block_type": self.block_type,
            "text": self.text,
            "box": self.box.to_dict() if self.box else None,
            "v3_box": list(self.v3_box) if self.v3_box else None,
            "parent_id": self.parent_id,
            "children": list(self.children),
            "source_backend": self.source_backend,
            "source_id": self.source_id,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IngestBlock":
        v3 = data.get("v3_box")
        return cls(
            block_id=data["block_id"],
            page_no=int(data.get("page_no", 0)),
            block_type=data.get("block_type", KIND_PARAGRAPH),
            text=data.get("text", ""),
            box=IngestBox.from_dict(data["box"]) if data.get("box") else None,
            v3_box=tuple(float(v) for v in v3) if v3 else None,
            parent_id=data.get("parent_id"),
            children=list(data.get("children", [])),
            source_backend=data.get("source_backend", ""),
            source_id=data.get("source_id", ""),
            confidence=data.get("confidence"),
            metadata=dict(data.get("metadata") or {}),
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"IngestBlock(id={self.block_id!r}, page={self.page_no}, "
            f"type={self.block_type!r}, text={self.text[:24]!r})"
        )


@dataclass
class IngestPage:
    """One page: its size and its blocks in reading order.

    ``width_pt`` / ``height_pt`` are the real PDF page size in points (from
    the PDF itself, not from the backend).  ``raw_box`` holds the backend's
    page box in the backend's own frame (Marker: whole-page image px).
    """

    page_no: int
    width_pt: float = 0.0
    height_pt: float = 0.0
    block_ids: List[str] = field(default_factory=list)
    raw_box: Optional[IngestBox] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_no": self.page_no,
            "width_pt": round(float(self.width_pt), 3),
            "height_pt": round(float(self.height_pt), 3),
            "block_ids": list(self.block_ids),
            "raw_box": self.raw_box.to_dict() if self.raw_box else None,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IngestPage":
        return cls(
            page_no=int(data["page_no"]),
            width_pt=float(data.get("width_pt", 0.0)),
            height_pt=float(data.get("height_pt", 0.0)),
            block_ids=list(data.get("block_ids", [])),
            raw_box=(
                IngestBox.from_dict(data["raw_box"]) if data.get("raw_box") else None
            ),
            metadata=dict(data.get("metadata") or {}),
        )


class IngestDocument:
    """Canonical ingestion IR — a flat block registry + per-page reading order.

    Blocks are keyed by id (children are references), pages keep reading
    order, and the whole document records which backend produced it plus
    environment facts (marker/surya/torch versions, model hashes, ...) so a
    trace is always reproducible.
    """

    def __init__(self, source_backend: str = "", title: str = "") -> None:
        self.source_backend = source_backend
        self.title = title
        self._blocks: Dict[str, IngestBlock] = {}
        self._pages: Dict[int, IngestPage] = {}
        self.metadata: Dict[str, Any] = {}

    # ── construction ────────────────────────────────────────────────

    def add_page(
        self, page_no: int, width_pt: float, height_pt: float, **kw: Any
    ) -> IngestPage:
        if page_no not in self._pages:
            self._pages[page_no] = IngestPage(
                page_no=page_no, width_pt=width_pt, height_pt=height_pt
            )
        page = self._pages[page_no]
        for key, value in kw.items():
            setattr(page, key, value)
        return page

    def add_block(
        self, block: IngestBlock, reading_index: Optional[int] = None
    ) -> IngestBlock:
        """Register a block and append it to its page's reading order."""
        self._blocks[block.block_id] = block
        page = self._pages.get(block.page_no)
        if page is None:
            page = self.add_page(block.page_no, 0.0, 0.0)
        if block.block_id not in page.block_ids:
            if reading_index is None:
                page.block_ids.append(block.block_id)
            else:
                page.block_ids.insert(reading_index, block.block_id)
        return block

    def add_leaf(
        self,
        *,
        block_id: str,
        page_no: int,
        block_type: str = KIND_PARAGRAPH,
        text: str = "",
        box: Optional[IngestBox] = None,
        v3_box: Optional[Tuple[float, float, float, float]] = None,
        parent_id: Optional[str] = None,
        source_backend: str = "",
        source_id: str = "",
        confidence: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        reading_index: Optional[int] = None,
    ) -> IngestBlock:
        block = IngestBlock(
            block_id=block_id,
            page_no=page_no,
            block_type=block_type,
            text=text,
            box=box,
            v3_box=v3_box,
            parent_id=parent_id,
            source_backend=source_backend or self.source_backend,
            source_id=source_id,
            confidence=confidence,
            metadata=dict(metadata or {}),
        )
        self.add_block(block, reading_index=reading_index)
        if parent_id is not None and parent_id in self._blocks:
            parent = self._blocks[parent_id]
            if block.block_id not in parent.children:
                parent.children.append(block.block_id)
        return block

    # ── queries ─────────────────────────────────────────────────────

    def page(self, page_no: int) -> Optional[IngestPage]:
        return self._pages.get(page_no)

    def pages(self) -> List[IngestPage]:
        return [self._pages[k] for k in sorted(self._pages)]

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def block_count(self) -> int:
        return len(self._blocks)

    def block(self, block_id: str) -> Optional[IngestBlock]:
        return self._blocks.get(block_id)

    def blocks(self) -> List[IngestBlock]:
        return list(self._blocks.values())

    def page_blocks(self, page_no: int) -> List[IngestBlock]:
        """Blocks of one page in the backend's reading order."""
        page = self._pages.get(page_no)
        if page is None:
            return []
        return [self._blocks[bid] for bid in page.block_ids if bid in self._blocks]

    def kinds_histogram(self, page_no: Optional[int] = None) -> Dict[str, int]:
        hist: Dict[str, int] = {}
        for b in self.blocks():
            if page_no is not None and b.page_no != page_no:
                continue
            hist[b.block_type] = hist.get(b.block_type, 0) + 1
        return dict(sorted(hist.items()))

    def text_coverage(self, page_no: Optional[int] = None) -> Dict[str, Any]:
        """Text chars on text-bearing blocks (see ``TEXT_KINDS``)."""
        chars = 0
        blocks = 0
        for b in self.blocks():
            if page_no is not None and b.page_no != page_no:
                continue
            if b.is_text_block():
                chars += len(_norm_text(b.text))
                blocks += 1
        return {"blocks": blocks, "chars": chars}

    def set_env(self, **facts: Any) -> None:
        """Record backend environment facts (versions / hashes)."""
        for key, value in facts.items():
            if value is not None:
                self.metadata[key] = value

    # ── serialization ───────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "pdf2zh.v3.ingestion.ir",
            "version": 1,
            "source_backend": self.source_backend,
            "title": self.title,
            "metadata": dict(self.metadata),
            "pages": [p.to_dict() for p in self.pages()],
            "blocks": [b.to_dict() for b in self.blocks()],
        }

    def to_json(self, indent: int = 1) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IngestDocument":
        doc = cls(
            source_backend=data.get("source_backend", ""),
            title=data.get("title", ""),
        )
        doc.metadata = dict(data.get("metadata") or {})
        for pd in data.get("pages", []):
            page = IngestPage.from_dict(pd)
            doc._pages[page.page_no] = page
        for bd in data.get("blocks", []):
            block = IngestBlock.from_dict(bd)
            doc._blocks[block.block_id] = block
        return doc

    @classmethod
    def from_json(cls, text: str) -> "IngestDocument":
        return cls.from_dict(json.loads(text))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"IngestDocument(backend={self.source_backend!r}, "
            f"pages={self.page_count}, blocks={self.block_count})"
        )


__all__ = [
    "SPACE_V3",
    "SPACE_MARKER_IMAGE",
    "ORIGIN_LOWER_LEFT",
    "ORIGIN_TOP_LEFT",
    "UNIT_PT",
    "UNIT_PX",
    "MEANING_BLOCK",
    "KIND_PARAGRAPH",
    "KIND_HEADING",
    "KIND_CAPTION",
    "KIND_TABLE",
    "KIND_TABLE_ROW",
    "KIND_TABLE_CELL",
    "KIND_FIGURE",
    "KIND_IMAGE",
    "KIND_FORMULA",
    "KIND_FORMULA_INLINE",
    "KIND_LIST",
    "KIND_LIST_ITEM",
    "KIND_HEADER",
    "KIND_FOOTER",
    "KIND_FOOTNOTE",
    "KIND_BIBLIOGRAPHY",
    "KIND_REFERENCE",
    "KIND_TOC",
    "KIND_CODE",
    "KIND_OTHER",
    "TEXT_KINDS",
    "IngestBox",
    "IngestBlock",
    "IngestPage",
    "IngestDocument",
]
