"""Layout primitive / geometry constraint layer — Commit 7B.

Renderer-independent geometric vocabulary that sits between the semantic
layer and the PDF renderer::

    Semantic meaning
         ↓
    RenderPayload
         ↓
    Layout primitives / constraints   <- this package
         ↓
    PDF renderer

The layer never re-judges *generated* semantics (code / list / toc / heading /
style) — those were already decided in the semantic layer.  It only carries
original geometry plus the constraints that say how rigid each piece is, so
the renderer consumes fixed shapes instead of re-deriving them.

Modules:

- :mod:`~pdf2zh.semantic.layout.primitives` — FlowText / FixedAnchor /
  FixedColumn / PreservedRegion / Continuation.
- :mod:`~pdf2zh.semantic.layout.constraints` — FixedX / FixedY / FixedWidth /
  MaxWidth / MaxHeight / PreserveBBox + ``resolve_geometry``.
- :mod:`~pdf2zh.semantic.layout.measure` — unified ``measure_text`` API.
- :mod:`~pdf2zh.semantic.layout.mapping` — how existing payloads map onto
  primitives.
- :mod:`~pdf2zh.semantic.layout.wrap` — wrapping / shrink / clip mechanics.
- :mod:`~pdf2zh.semantic.layout.overflow` — OverflowPolicy + LayoutResult +
  the ``lay_out`` engine.
"""

from __future__ import annotations

from pdf2zh.semantic.layout.constraints import (
    FixedWidth,
    FixedX,
    FixedY,
    LayoutGeometry,
    MaxHeight,
    MaxWidth,
    PreserveBBox,
    resolve_geometry,
)
from pdf2zh.semantic.layout.mapping import (
    flow_text,
    list_anchor,
    list_continuation,
    preserved_region,
    toc_page_column,
    toc_title_anchor,
)
from pdf2zh.semantic.layout.measure import measure_text, measure_text_estimate
from pdf2zh.semantic.layout.overflow import (
    LayoutResult,
    OverflowPolicy,
    lay_out,
    policy_for,
)
from pdf2zh.semantic.layout.primitives import (
    Continuation,
    FixedAnchor,
    FixedColumn,
    FlowText,
    PreservedRegion,
)
from pdf2zh.semantic.layout.wrap import clip_text, shrink_to_fit, wrap_lines, tokenize

__all__ = [
    # primitives
    "FlowText",
    "FixedAnchor",
    "FixedColumn",
    "PreservedRegion",
    "Continuation",
    # constraints
    "LayoutGeometry",
    "FixedX",
    "FixedY",
    "FixedWidth",
    "MaxWidth",
    "MaxHeight",
    "PreserveBBox",
    "resolve_geometry",
    # measure
    "measure_text",
    "measure_text_estimate",
    # mapping
    "flow_text",
    "list_anchor",
    "list_continuation",
    "toc_title_anchor",
    "toc_page_column",
    "preserved_region",
    # wrap / overflow
    "wrap_lines",
    "tokenize",
    "shrink_to_fit",
    "clip_text",
    "OverflowPolicy",
    "LayoutResult",
    "policy_for",
    "lay_out",
]