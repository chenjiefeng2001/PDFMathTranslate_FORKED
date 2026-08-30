"""Block shift executor — Commit 7F-8d.

Executes the 8c :class:`BlockShiftDecision` — the first phase of 7F-8 that
actually changes final layout geometry — and re-validates the page flow::

    resolved_bbox            (8b)
        ↓ PageCollision      (8b)
        ↓ BlockShiftDecision (8c)
        ↓ this module        (8d — the ONLY place a decision becomes geometry)
    resolved_bbox'
        ↓ 8b detect again
    collision gone (before: N → after: 0)

Scope of 7F-8d (hard rules, enforced by ``tests/test_page_shift_7f8d.py``):

- **only SHIFT_DOWN is executed** — ``KEEP`` / ``PRESERVE_OVERFLOW`` leave the
  block untouched; ``NEXT_PAGE`` is **deferred** (7F-8e), never executed here;
- **only Y changes** — ``x0`` / ``x1`` / ``width`` are immutable; the shift is
  a pure translation of the resolved bbox (and, in the plan, of the draw
  commands' ``y``);
- **source geometry never changes** — ``BlockPlacement.bbox`` (and the plan's
  ``src_box``) stay forever; only ``resolved_bbox`` / ``dst_box`` move;
- **the shift direction contract** — ``shift_y`` is positive = distance toward
  the page bottom; the executor converts it into the actual coordinate change
  (v3 y-up: decreasing y) in one explicit place (:func:`shift_box_down`);
- **bounded cascade** — shifts are re-detected and re-decided in passes
  (``detect → decide → shift → detect again``), at most ``max_passes``
  (default ``number_of_placements + 1``); a pass that cannot progress
  (only NEXT_PAGE / PRESERVE_OVERFLOW left) stops early; remaining collisions
  are recorded as unresolved — their final stance is PRESERVE_OVERFLOW, never
  a silent off-page shift;
- **pure read of the settled plan** — no ``lay_out`` / ``adaptive_layout``,
  no wrap/shrink/clip, no detector / parser / renderer, no ``level`` /
  ``index`` geometry math;
- **``required_shift`` comes from 8c** (which consumed it from 8b) — this
  module never re-derives it.

Nothing here imports or touches the renderer, converter or ONNX: the executor
produces shifted geometry; the renderer stays a draw-only consumer.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional, Sequence

from pdf2zh.semantic.layout.page_flow import (
    BlockPlacement,
    PageCollision,
    detect_collisions_from_placements,
    placements_from_plan,
)
from pdf2zh.semantic.layout.page_recovery import (
    BlockShiftDecision,
    PageRecoveryDecision,
    decide_block_shift,
)

__all__ = [
    "shift_box_down",
    "apply_block_shift",
    "resolve_page_shifts",
    "apply_page_shifts",
    "block_deltas",
    "ShiftExecutionReport",
]

_TOL = 1e-6


# ---------------------------------------------------------------------------
# 8d-1 — pure geometry shift
# ---------------------------------------------------------------------------


def shift_box_down(box, delta_y: float) -> tuple:
    """Shift a v3 y-up box ``(x0, y0, x1, y1)`` toward the page bottom.

    ``delta_y`` is the page-down distance (positive).  In the v3 y-up space
    the bottom edge is 0 and "down" means decreasing y, so the executor
    subtracts — this conversion lives in exactly one place.  X is never
    touched: the box's width / horizontal position are immutable.
    """
    d = float(delta_y or 0.0)
    return (
        float(box[0]),
        round(float(box[1]) - d, 2),
        float(box[2]),
        round(float(box[3]) - d, 2),
    )


def apply_block_shift(
    placement: BlockPlacement,
    decision: BlockShiftDecision,
) -> BlockPlacement:
    """Execute one 8c decision on a placement (8d-1).

    - ``SHIFT_DOWN`` → a NEW placement whose ``resolved_bbox`` is shifted down
      by ``decision.shift_y`` (page-down positive);
    - ``KEEP`` / ``PRESERVE_OVERFLOW`` / ``NEXT_PAGE`` → unchanged (8d only
      executes SHIFT_DOWN; NEXT_PAGE is deferred to 7F-8e);
    - ``bbox`` (source geometry) is never touched; x never changes.

    Returns a NEW :class:`BlockPlacement` — the input is never mutated.

    Raises:
        ValueError: when the shifted box would cross the page bottom edge
            (v3 y-up: bottom edge 0).  That situation must have been decided
            NEXT_PAGE — 8d never silently shifts a block off-page.  The guard
            is what makes a missing-page-context decision fail loud instead of
            producing broken geometry.
    """
    if decision.decision is not PageRecoveryDecision.SHIFT_DOWN:
        return placement
    shifted = shift_box_down(placement.resolved_bbox, decision.shift_y)
    if shifted[1] < -_TOL:
        raise ValueError(
            f"shift {decision.shift_y} on p{placement.page} block "
            f"{placement.block_index} would cross the page bottom — "
            "this must have been decided NEXT_PAGE, never SHIFT_DOWN"
        )
    return BlockPlacement(
        block_index=placement.block_index,
        page=placement.page,
        kind=placement.kind,
        bbox=placement.bbox,
        resolved_bbox=shifted,
        height=shifted[3] - shifted[1],
        preserved=placement.preserved,
        has_continuation=placement.has_continuation,
    )


# ---------------------------------------------------------------------------
# 8d-2 — bounded cascade (detect → decide → shift → detect again)
# ---------------------------------------------------------------------------


@dataclass
class ShiftExecutionReport:
    """What the 8d cascade actually did (observability, not movement policy)."""

    passes: int = 0
    max_passes: int = 0
    stopped_early: bool = False
    applied: list = field(default_factory=list)       # executed SHIFT_DOWN decisions
    deferred: list = field(default_factory=list)      # final decisions NOT executed
    unresolved: list = field(default_factory=list)    # final PageCollision records

    def summary(self) -> dict:
        return {
            "passes": self.passes,
            "max_passes": self.max_passes,
            "stopped_early": self.stopped_early,
            "applied_count": len(self.applied),
            "deferred_count": len(self.deferred),
            "unresolved_count": len(self.unresolved),
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "applied": [d.to_dict() for d in self.applied],
            "deferred": [d.to_dict() for d in self.deferred],
            "unresolved": [c.to_dict() for c in self.unresolved],
        }


def _group_by_page(placements):
    pages: dict[int, list[BlockPlacement]] = {}
    for p in placements:
        pages.setdefault(p.page, []).append(p)
    return pages


def resolve_page_shifts(
    plan,
    page_sizes: Optional[dict] = None,
    *,
    max_passes: Optional[int] = None,
) -> tuple[list[BlockPlacement], ShiftExecutionReport]:
    """Bounded cascade: resolve all SHIFT_DOWN decisions on a settled plan.

    Each pass (at most ``max_passes``, default ``len(placements) + 1``):

    - detect resolved collisions on the **current** placements (8b authority);
    - decide each collision (8c authority — ``required_shift`` consumed, never
      recomputed);
    - apply every ``SHIFT_DOWN`` simultaneously (a sweep); NEXT_PAGE and
      PRESERVE_OVERFLOW are never applied.

    A pass with zero applicable shifts cannot make progress → stops early.
    After the budget, any remaining collisions are recorded as unresolved
    (their final stance is PRESERVE_OVERFLOW — 8d never moves them off-page).

    Returns:
        ``(final_placements, report)`` — final placements in the SAME reading
        order as ``placements_from_plan(plan)``, plus the execution report.
        Never mutates the plan.
    """
    sizes = dict(page_sizes or {})
    initial = placements_from_plan(plan)
    order = [(p.page, p.block_index) for p in initial]
    current = {key: p for key, p in zip(order, initial)}
    bound = max_passes if max_passes is not None else len(initial) + 1
    report = ShiftExecutionReport(max_passes=max(bound, 1))

    for i in range(bound):
        collisions = detect_collisions_from_placements(
            list(current.values())
        )
        if not collisions:
            break
        decisions = [
            decide_block_shift(c, page_height=sizes.get(c.page))
            for c in collisions
        ]
        applicable = [
            d for d in decisions
            if d.decision is PageRecoveryDecision.SHIFT_DOWN
        ]
        if not applicable:
            report.stopped_early = True
            break
        for d in applicable:
            key = (d.page, d.block_index)
            current[key] = apply_block_shift(current[key], d)
        report.applied.extend(applicable)
        report.passes = i + 1
    else:  # budget exhausted with collisions still present
        pass

    final = [current[k] for k in order]
    report.unresolved.extend(
        detect_collisions_from_placements(list(current.values()))
    )
    report.deferred.extend(
        decide_block_shift(c, page_height=sizes.get(c.page))
        for c in report.unresolved
    )
    return final, report


def block_deltas(
    initial: Sequence[BlockPlacement],
    final: Sequence[BlockPlacement],
) -> dict:
    """Page-down distance per ``(page, block_index)`` between two placement
    sets (positive = moved down).  Zero-delta blocks are omitted."""
    out: dict[tuple, float] = {}
    for a, b in zip(initial, final):
        delta = a.resolved_bbox[1] - b.resolved_bbox[1]
        if abs(delta) > _TOL:
            out[(a.page, a.block_index)] = round(float(delta), 2)
    return out


# ---------------------------------------------------------------------------
# 8d-3 — settled render plan wiring (the ONLY place a decision hits a plan)
# ---------------------------------------------------------------------------


def _shift_commands_y(cmds, delta: float) -> None:
    """Shift every positioned command's ``y`` down (in place)."""
    if not isinstance(cmds, list):
        return
    for c in cmds:
        if isinstance(c, dict) and isinstance(c.get("y"), (int, float)):
            c["y"] = round(float(c["y"]) - delta, 2)


def _shift_entry_down(entry: dict, delta: float) -> None:
    """Apply a page-down delta to one deep-copied plan entry.

    Moves ONLY the final draw position: ``dst_box`` and the settled payload
    commands' ``y`` (plus the legacy list_items / toc_commands command copies
    the host renderer may fall back to).  ``src_box``, all x / width /
    font_size / text and the semantic payload fields stay untouched.

    ``render_payload.commands`` and the compat ``list_items`` /
    ``toc_commands`` copies often alias the SAME list (both are built from the
    one TranslationUnit payload) — and ``deepcopy`` preserves that identity,
    so the dedup guard below prevents shifting the same commands twice.
    """
    dst = entry.get("dst_box")
    if isinstance(dst, list) and len(dst) == 4:
        entry["dst_box"] = [
            dst[0], round(float(dst[1]) - delta, 2),
            dst[2], round(float(dst[3]) - delta, 2),
        ]
    shifted: set[int] = set()
    payload = entry.get("render_payload")
    if isinstance(payload, dict):
        cmds = payload.get("commands")
        _shift_commands_y(cmds, delta)
        if isinstance(cmds, list):
            shifted.add(id(cmds))
    for key in ("list_items", "toc_commands"):
        obj = entry.get(key)
        if isinstance(obj, dict):
            cmds = obj.get("commands")
            if id(cmds) in shifted:
                continue
            _shift_commands_y(cmds, delta)
            shifted.add(id(cmds))


def apply_page_shifts(
    plan,
    page_sizes: Optional[dict] = None,
    *,
    max_passes: Optional[int] = None,
) -> tuple[list[dict], ShiftExecutionReport]:
    """Shift the settled render plan per the executed 8c decisions (8d-3).

    Returns ``(new_plan, report)``: a NEW deep-copied plan whose entries have
    only the Y of their final draw position changed — ``dst_box`` and the
    payload commands' ``y`` (draw position).  ``src_box``, every x / width /
    font_size / text and all semantic fields are byte-identical.  The input
    plan is never mutated.  Re-running 8b on the returned plan reports the
    resolved collisions that the shifts cleared (before: N → after: 0).
    """
    initial = placements_from_plan(plan)
    final, report = resolve_page_shifts(plan, page_sizes, max_passes=max_passes)
    deltas = block_deltas(initial, final)
    new_plan = copy.deepcopy(list(plan or []))
    # Entry ↔ placement pairing is positional: both ``placements_from_plan``
    # and the deepcopy preserve the settled plan's reading order, so each
    # entry is matched to the placement it came from (7F-9.1).  ``block_id``
    # is never re-parsed here — the placement's structured ``block_index``
    # is the only identity used for the delta lookup.
    it = iter(initial)
    for entry in new_plan:
        if not isinstance(entry, dict):
            continue
        placement = next(it)
        delta = deltas.get((placement.page, placement.block_index), 0.0)
        if abs(delta) > _TOL:
            _shift_entry_down(entry, delta)
    return new_plan, report
