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
from pdf2zh.semantic.layout.page_break import (
    PageBreakDecision,
    PageBreakExecution,
    PageBreakReport,
    assert_break_invariants,
    break_invariants,
    break_placement_to_page,
    decide_page_break,
    decide_page_breaks,
    next_free_page,
    next_page_start_y,
    page_break_execution,
    page_break_from_shift,
)
from pdf2zh.semantic.layout.page_break_continuation import (
    ContinuationBreakRecord,
    ContinuationBreakReport,
    execute_continuation_breaks,
    split_continuation_break,
)
from pdf2zh.semantic.layout.global_recovery import (
    GlobalRecoveryEvent,
    GlobalRecoveryReport,
    global_recovery,
    source_geometry_snapshot,
)
from pdf2zh.semantic.layout.page_break_executor import (
    PageBreakExecutionReport,
    execute_page_breaks,
    move_entry_to_page,
    shift_command_fields,
)
from pdf2zh.semantic.layout.page_flow import (
    BlockPlacement,
    PageCollision,
    PageFlowReport,
    PageOverflow,
    PRESERVE_KINDS,
    build_page_flow_report,
    detect_collisions_from_placements,
    detect_page_collisions,
    detect_page_overflows,
    placements_from_plan,
)
from pdf2zh.semantic.layout.page_recovery import (
    BlockShiftDecision,
    PageRecoveryDecision,
    decide_block_shift,
    decide_page_recovery,
    decision_summary,
    keep_decision,
)
from pdf2zh.semantic.layout.page_shift import (
    ShiftExecutionReport,
    apply_block_shift,
    apply_page_shifts,
    block_deltas,
    resolve_page_shifts,
    shift_box_down,
)
from pdf2zh.semantic.layout.placement import (
    PlacementDecision,
    PlacementPolicy,
    PlacementScore,
    PlacementTarget,
    decide_from_settled,
    decide_placement,
    estimate_block_height,
    remaining_space_for_page,
    score_fit,
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
    # page-flow (7F-8a)
    "BlockPlacement",
    "BlockShiftDecision",
    # placement (7G-1)
    "PlacementDecision",
    "PlacementPolicy",
    "PlacementScore",
    "PlacementTarget",
    "Continuation",
    "FixedAnchor",
    "FixedColumn",
    "FixedWidth",
    "FixedX",
    "FixedY",
    # primitives
    "FlowText",
    "LayoutBudget",
    "PageBreakDecision",
    "PageBreakExecution",
    "PageBreakExecutionReport",
    "PageBreakReport",
    "PageRecoveryDecision",
    "ContinuationBreakRecord",
    "ContinuationBreakReport",
    "GlobalRecoveryEvent",
    "GlobalRecoveryReport",
    "ShiftExecutionReport",
    "PRESERVE_KINDS",
    # constraints
    "LayoutGeometry",
    "LayoutResult",
    "PageCollision",
    "PageFlowReport",
    "PageOverflow",
    "apply_block_shift",
    "apply_page_shifts",
    "assert_break_invariants",
    "block_deltas",
    "break_invariants",
    "break_placement_to_page",
    "build_page_flow_report",
    "decide_block_shift",
    "decide_page_break",
    "decide_page_breaks",
    "decide_page_recovery",
    "decision_summary",
    "detect_collisions_from_placements",
    "detect_page_collisions",
    "detect_page_overflows",
    "execute_continuation_breaks",
    "execute_page_breaks",
    "global_recovery",
    "source_geometry_snapshot",
    "split_continuation_break",
    "keep_decision",
    "move_entry_to_page",
    "next_free_page",
    "next_page_start_y",
    "page_break_execution",
    "page_break_from_shift",
    "placements_from_plan",
    "resolve_page_shifts",
    "shift_box_down",
    "shift_command_fields",
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
    "decide_from_settled",
    "decide_placement",
    "decide_recovery",
    "default_budget",
    "diagnose_overflow",
    "estimate_block_height",
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
    "remaining_space_for_page",
    "resolve_geometry",
    "score_fit",
    "shrink_to_fit",
    "toc_layout_commands",
    "toc_page_column",
    "toc_title_anchor",
    "tokenize",
    # wrap / overflow
    "wrap_lines",
]