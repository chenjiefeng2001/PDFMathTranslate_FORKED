"""Map existing render payloads onto layout primitives — Commit 7B.

The commit does **not** require rewriting the renderers; it only needs to prove
every existing payload shape maps cleanly onto the new primitives, so the
renderer side can migrate incrementally without behavior change::

    Paragraph       → FlowText
    List            → FixedAnchor(content_x) → Continuation
    TOC title       → FixedAnchor(title_x)
    TOC page        → FixedColumn(page_x)
    Code            → PreservedRegion

The factories below are thin, geometry-pass-through shims: they take the
already-parsed original geometry and hand it back verbatim.  None of them
recompute geometry from ``level`` / ``index`` / page width.
"""

from __future__ import annotations

from pdf2zh.semantic.layout.primitives import (
    Continuation,
    FixedAnchor,
    FixedColumn,
    FlowText,
    PreservedRegion,
)

__all__ = [
    "flow_text",
    "list_anchor",
    "list_continuation",
    "toc_title_anchor",
    "toc_page_column",
    "preserved_region",
]


def flow_text(
    text: str,
    origin: tuple[float, float] = (0.0, 0.0),
    max_width: float = 0.0,
    max_height: float = 0.0,
    line_height: float = 0.0,
) -> FlowText:
    """Paragraph -> :class:`FlowText` (plain flow, width/height allow wrapping)."""
    return FlowText(
        text=text,
        origin=origin,
        max_width=max_width,
        max_height=max_height,
        line_height=line_height,
    )


def list_anchor(
    text: str,
    x: float,
    y: float,
    max_width: float = 0.0,
) -> FixedAnchor:
    """List content -> :class:`FixedAnchor` pinned at original ``content_x``."""
    return FixedAnchor(text=text, x=x, y=y, max_width=max_width, role="content_x")


def list_continuation(
    text: str,
    x: float,
    y: float,
    parent: FixedAnchor | None = None,
) -> Continuation:
    """A wrapped/follow-on list line -> :class:`Continuation` at ``content_x``."""
    return Continuation(
        text=text,
        continuation_x=x,
        continuation_y=y,
        parent_anchor=parent,
    )


def toc_title_anchor(
    text: str,
    x: float,
    y: float,
    max_width: float = 0.0,
) -> FixedAnchor:
    """TOC title -> :class:`FixedAnchor` pinned at original ``title_x``."""
    return FixedAnchor(text=text, x=x, y=y, max_width=max_width, role="title_x")


def toc_page_column(
    text: str,
    column_x: float,
    y: float,
) -> FixedColumn:
    """TOC page number -> :class:`FixedColumn` at original ``page_x``.

    ``column_x`` is the original page-number column and is deliberately not
    derived from the title's rendered width — a longer translated title must
    not move the column.
    """
    return FixedColumn(text=text, column_x=column_x, y=y)


def preserved_region(
    text: str,
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
) -> PreservedRegion:
    """Code / other preserve regions -> :class:`PreservedRegion` (bbox verbatim)."""
    return PreservedRegion(text=text, bbox=bbox)
