"""``--debug-list`` analysis entry (Commit 4, detection-only; no PDF change).

Opens a PDF with PyMuPDF, extracts per-line paragraphs with geometry, runs
the list detector + parser (:mod:`pdf2zh.semantic.list_detector` /
:mod:`pdf2zh.semantic.list_parser`), and writes ``debug/list.json`` in the
user's schema:

    {
      "pages": {
        "3": [{
          "type": "list", "bbox": [...], "ordered": true,
          "marker_style": "decimal",
          "items": [{"marker": "1.", "level": 0, "indent": 72,
                     "content_x": 91, "continuation_lines": 2}, ...]
        }]
      }
    }

Pure analysis: it never modifies the PDF or touches the translation
pipeline, so it can be wired into the CLI independently of the engine.
"""

from __future__ import annotations

import json
import os

from pdf2zh.semantic.list_detector import detect_list_candidates
from pdf2zh.semantic.list_parser import parse_list_tree
from pdf2zh.semantic.models import ListItemNode, ListNode

__all__ = ["extract_page_lines", "dump_list_debug"]


def extract_page_lines(page) -> tuple[list[str], list[dict]]:
    """Per-line paragraphs + geometry from a PyMuPDF page.

    Returns ``(texts, geom)``; ``geom[i] = {x0, y0, x1, y1, size}``.
    """
    texts: list[str] = []
    geom: list[dict] = []
    raw = page.get_text("dict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = "".join(sp.get("text", "") for sp in spans)
            if not text.strip():
                continue
            x0, y0, x1, y1 = (line.get("bbox") or [0.0] * 4)[:4]
            size = max((sp.get("size") or 0.0) for sp in spans)
            texts.append(text)
            geom.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "size": size})
    return texts, geom


def _list_entries(tree: ListNode) -> list[dict]:
    """Map a parsed list tree to the debug-JSON entries schema."""
    if not tree.items:
        return []
    entries: list[dict] = []
    for item in tree.items:
        entries.append(
            {
                "marker": item.marker,
                "level": item.level,
                "indent": round(item.indent, 1),
                "content_x": round(item.content_x, 1),
                "continuation_lines": len(item.continuation),
            }
        )
    return entries


def dump_list_debug(pdf_path: str, out_dir: str | None = None) -> dict:
    """Analyze a PDF for list structures and write ``<out_dir>/list.json``.

    Returns the payload dict (also written to disk).
    """
    import pymupdf  # noqa: PLC0415 -- 懒加载，避免无关路径引入 PyMuPDF

    pages: dict[str, list[dict]] = {}
    doc = pymupdf.open(pdf_path)
    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            texts, geom = extract_page_lines(page)
            if not texts:
                continue
            cands = detect_list_candidates(texts, geom)
            tree = parse_list_tree(texts, cands, geom)
            if tree is None or not tree.items:
                continue
            entries = _list_entries(tree)
            if not entries:
                continue
            ordered = tree.items[0].marker_type != "bullet"
            # 整页列表 bbox：只取 ListItemNode 的 bbox（ListNode 无几何）
            items_nodes = [it for it in tree.walk() if isinstance(it, ListItemNode)]
            xs0 = [it.bbox[0] for it in items_nodes if it.bbox]
            ys0 = [it.bbox[1] for it in items_nodes if it.bbox]
            xs1 = [it.bbox[2] for it in items_nodes if it.bbox]
            ys1 = [it.bbox[3] for it in items_nodes if it.bbox]
            bbox = (
                [min(xs0), min(ys0), max(xs1), max(ys1)]
                if xs0
                else [0, 0, 0, 0]
            )
            pages[str(pno + 1)] = [
                {
                    "type": "list",
                    "bbox": [round(v, 1) for v in bbox],
                    "ordered": ordered,
                    "marker_style": tree.items[0].marker_type,
                    "items": entries,
                }
            ]
    finally:
        doc.close()

    payload = {"pages": pages}
    out_dir = out_dir or "debug"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "list.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return payload