"""pdf_inspector — read evidence OUT of a rendered PDF page.

Answers the ``render`` and ``pdf`` evidence stages (7H-1 §3 render.json): what
text / image / path objects actually exist on a target page, with geometry,
font, size and color.  Pure introspection: never renders, never mutates.

Coordinate conventions
----------------------
- ``pymupdf`` extraction is PDF-native (top-left origin, y-down).  We keep
  ``final_bbox`` here in y-down as extracted and expose ``v3_bbox`` (y-up) for
  comparison against model / layout evidence (which are v3 y-up).
- ``Page.rawdict`` gives char-level spans; we flatten to **runs** (consecutive
  same (bbox-line, font, size, color)) so a single text object approximates a
  drawn ``Tj``/``TJ`` at render-evidence granularity.

Defect signals also exposed:
- ``content_stream_anomaly`` — scan of the raw page content stream for malformed
  numeric tokens (e.g. a scientific-notation float truncated to ``-9.0e``).  This
  is the ``-9.000000001435637e``-style renderer emitter defect seen in the real
  C book dual, a **renderer-stage** F4/F9 signal the text layer does not show.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import pymupdf

__all__ = [
    "inspect_page_text",
    "page_drawings",
    "content_stream_anomaly",
    "inspect_page",
]

# A numeric literal that ends with 'e'/'E' and NO exponent digits following, or a
# doubled/empty exponent, is a malformed emitter token (floats that got truncated
# to a bare mantissa+sig like ``-1.1999999998124622e``).
_RE_BAD_FLOAT = re.compile(r"(?<![\w.])[-+]?\d*\.\d+[eE](?![-+]?\d)(?![\w])")
_RE_SUSPECT_TOKEN = re.compile(r"[-+]?\d+\.\d{6,}[eE]\b")


def inspect_page_text(doc: pymupdf.Document, pno: int) -> List[Dict[str, Any]]:
    """Flatten one page's ``rawdict`` into drawn text runs.

    Each run::

        {"pdf_object_id": int, "operator": "TJ",
         "text": str, "bbox": [x0,y0,x1,y1] (y-down),
         "font": name, "font_size": float, "color": int,
         "line": baseline index, "page": pno}
    """
    out: List[Dict[str, Any]] = []
    page = doc[pno]
    try:
        raw = page.get_text("dict")
    except Exception:  # noqa: BLE001
        return out
    obj_id = 0
    line_idx = 0
    for block in raw.get("blocks", []):
        btype = block.get("type")
        for line in block.get("lines", []):
            line_idx += 1
            for span in line.get("spans", []):
                text = span.get("text") or ""
                if not text:
                    continue
                obj_id += 1
                box = span.get("bbox") or [0, 0, 0, 0]
                font = span.get("font") or ""
                size = span.get("size") or 0.0
                color = span.get("color")
                out.append(
                    {
                        "pdf_object_id": "text:%d" % obj_id,
                        "operator": "TJ",
                        "text": text,
                        "bbox": [round(float(v), 2) for v in box],
                        "font": font,
                        "font_size": round(float(size), 2),
                        "color": color,
                        "line": line_idx,
                        "page": pno,
                        "block_type": btype,
                    }
                )
    return out


def page_drawings(doc: pymupdf.Document, pno: int) -> List[Dict[str, Any]]:
    """List drawing (path) objects on a page — non-text visual evidence."""
    out: List[Dict[str, Any]] = []
    try:
        page = doc[pno]
        drawings = page.get_drawings() or []
    except Exception:  # noqa: BLE001
        return out
    for i, d in enumerate(drawings):
        rect = getattr(d, "rect", None)
        item = {
            "pdf_object_id": i,
            "operator": "path",
            "kind": d.get("type") if isinstance(d, dict) else type(d).__name__,
            "bbox": ([round(float(v), 2) for v in rect] if rect else None),
            "fill": d.get("fill") if isinstance(d, dict) else None,
            "color": d.get("color") if isinstance(d, dict) else None,
            "has_text": bool(d.get("text")) if isinstance(d, dict) else False,
        }
        out.append(item)
    return out


def _page_xobject_streams(doc: pymupdf.Document, pno: int) -> List[bytes]:
    """Page content + resources dict (any XObject / inheritable resource)."""
    streams: List[bytes] = []
    try:
        page = doc[pno]
        streams.append(bytes(page.read_contents()) if page.read_contents() else b"")
    except Exception:  # noqa: BLE001
        pass
    try:
        xref = doc.page_xref(pno)
        obj = doc.xref_object(xref)
        # Collect all Form XObject / image stream xrefs reachable from the page.
        seen = set()
        stack = []
        import re

        for m in re.finditer(r"/(?:XObject|Resources)\s*<<([^>]*?)>>", obj):
            seg = m.group(1)
            for sm in re.finditer(r"/(\w+)\s+(\d+)\s+0\s+R", seg):
                stack.append((sm.group(1), int(sm.group(2))))
        for idx in range(len(stack)):
            name, xrefno = stack[idx]
            if xrefno in seen:
                continue
            seen.add(xrefno)
            try:
                sub = doc.xref_object(xrefno)
            except Exception:  # noqa: BLE001
                continue
            if "/Subtype /Form" in sub or "/Subtype/Form" in sub or "/XObject" in sub:
                try:
                    streams.append(bytes(doc.xref_stream(xrefno)))
                except Exception:  # noqa: BLE001
                    pass
                for sm in re.finditer(r"/(\w+)\s+(\d+)\s+0\s+R", sub):
                    stack.append((sm.group(1), int(sm.group(2))))
    except Exception:  # noqa: BLE001
        pass
    return streams


def content_stream_anomaly(doc: pymupdf.Document, pno: int) -> dict:
    """Detect MuPDF-emitter syntax errors on a page's content streams.

    Primary signal: MuPDF's own parser, whose warnings are captured via
    ``TOOLS.reset_mupdf_warnings`` / ``TOOLS.mupdf_warnings``.  The real C book
    dual trips it on float literals truncated to a bare mantissa+sig, which the
    text layer hides entirely (the glyphs extract fine) — a **renderer-stage**
    defect only visible at the PDF-object layer.

    Returns::

        {"checked": bool, "syntax_error_tokens": int,
         "sample": [...up to 5...], "anomaly": bool}
    """
    try:
        pymupdf.TOOLS.reset_mupdf_warnings()
    except Exception:  # noqa: BLE001
        pass
    try:
        _ = doc[pno].get_text()  # force MuPDF to parse the page content
        warnings = pymupdf.TOOLS.mupdf_warnings() or ""
    except Exception:  # noqa: BLE001
        warnings = ""
    lines = [ln for ln in warnings.splitlines() if ln.strip()]
    err_lines = [
        ln
        for ln in lines
        if "syntax error" in ln.lower() or "unknown keyword" in ln.lower()
    ]
    if err_lines:
        return {
            "checked": True,
            "syntax_error_tokens": len(err_lines),
            "sample": err_lines[:5],
            "anomaly": True,
            "source": "mupdf",
        }
    # Fallback: raw-stream regex scan (valid number also trips MuPDF on `cm`).
    text = "".join(
        stream.decode("latin-1", "ignore") for stream in _page_xobject_streams(doc, pno)
    )
    hits = sorted(set(m.group(0) for m in _RE_BAD_FLOAT.finditer(text)))
    return {
        "checked": bool(text),
        "syntax_error_tokens": len(hits),
        "sample": hits[:5],
        "anomaly": bool(hits),
        "source": "regex",
    }


def inspect_page(doc: pymupdf.Document, pno: int, page_height: float) -> Dict[str, Any]:
    """Assemble the ``render``+``pdf`` evidence for one target page."""
    texts = inspect_page_text(doc, pno)
    for t in texts:
        b = t["bbox"]
        t["v3_bbox"] = [
            round(b[0], 2),
            round(page_height - b[3], 2),
            round(b[2], 2),
            round(page_height - b[1], 2),
        ]
    return {
        "page": pno,
        "page_height": page_height,
        "text_runs": texts,
        "drawings": page_drawings(doc, pno),
        "content_stream": content_stream_anomaly(doc, pno),
    }


__all__ = [
    "inspect_page_text",
    "page_drawings",
    "content_stream_anomaly",
    "inspect_page",
]
