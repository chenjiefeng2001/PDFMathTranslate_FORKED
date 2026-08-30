"""Shared PyMuPDF word helper for adaptive layout PDF tests (7F-5c+).

The one canonical way to read ``page.get_text("words")`` in this suite.
A word tuple from PyMuPDF is ``(x0, y0, x1, y1, text, block, line, word)`` —
we always project it to a dict with **explicit field names**, so a caller can
never silently mis-index it (the 7F-4 ``w[:2]`` lesson).

Usage::

    words = extract_words(page)          # list[dict]
    for w in words:
        w["text"], w["x0"]               # text field and left x, always correct
"""

from __future__ import annotations


def extract_words(page) -> list[dict]:
    """``page.get_text("words")`` → ``[{x0, y0, x1, y1, text}, ...]``.

    ``x0``/``y0`` are the word bbox's left / top, ``x1``/``y1`` the right /
    bottom — in PDF (y-down) coordinates.  The caller converts to v3 y-up
    only when the assertion needs it; geometry columns (x) are identical.
    """
    return [
        {
            "x0": float(w[0]),
            "y0": float(w[1]),
            "x1": float(w[2]),
            "y1": float(w[3]),
            "text": w[4],
        }
        for w in page.get_text("words")
    ]


def words_at_x(words: list[dict], x: float, eps: float = 1.0) -> list[dict]:
    """Words whose left edge lands within ``eps`` of ``x`` (column check)."""
    return [w for w in words if abs(w["x0"] - x) <= eps]


def words_with_text(words: list[dict], text: str) -> list[dict]:
    """Words whose extracted text equals ``text`` (exact token match)."""
    return [w for w in words if w["text"] == text]


def page_word_x(words: list[dict], text: str) -> float | None:
    """x0 of the first word whose text equals ``text`` (e.g. a page number)."""
    hits = words_with_text(words, text)
    return hits[0]["x0"] if hits else None


def assert_page_column_stable(result, words, page_x: float, page_number: str,
                              eps: float = 1.5) -> None:
    """Double-layered ``page_x`` verification (7F-5c DoD):

    1. Layout contract — the settled ``TocEntryLayoutResult.page_x`` equals
       the original ``page_x``;
    2. PDF reality — the page-number word's left edge equals ``page_x``.
    """
    # layer 1: semantic -> layout geometry does not drift
    assert abs(float(result.page_x) - float(page_x)) < 1e-6, (
        f"layout page_x drifted: {result.page_x} != {page_x}"
    )
    # layer 2: layout -> render command -> PDF glyph does not drift
    x = page_word_x(words, page_number)
    assert x is not None, f"page number {page_number!r} not found in PDF words"
    assert abs(x - float(page_x)) <= eps, (
        f"PDF page-number x drifted: {x} != {page_x} (eps {eps})"
    )
