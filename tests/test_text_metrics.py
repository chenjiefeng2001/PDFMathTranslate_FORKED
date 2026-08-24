"""Tests for TextMetrics (Phase 1).

These tests require a real font file. If no font file is found,
the tests are skipped gracefully.
"""

import os
import unittest
import tempfile
from pathlib import Path
from pdf2zh.text_metrics import TextMetrics


def _find_font() -> str:
    """Find a usable font file for testing."""
    # Try common font paths
    candidates = [
        # Windows fonts
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/times.ttf",
        "C:/Windows/Fonts/consola.ttf",
        # Linux fonts
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    # As a last resort, try to find any .ttf in Windows fonts
    win_fonts = "C:/Windows/Fonts"
    if os.path.exists(win_fonts):
        for f in os.listdir(win_fonts):
            if f.endswith(".ttf") or f.endswith(".ttc"):
                return os.path.join(win_fonts, f)
    return ""


def _has_font() -> bool:
    return bool(_find_font())


class TestTextMetrics(unittest.TestCase):
    """Test physical text measurement."""

    @classmethod
    def setUpClass(cls):
        cls.font_path = _find_font()
        if not cls.font_path:
            raise unittest.SkipTest("No font file found for testing")

    def setUp(self):
        self.metrics = TextMetrics(self.font_path)

    def tearDown(self):
        self.metrics.close()

    def test_measure_string_returns_dict(self):
        """measure_string should return a dict with expected keys."""
        result = self.metrics.measure_string("Hello", 12.0)
        self.assertIn("total_width", result)
        self.assertIn("glyph_widths", result)
        self.assertIn("ascent", result)
        self.assertIn("descent", result)

    def test_total_width_positive(self):
        """Total width should be positive for non-empty text."""
        result = self.metrics.measure_string("Hello World", 12.0)
        self.assertGreater(result["total_width"], 0)

    def test_glyph_widths_count(self):
        """Number of glyph widths should match text length."""
        text = "Test123"
        result = self.metrics.measure_string(text, 12.0)
        self.assertEqual(len(result["glyph_widths"]), len(text))

    def test_empty_string(self):
        """Empty string should produce zero width."""
        result = self.metrics.measure_string("", 12.0)
        self.assertEqual(result["total_width"], 0.0)
        self.assertEqual(len(result["glyph_widths"]), 0)

    def test_larger_font_larger_width(self):
        """Larger font size should produce larger widths."""
        result_12 = self.metrics.measure_string("Hello", 12.0)
        result_24 = self.metrics.measure_string("Hello", 24.0)
        self.assertGreater(result_24["total_width"], result_12["total_width"])

    def test_char_width_positive(self):
        """char_width should return positive value."""
        width = self.metrics.char_width("A", 12.0)
        self.assertGreater(width, 0)

    def test_char_width_empty_string_zero(self):
        """char_width with empty string should not crash (maps to GID 0)."""
        width = self.metrics.char_width("", 12.0)
        self.assertGreaterEqual(width, 0)

    def test_ascent_positive(self):
        """Ascent should be positive."""
        result = self.metrics.measure_string("Hello", 12.0)
        self.assertGreater(result["ascent"], 0)

    def test_descent_non_positive(self):
        """Descent should be non-positive (below baseline)."""
        result = self.metrics.measure_string("Hello", 12.0)
        self.assertLessEqual(result["descent"], 0)

    def test_scaled_metrics(self):
        """Ascent/descent should scale with font size."""
        result_12 = self.metrics.measure_string("Hello", 12.0)
        result_24 = self.metrics.measure_string("Hello", 24.0)
        self.assertAlmostEqual(
            result_24["ascent"] / result_12["ascent"], 2.0, delta=0.01
        )

    def test_char_spacing_adds_width(self):
        """Adding char spacing should increase total width."""
        without = self.metrics.measure_string("Hello", 12.0, char_spacing=0.0)
        with_spacing = self.metrics.measure_string("Hello", 12.0, char_spacing=2.0)
        self.assertGreater(with_spacing["total_width"], without["total_width"])


@unittest.skipIf(not _has_font(), "No font file available")
class TestTextMetricsCJK(unittest.TestCase):
    """Test TextMetrics with CJK characters (uses fallback GID 0)."""

    @classmethod
    def setUpClass(cls):
        cls.font_path = _find_font()

    def test_cjk_character_does_not_crash(self):
        """CJK characters should not cause errors (maps to GID 0)."""
        metrics = TextMetrics(self.font_path)
        result = metrics.measure_string("你好世界", 12.0)
        self.assertGreaterEqual(result["total_width"], 0)
        metrics.close()


if __name__ == "__main__":
    unittest.main()
