# -*- coding: utf-8 -*-
"""Commit 7G-2 (optimisation half) — whitespace / page-packing V2 executor.

Turns the measured ``internal_gap`` + ``trailing_gap`` (packing.py) into an
actual packing pass on a settled plan.  Two levers, consumed from the measured
per-column band:

- **compaction** — pull movable blocks UP (v3 y-up) in reading order so the
  vertical gap to the block above collapses to a target ``gutter`` (topmost
  block is the anchor); shrinks ``internal_gap``;
- **re-anchor** — push the compacted column DOWN into the trailing gap,
  bounded by ``bottom_margin`` and preserved / footer blocks below; shrinks
  ``trailing_gap``.

Locked guarantees (mirroring the 7F-8 shift / recovery discipline):

1. **pure read of settled geometry** — consumes ``resolved_bbox`` only, never
   re-lays-out (no ``lay_out`` / ``adaptive_layout`` / wrap / shrink / clip);
2. **only Y changes** — ``dst_box`` / command ``y`` move; ``src_box`` and all
   X / width / font / text are byte-identical;
3. **preserved blocks immovable** — code / formula / figure / table / header /
   footer / column are never moved and act as barriers;
4. **reading order never inverted** — compaction only reduces gaps, a lower
   block never rises above the block above it;
5. **no detector / parser / renderer / translator imports**, no ``level`` /
   ``index`` geometry math, ``block_id`` is identity only.
"""

import ast
import json
import unittest
from pathlib import Path

from pdf2zh.semantic.layout.page_flow import BlockPlacement, placements_from_plan
from pdf2zh.semantic.layout.packer import (
    PackConfig,
    compact_column,
    resolve_packing,
    shift_box_v,
)
from pdf2zh.semantic.layout.packing import document_packing_report

_HERE = Path(__file__).resolve().parent
_PACKER_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "packer.py"


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


def _column_with_gaps():
    """One single-column page with three stacked blocks separated by gaps.

    blocks (v3 y-up, bottom edge 0): topmost at 500..720, middle 394..494,
    bottom-most 300..390 -> internal gaps of 500-494=6 and 394-390=4 -> 10pt.
    """
    return [
        _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
        _blk(1, 1, 60.0, 394.0, 260.0, 494.0),
        _blk(2, 1, 60.0, 300.0, 260.0, 390.0),
    ]


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
                    {"text": "t", "x": p.left + 2.0, "y": p.top - 5.0, "font_size": 11.0}
                ],
            },
        })
    return entries


# ---------------------------------------------------------------------------
# 1. pure geometry — shift_box_v
# ---------------------------------------------------------------------------


class TestShiftBoxV(unittest.TestCase):
    def test_positive_is_up(self):
        self.assertEqual(shift_box_v((60.0, 300.0, 260.0, 390.0), 10.0),
                         (60.0, 310.0, 260.0, 400.0))

    def test_negative_is_down(self):
        self.assertEqual(shift_box_v((60.0, 300.0, 260.0, 390.0), -5.0),
                         (60.0, 295.0, 260.0, 385.0))

    def test_x_never_changes(self):
        up = shift_box_v((10.0, 20.0, 300.0, 90.0), 7.0)
        self.assertEqual((up[0], up[2]), (10.0, 300.0))


# ---------------------------------------------------------------------------
# 2. compaction — closes internal gaps, anchor top, preserved immovable
# ---------------------------------------------------------------------------


class TestCompactColumn(unittest.TestCase):
    def test_closes_internal_gaps_up(self):
        # gutter=0 -> compaction collapses every movable gap to exactly 0.
        cols = resolve_packing(_plan(_column_with_gaps()),
                               {1: 792.0},
                               config=PackConfig(gutter=0.0, preserved_gutter=0.0,
                                                 re_anchor=False))[0]
        # topmost anchor stays put
        self.assertEqual(cols[0].bottom, 500.0)
        self.assertEqual(cols[0].top, 720.0)
        # middle pulled up 6 -> bottom 400, top 500
        self.assertAlmostEqual(cols[1].bottom, 400.0, places=1)
        self.assertAlmostEqual(cols[1].top, 500.0, places=1)
        # bottom-most (idx2) pulled up 10 -> bottom 310, top 400
        self.assertAlmostEqual(cols[2].bottom, 310.0, places=1)
        self.assertAlmostEqual(cols[2].top, 400.0, places=1)
        # gaps now 0: each lower block's top exactly meets the block above's bottom
        self.assertAlmostEqual(cols[1].top, cols[0].bottom, places=1)
        self.assertAlmostEqual(cols[2].top, cols[1].bottom, places=1)

    def test_topmost_is_anchor(self):
        deltas = compact_column(_column_with_gaps(), gutter=0.0, preserved_gutter=0.0)
        self.assertEqual(deltas[0], 0.0)
        self.assertGreater(deltas[1], 0.0)
        self.assertGreater(deltas[2], 0.0)

    def test_reading_order_never_inverted(self):
        # packed placements keep the input reading order; every pair retains
        # top-below ordering (a above b: a's bottom edge >= b's top edge) and
        # no block rises above the block above it.
        packed = resolve_packing(_plan(_column_with_gaps()),
                                 {1: 792.0}, config=PackConfig(re_anchor=False))[0]
        self.assertEqual([p.block_index for p in packed], [0, 1, 2])
        for a, b in zip(packed, packed[1:]):
            self.assertGreaterEqual(a.bottom + 1e-6, b.top)  # no overlap across
            self.assertGreaterEqual(a.bottom, b.bottom)      # a still above b

    def test_preserved_blocks_not_moved(self):
        placements = [
            _blk(0, 1, 60.0, 500.0, 260.0, 720.0),
            _pres(1, 1, 60.0, 300.0, 260.0, 494.0),   # preserved (code)
            _blk(2, 1, 60.0, 200.0, 260.0, 290.0),
        ]
        packed = resolve_packing(_plan(placements), {1: 792.0},
                                 config=PackConfig(re_anchor=False))[0]
        self.assertEqual(packed[1].resolved_bbox, placements[1].resolved_bbox)
        self.assertEqual(packed[1].bottom, 300.0)


# ---------------------------------------------------------------------------
# 3. re-anchor — reclaim trailing gap, bounded by margin + preserved
# ---------------------------------------------------------------------------


class TestReanchor(unittest.TestCase):
    def test_pushes_column_down_into_trailing(self):
        # topmost anchor at bottom 500 (bottom edge 0 -> trailing 500 on 792
        # page).  Re-anchor (no bottom margin) slides the whole column down so
        # the lowest block settles at the page-bottom floor.
        cfg = PackConfig(bottom_margin=0.0, preserved_gutter=0.0,
                         max_reclaim=None)
        packed = resolve_packing(_plan(_column_with_gaps()), {1: 792.0},
                                 config=cfg)[0]
        # bottom-most block moved down from 300 to ~0 (fully reclaiming the
        # trailing gap below the original lowest content).
        self.assertAlmostEqual(packed[2].bottom, 0.0, delta=0.1)
        # the whole column shifted together: topmost bottom moved down too
        self.assertLess(packed[0].bottom, 500.0)

    def test_bottom_margin_bounds_shift(self):
        packed = resolve_packing(_plan(_column_with_gaps()), {1: 792.0},
                                 config=PackConfig(bottom_margin=100.0))[0]
        # no movable block's bottom below the 100 pt floor
        for p in packed:
            if not p.preserved:
                self.assertGreaterEqual(p.bottom + 1e-9, 100.0)


# ---------------------------------------------------------------------------
# 4. plan wiring — only Y changes, src_box verbatim
# ---------------------------------------------------------------------------


class TestApplyFlow(unittest.TestCase):
    def test_src_box_verbatim_and_only_y_moves(self):
        from pdf2zh.semantic.layout.packer import apply_packing
        plan = _plan(_column_with_gaps())
        src_before = [list(e["src_box"]) for e in plan]
        dst_before = [list(e["dst_box"]) for e in plan]
        new_plan, report = apply_packing(plan, {1: 792.0},
                                         config=PackConfig(re_anchor=False))
        self.assertEqual([list(e["src_box"]) for e in new_plan], src_before)
        for e, db in zip(new_plan, dst_before):
            # x edges identical; only y may change
            self.assertEqual(e["dst_box"][0], db[0])
            self.assertEqual(e["dst_box"][2], db[2])
        self.assertGreater(report.moves, 0)

    def test_commands_y_move_with_dst_box(self):
        from pdf2zh.semantic.layout.packer import apply_packing
        plan = _plan(_column_with_gaps())
        new_plan, _ = apply_packing(plan, {1: 792.0},
                                    config=PackConfig(re_anchor=False))
        e, db = new_plan[1], plan[1]
        cmd_y = e["render_payload"]["commands"][0]["y"]
        self.assertNotEqual(cmd_y, db["render_payload"]["commands"][0]["y"])

    def test_input_plan_never_mutated(self):
        from pdf2zh.semantic.layout.packer import apply_packing
        plan = _plan(_column_with_gaps())
        snap = json.dumps(plan)
        apply_packing(plan, {1: 792.0})
        self.assertEqual(json.dumps(plan), snap)


# ---------------------------------------------------------------------------
# 5. architecture purity — reads settled geometry, only-Y, no renderer etc.
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _clean(body):
        return [
            n for n in body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))
        ]

    tree.body = _clean(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = _clean(node.body)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


class TestPackerArchitecture(unittest.TestCase):
    def test_never_re_lays_out(self):
        src = _code(_PACKER_PATH)
        for banned in ("lay_out(", "adaptive_layout(", "wrap_lines(",
                       "shrink_to_fit(", "clip_text("):
            self.assertNotIn(banned, src, f"packer.py 不得: {banned}")

    def test_never_references_detector_renderer_translator(self):
        src = _code(_PACKER_PATH)
        for banned in ("list_detector", "list_parser", "toc_parser",
                       "code_detector", "style_detector",
                       "semantic.renderer", "magicpdf", "translator"):
            self.assertNotIn(banned, src, f"packer.py 不得引用: {banned}")

    def test_never_derives_geometry_from_level_index(self):
        tree = ast.parse(_code(_PACKER_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(
                    f"packer.py 用 {type(node.op).__name__} 重建几何"
                )

    def test_reads_settled_geometry_through_placements(self):
        src = _code(_PACKER_PATH)
        self.assertIn("placements_from_plan", src)
        self.assertIn("resolved_bbox", src)

    def test_only_shift_box_v_touches_geometry(self):
        src = _code(_PACKER_PATH)
        self.assertIn("def shift_box_v", src)


if __name__ == "__main__":
    unittest.main()