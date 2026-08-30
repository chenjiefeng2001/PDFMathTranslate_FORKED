"""Whitespace / page-packing V2 executor — Commit 7G-2 (optimisation half).

Turns the space that ``pdf2zh/semantic/layout/packing.py`` *measures*
(``internal_gap`` + ``trailing_gap``) into an actual packing pass on a settled
plan.  Where 7G-1 *decides where a block belongs* (placement decision) and the
measurement half *says how empty a column is today*, this module **moves blocks**
to reclaim the measured whitespace::

    settled plan
        ↓ placements_from_plan (7F-8a)         pure read of resolved geometry
    page columns (x-overlap clustering, 7G-2)
        ↓ compact_column                       close internal gaps (pull up)
    compacted columns
        ↓ column_reanchor                      push the compacted column DOWN
                                                 into the trailing gap
    shifted placements
        ↓ apply_packing                        only-Y plan wiring (like 7F-8d)
    packed plan

The pass is the second half of 7G-2, described in `doc/corpus_layout_scan_7g_report.md` §8:

> Next: turn the measured reclaimable space (``internal_gap`` + ``trailing_gap``)
> into an actual packing pass, gated by these baseline numbers.

Two levers, both consumed from the *measured* per-column band (they operate on
reading-order blocks that already overlap horizontally → one column):

1. **Compaction** — within a column, in reading order (topmost first), pull
   each movable block UP so the vertical whitespace between it and the block
   above collapses to a target ``gutter``.  This directly shrinks
   ``internal_gap``.  The column's topmost block is the anchor (stays put), so
   content is never pulled above where it already was.
2. **Re-anchor** — shift the whole compacted column DOWN (v3 y-up: decreasing
   y) so the lowest movable block settles toward the page bottom, reclaiming
   the ``trailing_gap`` that compaction just opened.  The downward shift is
   bounded so no movable block crosses the page bottom (``bottom_margin`` is
   the floor) or a preserved / footer block below it.

Hard rules (enforced by ``tests/test_layout_packer_7g2.py``):

- **input is pure read** — consumes settled ``resolved_bbox`` via
  :func:`placements_from_plan`; never re-lays-out (no ``lay_out`` /
  ``adaptive_layout`` / wrap / shrink / clip);
- **only Y changes** — a packing move is a pure vertical translation of
  ``resolved_bbox`` / ``dst_box`` / command ``y``; ``src_box`` and all X /
  width / font / text stay byte-identical;
- **preserved blocks are immovable** — code / formula / figure / table /
  header / footer / column are never moved and act as barriers (content packs
  *against* them, never across them);
- **reading order is never inverted** — compaction only ever reduces a gap
  (never overshoots its target), so a lower block never rises above the block
  above it;
- **no detector / parser / renderer / translator** imports, no ``level`` /
  ``index`` geometry math — every number comes from a settled field;
- **``block_id`` is identity only** — geometry is never derived from the
  ``block_id`` string (7F-9.1 discipline).

Nothing here imports or touches the renderer / converter / ONNX: the executor
produces packed geometry; the renderer stays a draw-only consumer (same as the
7F-8 shift/recovery side).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from pdf2zh.semantic.layout.packing import (
    column_packing_metrics,
    page_columns,
)
from pdf2zh.semantic.layout.page_flow import BlockPlacement, placements_from_plan

__all__ = [
    "PackConfig",
    "PackSummary",
    "PackingReport",
    "shift_box_v",
    "compact_column",
    "column_reanchor",
    "resolve_packing",
    "apply_packing",
]

_TOL = 1e-6


def _round2(v: float) -> float:
    return round(float(v), 2)


@dataclass(frozen=True)
class PackConfig:
    """Knobs for the 7G-2 packing pass (policy, not geometry source)."""

    compact: bool = True
    #: Target vertical whitespace (pt) between two movable blocks after
    #: compaction.  Only gaps LARGER than this are reclaimed (never widened).
    gutter: float = 2.0
    #: Margin kept around a preserved block when content packs against it.
    #: A larger value keeps code / figures / footers from being pressed into.
    preserved_gutter: float = 6.0
    re_anchor: bool = True
    #: v3 y-up floor below which no movable block's bottom edge may be pushed
    #: by re-anchoring (page bottom edge = 0).  Keeps content off the footer.
    bottom_margin: float = 36.0
    #: Hard cap (pt) on the downward re-anchor shift; None = only the floor /
    #: preserved-barrier bounds apply.
    max_reclaim: Optional[float] = None


def shift_box_v(box, dy: float) -> tuple:
    """Shift a v3 y-up box ``(x0, bottom, x1, top)`` vertically by ``dy``.

    ``dy > 0`` moves toward the page top (increasing y), ``dy < 0`` toward the
    page bottom.  X / width are never touched.  Rounds to 2dp to match the
    7F-8 executor's rounding contract.
    """
    d = _round2(dy or 0.0)
    return (
        float(box[0]),
        _round2(box[1] + d),
        float(box[2]),
        _round2(box[3] + d),
    )


def _moved(placement, dy: float) -> BlockPlacement:
    """New placement with ``resolved_bbox`` translated vertically by ``dy``."""
    shifted = shift_box_v(placement.resolved_bbox, dy)
    return BlockPlacement(
        block_index=placement.block_index,
        page=placement.page,
        kind=placement.kind,
        bbox=placement.bbox,
        resolved_bbox=shifted,
        height=_round2(shifted[3] - shifted[1]),
        preserved=placement.preserved,
        has_continuation=placement.has_continuation,
    )


# ---------------------------------------------------------------------------
# pure geometry — per-column compaction
# ---------------------------------------------------------------------------


def compact_column(
    placements,
    *,
    gutter: float = 2.0,
    preserved_gutter: float = 6.0,
) -> list[float]:
    """Upward deltas (v3 y-up: ``+`` = up) for a column's blocks, in the SAME
    order as the input ``placements``.

    Blocks are processed in reading order (topmost first).  The topmost block
    is the anchor (delta 0).  Each subsequent block is pulled UP only so that
    the vertical gap to the block directly above it is reduced to ``gutter``
    (movable-to-movable) / ``preserved_gutter`` (movable-to-preserved); a gap
    already small enough is left untouched.  Preserved blocks always get
    delta 0 (immovable).  A lower block never moves above the block above it
    (the pull is clamped at the target, so it never overshoots).

    Pure read of resolved geometry; returns a list parallel to ``placements``.
    """
    order = sorted(enumerate(placements), key=lambda ie: (-ie[1].top, ie[1].bottom))
    deltas: dict[int, float] = {}
    prev_bottom: Optional[float] = None
    prev_preserved = False
    for idx, p in order:
        if p.preserved:
            deltas[idx] = 0.0
            prev_bottom = p.bottom
            prev_preserved = True
            continue
        if prev_bottom is None:
            # topmost movable block anchors the column
            deltas[idx] = 0.0
            prev_bottom = p.bottom
            prev_preserved = p.preserved
            continue
        g = preserved_gutter if prev_preserved else gutter
        target_top = prev_bottom - g
        dy = max(0.0, target_top - p.top)
        deltas[idx] = _round2(dy)
        prev_bottom = p.bottom + dy
        prev_preserved = p.preserved
    return [_round2(deltas.get(i, 0.0)) for i in range(len(placements))]


def _applied(placements, deltas):
    """Apply per-block vertical deltas to placements (pure)."""
    return [_moved(p, deltas[i]) for i, p in enumerate(placements)]


# ---------------------------------------------------------------------------
# pure geometry — column re-anchor (reclaim trailing gap)
# ---------------------------------------------------------------------------


def column_reanchor(
    placements,
    *,
    bottom_margin: float = 36.0,
    preserved_gutter: float = 6.0,
    max_reclaim: Optional[float] = None,
) -> float:
    """Downward delta (v3 y-up: ``-``) for one compacted column, bounded so no
    movable block crosses the page bottom or a preserved block below it.

    ``placements`` are the column's blocks AFTER compaction.  The whole column
    is moved DOWN (decreasing y) by at most the space to the nearest downward
    barrier: for the blocks below a preserved block the barrier is that
    preserved block's top (+ ``preserved_gutter``); for the lowest blocks, the
    page-bottom floor ``bottom_margin``.  Extra cap via ``max_reclaim``.

    Returns a non-positive float (the re-anchor delta), 0 when nothing is
    safely reclaimable.  Preserved blocks are never moved; they only bound how
    far the movable content below them may descend.
    """
    if not placements:
        return 0.0
    # downward barrier y (v3 y-up: higher = closer to page top) per movable block
    # = the lowest immovable block above it, or the page-bottom floor.
    movable = [p for p in placements if not p.preserved]
    if not movable:
        return 0.0
    # Find, for each movable block, the nearest preserved block strictly below
    # it in the same column (its bottom is above that preserved top).  The
    # binding constraint is the deepest such descent.
    preserved_below: dict[int, float] = {}
    for p in movable:
        top_of_below = None
        for q in placements:
            if q.preserved and q.top < p.bottom - _TOL:
                if top_of_below is None or q.top > top_of_below:
                    top_of_below = q.top
        preserved_below.setdefault(id(p), top_of_below)
    shift = None
    for p in movable:
        floor = bottom_margin
        if preserved_below.get(id(p)) is not None:
            floor = preserved_below[id(p)] + preserved_gutter
        allow = max(0.0, p.bottom - floor)
        if shift is None or allow < shift:
            shift = allow
    shift = shift or 0.0
    if max_reclaim is not None:
        shift = min(shift, max(0.0, max_reclaim))
    return _round2(-shift)


# ---------------------------------------------------------------------------
# resolve — pure geometry over placements (no plan writes)
# ---------------------------------------------------------------------------


@dataclass
class PackSummary:
    """One column's before/after packing (observability, not policy)."""

    before_internal_gap: float = 0.0
    after_internal_gap: float = 0.0
    before_trailing_gap: float = 0.0
    after_trailing_gap: float = 0.0
    moved_blocks: int = 0
    reclaimed_gap_pt: float = 0.0

    def to_dict(self) -> dict:
        return {
            "before_internal_gap": _round2(self.before_internal_gap),
            "after_internal_gap": _round2(self.after_internal_gap),
            "before_trailing_gap": _round2(self.before_trailing_gap),
            "after_trailing_gap": _round2(self.after_trailing_gap),
            "moved_blocks": int(self.moved_blocks),
            "reclaimed_gap_pt": _round2(self.reclaimed_gap_pt),
        }


@dataclass
class PackingReport:
    """What the packing pass did (per-column sums + per-page detail)."""

    pages: list = field(default_factory=list)
    moves: int = 0
    reclaimed_internal_pt: float = 0.0
    reclaimed_trailing_pt: float = 0.0

    def summary(self) -> dict:
        return {
            "moves": int(self.moves),
            "reclaimed_internal_pt": _round2(self.reclaimed_internal_pt),
            "reclaimed_trailing_pt": _round2(self.reclaimed_trailing_pt),
            "columns": len(self.pages),
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "per_column": [
                {"page": pc[0], "col": pc[1], **sm.to_dict()}
                for pc, sm in self.pages
            ],
        }


def _metrics(placements, page_height: float):
    cols = page_columns(placements)
    total_gap = sum(c.internal_gap for c in (column_packing_metrics(c, page_height) for c in cols))
    trail = [
        column_packing_metrics(c, page_height).trailing_gap for c in cols
    ]
    return _round2(total_gap), _round2(sum(trail) / len(trail)) if trail else 0.0


def resolve_packing(
    plan,
    page_sizes: Optional[dict] = None,
    *,
    config: Optional[PackConfig] = None,
) -> tuple[list, PackingReport]:
    """Pure geometry: compute packed placements for a settled plan.

    Returns ``(packed_placements, report)`` — ``packed_placements`` in the SAME
    reading order as :func:`placements_from_plan`.  Never mutates the plan.
    The input ``BoxPlacements`` are fully resolved outside; this only produces
    the target (possibly shifted) placements.
    """
    cfg = config or PackConfig()
    sizes = dict(page_sizes or {})
    initial = placements_from_plan(plan)
    keyed = {(p.page, p.block_index): p for p in initial}
    pages: dict[int, list] = {}
    for p in initial:
        pages.setdefault(p.page, []).append(p)
    report = PackingReport()

    # collect per-column deltas (up for compaction, down for re-anchor)
    per_key: dict[tuple, float] = {}
    for pg in sorted(pages):
        page_ph = float(sizes.get(pg, 0.0) or 0.0) or 792.0
        cols = page_columns(pages[pg])
        for col in cols:
            deltas_up = compact_column(
                col.placements,
                gutter=cfg.gutter,
                preserved_gutter=cfg.preserved_gutter,
            ) if cfg.compact else [0.0] * len(col.placements)
            # apply compaction to derive the compacted column for re-anchor
            compacted = _applied(col.placements, deltas_up)
            down = (
                column_reanchor(
                    compacted,
                    bottom_margin=cfg.bottom_margin,
                    preserved_gutter=cfg.preserved_gutter,
                    max_reclaim=cfg.max_reclaim,
                ) if cfg.re_anchor else 0.0
            )
            for i, p in enumerate(col.placements):
                dy = deltas_up[i] + down if not p.preserved else 0.0
                per_key[(p.page, p.block_index)] = dy
            # per-column observability
            moved = sum(1 for i, p in enumerate(col.placements)
                        if not p.preserved and abs(deltas_up[i]) > _TOL)
            before_gap, before_trail = _metrics(col.placements, page_ph)
            after_gap, after_trail = _metrics(
                _applied(col.placements, [deltas_up[i] + down for i in range(len(col.placements))]),
                page_ph,
            )
            report.pages.append(
                (pg, len(report.pages),
                 PackSummary(
                     before_internal_gap=before_gap,
                     after_internal_gap=after_gap,
                     before_trailing_gap=before_trail,
                     after_trailing_gap=after_trail,
                     moved_blocks=moved,
                     reclaimed_gap_pt=_round2(before_gap - after_gap),
                 ))
            )
            report.moves += moved
            report.reclaimed_internal_pt += max(0.0, before_gap - after_gap)
            report.reclaimed_trailing_pt += max(0.0, before_trail - after_trail)

    ordered = [keyed[(p.page, p.block_index)] for p in initial]
    result = []
    for p in ordered:
        dy = per_key.get((p.page, p.block_index), 0.0)
        result.append(_moved(p, dy) if abs(dy) > _TOL else p)
    report.reclaimed_internal_pt = _round2(report.reclaimed_internal_pt)
    report.reclaimed_trailing_pt = _round2(report.reclaimed_trailing_pt)
    return result, report


# ---------------------------------------------------------------------------
# plan wiring — the ONLY place a packing move hits a plan (mirrors 7F-8d/8d-3)
# ---------------------------------------------------------------------------


def _shift_commands_y(cmds, delta: float) -> None:
    if not isinstance(cmds, list):
        return
    for c in cmds:
        if isinstance(c, dict) and isinstance(c.get("y"), (int, float)):
            c["y"] = _round2(float(c["y"]) + delta)


def _shift_entry_v(entry: dict, delta: float) -> None:
    """Apply a vertical delta (v3 y-up, ``+`` = up) to a deep-copied entry.

    Moves ONLY the final draw position: ``dst_box`` and the settled payload
    commands' ``y`` (plus legacy ``list_items`` / ``toc_commands`` copies the
    host renderer may fall back to).  ``src_box``, every x / width / font_size /
    text and all semantic fields stay untouched.  Mirrors ``page_shift``'s
    entry mutation but for a signed vertical move.
    """
    dst = entry.get("dst_box")
    if isinstance(dst, list) and len(dst) == 4:
        entry["dst_box"] = [
            dst[0], _round2(float(dst[1]) + delta),
            dst[2], _round2(float(dst[3]) + delta),
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


def apply_packing(
    plan,
    page_sizes: Optional[dict] = None,
    *,
    config: Optional[PackConfig] = None,
) -> tuple[list[dict], PackingReport]:
    """Pack the settled render plan per the resolved 7G-2 deltas (plan wiring).

    Returns ``(new_plan, report)``: a NEW deep-copied plan whose entries have
    only the Y of their final draw position changed — ``dst_box`` and the
    payload commands' ``y``.  ``src_box`` and every other field are
    byte-identical; the input plan is never mutated.  Entry ↔ placement pairing
    is positional (reading order preserved, 7F-9.1).
    """
    packed, report = resolve_packing(plan, page_sizes, config=config)
    initial = placements_from_plan(plan)  # same reading order as `packed`
    new_plan = copy.deepcopy(list(plan or []))
    for entry, orig, target in zip(new_plan, initial, packed):
        if not isinstance(entry, dict):
            continue
        delta = target.resolved_bbox[1] - orig.resolved_bbox[1]
        if abs(delta) > _TOL:
            _shift_entry_v(entry, delta)
    return new_plan, report