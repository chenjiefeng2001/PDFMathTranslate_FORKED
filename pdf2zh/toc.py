"""书籍目录（TOC）行处理：识别"标题 + 点线 + 页码"式目录条目。

目录行由三部分组成：标题（左对齐）、点线引导（dot leaders）、页码（右对齐）。
整行作为普通段落翻译会破坏点线与页码结构（点线/页码被机器翻译改写、
译文膨胀折行、右对齐页码列丢失），因此把标题单独切出翻译，
点线与页码保留原样并原位渲染。

本模块独立于 converter.py，避免 legacy 转换器继续膨胀（v3 strangulation 门控）。
"""
from __future__ import annotations

import re

# 目录行点线字符集：'.' 常规点线、'·' 中点、'…'/'‥' 省略号式点线
TOC_LEADER_CHARS = ".·…‥"
# 目录行模式：标题 + 点线引导（2+ 个点线字符）+ 右对齐页码（1-4 位数字）
TOC_LEADER_RE = re.compile(r"(?P<lead>(?:[.·…‥])[\.\s·…‥]*)(?P<page>\d{1,4})\s*$")


def detect_toc_line(text, brk, track, page_right):
    """识别"标题 + 点线（dot leaders）+ 页码"式目录条目。

    参数：
        text:       段落原始文本
        brk:        段落是否含物理换行（多行段落不做目录行处理）
        track:      段落内点线字符/数字字符记录 [(字符, x0, x1), ...]
        page_right: 段落右边界（= 页码右边缘，用于右对齐）
    返回：
        dict（title / page_digits / page_start_x / page_right_x）或 None
    """
    if brk:
        return None
    m = TOC_LEADER_RE.search(text or "")
    if not m:
        return None
    lead = m.group("lead")
    if sum(lead.count(c) for c in TOC_LEADER_CHARS) < 2:
        return None
    title = text[: m.start()].rstrip()
    if len(title) < 2:
        return None
    page = m.group("page")
    # 从字符记录中取尾部数字串的几何位置（页码）
    i = len(track) - 1
    while i >= 0 and not track[i][0].isdigit():
        i -= 1
    if i < 0:
        return None
    j = i
    while j >= 0 and track[j][0].isdigit():
        j -= 1
    digits = track[j + 1 : i + 1]
    if len(digits) != len(page):
        return None
    page_start_x = digits[0][1]
    page_right_x = page_right if page_right is not None else digits[-1][2]
    if page_right_x - page_start_x <= 0:
        return None
    return {
        "title": title,
        "page_digits": page,
        "page_start_x": page_start_x,
        "page_right_x": page_right_x,
    }


def char_adv(conv, ch, size):
    """计算单个字符的推进宽度（与 converter 主循环相同的字体度量逻辑）。

    供目录行点线/页码原位渲染使用。conv 为 TranslateConverter 实例。
    """
    fcur_ = None
    try:
        if conv.fontmap["tiro"].to_unichr(ord(ch)) == ch:
            fcur_ = "tiro"
    except Exception:
        pass
    if fcur_ is None:
        fcur_ = conv.noto_name
    tm = conv.text_metrics.get(fcur_) if conv.text_metrics else None
    if tm:
        return tm.char_width(ch, size)
    if fcur_ == conv.noto_name:
        try:
            return conv.noto.char_lengths(ch, size)[0]
        except Exception:
            return size * 0.5
    font_obj = conv.fontmap.get(fcur_)
    if font_obj:
        adv = font_obj.char_width(ord(ch)) * size
        if adv <= 0 and not conv.skip_subset_fonts:
            adv = size * 0.5
        return adv
    return size * 0.5
