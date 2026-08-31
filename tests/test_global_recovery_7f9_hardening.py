# -*- coding: utf-8 -*-
"""P0-1 regression — Global Recovery progress / stuck-state hardening.

Discovered on the real-PDF corpus scan: ``global_recovery`` burned its whole
``max_passes`` budget in a **zero-progress loop** (1808: 570/570 identical
consecutive passes, 2,615,883 no-op "applied" events, 445 s).  Root cause:

1. ``PageCollision.required_shift`` is ``round(..., 2)`` — a sub-centipoint
   overlap (e.g. 0.004 pt from line-spacing padding / bbox inflation) becomes
   ``shift_y = 0.0``;
2. ``decide_block_shift`` returned ``SHIFT_DOWN(0.0)``, which 8d counted as
   *applicable* and "applied" as a no-op (geometry unchanged);
3. ``global_recovery``'s guard was "applied non-empty" — zero-delta decisions
   kept it True every round, so the loop re-detected the same stuck
   collisions and re-applied the same zero shifts until max_passes ran out.

This file is the executor/orchestrator **correctness hardening**, NOT a new
recovery policy.  It locks the real-progress contract:

- **progress** = collision multiset shrank OR resolved geometry actually moved;
- a zero-delta SHIFT_DOWN is an invalid action (not ``applied``, no fake
  recovery event, no budget consumed);
- a round that changes nothing stops immediately → ``stopped_reason =
  \"no_progress\"`` with the leftovers recorded ``unresolved``.

Regression corpus:

- case A: overlap = 0.004 pt, required_shift = 0   → stuck on pass 1, no_progress
- case B: SHIFT_DOWN = 0                            → applied = 0, unresolved > 0
- case C: collision set identical across passes     → no_progress, never burns budget
- case D: normal SHIFT 20 pt                        → collision gone, applied = 1, converged
"""

import unittest

from pdf2zh.semantic.layout.page_flow import (
    detect_page_collisions,
    placements_from_plan,
)
from pdf2zh.semantic.layout.page_shift import resolve_page_shifts
from pdf2zh.semantic.layout.global_recovery import global_recovery

_PAGE = {1: 792.0}


def _entry(block_id, page, kind, x0, y0, x1, y1, payload=None):
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id, "page": page, "kind": kind,
        "text": "t", "translated": "t",
        "src_box": list(box), "dst_box": list(box),
        "font_size": 11.0,
        "render_payload": payload if payload is not None
        else {"kind": kind, "commands": []},
        "list_items": None, "toc_entries": None, "toc_commands": None,
    }


def _flow(block_id, page, x0, y0, x1, y1):
    """Flow entry with a single settled command line inside its box.

    7G-5 (recovery-side draw-extent parity): the cascade floor is the
    receiver's REAL glyph top (settled baseline + ascent).  A well-formed
    block's baseline sits INSIDE its box, so the command baseline is placed
    at ``top - 9`` (ascent 0.8*10 = 8 → glyph top = top - 1 < top → zero
    excess, the 7G-2.1 convention).  Tiny boxes (height < 12) center the
    line so the drawn bottom never pokes below the box.
    """
    box_h = float(y1) - float(y0)
    cmd_y = float(y1) - 9.0 if box_h >= 12.0 else (float(y0) + float(y1)) / 2.0
    payload = {
        "kind": "flow", "font_size": 10.0,
        "commands": [{"kind": "flow-text", "text": "t", "x": float(x0),
                      "y": cmd_y, "width": 100.0, "line": 0,
                      "is_last": True, "overflow": False}],
    }
    return _entry(block_id, page, "flow", x0, y0, x1, y1, payload=payload)


def _subpixel_stuck_plan():
    """Two stacked flow blocks overlapping by 0.004 pt.

    ``B.top - A.bottom = 700.004 - 700.0 = 0.004`` → ``required_shift`` rounds
    to 0.0 (the exact P0-1 signature detected on real PDFs).
    """
    return [
        _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
        _flow("p1_1", 1, 60.0, 660.0, 260.0, 700.004),
    ]


def _normal_shift_plan():
    """A clean 20 pt overlap — SHIFT_DOWN must actually resolve it."""
    return [
        _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
        _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0),
    ]


class TestCaseA_SubpixelOverlap(unittest.TestCase):
    """overlap = 0.004 pt, required_shift = 0 → stuck on pass 1, not 572."""

    def test_required_shift_rounds_to_zero(self):
        collisions = detect_page_collisions(_subpixel_stuck_plan())
        # B.top - A.bottom = 0.004 pt — a real overlap that rounds BOTH the
        # overlap and the required_shift to 0.0 (the P0-1 signature: a
        # geometric problem policy reports as "shift nothing").
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0].required_shift, 0.0)
        self.assertEqual(collisions[0].overlap, 0.0)

    def test_global_recovery_sticks_on_first_pass_no_progress(self):
        final, report = global_recovery(
            _subpixel_stuck_plan(), page_sizes=_PAGE)
        self.assertFalse(report.converged)
        self.assertTrue(report.stopped_early)
        self.assertEqual(report.stopped_reason, "no_progress")
        # one round — it must NEVER burn the max_passes budget
        self.assertEqual(report.passes, 1)
        # zero-delta is not "applied"; leftovers are visible unresolved
        self.assertEqual(report.applied, 0)
        self.assertGreater(report.unresolved, 0)
        # no fake SHIFT_DOWN event fabricates work
        shifts = [e for e in report.events if e.action == "SHIFT_DOWN"]
        self.assertEqual(shifts, [])
        # geometry untouched (the no-op was not written back)
        self.assertEqual(len(detect_page_collisions(final)), 1)


class TestCaseB_ZeroShiftExecutor(unittest.TestCase):
    """SHIFT_DOWN = 0 → applied = 0, unresolved > 0 (8d zero-delta guard)."""

    def test_zero_shift_is_not_applicable(self):
        final, report = resolve_page_shifts(
            _subpixel_stuck_plan(), page_sizes=_PAGE)
        self.assertTrue(report.stopped_early)
        self.assertEqual(report.stopped_reason, "no_progress")
        self.assertEqual(report.passes, 0)
        self.assertEqual(report.applied, [])            # nothing applied
        self.assertGreater(len(report.unresolved), 0)   # collision stays
        # geometry is unchanged — the decision was a no-op and was skipped
        self.assertEqual(final[1].resolved_bbox, (60.0, 660.0, 260.0, 700.004))


class TestCaseC_StableStateNeverBurnsBudget(unittest.TestCase):
    """Collision set identical across passes → no_progress, far under budget."""

    def test_no_progress_stops_well_before_max_passes(self):
        plan = _subpixel_stuck_plan() + [
            _flow("p1_4", 1, 60.0, 640.0, 260.0, 650.0),
            _flow("p1_5", 1, 60.0, 620.0, 260.0, 630.0),
        ]
        # a larger max_passes bound than the default must NOT be consumed
        final, report = global_recovery(plan, page_sizes=_PAGE, max_passes=200)
        self.assertFalse(report.converged)
        self.assertTrue(report.stopped_early)
        self.assertEqual(report.stopped_reason, "no_progress")
        self.assertEqual(report.passes, 1)
        # leftover collisions are visible — the system stopped, it did not burn
        self.assertEqual(
            report.unresolved,
            len(detect_page_collisions(final)) + report.deferred)

    def test_budget_exhaustion_is_attributed_not_disguised(self):
        # 8d's own cascade needs 2 passes (A-B then B-C); cap it at 1.  The
        # leftover must be recorded unresolved with an attributed reason
        # ("budget_expired"), never silently spun to the cap count.
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 730.0),
            _flow("p1_1", 1, 60.0, 670.0, 260.0, 710.0),
            _flow("p1_2", 1, 60.0, 650.0, 260.0, 665.0),
        ]
        final, report = resolve_page_shifts(plan, page_sizes=_PAGE, max_passes=1)
        self.assertFalse(report.stopped_early)   # budget run out, not stuck
        self.assertEqual(report.stopped_reason, "budget_expired")
        self.assertEqual(report.passes, 1)
        self.assertEqual(len(report.applied), 1)
        self.assertGreater(len(report.unresolved), 0)
        # B moved 10 pt down in pass 1; the B-C leftover is visible, not hidden
        self.assertEqual(final[1].resolved_bbox, (60.0, 660.0, 260.0, 700.0))


class TestCaseD_NormalShift(unittest.TestCase):
    """A real 20 pt shift converges: collision gone, applied = 1."""

    def test_clean_shift_converges_in_one_pass(self):
        before = len(detect_page_collisions(_normal_shift_plan()))
        self.assertEqual(before, 1)
        final, report = global_recovery(_normal_shift_plan(), page_sizes=_PAGE)
        self.assertTrue(report.converged)
        self.assertEqual(report.stopped_reason, "")
        self.assertEqual(report.applied, 1)
        self.assertEqual(report.unresolved, 0)
        self.assertEqual(report.passes, 1)
        self.assertEqual(len(detect_page_collisions(final)), 0)
        # SHIFTFLOW landed 20 pt lower (v3 y-up: y decreased)
        placements = placements_from_plan(final)
        self.assertEqual(placements[1].resolved_bbox, (60.0, 640.0, 260.0, 700.0))


if __name__ == "__main__":
    unittest.main()
