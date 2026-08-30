"""Global recovery orchestrator — Commit 7F-9.

The document-level recovery pass.  Where 8a–8e built *capabilities*, 7F-9
*orchestrates* the ones that already exist into one bounded, observable loop
and proves that multiple recoveries firing on the same document still
converge instead of fighting each other:::

    settled plan
        ↓ 8b  detect_page_collisions / detect_page_overflows
        ↓ 8c  decide_page_recovery / page_break_from_shift
        ↓ 8d  apply_page_shifts            (same-page SHIFT_DOWN)
        ↓ 8e  execute_continuation_breaks  (cross-page BREAK / continuation)
        ↓ re-diagnose
        ↓ until 0 collisions + 0 overflows, or the budget is exhausted
    final plan + GlobalRecoveryReport (event chain)

The hard rule of 7F-9:

> **orchestrate, never re-implement.** Policy decides once where a block
> belongs; the executors (8d / 8e) are the only writers of geometry; this
> module only sequences them and answers *round, block, reason, action, what it
> triggered next*.

It calls exactly the existing authorities — ``detect_page_collisions`` /
``detect_page_overflows`` (8b), ``decide_page_recovery`` /
``page_break_from_shift`` (8c, via the executors), ``apply_page_shifts`` (8d),
``execute_continuation_breaks`` (8e).  Nothing here calls ``adaptive_layout`` /
``wrap`` / ``shrink`` / ``clip``, no detector / parser / renderer / translator,
no new recovery policy.

Convergence guarantees:

- **bounded** — at most ``max_passes`` rounds (default ``blocks + 1``);
- **no-progress guard** — a round that executes nothing (no shift, no break,
  no split) stops immediately and reports the remaining
  collisions + overflows as **unresolved**, never silently:
  ``3 → 3 → 3 …`` cannot happen;
- **source geometry is the anchor** — the executors only ever change resolved
  ``page`` / ``dst_box.y`` / command ``page`` / ``y``; ``src_box`` and every X
  stay verbatim, so the final plan's source is byte-identical to the input.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from pdf2zh.semantic.layout.page_break_continuation import execute_continuation_breaks
from pdf2zh.semantic.layout.page_flow import (
    detect_page_collisions,
    detect_page_overflows,
    placements_from_plan,
)
from pdf2zh.semantic.layout.page_shift import apply_page_shifts

__all__ = [
    "GlobalRecoveryEvent",
    "GlobalRecoveryReport",
    "global_recovery",
    "source_geometry_snapshot",
]

_TOL = 1e-6


def _state_signature(plan) -> tuple:
    """Stable per-placement resolved-geometry signature for progress detection.

    Two consecutive passes with the same signature executed NO real action:
    no placement moved, no split / whole-block break happened, so the
    collision set is identical.  ``(page, block_index)`` is the structured
    placement identity (7F-9.1); ``resolved_bbox`` is rounded so float noise
    never looks like progress.  This is the orchestrator's real-progress
    contract: progress == collision multiset shrank OR resolved geometry moved.
    """
    keys: list = []
    for p in placements_from_plan(plan):
        r = p.resolved_bbox
        keys.append((
            p.page, p.block_index,
            round(r[0], 6), round(r[1], 6), round(r[2], 6), round(r[3], 6),
        ))
    return tuple(sorted(keys))


def source_geometry_snapshot(plan) -> list:
    """JSON-safe per-entry source-anchor copy for immutability assertions.

    Captures ``src_box`` and, for list / toc, the fixed X anchors (marker_x /
    content_x / continuation_x / title_x / page_x) — everything a recovery
    must NEVER change.
    """
    out: list = []
    for e in plan or []:
        entry = dict(e or {})
        src = entry.get("src_box")
        anchors: dict = {}
        items = entry.get("list_items")
        if isinstance(items, dict):
            for it in items.get("items") or []:
                if not isinstance(it, dict):
                    continue
                for k in ("marker_x", "content_x", "continuation_x"):
                    if k in it:
                        anchors[k] = float(it[k])
        for te in (entry.get("toc_entries") or []) or []:
            if isinstance(te, dict):
                for k in ("title_x", "page_x", "continuation_x"):
                    if k in te:
                        anchors[k] = float(te[k])
        out.append({
            "block_id": entry.get("block_id"),
            "page": int(entry.get("page") or 0),
            "kind": entry.get("kind"),
            "src_box": list(src) if isinstance(src, (list, tuple)) else None,
            "anchors": anchors,
        })
    return out


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlobalRecoveryEvent:
    """One action taken during a recovery round (the explainability chain)."""

    pass_no: int
    block_index: int | None
    page: int
    action: str          # SHIFT_DOWN | BREAK_TO_NEXT_PAGE | CONTINUATION | PRESERVE_OVERFLOW
    detail: str = ""
    kind: str = ""

    def to_dict(self) -> dict:
        return {
            "pass": int(self.pass_no),
            "block": self.block_index,
            "page": int(self.page),
            "kind": self.kind,
            "action": self.action,
            "detail": self.detail,
        }


@dataclass
class GlobalRecoveryReport:
    """Document-level recovery trace (observability, not movement policy)."""

    passes: int = 0
    max_passes: int = 0
    converged: bool = False
    applied: int = 0
    deferred: int = 0
    unresolved: int = 0
    stopped_early: bool = False
    stopped_reason: str = ""         # "" | "no_progress" | "budget_expired"
    events: list = field(default_factory=list)               # GlobalRecoveryEvent
    pass_summaries: list = field(default_factory=list)       # per-round counts

    def to_dict(self) -> dict:
        return {
            "passes": self.passes,
            "max_passes": self.max_passes,
            "converged": self.converged,
            "applied": self.applied,
            "deferred": self.deferred,
            "unresolved": self.unresolved,
            "stopped_early": self.stopped_early,
            "stopped_reason": self.stopped_reason,
            "events": [e.to_dict() for e in self.events],
            "pass_summaries": [dict(s) for s in self.pass_summaries],
        }


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def global_recovery(
    plan,
    page_sizes: Optional[dict] = None,
    *,
    max_passes: Optional[int] = None,
    page_start_y: float = 0.0,
    page_bottom_y: float = 0.0,
) -> tuple[list[dict], GlobalRecoveryReport]:
    """Global bounded recovery pass over a settled plan.

    Each round:

    - **8d phase** — ``apply_page_shifts`` resolves same-page collisions by
      SHIFT_DOWN (NEXT_PAGE / PRESERVE are deferred, never shifted off-page);
    - **8e phase** — ``execute_continuation_breaks`` turns overflow / deferred
      NEXT_PAGE into cross-page moves: list / toc continuation splits, flow
      whole-block breaks, and PRESERVE_OVERFLOW for code / preserved regions;
    - **re-diagnose** — count the remaining collisions + overflows; if 0 the
      document has converged.

    Stops when all recoveries execute (``max_passes``, default ``blocks + 1``)
    or a round executes nothing (no-progress guard).  Returns a NEW deep-copied
    plan and a :class:`GlobalRecoveryReport`.  The source plan is never mutated;
    source geometry is byte-identical in the output.
    """
    sizes = dict(page_sizes or {})
    new_plan = copy.deepcopy(list(plan or []))
    n_blocks = len(placements_from_plan(new_plan))
    bound = max(1, int(max_passes) if max_passes is not None else n_blocks + 1)
    report = GlobalRecoveryReport(max_passes=bound)

    for pass_no in range(1, bound + 1):
        # capture the state BEFORE the round — *against* this we measure real
        # progress (geometry actually moved / collision set shrank).
        before_sig = _state_signature(new_plan)

        # ── 8d: same-page SHIFT_DOWN ─────────────────────────────────────
        shifted, shift_rep = apply_page_shifts(new_plan, page_sizes=sizes)
        for d in shift_rep.applied:
            report.events.append(GlobalRecoveryEvent(
                pass_no=pass_no, block_index=d.block_index, page=d.page,
                action="SHIFT_DOWN", kind="",
                detail=f"down {float(d.shift_y):.2f}pt"))
            report.applied += 1

        # ── 8e: cross-page BREAK / continuation / preserve ───────────────
        broken, cont_rep = execute_continuation_breaks(
            shifted, page_sizes=sizes,
            page_start_y=page_start_y, page_bottom_y=page_bottom_y)
        for r in cont_rep.applied:
            if r.mode == "split":
                action = "CONTINUATION"
            elif r.mode == "whole_block":
                action = "BREAK_TO_NEXT_PAGE"
            else:
                action = r.mode
            report.events.append(GlobalRecoveryEvent(
                pass_no=pass_no, block_index=r.block_index, page=r.source_page,
                action=action, kind=r.kind,
                detail=f"{r.kind} -> page {r.target_page} (lines moved {r.moved_lines})"))
            report.applied += 1
        for r in cont_rep.deferred:
            if r.mode != "preserve":
                continue
            report.events.append(GlobalRecoveryEvent(
                pass_no=pass_no, block_index=r.block_index, page=r.source_page,
                action="PRESERVE_OVERFLOW", kind=r.kind, detail="immovable"))
            report.deferred += 1

        new_plan = broken

        # ── re-diagnose convergence ──────────────────────────────────────
        collisions = detect_page_collisions(new_plan)
        overflows = detect_page_overflows(new_plan, page_sizes=sizes)
        total = len(collisions) + len(overflows)
        report.pass_summaries.append({
            "pass": pass_no,
            "collision_count": total,
            "collisions": len(collisions),
            "page_overflow": len(overflows),
            "shifts": len(shift_rep.applied),
            "breaks": len(cont_rep.applied),
        })
        report.passes = pass_no

        if total == 0:
            report.converged = True
            break

        # Real progress = the round changed resolved geometry.  If a round
        # executes only no-ops (zero-shift SHIFT_DOWN, deferred PRESERVE),
        # the signature — and therefore the collision set — is byte-identical
        # to the previous round: "3 → 3 → 3 …" cannot burn the budget.
        if _state_signature(new_plan) == before_sig:
            report.stopped_early = True
            report.stopped_reason = "no_progress"
            break

    # budget / no-progress exit with leftovers → unresolved
    if not report.converged:
        if not report.stopped_reason:
            report.stopped_reason = "budget_expired"
        report.unresolved = len(detect_page_collisions(new_plan)) + len(
            detect_page_overflows(new_plan, page_sizes=sizes))

    return new_plan, report