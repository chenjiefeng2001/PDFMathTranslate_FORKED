"""
Real physical text measurement engine for pdf2zh 2.0.

Uses fontTools to measure glyph metrics directly from the font file,
enabling accurate text width calculation for CJK and Latin text.
"""
import logging
from typing import Dict, List, Optional

from fontTools.ttLib import TTFont

logger = logging.getLogger(__name__)


class TextMetrics:
    """Physical text measurement using fontTools glyph metrics.

    Replaces the old `len(text) * fontsize * 0.8` estimation approach
    with real glyph advance width measurements from the font file.
    """

    def __init__(self, font_path: str):
        self.ttfont = TTFont(font_path)
        self.cmap = self.ttfont.getBestCmap()
        self.hmtx = self.ttfont["hmtx"]
        self.upem = self.ttfont["head"].unitsPerEm
        self.ascent = self.ttfont["hhea"].ascent / self.upem
        self.descent = self.ttfont["hhea"].descent / self.upem
        self._validate_metrics()
        logger.debug(
            "TextMetrics initialized: unitsPerEm=%d, ascent=%.4f, descent=%.4f",
            self.upem,
            self.ascent,
            self.descent,
        )

    def _validate_metrics(self):
        """Validate font metrics sanity; log warnings for abnormal values."""
        if self.upem <= 0 or self.upem > 10000:
            logger.warning(
                "Unusual unitsPerEm=%d; measurements may be inaccurate", self.upem
            )
        if not self.cmap:
            logger.warning("Font has no character map (cmap); all chars map to GID 0")

    def measure_string(
        self, text: str, font_size: float, char_spacing: float = 0.0
    ) -> Dict[str, object]:
        """Measure the physical width of a text string.

        Args:
            text: Unicode text to measure
            font_size: Font size in points
            char_spacing: Additional character spacing in points

        Returns:
            dict with keys:
                - total_width (float): total advance width in points
                - glyph_widths (List[float]): per-glyph widths
                - ascent (float): font ascent in points
                - descent (float): font descent in points
        """
        total_width = 0.0
        widths: List[float] = []

        for ch in text:
            glyph_name = self.cmap.get(ord(ch))
            if glyph_name is None:
                glyph_name = self.ttfont.getGlyphName(0)
            try:
                advance, _ = self.hmtx[glyph_name]
            except KeyError:
                advance = self.upem // 2
            char_width = (advance / self.upem) * font_size
            widths.append(char_width)
            total_width += char_width + char_spacing

        return {
            "total_width": total_width,
            "glyph_widths": widths,
            "ascent": self.ascent * font_size,
            "descent": self.descent * font_size,
        }

    def char_width(self, char: str, font_size: float) -> float:
        """Measure width of a single character.

        Args:
            char: Single Unicode character
            font_size: Font size in points

        Returns:
            Advance width in points
        """
        if not char:
            return 0.0
        try:
            glyph_id_or_name = self.cmap.get(ord(char))
            if glyph_id_or_name is None:
                glyph_id_or_name = self.ttfont.getGlyphName(0)
            if isinstance(glyph_id_or_name, int):
                glyph_name = self.ttfont.getGlyphName(glyph_id_or_name)
            elif isinstance(glyph_id_or_name, str):
                glyph_name = glyph_id_or_name
            else:
                glyph_name = self.ttfont.getGlyphName(int(glyph_id_or_name))
            advance, _ = self.hmtx[glyph_name]
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "char_width fallback for U+%04X at size %.1f: %s",
                ord(char), font_size, exc,
            )
            advance = self.upem // 2
        return (advance / self.upem) * font_size

    def close(self):
        """Release font resources."""
        if hasattr(self, "ttfont") and self.ttfont:
            self.ttfont.close()
