"""magicpdf 解析路径的渲染接管（Step 3.x）：render_plan → PDF。

可行性报告 §12.3「渲染接管」落地：由
``document_model.render_plan_from_model`` 产出并经
``render_takeover.fixup_render_plan`` 修正的渲染计划，由本模块渲染为 PDF，
使 ``--parse-engine magicpdf`` 从「仅 JSON 转储」升级为「输出译后 mono PDF」。

坐标约定
--------
render_plan 的 ``src_box``/``dst_box`` 采用 v3 规范树坐标系（左下原点、
y 向上，pdfminer 惯例，见 ``magicpdf_bridge.flip_bbox``）；PDF 使用左上原点、
y 向下。本模块统一翻转（``y_flip = page_height - y``）后交给 pymupdf 绘制。

行为
----
- 逐块按 ``dst_box`` 插入译文文本（``insert_textbox`` 矩形内自动换行）；
- 空文本 / 空 plan 安全跳过，输出可打开的 PDF（0 页时不崩溃）；
- 溢出不裁剪、不报错（评测用途；行数估算与下移决策已由 RenderTakeover
  在 fixup 阶段完成）。

纯数据进出：输入 render_plan（list[dict]）+ page_sizes（{pno: [w, h]}），
输出 PDF bytes 与统计；不触碰 legacy converter / BabelDOC 渲染。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_PAGE = (612.0, 792.0)
_DEFAULT_FONT_SIZE = 12.0


def _flip_v3_box(box: Sequence[float], page_height: float) -> list[float]:
    """v3 坐标系（左下原点、y 向上）→ PDF 左上原点、y 向下。"""
    x0, y0, x1, y1 = (float(v) for v in box)
    return [x0, page_height - y1, x1, page_height - y0]


def _entry_text(entry: dict) -> str:
    """取块渲染文本：译文优先（保留块 translated 已由 translate_document
    回填为原文），缺失时回退原文。"""
    translated = entry.get("translated")
    if isinstance(translated, str) and translated.strip():
        return translated
    text = entry.get("text")
    return text or ""


def _insert_text_wrapped(
    page: Any,
    rect: Any,
    text: str,
    font_size: float,
    fontname: Optional[str],
) -> None:
    """在 rect 内手动换行插入文本（兼容 CJK 字体度量）。

    - 按「词」（空白分隔）累积行，行宽用 ``page.get_text_length`` 精确度量；
    - 全角/无空格文本（中文）逐字符累积；
    - 行高 ``font_size * 1.4``，超出 rect 下边界即停止（不裁剪不报错，
      评测用途，后续排版迭代处理截断/换页）。
    """
    line_h = font_size * 1.4
    y = float(rect.y0) + font_size * 0.85
    x = float(rect.x0)
    max_w = max(0.1, float(rect.x1) - float(rect.x0))
    bottom = float(rect.y1)
    import pymupdf

    # pymupdf 内置 CJK 字体对拉丁字符的 advance 偏宽，提取文本时会在字符间
    # 插入多余空格（"x = a" → "x  =  a"）。纯拉丁行回退默认字体（helv），
    # 既保证提取保真；含 CJK 的行仍用中文字体保证显示。
    effective_font = fontname or "helv"
    if effective_font == "china-ss" and all(ord(ch) < 0x2E80 for ch in text):
        effective_font = "helv"

    def _width(s: str) -> float:
        if effective_font == "helv":
            return pymupdf.get_text_length(s, fontsize=font_size)
        # CJK 内置字体（china-ss）对全角/拉丁均近似 1em 等宽，逐字符估算。
        return len(s) * font_size

    tokens = text.split(" ")
    cur = ""
    for tok in tokens:
        sep = " " if cur else ""
        trial = f"{cur}{sep}{tok}"
        if cur and _width(trial) > max_w:
            page.insert_text((x, y), cur, fontsize=font_size, fontname=effective_font)
            y += line_h
            if y > bottom:
                return
            cur = tok
        else:
            cur = trial
    if cur:
        page.insert_text((x, y), cur, fontsize=font_size, fontname=effective_font)


def render_plan_to_pdf(
    plan: Optional[Sequence[dict]],
    page_sizes: Optional[Dict[int, Sequence[float]]] = None,
    output_path: Optional[str] = None,
    font_size_fallback: float = _DEFAULT_FONT_SIZE,
    cjk_font: bool = True,
) -> Tuple[bytes, dict]:
    """把（fixup 后的）render_plan 渲染为 PDF。

    Args:
        plan: ``render_plan_from_model`` 输出的逐块渲染计划（可含
            ``dst_box``/``src_box``/``translated``/``text``/``font_size``）。
        page_sizes: ``{page_num: [width, height]}``；缺失页用 612x792。
        output_path: 非空时同时落盘。
        font_size_fallback: 块未带 ``font_size`` 或非法时使用的字号。
        cjk_font: 为 True 时使用 pymupdf 内置简体中文字体（``china-ss``），
            避免中文译文无法显示；为 False 时用默认字体（纯文本层）。

    Returns:
        ``(pdf_bytes, stats)``，``stats`` 含 ``pages``/``blocks``/``glyphs``。
    """
    import pymupdf

    sizes = dict(page_sizes or {})
    default_page = tuple(_DEFAULT_PAGE)

    by_page: Dict[int, List[dict]] = {}
    for entry in list(plan or []):
        pno = int(entry.get("page") or 0)
        by_page.setdefault(pno, []).append(entry)

    doc = pymupdf.Document()
    stats = {"pages": 0, "blocks": 0, "glyphs": 0}
    fontname = "china-ss" if cjk_font else None

    # 空 plan 也产出至少 1 个空页，保证下游可打开（pymupdf 无 0 页 PDF）。
    if not by_page:
        by_page[0] = []

    for pno in sorted(by_page):
        w, h = sizes.get(pno, default_page)
        if w is None or h is None or float(w) <= 0 or float(h) <= 0:
            w, h = default_page
        w = float(w)
        h = float(h)
        page = doc.new_page(width=w, height=h)
        for entry in by_page[pno]:
            text = _entry_text(entry)
            if not text:
                continue
            box = list(entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0])
            if len(box) != 4:
                box = [0, 0, 0, 0]
            rect = pymupdf.Rect(_flip_v3_box(box, h))
            font_size = entry.get("font_size")
            try:
                font_size = float(font_size) if font_size else 0.0
            except (TypeError, ValueError):
                font_size = 0.0
            if font_size <= 0:
                font_size = float(font_size_fallback) or _DEFAULT_FONT_SIZE
            _insert_text_wrapped(page, rect, text, font_size, fontname)
            stats["blocks"] += 1
            stats["glyphs"] += len(text)
        stats["pages"] += 1

    result = doc.write(deflate=True, garbage=3)
    doc.close()
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as fh:
            fh.write(result)
    return result, stats


__all__ = ["render_plan_to_pdf"]
