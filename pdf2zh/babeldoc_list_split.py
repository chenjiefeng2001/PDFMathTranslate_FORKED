"""BabelDOC 数字编号列表项（``1. XXX`` / ``2. XXX``）的段落拆分补丁。

背景
----
BabelDOC 的 ``ParagraphFinder._group_characters_into_paragraphs`` 按版面布局
（layout）分组字符：doclayout 模型把页面内的连续列表（``1. XXX``、``2. XXX``…）
通常识别为**单个** ``plain text`` 布局框，因此所有列表项字符合并成一个
``PdfParagraph``。而 ``process_independent_paragraphs`` 只会把"行首是 bullet
符号（``•`` 等）"的行拆成独立段落，**不识别数字编号** —— 于是编号列表被整体
当做一个段落翻译，翻译后重排时编号与正文混排，页面排版错乱。

本模块在 pdf2zh 侧以运行时补丁（monkey-patch，与
:mod:`pdf2zh.babeldoc_onnx_backend` 同一模式）包装 BabelDOC 的
``ParagraphFinder.process_independent_paragraphs``：原始拆分逻辑执行完毕后，
再对段落做**数字编号列表项拆分**——段落内若存在"行首匹配列表编号模式"的行，
则从该行起拆成独立 ``PdfParagraph``，使每个列表项独立翻译、独立排版。

开关（``PDF2ZH_BABELDOC_SPLIT_LIST_ITEMS``，默认 ``1`` 开启）：

============  ==============================================================
取值          行为
============  ==============================================================
``1``/``on``  开启列表项拆分（推荐：修复编号列表排版错乱）
``0``/``off`` 关闭，保持 BabelDOC 原生行为（整个列表作为一个段落）
============  ==============================================================

拆分对 BabelDOC 后续流程透明：补丁插入点在 ``process_independent_paragraphs``
内部末尾，随后的 ``merge_alternating_line_number_paragraphs``、
``update_paragraph_data(update_unicode=True)``、``fix_overlapping_paragraphs``
与 ``_set_paragraph_render_order`` 都会自动作用于新拆出的段落。

列表编号模式仅匹配"数字/字母 + 分隔符 + 空白"（``1. ``、``1) ``、``(1) ``、
``1、 ``、``a. `` 等），且要求编号后跟空白，避免误伤 ``1.5``、``2024.12``
这类小数/年份行首。
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
#: - 数字编号限 1-3 位（排除 2024. 这种年份行首），西文点/全角点/右括号
#:   后要求空白（排除 1.5 小数）；
#: - 中文顿号（1、）后不要求空白；
#: - 字母编号（a. / A)）后要求空白。
_LIST_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:\(\d{1,4}\)|\d{1,3}[.．)]\s|\d{1,3}、|[A-Za-z][.、．.)]\s)"
)

#: 补丁锁 + 原始 ``process_independent_paragraphs`` 引用（None = 未打补丁）。
_PATCH_LOCK = threading.Lock()
_ORIGINAL_PIP: Optional[object] = None


def is_list_item_line(line) -> bool:
    """判断一行是否为列表项（行首匹配编号模式）。"""
    text = "".join(
        (c.char_unicode or "") for c in (line.pdf_character or [])
    )
    return bool(_LIST_ITEM_PREFIX_RE.match(text))


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


def _patched_process_independent_paragraphs(self, paragraphs, median_width) -> None:
    """替换后的 ``ParagraphFinder.process_independent_paragraphs``。"""
    _ORIGINAL_PIP(self, paragraphs, median_width)
    if get_babeldoc_list_split_enabled():
        try:
            _split_list_items_in_paragraphs(self, paragraphs)
        except Exception as exc:  # noqa: BLE001 -- 拆分失败不阻断翻译
            logger.warning(
                "BabelDOC list-item split failed (%s: %s); "
                "continuing with original paragraphs",
                type(exc).__name__, str(exc)[:160],
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
