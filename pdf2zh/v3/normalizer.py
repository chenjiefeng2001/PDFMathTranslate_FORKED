"""Module 2: Normalizer Layer.

Transforms raw parser output (RawBlock list) into a normalized intermediate
representation by:

  1. Coordinate normalization (PDF point-space to normalized NDC space)
  2. Font unification (normalize font names and properties)
  3. Unicode normalization (NFC)
  4. Character normalization (whitespace, control chars)

All downstream modules consume NormalizedBlock, never RawBlock directly.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pdf2zh.font_resolver import FontResolver, FontStyle
from pdf2zh.v3.parser import RawBlock, RawBlockType

logger = logging.getLogger(__name__)


# ── Configuration ───────────────────────────────────────────────────────


@dataclass
class NormalizerConfig:
    """Configuration for the Normalizer layer."""

    lang_in: str = "auto"
    target_lang: str = "zh-cn"
    normalize_unicode: bool = True
    normalize_whitespace: bool = True
    normalize_font_names: bool = True
    merge_adjacent_spans: bool = True


# ── Output data structure ───────────────────────────────────────────────


@dataclass
@dataclass
class NormalizedBlock:
    """A parser block after normalization.

    All fields are guaranteed:
      - Coordinates in normalized PDF point space
      - Unicode in NFC form
      - Font names resolved to style categories
      - Whitespace collapsed
    """

    text: str
    bbox: Tuple[float, float, float, float]  # (x0, y0, x1, y1)
    page_num: int
    font_size_avg: float
    font_style: FontStyle  # SERIF, SANS_SERIF, MONOSPACE, etc.
    font_name_original: str
    confidence: float = 1.0
    raw_type: RawBlockType = RawBlockType.TEXT


# ── Normalizer ──────────────────────────────────────────────────────────


class Normalizer:
    """Normalize raw parser output into a clean intermediate representation.

    Leverages existing FontResolver for font style analysis.
    """

    # Regex for excessive whitespace
    _WHITESPACE_PATTERN = re.compile(r"\s+")

    def __init__(self, config: Optional[NormalizerConfig] = None):
        self.config = config or NormalizerConfig()
        self._font_resolver = FontResolver(
            target_lang=self.config.target_lang
        )

    def normalize(self, raw_blocks: List[RawBlock]) -> List[NormalizedBlock]:
        """Normalize a list of RawBlocks.

        Args:
            raw_blocks: Output from PDFParser.

        Returns:
            List of NormalizedBlock, one per input RawBlock.
        """
        result: List[NormalizedBlock] = []
        for raw in raw_blocks:
            if raw.block_type != RawBlockType.TEXT or not raw.spans:
                continue

            # 1. Extract and normalize text
            text = self._normalize_text(raw.text)

            # 2. Normalize bbox
            bbox = self._normalize_bbox(raw.bbox)

            # 3. Analyze font style
            font_name = raw.spans[0].font_name if raw.spans else ""
            font_style = self._resolve_font_style(font_name)

            # 4. Compute average font size
            font_size_avg = raw.font_size_avg

            normalized = NormalizedBlock(
                text=text,
                bbox=bbox,
                page_num=raw.page_num,
                font_size_avg=font_size_avg,
                font_style=font_style,
                font_name_original=font_name,
                confidence=raw.spans[0].confidence if raw.spans else 1.0,
                raw_type=raw.block_type,
            )
            result.append(normalized)

        return result

    def _normalize_text(self, text: str) -> str:
        """Apply text normalization rules."""
        if not text:
            return ""

        # Unicode normalization (NFC)
        if self.config.normalize_unicode:
            text = unicodedata.normalize("NFC", text)

        # Whitespace normalization
        if self.config.normalize_whitespace:
            text = self._WHITESPACE_PATTERN.sub(" ", text)
            text = text.strip()

        return text

    @staticmethod
    def _normalize_bbox(
        bbox: Tuple[float, float, float, float]
    ) -> Tuple[float, float, float, float]:
        """Normalize bounding box — ensure x0<=x1, y0<=y1."""
        x0, y0, x1, y1 = bbox
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        return (x0, y0, x1, y1)

    def _resolve_font_style(self, font_name: str) -> FontStyle:
        """Resolve a font name to its style category.

        Uses FontResolver for style classification.
        """
        if not font_name or not self.config.normalize_font_names:
            return FontStyle.SANS_SERIF
        try:
            return self._font_resolver._analyze_style(font_name, 0)
        except Exception:
            return FontStyle.SANS_SERIF
