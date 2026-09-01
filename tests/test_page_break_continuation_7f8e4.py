# -*- coding: utf-8 -*-
"""Commit 7F-8e-4 — Real-PDF Continuation Golden.

8e-3 proved the continuation executor is correct at the **plan** layer; 8e-4
proves the same geometry survives to the **PDF glyph layer**::

    settle list / toc (page 0, bottom < 0)
        ↓ 8e-1 PageBreakDecision
        ↓ 8e-3 execute_continuation_breaks  (kept page0 + cont page1)
        ↓ render_plan_to_pdf                (draw-only, v3 y-flip)
        ↓ PyMuPDF page.get_text("words")     → deterministic x/y assertions

Locked DoD (read the words layer, never \"looks like it's on the next page\"):

1. **List continuation** — the tail text exists on the real next page; no line
   dropped / duplicated; ``marker`` exactly once; continuation ``x0 == content_x``;
2. **TOC continuation** — every title line exists across pages, ``title_x`` /
   ``continuation_x`` don't drift, the page number is drawn exactly once at
   ``page_x``;
3. **mixed document** (TOC + Flow + List + Code) in ONE ``render_plan_to_pdf``
   — cross-page recoveries never contaminate each other;
4. **boundary cases** — a marker that falls at the page bottom isn't split
   (whole block moves, marker once); code / preserved → no continuation; an
   already-settled kept block is never re-split (0 second splits);
5. **plan_x == PDF x0** for continuation and ``page_x``; ``source_bbox`` 100%
   unchanged; page overflow ``1 → 0``;
6. **regression baseline** — ``tests/baselines/page_break_continuation_7f8e4.json``
   pins the split geometry + render metrics; the golden asserts the live result
   matches it.
"""

import json
import unittest
from pathlib import Path

import pymupdf

from pdf2zh.semantic.layout.page_break_continuation import execute_continuation_breaks
from pdf2zh.semantic.layout.page_flow import build_page_flow_report
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
from tests.pdf_word_utils import (
    extract_words,
    page_word_x,
    words_at_x,
    words_with_text,
)

_HERE = Path(__file__).resolve().parent
_BASELINE = _HERE / "baselines" / "page_break_continuation_7f8e4.json"

PAGE_W, PAGE_H = 612.0, 792.0
PAGE_START = 752.0  # v3 y-up content top edge (40pt margin below the top edge)
BOTTOM = 10.0  # fitted margin: lines below y=10 are the continuation tail
# 7G-2.1 P0: a break may only land on a page that exists (max page_sizes key).
# The mixed corpus needs THREE target pages (1, 2, 3) — the document must
# declare page 3 or the third break is correctly deferred as out-of-document.
HEIGHTS = {0: PAGE_H, 1: PAGE_H, 2: PAGE_H, 3: PAGE_H}
SIZES = {
    0: [PAGE_W, PAGE_H],
    1: [PAGE_W, PAGE_H],
    2: [PAGE_W, PAGE_H],
    3: [PAGE_W, PAGE_H],
}


def _entry(
    block_id,
    page,
    kind,
    x0,
    y0,
    x1,
    y1,
    payload=None,
    list_items=None,
    toc_entries=None,
    toc_commands=None,
):
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id,
        "page": page,
        "kind": kind,
        "text": "t",
        "translated": "t",
        "src_box": list(box),
        "dst_box": list(box),
        "font_size": 11.0,
        "render_payload": (
            payload if payload is not None else {"kind": kind, "commands": []}
        ),
        "list_items": list_items,
        "toc_entries": toc_entries,
        "toc_commands": toc_commands,
    }


def _list_block(block_id="p0_list", page=0):
    commands = [
        {"kind": "marker", "text": "1.", "x": 60.0, "y": 40.0, "width": 11.0},
        {"kind": "text", "text": "A", "x": 76.0, "y": 40.0, "width": 40.0},
        {"kind": "text", "text": "B", "x": 76.0, "y": 28.0, "width": 40.0},
        {"kind": "text", "text": "C", "x": 76.0, "y": 16.0, "width": 40.0},
        {"kind": "text", "text": "D", "x": 76.0, "y": 4.0, "width": 40.0},
        {"kind": "text", "text": "E", "x": 76.0, "y": -8.0, "width": 40.0},
        {"kind": "text", "text": "F", "x": 76.0, "y": -20.0, "width": 40.0},
    ]
    items = [
        {
            "marker": "1.",
            "marker_x": 60.0,
            "content_x": 76.0,
            "continuation_x": 76.0,
            "continuation": ["B", "C", "D", "E", "F"],
        }
    ]
    return _entry(
        block_id,
        page,
        "list",
        60.0,
        -30.0,
        260.0,
        50.0,
        list_items={"commands": commands, "items": items},
        payload={"kind": "list", "commands": commands},
    )


def _toc_block(block_id="p0_toc", page=0):
    commands = [
        {"kind": "number", "text": "1", "x": 72.0, "y": 40.0, "width": 8.0},
        {"kind": "title", "text": "Intro", "x": 82.0, "y": 40.0, "width": 60.0},
        {"kind": "title", "text": "wrap1", "x": 82.0, "y": 28.0, "width": 60.0},
        {"kind": "title", "text": "wrap2", "x": 82.0, "y": 16.0, "width": 60.0},
        {"kind": "leader", "text": "...", "x": 150.0, "y": 40.0, "width": 320.0},
        {"kind": "page", "text": "42", "x": 500.0, "y": 40.0, "width": 20.0},
        {"kind": "title", "text": "cont1", "x": 82.0, "y": 4.0, "width": 60.0},
        {"kind": "title", "text": "cont2", "x": 82.0, "y": -8.0, "width": 60.0},
        {"kind": "title", "text": "cont3", "x": 82.0, "y": -20.0, "width": 60.0},
    ]
    entries = [
        {
            "title": "Intro",
            "title_x": 82.0,
            "page_x": 500.0,
            "page_number": "42",
            "continuation": [],
        }
    ]
    return _entry(
        block_id,
        page,
        "toc",
        60.0,
        -30.0,
        260.0,
        50.0,
        toc_entries=entries,
        toc_commands={"commands": commands},
        payload={"kind": "toc", "commands": commands},
    )


def _code(block_id="p0_code", page=0):
    return _entry(
        block_id,
        page,
        "code",
        60.0,
        -30.0,
        260.0,
        50.0,
        payload={"kind": "code", "commands": []},
    )


def _flow_tail(block_id="p0_flow", page=0):
    return _entry(
        block_id,
        page,
        "flow",
        60.0,
        -20.0,
        260.0,
        50.0,
        payload={
            "kind": "flow",
            "commands": [
                {
                    "kind": "flow-text",
                    "text": "FLOWTAIL",
                    "x": 60.0,
                    "y": -20.0,
                    "width": 100.0,
                    "line": 0,
                    "is_last": True,
                    "overflow": True,
                }
            ],
        },
    )


def _split_render(plan):
    """Continue-split a plan, render it.

    Returns ``(doc, words, execed_plan, report)``.
    """
    execed, report = execute_continuation_breaks(
        plan, page_sizes=HEIGHTS, page_start_y=PAGE_START, page_bottom_y=BOTTOM
    )
    pdf, _ = render_plan_to_pdf(execed, page_sizes=SIZES, cjk_font=True)
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    words = [extract_words(doc[i]) for i in range(doc.page_count)]
    return doc, words, execed, report


def _all_words(words):
    return [w for page in words for w in page]


# ---------------------------------------------------------------------------
# 1. List continuation — glyph-layer
# ---------------------------------------------------------------------------


class TestListPdfGolden(unittest.TestCase):
    def test_continuation_text_lands_on_page_1(self):
        doc, words, _execed, report = _split_render([_list_block()])
        try:
            p0, p1 = words[0], words[1]
            # kept: marker + A/B/C on page 0
            self.assertEqual(len(words_with_text(p0, "1.")), 1)
            for t in ("A", "B", "C"):
                self.assertTrue(words_with_text(p0, t), f"{t} should stay on page0")
            # tail D/E/F on page 1, none on page 0
            for t in ("D", "E", "F"):
                self.assertTrue(words_with_text(p1, t), f"{t} should continue on page1")
                self.assertFalse(words_with_text(p0, t), f"{t} must not be on page0")
            # no dropped / duplicated line across the doc
            total = {}
            for t in ("A", "B", "C", "D", "E", "F", "1."):
                total[t] = len(words_with_text(_all_words(words), t))
            for t, n in total.items():
                self.assertEqual(n, 1, f"{t} must appear exactly once (got {n})")
        finally:
            doc.close()

    def test_marker_once_and_continuation_x(self):
        doc, words, execed, _report = _split_render([_list_block()])
        try:
            p0, p1 = words[0], words[1]
            # marker exactly once across the doc
            self.assertEqual(_count_label(words, "1."), 1)
            # marker on page 0, at marker_x
            mkw = words_with_text(p0, "1.")
            self.assertEqual(len(mkw), 1)
            self.assertLessEqual(abs(mkw[0]["x0"] - 60.0), 2.0)
            # continuation first word lands exactly at content_x (76) on page 1
            first_cont = words_with_text(p1, "D")
            self.assertLessEqual(abs(first_cont[0]["x0"] - 76.0), 2.0)
            # source_bbox unchanged on both splits
            for e in execute_and_plan([_list_block()])[0]:
                self.assertEqual(e["src_box"], _list_block()["src_box"])
        finally:
            doc.close()

    def test_before_after_overflow_gate(self):
        plan = [_list_block()]
        before = build_page_flow_report(plan, page_sizes=HEIGHTS).summary()[
            "page_overflow_count"
        ]
        execed, _ = execute_continuation_breaks(
            plan, page_sizes=HEIGHTS, page_start_y=PAGE_START, page_bottom_y=BOTTOM
        )
        after = build_page_flow_report(execed, page_sizes=HEIGHTS).summary()[
            "page_overflow_count"
        ]
        self.assertEqual(before, 1)
        self.assertEqual(after, 0)


def _count_label(words, label):
    return len(words_with_text(_all_words(words), label))


def execute_and_plan(plan):
    return execute_continuation_breaks(
        plan, page_sizes=HEIGHTS, page_start_y=PAGE_START, page_bottom_y=BOTTOM
    )


# ---------------------------------------------------------------------------
# 2. TOC continuation — glyph-layer
# ---------------------------------------------------------------------------


class TestTocPdfGolden(unittest.TestCase):
    def test_all_title_lines_survive(self):
        doc, words, _execed, _ = _split_render([_toc_block()])
        try:
            allw = _all_words(words)
            for t in ("Intro", "wrap1", "wrap2", "cont1", "cont2", "cont3"):
                self.assertTrue(words_with_text(allw, t), f"{t} lost across break")
            # wrapped lines on page0, continuation on page1
            p0, p1 = words[0], words[1]
            for t in ("Intro", "wrap1", "wrap2"):
                self.assertTrue(words_with_text(p0, t))
            for t in ("cont1", "cont2", "cont3"):
                self.assertTrue(words_with_text(p1, t))
                self.assertFalse(words_with_text(p0, t))
        finally:
            doc.close()

    def test_page_number_once_at_page_x(self):
        doc, words, _execed, _ = _split_render([_toc_block()])
        try:
            self.assertEqual(_count_label(words, "42"), 1)
            p0 = words[0]
            num = words_with_text(p0, "42")
            self.assertEqual(len(num), 1)
            self.assertLessEqual(abs(num[0]["x0"] - 500.0), 2.0)  # page_x
            # title_x / continuation_x don't drift in the real PDF
            intro = words_with_text(p0, "Intro")
            self.assertLessEqual(abs(intro[0]["x0"] - 82.0), 2.0)
            cont1 = words_with_text(words[1], "cont1")
            self.assertLessEqual(abs(cont1[0]["x0"] - 82.0), 2.0)
        finally:
            doc.close()

    def test_overflow_gate(self):
        plan = [_toc_block()]
        before = build_page_flow_report(plan, page_sizes=HEIGHTS).summary()[
            "page_overflow_count"
        ]
        execed, _ = execute_and_plan(plan)
        after = build_page_flow_report(execed, page_sizes=HEIGHTS).summary()[
            "page_overflow_count"
        ]
        self.assertEqual(before, 1)
        self.assertEqual(after, 0)


# ---------------------------------------------------------------------------
# 3. mixed document — TOC + Flow + List + Code in ONE render
# ---------------------------------------------------------------------------


class TestMixedPdfGolden(unittest.TestCase):
    def test_one_pipeline_no_cross_contamination(self):
        plan = [_toc_block(), _flow_tail(), _list_block(), _code()]
        doc, words, execed, report = _split_render(plan)
        try:
            # every text present exactly once (continuation never duplicated)
            counts = _count_label(words, "42"), _count_label(words, "1.")
            self.assertEqual(counts, (1, 1))  # page number once, marker once
            allw = _all_words(words)
            for t in (
                "Intro",
                "wrap1",
                "wrap2",
                "cont1",
                "cont2",
                "cont3",
                "A",
                "B",
                "C",
                "D",
                "E",
                "F",
                "FLOWTAIL",
                "42",
            ):
                self.assertEqual(len(words_with_text(allw, t)), 1, f"{t} dup/lost")
            # code preserved on page 0 (no continuation, no move)
            code = [e for e in execed if e["kind"] == "code"]
            self.assertTrue(code)
            self.assertEqual(code[0]["page"], 0)
            # plan-layer marker exactly once (never regenerated)
            markers = sum(
                1
                for e in execed
                for c in e["render_payload"]["commands"]
                if c.get("kind") == "marker"
            )
            self.assertEqual(markers, 1)
            # three breakable blocks recovered (toc/flow/list); code preserved
            self.assertEqual(len(report.applied), 3)
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# 4. boundary cases
# ---------------------------------------------------------------------------


class TestBoundary(unittest.TestCase):
    def test_marker_at_page_bottom_not_split(self):
        # whole run below the fitted margin → whole-block move; marker once,
        # on the next page, never duplicated / never re-generated.
        low = _entry(
            "p0_list",
            0,
            "list",
            60.0,
            -30.0,
            260.0,
            50.0,
            list_items={
                "commands": [
                    {
                        "kind": "marker",
                        "text": "1.",
                        "x": 60.0,
                        "y": 8.0,
                        "width": 11.0,
                    },
                    {"kind": "text", "text": "A", "x": 76.0, "y": 8.0, "width": 40.0},
                    {"kind": "text", "text": "B", "x": 76.0, "y": -4.0, "width": 40.0},
                ],
                "items": [],
            },
            payload={
                "kind": "list",
                "commands": [
                    {
                        "kind": "marker",
                        "text": "1.",
                        "x": 60.0,
                        "y": 8.0,
                        "width": 11.0,
                    },
                    {"kind": "text", "text": "A", "x": 76.0, "y": 8.0, "width": 40.0},
                    {"kind": "text", "text": "B", "x": 76.0, "y": -4.0, "width": 40.0},
                ],
            },
        )
        doc, words, execed, report = _split_render([low])
        try:
            self.assertEqual(report.applied[0].mode, "whole_block")
            self.assertEqual(report.applied[0].source_page, 0)
            self.assertEqual(report.applied[0].target_page, 1)
            self.assertEqual(_count_label(words, "1."), 1)  # marker once
            self.assertEqual(len(words_with_text(_all_words(words), "B")), 1)
            # plan-layer: marker never duplicated
            markers = sum(
                1
                for e in execed
                for c in e["render_payload"]["commands"]
                if c.get("kind") == "marker"
            )
            self.assertEqual(markers, 1)
        finally:
            doc.close()

    def test_code_preserved_no_continuation(self):
        _, _, _, report = _split_render([_code()])
        self.assertEqual(report.applied, [])

    def test_kept_block_not_resplit(self):
        # run once → feed the page-0 kept entry back → it fits → no second split
        first, _ = execute_and_plan([_list_block()])
        kept = [e for e in first if e["page"] == 0][0]
        second, r2 = execute_and_plan([kept])
        self.assertEqual(r2.applied, [])
        self.assertEqual(
            len(second[0]["render_payload"]["commands"]),
            len(kept["render_payload"]["commands"]),
        )


# ---------------------------------------------------------------------------
# 5. baseline regression gate
# ---------------------------------------------------------------------------


class TestBaselineMatches(unittest.TestCase):
    def test_split_geometry_and_render_metrics_match_baseline(self):
        baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
        split = baseline["continuation_split"]
        mm = baseline["metrics"]

        # ── plan-layer split geometry ────────────────────────────────────
        _, rl = execute_and_plan([_list_block()])
        lt = rl.applied[0]
        self.assertEqual(lt.fitted_lines, split["list"]["fitted_lines"])
        self.assertEqual(lt.moved_lines, split["list"]["moved_lines"])
        self.assertEqual(lt.target_page, split["list"]["target_page"])
        _, rt = execute_and_plan([_toc_block()])
        tt = rt.applied[0]
        self.assertEqual(tt.moved_lines, split["toc"]["moved_lines"])
        self.assertEqual(tt.target_page, split["toc"]["target_page"])

        # ── glyph-layer render metrics ───────────────────────────────────
        doc, wlist, _, _ = _split_render([_list_block()])
        try:
            self.assertEqual(_count_label(wlist, "1."), split["list"]["marker_count"])
            self.assertLessEqual(
                abs(page_word_x(wlist[1], "D") - split["list"]["content_x"]), 2.0
            )
        finally:
            doc.close()
        doc, wlist, _, _ = _split_render([_toc_block()])
        try:
            self.assertEqual(
                _count_label(wlist, "42"), split["toc"]["page_number_count"]
            )
            self.assertLessEqual(
                abs(page_word_x(wlist[0], "42") - split["toc"]["page_x"]), 2.0
            )
        finally:
            doc.close()

        # ── overflow gate ────────────────────────────────────────────────
        self.assertEqual(mm["overflow_count"], 0)


if __name__ == "__main__":
    unittest.main()
