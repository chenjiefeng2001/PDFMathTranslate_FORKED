# -*- coding: utf-8 -*-
"""Commit 7G-5 item 1 — recovery-side drawn-extent parity.

7G-4's receiver-at-FINAL floor bounds a descending block by the receiver's
**resolved box top**.  But a block's REAL drawn glyph top can rise above that
box top — the settled baseline + a conservative ascent pokes out of
``dst_box`` (single-line fragments whose lines sit high in the box).  When
recovery shifts ``L`` down to exactly ``R``'s box top, ``L``'s words land on
``R``'s actual glyphs: the residual ``recovery_introduced`` overlap on heavy
books (report §15.5 item 1).

7G-5 adds two pure-read maps (computed once from the settled plan) that only
change the AMOUNT of an already-decided move — never the moved set, never
``resolved_bbox`` (the §13.3 "extend resolved_bbox" rejection stays intact):

- **``excess_by_key``** — the receiver's TOP glyph excess (command blocks
  only): the floor is ``final_top + excess``, the receiver's REAL drawn top;
- **``spill_by_key``** — a command-less movable block's own wrapped BOTTOM
  spill (legacy ``_insert_text_wrapped`` fallback): the cap clears the real
  drawn bottom.  Command blocks are excluded — 7F-8b's ``_resolve_bbox``
  already folded their drawn bottom into ``resolved_bbox``, so subtracting it
  again would double-count.

Locked guarantees (regression gates for the 7G-5 branch):

1. **receiver glyph-top floor binds** — shift stops at ``R.top + excess``;
2. **absent maps keep exact 7G-4 behaviour** — floor = box top, cap = box
   bottom (byte-identical to 7G-4);
3. **movable's own wrapped spill reduces the cap** — never descends past the
   neighbour below its real drawn bottom;
4. **preserved receivers get the same glyph-top floor** — a fragment stops AT
   a preserved region's real glyph top, never inside it;
5. **no double-count** — command blocks never contribute ``spill_by_key``
   (7F-8b already folded their drawn bottom in);
6. **correction-layer discipline** — only the amount changes; Phase-1 intent
   (the moved set) and the 7G-4 guarantee set are unchanged.
"""

import unittest

from pdf2zh.semantic.layout.page_flow import BlockPlacement, placements_from_plan
from pdf2zh.semantic.layout.page_shift import (
    _ordered_cascade_plan,
    _recovery_draw_extent_by_key,
    resolve_page_shifts,
)

_X0, _X1 = 60.0, 260.0


def _blk(i, top, bottom, kind="flow", x0=_X0, x1=_X1):
    """One placement on a 792-pt page (v3 y-up: larger y = lower on page)."""
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


def _entry(block_id, box, commands, text="t", font_size=11.0, kind="flow"):
    """One render-plan entry with an explicit settled payload."""
    return {
        "block_id": block_id,
        "page": 1,
        "kind": kind,
        "src_box": list(box),
        "dst_box": list(box),
        "text": text,
        "translated": text,
        "font_size": font_size,
        "render_payload": {"kind": kind, "commands": commands},
    }


# Clean column geometry used by every Phase-2 case:
#   U = [670, 700], L = [600, 680], R = [540, 590]
#   U↕L collision (required 10), L↕R gap 10, U↕R disjoint.  L's required move
#   (10) equals the gap, so the phase-1 cascade never shoves R.
_U = (0, 700.0, 670.0)
_L = (1, 680.0, 600.0)
_R = (2, 590.0, 540.0)

# ---------------------------------------------------------------------------
# 1. Phase-2 pure plan -- the 7G-5 floor/cap contracts
# ---------------------------------------------------------------------------


class TestDrawnExtentFloor(unittest.TestCase):
    def test_receiver_glyph_top_binds_the_floor(self):
        # R's lines rise 6.8 pt above its box top (settled baseline + ascent
        # pokes out).  7G-4 would land L at R.top (cap 600-590 = 10); the
        # 7G-5 floor is R's REAL drawn top 596.8 -> cap 3.2.
        u = _blk(*_U)
        l = _blk(*_L)
        r = _blk(*_R)
        self.assertEqual(
            _ordered_cascade_plan([u, l, r], 792.0, excess_by_key={(1, 2): 6.8}),
            {1: 3.2},
        )

    def test_absent_map_keeps_7g4_floor(self):
        # no parity maps => exact 7G-4 behaviour: floor is the box top.
        u = _blk(*_U)
        l = _blk(*_L)
        r = _blk(*_R)
        self.assertEqual(_ordered_cascade_plan([u, l, r], 792.0), {1: 10.0})
        self.assertEqual(
            _ordered_cascade_plan([u, l, r], 792.0, excess_by_key={}, spill_by_key={}),
            {1: 10.0},
        )

    def test_zero_excess_entry_is_ignored(self):
        # an excess key with value 0 must not move the floor.
        u = _blk(*_U)
        l = _blk(*_L)
        r = _blk(*_R)
        self.assertEqual(
            _ordered_cascade_plan([u, l, r], 792.0, excess_by_key={(1, 2): 0.0}),
            {1: 10.0},
        )

    def test_movable_wrapped_spill_reduces_cap(self):
        # L is a command-less block whose wrapped extent dips 5 pt below its
        # box bottom: the cap clears the real drawn bottom (600-5-590 = 5).
        u = _blk(*_U)
        l = _blk(*_L)
        r = _blk(*_R)
        self.assertEqual(
            _ordered_cascade_plan([u, l, r], 792.0, spill_by_key={(1, 1): 5.0}),
            {1: 5.0},
        )

    def test_preserved_receiver_gets_glyph_top_floor_too(self):
        # a preserved region's REAL glyph top is the floor (immovable + top
        # excess): L stops AT it, never inside it.
        u = _blk(*_U)
        l = _blk(*_L)
        f = _blk(2, 590.0, 540.0, kind="code")
        self.assertEqual(
            _ordered_cascade_plan([u, l, f], 792.0, excess_by_key={(1, 2): 3.0}),
            {1: 7.0},
        )

    def test_cascade_receivers_stay_at_final_glyph_top(self):
        # a real all-movable cascade with a top-excess receiver resolves: the
        # receiver's OWN capped move propagates and the excess stays on top of
        # its final position (never starved, never landed on).
        b0 = _blk(0, top=700, bottom=600)
        b1 = _blk(1, top=615, bottom=550)  # overlaps b0 (req 15)
        b2 = _blk(2, top=560, bottom=500)  # overlaps b1 (req 10), glyph +4
        plan = _ordered_cascade_plan([b0, b1, b2], 792.0, excess_by_key={(1, 2): 4.0})
        # b2 descends 25 to final top 535; its glyph top is 539; b1's cap is
        # 550-539 = 11 (7G-4 without excess would let b1 reach 15).
        self.assertEqual(plan, {1: 11.0, 2: 25.0})


# ---------------------------------------------------------------------------
# 2. parity-map builder -- pure read, no double-count
# ---------------------------------------------------------------------------


class TestDrawExtentMapBuilder(unittest.TestCase):
    def test_command_excess_enters_and_command_less_zero(self):
        # R is a command block whose baseline+ascent pokes 6.8 above dst_top;
        # U/L are command-less with no geometry -> excess 0 (absent).
        plan = [
            _entry("p1_0", (60.0, 650.0, 260.0, 700.0), commands=[]),
            _entry("p1_1", (60.0, 600.0, 260.0, 680.0), commands=[]),
            _entry(
                "p1_2",
                (60.0, 540.0, 260.0, 590.0),
                commands=[{"y": 588.0, "x": 62.0, "text": "t", "font_size": 11.0}],
            ),
        ]
        excess_by_key, spill_by_key = _recovery_draw_extent_by_key(plan)
        self.assertEqual(excess_by_key, {(1, 2): 6.8})
        self.assertEqual(spill_by_key, {})

    def test_command_bottom_spill_is_never_double_counted(self):
        # a command block whose glyphs dip below dst_box has a positive raw
        # spill -- but _resolve_bbox (7F-8b) already folded that into the
        # placement's resolved bottom, so the recovery map must EXCLUDE it.
        cmd = _entry(
            "p1_0",
            (60.0, 540.0, 260.0, 590.0),
            commands=[{"y": 530.0, "x": 62.0, "text": "t", "font_size": 11.0}],
        )
        plan = [cmd]
        excess_by_key, spill_by_key = _recovery_draw_extent_by_key(plan)
        self.assertNotIn((1, 0), spill_by_key)
        # resolved_bbox already carries the drawn bottom (530 - 0.25*11)
        resolved = placements_from_plan(plan)[0].resolved_bbox
        self.assertEqual(round(resolved[1], 2), 527.25)
        self.assertEqual(excess_by_key, {})

    def test_command_less_wrapped_spill_enters(self):
        # a command-less block whose text re-wraps taller than its box has a
        # real bottom spill the cap must clear (3 lines vs a 2-line box).
        text = " ".join(
            [
                "a",
                "b",
                "c",
                "d",
                "e",
                "f",
                "g",
                "h",
                "i",
                "j",
                "k",
                "l",
                "m",
                "n",
                "o",
                "p",
                "q",
                "r",
                "s",
                "t",
                "u",
                "v",
                "w",
                "x",
                "y",
                "z",
                "aa",
                "bb",
            ]
        )
        slim = _entry(
            "p1_0", (60.0, 600.0, 260.0, 680.0), commands=[], text=text, font_size=20.0
        )
        excess_by_key, spill_by_key = _recovery_draw_extent_by_key([slim])
        self.assertEqual(spill_by_key, {(1, 0): 4.0})
        self.assertEqual(excess_by_key, {})


# ---------------------------------------------------------------------------
# 3. integration -- resolve_page_shifts with a settled payload
# ---------------------------------------------------------------------------


class TestResolveShifts7g5(unittest.TestCase):
    def test_receiver_glyph_top_applied_end_to_end(self):
        # R's settled command (y=588, fs=11) puts its real glyph top at
        # 596.8 (dst_top 590 + 6.8 excess).  L is forced down 10pt by U<->L
        # but must stop at 596.8 -> final L bottom 596.8, shift 3.2.
        plan = [
            _entry("p1_0", (60.0, 670.0, 260.0, 700.0), commands=[]),
            _entry("p1_1", (60.0, 600.0, 260.0, 680.0), commands=[]),
            _entry(
                "p1_2",
                (60.0, 540.0, 260.0, 590.0),
                commands=[{"y": 588.0, "x": 62.0, "text": "t", "font_size": 11.0}],
            ),
        ]
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(final[1].resolved_bbox, (60.0, 596.8, 260.0, 676.8))
        # U untouched, R untouched, X verbatim everywhere
        self.assertEqual(final[0].resolved_bbox, (60.0, 670.0, 260.0, 700.0))
        self.assertEqual(final[2].resolved_bbox, (60.0, 540.0, 260.0, 590.0))

    def test_command_less_wrapped_spill_reduces_end_to_end(self):
        # L is a command-less fragment whose text wraps 4 pt past its box
        # bottom: the cap clears the real drawn bottom (occ 596) so L stops at
        # 594 instead of landing on R's top 590 (the 7G-4 cap 10).
        text = " ".join(
            [
                "a",
                "b",
                "c",
                "d",
                "e",
                "f",
                "g",
                "h",
                "i",
                "j",
                "k",
                "l",
                "m",
                "n",
                "o",
                "p",
                "q",
                "r",
                "s",
                "t",
                "u",
                "v",
                "w",
                "x",
                "y",
                "z",
                "aa",
                "bb",
            ]
        )
        plan = [
            _entry("p1_0", (60.0, 670.0, 260.0, 700.0), commands=[]),
            _entry(
                "p1_1",
                (60.0, 600.0, 260.0, 680.0),
                commands=[],
                text=text,
                font_size=20.0,
            ),
            _entry("p1_2", (60.0, 540.0, 260.0, 590.0), commands=[]),
        ]
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(final[1].resolved_bbox, (60.0, 594.0, 260.0, 674.0))


if __name__ == "__main__":
    unittest.main()
