"""Visual TOC parser — plan Commit 6A, parser stage.

Consumes the detector's anchors and builds the semantic AST:

    PDF lines (with geometry)
        → toc_detector.detect_anchors  (entry anchors)
        → grouping                      (multi-line entries: dangling title
                                          fragments attach to the following anchor)
        → level inference               (indentation clusters, number-depth)
        → TOCEntryNode / TOCNode

Psychology mirrors :mod:`pdf2zh.semantic.list_parser`: geometry (``title_x``,
``indent``, ``page_x``) is **copied from the original PDF**, never recomputed
from the index, and the printed ``page_number`` is carried as a preserve-only
label — never merged into ``title``.

Level inference intentionally does **not** trust ``1 / 1.1 / 1.1.1`` alone:
levels come primarily from indentation clusters (quantized ``title_x``), and
number-depth is only a *secondary* tie-break when indentation is uniform.

Pure logic, no I/O, no PyMuPDF, no converter.
"""

from __future__ import annotations

import re

from pdf2zh.semantic.models import TOCEntryNode, TOCNode
from pdf2zh.semantic.toc_detector import (
    EntryMatch,
    detect_anchors,
    detect_header,
    is_title_prefix,
    match_entry,
    page_is_toc,
)

__all__ = ["parse_toc", "TOC_LEVEL_TOLERANCE"]

#: Leading-number matcher for depth (secondary level signal).
_NUMBER_HEAD = re.compile(r"^\s*(\d+(?:\.\d+)*)")

#: Quantization tolerance for indent clusters (title_x grouping).
TOC_LEVEL_TOLERANCE = 6.0


def _title_x_of(line: dict) -> float:
    try:
        return float(line.get("x0") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _page_x_of(line: dict) -> float:
    try:
        return float(line.get("x1") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _make_entry(
    line: dict,
    match: EntryMatch,
    prefix_title: str,
    continuation: list[str],
) -> TOCEntryNode:
    """Build a TOCEntryNode from a matched anchor + any absorbed title fragment."""
    base = match.title.strip()
    full_title = (prefix_title + " " + base).strip() if prefix_title else base
    return TOCEntryNode(
        title=full_title,
        title_x=_title_x_of(line),
        page_x=match.page_x,
        indent=_title_x_of(line),
        page_number=match.page,
        destination_page=None,
        dot_leader=match.leader,
        leader_present=match.leader_present,
        continuation=[ln.strip() for ln in continuation if ln.strip()],
    )


def _infer_levels(entries: list[TOCEntryNode]) -> None:
    """Assign ``level`` per entry using indentation clusters + number depth.

    1. Quantize ``title_x`` into clusters (tolerance :const:`TOC_LEVEL_TOLERANCE`).
    2. If multiple clusters → level = cluster rank (0, 1, 2… by increasing x).
       This is the primary signal (indentation), as the plan requires.
    3. If uniform indentation (one cluster) but any title carries a dotted
       number → level = dotted-number depth (secondary; never trusted alone).
    4. Otherwise all level 0.
    """
    if not entries:
        return
    xs = sorted(round(e.title_x, 2) for e in entries)
    clusters: list[float] = []
    for x in xs:
        if not clusters or x - clusters[-1] > TOC_LEVEL_TOLERANCE:
            clusters.append(x)
        else:
            clusters[-1] = (clusters[-1] + x) / 2.0

    def _rank(x: float) -> int:
        best = clusters[0]
        for c in clusters:
            if abs(x - c) < abs(x - best):
                best = c
        return clusters.index(best)

    has_dotted = any(
        (m := _NUMBER_HEAD.match(e.title)) is not None and "." in m.group(1)
        for e in entries
    )

    uniform = len(clusters) == 1
    for e in entries:
        if not uniform:
            e.level = _rank(e.title_x)
        elif has_dotted:
            m = _NUMBER_HEAD.match(e.title)
            e.level = m.group(1).count(".") if m else 0
        else:
            e.level = 0


def parse_toc(
    lines: list[dict],
    page_width: float,
) -> TOCNode | None:
    """Parse a page's lines into a :class:`TOCNode` when it is a TOC page.

    Args:
        lines: page lines with ``{text, x0, y0, x1, y1, size}`` geometry.
        page_width: page width (pt) for right-column page-number gating.

    Returns:
        :class:`TOCNode` with ordered entries, or ``None`` when the page is not
        a TOC page (no header + < 2 anchors), so normal/negative pages yield no
        TOC without touching anything downstream.
    """
    if not lines:
        return None
    anchors = detect_anchors(lines, page_width)
    if not anchors:
        return None
    header = detect_header(lines)
    if not page_is_toc(anchors, header is not None):
        return None

    anchor_set = set(anchors)
    entries: list[TOCEntryNode] = []
    buffered: list[str] = []  # dangling title-fragment lines before next anchor

    def _absorb() -> str:
        # Contiguous trailing title-fragment lines (numbered continuation) → prefix.
        full = []
        for frag in reversed(buffered):
            if is_title_prefix(frag):
                full.append(frag.strip())
            else:
                break
        buffered.clear()
        return " ".join(reversed(full))

    for i, ln in enumerate(lines):
        m = match_entry(ln, page_width) if i in anchor_set else None
        if m is not None:
            prefix = _absorb()
            entries.append(_make_entry(ln, m, prefix, buffered))
            buffered.clear()
        else:
            buffered.append(ln.get("text") or "")

    if not entries:
        return None
    _infer_levels(entries)
    return TOCNode(
        entries=entries,
        is_toc_page=True,
        has_header=header is not None,
        header_text=header,
    )
