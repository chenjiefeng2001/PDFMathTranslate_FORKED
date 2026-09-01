"""PDF structural extraction — Commit 7D.

Reads a PDF with PyMuPDF and returns a JSON-safe structural model, the input
side of ``evaluate(source_pdf, output_pdf)``.  Structural (not pixel) data is
the first-class metric source: page geometry, text lines with font/size/style,
word bboxes, and the document outline.

Extracted model::

    {
      "meta": {"page_count": int},
      "pages": [
        {"num", "width", "height",
         "lines": [ {text, bbox[x0,y0,x1,y1], size, font, bold, italic} ],
         "words": [ {text, bbox[x0,y0,x1,y1]} ]}
      ],
      "outline": [{"level", "title", "page"}]
    }

Everything is JSON-serializable (floats rounded, plain types).  ``lines`` come
from ``get_text("dict")`` (carries font/size/flags); ``words`` come from
``get_text("words")`` (carries exact per-word bboxes).  Bold/italic are derived
from the PDF span flags plus the font name.
"""

from __future__ import annotations

# PyMuPDF span flag bits
BOLD_FLAG = 16
ITALIC_FLAG = 2


def _span_bold(span) -> bool:
    fl = int(span.get("flags", 0) or 0)
    return bool(fl & BOLD_FLAG) or "bold" in (span.get("font") or "").lower()


def _span_italic(span) -> bool:
    fl = int(span.get("flags", 0) or 0)
    name = (span.get("font") or "").lower()
    return bool(fl & ITALIC_FLAG) or "italic" in name or "oblique" in name


def _round(v: float, ndigits: int = 2) -> float:
    return round(float(v), ndigits)


def extract_lines(page) -> list[dict]:
    """Extract per-text-line geometry + font/style from a page's dict stream."""
    lines: list[dict] = []
    blocks = page.get_text("dict").get("blocks", [])
    for block in blocks:
        if block.get("type") not in (0,):  # text blocks only
            continue
        for raw in block.get("lines", []):
            spans = raw.get("spans", [])
            if not spans:
                continue
            text = "".join(sp.get("text", "") for sp in spans)
            x0, y0, x1, y1 = [float(v) for v in raw["bbox"]]
            size = max((float(sp.get("size", 0) or 0) for sp in spans), default=0.0)
            font = spans[0].get("font", "") or ""
            lines.append(
                {
                    "text": text,
                    "bbox": [_round(x0), _round(y0), _round(x1), _round(y1)],
                    "size": _round(size),
                    "font": font,
                    "bold": any(_span_bold(sp) for sp in spans),
                    "italic": any(_span_italic(sp) for sp in spans),
                }
            )
    return lines


def extract_words(page) -> list[dict]:
    """Extract exact word bboxes from a page (``get_text(\"words\")``)."""
    words: list[dict] = []
    for w in page.get_text("words"):
        x0, y0, x1, y1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
        words.append(
            {"text": w[4], "bbox": [_round(x0), _round(y0), _round(x1), _round(y1)]}
        )
    return words


def extract(path: str) -> dict:
    """Extract the structural model of ``path`` (a PDF file or bytes holder).

    Args:
        path: path to a PDF file.

    Returns:
        The JSON-safe structural model described in the module docstring.
    """
    import pymupdf

    doc = pymupdf.open(path)
    pages: list[dict] = []
    for pno in range(doc.page_count):
        page = doc[pno]
        pages.append(
            {
                "num": pno + 1,
                "width": _round(page.rect.width),
                "height": _round(page.rect.height),
                "lines": extract_lines(page),
                "words": extract_words(page),
            }
        )

    outline: list[dict] = []
    for entry in doc.get_toc(simple=True) or []:
        # simple=True -> [level, title, page, ...]
        try:
            lvl, title, page_no = entry[0], entry[1], entry[2]
        except (IndexError, ValueError):  # pragma: no cover - malformed outline
            continue
        outline.append({"level": int(lvl), "title": str(title), "page": int(page_no)})

    meta = {"page_count": doc.page_count}
    result = {"meta": meta, "pages": pages, "outline": outline}
    doc.close()
    return result
