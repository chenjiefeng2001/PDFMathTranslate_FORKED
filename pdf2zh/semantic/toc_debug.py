"""``--debug-toc`` analysis entry (Commit 6A, detection-only; no PDF change).

Opens a PDF with PyMuPDF, extracts per-line paragraphs with geometry, runs the
visual TOC detector + parser (:mod:`pdf2zh.semantic.toc_detector` /
:mod:`pdf2zh.semantic.toc_parser`), and writes ``debug/toc.json``:

    {
      "pages": {
        "3": {
          "is_toc_page": true,
          "entries": [
            {"title": "Introduction", "level": 0, "page_number": "1",
             "indent": 72.0, "title_x": 72.0, "page_x": 500.0}
          ]
        }
      }
    }

Pure analysis: never modifies the PDF, never touches translation / renderer.
Self-contained JSON-safety (plain scalar fields only, ``ensure_ascii=False``).
"""

from __future__ import annotations

import json
import os

from pdf2zh.semantic.list_debug import extract_page_lines
from pdf2zh.semantic.toc_parser import parse_toc

__all__ = ["dump_toc_debug"]


def _page_entries(node) -> list[dict]:
    out: list[dict] = []
    for e in node.entries:
        entry = {
            "title": e.title,
            "level": e.level,
            "page_number": e.page_number,
            "indent": round(e.indent, 1),
            "title_x": round(e.title_x, 1),
            "page_x": round(e.page_x, 1),
            "dot_leader": e.dot_leader,
            "leader_present": e.leader_present,
            "continuation": list(e.continuation),
        }
        if e.destination_page is not None:
            entry["destination_page"] = e.destination_page
        out.append(entry)
    return out


def dump_toc_debug(pdf_path: str, out_dir: str | None = None) -> dict:
    """Analyze a PDF for visual TOC pages and write ``<out_dir>/toc.json``.

    Returns the payload dict (also written to disk).
    """
    import pymupdf  # noqa: PLC0415 -- 懒加载，避免无关路径引入 PyMuPDF

    pages: dict[str, dict] = {}
    doc = pymupdf.open(pdf_path)
    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            texts_geom = extract_page_lines(page)
            if not texts_geom[0]:
                continue
            texts, geom = texts_geom
            page_width = float(page.rect.width or 0.0)
            node = parse_toc(
                [{**g, "text": t} for t, g in zip(texts, geom)],
                page_width=page_width,
            )
            if node is None or not node.entries:
                continue
            pages[str(pno + 1)] = {
                "is_toc_page": node.is_toc_page,
                "has_header": node.has_header,
                "header_text": node.header_text,
                "entries": _page_entries(node),
            }
    finally:
        doc.close()

    payload = {"pages": pages}
    out_dir = out_dir or "debug"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "toc.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return payload
