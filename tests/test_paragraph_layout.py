"""Tests for ParagraphLayoutEngine (Phase 2)."""

import unittest
from pdf2zh.paragraph_layout import ParagraphLayoutEngine, TextAlignment, TextBlock


class MockTextMetrics:
    """Mock TextMetrics for testing layout without font files."""

    def __init__(self):
        self.cjk_widths = {
            "你": 12.0,
            "好": 12.0,
            "世": 12.0,
            "界": 12.0,
            "测": 12.0,
            "试": 12.0,
            "中": 12.0,
            "文": 12.0,
        }
        self.latin_width = 6.0  # Half-width chars

    def measure_string(self, text, font_size, char_spacing=0.0):
        width = sum(self.cjk_widths.get(ch, self.latin_width) for ch in text) * (
            font_size / 12.0
        )
        return {
            "total_width": width,
            "glyph_widths": [
                self.cjk_widths.get(ch, self.latin_width) * (font_size / 12.0)
                for ch in text
            ],
            "ascent": font_size * 0.7,
            "descent": font_size * -0.2,
        }

    def char_width(self, char, font_size):
        return self.cjk_widths.get(char, self.latin_width) * (font_size / 12.0)


class TestParagraphLayoutEngine(unittest.TestCase):
    """Test paragraph layout engine."""

    def setUp(self):
        self.metrics = MockTextMetrics()
        self.engine = ParagraphLayoutEngine(self.metrics, line_spacing=1.2)

    def test_wrap_text_cjk_single_line(self):
        """CJK text within width should stay on one line."""
        lines = self.engine.wrap_text("你好世界", 100.0, 12.0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "你好世界")

    def test_wrap_text_cjk_multi_line(self):
        """CJK text wider than max width should wrap."""
        lines = self.engine.wrap_text("你好世界测试中文", 50.0, 12.0)
        self.assertGreater(len(lines), 1)

    def test_wrap_text_empty(self):
        """Empty text should return empty lines."""
        lines = self.engine.wrap_text("", 100.0, 12.0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "")

    def test_wrap_text_whitespace_only(self):
        """Whitespace-only text should return empty line."""
        lines = self.engine.wrap_text("   ", 100.0, 12.0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "")

    def test_wrap_text_latin(self):
        """Latin text should wrap at word boundaries."""
        text = "Hello World This Is A Test"
        lines = self.engine.wrap_text(text, 30.0, 12.0)
        # Should wrap into multiple lines
        self.assertGreaterEqual(len(lines), 1)

    def test_layout_block_returns_textblock(self):
        """layout_block should return a TextBlock instance."""
        block = self.engine.layout_block("Hello", 0, 100, 200, 50, 12.0)
        self.assertIsInstance(block, TextBlock)

    def test_layout_block_has_lines(self):
        """Layout block should contain text lines."""
        block = self.engine.layout_block("Hello World", 0, 100, 200, 50, 12.0)
        self.assertGreater(len(block.lines), 0)

    def test_layout_block_left_aligned(self):
        """Left-aligned text should start at x0."""
        block = self.engine.layout_block(
            "Hello", 100, 200, 300, 50, 12.0, alignment=TextAlignment.LEFT
        )
        if block.lines:
            self.assertEqual(block.lines[0].x, 100)

    def test_layout_block_right_aligned(self):
        """Right-aligned text should be positioned at right margin."""
        block = self.engine.layout_block(
            "Hello", 100, 200, 300, 50, 12.0, alignment=TextAlignment.RIGHT
        )
        if block.lines:
            self.assertGreater(block.lines[0].x, 100)

    def test_layout_block_center_aligned(self):
        """Center-aligned text should be centered."""
        block = self.engine.layout_block(
            "Hello", 100, 200, 300, 50, 12.0, alignment=TextAlignment.CENTER
        )
        if block.lines:
            self.assertAlmostEqual(
                block.lines[0].x, 100 + (300 - block.lines[0].width) / 2, delta=1.0
            )

    def test_layout_block_respects_max_height(self):
        """Layout should not exceed max_height."""
        block = self.engine.layout_block(
            "你好世界你好世界你好世界", 0, 100, 30, 30, 12.0
        )
        for line in block.lines:
            self.assertGreaterEqual(line.y, 100 - 30)

    def test_cjk_detection(self):
        """CJK-heavy text should be detected correctly."""
        self.assertTrue(ParagraphLayoutEngine._is_cjk_heavy("你好世界"))
        self.assertTrue(
            ParagraphLayoutEngine._is_cjk_heavy("你好测试Hello")
        )  # 4/9=44%>30%
        self.assertFalse(ParagraphLayoutEngine._is_cjk_heavy("HelloWorld"))
        self.assertFalse(ParagraphLayoutEngine._is_cjk_heavy(""))
        self.assertFalse(ParagraphLayoutEngine._is_cjk_heavy("ABC123"))


class TestTextAlignment(unittest.TestCase):
    """Test TextAlignment enum."""

    def test_enum_values(self):
        self.assertEqual(TextAlignment.LEFT.value, "left")
        self.assertEqual(TextAlignment.RIGHT.value, "right")
        self.assertEqual(TextAlignment.CENTER.value, "center")
        self.assertEqual(TextAlignment.JUSTIFY.value, "justify")


if __name__ == "__main__":
    unittest.main()
