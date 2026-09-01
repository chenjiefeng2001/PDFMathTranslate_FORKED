"""TOC layout contract — Commit 7E-3a, extended by 7F-5b / 7F-6c-2.

Bridges a structured TOC entry (``toc_sidechannel`` schema dict — the same
shape produced by ``TOCEntryNode.to_dict``) onto the unified layout pipeline
so the renderer consumes settled ``LayoutResult`` shapes instead of computing
per-run geometry itself::

    TOCEntryNode / entry dict
        ↓  layout_toc_entry
    FixedAnchor (title, number)
    FixedColumn (page)
    + flexible leader region
        ↓  adaptive_layout(title)   (7F-6c: THE single bounded executor)
        ↓  lay_out(number/leader/page/continuation)
    TocEntryLayoutResult
        ↓  toc_layout_commands  →  existing TocRenderer (draw only)
    PDF commands

Channel semantics:

- **number** → :class:`FixedAnchor` at ``title_x`` — PRESERVE, verbatim,
  never translated / renumbered.
- **title** → :class:`FixedAnchor` at ``title_x`` (+number width + gap) —
  the only translatable part; width measured by the unified measurer.
  7F-5b: the title is laid out with WRAP and may occupy **multiple lines**
  when the translation grows.  The extra-line budget
  (``original_lines + max_extra_lines``) bounds the growth: WRAP first, then
  SHRINK if the budget is exceeded, then explicit PRESERVE_OVERFLOW — never
  CLIP, never infinite height growth (7F-5a contract).  Since 7F-6c-2 the
  title runs through :func:`pdf2zh.semantic.layout.adaptive.adaptive_layout`
  with ``target="title"`` — the same executor Flow / List use (the 7F-5a
  ladder lives in the executor, not in a second hand-rolled loop).
- **leader** → flexible dot region filling from the translated title's
  right edge to the original ``page_x``; when the title grows, the leader
  shrinks and ``page_x`` never moves; when ``leader_present`` is False no
  dots are ever forced.
- **page** → :class:`FixedColumn` at ``page_x`` — PRESERVE, verbatim,
  never moved by title growth.  The page number is an **entry-level**
  command: it is emitted exactly once, never once per wrapped line.
- **continuation** → :class:`FixedAnchor` at ``continuation_x`` (verbatim
  from the entry when present, else the established ``title_x + size``
  offset), stepping down (v3 y-up) by ``line_height`` per follow-on line.

Geometry invariant (architecture): ``title_x`` / ``page_x`` / ``indent`` /
``bbox`` / ``continuation_x`` come **verbatim** from the entry; nothing here
derives them from ``level``, entry ``index`` or ``level * constant``.  The
only synthesized values are the vertical step between continuation lines and
the multi-line title's y offsets (placement constants).  All fit / wrap /
overflow decisions go through the unified executor / ``lay_out`` — this
module never calls ``wrap_lines`` / ``shrink_to_fit`` / ``clip_text``
directly.

Overflow (translated title that cannot fit even after WRAP→SHRINK): the
result keeps ``overflow=True`` and the leader is not emitted (the page number
still stays at ``page_x``) — never silent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Optional

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.overflow import (
    LayoutResult,
    lay_out,
)
from pdf2zh.semantic.layout.primitives import FixedAnchor, FixedColumn
from pdf2zh.semantic.layout.recovery import LayoutBudget, budget_for_kind

__all__ = [
    "TocEntryLayoutResult",
    "layout_toc_entry",
    "toc_layout_commands",
]

_DEFAULT_SIZE = 10.0
_DEFAULT_LEADER_GAP = 4.0
_DEFAULT_LINE_HEIGHT = 14.0


@dataclass
class TocEntryLayoutResult:
    """One entry's settled layout: number/title/leader/page + continuation.

    Horizontal geometry (``title_x`` / ``page_x``) is passthrough from the
    entry; ``y`` is the first-line baseline (v3 y-up).  ``title_end`` is the
    translated title's right edge (drives the leader); ``overflow`` is True
    when the title (+number) still cannot fit after WRAP→SHRINK.

    7F-5b height semantics: ``line_count`` is the entry's total visual lines
    (wrapped title lines + continuation lines), ``total_height`` is
    ``line_count * line_height``.  ``continuation_x`` is verbatim from the
    entry (or the established ``title_x + size`` fallback) — never derived
    from ``level``.  ``recovery`` is a JSON-safe record of the WRAP→SHRINK→
    PRESERVE decision for the title.
    """

    number: LayoutResult | None = None
    title: LayoutResult | None = None
    leader: LayoutResult | None = None
    page: LayoutResult | None = None
    continuation: list[LayoutResult] = field(default_factory=list)
    title_x: float = 0.0
    page_x: float = 0.0
    y: float = 0.0
    size: float = _DEFAULT_SIZE
    leader_gap: float = _DEFAULT_LEADER_GAP
    title_end: float = 0.0
    overflow: bool = False
    level: int = 0
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # ── 7F-5b height semantics ──────────────────────────────────────────
    line_count: int = 0
    line_height: float = _DEFAULT_LINE_HEIGHT
    total_height: float = 0.0
    continuation_x: float = 0.0
    original_lines: int = 1
    max_extra_lines: int = 2
    recovery: dict | None = None

    def to_dict(self) -> dict:
        return {
            "number": self.number.to_dict() if self.number else None,
            "title": self.title.to_dict() if self.title else None,
            "leader": self.leader.to_dict() if self.leader else None,
            "page": self.page.to_dict() if self.page else None,
            "continuation": [c.to_dict() for c in self.continuation],
            "title_x": round(self.title_x, 2),
            "page_x": round(self.page_x, 2),
            "y": round(self.y, 2),
            "size": round(self.size, 1),
            "title_end": round(self.title_end, 2),
            "overflow": bool(self.overflow),
            "level": self.level,
            "line_count": int(self.line_count),
            "line_height": round(self.line_height, 2),
            "total_height": round(self.total_height, 2),
            "continuation_x": round(self.continuation_x, 2),
            "original_lines": int(self.original_lines),
            "max_extra_lines": int(self.max_extra_lines),
            "recovery": self.recovery,
        }


def _entry_value(entry, key: str, default):
    """Read from a dict or a node-like object (duck-typed passthrough)."""
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def layout_toc_entry(
    entry,
    *,
    measure: Callable[[str, float], float] | None = None,
    size: float = _DEFAULT_SIZE,
    leader_gap: float = _DEFAULT_LEADER_GAP,
    line_height: float = _DEFAULT_LINE_HEIGHT,
    y: float = 0.0,
    translated_title: str | None = None,
    translated_continuation: Sequence[str] | None = None,
    budget: LayoutBudget | None = None,
) -> TocEntryLayoutResult:
    """Lay out one TOC entry → :class:`TocEntryLayoutResult`.

    Args:
        entry: structured entry (``toc_sidechannel`` schema dict or a
            node with the same attribute names).  ``title_x`` / ``page_x`` /
            ``indent`` / ``bbox`` / ``level`` / ``continuation_x`` are read
            verbatim.
        measure: ``(text, font_size) -> width``; defaults to the layout
            layer's unified measurer (via ``lay_out``).
        size: nominal font size in points.
        leader_gap: minimum gap between the title end and the leader / page
            column (same constant as the golden renderer).
        line_height: vertical step between wrapped / continuation lines
            (positive; applied downward in v3 y-up).
        y: first-line baseline (v3 y-up, supplied by the host).
        translated_title: **pre-translated** title text (the caller owns
            translation; this adapter never touches a translator).  ``None``
            when the entry has no title.
        translated_continuation: pre-translated continuation lines.
        budget: title recovery budget (defaults to ``budget_for_kind(
            \"toc_title\")`` — WRAP→SHRINK→PRESERVE, never CLIP).

    Returns:
        :class:`TocEntryLayoutResult`.  Never raises: measurement / layout
        failure degrades via ``lay_out``'s safety net, never silent.
    """
    title_x = float(_entry_value(entry, "title_x", 0.0) or 0.0)
    page_x = float(_entry_value(entry, "page_x", 0.0) or 0.0)
    level = int(_entry_value(entry, "level", 0) or 0)
    bbox = tuple(
        _entry_value(entry, "bbox", (0.0, 0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0, 0.0)
    )
    sz = float(size or _DEFAULT_SIZE)
    gap = float(leader_gap or _DEFAULT_LEADER_GAP)
    lh = float(line_height or _DEFAULT_LINE_HEIGHT)
    b = budget or budget_for_kind("toc_title")

    number = str(_entry_value(entry, "number", "") or "").strip()
    page_text = str(_entry_value(entry, "page_number", "") or "").strip()
    leader_present = bool(_entry_value(entry, "leader_present", False))
    continuations = list(
        translated_continuation
        if translated_continuation is not None
        else (_entry_value(entry, "continuation", None) or [])
    )
    # continuation_x verbatim from the entry; fallback = established offset.
    cont_x = float(_entry_value(entry, "continuation_x", 0.0) or 0.0)
    if cont_x <= 0.0:
        cont_x = title_x + sz * 1.0

    def _lay(prim) -> LayoutResult:
        return lay_out(prim, measure=measure, font_size=sz)

    # ── Channel 1: number —— FixedAnchor at title_x (PRESERVE) ─────────
    num_result: LayoutResult | None = None
    cursor = title_x
    if number:
        num_result = _lay(
            FixedAnchor(text=number, x=title_x, y=y, max_width=0.0, role="title_x")
        )
        cursor = title_x + num_result.line_widths[0] + gap

    # ── Channel 2: title —— adaptive_layout(target="title")：WRAP→SHRINK→
    #    PRESERVE_OVERFLOW（7F-5a），同一个 executor，绝不 CLIP ────────────
    title_result: LayoutResult | None = None
    title_recovery: dict | None = None
    title_lines = 0
    title_end = cursor
    overflow = False
    if translated_title is not None:
        # 可用宽度 = 到 page_x 为止（减去 leader 间隙）
        avail = max(0.0, page_x - cursor - gap)
        max_extra = int(b.max_extra_lines or 0)
        allowed_lines = 1 + max_extra  # title's own original is 1 line
        anchor = FixedAnchor(
            text=translated_title, x=cursor, y=y, max_width=avail, role="title_x"
        )
        title_result = adaptive_layout(
            anchor,
            measure=measure,
            avail_width=avail,
            avail_height=0.0,
            font_size=sz,
            budget=b,
            target="title",
        )
        title_lines = len(title_result.lines)
        title_end = cursor + (
            title_result.line_widths[0] if title_result.line_widths else 0.0
        )
        overflow = bool(title_result.overflow) or title_lines > allowed_lines
        title_recovery = title_result.recovery

    # ── Channel 3: leader —— flexible dots to original page_x ──────────
    leader_result: LayoutResult | None = None
    if leader_present and not overflow and page_x > title_end + gap:
        available = page_x - title_end
        unit = lay_out(
            FixedAnchor(text=".", x=title_end, y=y, max_width=0.0),
            measure=measure,
            font_size=sz,
        ).line_widths[0]
        unit = float(unit or 1.0)
        n = max(1, int((available - gap) // unit))
        leader_result = _lay(
            FixedAnchor(text="." * n, x=title_end, y=y, max_width=0.0, role="leader")
        )

    # ── Channel 4: page —— FixedColumn at page_x (PRESERVE, once) ───────
    page_result: LayoutResult | None = None
    if page_text:
        page_result = _lay(FixedColumn(text=page_text, column_x=page_x, y=y))

    # ── Continuation lines: FixedAnchor at continuation_x, stepping down ─
    cont_results: list[LayoutResult] = []
    for k, cont in enumerate(continuations):
        if not (cont or "").strip():
            continue
        cont_results.append(
            _lay(
                FixedAnchor(
                    text=str(cont),
                    x=cont_x,
                    y=y - (k + 1) * lh,
                    max_width=max(0.0, page_x - cont_x - gap),
                    role="title_x",
                )
            )
        )

    line_count = title_lines + len(cont_results)
    return TocEntryLayoutResult(
        number=num_result,
        title=title_result,
        leader=leader_result,
        page=page_result,
        continuation=cont_results,
        title_x=title_x,
        page_x=page_x,
        y=y,
        size=sz,
        leader_gap=gap,
        title_end=title_end,
        overflow=overflow,
        level=level,
        bbox=bbox,
        line_count=line_count,
        line_height=lh,
        total_height=round(line_count * lh, 2),
        continuation_x=round(cont_x, 2),
        original_lines=1 + len(continuations),
        max_extra_lines=int(b.max_extra_lines or 0),
        recovery=title_recovery,
    )


def toc_layout_commands(result: TocEntryLayoutResult) -> list[dict]:
    """One layout result → JSON-safe positioned commands (draw only).

    Commands mirror the golden renderer's schema: ``{kind, text, x, y,
    width, level, bbox}`` with kind ∈ number/title/leader/page.  ``x``/``y``
    come from the settled LayoutResult anchors (verbatim placement); no
    re-layout happens here.

    7F-5b: a wrapped title emits **one command per line**, stepping down by
    ``line_height`` in v3 y-up — but the page number stays a single
    entry-level command (never duplicated per wrapped line).
    """
    cmds: list[dict] = []

    def _push(kind: str, res: LayoutResult | None) -> None:
        if res is None or not res.lines:
            return
        text = res.lines[0]
        cmds.append(
            {
                "kind": kind,
                "text": text,
                "x": round(float(res.bbox[0]), 2),
                "y": round(float(res.bbox[1]), 2),
                "width": round(
                    float(res.line_widths[0] if res.line_widths else 0.0), 2
                ),
                "level": result.level,
                "bbox": list(result.bbox),
            }
        )

    def _push_lines(kind: str, res: LayoutResult | None) -> None:
        """Emit every wrapped line of a LayoutResult at stepped y (v3 y-up)."""
        if res is None or not res.lines:
            return
        x = float(res.bbox[0])
        y0 = float(res.bbox[1])
        lh = float(result.line_height or _DEFAULT_LINE_HEIGHT)
        for i, (ln, w) in enumerate(zip(res.lines, res.line_widths)):
            cmds.append(
                {
                    "kind": kind,
                    "text": ln,
                    "x": round(x, 2),
                    "y": round(y0 - i * lh, 2),
                    "width": round(float(w), 2),
                    "level": result.level,
                    "bbox": list(result.bbox),
                }
            )

    _push("number", result.number)
    _push_lines("title", result.title)
    _push("leader", result.leader)
    _push("page", result.page)
    for cont in result.continuation:
        _push("title", cont)
    return cmds
