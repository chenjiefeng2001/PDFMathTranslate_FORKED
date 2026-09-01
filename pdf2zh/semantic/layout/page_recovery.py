"""Cross-block recovery decision — Commit 7F-8c.

Turns the 8b collision **fact layer** into a **decision contract** without
executing anything::

    resolved_bbox
        ↓ page_flow (8b)
    PageCollision / PageOverflow
        ↓ this module (8c — decision only)
    BlockShiftDecision
        ↓ page_recovery executor (7F-8d — later commits)

The single architecture rule of this commit:

> 选择“原地让位”还是“下一页”，而不是执行移动。

The decision ladder (the 8c Recovery Matrix) is::

    immovable geometry involved (preserved region / TOC page column)
        → PRESERVE_OVERFLOW                     (never shift)
    shift would push the lower block past the page bottom
        → NEXT_PAGE
    otherwise
        → SHIFT_DOWN(required_shift)
    no collision
        → KEEP

``shift_y`` is consumed **verbatim** from the authoritative 8b
:class:`PageCollision.required_shift` — this layer never re-derives it
(``lower.top - upper.bottom`` is never recomputed here; that would create a
second geometry authority).  Only Y is ever considered: a block moves down as
a whole, so List ``marker_x`` / ``content_x`` / ``continuation_x`` and TOC
``title_x`` / ``page_x`` / ``continuation_x`` are untouched by construction.

Hard rules (enforced by ``tests/test_page_recovery_7f8c.py``):

- **decision-only** — never moves a block, never modifies geometry, never
  writes back into the plan, never re-lays-out (no ``lay_out`` /
  ``adaptive_layout``), never calls wrap/shrink/clip;
- **no detector / parser / renderer imports** — block kinds come from the
  settled 8b placements verbatim;
- **no ``level`` / ``index`` geometry math**;
- **Code / PreservedRegion → PRESERVE_OVERFLOW, never SHIFT_DOWN**;
- **TOC page column → PRESERVE_OVERFLOW** (page_x never moves); a TOC block
  as a whole may SHIFT_DOWN because only Y changes;
- **page overflow → explicit NEXT_PAGE** — never a silent SHIFT_DOWN off-page.

Nothing here changes any PDF output: the renderer never sees this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pdf2zh.semantic.layout.page_flow import (
    BlockPlacement,
    PageCollision,
    build_page_flow_report,
)

__all__ = [
    "PageRecoveryDecision",
    "BlockShiftDecision",
    "decide_block_shift",
    "decide_page_recovery",
    "keep_decision",
    "decision_summary",
]

_TOL = 1e-6


class PageRecoveryDecision(Enum):
    """What to do about a cross-block collision (7F-8c).

    Decision vocabulary only — nothing here moves anything; the executor
    (7F-8d) is the only place a decision becomes geometry.
    """

    KEEP = "keep"  # no collision — nothing to do
    SHIFT_DOWN = "shift_down"  # move the lower block down by shift_y
    NEXT_PAGE = "next_page"  # shift would overflow the page — continue on the next page
    PRESERVE_OVERFLOW = (
        "preserve_overflow"  # immovable geometry involved — keep, report overflow
    )


def _collision_snapshot(collision: PageCollision) -> dict:
    """Light JSON-safe snapshot of the 8b diagnosis for the recovery trace.

    Carries the block indexes plus the authoritative overlap / required_shift /
    bbox_mode — exactly the numbers that answer "与哪个 block 碰撞了多少 pt".
    """
    return {
        "upper": collision.upper.block_index,
        "lower": collision.lower.block_index,
        "overlap": round(float(collision.overlap), 2),
        "required_shift": round(float(collision.required_shift), 2),
        "bbox_mode": collision.bbox_mode,
    }


@dataclass(frozen=True)
class BlockShiftDecision:
    """One decision about how to resolve a cross-block collision (8c).

    ``shift_y`` is consumed verbatim from the 8b ``PageCollision.required_shift``
    (the single geometry authority) — never recomputed here.  ``source_bbox`` /
    ``resolved_bbox`` are the moving (lower) block's settled boxes, copied for
    the trace.  ``collision`` is the 8b diagnosis snapshot (``None`` for KEEP).
    """

    block_index: int
    page: int
    decision: PageRecoveryDecision
    shift_y: float
    reason: str
    source_bbox: tuple
    resolved_bbox: tuple
    target: str = "lower"
    collision: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "block_index": self.block_index,
            "target": self.target,
            "collision": self.collision,
            "recovery": {
                "decision": self.decision.value,
                "shift_y": round(float(self.shift_y), 2),
                "reason": self.reason,
            },
        }


def decide_block_shift(
    collision: PageCollision,
    *,
    page_height: Optional[float] = None,
    tolerance: float = _TOL,
) -> BlockShiftDecision:
    """Decide how to resolve one 8b :class:`PageCollision` (the Recovery Matrix).

    Args:
        collision: the 8b diagnosis — its ``required_shift`` is authoritative.
        page_height: the page's height in the v3 y-up space (bottom edge 0);
            the lower block's resolved bottom minus ``required_shift`` must
            stay at or above the page bottom, otherwise → ``NEXT_PAGE``.
            ``None`` skips the boundary check (→ ``SHIFT_DOWN``).

    Returns:
        A :class:`BlockShiftDecision` — decision only; nothing moves.
    """
    lower = collision.lower
    shift = float(collision.required_shift or 0.0)

    # hard boundary: immovable geometry (preserved region / TOC page column)
    if collision.reason == "preserved_region":
        decision = PageRecoveryDecision.PRESERVE_OVERFLOW
        shift = 0.0
    else:
        ph = float(page_height) if page_height is not None else 0.0
        if ph > 0.0 and lower.resolved_bbox[1] - shift < -tolerance:
            decision = PageRecoveryDecision.NEXT_PAGE
        else:
            decision = PageRecoveryDecision.SHIFT_DOWN

    return BlockShiftDecision(
        block_index=lower.block_index,
        page=collision.page,
        decision=decision,
        shift_y=shift,
        reason=collision.reason,
        source_bbox=lower.bbox,
        resolved_bbox=lower.resolved_bbox,
        target="lower",
        collision=_collision_snapshot(collision),
    )


def keep_decision(placement: BlockPlacement) -> BlockShiftDecision:
    """KEEP — a placement with no collision needs no recovery (matrix row)."""
    return BlockShiftDecision(
        block_index=placement.block_index,
        page=placement.page,
        decision=PageRecoveryDecision.KEEP,
        shift_y=0.0,
        reason="none",
        source_bbox=placement.bbox,
        resolved_bbox=placement.resolved_bbox,
        target="lower",
        collision=None,
    )


def decide_page_recovery(
    plan,
    page_sizes: Optional[dict] = None,
) -> list[BlockShiftDecision]:
    """One decision per resolved collision of a settled plan (7F-8c).

    Uses the 8b report (``build_page_flow_report``) — the collision /
    ``required_shift`` authority — and turns each collision into a decision.
    Only non-KEEP decisions are emitted (KEEP is the implicit absence of a
    collision).  Never moves anything; never re-lays-out.
    """
    sizes = dict(page_sizes or {})
    report = build_page_flow_report(plan, page_sizes=sizes)
    return [
        decide_block_shift(c, page_height=sizes.get(c.page)) for c in report.collisions
    ]


def decision_summary(decisions: list[BlockShiftDecision]) -> dict:
    """Counts per decision kind for ``debug/layout.json`` (7F-8c-4)."""
    by_decision: dict[str, int] = {}
    for d in decisions:
        by_decision[d.decision.value] = by_decision.get(d.decision.value, 0) + 1
    return {"total": len(decisions), "by_decision": dict(sorted(by_decision.items()))}
