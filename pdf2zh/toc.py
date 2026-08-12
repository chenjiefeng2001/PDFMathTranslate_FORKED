"""书籍目录（TOC）行处理：识别"标题 + 点线 + 页码"式目录条目。

目录行由三部分组成：标题（左对齐）、点线引导（dot leaders）、页码（右对齐）。
整行作为普通段落翻译会破坏点线与页码结构（点线/页码被机器翻译改写、
译文膨胀折行、右对齐页码列丢失），因此把标题单独切出翻译，
点线与页码保留原样并原位渲染。

V1.17-3：新增「无点线空列页码」识别 —— 目录行页码列改用空格分隔时，
``detect_toc_line`` 通过几何页码列（页码在页面右缘）与标题编号开头
双重条件识别，页码列同样原位渲染、不参与翻译。

V1.19（本版）：把识别从"硬性二值判定"升级为**置信度评分 + 双模式**：

    score = 0.35×点线纯净度 + 0.25×页码列几何 + 0.20×起始结构
          + 0.10×页码形态 + 0.10×标题长度
    命中规则：
      - score ≥ 0.55 → mode="full"     结构化渲染（禁折行、点线/页码原位）
      - 0.30 ≤ score < 0.55 → mode="protect"
        保护性渲染：标题仍单独翻译、折行/压缩照常，点线+页码原样追加在尾部
        （绝不进翻译器；适用于页码区间、弱结构词等低置信形态）
      - score < 0.30 或几何无法验证（track 缺失/不符）→ None
    页码容错：支持 1-4 位数字、区间（12–13）、罗马数字（xii）。

本模块独立于 converter.py，避免 legacy 转换器继续膨胀（v3 strangulation 门控）。
"""
from __future__ import annotations

import re

# 目录行点线字符集：'.' 常规点线、'·' 中点、'…'/'‥' 省略号式点线
TOC_LEADER_CHARS = ".·…‥"
# 页码 token：1-4 位数字、区间（12–13）、罗马数字（xii/IV）
_TOC_PAGE_TOK = r"\d{1,4}(?:\s*[-–—]\s*\d{1,4})?|[ivxlcdmIVXLCDM]{1,4}"
# 目录行模式：标题 + 点线引导（2+ 个点线字符）+ 页码（数字/区间/罗马）
TOC_LEADER_RE = re.compile(
    rf"(?P<lead>(?:[{TOC_LEADER_CHARS}])[\s{TOC_LEADER_CHARS}]*)(?P<page>{_TOC_PAGE_TOK})\s*\)?$"
)
# 无点线空列页码：标题 + 空白 + 右对齐页码（空列页码目录行）
_TOC_SPACE_PAGE_RE = re.compile(r"^(?P<title>\S.*?)[ \t]{1,}(?P<page>\d{1,4})\s*$")
# 标题以章节编号开头（空列页码目录行的硬性前提，避免误拆正文尾数字）
_TOC_SPACE_HEAD = re.compile(r"^\s*\d+(?:\.\d+)*\s+\S")
# 目录行结构开头（章/节/附录/§/裸编号/中文"第X"）
_TOC_HEAD_RE = re.compile(
    r"^\s*(?:(?:chapter|ch|section|sec|subsection|subsec|part|appendix|appx|annex)\b\.?\s*"
    r"|§\s*|第"
    r"|\d+(?:\.\d+)*[\.、:]?)",
    re.IGNORECASE,
)
# 置信度阈值（0.30=保护 / 0.55=结构化渲染）
TOC_FULL_THRESHOLD = 0.55
TOC_PROTECT_THRESHOLD = 0.30


def _trailing_digits(track):
    """从字符记录取尾部数字串几何：[(字符, x0, x1), ...] 或 []。"""
    i = len(track) - 1
    while i >= 0 and not track[i][0].isdigit():
        i -= 1
    if i < 0:
        return []
    j = i
    while j >= 0 and track[j][0].isdigit():
        j -= 1
    return track[j + 1 : i + 1]


def _right_column_digits(track, x_min):
    """取页面右缘列内的尾部数字串（空列页码，与标题编号在同一 track 中）。"""
    run = []
    for ch, x0, x1 in reversed(track):
        if ch.isdigit() and x0 > x_min:
            run.append((ch, x0, x1))
        else:
            break
    run.reverse()
    return run


def _tail_number(page: str) -> str:
    """取页码 token 的最后一个阿拉伯数字串（"12–13"→"13"，"xii"→""）。"""
    m = re.search(r"(\d+)\s*$", (page or "").strip())
    return m.group(1) if m else ""


def _score_toc(title, lead, page, page_start_x, page_right_x, page_width) -> float:
    """置信度评分（见模块 docstring 权重表）。"""
    lead_txt = lead or ""
    leader_chars = sum(lead_txt.count(c) for c in TOC_LEADER_CHARS)
    leader_purity = (
        leader_chars / len(lead_txt) if lead_txt and leader_chars else 0.0
    )
    if page_width:
        page_col = 1.0 if page_start_x >= 0.8 * page_width else 0.3
    else:
        page_col = 0.4  # 几何未知 → 中性值
    start_fmt = 1.0 if _TOC_HEAD_RE.match(title) else 0.05
    page_digits_ok = bool(re.fullmatch(r"\d{1,4}|[ivxlcdmIVXLCDM]{1,4}", (page or "").strip()))
    digits_shape = 1.0 if page_digits_ok else 0.5
    title_len = min(1.0, len(title) / 8.0)
    return (
        0.35 * leader_purity
        + 0.25 * page_col
        + 0.20 * start_fmt
        + 0.10 * digits_shape
        + 0.10 * title_len
    )


def looks_like_toc_text(text) -> bool:
    """纯文本级"疑似目录行"判定（不依赖几何 track），供日志 / 观察用。

    与 ``detect_toc_line`` 互补：track 缺失时 detect 保守返回 None，
    但这里可提示"文本形态像目录行，可能因缺字符几何被漏检"。
    """
    text = (text or "").rstrip()
    if not text or len(text) < 4:
        return False
    m = TOC_LEADER_RE.search(text)
    if m is not None and sum(m.group(1).count(c) for c in TOC_LEADER_CHARS) >= 2:
        return len(text[: m.start()].rstrip()) >= 2
    sm = _TOC_SPACE_PAGE_RE.match(text)
    return bool(sm and _TOC_SPACE_HEAD.match(sm.group("title")))


def _detect_leader(
    text, brk, track, page_right, page_width
):
    """候选 A：点线引导 + 页码（支持区间/罗马）。返回 spec 或 None。"""
    m = TOC_LEADER_RE.search(text)
    if m is None:
        return None
    lead = m.group("lead")
    if sum(lead.count(c) for c in TOC_LEADER_CHARS) < 2:
        return None
    title = text[: m.start()].rstrip()
    if len(title) < 2:
        return None
    page = m.group("page").strip()
    digits = _trailing_digits(track)
    if not digits:
        return None  # 几何缺失：保守不识别（converter 侧可经 looks_like_toc_text 提示）
    tail = _tail_number(page)
    if tail and "".join(c for c, _, _ in digits) != tail:
        return None
    page_start_x = float(digits[0][1])
    page_end_x = page_right if page_right is not None else float(digits[-1][2])
    if page_end_x - page_start_x <= 0:
        return None
    page = page if page else tail
    score = _score_toc(title, lead, page, page_start_x, page_end_x, page_width)
    mode = "full" if score >= TOC_FULL_THRESHOLD else (
        "protect" if score >= TOC_PROTECT_THRESHOLD else None
    )
    # 区间页码（12–13）：右对齐渲染语义弱 → 一律走保护模式（标题折行、点线/页码尾部原位保留）
    if mode == "full" and re.search(r"[-–—]", page):
        mode = "protect"
    return {
        "title": title,
        "page_digits": page,
        "page_start_x": page_start_x,
        "page_right_x": page_end_x,
        "leader_orig": lead.strip(),
        "score": round(score, 4),
        "mode": mode,
    }


def detect_toc_line(text, brk, track, page_right, page_width=None):
    """识别目录行：标题 + 点线引导 + 页码（典型）+ 空列页码列目录。

    参数：
        text:       段落原始文本
        brk:        段落是否含物理换行（多行段落不做目录行处理）
        track:      段落内点线/数字字符记录 [(字符, x0, x1), ...]
        page_right: 段落右边界（= 页码右边缘，用于页码对齐）
        page_width: 页面宽度（空列页码列的几何检测用，可缺省）
    返回：
        dict（title / page_digits / page_start_x / page_right_x /
        leader / score / mode∈{full,protect}）或 None。

    V1.19 起为**置信度驱动**：结构词全命中 → "full"（结构化渲染）；置信度
    介于 0.30–0.55 的弱形态 → "protect"（点线/页码不翻译、尾部原位保留）；
    未达标或几何缺失（track 不符）→ None。
    """
    if brk:
        return None
    text = (text or "").rstrip()
    if not text:
        return None

    spec = _detect_leader(text, brk, track, page_right, page_width)
    if spec is not None:
        return spec if spec["mode"] is not None else None

    # 分支 B：无点线空列页码（几何页码列，页面宽必传）
    sm = _TOC_SPACE_PAGE_RE.match(text)
    if sm is None or not _TOC_SPACE_HEAD.match(sm.group("title")):
        return None
    page = sm.group("page")
    x_min = 0.8 * page_width if page_width else 0.0
    digits = _right_column_digits(track, x_min)
    if len(digits) != len(page):
        return None
    page_start_x = digits[0][1]
    page_right_x = page_right if page_right is not None else digits[-1][2]
    if page_right_x - page_start_x <= 0:
        return None
    if page_width and page_start_x <= 0.8 * page_width:
        return None
    return {
        "title": sm.group("title").rstrip(),
        "page_digits": page,
        "page_start_x": page_start_x,
        "page_right_x": page_right_x,
        "leader_orig": "",
        "score": 0.75,
        "mode": "full",
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
        try:
            adv = font_obj.char_width(ord(ch)) * size
        except Exception:
            adv = size * 0.5
        if adv <= 0 and not conv.skip_subset_fonts:
            adv = size * 0.5
        return adv
    return size * 0.5