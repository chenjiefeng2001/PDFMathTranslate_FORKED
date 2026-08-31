# -*- coding: utf-8 -*-
"""Commit 7G-2.1 — P0 fixes for the V2 render gate (doc report §10.4/10.5).

The V2 render gate caught two real production defects on the 44-doc corpus:

1. **8e out-of-document overflow** — ``NEXT_PAGE`` could push a block past the
   document's last real page (lol.pdf: 170 → 387 on a 382-page book; 280
   words dropped from the render).  The executor may propose a page change,
   but must never generate page / geometry the renderer cannot carry:
   ``next_free_page`` is now bounded by ``max_page`` (from ``page_sizes``),
   and a break with no free real page is recorded deferred + unresolved
   instead of being placed on a phantom page.

2. **packer word-level overlap** — ``column_reanchor`` floored the column's
   descent only against ``bottom_margin`` and *preserved* blocks, so it could
   push a compacted column DOWN onto a movable neighbour (page number,
   footer paragraph, adjacent column) — the block-level collision gate stayed
   clean (4,459→3,943) while the words layer showed 5,853 new overlaps on
   42/44 docs.  ``column_reanchor`` now takes the page's OTHER columns as
   ``other_barriers`` and refuses to descend onto any horizontally
   overlapping neighbour (movable or preserved).

Locked guarantees:

1. **no block past the last real page** — ``next_free_page`` returns ``None``
   when the monotonic scan passes ``max_page``; both 8e executors defer the
   block (applied empty, unresolved non-empty) rather than moving it;
2. **``last_page_index`` is the document's page ceiling** — max numeric key of
   ``page_sizes``, ``None`` without a size map;
3. **re-anchor never descends onto a movable neighbour** — a compacted column
   above another column's block (or a page number) stays above it with the
   configured gutter clearance;
4. **side-by-side columns still reclaim** — a fully non-overlapping neighbour
   (two-column gutter) does NOT floor the column, so re-anchor keeps working;
5. **only Y changes, ``src_box`` verbatim** — the guards never touch X / font /
   text, matching the 7G-2 discipline.
"""

import unittest

from pdf2zh.semantic.layout.page_break import last_page_index, next_free_page
from pdf2zh.semantic.layout.page_break_continuation import (
    execute_continuation_breaks,
)
from pdf2zh.semantic.layout.page_break_executor import execute_page_breaks
from pdf2zh.semantic.layout.page_flow import BlockPlacement, placements_from_plan
from pdf2zh.semantic.layout.packer import (
    PackConfig,
    apply_packing,
    column_reanchor,
)


def _blk(i, page, x0, bottom, x1, top, kind="flow"):
    box = (float(x0), float(bottom), float(x1), float(top))
    return BlockPlacement(
        block_index=i, page=page, kind=kind, bbox=box, resolved_bbox=box,
        height=top - bottom, preserved=False, has_continuation=False,
    )


def _pres(i, page, x0, bottom, x1, top, kind="code"):
    b = _blk(i, page, x0, bottom, x1, top, kind=kind)
    return BlockPlacement(
        block_index=i, page=page, kind=kind, bbox=b.bbox, resolved_bbox=b.bbox,
        height=b.height, preserved=True, has_continuation=False,
    )


def _plan(placements, page_height=792.0):
    entries = []
    for p in placements:
        entries.append({
            "block_id": f"p{p.page}_{p.block_index}",
            "page": p.page,
            "kind": p.kind,
            "src_box": list(p.resolved_bbox),
            "dst_box": list(p.resolved_bbox),
            "text": "t", "translated": "t",
            "font_size": 11.0,
            "render_payload": {
                "kind": p.kind,
                "commands": [
                    {"text": "t", "x": p.left + 2.0, "y": p.top - 5.0,
                     "font_size": 11.0}
                ],
            },
        })
    return entries


# ---------------------------------------------------------------------------
# 1. next_free_page / last_page_index — bounded to real pages (8e P0)
# ---------------------------------------------------------------------------


class TestNextFreePageBounded(unittest.TestCase):
    def test_bounded_by_max_page_returns_real_page(self):
        # a break from 170 lands on the first free page still inside the doc
        self.assertEqual(next_free_page(170, {171}, max_page=382), 172)

    def test_scan_past_max_page_returns_none(self):
        # every page after 170 up to the last real page is taken → no real
        # page exists → None (never a phantom page)
        occupied = set(range(171, 383))  # 171..382 all taken
        self.assertIsNone(next_free_page(170, occupied, max_page=382))

    def test_unbounded_without_max_page_preserves_old_chain(self):
        # historical callers without a size map keep the unbounded chain
        self.assertEqual(next_free_page(0, {0}), 1)

    def test_last_page_index_is_max_numeric_key(self):
        self.assertEqual(last_page_index({0: 792.0, 1: 792.0, 382: 792.0}), 382)

    def test_last_page_index_none_without_sizes(self):
        self.assertIsNone(last_page_index(None))
        self.assertIsNone(last_page_index({}))


# ---------------------------------------------------------------------------
# 2. 8e continuation executor — out-of-document break is deferred, not moved
# ---------------------------------------------------------------------------


class TestContinuationNoPhantomPage(unittest.TestCase):
    def test_block_on_last_page_is_deferred_not_moved(self):
        # a list tail overflowing on the LAST real page: no free page exists
        # → deferred + unresolved, entry stays on its page (words preserved).
        cmds = [
            {"kind": "marker", "text": "1.", "x": 60.0, "y": 40.0, "width": 11.0},
            {"kind": "text", "text": "A", "x": 76.0, "y": 40.0, "width": 40.0},
            {"kind": "text", "text": "B", "x": 76.0, "y": -8.0, "width": 40.0},
        ]
        entry = {
            "block_id": "p2_0", "page": 2, "kind": "list",
            "text": "t", "translated": "t",
            "src_box": [60.0, -30.0, 260.0, 50.0],
            "dst_box": [60.0, -30.0, 260.0, 50.0],
            "font_size": 11.0,
            "render_payload": {"kind": "list", "commands": list(cmds)},
            "list_items": {"commands": list(cmds),
                           "items": [{"marker": "1.", "marker_x": 60.0,
                                      "content_x": 76.0, "continuation_x": 76.0,
                                      "continuation": ["B"]}]},
        }
        # document has exactly 3 pages (0,1,2) — page 2 IS the last page
        new_plan, report = execute_continuation_breaks(
            [entry], page_sizes={0: 792.0, 1: 792.0, 2: 792.0},
            page_start_y=792.0, page_bottom_y=10.0)
        self.assertEqual(report.applied, [])           # nothing moved
        self.assertEqual(len(report.unresolved), 1)    # surfaced, not dropped
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(new_plan[0]["page"], 2)       # stays in-document
        self.assertEqual(new_plan[0]["dst_box"][1:4:2], [-30.0, 50.0])  # verbatim
        self.assertEqual(report.deferred[0].reason, "no_page")


class TestExecutorNoPhantomPage(unittest.TestCase):
    def test_whole_block_on_last_page_is_deferred(self):
        entry = {
            "block_id": "p1_0", "page": 1, "kind": "flow",
            "text": "t", "translated": "t",
            "src_box": [60.0, -20.0, 260.0, 50.0],
            "dst_box": [60.0, -20.0, 260.0, 50.0],
            "font_size": 11.0,
            "render_payload": {"kind": "flow", "commands": [
                {"kind": "flow-text", "text": "t", "x": 60.0, "y": -20.0,
                 "width": 100.0, "line": 0, "is_last": True, "overflow": True}]},
        }
        new_plan, report = execute_page_breaks(
            [entry], page_sizes={0: 792.0, 1: 792.0}, page_start_y=792.0)
        self.assertEqual(report.applied, [])
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(new_plan[0]["page"], 1)       # never past last page
        # the deferred record pins the block to its OWN page (the phantom
        # page+1 is never created)
        self.assertEqual(report.deferred[0].target_page, 1)


# ---------------------------------------------------------------------------
# 3. packer — neighbour-aware re-anchor floor (word-level overlap P0)
# ---------------------------------------------------------------------------


class TestReanchorNeighbourAware(unittest.TestCase):
    def test_reanchor_does_not_descend_onto_movable_neighbour(self):
        # page 792: a compacted column at the top, and a page-number-like
        # movable paragraph BELOW it, horizontally overlapping.  Before 7G-2.1
        # the re-anchor floor only knew preserved blocks, so the column would
        # descend onto the page number (the V1 gate's word overlap).  Now the
        # movable neighbour is a hard floor: the column stops at
        # neighbour.top + gutter.
        column = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 394.0, 260.0, 494.0),
            _blk(2, 1, 60.0, 300.0, 260.0, 390.0),
        ]
        page_number = _blk(99, 1, 200.0, 100.0, 330.0, 130.0)  # movable para
        down = column_reanchor(
            column, bottom_margin=36.0, preserved_gutter=6.0, gutter=2.0,
            other_barriers=[page_number])
        # column descends only until block[2].bottom == 130 + 2 = 132
        # (300 -> 132 = -168), never onto the page number
        self.assertAlmostEqual(down, -168.0, delta=0.1)

    def test_reanchor_respects_preserved_floor_without_other_columns(self):
        # preserved barrier still floors when no other columns exist (7G-2
        # behaviour unchanged)
        column = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _pres(1, 1, 60.0, 300.0, 260.0, 494.0),   # preserved (code)
            _blk(2, 1, 60.0, 200.0, 260.0, 290.0),
        ]
        down = column_reanchor(
            column, bottom_margin=36.0, preserved_gutter=6.0, gutter=2.0)
        # block[2] descends until 200 -> 494+6 = 500?  No: preserved top 494 is
        # ABOVE block[2] (y-up); the floor is bottom_margin 36 here.
        self.assertLessEqual(down, 0.0)
        self.assertGreaterEqual(down, -200.0 + 36.0)

    def test_side_by_side_neighbour_does_not_floor(self):
        # a fully side-by-side column (x-disjoint) has no word-overlap risk and
        # must NOT floor the reclaim — re-anchor still reaches bottom_margin.
        column = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 300.0, 260.0, 390.0),
        ]
        other_column = _blk(50, 1, 400.0, 200.0, 560.0, 210.0)  # far right
        down = column_reanchor(
            column, bottom_margin=36.0, preserved_gutter=6.0, gutter=2.0,
            other_barriers=[other_column])
        # lowest block 300 -> 36 = -264; the disjoint neighbour does not bind
        self.assertAlmostEqual(down, -264.0, delta=0.1)


# ---------------------------------------------------------------------------
# 4. packer — glyph-excess guard (word-level overlap P0, 7G-2.1)
# ---------------------------------------------------------------------------


def _glyph_top_of(entry):
    """Real glyph top of a plan entry (mirrors packer's ascent model)."""
    cmds = entry["render_payload"]["commands"]
    fs = entry["font_size"]
    return max(c["y"] for c in cmds) + 0.8 * fs


class TestGlyphExcessGuard(unittest.TestCase):
    def test_compaction_pull_reduced_by_glyph_excess(self):
        # command lines sit 5pt below dst top but ascent 0.8*11 = 8.8 pokes
        # 3.8pt ABOVE the box.  Bbox compaction would pull 6/10pt; the 7G-2.1
        # guard subtracts the excess so the real glyphs stop exactly at the
        # block above's resolved bottom (words never overlap).
        from pdf2zh.semantic.layout.packer import compact_column
        col = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 394.0, 260.0, 494.0),
            _blk(2, 1, 60.0, 300.0, 260.0, 390.0),
        ]
        deltas = compact_column(col, gutter=0.0, preserved_gutter=0.0,
                                glyph_excess=[3.8, 3.8, 3.8])
        self.assertAlmostEqual(deltas[1], 2.2, places=1)
        self.assertAlmostEqual(deltas[2], 2.4, places=1)

    def test_no_excess_keeps_bbox_pull(self):
        # well-formed blocks (excess 0) keep the exact 7G-2 bbox pulls
        from pdf2zh.semantic.layout.packer import compact_column
        col = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 394.0, 260.0, 494.0),
            _blk(2, 1, 60.0, 300.0, 260.0, 390.0),
        ]
        deltas = compact_column(col, gutter=0.0, preserved_gutter=0.0,
                                glyph_excess=[0.0, 0.0, 0.0])
        self.assertAlmostEqual(deltas[1], 6.0, places=1)
        self.assertAlmostEqual(deltas[2], 10.0, places=1)

    def test_reanchor_floored_by_barrier_glyph_top(self):
        # a below neighbour whose glyphs poke 10pt above its dst box floors
        # the descent at q.top + 10 + gutter — never onto its words
        column = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 300.0, 260.0, 390.0),
        ]
        neighbour = _blk(9, 1, 200.0, 100.0, 330.0, 130.0)
        down = column_reanchor(
            column, bottom_margin=36.0, preserved_gutter=6.0, gutter=2.0,
            other_barriers=[neighbour], barrier_excess={id(neighbour): 10.0})
        # lowest block stops at 130 + 10 + 2 = 142 (300 -> 142 = -158)
        self.assertAlmostEqual(down, -158.0, delta=0.1)

    def test_apply_packing_word_level_clean_with_excess(self):
        # integration on the defect signature (_plan y = top - 5 -> excess
        # 3.8 on every block): after packing, each block's real glyph top
        # stays at-or-below the block above's resolved bottom.
        col = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 394.0, 260.0, 494.0),
            _blk(2, 1, 60.0, 300.0, 260.0, 390.0),
        ]
        plan = _plan(col)
        new_plan, _ = apply_packing(
            plan, {1: 792.0},
            config=PackConfig(compact=True, gutter=0.0, preserved_gutter=0.0,
                              re_anchor=False))
        placements = placements_from_plan(new_plan)
        ordered = sorted(
            enumerate(new_plan), key=lambda ie: -placements[ie[0]].top)
        for (i, e), (j, f) in zip(ordered, ordered[1:]):
            self.assertLessEqual(
                _glyph_top_of(f) + 1e-6, placements[i].bottom + 1e-6)


class TestApplyPackingNeighbourAware(unittest.TestCase):
    def test_packed_plan_never_overlaps_neighbour(self):
        # integration: resolve + apply with a page-number-like movable neighbour
        # below the column; after packing no column block's bottom crosses the
        # neighbour's top.
        column = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _blk(1, 1, 60.0, 394.0, 260.0, 494.0),
            _blk(2, 1, 60.0, 300.0, 260.0, 390.0),
        ]
        page_number = _blk(3, 1, 200.0, 100.0, 330.0, 130.0)
        plan = _plan(column + [page_number])
        new_plan, _ = apply_packing(
            plan, {1: 792.0},
            config=PackConfig(compact=True, gutter=2.0, preserved_gutter=6.0,
                              re_anchor=True, bottom_margin=36.0))
        placements = placements_from_plan(new_plan)
        col_blocks = [p for p in placements if p.block_index in (0, 1, 2)]
        neighbour = [p for p in placements if p.block_index == 3][0]
        for p in col_blocks:
            self.assertGreaterEqual(p.bottom + 1e-6, neighbour.top + 2.0 - 0.2)
        # only Y changed: src_box verbatim everywhere
        for e, orig in zip(new_plan, plan):
            self.assertEqual(e["src_box"], orig["src_box"])


if __name__ == "__main__":
    unittest.main()
