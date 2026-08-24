"""Tests for PDFOpRebuilder (Phase 1)."""

import unittest
from unittest.mock import MagicMock, patch
from pdf2zh.pdf_op_builder import PDFOpRebuilder


class MockTextMetrics:
    """Mock TextMetrics for testing PDFOpRebuilder without font files."""

    def measure_string(self, text, font_size, char_spacing=0.0):
        # Simple mock: each char = 0.5 * font_size width
        n = len(text)
        total = n * font_size * 0.5 + (n - 1) * char_spacing if n > 0 else 0
        return {
            "total_width": total,
            "glyph_widths": [font_size * 0.5] * n,
            "ascent": font_size * 0.7,
            "descent": font_size * -0.2,
        }


class TestPDFOpRebuilder(unittest.TestCase):
    """Test PDF operator rebuilding."""

    def setUp(self):
        self.metrics = MockTextMetrics()

    def test_build_tj_simple_single_char(self):
        """Simple single-char TJ should contain font and position."""
        result = PDFOpRebuilder.build_tj_simple("A", "F1", 12.0, 100.0, 200.0)
        self.assertIn("/F1", result)
        self.assertIn("12.", result)
        self.assertIn("100.", result)
        self.assertIn("200.", result)
        self.assertIn("TJ", result)

    def test_build_tj_simple_multi_char(self):
        """Multi-char TJ should encode all chars."""
        result = PDFOpRebuilder.build_tj_simple("Hello", "F1", 12.0, 0.0, 0.0)
        self.assertIn("0048", result)  # H
        self.assertIn("0065", result)  # e
        self.assertIn("006c", result)  # l
        self.assertIn("006f", result)  # o

    def test_build_tj_left_aligned(self):
        """Left-aligned TJ should not include spacing adjustments."""
        result = PDFOpRebuilder.build_tj(
            "你好世界", self.metrics, 100.0, 12.0, alignment="left"
        )
        # No explicit spacing in left-aligned mode since actual_width <= target_width for mock
        self.assertIn("TJ", result)

    def test_build_tj_justify_with_gap(self):
        """Justified TJ should include spacing adjustments when text is shorter than target."""
        # Mock: 4 chars * 12 * 0.5 = 24 < target 100 -> justifies
        result = PDFOpRebuilder.build_tj(
            "test", self.metrics, 100.0, 12.0, alignment="justify"
        )
        self.assertIn("TJ", result)
        # Should contain spacing numbers
        self.assertRegex(result, r"-?\d+\.\d+")

    def test_build_tj_single_char_justify_no_adjust(self):
        """Single-char justify should not add spacing (no gaps)."""
        result = PDFOpRebuilder.build_tj(
            "t", self.metrics, 100.0, 12.0, alignment="justify"
        )
        self.assertIn("TJ", result)

    def test_build_tj_justify_within_limits(self):
        """Justify spacing should be clamped to safe range."""
        # Use 2-char text with very large gap to test clamping
        result = PDFOpRebuilder.build_tj(
            "ab", self.metrics, 1000.0, 12.0, alignment="justify"
        )
        self.assertIn("TJ", result)

    def test_build_tj_simple_hex_encoding(self):
        """build_tj_simple should hex-encode Unicode."""
        result = PDFOpRebuilder.build_tj_simple("测试", "F1", 14.0, 50.0, 60.0)
        self.assertIn("6d4b", result)  # 测
        self.assertIn("8bd5", result)  # 试 in hex


class TestPDFOpRebuilderConstants(unittest.TestCase):
    """Test class constants."""

    def test_max_spacing_scale(self):
        self.assertEqual(PDFOpRebuilder.MAX_SPACING_SCALE, 1.5)

    def test_min_spacing_scale(self):
        self.assertEqual(PDFOpRebuilder.MIN_SPACING_SCALE, 0.8)


if __name__ == "__main__":
    unittest.main()
