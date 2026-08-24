"""Tests for DocumentFontCache (Phase 1)."""

import unittest
from unittest.mock import MagicMock, patch
from unittest.mock import patch, MagicMock
from pdf2zh.font_cache import DocumentFontCache


class TestDocumentFontCache(unittest.TestCase):
    """Test document-level font caching."""

    def setUp(self):
        self.mock_doc = MagicMock()
        self.mock_doc.xref_length.return_value = 5
        patcher = patch("pdf2zh.font_cache.Font")
        self.mock_font = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_font.return_value = MagicMock()
        self.cache = DocumentFontCache(self.mock_doc)

    def test_register_returns_string(self):
        """register() should return a string font name."""
        font_name = self.cache.register("/fake/path/font.ttf")
        self.assertIsInstance(font_name, str)

    def test_register_same_font_returns_same_name(self):
        """Registering same font path twice should return same name."""
        name1 = self.cache.register("/fake/path/font.ttf")
        name2 = self.cache.register("/fake/path/font.ttf")
        self.assertEqual(name1, name2)

    def test_register_different_fonts_different_names(self):
        """Registering different fonts should return different names."""
        name1 = self.cache.register("/fake/path/font1.ttf")
        name2 = self.cache.register("/fake/path/font2.ttf")
        self.assertNotEqual(name1, name2)

    def test_get_font_registered(self):
        """get_font() should return Font for registered path."""
        path = "/fake/path/font.ttf"
        self.cache.register(path)
        font = self.cache.get_font(path)
        # Should be None since our Font constructor will fail with fake path
        # But the method should not crash
        self.assertIsNotNone(font)

    def test_get_font_unregistered(self):
        """get_font() should return None for unregistered path."""
        result = self.cache.get_font("/unregistered/path.ttf")
        self.assertIsNone(result)

    def test_get_name_registered(self):
        """get_name() should return the short name for registered path."""
        path = "/fake/path/font.ttf"
        name = self.cache.register(path)
        retrieved = self.cache.get_name(path)
        self.assertEqual(name, retrieved)

    def test_get_name_unregistered(self):
        """get_name() should return None for unregistered path."""
        result = self.cache.get_name("/unregistered.ttf")
        self.assertIsNone(result)

    def test_count_starts_at_zero(self):
        """New cache should have count=0."""
        cache = DocumentFontCache(self.mock_doc)
        self.assertEqual(cache.count, 0)

    def test_count_increments(self):
        """Count should increase with each unique registration."""
        self.cache.register("/path/a.ttf")
        self.assertEqual(self.cache.count, 1)
        self.cache.register("/path/b.ttf")
        self.assertEqual(self.cache.count, 2)

    def test_count_does_not_increment_on_duplicate(self):
        """Count should not increase for duplicate registrations."""
        self.cache.register("/path/a.ttf")
        self.cache.register("/path/a.ttf")
        self.assertEqual(self.cache.count, 1)

    def test_get_registered_fonts(self):
        """get_registered_fonts() should return list of tuples."""
        self.cache.register("/path/a.ttf")
        fonts = self.cache.get_registered_fonts()
        self.assertEqual(len(fonts), 1)
        font_name, font_obj, font_path = fonts[0]
        self.assertEqual(font_path, "/path/a.ttf")


if __name__ == "__main__":
    unittest.main()
