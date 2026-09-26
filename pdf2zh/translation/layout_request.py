"""TranslationRequest — layout-aware translation request。

Translation 不知道空间，现在加上 layout constraint。

用法：
    request = TranslationRequest(
        text="Deep Learning",
        bbox=(50, 700, 120, 720),
        available_width=70,
        font_size=10,
        max_lines=1,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TranslationRequest:
    """Layout-aware translation request。"""

    # 原文
    text: str = ""

    # Layout 约束
    bbox: Optional[Tuple[float, float, float, float]] = None
    available_width: float = 0.0  # pt
    font_size: float = 10.0  # pt
    max_lines: int = 0  # 0 = 无限制
    line_height: float = 1.2  # 倍数

    # 语义信息
    block_type: str = "paragraph"
    page_index: int = -1
    reading_order: int = -1

    # 翻译选项
    preserve_terms: bool = True
    domain: str = "general"

    @property
    def available_chars(self) -> int:
        """估算可用字符数。"""
        if self.available_width <= 0 or self.font_size <= 0:
            return 0
        # 中文字符约等于 font_size 宽度
        chars_per_line = int(self.available_width / self.font_size)
        if self.max_lines > 0:
            return chars_per_line * self.max_lines
        return chars_per_line * 3  # 默认 3 行

    @property
    def width_ratio(self) -> float:
        """中英文宽度比。"""
        # 中文字符宽度约为英文的 1.5-2 倍
        cn_chars = sum(1 for c in self.text if "\u4e00" <= c <= "\u9fff")
        en_chars = len(self.text) - cn_chars
        en_width = en_chars * self.font_size * 0.6
        cn_width = cn_chars * self.font_size * 1.0
        total = en_width + cn_width
        return cn_width / total if total > 0 else 0.0

    def fits_width(self, translated: str) -> bool:
        """检查翻译是否适合宽度。"""
        if self.available_width <= 0:
            return True
        # 粗略估算
        cn_chars = sum(1 for c in translated if "\u4e00" <= c <= "\u9fff")
        en_chars = len(translated) - cn_chars
        width = cn_chars * self.font_size + en_chars * self.font_size * 0.6
        return width <= self.available_width * 1.1  # 允许 10% overflow

    def to_dict(self) -> Dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "text": self.text,
            "bbox": self.bbox,
            "available_width": self.available_width,
            "font_size": self.font_size,
            "max_lines": self.max_lines,
            "block_type": self.block_type,
            "page_index": self.page_index,
        }


@dataclass
class TranslationResponse:
    """Translation response with quality hints。"""

    translated_text: str = ""
    confidence: float = 1.0
    fits_layout: bool = True
    truncated: bool = False
    quality_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_good(self) -> bool:
        """是否高质量翻译。"""
        return self.confidence >= 0.8 and self.fits_layout and not self.truncated
