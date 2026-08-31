"""snapshot — capture the *source-side* evidence chain (parser→model→translation→layout).

Runs the source PDF through the same v3 pipeline used in production
(``build_document_model`` → ``translate_document`` → ``render_plan_from_model``)
with an **identity** translator, so analysis is reproducible offline and needs no
translator / ONNX / renderer.  For every block on a requested page it emits a
per-stage evidence payload (7H-1 §2):

- **parser**: canonical page tree glyph/span/line/block (text, bbox, font, size);
- **model**: block kind / role / style / reading order via the DocumentModel;
- **translation**: source text vs identity-translated text + translation unit id +
  per-unit status (from ``render_payload.block_translation_unit`` policy);
- **layout**: the settled render-plan entry (``src_box`` / ``dst_box`` /
  ``font_size`` / ``render_path``).

Only reads: never mutates a plan, never renders, never translates via network.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

__all__ = ["identity", "capture_source_chain", "block_evidence_per_page"]


def identity(text: str) -> str:
    return text


def _page_lookup(model) -> Dict[int, object]:
    return {p.page_num: p for p in model.pages}


def _parser_evidence_for_page(page) -> List[dict]:
    """Canonical page tree → parser-layer evidence rows (index → block)."""
    rows: List[dict] = []
    for i, b in enumerate(page.blocks):
        # best-effort per-line font/size (parser evidence uses first major span)
        font = ""
        fs = 0.0
        for line in getattr(b, "lines", []) or []:
            for span in getattr(line, "spans", []) or []:
                if span.size:
                    font = span.font or font
                    fs = span.size
                    break
            break
        rows.append(
            {
                "primitive_index": i,
                "text": b.text,
                "bbox": [float(v) for v in b.bbox],
                "font": font,
                "font_size": round(float(fs), 2) if fs else None,
                "line_count": len(getattr(b, "lines", []) or []),
            }
        )
    return rows


def _translation_evidence_for_block(block, page_num: int, index: int) -> dict:
    """Per-block translation evidence (identity-translated, policy from unit)."""
    md = block.metadata or {}
    source = block.text or ""
    translated = md.get("translated")
    if translated is None:
        translated = source  # identity fallback
    # Restore the per-unit status from the render-payload policy when present.
    status = md.get("translate")
    if status is True:
        status = "translated"
    elif status is False:
        status = "preserved"
    elif block.kind in (
        "formula",
        "figure",
        "image",
        "table",
        "code",
        "command",
        "filename",
        "identifier",
        "header",
        "footer",
    ):
        status = "preserved"
    else:
        status = "translated"
    return {
        "source_text": source,
        "translated_text": translated,
        "same": translated == source,
        "segmentation": {
            "unit_kind": md.get("translation_unit_kind"),
            "line_count": len(getattr(block, "lines", []) or []),
        },
        "translation_status": status,
    }


def _layout_evidence_for_plan_entry(entry: dict) -> dict:
    return {
        "target_bbox": entry.get("dst_box"),
        "target_font": None,  # resolved at render
        "target_font_size": entry.get("font_size"),
        "scale": None,
        "clipping": None,
        "collision": None,
        "recovery": None,
        "render_path": entry.get("render_path"),
    }


def block_evidence_per_page(model, plan, page_num: int) -> List[dict]:
    """A page's full evidence list: one dict per block with all four stages."""
    from pdf2zh.v3.document_model import block_id

    page = _page_lookup(model).get(page_num)
    if page is None:
        return []
    plan_by_id = {e["block_id"]: e for e in plan}
    rows: List[dict] = []
    for i, b in enumerate(page.blocks):
        bid = block_id(page_num, i)
        entry = plan_by_id.get(bid)
        md = b.metadata or {}
        ev = {
            "node_id": bid,
            "page_id": page_num,
            "primitive_index": i,
            "parser": {
                "text": b.text,
                "bbox": [float(v) for v in b.bbox],
                "font": _major_font(md),
                "font_size": _major_size(md),
                "line_count": len(getattr(b, "lines", []) or []),
            },
            "model": {
                "kind": b.kind,
                "role": md.get("role"),
                "role_confidence": md.get("role_confidence"),
                "bbox": [float(v) for v in b.bbox],
                "font_size": _major_size(md),
                "font_major": _major_font(md),
                "multifont": md.get("multifont", False),
                "reading_order": i,
            },
            "translation": _translation_evidence_for_block(b, page_num, i),
            "layout": (
                _layout_evidence_for_plan_entry(entry)
                if entry is not None
                else {"target_bbox": None, "render_path": md.get("render_path")}
            ),
            "kind": b.kind,
        }
        rows.append(ev)
    return rows


def _major_font(md: dict):
    return md.get("font_major")


def _major_size(md: dict):
    fs = md.get("font_size")
    return round(float(fs), 2) if isinstance(fs, (int, float)) else None


def capture_source_chain(
    pdf_path: str,
    page_ids: Optional[Sequence[int]] = None,
) -> dict:
    """Build parser/model/translation/layout evidence for a source PDF.

    Returns a dict with ``pages`` (ev id → evidence list) and diagnostic stats.
    Never fails: parse failure yields empty payload.
    """
    from pdfminer.high_level import extract_pages

    from pdf2zh.v3.document_model import (
        build_document_model,
        render_plan_from_model,
        translate_document,
    )

    result = {"errors": [], "pages": {}, "page_count": 0}
    try:
        # Restrict parsing to requested pages when given — large books (1000+)
        # otherwise make the whole-pipeline scan impractical.
        if page_ids:
            lt = list(extract_pages(pdf_path, page_numbers=sorted(page_ids)))
        else:
            lt = list(extract_pages(pdf_path))
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"extract failed: {exc}")
        return result
    result["page_count"] = len(lt)
    try:
        model = build_document_model(lt)
        translate_document(model, identity)
        plan = render_plan_from_model(model)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"pipeline failed: {exc}")
        return result

    # pdfminer's ``pageid`` is an internal counter, not the 0-based page index
    # we requested — renumber the returned pages in order so block ids and page
    # keys match the caller's ``page_ids`` (and thus pymupdf's page numbering).
    if page_ids:
        wanted_sorted = sorted(page_ids)
        for k, p in enumerate(model.pages):
            if k < len(wanted_sorted):
                p.page_num = wanted_sorted[k]

    wanted = set(page_ids or [])
    for p in model.pages:
        pno = p.page_num
        if wanted and pno not in wanted:
            continue
        result["pages"][str(pno)] = block_evidence_per_page(model, plan, pno)
    result["model_stats"] = {
        "blocks": sum(len(getattr(p, "blocks", [])) for p in model.pages)
    }
    return result
