"""Bridge — canonical ingestion IR → the v3 canonical model the chain already runs.

``IngestDocument`` is the *upstream* contract; the v3 production chain
(translate → plan → fixup → render → raster, see ``magicpdf_cli``) consumes a
``DocumentModel`` built from canonical ``PageModel`` trees.  This module is
the adapter between the two: whatever backend produced the IR (pdfminer,
Marker, later MinerU/magic-pdf), its blocks — with geometry already declared
and normalized into v3 — are projected onto canonical pages, and the
standard annotation passes (roles/formulas/render-path, in
``MagicPdfBridge.to_document_model``) run unchanged.  Marker never renders:
it only supplies what the PDF contains, provenance included.

Synthesis rules (mirroring the MinerU bridge's accepted convention):

- Block geometry comes from ``IngestBlock.v3_box`` (PDF points, lower-left,
  y up).  Blocks without a normalized box are skipped — never guessed.
- Container blocks (table / figure / list / ...) collapse their subtree text
  into one canonical block, so downstream sees one table/paragraph per
  visual unit, the same shape MinerU produces.
- Line/span/glyph geometry is *synthesized* by evenly stacking the block's
  lines and interpolating per-char boxes (the same approach
  ``magicpdf_bridge`` already uses for span-level input) — Marker JSON has no
  glyph data, and the downstream refit flow re-wraps translated text anyway.
- Every canonical block keeps provenance in ``metadata``
  (``ingest_backend`` / ``ingest_source_id`` / merged ``ingest_blocks``), so
  an output defect can be traced back to the raw backend block.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

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
    KIND_PARAGRAPH,
    KIND_REFERENCE,
    KIND_TABLE,
    KIND_TABLE_CELL,
    KIND_TABLE_ROW,
    KIND_TOC,
    IngestBlock,
    IngestDocument,
)

log = logging.getLogger(__name__)

#: IR kind → v3 canonical kind (must match what document_model/render expect).
_IR_TO_CANONICAL: Dict[str, str] = {
    KIND_PARAGRAPH: "paragraph",
    KIND_HEADING: "heading",
    KIND_CAPTION: "caption",
    KIND_TABLE: "table",
    KIND_TABLE_ROW: "table",
    KIND_TABLE_CELL: "table",
    KIND_FIGURE: "figure",
    KIND_IMAGE: "figure",
    KIND_FORMULA: "formula",
    KIND_FORMULA_INLINE: "formula_inline",
    KIND_LIST: "list",
    KIND_LIST_ITEM: "list",
    KIND_HEADER: "header",
    KIND_FOOTER: "footer",
    KIND_FOOTNOTE: "footnote",
    KIND_BIBLIOGRAPHY: "references",
    KIND_REFERENCE: "references",
    KIND_TOC: "toc",
    KIND_CODE: "code",
}

#: kinds whose descendants are collapsed into the container block.
_COLLAPSE_KINDS = frozenset(
    {KIND_TABLE, KIND_FIGURE, KIND_IMAGE, KIND_LIST, KIND_CODE, KIND_TOC}
)

#: kinds with no textual payload (preserve-only in the v3 render paths).
_NO_TEXT_KINDS = frozenset({KIND_FIGURE, KIND_IMAGE, KIND_TABLE, KIND_FORMULA})


def _merge_text(block: IngestBlock, doc: IngestDocument) -> "Tuple[str, set]":
    """Block text incl. its textual subtree; returns (text, visited block ids)."""
    parts: List[str] = []
    visited: set = set()
    if block.block_type not in _NO_TEXT_KINDS and (block.text or "").strip():
        parts.append(block.text.strip())
    for cid in block.children:
        child = doc.block(cid)
        if child is None or cid in visited:
            continue
        visited.add(cid)
        child_text, child_visited = _merge_text(child, doc)
        visited.update(child_visited)
        if child_text:
            parts.append(child_text)
    return "\n".join(parts), visited


def ingest_document_to_pages(
    doc: IngestDocument,
    *,
    default_font: str = "",
    size_scale: float = 0.85,
    max_lines_per_block: int = 200,
) -> List[Any]:
    """Project an :class:`IngestDocument` onto v3 canonical ``PageModel``s.

    Returns a list of canonical pages (same shape ``MagicPdfBridge.convert_all``
    produces), ready for ``MagicPdfBridge().to_document_model(pages)``.
    """
    from pdf2zh.v3.canonical_page import (
        BlockModel,
        GlyphModel,
        LineModel,
        PageModel,
        SpanModel,
    )
    from pdf2zh.v3.magicpdf_bridge import flip_bbox, interpolate_char_bboxes

    pages: List[Any] = []
    for ipage in doc.pages():
        pno = ipage.page_no
        width_pt = float(ipage.width_pt or 0.0)
        height_pt = float(ipage.height_pt or 0.0)
        if height_pt <= 0:
            log.debug("bridge: skip page %s (no v3 page size)", pno)
            continue
        page = PageModel(page_num=pno, width=width_pt, height=height_pt)
        page.metadata["ingest_backend"] = doc.source_backend
        page.metadata["ingest_page"] = dict(ipage.metadata)
        skipped = 0

        consumed: set = set()
        for ib in doc.page_blocks(pno):
            if ib.block_id in consumed or ib.v3_box is None:
                if ib.v3_box is None:
                    skipped += 1
                continue
            # container kinds collapse their descendants into one block
            children_ids = list(ib.children)
            if ib.block_type in _COLLAPSE_KINDS and children_ids:
                text, subtree_ids = _merge_text(ib, doc)
                consumed.update(subtree_ids)
                consumed.update(children_ids)
            else:
                text = ib.text or ""
                for cid in children_ids:
                    consumed.add(cid)

            canonical_kind = _IR_TO_CANONICAL.get(ib.block_type, KIND_PARAGRAPH)
            x0, y0, x1, y1 = (float(v) for v in ib.v3_box)
            bm = BlockModel(
                text=text,
                kind=canonical_kind,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
            )
            bm.metadata["ingest_backend"] = ib.source_backend
            bm.metadata["ingest_source_id"] = ib.source_id
            bm.metadata["ingest_block_id"] = ib.block_id
            if ib.confidence is not None:
                bm.metadata["ingest_confidence"] = round(float(ib.confidence), 4)
            if canonical_kind == "table":
                bm.metadata["ingest_marker_type"] = ib.metadata.get("marker_block_type")

            lines = [ln for ln in (text or "").split("\n") if ln.strip()][
                :max_lines_per_block
            ]
            line_count = max(1, len(lines) or 1)
            line_h = max(1.0, (y1 - y0) / line_count) if y1 > y0 else 12.0
            font_size = round(line_h * size_scale, 2)
            # stack lines top→bottom inside the v3 block (y1 == box_top)
            for li, line_text in enumerate(lines):
                y_top = y1 - li * line_h
                y_bottom = y_top - line_h
                lm = LineModel(
                    text=line_text,
                    baseline=0.0,
                    x0=x0,
                    y0=min(y0, y_bottom),
                    x1=x1,
                    y1=y_top,
                )
                # span/glyph geometry synthesized in top-left space (Marker /
                # MinerU convention), then flipped back into v3 — same trick as
                # magicpdf_bridge.interpolate_char_bboxes.
                tl = flip_bbox([x0, min(y0, y_bottom), x1, y_top], height_pt)
                sm = SpanModel(
                    font=default_font,
                    size=font_size,
                    text=line_text,
                    x0=x0,
                    y0=min(y0, y_bottom),
                    x1=x1,
                    y1=y_top,
                )
                for g in interpolate_char_bboxes(tl, line_text):
                    gb = flip_bbox(g["bbox"], height_pt)
                    sm.glyphs.append(
                        GlyphModel(
                            char=g["char"],
                            cid=-1,
                            font=sm.font,
                            size=sm.size,
                            x0=gb[0],
                            y0=gb[1],
                            x1=gb[2],
                            y1=gb[3],
                            decode="ok",
                        )
                    )
                lm.spans.append(sm)
                bm.lines.append(lm)
            page.blocks.append(bm)
        if skipped:
            page.metadata["ingest_skipped_unnormalized"] = skipped
        pages.append(page)
    return pages


def model_from_ingest_document(
    doc: IngestDocument,
    *,
    default_font: str = "",
) -> Any:
    """Full chain entry: IngestDocument → annotated v3 :class:`DocumentModel`.

    Runs the identical annotation passes the MinerU path runs
    (``MagicPdfBridge.to_document_model``): style / layout-splits / roles /
    formulas / toc / render-path.  From here on the standard
    translate → plan → fixup → render pipeline is byte-for-byte unchanged.
    """
    from pdf2zh.v3.magicpdf_bridge import MagicPdfBridge

    pages = ingest_document_to_pages(doc, default_font=default_font)
    model = MagicPdfBridge(default_font=default_font).to_document_model(pages)
    model.metadata["ingest_backend"] = doc.source_backend
    model.metadata["ingest_document_meta"] = dict(doc.metadata)
    return model


def translate_model_with_ingest(
    ingest_doc: IngestDocument,
    translate_fn,
    *,
    lang_out: str = "zh-CN",
    default_font: str = "",
    trace=None,
) -> Tuple[Any, Dict[str, Any]]:
    """One-shot chain: IR → DocumentModel → translate → render plan.

    Convenience used by engines that want a whole translated model + plan
    from an IngestDocument (e.g. Marker ingestion).  Returns
    ``(model, plan)``; the plan is pre-fixup (callers keep their existing
    ``fixup_render_plan`` step).
    """
    from pdf2zh.v3.document_model import render_plan_from_model, translate_document

    model = model_from_ingest_document(ingest_doc, default_font=default_font)
    stats = translate_document(model, translate_fn, lang_out=lang_out)
    plan = render_plan_from_model(model, trace=trace)
    return model, {"translate": stats, "plan": plan}


__all__ = [
    "ingest_document_to_pages",
    "model_from_ingest_document",
    "translate_model_with_ingest",
]
