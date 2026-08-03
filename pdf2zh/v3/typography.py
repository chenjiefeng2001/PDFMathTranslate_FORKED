"""Module: Phase 3 / 阶段七 Adaptive Typography Engine.

Implements the roadmap's "自适应排版引擎" deliverables:

  * dynamic line-height / paragraph-spacing driven by the *translated* text
    length and script mix (CJK vs Latin), instead of fixed geometry;
  * baseline alignment metrics for mixed CJK / Latin runs;
  * expansion-ratio detection (translation grows / shrinks vs the source);
  * auto-fit font-size shrink when the translation overflows the container.

Pure-Python, no external deps — every metric is directly unit-testable.

Usage::

    from pdf2zh.v3.typography import AdaptiveTypography, GlyphProbe
    m = AdaptiveTypography().metrics("机器学习模型……", source="Machine learning model")
    print(m.line_height, m.block_height, m.expansion_ratio)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Common CJK / full-width blocks used for script detection.
_CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK Ext A
    (0x4E00, 0x9FFF),   # CJK Unified
    (0xF900, 0xFAFF),   # CJK Compatibility
    (0xFF00, 0xFFEF),   # Full-width forms / halfwidth katakana
    (0x3040, 0x30FF),   # Hiragana / Katakana
    (0xAC00, 0xD7AF),   # Hangul
)


def is_cjk(ch: str) -> bool:
    """True for CJK / full-width / Hangul / Kana characters."""
    if not ch:
        return False
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


@dataclass
class GlyphMetric:
    """Width / advance of a single character at a given font size."""
    char: str
    width: float = 0.0
    height: float = 12.0
    advance: float = 0.0
    is_cjk: bool = False


@dataclass
class TypographyMetrics:
    """Result of adaptive typography for one translated paragraph.

    Attributes:
        font_size: effective font size after auto-fit.
        line_height: dynamic line height (CJK higher than Latin).
        line_gap: extra gap between baselines (line_height - font_size).
        paragraph_spacing: dynamic spacing between paragraphs.
        lines: wrapped text lines.
        block_height: total height consumed by the paragraph.
        baseline_offset: distance from the box top to the first baseline.
        is_cjk_dominant: whether the text is mostly CJK.
        expansion_ratio: translated width / source width (CJK-aware).
        estimated_width: widest line width in the box.
    """

    font_size: float = 12.0
    line_height: float = 14.4
    line_gap: float = 2.4
    paragraph_spacing: float = 6.0
    lines: List[str] = field(default_factory=list)
    block_height: float = 0.0
    baseline_offset: float = 0.0
    is_cjk_dominant: bool = False
    expansion_ratio: float = 1.0
    estimated_width: float = 0.0

    def to_dict(self) -> dict:
        return {
            "font_size": round(self.font_size, 2),
            "line_height": round(self.line_height, 2),
            "line_gap": round(self.line_gap, 2),
            "paragraph_spacing": round(self.paragraph_spacing, 2),
            "lines": len(self.lines),
            "block_height": round(self.block_height, 2),
            "baseline_offset": round(self.baseline_offset, 2),
            "is_cjk_dominant": self.is_cjk_dominant,
            "expansion_ratio": round(self.expansion_ratio, 3),
            "estimated_width": round(self.estimated_width, 2),
        }


class GlyphProbe:
    """Character-level width estimation (kerning-style advance)."""

    CJK_WIDTH = 12.0
    ASCII_WIDTH = 7.0
    LETTER_SPACING = 0.5
    WORD_SPACING = 3.0

    @classmethod
    def char_width(cls, ch: str, font_size: float = 12.0) -> float:
        """Advance width of one char, scaled by font size."""
        scale = font_size / 12.0
        w = cls.CJK_WIDTH if is_cjk(ch) else cls.ASCII_WIDTH
        return (w + cls.LETTER_SPACING) * scale

    @classmethod
    def text_width(cls, text: str, font_size: float = 12.0) -> float:
        """Total advance width of a run.

        Spaces are separators, not glyphs: they only contribute the word
        spacing between adjacent words (consistent with ``break_lines``).
        """
        total = 0.0
        scale = font_size / 12.0
        for i, ch in enumerate(text):
            if ch == " ":
                if i + 1 < len(text):
                    total += cls.WORD_SPACING * scale
                continue
            total += cls.char_width(ch, font_size)
        return total

    @classmethod
    def cjk_fraction(cls, text: str) -> float:
        if not text:
            return 0.0
        return sum(1 for ch in text if is_cjk(ch)) / len(text)

    @classmethod
    def break_lines(cls, text: str, container_width: float,
                    font_size: float = 12.0) -> List[str]:
        """Greedy word wrap; CJK breaks at any char when a word is too wide."""
        words = text.split(" ")
        lines: List[str] = []
        current: List[str] = []
        current_w = 0.0
        for word in words:
            ww = cls.text_width(word, font_size)
            if ww > container_width and not current:
                # single over-wide word → split by char
                for ch in word:
                    cw = cls.char_width(ch, font_size)
                    if current and current_w + cw > container_width:
                        lines.append("".join(current))
                        current, current_w = [], 0.0
                    current.append(ch)
                    current_w += cw
                continue
            sep = cls.WORD_SPACING * (font_size / 12.0) if current else 0.0
            if current and current_w + sep + ww > container_width:
                lines.append(" ".join(current))
                current, current_w = [word], ww
            else:
                current.append(word)
                current_w += sep + ww
        if current:
            lines.append(" ".join(current))
        return lines if lines else [text]


class AdaptiveTypography:
    """Dynamic line-height / paragraph-spacing / baseline metrics.

    Core rule (roadmap 阶段七): when a translation expands, its line height
    and paragraph spacing grow proportionally so neighbouring blocks are not
    overlapped by the *typeset* text.
    """

    CJK_LINE_RATIO = 1.45      # CJK glyphs need taller line boxes
    LATIN_LINE_RATIO = 1.20
    MIXED_LINE_RATIO = 1.35
    MIN_FONT_SIZE = 6.0
    PARAGRAPH_SPACING_FACTOR = 0.5

    def __init__(self, container_width: float = 450.0,
                 font_size: float = 12.0) -> None:
        self.container_width = container_width
        self.font_size = font_size

    # ── Per-ratio helpers ─────────────────────────────────────────────

    @classmethod
    def line_ratio(cls, text: str) -> float:
        """Pick a line-height ratio from the script mix."""
        frac = GlyphProbe.cjk_fraction(text)
        if frac == 0.0:
            return cls.LATIN_LINE_RATIO
        if frac >= 0.5:
            return cls.CJK_LINE_RATIO
        return cls.MIXED_LINE_RATIO

    @classmethod
    def line_height_for(cls, text: str, font_size: float) -> float:
        """Dynamic line height = font_size × ratio(text)."""
        return font_size * cls.line_ratio(text)

    @classmethod
    def paragraph_spacing_for(cls, text: str, font_size: float) -> float:
        """Dynamic paragraph spacing scaled with the line height."""
        return cls.line_height_for(text, font_size) * cls.PARAGRAPH_SPACING_FACTOR

    @classmethod
    def expansion_ratio(cls, translated: str, source: Optional[str]) -> float:
        """CJK-aware length ratio: how much the translation grew.

        Widths are estimated in glyph units, so a short CJK sentence that is
        actually *wider* than its Latin source is correctly detected.
        """
        if source is None or not source:
            return 1.0
        src_w = GlyphProbe.text_width(source, 12.0)
        if src_w <= 0:
            return 1.0
        return GlyphProbe.text_width(translated, 12.0) / src_w

    # ── Public API ────────────────────────────────────────────────────

    def metrics(self, translated: str, source: Optional[str] = None,
                font_size: Optional[float] = None,
                container_width: Optional[float] = None) -> TypographyMetrics:
        """Full adaptive metrics for one translated paragraph."""
        fs = font_size or self.font_size
        cw = container_width or self.container_width
        lines = GlyphProbe.break_lines(translated, cw, fs)
        line_height = self.line_height_for(translated, fs)
        expansion = self.expansion_ratio(translated, source)
        block_height = len(lines) * line_height
        cjk_dominant = GlyphProbe.cjk_fraction(translated) >= 0.5
        return TypographyMetrics(
            font_size=fs,
            line_height=line_height,
            line_gap=line_height - fs,
            paragraph_spacing=self.paragraph_spacing_for(translated, fs),
            lines=lines,
            block_height=block_height,
            baseline_offset=line_height * 0.8,
            is_cjk_dominant=cjk_dominant,
            expansion_ratio=expansion,
            estimated_width=max((GlyphProbe.text_width(ln, fs)
                                 for ln in lines), default=0.0),
        )

    def auto_fit_font_size(self, text: str, font_size: Optional[float] = None,
                           container_width: Optional[float] = None,
                           max_lines: Optional[int] = None,
                           target_height: Optional[float] = None) -> float:
        """Shrink the font until the text fits (width / line / height budget).

        Returns the largest font size in [MIN_FONT_SIZE, original] whose
        wrapped lines fit the container width and, when given, the line count
        and total block-height budgets. Falls back to MIN_FONT_SIZE.
        """
        fs = font_size or self.font_size
        cw = container_width or self.container_width
        while fs >= self.MIN_FONT_SIZE:
            lines = GlyphProbe.break_lines(text, cw, fs)
            fits_width = all(
                GlyphProbe.text_width(ln, fs) <= cw + 1e-6 for ln in lines)
            fits_lines = max_lines is None or len(lines) <= max_lines
            fits_height = (target_height is None
                           or len(lines) * self.line_height_for(text, fs)
                           <= target_height + 1e-6)
            if fits_width and fits_lines and fits_height:
                return fs
            fs -= 0.5
        return self.MIN_FONT_SIZE

    def baseline_metrics(self, text: str,
                         font_size: Optional[float] = None) -> dict:
        """Mixed CJK / Latin baseline metrics.

        Latin ascenders sit lower than CJK em-boxes; when both scripts share
        a line we align on the CJK top edge and offset the Latin baseline so
        the visual centers of both scripts coincide.
        """
        fs = font_size or self.font_size
        cjk_dominant = GlyphProbe.cjk_fraction(text) >= 0.5
        if cjk_dominant:
            ascent = fs * 0.88          # em-box top of CJK
            descent = fs * 0.12
            latin_baseline_offset = -fs * 0.12
        else:
            ascent = fs * 0.80
            descent = fs * 0.20
            latin_baseline_offset = 0.0
        return {
            "ascent": ascent,
            "descent": descent,
            "baseline_offset": ascent,          # y from box top to baseline
            "latin_baseline_offset": latin_baseline_offset,
            "cjk_dominant": cjk_dominant,
        }


__all__ = [
    "is_cjk", "GlyphMetric", "TypographyMetrics",
    "GlyphProbe", "AdaptiveTypography",
]

