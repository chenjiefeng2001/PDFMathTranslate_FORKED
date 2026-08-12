"""P5.3 — VisualLine: 视觉物理行重构（规范书 §5.1）。

两组字形/StyleRun 是否属于同一物理视觉行的三重判定：

  1. **基线对齐**：|baseline_A - baseline_B| < theta_base × median_font_size
     （默认 theta_base = 0.35）；
  2. **垂直重叠率**：Vertical Overlap Ratio >= 0.60；
  3. **水平间距阈值**：dx <= max(2.5 × font_size, gap_threshold)。

输出 ``VisualLine``（含 style_runs、master_baseline、is_math_line 标记）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import List, Optional, Sequence

from pdf2zh.geometry.glyph import Glyph, GlyphBBox
from pdf2zh.geometry.style_run import StyleRun, build_style_runs


@dataclass
class VisualLineConfig:
    """视觉行重建阈值（全部基于字号自适应，无魔法硬编码）。"""

    baseline_theta: float = 0.35          # §5.1-1：基线对齐容差 / 字号
    vertical_overlap_min: float = 0.60    # §5.1-2：垂直重叠率下限
    horizontal_ratio: float = 2.5         # §5.1-3：水平间距 / 字号
    gap_threshold: float = 0.0            # §5.1-3：绝对间距阈值（0=自动）
    use_baseline_tol_for_subscript: bool = True
    """下标/上标字形（字号偏小）仍并入主行：以主字号折算容差。"""


# 上/下标字形判定：字号 < 0.6 × 全局最大字号（§5.1 上标/下标结构）
cfg_sub_size_ratio = 0.6


@dataclass
class VisualLine:
    """一条视觉物理行：同一主基线簇上的字形集合。"""

    line_id: str
    glyphs: List[Glyph] = field(default_factory=list)
    style_runs: List[StyleRun] = field(default_factory=list)
    bbox: GlyphBBox = (0.0, 0.0, 0.0, 0.0)
    master_baseline: float = 0.0
    is_math_line: bool = False

    # ── 派生几何 ────────────────────────────────────────────────────
    @property
    def text(self) -> str:
        return "".join(g.char for g in self.glyphs)

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
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def font_size(self) -> float:
        """行主字号 = 行内最大字形字号（标题/公式主导）。"""
        return max((g.font_size for g in self.glyphs), default=12.0)

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "text": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "master_baseline": round(self.master_baseline, 2),
            "is_math_line": self.is_math_line,
            "glyph_count": len(self.glyphs),
            "style_runs": [r.to_dict() for r in self.style_runs],
        }


def _vertical_overlap_ratio(ga: Glyph, gb: Glyph) -> float:
    """两字形 bbox 垂直重叠率 = 重叠高 / min(高)。"""
    ha = ga.height
    hb = gb.height
    if ha <= 0 or hb <= 0:
        return 0.0
    overlap = min(ga.y1, gb.y1) - max(ga.y0, gb.y0)
    return max(0.0, overlap) / min(ha, hb)


def _can_join(glyph: Glyph, line: VisualLine, cfg: VisualLineConfig,
              median_size: float) -> bool:
    """判定字形能否并入既有行（§5.1 三重条件全部满足）。"""
    # 参考基线：取行内「主字形」（最大字号，正文主导）的基线。
    # 构建期 master_baseline 尚未最终计算，且行首可能是上/下标小字号字形，
    # 若固定取行首字形基线会错误地以上标基线为行锚（幽灵行分裂）。
    ref_glyph = max(line.glyphs, key=lambda g: g.font_size) if line.glyphs else None
    ref_baseline = ref_glyph.baseline if ref_glyph else glyph.baseline
    # 1. 基线对齐
    if cfg.use_baseline_tol_for_subscript:
        # 上下标：以行主字号折算容差，允许小字号字形基线漂移
        ref_size = max(median_size, 0.01)
        tol = cfg.baseline_theta * ref_size
    else:
        ref_size = max(glyph.font_size, 0.01)
        tol = cfg.baseline_theta * ref_size
    if abs(glyph.baseline - ref_baseline) > tol:
        return False
    # 2. 垂直重叠率：与行内「水平相邻」的字形比较
    gap_ratio = max(cfg.horizontal_ratio * ref_size, cfg.gap_threshold)
    neighbor = None
    for g in line.glyphs:
        if abs(g.center_x - glyph.center_x) <= gap_ratio:
            neighbor = g
            break
    if neighbor is not None:
        if _vertical_overlap_ratio(glyph, neighbor) < cfg.vertical_overlap_min:
            return False
    # 3. 水平间距：与行内 x 相邻字形
    nearest_dx = None
    for g in line.glyphs:
        dx = abs(g.center_x - glyph.center_x)
        if nearest_dx is None or dx < nearest_dx:
            nearest_dx = dx
    if nearest_dx is not None and nearest_dx > gap_ratio:
        return False
    return True


class VisualLineBuilder:
    """纯算法 Glyph → VisualLine 重构器（确定性、无外部依赖）。"""

    def __init__(self, config: Optional[VisualLineConfig] = None) -> None:
        self.config = config or VisualLineConfig()

    def build(self, glyphs: Sequence[Glyph],
              page_id: int = 0, line_prefix: str = "L") -> List[VisualLine]:
        """按主基线聚簇字形为视觉行，行内按 x 排序。

        两阶段构建（避免上/下标小字号字形成为行锚的「幽灵行」问题）：
          pass1  主字形（font_size >= 0.6×全局最大字号）按基线聚类成行；
          pass2  上/下标字形（明显小于主字号）并入最近的既有主行；
                无法并入的残留字形按原贪心逻辑成行。
        随后：行内重排 → 生成 style_runs → 计算 master_baseline
        （字形基线按字号加权平均）→ 合并 bbox。
        """
        if not glyphs:
            return []
        # F2 修复：聚类排序只按基线（稳定 → 同 baseline 保持内容流序）。
        # 原 `(-baseline, x0)` 用 x0 作次级 key，但未知字体 x0≈相同（微差
        # 0.01pt 噪音），会打乱行内流序 → font.unknown.pdf 乱码
        # （"The sociology..." → "onwiogyofnestolproducsiheocT"）。
        ordered = sorted(glyphs, key=lambda g: -g.baseline)
        max_size = max(g.font_size for g in ordered) if ordered else 1.0
        sub_threshold = cfg_sub_size_ratio * max_size
        main = [g for g in ordered if g.font_size >= sub_threshold]
        subs = [g for g in ordered if g.font_size < sub_threshold]
        median_size = median([g.font_size for g in ordered])
        # pass1: 主字形聚类
        lines: List[VisualLine] = []
        for g in main:
            placed = False
            for line in lines:
                if _can_join(g, line, self.config, median_size):
                    line.glyphs.append(g)
                    placed = True
                    break
            if not placed:
                lines.append(VisualLine(line_id=f"{line_prefix}{page_id}_{len(lines)}",
                                        glyphs=[g]))
        # pass2: 上/下标字形并入既有主行
        for g in subs:
            placed = False
            for line in lines:
                if _can_join(g, line, self.config, median_size):
                    line.glyphs.append(g)
                    placed = True
                    break
            if not placed:
                lines.append(VisualLine(line_id=f"{line_prefix}{page_id}_{len(lines)}",
                                        glyphs=[g]))
        for line in lines:
            # 行内排序：仅当 x0 几何可靠（每字符 x0 基本互不相同 → 单调递增
            # 布局）才按 x0 排序；否则保持内容流序。
            # font.unknown.pdf：unknown 字体 x0 按 content 段重置（行内部分
            # 字符 51.9、部分 53.5，非单调），unique-x0 远少于字符数；按 x0
            # 排序会把段分组 → 乱码（"Newsis..." → "oung,Yssndit..."）。
            _xs = [g.x0 for g in line.glyphs]
            _uniq = len({round(v, 1) for v in _xs})
            if _uniq >= max(2, int(len(line.glyphs) * 0.5)):
                line.glyphs.sort(key=lambda g: g.x0)
            line.style_runs = build_style_runs(line.glyphs)
            total_w = sum(max(g.font_size, 0.01) for g in line.glyphs)
            if total_w > 0:
                line.master_baseline = sum(
                    g.baseline * max(g.font_size, 0.01) for g in line.glyphs
                ) / total_w
            else:
                line.master_baseline = line.glyphs[0].baseline if line.glyphs else 0.0
            line.bbox = (
                min(g.x0 for g in line.glyphs),
                min(g.y0 for g in line.glyphs),
                max(g.x1 for g in line.glyphs),
                max(g.y1 for g in line.glyphs),
            )
        lines.sort(key=lambda l: -l.master_baseline)
        return lines


__all__ = ["VisualLine", "VisualLineConfig", "VisualLineBuilder"]
