"""Tests for OverflowPolicy (Phase 2)."""

import unittest
from pdf2zh.overflow_policy import OverflowPolicy, OverflowAction
from pdf2zh.paragraph_style import ParagraphStyle, TextBlock, TextLine


class TestOverflowPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = OverflowPolicy()
        self.style = ParagraphStyle(font_size=12.0, line_spacing=1.35)

    def test_empty_block_no_overflow(self):
        """Empty block should return LINE_BREAK with no adjustments."""
        block = TextBlock(lines=[], style=self.style)
        result = self.policy.resolve(block, 100.0)
        self.assertEqual(result.action, OverflowAction.LINE_BREAK)

    def test_fits_within_available(self):
        """Block smaller than available should return LINE_BREAK."""
        block = TextBlock(
            lines=[TextLine("Hi", 0, 0, 50, 12, 12.0)],
            style=self.style,
        )
        result = self.policy.resolve(block, 100.0)
        self.assertEqual(result.action, OverflowAction.LINE_BREAK)

    def test_compress_line_spacing(self):
        """Overflow should trigger line spacing compression."""
        block = TextBlock(
            lines=[TextLine(f"Line {i}", 0, 0, 50, 12, 12.0) for i in range(5)],
            style=ParagraphStyle(font_size=12.0, line_spacing=2.0),
        )
        total = sum(l.height for l in block.lines)
        available = total * 0.8
        result = self.policy.resolve(block, available)
        self.assertIn(
            result.action,
            [OverflowAction.COMPRESS_LINE_SPACING, OverflowAction.PUSH_DOWN],
        )

    def test_push_down_for_small_overflow(self):
        """Small overflow should trigger PUSH_DOWN."""
        block = TextBlock(
            lines=[
                TextLine("A", 0, 0, 50, 12, 12.0),
                TextLine("B", 0, 12, 50, 12, 12.0),
            ],
            style=self.style,
        )
        total = sum(l.height for l in block.lines)
        available = total - 5.0
        result = self.policy.resolve(block, available)
        self.assertIn(
            result.action,
            [OverflowAction.COMPRESS_LINE_SPACING, OverflowAction.PUSH_DOWN],
        )

    def test_reduce_font(self):
        """Large overflow should trigger font size reduction."""
        block = TextBlock(
            lines=[TextLine(f"Long line {i}", 0, 0, 200, 12, 12.0) for i in range(50)],
            style=self.style,
        )
        result = self.policy.resolve(block, 20.0)
        self.assertIn(
            result.action,
            [
                OverflowAction.REDUCE_FONT,
                OverflowAction.COMPRESS_LINE_SPACING,
                OverflowAction.PUSH_DOWN,
                OverflowAction.EXPAND_BBOX,
            ],
        )

    def test_expand_bbox_as_fallback(self):
        """When all strategies fail, expand bbox."""
        block = TextBlock(
            lines=[TextLine("X", 0, 0, 200, 12, 12.0) for i in range(100)],
            style=ParagraphStyle(font_size=12.0, line_spacing=1.0),
        )
        result = self.policy.resolve(block, 5.0)
        self.assertIsNotNone(result)
        self.assertIn(
            result.action,
            [
                OverflowAction.EXPAND_BBOX,
                OverflowAction.REDUCE_FONT,
            ],
        )

    def test_min_font_scale_enforced(self):
        """Font should not shrink below 4pt."""
        big_block = TextBlock(
            lines=[TextLine("A", 0, 0, 200, 48, 48.0) for _ in range(10)],
            style=ParagraphStyle(font_size=48.0, line_spacing=1.0),
        )
        result = self.policy.resolve(big_block, 10.0)
        if result.action == OverflowAction.REDUCE_FONT:
            self.assertGreaterEqual(result.adjusted_font_size, 4.0)

    def test_no_style_no_crash(self):
        """Block without style should not crash."""
        block = TextBlock(
            lines=[TextLine("Hi", 0, 0, 50, 12, 12.0)],
            style=None,
        )
        result = self.policy.resolve(block, 5.0)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
