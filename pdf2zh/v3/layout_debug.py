"""Module: LayoutDebug — Phase D4 排版 Debug（编辑器式几何标注）。

给定一元化的 page/快照，为每行计算并导出排版诊断度量：
BBox · Baseline · LineHeight · Ascender · Descender · 字号 ——
并渲染为 SVG/HTML 标注图（bbox 描边 + 基线红线 + asc/desc 刻度线）。

    from pdf2zh.v3.layout_debug import (
        line_metrics_from_page, line_metrics_from_snapshot,
        render_svg, render_html,
    )
    metrics = line_metrics_from_page(page)
    svg = render_svg(metrics, page_width, page_height)

纯计算 + 字符串产出，无 I/O 副作用。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class LineMetrics:
    node_id: str = ""
    text: str = ""
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    baseline: float = 0.0
    line_height: float = 0.0
    ascender: float = 0.0
    descender: float = 0.0
    font_size: float = 0.0
    glyph_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "text": self.text,
                "bbox": [round(v, 2) for v in self.bbox],
                "baseline": round(self.baseline, 2),
                "line_height": round(self.line_height, 2),
                "ascender": round(self.ascender, 2),
                "descender": round(self.descender, 2),
                "font_size": round(self.font_size, 2),
                "glyph_count": self.glyph_count}


def _line_metrics(pno: int, bi: int, li: int, line) -> LineMetrics:
    glyphs = [g for s in line.spans for g in s.glyphs]
    ys = [g.y0 for g in glyphs] + [g.y1 for g in glyphs]
    base = float(line.baseline or 0.0)
    top = max(ys) if ys else 0.0
    bottom = min(ys) if ys else 0.0
    sizes = [s.size for s in line.spans if s.size > 0]
    size = max(sizes) if sizes else 0.0
    if not glyphs and size:
        base = float(line.y1 or 0.0)
        top, bottom = base + size * 0.8, base - size * 0.2
    asc = top - base
    desc = base - bottom
    lh = (top - bottom) if glyphs else (size * 1.2 if size else 0.0)
    return LineMetrics(
        node_id=f"P{pno}::B{bi}::L{li}",
        text=line.text or "",
        bbox=(float(line.x0 or 0.0), float(line.y0 or 0.0),
              float(line.x1 or 0.0), float(line.y1 or 0.0)),
        baseline=base, line_height=lh, ascender=asc,
        descender=desc, font_size=size, glyph_count=len(glyphs))


def line_metrics_from_page(page, page_num: Optional[int] = None) -> List[LineMetrics]:
    """PageModel（canonical_page）→ 每行排版度量。"""
    pno = page_num if page_num is not None else int(getattr(page, "page_num", 0) or 0)
    out: List[LineMetrics] = []
    for i, block in enumerate(page.blocks):
        for j, line in enumerate(block.lines):
            out.append(_line_metrics(pno, i, j, line))
    return out


def line_metrics_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> List[LineMetrics]:
    """快照 dict → 每行度量（无 glyph 时用 spans size 近似）。"""
    out: List[LineMetrics] = []
    if not snapshot:
        return out
    for nid, node in (snapshot.get("nodes") or {}).items():
        if "lines" not in node:
            continue
        for ln, ldata in (node.get("lines") or {}).items():
            sizes = [sp.get("size", 0.0) for sp in ldata.get("spans", [])]
            size = max(sizes) if sizes else 0.0
            x0, y0 = ldata.get("x0", 0.0), ldata.get("y0", 0.0)
            x1, y1 = ldata.get("x1", 0.0), ldata.get("y1", 0.0)
            baseline = float(ldata.get("baseline", y0))
            lh = (y1 - y0) if (y1 and y0) else (size * 1.2 if size else 0.0)
            asc = baseline - (y1 if baseline else 0.0) if baseline else 0.0
            desc = 0.0
            if baseline and y0:
                asc = (y1 or baseline) - baseline
                desc = baseline - y0
            out.append(LineMetrics(
                node_id=f"{nid}::{ln}", text=ldata.get("text", ""),
                bbox=(x0, y0, x1, y1), baseline=baseline,
                line_height=lh, ascender=asc, descender=desc,
                font_size=size, glyph_count=0))
    return out


def render_svg(metrics: List[LineMetrics], width: float = 600.0,
               height: float = 800.0, flip_y: bool = True) -> str:
    """把排版度量渲染成 SVG 标注字符串。坐标一致，y 默认翻转成自顶向下。"""
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width:.1f}" height="{height:.1f}" '
        f'viewBox="0 0 {width:.1f} {height:.1f}" '
        f'font-family="monospace">',
        f'<rect x="0" y="0" width="{width:.1f}" height="{height:.1f}" '
        f'fill="#ffffff" stroke="#bbbbbb"/>']
    for m in metrics:
        x0, y0, x1, y1 = m.bbox
        if flip_y:
            y0, y1 = (float(height) - y0), (float(height) - y1)
            baseline = float(height) - m.baseline
        else:
            baseline = m.baseline
        top, bottom = min(y0, y1), max(y0, y1)
        parts.append(
            f'<rect x="{x0:.1f}" y="{top:.1f}" width="{max(0.0, x1 - x0):.1f}" '
            f'height="{max(0.0, bottom - top):.1f}" fill="rgba(38,105,190,0.12)" '
            f'stroke="#3b82c4" stroke-width="1"/>')
        parts.append(
            f'<line x1="{x0:.1f}" y1="{baseline:.1f}" x2="{x1:.1f}" '
            f'y2="{baseline:.1f}" stroke="#e53935" stroke-width="1.2"/>')
        # ascender（baseline 上方 = 更小 y）与 descender（下方）刻度
        asc_y = baseline - m.ascender
        desc_y = baseline + m.descender
        parts.append(
            f'<line x1="{x0 - 2:.1f}" y1="{asc_y:.1f}" x2="{x0 - 2:.1f}" '
            f'y2="{baseline:.1f}" stroke="#8e24aa" stroke-width="1"/>')
        parts.append(
            f'<line x1="{x0 + 2:.1f}" y1="{desc_y:.1f}" x2="{x0 + 2:.1f}" '
            f'y2="{baseline:.1f}" stroke="#8e24aa" stroke-width="1"/>')
        label = (m.text or "")[:16]
        if label:
            label_y = (min(y0, y1) - 3) if not flip_y else (max(y0, y1) + 3)
            parts.append(
                f'<text x="{x0 + 2:.1f}" y="{label_y:.1f}" '
                f'font-size="9" fill="#333">{esc(label)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(metrics: List[LineMetrics], width: float = 600.0,
                height: float = 800.0) -> str:
    """排版 Debug 的独立 HTML 查看器（内嵌 SVG + 度量表）。"""
    svg = render_svg(metrics, width, height)
    rows = "".join(
        f"<tr><td>{esc(m.node_id)}</td><td>{esc(m.text[:32])}</td>"
        f"<td>{','.join(f'{v:.0f}' for v in m.bbox)}</td>"
        f"<td>{m.baseline:.1f}</td><td>{m.line_height:.1f}</td>"
        f"<td>{m.ascender:.1f}</td><td>{m.descender:.1f}</td>"
        f"<td>{m.font_size:.1f}</td></tr>"
        for m in metrics)
    return f"""<!doctype html><meta charset="utf-8"><title>LayoutDebug</title>
<body style="font-family:monospace">
<h3>Layout Debug ({len(metrics)} lines)</h3>{svg}<table border="1"
cellspacing="0"><tr><th>node</th><th>text</th><th>bbox</th><th>baseline</th>
<th>line-height</th><th>ascender</th><th>descender</th><th>size</th></tr>
{rows}</table></body>"""


def metrics_json(metrics: List[LineMetrics]) -> str:
    return json.dumps([m.to_dict() for m in metrics], ensure_ascii=False,
                      indent=2)


__all__ = ["LineMetrics", "line_metrics_from_page",
           "line_metrics_from_snapshot", "render_svg", "render_html",
           "metrics_json", "esc"]