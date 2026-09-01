"""Layout-level recovery audit — Commit 7F-6c-4.

Independent, per-kind **recovery-behavior** metrics computed over the unified
7F-6a :class:`~pdf2zh.semantic.layout.contract.LayoutResultLike` contract (and
the raw List / TOC aggregates).  Unlike the 7D/7F-5d PDF evaluator (which
reads *rendered* geometry), this audit reads the *recovery record* the layout
executor produced — the renderer is draw-only and never sees these.

    Flow  → audit_recovery(result)     (atomic LayoutResult)
    Code  → audit_recovery(result)     (PRESERVE — must never carry recovery)
    List  → audit_list(aggregate)      (marker channel must stay clean)
    TOC   → audit_toc(aggregate)       (page channel must stay clean; title never CLIP)

Every metric is individually readable; none is synthesized into a single
score (7F-6c: "每个指标独立，不要合成一个 score"):

- ``recovery_policy_integrity`` — executed steps are consistent with the
  recorded decision: no ``overflow=True`` with a stale ``no_action`` / ``wrap``
  decision; no CLIP on a TOC title; decision ∈ the kind's ladder.
- ``recovery_bounded`` — ``len(steps)`` within the kind's hard bound (generic
  kinds ≤ 3 = WRAP+SHRINK+CLIP; TOC title ≤ 1 + max shrink steps).
- ``flow_recovery_integrity`` — 1.0 for an atomic flow result whose recovery
  obeys the flow ladder; ``code_recovery_integrity`` — 1.0 only when a
  preserved region never ran recovery.
- ``list_recovery_integrity`` — 1.0 when the marker channel never carried
  recovery and every content / continuation channel is policy-consistent and
  bounded.
- ``list_recovery_steps`` / ``toc_recovery_steps`` — the maximum steps any
  adaptive channel executed (raw observable, 0 = no recovery).
- ``list_recovery_font_size`` / ``toc_recovery_font_size`` — min
  ``final/original`` over adaptive channels (1.0 when nothing shrank).
- ``toc_recovery_overflow`` — 1.0 when an over-budget entry reports overflow
  honestly, 0.0 when it silently fits.

This module imports no detector, parser, renderer, or translator — it is a
pure read of already-settled layout results.
"""

from __future__ import annotations

from typing import Any

from pdf2zh.semantic.layout.contract import as_layout_result

__all__ = [
    "audit_recovery",
    "audit_flow",
    "audit_code",
    "audit_list",
    "audit_toc",
]

#: Decisions that are *stale* when the result is still overflowing (7F-6b: the
#: record must reflect what actually ran, never a leftover wrap/no_action).
_STALE_DECISIONS = {"no_action", "wrap"}

#: Hard step bound per kind — generic ladder is WRAP(1) → SHRINK(1) → CLIP(1);
#: the TOC-title SHRINK descends geometrically (bounded by the executor).
_STEP_BOUND = {"toc": 9, "toc_title": 9}
_DEFAULT_STEP_BOUND = 3

#: Decision ladders per primitive kind (mirrors recovery.decide_recovery).
_LADDERS: dict[str, set[str]] = {
    "preserved": {"preserve_overflow"},
    "column": {"preserve_overflow"},
    "toc": {"no_action", "wrap", "shrink", "preserve_overflow"},  # never CLIP
    "toc_title": {"no_action", "wrap", "shrink", "preserve_overflow"},
}
_DEFAULT_LADDER = {"no_action", "wrap", "shrink", "clip", "preserve_overflow"}


def _channels_of(aggregate: Any, kind: str) -> list[Any]:
    """Settled channel results of a List / TOC aggregate, reading order."""
    if kind == "list":
        out: list[Any] = [getattr(aggregate, "marker", None)]
        content = getattr(aggregate, "content", None)
        if content is not None:
            out.append(content)
        out.extend(list(getattr(aggregate, "continuation", None) or []))
    else:
        out = []
        for name in ("number", "title", "leader", "page"):
            v = getattr(aggregate, name, None)
            if v is not None:
                out.append(v)
        out.extend(list(getattr(aggregate, "continuation", None) or []))
    return [c for c in out if c is not None]


def _decision_ok(r: Any) -> bool:
    """Policy consistency of one channel result."""
    if not getattr(r, "overflow", False):
        return True
    d = getattr(r, "recovery_decision", None)
    if not d:
        return False
    if d in _STALE_DECISIONS:
        return False
    kind = str(getattr(r, "primitive_kind", "") or "")
    return d in _LADDERS.get(kind, _DEFAULT_LADDER)


def _font_ratio(channels: list[Any]) -> float:
    """min(final/original) over adaptive channels; 1.0 when nothing shrank."""
    ratios = []
    for r in channels:
        final = getattr(r, "font_size", None)
        orig = getattr(r, "original_font_size", None)
        if orig is None:
            orig = final  # no recovery → ratio 1.0
        try:
            final = float(final or 0.0)
            orig = float(orig or 0.0)
        except (TypeError, ValueError):
            continue
        if final > 0.0 and orig > 0.0:
            ratios.append(final / orig)
    return min(ratios) if ratios else 1.0


def _bounded(r: Any) -> bool:
    kind = str(getattr(r, "primitive_kind", "") or "")
    bound = _STEP_BOUND.get(kind, _DEFAULT_STEP_BOUND)
    return len(getattr(r, "recovery_steps", None) or []) <= bound


def audit_recovery(result: Any) -> dict:
    """Shared per-result recovery audit over the unified contract.

    Args:
        result: any settled layout result (atomic ``LayoutResult`` or a List /
            TOC aggregate — both expose ``LayoutResultLike`` via the contract).

    Returns:
        ``{recovery_policy_integrity, recovery_bounded, decision, steps,
        overflow}`` — the first two are 0/1 gates; the rest are raw records.
    """
    view = as_layout_result(result)
    rec = view.recovery or {}
    decision = rec.get("decision")
    steps = list(rec.get("steps") or [])
    overflow = bool(view.overflow)
    kind = str(view.primitive_kind or "")

    ok = True
    if overflow and decision in _STALE_DECISIONS:
        ok = False
    if decision == "clip" and kind in ("toc", "toc_title"):
        ok = False
    if decision is not None and decision not in _LADDERS.get(kind, _DEFAULT_LADDER):
        ok = False
    if len(steps) > _STEP_BOUND.get(kind, _DEFAULT_STEP_BOUND):
        ok = False
    return {
        "recovery_policy_integrity": 1.0 if ok else 0.0,
        "recovery_bounded": (
            1.0 if len(steps) <= _STEP_BOUND.get(kind, _DEFAULT_STEP_BOUND) else 0.0
        ),
        "decision": decision,
        "steps": steps,
        "overflow": overflow,
    }


def audit_flow(result: Any) -> dict:
    """Flow result: 1.0 when the recovery obeys the flow ladder."""
    return {
        "flow_recovery_integrity": audit_recovery(result)["recovery_policy_integrity"],
        "recovery_bounded": audit_recovery(result)["recovery_bounded"],
        "recovery_steps": int(len(audit_recovery(result)["steps"])),
    }


def audit_code(result: Any) -> dict:
    """Code (PreservedRegion): 1.0 only when it never ran recovery."""
    steps = getattr(result, "recovery_steps", None) or []
    decision = getattr(result, "recovery_decision", None)
    clean = not steps and not decision
    return {
        "code_recovery_integrity": 1.0 if clean else 0.0,
        "recovery_steps": int(len(steps)),
    }


def audit_list(aggregate: Any) -> dict:
    """Independent list recovery metrics over a :class:`ListLayoutResult`.

    The **marker** channel must never carry recovery (marker is PRESERVE);
    content / continuation channels must be policy-consistent and bounded.
    """
    channels = _channels_of(aggregate, "list")
    marker = getattr(aggregate, "marker", None)
    adaptive = [c for c in channels if c is not marker and c is not None]
    marker_clean = bool(
        not marker
        or not (
            getattr(marker, "recovery_decision", None)
            or getattr(marker, "recovery_steps", None)
        )
    )
    steps_max = max(
        (len(getattr(r, "recovery_steps", None) or []) for r in adaptive), default=0
    )
    integrity = bool(
        marker_clean
        and all(_decision_ok(r) for r in adaptive)
        and all(_bounded(r) for r in adaptive)
    )
    return {
        "list_recovery_integrity": 1.0 if integrity else 0.0,
        "list_recovery_steps": int(steps_max),
        "list_recovery_font_size": round(_font_ratio(adaptive), 4),
    }


def audit_toc(aggregate: Any) -> dict:
    """Independent TOC recovery metrics over a :class:`TocEntryLayoutResult`.

    The **page** channel must never carry recovery (FixedColumn PRESERVE) and
    the **title** must never CLIP; an over-budget entry reports overflow
    honestly.
    """
    channels = _channels_of(aggregate, "toc")
    title = getattr(aggregate, "title", None)
    page = getattr(aggregate, "page", None)
    adaptive = [c for c in channels if c is not page and c is not None]
    page_clean = bool(
        not page
        or not (
            getattr(page, "recovery_decision", None)
            or getattr(page, "recovery_steps", None)
        )
    )
    title_no_clip = bool(
        not title or "CLIP" not in (getattr(title, "recovery_steps", None) or [])
    )
    steps_max = max(
        (len(getattr(r, "recovery_steps", None) or []) for r in adaptive), default=0
    )
    overflow = bool(getattr(aggregate, "overflow", False))
    honest = bool(
        (not overflow)
        or any(getattr(r, "overflow", False) for r in adaptive)
        or steps_max > 0
    )
    integrity = bool(
        page_clean and title_no_clip and all(_decision_ok(r) for r in adaptive)
    )
    return {
        "toc_recovery_integrity": 1.0 if integrity else 0.0,
        "toc_recovery_steps": int(steps_max),
        "toc_recovery_font_size": round(_font_ratio(adaptive), 4),
        "toc_recovery_overflow": 1.0 if honest else 0.0,
    }
