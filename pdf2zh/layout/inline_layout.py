"""P7 — Unified Inline Layout Model（规范书 §4.3 / §6.2 / §7）。

统一行内布局数据模型：Text / Formula 作为并列的一等行内对象，
参与段落级 Inline 排版。

    InlineTextRun   # 普通文本段（含 style_runs）
    FormulaObject   # 公式对象（P6 产出，几何不可变）
    InlineObject    # = InlineTextRun | FormulaObject（联合）
    TranslationUnit # 翻译与排版单元（§4.4）
    InlineLayoutEngine  # 行内混合排版模型

三阶段坐标（§6.2）：
    source_bbox → translated_bbox → render_bbox
本模块负责 Inline 换行与对象序列化；三阶段坐标计算在 solver.py（P9）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pdf2zh.geometry.glyph import GlyphBBox
from pdf2zh.layout.baseline import BaselineMetrics, BaselineComputer

# 行内对象联合类型（P6 FormulaObject 经 __init__ 统一导出避免循环导入）
try:  # pragma: no cover - 防循环导入的优雅降级
    from pdf2zh.formula.extractor import FormulaObject, InlineTextRun
except ImportError:  # pragma: no cover
    FormulaObject = object
    InlineTextRun = object


@dataclass
class TranslationUnit:
    """翻译与排版单元（规范书 §4.4）。"""

    unit_id: str
    page_id: int
    source_text_with_anchors: str        # 例: "Let <formula_0> be computable."
    formula_map: Dict[str, object] = field(default_factory=dict)
    source_bbox: GlyphBBox = (0.0, 0.0, 0.0, 0.0)
    inline_structure: List = field(default_factory=list)  # InlineObject[]
    master_baseline: float = 0.0
    source_line_baselines: List[float] = field(default_factory=list)
    """源各视觉行的主基线（P8 Baseline-aware 行级求解依据，保持相对几何）。"""

    @property
    def text(self) -> str:
        return self.source_text_with_anchors

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "page_id": self.page_id,
            "source_text_with_anchors": self.source_text_with_anchors,
            "formula_ids": list(self.formula_map.keys()),
            "source_bbox": [round(v, 2) for v in self.source_bbox],
            "inline_object_count": len(self.inline_structure),
        }


@dataclass
class InlineSegment:
    """排版后的一行内的一个片段（文本或公式）。"""

    kind: str                            # "text" | "formula"
    text: str
    width: float
    formula_id: Optional[str] = None
    baseline_offset: float = 0.0         # 相对行主基线的偏移（公式上下标）
    display: bool = False                # 块级展示公式（独占一行，独立垂直块）

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "width": round(self.width, 2),
            "formula_id": self.formula_id,
            "baseline_offset": round(self.baseline_offset, 2),
            "display": self.display,
        }


@dataclass
class LayoutLine:
    """换行后的一行（保留源语义锚点 + 几何）。"""

    segments: List[InlineSegment] = field(default_factory=list)
    width: float = 0.0
    master_baseline: float = 0.0
    glyphs: List = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "width": round(self.width, 2),
            "master_baseline": round(self.master_baseline, 2),
            "segments": [s.to_dict() for s in self.segments],
        }


class InlineLayoutEngine:
    """Inline 混合排版模型：把 TranslationUnit 拆成换行后的 LayoutLine 序列。"""

    def __init__(self, char_width_ratio: float = 0.5,
                 cjk_width_ratio: float = 1.0,
                 space_width: float = 0.28,
                 formula_scale: float = 1.0) -> None:
        self.char_width_ratio = char_width_ratio   # 拉丁/数字半角宽度比例
        self.cjk_width_ratio = cjk_width_ratio     # CJK 全角宽度比例
        self.space_width = space_width
        self.formula_scale = formula_scale         # 公式对象宽度缩放（几何锁定）

    # ── 宽度估计 ──────────────────────────────────────────────────

    def text_width(self, text: str, font_size: float) -> float:
        """估算文本段渲染宽度（CJK 全角、其余半角）。"""
        w = 0.0
        for c in text:
            if "\u4e00" <= c <= "\u9fff" or "\u3000" <= c <= "\u303f" \
                    or c in "，。；：、（）【】《》“”‘’！？":
                w += font_size * self.cjk_width_ratio
            elif c.isspace():
                w += font_size * self.space_width
            else:
                w += font_size * self.char_width_ratio
        return w

    def object_width(self, obj, font_size: float) -> float:
        """InlineObject 的渲染宽度：公式按源 bbox 宽度×scale，文本按字形。"""
        if isinstance(obj, FormulaObject):
            return max(obj.width * self.formula_scale, font_size * 0.6)
        if isinstance(obj, InlineTextRun):
            return max(self.text_width(obj.text, font_size), obj.width)
        # 兜底
        return self.text_width(getattr(obj, "text", ""), font_size)

    def baseline_metrics(self, obj) -> BaselineMetrics:
        """InlineObject 的主基线几何（公式/文本共用 Glyph 来源）。"""
        glyphs = list(getattr(obj, "glyphs", []))
        if not glyphs and isinstance(obj, InlineTextRun):
            return BaselineMetrics(master_baseline=0.0, ascent=obj.font_size * 0.8,
                                   descent=-obj.font_size * 0.2,
                                   line_height=obj.font_size)
        return BaselineComputer.compute(glyphs)

    # ── 换行 ──────────────────────────────────────────────────────

    def wrap(self, objects: Sequence,
             container_width: float,
             font_size: float,
             max_lines: Optional[int] = None) -> List[LayoutLine]:
        """贪心换行：把 InlineObject 序列按容器宽度拆成 LayoutLine。

        公式对象不折行（整体不可分割），只在其宽度超过容器时独立成行。
        **Display 块级展示公式（``is_display_mode``）独占一行**：break
        before/after，作为独立垂直块参与 P9 垂直流堆叠 —— 不在行内与
        文本流式混排，从源头杜绝「展示公式被拉进行首/行尾」。
        """
        lines: List[LayoutLine] = []
        current = LayoutLine()
        current_width = 0.0
        for i, obj in enumerate(objects):
            obj_width = self.object_width(obj, font_size)
            if isinstance(obj, InlineTextRun):
                # 文本段：按 "\n" 切分（保留源行结构），必要时字符级折行
                chunks = obj.text.split("\n")
                for ci, chunk in enumerate(chunks):
                    if ci > 0:
                        if current.segments:
                            lines.append(current)
                            current = LayoutLine()
                            current_width = 0.0
                            if max_lines and len(lines) >= max_lines:
                                break
                    if not chunk:
                        continue
                    cw = self.text_width(chunk, font_size)
                    if cw > container_width:
                        for c in chunk:
                            cchw = self.text_width(c, font_size)
                            if current.segments and current_width + cchw > container_width:
                                lines.append(current)
                                current = LayoutLine()
                                current_width = 0.0
                                if max_lines and len(lines) >= max_lines:
                                    break
                            seg = InlineSegment(kind="text", text=c, width=cchw)
                            current.segments.append(seg)
                            current.width = current_width + cchw
                            current_width = current.width
                    else:
                        if current.segments and current_width + cw > container_width:
                            lines.append(current)
                            current = LayoutLine()
                            current_width = 0.0
                            if max_lines and len(lines) >= max_lines:
                                break
                        seg = InlineSegment(kind="text", text=chunk, width=cw)
                        current.segments.append(seg)
                        current.width = current_width + cw
                        current_width = current.width
                continue
            # 公式对象（含 Display 块级展示公式）
            display = bool(getattr(obj, "is_display_mode", False))
            if (display and current.segments
                    and not all(getattr(s, "display", False)
                                for s in current.segments)):
                # break before：仅在当前行含非 display 内容时触发，
                # 连续 display 公式保持同一行（整行公式多段场景）。
                lines.append(current)
                current = LayoutLine()
                current_width = 0.0
                if max_lines and len(lines) >= max_lines:
                    break
            if (not display and current.segments
                    and current_width + obj_width > container_width):
                lines.append(current)
                current = LayoutLine()
                current_width = 0.0
                if max_lines and len(lines) >= max_lines:
                    break
            kind = "formula" if isinstance(obj, FormulaObject) else "text"
            seg = InlineSegment(
                kind=kind,
                text=getattr(obj, "text", ""),
                width=obj_width,
                formula_id=getattr(obj, "formula_id", None),
                baseline_offset=0.0,
                display=display,
            )
            current.segments.append(seg)
            current.width = current_width + obj_width
            current_width = current.width
            if hasattr(obj, "glyphs"):
                current.glyphs.extend(list(obj.glyphs))
            if display:
                # break after：连续 display 公式合并为同一行（整行公式被
                # style_runs 切成多段时保持一行）；遇非 display 对象才 flush。
                nxt = objects[i + 1] if i + 1 < len(objects) else None
                nxt_display = bool(
                    isinstance(nxt, FormulaObject)
                    and getattr(nxt, "is_display_mode", False))
                if not nxt_display:
                    lines.append(current)
                    current = LayoutLine()
                    current_width = 0.0
                    if max_lines and len(lines) >= max_lines:
                        break
        if current.segments and (not max_lines or len(lines) < max_lines):
            lines.append(current)
        # 计算每行主基线（字形加权）
        for line in lines:
            if line.glyphs:
                line.master_baseline = BaselineComputer.compute(line.glyphs).master_baseline
            elif line.segments:
                line.master_baseline = 0.0
        return lines


def build_translation_unit(para, formula_prefix: str = "formula",
                           anchor_text: Optional[str] = None) -> TranslationUnit:
    """把 LogicalParagraph（inline_objects 已填充）转成 TranslationUnit。

    ``anchor_text`` 为已注入 ``<formula_x>`` 的语义文本；缺省时从
    ``para.inline_objects`` 现场生成。
    """
    objects = list(getattr(para, "inline_objects", []) or [])
    line_objects = getattr(para, "_line_objects", None)
    text = anchor_text
    formula_map: Dict[str, object] = {}
    if text is None:
        parts: List[str] = []
        fidx = 0
        if line_objects:
            # 按行拼接，行间以 "\n" 分隔（保留源段落行结构，§6.2）
            for i, objs in enumerate(line_objects):
                if i > 0:
                    parts.append("\n")
                for obj in objs:
                    if isinstance(obj, FormulaObject):
                        token = f"<formula_{fidx}>"
                        formula_map[token] = obj
                        parts.append(token)
                        fidx += 1
                    else:
                        parts.append(getattr(obj, "text", ""))
        else:
            for obj in objects:
                if isinstance(obj, FormulaObject):
                    token = f"<formula_{fidx}>"
                    formula_map[token] = obj
                    parts.append(token)
                    fidx += 1
                else:
                    parts.append(getattr(obj, "text", ""))
        text = "".join(parts)
    return TranslationUnit(
        unit_id=f"{formula_prefix}_unit_{para.paragraph_id}",
        page_id=para.page_id,
        source_text_with_anchors=text,
        formula_map=formula_map,
        source_bbox=para.bbox,
        inline_structure=objects,
        master_baseline=para.master_baseline,
        source_line_baselines=[line.master_baseline for line in para.lines],
    )


__all__ = [
    "TranslationUnit", "InlineSegment", "LayoutLine", "InlineLayoutEngine",
    "build_translation_unit",
]
