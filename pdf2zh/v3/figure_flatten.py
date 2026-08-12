"""V8.4-F3 — 整页型 Form XObject（LTFigure）文字平铺。

背景：某些 PDF 的正文全部绘制在 Form XObject 内，pdfminer 把它包装成顶层
LTFigure（with.figure.pdf：顶层仅 1 个 LTFigure，4418 字符全在其内）。legacy
``converter.receive_layout`` 只遍历顶层，看不到 LTFigure 内部字符 → ``sstk``
为空 → 整页 0 段不翻译、P5–P10 接管必然失败。

平铺策略（保守）：
  * 保留 LTFigure 本身（供 receive_layout 的 LTFigure 分支登记障碍物）；
  * 仅对**面积 > 70% 页面**的 LTFigure（整页 Form / 装饰层）平铺其内部
    LTChar 进主循环 —— 装饰层无文字时平铺为空，行为不变；
  * 局部 LTFigure（Logo/页眉/插图）保持原状，不平铺避免引入垃圾文本。
"""
from __future__ import annotations

from typing import Iterable

from pdfminer.layout import LTFigure

from pdf2zh.geometry.glyph import _iter_ltchars

# 整页判定阈值：LTFigure 面积 / 页面面积 > 70%
_FULL_PAGE_FIGURE_RATIO = 0.7


def flatten_page_children(ltpage, page_w: float, page_h: float) -> Iterable:
    """平铺页面子元素：yield 原顶层子元素，并对整页型 LTFigure 追加其内部 LTChar。"""
    page_area = max(float(page_w) * float(page_h), 1.0)
    for child in ltpage:
        yield child
        if isinstance(child, LTFigure):
            fig_area = (
                max(float(child.x1 - child.x0), 0.0)
                * max(float(child.y1 - child.y0), 0.0)
            )
            if fig_area > _FULL_PAGE_FIGURE_RATIO * page_area:
                yield from _iter_ltchars(child)
