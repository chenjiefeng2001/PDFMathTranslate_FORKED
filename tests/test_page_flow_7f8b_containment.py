# -*- coding: utf-8 -*-
"""P0-2 — containment-aware adjacency in the PageFlow detector.

Real-PDF scan found ~88% of ``preserved_region`` collisions were segmentation
artifacts, not real layout collisions:

- **inline formula**: the formula block's bbox is horizontally contained inside
  its paragraph (adjacency treated as stacked collision) — 1808: 70 of 80
  preserved collisions had this contained-bbox signature, kinds 99x formula /
  59x paragraph;
- multi-line titles split into separate blocks overlapping ~1 pt; headings
  merged with the following paragraph into one huge bbox.

This file locks the P0-1-adjacent correctness fix in ``detect_collisions_from_placements``:
a pair where one box is **horizontally contained inside** the other (inline
membership) — or where either side is ``formula_inline`` — is excluded from
stacking adjacency.  Recovery then solves *real* vertical collisions, not
parser noise.  It is a detection-only change: placement IDs, reasons, X
geometry and the source anchor are untouched.
"""

import unittest

from pdf2zh.semantic.layout.page_flow import detect_page_collisions


def _entry(block_id, page, kind, x0, y0, x1, y1):
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id, "page": page, "kind": kind,
        "text": "t", "translated": "t",
        "src_box": list(box), "dst_box": list(box),
        "font_size": 11.0,
        "render_payload": {"kind": kind, "commands": []},
        "list_items": None, "toc_entries": None, "toc_commands": None,
    }


def _flow(block_id, page, x0, y0, x1, y1):
    return _entry(block_id, page, "flow", x0, y0, x1, y1)


class TestContainmentAware(unittest.TestCase):
    def test_inline_formula_contained_inside_paragraph_skipped(self):
        # paragraph spans [60,260]; the formula sits fully inside it ([90,120])
        # and overlaps vertically — the inline-membership signature the scan
        # flagged 88% of.  Contained box -> NOT a stacked collision.
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),          # paragraph
            _entry("p1_1", 1, "formula", 90.0, 660.0, 120.0, 720.0),  # inline inside
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_containment_inside_paragraph_from_below_also_skipped(self):
        # the inline box is the lower block (the usual reading order)
        plan = [
            _entry("p1_0", 1, "formula", 90.0, 720.0, 120.0, 742.0),
            _flow("p1_1", 1, 60.0, 700.0, 260.0, 740.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_stacked_same_column_still_collides(self):
        # identical x-ranges (a real stacked paragraph overlap) are NOT
        # "contained" — containment requires a proper horizontal subset.
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 750.0),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0),
        ]
        c = detect_page_collisions(plan)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].reason, "overlap")

    def test_formula_inline_kind_skipped_even_same_column(self):
        # a formula_inline block shares the column x yet is inline by kind —
        # stacking adjacency against it is never a shiftable vertical collision.
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
            _entry("p1_1", 1, "formula_inline", 60.0, 660.0, 260.0, 720.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_real_stacked_code_still_preserved_region(self):
        # code is not inline/contained over the same column -> real collision,
        # classified preserved_region (immovable by shift).
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
            _entry("p1_1", 1, "code", 60.0, 660.0, 260.0, 720.0),
        ]
        c = detect_page_collisions(plan)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].reason, "preserved_region")

    def test_two_column_interleave_still_not_collision(self):
        # separate columns that merely touch vertically stay non-colliding and
        # are unaffected by containment (they are not contained either).
        plan = [
            _flow("p1_0", 1, 0.0, 700.0, 300.0, 760.0),
            _flow("p1_1", 1, 310.0, 500.0, 600.0, 560.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])


if __name__ == "__main__":
    unittest.main()