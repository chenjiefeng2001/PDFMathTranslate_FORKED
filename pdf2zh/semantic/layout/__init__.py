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
- :mod:`~pdf2zh.semantic.layout.recovery` — OverflowReason + RecoveryDecision +
  LayoutBudget + diagnose/decide policy layer (7F).
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
from pdf2zh.semantic.layout.list_layout import (
    ListLayoutResult,
    layout_list_item,
    layout_list_node,
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
from pdf2zh.semantic.layout.recovery import (
    LayoutBudget,
    OverflowDiagnosis,
    OverflowReason,
    RecoveryDecision,
    budget_for_kind,
    classify_reason,
    decide_recovery,
    default_budget,
    diagnose_overflow,
)
from pdf2zh.semantic.layout.toc_layout import (
    TocEntryLayoutResult,
    layout_toc_entry,
    toc_layout_commands,
)
from pdf2zh.semantic.layout.wrap import clip_text, shrink_to_fit, tokenize, wrap_lines

__all__ = [
    "Continuation",
    "FixedAnchor",
    "FixedColumn",
    "FixedWidth",
    "FixedX",
    "FixedY",
    # primitives
    "FlowText",
    "LayoutBudget",
    # constraints
    "LayoutGeometry",
    "LayoutResult",
    # list
    "ListLayoutResult",
    "MaxHeight",
    "MaxWidth",
    "OverflowDiagnosis",
    "OverflowPolicy",
    # recovery (7F)
    "OverflowReason",
    "PreserveBBox",
    "PreservedRegion",
    "RecoveryDecision",
    # toc
    "TocEntryLayoutResult",
    "budget_for_kind",
    "classify_reason",
    "clip_text",
    "decide_recovery",
    "default_budget",
    "diagnose_overflow",
    # mapping
    "flow_text",
    "lay_out",
    "layout_list_item",
    "layout_list_node",
    "layout_toc_entry",
    "list_anchor",
    "list_continuation",
    # measure
    "measure_text",
    "measure_text_estimate",
    "policy_for",
    "preserved_region",
    "resolve_geometry",
    "shrink_to_fit",
    "toc_layout_commands",
    "toc_page_column",
    "toc_title_anchor",
    "tokenize",
    # wrap / overflow
    "wrap_lines",
]