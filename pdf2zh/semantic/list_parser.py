"""Nested list parser — plan Phase 3, parser stage.

Turns the detector's flat per-paragraph candidates into the structural
tree from the plan:

    List(level=0)
     ├── Item 1
     │   └── List(level=1)
     │       ├── Item a
     │       └── Item b
     └── Item 2
         └── List(level=1)
             └── Item a

Rules:
- items at the same indent level (within :data:`LEVEL_TOLERANCE`) form one
  :class:`ListNode`; marker *sequence* is not required for grouping (the
  detector already rewards it), so decimal / alpha / bullet lists all group;
- a candidate with a *deeper* indent than the current item becomes a nested
  :class:`ListNode` under that item;
- a paragraph **without** a candidate is a **continuation line** of the
  current item only when it aligns with the item's ``content_x`` (deep-enough
  indent) — a paragraph that starts back at the marker column (e.g. a normal
  paragraph following the list) is left alone and closes the item context;
- geometry is **copied from the original PDF** (``marker_x`` / ``content_x``
  / ``y`` / bbox); the renderer never recomputes levels into indents.
"""

from __future__ import annotations

from pdf2zh.semantic.list_detector import (
    ListCandidate,
    detect_list_candidates,
    indent_of,
)
from pdf2zh.semantic.models import ListItemNode, ListNode

#: 判定“同一层级”的缩进容差（points 或前导空格数）。
LEVEL_TOLERANCE = 1.5

__all__ = ["parse_list_tree"]


def parse_list_tree(
    paragraphs: list[str],
    candidates: list[ListCandidate | None] | None = None,
    geom: list[dict] | None = None,
) -> ListNode | None:
    """Build a :class:`ListNode` tree from page paragraphs in reading order.

    Args:
        paragraphs: page paragraphs (same order as the detector saw them).
        candidates: precomputed detector output (optional; recomputed when
            None).
        geom: optional per-paragraph ``{x0, x1, y0, size}`` geometry.

    Returns:
        root :class:`ListNode` (``items`` empty ⇒ ``None``).
    """
    cands = (
        candidates
        if candidates is not None
        else detect_list_candidates(paragraphs, geom)
    )
    if not cands or not any(c is not None for c in cands):
        return None

    root = ListNode(level=0)
    #: 栈：[ListNode]，root 永远在栈底；同时记录每层对应的缩进。
    stack: list[ListNode] = [root]
    stack_indent: list[float | None] = [None]
    cur_item: ListItemNode | None = None

    for i, para in enumerate(paragraphs):
        cand = cands[i] if i < len(cands) else None
        indent = cand.indent if cand is not None else indent_of(para, (geom[i] if geom and i < len(geom) else None))

        if cand is not None:
            # 弹出缩进更浅的层（同一层或更浅 → 回到对应层级）
            while (
                len(stack_indent) > 1
                and stack_indent[-1] is not None
                and indent < stack_indent[-1] - LEVEL_TOLERANCE
            ):
                stack.pop()
                stack_indent.pop()
            top = stack_indent[-1]
            # 根层（top None）用上一个 item 的缩进作基线；否则用当前层缩进。
            base = top if top is not None else (cur_item.indent if cur_item else 0.0)
            if top is not None and abs(indent - top) <= LEVEL_TOLERANCE:
                pass  # 同层：继续在当前 ListNode 上
            elif cur_item is not None and indent > base + LEVEL_TOLERANCE:
                # 更深缩进 → 嵌套列表挂到上一个 item 之下
                child = ListNode(level=len(stack_indent))
                cur_item.children.append(child)
                stack.append(child)
                stack_indent.append(indent)
            # else: 同层或根层 → 直接加到当前 ListNode

            g = geom[i] if geom and i < len(geom) else None
            item = ListItemNode(
                marker=cand.marker,
                marker_type=cand.marker_type,
                content=cand.content,
                level=len(stack_indent) - 1,
                indent=cand.indent,
                marker_x=cand.marker_x,
                marker_width=cand.marker_width,
                content_x=cand.content_x,
                y=_y_of(g),
                content_width=_width_of(g, cand.content_x),
            )
            item.bbox = _bbox_of(g, cand.content_x)
            stack[-1].items.append(item)
            cur_item = item
        else:
            # 延续行：无 marker 且与当前 item 的 content_x 对齐（缩进足够深）
            if cur_item is not None and para.strip():
                if indent >= cur_item.content_x - LEVEL_TOLERANCE:
                    cur_item.continuation.append(para.strip())
                else:
                    # 回到 marker 列 → 普通段落（列表已结束），关闭 item 上下文
                    cur_item = None

    return root if root.items else None


def _y_of(g: dict | None) -> float:
    if g:
        for key in ("y0", "y"):
            if g.get(key) is not None:
                try:
                    return float(g[key])
                except (TypeError, ValueError):
                    pass
    return 0.0


def _width_of(g: dict | None, content_x: float) -> float:
    if g and g.get("x1") is not None:
        try:
            return max(float(g["x1"]) - content_x, 0.0)
        except (TypeError, ValueError):
            pass
    return 0.0


def _bbox_of(g: dict | None, content_x: float) -> tuple[float, float, float, float]:
    if not g:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        x0 = float(g.get("x0") or 0.0)
        x1 = float(g.get("x1") or x0)
        y0 = float(g.get("y0") or 0.0)
        y1 = float(g.get("y1") or y0)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (min(x0, content_x), y0, max(x1, content_x), y1)