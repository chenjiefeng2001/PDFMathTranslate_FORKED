"""Adaptive layout execution — Commit 7F-4.

A thin **execution** layer that turns a decision from
:mod:`pdf2zh.semantic.layout.recovery` into a final :class:`LayoutResult`:

    LayoutResult (overflow)
        ↓ classify_reason / decide_recovery   (recovery.py — policy)
    RecoveryDecision
        ↓ adaptive_layout                     (this module — execution)
    final LayoutResult (with recovery diagnostics)

Responsibilities are strictly limited to **executing** the policy that
``recovery.py`` already chose.  It never detects, never translates, never draws,
and never derives geometry from ``level`` / ``index``.  The only engine it calls
is :func:`pdf2zh.semantic.layout.overflow.lay_out` — which internally uses the
7C mechanics ``wrap_lines`` / ``shrink_to_fit`` / ``clip_text``.

Execution is a **finite state machine**, never a `while overflow` loop::

    initial = lay_out(...)
    if not initial.overflow:  → return initial            (NO_ACTION)
       ↓
    WRAP    (if budget.allow_wrap)    → still overflow?
       ↓
    SHRINK  (if budget.allow_shrink)  → still overflow?
       ↓
    CLIP    (if budget.allow_clip)    → overflow stays True (never silent)
       ↓
    otherwise → PRESERVE_OVERFLOW     (budget ran out, no silent success)

Each stage runs **at most once** and is budget-gated, so a `translation
= "A" * 10000`` can never cause an infinite recover loop.  SHRINK is clamped by
``min_font_size`` / ``max_font_reduction`` — it will never shrink a paragraph to
an unreadable 3pt.

Integration (7F-4): Flow and List content/continuation route through
``adaptive_layout``.  List **marker** stays a raw ``FixedAnchor`` / ``lay_out``
call — adaptive never touches it (marker is PRESERVE).  TOC (anchor) adaptation
is deferred to 7F-5.
"""

from __future__ import annotations

from typing import Callable, Optional

from pdf2zh.semantic.layout.overflow import (
    LayoutResult,
    OverflowPolicy,
    lay_out,
)
from pdf2zh.semantic.layout.recovery import (
    LayoutBudget,
    OverflowReason,
    RecoveryDecision,
    budget_for_kind,
    classify_reason,
    decide_recovery,
)

__all__ = ["adaptive_layout"]

_DEFAULT_MIN_FONT = 5.0


def _stage_steps(stage: OverflowPolicy) -> str:
    return {OverflowPolicy.WRAP: "WRAP", OverflowPolicy.SHRINK: "SHRINK",
            OverflowPolicy.CLIP: "CLIP"}[stage]


def _shrink_floor(budget: LayoutBudget, original_size: float) -> float:
    """Hard floor for SHRINK: respects ``min_font_size`` and ``max_font_reduction``."""
    floor = budget.min_font_size if budget.min_font_size is not None else _DEFAULT_MIN_FONT
    if budget.max_font_reduction is not None and original_size > 0.0:
        floor = max(floor, original_size * (1.0 - budget.max_font_reduction))
    return float(floor)


def _finalize(
    result: LayoutResult,
    reason: Optional[OverflowReason],
    decision: RecoveryDecision,
    steps: list[str],
    original_font_size: float,
) -> LayoutResult:
    """Attach JSON-safe recovery diagnostics to the result."""
    result.recovery_reason = reason.value if reason else None
    result.recovery_decision = decision.value
    result.recovery_steps = list(steps)
    result.original_font_size = round(float(original_font_size), 2)
    result.font_size = round(float(result.font_size), 2)
    return result


def adaptive_layout(
    primitive,
    *,
    measure: Callable[[str, float], float] | None = None,
    avail_width: float = 0.0,
    avail_height: float = 0.0,
    constraints: tuple = (),
    font_size: float = 11.0,
    budget: Optional[LayoutBudget] = None,
    target: Optional[str] = None,
) -> LayoutResult:
    """Execute a finite recovery and return one final :class:`LayoutResult`.

    Args:
        primitive: a layout primitive (FlowText / FixedAnchor / ...).  The kind
            is read off the primitive only to pick a default budget; the actual
            decision comes from ``recovery.py``.
        measure: ``(text, font_size) -> width`` measurer (passes to ``lay_out``).
        avail_width / avail_height: available box for this run.
        constraints: constraint tuple passed to every ``lay_out`` call.
        font_size: nominal font size (the SHRINK floor / diagnostics reference it).
        budget: per-primitive recovery budget; defaults by ``primitive.kind``.
        target: ``"marker"`` refines an anchor (see ``recovery.decide_recovery``).

    Returns:
        A single :class:`LayoutResult` — the caller treats it like any other
        LayoutResult.  ``overflow`` is True only when recovery genuinely could
        not fit it (CLIP / running out of budget) — **never silently flipped to
        False by clipping**.
    """
    kind = str(getattr(primitive, "kind", "flow") or "flow")
    b = budget or budget_for_kind(kind)
    fs = float(font_size or _DEFAULT_MIN_FONT)
    original = fs

    # Stage 0: baseline fit (lay_out applies the primitive's default policy).
    initial = lay_out(
        primitive,
        measure=measure,
        avail_width=avail_width,
        avail_height=avail_height,
        constraints=constraints,
        font_size=fs,
    )
    if not initial.overflow:
        # no overflow -> NO_ACTION; leave recovery fields empty (JSON hides them).
        return initial

    reason = classify_reason(
        initial, avail_width=avail_width, avail_height=avail_height
    )
    steps: list[str] = []
    result = initial
    cur_fs = fs  # track the font actually in effect (may be shrunk below)

    # Stage 1: WRAP (only when budget allows and wrapping is meaningful).
    if b.allow_wrap and reason in (OverflowReason.WIDTH, OverflowReason.HEIGHT):
        result = lay_out(
            primitive,
            measure=measure,
            avail_width=avail_width,
            avail_height=avail_height,
            constraints=constraints,
            font_size=fs,
            policy=OverflowPolicy.WRAP,
        )
        steps.append(_stage_steps(OverflowPolicy.WRAP))
        if not result.overflow:
            return _finalize(result, reason, RecoveryDecision.WRAP, steps, original)
        reason = classify_reason(
            result, avail_width=avail_width, avail_height=avail_height
        )

    # Stage 2: SHRINK (budget-gated, clamped to a readable floor).
    if b.allow_shrink and result.overflow and reason is not OverflowReason.PRESERVED_REGION:
        floor = _shrink_floor(b, original)
        result = lay_out(
            primitive,
            measure=measure,
            avail_width=avail_width,
            avail_height=avail_height,
            constraints=constraints,
            font_size=cur_fs,
            policy=OverflowPolicy.SHRINK,
            allow_shrink=True,
            min_font_size=floor,
        )
        steps.append(_stage_steps(OverflowPolicy.SHRINK))
        cur_fs = float(result.font_size or cur_fs)
        if not result.overflow:
            return _finalize(result, reason, RecoveryDecision.SHRINK, steps, original)
        reason = classify_reason(
            result, avail_width=avail_width, avail_height=avail_height
        )

    # Stage 3: CLIP (last resort; always keeps overflow=True).  It clips at the
    # font currently in effect (shrunk size if SHRINK ran) so the diagnostics
    # ``final_font_size`` honestly reflects the whole pipeline.
    if b.allow_clip and result.overflow and reason is not OverflowReason.PRESERVED_REGION:
        result = lay_out(
            primitive,
            measure=measure,
            avail_width=avail_width,
            avail_height=avail_height,
            constraints=constraints,
            font_size=cur_fs,
            policy=OverflowPolicy.CLIP,
        )
        steps.append(_stage_steps(OverflowPolicy.CLIP))
        decision = RecoveryDecision.CLIP
        return _finalize(result, reason, decision, steps, original)

    # Budget exhausted: PRESERVE_OVERFLOW — keep geometry, report overflow.
    decision = decide_recovery(kind, reason, budget=b, target=target)
    if decision in (RecoveryDecision.NO_ACTION, RecoveryDecision.WRAP,
                    RecoveryDecision.SHRINK) and not steps:
        decision = RecoveryDecision.PRESERVE_OVERFLOW
    return _finalize(result, reason, decision, steps, original)