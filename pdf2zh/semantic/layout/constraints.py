"""Renderer-independent geometry constraints — Commit 7B.

Constraints express *how rigid* a piece of translated geometry is, in one
neutral vocabulary the PDF renderer can apply.  The renderer must not re-derive
these from level/index — the semantic/plan stage already decided them.

Supported constraints:

- :class:`FixedX`       — pin horizontal origin.
- :class:`FixedY`       — pin vertical origin.
- :class:`FixedWidth`   — pin width exactly (no shrink allowed).
- :class:`MaxWidth`     — cap width (text may overflow rightwards otherwise).
- :class:`MaxHeight`    — cap height (text may overflow downwards otherwise).
- :class:`PreserveBBox` — keep the whole bbox verbatim.

:func:`resolve_geometry` folds a list of constraints onto a base
:class:`LayoutGeometry`, left-to-right (later constraints override earlier
ones) — the "resolved" shape the renderer is allowed to write into.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "LayoutGeometry",
    "FixedX",
    "FixedY",
    "FixedWidth",
    "MaxWidth",
    "MaxHeight",
    "PreserveBBox",
    "resolve_geometry",
]


@dataclass(frozen=True)
class LayoutGeometry:
    """A resolved rectangular placement (x, y, width, height)."""

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass(frozen=True)
class FixedX:
    """Pin the horizontal origin to an exact value."""

    x: float


@dataclass(frozen=True)
class FixedY:
    """Pin the vertical origin to an exact value."""

    y: float


@dataclass(frozen=True)
class FixedWidth:
    """Pin the width to an exact value (no partial shrink)."""

    width: float


@dataclass(frozen=True)
class MaxWidth:
    """Cap the width; a wider result is allowed to overflow rightwards."""

    max_width: float


@dataclass(frozen=True)
class MaxHeight:
    """Cap the height; a taller result is allowed to overflow downwards."""

    max_height: float


@dataclass(frozen=True)
class PreserveBBox:
    """Preserve the full original bbox (x0, y0, x1, y1) verbatim.

    Used for code / formula / other untranslatable regions.  ``width`` /
    ``height`` are derived from the bbox so they stay consistent with it.
    """

    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


def resolve_geometry(
    geometry: LayoutGeometry,
    constraints: tuple = (),
) -> LayoutGeometry:
    """Fold ``constraints`` onto ``geometry`` (left-to-right override).

    Args:
        geometry: the base placement (origin + natural width/height).
        constraints: an iterable of constraint instances from this module.

    Returns:
        A new :class:`LayoutGeometry` with the constraints applied.  Unknown
        constraint types are ignored (so the caller can pass supersets safely).
    """
    x, y, w, h = geometry.x, geometry.y, geometry.width, geometry.height
    for c in constraints:
        if isinstance(c, FixedX):
            x = float(c.x)
        elif isinstance(c, FixedY):
            y = float(c.y)
        elif isinstance(c, FixedWidth):
            w = float(c.width)
        elif isinstance(c, MaxWidth):
            w = min(float(geometry.width), float(c.max_width))
        elif isinstance(c, MaxHeight):
            h = min(float(geometry.height), float(c.max_height))
        elif isinstance(c, PreserveBBox):
            x, y, x1, y1 = (float(v) for v in c.bbox)
            w = x1 - x
            h = y1 - y
    return LayoutGeometry(x=x, y=y, width=w, height=h)