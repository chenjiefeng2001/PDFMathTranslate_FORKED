"""
Font Resolver for pdf2zh 2.0.

Analyzes original PDF font properties (serif/sans-serif/monospace)
and maps them to target language CJK fonts, preserving visual style.
"""

from enum import Enum
from typing import Optional


class FontStyle(Enum):
    SERIF = "serif"
    SANS_SERIF = "sans-serif"
    MONOSPACE = "monospace"
    SCRIPT = "script"
    SYMBOL = "symbol"


class FontResolver:
    """Resolve appropriate CJK font based on original font style.

    Analyzes font name and PDF font flags to determine the original
    font style (serif/sans/mono), then maps it to the best matching
    target-language CJK font while preserving the visual style.
    """

    # Font mapping: style -> language -> font filename
    # These fonts are distributed via babeldoc
    FONT_MAP = {
        FontStyle.SERIF: {
            "zh-cn": "SourceHanSerifCN-Regular.otf",
            "zh-tw": "SourceHanSerifTW-Regular.otf",
            "zh": "SourceHanSerifCN-Regular.otf",
            "ja": "NotoSerifJP-Regular.otf",
            "ko": "NotoSerifKR-Regular.otf",
        },
        FontStyle.SANS_SERIF: {
            "zh-cn": "SourceHanSansCN-Regular.otf",
            "zh-tw": "SourceHanSansTW-Regular.otf",
            "zh": "SourceHanSansCN-Regular.otf",
            "ja": "NotoSansJP-Regular.otf",
            "ko": "NotoSansKR-Regular.otf",
        },
        FontStyle.MONOSPACE: {
            "zh-cn": "NotoSansMonoCJKsc-Regular.otf",
            "zh-tw": "NotoSansMonoCJKsc-Regular.otf",
            "zh": "NotoSansMonoCJKsc-Regular.otf",
            "ja": "NotoSansMonoCJKjp-Regular.otf",
            "ko": "NotoSansMonoCJKkr-Regular.otf",
        },
        FontStyle.SCRIPT: {
            "zh-cn": "SourceHanSerifCN-Regular.otf",
            "zh-tw": "SourceHanSerifTW-Regular.otf",
            "ja": "NotoSerifJP-Regular.otf",
            "ko": "NotoSerifKR-Regular.otf",
        },
        FontStyle.SYMBOL: {
            "zh-cn": "SourceHanSerifCN-Regular.otf",
            "zh-tw": "SourceHanSerifTW-Regular.otf",
            "ja": "NotoSerifJP-Regular.otf",
            "ko": "NotoSerifKR-Regular.otf",
        },
    }

    # Fallback chain when preferred font is unavailable
    FALLBACK_CHAIN = [
        FontStyle.SERIF,
        FontStyle.SANS_SERIF,
        FontStyle.MONOSPACE,
    ]

    # Keywords for font name matching (lowercase)
    MONO_KEYWORDS = ["mono", "courier", "console", "code", "fixed"]
    SANS_KEYWORDS = ["sans", "arial", "helvetica", "verdana", "tahoma", "calibri"]
    SERIF_KEYWORDS = ["times", "roman", "georgia", "palatino", "garamond", "serif"]
    SCRIPT_KEYWORDS = ["script", "cursive", "handwriting"]

    def __init__(self, target_lang: str = "zh-cn"):
        self.target_lang = target_lang.lower()

    def match(self, font_name: str, font_flags: int = 0) -> str:
        """Map original font to target language font path.

        Args:
            font_name: Original font name (e.g. 'TimesNewRoman')
            font_flags: PDF font flags bitfield

        Returns:
            Font filename for the target language
        """
        style = self._analyze_style(font_name, font_flags)
        return self._resolve_font(style)

    def _resolve_font(self, style: FontStyle) -> str:
        """Resolve font filename for a given style with fallback."""
        lang_map = self.FONT_MAP.get(style)
        if lang_map and self.target_lang in lang_map:
            return lang_map[self.target_lang]

        # Try fallback chain
        for fallback_style in self.FALLBACK_CHAIN:
            lang_map = self.FONT_MAP.get(fallback_style)
            if lang_map and self.target_lang in lang_map:
                return lang_map[self.target_lang]

        # Ultimate fallback
        return "SourceHanSerifCN-Regular.otf"

    def _analyze_style(self, font_name: str, flags: int) -> FontStyle:
        """Determine font style from name and PDF flags.

        Uses a two-pass approach:
        1. Keyword matching on font name (most reliable)
        2. PDF font flags bitfield analysis (fallback)
        """
        name = font_name.lower()

        # Pass 1: Keyword matching
        if any(kw in name for kw in self.MONO_KEYWORDS):
            return FontStyle.MONOSPACE
        if any(kw in name for kw in self.SANS_KEYWORDS):
            return FontStyle.SANS_SERIF
        if any(kw in name for kw in self.SCRIPT_KEYWORDS):
            return FontStyle.SCRIPT
        if any(kw in name for kw in self.SERIF_KEYWORDS):
            return FontStyle.SERIF

        # Pass 2: PDF font flags analysis
        # Bit 0 (1): FixedPitch (monospace)
        if flags & 0x01 and not (flags & 0x40):  # FixedPitch but not Symbolic
            return FontStyle.MONOSPACE
        # Bit 1 (2): Serif
        if flags & 0x02:
            return FontStyle.SERIF
        # Bit 3 (8): Script
        if flags & 0x08:
            return FontStyle.SCRIPT
        # Bit 4 (16): Nonsymbolic (sans-serif is typical default)
        if flags & 0x20:  # Symbolic flag
            return FontStyle.SYMBOL
        if flags & 0x10 or flags == 0:  # Nonsymbolic or unset
            return FontStyle.SANS_SERIF

        # Default: serif
        return FontStyle.SERIF
