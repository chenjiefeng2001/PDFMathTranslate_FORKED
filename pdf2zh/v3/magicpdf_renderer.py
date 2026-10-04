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

_DEFAULT_PAGE = (612.0, 792.0)  # US Letter; see pdf2zh.page_standard
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


def _emit_render_trace(
    trace,
    entry: dict,
    page_no: int,
    page_height: float,
    event: str,
    *,
    commands: Optional[Sequence[dict]] = None,
    baseline: Optional[float] = None,
    expected_baseline: Optional[float] = None,
    erase_rect=None,
    font_size: Optional[float] = None,
    used_baselines: Optional[Sequence[tuple]] = None,
) -> None:
    """FlightRecorder：render 层事件（坐标语义显式声明）。

    ``render.flow`` 记录每条命令的 v3 box_top 锚 → fitz baseline 的完整
    因果链；``render.erase`` 记录擦除矩形与 src/dst，供 ERASE_GEOMETRY
    规则判定白矩形是否覆盖源几何。
    """
    if trace is None or not getattr(trace, "enabled", False):
        return
    src = list(entry.get("src_box") or [0, 0, 0, 0])
    dst = list(entry.get("dst_box") or src)
    payload: Dict[str, Any] = {
        "kind": entry.get("kind"),
        "text": (entry.get("text") or "")[:200],
        "translated": (entry.get("translated") or "")[:200],
        "src_box": src,
        "dst_box": dst,
        "page_height": round(float(page_height), 2),
        "font_size": round(float(font_size), 2) if font_size else None,
    }
    if erase_rect is not None:
        # erase_rect 是 fitz 空间（翻转后直接 draw_rect）。与 src_box /
        # dst_box（v3 y-up）同帧比较前必须先翻回 v3 —— 否则语义标注永远
        # 是 "other"，ERASE_GEOMETRY 规则也永远无法命中（跨帧比较是静默
        # no-op，正是 trace 体系要消灭的那类缺陷）。
        er_fitz = list(erase_rect)
        er_v3 = _flip_v3_box(er_fitz, page_height)
        payload["erase_rect_fitz"] = [round(float(v), 2) for v in er_fitz]
        payload["erase_rect"] = [round(float(v), 2) for v in er_v3]
        payload["erase_semantics"] = (
            "src_box"
            if _same_box(er_v3, src)
            else ("dst_box" if _same_box(er_v3, dst) else "other")
        )
    if commands is not None:
        used = list(used_baselines or [])
        cmds_out = []
        for i, c in enumerate((commands or [])[:20]):
            entry_cmd: Dict[str, Any] = {
                "x": round(float(c.get("x") or 0.0), 2),
                # v3 y-up：命令 y 是 box 顶沿锚（plan.flow 已声明 y_meaning=box_top）
                "y": round(float(c.get("y") or 0.0), 2),
                "y_space": "v3",
                "y_meaning": "box_top",
                "font_size": round(float(c.get("font_size") or 0.0), 2),
                "text": (c.get("text") or "")[:80],
            }
            if i < len(used):
                y_v3, draw_fs, actual = used[i]
                entry_cmd["actual_baseline"] = round(float(actual), 2)
                entry_cmd["expected_baseline"] = round(
                    (float(page_height) - float(y_v3)) + float(draw_fs) * 0.85, 2
                )
                entry_cmd["baseline_delta"] = round(
                    float(actual)
                    - ((float(page_height) - float(y_v3)) + float(draw_fs) * 0.85),
                    2,
                )
            cmds_out.append(entry_cmd)
        payload["commands"] = cmds_out
    if baseline is not None:
        payload["baseline"] = round(float(baseline), 2)
        payload["baseline_space"] = "fitz"
        payload["baseline_meaning"] = "baseline"
    if expected_baseline is not None:
        payload["expected_baseline"] = round(float(expected_baseline), 2)
    trace.emit(
        event,
        trace.ctx(page_no, entry.get("block_id") or "?", "render"),
        payload,
    )


def _same_box(a, b, tol: float = 1.0) -> bool:
    if len(a) != 4 or len(b) != 4:
        return False
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


def _erase_rect_for(entry: dict, dst_box: Sequence[float], page_height: float):
    """白矩形几何（7N-FIX-3B）：只覆盖需要替换的**源文本几何**。

    ``src_box`` 与 ``dst_box`` 解耦：非 shift 块两者相等（行为不变）；
    shift_down 块 dst 已整体平移，若按 dst 擦除会把相邻行一并抹掉（MECH-4
    实测 28/30 例），因此这里恒取 src_box 作为擦除区域，译文落点仍由
    dst_box / commands 决定。
    """
    import pymupdf

    src = list(entry.get("src_box") or dst_box)
    if len(src) != 4:
        src = list(dst_box)
    return pymupdf.Rect(_flip_v3_box(src, page_height))


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
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    """在 rect 内手动换行插入文本（兼容 CJK 字体度量）。

    - 按「词」（空白分隔）累积行，行宽按绘制字体精确度量；
    - 全角/无空格文本（中文）逐字符累积；
    - 行高 ``font_size * 1.4``；**放不下时先缩字号重排**（与 flow 路径的 SHRINK
      recovery 同一取舍），缩到下限仍放不下才截断，并记入 ``wrap_truncated``。
    - 落笔统一走 :func:`_draw_line` —— 换行只按 rect 宽度收敛，rect 本身可能
      越出页面右边界，或单个超长 token 宽于整行，两者都会让 pymupdf 静默截断。

    旧实现「超出 rect 下边界即停止」是静默丢字：译文比原文多一行时，后面整段
    直接消失且无任何日志/指标。实测扫描件上的标题块
    ``'Chapter One: The Scanning Machine'`` 只剩下 ``'Chapter One:'``。
    """
    stats = stats if stats is not None else {}
    x = float(rect.x0)
    max_w = max(0.1, float(rect.x1) - float(rect.x0))
    box_h = max(1e-6, float(rect.y1) - float(rect.y0))
    top = float(rect.y0)
    import pymupdf

    # pymupdf 内置 CJK 字体对拉丁字符的 advance 偏宽，提取文本时会在字符间
    # 插入多余空格（"x = a" → "x  =  a"）。纯拉丁行回退默认字体（helv），
    # 既保证提取保真；含 CJK 的行仍用中文字体保证显示。
    effective_font = fontname or "helv"
    if effective_font == "china-ss" and all(ord(ch) < 0x2E80 for ch in text):
        effective_font = "helv"

    def _layout(fs: float) -> list[str]:
        def _width(s: str) -> float:
            if effective_font in ("helv", "cour"):
                return pymupdf.get_text_length(s, fontsize=fs, fontname=effective_font)
            # CJK 内置字体（china-ss）对全角/拉丁均近似 1em 等宽，逐字符估算。
            return len(s) * fs

        lines: list[str] = []
        cur = ""
        for tok in text.split(" "):
            sep = " " if cur else ""
            trial = f"{cur}{sep}{tok}"
            if cur and _width(trial) > max_w:
                lines.append(cur)
                cur = tok
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def _stack_height(n_lines: int, fs: float) -> float:
        # 首行基线在 top + 0.85em，末行基线再下移 (n-1) 个行距
        return fs * 0.85 + max(0, n_lines - 1) * fs * 1.4

    draw_fs = float(font_size)
    lines = _layout(draw_fs)
    if _stack_height(len(lines), draw_fs) > box_h:
        for scale in _WRAP_SHRINK_STEPS:
            if scale >= 1.0:
                continue
            candidate = float(font_size) * scale
            if _stack_height(len(_layout(candidate)), candidate) <= box_h:
                draw_fs = candidate
                lines = _layout(candidate)
                stats["fit_shrunk"] = stats.get("fit_shrunk", 0) + 1
                logger.debug(
                    "[magicpdf] wrapped block shrunk %.2f -> %.2f to fit %d line(s) "
                    "in %.1fpt",
                    float(font_size),
                    draw_fs,
                    len(lines),
                    box_h,
                )
                break

    line_h = draw_fs * 1.4
    y = top + draw_fs * 0.85
    drawn = 0
    for line in lines:
        # 首行始终落笔：退化几何（零高/零宽的 box）下这也是历史行为，
        # 既有测试锁定了它。行数不够时只从第二行开始截，并如实计数。
        if drawn and y > float(rect.y1) + 1e-6:
            break
        _draw_line(page, line, x, y, effective_font, draw_fs, stats, "wrapped")
        drawn += 1
        y += line_h
    if drawn < len(lines):
        dropped = len(lines) - drawn
        stats["wrap_truncated"] = stats.get("wrap_truncated", 0) + 1
        logger.warning(
            "[magicpdf] wrapped block needs %d line(s) but only %d fit in %.1fpt; "
            "%d line(s) dropped: %r",
            len(lines),
            drawn,
            box_h,
            dropped,
            text[:60],
        )


def _resolve_effect_font(text: str, fontname: Optional[str]) -> Optional[str]:
    """单行有效字体：cjk 字体对纯拉丁行回退 helv（提取保真），含 CJK 用中文字体。"""
    effective = fontname or "helv"
    if effective == "china-ss" and all(ord(ch) < 0x2E80 for ch in text):
        effective = "helv"
    return effective


#: 单行字号可缩到的最小比例。排版层自身的 SHRINK recovery（flow payload 的
#: ``recovery.steps``）已在上游缩过一次，这里是渲染层的兜底：只保证字形不越出
#: 页面右边界，不再重排（重排归排版层，见 _render_flow_commands 的不变式）。
_FIT_MIN_SCALE = 0.55

#: 末字形与页面右边界之间保留的余量（pt），避免压边。
_FIT_MARGIN_PT = 1.5

#: legacy wrapped 路径逐级尝试的缩放比例（用于把多行译文压进源框高度）。
_WRAP_SHRINK_STEPS = (1.0, 0.92, 0.85, 0.78, 0.72, 0.66, 0.60, 0.55)


def _page_width(page: Any) -> float:
    """页面宽度（pt）；取不到时返回 0.0 表示「不做页面内收敛」。"""
    try:
        return float(page.rect.width)
    except Exception:  # noqa: BLE001 -- 拿不到页宽就按原样画
        return 0.0


def _advance_width(text: str, fontname: Optional[str], font_size: float) -> float:
    """``text`` 在**真正用来绘制它的字体**下的水平前进宽度。

    必须用绘制字体度量：``china-ss`` 的拉丁字符 advance ≈ 1em，而排版层
    （``semantic/layout/measure.py``）按 helv 度量，同一行两者相差约 2 倍。
    """
    import pymupdf

    if not text:
        return 0.0
    size = float(font_size)
    try:
        return float(
            pymupdf.get_text_length(text, fontname=fontname or "helv", fontsize=size)
        )
    except Exception:  # noqa: BLE001 -- 度量失败退回等宽估算，不阻断渲染
        return len(text) * size


def _fit_line_to_page(
    text: str,
    fontname: Optional[str],
    font_size: float,
    x: float,
    page_width: float,
) -> Tuple[float, str, str]:
    """把一行缩放/裁剪到能落在页面内，返回 ``(字号, 文本, 状态)``。

    ``pymupdf.Page.insert_text`` 对超出页面右边界的部分**静默丢弃**并仍然返回
    成功码，译文会毫无征兆地被从中间截断（实测 x=300 时一行 60 字符只剩 30）。
    根因是排版按 helv 度量、渲染按 china-ss 绘制（含 CJK 的行一律切
    china-ss，见 :func:`_resolve_effect_font`），两者宽度不一致。

    先按比例缩字号（与排版层 SHRINK recovery 同一取舍），仍放不下才显式裁剪
    并把状态返回给调用方记入统计 —— 任何丢失都变成可观测的，而不是静默的。
    """
    if not text or page_width <= 0:
        return float(font_size), text, "fit"
    avail = float(page_width) - float(x) - _FIT_MARGIN_PT
    if avail <= 0:
        return float(font_size), "", "dropped"
    width = _advance_width(text, fontname, font_size)
    if width <= 0 or width <= avail:
        return float(font_size), text, "fit"
    scale = avail / width
    floor = float(font_size) * _FIT_MIN_SCALE
    if scale >= _FIT_MIN_SCALE:
        return float(font_size) * scale, text, "shrunk"
    # 已到缩放下限仍放不下：显式裁剪到能放下的前缀（pymupdf 只会默默扔掉剩余部分）
    keep = ""
    for ch in text:
        if _advance_width(keep + ch, fontname, floor) > avail:
            break
        keep += ch
    if not keep:
        return floor, "", "dropped"
    return floor, keep, "clipped"


def _draw_line(
    page: Any,
    text: str,
    x: float,
    y: float,
    eff_font: Optional[str],
    font_size: float,
    stats: Dict[str, Any],
    label: str = "",
) -> Tuple[float, str, str]:
    """所有 draw 路径的唯一落笔出口：先保证整行在页面内，再 ``insert_text``。

    四个绘制路径（list / toc / flow / legacy wrapped）都曾直接调
    ``insert_text`` 且不做任何宽度核算，因此统一收敛到这里，避免只修一条路径。

    ``eff_font`` 须已由 :func:`_resolve_effect_font` 解析。返回实际使用的
    ``(字号, 落笔文本, 状态)`` —— 调用方要用它回算基线（trace 的
    ``FLOW_BASELINE_MISMATCH`` 规则比对的就是这个值）。
    """
    if not text:
        return float(font_size), "", "empty"
    draw_fs, out_text, status = _fit_line_to_page(
        text, eff_font, font_size, x, _page_width(page)
    )
    if status == "shrunk":
        stats["fit_shrunk"] = stats.get("fit_shrunk", 0) + 1
        logger.debug(
            "[magicpdf] line shrunk %.2f -> %.2f to stay on page%s",
            float(font_size),
            draw_fs,
            f" ({label})" if label else "",
        )
    elif status == "clipped":
        stats["fit_clipped"] = stats.get("fit_clipped", 0) + 1
        logger.warning(
            "[magicpdf] line too wide for the page even at the size floor; "
            "%d of %d characters dropped%s: %r",
            len(text) - len(out_text),
            len(text),
            f" ({label})" if label else "",
            text[:60],
        )
    elif status == "dropped":
        stats["fit_clipped"] = stats.get("fit_clipped", 0) + 1
        logger.warning(
            "[magicpdf] line starts past the page's right edge; dropped whole%s: %r",
            f" ({label})" if label else "",
            text[:60],
        )
        return draw_fs, "", status
    page.insert_text((x, y), out_text, fontsize=draw_fs, fontname=eff_font)
    stats["blocks"] += 1
    stats["glyphs"] += len(out_text)
    return draw_fs, out_text, status


def _render_list_commands(
    page: Any,
    commands: Sequence[dict],
    page_height: float,
    font_size: float,
    fontname: Optional[str],
    erase_rect: Any,
    stats: Dict[str, Any],
    src_doc: Optional[Any],
) -> None:
    """把列表渲染计划中的 marker/text 命令逐条落到 PDF（几何来自节点，y 翻转）。

    7N-FIX-3：``erase_rect`` 是**源文本几何**（src_box 翻转），与命令落点
    （dst）解耦 —— shift 块的白矩形只覆盖真正需要替换的原文，绝不盖相邻行。
    """
    if src_doc is not None:
        # 覆盖原文区域（白色矩形），保证译文不与原文混排。
        page.draw_rect(erase_rect, color=None, fill=(1, 1, 1))
    for c in commands or []:
        t = c.get("text") or ""
        if not t:
            continue
        x = float(c.get("x") or 0.0)
        y = float(c.get("y") or 0.0)
        eff = _resolve_effect_font(t, fontname)
        _draw_line(page, t, x, page_height - y, eff, font_size, stats, "list")


def _render_flow_commands(
    page: Any,
    commands: Sequence[dict],
    page_height: float,
    font_size: float,
    fontname: Optional[str],
    erase_rect: Any,
    stats: Dict[str, Any],
    src_doc: Optional[Any],
    entry: Optional[dict] = None,
) -> None:
    """Draw settled FlowText LayoutResult lines (already wrapped/positioned).

    Each command is a pre-laid-out line whose ``y`` is the **box-top anchor**
    in v3 y-up (``first_cmd_y == dst_box.y1``, see 7N-FIX-3 / MECH-4).  This
    renderer applies no re-wrap / re-fit — it only flips y, converts the
    box-top anchor into a real baseline (7N-FIX-3A: ``baseline =
    (page_height - y) + draw_fs * 0.85``, the same anchoring as
    ``_insert_text_wrapped`` — otherwise the ink floats a full em above the
    box), and inserts the glyphs at the **settled** font size carried by the
    command (7F-6b: a SHRINK recovery reduces it; falling back to the block
    font when absent).  Overflow carried by a command is surfaced via a debug
    log + ``stats["flow_overflow"]`` so it stays observable.

    ``erase_rect`` is the **source** geometry (flipped src_box, 7N-FIX-3B):
    the white erase rectangle only ever covers the text region that is being
    replaced — never the shifted dst_box — so shifted blocks cannot wipe out
    neighbouring lines.
    """
    if src_doc is not None:
        page.draw_rect(erase_rect, color=None, fill=(1, 1, 1))
    overflow_hit = False
    # FlightRecorder：记录每条命令**实际使用**的 fitz baseline（供
    # FLOW_BASELINE_MISMATCH 规则与实际基线比对 —— 若未来回归去掉 0.85
    # 偏移，trace 会立即暴露 actual != expected）。
    used_baselines: List[float] = []
    block_id = (entry or {}).get("block_id")
    page_width = _page_width(page)
    for c in commands or []:
        t = c.get("text") or ""
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
        # 7N-FIX-3A：命令 y 是 v3 y-up 的 box 顶沿，不是基线 —— 翻转后落在
        # fitz 矩形顶边；基线必须再下移 ~0.85em（与 _insert_text_wrapped 的
        # 锚定一致），否则译文墨水整体上浮 ≈1em 顶进上一行。多行命令的
        # 相对步进（line_step）保持不变，整列只平移这个锚定偏移。
        # 这里的 0.85 必须用**实际落笔**字号：_fit_line_to_page 可能为把整行
        # 留在页面内而缩过字号（见该函数），用计划字号算出的 baseline 会与
        # 真实墨水错位，trace 的 FLOW_BASELINE_MISMATCH 也就不再成立。
        fitted_fs, _, _ = _fit_line_to_page(t, eff, draw_fs, x, page_width)
        baseline = (page_height - y) + fitted_fs * 0.85
        used_fs, _drawn, _status = _draw_line(
            page, t, x, baseline, eff, draw_fs, stats, f"flow {block_id}"
        )
        used_baselines.append((float(y), float(used_fs), float(baseline)))
        overflow_hit = overflow_hit or bool(c.get("overflow"))
    if overflow_hit:
        logger.debug(
            "[magicpdf] flow block %r overflowed (%s lines)",
            (entry or {}).get("block_id"),
            len(commands or []),
        )
        stats["flow_overflow"] = stats.get("flow_overflow", 0) + 1
    # FlightRecorder 回传：每行 (y_v3, draw_fs, actual_baseline_fitz)
    return used_baselines


def _render_toc_commands(
    page: Any,
    commands: Sequence[dict],
    page_height: float,
    font_size: float,
    fontname: Optional[str],
    erase_rect: Any,
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
        erase_rect,
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
    trace=None,
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
        trace: 可选 FlightRecorder —— 逐块发射 ``render.flow`` /
            ``render.erase`` / ``render.wrapped`` / ``render.block`` 事件，
            声明坐标语义（v3 box_top → fitz baseline 的完整因果链），
            供 trace_rules / trace_audit 消费。

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
    # 页集合 = 计划里出现的页 ∪ 调用方声明了页尺寸的页。
    # 后半是关键：某一页 OCR/布局一个块都没检出时，render_plan 里不会有它，
    # 但扫描件上那一页的内容来自背景层 —— 只按计划建页会把整页从产物里删掉
    # （实测 3 页扫描件 → 2 页 PDF，且退出码 0、无任何告警）。
    for pno in sizes:
        try:
            by_page.setdefault(int(pno), [])
        except (TypeError, ValueError):
            continue
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

    empty_pages = sum(1 for entries in by_page.values() if not entries)
    if empty_pages:
        # 显式计数：这些页没有可翻译块，但仍必须出现在产物里（背景层承载内容）。
        stats["pages_without_entries"] = empty_pages

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
                erase_rect = _erase_rect_for(entry, box, h)
                font_size = entry.get("font_size")
                try:
                    font_size = float(font_size) if font_size else 0.0
                except (TypeError, ValueError):
                    font_size = 0.0
                if font_size <= 0:
                    font_size = float(font_size_fallback) or _DEFAULT_FONT_SIZE
                _render_list_commands(
                    page, list_cmds, h, font_size, fontname, erase_rect, stats, src_doc
                )
                _emit_render_trace(
                    trace,
                    entry,
                    pno,
                    h,
                    "render.block",
                    commands=list_cmds,
                    erase_rect=list(erase_rect),
                    font_size=font_size,
                )
                if prov is not None:
                    prov.record(
                        entry.get("block_id", "?"),
                        pno,
                        "list",
                        entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                        font_size,
                        text,
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
                erase_rect = _erase_rect_for(entry, box, h)
                font_size = entry.get("font_size")
                try:
                    font_size = float(font_size) if font_size else 0.0
                except (TypeError, ValueError):
                    font_size = 0.0
                if font_size <= 0:
                    font_size = float(font_size_fallback) or _DEFAULT_FONT_SIZE
                _render_toc_commands(
                    page, toc_cmds, h, font_size, fontname, erase_rect, stats, src_doc
                )
                _emit_render_trace(
                    trace,
                    entry,
                    pno,
                    h,
                    "render.block",
                    commands=toc_cmds,
                    erase_rect=list(erase_rect),
                    font_size=font_size,
                )
                if prov is not None:
                    prov.record(
                        entry.get("block_id", "?"),
                        pno,
                        "toc",
                        entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                        font_size,
                        text,
                    )
                continue
            # Commit 7E-1: flow block with a settled FlowText LayoutResult draws
            # its pre-laid-out lines directly (the renderer does NOT re-wrap).
            flow_cmds = payload.get("commands") or []
            if payload_kind == "flow" and flow_cmds:
                box = list(entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0])
                if len(box) != 4:
                    box = [0, 0, 0, 0]
                # 7N-FIX-3B：白矩形只覆盖源文本几何（src_box），与译文落点
                # （dst_box / commands）解耦 —— shift 块不再误抹相邻行。
                erase_rect = _erase_rect_for(entry, box, h)
                font_size = entry.get("font_size")
                try:
                    font_size = float(font_size) if font_size else 0.0
                except (TypeError, ValueError):
                    font_size = 0.0
                if font_size <= 0:
                    font_size = float(font_size_fallback) or _DEFAULT_FONT_SIZE
                used_baselines = _render_flow_commands(
                    page,
                    flow_cmds,
                    h,
                    font_size,
                    fontname,
                    erase_rect,
                    stats,
                    src_doc,
                    entry,
                )
                # 7N-FIX-3A：命令 y（v3 box_top）→ fitz baseline 的因果链。
                # baseline 由 _render_flow_commands 实际使用值回传（SHRINK 后
                # 的定版字号），expected 独立推导 —— 二者不等即 FLOW_BASELINE
                # 类规则 FAIL。
                _emit_render_trace(
                    trace,
                    entry,
                    pno,
                    h,
                    "render.flow",
                    commands=flow_cmds,
                    erase_rect=list(erase_rect),
                    font_size=font_size,
                    used_baselines=used_baselines,
                )
                if prov is not None:
                    prov.record(
                        entry.get("block_id", "?"),
                        pno,
                        "flow",
                        entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                        font_size,
                        text,
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
                # 7N-FIX-3B：白矩形只覆盖源文本几何（src_box），译文仍画进
                # dst_box（rect）—— 擦除与渲染几何解耦，shift 块不误抹相邻行。
                erase_rect = _erase_rect_for(entry, box, h)
                page.draw_rect(erase_rect, color=None, fill=(1, 1, 1))
                _emit_render_trace(
                    trace,
                    entry,
                    pno,
                    h,
                    "render.erase",
                    erase_rect=list(erase_rect),
                    font_size=font_size,
                )
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
            # 7N-FIX-3A：wrapped 路径与 _insert_text_wrapped 同一锚定 ——
            # baseline = box top (fitz rect.y0) + 0.85*fs。
            baseline = float(rect.y0) + font_size * 0.85
            # stats 交给 _insert_text_wrapped：逐行经 _draw_line 累加，块级的
            # blocks/glyphs 由那里按**实际落笔**的文本计（裁剪时不能按原文数）。
            _insert_text_wrapped(page, rect, text, font_size, block_font, stats)
            _emit_render_trace(
                trace,
                entry,
                pno,
                h,
                "render.wrapped",
                baseline=baseline,
                expected_baseline=baseline,
                font_size=font_size,
            )
            if prov is not None:
                prov.record(
                    entry.get("block_id", "?"),
                    pno,
                    "wrapped",
                    entry.get("dst_box") or entry.get("src_box") or [0, 0, 0, 0],
                    font_size,
                    text,
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
