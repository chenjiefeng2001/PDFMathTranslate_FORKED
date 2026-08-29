"""Geometry layout primitives — Commit 7B.

Renderer-independent vocabulary for *where* translated text is placed, so the
PDF renderer can consume a fixed shape instead of re-deriving geometry from
semantic hints (level, index, page width).

The layer sits below the semantic layer and above the renderer::

    Semantic meaning
         ↓
    RenderPayload
         ↓
    Layout primitives / constraints   <- this package
         ↓
    PDF renderer

The primitives deliberately encode **fixed original geometry**:

- :class:`FlowText`       — ordinary paragraphs; allows future wrapping.
- :class:`FixedAnchor`    — a run pinned to an original ``x`` (list content_x,
  TOC title_x); never recomputed from level/index.
- :class:`FixedColumn`    — a run pinned to an original column ``x`` (TOC
  page_x); translation length change must not move it.
- :class:`PreservedRegion`— an untranslatable region whose bbox is preserved
  verbatim (code, formula, …).
- :class:`Continuation`   — a follow-on line of a semantic block, with an
  explicit anchor + parent.

No semantic detection here: these are pure geometry carriers.  Architecture
tests forbid ``looks_like_*`` / ``detect_*`` / ``level *`` / ``index *`` in
this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "FlowText",
    "FixedAnchor",
    "FixedColumn",
    "PreservedRegion",
    "Continuation",
]


@dataclass(frozen=True)
class FlowText:
    """A normal flow paragraph.

    ``origin`` is ``(x, y)`` of the block; ``max_width`` / ``max_height`` bound
    the region available for (future) wrapping; ``line_height`` is the vertical
    step between wrapped lines.
    """

    text: str
    origin: tuple[float, float] = (0.0, 0.0)
    max_width: float = 0.0
    max_height: float = 0.0
    line_height: float = 0.0

    kind = "flow"

    @property
    def x(self) -> float:
        return self.origin[0]

    @property
    def y(self) -> float:
        return self.origin[1]


@dataclass(frozen=True)
class FixedAnchor:
    """A run pinned to an **original** ``x`` anchor.

    Covers list ``content_x`` and TOC ``title_x``.  ``x`` is copied verbatim
    from the parsed geometry and must never be recomputed from ``level``,
    entry index, or ``level * constant`` math.  ``role`` records what the
    anchor is (e.g. ``\"content_x\"`` / ``\"title_x\"``) for debugging; it is
    informational and does not participate in geometry.
    """

    text: str
    x: float = 0.0
    y: float = 0.0
    max_width: float = 0.0
    role: str = "anchor"

    kind = "anchor"


@dataclass(frozen=True)
class FixedColumn:
    """A run pinned to an original **column** x (TOC page_x).

    ``column_x`` must equal the original page-number column; a change in the
    translated text length must never shift it.
    """

    text: str
    column_x: float = 0.0
    y: float = 0.0

    kind = "column"

    @property
    def x(self) -> float:
        return self.column_x


@dataclass(frozen=True)
class PreservedRegion:
    """An untranslatable region (code, formula, …) whose bbox is preserved.

    Geometry is taken verbatim from the original; it never changes during
    translation.
    """

    text: str
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    kind = "preserved"

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def origin(self) -> tuple[float, float]:
        return (self.bbox[0], self.bbox[1])


@dataclass(frozen=True)
class Continuation:
    """A follow-on line of a semantic block.

    Expresses its own horizontal/vertical position and links back to the
    parent :class:`FixedAnchor` so the renderer knows which block it belongs
    to.  ``continuation_x`` is copied from the block's continuation geometry
    (e.g. a list's ``content_x`` for wrapped lines), never recomputed.
    """

    text: str
    continuation_x: float = 0.0
    continuation_y: float = 0.0
    parent_anchor: Optional[FixedAnchor] = None

    kind = "continuation"

    @property
    def x(self) -> float:
        return self.continuation_x

    @property
    def y(self) -> float:
        return self.continuation_y