"""
Overflow policy for pdf2zh 2.0.
Provides cascading overflow resolution strategies for paragraph layout.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Tuple

from pdf2zh.paragraph_style import TextBlock

logger = logging.getLogger(__name__)


class OverflowAction(Enum):
    LINE_BREAK = auto()
    EXPAND_BBOX = auto()
    COMPRESS_LINE_SPACING = auto()
    PUSH_DOWN = auto()
    NEXT_PAGE = auto()
    REDUCE_FONT = auto()


@dataclass
class OverflowResult:
    action: OverflowAction
    adjusted_y: float = 0.0
    adjusted_font_size: float = 0.0
    adjusted_line_spacing: float = 0.0
    adjusted_width: float = 0.0
    expanded_bbox: bool = False
    pushed_down: bool = False
    requires_reflow: bool = False
    compressed_height: float = 0.0
    expanded_height: float = 0.0


class OverflowPolicy:
    """Cascading overflow resolution policy for paragraph layout.

    Applies strategies in order of preference:
    1. LINE_BREAK - Allow natural line breaking
    2. COMPRESS_LINE_SPACING - Reduce line spacing to fit
    3. PUSH_DOWN - Shift content downward
    4. REDUCE_FONT - Reduce font size as last resort
    """

    def __init__(
        self,
        max_line_spacing_compress: float = 1.0,
        min_font_scale: float = 0.85,
        max_push_down: float = 20.0,
    ):
        self.max_line_spacing_compress = max_line_spacing_compress
        self.min_font_scale = min_font_scale
        self.max_push_down = max_push_down

    def resolve(
        self,
        block: TextBlock,
        available_height: float,
    ) -> OverflowResult:
        """Resolve overflow for a text block within available height.

        Args:
            block: TextBlock that may overflow
            available_height: Maximum height available for this block

        Returns:
            OverflowResult with the applied resolution strategy
        """
        if not block.lines:
            return OverflowResult(action=OverflowAction.LINE_BREAK)

        total_text_height = sum(line.height for line in block.lines)
        if total_text_height <= available_height:
            return OverflowResult(action=OverflowAction.LINE_BREAK)

        overflow = total_text_height - available_height

        # Strategy 1: Compress line spacing
        if block.style and block.style.line_spacing > self.max_line_spacing_compress:
            compressed = self._compress_line_spacing(block, available_height)
            if compressed is not None:
                return compressed

        # Strategy 2: Push down
        if overflow <= self.max_push_down:
            return OverflowResult(
                action=OverflowAction.PUSH_DOWN,
                pushed_down=True,
                adjusted_y=overflow,
            )

        # Strategy 3: Reduce font size
        reduced = self._reduce_font(block, available_height)
        if reduced is not None:
            return reduced

        # Fallback: expand bbox and notify
        return OverflowResult(
            action=OverflowAction.EXPAND_BBOX,
            expanded_bbox=True,
            expanded_height=overflow,
            requires_reflow=True,
        )

    def _compress_line_spacing(
        self,
        block: TextBlock,
        available_height: float,
    ) -> Optional[OverflowResult]:
        """Try compressing line spacing to fit within available height."""
        if (
            not block.style
            or block.style.line_spacing <= self.max_line_spacing_compress
        ):
            return None

        num_lines = len(block.lines)
        if num_lines == 0:
            return None

        new_spacing = max(
            self.max_line_spacing_compress,
            available_height / (num_lines * block.style.font_size),
        )
        return OverflowResult(
            action=OverflowAction.COMPRESS_LINE_SPACING,
            adjusted_line_spacing=new_spacing,
            compressed_height=available_height,
        )

    def _reduce_font(
        self,
        block: TextBlock,
        available_height: float,
    ) -> Optional[OverflowResult]:
        """Try reducing font size to fit within available height."""
        if not block.lines or not block.style:
            return None

        new_size = block.style.font_size * self.min_font_scale
        if new_size < 4.0:
            return None

        return OverflowResult(
            action=OverflowAction.REDUCE_FONT,
            adjusted_font_size=new_size,
        )
