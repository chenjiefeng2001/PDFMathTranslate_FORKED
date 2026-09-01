# -*- coding: utf-8 -*-
"""Commit 7G-4 — ordered two-phase, receiver-at-FINAL recovery cascade.

The corpus ledger (`doc/corpus_v1_failures.md` §5) and the §14.2 root-cause
boiled the recovery word-overlap down to ONE mono-pattern, seen on every REAL
page recovery shifted >=7 pt::

    collision U↕L → SHIFT_DOWN moves L down by required_shift
       → L's drawn glyphs land on the receiver R below it
       → R (in the same decision) was not bounded by L → new overlap

The V1 baseline decided each SHIFT_DOWN from its own pre-move geometry, so a
descending block was never bounded against the receiver it lands on.  7G-4 is
a per-page correction layer over that frozen cascade it mirrors 7G-2.1's
``compact → re-anchor`` split:

- **Phase 1 — intent** (the authoritative 8c decisions): the exact V1
  ``detect → decide → apply required_shift`` cascade on THIS page, so the set
  of moved blocks is byte-identical to V1 — only the amounts are corrected.
- **Phase 2 — receiver-at-FINAL floor**, resolved BOTTOM-UP: each movable
  block's shift is capped so its **drawn bottom** does not pass the **drawn
  top** of the nearest horizontally-overlapping receiver below it, at that
  receiver's OWN final position.  Preserved blocks and the page-bottom edge are
  immovable floors.  Because the floor uses each receiver's final position, a
  genuine cascade (U↕L→L↕R, both movable) is NOT starved — L descends to R's
  new top — but L is never shoved onto a receiver that merely sits below.

Locked guarantees (regression gates for the 7G-4 branch, per §6):

1. **intent preserved on a clean column** — no floor below ⇒ the exact V1
   `required_shift` is applied (the cascade keeps V1's focused moves);
2. **drawn-bottom cap (not top)** — a block's DRAWN BOTTOM, never its box top,
   is what must clear the receiver; the old cap (by box top) let a tall block
   descend on top of the receiver by its own height;
3. **preserved receiver is a hard floor** — a movable block is stopped so its
   drawn bottom lands AT the preserved region's top, never inside it; the
   upper collision is surfaced unresolved instead of being \"resolved\" by
   landing on the region;
4. **cascade not starved** — when the receiver below is also movable and will
   move down, L is allowed to descend to the receiver's FINAL top (the stack
   resolves, nothing is left unresolved);
5. **side-by-side (x-disjoint) block below is NOT a floor** — a different
   column never binds the reclaim;
6. **only Y changes, ``src_box`` verbatim** — the fix never touches X / font /
   text, matching the 7G-2 discipline;
7. **preserved blocks never enter the move map** — `_ordered_cascade_plan`
   returns no key for a preserved placement.
"""

import unittest

from pdf2zh.semantic.layout.page_flow import (
    BlockPlacement,
    placements_from_plan,
)
from pdf2zh.semantic.layout.page_shift import (
    _ordered_cascade_plan,
    apply_block_shift,
    apply_page_shifts,
    resolve_page_shifts,
)

_X0, _X1 = 60.0, 260.0


def _blk(i, top, bottom, kind="flow", x0=_X0, x1=_X1):
    """One placement on a 792-pt page (v3 y-up: larger y = lower on page... i.e.
    top edge is the larger y; ``top >= bottom``)."""
    box = (float(x0), float(bottom), float(x1), float(top))
    return BlockPlacement(
        block_index=i,
        page=1,
        kind=kind,
        bbox=box,
        resolved_bbox=box,
        height=top - bottom,
        preserved=kind in {"code", "formula", "figure"},
        has_continuation=False,
    )


def _plan(placements):
    """Render-plan entries for an integration run (dst == src, simple flow cmd)."""
    entries = []
    for p in placements:
        entries.append(
            {
                "block_id": f"p1_{p.block_index}",
                "page": 1,
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


# ---------------------------------------------------------------------------
# 1. Phase-2 pure plan — the four geometric contracts
# ---------------------------------------------------------------------------


class TestOrderedCascadePlan(unittest.TestCase):
    def test_clean_column_keeps_v1_intent(self):
        # one U↕L collision, nothing below L: the receiver floor is the page
        # bottom, so the exact required_shift is applied (V1 intent kept).
        u = _blk(0, top=700, bottom=600)
        l = _blk(1, top=680, bottom=640)  # overlaps U → required = 680-600 = 80
        self.assertEqual(_ordered_cascade_plan([u, l], 792.0), {1: 80.0})

    def test_drawn_bottom_cap_binds_immovable_floor(self):
        # Block L above a PRESERVED receiver F: L is forced down 140pt by U↕L,
        # but F is immovable just below (F.top = 640, L.bottom = 650).
        # Correct cap = L.bottom(650) − F.top(640) = 10 — so L's DRAWN BOTTOM
        # lands exactly at F's top.  A cap by box TOP (= 700 − 640 = 60) would
        # let L descend 50pt INTO the preserved region — the exact old-bug
        # signature.  The floor is the drawn bottom, never the box top.
        u = _blk(0, top=720, bottom=560)
        l = _blk(1, top=700, bottom=650)  # required = 700-560 = 140
        f = _blk(2, top=640, bottom=600, kind="code")  # preserved
        self.assertEqual(_ordered_cascade_plan([u, l, f], 792.0), {1: 10.0})

    def test_preserved_receiver_never_in_move_map(self):
        # a preserved placement contributes no key and no shift
        u = _blk(0, top=720, bottom=560)
        f = _blk(1, top=640, bottom=600, kind="code")
        self.assertEqual(_ordered_cascade_plan([u, f], 792.0), {})

    def test_receiver_at_final_lets_a_real_cascade_resolve(self):
        # A REAL column cascade — every block has its own collision — must
        # resolve, never starve.  b1 overlaps b0 (req 15) and b2 overlaps b1
        # (req 10).  Phase 1 intent slides the whole chain (b1 has intent and
        # b2 gains intent as it is pushed); Phase 2's bottom-up floor uses each
        # receiver's FINAL position, so L descends 15 fully while b2 absorbs
        # its 25.  All three end touching — nothing left unresolved.
        b0 = _blk(0, top=700, bottom=600)
        b1 = _blk(1, top=615, bottom=550)  # overlaps b0
        b2 = _blk(2, top=560, bottom=500)  # overlaps b1
        self.assertEqual(_ordered_cascade_plan([b0, b1, b2], 792.0), {1: 15.0, 2: 25.0})

    def test_side_by_side_receiver_is_not_a_floor(self):
        # L in the left column is forced down; a block in a fully x-disjoint
        # column sits below-ish but NEVER floors the descent (two-column gutter
        # must keep reclaiming).  Only the page-bottom edge binds.
        u = _blk(0, top=700, bottom=560)
        l = _blk(1, top=680, bottom=600)
        other = _blk(2, top=590, bottom=500, x0=400.0, x1=560.0)
        self.assertEqual(_ordered_cascade_plan([u, l, other], 792.0), {1: 120.0})

    def test_page_bottom_shifts_that_fit_stay_and_bottom_breaks_stay_out(self):
        # the page-bottom edge is the last-resort floor.  A required shift that
        # stays inside the page is applied in full; one that would cross the
        # bottom is NOT a SHIFT_DOWN at all (8c decides NEXT_PAGE → 8e's job),
        # so it must never enter the 8d move map.
        u = _blk(0, top=700, bottom=620)
        fits = _blk(1, top=680, bottom=70)  # req 60, bottom 70-60=10 >= 0
        self.assertEqual(_ordered_cascade_plan([u, fits], 792.0), {1: 60.0})
        crosses = _blk(1, top=680, bottom=20)  # req 60, bottom 20-60<0 → NEXT_PAGE
        self.assertEqual(_ordered_cascade_plan([u, crosses], 792.0), {})


# ---------------------------------------------------------------------------
# 2. integration — apply_page_shifts on the defect signatures
# ---------------------------------------------------------------------------


class TestApplyShifts7g4(unittest.TestCase):
    def test_movable_block_never_lands_on_preserved_region(self):
        # The 7G-4 headline: a block pushed down clears U only so far as it
        # stays OFF the preserved region below it (lands AT its top, never in
        # it), and the upper collision is surfaced unresolved, not hidden.
        u = _blk(0, top=720, bottom=560)
        l = _blk(1, top=700, bottom=650)
        f = _blk(2, top=640, bottom=600, kind="code")
        plan = _plan([u, l, f])
        new_plan, report = apply_page_shifts(plan, page_sizes={1: 792.0})
        # L shifted exactly 10pt — its drawn bottom (650) down to F's top (640)
        self.assertEqual(new_plan[1]["dst_box"], [60.0, 640.0, 260.0, 690.0])
        # upper (U↕L) collision is genuinely unresolved — surfaced, never dropped;
        # the cascade stops on no-progress (L can descend no further without
        # landing on the region) instead of burning the budget
        self.assertGreaterEqual(len(report.unresolved), 1)
        self.assertTrue(report.stopped_early)
        self.assertEqual(report.stopped_reason, "no_progress")
        # only Y changed: src_box verbatim everywhere, X untouched
        self.assertEqual(new_plan[1]["src_box"], [60.0, 650.0, 260.0, 700.0])
        self.assertEqual(new_plan[0]["dst_box"], [60.0, 560.0, 260.0, 720.0])
        self.assertEqual(new_plan[2]["dst_box"], [60.0, 600.0, 260.0, 640.0])

    def test_apply_block_shift_keeps_a_clean_shift_idempotent(self):
        # the pure executor still translates by the decided shift (unchanged
        # 7F-8d contract) — only the AMOUNT the cascade feeds it changed.
        u = _blk(0, top=700, bottom=600)
        l = _blk(1, top=680, bottom=640)
        from pdf2zh.semantic.layout.page_recovery import (
            BlockShiftDecision,
            PageRecoveryDecision,
        )

        shifted = apply_block_shift(
            l,
            BlockShiftDecision(
                block_index=1,
                page=1,
                decision=PageRecoveryDecision.SHIFT_DOWN,
                shift_y=80.0,
                reason="overlap",
                source_bbox=l.bbox,
                resolved_bbox=l.resolved_bbox,
            ),
        )
        self.assertEqual(shifted.resolved_bbox, (60.0, 560.0, 260.0, 600.0))
        self.assertEqual(shifted.bbox, l.bbox)  # source never changes

    def test_cascade_of_movables_converges(self):
        # a real all-movable column cascade (every block overlaps) converges:
        # receiver-at-FINAL lets the whole chain slide to a touching, overlap-
        # free final state — nothing unresolved, nothing shoved onto a receiver.
        b0 = _blk(0, top=700, bottom=600)
        b1 = _blk(1, top=615, bottom=550)  # overlaps b0 (req 15)
        b2 = _blk(2, top=560, bottom=500)  # overlaps b1
        plan = _plan([b0, b1, b2])
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(report.unresolved, [])
        self.assertEqual(final[0].resolved_bbox, (60.0, 600.0, 260.0, 700.0))
        self.assertEqual(final[1].resolved_bbox, (60.0, 535.0, 260.0, 600.0))
        self.assertEqual(final[2].resolved_bbox, (60.0, 475.0, 260.0, 535.0))
        # the final placements carry no resolved collisions
        from pdf2zh.semantic.layout.page_flow import detect_collisions_from_placements

        self.assertEqual(detect_collisions_from_placements(final), [])


if __name__ == "__main__":
    unittest.main()
