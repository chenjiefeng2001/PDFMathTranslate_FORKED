"""Commit 7F-5c — real-PDF Adaptive TOC golden cases.

Proves the 7F-5a/5b layout contract survives the full render chain:

    TOCEntryNode → layout_toc_entry → TocEntryLayoutResult
        → toc_layout_commands → render_payload → magicpdf_renderer
        → PDF → PyMuPDF get_text("words")

Six golden cases:

1. Short title → 1 line, no overflow, page number at page_x.
2. Long title → WRAP: every wrapped line's first word lands at title_x.
3. Long title → SHRINK: font_size drops, no overflow, page_x unchanged.
4. Extremely long title → PRESERVE_OVERFLOW (never CLIP, never silent).
5. CJK title → correct glyph wrap, no character loss, geometry stable.
6. Multi-line continuation → continuation word lands at continuation_x.

Every case runs the double ``page_x`` verification: the LayoutResult keeps
``page_x`` verbatim AND the rendered page-number word's x0 equals it — the
geometry must not drift at any layer.

Words are read through ``tests.pdf_word_utils.extract_words`` (explicit
``w["text"]`` / ``w["x0"]`` dict fields) — no implicit tuple slicing.
"""

import unittest

import pymupdf

from pdf2zh.semantic.layout.toc_layout import layout_toc_entry
from pdf2zh.semantic.renderer.toc import TocRenderer
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
from tests.pdf_word_utils import (
    assert_page_column_stable,
    extract_words,
    page_word_x,
    words_at_x,
    words_with_text,
)


def _fixed_measure(text, size=10.0):
    w = 0.0
    for ch in text or "":
        if ch.isspace() or ch == ".":
            w += size * 0.3
        elif ord(ch) >= 0x2E80:
            w += size * 1.0
        else:
            w += size * 0.5
    return w


def _entry(
    title_x=72.0,
    page_x=500.0,
    level=0,
    number="",
    title_only="Introduction",
    page_number="12",
    dot_leader="......",
    leader_present=True,
    continuation=None,
    continuation_x=None,
):
    d = {
        "title": f"{number} {title_only}".strip(),
        "number": number,
        "title_only": title_only,
        "level": level,
        "page_number": page_number,
        "title_x": title_x,
        "page_x": page_x,
        "indent": title_x,
        "dot_leader": dot_leader,
        "leader_present": leader_present,
        "continuation": list(continuation or []),
        "bbox": [title_x, 0.0, page_x, 16.0],
    }
    if continuation_x is not None:
        d["continuation_x"] = float(continuation_x)
    return d


def _layout(entry, translated, **kw):
    """Layout contract layer (same measurer the renderer uses)."""
    return layout_toc_entry(
        entry, measure=_fixed_measure, size=10.0, translated_title=translated, **kw
    )


def _render(entry, translate, ys=None, cjk=False):
    """Render chain layer: TocRenderer → render_plan_to_pdf → words."""
    renderer = TocRenderer(measure_width=_fixed_measure)
    cmds = renderer.render([entry], ys=[ys or 750.0], size=10.0, translate=translate)
    plan_entry = {
        "page": 0,
        "block_id": "p0_toc",
        "kind": "toc",
        "text": entry.get("title", ""),
        "translated": entry.get("title_only", ""),
        "render_path": "overlay",
        "src_box": [entry["title_x"], 700.0, entry["page_x"], 760.0],
        "dst_box": [entry["title_x"], 700.0, entry["page_x"], 760.0],
        "font_size": 10.0,
        "toc_commands": {
            "commands": [c.to_dict() for c in cmds],
            "translated_calls": [],
        },
    }
    pdf_bytes, _stats = render_plan_to_pdf(
        [plan_entry],
        page_sizes={0: [612.0, 792.0]},
        cjk_font=True,
        source_pdf=None,
    )
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[0]
        words = extract_words(page)
        text = page.get_text()
        return words, text
    finally:
        doc.close()


class TestShortTitle(unittest.TestCase):
    def test_one_line_no_overflow(self):
        e = _entry(title_only="Introduction", page_number="12")
        r = _layout(e, translated="Introduction")
        self.assertEqual(r.line_count, 1)
        self.assertFalse(r.overflow)
        self.assertIsNone(r.recovery)  # NO_ACTION
        words, text = _render(e, lambda s: s)
        # page number exactly once, at page_x
        self.assertEqual(len(words_with_text(words, "12")), 1)
        assert_page_column_stable(r, words, page_x=500.0, page_number="12")
        # title word at title_x
        intro = words_at_x(words, 72.0)
        self.assertTrue(intro, "title should land at title_x")
        self.assertIn("Introduction", text)


class TestLongTitleWrap(unittest.TestCase):
    def test_wrapped_lines_keep_title_x(self):
        e = _entry(title_only="A", page_x=500.0, page_number="12")
        translated = ("word " * 30).strip()  # ~690pt → wraps to 2 lines
        r = _layout(e, translated=translated)
        self.assertGreaterEqual(r.line_count, 2)
        self.assertFalse(r.overflow)
        self.assertEqual(r.recovery["decision"], "wrap")
        self.assertNotIn("SHRINK", r.recovery["steps"])
        words, _ = _render(e, lambda s: translated)
        # every wrapped line's first word lands at the title anchor column
        line_starts = words_at_x(words, 72.0, eps=2.0)
        self.assertGreaterEqual(
            len(line_starts), 2, "each wrapped line must start at title_x"
        )
        # page number still exactly once at page_x
        assert_page_column_stable(r, words, page_x=500.0, page_number="12")


class TestLongTitleShrink(unittest.TestCase):
    def test_shrink_drops_font_keeps_page_x(self):
        e = _entry(title_only="A", page_x=500.0, page_number="12")
        translated = ("word " * 60).strip()  # ~1380pt → 4+ lines → SHRINK
        r = _layout(e, translated=translated)
        self.assertIn("SHRINK", r.recovery["steps"])
        self.assertEqual(r.recovery["decision"], "shrink")
        self.assertLess(r.title.font_size, 10.0)  # font_size_after < before
        self.assertFalse(r.overflow)
        self.assertLessEqual(r.line_count, 3)  # fits the 1+2 budget
        words, _ = _render(e, lambda s: translated)
        assert_page_column_stable(r, words, page_x=500.0, page_number="12")


class TestExtremeTitlePreserveOverflow(unittest.TestCase):
    def test_preserve_overflow_never_clip(self):
        e = _entry(title_only="A", page_x=500.0, page_number="12")
        translated = ("word " * 200).strip()  # even at min font > 3 lines
        r = _layout(e, translated=translated)
        self.assertTrue(r.overflow)
        self.assertEqual(r.recovery["decision"], "preserve_overflow")
        self.assertNotIn("CLIP", r.recovery["steps"])  # CLIP never appears
        words, text = _render(e, lambda s: translated)
        # page number still pinned at page_x (overflow explicit, not hidden)
        assert_page_column_stable(r, words, page_x=500.0, page_number="12")
        # title text is NOT truncated away
        self.assertIn("word", text)


class TestCjkTitle(unittest.TestCase):
    def test_cjk_wraps_without_char_loss(self):
        e = _entry(title_only="A", page_x=500.0, page_number="12")
        cjk = "这是一个非常非常长的目录标题用于测试自适应布局的正确换行显示它应该被正确的分到多行以保持页面整洁美观"
        r = _layout(e, translated=cjk)
        self.assertGreaterEqual(r.line_count, 2)
        self.assertFalse(r.overflow)
        words, text = _render(e, lambda s: cjk, cjk=True)
        # no character loss: every CJK glyph survives into the PDF text layer
        for ch in cjk:
            self.assertIn(ch, text, f"CJK glyph {ch!r} lost in render")
        # geometry stable: title_x / page_x both preserved
        assert_page_column_stable(r, words, page_x=500.0, page_number="12")
        self.assertEqual(r.title_x, 72.0)


class TestMultilineContinuation(unittest.TestCase):
    def test_continuation_word_lands_at_continuation_x(self):
        e = _entry(
            title_only="Long title",
            page_x=500.0,
            page_number="42",
            continuation=["part two"],
            continuation_x=100.0,
        )
        r = _layout(
            e,
            translated="Long translated title",
            translated_continuation=["译_part two"],
        )
        self.assertEqual(r.continuation_x, 100.0)
        self.assertEqual(r.continuation[0].bbox[0], 100.0)
        words, _ = _render(
            e, lambda s: "Long translated title" if s == "Long title" else f"译_{s}"
        )
        # the continuation word lands at continuation_x in the real PDF
        cont = words_at_x(words, 100.0, eps=2.0)
        self.assertTrue(cont, "continuation should land at continuation_x")
        # page number still at page_x, once
        assert_page_column_stable(r, words, page_x=500.0, page_number="42")


class TestLeaderContract(unittest.TestCase):
    def test_leader_never_crosses_page_x(self):
        e = _entry(title_only="Intro", page_x=500.0, page_number="5")
        r = _layout(e, translated="Introduction")
        words, _ = _render(e, lambda s: s)
        if r.leader is not None:
            leader_end = r.leader.bbox[0] + r.leader.line_widths[0]
            self.assertLessEqual(leader_end, 500.0)
        # page number never moves
        self.assertEqual(page_word_x(words, "5"), 500.0)

    def test_no_leader_never_creates_dots(self):
        e = _entry(
            title_only="Intro",
            page_x=500.0,
            page_number="5",
            leader_present=False,
            dot_leader="",
        )
        r = _layout(e, translated="Introduction")
        self.assertIsNone(r.leader)
        words, text = _render(e, lambda s: s)
        # no stray dot runs in the rendered text layer
        self.assertNotRegex(text, r"\.{3,}")


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__]))
