# -*- coding: utf-8 -*-
"""Commit 7G-1 — Whitespace-aware Adaptive Placement.

The first step past pure recovery: decide *before* placement whether a block
belongs on the current page or the next page, consuming only already-settled
geometry — a :class:`LayoutResult` (settled height) and a :class:`PageFlowReport`
(settled placements / resolved extents).  Locked guarantees (the 7G-1 DoD):

1. **settled inputs only** — block height is read off the LayoutResult
   (``line_count * font_size``), remaining page space off the settled
   placements' ``resolved_bbox``; nothing is re-laid-out, re-wrapped,
   re-shrunk or re-clipped;
2. **decision matrix** — fits → CURRENT_PAGE; small overrun + reasonably full
   page → CURRENT_PAGE (minor_overflow); large overrun / mostly-empty page →
   NEXT_PAGE; preserved kind → CURRENT_PAGE (never moves); no page height →
   UNDECIDED;
3. **decision-only** — a decision is a record (target + score + reason); no
   geometry is ever written, no block ever moves, the plan / report inputs
   are never mutated;
4. **identity discipline (7F-9.1)** — nothing derives geometry from the
   ``block_id`` string.

Architecture guards (same discipline as 7F-8): placement.py is pure decision
— no detector / parser / renderer / translator, no lay_out / adaptive_layout /
wrap/shrink/clip, no level/index math, no adaptive* executor, no plan writes.
"""

import ast
import copy
import json
import unittest
from pathlib import Path

from pdf2zh.semantic.layout.overflow import LayoutResult, lay_out
from pdf2zh.semantic.layout.page_flow import (
    build_page_flow_report,
    placements_from_plan,
)
from pdf2zh.semantic.layout.placement import (
    PlacementDecision,
    PlacementPolicy,
    PlacementScore,
    PlacementTarget,
    decide_from_settled,
    decide_placement,
    estimate_block_height,
    remaining_space_for_page,
    score_fit,
)

_HERE = Path(__file__).resolve().parent
_PLACEMENT_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "placement.py"


def _entry(block_id, page, kind, x0, y0, x1, y1, payload=None):
    """One settled render-plan entry (v3 y-up boxes: y0 bottom, y1 top)."""
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id, "page": page, "kind": kind,
        "text": "t", "translated": "t",
        "src_box": list(box), "dst_box": list(box),
        "font_size": 11.0,
        "render_payload": payload if payload is not None
        else {"kind": kind, "commands": []},
    }


def _flow(block_id, page, x0, y0, x1, y1):
    return _entry(block_id, page, "flow", x0, y0, x1, y1)


def _report(plan, page_sizes=None):
    return build_page_flow_report(plan, page_sizes=page_sizes or {})


# ---------------------------------------------------------------------------
# 1. settled inputs — LayoutResult height + page remaining space
# ---------------------------------------------------------------------------


class TestSettledInputs(unittest.TestCase):
    def test_estimate_block_height_from_settled_result(self):
        r = LayoutResult(text="x", lines=["a", "b", "c"], font_size=10.0)
        self.assertEqual(estimate_block_height(r), (3, 30.0))  # 3 lines * 10pt

    def test_estimate_block_height_empty(self):
        self.assertEqual(estimate_block_height(LayoutResult(text="", lines=[])),
                         (0, 0.0))
        # text present but no wrapped lines -> single line
        self.assertEqual(estimate_block_height(LayoutResult(text="x", lines=[],
                                                            font_size=12.0)),
                         (1, 12.0))

    def test_estimate_matches_lay_out_overflow_check(self):
        # the canonical estimate lay_out uses: len(lines) * fs > avail_height
        r = lay_out(type("P", (), {"kind": "flow", "text": "aaa bbb ccc",
                                   "max_width": 20.0, "max_height": 10.0,
                                   "origin": (0.0, 0.0)})(),
                    measure=lambda s, fs: float(len(s)) * 3.0,
                    font_size=10.0)
        lines, total = estimate_block_height(r)
        self.assertGreater(total, 10.0)
        self.assertTrue(r.overflow or total > 10.0)

    def test_remaining_space_empty_page_is_full_height(self):
        self.assertEqual(remaining_space_for_page(_report([]), 1, 792.0), 792.0)

    def test_remaining_space_below_last_settled_block(self):
        # a block whose resolved bottom is at 120 leaves 120pt below it
        plan = [_flow("p1_0", 1, 60.0, 120.0, 260.0, 160.0)]
        self.assertEqual(remaining_space_for_page(_report(plan), 1, 792.0), 120.0)

    def test_remaining_space_overlapping_bottom_is_zero(self):
        plan = [_flow("p1_0", 1, 60.0, -20.0, 260.0, 50.0)]
        self.assertEqual(remaining_space_for_page(_report(plan), 1, 792.0), 0.0)

    def test_remaining_space_unknown_page_height(self):
        plan = [_flow("p1_0", 1, 60.0, 120.0, 260.0, 160.0)]
        self.assertEqual(remaining_space_for_page(_report(plan), 1, 0.0), 0.0)

    def test_remaining_space_ignores_other_pages(self):
        plan = [_flow("p1_0", 1, 60.0, 120.0, 260.0, 160.0)]
        self.assertEqual(remaining_space_for_page(_report(plan), 2, 792.0), 792.0)


# ---------------------------------------------------------------------------
# 2. pure score — fits / shortfall / fill_ratio
# ---------------------------------------------------------------------------


class TestPlacementScore(unittest.TestCase):
    def test_score_fits_and_shortfall(self):
        s = score_fit(140.0, 120.0, 792.0, line_count=7)
        self.assertIsInstance(s, PlacementScore)
        self.assertFalse(s.fits)
        self.assertEqual(s.shortfall, 20.0)
        self.assertEqual(s.line_count, 7)
        # touching is a fit
        self.assertTrue(score_fit(120.0, 120.0, 792.0).fits)

    def test_score_fill_ratio_caps(self):
        self.assertEqual(score_fit(100.0, 692.0, 792.0).fill_ratio, 0.126)
        self.assertEqual(score_fit(900.0, 0.0, 792.0).fill_ratio, 1.0)
        self.assertEqual(score_fit(100.0, 100.0, 0.0).fill_ratio, 0.0)

    def test_score_json_safe(self):
        d = score_fit(140.0, 120.0, 792.0).to_dict()
        self.assertEqual(set(d),
                         {"needed", "available", "fits", "shortfall",
                          "fill_ratio", "line_count"})
        json.dumps(d)


# ---------------------------------------------------------------------------
# 3. the decision matrix — the 7G-1 contract
# ---------------------------------------------------------------------------


class TestDecisionMatrix(unittest.TestCase):
    def test_fits_is_current_page(self):
        d = decide_placement(100.0, 120.0, 792.0)
        self.assertIsInstance(d, PlacementDecision)
        self.assertEqual(d.target, PlacementTarget.CURRENT_PAGE)
        self.assertEqual(d.reason, "fits")
        self.assertTrue(d.score.fits)

    def test_no_page_height_is_undecided(self):
        d = decide_placement(100.0, 100.0, 0.0)
        self.assertEqual(d.target, PlacementTarget.UNDECIDED)
        self.assertEqual(d.reason, "no_page_height")

    def test_preserved_kind_never_moves(self):
        # code needs 900pt on a page with 10 left — still CURRENT_PAGE
        d = decide_placement(900.0, 10.0, 792.0, kind="code")
        self.assertEqual(d.target, PlacementTarget.CURRENT_PAGE)
        self.assertEqual(d.reason, "preserved")

    def test_small_overrun_full_page_is_minor_overflow_current(self):
        # remaining 120, block needs 140 (overrun 20 <= 24pt) and the page is
        # ~85% full -> worth keeping: 7G-1's "YES -> 当前页"
        d = decide_placement(140.0, 120.0, 792.0)
        self.assertEqual(d.target, PlacementTarget.CURRENT_PAGE)
        self.assertEqual(d.reason, "minor_overflow")
        self.assertEqual(d.score.shortfall, 20.0)

    def test_overrun_bounded_by_ratio_is_minor_overflow(self):
        # remaining 200, block needs 230 (overrun 30 > 24pt but <= 20% of 200)
        d = decide_placement(230.0, 200.0, 792.0)
        self.assertEqual(d.target, PlacementTarget.CURRENT_PAGE)
        self.assertEqual(d.reason, "minor_overflow")

    def test_large_overrun_is_next_page(self):
        # the user's example: remaining 120pt, block needs 150pt -> overrun 30
        # > 24pt and > 20% of 120 -> NO -> NEXT_PAGE
        d = decide_placement(150.0, 120.0, 792.0)
        self.assertEqual(d.target, PlacementTarget.NEXT_PAGE)
        self.assertEqual(d.reason, "page_overflow")

    def test_overrun_on_mostly_empty_page_is_next_page(self):
        # one small block near the top leaves 700pt free; the 750pt block does
        # not fit and the page is only ~12% full -> a fresh page is better
        d = decide_placement(750.0, 700.0, 792.0)
        self.assertEqual(d.target, PlacementTarget.NEXT_PAGE)
        self.assertEqual(d.reason, "page_overflow")

    def test_policy_knobs_are_respected(self):
        # default policy says NEXT_PAGE for the 150-vs-120 case; a generous
        # policy accepts it as minor overflow
        strict = PlacementPolicy(max_shortfall_pt=10.0, max_overflow_ratio=0.1)
        d = decide_placement(150.0, 120.0, 792.0, policy=strict)
        self.assertEqual(d.target, PlacementTarget.NEXT_PAGE)
        generous = PlacementPolicy(max_shortfall_pt=30.0)
        d = decide_placement(150.0, 120.0, 792.0, policy=generous)
        self.assertEqual(d.target, PlacementTarget.CURRENT_PAGE)
        self.assertEqual(d.reason, "minor_overflow")

    def test_decision_json_safe(self):
        d = decide_placement(150.0, 120.0, 792.0)
        out = d.to_dict()
        self.assertEqual(set(out), {"target", "reason", "score"})
        json.dumps(out)


# ---------------------------------------------------------------------------
# 4. end-to-end — settled LayoutResult + PageFlowReport -> decision
# ---------------------------------------------------------------------------


class TestDecideFromSettled(unittest.TestCase):
    def test_settled_flow_block_minor_overflow(self):
        # page 1 has one settled block whose bottom is at 120 -> 120pt free;
        # a settled 7-line @ 20pt block needs 140 -> minor overflow, current
        plan = [_flow("p1_0", 1, 60.0, 120.0, 260.0, 160.0)]
        report = _report(plan, page_sizes={1: 792.0})
        result = LayoutResult(text="t", lines=["l%d" % i for i in range(7)],
                              font_size=20.0)
        d = decide_from_settled(result, report, 1, 792.0)
        self.assertEqual(d.target, PlacementTarget.CURRENT_PAGE)
        self.assertEqual(d.reason, "minor_overflow")
        self.assertEqual(d.score.needed, 140.0)
        self.assertEqual(d.score.available, 120.0)
        self.assertEqual(d.score.line_count, 7)

    def test_settled_block_that_fits(self):
        plan = [_flow("p1_0", 1, 60.0, 500.0, 260.0, 540.0)]  # 500pt free
        report = _report(plan, page_sizes={1: 792.0})
        result = LayoutResult(text="t", lines=["a", "b"], font_size=10.0)
        d = decide_from_settled(result, report, 1, 792.0)
        self.assertEqual(d.target, PlacementTarget.CURRENT_PAGE)
        self.assertEqual(d.reason, "fits")

    def test_read_only_no_mutation(self):
        plan = [_flow("p1_0", 1, 60.0, 120.0, 260.0, 160.0)]
        report = _report(plan, page_sizes={1: 792.0})
        result = LayoutResult(text="t", lines=["a"] * 7, font_size=20.0)
        report_snapshot = copy.deepcopy(report.to_dict())
        result_snapshot = result.to_dict()
        decide_from_settled(result, report, 1, 792.0)
        decide_placement(900.0, 10.0, 792.0)
        self.assertEqual(report.to_dict(), report_snapshot)
        self.assertEqual(result.to_dict(), result_snapshot)


# ---------------------------------------------------------------------------
# 5. architecture purity — decision only, no layout engine / no writes
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
    """Executable code with docstrings stripped (prose must not trip guards)."""
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


class TestPlacementArchitecture(unittest.TestCase):
    def test_placement_never_re_lays_out(self):
        src = _code(_PLACEMENT_PATH)
        for banned in ("lay_out(", "adaptive_layout(", "wrap_lines(",
                       "shrink_to_fit(", "clip_text(", "detect_page_collisions(",
                       "detect_page_overflows(", "build_page_flow_report(",
                       "resolve_page_shifts(", "apply_page_shifts(",
                       "decide_page_recovery("):
            self.assertNotIn(banned, src,
                             f"placement.py 不得执行/复用: {banned}")

    def test_placement_never_references_detector_or_renderer(self):
        src = _code(_PLACEMENT_PATH)
        for banned in ("list_detector", "list_parser", "toc_parser",
                       "code_detector", "style_detector",
                       "semantic.renderer", "translator", "magicpdf"):
            self.assertNotIn(banned, src,
                             f"placement.py 不得引用: {banned}")

    def test_placement_never_derives_geometry_from_level_index(self):
        tree = ast.parse(_code(_PLACEMENT_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(
                    f"placement.py 用 {type(node.op).__name__} 重建几何"
                )

    def test_placement_defines_no_adaptive_executor(self):
        tree = ast.parse(_PLACEMENT_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("adaptive"):
                raise AssertionError(
                    f"placement.py 定义了第二个 executor: {node.name}"
                )

    def test_placement_never_writes_geometry(self):
        src = _code(_PLACEMENT_PATH)
        for banned in ('"dst_box"] =', '"src_box"] =', '"page"] =',
                       'entry["', 'placement.'):
            self.assertNotIn(banned, src,
                             f"placement.py 不得写 plan/geometry: {banned}")

    def test_placement_consumes_settled_geometry_only(self):
        src = _code(_PLACEMENT_PATH)
        # settled extents are consumed via the placement's bottom edge
        # (``BlockPlacement.bottom`` == ``resolved_bbox[1]``) and the settled
        # LayoutResult's own line/font fields
        self.assertIn("last.bottom", src)
        self.assertIn("LayoutResult", src)       # settled height source
        self.assertIn("PRESERVE_KINDS", src)     # immovable kinds, read-only


if __name__ == "__main__":
    unittest.main()
