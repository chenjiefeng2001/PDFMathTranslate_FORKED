"""Commit 7F-5a — TOC Recovery Contract.

Locks the TOC-specific recovery behavior before any renderer is touched:

- TOC title ladder is **WRAP → SHRINK → PRESERVE_OVERFLOW** — CLIP is
  **always forbidden** for a TOC title (a title must never be truncated into
  the page column);
- ``page_x`` / ``page_number`` / ``title_x`` / destination are immovable —
  ``column`` and ``marker`` always PRESERVE_OVERFLOW;
- the dot leader only **shrinks** toward ``page_x`` and never crosses it;
- ``adaptive_layout(..., target="title")`` never clips even when a caller
  passes a clip-allowing budget (defense in depth at the executor);
- when the budget is exhausted the final decision is PRESERVE_OVERFLOW, never
  a stale WRAP/SHRINK — diagnostics must say ``preserve_overflow``.

The contract is policy-layer + executor only: no TOC renderer / detector /
parser changes here (those land in 7F-5b/5c).
"""

import unittest

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.overflow import LayoutResult
from pdf2zh.semantic.layout.primitives import FixedAnchor, FixedColumn
from pdf2zh.semantic.layout.recovery import (
    LayoutBudget,
    OverflowReason,
    RecoveryDecision,
    budget_for_kind,
    decide_recovery,
)


def _measure(text, size):
    w = 0.0
    for ch in text or "":
        w += size if ord(ch) >= 0x2E80 else size * 0.5
    return w


def _title(text, x=72.0, page_x=500.0, size=10.0):
    """A TOC title anchor: pinned at title_x, bounded by the page column."""
    return FixedAnchor(
        text=text, x=x, y=700.0, max_width=max(0.0, page_x - x - 4.0), role="title_x"
    )


# ---------------------------------------------------------------------------
# 1. toc_title budget — CLIP forbidden by default
# ---------------------------------------------------------------------------


class TestTocTitleBudget(unittest.TestCase):
    def test_budget_for_kind_toc_title_never_clips(self):
        b = budget_for_kind("toc_title")
        self.assertTrue(b.allow_wrap)
        self.assertTrue(b.allow_shrink)
        self.assertFalse(b.allow_clip)  # the whole point of 7F-5a
        self.assertEqual(b.max_extra_lines, 2)
        # "toc" alias resolves to the same budget
        self.assertFalse(budget_for_kind("toc").allow_clip)

    def test_anchor_budget_still_allows_clip_for_generic_use(self):
        # list content anchor keeps the aggressive ladder; TOC uses toc_title
        self.assertTrue(budget_for_kind("anchor").allow_clip)

    def test_column_budget_never_moves(self):
        b = budget_for_kind("column")
        self.assertFalse(b.allow_wrap)
        self.assertFalse(b.allow_shrink)
        self.assertFalse(b.allow_clip)


# ---------------------------------------------------------------------------
# 2. decide_recovery — the WRAP → SHRINK → PRESERVE ladder, never CLIP
# ---------------------------------------------------------------------------


class TestTocTitleDecisionLadder(unittest.TestCase):
    def test_width_overflow_wraps_first(self):
        d = decide_recovery(
            "toc_title", OverflowReason.WIDTH, budget=budget_for_kind("toc_title")
        )
        self.assertIs(d, RecoveryDecision.WRAP)

    def test_height_overflow_wraps_first(self):
        d = decide_recovery(
            "toc_title", OverflowReason.HEIGHT, budget=budget_for_kind("toc_title")
        )
        self.assertIs(d, RecoveryDecision.WRAP)

    def test_wrap_disabled_then_shrink(self):
        b = LayoutBudget(allow_wrap=False, allow_shrink=True, allow_clip=True)
        d = decide_recovery("toc_title", OverflowReason.WIDTH, budget=b)
        # never CLIP even though the budget allows it — the kind wins
        self.assertIs(d, RecoveryDecision.SHRINK)

    def test_shrink_disabled_then_preserve(self):
        b = LayoutBudget(allow_wrap=False, allow_shrink=False, allow_clip=True)
        d = decide_recovery("toc_title", OverflowReason.WIDTH, budget=b)
        self.assertIs(d, RecoveryDecision.PRESERVE_OVERFLOW)

    def test_unbreakable_token_skips_wrap_goes_shrink(self):
        d = decide_recovery(
            "toc_title",
            OverflowReason.UNBREAKABLE_TOKEN,
            budget=budget_for_kind("toc_title"),
        )
        self.assertIs(d, RecoveryDecision.SHRINK)

    def test_clip_never_returned_for_toc_title_any_reason(self):
        for reason in OverflowReason:
            for budget in (
                budget_for_kind("toc_title"),
                LayoutBudget(allow_wrap=True, allow_shrink=True, allow_clip=True),
            ):
                d = decide_recovery("toc_title", reason, budget=budget)
                self.assertIsNot(d, RecoveryDecision.CLIP, f"reason={reason}")

    def test_toc_alias_same_ladder(self):
        d = decide_recovery("toc", OverflowReason.WIDTH, budget=budget_for_kind("toc"))
        self.assertIs(d, RecoveryDecision.WRAP)


# ---------------------------------------------------------------------------
# 3. immovable channels — page column / marker never move
# ---------------------------------------------------------------------------


class TestImmovableChannels(unittest.TestCase):
    def test_page_column_always_preserve(self):
        for reason in OverflowReason:
            d = decide_recovery("column", reason)
            self.assertIs(d, RecoveryDecision.PRESERVE_OVERFLOW, reason)

    def test_marker_target_always_preserve(self):
        for reason in OverflowReason:
            d = decide_recovery("anchor", reason, target="marker")
            self.assertIs(d, RecoveryDecision.PRESERVE_OVERFLOW, reason)


# ---------------------------------------------------------------------------
# 4. executor-level: adaptive_layout(target="title") never clips
# ---------------------------------------------------------------------------


class TestAdaptiveTocTitleNeverClips(unittest.TestCase):
    def test_long_title_wraps_without_clip(self):
        r = adaptive_layout(
            _title("A moderately long translated title that still fits"),
            measure=_measure,
            avail_width=424.0,
            avail_height=400.0,
            font_size=10.0,
            budget=budget_for_kind("toc_title"),
            target="title",
        )
        self.assertIsInstance(r, LayoutResult)
        self.assertFalse(r.overflow)
        self.assertNotIn("CLIP", r.recovery_steps)

    def test_extreme_title_clip_forbidden_even_with_clip_budget(self):
        """Defense in depth: even a clip-allowing budget must not CLIP a title."""
        b = LayoutBudget(allow_wrap=True, allow_shrink=True, allow_clip=True)
        r = adaptive_layout(
            _title("VeryLong" * 60),
            measure=_measure,
            avail_width=20.0,
            avail_height=10.0,
            font_size=10.0,
            budget=b,
            target="title",
        )
        self.assertTrue(r.overflow)
        self.assertNotIn("CLIP", r.recovery_steps)
        # exhausted budget → honest PRESERVE_OVERFLOW, not a stale wrap/shrink
        self.assertEqual(r.recovery_decision, "preserve_overflow")

    def test_title_without_target_still_obeys_toc_budget(self):
        b = budget_for_kind("toc_title")
        r = adaptive_layout(
            _title("VeryLong" * 60),
            measure=_measure,
            avail_width=20.0,
            avail_height=10.0,
            font_size=10.0,
            budget=b,
        )
        self.assertTrue(r.overflow)
        self.assertNotIn("CLIP", r.recovery_steps)
        self.assertEqual(r.recovery_decision, "preserve_overflow")

    def test_page_column_anchor_never_moves(self):
        """FixedColumn at page_x stays put even when the title overflows."""
        page = FixedColumn(text="42", column_x=500.0, y=700.0)
        r = adaptive_layout(
            page,
            measure=_measure,
            avail_width=20.0,
            avail_height=10.0,
            font_size=10.0,
            budget=budget_for_kind("column"),
        )
        # PRESERVE path keeps geometry, reports overflow explicitly
        self.assertEqual(r.bbox[0], 500.0)


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__]))
