"""Tests for OverlayRenderer (Phase 4)."""
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
from pdf2zh.overlay_renderer import OverlayRenderer, OverlaySegment, composite_overlay


class TestOverlaySegment(unittest.TestCase):
    """Test OverlaySegment data class."""

    def test_default_opacity(self):
        seg = OverlaySegment(
            text="Hello",
            bbox=(0, 0, 100, 20),
            font_size=12.0,
        )
        self.assertEqual(seg.opacity, 0.85)

    def test_custom_opacity(self):
        seg = OverlaySegment(
            text="Hello",
            bbox=(0, 0, 100, 20),
            font_size=12.0,
            opacity=0.5,
        )
        self.assertEqual(seg.opacity, 0.5)


class MockDoc:
    def new_page(self, width=0, height=0):
        return MagicMock()
    def write(self, **kwargs):
        return b"%PDF-1.7 overlay"

class TestOverlayRenderer(unittest.TestCase):
    """Test overlay rendering logic."""

    def setUp(self):
        self.renderer = OverlayRenderer(dpi=300)
        self.mock_page = MagicMock()
        self.mock_page.rect.width = 612.0  # US Letter
        self.mock_page.rect.height = 792.0
        self.patcher = patch("pdf2zh.overlay_renderer.Document", return_value=MockDoc())
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_render_overlay_returns_bytes(self):
        """render_overlay should return bytes."""
        segments = [
            OverlaySegment(
                text="Hello World",
                bbox=(100, 100, 300, 120),
                font_size=12.0,
            )
        ]
        with patch("pymupdf.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_page = MagicMock()
            mock_doc_cls.return_value = mock_doc
            mock_doc.new_page.return_value = mock_page
            mock_doc.write.return_value = b"%PDF-1.7 overlay"

            result = self.renderer.render_overlay(
                self.mock_page, segments
            )
            self.assertIsInstance(result, bytes)
            self.assertIn(b"overlay", result)

    def test_empty_segments_returns_valid_pdf(self):
        """Empty segments should produce valid PDF bytes."""
        with patch("pymupdf.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_page = MagicMock()
            mock_doc_cls.return_value = mock_doc
            mock_doc.new_page.return_value = mock_page
            mock_doc.write.return_value = b"%PDF-1.7 empty"

            result = self.renderer.render_overlay(
                self.mock_page, []
            )
            self.assertIsInstance(result, bytes)

    def test_whitespace_segment_skipped(self):
        """Whitespace-only segments should be skipped."""
        segments = [
            OverlaySegment(
                text="   ",
                bbox=(0, 0, 100, 20),
                font_size=12.0,
            )
        ]
        with patch("pymupdf.Document") as mock_doc_cls:
            mock_doc = MagicMock()
            mock_page = MagicMock()
            mock_doc_cls.return_value = mock_doc
            mock_doc.new_page.return_value = mock_page
            mock_doc.write.return_value = b"%PDF-1.7 whitespace"

            result = self.renderer.render_overlay(
                self.mock_page, segments
            )
            self.assertIsInstance(result, bytes)

    def test_dpi_scale(self):
        """DPI should map to correct scale factor."""
        renderer_72 = OverlayRenderer(dpi=72)
        self.assertAlmostEqual(renderer_72.scale, 1.0)

        renderer_300 = OverlayRenderer(dpi=300)
        self.assertAlmostEqual(renderer_300.scale, 300.0 / 72.0)


class TestCompositeOverlay(unittest.TestCase):
    """Test image compositing."""

    def setUp(self):
        self.orig = np.ones((100, 200, 3), dtype=np.uint8) * 255
        self.overlay = np.ones((100, 200, 3), dtype=np.uint8) * 200

    def test_composite_returns_same_shape(self):
        result = composite_overlay(self.orig, self.overlay, alpha=0.15)
        self.assertEqual(result.shape, self.orig.shape)

    def test_composite_dtype_uint8(self):
        result = composite_overlay(self.orig, self.overlay, alpha=0.15)
        self.assertEqual(result.dtype, np.uint8)

    def test_composite_with_alpha_0(self):
        """Alpha=0 should return original image unchanged."""
        result = composite_overlay(self.orig, self.overlay, alpha=0.0)
        np.testing.assert_array_equal(result, self.orig)

    def test_composite_with_alpha_1(self):
        """Alpha=1 (and content) should return overlay where different."""
        result = composite_overlay(self.orig, self.overlay, alpha=1.0)
        # With alpha=1 and content different, result should differ
        self.assertTrue(np.any(result != 255))

    def test_composite_different_sizes_handled(self):
        """Different sized images should be handled gracefully."""
        overlay_small = np.ones((50, 100, 3), dtype=np.uint8) * 200
        result = composite_overlay(self.orig, overlay_small, alpha=0.5)
        self.assertEqual(result.shape, self.orig.shape)


if __name__ == "__main__":
    unittest.main()
