"""P9 — Layout Solver：三阶段坐标推导 + 译文变长多行自适应排版。

规范书 §6.2 三阶段坐标模型（严格区分，防止原始坐标被污染）：

    source_bbox (PDF) ──► TranslationUnit ──► LLM
        ──► Layout Solver ──► translated_bbox ──► Render Engine
        ──► render_bbox

    source_bbox      # 原生 PDF 原始物理坐标（Immutable）
    translated_bbox  # 译文长度 + Layout Solver 计算的逻辑目标坐标
    render_bbox      # 最终渲染引擎实际绘制坐标

规范书 §7（P9）：处理译文变长后的多行自适应排版算法 —— 译文比源文长
时向下扩张（y-up 坐标，多行行距 = master baseline 间距），公式锚点
保持几何不可变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pdf2zh.geometry.glyph import GlyphBBox
from pdf2zh.layout.baseline import BaselineComputer
from pdf2zh.layout.inline_layout import (
    InlineLayoutEngine,
    LayoutLine,
    TranslationUnit,
)


@dataclass
class SolvedUnit:
    """三阶段坐标求解结果。"""

    unit_id: str
    source_bbox: GlyphBBox
    translated_bbox: GlyphBBox
    render_bbox: GlyphBBox
    font_size: float
    line_count: int
    lines: List[LayoutLine] = None
    formula_placements: List[Dict] = field(default_factory=list)
    """公式对象级落位：{formula_id, source_bbox, render_bbox, baseline}。"""

    @property
    def drift_dx(self) -> float:
        return self.render_bbox[0] - self.source_bbox[0]

    @property
    def drift_dy(self) -> float:
        return self.render_bbox[1] - self.source_bbox[1]

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "source_bbox": [round(v, 2) for v in self.source_bbox],
            "translated_bbox": [round(v, 2) for v in self.translated_bbox],
            "render_bbox": [round(v, 2) for v in self.render_bbox],
            "font_size": round(self.font_size, 2),
            "line_count": self.line_count,
            "drift_dx": round(self.drift_dx, 3),
            "drift_dy": round(self.drift_dy, 3),
            "lines": [l.to_dict() for l in (self.lines or [])],
            "formula_placements": [
                {
                    **p,
                    "source_bbox": [round(v, 2) for v in p["source_bbox"]],
                    "render_bbox": [round(v, 2) for v in p["render_bbox"]],
                }
                for p in self.formula_placements
            ],
        }


class LayoutSolver:
    """三阶段坐标计算器（P9：译文变长多行自适应）。"""

    def __init__(
        self,
        inline_engine: Optional[InlineLayoutEngine] = None,
        line_gap_ratio: float = 1.15,
        min_font_size: float = 5.0,
        container_pad: float = 4.0,
    ) -> None:
        self.inline = inline_engine or InlineLayoutEngine()
        self.line_gap_ratio = line_gap_ratio  # 行距 = font_size × ratio
        self.min_font_size = min_font_size
        self.container_pad = container_pad

    # ── 阶段 1：translated_bbox（逻辑目标坐标）────────────────────

    def translated_box(
        self,
        unit: TranslationUnit,
        translated_text: str,
        font_size: Optional[float] = None,
        container_width: Optional[float] = None,
    ) -> "tuple":
        """由译文文本计算逻辑目标坐标（不触碰 source_bbox）。

        译文变长 → 按容器宽度贪心换行 → 垂直向下扩张（y-up：基线递减）。
        译文很短 → 保持源行基线（不抬升），宽度压缩。

        **Display 公式垂直流堆叠（用户驱动修复）**：段落内存在块级展示
        公式（``FormulaObject.is_display_mode``）时，禁止使用全局固定 y
        坐标，改按垂直流（Vertical Flow Stacking）动态推进 y 轴：

            y_next = y_current - Height(TextLine) - Height(DisplayFormula)
                     - Padding

        展示公式行占据独立垂直高度（公式物理高度 + 上下 margin），
        后续文本/公式基线一定在展示公式之下 —— 杜绝「译文绘制在展示
        公式正上方」的文字重叠碰撞。
        """
        fs = font_size or self._main_font(unit)
        fs = max(fs, self.min_font_size)
        cw = container_width or (unit.source_bbox[2] - unit.source_bbox[0])
        cw = max(cw - 2 * self.container_pad, fs * 0.8)

        # 构建译文 InlineObject 序列：锚点替换为公式对象，其余为文本段
        objects = self._translate_objects(unit, translated_text, fs)
        lines = self.inline.wrap(objects, cw, fs)
        line_count = max(len(lines), 1)

        # 垂直布局（P8 Baseline-aware）：译文行 i 主基线 = 源行 i 主基线
        # （恒等/行结构不变 → 零漂移）；译文变长新增行沿末行基线递减。
        src = unit.source_bbox
        src_metrics = BaselineComputer.compute(
            [g for o in unit.inline_structure for g in getattr(o, "glyphs", [])] or []
        )
        if src_metrics.master_baseline == 0:
            src_metrics.master_baseline = unit.master_baseline
        line_height = fs * self.line_gap_ratio
        src_baselines = list(unit.source_line_baselines)
        if not src_baselines:
            src_baselines = [src_metrics.master_baseline or (src[3] - fs * 0.8)]
        translated_lines: List[LayoutLine] = []
        formula_placements: List[Dict] = []
        # token ↔ 公式对象 反查表：seg.formula_id 记录的是对象级 id，
        # 需映射回 <formula_x> token 才能拿到几何（§4.4 formula_map）。
        formula_by_id = {
            o.formula_id: (token, o)
            for token, o in unit.formula_map.items()
            if getattr(o, "formula_id", None) is not None
        }
        has_display = any(
            getattr(o, "is_display_mode", False) for o in unit.inline_structure
        )
        if not has_display:
            # ── 原路径：源行基线逐行映射（恒等译文零漂移，QA §9.2）──
            prev_baseline = src_baselines[0]
            for i, line in enumerate(lines):
                if i < len(src_baselines):
                    line.master_baseline = src_baselines[i]
                else:
                    # 译文变长新增行：紧接上一源行的几何延续
                    line.master_baseline = prev_baseline - line_height
                prev_baseline = line.master_baseline
                translated_lines.append(line)
                self._place_line_formulas(
                    line, formula_by_id, src, fs, formula_placements
                )
        else:
            # ── Display 公式垂直流堆叠（用户驱动修复核心）──────────
            # y-up 坐标系：段落顶部 y 最大，逐元素向下（y 递减）推进。
            cursor = float(src[3])  # 流起点 = 段落最高点
            display_margin = 0.6 * line_height  # 展示公式上下留白
            for i, line in enumerate(lines):
                line_display = any(
                    getattr(seg, "display", False) for seg in line.segments
                )
                if line_display:
                    # 展示公式行：物理高度 = 行内最大公式高度 + 上下 margin
                    f_heights = [
                        formula_by_id[seg.formula_id][1].height
                        for seg in line.segments
                        if getattr(seg, "display", False)
                        and seg.formula_id in formula_by_id
                    ]
                    f_h = max(f_heights, default=line_height)
                    # 块顶 = cursor - margin；公式基线由「内部几何相对
                    # 基线」保持（§4.3 不可变：bbox 顶部在基线之上的距离
                    # 恒定），因此公式渲染几何零形变、仅随流整体落位。
                    top = cursor - display_margin
                    anchor = next(
                        (
                            formula_by_id[seg.formula_id][1]
                            for seg in line.segments
                            if getattr(seg, "display", False)
                            and seg.formula_id in formula_by_id
                        ),
                        None,
                    )
                    if anchor is not None:
                        line.master_baseline = top - (anchor.baseline - anchor.y0)
                    else:
                        line.master_baseline = top
                    # 下推：公式块 + 上下 margin 之后的 y
                    cursor -= f_h + 2 * display_margin
                else:
                    # 文本行（可含 inline 公式）：行高 = line_height
                    line.master_baseline = cursor - src_metrics.ascent
                    cursor -= line_height
                translated_lines.append(line)
                self._place_line_formulas(
                    line, formula_by_id, src, fs, formula_placements
                )
        # 纳入 display 公式物理高度：translated_bbox 必须覆盖公式块，
        # 否则段落容器被压缩、接管后与后续段落在垂直方向上重叠。
        t_y1 = max(l.master_baseline for l in translated_lines) + src_metrics.ascent
        t_y0 = min(l.master_baseline for l in translated_lines) + src_metrics.descent
        for p in formula_placements:
            t_y1 = max(t_y1, p["render_bbox"][3])
            t_y0 = min(t_y0, p["render_bbox"][1])
        translated_bbox = (src[0], min(t_y0, src[1]), src[2], max(t_y1, src[3]))
        return translated_bbox, line_count, translated_lines, formula_placements

    @staticmethod
    def _place_line_formulas(
        line: LayoutLine, formula_by_id, src, fs, formula_placements: List[Dict]
    ) -> None:
        """把一行内的公式对象落位到 ``formula_placements``（共享路径）。

        - **inline 公式**：源 x0 优先，与译文文本重叠时向右避让（不超容器
          右边界），并在容器宽度内夹紧；避让占用区间 ``text_right`` 顺序
          推进，恒等译文下零漂移保持。
        - **display 公式**：独占一行，水平保持源 x0（夹紧容器），不参与
          文本避让（行内无文本竞争）；垂直由垂直流堆叠决定的行主基线承载。
        """
        text_right = src[0]
        for seg in line.segments:
            if seg.formula_id and seg.formula_id in formula_by_id:
                token, formula = formula_by_id[seg.formula_id]
                display = bool(getattr(seg, "display", False))
                f_w = formula.width if seg.width <= 0 else seg.width
                f_x0 = max(src[0], min(formula.x0, src[2] - f_w))
                if not display and f_x0 < text_right:
                    # 与译文文本重叠 → 右移避让（不超容器右边界）
                    f_x0 = min(text_right, src[2] - f_w)
                    f_x0 = max(f_x0, src[0])
                f_x1 = f_x0 + f_w
                if not display:
                    text_right = max(text_right, f_x1 + fs * 0.2)  # 公式占位+间隙
                dy0 = formula.y0 - formula.baseline
                dy1 = formula.y1 - formula.baseline
                f_baseline = line.master_baseline + seg.baseline_offset
                formula_placements.append(
                    {
                        "formula_id": seg.formula_id,
                        "anchor": token,
                        "source_bbox": list(formula.bbox),
                        "render_bbox": (f_x0, f_baseline + dy0, f_x1, f_baseline + dy1),
                        "baseline": f_baseline,
                        "display": display,
                        "collision_evaded": bool(f_x0 != formula.x0),
                    }
                )
            else:
                text_right += max(float(getattr(seg, "width", 0.0) or 0.0), 0.0)

    def _translate_objects(
        self, unit: TranslationUnit, translated_text: str, font_size: float
    ) -> List:
        """把译文文本与公式锚点重组为 InlineObject 序列。"""
        from pdf2zh.formula.extractor import FormulaObject, InlineTextRun
        from pdf2zh.formula.anchor import ANCHOR_RE

        objects: List = []
        pos = 0
        for m in ANCHOR_RE.finditer(translated_text):
            if m.start() > pos:
                text = translated_text[pos : m.start()]
                objects.append(
                    InlineTextRun(
                        text=text, style_runs=[], bbox=(0, 0, 0, 0), font_size=font_size
                    )
                )
            token = f"<formula_{m.group(1)}>"
            formula = unit.formula_map.get(token)
            if isinstance(formula, FormulaObject):
                objects.append(formula)  # 公式对象几何不可变，原样嵌入
            pos = m.end()
        if pos < len(translated_text):
            objects.append(
                InlineTextRun(
                    text=translated_text[pos:],
                    style_runs=[],
                    bbox=(0, 0, 0, 0),
                    font_size=font_size,
                )
            )
        if not objects:
            objects.append(
                InlineTextRun(
                    text=translated_text,
                    style_runs=[],
                    bbox=(0, 0, 0, 0),
                    font_size=font_size,
                )
            )
        return objects

    # ── 阶段 2：render_bbox（渲染坐标）────────────────────────────

    def render_box(
        self,
        translated_bbox: "GlyphBBox",
        page_rect: Optional["GlyphBBox"],
        font_size: float,
    ) -> "GlyphBBox":
        """把 translated_bbox 映射到渲染坐标（页面边界夹紧 + 基线保持）。

        P1–P4 已消除幽灵障碍物；这里只做防御性边界夹紧，**不做** y<0 的
        粗暴 clamp —— 越界时保留逻辑坐标（交由上层 QA 标记）。
        """
        if page_rect is None:
            return translated_bbox
        x0, y0, x1, y1 = translated_bbox
        p_x0, p_y0, p_x1, p_y1 = page_rect
        margin = max(font_size * 0.5, 2.0)
        out_x0 = max(x0, p_x0)
        out_y1 = min(y1, p_y1 - margin)
        out_x1 = min(x1, p_x1)
        out_y0 = max(y0, p_y0 + margin)
        if out_y1 < p_y1 - margin - font_size:  # 顶部越界：保留逻辑值
            out_y1 = y1
        if out_y0 > p_y0 + margin + font_size:  # 底部越界：保留逻辑值
            out_y0 = y0
        return (out_x0, out_y0, out_x1, out_y1)

    # ── 端到端求解 ────────────────────────────────────────────────

    def solve(
        self,
        unit: TranslationUnit,
        translated_text: str,
        page_rect: Optional["GlyphBBox"] = None,
        font_size: Optional[float] = None,
        container_width: Optional[float] = None,
    ) -> SolvedUnit:
        """完整三阶段求解：source → translated → render。"""
        translated_bbox, line_count, lines, formula_placements = self.translated_box(
            unit, translated_text, font_size, container_width
        )
        fs = font_size or self._main_font(unit)
        render_bbox = self.render_box(translated_bbox, page_rect, fs)
        return SolvedUnit(
            unit_id=unit.unit_id,
            source_bbox=unit.source_bbox,
            translated_bbox=translated_bbox,
            render_bbox=render_bbox,
            font_size=fs,
            line_count=line_count,
            lines=lines,
            formula_placements=formula_placements,
        )

    @staticmethod
    def _main_font(unit: TranslationUnit) -> float:
        sizes = [getattr(o, "font_size", 0) for o in unit.inline_structure]
        sizes = [s for s in sizes if s]
        return max(sizes) if sizes else 12.0


__all__ = ["SolvedUnit", "LayoutSolver"]
