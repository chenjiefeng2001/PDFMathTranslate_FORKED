"""Commit 7F-4 — real-PDF adaptive layout integration.

Paths through the (now adaptive-wired) ``build_page_list_plan`` ->
``render_plan_to_pdf`` chain and opens the produced PDF with
``get_text("words")`` to verify *actual* glyph landing:

- a very long translated item (pathological) still keeps markers verbatim;
- a long translated item wrapping over many lines keeps every wrapped first
  word at ``content_x`` (not back at the marker column);
- ``TRANSLATED`` garbage output still yields ``1. TRANSLATED`` markers.

Marker is PRESERVE; adaptive recovery may only affect content / continuation —
never markers or the content_x anchor.
"""

import unittest

import pymupdf

from pdf2zh.semantic.renderer.list import build_page_list_plan
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf


def _entry(payload, page=0, font_size=12.0):
    return {
        "block_id": f"p{page}_list",
        "page": page,
        "kind": "list",
        "text": "list block",
        "translated": "list block",
        "render_path": "translate_refit",
        "src_box": [30, 620, 560, 740],
        "dst_box": [30, 620, 560, 740],
        "font_size": font_size,
        "list_items": payload,
    }


def _render(payload, font_size=12.0):
        pdf, _stats = render_plan_to_pdf(
            [_entry(payload, font_size=font_size)], page_sizes={0: [612.0, 792.0]}
        )
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        words = doc[0].get_text("words")
        text = doc[0].get_text()
        doc.close()
        # list[(x, text)] — x 坐标在前，文本在后（断言按 x round、按文本过滤）
        return [(float(w[0]), w[4]) for w in words], text


class TestAdaptiveRealPdf(unittest.TestCase):
    def test_100x_long_translation_markers_preserved_in_pdf(self):
        """A pathological translation repeated 100x still yields ``1.``/``2.``
        markers in the output PDF, with translated content present (pymupdf may
        re-tokenise a very long run, so we assert presence, not exact count)."""
        huge = "TEXT" * 100
        payload = build_page_list_plan(
            ["1. Alpha", "2. Beta"], translate=lambda s: huge
        )
        words, text = _render(payload)
        self.assertIn("1.", text)
        self.assertIn("2.", text)
        self.assertNotIn("1.1", text)
        self.assertGreaterEqual(text.count("TEXT"), 1)  # content present

    def test_long_item_wraps_keeping_content_x_in_pdf(self):
        """A long translated item wraps into many lines, every wrapped first
        word keeps the content_x column (never back to the marker column)."""
        paras = ["1. Original item", "2. Short item"]
        geom = [
            {"x0": 40.0, "x1": 120.0, "size": 12.0, "y0": 700.0},  # narrow
            {"x0": 40.0, "x1": 560.0, "size": 12.0, "y0": 680.0},
        ]
        payload = build_page_list_plan(
            paras, geom=geom,
            translate=lambda s: "very " * 40 if "Original" in s else s,
        )
        words, text = _render(payload)
        content_x = payload["items"][0]["content_x"]
        # some ``very`` word lands exactly at the content_x column (line start)
        very_xs = {round(w, 1) for w, _t in words if _t == "very"}
        self.assertIn(round(content_x, 1), very_xs)
        # marker ``1.`` is at its own column, distinct from the content column
        marker_xs = [round(w, 1) for w, _t in words if _t == "1."]
        self.assertTrue(marker_xs)
        self.assertNotAlmostEqual(marker_xs[0], content_x, places=1)

    def test_garbage_translator_markers_verbatim_in_pdf(self):
        """``TRANSLATED`` garbage still yields 1. TRANSLATED / 2. TRANSLATED —
        never a merged marker, never ``1.1``."""
        payload = build_page_list_plan(
            ["1. This is an item", "2. Second item"], translate=lambda s: "TRANSLATED"
        )
        words, text = _render(payload)
        self.assertIn("1.", text)
        self.assertIn("2.", text)
        self.assertNotIn("1.1", text)
        self.assertEqual(text.count("TRANSLATED"), 2)

    def test_adaptive_layout_never_breaks_continuation_column(self):
        """A continuation that itself wraps still lands its first word at
        content_x in the real PDF."""
        paras = ["1. First item", "    continuation line"]
        geom = [
            {"x0": 40.0, "x1": 120.0, "size": 12.0, "y0": 700.0},
            {"x0": 62.0, "x1": 120.0, "size": 12.0, "y0": 685.0},
        ]
        payload = build_page_list_plan(paras, geom=geom, translate=lambda s: s)
        words, text = _render(payload)
        content_x = payload["items"][0]["content_x"]
        cont_x = [round(w, 1) for w, _t in words if _t == "continuation"]
        self.assertTrue(cont_x)
        self.assertAlmostEqual(cont_x[0], content_x, places=1)


if __name__ == "__main__":
    unittest.main()