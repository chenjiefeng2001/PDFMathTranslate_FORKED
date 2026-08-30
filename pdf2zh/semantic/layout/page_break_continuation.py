"""List / TOC continuation break executor — Commit 7F-8e-3.

The second phase of 7F-8e that changes cross-page ownership.  Where 8e-2 moved
a block whole, 8e-3 *splits a list / TOC block at its settled line boundary* —
the part that fits stays on its page and only the overflowing tail becomes a
continuation on the next page:::

    settled list / toc block (page 0, bottom < 0)
        ↓ 8e-1 PageBreakDecision (BREAK_TO_NEXT_PAGE)
        ↓ this module — partition the ALREADY-SETTLED command run at the
          page bottom edge (page + Y only)
    kept entry   (page 0: fitted lines, marker once)   ┐
    cont entry   (page 1: tail lines, re-anchored at   ┘ = overflow 1 → 0
                  next_page_start_y, no new marker)

The ONLY rule is line ownership across a page.  Everything else — X, width,
font, text, ``src_box``, the semantic payload and the list / TOC anchors
(``marker_x`` / ``content_x`` / ``continuation_x`` / ``title_x`` / ``page_x`` /
``continuation_x``) — is **copied verbatim**.  A split never re-wraps, never
re-shrinks, never re-clips and never derives X from ``level`` / ``index``.

Hard rules (enforced by ``tests/test_page_break_continuation_7f8e3.py``):

- **split only already-settled lines/commands** — it partitions ``render_payload
  .commands`` (and its ``list_items`` / ``toc_commands`` aliases) by the page
  bottom edge; no ``adaptive_layout`` / ``wrap`` / ``shrink`` / ``clip``;
- **marker appears exactly once** — the marker line is never regenerated on the
  continuation page and is never duplicated (a block whose marker itself falls
  below the fold is NOT split — it degrades to a whole-block move);
- **TOC page number drawn exactly once, ``page_x`` never moves**;
- **only ``page`` and ``y`` change** — ``src_box`` and all X stay verbatim;
- **Code / preserved region → PRESERVE_OVERFLOW** — never split, never moved;
- **non-list/toc (flow) BREAK → whole-block move** (8e-2 semantics);
- **bounded budget** — at most ``max_splits <= `` blocks are touched; the rest
  are recorded deferred / unresolved, never silently applied;
- **pure read + decisions** — the source plan is never mutated; a NEW deep-copied
  plan is returned; it never calls the detector / renderer / re-layout.

The continuation page lands via the 8e-1 contracts (``next_free_page`` /
``break_placement_to_page`` / ``next_page_start_y``) — no page start is ever
recomputed here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from pdf2zh.semantic.layout.page_break import (
    PageBreakDecision,
    PageBreakExecution,
    break_placement_to_page,
    decide_page_breaks,
    next_free_page,
    next_page_start_y,
    page_break_execution,
)
from pdf2zh.semantic.layout.page_flow import (
    placements_from_plan,
)

__all__ = [
    "ContinuationBreakRecord",
    "ContinuationBreakReport",
    "split_continuation_break",
    "execute_continuation_breaks",
]

_TOL = 1e-6
_DESCENT_RATIO = 0.25
#: Kinds that carry a continuation run and may be line-split across a page.
_SPLIT_KINDS = ("list", "toc")


def _cmd_y(cmd) -> float:
    try:
        return float(cmd.get("y") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _entry_commands(entry: dict) -> list:
    payload = entry.get("render_payload")
    if isinstance(payload, dict):
        cmds = payload.get("commands")
        if isinstance(cmds, list):
            return cmds
    return []


def _set_commands(entry: dict, cmds: list) -> None:
    """Repoint an entry's command lists at ``cmds`` (render_payload + aliases).

    ``render_payload.commands`` and the compat ``list_items.commands`` /
    ``toc_commands.commands`` copies often alias the SAME list; pointing them
    all at the partitioned list keeps them consistent.
    """
    payload = entry.get("render_payload")
    if isinstance(payload, dict):
        payload["commands"] = cmds
    for key in ("list_items", "toc_commands"):
        obj = entry.get(key)
        if isinstance(obj, dict):
            obj["commands"] = cmds


# ---------------------------------------------------------------------------
# 8e-3-1 — one settled block → (kept split, continuation split)
# ---------------------------------------------------------------------------


def split_continuation_break(
    entry: dict,
    *,
    page_bottom_y: float = 0.0,
    page_start_y: float = 0.0,
    target_page: Optional[int] = None,
):
    """Split one settled list / TOC entry at its line boundary.

    Partitions the settled command run into the lines that fit
    (``y >= page_bottom_y``) — which stay on the source page — and the overflow
    tail (``y < page_bottom_y``) — which re-anchors as a continuation on
    ``target_page`` (default ``page + 1``).  Returns ``(kept, cont, info)`` as
    new dicts, or ``None`` when the block is not splittable:

    - no commands / nothing overflows (nothing to split);
    - the whole block overflows (``fitted`` empty → caller moves it whole);
    - a ``marker`` line itself overflowed — its marker would be regenerated on
      the continuation page, which 8e never does.

    Only ``page`` and ``y`` change; ``src_box`` and every X (marker_x /
    content_x / continuation_x / title_x / page_x) are copied verbatim onto
    both splits.
    """
    cmds = _entry_commands(entry)
    if not cmds:
        return None
    fitted: list = []
    moved: list = []
    for c in cmds:
        (fitted if _cmd_y(c) >= float(page_bottom_y) else moved).append(c)
    if not moved or not fitted:
        return None
    if any(c.get("kind") == "marker" for c in moved):
        return None  # never regenerate / duplicate a marker

    target = (
        int(target_page)
        if target_page is not None
        else int(entry.get("page") or 0) + 1
    )
    fs = 0.0
    for v in (entry.get("font_size"),):
        if isinstance(v, (int, float)) and v > 0:
            fs = float(v)
            break
    desc = fs * _DESCENT_RATIO

    # ── kept split: only the fitted lines; the fitted extent ─────────────
    kept = copy.deepcopy(entry)
    _set_commands(kept, list(fitted))
    dst = entry.get("dst_box") or [0.0, 0.0, 0.0, 0.0]
    min_fitted = min(_cmd_y(c) for c in fitted)
    fitted_bottom = round(max(min_fitted - desc, 0.0), 2)
    kept["dst_box"] = [
        float(dst[0]), fitted_bottom, float(dst[2]),
        round(float(dst[3]), 2),
    ]

    # ── continuation split: re-anchor the overflow tail (page + y only) ──
    top_moved = max(_cmd_y(c) for c in moved)
    min_moved = min(_cmd_y(c) for c in moved)
    start_y = next_page_start_y(float(page_start_y))
    delta = round(float(start_y) - top_moved, 2)
    cont_cmds: list = []
    moved_span = top_moved - min_moved
    for c in moved:
        cc = copy.deepcopy(c)
        if isinstance(cc.get("y"), (int, float)):
            cc["y"] = round(float(cc["y"]) + delta, 2)
        if "page" in cc:
            cc["page"] = int(target)
        cont_cmds.append(cc)
    cont = copy.deepcopy(entry)
    _set_commands(cont, cont_cmds)
    cont["page"] = int(target)
    cont["dst_box"] = [
        float(dst[0]),
        round(float(start_y) - moved_span - desc, 2),
        float(dst[2]),
        round(float(start_y), 2),
    ]

    info = {
        "block_index": _block_index(entry.get("block_id")),
        "kind": entry.get("kind"),
        "target_page": target,
        "fitted_lines": len(fitted),
        "moved_lines": len(moved),
        "min_fitted_y": round(min_fitted, 2),
        "top_moved_y": round(top_moved, 2),
        "marker_kept": True,
    }
    return kept, cont, info


def _block_index(block_id) -> int:
    """Parse ``p3_12`` → 12 (best-effort; 0 when unparseable)."""
    if "_" in str(block_id or ""):
        try:
            return int(str(block_id).rsplit("_", 1)[1])
        except (TypeError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------------------
# 8e-3-2 — records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContinuationBreakRecord:
    """One executed cross-page recovery (split or whole-block move or preserve)."""

    block_index: int
    source_page: int
    target_page: int
    kind: str
    mode: str            # "split" | "whole_block" | "preserve"
    reason: str
    fitted_lines: int = 0
    moved_lines: int = 0

    def to_dict(self) -> dict:
        return {
            "block_index": self.block_index,
            "source_page": self.source_page,
            "target_page": self.target_page,
            "kind": self.kind,
            "mode": self.mode,
            "reason": self.reason,
            "fitted_lines": int(self.fitted_lines),
            "moved_lines": int(self.moved_lines),
        }


@dataclass
class ContinuationBreakReport:
    """Aggregate of 8e-3 cross-page recoveries (observability, not policy)."""

    passes: int = 0
    max_splits: int = 0
    stopped_early: bool = False
    applied: list = field(default_factory=list)       # ContinuationBreakRecord
    deferred: list = field(default_factory=list)      # decisions not executed
    unresolved: list = field(default_factory=list)    # placements left on a full page

    def summary(self) -> dict:
        return {
            "passes": self.passes,
            "max_splits": self.max_splits,
            "stopped_early": self.stopped_early,
            "applied_count": len(self.applied),
            "deferred_count": len(self.deferred),
            "unresolved_count": len(self.unresolved),
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "applied": [r.to_dict() for r in self.applied],
            "deferred": [r.to_dict() if hasattr(r, "to_dict") else dict(r)
                         for r in self.deferred],
            "unresolved": [p.to_dict() for p in self.unresolved],
        }


def _normalize_decisions(placements, decisions, page_sizes):
    if decisions is None:
        return [d for _, d in decide_page_breaks(placements, page_sizes=page_sizes)]
    out: list = []
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


# ---------------------------------------------------------------------------
# 8e-3-3 — bounded cascade over a settled plan
# ---------------------------------------------------------------------------


def execute_continuation_breaks(
    plan,
    page_sizes: Optional[dict] = None,
    decisions=None,
    *,
    page_start_y: float = 0.0,
    page_bottom_y: float = 0.0,
    max_splits: Optional[int] = None,
) -> tuple[list[dict], ContinuationBreakReport]:
    """Execute the 8e-1 BREAK decisions on a settled plan with continuation splits.

    Returns ``(new_plan, report)``.  For each ``BREAK_TO_NEXT_PAGE`` decision:

    - **list / TOC** → ``split_continuation_break`` at ``page_bottom_y``; the
      fitted lines stay on the source page, the tail continues on the next free
      page at ``next_page_start_y``.  ``marker`` / TOC page number stay once;
    - **flow (movable, unsplittable)** → whole-block move (8e-2 semantics);
    - **code / preserved region** → PRESERVE_OVERFLOW (never split, never moved).

    ``max_splits`` caps the number of blocks touched (default ``number_of_blocks``);
    the rest are recorded deferred + unresolved, never silently applied.  A NEW
    deep-copied plan is returned; the input is never mutated.
    """
    entries = copy.deepcopy(list(plan or []))
    placements = placements_from_plan(entries)
    n = len(placements)
    bound = min(int(max_splits), n) if max_splits is not None else n
    if bound < 0:
        bound = 0
    report = ContinuationBreakReport(max_splits=bound)

    decs = _normalize_decisions(placements, decisions, page_sizes)
    taken = {p.page for p in placements}
    budget = bound
    out: list[dict] = []

    for i, (p, dec) in enumerate(zip(placements, decs)):
        e = entries[i]
        if dec is PageBreakDecision.KEEP or p.preserved:
            if p.preserved and dec is not PageBreakDecision.KEEP:
                record = ContinuationBreakRecord(
                    block_index=p.block_index, source_page=p.page,
                    target_page=p.page, kind=p.kind, mode="preserve",
                    reason="preserved_region")
                report.deferred.append(record)
                report.unresolved.append(p)
            out.append(e)
            continue
        if dec is PageBreakDecision.PRESERVE_OVERFLOW:
            record = ContinuationBreakRecord(
                block_index=p.block_index, source_page=p.page,
                target_page=p.page, kind=p.kind, mode="preserve",
                reason="preserve_overflow")
            report.deferred.append(record)
            report.unresolved.append(p)
            out.append(e)
            continue
        # --- BREAK_TO_NEXT_PAGE on a movable block ---
        start_y = next_page_start_y(float(page_start_y))
        if budget <= 0:
            record = ContinuationBreakRecord(
                block_index=p.block_index, source_page=p.page, target_page=p.page + 1,
                kind=p.kind, mode="whole_block", reason="budget_exhausted")
            report.deferred.append(record)
            report.unresolved.append(p)
            report.stopped_early = True
            out.append(e)
            continue

        # try a list / TOC continuation split first; landing goes through the
        # 8e-1 page chain (never reuses a taken page, never unbounded)
        target = next_free_page(p.page, taken)
        split = None
        if e.get("kind") in _SPLIT_KINDS:
            split = split_continuation_break(
                e, page_bottom_y=page_bottom_y, page_start_y=start_y,
                target_page=target)
        if split is not None:
            kept, cont, info = split
            out.append(kept)
            out.append(cont)
            taken.add(info["target_page"])
            report.applied.append(ContinuationBreakRecord(
                block_index=p.block_index, source_page=p.page,
                target_page=info["target_page"], kind=p.kind, mode="split",
                reason="continuation",
                fitted_lines=info["fitted_lines"],
                moved_lines=info["moved_lines"]))
            budget -= 1
            continue

        # unsplittable movable block → whole-block move (8e-2 semantics)
        target = next_free_page(p.page, taken)
        mapped = break_placement_to_page(p, target_page=target, page_start_y=start_y)
        delta = round(float(mapped.resolved_bbox[3]) - float(p.resolved_bbox[3]), 2)
        _move_entry_page(e, delta, target)
        out.append(e)
        taken.add(target)
        report.applied.append(ContinuationBreakRecord(
            block_index=p.block_index, source_page=p.page, target_page=target,
            kind=p.kind, mode="whole_block", reason="page_capacity"))
        budget -= 1

    report.passes = 1 if report.applied else 0
    return out, report


def _shift_command_fields(cmds, delta: float, target_page: int) -> None:
    if not isinstance(cmds, list):
        return
    for c in cmds:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("y"), (int, float)):
            c["y"] = round(float(c["y"]) + delta, 2)
        if "page" in c:
            c["page"] = int(target_page)


def _move_entry_page(entry: dict, delta: float, target_page: int) -> None:
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
        _shift_command_fields(cmds, delta, target_page)
        if isinstance(cmds, list):
            moved.add(id(cmds))
    for key in ("list_items", "toc_commands"):
        obj = entry.get(key)
        if isinstance(obj, dict):
            cmds = obj.get("commands")
            if id(cmds) in moved:
                continue
            _shift_command_fields(cmds, delta, target_page)
            moved.add(id(cmds))