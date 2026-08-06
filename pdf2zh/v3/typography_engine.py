"""Module: TypographyEngine — Phase 3 排版引擎基础（纯逻辑、无渲染）。

与 V4 ``typography.py``（GlyphProbe/AdaptiveTypography，面向宽度探测与
自适应字号）互补：本模块面向**模型驱动的排版预检** —— 度量取自
DocumentModel 里的字形 bbox（build_width_map），不依赖字体文件。

    build_width_map(block) → {char: width}
    measure(text, widths, default_adv) → float
    line_break(text, max_width, measure_fn) → [line, ...]
    justify_advances(text, line_width, measure_fn) → [advance, ...]
    widow_orphan_flag(line_count, is_paragraph) → bool

CJK 行逐字可断、拉丁按词；断行/对齐结果只标注，不修改模型文本。
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

# CJK 范围（断行策略：逐字可断）
_RE_CJK = re.compile(
    r"[\u2E80-\u2EFF\u3000-\u303F\u3040-\u30FF\u3400-\u4DBF"
    r"\u4E00-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF]")


def build_width_map(block) -> Dict[str, float]:
    """从块的 Span/Glyph bbox 建字符宽度表（同字符取最大宽度）。"""
    widths: Dict[str, float] = {}
    for line in getattr(block, "lines", []) or []:
        for span in getattr(line, "spans", []) or []:
            for g in getattr(span, "glyphs", []) or []:
                w = float(g.x1 - g.x0)
                if w > 0:
                    widths[g.char] = max(widths.get(g.char, 0.0), w)
    return widths


def measure(text: str, widths: Optional[Dict[str, float]] = None,
            default_adv: float = 5.0) -> float:
    """文本宽度：逐字符查宽度表，缺失用默认字宽。"""
    widths = widths or {}
    total = 0.0
    for ch in text or "":
        total += widths.get(ch, default_adv)
    return total


def _tokens(text: str) -> List[str]:
    """分词：CJK 字符逐字拆开（可断行），拉丁按空白成词。"""
    out: List[str] = []
    for part in re.findall(r"\S+|\s+", text or ""):
        if not part.strip():
            continue
        if _RE_CJK.search(part):
            for ch in part:
                out.append(ch)
        else:
            out.append(part)
    return out


def line_break(text: str, max_width: float,
               measure_fn: Callable[[str], float],
               max_lines: int = 200) -> List[str]:
    """贪心断行：行超宽且单 token 可放下时换行；单 token 超宽则逐字硬切。"""
    if not text:
        return []
    lines: List[str] = []
    cur = ""
    cur_w = 0.0

    def flush() -> None:
        nonlocal cur, cur_w
        if cur.strip():
            lines.append(cur)
        cur, cur_w = "", 0.0

    def hard_cut(token: str) -> None:
        nonlocal cur, cur_w
        for ch in token:
            cw = measure_fn(ch)
            if cur and cur_w + cw > max_width:
                flush()
            cur += ch
            cur_w += cw

    for token in _tokens(text):
        tw = measure_fn(token)
        if not cur:
            if tw <= max_width:
                cur, cur_w = token, tw
            else:
                hard_cut(token)
            continue
        if cur_w + tw <= max_width:
            cur += token
            cur_w += tw
            continue
        flush()
        if tw <= max_width:
            cur, cur_w = token, tw
        else:
            hard_cut(token)
        if len(lines) >= max_lines:
            return lines
    flush()
    return lines


def justify_advances(text: str, line_width: float,
                     measure_fn: Callable[[str], float]) -> List[float]:
    """行对齐：返回逐字符 advance（原宽 + 均分增量）。

    CJK 行在字符间均分；拉丁行在词间空格均分。无法分配时零扩展。
    """
    text = text or ""
    used = measure_fn(text)
    extra = max(0.0, line_width - used)
    n = len(text)
    if n <= 1 or extra <= 0:
        return [measure_fn(ch) for ch in text]
    if _RE_CJK.search(text):
        # 增量只加在前 n-1 个字符后（行尾不加），总增量恰为 extra
        per = extra / (n - 1)
        return [measure_fn(ch) + (per if i < n - 1 else 0.0)
                for i, ch in enumerate(text)]
    gaps = [i for i, ch in enumerate(text) if ch.isspace()]
    advances = [measure_fn(ch) for ch in text]
    if not gaps:
        per = extra / (n - 1)
        return [a + (per if i < n - 1 else 0.0)
                for i, a in enumerate(advances)]
    per = extra / len(gaps)
    for i in gaps:
        advances[i] += per
    return advances


def widow_orphan_flag(line_count: int, is_paragraph: bool = True) -> bool:
    """段落只有 1 行时标记（孤立段提示；页级判定需分页信息）。"""
    return is_paragraph and line_count == 1


__all__ = [
    "build_width_map", "measure", "line_break",
    "justify_advances", "widow_orphan_flag",
]