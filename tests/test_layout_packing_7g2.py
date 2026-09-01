# -*- coding: utf-8 -*-
"""Commit 7G-2 (measurement half) — whitespace / page-packing V2 baseline.

The pure measurement layer that answers the first Adaptive-v2 question —
*"how empty is the settled output today?"* — WITHOUT re-laying-out, moving a
block, or writing a PDF.  It reads only settled :class:`BlockPlacement`
geometry (7F-8a), clusters each page's placements into vertical text columns,
and measures each column's vertical-band utilization:

- ``fill_ratio`` = content_height / page_height (what fraction of the band is
  occupied — NOT "page - last_block_bottom", which the P1 corpus lesson showed
  under-fires on papers where footers sit at the page bottom);
- ``internal_gap`` = vertical gaps *between* stacked reading-order blocks
  (the packing-reclaimable space);
- ``trailing_gap`` = free band below a column's lowest block down to the page
  bottom edge;
- document report aggregates pages / columns / averages.

Locked guarantees:

1. **pure read** — consumes settled ``resolved_bbox`` only, never re-lays-out
   (no ``lay_out`` / ``adaptive`` / wrap / shrink / clip);
2. **geometry-derived columns, no block_id math** — columns come from x-extent
   overlap, reading order from settled bottom edges;
3. **no detector / parser / renderer / translator imports**;
4. **no geometry writes** — measurement only, the plan is never mutated.

These matches mirror the guard discipline used by test_page_flow_7f8a /
test_placement_7g1.
"""

import ast
import json
import unittest
from pathlib import Path

from pdf2zh.semantic.layout.page_flow import BlockPlacement, placements_from_plan
from pdf2zh.semantic.layout.packing import (
    ColumnMetrics,
    column_from_placements,
    column_packing_metrics,
    document_packing_report,
    page_columns,
    page_packing_metrics_for_placements,
)

_HERE = Path(__file__).resolve().parent
_PACKING_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "packing.py"


def _blk(i, page, x0, bottom, x1, top, kind="flow"):
    """One settled placement (v3 y-up: bottom < top, bottom edge = 0)."""
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


def _two_column_page(pg=1):
    """A classic two-column page: left column upper+lower blocks, right column."""
    return [
        _blk(0, pg, 40.0, 600.0, 300.0, 720.0),  # left upper
        _blk(1, pg, 40.0, 500.0, 300.0, 594.0),  # left lower (gap = 6pt)
        _blk(2, pg, 340.0, 600.0, 560.0, 700.0),  # right (separate column)
    ]


# ---------------------------------------------------------------------------
# 1. column clustering — pure geometry
# ---------------------------------------------------------------------------


class TestColumnClustering(unittest.TestCase):
    def test_two_columns_from_overlap(self):
        cols = page_columns(_two_column_page())
        self.assertEqual(len(cols), 2)
        col_l, col_r = sorted(cols, key=lambda c: c.left)
        self.assertAlmostEqual(col_l.left, 40.0)
        self.assertAlmostEqual(col_r.left, 340.0)
        self.assertEqual(len(col_l.placements), 2)  # both left blocks
        self.assertEqual(len(col_r.placements), 1)

    def test_single_column_when_blocks_overlap(self):
        cols = page_columns(
            [
                _blk(0, 1, 60.0, 600.0, 260.0, 720.0),
                _blk(1, 1, 60.0, 500.0, 260.0, 594.0),
            ]
        )
        self.assertEqual(len(cols), 1)

    def test_empty_yields_no_columns(self):
        self.assertEqual(page_columns([]), [])

    def test_column_from_placements_union(self):
        c = column_from_placements(
            [
                _blk(0, 1, 40.0, 500.0, 300.0, 600.0),
                _blk(1, 1, 50.0, 400.0, 310.0, 490.0),
            ]
        )
        self.assertEqual(c.left, 40.0)
        self.assertEqual(c.right, 310.0)
        self.assertEqual(len(c.placements), 2)
        json.dumps(c.to_dict())


# ---------------------------------------------------------------------------
# 2. per-column vertical-band metrics
# ---------------------------------------------------------------------------


class TestColumnMetrics(unittest.TestCase):
    def test_fill_ratio_is_content_band(self):
        # left column: blocks span bottom 500 .. top 720 -> content band 220pt
        col = page_columns(
            _two_column_page(),
        )[0]
        m = column_packing_metrics(col, 792.0)
        self.assertIsInstance(m, ColumnMetrics)
        self.assertAlmostEqual(m.content_height, 220.0, places=1)
        self.assertAlmostEqual(m.fill_ratio, 220.0 / 792.0, places=3)
        self.assertAlmostEqual(m.whitespace_ratio, 1.0 - 220.0 / 792.0, places=3)

    def test_internal_gap_is_between_blocks(self):
        cols = page_columns(_two_column_page())
        left = next(c for c in cols if c.left < 200)
        m = column_packing_metrics(left, 792.0)
        # blocks at 500..594 and 600..720 -> gap 6pt
        self.assertAlmostEqual(m.internal_gap, 6.0, places=1)

    def test_trailing_gap_below_lowest_block(self):
        col = page_columns(_two_column_page())[0]
        m = column_packing_metrics(col, 792.0)
        self.assertAlmostEqual(m.trailing_gap, 500.0, places=1)  # bottom block at 500

    def test_floor_full_page_fill(self):
        col = page_columns([_blk(0, 1, 40.0, 0.0, 600.0, 792.0)])[0]
        m = column_packing_metrics(col, 792.0)
        self.assertEqual(m.fill_ratio, 1.0)
        self.assertEqual(m.whitespace_ratio, 0.0)

    def test_json_safe(self):
        col = page_columns(_two_column_page())[0]
        json.dumps(column_packing_metrics(col, 792.0).to_dict())


# ---------------------------------------------------------------------------
# 3. page-level aggregation
# ---------------------------------------------------------------------------


class TestPagePacking(unittest.TestCase):
    def test_two_column_page(self):
        pm = page_packing_metrics_for_placements(
            _two_column_page(), page=1, page_height=792.0
        )
        self.assertEqual(pm.column_count, 2)
        # left band 220 / 792, right band 100 / 792
        self.assertAlmostEqual(
            pm.avg_fill_ratio, ((220.0 / 792.0) + (100.0 / 792.0)) / 2.0, places=3
        )
        self.assertEqual(pm.total_internal_gap, 6.0)
        pm.to_dict()

    def test_empty_placements(self):
        pm = page_packing_metrics_for_placements([], page=1, page_height=792.0)
        self.assertEqual(pm.column_count, 0)
        self.assertEqual(pm.avg_fill_ratio, 0.0)


# ---------------------------------------------------------------------------
# 4. document-level baseline report
# ---------------------------------------------------------------------------


def _plan_split(placements):
    """Plan entries from placements (dst_box = resolved box)."""
    entries = []
    for p in placements:
        entries.append(
            {
                "block_id": f"p{p.page}_{p.block_index}",
                "page": p.page,
                "kind": p.kind,
                "src_box": list(p.resolved_bbox),
                "dst_box": list(p.resolved_bbox),
                "text": "t",
                "translated": "t",
                "font_size": 11.0,
                "render_payload": {"kind": p.kind, "commands": []},
            }
        )
    return entries


class TestDocumentReport(unittest.TestCase):
    def test_aggregates_pages_and_columns(self):
        plan = _plan_split(
            _two_column_page(1)
            + [
                _blk(0, 2, 40.0, 100.0, 300.0, 500.0),
                _blk(1, 2, 40.0, 40.0, 300.0, 90.0),
            ]
        )
        sizes = {1: 792.0, 2: 612.0}
        report = document_packing_report(plan, page_sizes=sizes)
        self.assertEqual(report["pages"], 2)
        self.assertEqual(report["columns"], 3)  # page1=2, page2=1
        self.assertGreater(report["avg_fill_ratio"], 0.0)
        self.assertLess(report["avg_fill_ratio"], 1.0)
        self.assertEqual(len(report["per_page"]), 2)
        json.dumps(report)

    def test_runs_via_pipeline_placeholder(self):
        # the plan-level entry point consumes the same placements_from_plan
        # read the 7F-8 pipeline uses — smoke that it round-trips the plan
        plan = _plan_split(_two_column_page(3))
        report = document_packing_report(plan, page_sizes={3: 792.0})
        self.assertEqual(report["pages"], 1)
        self.assertEqual(report["columns"], 2)


# ---------------------------------------------------------------------------
# 5. architecture purity — measurement only
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
    """Executable code with docstrings stripped (prose must not trip guards)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _clean(body):
        return [
            n
            for n in body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )
        ]

    tree.body = _clean(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = _clean(node.body)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


class TestPackingArchitecture(unittest.TestCase):
    def test_packing_never_re_lays_out(self):
        src = _code(_PACKING_PATH)
        for banned in (
            "lay_out(",
            "adaptive_layout(",
            "wrap_lines(",
            "shrink_to_fit(",
            "clip_text(",
            "block_id",
        ):
            self.assertNotIn(banned, src, f"packing.py 不得: {banned}")

    def test_packing_never_references_detector_or_renderer(self):
        src = _code(_PACKING_PATH)
        for banned in (
            "list_detector",
            "list_parser",
            "toc_parser",
            "code_detector",
            "style_detector",
            "semantic.renderer",
            "translator",
            "magicpdf",
        ):
            self.assertNotIn(banned, src, f"packing.py 不得引用: {banned}")

    def test_packing_never_derives_geometry_from_level_index(self):
        tree = ast.parse(_code(_PACKING_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(f"packing.py 用 {type(node.op).__name__} 重建几何")

    def test_packing_never_writes_geometry(self):
        src = _code(_PACKING_PATH)
        for banned in (
            '"dst_box"] =',
            '"src_box"] =',
            '"page"] =',
            'entry["',
            "placement.",
        ):
            self.assertNotIn(banned, src, f"packing.py 不得写 plan/geometry: {banned}")

    def test_packing_reads_settled_geometry_only(self):
        src = _code(_PACKING_PATH)
        self.assertIn("placements_from_plan", src)  # settled placements source
        # layout reads settled extents through BlockPlacement's resolved-property
        # accessors (never re-lays-out, never guesses from block_id)
        self.assertIn("blk.right", src)
        self.assertIn("b.bottom", src)
        self.assertIn("b.top", src)


if __name__ == "__main__":
    unittest.main()
