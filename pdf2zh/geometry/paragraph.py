"""P5.4 — LogicalParagraph: 逻辑自然段聚合（规范书 §5.2）。

对连续的 ``VisualLine`` 进行逻辑段落合并，**忽略内部 Font 切换**
（样式与语义解耦，§2.1）——字体/字号切换严禁作为语义单元截断边界。

* 正向聚合指标：
    - 行间距稳定性（Line Gap Consistency）
    - 左/右对齐容差（Margin Alignment Tolerance <= 2.0pt）
    - 首行缩进模式（Indentation Recognition，不阻断聚合）
* 硬性截断条件（任一满足即终止段落合并）：
    - 明确的 Block 级障碍物分割
    - 文本方向改变（Text Direction Change）
    - 垂直间距 > 1.8 × line_height
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import List, Optional, Sequence

from pdf2zh.geometry.glyph import GlyphBBox
from pdf2zh.geometry.line import VisualLine


@dataclass
class ParagraphConfig:
    """逻辑段落聚合阈值（规范书 §5.2 全部可调）。"""

    line_gap_factor: float = 1.8          # 硬截断：垂直间距 > factor × line_height
    margin_tolerance: float = 2.0         # 正向聚合：左/右对齐容差（pt）
    gap_stability_ratio: float = 0.5      # 行间距稳定性容差 / 中位行距
    direction_change_break: bool = True   # 文本方向改变 → 截断
    block_break: bool = True              # Block 级障碍物分割 → 截断
    # 失效点 3 加固：Block 掩码与行的「交叠面积占比」≥ 阈值才截断。
    # DocLayout (YOLO) 检测框存在 1~3pt 越界是常态——若任何垂直重叠都硬裁，
    # 重构好的段落会被上游检测框二次打碎。默认 0.3（与行重叠 >30% 才算障碍）。
    block_iou_threshold: float = 0.3


@dataclass
class LogicalParagraph:
    """逻辑自然段：连续 VisualLine 的语义集合（inline_objects 由 P6 填充）。"""

    paragraph_id: str
    page_id: int
    lines: List[VisualLine] = field(default_factory=list)
    inline_objects: List = field(default_factory=list)   # InlineObject 联合类型
    bbox: GlyphBBox = (0.0, 0.0, 0.0, 0.0)
    master_baseline: float = 0.0

    # ── 派生几何 ────────────────────────────────────────────────────
    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

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
        return max((l.font_size for l in self.lines), default=12.0)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def to_dict(self) -> dict:
        return {
            "paragraph_id": self.paragraph_id,
            "page_id": self.page_id,
            "text": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "master_baseline": round(self.master_baseline, 2),
            "line_count": self.line_count,
            "lines": [l.to_dict() for l in self.lines],
        }


def _line_height(line: VisualLine) -> float:
    """行高估计 = (ascent - descent)，至少一个字号。"""
    asc = max((g.ascent for g in line.glyphs), default=0.8 * line.font_size)
    dsc = min((g.descent for g in line.glyphs), default=-0.2 * line.font_size)
    return max(asc - dsc, line.font_size)


def _same_direction(a: VisualLine, b: VisualLine) -> bool:
    """文本方向是否一致：按行内字形宽高比聚类判断旋转行。"""
    def _rotated(line: VisualLine) -> bool:
        if not line.glyphs:
            return False
        tall = sum(1 for g in line.glyphs if g.height > 1.2 * g.width)
        return tall / len(line.glyphs) > 0.8
    return _rotated(a) == _rotated(b)


def _block_split(line: VisualLine,
                 blocks: Sequence[GlyphBBox],
                 iou_threshold: float = 0.3) -> bool:
    """行与 Block 级障碍物重叠且**交叠面积占行面积 ≥ 阈值** → 段落截断。

    失效点 3 加固：只做「垂直重叠即截断」会令 DocLayout 检测框轻微越界
    （1~3pt 越界是常态）就把已重构段落二次打碎。此处要求障碍物与行的
    交叠面积占行面积比例达到阈值才算障碍；轻微越界的检测框不影响聚合。
    """
    line_area = max(
        (line.x1 - line.x0) * max(line.y1 - line.y0, 1e-6), 1e-6)
    for (bx0, by0, bx1, by1) in blocks:
        # 二维重叠
        if not (line.x0 < bx1 and line.x1 > bx0
                and line.y0 < by1 and line.y1 > by0):
            continue
        ox = min(line.x1, bx1) - max(line.x0, bx0)
        oy = min(line.y1, by1) - max(line.y0, by0)
        if ox <= 0 or oy <= 0:
            continue
        if (ox * oy) / line_area >= max(iou_threshold, 0.0):
            return True
    return False


def build_logical_paragraphs(
    lines: Sequence[VisualLine],
    page_id: int = 0,
    para_prefix: str = "P",
    blocks: Optional[Sequence[GlyphBBox]] = None,
    config: Optional[ParagraphConfig] = None,
) -> List[LogicalParagraph]:
    """按规范书 §5.2 聚合逻辑段落（忽略字体切换，硬条件截断）。"""
    cfg = config or ParagraphConfig()
    blocks = list(blocks or [])
    if not lines:
        return []
    ordered = sorted(lines, key=lambda l: -l.master_baseline)
    paragraphs: List[LogicalParagraph] = []
    current: Optional[LogicalParagraph] = None
    prev: Optional[VisualLine] = None
    gaps: List[float] = []

    def _finish() -> None:
        nonlocal current, prev, gaps
        if current is not None:
            paragraphs.append(current)
        current = None
        prev = None
        gaps = []

    for idx, line in enumerate(ordered):
        if current is None:
            current = LogicalParagraph(
                paragraph_id=f"{para_prefix}{page_id}_{idx}", page_id=page_id)
            current.lines.append(line)
            prev = line
            continue
        assert prev is not None
        gap = prev.master_baseline - line.master_baseline   # y-up：下行基线更小
        lh = max(_line_height(prev), _line_height(line), 1e-6)

        # ── 硬性截断条件 ─────────────────────────────────────────
        hard_break = False
        if gap > cfg.line_gap_factor * lh:          # 垂直间距过大
            hard_break = True
        if cfg.direction_change_break and not _same_direction(prev, line):
            hard_break = True
        if cfg.block_break and _block_split(line, blocks, cfg.block_iou_threshold):
            hard_break = True
        if hard_break:
            _finish()
            current = LogicalParagraph(
                paragraph_id=f"{para_prefix}{page_id}_{idx}", page_id=page_id)
            current.lines.append(line)
            prev = line
            continue

        # ── 正向聚合指标 ─────────────────────────────────────────
        gaps.append(gap)
        median_gap = median(gaps) if gaps else gap
        gap_stable = abs(gap - median_gap) <= cfg.gap_stability_ratio * max(median_gap, 1e-6)
        # 左/右对齐容差
        x0s = [l.x0 for l in current.lines]
        x1s = [l.x1 for l in current.lines]
        left_aligned = abs(line.x0 - median(x0s)) <= cfg.margin_tolerance
        right_aligned = abs(line.x1 - median(x1s)) <= cfg.margin_tolerance
        if gap_stable and (left_aligned or right_aligned):
            current.lines.append(line)
            prev = line
        else:
            _finish()
            current = LogicalParagraph(
                paragraph_id=f"{para_prefix}{page_id}_{idx}", page_id=page_id)
            current.lines.append(line)
            prev = line

    _finish()
    for para in paragraphs:
        para.bbox = (
            min(l.x0 for l in para.lines),
            min(l.y0 for l in para.lines),
            max(l.x1 for l in para.lines),
            max(l.y1 for l in para.lines),
        )
        total_w = sum(max(l.font_size, 0.01) for l in para.lines)
        para.master_baseline = (
            sum(l.master_baseline * max(l.font_size, 0.01) for l in para.lines) / total_w
            if total_w > 0 else (para.lines[0].master_baseline if para.lines else 0.0)
        )
    return paragraphs


__all__ = [
    "LogicalParagraph", "ParagraphConfig", "build_logical_paragraphs",
]
