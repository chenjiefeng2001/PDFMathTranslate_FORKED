"""Unified LayoutResult contract — Commit 7F-6a.

7F-6a unifies the **output contract** across the four layout paths (Flow /
List / TOC / Code) *without* unifying their implementations and *without*
changing any rendering behavior, geometry, recovery policy, or existing
output.  The guiding principle: **unify the contract, not the implementation.**

The four paths each settle translated text into a result::

    Flow  -> lay_out(FlowText)          -> LayoutResult         (atomic run)
    Code  -> lay_out(PreservedRegion)   -> LayoutResult         (PRESERVE, never adaptive)
    List  -> layout_list_item           -> ListLayoutResult     (marker/content/… LayoutResults)
    TOC   -> layout_toc_entry           -> TocEntryLayoutResult (number/title/leader/page/… LayoutResults)

The atomic run is already a single :class:`LayoutResult` everywhere.  What
7F-6a adds is one declared contract — :class:`LayoutResultLike` — plus a thin
:func:`as_layout_result` adapter so a consumer can treat **any** of the four
results uniformly:

    primitive_kind / lines / line_widths / bbox / overflow / font_size
        recovery / to_dict()

- Atomic ``LayoutResult`` objects (Flow / Code) already satisfy the contract
  structurally (7F-6a adds the uniform ``recovery`` member to
  :class:`LayoutResult`).
- ``ListLayoutResult`` / ``TocEntryLayoutResult`` are aggregates of channel
  ``LayoutResult``\ s; :func:`as_layout_result` wraps them in a read-only
  *view* that presents the same contract over their settled channels.

Nothing here re-derives geometry from ``level`` / ``index``, detects, parses,
translates, or draws — the adapter is a pure read of already-settled results.
In 7F-6a it is used for inspection / architecture tests only; no renderer
consumes it yet (renderer wiring lands in later 7F-6 steps).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pdf2zh.semantic.layout.overflow import LayoutResult

__all__ = ["LayoutResultLike", "as_layout_result"]


@runtime_checkable
class LayoutResultLike(Protocol):
    """The common *inspection* contract every settled layout exposes.

    Read-only shape consumed uniformly by architecture tests / evaluators and,
    in later 7F-6 steps, by renderer entry points.  Atomic ``LayoutResult``
    objects satisfy it directly; List / TOC aggregates satisfy it through
    :func:`as_layout_result`.
    """

    primitive_kind: str
    lines: list[str]
    line_widths: list[float]
    bbox: tuple[float, float, float, float]
    overflow: bool
    font_size: float
    recovery: dict | None

    def to_dict(self) -> dict:
        ...


# ---------------------------------------------------------------------------
# adapter — one uniform view over any of the four results
# ---------------------------------------------------------------------------


def _is_list_aggregate(x: Any) -> bool:
    return hasattr(x, "marker") and hasattr(x, "content") and hasattr(x, "continuation")


def _is_toc_aggregate(x: Any) -> bool:
    return hasattr(x, "title") and hasattr(x, "page") and hasattr(x, "page_x")


def _channels(aggregate: Any) -> list[LayoutResult]:
    """Settled channel ``LayoutResult``\ s of a List / TOC aggregate, reading order."""
    if _is_list_aggregate(aggregate):
        out: list[LayoutResult] = [aggregate.marker]
        if aggregate.content is not None:
            out.append(aggregate.content)
        out.extend(list(aggregate.continuation or []))
        return [c for c in out if c is not None]
    if _is_toc_aggregate(aggregate):
        out = []
        for name in ("number", "title", "leader", "page"):
            v = getattr(aggregate, name, None)
            if v is not None:
                out.append(v)
        out.extend(list(aggregate.continuation or []))
        return [c for c in out if c is not None]
    return []


class _AggregateView:
    """Read-only :class:`LayoutResultLike` view over a List / TOC aggregate.

    Every member is derived from already-settled channel ``LayoutResult``\ s
    (or the aggregate's own verbatim fields) — the view never re-lays-out,
    never re-derives geometry, and is never consumed by the renderer in 7F-6a.
    """

    def __init__(self, aggregate: Any, kind: str) -> None:
        self._agg = aggregate
        self._kind = str(kind)
        self._ch = _channels(aggregate)

    @property
    def primitive_kind(self) -> str:
        k = getattr(self._agg, "primitive_kind", None)
        return str(k) if k else self._kind

    @property
    def lines(self) -> list[str]:
        return [ln for c in self._ch for ln in (c.lines or []) if ln]

    @property
    def line_widths(self) -> list[float]:
        return [float(w) for c in self._ch for w in (c.line_widths or [])]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        b = getattr(self._agg, "bbox", None)
        if b is not None and any(b):
            return tuple(float(v) for v in b)
        # best-effort block bbox from channel anchors (inspection only)
        xs = [c.bbox[0] for c in self._ch if (c.lines or [])]
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        x1 = max(
            (c.bbox[0] + (c.line_widths[0] if c.line_widths else 0.0))
            for c in self._ch
            if (c.lines or [])
        )
        ys = [c.bbox[1] for c in self._ch if (c.lines or [])]
        return (min(xs), min(ys), max(x1, 0.0), max(ys))

    @property
    def overflow(self) -> bool:
        o = getattr(self._agg, "overflow", None)
        if o is not None:
            return bool(o)
        return any(bool(getattr(c, "overflow", False)) for c in self._ch)

    @property
    def font_size(self) -> float:
        fs = getattr(self._agg, "font_size", None)
        if fs is None:
            fs = getattr(self._agg, "size", None)
        return float(fs) if fs is not None else 0.0

    @property
    def recovery(self) -> dict | None:
        r = getattr(self._agg, "recovery", None)
        if r:
            return r
        for c in self._ch:
            d = c.to_dict() if hasattr(c, "to_dict") else {}
            rec = d.get("recovery")
            if rec:
                return rec
        return None

    def to_dict(self) -> dict:
        d = getattr(self._agg, "to_dict", None)
        return dict(d()) if callable(d) else {}


def as_layout_result(result: Any) -> LayoutResultLike:
    """One uniform :class:`LayoutResultLike` view of any of the four results.

    Args:
        result: a settled layout result — ``LayoutResult`` (Flow / Code), or a
            ``ListLayoutResult`` / ``TocEntryLayoutResult`` aggregate.

    Returns:
        The atomic :class:`LayoutResult` unchanged when already one; otherwise
        a read-only view exposing the uniform contract over the aggregate's
        settled channels.

    Raises:
        TypeError: when ``result`` is not a settled layout result of any of the
            four paths.
    """
    if isinstance(result, LayoutResult):
        return result
    if _is_list_aggregate(result):
        return _AggregateView(result, "list")
    if _is_toc_aggregate(result):
        return _AggregateView(result, "toc")
    raise TypeError(f"not a settled layout result: {type(result).__name__}")
