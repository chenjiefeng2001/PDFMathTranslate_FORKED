"""BabelDOC 目录（TOC）行"点线引导 + 页码"公式保护补丁。

背景
----
BabelDOC 0.6.x 把书籍目录（TOC）区域识别为普通 ``plain text`` 段落，目录行
（``1 G. Müller .......... 27``）整行进入翻译器后，点线引导（``...``）与右对齐
页码会被机器翻译改写、译文膨胀折行、右对齐页码列丢失 —— 目录排版被破坏。

pdf2zh 的 legacy 转换器已有独立的目录行识别（:mod:`pdf2zh.toc`：点线引导 +
页码 / 空列页码双模式 + 置信度评分 + 字符几何验证）。本模块以运行时补丁
（monkey-patch，与 :mod:`pdf2zh.babeldoc_onnx_backend` 同一模式）把同一套识别
接到 BabelDOC 的 ``ParagraphFinder.process_page``：

1. 原始 ``process_page`` 执行完毕后，对每个段落的行级 ``PdfLine`` 做 TOC 识别
   （复用 ``pdf2zh.toc.detect_toc_line``）；
2. 命中时把"点线引导 + 页码"字符从 ``PdfLine`` 拆出，构造成 ``PdfFormula``
   composition 追加在该行之后 —— 翻译阶段公式以占位符原样保留（不进入翻译
   文本），重排阶段由 BabelDOC 公式重排逻辑按 x/y 偏移原位渲染；
3. 目录行（标题 + 点线/页码公式）与相邻非目录行会被拆成独立段落，避免
   BabelDOC 的"单元扁平化重排"把跨行的标题/公式挤到同一行；
4. 标题部分仍作为普通文本行参与翻译。

保护对 BabelDOC 后续流程透明：公式 composition 会经过
``process_page_formulas``（原样保留）、``process_translatable_formulas``
（通过假 ``formula_layout_id`` 避免被转回普通文本）、``process_page_offsets``
（计算相对标题行的 x/y 偏移）、``IlTranslator``（占位符往返）、``Typesetting``
（原位渲染）。

开关（``PDF2ZH_BABELDOC_TOC_PROTECT``，默认 ``1`` 开启）：

============  ==============================================================
取值          行为
============  ==============================================================
``1``/``on``  开启目录行保护（推荐：点线/页码不参与翻译、原位保留）
``0``/``off`` 关闭，保持 BabelDOC 原生行为（整行作为一个段落整体翻译）
============  ==============================================================
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

#: 环境变量名。
_ENV_TOC_PROTECT = "PDF2ZH_BABELDOC_TOC_PROTECT"

#: 假公式布局 id 基数：给 TOC 保护公式字符打上"非空 formula_layout_id"标记，
#: 使 ``is_translatable_formula`` / ``should_split_formula`` 认为它不是"纯数字
#: 可翻译公式"，从而避免被 BabelDOC 转回普通文本行重新进入翻译器。
#: 值取远小于真实布局 id（真实 id 为 doclayout 正数）的负数，杜绝与真实布局冲突。
_FAKE_FORMULA_LAYOUT_BASE = -1000

#: 假行号基数：TOC 保护公式的 ``line_id`` 使用负数，保证与 BabelDOC 解析阶段
#: 生成的公式（line_id >= 0）永远不同 —— ``merge_overlapping_formulas`` 的
#: ``same_line`` 判断要求两个公式 line_id 相同才合并，从而不会被误合并。
_FAKE_LINE_ID_BASE = -1000

#: 补丁锁 + 原始 ``process_page`` 引用（None = 未打补丁）。
_PATCH_LOCK = threading.Lock()
_ORIGINAL_PROCESS_PAGE: Optional[object] = None


def get_babeldoc_toc_protect_enabled() -> bool:
    """读取目录行保护开关（环境变量，默认开启）。"""
    raw = os.environ.get(_ENV_TOC_PROTECT, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _build_track(chars: List[object]) -> list:
    """从 ``PdfLine`` 字符构建 toc.py 所需的字符几何 track。

    ``detect_toc_line`` 的 track 只关心点线字符与数字字符：``[(字符, x0, x1)]``。
    BabelDOC 字符使用 ``visual_bbox``（与页面渲染坐标一致）。
    """
    from pdf2zh.toc import TOC_LEADER_CHARS  # noqa: PLC0415

    track: list = []
    for ch in chars:
        unicode_ = ch.char_unicode or ""
        if unicode_ in TOC_LEADER_CHARS or unicode_.isdigit():
            try:
                bbox = ch.visual_bbox.box
            except Exception:  # noqa: BLE001 -- 几何缺失时保守跳过该字符
                continue
            track.append((unicode_, float(bbox.x), float(bbox.x2)))
    return track


def _toc_split_index(text: str) -> Optional[int]:
    """计算目录行"点线引导 / 页码列"在文本中的起始字符索引。

    与 ``pdf2zh.toc.detect_toc_line`` 的两种识别分支保持一致：
    优先点线引导（``TOC_LEADER_RE``），其次空列页码（``_TOC_SPACE_PAGE_RE``）。
    返回 ``None`` 表示文本形态不构成目录行。
    """
    from pdf2zh.toc import (  # noqa: PLC0415
        TOC_LEADER_CHARS,
        TOC_LEADER_RE,
        _TOC_SPACE_HEAD,
        _TOC_SPACE_PAGE_RE,
    )

    m = TOC_LEADER_RE.search(text)
    if m is not None:
        lead = m.group("lead")
        if sum(lead.count(c) for c in TOC_LEADER_CHARS) >= 2:
            title = text[: m.start()].rstrip()
            if len(title) >= 2:
                return m.start("lead")
    sm = _TOC_SPACE_PAGE_RE.match(text)
    if sm is not None and _TOC_SPACE_HEAD.match(sm.group("title")):
        return sm.start("page")
    return None


def _try_protect_line(self, page, comp, line_no: int) -> Optional[List[object]]:
    """对单个 ``PdfLine`` composition 做目录行保护。

    命中时返回 ``[原标题行 composition, 点线+页码公式 composition]``；
    未命中或不可保护时返回 ``None``（调用方原样保留原 composition）。
    """
    from pdf2zh.toc import detect_toc_line  # noqa: PLC0415

    line = comp.pdf_line
    if line is None or not line.pdf_character:
        return None

    chars = line.pdf_character
    text = "".join((c.char_unicode or "") for c in chars)
    if len(text) < 4:
        return None

    track = _build_track(chars)
    if not track:
        return None

    page_box = getattr(page, "box", None)
    if page_box is None:
        # BabelDOC 的 Page 没有 .box，页面几何在 cropbox / mediabox 上。
        for holder in ("cropbox", "mediabox"):
            box = getattr(page, holder, None)
            if box is not None and getattr(box, "box", None) is not None:
                page_box = box.box
                break
    page_width = None
    if page_box is not None:
        page_width = float(page_box.x2 - page_box.x)
    # page_right 传 None：让 detect_toc_line 使用页码数字自身的右缘做对齐判定，
    # 避免"段落右边界 > 页码右缘"时引入的几何偏差。
    spec = detect_toc_line(text, False, track, None, page_width)
    if spec is None:
        return None

    split_idx = _toc_split_index(text)
    # 标题为空（split_idx == 0）或点线+页码为空（split_idx >= len）不保护。
    if split_idx is None or not (0 < split_idx < len(chars)):
        return None

    from babeldoc.format.pdf.document_il import PdfParagraphComposition  # noqa: PLC0415
    from babeldoc.format.pdf.document_il.il_version_1 import PdfFormula  # noqa: PLC0415
    from babeldoc.format.pdf.document_il.utils.formular_helper import (  # noqa: PLC0415
        update_formula_data,
    )

    title_chars = chars[:split_idx]
    formula_chars = chars[split_idx:]

    # 构造点线+页码公式；假 formula_layout_id 防转回普通文本，假 line_id 防误合并。
    formula = PdfFormula(
        pdf_character=formula_chars,
        line_id=_FAKE_LINE_ID_BASE - line_no,
    )
    fake_layout_id = _FAKE_FORMULA_LAYOUT_BASE - line_no
    for ch in formula_chars:
        try:
            ch.formula_layout_id = fake_layout_id
        except Exception:  # noqa: BLE001 -- 只读对象等极端情况，跳过标记
            pass
    update_formula_data(formula)

    # 原标题行收缩为标题部分（后续 process_page_formulas 会重建并重算其 box）。
    line.pdf_character = title_chars
    try:
        self.update_line_data(line)
    except Exception:  # noqa: BLE001 -- box 刷新失败不影响翻译主流程
        pass

    logger.info(
        "BabelDOC TOC protect: split line %r -> title=%r formula=%r (score=%s)",
        text,
        "".join((c.char_unicode or "") for c in title_chars),
        "".join((c.char_unicode or "") for c in formula_chars),
        spec.get("score"),
    )
    return [comp, PdfParagraphComposition(pdf_formula=formula)]


def _protect_toc_lines_in_page(self, page) -> None:
    """扫描页面所有段落，对命中的目录行做点线/页码公式保护。

    命中时该行被拆成两条 composition：``[原标题行（仅标题）, 点线+页码公式]``。
    为了让 BabelDOC 重排阶段能独立定位这些行，命中行与相邻非目录行会被拆成
    独立段落（复用原段落的 ``layout_id``/``layout_label``，保持原始顺序）；
    否则整页合段后，BabelDOC 的"单元扁平化重排"会把跨行的目录标题/公式挤到
    同一行。
    """
    if not page.pdf_paragraph:
        return
    from babeldoc.format.pdf.document_il import Box  # noqa: PLC0415
    from babeldoc.format.pdf.document_il import PdfParagraph  # noqa: PLC0415
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
        generate_base58_id,
    )

    line_no = 0
    new_paragraphs = []
    for paragraph in page.pdf_paragraph:
        comps = paragraph.pdf_paragraph_composition
        if not comps:
            new_paragraphs.append(paragraph)
            continue
        blocks: list = []
        kept: list = []
        for comp in comps:
            if comp.pdf_line is not None:
                result = _try_protect_line(self, page, comp, line_no)
                line_no += 1
                if result is not None:
                    if kept:
                        blocks.append(kept)
                        kept = []
                    blocks.append(result)
                    continue
            kept.append(comp)
        if kept:
            blocks.append(kept)
        if not any(
            len(block) == 2 and block[1].pdf_formula is not None
            for block in blocks
        ):
            # 没有目录行命中：原样保留原段落（零改动）。
            new_paragraphs.append(paragraph)
            continue
        # 至少一个目录行：按原始顺序把每个块拆成独立段落。
        for block in blocks:
            new_paragraph = PdfParagraph(
                box=Box(0, 0, 0, 0),  # update_paragraph_data 会重算
                pdf_paragraph_composition=block,
                unicode="",
                debug_id=generate_base58_id(),
                layout_label=paragraph.layout_label,
                layout_id=paragraph.layout_id,
            )
            try:
                self.update_paragraph_data(new_paragraph, update_unicode=True)
            except Exception:  # noqa: BLE001 -- 刷新失败不阻断翻译
                pass
            new_paragraphs.append(new_paragraph)
    page.pdf_paragraph = new_paragraphs


def _patched_process_page(self, page) -> None:
    """替换后的 ``ParagraphFinder.process_page``：原始逻辑 + 目录行保护。"""
    _ORIGINAL_PROCESS_PAGE(self, page)
    if get_babeldoc_toc_protect_enabled():
        try:
            _protect_toc_lines_in_page(self, page)
        except Exception as exc:  # noqa: BLE001 -- 保护失败不阻断翻译
            logger.warning(
                "BabelDOC TOC protect failed (%s: %s); "
                "continuing with original paragraphs",
                type(exc).__name__, str(exc)[:160],
            )


def apply_babeldoc_toc_protect() -> bool:
    """为 BabelDOC ``ParagraphFinder.process_page`` 打上目录行保护补丁（幂等）。

    Returns:
        True 表示补丁已生效（或原本已生效）；False 表示 babeldoc 不可用。
    """
    global _ORIGINAL_PROCESS_PAGE
    try:
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
            ParagraphFinder,
        )
    except Exception:  # noqa: BLE001 -- babeldoc 可选依赖
        logger.debug("babeldoc not importable; TOC-protect patch skipped")
        return False
    with _PATCH_LOCK:
        if _ORIGINAL_PROCESS_PAGE is not None:
            return True
        _ORIGINAL_PROCESS_PAGE = ParagraphFinder.process_page
        ParagraphFinder.process_page = _patched_process_page
        logger.info(
            "BabelDOC TOC-protect patch applied (enabled=%s)",
            get_babeldoc_toc_protect_enabled(),
        )
        return True


def reset_babeldoc_toc_protect() -> bool:
    """恢复 BabelDOC ``ParagraphFinder.process_page`` 原始实现（供测试使用）。"""
    global _ORIGINAL_PROCESS_PAGE
    try:
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
            ParagraphFinder,
        )
    except Exception:  # noqa: BLE001
        return False
    with _PATCH_LOCK:
        if _ORIGINAL_PROCESS_PAGE is None:
            return True
        ParagraphFinder.process_page = _ORIGINAL_PROCESS_PAGE
        _ORIGINAL_PROCESS_PAGE = None
        logger.info("BabelDOC TOC-protect patch restored")
        return True

