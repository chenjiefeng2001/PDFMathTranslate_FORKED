"""
Paragraph layout engine for pdf2zh 2.0.

Provides paragraph-level text layout with proper line wrapping,
justification, and column-aware text flow.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from pdf2zh.text_metrics import TextMetrics

logger = logging.getLogger(__name__)


class TextAlignment(Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


@dataclass
class TextLine:
    """A single line of laid-out text."""

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

    lines: List[TextLine] = field(default_factory=list)
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    font_size: float = 0.0


class ParagraphLayoutEngine:
    """Lays out translated text into paragraphs with proper wrapping.

    Handles:
    - CJW text wrapping (no spaces between characters)
    - Mixed CJK/Latin text
    - Column-aware layout
    - Justified alignment
    """

    def __init__(self, metrics: TextMetrics, line_spacing: float = 1.2):
        self.metrics = metrics
        self.line_spacing = line_spacing

    def wrap_text(self, text: str, max_width: float, font_size: float) -> List[str]:
        """Wrap text to fit within max_width.

        Args:
            text: Unicode text to wrap
            max_width: Maximum line width in points
            font_size: Font size in points

        Returns:
            List of wrapped lines
        """
        if not text.strip():
            return [""]

        lines: List[str] = []
        current_line = ""
        current_width = 0.0

        # Determine if this is CJK-heavy text by checking first few chars
        is_cjk = self._is_cjk_heavy(text)

        if is_cjk:
            # CJK text: wrap at character boundaries
            for ch in text:
                ch_width = self.metrics.char_width(ch, font_size)
                if current_width + ch_width > max_width and current_line:
                    lines.append(current_line)
                    current_line = ch
                    current_width = ch_width
                else:
                    current_line += ch
                    current_width += ch_width
        else:
            # Latin text: wrap at word boundaries
            words = text.split(" ")
            for word in words:
                word_width = self.metrics.measure_string(
                    (word + " ") if word != words[-1] else word, font_size
                )["total_width"]
                if current_width + word_width > max_width and current_line:
                    lines.append(current_line)
                    current_line = word + " "
                    current_width = word_width
                else:
                    current_line += (word + " ") if word != words[-1] else word
                    current_width += word_width

            # Trim trailing space
            current_line = current_line.rstrip()

        if current_line:
            lines.append(current_line)

        return lines if lines else [text]

    def layout_block(
        self,
        text: str,
        x0: float,
        y0: float,
        max_width: float,
        max_height: float,
        font_size: float,
        alignment: TextAlignment = TextAlignment.LEFT,
    ) -> TextBlock:
        """Layout a paragraph block.

        Args:
            text: Translated text to layout
            x0: Left edge of the block
            y0: Top of the block (PDF coordinate: larger = up)
            max_width: Maximum line width
            max_height: Maximum block height
            font_size: Font size in points
            alignment: Text alignment

        Returns:
            TextBlock with laid-out lines
        """
        lines = self.wrap_text(text, max_width, font_size)
        block = TextBlock(
            x0=x0,
            y0=y0 - max_height,
            x1=x0 + max_width,
            y1=y0,
            font_size=font_size,
        )
        line_height = font_size * self.line_spacing
        current_y = y0 - font_size  # PDF: y decreases downward

        for line_text in lines:
            if current_y < y0 - max_height:
                break  # Out of space

            measured = self.metrics.measure_string(line_text, font_size)
            line_width = measured["total_width"]

            # Position based on alignment
            if alignment == TextAlignment.RIGHT:
                line_x = x0 + max_width - line_width
            elif alignment == TextAlignment.CENTER:
                line_x = x0 + (max_width - line_width) / 2
            else:  # LEFT or JUSTIFY
                line_x = x0

            line = TextLine(
                text=line_text,
                x=line_x,
                y=current_y,
                width=line_width,
                height=font_size,
                font_size=font_size,
                alignment=alignment,
            )
            block.lines.append(line)
            current_y -= line_height

        return block

    @staticmethod
    def _is_cjk_heavy(text: str, threshold: float = 0.3) -> bool:
        """Check if text is CJK-heavy based on character ranges."""
        if not text:
            return False
        cjk_count = sum(
            1
            for ch in text
            if "\u4e00" <= ch <= "\u9fff"
            or "\u3000" <= ch <= "\u303f"
            or "\uff00" <= ch <= "\uffef"
        )
        return (cjk_count / len(text)) >= threshold
