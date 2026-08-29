"""Commit 7E-2d — magicpdf list layout: real PDF geometry verification.

Opens the produced PDF with PyMuPDF ``get_text("words")`` and measures
actual glyph columns:

- nested list content_x strictly increases down levels (x1 < x2 < x3);
- continuation lines land exactly at their item's content_x;
- a long translated item wraps into multiple lines, every wrapped line
  keeping the content_x column (renderer never re-decides placement);
- garbage translator output still keeps ``1. TRANSLATED`` markers.

The render payload keeps ``kind == "list"`` and the legacy ``list_items``
field; the renderer draws the settled commands without re-layout.
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
    doc.close()
    return {w[4]: float(w[0]) for w in words}


class TestMagicPdfListLayout(unittest.TestCase):
    def test_nested_content_x_strictly_increasing_in_pdf(self):
        paras = ["1. Intro", "   a. Background", "      i. deep"]
        geom = [
            {"x0": 40.0, "x1": 300.0, "size": 12.0, "y0": 700.0},
            {"x0": 52.0, "x1": 300.0, "size": 12.0, "y0": 680.0},
            {"x0": 64.0, "x1": 300.0, "size": 12.0, "y0": 660.0},
        ]
        payload = build_page_list_plan(paras, geom=geom, translate=lambda s: f"C[{s}]")
        words = _render(payload)
        # 三个 content 的真实 x 列：逐级右移（不是 JSON 假装不同）
        xs = [words[f"C[{t}]"] for t in ("Intro", "Background", "deep")]
        self.assertGreater(xs[1], xs[0])
        self.assertGreater(xs[2], xs[1])
        # marker 列也在（原样保留）
        self.assertIn("1.", words)
        self.assertIn("a.", words)
        self.assertIn("i.", words)

    def test_continuation_x_equals_content_x_in_pdf(self):
        paras = ["1. First item", "    continuation line"]
        geom = [
            {"x0": 40.0, "x1": 560.0, "size": 12.0, "y0": 700.0},
            {"x0": 62.0, "x1": 560.0, "size": 12.0, "y0": 685.0},
        ]
        payload = build_page_list_plan(paras, geom=geom, translate=lambda s: s)
        words = _render(payload)
        content_x = payload["items"][0]["content_x"]
        # 延续行首词落位 == content_x（精确列对齐）
        self.assertIn("continuation", words)
        self.assertAlmostEqual(words["continuation"], content_x, places=1)
        # 且与 marker 列不同
        self.assertNotAlmostEqual(words["continuation"], words["1."], places=1)

    def test_long_item_wraps_keeping_content_x(self):
        long = "This is a very long translated list item that cannot fit on one single line"
        paras = ["1. Original item", "2. Short item"]
        geom = [
            {"x0": 40.0, "x1": 200.0, "size": 12.0, "y0": 700.0},  # 窄 → wrap
            {"x0": 40.0, "x1": 560.0, "size": 12.0, "y0": 680.0},
        ]
        payload = build_page_list_plan(
            paras, geom=geom, translate=lambda s: long if "Original" in s else s
        )
        words = _render(payload)
        # 第一项 wrap 成 ≥2 行：长文本的所有词都在
        text = " ".join(sorted(words, key=words.get))
        self.assertIn("cannot", text)
        self.assertIn("single", text)
        # 所有 wrap 行的首词 x == content_x（不落回 marker 列）
        content_x = payload["items"][0]["content_x"]
        wrapped_first_words = [w for w in words if words[w] == content_x]
        self.assertGreaterEqual(len(wrapped_first_words), 2)

    def test_garbage_translator_markers_preserved_in_pdf(self):
        payload = build_page_list_plan(
            ["1. This is an item", "2. Second item"], translate=lambda s: "TRANSLATED"
        )
        words = _render(payload)
        text = " ".join(sorted(words, key=words.get))
        self.assertIn("1.", text)
        self.assertIn("2.", text)
        self.assertNotIn("1.1", text)
        self.assertEqual(text.count("TRANSLATED"), 2)


if __name__ == "__main__":
    unittest.main()
