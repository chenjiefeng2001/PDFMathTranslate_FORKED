"""FlowText render side-channel — Commit 7E-1 (block → LayoutResult → PDF).

Bridges a plain paragraph block's *translated* text into the unified FlowText
layout pipeline so the PDF renderer consumes a settled LayoutResult instead of
re-wrapping at draw time::

    Block(translated, bbox, font_size)
        ↓  flow_text_from_block / build_block_flow_payload
    FlowText (origin / max_width / max_height from block geometry verbatim)
        ↓  semantic.renderer.flow.render_flow_text()
    LayoutResult → commands
        ↓
    magicpdf_renderer draws commands  (no re-layout)

Coordinate convention is v3 lower-left (y up): the block's ``bbox`` uses
(``x0``,``y0``,``x1``,``y1``) with ``y1`` the top; the first line anchors at
``y1`` (or the block's first line baseline when present) and wrapped lines step
downward via a **negative** step so magicpdf's y-flip places them correctly.

Pure logic + geometry passthrough: this module never re-derives position from
level / index / page width and never calls the wrap/shrink/clip primitives
directly (all fit decisions go through ``lay_out``).
"""

from __future__ import annotations

from typing import Callable, Optional

from pdf2zh.semantic.layout.primitives import FlowText
from pdf2zh.semantic.renderer.flow import FlowTextRenderer, render_flow_text

__all__ = [
    "flow_text_from_block",
    "build_block_flow_payload",
    "DEFAULT_LINE_HEIGHT",
]

DEFAULT_LINE_HEIGHT = 1.4


def _block_translated(block) -> str:
    md = getattr(block, "metadata", None) or {}
    translated = md.get("translated")
    if isinstance(translated, str) and translated.strip():
        return translated
    return (getattr(block, "text", None) or "")


def _block_font_size(block, default: float = 11.0) -> float:
    """Consume Font-Resolution major size, else block font_size, else default."""
    md = getattr(block, "metadata", None) or {}
    for k in ("font_size", "font_size_max"):
        v = md.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v), 2)
    try:
        fs = float(getattr(block, "font_size", 0.0) or 0.0)
    except (TypeError, ValueError):
        fs = 0.0
    return round(fs, 2) or default


def _block_baseline(block) -> Optional[float]:
    """First line's baseline in v3 y-up when known, else ``None``."""
    lines = list(getattr(block, "lines", None) or [])
    if lines:
        b = getattr(lines[0], "baseline", 0.0)
        try:
            b = float(b)
        except (TypeError, ValueError):
            b = 0.0
        if b > 0:
            return b
    return None


def flow_text_from_block(block) -> FlowText:
    """A block's translated text + verbatim geometry → :class:`FlowText`.

    ``origin``/``max_width``/``max_height`` are copied from the block bbox
    without recomputation.
    """
    x0 = float(getattr(block, "x0", 0.0) or 0.0)
    y0 = float(getattr(block, "y0", 0.0) or 0.0)
    x1 = float(getattr(block, "x1", 0.0) or 0.0)
    y1 = float(getattr(block, "y1", 0.0) or 0.0)
    size = _block_font_size(block)
    return FlowText(
        text=_block_translated(block),
        origin=(x0, y1),  # v3 top-left anchor; renderer steps downward
        max_width=max(0.0, x1 - x0),
        max_height=max(0.0, y1 - y0),
        line_height=size * DEFAULT_LINE_HEIGHT,
    )


def build_block_flow_payload(
    block,
    *,
    measure: Optional[Callable[[str, float], float]] = None,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> dict:
    """A paragraph block → FlowText ``LayoutResult`` render payload (JSON-safe).

    Geometry / font size come verbatim from the block; only the *translated*
    text is used.  Returns a payload with ``commands`` for the renderer; on any
    failure returns a ``layout_ok=False`` dict (never raises) so the caller can
    cascade to a legacy render path deterministically and observably.
    """
    x0 = float(getattr(block, "x0", 0.0) or 0.0)
    y1 = float(getattr(block, "y1", 0.0) or 0.0)
    x1 = float(getattr(block, "x1", 0.0) or 0.0)
    y0 = float(getattr(block, "y0", 0.0) or 0.0)
    size = _block_font_size(block)
    _bl = _block_baseline(block)
    anchor = _bl if _bl is not None else y1
    return render_flow_text(
        _block_translated(block),
        origin=(x0, anchor),
        max_width=max(0.0, x1 - x0),
        max_height=max(0.0, y1 - y0),
        line_height=float(line_height),
        font_size=size,
        measure=measure,
        line_step=-(size * float(line_height)),
    )