"""Module: OverlayView — Phase D3 角色 Overlay（编译器式 Debug 可视化）。

把每一块的 kind/role 画到页面坐标上：Heading 绿 / TOC 蓝 / Formula 黄 /
Image 红 / Caption 青 / Table 紫 —— 一眼看出布局分段去了哪。

    from pdf2zh.v3.overlay_view import (
        overlay_for_page, render_svg, render_html, role_for_block,
    )
    records = overlay_for_page(page)
    svg = render_svg(records, page.width, page.height)

纯计算 + 字符串导出；块 bbox 缺失（全 0）时跳过，杜绝空矩形噪音。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pdf2zh.v3.observability import ROLE_COLORS

_DEFAULT_COLOR = "#9e9e9e"


@dataclass
class OverlayRecord:
    node_id: str = ""
    kind: str = ""
    role: str = ""
    bbox: tuple = (0.0, 0.0, 0.0, 0.0)
    text: str = ""
    color: str = _DEFAULT_COLOR

    def to_dict(self) -> Dict[str, Any]:
        x0, y0, x1, y1 = [round(float(v), 2) for v in self.bbox]
        return {"node_id": self.node_id, "kind": self.kind,
                "role": self.role, "bbox": [x0, y0, x1, y1],
                "text": self.text[:40], "color": self.color}


def _color_for(kind: str, role: Optional[str]) -> str:
    key = role or kind or ""
    if key in ROLE_COLORS:
        return ROLE_COLORS[key]
    return ROLE_COLORS.get(kind, _DEFAULT_COLOR)


def overlay_for_page(page, page_num: Optional[int] = None) -> List[OverlayRecord]:
    """PageModel → 每块 Overlay 记录（跳过全 0 bbox）。"""
    pno = page_num if page_num is not None else int(getattr(page, "page_num", 0) or 0)
    out: List[OverlayRecord] = []
    for i, block in enumerate(page.blocks):
        x0, y0, x1, y1 = [float(v) for v in block.bbox]
        if x1 <= x0 or y1 <= y0:
            continue
        role = block.metadata.get("role") if block.metadata else None
        out.append(OverlayRecord(
            node_id=f"P{pno}::B{i}",
            kind=block.kind,
            role=role,
            bbox=(x0, y0, x1, y1),
            text=(block.text or "")[:80],
            color=_color_for(block.kind, role)))
    return out


def overlay_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> List[OverlayRecord]:
    """快照 dict 重建 overlay（供离线复盘 / 回放）。"""
    out: List[OverlayRecord] = []
    if not snapshot:
        return out
    for nid, node in (snapshot.get("nodes") or {}).items():
        if "kind" not in node or "lines" not in node:
            continue
        bbox = list(node.get("bbox") or [0, 0, 0, 0])
        if len(bbox) < 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        md = node.get("metadata") or {}
        role = md.get("role")
        out.append(OverlayRecord(
            node_id=nid, kind=node["kind"], role=role,
            bbox=tuple(bbox), text=node.get("text", ""),
            color=_color_for(node["kind"], role)))
    return out


def render_svg(records: List[OverlayRecord], width: float = 600.0,
               height: float = 800.0, legend: bool = True,
               flip_y: bool = True) -> str:
    """SVG overlay：每个块 bbox 半透明填充 + 角色文字，右上角图例。"""
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.1f}" '
        f'height="{height:.1f}" viewBox="0 0 {width:.1f} {height:.1f}" '
        f'font-family="monospace">',
        f'<rect x="0" y="0" width="{width:.1f}" height="{height:.1f}" '
        f'fill="#ffffff" stroke="#ccc"/>']
    for r in records:
        x0, y0, x1, y1 = [float(v) for v in r.bbox]
        if flip_y:
            y0, y1 = (float(height) - y0), (float(height) - y1)
        top, bottom = min(y0, y1), max(y0, y1)
        w = max(0.5, x1 - x0)
        h = max(0.5, bottom - top)
        c = r.color
        parts.append(
            f'<rect x="{x0:.1f}" y="{top:.1f}" width="{w:.1f}" '
            f'height="{h:.1f}" fill="{c}" opacity="0.22" '
            f'stroke="{c}" stroke-width="1.1"/>')
        label = r.role or r.kind or ""
        if label:
            parts.append(
                f'<text x="{x0 + 3:.1f}" y="{(top + 11):.1f}" font-size="9" '
                f'fill="{c}">{esc(label)} {esc(r.node_id.split("::")[-1])}</text>')
    if legend:
        lx, ly = float(width) - 130, 12.0
        for i, (name, col) in enumerate(ROLE_COLORS.items()):
            parts.append(
                f'<rect x="{lx:.1f}" y="{ly:.1f}" width="8" height="8" '
                f'fill="{col}"/>'
                f'<text x="{lx + 11:.1f}" y="{ly + 8:.1f}" font-size="8" '
                f'fill="#333">{esc(name)}</text>')
            ly += 11.0
    parts.append("</svg>")
    return "\n".join(parts)


def render_html(records: List[OverlayRecord], width: float = 600.0,
                height: float = 800.0) -> str:
    svg = render_svg(records, width, height)
    return f"""<!doctype html><meta charset="utf-8"><title>Role Overlay</title>
<body style="font-family:monospace"><h3>Role Overlay ({len(records)} blocks)</h3>
{svg}</body>"""


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def records_json(records: List[OverlayRecord]) -> str:
    return json.dumps([r.to_dict() for r in records], ensure_ascii=False,
                      indent=2)


__all__ = ["OverlayRecord", "overlay_for_page", "overlay_from_snapshot",
           "render_svg", "render_html", "records_json", "esc",
           "_color_for"]