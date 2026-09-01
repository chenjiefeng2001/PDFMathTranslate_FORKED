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
import unicodedata
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
#: Conservative ascent above a settled line baseline (fraction of font size).
#: The packer's gap math must clear the block's REAL glyph extent, not just the
#: resolved bbox: a block whose settled command lines rise above ``dst_box``
#: (source segmentation noise) would otherwise be compacted by bbox while its
#: drawn glyphs overlap the neighbour below (7G-2.1 P0).
_GLYPH_ASCENT = 0.8
#: Conservative descent below a settled line baseline (fraction of font size).
#: The lowest baseline's glyphs dip ``_DESCENT_RATIO * fs`` below it; the packer's
#: occupied bottom must clear that or the words of the last line overlap the
#: neighbour below (7G-2.2 geometry parity, command-block side).
_DESCENT_RATIO = 0.25
#: The renderer's ``_insert_text_wrapped`` legacy fallback steps lines at
#: ``font_size * 1.4`` (``magicpdf_renderer``).  The packer reproduces ONLY
#: that line step as a pure occupancy estimate — it never lays out.
_LINE_HEIGHT = 1.4


def _round2(v: float) -> float:
    return round(float(v), 2)


def _boxes_h_overlap(a: tuple, b: tuple) -> bool:
    """True when two ``(x0, bottom, x1, top)`` boxes share an x-band."""
    return min(a[2], b[2]) - max(a[0], b[0]) > _TOL


def _spill_of(p, occ_by_id: Optional[dict]) -> float:
    """A placement's conservative bottom spill (pt below its dst box), or 0.

    Shared by compaction's `_occ_bottom` and re-anchor's page-bottom floor
    (7G-2.2).  ``occ_by_id`` is ``{id(placement): spill}``; absent/malformed
    entries are 0 (the exact bbox behaviour).
    """
    if occ_by_id is None:
        return 0.0
    try:
        return max(0.0, float(occ_by_id.get(id(p)) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _glyph_excess(entry: dict, dst_top: float) -> float:
    """How far the settled command lines' real glyph tops rise above ``dst_top``.

    Pure read of the settled payload — the positions the layout layer already
    decided (from the LayoutResult), never re-laid-out.  The block's topmost
    line baseline plus a conservative ascent is the real glyph extent; the
    excess is that extent minus the declared ``dst_box`` top, clamped at 0
    (a well-formed block whose glyphs sit inside its box has no excess).  When
    a block has no command geometry (legacy ``_insert_text_wrapped`` fallback)
    there is nothing to read and the excess is 0 — the bbox behaviour.
    """
    payload = entry.get("render_payload")
    if not isinstance(payload, dict):
        return 0.0
    cmds = payload.get("commands")
    if not isinstance(cmds, list) or not cmds:
        return 0.0
    top_y: Optional[float] = None
    for c in cmds:
        if isinstance(c, dict) and "y" in c:
            try:
                y = float(c.get("y", 0.0))
            except (TypeError, ValueError):
                continue
            if top_y is None or y > top_y:
                top_y = y
    if top_y is None:
        return 0.0
    fs = 0.0
    for v in (payload.get("font_size"), entry.get("font_size")):
        if isinstance(v, (int, float)) and v > 0:
            fs = float(v)
            break
    if fs <= 0.0:
        return 0.0
    return _round2(max(0.0, top_y + fs * _GLYPH_ASCENT - float(dst_top)))


def _glyph_excess_by_key(plan) -> dict:
    """``{(page, block_index): glyph_excess}`` for every plan entry.

    Same reading order as :func:`placements_from_plan` (7F-9.1) — ``plan``
    order, block_index is the per-page ordinal.  Pure read of the settled
    payload; entries with zero excess (or no command geometry) are absent.
    """
    out: dict = {}
    counts: dict[int, int] = {}
    for e in plan or []:
        if not isinstance(e, dict):
            continue
        page = int(e.get("page") or 0)
        idx = counts.get(page, 0)
        counts[page] = idx + 1
        dst = e.get("dst_box") or e.get("src_box")
        if not isinstance(dst, (list, tuple)) or len(dst) < 4:
            continue
        try:
            dst_top = max(float(dst[1]), float(dst[3]))
        except (TypeError, ValueError):
            continue
        ex = _glyph_excess(e, dst_top)
        if ex > 0.0:
            out[(page, idx)] = ex
    return out


# ---------------------------------------------------------------------------
# 7G-2.2 — conservative occupied draw-extent (bottom spill)
# ---------------------------------------------------------------------------
#
# The packer's occupied geometry must cover the renderer's ACTUAL drawn
# extent, not just the resolved ``dst_box`` ("declared geometry == drawn
# geometry" parity).  Two geometry worlds disagree for a block that falls back
# to the legacy ``_insert_text_wrapped`` path (empty ``render_payload.commands``)::
#
#     packer sees  dst_box / settled bbox
#     renderer draws  lines at 1.4 * font_size re-wrapped against box width
#
# A heading / TOC / plain-paragraph box is sized from SOURCE geometry; when its
# text re-wraps at the taller legacy line height the drawn glyphs spill BELOW
# the box bottom by up to roughly a line, so compaction closing the *bbox* gap
# to ``gutter`` still leaves the *words* overlapping.  ``_glyph_excess`` cannot
# see it (no command geometry to read) — the V2 render gate's residual 3,355
# overlap class (report §11.4).
#
# The fix is a conservative OCCUPANCY EXTENT per block (7G-2.2 方案 A): for a
# command-less block estimate the wrapped line count from ``text`` +
# ``font_size`` + ``dst_box.width`` (pure metric read — the renderer's own
# token-wrap rule), derive ``occupied_height = max(dst_box.height,
# lines * 1.4 * font_size)`` and treat the block as that tall.  It never
# re-lays-out, never changes X / font / text / commands; it only makes the
# packer's gap math respect the drawn bottom.  No ``level`` / ``index`` math,
# no renderer / pymupdf import — every width is a settled field.


def _char_advance(ch: str) -> float:
    """Per-glyph advance as a fraction of font_size (pure metric read).

    Mirrors the typographer's width model (``render_takeover``): full/fullwidth
    glyphs ≈ 1em, latin ≈ 0.5em, space ≈ 0.28em.  Deterministic and CJK-safe —
    no pymupdf / font-file dependency.
    """
    if ch == " ":
        return 0.28
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5


def _estimate_wrapped_lines(text: str, font_size: float, max_w: float) -> int:
    """Reproduce the renderer's ``_insert_text_wrapped`` token-wrap line count.

    Exactly the legacy fallback's rule: words are accumulated onto a line until
    adding the next token would exceed ``max_w``, then a new line starts; a
    line is emitted only when it has content.  Pure count — nothing is drawn,
    positioned or re-laid-out.  ``max_w <= 0`` / empty text → 1 (the degenerate
    single line the renderer always emits).
    """
    if not isinstance(text, str) or not text or max_w <= 0.0 or font_size <= 0.0:
        return 1
    lines = 0
    cur_w = 0.0
    cur_has = False
    for tok in text.split(" "):
        tok_w = sum(_char_advance(c) for c in tok) * font_size
        if not cur_has:
            cur_w = tok_w
            cur_has = True
            continue
        if cur_w + 0.28 * font_size + tok_w > max_w:
            lines += 1
            cur_w = tok_w
        else:
            cur_w += 0.28 * font_size + tok_w
    if cur_has:
        lines += 1
    return max(1, lines)


def _entry_occupied_bottom_spill(entry: dict) -> float:
    """How far a block's drawn glyphs dip BELOW its ``dst_box`` bottom (pt).

    Read straight from the settled payload where geometry exists; estimated
    conservatively where it does not:

    - **command block** (settled ``render_payload.commands``): spill is the
      lowest baseline's glyph bottom (``min_y - descent``) below ``dst_bottom``;
    - **command-less block** (legacy ``_insert_text_wrapped`` fallback): spill
      is ``max(0, lines * 1.4 * fs - dst_box.height)`` — the wrapped extent
      minus the declared box height (7G-2.2 方案 A).

    Pure read, ``0.0`` returned for anything missing / malformed.  Never
    mutates, never lays out.
    """
    fs = 0.0
    if isinstance(entry.get("font_size"), (int, float)) and entry["font_size"] > 0:
        fs = float(entry["font_size"])
    dst = entry.get("dst_box") or entry.get("src_box")
    if not isinstance(dst, (list, tuple)) or len(dst) < 4 or fs <= 0.0:
        return 0.0
    try:
        dst_top = max(float(dst[1]), float(dst[3]))
        dst_bottom = min(float(dst[1]), float(dst[3]))
    except (TypeError, ValueError):
        return 0.0
    box_h = dst_top - dst_bottom
    payload = entry.get("render_payload")
    cmds = None
    if isinstance(payload, dict):
        cmds = payload.get("commands")
    if isinstance(cmds, list) and cmds:
        min_y: Optional[float] = None
        for c in cmds:
            if not isinstance(c, dict) or not isinstance(c.get("y"), (int, float)):
                continue
            y = float(c["y"])
            if min_y is None or y < min_y:
                min_y = y
        if min_y is None:
            return 0.0
        # v3 y-up: the lowest baseline is the SMALLEST y; glyphs dip ``descent``
        # below it.  Spill only when that glyph bottom crosses the box bottom.
        glyph_bottom = min_y - fs * _DESCENT_RATIO
        return _round2(max(0.0, dst_bottom - glyph_bottom))
    width = float(dst[2]) - float(dst[0])
    if width <= 0.0:
        return 0.0
    text = entry.get("translated") or entry.get("text") or ""
    lines = _estimate_wrapped_lines(text, fs, width)
    est_h = lines * fs * _LINE_HEIGHT
    return _round2(max(0.0, est_h - box_h))


def _occupied_bottom_spill_by_key(plan) -> dict:
    """``{(page, block_index): bottom_spill}`` for every plan entry.

    Same reading order as :func:`placements_from_plan` and
    :func:`_glyph_excess_by_key`; entries with zero spill (or no geometry) are
    absent.
    """
    out: dict = {}
    counts: dict[int, int] = {}
    for e in plan or []:
        if not isinstance(e, dict):
            continue
        page = int(e.get("page") or 0)
        idx = counts.get(page, 0)
        counts[page] = idx + 1
        sp = _entry_occupied_bottom_spill(e)
        if sp > 0.0:
            out[(page, idx)] = sp
    return out


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
    glyph_excess: Optional[list] = None,
    other_barriers: Optional[list] = None,
    occ_by_id: Optional[dict] = None,
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

    ``glyph_excess`` (parallel to ``placements``) is each block's real glyph
    extent rising above its ``dst_box`` top (see :func:`_glyph_excess`) —
    ``None`` entries mean 0.  The pull is reduced by that excess: a block
    whose drawn lines poke above its declared top is NOT pulled up until its
    real glyphs (not its bbox) touch the block above.  Without this the 7G-2
    compaction could close a gap the *bboxes* had while the *words* already
    overlapped — the V2 render gate's word-level overlap (7G-2.1 P0).

    ``occ_by_id`` (``{id(placement): pt}``) is the same parity that ``excess``
    gives the TOP side, for the BOTTOM side (7G-2.2): a block whose drawn glyphs
    dip BELOW its ``dst_box`` bottom (legacy ``_insert_text_wrapped`` text
    spilling past a source-sized box) must not be packed by its box bottom.  The
    compaction reference — the block ABOVE's bottom edge, the cross-floor
    ``final_bottom`` and the other-column barriers — is the block's conservative
    occupied bottom (``resolved_bottom - spill``), so a lower block is not
    pulled up until its real glyphs clear the upper block's real drawn extent.

    7G-2.1 cross-floor: a block is also never pulled up past the bottom edge
    of ANY horizontally-overlapping block above it, not just the previous
    block in the reading-order chain.  When full-width paragraphs bridge two
    visual columns, :func:`page_columns` clusters both chains into ONE band;
    the -top processing order then interleaves them, and a block could be
    pulled up into a NON-parent overlapping block (its other column's
    neighbour) — dst boxes stay disjoint while the words overlap.  The pull
    is bounded by the nearest such overlapping block's already-computed final
    bottom (blocks above are processed first, so their deltas are known).

    ``other_barriers`` are the page's OTHER columns' placements (original,
    settled geometry): the same cross-floor applies to them at their original
    bottoms — a compaction pass must never pull a block up past an
    horizontally-overlapping neighbour that lives in a different column band
    (tiny fragments cluster into their own columns via the strict x-overlap
    threshold, so the neighbour would otherwise be invisible to the chain).

    Pure read of resolved geometry; returns a list parallel to ``placements``.
    """
    order = sorted(enumerate(placements), key=lambda ie: (-ie[1].top, ie[1].bottom))
    deltas: dict[int, float] = {}
    final_bottom: dict[int, float] = {}
    prev_bottom: Optional[float] = None
    prev_preserved = False
    same_ids = {id(q) for q in placements}

    def _occ_bottom(p, dy: float = 0.0) -> float:
        """Conservative drawn bottom: resolved bottom minus how far this block's
        wrapped glyphs dip below its dst box (7G-2.2).  Zero spill (or no entry)
        keeps the exact 7G-2/7G-2.1 bbox behaviour."""
        return (p.bottom + dy) - _spill_of(p, occ_by_id)

    def _cross_bound(p, ex, cross, q, bottom_q, clr2):
        """Tighten ``cross`` by a horizontally-overlapping block above p."""
        if not _boxes_h_overlap(p.resolved_bbox, q.resolved_bbox):
            return cross
        bound = bottom_q - clr2
        if cross is None or bound < cross:
            cross = bound
        return cross

    for idx, p in order:
        ex = 0.0
        if glyph_excess is not None and idx < len(glyph_excess):
            try:
                ex = max(0.0, float(glyph_excess[idx] or 0.0))
            except (TypeError, ValueError):
                ex = 0.0
        if p.preserved:
            deltas[idx] = 0.0
            prev_bottom = _occ_bottom(p)
            prev_preserved = True
            final_bottom[idx] = _occ_bottom(p)
            continue
        if prev_bottom is None:
            # topmost movable block anchors the column
            deltas[idx] = 0.0
            prev_bottom = _occ_bottom(p)
            prev_preserved = p.preserved
            final_bottom[idx] = _occ_bottom(p)
            continue
        g = preserved_gutter if prev_preserved else gutter
        target_top = prev_bottom - g
        dy = max(0.0, target_top - p.top - ex)
        # cross-floor: nearest already-moved horizontally-overlapping block
        # above p (any chain in the band) — p may never cross its bottom
        cross = None
        for qidx, q in enumerate(placements):
            if qidx not in deltas:
                continue  # below p — not yet processed
            if q is p or q.top <= p.top + _TOL:
                continue
            clr2 = preserved_gutter if q.preserved else gutter
            cross = _cross_bound(p, ex, cross, q, final_bottom[qidx], clr2)
        for q in other_barriers or []:
            if id(q) in same_ids or q is p:
                continue
            if q.top <= p.top + _TOL:
                continue
            clr2 = preserved_gutter if q.preserved else gutter
            cross = _cross_bound(p, ex, cross, q, _occ_bottom(q), clr2)
        if cross is not None:
            dy = min(dy, max(0.0, cross - p.top - ex))
        deltas[idx] = _round2(dy)
        prev_bottom = _occ_bottom(p, dy)
        prev_preserved = p.preserved
        final_bottom[idx] = _occ_bottom(p, dy)
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
    gutter: float = 2.0,
    max_reclaim: Optional[float] = None,
    other_barriers: Optional[list] = None,
    barrier_excess: Optional[dict] = None,
    occ_by_id: Optional[dict] = None,
) -> float:
    """Downward delta (v3 y-up: ``-``) for one compacted column, bounded so no
    movable block crosses the page bottom or a neighbour block below it.

    ``placements`` are the column's blocks AFTER compaction.  The whole column
    is moved DOWN (decreasing y) by at most the space to the nearest downward
    barrier:

    - the page-bottom floor ``bottom_margin``;
    - a **preserved** block below in the same column (clearance
      ``preserved_gutter``) — unchanged from 7G-2;
    - any **movable neighbour** from another column on the same page
      (``other_barriers``, clearance ``gutter``) — 7G-2.1 P0.  Before this
      guard the re-anchor floor only knew preserved blocks, so it could push a
      compacted column DOWN onto a movable neighbour (a page-number block, a
      footer paragraph, another column's content) long after the *block-level*
      collision gate stopped firing — the word-level overlap the V2 render gate
      caught.  A neighbour column's content is now a hard floor the column may
      not descend past.

    ``barrier_excess`` (``{id(q): pt}``) lets a below barrier's REAL glyph top
    floor the descent: a neighbour whose drawn lines poke above its ``dst_box``
    top (``_glyph_excess``) is visually higher than its bbox — descending
    until the bbox clears it would still let the words overlap.  7G-2.1 P0.

    Extra cap via ``max_reclaim``.  Returns a non-positive float (the re-anchor
    delta), 0 when nothing is safely reclaimable.  Preserved blocks are never
    moved; they only bound how far the movable content below them may descend.
    """
    if not placements:
        return 0.0
    # downward barrier y (v3 y-up: higher = closer to page top) per movable block
    # = the nearest block below it that the column must not descend past.  Barriers
    # are preserved blocks (any column) and movable neighbours from OTHER columns;
    # the column's own movable stack moves together so it never self-floors.
    movable = [p for p in placements if not p.preserved]
    if not movable:
        return 0.0
    same_ids = {id(p) for p in placements}
    barriers = list(placements)  # preserved-in-column + (other cols passed below)
    for q in other_barriers or []:
        if id(q) not in same_ids:
            barriers.append(q)
    # per-block allowance: how far the block may descend before hitting its
    # nearest real barrier.  Each horizontally-overlapping barrier q bounds p's
    # descent by its REAL glyph top (dst top + excess) — 7G-2.1 P0 — so the
    # descending column never lands on q's words:
    #   - q entirely BELOW p (q.glyph_top < p.bottom): floor q.glyph_top + clr;
    #   - q entirely ABOVE p (q.glyph_bottom > p.bottom): no constraint
    #     (descending increases the gap);
    #   - q STRADDLES p's bottom (already overlapping): the block cannot
    #     descend at all — any move makes the word overlap worse (the tiny
    #     fragment re-anchoring down through a paragraph that already
    #     overlapped it).
    # A fully side-by-side column (two-column arxiv gutter) never horizontally
    # overlaps, so it never floors this column's reclaim.
    allow_map: dict[int, float] = {}
    for p in movable:
        pbox = p.resolved_bbox
        # the lowest block's REAL drawn bottom governs how far the column may
        # descend before its wrapped glyphs cross the page-bottom floor
        # (7G-2.2 parity: spill below dst_bottom counts)
        occ_bottom = p.bottom - _spill_of(p, occ_by_id)
        allow = max(0.0, occ_bottom - bottom_margin)
        for q in barriers:
            if q is p or id(q) == id(p):
                continue
            if not q.preserved and id(q) in same_ids:
                continue  # own movable stack — moves together
            if not _boxes_h_overlap(pbox, q.resolved_bbox):
                continue
            eff_top = q.top
            if barrier_excess is not None:
                try:
                    eff_top += max(0.0, float(barrier_excess.get(id(q), 0.0) or 0.0))
                except (TypeError, ValueError):
                    pass
            clr = preserved_gutter if q.preserved else gutter
            if eff_top < p.bottom - _TOL:
                # q is entirely below p's glyph bottom → floor by q's glyph top
                allow = min(allow, max(0.0, p.bottom - eff_top - clr))
            elif q.bottom > p.bottom + _TOL:
                pass  # q entirely above → descending only widens the gap
            else:
                allow = min(allow, 0.0)  # straddling → already overlapping
        allow_map[id(p)] = allow
    shift = None
    for p in movable:
        allow = allow_map.get(id(p), 0.0)
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
                {"page": pc[0], "col": pc[1], **sm.to_dict()} for pc, sm in self.pages
            ],
        }


def _metrics(placements, page_height: float):
    cols = page_columns(placements)
    total_gap = sum(
        c.internal_gap for c in (column_packing_metrics(c, page_height) for c in cols)
    )
    trail = [column_packing_metrics(c, page_height).trailing_gap for c in cols]
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

    # 7G-2.1 P0: per-block glyph excess (settled command lines rising above
    # ``dst_box`` top) — the geometry the block gate cannot see but the words
    # layer can.  Compaction pulls are reduced by it; re-anchor floors are
    # raised by it.
    excess_by_key = _glyph_excess_by_key(plan)
    # 7G-2.2: per-block bottom spill (drawn glyphs dipping below ``dst_box``
    # bottom — legacy ``_insert_text_wrapped`` re-wrap).  Compaction packs
    # against a block's OCCUPIED bottom; re-anchor's page-bottom floor uses it.
    spill_by_key = _occupied_bottom_spill_by_key(plan)

    # collect per-column deltas (up for compaction, down for re-anchor).
    # 7G-2.1 two-phase sequencing: ALL columns compact first, then ALL columns
    # re-anchor against the other columns' FINAL (compacted) positions — a
    # column's moves are never computed against a neighbour's pre-move geometry
    # (that was how a paragraph compacting UP and a tiny fragment re-anchoring
    # DOWN could slide past each other's original positions and overlap).
    per_key: dict[tuple, float] = {}
    for pg in sorted(pages):
        page_ph = float(sizes.get(pg, 0.0) or 0.0) or 792.0
        cols = page_columns(pages[pg])
        page_blocks = pages[pg]
        occ_by_id = {
            id(p): spill_by_key.get((pg, p.block_index), 0.0) for p in page_blocks
        }

        # phase 1 — compaction for every column; the cross-floor uses other
        # columns' ORIGINAL bottoms (conservative: compaction only moves up)
        col_up: dict[int, list] = {}
        col_compacted: dict[int, list] = {}
        for col in cols:
            col_ex = [
                excess_by_key.get((pg, p.block_index), 0.0) for p in col.placements
            ]
            own_ids = {id(x) for x in col.placements}
            others = [b for b in page_blocks if id(b) not in own_ids]
            deltas_up = (
                compact_column(
                    col.placements,
                    gutter=cfg.gutter,
                    preserved_gutter=cfg.preserved_gutter,
                    glyph_excess=col_ex,
                    other_barriers=others,
                    occ_by_id=occ_by_id,
                )
                if cfg.compact
                else [0.0] * len(col.placements)
            )
            col_up[id(col)] = deltas_up
            col_compacted[id(col)] = _applied(col.placements, deltas_up)

        # phase 2 — re-anchor for every column against the other columns'
        # COMPACTED placements (their compaction is already applied), so a
        # column never descends onto a neighbour that compaction just raised
        page_compacted = []
        page_excess: dict[int, float] = {}
        for col in cols:
            compacted = col_compacted[id(col)]
            page_compacted.extend(compacted)
            for i, c in enumerate(compacted):
                page_excess[id(c)] = excess_by_key.get(
                    (pg, col.placements[i].block_index), 0.0
                )
        for col in cols:
            compacted = col_compacted[id(col)]
            deltas_up = col_up[id(col)]
            comp_ids = {id(x) for x in compacted}
            barriers = [b for b in page_compacted if id(b) not in comp_ids]
            # the re-anchor floor belongs to the column's OWN compacted blocks
            # (new objects from `_moved`); key the occ map to those ids so the
            # lowest wrapped block's real bottom governs the descent floor
            occ_own = {
                id(c): spill_by_key.get((pg, col.placements[i].block_index), 0.0)
                for i, c in enumerate(compacted)
            }
            down = (
                column_reanchor(
                    compacted,
                    bottom_margin=cfg.bottom_margin,
                    preserved_gutter=cfg.preserved_gutter,
                    gutter=cfg.gutter,
                    max_reclaim=cfg.max_reclaim,
                    other_barriers=barriers,
                    barrier_excess=page_excess,
                    occ_by_id=occ_own,
                )
                if cfg.re_anchor
                else 0.0
            )
            for i, p in enumerate(col.placements):
                dy = deltas_up[i] + down if not p.preserved else 0.0
                per_key[(p.page, p.block_index)] = dy
            # per-column observability
            moved = sum(
                1
                for i, p in enumerate(col.placements)
                if not p.preserved and abs(deltas_up[i]) > _TOL
            )
            before_gap, before_trail = _metrics(col.placements, page_ph)
            after_gap, after_trail = _metrics(
                _applied(
                    col.placements,
                    [deltas_up[i] + down for i in range(len(col.placements))],
                ),
                page_ph,
            )
            report.pages.append(
                (
                    pg,
                    len(report.pages),
                    PackSummary(
                        before_internal_gap=before_gap,
                        after_internal_gap=after_gap,
                        before_trailing_gap=before_trail,
                        after_trailing_gap=after_trail,
                        moved_blocks=moved,
                        reclaimed_gap_pt=_round2(before_gap - after_gap),
                    ),
                )
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
            dst[0],
            _round2(float(dst[1]) + delta),
            dst[2],
            _round2(float(dst[3]) + delta),
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
