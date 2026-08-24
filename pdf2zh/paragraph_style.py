"""
ParagraphStyle for pdf2zh 2.0.

Provides consistent line height management independent of font metrics,
ensuring uniform paragraph spacing across different fonts and languages.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TextAlignment(Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


@dataclass
class ParagraphStyle:
    """Consistent paragraph styling parameters.

    Font metrics (ascent/descent) serve as a baseline, but the actual
    line height is determined by line_spacing * font_size, ensuring
    uniform spacing even when fonts are swapped.
    """

    font_size: float = 12.0
    line_spacing: float = 1.35  # Unified line height ratio (CJK target)
    alignment: TextAlignment = TextAlignment.LEFT
    first_line_indent: float = 0.0
    space_before: float = 2.0
    space_after: float = 2.0

    # Language-specific default line spacings
    LANG_LINE_SPACING: Dict[str, float] = field(
        default_factory=lambda: {
            "zh-cn": 1.35,
            "zh-tw": 1.35,
            "zh-hans": 1.35,
            "zh-hant": 1.35,
            "zh": 1.35,
            "ja": 1.25,
            "ko": 1.30,
            "en": 1.20,
            "ar": 1.10,
            "ru": 1.15,
            "uk": 1.15,
            "ta": 1.10,
        }
    )

    def __post_init__(self):
        if self.line_spacing < 0.8:
            self.line_spacing = 0.8
        if self.line_spacing > 3.0:
            self.line_spacing = 3.0

    @classmethod
    def for_language(cls, lang: str, font_size: float = 12.0) -> "ParagraphStyle":
        """Create a ParagraphStyle tuned for a specific target language.

        Args:
            lang: Target language code (e.g. 'zh-cn', 'ja', 'en')
            font_size: Base font size in points

        Returns:
            ParagraphStyle instance with appropriate line spacing
        """
        lang_lower = lang.lower().replace("_", "-")
        base = cls.LANG_LINE_SPACING
        # Try full match first, then prefix match
        spacing = base.get(lang_lower, 1.25)
        if spacing is None:
            for key, val in base.items():
                if lang_lower.startswith(key) or key.startswith(lang_lower):
                    spacing = val
                    break
            else:
                spacing = 1.25
        return cls(font_size=font_size, line_spacing=spacing)

    @property
    def line_height(self) -> float:
        """Get the line height in points."""
        return self.font_size * self.line_spacing


@dataclass
class TextLine:
    """A single line of laid-out text with positioning info."""

    text: str
    x: float
    y: float
    width: float
    height: float
    font_size: float
    alignment: TextAlignment = TextAlignment.LEFT


@dataclass
class TextBlock:
    """A paragraph block containing one or more text lines."""

    lines: list = field(default_factory=list)
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    font_size: float = 0.0
    style: Optional[ParagraphStyle] = None

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)
