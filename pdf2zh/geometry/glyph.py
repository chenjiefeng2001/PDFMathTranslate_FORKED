"""P5.1 — Glyph: PDF 原生字形的原子级几何元（不可变记录）。

对应规范书 §4.1：保留原生 PDF 解析的原子级物理属性（字符、包围盒、
基线、字面升部/降部、字体名、字号、页面/对象 ID）。

提取适配器：
  * ``extract_glyphs_from_ltpage`` —— pdfminer ``LTChar`` 流（与 legacy
    converter / Geometry Engine 消费同一份字符流，V8.3 收敛点）；
  * ``extract_glyphs_from_page``  —— pymupdf ``rawdict`` 页面对象。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

# (x0, y0, x1, y1)，PDF y-up 坐标系
GlyphBBox = Tuple[float, float, float, float]


@dataclass(frozen=True)
class Glyph:
    """PDF 原生字形元数据（frozen：公式几何不可变性的载体）。"""

    char: str
    bbox: GlyphBBox                       # (x0, y0, x1, y1) y-up
    baseline: float                       # 主基线 y 坐标（y-up）
    ascent: float                         # 字面升部（正数，相对基线向上）
    descent: float                        # 字面降部（负数，相对基线向下）
    font_name: str
    font_size: float
    page_id: int
    object_id: int

    # ── 派生几何 ────────────────────────────────────────────────────
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
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    def to_dict(self) -> dict:
        return {
            "char": self.char,
            "bbox": [round(v, 2) for v in self.bbox],
            "baseline": round(self.baseline, 2),
            "ascent": round(self.ascent, 2),
            "descent": round(self.descent, 2),
            "font_name": self.font_name,
            "font_size": round(self.font_size, 2),
            "page_id": self.page_id,
            "object_id": self.object_id,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"Glyph({self.char!r} {self.bbox} base={self.baseline:.1f} "
                f"{self.font_name}/{self.font_size:.1f})")


# ── 字体度量近似 ────────────────────────────────────────────────────────


def _safe_font_metric(font, method: str, default: float) -> float:
    """容错读取字体度量（get_ascent/get_descent），失败返回默认值。"""
    try:
        value = float(getattr(font, method)())
    except (AttributeError, TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return value


def _baseline_of_ltchar(child) -> Tuple[float, float, float]:
    """从 pdfminer LTChar 推导 (baseline, ascent, descent)。

    pdfminer 的 LTChar 不暴露 baseline；用字体降部 × 字号还原：
    baseline = y0 - descent × size（descent 为负，故基线在 y0 上方）。
    """
    size = float(getattr(child, "size", 12.0) or 12.0)
    font = getattr(child, "font", None)
    ascent = size * _safe_font_metric(font, "get_ascent", 0.8)
    descent = size * _safe_font_metric(font, "get_descent", -0.25)
    baseline = float(getattr(child, "y0", 0.0)) - descent
    return baseline, ascent, descent


def _iter_ltchars(obj, _depth: int = 0) -> Iterator:
    """递归遍历 pdfminer 布局树，产出所有 LTChar 叶子。"""
    if _depth > 24:  # 防御深层嵌套（LTFigure/LTTextBox）
        return
    if hasattr(obj, "get_text") and hasattr(obj, "size"):
        yield obj
        return
    for child in getattr(obj, "__iter__", lambda: iter(()))():
        yield from _iter_ltchars(child, _depth + 1)


def extract_glyphs_from_ltpage(ltpage, page_id: Optional[int] = None,
                               skip_whitespace: bool = True) -> List[Glyph]:
    """从 pdfminer LTPage 提取 Glyph 流（与 legacy receive_layout 同源）。

    ``page_id`` 缺省时取 ``ltpage.pageid``；``skip_whitespace`` 为真时
    跳过纯空白字形（不影响文本重构，避免大量空格 Glyph）。
    """
    if page_id is None:
        page_id = int(getattr(ltpage, "pageid", 0) or 0)
    glyphs: List[Glyph] = []
    obj_id = 0
    for child in _iter_ltchars(ltpage):
        try:
            text = child.get_text() or ""
        except Exception:  # noqa: BLE001
            continue
        if skip_whitespace and not text.strip():
            continue
        if not text:
            continue
        baseline, ascent, descent = _baseline_of_ltchar(child)
        try:
            bbox = tuple(float(v) for v in (child.x0, child.y0, child.x1, child.y1))
        except (AttributeError, TypeError, ValueError):
            continue
        glyphs.append(Glyph(
            char=text,
            bbox=bbox,
            baseline=baseline,
            ascent=ascent,
            descent=descent,
            font_name=str(getattr(child, "fontname", "") or ""),
            font_size=float(getattr(child, "size", 12.0) or 12.0),
            page_id=page_id,
            object_id=obj_id,
        ))
        obj_id += 1
    return glyphs


def extract_glyphs_from_page(page, page_id: int = 0,
                             skip_whitespace: bool = True) -> List[Glyph]:
    """从 pymupdf 页面对象（rawdict）提取 Glyph 流。"""
    glyphs: List[Glyph] = []
    raw = page.get_text("rawdict")
    obj_id = 0
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_size = float(span.get("size", 12.0) or 12.0)
                span_font = str(span.get("font", ""))
                span_bbox = tuple(float(v) for v in span.get("bbox", (0, 0, 0, 0)))
                for ch in span.get("chars", []):
                    cb = ch.get("bbox") or span_bbox
                    text = ch.get("c", "")
                    if skip_whitespace and not text.strip():
                        continue
                    if not text:
                        continue
                    bbox = tuple(float(v) for v in cb)
                    # pymupdf 不直接给每字符基线；用 bbox 下缘 - 降部近似
                    descent = -0.25 * span_size
                    baseline = bbox[1] - descent
                    glyphs.append(Glyph(
                        char=text,
                        bbox=bbox,
                        baseline=baseline,
                        ascent=0.8 * span_size,
                        descent=descent,
                        font_name=span_font,
                        font_size=span_size,
                        page_id=page_id,
                        object_id=obj_id,
                    ))
                    obj_id += 1
    return glyphs


__all__ = [
    "Glyph", "GlyphBBox",
    "extract_glyphs_from_ltpage", "extract_glyphs_from_page",
]

