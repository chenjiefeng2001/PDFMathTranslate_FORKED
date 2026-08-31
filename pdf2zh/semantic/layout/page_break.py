"""Page break recovery contract — Commit 7F-8e-1.

Pins the *semantics* of crossing a page boundary BEFORE any executor moves a
block.  8e is the cross-page counterpart of 8d (same-page shift)::

    8b 事实 → 8c 决策 → 8d 同页执行 (SHIFT_DOWN) → 8e 跨页执行 (NEXT_PAGE)

This module is decision / contract only — nothing here touches a plan, a
renderer, or any geometry beyond computing what a break *would* look like:

- :class:`PageBreakDecision` — ``KEEP`` / ``BREAK_TO_NEXT_PAGE`` /
  ``PRESERVE_OVERFLOW``;
- :func:`decide_page_break` — does the settled placement fit its page?
  (consumes ONLY the settled ``resolved_bbox`` — height is never recomputed;
  LayoutResult stays the geometry authority);
- :func:`page_break_from_shift` — consumes an 8c ``BlockShiftDecision``
  directly, never re-judging the collision;
- :func:`break_placement_to_page` / :func:`next_page_start_y` — the
  **next-page start position** contract: only ``page`` and ``y`` change;
- :func:`break_invariants` / :func:`assert_break_invariants` — the
  List / TOC continuation invariants a break must preserve (marker_x /
  content_x / continuation_x / title_x / page_x byte-identical; the TOC page
  number drawn exactly once and never folded into a continuation line);
- :func:`next_free_page` — monotonic page-chain assignment (A→0, B→1, C→2,
  never all on one page, never an unbounded page stream).

Hard rules (enforced by ``tests/test_page_break_7f8e.py``):

- **8e decides \"which page\", never recomputes X** — no ``new_x`` /
  ``content_x`` / ``title_x`` / ``marker_x`` / ``page_x`` derivation;
- **Code / PreservedRegion → PRESERVE_OVERFLOW** — never split, never moved;
- **first version is whole-block breaks only** — no paragraph / line-level
  splitting (continuation splitting is a later 8e step);
- **no detector / parser / renderer / translator imports**, no
  ``lay_out`` / ``adaptive_layout`` / wrap/shrink/clip, no ``level`` /
  ``index`` geometry math;
- **pure** — every function returns records / placements; the plan is never
  mutated here (the executor in 8e-2 is the only writer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from pdf2zh.semantic.layout.page_flow import (
    PRESERVE_KINDS,
    BlockPlacement,
)
from pdf2zh.semantic.layout.page_recovery import (
    BlockShiftDecision,
    PageRecoveryDecision,
)

__all__ = [
    "PageBreakDecision",
    "PageBreakExecution",
    "PageBreakReport",
    "decide_page_break",
    "decide_page_breaks",
    "page_break_from_shift",
    "next_page_start_y",
    "break_placement_to_page",
    "page_break_execution",
    "next_free_page",
    "last_page_index",
    "break_invariants",
    "assert_break_invariants",
]

_TOL = 1e-6

#: X anchors a page break must leave byte-identical, per block kind (8e never
#: recomputes X — these are read verbatim from the settled payload).
_BREAK_INVARIANT_X = {
    "list": ("marker_x", "content_x", "continuation_x"),
    "toc": ("title_x", "page_x", "continuation_x"),
}


class PageBreakDecision(Enum):
    """What to do with a block that does not fit its page (8e-1)."""

    KEEP = "keep"                            # fits — stay on the current page
    BREAK_TO_NEXT_PAGE = "break_to_next_page"  # whole block → next page
    PRESERVE_OVERFLOW = "preserve_overflow"  # immovable — never split/move


# ---------------------------------------------------------------------------
# decisions — NEXT_PAGE semantics
# ---------------------------------------------------------------------------


def decide_page_break(
    placement: BlockPlacement,
    *,
    page_height: float,
    page_start_y: float = 0.0,
    tolerance: float = _TOL,
) -> PageBreakDecision:
    """Does the settled placement fit its page (8e-1 policy)?

    Consumes ONLY the settled ``resolved_bbox`` — the block height is never
    recomputed here (LayoutResult is the geometry authority).

    - ``preserved`` (code / formula / page column) → ``PRESERVE_OVERFLOW``:
      never split, never move, even when it overflows;
    - resolved extent within ``[page_start_y, page_height]`` → ``KEEP``;
    - otherwise → ``BREAK_TO_NEXT_PAGE`` (the whole block, not a line).
    """
    if placement.preserved:
        return PageBreakDecision.PRESERVE_OVERFLOW
    bottom = placement.resolved_bbox[1]
    top = placement.resolved_bbox[3]
    if bottom >= page_start_y - tolerance and top <= page_height + tolerance:
        return PageBreakDecision.KEEP
    return PageBreakDecision.BREAK_TO_NEXT_PAGE


def decide_page_breaks(
    placements,
    page_sizes: Optional[dict] = None,
) -> list[tuple[BlockPlacement, PageBreakDecision]]:
    """One page-break decision per settled placement (8e-1 policy over a plan).

    Pages without a known height default to ``KEEP`` (no boundary judgment
    possible).  Pure read — never mutates anything.
    """
    sizes = dict(page_sizes or {})
    out: list[tuple[BlockPlacement, PageBreakDecision]] = []
    for p in placements:
        ph = sizes.get(p.page)
        if ph is None or ph <= 0.0:
            out.append((p, PageBreakDecision.KEEP))
            continue
        out.append((p, decide_page_break(p, page_height=ph)))
    return out


def page_break_from_shift(
    shift_decision: BlockShiftDecision,
    *,
    placement: Optional[BlockPlacement] = None,
) -> PageBreakDecision:
    """Consume an 8c decision without re-judging the collision (8e-1).

    - 8c ``NEXT_PAGE`` → ``BREAK_TO_NEXT_PAGE`` (the collision cannot be
      resolved by a same-page shift);
    - 8c ``PRESERVE_OVERFLOW`` → ``PRESERVE_OVERFLOW`` (immovable);
    - 8c ``KEEP`` / ``SHIFT_DOWN`` → ``KEEP`` (same-page business, not a break).

    A ``preserved`` ``placement`` wins over the shift decision: code is never
    broken even if a NEXT_PAGE decision ever targeted it.
    """
    if placement is not None and placement.preserved:
        return PageBreakDecision.PRESERVE_OVERFLOW
    if shift_decision.decision is PageRecoveryDecision.NEXT_PAGE:
        return PageBreakDecision.BREAK_TO_NEXT_PAGE
    if shift_decision.decision is PageRecoveryDecision.PRESERVE_OVERFLOW:
        return PageBreakDecision.PRESERVE_OVERFLOW
    return PageBreakDecision.KEEP


# ---------------------------------------------------------------------------
# next-page start position — only page and y change
# ---------------------------------------------------------------------------


def next_page_start_y(page_start_y: float = 0.0) -> float:
    """The y (v3 y-up) where a broken block's TOP lands on the next page.

    Contract: the new page's content start.  ``page_start_y`` is the content
    area's top edge (v3: larger y); the executor (8e-2) is the only consumer
    that turns this into a placement.
    """
    return float(page_start_y)


def break_placement_to_page(
    placement: BlockPlacement,
    *,
    target_page: int,
    page_start_y: float = 0.0,
) -> BlockPlacement:
    """The placement as it would sit on ``target_page`` (pure mapping, 8e-1).

    ``page`` becomes ``target_page``; the resolved bbox is re-anchored so its
    TOP lands at ``page_start_y`` — ``x0`` / ``x1`` / ``height`` / kind /
    flags and the source ``bbox`` are preserved verbatim.  Only ``page`` and
    ``y`` change; X is never recomputed.

    Returns the placement unchanged for preserved blocks (never broken).
    """
    if placement.preserved:
        return placement
    h = placement.height
    new_box = (
        placement.resolved_bbox[0],
        round(page_start_y - h, 2),
        placement.resolved_bbox[2],
        round(page_start_y, 2),
    )
    return BlockPlacement(
        block_index=placement.block_index,
        page=int(target_page),
        kind=placement.kind,
        bbox=placement.bbox,
        resolved_bbox=new_box,
        height=h,
        preserved=placement.preserved,
        has_continuation=placement.has_continuation,
    )


def last_page_index(page_sizes) -> Optional[int]:
    """The largest real page index a break may land on, or ``None``.

    8e may propose a page change, but it must never generate page / geometry
    the renderer cannot carry (7G-2.1 P0): a block pushed past the document's
    last real page lands on a page that has no size, gets dropped from the
    render, and loses words.  The last real page is therefore the largest
    key in the ``page_sizes`` map — the set of pages the renderer will emit.
    ``None`` when no page sizes are known (the caller then stays unbounded,
    preserving historical behaviour for callers without a size map).
    """
    sizes = dict(page_sizes or {})
    numeric = [int(k) for k in sizes.keys() if isinstance(k, (int, float))]
    if not numeric:
        return None
    return max(numeric)


def next_free_page(
    source_page: int,
    occupied: Sequence[int],
    *,
    max_page: Optional[int] = None,
) -> Optional[int]:
    """The next page after ``source_page`` not already occupied.

    7G-2.1 P0 (8e out-of-document overflow): a block may only break to a
    page that actually exists.  ``max_page`` is the document's last real page
    (see :func:`last_page_index`); when the monotonic scan runs past it there
    is no real page to land on, so this returns ``None`` — the caller records
    the block as unresolved instead of placing it on a phantom page that the
    renderer will not carry.

    Pins the page-chain semantics: A stays on 0, a break from 0 lands on 1,
    a second break from 0 lands on 2 (never all on one page, never reusing a
    taken page).  Without ``max_page`` the chain is bounded only by the
    occupied set (historical callers / unit tests that pass no size map).
    """
    page = int(source_page) + 1
    taken = set(int(o) for o in occupied)
    while page in taken:
        page += 1
    if max_page is not None and page > int(max_page):
        return None
    return page


# ---------------------------------------------------------------------------
# continuation invariants — what a break must preserve (8e never recomputes X)
# ---------------------------------------------------------------------------


def break_invariants(entry: dict) -> dict:
    """The X anchors a page break must preserve, read verbatim from the
    settled payload (list items / toc entries).  Empty for flow / code."""
    out: dict[str, float] = {}
    items = entry.get("list_items")
    if isinstance(items, dict):
        for it in items.get("items") or []:
            if not isinstance(it, dict):
                continue
            for k in _BREAK_INVARIANT_X["list"]:
                v = it.get(k)
                if isinstance(v, (int, float)):
                    out.setdefault(k, float(v))
    for e in (entry.get("toc_entries") or []) or []:
        if not isinstance(e, dict):
            continue
        for k in _BREAK_INVARIANT_X["toc"]:
            v = e.get(k)
            if isinstance(v, (int, float)):
                out.setdefault(k, float(v))
    return out


def _page_command_count(entry: dict) -> int:
    payload = entry.get("render_payload")
    if not isinstance(payload, dict):
        return 0
    cmds = payload.get("commands")
    if not isinstance(cmds, list):
        return 0
    return sum(
        1 for c in cmds
        if isinstance(c, dict) and c.get("kind") == "page"
    )


def assert_break_invariants(source_entry: dict, target_entry: dict) -> list[str]:
    """Validate a would-be page break: returns violations ([] == ok).

    Per the 8e contract, a break may change ONLY ``page`` and ``y``:

    - the X anchors (list marker_x/content_x/continuation_x; toc title_x/
      page_x/continuation_x) are byte-identical;
    - source geometry (``src_box``) is unchanged;
    - the placed box's x0 / x1 are unchanged and its y actually moved;
    - a preserved block is never moved or split;
    - the TOC page-number run stays drawn exactly once and the page number
      never leaks into a continuation line.
    """
    violations: list[str] = []
    src_anchors = break_invariants(source_entry)
    dst_anchors = break_invariants(target_entry)
    for k, v in sorted(src_anchors.items()):
        if dst_anchors.get(k) != v:
            violations.append(
                f"{k} changed across break: {v} -> {dst_anchors.get(k)}"
            )
    if target_entry.get("src_box") != source_entry.get("src_box"):
        violations.append("src_box changed across break (source must stay immutable)")
    src_dst = source_entry.get("dst_box") or [0, 0, 0, 0]
    dst_dst = target_entry.get("dst_box") or [0, 0, 0, 0]
    if len(src_dst) == 4 and len(dst_dst) == 4:
        if src_dst[0] != dst_dst[0] or src_dst[2] != dst_dst[2]:
            violations.append("dst_box x changed across break")
        if src_dst[1] == dst_dst[1] and src_dst[3] == dst_dst[3]:
            violations.append("dst_box y did not move (no break executed)")
    kind = str(target_entry.get("kind") or "")
    if kind in PRESERVE_KINDS:
        if target_entry.get("page") != source_entry.get("page") or \
                dst_dst[1:4:2] != src_dst[1:4:2]:
            violations.append("preserved block must never break/move")
    src_pages = _page_command_count(source_entry)
    dst_pages = _page_command_count(target_entry)
    if src_pages != dst_pages:
        violations.append(
            f"TOC page-number run count changed across break: "
            f"{src_pages} -> {dst_pages}"
        )
    src_entries = (source_entry.get("toc_entries") or []) or []
    dst_entries = (target_entry.get("toc_entries") or []) or []
    for se, de in zip(src_entries, dst_entries):
        if not isinstance(se, dict) or not isinstance(de, dict):
            continue
        num = se.get("page_number")
        if num and any(num in (c or "") for c in (de.get("continuation") or [])):
            violations.append("page number became part of a continuation line")
    return violations


# ---------------------------------------------------------------------------
# records — the executor (8e-2) will emit these; 8e-1 defines the shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageBreakExecution:
    """One planned / executed page break (8e-1 record; 8e-2 executor output).

    ``resolved_bbox`` is the MAPPED box on the target page; ``source_bbox`` is
    the immutable source geometry.
    """

    block_index: int
    source_page: int
    target_page: int
    decision: PageBreakDecision
    reason: str
    next_start_y: float
    kind: str
    source_bbox: tuple
    resolved_bbox: tuple
    target: str = "block"

    def to_dict(self) -> dict:
        return {
            "block_index": self.block_index,
            "source_page": self.source_page,
            "target_page": self.target_page,
            "kind": self.kind,
            "decision": self.decision.value,
            "reason": self.reason,
            "next_start_y": round(float(self.next_start_y), 2),
            "source_bbox": [round(v, 2) for v in self.source_bbox],
            "resolved_bbox": [round(v, 2) for v in self.resolved_bbox],
            "target": self.target,
        }


@dataclass
class PageBreakReport:
    """Aggregate of :class:`PageBreakExecution` records (observability)."""

    executions: list = field(default_factory=list)

    def summary(self) -> dict:
        by_decision: dict[str, int] = {}
        for e in self.executions:
            by_decision[e.decision.value] = by_decision.get(e.decision.value, 0) + 1
        return {
            "total": len(self.executions),
            "by_decision": dict(sorted(by_decision.items())),
        }

    def to_dict(self) -> dict:
        return {
            "executions": [e.to_dict() for e in self.executions],
            "summary": self.summary(),
        }


def page_break_execution(
    placement: BlockPlacement,
    *,
    target_page: Optional[int] = None,
    page_start_y: float = 0.0,
    reason: str = "page_capacity",
) -> tuple[PageBreakExecution, BlockPlacement]:
    """Plan one break: ``(execution record, mapped placement)`` — 8e-1 contract.

    ``target_page`` defaults to ``placement.page + 1``; the mapped placement
    is what the 8e-2 executor will place on the target page.  Pure — the plan
    is never touched.  Preserved placements map unchanged (PRESERVE_OVERFLOW).
    """
    target = int(target_page) if target_page is not None else placement.page + 1
    mapped = break_placement_to_page(
        placement, target_page=target, page_start_y=page_start_y
    )
    record = PageBreakExecution(
        block_index=placement.block_index,
        source_page=placement.page,
        target_page=target,
        decision=(
            PageBreakDecision.PRESERVE_OVERFLOW
            if placement.preserved
            else PageBreakDecision.BREAK_TO_NEXT_PAGE
        ),
        reason=reason,
        next_start_y=float(page_start_y),
        kind=placement.kind,
        source_bbox=placement.bbox,
        resolved_bbox=mapped.resolved_bbox,
    )
    return record, mapped
