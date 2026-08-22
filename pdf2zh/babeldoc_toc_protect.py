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

多条目合并行（V1.24）：BabelDOC 的 ``ParagraphFinder`` 会把同一物理行的
**多个目录条目**合并成一个 ``PdfLine``（紧凑型书籍目录常见，如
``13.62 ... 388 13.63 ... 389``）。``detect_toc_line`` 基于行尾锚定的正则
只匹配最后一个条目，前 N-1 个条目的点线/页码会留在标题文本中被整体翻译、
破坏目录列结构。本模块因此先做**多条目检测**（非行尾锚定的点线+页码多匹配
+ 编号标题头验证 + 几何页码验证 + 置信度评分），命中时把该行逐条拆成
``[标题 PdfLine, 点线+页码 PdfFormula]`` 的序列，再走与单条目相同的保护链路。

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
import re
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


# ---------------------------------------------------------------------------
# 多条目合并行拆分（V1.24）
# ---------------------------------------------------------------------------

#: 目录条目标题头（多条目拆分用）：可选星号/井号等前缀 + 章节编号 + 空白 + 文本。
#: 比 ``toc.py._TOC_HEAD_RE`` 多允许 ``*`` 前缀（如 ``*13.60 Quadratic estimation``）。
_MERGED_ENTRY_HEAD = re.compile(r"^\s*[*×#†‡§]?\s*\d+(?:\.\d+)*[\.、:]?\s+\S")

#: 非行尾锚定的\"点线 + 页码\"多匹配正则（延迟构建，字符集与 toc.py 同步）。
_MERGED_ENTRY_RE: Optional[re.Pattern] = None


def _merged_entry_re() -> re.Pattern:
    """构建/取多条目\"点线+页码\"匹配正则（模块级缓存）。"""
    global _MERGED_ENTRY_RE
    if _MERGED_ENTRY_RE is None:
        from pdf2zh.toc import TOC_LEADER_CHARS  # noqa: PLC0415

        _MERGED_ENTRY_RE = re.compile(
            rf"(?P<lead>(?:[{TOC_LEADER_CHARS}])[\s{TOC_LEADER_CHARS}]*)?"
            rf"(?P<page>\d{{1,4}}(?:\s*[-–—]\s*\d{{1,4}})?|[ivxlcdmIVXLCDM]{{1,4}})"
        )
    return _MERGED_ENTRY_RE


def _build_offset_track(chars: List[object]) -> list:
    """构建带文本偏移的点线/数字字符几何记录。

    与 ``_build_track`` 的差异：额外记录每个字符在拼接文本中的起始偏移
    （``char_unicode`` 可能为空串，字符与文本索引非一一对应），供
    多条目拆分的几何页码验证与字符区间切分使用。

    Returns:
        ``[(text_offset, char, x0, x1), ...]``（几何缺失时 x0/x1 为 None）。
    """
    from pdf2zh.toc import TOC_LEADER_CHARS  # noqa: PLC0415

    out: list = []
    pos = 0
    for ch in chars:
        unicode_ = ch.char_unicode or ""
        if unicode_ in TOC_LEADER_CHARS or unicode_.isdigit():
            try:
                bbox = ch.visual_bbox.box
            except Exception:  # noqa: BLE001 -- 几何缺失时记录占位
                out.append((pos, unicode_, None, None))
            else:
                out.append((pos, unicode_, float(bbox.x), float(bbox.x2)))
        pos += len(unicode_)
    return out


def _char_offsets(chars: List[object]) -> List[int]:
    """每个字符在拼接文本中的起始偏移（``text[pos_i]`` 对应 ``chars[i]``）。"""
    offsets: List[int] = []
    pos = 0
    for ch in chars:
        offsets.append(pos)
        pos += len(ch.char_unicode or "")
    return offsets


def _offset_to_char(offsets: List[int], pos: int) -> int:
    """文本偏移 → 字符索引（二分；pos 在字符间隙时归入左侧字符）。"""
    lo, hi = 0, len(offsets)
    while lo < hi:
        mid = (lo + hi) // 2
        if offsets[mid] <= pos:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _split_merged_toc_line(text: str, offset_track: list,
                           page_width) -> List[tuple]:
    """一行含多个目录条目时逐条拆分。

    对一行文本找所有\"点线 + 页码\"匹配（非行尾锚定），逐条验证：
      1. 标题头以章节编号开头（``_MERGED_ENTRY_HEAD``）且长度 >= 2；
      2. 页码数字在几何 track 中有对应（缺几何保守跳过）；
      3. 置信度评分 >= ``TOC_PROTECT_THRESHOLD``。

    返回 ``[(title, leader, page, title_start, lead_start, lead_end), ...]``，
    仅当有效条目 >= 2 时返回列表；否则返回空列表（调用方回退单条目逻辑）。
    """
    from pdf2zh.toc import (  # noqa: PLC0415
        TOC_LEADER_CHARS,
        TOC_PROTECT_THRESHOLD,
        _score_toc,
    )

    matches = []
    for m in _merged_entry_re().finditer(text):
        lead = m.group("lead") or ""
        if sum(lead.count(c) for c in TOC_LEADER_CHARS) < 2:
            continue
        page = (m.group("page") or "").strip()
        if not page:
            continue
        matches.append((m.start("lead"), m.end("page"), lead, page))
    if len(matches) < 2:
        return []

    entries: List[tuple] = []
    prev_end = 0
    for lead_start, lead_end, lead, page in matches:
        title = text[prev_end:lead_start].strip()
        if len(title) < 2 or not _MERGED_ENTRY_HEAD.match(title):
            prev_end = lead_end
            continue
        # 页码数字几何（右缘验证与置信度评分用）
        page_geo = None
        for pos, ch, x0, x1 in offset_track:
            if lead_start <= pos < lead_end and ch.isdigit() and x0 is not None:
                page_geo = (float(x0), float(x1))
        if page_geo is None:
            prev_end = lead_end
            continue
        score = _score_toc(title, lead, page, page_geo[0], page_geo[1], page_width)
        if score < TOC_PROTECT_THRESHOLD:
            prev_end = lead_end
            continue
        entries.append((title, lead, page, prev_end, lead_start, lead_end))
        prev_end = lead_end
    return entries if len(entries) >= 2 else []


def _try_protect_merged_line(self, page, comp, line_no: int):
    """多条目合并行逐条保护：把一行含 ≥2 个目录条目的 PdfLine 拆成多条。

    Returns:
        每个目录条目一组 ``[标题 PdfLine composition, 点线+页码 PdfFormula
        composition]`` 的列表；非合并行或不可拆分时返回 None（调用方回退
        单条目 ``_try_protect_line``）。
    """
    line = comp.pdf_line
    if line is None or not line.pdf_character:
        return None
    chars = line.pdf_character
    text = "".join((c.char_unicode or "") for c in chars)
    if len(text) < 8:
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

    offset_track = _build_offset_track(chars)
    if not offset_track:
        return None
    entries = _split_merged_toc_line(text, offset_track, page_width)
    if len(entries) < 2:
        return None

    from babeldoc.format.pdf.document_il import (  # noqa: PLC0415
        PdfLine,
        PdfParagraphComposition,
    )
    from babeldoc.format.pdf.document_il.il_version_1 import (  # noqa: PLC0415
        PdfFormula,
    )
    from babeldoc.format.pdf.document_il.utils.formular_helper import (  # noqa: PLC0415
        update_formula_data,
    )

    offsets = _char_offsets(chars)
    blocks = []
    for (title, lead, page, title_start, lead_start, lead_end) in entries:
        ci_title = _offset_to_char(offsets, title_start)
        ci_lead = _offset_to_char(offsets, lead_start)
        ci_end = _offset_to_char(offsets, lead_end - 1) + 1
        if not (0 <= ci_title <= ci_lead < ci_end <= len(chars)):
            continue
        # 标题 → 独立 PdfLine；点线+页码 → 假公式（防转回普通文本、防误合并）
        title_line = PdfLine(pdf_character=chars[ci_title:ci_lead])
        try:
            self.update_line_data(title_line)
        except Exception:  # noqa: BLE001 -- box 刷新失败不影响翻译主流程
            pass
        formula = PdfFormula(
            pdf_character=chars[ci_lead:ci_end],
            line_id=_FAKE_LINE_ID_BASE - line_no,
        )
        fake_layout_id = _FAKE_FORMULA_LAYOUT_BASE - line_no
        for ch in formula.pdf_character:
            try:
                ch.formula_layout_id = fake_layout_id
            except Exception:  # noqa: BLE001 -- 只读对象等极端情况，跳过标记
                pass
        update_formula_data(formula)
        blocks.append([
            PdfParagraphComposition(pdf_line=title_line),
            PdfParagraphComposition(pdf_formula=formula),
        ])
        line_no += 1
        logger.info(
            "BabelDOC TOC protect (merged): entry %d/%d %r -> title=%r formula=%r",
            len(blocks), len(entries), title,
            "".join((c.char_unicode or "") for c in chars[ci_title:ci_lead]),
            "".join((c.char_unicode or "") for c in chars[ci_lead:ci_end]),
        )
    if not blocks:
        return None
    return blocks


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
    （多条目合并行 —— 一行含 ≥2 个目录条目 —— 会被逐条拆成多组这样的
    composition，见 ``_try_protect_merged_line``。）
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
                merged_blocks = _try_protect_merged_line(self, page, comp, line_no)
                if merged_blocks:
                    if kept:
                        blocks.append(kept)
                        kept = []
                    blocks.extend(merged_blocks)
                    line_no += len(merged_blocks)
                    continue
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

