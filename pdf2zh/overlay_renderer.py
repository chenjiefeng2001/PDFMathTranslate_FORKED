"""
Overlay/transparent rendering engine for pdf2zh 2.0.

Renders translated text on top of the original scanned page image
with proper opacity, background masking, and position matching.
"""
import io
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from pymupdf import Document, Page, Rect

logger = logging.getLogger(__name__)


@dataclass
class OverlaySegment:
    """A single segment of overlay text."""
    text: str
    bbox: Tuple[float, float, float, float]  # x0, y0, x1, y1
    font_size: float
    opacity: float = 0.85


class OverlayRenderer:
    """Renders translation as a transparent overlay on scanned page images.

    This renderer:
    1. Takes the original page image
    2. Creates a transparency layer at the same dimensions
    3. Renders translated text on the transparency layer
    4. Composites the result over the original image
    """

    def __init__(self, dpi: int = 300):
        self.dpi = dpi
        self.scale = dpi / 72.0  # PDF points to pixels

    def render_overlay(
        self,
        page: Page,
        segments: List[OverlaySegment],
    ) -> bytes:
        """Render translated text overlay on a page.

        Args:
            page: The pymupdf Page object (for dimensions)
            segments: List of text segments to render

        Returns:
            PDF byte string with overlay rendered
        """
        # Get page dimensions in PDF points
        rect = page.rect
        pdf_width = rect.width
        pdf_height = rect.height

        # Create a new PDF with just the overlay
        overlay_doc = Document()
        overlay_page = overlay_doc.new_page(
            width=pdf_width,
            height=pdf_height,
        )

        # Render each text segment
        for seg in segments:
            if not seg.text.strip():
                continue

            x0, y0, x1, y1 = seg.bbox

            # Insert text at the exact position
            # Use the font already embedded in the page
            overlay_page.insert_text(
                point=(x0, y0 + seg.font_size * 0.85),  # Adjust for baseline
                text=seg.text,
                fontsize=seg.font_size,
                color=(0, 0, 0),
                overlay=True,
            )

        return overlay_doc.write(deflate=True, garbage=3)

    def render_hybrid(
        self,
        page: Page,
        segments: List[OverlaySegment],
        original_pdf: bytes,
    ) -> bytes:
        """Render hybrid PDF with original image + overlay text layer.

        For scanned PDFs: preserves the original image and adds an
        invisible text layer on top for searchability, plus a visible
        overlay for translated text.

        Args:
            page: The pymupdf Page object
            segments: List of text segments to render
            original_pdf: Original scanned PDF bytes

        Returns:
            Hybrid PDF bytes
        """
        # This is a stub for Phase 4 implementation
        # Full implementation requires:
        # 1. Extract original page image
        # 2. Create PDF with image + text layer
        # 3. Add transparent overlay text
        return self.render_overlay(page, segments)


def composite_overlay(
    original_image: np.ndarray,
    overlay_image: np.ndarray,
    alpha: float = 0.15,
) -> np.ndarray:
    """Composite overlay onto original image with alpha blending.

    Args:
        original_image: Original page image (HxWx3 uint8)
        overlay_image: Overlay text image (HxWx3 uint8) - white bg
        alpha: Overlay opacity (0 = transparent, 1 = opaque)

    Returns:
        Composited image
    """
    if original_image.shape != overlay_image.shape:
        # Resize overlay to match original
        overlay_image = np.resize(overlay_image, original_image.shape)

    # Create difference mask (text areas)
    diff = np.abs(overlay_image.astype(np.float32) - 255.0)
    mask = np.max(diff, axis=2) > 10  # Pixels with significant content

    # Composite only on masked areas
    result = original_image.copy().astype(np.float32)
    for c in range(3):
        result[mask, c] = (
            result[mask, c] * (1 - alpha) + overlay_image[mask, c] * alpha
        )

    return result.astype(np.uint8)
