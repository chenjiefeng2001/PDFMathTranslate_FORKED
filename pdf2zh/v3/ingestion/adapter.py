"""Adapters — normalize each backend's block tree into the canonical ingestion IR.

The one hard rule of this module (MECH-4 lesson applied at the ingestion
boundary): **never copy a foreign bbox into v3 without declaring what the
numbers mean and how they were projected.**  Marker polygons live in rendered
page-image pixels (top-left origin, y down); v3 blocks live in PDF points
(lower-left origin, y up).  The two are only comparable after an explicit
scale + y-flip, which is exactly what :func:`normalize_marker_box` performs
— and when the scale is not derivable, ``v3_box`` stays ``None`` instead of
being silently guessed.

Pipeline::

    Marker JSON ──► marker_json_to_document()  ──► IngestDocument
    LTChar / PageModel ──► existing_pages_to_document() ──► IngestDocument
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.ingestion.base import BACKEND_EXISTING, BACKEND_MARKER
from pdf2zh.v3.ingestion.ir import (
    KIND_BIBLIOGRAPHY,
    KIND_CAPTION,
    KIND_CODE,
    KIND_FIGURE,
    KIND_FOOTER,
    KIND_FOOTNOTE,
    KIND_FORMULA,
    KIND_FORMULA_INLINE,
    KIND_HEADER,
    KIND_HEADING,
    KIND_IMAGE,
    KIND_LIST,
    KIND_LIST_ITEM,
    KIND_OTHER,
    KIND_PARAGRAPH,
    KIND_REFERENCE,
    KIND_TABLE,
    KIND_TABLE_CELL,
    KIND_TABLE_ROW,
    KIND_TOC,
    MEANING_BLOCK,
    ORIGIN_LOWER_LEFT,
    ORIGIN_TOP_LEFT,
    SPACE_MARKER_IMAGE,
    SPACE_V3,
    UNIT_PT,
    UNIT_PX,
    IngestBox,
    IngestDocument,
)

#: Marker block_type (v2.0.0 schema BlockTypes) → IR kind vocabulary.
MARKER_KIND_MAP: Dict[str, str] = {
    "Text": KIND_PARAGRAPH,
    "SectionHeader": KIND_HEADING,
    "Caption": KIND_CAPTION,
    "Code": KIND_CODE,
    "Table": KIND_TABLE,
    "TableGroup": KIND_TABLE,
    "TableCell": KIND_TABLE_CELL,
    "Figure": KIND_FIGURE,
    "FigureGroup": KIND_FIGURE,
    "Picture": KIND_IMAGE,
    "PictureGroup": KIND_IMAGE,
    "Diagram": KIND_FIGURE,
    "Equation": KIND_FORMULA,
    "TextInlineMath": KIND_FORMULA_INLINE,
    "ChemicalBlock": KIND_FORMULA,
    "ListGroup": KIND_LIST,
    "ListItem": KIND_LIST_ITEM,
    "PageHeader": KIND_HEADER,
    "PageFooter": KIND_FOOTER,
    "Footnote": KIND_FOOTNOTE,
    "Bibliography": KIND_BIBLIOGRAPHY,
    "Reference": KIND_REFERENCE,
    "TableOfContents": KIND_TOC,
    "ComplexRegion": KIND_OTHER,
    "Form": KIND_OTHER,
    "Handwriting": KIND_OTHER,
}
DEFAULT_KIND = KIND_PARAGRAPH

#: Marker JSON emits Line/Span/Char scaffolding only inside leaf containers;
#: their text is already contained by the parent Text block — drop them so a
#: paragraph is not double-counted by the comparator.
MARKER_SKIP_TYPES = frozenset({"Line", "Span", "Char", "Document"})


def marker_block_kind(block_type: str) -> str:
    """Marker ``block_type`` name → IR kind (case-sensitive like the schema)."""
    return MARKER_KIND_MAP.get(block_type, DEFAULT_KIND)


# ── text extraction ──────────────────────────────────────────────────────


def html_to_text(html: str) -> str:
    """Marker JSON stores block content as (possibly marked-up) HTML.

    Best-effort plain-text extraction: <br> → newline, strip tags, unescape
    entities, then collapse whitespace (kept single-line per block — compare
    & translation run on whitespace-normalized text anyway).
    """
    if not html:
        return ""
    text = html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    return " ".join(text.split())


# ── coordinate normalization (the declared-semantics boundary) ────────────


def normalize_marker_box(
    raw: Sequence[float],
    *,
    page_px_width: float,
    page_px_height: float,
    page_pt_width: float,
    page_pt_height: float,
) -> Tuple[float, float, float, float]:
    """Marker image-px box (top-left origin, y down) → v3 box.

    v3 is PDF points with lower-left origin, y up; the projection is a
    per-axis scale by the page-size ratio, then a y flip:

        x_v3 = x_px * (w_pt / w_px)
        y_v3 = h_pt − y_px * (h_pt / h_px)

    Returns ``None``-able tuple; callers guard for degenerate sizes.
    """
    x0, y0, x1, y1 = (float(v) for v in raw)
    sx = page_pt_width / page_px_width if page_px_width > 0 else 0.0
    sy = page_pt_height / page_px_height if page_px_height > 0 else 0.0
    if sx <= 0 or sy <= 0:
        raise ValueError("cannot normalize marker box without page sizes")
    return (
        x0 * sx,
        page_pt_height - y1 * sy,
        x1 * sx,
        page_pt_height - y0 * sy,
    )


def declared_v3_box(box: Sequence[float]) -> IngestBox:
    """A v3-space raw box → declared IngestBox (already canonical frame)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    return IngestBox(
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        space=SPACE_V3,
        origin=ORIGIN_LOWER_LEFT,
        unit=UNIT_PT,
        meaning=MEANING_BLOCK,
        semantics={"y1": "box_top"},
    )


def declared_marker_box(box: Sequence[float]) -> IngestBox:
    """A Marker image-px box → declared IngestBox (raw frame, NOT v3)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    return IngestBox(
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        space=SPACE_MARKER_IMAGE,
        origin=ORIGIN_TOP_LEFT,
        unit=UNIT_PX,
        meaning=MEANING_BLOCK,
        semantics={"y_direction": "down"},
    )


# ── Marker JSON (v2 renderer ``JSONOutput``) ──────────────────────────────


def _marker_page_size(
    pages_data: Sequence[Dict[str, Any]], index: int
) -> Tuple[float, float]:
    """Page node's own px size (from its declared bbox)."""
    try:
        page = pages_data[index]
        bbox = page.get("bbox")
        if not bbox:
            return 0.0, 0.0
        return abs(float(bbox[2]) - float(bbox[0])), abs(
            float(bbox[3]) - float(bbox[1])
        )
    except (IndexError, TypeError, ValueError):
        return 0.0, 0.0


def marker_json_to_document(
    data: Dict[str, Any],
    *,
    pdf_page_sizes: Optional[Sequence[Tuple[float, float]]] = None,
    title: str = "",
    seq_base: int = 0,
) -> IngestDocument:
    """Marker ``marker.json`` (JSONOutput schema) → canonical IngestDocument.

    ``pdf_page_sizes``: [(width_pt, height_pt)] in PDF page order — when
    given, every block also gets its normalized ``v3_box``; otherwise blocks
    keep only their declared marker-image box and ``v3_box`` is None (never
    guessed).
    """
    doc = IngestDocument(source_backend=BACKEND_MARKER, title=title)
    meta = data.get("metadata") or {}
    doc.set_env(
        marker_output_schema=data.get("block_type"),
        marker_metadata=meta if isinstance(meta, dict) else {},
    )

    pages_data = [
        c for c in (data.get("children") or []) if c.get("block_type") == "Page"
    ]
    if not pages_data:
        # Tolerant: page-less trees (already-flattened docs) fall back to the
        # root children treated as page-0 content.
        pages_data = [
            {
                "id": "/page/0",
                "block_type": "Page",
                "children": data.get("children") or [],
            }
        ]
    for pidx, page_node in enumerate(pages_data):
        px_w, px_h = _marker_page_size(pages_data, pidx)
        pt_w = pt_h = 0.0
        if pdf_page_sizes and pidx < len(pdf_page_sizes):
            pt_w, pt_h = float(pdf_page_sizes[pidx][0]), float(pdf_page_sizes[pidx][1])
        page_box = (
            declared_marker_box(page_node.get("bbox") or [0, 0, px_w, px_h])
            if px_w
            else None
        )
        page = doc.add_page(
            page_no=pidx,
            width_pt=pt_w,
            height_pt=pt_h,
            raw_box=page_box,
            metadata={
                "marker_page_id": page_node.get("id", f"/page/{pidx}"),
                "marker_px_width": round(px_w, 3),
                "marker_px_height": round(px_h, 3),
            },
        )

        def walk(
            node: Dict[str, Any], parent_id: Optional[str], seq: List[int]
        ) -> None:
            block_type = node.get("block_type") or ""
            if block_type in MARKER_SKIP_TYPES:
                # Scaffolding nodes are not blocks; recurse into their
                # children (they exist only to hold text owned by a parent).
                for child in node.get("children") or []:
                    walk(child, parent_id, seq)
                return
            seq[0] += 1
            block_id = f"m{pidx}_{seq[0]}"
            kind = marker_block_kind(block_type)
            children = node.get("children") or []
            text = html_to_text(node.get("html") or "")
            if children and not text:
                # Container groups (Table/Figure/List...) keep '' text; their
                # real content lives in children (flattened next).
                text = ""
            bbox_raw = node.get("bbox")
            box = declared_marker_box(bbox_raw) if bbox_raw else None
            v3_box: Optional[Tuple[float, float, float, float]] = None
            if box is not None and px_w > 0 and px_h > 0 and pt_w > 0 and pt_h > 0:
                try:
                    v3_box = normalize_marker_box(
                        [box.x0, box.y0, box.x1, box.y1],
                        page_px_width=px_w,
                        page_px_height=px_h,
                        page_pt_width=pt_w,
                        page_pt_height=pt_h,
                    )
                except ValueError:
                    v3_box = None
            conf_raw = node.get("confidence")
            doc.add_leaf(
                block_id=block_id,
                page_no=pidx,
                block_type=kind,
                text=text,
                box=box,
                v3_box=v3_box,
                parent_id=parent_id,
                source_backend=BACKEND_MARKER,
                source_id=node.get("id", ""),
                confidence=float(conf_raw) if conf_raw is not None else None,
                metadata={
                    "marker_block_type": block_type,
                    "container": bool(children),
                },
            )
            for child in children or []:
                walk(child, block_id, seq)

        # The Page node is the page container itself (already an IngestPage) —
        # only its children become blocks (one shared sequence per page).
        seq = [seq_base]
        for child in page_node.get("children") or []:
            walk(child, None, seq)
    return doc


# ── existing v3 pages (PageModel list) ────────────────────────────────────


#: canonical kind → IR kind; kinds already in the IR vocabulary pass through.
_CANONICAL_KIND_EXTRA: Dict[str, str] = {
    "abstract": KIND_PARAGRAPH,
    "references": KIND_BIBLIOGRAPHY,
    "metadata": KIND_OTHER,
    "formula_inline": KIND_FORMULA_INLINE,
}

#: every normalized kind name (lowercase) accepted by the IR vocabulary.
_IR_KIND_NAMES = frozenset(
    {
        KIND_PARAGRAPH,
        KIND_HEADING,
        KIND_CAPTION,
        KIND_TABLE,
        KIND_TABLE_ROW,
        KIND_TABLE_CELL,
        KIND_FIGURE,
        KIND_IMAGE,
        KIND_FORMULA,
        KIND_FORMULA_INLINE,
        KIND_LIST,
        KIND_LIST_ITEM,
        KIND_HEADER,
        KIND_FOOTER,
        KIND_FOOTNOTE,
        KIND_BIBLIOGRAPHY,
        KIND_REFERENCE,
        KIND_TOC,
        KIND_CODE,
        KIND_OTHER,
    }
)


def canonical_kind_to_ir(kind: str) -> str:
    k = (kind or "").strip().lower()
    if k in _CANONICAL_KIND_EXTRA:
        return _CANONICAL_KIND_EXTRA[k]
    # Vocab members are lowercase & overlap canonical kinds (paragraph/heading/
    # caption/table/figure/formula/list/header/footer/footnote/code/...).
    return k if k in _IR_KIND_NAMES else KIND_OTHER


def existing_pages_to_document(
    pages: Sequence[Any],
    *,
    source_backend: str = BACKEND_EXISTING,
    title: str = "",
) -> IngestDocument:
    """v3 canonical PageModel list (pdfminer-derived) → IngestDocument.

    Page blocks already live in v3 space, so ``box`` and ``v3_box`` agree;
    block ids reuse the v3 convention ``p<page>_<reading_index>`` so trace
    identity is preserved end-to-end.
    """
    doc = IngestDocument(source_backend=source_backend, title=title)
    for page in pages or []:
        pno = int(getattr(page, "page_num", 0))
        width = float(getattr(page, "width", 0.0) or 0.0)
        height = float(getattr(page, "height", 0.0) or 0.0)
        doc.add_page(page_no=pno, width_pt=width, height_pt=height)
        for i, block in enumerate(getattr(page, "blocks", []) or []):
            bid = f"p{pno}_{i}"
            bbox = getattr(block, "bbox", None) or (0.0, 0.0, 0.0, 0.0)
            kind = canonical_kind_to_ir(getattr(block, "kind", KIND_PARAGRAPH))
            text = getattr(block, "text", "") or ""
            conf = None
            md = getattr(block, "metadata", None) or {}
            try:
                conf_raw = md.get("confidence")
                if conf_raw is not None:
                    conf = float(conf_raw)
            except (TypeError, ValueError):
                conf = None
            box = declared_v3_box(bbox)
            doc.add_leaf(
                block_id=bid,
                page_no=pno,
                block_type=kind,
                text=text,
                box=box,
                v3_box=tuple(float(v) for v in bbox),
                source_backend=source_backend,
                source_id=f"existing:{pno}:{bid}",
                confidence=conf,
                metadata={
                    "v3_kind": getattr(block, "kind", KIND_PARAGRAPH),
                    "lines": len(getattr(block, "lines", []) or []),
                },
            )
    return doc


# ── pdf page sizes (points), via pdfminer (no extra heavy deps) ───────────


def read_pdf_page_sizes(pdf_path: str) -> List[Tuple[float, float]]:
    """[(width_pt, height_pt)] per PDF page, from the PDF itself.

    Uses pdfminer's document layer only (no layout interpretation), so it is
    cheap enough to run once per ingestion.  Returns [] on any failure.
    """
    try:
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfparser import PDFParser
    except Exception as exc:  # noqa: BLE001 -- best-effort helper
        raise RuntimeError(f"pdfminer not available for page-size read: {exc}") from exc
    sizes: List[Tuple[float, float]] = []
    with open(pdf_path, "rb") as fh:
        doc = PDFDocument(PDFParser(fh))
        for page in PDFPage.create_pages(doc):
            mb = page.mediabox
            w = float(mb[2]) - float(mb[0])
            h = float(mb[3]) - float(mb[1])
            sizes.append((w, h))
    return sizes


__all__ = [
    "MARKER_KIND_MAP",
    "marker_block_kind",
    "html_to_text",
    "normalize_marker_box",
    "declared_v3_box",
    "declared_marker_box",
    "marker_json_to_document",
    "existing_pages_to_document",
    "canonical_kind_to_ir",
    "read_pdf_page_sizes",
]
