"""Whitespace-aware adaptive placement — Commit 7G-1.

The first step past pure recovery: where 7F fixes a block *after* it overflows,
this layer decides *before* placement whether a block belongs on the current
page or the next page — consuming only already-settled geometry::

    LayoutResult (line_count / total_height / overflow)      (7C / 7F-4)
        + PageFlowReport (settled placements / resolved_bbox) (7F-8)
            ↓ this module — placement scoring / packing decision ONLY
    PlacementScore / PlacementDecision
        ↓ (7G-2+ executors consume the decision; nothing moves here)

The example this layer answers::

    current page remaining 120pt
    block estimated settled height 150pt
        ↓ decide_placement
    fits? no → worth keeping on this page?
        YES (small overrun, page already reasonably full) → CURRENT_PAGE
        NO  (large overrun / page mostly empty)           → NEXT_PAGE

This is NOT a layout engine: it never re-wraps, never re-shrinks, never moves
a block and never writes geometry into a plan.  It reads the block's settled
height off its :class:`LayoutResult` (``line_count * font_size`` — the same
canonical estimate ``lay_out`` uses for its height check) and the page's free
space off the settled placements' ``resolved_bbox``, then produces a scored
placement decision the 7G-2 executor phase can consume.

Hard rules (enforced by ``tests/test_placement_7g1.py``):

- **never re-lays-out** — no ``lay_out`` / ``adaptive_layout`` / ``wrap_lines``
  / ``shrink_to_fit`` / ``clip_text`` anywhere in this module;
- **never moves a block** — no plan / geometry mutation; a decision is a
  record (target + score + reason), never a write;
- **no detector / parser / renderer / translator** imports;
- **no ``level`` / ``index`` geometry math** — every number comes from a
  settled field;
- **``block_id`` is identity only** — nothing here derives geometry from the
  ``block_id`` string (7F-9.1 discipline).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pdf2zh.semantic.layout.overflow import LayoutResult
from pdf2zh.semantic.layout.page_flow import PRESERVE_KINDS

__all__ = [
    "PlacementTarget",
    "PlacementScore",
    "PlacementDecision",
    "PlacementPolicy",
    "estimate_block_height",
    "remaining_space_for_page",
    "score_fit",
    "decide_placement",
    "decide_from_settled",
]

_TOL = 1e-6


class PlacementTarget(Enum):
    """Where a block should land, decided before placement (7G-1)."""

    CURRENT_PAGE = "current_page"   # keep on the page it is being placed on
    NEXT_PAGE = "next_page"         # target the next page instead
    UNDECIDED = "undecided"         # not enough settled context to judge


@dataclass(frozen=True)
class PlacementScore:
    """The pure numbers behind a placement decision (no policy, no judgment).

    ``needed`` is the block's estimated settled height; ``available`` is the
    remaining space below the page's last settled placement; ``fits`` is the
    raw geometry test; ``shortfall`` / ``fill_ratio`` are derived read-only
    views the decision layer and (later) the packing layer score on.
    """

    needed: float
    available: float
    page_height: float
    line_count: int = 0

    @property
    def fits(self) -> bool:
        return self.needed <= self.available + _TOL

    @property
    def shortfall(self) -> float:
        return max(0.0, round(self.needed - self.available, 2))

    @property
    def fill_ratio(self) -> float:
        """How much of the page the block would occupy (0..1)."""
        if self.page_height <= 0.0:
            return 0.0
        return round(min(1.0, max(0.0, self.needed / self.page_height)), 3)

    def to_dict(self) -> dict:
        return {
            "needed": round(self.needed, 2),
            "available": round(self.available, 2),
            "fits": self.fits,
            "shortfall": self.shortfall,
            "fill_ratio": self.fill_ratio,
            "line_count": int(self.line_count),
        }


@dataclass(frozen=True)
class PlacementPolicy:
    """Knobs for the \"worth keeping on this page?\" judgment (7G-1).

    When a block does not fit the remaining space, it stays on the current
    page only when the overrun is small — bounded by ``max_shortfall_pt`` or
    ``max_overflow_ratio`` of the remaining space — **and** the page is
    already reasonably full (``min_fill_ratio``).  Otherwise the block targets
    the next page instead of spilling past the bottom edge.
    """

    max_shortfall_pt: float = 24.0    # accept an overrun up to 24pt
    max_overflow_ratio: float = 0.2   # ...or up to 20% of the remaining space
    min_fill_ratio: float = 0.35      # page must be >= 35% full to tolerate one


@dataclass(frozen=True)
class PlacementDecision:
    """One scored placement decision (7G-1 output — a record, never a move)."""

    target: PlacementTarget
    reason: str
    score: PlacementScore

    def to_dict(self) -> dict:
        return {
            "target": self.target.value,
            "reason": self.reason,
            "score": self.score.to_dict(),
        }


# ---------------------------------------------------------------------------
# settled inputs — read only, never re-laid-out
# ---------------------------------------------------------------------------


def estimate_block_height(result: LayoutResult) -> tuple[int, float]:
    """``(line_count, total_height)`` read off a SETTLED LayoutResult.

    ``total_height = line_count * font_size`` — the same canonical estimate
    ``lay_out`` itself uses for its height-overflow check, read back from the
    result.  Never re-lays-out, never re-derives from the primitive.
    """
    lines = list(getattr(result, "lines", None) or [])
    fs = float(getattr(result, "font_size", 0.0) or 0.0)
    line_count = len(lines) if lines else (1 if getattr(result, "text", "") else 0)
    return line_count, round(line_count * fs, 2)


def remaining_space_for_page(report, page: int, page_height: float) -> float:
    """Free vertical band below the last settled placement on ``page``.

    v3 y-up: the page bottom edge is 0, so the gap below the reading-order-last
    placement is its resolved bottom edge.  An empty page offers the full page
    height; an extent already past the bottom offers 0.  Pure read of the
    settled placements (7F-8) — no geometry is derived from ``block_id``.
    """
    ph = float(page_height or 0.0)
    if ph <= 0.0:
        return 0.0
    last = None
    for p in (getattr(report, "placements", None) or []):
        if getattr(p, "page", None) == page:
            last = p
    if last is None:
        return ph
    return max(0.0, round(float(last.bottom), 2))


# ---------------------------------------------------------------------------
# scoring + decision — pure math / policy, no geometry writes
# ---------------------------------------------------------------------------


def score_fit(
    needed: float,
    available: float,
    page_height: float,
    line_count: int = 0,
) -> PlacementScore:
    """Pure geometry score for ``needed`` height against ``available`` space."""
    return PlacementScore(
        needed=float(needed or 0.0),
        available=float(available or 0.0),
        page_height=float(page_height or 0.0),
        line_count=int(line_count or 0),
    )


def decide_placement(
    needed: float,
    available: float,
    page_height: float,
    *,
    kind: str = "flow",
    policy: PlacementPolicy | None = None,
    line_count: int = 0,
) -> PlacementDecision:
    """Score a block against a page's remaining space and pick its target.

    Decision matrix (7G-1):

    - no page height → ``UNDECIDED`` (``no_page_height``) — nothing to judge;
    - preserved / immovable kind (code / formula / …) → ``CURRENT_PAGE``
      (``preserved``) — it never moves, whatever the space;
    - ``needed <= available`` → ``CURRENT_PAGE`` (``fits``);
    - small overrun **and** a reasonably full page → ``CURRENT_PAGE``
      (``minor_overflow``) — recovery (7F-8c) may still nudge it, but the
      page is worth keeping rather than wasting it;
    - otherwise → ``NEXT_PAGE`` (``page_overflow``) — a fresh page is the
      better landing point than spilling.

    Returns a :class:`PlacementDecision` record — this function never moves
    a block and never writes to a plan.
    """
    p = policy or PlacementPolicy()
    score = score_fit(needed, available, page_height, line_count=line_count)
    ph = float(page_height or 0.0)
    if ph <= 0.0:
        return PlacementDecision(
            target=PlacementTarget.UNDECIDED, reason="no_page_height", score=score
        )
    if kind in PRESERVE_KINDS:
        return PlacementDecision(
            target=PlacementTarget.CURRENT_PAGE, reason="preserved", score=score
        )
    if score.fits:
        return PlacementDecision(
            target=PlacementTarget.CURRENT_PAGE, reason="fits", score=score
        )
    overrun = score.shortfall
    overrun_tolerable = overrun <= p.max_shortfall_pt or (
        score.available > 0.0
        and overrun <= score.available * p.max_overflow_ratio
    )
    page_fill = 1.0 - score.available / ph if ph > 0.0 else 1.0
    if overrun_tolerable and page_fill >= p.min_fill_ratio:
        return PlacementDecision(
            target=PlacementTarget.CURRENT_PAGE, reason="minor_overflow", score=score
        )
    return PlacementDecision(
        target=PlacementTarget.NEXT_PAGE, reason="page_overflow", score=score
    )


def decide_from_settled(
    result: LayoutResult,
    report,
    page: int,
    page_height: float,
    *,
    kind: str = "flow",
    policy: PlacementPolicy | None = None,
) -> PlacementDecision:
    """End-to-end 7G-1: settled LayoutResult + PageFlowReport → decision.

    Convenience over :func:`estimate_block_height` +
    :func:`remaining_space_for_page` + :func:`decide_placement`.  The settled
    inputs are consumed verbatim; nothing is re-laid-out or moved.
    """
    line_count, total = estimate_block_height(result)
    available = remaining_space_for_page(report, page, page_height)
    return decide_placement(
        total, available, page_height,
        kind=kind, policy=policy, line_count=line_count,
    )
