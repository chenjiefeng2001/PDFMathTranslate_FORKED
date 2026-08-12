"""P8 — Baseline-aware Layout Model：Master Baseline 几何计算。

规范书 §2.3：垂直方向排版与对齐机制必须基于**主基线（Master
Baseline）**，而非简单的包围盒（BBox）边缘，防止字体切换引发的
垂直漂移。

对一行/一段字形集合，按字号加权的基线均值即主基线；升部/降部取
字形跨度的稳健估计（中位数），用于三阶段坐标推导（P9 solver）与
Inline 排版（P7 inline_layout）。
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import List, Optional, Sequence

from pdf2zh.geometry.glyph import Glyph


@dataclass
class BaselineMetrics:
    """主基线几何度量（y-up 坐标系）。"""

    master_baseline: float = 0.0     # 主基线 y
    ascent: float = 0.0              # 字面升部（正数）
    descent: float = 0.0             # 字面降部（负数）
    line_height: float = 0.0         # ascent - descent

    @property
    def cap_height(self) -> float:
        """大写字母高度估计（用于行内对齐微调）。"""
        return max(0.0, self.ascent * 0.7)

    def to_dict(self) -> dict:
        return {
            "master_baseline": round(self.master_baseline, 2),
            "ascent": round(self.ascent, 2),
            "descent": round(self.descent, 2),
            "line_height": round(self.line_height, 2),
        }


class BaselineComputer:
    """Master Baseline 计算器（字重加权 + 稳健升/降部）。"""

    @staticmethod
    def compute(glyphs: Sequence[Glyph],
                baseline: Optional[float] = None) -> BaselineMetrics:
        """由字形集合计算主基线几何。

        基线：若显式传入 ``baseline`` 则用之；否则按字号加权平均字形基线。
        升部：字形 ascent 加权平均；降部同理（负值）。
        """
        if not glyphs:
            return BaselineMetrics()
        sizes = [max(g.font_size, 0.01) for g in glyphs]
        total_w = sum(sizes)
        if baseline is None:
            base = sum(g.baseline * s for g, s in zip(glyphs, sizes)) / total_w
        else:
            base = baseline
        ascent = sum(max(g.ascent, 0.0) * s for g, s in zip(glyphs, sizes)) / total_w
        descent = sum(min(g.descent, 0.0) * s for g, s in zip(glyphs, sizes)) / total_w
        return BaselineMetrics(
            master_baseline=base,
            ascent=ascent,
            descent=descent,
            line_height=max(ascent - descent, max(sizes)),
        )

    @staticmethod
    def median(glyphs: Sequence[Glyph]) -> BaselineMetrics:
        """稳健变体：中位数基线 + 中位升/降部（抗公式超大字字形干扰）。"""
        if not glyphs:
            return BaselineMetrics()
        base = median(g.baseline for g in glyphs)
        asc = median(max(g.ascent, 0.0) for g in glyphs)
        dsc = median(min(g.descent, 0.0) for g in glyphs)
        size = median(max(g.font_size, 0.01) for g in glyphs)
        return BaselineMetrics(
            master_baseline=base,
            ascent=asc,
            descent=dsc,
            line_height=max(asc - dsc, size),
        )


def align_baselines(target: BaselineMetrics, source: BaselineMetrics) -> float:
    """把 ``source`` 行对齐到 ``target`` 主基线所需的垂直位移（dy）。

    dy = target.master_baseline - source.master_baseline
    （y-up：正 dy 表示上移）。
    """
    return target.master_baseline - source.master_baseline


__all__ = ["BaselineMetrics", "BaselineComputer", "align_baselines"]
