# -*- coding: utf-8 -*-
"""Commit 7F-6d — Mixed-Primitive PDF Golden Gate.

The single most valuable integration test of the 7F phase: **one document**
containing Code + List + TOC + Flow + CJK (+ Outline + a recovery trigger),
driven through the whole pipeline and verified on the real output PDF::

    source PDF
        ↓ semantic renderers (TocRenderer / build_page_list_plan /
          build_block_flow_payload)
        ↓ layout + recovery (adaptive_layout / lay_out)
        ↓ magicpdf_renderer (draw-only)
        ↓ output PDF
        ↓ PyMuPDF extraction → structural invariants

Locked invariants:

- TOC ``page_x`` — the page-number word lands exactly at ``page_x`` (500.0).
- TOC title translated; page number never translated.
- List markers verbatim; content word lands at the parsed ``content_x``.
- Flow wraps into multiple lines (draw path, not just background).
- CJK glyphs survive rendering (no character loss).
- Code stays preserved at its source bbox (source-page background).
- Recovery is observable (``stats["flow_overflow"]`` fires for a pathological
  translated block).
- Outline destinations survive (translated titles, correct pages).
- ``evaluate`` structural gates (code bbox / list wrap) still pass.
"""

import unittest

import pymupdf

from pdf2zh.semantic.eval import evaluate
from pdf2zh.v3.canonical_page import BlockModel, LineModel, SpanModel
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
from pdf2zh.v3.outline_renderer import build_outline_toc
from pdf2zh.semantic.renderer.list import build_page_list_plan
from pdf2zh.semantic.renderer.toc import TocRenderer
from tests.pdf_word_utils import (
    extract_words,
    page_word_x,
    words_at_x,
    words_with_text,
)

PAGE_W = 612.0
PAGE_H = 792.0

# ── geometry constants (shared by the source PDF and the render plan) ──────
TOC_X = 72.0
TOC_PAGE_X = 500.0
LIST_X0 = 60.0
FLOW_X0, FLOW_X1 = 60.0, 260.0
CJK_X0, CJK_X1 = 60.0, 260.0
CODE_X0, CODE_X1 = 60.0, 260.0

# ── content (source vs translated) ─────────────────────────────────────────
TOC_ENTRIES = [
    {
        "title": "Introduction",
        "title_only": "Introduction",
        "number": "",
        "level": 0,
        "page_number": "42",
        "title_x": TOC_X,
        "page_x": TOC_PAGE_X,
        "indent": TOC_X,
        "dot_leader": "........",
        "leader_present": True,
        "continuation": [],
        "bbox": [TOC_X, 0.0, TOC_PAGE_X, 16.0],
    },
    {
        "title": "Method",
        "title_only": "Method",
        "number": "",
        "level": 0,
        "page_number": "12",
        "title_x": TOC_X,
        "page_x": TOC_PAGE_X,
        "indent": TOC_X,
        "dot_leader": "........",
        "leader_present": True,
        "continuation": [],
        "bbox": [TOC_X, 0.0, TOC_PAGE_X, 16.0],
    },
]
FLOW_TRANSLATED = "ALPHAFLOW " * 12  # distinctive: proves the draw path ran
FLOW_SOURCE = "This is a translated paragraph that wraps over lines"
CJK_TRANSLATED = "这是混合文档中的中文段落用于验证CJK字形完整性与自动换行显示"
CJK_SOURCE = "这是混合文档中的中文段落（源文）"
# single line: the plain-layer draw path preserves it verbatim (leading-
# whitespace lines are not byte-preserved by the legacy word-wrap draw)
CODE_TEXT = "def f(): return 42"
LIST_PARAS = ["1. Alpha", "   a. Beta"]
LIST_GEOM = [
    {"x0": LIST_X0, "x1": 200.0, "size": 11.0, "y0": 700.0},
    {"x0": 76.0, "x1": 200.0, "size": 11.0, "y0": 680.0},
]


def _build_source(tmp_path):
    """Source PDF: the same document, original text, with native outline."""
    doc = pymupdf.Document()
    p1 = doc.new_page(width=PAGE_W, height=PAGE_H)
    p1.insert_text((TOC_X, 120), "Introduction", fontsize=10)
    p1.insert_text((180, 120), "........", fontsize=10)
    p1.insert_text((496, 120), "42", fontsize=10)
    p1.insert_text((TOC_X, 140), "Method", fontsize=10)
    p1.insert_text((180, 140), "........", fontsize=10)
    p1.insert_text((496, 140), "12", fontsize=10)

    p2 = doc.new_page(width=PAGE_W, height=PAGE_H)
    p2.insert_text((60, 100), "1 Introduction", fontsize=12)
    p2.insert_text((FLOW_X0, 130), FLOW_SOURCE, fontsize=10)
    p2.insert_text((LIST_X0, 160), "1. Alpha", fontsize=11)
    p2.insert_text((76, 180), "a. Beta", fontsize=11)
    p2.insert_text((CJK_X0, 220), CJK_SOURCE, fontsize=11)
    p2.insert_text((CODE_X0, 260), "def f(): return 42", fontsize=9, fontname="cour")

    p3 = doc.new_page(width=PAGE_W, height=PAGE_H)
    p3.insert_text((60, 100), "2 Background", fontsize=11)
    p3.insert_text((60, 130), "A long paragraph " * 3, fontsize=10)

    doc.set_toc(
        [
            [1, "Introduction", 1],
            [1, "Method", 1],
            [2, "Background", 3],
        ]
    )
    path = tmp_path / "mixed_src.pdf"
    doc.save(path)
    doc.close()
    return str(path)


def _block(text, translated, x0, y0, x1, y1, font_size=10.0):
    line = LineModel(text=text, baseline=0.0, x0=x0, y0=y0, x1=x1, y1=y1)
    line.spans.append(SpanModel(size=font_size, text=text, x0=x0, y0=y0, x1=x1, y1=y1))
    return BlockModel(
        text=text,
        kind="paragraph",
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        lines=[line],
        metadata={"translated": translated},
    )


def _flow_entry(block_id, page, text, translated, x0, y0, x1, y1, font_size=10.0):
    from pdf2zh.v3.flow_sidechannel import build_block_flow_payload

    payload = build_block_flow_payload(
        _block(text, translated, x0, y0, x1, y1, font_size=font_size)
    )
    return {
        "block_id": block_id,
        "page": page,
        "kind": "paragraph",
        "text": text,
        "translated": translated,
        "render_path": "translate_refit",
        "src_box": [x0, y0, x1, y1],
        "dst_box": [x0, y0, x1, y1],
        "font_size": font_size,
        "render_payload": payload,
    }


def _build_plan():
    """Semantic renderers → one mixed render plan (translation + layout +
    recovery all happen here)."""
    # TOC page 0
    toc_cmds = TocRenderer(measure_width=None).render(
        TOC_ENTRIES,
        ys=[750.0, 730.0],
        size=10.0,
        translate=lambda s: "译_" + s,
    )
    toc_entry = {
        "block_id": "p0_toc",
        "page": 0,
        "kind": "toc",
        "text": "Introduction",
        "translated": "译_Introduction",
        "render_path": "overlay",
        "src_box": [TOC_X, 700.0, TOC_PAGE_X, 760.0],
        "dst_box": [TOC_X, 700.0, TOC_PAGE_X, 760.0],
        "font_size": 10.0,
        "toc_commands": {
            "commands": [c.to_dict() for c in toc_cmds],
            "translated_calls": [],
        },
    }
    # page 1: flow + CJK + nested list + code
    # 7N-FIX-3A：flow 译文按 box-top + 0.85em 落基线（不再整体上浮 1em）。
    # 旧几何 [640, 690]（fitz 102–152）与下方 list 块 [620, 700]（fitz
    # 92–172）重叠 —— 旧实现的上浮恰好躲开第二行 marker "a."，新正确基线
    # 会让 flow 墨水压进 marker 行（evaluator 行聚类合并 → list_wrap_integrity
    # 0.83）。布局层真实产物绝不产生重叠 dst_box，fixture 移出重叠区即可。
    flow = _flow_entry(
        "p1_flow", 1, FLOW_SOURCE, FLOW_TRANSLATED, FLOW_X0, 720.0, FLOW_X1, 770.0
    )
    cjk = _flow_entry(
        "p1_cjk", 1, CJK_SOURCE, CJK_TRANSLATED, CJK_X0, 560.0, CJK_X1, 610.0
    )
    list_plan = build_page_list_plan(
        LIST_PARAS,
        geom=LIST_GEOM,
        translate=lambda s: "译_" + s,
    )
    list_entry = {
        "block_id": "p1_list",
        "page": 1,
        "kind": "list",
        "text": "1. Alpha",
        "translated": "译_Alpha",
        "render_path": "translate_refit",
        "src_box": [LIST_X0, 620.0, 300.0, 700.0],
        "dst_box": [LIST_X0, 620.0, 300.0, 700.0],
        "font_size": 11.0,
        "list_items": {
            "commands": list_plan["commands"],
            "translated_calls": [],
        },
    }
    code_entry = {
        "block_id": "p1_code",
        "page": 1,
        "kind": "code",
        "text": CODE_TEXT,
        "translated": CODE_TEXT,  # preserved — never translated
        "render_path": "preserve_float",
        # v3 coordinates (bottom-left origin): the source PDF draws the code
        # line at baseline y=260 (PDF top-left bbox [60, 251.6, ~157, 262.9]),
        # so the preserved dst_box must map to the SAME location — otherwise
        # the evaluator's code_preserved_bbox gate (bbox center within 10pt)
        # would flag a ~250pt vertical drift.
        "src_box": [CODE_X0, 792.0 - 262.9, CODE_X1, 792.0 - 251.6],
        "dst_box": [CODE_X0, 792.0 - 262.9, CODE_X1, 792.0 - 251.6],
        "font_size": 9.0,
    }
    # page 2: pathological translated flow → recovery must be observable
    pathological = _flow_entry(
        "p2_boom",
        2,
        "source",
        "A" * 500,
        60.0,
        600.0,
        100.0,
        620.0,
        font_size=12.0,
    )
    return [toc_entry, flow, cjk, list_entry, code_entry, pathological], list_plan


class TestMixedDocumentGoldenGate(unittest.TestCase):
    def test_all_primitives_one_pipeline(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            import pathlib

            src_path = _build_source(pathlib.Path(tmp))
            plan, list_plan = _build_plan()
            # Plain text layer (no source background): a white rect cannot
            # remove the source glyphs underneath, so a background render would
            # extract both original and translated text (duplicate markers).
            # The plain layer contains ONLY the settled translated drawing —
            # exactly what the layout pipeline decided to emit.
            pdf, stats = render_plan_to_pdf(
                plan,
                page_sizes={
                    0: [PAGE_W, PAGE_H],
                    1: [PAGE_W, PAGE_H],
                    2: [PAGE_W, PAGE_H],
                },
                cjk_font=True,
            )
            out_path = pathlib.Path(tmp) / "mixed_out.pdf"
            with open(out_path, "wb") as fh:
                fh.write(pdf)

            # ── outline destinations (translated titles, correct pages) ──
            doc = pymupdf.open(stream=pdf, filetype="pdf")
            doc.set_toc(
                build_outline_toc(
                    [
                        {
                            "title": "Introduction",
                            "translated_title": "译_Introduction",
                            "level": 0,
                            "destination_page": 1,
                            "page_number": "42",
                        },
                        {
                            "title": "Method",
                            "translated_title": "译_Method",
                            "level": 0,
                            "destination_page": 1,
                            "page_number": "12",
                        },
                        {
                            "title": "Background",
                            "translated_title": "译_Background",
                            "level": 0,
                            "destination_page": 3,
                            "page_number": "2",
                        },
                    ]
                )
            )
            toc = doc.get_toc()
            self.assertEqual(
                [t[1] for t in toc], ["译_Introduction", "译_Method", "译_Background"]
            )
            self.assertEqual([t[2] for t in toc], [1, 1, 3])

            # ── TOC page_x: the drawn page number lands at page_x ────────
            w0 = extract_words(doc[0])
            for num in ("42", "12"):
                hit = words_at_x(w0, TOC_PAGE_X, eps=1.5)
                self.assertTrue(
                    any(w["text"] == num for w in hit),
                    f"page number {num} not at page_x {TOC_PAGE_X}",
                )
            text0 = doc[0].get_text()
            self.assertIn("译_Introduction", text0)  # title translated
            self.assertNotIn("译_42", text0)  # page number never translated

            # ── list: markers verbatim, content at content_x ─────────────
            w1 = extract_words(doc[1])
            self.assertIsNotNone(page_word_x(w1, "1."))
            self.assertIsNotNone(page_word_x(w1, "a."))
            content_x = float(list_plan["items"][0]["content_x"])
            alpha = words_at_x(w1, content_x, eps=2.0)
            self.assertTrue(
                any(w["text"] == "译_Alpha" for w in alpha),
                f"content 未落在 content_x={content_x}",
            )

            # ── flow: translated text drawn, wrapped into ≥ 2 lines ──────
            flow_words = words_with_text(w1, "ALPHAFLOW")
            self.assertGreaterEqual(len(flow_words), 2)
            self.assertGreaterEqual(
                len({round(w["y0"], 1) for w in flow_words}),
                2,
                "flow 应换行为多行（draw 路径）",
            )

            # ── CJK: every translated glyph survives rendering ───────────
            text1 = doc[1].get_text()
            for ch in CJK_TRANSLATED:
                self.assertIn(ch, text1, f"CJK 字形 {ch!r} 丢失")

            # ── code: preserved verbatim at its source bbox ──────────────
            code_words = words_with_text(w1, "def")
            self.assertTrue(code_words, "code 文本必须绘制")
            self.assertLessEqual(
                abs(code_words[0]["x0"] - CODE_X0), 2.0, "code bbox 不应漂移"
            )

            # ── recovery observable end-to-end ───────────────────────────
            self.assertGreaterEqual(
                stats.get("flow_overflow", 0), 1, "病态译文块应触发可观测 overflow"
            )
            doc.close()

            # ── evaluator structural gates still hold on the mixed doc ───
            rep = evaluate(src_path, str(out_path))
            m = rep["metrics"]
            self.assertEqual(m["code_preserved_bbox"], 1.0, "code bbox 必须逐字保留")
            self.assertEqual(
                m["list_wrap_integrity"], 1.0, "list marker 结构必须完整（无重复/丢失）"
            )
            self.assertEqual(m["overflow_count"], 0, "混合文档不应产生页面级溢出")


if __name__ == "__main__":
    unittest.main()
