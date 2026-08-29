"""Normalization — Commit 7D.

Eliminates *unimportant* differences before comparison so the metrics don't
count noise, while never hiding *real* drift:

- PDF object ordering / metadata: ignored entirely (we only read extraction
  output, not PDF internals).
- floating-point noise: bboxes are already rounded to 2 dp at extraction.
- font identity noise: subset prefixes (``ABCDEF+``) and near-duplicate family
  names collapse into canonical families via :func:`normalize_font`.
- whitespace noise: :func:`canon` collapses runs of whitespace.

It does **not** smooth away geometric/style/material drift: bbox movement, font
family change, list indentation, TOC page column, missing/extra text all
survive into the compared representation.
"""

from __future__ import annotations

from pdf2zh.semantic.eval.extract import extract

_CJK_HINTS = (
    "cjk",
    "noto",
    "notosans",
    "simsun",
    "simhei",
    "songti",
    "heiti",
    "kaiti",
    "fangsong",
    "yahei",
    "msyh",
    "china",
    "pingfang",
    "sourcehan",
    "droidsansfallback",
)


def normalize_font(name: str) -> str:
    """Map a raw font name to a canonical family token.

    Strips the ``ABCDEF+`` subset prefix, lower-cases, keeps alphanumerics
    only, then collapses well-known families (Times / Helvetica-Arial / Courier
    / CJK).  Unknown families fall back to their cleaned name or ``"unknown"``.
    Two spells of the same real family therefore match; a genuinely different
    family does not collide.
    """
    s = (name or "").strip()
    if "+" in s:
        s = s.split("+", 1)[1]
    s = "".join(ch for ch in s.lower() if ch.isalnum())
    if not s:
        return "unknown"
    if s.startswith("times") or "timesnewroman" in s:
        return "times"
    if s.startswith("helv") or "arial" in s or "helvetica" in s:
        return "helv"
    if s.startswith("cour") or "courier" in s:
        return "cour"
    for hint in _CJK_HINTS:
        if hint in s:
            return "cjk"
    return s


def canon(text: str) -> str:
    """Collapse internal whitespace to single spaces and strip ends."""
    return " ".join((text or "").split())


def normalize_doc(doc: dict) -> dict:
    """Return a copy of an extracted doc with normalization applied.

    - line ``font`` -> :func:`normalize_font`
    - line/word ``text`` -> :func:`canon`
    - bboxes re-rounded (idempotent)
    - outline titles canonicalized
    """
    out = {"meta": dict(doc.get("meta", {})), "pages": [], "outline": []}
    for pg in doc.get("pages", []):
        npg = {
            "num": int(pg["num"]),
            "width": float(pg["width"]),
            "height": float(pg["height"]),
            "lines": [],
            "words": [],
        }
        for ln in pg.get("lines", []):
            npg["lines"].append(
                {
                    "text": canon(ln["text"]),
                    "bbox": [round(float(v), 2) for v in ln["bbox"]],
                    "size": round(float(ln["size"]), 2),
                    "font": normalize_font(ln["font"]),
                    "bold": bool(ln.get("bold")),
                    "italic": bool(ln.get("italic")),
                }
            )
        for w in pg.get("words", []):
            npg["words"].append(
                {
                    "text": canon(w["text"]),
                    "bbox": [round(float(v), 2) for v in w["bbox"]],
                }
            )
        out["pages"].append(npg)
    for e in doc.get("outline", []):
        out["outline"].append(
            {
                "level": int(e["level"]),
                "title": canon(e["title"]),
                "page": int(e["page"]),
            }
        )
    return out


def normalize_pdf(path: str) -> dict:
    """Extract + normalize a PDF in one step (handy for tests / baselines)."""
    return normalize_doc(extract(path))