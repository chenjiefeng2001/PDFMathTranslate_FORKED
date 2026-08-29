"""List item candidate detection — plan Phase 3, detector stage.

Answers "is this paragraph a list item?" with **evidence fusion** rather
than a single marker rule:

    score = 3×marker_matches
          + 4×next_marker_is_sequential
          + 3×same_indent
          + 3×same_content_x
          + 1×same_line_height
          + 3×previous_is_list_item
          + 1×paragraph_width_is_consistent

``marker_matches`` alone is never sufficient either: a lone ordered marker
with no list context is treated as a numbered section title (``1.
Introduction``) and rejected. Markerless paragraphs are **never** candidates
— they are continuation lines and belong to the parser
(:mod:`pdf2zh.semantic.list_parser`), which attaches them when they align
with the item's ``content_x``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 判定阈值（融合计分；与 CodeDetector 的评分范式一致）。
LIST_THRESHOLD = 6.0

#: 判断“同缩进”的容差（同一列表项之间允许的几何偏移）。
INDENT_TOLERANCE = 3.0

#: 标记字符（bullet / 特殊项目符号；含 PDF 常见的私用区字形）。
_BULLET_CHARS = "•·◦▪▫‣–—*●○■□◆◇⁃∙‣" "\uf0b7\uf0a7\uf0d8\uf0a8\uf0b0"
_BULLET_CLASS = "".join(re.escape(c) for c in _BULLET_CHARS)

#: 标记正则（按优先级尝试；marker → marker_type → 正文内容）。
#: 同时接受 ``1.`` / ``1)`` / ``1、`` / ``(a)`` 等常见形态；分隔符后允许无空格
#: （中文列表 ``1、引言``）。marker 捕获**完整标记含分隔符**（如 ``1.`` / ``(a)``），
#: 渲染时原样放置，不参与翻译。``(?!\d)`` 拒绝 ``3.1 Method`` / ``1.5 × 10⁻³``
#: 这类编号小节/十进制数：分隔符后紧跟数字 → 不是列表标记。
_MARKER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("decimal", re.compile(r"^\s*(\(?\d{1,3}[\.\)、])(?!\d)\s*(\S.*)$")),
    ("lower_alpha", re.compile(r"^\s*(\(?[a-z]\)|[a-z][\.\)、])(?!\d)\s*(\S.*)$")),
    ("upper_alpha", re.compile(r"^\s*(\(?[A-Z]\)|[A-Z][\.\)、])(?!\d)\s*(\S.*)$")),
    ("lower_roman", re.compile(r"^\s*(\(?[ivxlcdm]{1,4}\)|[ivxlcdm]{1,4}[\.\)、])(?!\d)\s*(\S.*)$")),
    ("bullet", re.compile(rf"^\s*([{_BULLET_CLASS}])\s*(.*)$")),
]

#: 首行（潜在 marker 行）长度上限：更长的首行不可能是列表项。
_MAX_MARKER_LINE = 120

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _roman_to_int(s: str) -> int | None:
    s = s.lower()
    if not s or not re.fullmatch(r"[ivxlcdm]+", s):
        return None
    total = 0
    prev = 0
    for ch in reversed(s):
        v = _ROMAN[ch]
        total += -v if v < prev else v
        prev = v
    return total


@dataclass
class ListCandidate:
    """A paragraph suspected to be a list item."""

    marker: str
    marker_type: str
    content: str
    score: float
    reasons: list[str] = field(default_factory=list)
    level: int = 0
    indent: float = 0.0
    marker_x: float = 0.0
    marker_width: float = 0.0
    content_x: float = 0.0

    def to_dict(self) -> dict:
        return {
            "marker": self.marker,
            "marker_type": self.marker_type,
            "content": self.content,
            "score": round(self.score, 2),
            "reasons": list(self.reasons),
            "level": self.level,
            "indent": round(self.indent, 1),
            "marker_x": round(self.marker_x, 1),
            "content_x": round(self.content_x, 1),
        }


def match_marker(line: str) -> tuple[str, str, str] | None:
    """Extract ``(marker, marker_type, content)`` from a potential item line."""
    line = line or ""
    if not line.strip() or len(line) > _MAX_MARKER_LINE:
        return None
    for mtype, pat in _MARKER_PATTERNS:
        m = pat.match(line)
        if m is None:
            continue
        marker = m.group(1)
        content = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
        return marker, mtype, content
    return None


def _marker_value(marker: str) -> str:
    """Strip separators/parens from a marker ("1." → "1", "(a)" → "a")."""
    return re.sub(r"[^A-Za-z0-9]", "", marker or "")


def markers_are_sequential(
    prev_marker: str, prev_type: str, marker: str, mtype: str
) -> bool:
    """Whether two item markers form a running sequence at the same level."""
    if prev_type != mtype:
        return False
    if mtype == "bullet":
        return True  # bullet 列表无需序号，任意两个 bullet 视为连续
    prev_v = _marker_value(prev_marker)
    cur_v = _marker_value(marker)
    if mtype == "decimal":
        try:
            return int(cur_v) == int(prev_v) + 1
        except ValueError:
            return False
    if mtype in ("lower_alpha", "upper_alpha"):
        return (
            len(cur_v) == 1
            and len(prev_v) == 1
            and ord(cur_v) == ord(prev_v) + 1
        )
    if mtype == "lower_roman":
        a, b = _roman_to_int(prev_v), _roman_to_int(cur_v)
        return a is not None and b is not None and b == a + 1
    return False


@dataclass
class _Context:
    """Neighbor / geometry signals for the fusion score."""

    prev_candidate: bool = False
    prev_marker: str | None = None
    prev_type: str | None = None
    next_candidate: bool = False
    next_marker: str | None = None
    next_type: str | None = None
    same_indent: bool = False
    same_content_x: bool = False
    same_line_height: bool = False
    width_consistent: bool = False
    continuation_after: bool = False


def score_list_item(
    marker: str | None,
    marker_type: str | None,
    ctx: _Context,
) -> tuple[float, list[str]]:
    """Evidence-fusion score for one paragraph (weights per the plan)."""
    score = 0.0
    reasons: list[str] = []
    if marker:
        score += 3.0
        reasons.append("marker_matches")
    if (
        ctx.prev_candidate
        and marker
        and markers_are_sequential(ctx.prev_marker or "", ctx.prev_type or "",
                                   marker, marker_type or "")
    ):
        score += 4.0
        reasons.append("next_marker_sequential")
    if ctx.same_indent:
        score += 3.0
        reasons.append("same_indent")
    if ctx.same_content_x:
        score += 3.0
        reasons.append("same_content_x")
    if ctx.same_line_height:
        score += 1.0
        reasons.append("same_line_height")
    if ctx.prev_candidate:
        score += 3.0
        reasons.append("previous_is_list_item")
    if ctx.width_consistent:
        score += 1.0
        reasons.append("width_consistent")
    return score, reasons


def indent_of(paragraph: str, geom: dict | None = None) -> float:
    """Paragraph indent (marker column).

    With geometry: ``x0`` plus the advance width of leading whitespace
    (some extractors report leading spaces as characters at the same x0);
    without geometry: the leading-whitespace column count.
    """
    lead = re.match(r"^\s*", paragraph or "")
    ws = float(len(lead.group(0))) if lead else 0.0
    if geom and geom.get("x0") is not None:
        try:
            base = float(geom["x0"])
        except (TypeError, ValueError):
            return ws
        if ws and geom.get("size"):
            try:
                base += ws * float(geom["size"]) * 0.25  # 空格 ≈ 0.25em
            except (TypeError, ValueError):
                pass
        return base
    return ws


def _content_col(paragraph: str, marker: str | None, indent: float) -> float:
    """Approximate content start column (after the marker)."""
    if marker:
        return indent + len(marker)
    return indent


def _marker_width(
    paragraph: str,
    marker: str | None,
    indent: float,
    geom: list[dict] | None,
    i: int,
) -> float:
    """Estimate the marker advance width.

    With geometry we use the font size (≈ 0.5 em per character, Latin);
    without geometry the marker spans ``len(marker)+1`` whitespace columns.
    """
    if not marker:
        return 0.0
    if geom and i < len(geom) and geom[i] and geom[i].get("size"):
        try:
            return len(marker) * float(geom[i]["size"]) * 0.5
        except (TypeError, ValueError):
            pass
    return len(marker)


def detect_list_candidates(
    paragraphs: list[str],
    geom: list[dict] | None = None,
    threshold: float = LIST_THRESHOLD,
) -> list[ListCandidate | None]:
    """Run the detector over a page's paragraphs (in reading order).

    ``geom`` optionally provides per-paragraph ``{x0, x1, size}`` for
    geometry-based signals; without it, indent is derived from leading
    whitespace. Returns one entry per paragraph (None = not a candidate).
    """
    n = len(paragraphs)
    infos: list[tuple[str, str, str] | None] = [None] * n
    indents = [0.0] * n
    sizes: list[float | None] = [None] * n
    widths: list[float | None] = [None] * n
    for i, para in enumerate(paragraphs):
        infos[i] = match_marker(para)
        indents[i] = indent_of(para, (geom[i] if geom and i < len(geom) else None))
        g = geom[i] if geom and i < len(geom) else None
        if g:
            try:
                sizes[i] = float(g["size"]) if g.get("size") is not None else None
            except (TypeError, ValueError):
                sizes[i] = None
            try:
                if g.get("x0") is not None and g.get("x1") is not None:
                    widths[i] = max(float(g["x1"]) - float(g["x0"]), 0.0)
            except (TypeError, ValueError):
                widths[i] = None

    cands: list[ListCandidate | None] = [None] * n
    # 最近的前后候选索引（中间可能隔着延续行/普通段）：用于上下文信号。
    prev_idx = [-1] * n
    last = -1
    for i in range(n):
        prev_idx[i] = last
        if infos[i] is not None:
            last = i
    next_idx = [-1] * n
    nxt = -1
    for i in range(n - 1, -1, -1):
        next_idx[i] = nxt
        if infos[i] is not None:
            nxt = i

    for i in range(n):
        info = infos[i]
        marker = info[0] if info else None
        mtype = info[1] if info else None
        content = info[2] if info else None
        ctx = _Context()
        if prev_idx[i] >= 0:
            ctx.prev_candidate = True
            ctx.prev_marker, ctx.prev_type = infos[prev_idx[i]][0], infos[prev_idx[i]][1]
        if next_idx[i] >= 0:
            ctx.next_candidate = True
            ctx.next_marker, ctx.next_type = infos[next_idx[i]][0], infos[next_idx[i]][1]
        # 延续行证据：紧随的非空段缩进对齐到本项 content_x（单条目 + 延续行也是
        # 列表）；仍停在 marker 列的段（如后面的编号章节）不算延续。
        if marker is not None:
            cont_col = _content_col(paragraphs[i], marker, indents[i])
            for j in range(i + 1, min(n, i + 5)):
                if not paragraphs[j].strip():
                    continue
                if indents[j] >= cont_col - 0.5:
                    ctx.continuation_after = True
                break  # 只检查紧随的非空段
        if i > 0:
            ctx.same_indent = abs(indents[i] - indents[i - 1]) <= INDENT_TOLERANCE
            if (
                infos[i - 1] is not None
                and marker
                and abs(
                    _content_col(paragraphs[i], marker, indents[i])
                    - _content_col(paragraphs[i - 1], infos[i - 1][0], indents[i - 1])
                )
                <= 2.0
            ):
                ctx.same_content_x = True
            if sizes[i] is not None and sizes[i - 1] is not None:
                ctx.same_line_height = abs(sizes[i] - sizes[i - 1]) < 0.5
            if widths[i] is not None and widths[i - 1] is not None:
                ctx.width_consistent = (
                    abs(widths[i] - widths[i - 1]) <= max(4.0, 0.15 * (widths[i - 1] or 1.0))
                )
        # 单条有序 marker 且无任何上下文（前后无列表项、无几何证据）→ 疑似
        # 章节标题（"1. Introduction" / "2. Related Work"），不判为列表项。
        # bullet 豁免（单独一个 • 仍是列表项）。
        if marker is not None and mtype != "bullet":
            has_ctx = (
                ctx.prev_candidate
                or ctx.next_candidate
                or ctx.same_indent
                or ctx.same_content_x
                or ctx.same_line_height
                or ctx.width_consistent
                or ctx.continuation_after
            )
            if not has_ctx:
                marker = None
                mtype = None
        score, reasons = score_list_item(marker, mtype, ctx)
        # 候选必须带 marker；无 marker 的段落是延续行，交给 parser 处理。
        if marker is not None:
            marker_x = indents[i]
            marker_width = _marker_width(paragraphs[i], marker, indents[i], geom, i)
            cands[i] = ListCandidate(
                marker=marker,
                marker_type=mtype or "geo",
                content=(content or paragraphs[i].strip()),
                score=score,
                reasons=reasons,
                indent=indents[i],
                marker_x=marker_x,
                marker_width=marker_width,
                content_x=marker_x + marker_width,
            )
    return cands


def list_debug_dict(
    paragraphs: list[str],
    candidates: list[ListCandidate | None] | None = None,
    geom: list[dict] | None = None,
    tree=None,
) -> dict:
    """Debug-JSON friendly snapshot (Commit 2: detect only, no PDF change).

    Builds the candidate list (and the parsed tree, unless one is passed)
    so the snapshot is self-contained for ``--debug-list`` style reports.
    """
    cands = (
        candidates
        if candidates is not None
        else detect_list_candidates(paragraphs, geom)
    )
    if tree is None:
        # 延迟导入避免 detector ↔ parser 循环依赖
        from pdf2zh.semantic.list_parser import parse_list_tree

        tree = parse_list_tree(paragraphs, cands, geom)
    paras = [
        {
            "index": i,
            "text": p,
            "indent": round(indent_of(p, (geom[i] if geom and i < len(geom) else None)), 1),
        }
        for i, p in enumerate(paragraphs)
    ]
    return {
        "paragraphs": paras,
        "candidates": [c.to_dict() if c else None for c in cands],
        "tree": tree.to_dict() if tree is not None else None,
    }


__all__ = [
    "LIST_THRESHOLD",
    "ListCandidate",
    "match_marker",
    "markers_are_sequential",
    "score_list_item",
    "detect_list_candidates",
    "list_debug_dict",
    "indent_of",
]