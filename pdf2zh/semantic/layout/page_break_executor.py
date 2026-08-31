"""Page break executor — Commit 7F-8e-2.

The first phase of 7F-8e that actually changes cross-page ownership — the
second writer of the settled plan (after 8d).  It turns the 8e-1
:class:`PageBreakDecision` into geometry, then the renderer is the only
consumer:::

    resolved_bbox            (8b)
        ↓ PageCollision      (8b)
        ↓ PageRecovery       (8c)
        ↓ ShiftExecutor      (8d — same page, SHIFT_DOWN)
        ↓ PageBreakDecision  (8e-1 — "which page", never recomputes X)
        ↓ this module        (8e-2 — the ONLY place a NEXT_PAGE decision
                              becomes cross-page geometry)
    page = N+1, top = next_page_start_y
        ↓ detect again (external) → overflow 1 → 0

Scope of 7F-8e-2 (hard rules, enforced by ``tests/test_page_break_executor_7f8e2.py``):

- **consumes ONLY the 8e-1 ``PageBreakDecision``** — never re-judges a
  collision, never calls ``detect_page_collisions``, never calls
  ``lay_out`` / ``adaptive_layout`` / wrap / shrink / clip;
- **only ``BREAK_TO_NEXT_PAGE`` is executed** — ``KEEP`` /
  ``PRESERVE_OVERFLOW`` (code / formula / …) leave the block untouched;
  a preserved block is never broken even when a BREAK decision targeted it;
- **whole-block breaks only** — no paragraph / line splitting, no list / TOC
  continuation splitting, no auto re-wrap (those are 8e-3+);
- **only ``page`` and ``y`` change** — ``dst_box`` y re-anchored so the TOP
  lands at ``next_page_start_y``; payload command ``page`` / ``y`` follow;
  ``src_box``, every X, width, font, text and the semantic payload are
  byte-identical;
- **landing page from ``next_free_page``** (the 8e-1 page-chain contract) —
  a break from 0 lands on 1, a second from 0 on 2, never reuses a taken page,
  never an unbounded page stream;
- **bounded budget** — at most ``max_page_breaks <= `` blocks are broken; a
  break that would exceed the budget is recorded deferred / unresolved, never
  silently applied;
- **pure read of the settled plan plus the supplied decisions** — the source
  plan is never mutated; a NEW deep-copied plan is returned.

The renderer stays a draw-only consumer: this module moves blocks between
pages; it never decides where they go (that is 8e-1) and never draws them.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional, Sequence

from pdf2zh.semantic.layout.page_break import (
    PageBreakDecision,
    PageBreakExecution,
    break_placement_to_page,
    decide_page_breaks,
    last_page_index,
    next_free_page,
    next_page_start_y,
    page_break_execution,
)
from pdf2zh.semantic.layout.page_flow import (
    BlockPlacement,
    placements_from_plan,
)

__all__ = [
    "PageBreakExecutionReport",
    "execute_page_breaks",
    "move_entry_to_page",
    "shift_command_fields",
]

_TOL = 1e-6


# ---------------------------------------------------------------------------
# 8e-2-1 — settled plan wiring (only page and y change)
# ---------------------------------------------------------------------------


def shift_command_fields(cmds, delta: float, target_page: int) -> None:
    """Move positioned payload commands to ``target_page`` (in place).

    Shifts command ``y`` by ``delta`` and sets command ``page`` to
    ``target_page`` when the command carries one.  X / width / font / text are
    untouched — a command moves as a unit.
    """
    if not isinstance(cmds, list):
        return
    for c in cmds:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("y"), (int, float)):
            c["y"] = round(float(c["y"]) + delta, 2)
        if "page" in c:
            c["page"] = int(target_page)


def move_entry_to_page(entry: dict, delta: float, target_page: int) -> None:
    """Move one deep-copied plan entry across pages (in place).

    Changes ONLY ``page`` (``target_page``) and Y — ``dst_box`` is re-anchored
    by ``delta`` and the settled payload commands' ``page`` / ``y`` follow.
    ``src_box``, all x / width / font_size / text and the semantic payload
    fields stay untouched.

    ``render_payload.commands`` and the compat ``list_items`` / ``toc_commands``
    copies often alias the SAME command list — ``deepcopy`` preserves that
    identity, so the dedup guard below moves the same commands only once.
    """
    entry["page"] = int(target_page)
    dst = entry.get("dst_box")
    if isinstance(dst, list) and len(dst) == 4:
        entry["dst_box"] = [
            dst[0], round(float(dst[1]) + delta, 2),
            dst[2], round(float(dst[3]) + delta, 2),
        ]
    moved: set[int] = set()
    payload = entry.get("render_payload")
    if isinstance(payload, dict):
        cmds = payload.get("commands")
        shift_command_fields(cmds, delta, target_page)
        if isinstance(cmds, list):
            moved.add(id(cmds))
    for key in ("list_items", "toc_commands"):
        obj = entry.get(key)
        if isinstance(obj, dict):
            cmds = obj.get("commands")
            if id(cmds) in moved:
                continue
            shift_command_fields(cmds, delta, target_page)
            moved.add(id(cmds))


# ---------------------------------------------------------------------------
# 8e-2-2 — records (applied / deferred / unresolved / passes)
# ---------------------------------------------------------------------------


@dataclass
class PageBreakExecutionReport:
    """What the 8e-2 cascade actually did (observability, not break policy).

    - ``applied``    — :class:`PageBreakExecution` records actually executed;
    - ``deferred``   — :class:`PageBreakExecution` plans NOT executed (budget
      exhausted, or a BREAK decision that targeted an immovable block);
    - ``unresolved`` — the :class:`BlockPlacement` still standing on a page it
      cannot fit (the mover's final stance — never a silent off-page shift);
    - ``passes``     — how many application sweeps ran.
    """

    passes: int = 0
    max_page_breaks: int = 0
    stopped_early: bool = False
    applied: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "passes": self.passes,
            "max_page_breaks": self.max_page_breaks,
            "stopped_early": self.stopped_early,
            "applied_count": len(self.applied),
            "deferred_count": len(self.deferred),
            "unresolved_count": len(self.unresolved),
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "applied": [e.to_dict() for e in self.applied],
            "deferred": [e.to_dict() for e in self.deferred],
            "unresolved": [
                p.to_dict() if isinstance(p, BlockPlacement)
                else (p.to_dict() if hasattr(p, "to_dict") else str(p))
                for p in self.unresolved
            ],
        }


# ---------------------------------------------------------------------------
# 8e-2-3 — bounded cascade (execute NEXT_PAGE, never more than budget)
# ---------------------------------------------------------------------------


def _normalize_decisions(placements, decisions, page_sizes):
    """Align the supplied 8e-1 decisions with the placements reading order.

    Accepts ``None`` (compute via :func:`decide_page_breaks`), a list of
    ``(placement, decision)`` pairs (as produced by 8e-1), or a bare list of
    decisions aligned to the placements.  Missing tail entries default to
    ``KEEP``; extras are ignored.
    """
    if decisions is None:
        return [d for _, d in decide_page_breaks(placements, page_sizes=page_sizes)]
    out: list[PageBreakDecision] = []
    for item in list(decisions):
        if isinstance(item, tuple) and len(item) == 2:
            out.append(item[1])
        elif isinstance(item, PageBreakDecision):
            out.append(item)
        else:
            out.append(PageBreakDecision.KEEP)
    if len(out) < len(placements):
        out.extend(PageBreakDecision.KEEP for _ in range(len(placements) - len(out)))
    return out[: len(placements)]


def execute_page_breaks(
    plan,
    page_sizes: Optional[dict] = None,
    decisions=None,
    *,
    page_start_y: float = 0.0,
    max_page_breaks: Optional[int] = None,
) -> tuple[list[dict], PageBreakExecutionReport]:
    """Execute the 8e-1 ``BREAK_TO_NEXT_PAGE`` decisions on a settled plan.

    Returns ``(new_plan, report)``: a NEW deep-copied plan whose entries have
    only ``page`` and Y changed — the moved block's top lands at
    ``next_page_start_y(page_start_y)`` on ``next_free_page``.  ``src_box``,
    every x / width / font_size / text and all semantic fields are
    byte-identical.  The input plan is never mutated.  Re-detecting on the
    returned plan reports the page overflow the breaks cleared
    (before overflow 1 → after 0).

    - ``decisions``           — the 8e-1 decisions (``None`` → computed).
    - ``page_start_y``        — the target page's content start (v3 y-up top).
    - ``max_page_breaks``     — budget; defaults to ``number_of_blocks``.  Breaks
      beyond the budget are recorded deferred / unresolved, never silently
      applied.  The budget is the guarantee that a page chain is bounded.
    """
    entries = copy.deepcopy(list(plan or []))
    placements = placements_from_plan(entries)
    n = len(placements)
    bound = min(int(max_page_breaks), n) if max_page_breaks is not None else n
    if bound < 0:
        bound = 0
    report = PageBreakExecutionReport(max_page_breaks=bound)

    decs = _normalize_decisions(placements, decisions, page_sizes)
    taken = {p.page for p in placements}
    # 7G-2.1 P0: a NEXT_PAGE break may only land on a page that really exists
    # (last_page_index = max page_sizes key).  Pushing a block past the
    # document's last page puts it on a page the renderer cannot carry — the
    # words are dropped (lol.pdf: 170 -> 387 on a 382-page book, 280 words
    # lost).  When no free real page exists, the block stays and is recorded
    # unresolved, never silently moved off-document.
    max_page = last_page_index(page_sizes)
    budget = bound

    for i, (p, dec) in enumerate(zip(placements, decs)):
        if dec is PageBreakDecision.KEEP:
            continue
        # immovable: PRESERVE_OVERFLOW (code / formula / …) is never broken,
        # and a BREAK decision that ever targeted a preserved block is demoted
        # to PRESERVE.  Both are recorded deferred + unresolved, never applied.
        if dec is PageBreakDecision.PRESERVE_OVERFLOW or p.preserved:
            record, _ = page_break_execution(
                p, target_page=p.page + 1, page_start_y=float(page_start_y)
            )
            report.deferred.append(record)
            report.unresolved.append(p)
            continue
        # here dec is BREAK_TO_NEXT_PAGE on a movable block: budget-gated
        if budget <= 0:
            record, _ = page_break_execution(
                p, target_page=p.page + 1, page_start_y=float(page_start_y)
            )
            report.deferred.append(record)
            report.unresolved.append(p)
            report.stopped_early = True
            continue

        next_y = next_page_start_y(float(page_start_y))
        target = next_free_page(p.page, sorted(taken), max_page=max_page)
        if target is None:
            # no real page below this one exists — out-of-document overflow
            # (7G-2.1 P0): leave the block in place, surface as unresolved.
            record, _ = page_break_execution(
                p, target_page=p.page, page_start_y=float(page_start_y)
            )
            report.deferred.append(record)
            report.unresolved.append(p)
            continue
        mapped = break_placement_to_page(
            p, target_page=target, page_start_y=next_y
        )
        # only page + y: the whole block re-anchors its top at the start of the
        # target page; X / height / source geometry are copied verbatim.
        delta = round(float(mapped.resolved_bbox[3]) - float(p.resolved_bbox[3]), 2)
        move_entry_to_page(entries[i], delta, target)

        record = PageBreakExecution(
            block_index=p.block_index,
            source_page=p.page,
            target_page=target,
            decision=PageBreakDecision.BREAK_TO_NEXT_PAGE,
            reason="page_capacity",
            next_start_y=float(next_y),
            kind=p.kind,
            source_bbox=p.bbox,
            resolved_bbox=mapped.resolved_bbox,
        )
        report.applied.append(record)
        taken.add(target)
        budget -= 1

    report.passes = 1 if report.applied else 0
    return entries, report