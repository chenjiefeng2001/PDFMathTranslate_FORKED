"""BabelDOC 列表项（``1. XXX`` / ``2. XXX`` / ``- XXX`` / ``a. XXX`` 等）处理补丁。

背景
----
BabelDOC 的 ``ParagraphFinder._group_characters_into_paragraphs`` 按版面布局
（layout）分组字符：doclayout 模型把页面内的连续列表（``1. XXX``、``2. XXX``…）
通常识别为**单个** ``plain text`` 布局框，因此所有列表项字符合并成一个
``PdfParagraph``。而 ``process_independent_paragraphs`` 只会把"行首是 bullet
符号（``•`` 等）"的行拆成独立段落，**不识别数字/字母/连字符编号** —— 于是编号
列表被整体当做一个段落翻译，翻译后重排时编号与正文混排，页面排版错乱。

本模块在 pdf2zh 侧以运行时补丁（monkey-patch，与
:mod:`pdf2zh.babeldoc_onnx_backend` 同一模式）包装 BabelDOC 的
``ParagraphFinder.process_independent_paragraphs``：原始拆分逻辑执行完毕后，
依次做三件事：

1. **编号列表项拆分**：段落内若存在"行首匹配列表编号模式"的行，则从该行起
   拆成独立 ``PdfParagraph``，使每个列表项独立翻译、独立排版。覆盖数字
   （``1.``/``1)``/``1）``/``1、``/``1・``/``(1)``/``[1]``/带圈数字）、字母
   （``a.``/``A)``/``a、``）、连字符（``-``/``–``/``—``）、罗马数字
   （``(i)``）、中文序号（``第1条``/``第一条``/``(一)``）等样式；同时通过
   "编号后跟非数字字符"门控排除 ``1.5``/``2024.12``/``7.3.2`` 等小数/年份/
   多级编号误伤。
2. **长列表项续行合并**：把紧跟在列表项段落之后、无编号前缀、左缘缩进的
   续行段落（含 BabelDOC"短行拆分"拆出的缩进续行，以及超宽行溢出的
   ``fallback_line`` 片段）合并回所属列表项段落，避免列表项正文在段落级被
   割裂、重排错乱。
3. **编号前缀公式保护**：把列表项行首的编号前缀（``1.``/``a.``/``-`` 等）
   构造成 ``PdfFormula`` composition（假 ``formula_layout_id``/``line_id``，
   复用 :mod:`pdf2zh.babeldoc_toc_protect` 的占位符机制）—— 前缀不进入翻译
   文本，重排时由 BabelDOC 公式重排逻辑按 x/y 偏移原位渲染，彻底摆脱
   "机器翻译改写/丢弃编号"的不确定性。

开关（``PDF2ZH_BABELDOC_SPLIT_LIST_ITEMS``，默认 ``1`` 开启）：

============  ==============================================================
取值          行为
============  ==============================================================
``1``/``on``  开启列表项拆分 + 续行合并 + 编号公式保护（推荐）
``0``/``off`` 关闭，保持 BabelDOC 原生行为（整个列表作为一个段落）
============  ==============================================================

拆分对 BabelDOC 后续流程透明：补丁插入点在 ``process_independent_paragraphs``
内部末尾，随后的 ``merge_alternating_line_number_paragraphs``、
``update_paragraph_data(update_unicode=True)``、``fix_overlapping_paragraphs``
与 ``_set_paragraph_render_order`` 都会自动作用于新拆出的段落；编号公式
composition 与 TOC 保护一样经过 ``process_page_formulas``、
``process_translatable_formulas``、``process_page_offsets``（占位符往返、
按偏移原位渲染）。

列表编号模式匹配"数字/字母 + 分隔符 + 非数字/空白"（``1. ``、``1) ``、``(1)``、
``1、``、``1．``、``1・``、``a. ``、``- `` 等），并通过"编号后跟非数字"门控
排除 ``1.5``、``2024.12`` 这类小数/年份行首。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

#: 环境变量名。
_ENV_SPLIT_LIST = "PDF2ZH_BABELDOC_SPLIT_LIST_ITEMS"

#: 列表编号模式（行首）。
#: 覆盖数字（1. / 1) / 1） / 1． / 1、 / 1・ / (1) / [1] / 带圈数字）、
#: 字母（a. / A) / a、）、连字符（- / – / —）、罗马数字（(i)）、中文序号
#: （第1条 / 第一条 / (一)）。
#: - "数字/字母 + 分隔符"后跟**非数字**才视为编号（排除 1.5、2024.12、
#:   7.3.2 小数/年份/多级编号），其中 `1、` / `1・` 顿号/中黑点分支不要求空白；
#: - 数字编号限 1-3 位（排除 2024. 这种年份行首）；
#: - 字母编号（a. / A)）要求空白，且排除 "Fig. 1" 这类"字母点+空格+数字"；
#: - 连字符分支要求后跟空白或行尾（避免 -dash 这种连字符单词误伤）。
_LIST_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\(\d{1,4}\)\s*|"  # (1) (12)
    r"\d{1,3}[.．)）](?!\d)|"  # 1. 1) 1） 1． (排除 1.5)
    r"\d{1,3}、|"  # 1、
    r"\d{1,3}・|"  # 1・ (U+30FB)
    r"[A-Za-z][.．.)）](?![ \t]*\d)\s|"  # a. A) (排除 Fig. 1)
    r"[A-Za-z]、|"  # a、
    r"[-–—](?:\s|$)|"  # - – —
    r"\([ivxlcdmIVXLCDM]{1,7}\)\s*|"  # (i) (iii)
    r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|"  # 带圈数字
    r"\[[0-9]{1,4}\]\s*|"  # [1] [12]
    r"第\s*(?:\d{1,4}|[一二三四五六七八九十百千]{1,4})\s*[条章]|"  # 第1条 第一条 第12章
    r"[（(][一二三四五六七八九十百千]{1,4}[）)]"  # (一) （二）
    r")"
)

#: 假公式布局 id 基数：给列表编号前缀公式字符打上"非空 formula_layout_id"标记，
#: 使 ``is_translatable_formula`` / ``should_split_formula`` 认为它不是"纯数字
#: 可翻译公式"，从而避免被 BabelDOC 转回普通文本行重新进入翻译器。
#: 值取远小于真实布局 id（真实 id 为 doclayout 正数）的负数，杜绝与真实布局冲突。
_FAKE_FORMULA_LAYOUT_BASE = -2000

#: 假行号基数：列表编号公式的 ``line_id`` 使用负数，保证与 BabelDOC 解析阶段
#: 生成的公式（line_id >= 0）以及 TOC 保护公式（-1000 基数）都不同 ——
#: ``merge_overlapping_formulas`` 的 ``same_line`` 判断要求两个公式 line_id
#: 相同才合并，从而不会被误合并。
_FAKE_LINE_ID_BASE = -2000

#: 补丁锁 + 原始 ``process_independent_paragraphs`` 引用（None = 未打补丁）。
_PATCH_LOCK = threading.Lock()
_ORIGINAL_PIP: Optional[object] = None


def is_list_item_line(line) -> bool:
    """判断一行是否为列表项（行首匹配编号模式）。"""
    text = "".join((c.char_unicode or "") for c in (line.pdf_character or []))
    return bool(_LIST_ITEM_PREFIX_RE.match(text))


def _prefix_char_count(chars: List[object], prefix_text: str) -> int:
    """计算``prefix_text``文本宽度对应``chars``中的字符数（含 dummy 容错）。

    每字符按 ``char_unicode`` 的长度累计（dummy 字符可能为空串），达到
    ``len(prefix_text)`` 即返回覆盖到的字符下标 + 1。
    """
    if not chars:
        return 0
    target = len(prefix_text)
    if target <= 0:
        return 0
    total = 0
    for i, ch in enumerate(chars):
        total += len(ch.char_unicode or "")
        if total >= target:
            return i + 1
    return len(chars)


def _line_x0(line) -> Optional[float]:
    """行首字符左缘 x（优先 visual_bbox，回退行 box）。"""
    if not line.pdf_character:
        return None
    first = line.pdf_character[0]
    try:
        return float(first.visual_bbox.box.x)
    except Exception:  # noqa: BLE001 -- 缺几何时回退行 box
        try:
            return float(line.box.x)
        except Exception:  # noqa: BLE001
            return None


def _line_y_band(line) -> Optional[tuple]:
    """行字符的 y 区间 ``(min_y, max_y2)``；无几何信息时返回 None。"""
    ys = []
    for ch in line.pdf_character:
        try:
            b = ch.visual_bbox.box
            ys.append((float(b.y), float(b.y2)))
        except Exception:  # noqa: BLE001 -- 缺几何的字符跳过
            continue
    if not ys:
        return None
    return (min(y for y, _ in ys), max(y2 for _, y2 in ys))


def _first_line_of(para) -> Optional[object]:
    """段落的第一个``PdfLine`` composition。"""
    for comp in para.pdf_paragraph_composition or []:
        if comp and comp.pdf_line:
            return comp.pdf_line
    return None


def _last_line_of(para) -> Optional[object]:
    """段落的最后一个``PdfLine`` composition。"""
    for comp in reversed(para.pdf_paragraph_composition or []):
        if comp and comp.pdf_line:
            return comp.pdf_line
    return None


def _paragraph_anchor_x(para) -> Optional[float]:
    """列表项段落的锚点：第一行左缘 x（续行必须 >= 该值才合并）。"""
    line = _first_line_of(para)
    if line is None:
        return None
    return _line_x0(line)


def _is_list_item_paragraph(para) -> bool:
    """段落内是否存在行首匹配列表编号模式的行。"""
    for comp in para.pdf_paragraph_composition or []:
        if comp and comp.pdf_line and is_list_item_line(comp.pdf_line):
            return True
    return False


def _is_continuation_paragraph(nxt, para) -> bool:
    """判断``nxt``是否为应合并进列表项段落``para``的续行段落。

    条件（全部满足）：
    1. 至少一个``PdfLine``；
    2. 所有行都**不是**列表项行（避免吞掉下一个列表项）；
    3. 与列表项段落同一 ``layout_id``；或为 BabelDOC 的 ``fallback_line``
       （超宽行溢出片段）且与列表项段落最后一行处于同一 y 带；
    4. 所有行左缘 >= 列表项段落首行左缘（缩进续行/行内溢出，排除左对齐新段落）。
    """
    comps = nxt.pdf_paragraph_composition or []
    if not comps:
        return False
    lines = [c.pdf_line for c in comps if c and c.pdf_line]
    if not lines:
        return False
    if any(is_list_item_line(line) for line in lines):
        return False

    same_layout = (
        getattr(para, "layout_id", None) is not None
        and getattr(nxt, "layout_id", None) == para.layout_id
    )
    if not same_layout:
        label = str(getattr(nxt, "layout_label", "") or "")
        if "fallback" not in label.lower():
            return False
        last_band = _line_y_band(_last_line_of(para))
        if last_band is None:
            return False
        for line in lines:
            band = _line_y_band(line)
            if (
                band is None
                or band[0] > last_band[1] + 1.0
                or band[1] < last_band[0] - 1.0
            ):
                return False

    anchor = _paragraph_anchor_x(para)
    if anchor is None:
        return False
    for line in lines:
        x0 = _line_x0(line)
        if x0 is None or x0 < anchor - 1.0:
            return False
    return True


def _merge_continuation_lines_in_paragraphs(self, paragraphs: List[object]) -> None:
    """把列表项段落后紧随的缩进/溢出续行段落合并回列表项段落。

    在 ``_split_list_items_in_paragraphs`` 之后调用：BabelDOC 的\"短行拆分\"会把
    长列表项的缩进续行（``x0`` 大于列表项锚点、无编号前缀的行）以及超宽行溢出
    的 ``fallback_line`` 片段拆成独立段落，导致列表项正文被割裂、翻译重排错乱。
    本函数把这样的续行段落按原始顺序合并回所属列表项段落（仅当满足
    :func:`_is_continuation_paragraph` 的严格条件），使列表项段落保持
    ``[编号行, 续行...]`` 的完整形态。
    """
    if not paragraphs:
        return
    i = 0
    while i < len(paragraphs):
        para = paragraphs[i]
        if not _is_list_item_paragraph(para):
            i += 1
            continue
        while i + 1 < len(paragraphs) and _is_continuation_paragraph(
            paragraphs[i + 1], para
        ):
            nxt = paragraphs.pop(i + 1)
            para.pdf_paragraph_composition.extend(nxt.pdf_paragraph_composition)
        try:
            self.update_paragraph_data(para, update_unicode=True)
        except Exception:  # noqa: BLE001 -- 刷新失败不阻断翻译
            pass
        i += 1


def _split_list_items_in_paragraphs(self, paragraphs: List[object]) -> None:
    """把段落内"编号列表项"行拆分为独立段落（作用于 process_independent 之后）。

    对每个段落，扫描其行级 composition：找到第一个行首匹配列表编号模式
    的行（跳过第 0 行），从该行起拆成新段落。原段落保留拆分点之前的行，
    新段落插入原段落之后；循环继续处理新段落（可能仍含更多列表项行），
    直到所有列表项各自独立成段。

    只有段落行数 > 1 时才可能拆分；单个列表项本就独立成段，无需处理。
    """
    if not paragraphs:
        return
    i = 0
    while i < len(paragraphs):
        paragraph = paragraphs[i]
        comps = paragraph.pdf_paragraph_composition
        if len(comps) <= 1:
            i += 1
            continue

        # 找到第一个列表项行（非首行）
        split_at = None
        for j in range(1, len(comps)):
            comp = comps[j]
            line = comp.pdf_line if comp else None
            if line is None:
                continue
            if is_list_item_line(line):
                split_at = j
                break
        if split_at is None:
            i += 1
            continue

        from babeldoc.format.pdf.document_il import Box  # noqa: PLC0415
        from babeldoc.format.pdf.document_il import PdfParagraph  # noqa: PLC0415
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
            generate_base58_id,
        )

        new_paragraph = PdfParagraph(
            box=Box(0, 0, 0, 0),  # 临时边界框，update_paragraph_data 会重算
            pdf_paragraph_composition=comps[split_at:],
            unicode="",
            debug_id=generate_base58_id(),
            layout_label=paragraph.layout_label,
            layout_id=paragraph.layout_id,
        )
        paragraph.pdf_paragraph_composition = comps[:split_at]

        self.update_paragraph_data(paragraph)
        self.update_paragraph_data(new_paragraph)
        paragraphs.insert(i + 1, new_paragraph)

        # 不移动 i：新段落插在 i+1，下一轮迭代处理它（可能还有更多列表项行）。
        i += 1


def _protect_list_prefixes_in_paragraphs(self, paragraphs: List[object]) -> None:
    """把列表项行首的编号前缀构造成公式 composition（不参与翻译、原位保留）。

    对每个段落的每个行级 ``PdfLine``：行首匹配列表编号模式时，把前缀字符
    （``1.``/``a.``/``- `` 等）从行中拆出，构造成 ``PdfFormula`` composition
    放在行 composition 之前，并打上假 ``formula_layout_id``（防转回普通文本）
    与假 ``line_id``（防误合并）。翻译阶段公式以占位符原样保留，重排阶段由
    BabelDOC 公式重排逻辑按 x/y 偏移原位渲染 —— 编号不再受机器翻译改写。
    """
    from babeldoc.format.pdf.document_il import (  # noqa: PLC0415
        PdfParagraphComposition,
    )
    from babeldoc.format.pdf.document_il.il_version_1 import (  # noqa: PLC0415
        PdfFormula,
    )
    from babeldoc.format.pdf.document_il.utils.formular_helper import (  # noqa: PLC0415
        update_formula_data,
    )

    line_no = 0
    for paragraph in paragraphs:
        comps = paragraph.pdf_paragraph_composition
        if not comps:
            continue
        new_comps = []
        changed = False
        for comp in comps:
            line = comp.pdf_line if comp else None
            if line is None or not line.pdf_character:
                new_comps.append(comp)
                continue
            text = "".join((c.char_unicode or "") for c in line.pdf_character)
            m = _LIST_ITEM_PREFIX_RE.match(text)
            if m is None:
                new_comps.append(comp)
                continue
            matched = m.group(0)
            trimmed = matched.lstrip()
            if not trimmed:
                # 纯空白行，不保护
                new_comps.append(comp)
                continue
            lead_text = matched[: len(matched) - len(trimmed)]
            lead_chars = _prefix_char_count(line.pdf_character, lead_text)
            total_chars = _prefix_char_count(line.pdf_character, matched)
            prefix_len = total_chars - lead_chars
            if prefix_len <= 0 or lead_chars + prefix_len > len(line.pdf_character):
                new_comps.append(comp)
                continue
            prefix_chars = line.pdf_character[lead_chars : lead_chars + prefix_len]
            body_chars = line.pdf_character[lead_chars + prefix_len :]
            if not body_chars:
                # 整行都是编号（如独立的"第1条"标题行）：无正文可翻译，不保护
                new_comps.append(comp)
                continue

            # 构造编号公式（占位符机制与 TOC 保护一致）
            formula = PdfFormula(
                pdf_character=prefix_chars,
                line_id=_FAKE_LINE_ID_BASE - line_no,
            )
            fake_layout_id = _FAKE_FORMULA_LAYOUT_BASE - line_no
            for ch in formula.pdf_character:
                try:
                    ch.formula_layout_id = fake_layout_id
                except Exception:  # noqa: BLE001 -- 只读对象等极端情况，跳过标记
                    pass
            update_formula_data(formula)

            # 行收缩为正文部分，box 由 update_line_data 刷新
            line.pdf_character = body_chars
            try:
                self.update_line_data(line)
            except Exception:  # noqa: BLE001 -- box 刷新失败不影响翻译主流程
                pass

            # 公式放在行之前（编号在行首，物理位置在正文左侧）
            new_comps.append(PdfParagraphComposition(pdf_formula=formula))
            new_comps.append(comp)
            line_no += 1
            changed = True
            logger.debug(
                "BabelDOC list-prefix protect: %r -> formula=%r body=%r",
                text,
                "".join((c.char_unicode or "") for c in prefix_chars),
                "".join((c.char_unicode or "") for c in body_chars),
            )
        if changed:
            paragraph.pdf_paragraph_composition = new_comps
            try:
                self.update_paragraph_data(paragraph, update_unicode=True)
            except Exception:  # noqa: BLE001 -- 刷新失败不阻断翻译
                pass


def _patched_process_independent_paragraphs(self, paragraphs, median_width) -> None:
    """替换后的 ``ParagraphFinder.process_independent_paragraphs``。

    原始拆分逻辑执行完毕后，依次执行：编号列表项拆分 → 长列表项续行合并 →
    编号前缀公式保护。任何一步失败都降级为继续用当前段落（不阻断翻译）。
    """
    _ORIGINAL_PIP(self, paragraphs, median_width)
    if get_babeldoc_list_split_enabled():
        try:
            _split_list_items_in_paragraphs(self, paragraphs)
            _merge_continuation_lines_in_paragraphs(self, paragraphs)
            _protect_list_prefixes_in_paragraphs(self, paragraphs)
        except Exception as exc:  # noqa: BLE001 -- 处理失败不阻断翻译
            logger.warning(
                "BabelDOC list-item processing failed (%s: %s); "
                "continuing with original paragraphs",
                type(exc).__name__,
                str(exc)[:160],
            )


def get_babeldoc_list_split_enabled() -> bool:
    """读取列表项拆分开关（环境变量，默认开启）。"""
    raw = os.environ.get(_ENV_SPLIT_LIST, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def apply_babeldoc_list_split() -> bool:
    """为 BabelDOC 段落查找打上编号列表项拆分补丁（幂等）。

    Returns:
        True 表示补丁已生效（或原本已生效）；False 表示 babeldoc 不可用。
    """
    global _ORIGINAL_PIP
    try:
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
            ParagraphFinder,
        )
    except Exception:  # noqa: BLE001 -- babeldoc 可选依赖
        logger.debug("babeldoc not importable; list-split patch skipped")
        return False
    with _PATCH_LOCK:
        if _ORIGINAL_PIP is not None:
            return True
        _ORIGINAL_PIP = ParagraphFinder.process_independent_paragraphs
        ParagraphFinder.process_independent_paragraphs = (
            _patched_process_independent_paragraphs
        )
        logger.info(
            "BabelDOC list-item split patch applied (enabled=%s)",
            get_babeldoc_list_split_enabled(),
        )
        return True


def reset_babeldoc_list_split() -> bool:
    """恢复 BabelDOC ``process_independent_paragraphs`` 原始实现（供测试使用）。"""
    global _ORIGINAL_PIP
    try:
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
            ParagraphFinder,
        )
    except Exception:  # noqa: BLE001
        return False
    with _PATCH_LOCK:
        if _ORIGINAL_PIP is None:
            return True
        ParagraphFinder.process_independent_paragraphs = _ORIGINAL_PIP
        _ORIGINAL_PIP = None
        logger.info("BabelDOC list-item split patch restored")
        return True
