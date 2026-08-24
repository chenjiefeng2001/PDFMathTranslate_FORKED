"""
PDF operator builder for pdf2zh 2.0.

Reconstructs TJ/Tj instruction streams for Chinese/CJK text,
replacing the old fixed-spacing approach with proper character
spacing that supports both left-aligned and justified alignment.
"""

import logging
from typing import Dict, List, Optional

from pdf2zh.text_metrics import TextMetrics

logger = logging.getLogger(__name__)


class PDFOpRebuilder:
    """Rebuild PDF text rendering operators with proper CJK spacing.

    Key improvements over 1.x:
    - Fine-grained per-character spacing (Tc) in TJ arrays
    - Justified alignment support via gap-evenly-distributed spacing
    - Safety limits to prevent excessive stretching/compression
    """

    MAX_SPACING_SCALE = 1.5  # Maximum stretch factor
    MIN_SPACING_SCALE = 0.8  # Minimum compression factor

    @classmethod
    def build_tj(
        cls,
        text: str,
        metrics: TextMetrics,
        target_width: float,
        font_size: float,
        alignment: str = "left",
    ) -> str:
        """Build a PDF TJ instruction array for CJK text.

        Args:
            text: Unicode text to render
            metrics: TextMetrics instance for the output font
            target_width: Target bounding box width in points
            font_size: Font size in points
            alignment: 'left' or 'justify'

        Returns:
            PDF TJ instruction string, e.g.:
                [(<5b57>) -12.34 (<7b2c>) ...] TJ
        """
        measured = metrics.measure_string(text, font_size)
        actual_width = measured["total_width"]

        # Calculate spacing adjustment for justified alignment
        tj_adj = 0.0
        if alignment == "justify" and len(text) > 1 and target_width > actual_width:
            gap = target_width - actual_width
            offset_per_gap = gap / (len(text) - 1)
            # PDF TJ offset: 1000 text space units = 1 em
            # Negative value = increase spacing (move text apart)
            tj_adj = -(offset_per_gap / font_size) * 1000.0
            # Clamp to safety limits
            max_adj = -1000.0 * (1.0 - cls.MAX_SPACING_SCALE)
            min_adj = -1000.0 * (1.0 - cls.MIN_SPACING_SCALE)
            tj_adj = max(tj_adj, max_adj)  # Most negative = most stretch
            tj_adj = min(tj_adj, min_adj)  # Least negative = least stretch

        # Build TJ array
        parts: List[str] = []
        for i, ch in enumerate(text):
            # Encode char as hex string
            hex_str = f"{ord(ch):04x}"
            parts.append(f"<{hex_str}>")
            if tj_adj != 0.0 and i < len(text) - 1:
                parts.append(f"{tj_adj:.2f}")

        return f"[{' '.join(parts)}] TJ"

    @classmethod
    def build_tj_simple(
        cls, text: str, font_id: str, font_size: float, x: float, y: float
    ) -> str:
        """Build a simple single-char TJ instruction (backward compatible).

        Used for non-CJK text or fallback cases where simple
        positioning is sufficient.
        """
        hex_str = "".join(f"{ord(ch):04x}" for ch in text)
        return (
            f"/{font_id} {font_size:.6f} Tf 1 0 0 1 {x:.6f} {y:.6f} Tm "
            f"[<{hex_str}>] TJ "
        )
