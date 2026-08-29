"""Overflow policy + LayoutResult engine — Commit 7C.

Given a layout primitive (already carrying *original* geometry) and a width
measurer, :func:`lay_out` makes the single, testable decision of *how the text
fits its constraints* and returns a :class:`LayoutResult`.  The PDF renderer
consumes only the result — it no longer re-derives "fit / overflow" itself.

Policy pipeline per primitive kind (different primitives use different
policies; see :func:`policy_for`)::

    FlowText        -> WRAP      (normal paragraphs reflow)
    PreservedRegion -> PRESERVE  (code / formula — geometry immutable)
    FixedColumn     -> PRESERVE  (TOC page column — never moved)
    FixedAnchor     -> SHRINK    (TOC title / list content — mechanism ready,
                                  not auto-applied this commit unless enabled)
    Continuation    -> WRAP      (already a follow-on line)

Rules that matter:

- **Code never enters the wrapping path** — :class:`PreservedRegion` is always
  ``PRESERVE`` (locked by the 7C architecture test).
- **CLIP is a last resort** — any clip returns ``overflow=True`` +
  ``policy=CLIP``; never silent.
- **SHRINK machinery exists** (see
  :func:`pdf2zh.semantic.layout.wrap.shrink_to_fit`) but is only exercised
  when ``allow_shrink=True`` — this commit does not auto-shrink TOC/list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from pdf2zh.semantic.layout.constraints import FixedWidth, MaxHeight, MaxWidth
from pdf2zh.semantic.layout.measure import measure_text
from pdf2zh.semantic.layout.primitives import (
    FixedAnchor,
    FixedColumn,
    FlowText,
    PreservedRegion,
)
from pdf2zh.semantic.layout.wrap import clip_text, shrink_to_fit, wrap_lines

__all__ = ["OverflowPolicy", "LayoutResult", "policy_for", "lay_out"]


class OverflowPolicy(Enum):
    """How translated text adapts to an overflowing geometry constraint."""

    PRESERVE = "preserve"  # never change geometry; report overflow if any
    WRAP = "wrap"          # reflow into multiple lines within max_width
    SHRINK = "shrink"      # reduce font_size to fit (mechanism; opt-in)
    CLIP = "clip"          # truncate — always recorded, never silent


_POLICY_BY_KIND = {
    "flow": OverflowPolicy.WRAP,
    "anchor": OverflowPolicy.SHRINK,
    "column": OverflowPolicy.PRESERVE,
    "preserved": OverflowPolicy.PRESERVE,
    "continuation": OverflowPolicy.WRAP,
}


def policy_for(kind: str) -> OverflowPolicy:
    """Return the default overflow policy for a primitive ``kind``."""
    return _POLICY_BY_KIND.get(kind, OverflowPolicy.WRAP)


@dataclass
class LayoutResult:
    """The outcome of deciding how translated text fits its constraints.

    The renderer reads this and writes drawing commands only; it never
    re-applies the fit / overflow decision itself.
    """

    text: str
    lines: list[str] = field(default_factory=list)
    line_widths: list[float] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    overflow: bool = False
    policy: OverflowPolicy = OverflowPolicy.WRAP
    font_size: float = 11.0
    primitive_kind: str = "flow"

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "lines": list(self.lines),
            "line_widths": [round(w, 1) for w in self.line_widths],
            "bbox": [round(v, 1) for v in self.bbox],
            "overflow": bool(self.overflow),
            "policy": self.policy.value,
            "font_size": round(self.font_size, 1),
            "primitive_kind": self.primitive_kind,
        }


def _primitive_geometry(prim) -> tuple[float, float, float, float]:
    """Base region ``(x0, y0, x1, y1)`` for a primitive, verbatim where known."""
    if isinstance(prim, PreservedRegion):
        return tuple(float(v) for v in prim.bbox)
    if isinstance(prim, FixedColumn):
        y = float(prim.y or 0.0)
        return (prim.column_x, y, prim.column_x, y)
    if isinstance(prim, FixedAnchor):
        w = float(getattr(prim, "max_width", 0.0) or 0.0)
        y = float(getattr(prim, "y", 0.0) or 0.0)
        return (float(prim.x), y, float(prim.x) + w, y)
    if isinstance(prim, FlowText):
        x, y = prim.origin
        return (x, y, x + float(prim.max_width or 0.0), y + float(prim.max_height or 0.0))
    return (0.0, 0.0, 0.0, 0.0)


def _available_sizes(prim, constraints: tuple = ()) -> tuple[float, float]:
    """Available width / height from the primitive + width-relevant constraints."""
    if isinstance(prim, PreservedRegion):
        width = prim.bbox[2] - prim.bbox[0]
        height = prim.bbox[3] - prim.bbox[1]
    else:
        width = float(getattr(prim, "max_width", 0.0) or 0.0)
        height = float(getattr(prim, "max_height", 0.0) or 0.0)
    for c in constraints:
        if isinstance(c, FixedWidth):
            width = float(c.width)
        elif isinstance(c, MaxWidth):
            width = float(c.max_width) if width <= 0.0 else min(width, float(c.max_width))
        elif isinstance(c, MaxHeight):
            height = float(c.max_height) if height <= 0.0 else min(height, float(c.max_height))
    return width, height


def _safe_w2(measure: Callable[[str, float], float], s: str, size: float) -> float:
    try:
        w = float(measure(s, size))
    except Exception:  # noqa: BLE001 -- measurement failure is non-fatal
        return 0.0
    return w if w >= 0.0 else 0.0


def lay_out(
    primitive: object,
    *,
    measure: Callable[[str, float], float] | None = None,
    avail_width: float | None = None,
    avail_height: float | None = None,
    constraints: tuple = (),
    font_size: float = 11.0,
    policy: OverflowPolicy | None = None,
    allow_shrink: bool = False,
    min_font_size: float = 5.0,
    tolerance: float = 1e-6,
) -> LayoutResult:
    """Decide how ``primitive``'s (translated) text fits its constraints.

    Args:
        primitive: a layout primitive (FlowText / FixedAnchor / FixedColumn /
            PreservedRegion / Continuation).  Its ``kind`` selects the policy.
        measure: ``(text, font_size) -> width``; defaults to the unified
            :func:`pdf2zh.semantic.layout.measure.measure_text`.
        avail_width / avail_height: explicit sizes; otherwise derived from the
            primitive + ``constraints`` (FixedWidth / MaxWidth / MaxHeight).
        constraints: geometry constraints to fold in for width/height.
        font_size: nominal font size.
        policy: override the primitive's default policy.
        allow_shrink: exercise the SHRINK mechanism (default off — this commit
            does not auto-shrink TOC/list).
        min_font_size: lower clamp for SHRINK.
        tolerance: floating-point slack for fit checks.

    Returns:
        :class:`LayoutResult`.  Never raises from the measurer.
    """
    measurer = measure or (lambda s, size: measure_text(s, None, size))
    kind = getattr(primitive, "kind", "flow")
    text = str(getattr(primitive, "text", "") or "")
    pol = policy or policy_for(kind)
    fs = float(font_size)

    width, height = _available_sizes(primitive, constraints)
    if avail_width is not None:
        width = float(avail_width)
    if avail_height is not None:
        height = float(avail_height)
    width = float(width or 0.0)
    height = float(height or 0.0)

    def _m(s: str) -> float:
        return _safe_w2(measurer, s, fs)

    def _finish(lines, widths, overflow, used_policy, used_fs):
        return LayoutResult(
            text=text,
            lines=lines,
            line_widths=widths,
            bbox=_primitive_geometry(primitive),
            overflow=bool(overflow),
            policy=used_policy,
            font_size=used_fs,
            primitive_kind=kind,
        )

    if pol is OverflowPolicy.PRESERVE:
        w = _m(text)
        overflow = width > 0.0 and bool(text) and (w > width + tolerance)
        return _finish([text], [w], overflow, pol, fs)

    if pol is OverflowPolicy.WRAP:
        if width > 0.0:
            lines = wrap_lines(text, _m, width)
        else:
            lines = [(text, _m(text))]
        line_texts = [ln for ln, _ in lines]
        widths = [w for _, w in lines]
        overflow = (bool(text) and any(w > width + tolerance for w in widths)) or (
            height > 0.0 and len(lines) * fs > height + tolerance
        )
        return _finish(line_texts, widths, overflow, pol, fs)

    if pol is OverflowPolicy.SHRINK:
        if allow_shrink and width > 0.0:
            eff, still = shrink_to_fit(text, measurer, width, fs, min_font_size)
            w = _safe_w2(measurer, text, eff)
            return _finish([text], [w], still, pol, eff)
        w = _m(text)
        overflow = width > 0.0 and bool(text) and (w > width + tolerance)
        return _finish([text], [w], overflow, pol, fs)

    # CLIP — last resort, never silent.
    clipped, _ = clip_text(text, _m, width)
    return _finish([clipped], [_m(clipped)], True, pol, fs)