"""Cross-block page-flow analysis — Commit 7F-8a / 7F-8b.

7F-8a is the first step of the cross-block / cross-page recovery phase.  It
builds the **spatial relationship layer** between already-settled blocks::

    LayoutResult
        ↓ adaptive_layout / lay_out          (local recovery — single primitive)
    settled render plan
        ↓ this module                        (global recovery — detection only)
    BlockPlacement / PageCollision / PageOverflow
        ↓ 7F-8c+                             (SHIFT_Y / NEXT_PAGE — later commits)

7F-8b extends the analysis to **resolved geometry**: the resolved placement of
an entry is the settled drawn extent, not just the declared box.  A flow block
whose translation wraps to more lines than its source box occupies space below
the box's bottom edge — exactly the translation-inflated collision that 8a's
declared-box view could not see::

    source:  A ─────          resolved:  A ─────────────
                  gap                              
             B ─────                      B ─────        ← A 的译文撑高

``BlockPlacement.resolved_bbox`` is therefore ``dst_box`` **extended downward**
by the settled payload's drawn lines (the LayoutResult's own geometry, read
verbatim from the render payload — never re-laid-out).  ``bbox`` stays the
pure source geometry.  Detection runs in two modes:

- ``bbox_mode="resolved"`` (default, 7F-8b) — uses ``resolved_bbox``;
- ``bbox_mode="source"``   (7F-8a view)      — uses ``bbox``.

so the report can answer *where a collision came from*:
``source_collision_count`` (the source document already overlapped) vs
``resolved_collision_count`` (the translation created the overlap).

The module answers the DoD question directly:

> 第几页、第几个 block，与哪个 block 发生了多少 pt 的碰撞（source 还是
> resolved 几何）。

The five adjacency cases are kept distinct:

- 正常相邻 (normal adjacency)      → no record (gap / touching / separate columns);
- 真正重叠 (real overlap)          → ``reason="overlap"``;
- block 超出 page bottom / top     → :class:`PageOverflow` (page-boundary record);
- PreservedRegion                 → ``reason="preserved_region"`` (immovable —
                                   the collision can never be resolved by shift);
- continuation                    → ``reason="continuation"`` (list / TOC block
                                   whose payload draws continuation lines).

Hard rules (enforced by ``tests/test_page_flow_7f8a.py`` /
``tests/test_page_flow_7f8b.py``):

- **pure read of the settled plan** — never re-lays-out (no ``lay_out`` /
  ``adaptive_layout``), never moves a block, never mutates an entry, never
  writes geometry back into the plan;
- **no detector / parser / renderer imports** — the semantic kind is read
  verbatim off the settled entry;
- **no geometry re-derivation** — ``level → x`` / ``index → y`` /
  ``content_x`` / ``title_x`` / ``continuation_x`` are never recomputed here;
  every number comes from the settled payload or the entry verbatim;
- **resolved only extends downward** — the declared box is the anchor; the
  drawn lines may spill below its bottom edge (7F-8 moves only Y), never above
  and never sideways;
- **coordinate-agnostic** — boxes are normalized to ``(x0, bottom, x1, top)``
  with ``top >= bottom``, so the collision math never depends on whether the
  caller's y axis points up or down.

Nothing here changes any PDF output: the renderer never sees this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "BlockPlacement",
    "PageCollision",
    "PageOverflow",
    "PageFlowReport",
    "PRESERVE_KINDS",
    "placements_from_plan",
    "detect_collisions_from_placements",
    "detect_page_collisions",
    "detect_page_overflows",
    "build_page_flow_report",
]

#: Kinds whose geometry is immutable during recovery — a semantic preserved
#: region (code / formula / …) or a fixed column (TOC ``page_x``).  A
#: collision involving one of these can never be resolved by shifting; it is
#: flagged ``preserved_region`` so later recovery stages skip it.
PRESERVE_KINDS = frozenset(
    {
        "code", "formula", "figure", "image", "table", "header", "footer",
        "formula_inline", "column",
    }
)
_PRESERVE_KINDS = PRESERVE_KINDS  # back-compat alias

#: Descender estimate below a settled line baseline (fraction of font size),
#: used to turn the payload's baseline positions into a drawn bottom edge.
_DESCENT_RATIO = 0.25

_TOL = 1e-6

#: Collision-geometry modes: resolved uses the settled drawn extent (7F-8b),
#: source uses the original semantic box (7F-8a view).
_BBOX_MODES = ("resolved", "source")


def _normalize_box(box) -> tuple:
    """``(x0, bottom, x1, top)`` with ``top >= bottom``, y-direction agnostic."""
    if not box:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        x0, y0, x1, y1 = (float(v) for v in tuple(box)[:4])
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _drawn_bottom(entry: dict) -> Optional[float]:
    """Lowest edge (v3 y-up) of the block's settled drawn text, or ``None``.

    Reads the settled payload commands — the positions the layout layer
    already decided (from the LayoutResult) — verbatim; it never re-lays-out
    and never re-derives positions from level / index.  Returns ``None`` when
    there is no command geometry to read.
    """
    payload = entry.get("render_payload")
    if not isinstance(payload, dict):
        return None
    cmds = payload.get("commands")
    if not isinstance(cmds, list) or not cmds:
        return None
    ys: list[float] = []
    for c in cmds:
        if isinstance(c, dict) and "y" in c:
            try:
                ys.append(float(c.get("y", 0.0)))
            except (TypeError, ValueError):
                continue
    if not ys:
        return None
    fs = 0.0
    for v in (payload.get("font_size"), entry.get("font_size")):
        if isinstance(v, (int, float)) and v > 0:
            fs = float(v)
            break
    return min(ys) - fs * _DESCENT_RATIO


def _resolve_bbox(entry: dict, dst: tuple) -> tuple:
    """Refine the resolved bbox with the settled drawn extent (7F-8b).

    Only ever extends the declared box **downward**: wrapped translated lines
    may spill below the source box's bottom edge when the translation is
    taller than the source geometry.  The top edge and both x edges stay
    verbatim (7F-8 moves only Y; horizontal geometry is never re-derived).
    """
    drawn = _drawn_bottom(entry)
    if drawn is None or drawn >= dst[1] - _TOL:
        return dst
    return (dst[0], drawn, dst[2], dst[3])


def _has_continuation(entry: dict) -> bool:
    """True when the settled payload draws continuation lines (list / TOC).

    Continuation lines render below the entry's declared bbox, so a collision
    *involving* such a block is classified ``continuation`` rather than a plain
    overlap — later stages must account for the drawn extent, not the bbox.
    """
    items = entry.get("list_items")
    if isinstance(items, dict):
        for it in items.get("items") or []:
            if isinstance(it, dict) and it.get("continuation"):
                return True
    payload = entry.get("render_payload")
    if isinstance(payload, dict):
        for e in payload.get("entries") or []:
            if isinstance(e, dict) and e.get("continuation"):
                return True
    for e in (entry.get("toc_entries") or []) or []:
        if isinstance(e, dict) and e.get("continuation"):
            return True
    return False


@dataclass(frozen=True)
class BlockPlacement:
    """One settled block's placement on its page (pure read of the plan).

    ``bbox`` is the original (source) geometry; ``resolved_bbox`` is where the
    block is placed after layout — in 7F-8a nothing has moved yet, so it equals
    the source; 7F-8b+ will carry the shifted placement here.

    ``block_index`` is the structured **per-page reading-order ordinal** (see
    :func:`placements_from_plan`) — ``(page, block_index)`` uniquely identifies
    a placement; it is never derived from the ``block_id`` string.
    """

    block_index: int
    page: int
    kind: str
    bbox: tuple
    resolved_bbox: tuple
    height: float
    preserved: bool = False
    has_continuation: bool = False

    @property
    def bottom(self) -> float:
        """Bottom edge (smaller y) of the resolved placement."""
        return self.resolved_bbox[1]

    @property
    def top(self) -> float:
        """Top edge (larger y) of the resolved placement."""
        return self.resolved_bbox[3]

    @property
    def left(self) -> float:
        return self.resolved_bbox[0]

    @property
    def right(self) -> float:
        return self.resolved_bbox[2]

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "block_index": self.block_index,
            "kind": self.kind,
            "bbox": [round(v, 2) for v in self.bbox],
            "resolved_bbox": [round(v, 2) for v in self.resolved_bbox],
            "height": round(self.height, 2),
            "preserved": self.preserved,
            "has_continuation": self.has_continuation,
        }


@dataclass(frozen=True)
class PageCollision:
    """One vertical collision between two adjacent blocks on a page.

    ``overlap`` is the actual vertical intersection in points; ``required_shift``
    is how far the **lower** block must move down (v3 y-up: decreasing y) so its
    top edge clears the upper block's bottom edge — the 7F-8c recovery input.

    ``bbox_mode`` says which geometry produced the collision: ``"resolved"``
    (the settled drawn extent — the translation itself caused it) or
    ``"source"`` (the source document already overlapped).
    """

    page: int
    upper: BlockPlacement
    lower: BlockPlacement
    overlap: float
    required_shift: float
    reason: str
    bbox_mode: str = "resolved"

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "upper": self.upper.to_dict(),
            "lower": self.lower.to_dict(),
            "overlap": round(float(self.overlap), 2),
            "required_shift": round(float(self.required_shift), 2),
            "reason": self.reason,
            "bbox_mode": self.bbox_mode,
        }


@dataclass(frozen=True)
class PageOverflow:
    """A block whose resolved extent crosses the page edge (v3 y-up: bottom 0,
    top = page height).  ``direction`` is ``"bottom"`` / ``"top"`` and
    ``amount`` is how far the edge is crossed in points — the 7F-8c trigger
    for a NEXT_PAGE decision.
    """

    page: int
    block: BlockPlacement
    direction: str
    amount: float

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "block": self.block.to_dict(),
            "direction": self.direction,
            "amount": round(float(self.amount), 2),
        }


def placements_from_plan(plan) -> list[BlockPlacement]:
    """Settled plan entries → one :class:`BlockPlacement` per entry.

    Pure read: ``bbox`` / ``resolved_bbox`` are copied verbatim (never
    recomputed), the kind is read off the entry, and the preserved /
    continuation flags describe the block's *recovery rigidity*, not semantics.

    ``block_index`` is the block's **per-page reading-order ordinal** — its
    0-based position among the page's entries in plan order (7F-9.1).  It is a
    structured property of the settled plan itself, never guessed from the
    ``block_id`` string; ``block_id`` stays identity / debug information only.
    ``(page, block_index)`` is therefore a unique, stable placement key even
    for entries whose ``block_id`` is not ``p{page}_{number}`` — unparseable
    IDs can never silently collapse to ``0`` and share a key.
    """
    out: list[BlockPlacement] = []
    page_counts: dict[int, int] = {}
    for entry in plan or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "")
        src = _normalize_box(entry.get("src_box") or entry.get("bbox"))
        dst = _normalize_box(entry.get("dst_box") or src)
        # 7F-8b: the resolved placement is the settled drawn extent — dst_box
        # extended downward by the payload's drawn lines (read-only analysis;
        # the plan entry itself is never touched).
        resolved = _resolve_bbox(entry, dst)
        page = int(entry.get("page") or 0)
        ordinal = page_counts.get(page, 0)
        page_counts[page] = ordinal + 1
        out.append(
            BlockPlacement(
                block_index=ordinal,
                page=page,
                kind=kind,
                bbox=src,
                resolved_bbox=resolved,
                height=resolved[3] - resolved[1],
                preserved=kind in _PRESERVE_KINDS,
                has_continuation=_has_continuation(entry),
            )
        )
    return out


def _collision_reason(upper: BlockPlacement, lower: BlockPlacement) -> str:
    """Classify a detected collision (see module docstring).  Preserved regions
    win over continuation — immovable geometry is the more severe constraint."""
    if upper.preserved or lower.preserved:
        return "preserved_region"
    if upper.has_continuation or lower.has_continuation:
        return "continuation"
    return "overlap"


def _inline_membership(
    upper: BlockPlacement,
    lower: BlockPlacement,
    ub: tuple,
    lb: tuple,
    tolerance: float,
) -> bool:
    """True when an adjacent pair is in-line containment, not a real stacked
    collision — P0-2 containment-aware adjacency.

    Two noisy signatures from real-PDF segmentation are excluded:

    - **horizontal containment** — one block's x-extent is a *strict* subset of
      the other's (``x0``/``x1``), i.e. the inline-membership pattern of a
      small formula / inline block sitting inside its paragraph.  A same-column
      stacked paragraph (identical x-range) is NOT contained and is still a
      collision; only proper-nesting is in-line.
    - **``formula_inline`` kind** — an inline-formula block is by construction
      inside its container, so stacking adjacency against it is never a real
      vertical collision recovery could fix with SHIFT_DOWN.

    Pure read; never mutates anything.
    """
    if upper.kind == "formula_inline" or lower.kind == "formula_inline":
        return True
    u_in_l = ub[0] > lb[0] + tolerance and ub[2] < lb[2] - tolerance
    l_in_u = lb[0] > ub[0] + tolerance and lb[2] < ub[2] - tolerance
    return u_in_l or l_in_u


def detect_collisions_from_placements(
    placements,
    *,
    bbox_mode: str = "resolved",
    tolerance: float = _TOL,
) -> list[PageCollision]:
    """Core pair-testing over already-built placements (7F-8d cascade input).

    For every page, adjacent pairs in reading order are tested::

        upper = placements[i]
        lower = placements[i+1]

    A collision exists when the blocks overlap **vertically** (``upper.bottom <
    lower.top`` — the y-up form of "A.bottom > B.top" in y-down coordinates)
    **and** horizontally (they share a column; side-by-side blocks are not
    collisions).  Pairs where the earlier block sits below the later one
    (two-column interleave) are skipped.

    Args:
        bbox_mode: ``"resolved"`` (default, 7F-8b) tests the settled drawn
            extent (``resolved_bbox``); ``"source"`` tests the pure source
            geometry (``bbox``) — the 7F-8a view.  The mode is recorded on
            every returned :class:`PageCollision`.

    Returns:
        One :class:`PageCollision` per colliding adjacent pair with the exact
        overlap (pt), the required downward shift of the lower block, and the
        ``bbox_mode`` that produced it.  Never mutates the placements.
    """
    if bbox_mode not in _BBOX_MODES:
        raise ValueError(f"bbox_mode must be one of {_BBOX_MODES}, got {bbox_mode!r}")
    pages: dict[int, list[BlockPlacement]] = {}
    for p in placements:
        pages.setdefault(p.page, []).append(p)
    out: list[PageCollision] = []
    for page in sorted(pages):
        blocks = pages[page]
        for upper, lower in zip(blocks, blocks[1:]):
            if bbox_mode == "resolved":
                ub, lb = upper.resolved_bbox, lower.resolved_bbox
            else:
                ub, lb = upper.bbox, lower.bbox
            u_bottom, u_top = ub[1], ub[3]
            l_bottom, l_top = lb[1], lb[3]
            if u_top < l_top:
                continue  # not stacked (two-column interleave)
            v_overlap = min(u_top, l_top) - max(u_bottom, l_bottom)
            h_overlap = min(ub[2], lb[2]) - max(ub[0], lb[0])
            if v_overlap <= tolerance or h_overlap <= tolerance:
                continue  # normal adjacency (gap / touching) or separate columns
            # P0-2 containment-aware adjacency: an in-line contained box
            # (inline formula / embedded block) is not a stacked sibling — a
            # real vertical overlap recovery could clear with SHIFT_DOWN.
            if _inline_membership(upper, lower, ub, lb, tolerance):
                continue
            required_shift = max(0.0, l_top - u_bottom)
            out.append(
                PageCollision(
                    page=page,
                    upper=upper,
                    lower=lower,
                    overlap=round(v_overlap, 2),
                    required_shift=round(required_shift, 2),
                    reason=_collision_reason(upper, lower),
                    bbox_mode=bbox_mode,
                )
            )
    return out


def detect_page_collisions(
    plan,
    *,
    bbox_mode: str = "resolved",
    tolerance: float = _TOL,
) -> list[PageCollision]:
    """Detect vertical collisions between adjacent blocks on each page (8a/8b).

    Thin wrapper over :func:`detect_collisions_from_placements` — the single
    detection authority.  See that function for semantics.
    """
    return detect_collisions_from_placements(
        placements_from_plan(plan), bbox_mode=bbox_mode, tolerance=tolerance
    )


def detect_page_overflows(
    plan,
    page_sizes: Optional[dict] = None,
    *,
    tolerance: float = _TOL,
) -> list[PageOverflow]:
    """Blocks whose resolved extent crosses the page edge.

    ``page_sizes`` maps ``page -> height`` in v3 y-up (bottom edge 0, top edge
    ``height``).  Without it no page-boundary check is possible and the list is
    empty — page-bottom overflow is exactly the trigger 7F-8c turns into a
    NEXT_PAGE decision, so detecting it now is pure observability.
    """
    sizes = dict(page_sizes or {})
    out: list[PageOverflow] = []
    for p in placements_from_plan(plan):
        ph = sizes.get(p.page)
        if ph is None or ph <= 0.0:
            continue
        if p.bottom < -tolerance:
            out.append(
                PageOverflow(page=p.page, block=p, direction="bottom",
                             amount=round(-p.bottom, 2))
            )
        if p.top > ph + tolerance:
            out.append(
                PageOverflow(page=p.page, block=p, direction="top",
                             amount=round(p.top - ph, 2))
            )
    return out


@dataclass
class PageFlowReport:
    """Consolidated cross-block report: placements + collisions + overflows.

    ``collisions`` are the 7F-8b resolved-geometry records; ``source_collision_count``
    is how many of the page's collisions already existed in the source geometry
    (computed in a separate ``bbox_mode="source"`` pass), so the summary can
    answer *where the collision came from* without a second analysis pass later.
    """

    placements: list = field(default_factory=list)
    collisions: list = field(default_factory=list)
    overflows: list = field(default_factory=list)
    source_collision_count: int = 0

    def summary(self) -> dict:
        by_reason: dict[str, int] = {}
        for c in self.collisions:
            by_reason[c.reason] = by_reason.get(c.reason, 0) + 1
        return {
            "blocks": len(self.placements),
            "collision_count": len(self.collisions),
            "resolved_collision_count": len(self.collisions),
            "source_collision_count": int(self.source_collision_count),
            "page_overflow_count": len(self.overflows),
            "by_reason": dict(sorted(by_reason.items())),
        }

    def to_dict(self) -> dict:
        return {
            "placements": [p.to_dict() for p in self.placements],
            "collisions": [c.to_dict() for c in self.collisions],
            "overflows": [o.to_dict() for o in self.overflows],
            "summary": self.summary(),
        }


def build_page_flow_report(plan, page_sizes: Optional[dict] = None) -> PageFlowReport:
    """One consolidated page-flow report (placements + collisions + overflows).

    ``collisions`` use the 7F-8b resolved geometry; a second source-mode pass
    feeds ``source_collision_count`` so the summary distinguishes
    translation-caused collisions from source-document ones.  Pure read of the
    settled plan; safe to call on any render plan.
    """
    return PageFlowReport(
        placements=placements_from_plan(plan),
        collisions=detect_page_collisions(plan),
        overflows=detect_page_overflows(plan, page_sizes=page_sizes),
        source_collision_count=len(detect_page_collisions(plan, bbox_mode="source")),
    )
