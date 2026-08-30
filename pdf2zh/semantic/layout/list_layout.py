"""List layout contract — Commit 7E-2a, extended by 7F-6c-1.

Bridges a semantic :class:`~pdf2zh.semantic.models.ListItemNode` onto the
unified layout pipeline so the renderer consumes settled ``LayoutResult``
shapes instead of re-wrapping at draw time::

    ListItemNode
        ↓  layout_list_item
    FixedAnchor (marker)  +  FlowText (content)  +  FlowText (continuation)
        ↓  adaptive_layout(content/continuation) + lay_out(marker)
    ListLayoutResult
        ↓  ListRenderer (draw only)
    PDF commands

Channel semantics (marker is FIXED, content is FLOW, continuation keeps x):

- **marker** → :class:`FixedAnchor` — ``1.`` / ``(a)`` / ``•`` never wrap,
  never shrink, never clip, never enter the translator; verbatim single
  line (a raw ``lay_out`` call — adaptive recovery never touches it).
- **content** → :class:`FlowText` — runs the shared adaptive executor with
  the ``list_content`` budget (7F-6c-1: WRAP → SHRINK → CLIP, clamped by
  ``LayoutBudget``); ``content_x`` never moves.
- **continuation** → :class:`FlowText` pinned to ``content_x`` — same
  ``list_content`` budget; ``continuation_x == content_x`` always.

Geometry invariant (architecture): ``marker_x`` / ``content_x`` /
``continuation_x`` and the first-line baseline ``y`` are **copied verbatim**
from the semantic node.  Nothing here derives them from ``level``, item
``index`` or list numbering.  The only value synthesized is the vertical
step between wrapped lines (``line_step``), which is a placement constant,
not a fit decision.

Fit decisions (wrap / shrink / clip / overflow) are delegated to the unified
executor (:func:`pdf2zh.semantic.layout.adaptive.adaptive_layout`) and
:func:`~pdf2zh.semantic.layout.overflow.lay_out` — this module never calls
``wrap_lines`` / ``shrink_to_fit`` / ``clip_text`` directly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.overflow import LayoutResult, lay_out
from pdf2zh.semantic.layout.primitives import FixedAnchor, FlowText
from pdf2zh.semantic.layout.recovery import budget_for_kind

__all__ = ["ListLayoutResult", "layout_list_item", "layout_list_node"]

_DEFAULT_FONT_SIZE = 11.0
_DEFAULT_LINE_HEIGHT = 1.4


@dataclass
class ListLayoutResult:
    """One item's settled layout: marker + content + continuation results.

    All horizontal geometry is passthrough from the semantic node —
    ``marker_x`` / ``content_x`` / ``continuation_x`` are never recomputed.
    ``y`` is the first-line baseline (v3 y-up); wrapped / continuation lines
    step by ``line_step`` (negative in y-up spaces so lines flow downward).
    """

    marker: LayoutResult = field(default_factory=lambda: LayoutResult(text=""))
    content: LayoutResult = field(default_factory=lambda: LayoutResult(text=""))
    continuation: list[LayoutResult] = field(default_factory=list)
    marker_x: float = 0.0
    content_x: float = 0.0
    continuation_x: float = 0.0
    y: float = 0.0
    font_size: float = _DEFAULT_FONT_SIZE
    line_step: float = 0.0

    def to_dict(self) -> dict:
        return {
            "marker": self.marker.to_dict(),
            "content": self.content.to_dict(),
            "continuation": [c.to_dict() for c in self.continuation],
            "marker_x": round(self.marker_x, 2),
            "content_x": round(self.content_x, 2),
            "continuation_x": round(self.continuation_x, 2),
            "y": round(self.y, 2),
            "font_size": round(self.font_size, 1),
            "line_step": round(self.line_step, 2),
        }


def _node_text(item, override: str | None, default: str) -> str:
    if override is not None:
        return override
    return default or ""


def layout_list_item(
    item,
    *,
    measure: Callable[[str, float], float] | None = None,
    font_size: float = _DEFAULT_FONT_SIZE,
    available_width: float | None = None,
    available_height: float | None = None,
    line_height: float = _DEFAULT_LINE_HEIGHT,
    line_step: float | None = None,
    content_text: str | None = None,
    continuation_texts: Sequence[str] | None = None,
) -> ListLayoutResult:
    """Lay out one :class:`ListItemNode` → :class:`ListLayoutResult`.

    Args:
        item: a semantic :class:`~pdf2zh.semantic.models.ListItemNode` with
            verbatim geometry (``marker_x`` / ``content_x`` / ``content_width``
            / ``y``).
        measure: ``(text, font_size) -> width``; defaults to the layout layer's
            CJK-aware estimate (via ``lay_out``).
        font_size: nominal font size in points.
        available_width: explicit content wrap width; defaults to the item's
            ``content_width`` (``<= 0`` means no wrap → single line).
        available_height: optional vertical constraint (``None``/``0`` = none).
        line_height: vertical-step multiple used only when ``line_step`` is
            not given.
        line_step: explicit signed vertical step between wrapped lines
            (negative for y-up spaces).  Defaults to ``-(font_size *
            line_height)``.
        content_text: **translated** content to lay out (defaults to
            ``item.content``).  The caller (renderer) is responsible for
            translation — this adapter never touches a translator.
        continuation_texts: **translated** continuation lines (defaults to
            ``item.continuation``).

    Returns:
        :class:`ListLayoutResult`.  Never raises: any layout failure degrades
        to an overflow-flagged single-line result (via ``lay_out``'s safety
        net), never silent.
    """
    fs = float(font_size or _DEFAULT_FONT_SIZE)
    step = (
        float(line_step)
        if line_step is not None
        else -(fs * float(line_height or _DEFAULT_LINE_HEIGHT))
    )
    marker_x = float(getattr(item, "marker_x", 0.0) or 0.0)
    content_x = float(getattr(item, "content_x", 0.0) or 0.0)
    y = float(getattr(item, "y", 0.0) or 0.0)
    avail_w = (
        float(available_width)
        if available_width is not None and float(available_width) > 0
        else (float(getattr(item, "content_width", 0.0) or 0.0))
    )
    avail_h = (
        float(available_height)
        if available_height is not None and float(available_height) > 0
        else 0.0
    )

    # ── marker：FixedAnchor —— 永不 wrap / 默认不 shrink / 原样单行 ──
    marker = lay_out(
        FixedAnchor(
            text=getattr(item, "marker", "") or "",
            x=marker_x,
            y=y,
            max_width=float(getattr(item, "marker_width", 0.0) or 0.0),
            role="marker_x",
        ),
        measure=measure,
        font_size=fs,
    )

    # ── content：FlowText —— 7F-6c-1：统一 executor + list_content budget
    #    （WRAP → SHRINK → CLIP）；content_x 永不动 ────────────────────────
    content = adaptive_layout(
        FlowText(
            text=_node_text(item, content_text, getattr(item, "content", "")),
            origin=(content_x, y),
            max_width=avail_w,
            max_height=avail_h,
            line_height=fs * (line_height or _DEFAULT_LINE_HEIGHT),
        ),
        measure=measure,
        avail_width=avail_w,
        avail_height=avail_h,
        font_size=fs,
        budget=budget_for_kind("list_content"),
    )

    # ── continuation：FlowText 钉在 content_x（x 固定；同一 list_content
    #    budget，7F-6c-1）──────────────────────────────────────────────────
    continuations = list(
        continuation_texts
        if continuation_texts is not None
        else (getattr(item, "continuation", None) or [])
    )
    cont_results: list[LayoutResult] = []
    for ct in continuations:
        cont_results.append(
            adaptive_layout(
                FlowText(
                    text=ct or "",
                    origin=(content_x, y),
                    max_width=avail_w,
                    max_height=avail_h,
                    line_height=fs * (line_height or _DEFAULT_LINE_HEIGHT),
                ),
                measure=measure,
                avail_width=avail_w,
                avail_height=avail_h,
                font_size=fs,
                budget=budget_for_kind("list_content"),
            )
        )

    return ListLayoutResult(
        marker=marker,
        content=content,
        continuation=cont_results,
        marker_x=marker_x,
        content_x=content_x,
        continuation_x=content_x,
        y=y,
        font_size=fs,
        line_step=step,
    )


def layout_list_node(
    node,
    *,
    measure: Callable[[str, float], float] | None = None,
    font_size: float = _DEFAULT_FONT_SIZE,
    available_width: float | None = None,
    available_height: float | None = None,
    line_height: float = _DEFAULT_LINE_HEIGHT,
    line_step: float | None = None,
    content_texts: dict | None = None,
    continuation_texts: dict | None = None,
) -> list[ListLayoutResult]:
    """Lay out a whole :class:`ListNode` tree → flat per-item results.

    ``content_texts`` / ``continuation_texts`` map ``id(item) -> translated
    text`` so the caller can translate once and reuse; missing keys fall back
    to the item's original text.  Nesting order is depth-first reading order.
    """
    out: list[ListLayoutResult] = []

    def _walk(lnode) -> None:
        for item in lnode.items:
            out.append(
                layout_list_item(
                    item,
                    measure=measure,
                    font_size=font_size,
                    available_width=available_width,
                    available_height=available_height,
                    line_height=line_height,
                    line_step=line_step,
                    content_text=(content_texts or {}).get(id(item)),
                    continuation_texts=(continuation_texts or {}).get(id(item)),
                )
            )
            for child in item.children:
                _walk(child)

    _walk(node)
    return out
