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

# ── TOC-title ladder constants (7F-5a; housed here since 7F-6c so the title
# path shares THE single executor — no second hand-rolled recovery loop) ──
_TITLE_SHRINK_STEP = 0.85       # geometric font descent per SHRINK iteration
_MAX_TITLE_SHRINK_STEPS = 8     # bounded: never a while-loop


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
    trace: Optional[list[dict]] = None,
) -> LayoutResult:
    """Attach JSON-safe recovery diagnostics to the result.

    ``trace`` (7F-7d): per-stage recovery trace, optional.  Kept out of the
    ``recovery`` dict / ``to_dict()`` so the 7F-6a contract stays identical;
    the diagnostics layer surfaces it.
    """
    result.recovery_reason = reason.value if reason else None
    result.recovery_decision = decision.value
    result.recovery_steps = list(steps)
    result.original_font_size = round(float(original_font_size), 2)
    result.font_size = round(float(result.font_size), 2)
    if trace:
        result.recovery_trace = [dict(t) for t in trace]
    return result


def _trace_entry(
    decision: str, overflow: bool, line_count: int, font_size: float
) -> dict:
    """One JSON-safe recovery-trace entry (7F-7d): what a stage *did*."""
    return {
        "decision": decision,
        "overflow": bool(overflow),
        "line_count": int(line_count),
        "font_size": round(float(font_size), 2),
    }


def _adaptive_layout_title(
    primitive,
    *,
    measure: Callable[[str, float], float] | None,
    avail_width: float,
    avail_height: float,
    constraints: tuple,
    font_size: float,
    budget: LayoutBudget,
) -> LayoutResult:
    """Bounded TOC-title ladder (7F-5a): WRAP → SHRINK → PRESERVE_OVERFLOW.

    The title is the ONLY adaptive channel of a TOC entry — ``page_x`` /
    ``page_number`` / ``title_x`` are immovable and a title is **never CLIP**
    (it must never be truncated into the page column).  Ladder:

    - single line that fits → NO_ACTION (recovery stays ``None``);
    - wraps within ``1 + budget.max_extra_lines`` → WRAP;
    - over the line budget → SHRINK (bounded geometric font descent,
      re-wrapping at each size, at most ``_MAX_TITLE_SHRINK_STEPS``);
    - still over budget (or an unbreakable token wider than the box) →
      PRESERVE_OVERFLOW — overflow stays explicit, never silent.

    ``allowed_lines = 1 + max_extra_lines`` (the title's own original is one
    line; continuation lines are separate channels).
    """
    allowed = 1 + int(budget.max_extra_lines or 0)
    original = float(font_size)
    r = lay_out(
        primitive, measure=measure, avail_width=avail_width,
        avail_height=avail_height, constraints=constraints,
        font_size=font_size, policy=OverflowPolicy.WRAP,
    )
    n = len(r.lines)

    # fits the budget already (single line, or wrapped within the extra lines)
    if n <= allowed and not r.overflow:
        if n == 1:
            return r  # NO_ACTION — leave recovery fields empty (recovery None)
        return _finalize(r, OverflowReason.WIDTH, RecoveryDecision.WRAP,
                         ["WRAP"], original)

    steps: list[str] = ["WRAP"] if n > 1 else []
    trace: list[dict] = []
    if n > 1:
        trace.append(_trace_entry("WRAP", True, n, float(font_size)))
    if not budget.allow_shrink:
        reason = OverflowReason.HEIGHT if n > allowed else OverflowReason.WIDTH
        if n > allowed:
            r.overflow = True  # line-budget overflow is explicit, never silent
        return _finalize(r, reason, RecoveryDecision.PRESERVE_OVERFLOW, steps, original, trace)

    floor = _shrink_floor(budget, original)
    size = float(font_size)
    best = r
    for _ in range(_MAX_TITLE_SHRINK_STEPS):
        size = max(floor, size * _TITLE_SHRINK_STEP)
        r2 = lay_out(
            primitive, measure=measure, avail_width=avail_width,
            avail_height=avail_height, constraints=constraints,
            font_size=size, policy=OverflowPolicy.WRAP,
        )
        best = r2
        steps.append("SHRINK")
        trace.append(
            _trace_entry("SHRINK", len(r2.lines) > allowed or r2.overflow,
                         len(r2.lines), size)
        )
        if len(r2.lines) <= allowed and not r2.overflow:
            reason = (
                OverflowReason.HEIGHT if len(r2.lines) > allowed
                else OverflowReason.WIDTH
            )
            return _finalize(r2, reason, RecoveryDecision.SHRINK, steps, original, trace)
        if size <= floor + 1e-6:
            break
    # budget exhausted: an over-budget line count is explicit, never silent.
    reason = OverflowReason.HEIGHT if len(best.lines) > allowed else OverflowReason.WIDTH
    if len(best.lines) > allowed:
        best.overflow = True
    return _finalize(best, reason, RecoveryDecision.PRESERVE_OVERFLOW, steps, original, trace)


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
        target: ``"marker"`` refines an anchor (see ``recovery.decide_recovery``);
            ``"title"`` selects the 7F-5a TOC-title ladder (WRAP → SHRINK →
            PRESERVE_OVERFLOW, never CLIP) — 7F-6c keeps the TOC title on THE
            single executor.

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

    # 7F-6c-2: TOC title routes through the same executor — the difference is
    # the ``target`` (7F-5a ladder: never CLIP, geometric re-wrap SHRINK).
    if target == "title":
        return _adaptive_layout_title(
            primitive, measure=measure, avail_width=avail_width,
            avail_height=avail_height, constraints=constraints,
            font_size=fs, budget=b,
        )

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
    trace: list[dict] = []
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
        trace.append(_trace_entry("WRAP", result.overflow, len(result.lines), fs))
        if not result.overflow:
            return _finalize(result, reason, RecoveryDecision.WRAP, steps, original, trace)
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
        trace.append(_trace_entry("SHRINK", result.overflow, len(result.lines), cur_fs))
        if not result.overflow:
            return _finalize(result, reason, RecoveryDecision.SHRINK, steps, original, trace)
        reason = classify_reason(
            result, avail_width=avail_width, avail_height=avail_height
        )

    # Stage 3: CLIP (last resort; always keeps overflow=True).  It clips at the
    # font currently in effect (shrunk size if SHRINK ran) so the diagnostics
    # ``final_font_size`` honestly reflects the whole pipeline.
    #
    # 7F-5a: a TOC title (target="title") is NEVER clipped — page_x / page
    # number / title_x are immovable and the leader only shrinks; an overlong
    # title must degrade to explicit PRESERVE_OVERFLOW, never truncation.
    if (
        target != "title"
        and b.allow_clip
        and result.overflow
        and reason is not OverflowReason.PRESERVED_REGION
    ):
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
        trace.append(_trace_entry("CLIP", result.overflow, len(result.lines), cur_fs))
        decision = RecoveryDecision.CLIP
        return _finalize(result, reason, decision, steps, original, trace)

    # Budget exhausted: PRESERVE_OVERFLOW — keep geometry, report overflow.
    # When the result is STILL overflowing after every budgeted stage, the
    # honest decision is PRESERVE_OVERFLOW — never a stale WRAP/SHRINK that
    # implies the budget satisfied it (7F-5a: TOC title reaches here instead
    # of CLIP; diagnostics must say preserve_overflow, not wrap).
    decision = decide_recovery(kind, reason, budget=b, target=target)
    if result.overflow and decision in (
        RecoveryDecision.NO_ACTION, RecoveryDecision.WRAP, RecoveryDecision.SHRINK
    ):
        decision = RecoveryDecision.PRESERVE_OVERFLOW

    return _finalize(result, reason, decision, steps, original, trace)