"""Fixture builder for the 7D PDF-eval tests.

Creates tiny *real* PDFs with PyMuPDF so the evaluator runs on actual
extration output: controllable text, fonts (incl. bold/italic builtins and a
CJK font), mono code font, list indentation, TOC columns, and outlines.
"""

import pymupdf

#: pymupdf built-in font names mapped to semantic style/family for clarity.
FONT = {
    "body": "helv",  # Helvetica
    "bold": "hebo",  # Helvetica-Bold
    "italic": "heit",  # Helvetica-Oblique
    "bolditalic": "hebi",  # Helvetica-BoldOblique
    "serif": "tiro",  # Times
    "mono": "cour",  # Courier
    "cjk": "china-s",  # built-in simplified Chinese
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
        page.insert_text(
            (float(x), float(y)), text, fontname=font, fontsize=float(size), color=color
        )
    return page


def new_doc():
    return pymupdf.Document()


def write(doc, path):
    doc.save(str(path))
    doc.close()


def build_prose(path, with_outline=False):
    """A plain prose page (body + a bold title + an italic emphasis line)."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 90, "Title Here", "bold", 18),
            (
                72,
                130,
                "This is a normal body paragraph with several words.",
                "body",
                11,
            ),
            (72, 150, "And an italicised emphasis line.", "italic", 11),
            (72, 190, "A second short paragraph.", "body", 11),
        ],
    )
    if with_outline:
        doc.set_toc([[1, "Introduction", 1]])
    write(doc, path)
    return path


def build_list(path, width=612.0, height=792.0):
    """A list page: two items + one wrapped continuation line per item."""
    doc = new_doc()
    add_page(
        doc,
        [
            (60, 100, "1. Algorithm design", "body", 12),
            (95, 115, "a wrapped continuation line", "body", 12),
            (60, 150, "2. Evaluation on the test set", "body", 12),
            (95, 165, "second continuation line", "body", 12),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_nested_list(path, width=612.0, height=792.0):
    """A three-level nested list (1 → a → i) for wrap/nesting metrics."""
    doc = new_doc()
    add_page(
        doc,
        [
            (40, 100, "1. Intro", "body", 12),
            (52, 120, "a. Background", "body", 12),
            (64, 140, "i. deep", "body", 12),
            (40, 180, "2. Method", "body", 12),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc(path, width=612.0, height=792.0):
    """A two-entry TOC page with a right-aligned page column."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            (72, 110, "Introduction ......... 1", "body", 12),
            (96, 135, "Background .......... 3", "body", 12),
        ],
        width=width,
        height=height,
    )
    doc.set_toc([[1, "Introduction", 1], [2, "Background", 1]])
    write(doc, path)
    return path


def build_toc_no_leader(path, width=612.0, height=792.0):
    """A TOC whose entries have a right-aligned page column but no dots."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            (72, 110, "Introduction", "body", 12),
            (500, 110, "1", "body", 12),
            (96, 135, "Background", "body", 12),
            (500, 135, "3", "body", 12),
        ],
        width=width,
        height=height,
    )
    doc.set_toc([[1, "Introduction", 1], [2, "Background", 1]])
    write(doc, path)
    return path


def build_toc_multiline(path, width=612.0, height=792.0):
    """A multi-line TOC entry: title + wrapped continuation at title_x."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            (72, 110, "A very long title that ......... 1", "body", 12),
            (82, 130, "continues here", "body", 12),
            (96, 155, "Background .......... 3", "body", 12),
        ],
        width=width,
        height=height,
    )
    doc.set_toc([[1, "Introduction", 1], [2, "Background", 1]])
    write(doc, path)
    return path


def build_code(path, width=612.0, height=792.0):
    """Two monospaced code lines (preserved region)."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 90, "def f(x, y):", "mono", 10),
            (84, 110, "return x + long_identifier_name(y)", "mono", 10),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_cjk(path, width=612.0, height=792.0):
    """A mixed Chinese/English paragraph on a CJK font."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 100, "中文标题", "cjk", 16),
            (72, 130, "这是中文正文 content 与 English 混合的句子。", "cjk", 12),
            (72, 160, "另一段中文 text 行。", "cjk", 12),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


# -- 7F-5d: adaptive-TOC corpus builders -----------------------------------
#
# Source = the original (short) entry; output = what the 7F-5a/5b layout
# contract renders after translation.  Geometry mirrors the golden renderer:
# the page number stays at ``page_x`` on the first line (entry-level command,
# exactly once), wrapped title lines start at ``title_x`` and step down, and
# the leader shrinks but never moves ``page_x``.

#: wrapped (WRAP, no SHRINK) title — 2 lines, font stays 12pt.
_TOC_WRAP_L1 = "A very long translated introduction title that wraps into two lines"
_TOC_WRAP_L2 = "and continues on the second line"
#: SHRINK title — 45 words need ~4 lines at 12pt but fit 3 lines at 9pt.
_TOC_SHRINK_WORDS = (
    "adaptive layout recovery wraps a long translated table of contents title "
    "into several lines then shrinks the font until every word fits before "
    "the page column stops gracefully without any loss of meaning or "
    "interruption of the reading flow and keeps the page column stable"
).split()
#: extreme title — far beyond any budget; rendered at min font, many lines.
#: unique tokens so no two wrapped lines read identically (double-render check).
_TOC_EXTREME_WORDS = [f"chapter{k}" for k in range(1, 101)]
#: long CJK title that wraps to two lines at 12pt.
_TOC_CJK_TITLE = (
    "这是一个非常非常长的目录标题用于测试自适应布局的正确换行显示"
    "它应该被正确的分到多行以保持页面整洁美观"
)


def _page_column_items(x, y, title, size, page_number="12", page_x=500.0, font="body"):
    """A TOC first line as draw items: title + leader dots, then the page
    number pinned at ``page_x`` (the FixedColumn behaviour of the renderer)."""
    return [
        (x, y, f"{title} .........", font, size),
        (page_x, y, page_number, font, size),
    ]


def build_toc_adaptive_short(path, width=612.0, height=792.0):
    """A single short TOC entry, one line (also the wrap/shrink source)."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, "Introduction", 12.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_wrap_output(path, width=612.0, height=792.0):
    """The same entry after translation: title wrapped to two lines at 12pt."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, _TOC_WRAP_L1, 12.0),
            (72, 132, _TOC_WRAP_L2, "body", 12.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_wrap_missing_word(path, width=612.0, height=792.0):
    """Wrap output with one wrapped word deleted (same-language text loss)."""
    l2 = "and continues on the line"  # "second" dropped
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, _TOC_WRAP_L1, 12.0),
            (72, 132, l2, "body", 12.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_shrink_output(path, width=612.0, height=792.0):
    """SHRINK output: the long title fits 3 lines at 9pt (was 4+ at 12pt)."""
    w = _TOC_SHRINK_WORDS
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, " ".join(w[:15]), 9.0),
            (72, 130, " ".join(w[15:30]), "body", 9.0),
            (72, 150, " ".join(w[30:]), "body", 9.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_shrink_undone(path, width=612.0, height=792.0):
    """Regression: SHRINK ignored — the title drawn at the original 12pt
    keeping the 9pt line breaks, so the wrapped lines overflow the page
    column (a renderer that dropped the SHRINK font-size decision)."""
    w = _TOC_SHRINK_WORDS
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, " ".join(w[:11]), 12.0),
            (72, 134, " ".join(w[11:28]), "body", 12.0),
            (72, 158, " ".join(w[28:]), "body", 12.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_extreme_output(path, width=612.0, height=792.0):
    """PRESERVE_OVERFLOW output: the extreme title fully drawn at min font.

    Matches the renderer's overflow behaviour: the leader is not emitted and
    the page number stays pinned at ``page_x`` (no-leader TOC fallback)."""
    doc = new_doc()
    words = list(_TOC_EXTREME_WORDS)
    per = 18
    lines = [
        (72, 110, " ".join(words[:per]), "body", 5.0),
        (500.0, 110, "12", "body", 5.0),
    ]
    for k in range(1, (len(words) + per - 1) // per):
        lines.append(
            (
                72,
                130 + (k - 1) * 18,
                " ".join(words[k * per : (k + 1) * per]),
                "body",
                5.0,
            )
        )
    add_page(
        doc, [(72, 80, "Contents", "bold", 14)] + lines, width=width, height=height
    )
    write(doc, path)
    return path


def build_toc_adaptive_extreme_clip(path, width=612.0, height=792.0):
    """Regression: CLIP — only the first two lines of the extreme title drawn."""
    doc = new_doc()
    words = list(_TOC_EXTREME_WORDS)
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            (72, 110, " ".join(words[:18]), "body", 5.0),
            (500.0, 110, "12", "body", 5.0),
            (72, 130, " ".join(words[18:36]), "body", 5.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_cjk_source(path, width=612.0, height=792.0):
    """A short single-line CJK TOC entry (the cjk case source)."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, "目录", 12.0, font="cjk"),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_cjk_output(path, width=612.0, height=792.0):
    """The long CJK title wrapped to two lines at 12pt."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, _TOC_CJK_TITLE[:30], 12.0, font="cjk"),
            (72, 132, _TOC_CJK_TITLE[30:], "cjk", 12.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_page_column_shifted(path, width=612.0, height=792.0):
    """Regression: the page-number column moved 20pt right of page_x."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            *_page_column_items(72, 110, _TOC_WRAP_L1, 12.0, page_x=520.0),
            (72, 132, _TOC_WRAP_L2, "body", 12.0),
        ],
        width=width,
        height=height,
    )
    write(doc, path)
    return path


def build_toc_adaptive_cont_shifted(path, width=612.0, height=792.0):
    """Regression: the continuation line dragged 20pt right of its column."""
    doc = new_doc()
    add_page(
        doc,
        [
            (72, 80, "Contents", "bold", 14),
            (72, 110, "A very long title that ......... 1", "body", 12),
            (102, 130, "continues here", "body", 12),
            (96, 155, "Background .......... 3", "body", 12),
        ],
    )
    doc.set_toc([[1, "Introduction", 1], [2, "Background", 1]])
    write(doc, path)
    return path
