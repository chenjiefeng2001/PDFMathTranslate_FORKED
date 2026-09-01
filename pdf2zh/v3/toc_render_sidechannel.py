"""TOC render side-channel — Commit 6C 接线（structured entries → render commands）。

把语义层的视觉 TOC（``pdf2zh.semantic.renderer.toc.TocRenderer``）接进 v3
渲染链，同时保持 ``converter.py`` 的 strangulation gate 不变：所有 TOC 渲染
编排逻辑定义在这里与 ``pdf2zh.semantic.renderer.toc``。

- :func:`build_block_toc_payload` — 一个 ``kind == \"toc\"`` 且带
  ``metadata[\"toc_entries\"]`` 的块 → JSON 安全渲染载荷 ``{commands,
  translated_calls}``。**只有 title_only 进 translator**；numbering prefix /
  dot leader / page number / 几何一律不进 translator。水平几何（title_x /
  page_x）直接来自条目；垂直基线取块内行 bbox 的中位 y（v3 左下原点），
  缺失时回退条目序索引步进。
- :func:`build_page_toc_payload` — 通用入口：行文本 + 几何 + 翻译回调。

输出与 ``render_plan_from_model`` 透传给 ``magicpdf_renderer`` 的载荷同构，
供其消费：TOC 块不再按普通段落整块换行排版，而是逐条目落位。
"""

from __future__ import annotations

import logging
from typing import Callable, Mapping, Sequence

from pdf2zh.semantic.renderer.toc import TocRenderer

log = logging.getLogger(__name__)

__all__ = ["build_block_toc_payload", "build_page_toc_payload"]


def _entry_baselines(entries: Sequence[Mapping], block) -> list[float]:
    """提取每个条目的垂直基线（v3 左下原点）。

    优先用块内行 bbox 的中位 y（与阅读序一致），缺失时回退
    ``index * line_step``。返回按条目序的 ``[y0, y1, ...]``。
    """
    ys: list[float] = []
    lines = list(getattr(block, "lines", None) or [])
    for i, e in enumerate(entries or []):
        if i < len(lines):
            ln = lines[i]
            try:
                base = (
                    float(getattr(ln, "y0", 0.0) or 0.0)
                    + float(getattr(ln, "y1", 0.0) or 0.0)
                ) / 2.0
            except (TypeError, ValueError):
                base = 0.0
            ys.append(base)
        else:
            ys.append(float(i * 14.0))
    return ys


def build_page_toc_payload(
    lines: Sequence[Mapping],
    page_width: float,
    *,
    translate: Callable[[str], str] | None = None,
    size: float = 10.0,
) -> dict:
    """对一页的行序列运行完整 ``detect -> parse -> translate -> render``。

    Args:
        lines: 行文本 + 几何（``{text, x0, x1, size}``，阅读顺序）。
        page_width: 页宽（pt）。
        translate: **仅 title_only 的** 翻译回调；number/leader/page 不会被调用。
        size: 标称字号。

    Returns:
        ``build_toc_render`` 的渲染载荷；非 TOC 页时 ``commands`` 为空。
    """
    try:
        from pdf2zh.semantic.renderer.toc import build_page_toc_plan

        return build_page_toc_plan(
            lines,
            float(page_width),
            translate=translate,
            size=float(size),
        )
    except Exception as exc:  # noqa: BLE001 -- TOC 侧信道失败不影响主链
        log.debug("toc_render_sidechannel: page payload failed: %s", exc)
        return {"tree": None, "entries": [], "commands": [], "translated_calls": []}


def build_block_toc_payload(
    block,
    translate: Callable[[str], str] | None = None,
    size: float = 10.0,
) -> dict:
    """一个 v3 ``BlockModel``（kind == \"toc\" 且带 toc_entries）→ 渲染载荷。

    条目的 number/title_only 已由 ``document_model.translate_document`` 回填
    （含 translated_title）。这里只负责把结构化条目 + 几何编译成渲染命令。
    块无条目 / 无标题时返回空载荷 —— 上层回退到普通块渲染。
    """
    entries = list((getattr(block, "metadata", {}) or {}).get("toc_entries") or [])
    if not entries:
        return {"commands": [], "translated_calls": []}
    ys = _entry_baselines(entries, block)
    try:
        renderer = TocRenderer()
        calls: list[str] = []

        def _tr(s: str) -> str:
            calls.append(s)
            return (translate or (lambda t: t))(s)

        cmds = renderer.render(entries, ys=ys, size=size, translate=_tr)
        return {
            "commands": [c.to_dict() for c in cmds],
            "translated_calls": list(calls),
        }
    except Exception as exc:  # noqa: BLE001 -- 渲染载荷失败不阻塞主链
        log.debug("toc_render_sidechannel: block payload failed: %s", exc)
        return {"commands": [], "translated_calls": []}
