"""P5.2 — StyleRun: 同样式连续流（样式与语义解耦的关键抽象）。

对应规范书 §4.2 + §5.1：将相同样式特性（字体族 + 字号）的连续字形
聚合为 ``StyleRun``。StyleRun **不是** 语义单元的截断边界 —— 它只
携带 Render/Style 属性，供 VisualLine / 公式置信度引擎消费。

划分规则：
  * 按字形顺序连续分组；
  * ``font_name`` 规范化后相同（忽略大小写/空格，兼容子集字体）；
  * ``font_size`` 落在容差内（默认 0.15pt）视为同一字号。
"""
from __future__ import annotations

from typing import List, Sequence

from pdf2zh.geometry.glyph import Glyph, GlyphBBox


def normalize_font_name(name: str) -> str:
    """规范化字体名：去空白/下划线，统一小写（兼容 ``ABCdef-XYZ`` 子集）。"""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


class StyleRun:
    """同样式连续字形聚合（``start_index``/``end_index`` 为 glyphs 下标）。"""

    __slots__ = ("start_index", "end_index", "font_name", "font_size",
                 "bbox", "_font_key")

    def __init__(self, start_index: int, end_index: int, font_name: str,
                 font_size: float, bbox: GlyphBBox) -> None:
        self.start_index = start_index
        self.end_index = end_index            # 含
        self.font_name = font_name
        self.font_size = font_size
        self.bbox = bbox                      # (x0, y0, x1, y1)
        self._font_key = normalize_font_name(font_name)

    # ── 属性 ────────────────────────────────────────────────────────
    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def font_key(self) -> str:
        return self._font_key

    def to_dict(self) -> dict:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "font_name": self.font_name,
            "font_size": round(self.font_size, 2),
            "bbox": [round(v, 2) for v in self.bbox],
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"StyleRun[{self.start_index}:{self.end_index}] "
                f"{self.font_name}/{self.font_size:.1f}")


def _union_bbox(bboxes: Sequence[GlyphBBox]) -> GlyphBBox:
    xs0 = [b[0] for b in bboxes]
    ys0 = [b[1] for b in bboxes]
    xs1 = [b[2] for b in bboxes]
    ys1 = [b[3] for b in bboxes]
    if not xs0:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def build_style_runs(glyphs: Sequence[Glyph],
                     size_tol: float = 0.15) -> List[StyleRun]:
    """按「字体键 + 字号容差」把连续字形聚合为 StyleRun。

    参数：
        glyphs: 已按阅读顺序排序的字形流（通常先按 y 行分组再按 x）。
        size_tol: 同组字号最大偏差（pt），默认 0.15。
    """
    runs: List[StyleRun] = []
    if not glyphs:
        return runs
    start = 0
    cur_key = normalize_font_name(glyphs[0].font_name)
    cur_size = glyphs[0].font_size
    for i in range(1, len(glyphs)):
        g = glyphs[i]
        key = normalize_font_name(g.font_name)
        if key != cur_key or abs(g.font_size - cur_size) > size_tol:
            runs.append(StyleRun(
                start_index=start,
                end_index=i - 1,
                font_name=glyphs[start].font_name,
                font_size=cur_size,
                bbox=_union_bbox([gg.bbox for gg in glyphs[start:i]]),
            ))
            start = i
            cur_key = key
            cur_size = g.font_size
    runs.append(StyleRun(
        start_index=start,
        end_index=len(glyphs) - 1,
        font_name=glyphs[start].font_name,
        font_size=cur_size,
        bbox=_union_bbox([gg.bbox for gg in glyphs[start:]]),
    ))
    return runs


def style_runs_text(glyphs: Sequence[Glyph],
                    runs: Sequence[StyleRun]) -> List[str]:
    """每个 StyleRun 的纯文本（用于公式置信度引擎的 C_density 统计）。"""
    out: List[str] = []
    for r in runs:
        out.append("".join(gg.char for gg in glyphs[r.start_index:r.end_index + 1]))
    return out


__all__ = [
    "StyleRun", "normalize_font_name", "build_style_runs", "style_runs_text",
]
