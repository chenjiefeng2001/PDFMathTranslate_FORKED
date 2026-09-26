"""页面尺寸标准定义与校验工具。

所有 612×792 / 595×842 硬编码统一收归此处；新增/修改标准页面时只需改一处。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

# ── 标准页面尺寸（PDF 点，1 pt = 1/72 inch）──────────────────────────

#: US Letter — 北美常用
LETTER_W, LETTER_H = 612.0, 792.0

#: A4 — ISO 216 国际标准（多数国家）
A4_W, A4_H = 595.28, 841.89

#: A3 — ISO 216
A3_W, A3_H = 841.89, 1190.55

#: Legal — 美国法律文书
LEGAL_W, LEGAL_H = 612.0, 1008.0

#: B5 — ISO B 系列
B5_W, B5_H = 498.90, 708.66

#: 默认回退页面（US Letter）
DEFAULT_PAGE_W, DEFAULT_PAGE_H = LETTER_W, LETTER_H

#: 常见标准页面（宽 × 高），容差 ±2pt 匹配
_STANDARD_PAGES: list[Tuple[str, float, float]] = [
    ("A4", A4_W, A4_H),
    ("Letter", LETTER_W, LETTER_H),
    ("A3", A3_W, A3_H),
    ("Legal", LEGAL_W, LEGAL_H),
    ("B5", B5_W, B5_H),
]

#: 匹配容差（pt）—— 页面尺寸偏差在此范围内视为匹配标准
_SIZE_TOLERANCE_PT = 2.0


@dataclass(frozen=True, slots=True)
class PageStandard:
    """检测到的标准页面信息。"""

    name: str
    width: float
    height: float
    is_landscape: bool
    match_score: float  # 0~1，1 = 完全匹配


def detect_standard(
    width: float,
    height: float,
    tolerance: float = _SIZE_TOLERANCE_PT,
) -> Optional[PageStandard]:
    """检测页面尺寸是否匹配某个标准。

    支持横版/竖版自动识别。返回最接近的标准，无匹配返回 None。
    """
    if width <= 0 or height <= 0:
        return None

    best: Optional[PageStandard] = None
    best_dist = float("inf")

    for name, sw, sh in _STANDARD_PAGES:
        # 竖版
        dist_v = math.hypot(width - sw, height - sh)
        if dist_v < best_dist and dist_v <= tolerance * math.sqrt(2):
            best = PageStandard(name, sw, sh, False, 1.0 - dist_v / (tolerance * 2))
            best_dist = dist_v
        # 横版
        dist_h = math.hypot(width - sh, height - sw)
        if dist_h < best_dist and dist_h <= tolerance * math.sqrt(2):
            best = PageStandard(name, sh, sw, True, 1.0 - dist_h / (tolerance * 2))
            best_dist = dist_h

    return best


def validate_page_size(
    width: float,
    height: float,
    source_name: str = "page",
    tolerance: float = _SIZE_TOLERANCE_PT,
) -> Tuple[float, float, Optional[PageStandard]]:
    """校验页面尺寸，返回修正后的 (width, height) 和检测到的标准。

    - 尺寸有效且匹配标准 → 原样返回
    - 尺寸有效但不匹配标准 → 记录警告，原样返回（尊重非标准尺寸）
    - 尺寸无效（≤0 或 NaN）→ 回退到 DEFAULT，记录警告
    """
    import logging

    logger = logging.getLogger(__name__)

    # 无效尺寸 → 回退
    if not (
        width > 0 and height > 0 and math.isfinite(width) and math.isfinite(height)
    ):
        logger.warning(
            "invalid page size for %s: %.1f×%.1f; falling back to %.1f×%.1f",
            source_name,
            width,
            height,
            DEFAULT_PAGE_W,
            DEFAULT_PAGE_H,
        )
        return DEFAULT_PAGE_W, DEFAULT_PAGE_H, None

    standard = detect_standard(width, height, tolerance)
    if standard:
        logger.debug(
            "page %s matches standard %s (%.1f×%.1f, landscape=%s)",
            source_name,
            standard.name,
            width,
            height,
            standard.is_landscape,
        )
    else:
        logger.debug(
            "page %s has non-standard size: %.1f×%.1f",
            source_name,
            width,
            height,
        )

    return width, height, standard


def normalize_orientation(width: float, height: float) -> Tuple[float, float]:
    """确保 width <= height（竖版），否则翻转。"""
    if width > height:
        return height, width
    return width, height
