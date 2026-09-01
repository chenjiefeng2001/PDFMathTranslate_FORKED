# -*- coding: utf-8 -*-
"""Commit 7G-2.2 — renderer-geometry parity: conservative occupied draw-extent.

The V2 render gate (§11.4) showed the packer and the renderer live in **two
geometry worlds** for a block that falls back to the legacy ``_insert_text_wrapped``
path (empty ``render_payload.commands``):

    packer sees   dst_box              →  treats it as the block's height
    renderer draws  re-wraps at 1.4·font_size against box width

A heading / TOC / short-plan box sized from SOURCE geometry can re-wrap taller
than ``dst_box``, so its drawn glyphs spill below the box bottom by up to ~a
line; compaction closing the *bbox* gap to ``gutter`` then leaves the *words*
overlapping the neighbour.  ``_glyph_excess`` (7G-2.1) cannot see it — there is
no command geometry to read.

7G-2.2 closes the gap with a **conservative occupied bottom per block**
(方案 A, no re-layout):

- command block → read the lowest baseline's real glyph bottom;
- command-less block → estimate wrapped line count from ``text`` +
  ``font_size`` + ``dst_box.width`` (pure metric read, the renderer's own
  token-wrap rule), derive ``occupied_height = max(dst_box.height,
  lines * 1.4 * font_size)`` and pad the spill below the box.

Locked guarantees (mirroring 7G-2/7G-2.1):

1. **pure estimation** — the estimator never lays out, never draws, never
   changes X / font / text / commands; every input is a settled field;
2. **occupancy only** — the spill only makes the packer's gap math clear the
   drawn bottom; a block is never mutated by estimation;
3. **zero spill keeps bbox behaviour** — a block whose wrapped lines fit its
   box (or any command block whose last baseline sits inside) has spill 0 and
   compaction/re-anchor are byte-identical to 7G-2.1;
4. **no renderer / pymupdf dependency** — the metric model is the typographer's
   char-advance model, deterministic and CJK-safe;
5. **no overlap introduced** — a lower block is never pulled above the upper
   block's OCCUPIED bottom (drawn extent), so packing_introduced_overlap → 0
   for this class.
"""

import unittest

from pdf2zh.semantic.layout.packer import (
    _entry_occupied_bottom_spill,
    _estimate_wrapped_lines,
    _occupied_bottom_spill_by_key,
    compact_column,
)
from pdf2zh.semantic.layout.page_flow import BlockPlacement


def _blk(i, page, x0, bottom, x1, top, kind="flow"):
    box = (float(x0), float(bottom), float(x1), float(top))
    return BlockPlacement(
        block_index=i,
        page=page,
        kind=kind,
        bbox=box,
        resolved_bbox=box,
        height=top - bottom,
        preserved=False,
        has_continuation=False,
    )


def _entry(box, text="t", font_size=11.0, commands=None):
    return {
        "block_id": "p1_0",
        "page": 1,
        "kind": "flow",
        "text": text,
        "translated": text,
        "src_box": list(box),
        "dst_box": list(box),
        "font_size": font_size,
        "render_payload": {
            "kind": "flow",
            "commands": [] if commands is None else commands,
        },
    }


# ---------------------------------------------------------------------------
# 1. wrapped line estimator — pure metric read, mirrors the renderer rule
# ---------------------------------------------------------------------------


class TestWrapLineEstimate(unittest.TestCase):
    def test_single_line_when_it_fits(self):
        # a short latin string in a wide box wraps to one line
        self.assertEqual(_estimate_wrapped_lines("hello world", 12.0, 300.0), 1)

    def test_more_lines_for_more_text(self):
        words = "The quick brown fox jumps over the lazy dog"
        wide = _estimate_wrapped_lines(words, 12.0, 400.0)
        narrow = _estimate_wrapped_lines(words, 12.0, 60.0)
        self.assertGreaterEqual(wide, 1)
        self.assertGreater(narrow, wide)

    def test_cjk_fullwidth_counts_double(self):
        # CJK glyphs are ~1em (double a 0.5em latin char) → fewer per line
        cjk = "".join("\u4e2d" for _ in range(40))  # 40 fullwidth glyphs
        latin = "m" * 80  # 80 half-width glyphs
        # same *advance* budget → same line count
        self.assertEqual(
            _estimate_wrapped_lines(cjk, 12.0, 240.0),
            _estimate_wrapped_lines(latin, 12.0, 240.0),
        )

    def test_empty_text_degenerate_one_line(self):
        self.assertEqual(_estimate_wrapped_lines("", 12.0, 100.0), 1)


# ---------------------------------------------------------------------------
# 2. occupied bottom spill — the conservative draw-extent
# ---------------------------------------------------------------------------


class TestOccupiedBottomSpill(unittest.TestCase):
    def test_command_block_last_baseline_inside_box_is_zero(self):
        # single baseline at top-9, descent below it stays INSIDE the box
        box = (60.0, 500.0, 260.0, 720.0)  # top=720, bottom=500, h=220
        e = _entry(
            box, commands=[{"text": "t", "x": 62.0, "y": 711.0, "font_size": 11.0}]
        )
        self.assertEqual(_entry_occupied_bottom_spill(e), 0.0)

    def test_command_block_spill_when_baseline_below_box(self):
        # a command baseline below the declaered bottom → glyph descent spills
        box = (60.0, 500.0, 260.0, 720.0)
        e = _entry(
            box, commands=[{"text": "t", "x": 62.0, "y": 495.0, "font_size": 11.0}]
        )
        # glyph bottom = 495 - 0.25*11 = 492.25, 500-492.25 = 7.75 below box
        self.assertAlmostEqual(_entry_occupied_bottom_spill(e), 7.75, places=1)

    def test_commandless_wrap_spill_when_box_under_declares(self):
        # tiny-width box re-wraps into several 1.4·fs lines → drawn extent
        # exceeds the declared height → positive spill (方案 A)
        e = _entry(
            (60.0, 680.0, 80.0, 720.0), text="alpha beta gamma delta", font_size=11.0
        )
        spill = _entry_occupied_bottom_spill(e)
        self.assertGreater(spill, 0.0)

    def test_commandless_fitting_text_is_zero(self):
        # a large roomy box whose wrapped estimate fits → no spill (bbox behaviour)
        e = _entry((60.0, 100.0, 560.0, 720.0), text="hello", font_size=11.0)
        self.assertEqual(_entry_occupied_bottom_spill(e), 0.0)

    def test_spill_map_keys_by_page_block_index(self):
        plan = [
            _entry(
                (60.0, 680.0, 80.0, 720.0),
                text="alpha beta gamma delta",
                font_size=11.0,
            ),
            _entry((60.0, 100.0, 560.0, 720.0), text="hello", font_size=11.0),
        ]
        spills = _occupied_bottom_spill_by_key(plan)
        self.assertIn((1, 0), spills)
        self.assertNotIn((1, 1), spills)


# ---------------------------------------------------------------------------
# 3. compaction parity — pulls are bounded by the upper block's OCCUPIED bottom
# ---------------------------------------------------------------------------


class TestCompactParity(unittest.TestCase):
    def test_compaction_respects_upper_occupied_bottom(self):
        # column with internal gaps; the TOP block A's drawn glyphs dip 10pt
        # below its box bottom (500 -> occupied 490).  With gutter=0:
        #   - no occ: B pulled up 6 (bboxes touch), words overlap A's spill
        #   - with occ: B must clear A's OCCUPIED bottom 490, so B (top 494)
        #     is NOT pulled up at all — words just touch.
        a, b, c = (
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 394.0, 260.0, 494.0),
            _blk(2, 1, 60.0, 300.0, 260.0, 390.0),
        )
        occ = {id(a): 10.0}

        plain = compact_column([a, b, c], gutter=0.0, preserved_gutter=0.0)
        parit = compact_column(
            [a, b, c], gutter=0.0, preserved_gutter=0.0, occ_by_id=occ
        )
        # without parity B was pulled up 6 (boom — overlaps A's spill)
        self.assertAlmostEqual(plain[1], 6.0, places=1)
        # with parity B stays put; C packs against B's (unspilled) bottom
        self.assertAlmostEqual(parit[1], 0.0, places=1)
        self.assertAlmostEqual(parit[2], 4.0, places=1)

    def test_cross_column_barrier_uses_occupied_bottom(self):
        # a barrier in another column whose glyphs spill 10 below its box
        # bottom must bound the pull just like a same-column neighbour.
        a = _blk(0, 1, 60.0, 500.0, 260.0, 720.0)
        b = _blk(1, 1, 60.0, 394.0, 260.0, 494.0)
        barrier = _blk(99, 1, 100.0, 460.0, 200.0, 470.0)
        occ = {id(barrier): 10.0}
        deltas = compact_column(
            [a, b],
            gutter=0.0,
            preserved_gutter=0.0,
            other_barriers=[barrier],
            occ_by_id=occ,
        )
        # B (top 494, below barrier's occupied bottom 450) is not pulled past it
        self.assertGreaterEqual(deltas[1], 0.0)


if __name__ == "__main__":
    unittest.main()
