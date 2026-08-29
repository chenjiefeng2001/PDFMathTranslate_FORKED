# -*- coding: utf-8 -*-
"""Fixture builder for the 7D PDF-eval tests.

Creates tiny *real* PDFs with PyMuPDF so the evaluator runs on actual
extration output: controllable text, fonts (incl. bold/italic builtins and a
CJK font), mono code font, list indentation, TOC columns, and outlines.
"""

import pymupdf

#: pymupdf built-in font names mapped to semantic style/family for clarity.
FONT = {
    "body": "helv",        # Helvetica
    "bold": "hebo",        # Helvetica-Bold
    "italic": "heit",      # Helvetica-Oblique
    "bolditalic": "hebi",  # Helvetica-BoldOblique
    "serif": "tiro",       # Times
    "mono": "cour",        # Courier
    "cjk": "china-s",      # built-in simplified Chinese
}


def _font(kind: str) -> str:
    return FONT.get(kind or "body", kind or "helv")


def add_page(doc, items, width=612.0, height=792.0):
    """Add a page and draw ``items=[(x, y, text, font, size)]`` (font/size opt)."""
    page = doc.new_page(width=width, height=height)
    for item in items:
        x, y, text = item[0], item[1], item[2]
        font = _font(item[3]) if len(item) > 3 else "helv"
        size = item[4] if len(item) > 4 else 11.0
        color = item[5] if len(item) > 5 else (0, 0, 0)
        page.insert_text((float(x), float(y)), text, fontname=font, fontsize=float(size), color=color)
    return page


def new_doc():
    return pymupdf.Document()


def write(doc, path):
    doc.save(str(path))
    doc.close()


def build_prose(path, with_outline=False):
    """A plain prose page (body + a bold title + an italic emphasis line)."""
    doc = new_doc()
    add_page(doc, [
        (72, 90, "Title Here", "bold", 18),
        (72, 130, "This is a normal body paragraph with several words.", "body", 11),
        (72, 150, "And an italicised emphasis line.", "italic", 11),
        (72, 190, "A second short paragraph.", "body", 11),
    ])
    if with_outline:
        doc.set_toc([[1, "Introduction", 1]])
    write(doc, path)
    return path


def build_list(path, width=612.0, height=792.0):
    """A list page: two items + one wrapped continuation line per item."""
    doc = new_doc()
    add_page(doc, [
        (60, 100, "1. Algorithm design", "body", 12),
        (95, 115, "a wrapped continuation line", "body", 12),
        (60, 150, "2. Evaluation on the test set", "body", 12),
        (95, 165, "second continuation line", "body", 12),
    ], width=width, height=height)
    write(doc, path)
    return path


def build_toc(path, width=612.0, height=792.0):
    """A two-entry TOC page with a right-aligned page column."""
    doc = new_doc()
    add_page(doc, [
        (72, 80, "Contents", "bold", 14),
        (72, 110, "Introduction ......... 1", "body", 12),
        (96, 135, "Background .......... 3", "body", 12),
    ], width=width, height=height)
    doc.set_toc([[1, "Introduction", 1], [2, "Background", 1]])
    write(doc, path)
    return path


def build_code(path, width=612.0, height=792.0):
    """Two monospaced code lines (preserved region)."""
    doc = new_doc()
    add_page(doc, [
        (72, 90, "def f(x, y):", "mono", 10),
        (84, 110, "return x + long_identifier_name(y)", "mono", 10),
    ], width=width, height=height)
    write(doc, path)
    return path


def build_cjk(path, width=612.0, height=792.0):
    """A mixed Chinese/English paragraph on a CJK font."""
    doc = new_doc()
    add_page(doc, [
        (72, 100, "中文标题", "cjk", 16),
        (72, 130, "这是中文正文 content 与 English 混合的句子。", "cjk", 12),
        (72, 160, "另一段中文 text 行。", "cjk", 12),
    ], width=width, height=height)
    write(doc, path)
    return path