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


# 渲染 provenance 记录器（7H-2A）：把「哪个 source_node_id 画到 PDF 的哪个
# 对象」逐块采集。纯增量：不传就是 None，历史行为/测试完全不受影响。
class _RenderProvenance:
    """Accumulates a block-id → render-object map while ``render_plan_to_pdf``
    draws.  Each drawn block appends its own record; the resulting ``records``
    are returned by the caller for the forensic tool's ID-direct diff."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self._seq = 0

    def record(
        self,
        source_node_id: str,
        page: int,
        object_type: str,
        final_bbox_v3: List[float],
        font_size: Optional[float],
        text: str,
    ) -> None:
        """Append one drawn-object record.

        ``final_bbox_v3`` is the block's dst_box in v3 y-up (what the plan
        asked to draw).  ``render_object_ref`` is a monotonic ref to the
        sequence position among drawn objects (the drawn object itself is the
        text/glyph stream we just wrote).
        """
        ref = f"R{self._seq}"
        self._seq += 1
        self.records.append(
            {
                "source_node_id": source_node_id,
                "render_object_ref": ref,
                "page": page,
                "object_type": object_type,
                "final_bbox_v3": [round(float(v), 2) for v in final_bbox_v3],
                "font_size": round(float(font_size), 2) if font_size else None,
                "text": text,
            }
        )


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
        if effective_font in ("helv", "cour"):
            return pymupdf.get_text_length(s, fontsize=font_size,
                                           fontname=effective_font)
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


def _resolve_effect_font(text: str, fontname: Optional[str]) -> Optional[str]:
    """单行有效字体：cjk 字体对纯拉丁行回退 helv（提取保真），含 CJK 用中文字体。"""
    effective = fontname or "helv"
    if effective == "china-ss" and all(ord(ch) < 0x2E80 for ch in text):
        effective = "helv"
    return effective


def _render_list_commands(
    page: Any,
    commands: Sequence[dict],
    page_height: float,
    font_size: float,
    fontname: Optional[str],
    block_rect: Any,
    stats: Dict[str, Any],
    src_doc: Optional[Any],
) -> None:
    """把列表渲染计划中的 marker/text 命令逐条落到 PDF（几何来自节点，y 翻转）。"""
    if src_doc is not None:
        # 覆盖原文区域（白色矩形），保证译文不与原文混排。
        page.draw_rect(block_rect, color=None, fill=(1, 1, 1))
    for c in commands or []:
        t = (c.get("text") or "")
        if not t:
            continue
        x = float(c.get("x") or 0.0)
        y = float(c.get("y") or 0.0)
        eff = _resolve_effect_font(t, fontname)
        page.insert_text((x, page_height - y), t, fontsize=font_size, fontname=eff)
        stats["blocks"] += 1
        stats["glyphs"] += len(t)


def _render_flow_commands(
    page: Any,
    commands: Sequence[dict],
    page_height: float,
    font_size: float,
    fontname: Optional[str],
    block_rect: Any,
    stats: Dict[str, Any],
    src_doc: Optional[Any],
    entry: Optional[dict] = None,
) -> None:
    """Draw settled FlowText LayoutResult lines (already wrapped/positioned).

    Each command is a pre-laid-out line (x/y baseline in v3 y-up).  This
    renderer applies no re-wrap / re-fit — it only flips y and inserts the
    glyphs at the **settled** font size carried by the command (7F-6b: a
    SHRINK recovery reduces it; falling back to the block font when absent).
    Overflow carried by a command is surfaced via a debug log +
    ``stats["flow_overflow"]`` so it stays observable.
    """
    if src_doc is not None:
        page.draw_rect(block_rect, color=None, fill=(1, 1, 1))
    overflow_hit = False
    for c in commands or []:
        t = (c.get("text") or "")
        if not t:
            continue
        x = float(c.get("x") or 0.0)
        y = float(c.get("y") or 0.0)
        draw_fs = c.get("font_size")
        try:
            draw_fs = float(draw_fs) if draw_fs else 0.0
        except (TypeError, ValueError):
            draw_fs = 0.0
        if draw_fs <= 0:
            draw_fs = float(font_size)
        eff = _resolve_effect_font(t, fontname)
        page.insert_text((x, page_height - y), t, fontsize=draw_fs, fontname=eff)
        stats["blocks"] += 1
        stats["glyphs"] += len(t)
        overflow_hit = overflow_hit or bool(c.get("overflow"))
    if overflow_hit:
        logger.debug(
            "[magicpdf] flow block %r overflowed (%s lines)",
            (entry or {}).get("block_id"),
            len(commands or []),
        )
        stats["flow_overflow"] = stats.get("flow_overflow", 0) + 1


def _render_toc_commands(
    page: Any,
    commands: Sequence[dict],
    page_height: float,
    font_size: float,
    fontname: Optional[str],
    block_rect: Any,
    stats: Dict[str, Any],
    src_doc: Optional[Any],
) -> None:
    """把 TOC 渲染计划中的 number/title/leader/page 命令逐条落到 PDF。

    与列表渲染同构：水平几何（title_x / page_x）与 leader 已在命令里（来自
    结构化条目的原几何），这里只做 y 翻转 + 逐条写入。numbering prefix /
    leader / page number 在渲染期已经是译后-titled —— 它们从不经过 translator
    （这一保证在 toc_sidechannel 完成）。
    """
    _render_list_commands(
        page,
        commands,
        page_height,
        font_size,
        fontname,
        block_rect,
        stats,
        src_doc,
    )


def render_plan_to_pdf(
    plan: Optional[Sequence[dict]],
    page_sizes: Optional[Dict[int, Sequence[float]]] = None,
    output_path: Optional[str] = None,
    font_size_fallback: float = _DEFAULT_FONT_SIZE,
    cjk_font: bool = True,
    source_pdf: Optional[str] = None,
    provenance: bool = False,
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
        source_pdf: 原 PDF 路径（可选）。提供时以原页作为背景层 —— 图形、
            颜色块、图片与保留块（formula/code/table…）的原文由背景直接显示，
            仅对**真正翻译**的块（``translated != text``）用白色矩形覆盖原文
            区域后写入译文；不提供时保持纯文本层（所有块直接写文本，兼容测试
            与无需背景的场景）。
        provenance: 为 True 时把逐块渲染对象（``source_node_id`` 关联到
            ``render_object_ref`` + 对象类型 + 最终 v3 bbox）采集到
            ``stats["provenance"]``，供 7H-2A 的 ID-direct 差分诊断使用。
            缺省 False：完全保持既有行为，stats 不含该键。

    Returns:
        ``(pdf_bytes, stats)``，``stats`` 含 ``pages``/``blocks``/``glyphs``；
        ``provenance=True`` 时另含 ``stats["provenance"]``（list[dict]）。
    """
    import pymupdf

    sizes = dict(page_sizes or {})
    default_page = tuple(_DEFAULT_PAGE)

    src_doc: Any = None
    if source_pdf:
        try:
            src_doc = pymupdf.open(source_pdf)
        except Exception as exc:  # noqa: BLE001 -- 背景加载失败回退纯文本层
            # 只记录错误文本，绝不把异常对象传入日志：pymupdf 打开失败的
            # FileDataError 的 traceback 会持有 C 层文件句柄，若异常被日志
            # 记录（pytest 捕获 / 长驻 handler）保留，Windows 上源 PDF 会
            # 一直被锁住导致临时目录无法清理。字符串化后立即丢弃。
            logger.warning(
                "[magicpdf] 渲染背景加载失败，回退纯文本层: %s (%s)",
                source_pdf,
                str(exc),
            )
            src_doc = None
            del exc

    def _is_translated_block(entry: dict) -> bool:
        """真翻译块：translated 非空且与原文不同。formula/code 等保留块的
        translated 由 translate_document 回填为原文，不满足此条件。"""
        text = entry.get("text") or ""
        translated = entry.get("translated")
        if not (isinstance(translated, str) and translated.strip()):
            return False
        return translated != text

    by_page: Dict[int, List[dict]] = {}
    for entry in list(plan or []):
        pno = int(entry.get("page") or 0)
        by_page.setdefault(pno, []).append(entry)

    doc = pymupdf.Document()
    stats = {"pages": 0, "blocks": 0, "glyphs": 0}
    fontname = "china-ss" if cjk_font else None
    prov = _RenderProvenance() if provenance else None

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
        if src_doc is not None and pno < src_doc.page_count:
            # 原页作为背景层：保留图形/颜色块/图片，公式/代码等保留块的
            # 原文也由背景直接显示（不再重复绘制 LaTeX/原文，避免叠影）。
            page.show_pdf_page(page.rect, src_doc, pno)
        for entry in by_page[pno]:
            text = _entry_text(entry)
            if not text:
                continue
            # Commit 7A：统一 render_payload.kind 分派（list/toc/flow），
            # 旧字段（list_items / toc_commands）作为兼容回退。
            payload = entry.get("render_payload") or {}
            payload_kind = payload.get("kind")
            list_cmds = payload.get("commands") or []
            if payload_kind == "list" or (
                not list_cmds and (entry.get("list_items") or {}).get("commands")
            ):
                if not list_cmds:
                    list_cmds = (entry.get("list_items") or {}).get("commands") or []
                # List 块：marker + content 逐条落位（几何来自解析阶段）
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
                _render_list_commands(
                    page, list_cmds, h, font_size, fontname, rect, stats, src_doc
                )
                if prov is not None:
                    prov.record(
                        entry.get("block_id", "?"), pno, "list",
                        entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                        font_size, text,
                    )
                continue
            toc_cmds = payload.get("commands") or []
            if payload_kind == "toc" or (
                not list_cmds and (entry.get("toc_commands") or {}).get("commands")
            ):
                if not toc_cmds:
                    toc_cmds = (entry.get("toc_commands") or {}).get("commands") or []
                # TOC 块：逐条目落位（number/title/leader/page，几何来自节点）
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
                _render_toc_commands(
                    page, toc_cmds, h, font_size, fontname, rect, stats, src_doc
                )
                if prov is not None:
                    prov.record(
                        entry.get("block_id", "?"), pno, "toc",
                        entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                        font_size, text,
                    )
                continue
            # Commit 7E-1: flow block with a settled FlowText LayoutResult draws
            # its pre-laid-out lines directly (the renderer does NOT re-wrap).
            flow_cmds = payload.get("commands") or []
            if payload_kind == "flow" and flow_cmds:
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
                _render_flow_commands(
                    page, flow_cmds, h, font_size, fontname, rect, stats, src_doc, entry
                )
                if prov is not None:
                    prov.record(
                        entry.get("block_id", "?"), pno, "flow",
                        entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                        font_size, text,
                    )
                stats["flow_layout_used"] = stats.get("flow_layout_used", 0) + 1
                continue
            # Observable legacy fallback: a flow block whose LayoutResult could
            # not be settled (layout_ok False / no commands) degrades to the
            # classic `_insert_text_wrapped` path — never silently.
            if payload_kind == "flow":
                stats["flow_legacy_fallback"] = stats.get("flow_legacy_fallback", 0) + 1
            if src_doc is not None and not _is_translated_block(entry):
                # 保留背景模式：公式/代码/表格等保留块原文已在背景中，
                # 跳过重画 —— 否则 LaTeX 源码/原文会叠在背景文字上。
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
            if src_doc is not None:
                # 覆盖原文区域（白色矩形），保证译文不与原文混排。仅覆盖本块
                # dst_box，背景图形在未覆盖区域原样保留（修复有色方块被整页
                # 白底吞噬 —— 旧版从零建白页丢弃全部背景）。
                page.draw_rect(rect, color=None, fill=(1, 1, 1))
            if src_doc is None and not _is_translated_block(entry):
                # 纯文本层：保留块（formula/code/table，translated == text）
                # 用等宽字体绘制，保持与源等宽文本一致的几何（否则 bbox 中心
                # 漂移，evaluator 的 code_preserved_bbox 会误报）。真实链路
                # 由背景直接显示保留块，不经过此路径。
                # 注意：局部变量，绝不覆盖外层 fontname —— 否则会污染后续
                # 翻译块（CJK 译文被 cour 渲染成乱码）。
                block_font = "cour"
            else:
                block_font = fontname
            _insert_text_wrapped(page, rect, text, font_size, block_font)
            stats["blocks"] += 1
            stats["glyphs"] += len(text)
            if prov is not None:
                prov.record(
                    entry.get("block_id", "?"), pno, "wrapped",
                    entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                    font_size, text,
                )
        stats["pages"] += 1

    result = doc.write(deflate=True, garbage=3)
    doc.close()
    if src_doc is not None:
        src_doc.close()
    if prov is not None:
        stats = dict(stats)
        stats["provenance"] = prov.records
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "wb") as fh:
            fh.write(result)
    return result, stats


__all__ = ["render_plan_to_pdf", "_RenderProvenance"]
