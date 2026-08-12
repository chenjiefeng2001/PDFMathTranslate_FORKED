# -*- coding: utf-8 -*-
"""F2：接管段真实译文渲染求解（P2 display 垂直流 + P4 render_bbox 真实化）。

P1 适配器用**恒等译文**求 ``SolvedUnit.render_bbox``，真实译文更长时几何失真。
本模块在 legacy 渲染循环之前，对**已接管段落**（``reconstruction_adoptions``
的 ``pairs``）用**真实译文**再跑三阶段求解：

- 把 solver 的 ``render_bbox`` 回写 ``pstk``（``gen_op_txt`` 实际消费的几何），
  使接管段几何随译文长度真实变化（P4）。
- 标记 display 公式（``{vN}`` → 块级展示公式），供 converter 垂直流推进
  （P2：display 公式独占一行按物理高度下推后续文本行）。

失败仅 debug 日志，回退 adapter 几何（零回归）。
"""
from __future__ import annotations

from typing import Dict, List

from pdf2zh.v3.reconstruction_adapter import (
    _ANCHOR_FORMULA_RE, _LEGACY_FORMULA_RE)

__all__ = ["run_render_resolve", "build_display_marks"]


def _anchor_num(token) -> int:
    _m = _ANCHOR_FORMULA_RE.match(str(token))
    return int(_m.group(1)) if _m else -1


def _legacy_to_unit_text(text: str, token_keys: List[str]) -> str:
    """legacy ``{vN}`` → 段内 ``<formula_k>`` token（按出现顺序对齐）。

    legacy ``{vN}`` 是页级编号，unit 的 ``formula_map`` 是段内编号；两者
    顺序一一对应：段内第 k 个 ``{vN}`` ↔ 第 k 个 ``<formula_k>``。
    """
    _kv = [0]

    def _sub(_m):
        if _kv[0] < len(token_keys):
            _tok = token_keys[_kv[0]]
        else:
            _tok = _m.group(0)
        _kv[0] += 1
        return _tok

    return _LEGACY_FORMULA_RE.sub(_sub, str(text))


def build_display_marks(conv, ltpage, sstk, pstk, news) -> Dict[int, bool]:
    """对接管段用真实译文重新求解；返回 display 公式标记 ``{vid: True}``。

    副作用：
    - 把 solver 的 ``render_bbox`` 回写 ``pstk[li]``（P4 几何真实化）；
    - 记录接管段源区域 ``conv._render_source_bboxes[pageid][li]``（F3 白底
      覆盖用：擦除旧图层再绘制译文，杜绝「原文/公式背景与译文重叠」）。
    """
    from pdf2zh.layout.solver import LayoutSolver

    pageid = getattr(ltpage, "pageid", 0)
    adopt = (getattr(conv, "reconstruction_adoptions", {}) or {}).get(
        pageid) or {}
    pairs = adopt.get("pairs") or []
    result = (getattr(conv, "reconstruction_results", {}) or {}).get(pageid)
    display_marks: Dict[int, bool] = {}
    source_bboxes: Dict[int, list] = {}
    if not pairs or result is None or not getattr(
            result, "translation_units", None):
        return display_marks
    page_rect = None
    _pr = getattr(conv, "_page_rect", None)
    if _pr is not None:
        page_rect = (_pr.x0, _pr.y0, _pr.x1, _pr.y1)
    solver = LayoutSolver()
    for (li, _le, ridx) in pairs:
        if li >= len(pstk) or ridx >= len(result.translation_units):
            continue
        para = pstk[li]
        unit = result.translation_units[ridx]
        vids = [int(m.group(1)) for m in
                _LEGACY_FORMULA_RE.finditer(str(sstk[li]))]
        token_keys = sorted(getattr(unit, "formula_map", {}) or {},
                            key=_anchor_num)
        if token_keys:
            real_text = _legacy_to_unit_text(str(news[li]), token_keys)
        else:
            real_text = str(news[li])
        solved = solver.solve(
            unit, real_text, page_rect=page_rect,
            font_size=float(getattr(para, "size", 12.0) or 12.0),
            container_width=max(1.0, float(para.x1) - float(para.x0)))
        rb = solved.render_bbox
        para.y, para.x, para.x0, para.x1, para.y0, para.y1 = (
            rb[1], rb[0], rb[0], rb[2], rb[1], rb[3])
        source_bboxes[li] = [round(v, 2) for v in solved.source_bbox]
        for fp in (solved.formula_placements or []):
            if fp.get("display") and fp.get("anchor"):
                n = _anchor_num(fp["anchor"])
                if 0 <= n < len(vids):
                    display_marks[vids[n]] = True
    conv._render_source_bboxes = {
        **getattr(conv, "_render_source_bboxes", {}),
        pageid: source_bboxes}
    return display_marks


def run_render_resolve(conv, ltpage, sstk, pstk, news) -> None:
    """F2/F3 入口：构建 display 标记并存入 ``conv._render_display_marks``。"""
    pageid = getattr(ltpage, "pageid", 0)
    if not getattr(conv, "reconstruction_channel", False):
        return
    marks = build_display_marks(conv, ltpage, sstk, pstk, news)
    conv._render_display_marks = {
        **getattr(conv, "_render_display_marks", {}),
        pageid: marks}
