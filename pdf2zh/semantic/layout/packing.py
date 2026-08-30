"""Whitespace / page-packing measurement — 7G-2 V2 baseline.

The measurement half of Adaptive v2.  Where 7G-1 decides *where a block
belongs* (placement decision) and 7G-2 will optimise *page packing / whitespace
utilization*, this module only **measures** how much of each page's vertical
band is actually occupied by settled content — the number that says whether V2
packing is buying anything at all::

    settled plan
        ↓ placements_from_plan (7F-8a)  — pure read of resolved geometry
    page columns (x-overlap clustering)
        ↓ per-column used-height / gaps
    page & document packing report      — this module (7G-2 baseline)

It consumes ONLY already-settled geometry (:class:`BlockPlacement` from
:mod:`pdf2zh.semantic.layout.page_flow`), normalised to ``(x0, bottom, x1,
top)`` with ``bottom`` = page bottom (v3 y-up, bottom edge 0).  It never
re-lays-out, never moves a block, never writes a PDF, and never derives
position from ``block_id`` — a pure read that is safe to run on any settled
plan.

Why not "whitespace = page_height - last_block_bottom"?  (The P1 lesson from
``doc/corpus_layout_scan_7g_report.md``.)  Footers / page numbers sit at the
page bottom on real papers, so the raw trace down to ``y=0`` is ~0 and the
metric "barely fires" — meaningless.  Page packing is a **vertical-band**
question: within the vertical extent a column actually occupies (its topmost
and bottom-most content), how much of that band is filled by blocks vs frozen
in gaps that a packing pass *could* reclaim?  So every column reports:

- ``content_height`` — the column's used vertical band (top - bottom);
- ``fill_ratio``   — content_height / page_height (vertical band utilization);
- ``whitespace_ratio`` — ``1 - fill_ratio``;
- ``internal_gap`` — vertical gaps *between* stacked reading-order blocks in
  the column (packing-reclaimable, unlike header/footer geometry);
- ``trailing_gap`` — distance from the column's lowest block bottom down to
  the page bottom edge (available space at the bottom of the column).

The document-level report aggregates across pages/columns and answers the
7G-2 first question — *"how empty is the output today?"* — which is the
baseline V2 page packing must beat:

    {
      "pages": 15,
      "columns": 22,               # across all pages (usually 2/page)
      "avg_fill_ratio": 0.43,      # 43% of each column's vertical band filled
      "avg_trailing_gap_pt": 28.0, # free space below last block, per column
      "total_internal_gap_pt": 180.0,
      "per_page": [...],           # per-page column metrics
    }

Renderer-independent, no PDF writes, no policy — measurement only.  Hard rules
(enforced by ``tests/test_layout_packing_7g2.py``): pure read of settled
placements, no detector / parser / renderer / translator, no ``lay_out`` /
``adaptive`` / wrap / shrink / clip, no geometry writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pdf2zh.semantic.layout.page_flow import placements_from_plan

__all__ = [
    "CollectColumns",
    "column_from_placements",
    "page_columns",
    "column_packing_metrics",
    "page_packing_metrics_for_placements",
    "document_packing_report",
    "CollectColumns",
    "ColumnMetrics",
    "PagePackingMetrics",
]

_TOL = 1e-6

#: Minimum horizontal overlap (pt) for two boxes to be considered the "same"
#: column.  Two-column arxiv papers have a gutter (no overlap); stacked blocks
#: inside one column overlap substantially.
_X_OVERLAP_COLUMN = 4.0


@dataclass
class CollectColumns:
    """A vertical text column discovered from a page's settled placements.

    ``placements`` are the reading-order blocks that landed in this column
    (already geometrically clustered); ``left`` / ``right`` bound the column's
    x-extent as the union of its blocks' resolved x-extents.  This is a
    **working structure** (mutable while a page is being clustered), not a
    frozen record.
    """

    placements: list = field(default_factory=list)
    left: float = 0.0
    right: float = 0.0

    def to_dict(self) -> dict:
        return {
            "left": round(self.left, 2),
            "right": round(self.right, 2),
            "blocks": len(self.placements),
        }


def column_from_placements(placements) -> CollectColumns:
    """One column from a list of placements (its x-union + its blocks)."""
    p = list(placements or [])
    if not p:
        return CollectColumns([], 0.0, 0.0)
    left = min(float(x.left) for x in p)
    right = max(float(x.right) for x in p)
    return CollectColumns(placements=list(p), left=left, right=right)


def page_columns(
    placements,
    *,
    x_overlap: float = _X_OVERLAP_COLUMN,
) -> list[CollectColumns]:
    """Cluster a page's settled placements into vertical text columns.

    Pure read of resolved x-extents.  A block joins a column when its x-range
    overlaps an existing column's x-range by more than ``x_overlap`` pt (the
    "same vertical band" test); otherwise it starts a new column to the right.
    Two-column papers therefore yield 2 columns; a single-column page yields 1.
    """
    cols: list[CollectColumns] = []
    # sort so columns form left to right in stable reading order
    for blk in sorted(placements or [], key=lambda b: (b.left, b.bottom)):
        placed = False
        for col in cols:
            if (
                min(col.right, blk.right) - max(col.left, blk.left)
            ) > x_overlap:
                col.placements.append(blk)
                col.left = min(col.left, blk.left)
                col.right = max(col.right, blk.right)
                placed = True
                break
        if not placed:
            cols.append(CollectColumns([blk], blk.left, blk.right))
    cols.sort(key=lambda c: c.left)
    return cols


def _band_gaps(placements) -> dict:
    """Vertical-band statistics over one column's reading-order placements.

    ``placements`` are assumed sorted in reading order (already clustered into
    a column).  Returns ``content_height`` (top - bottom), ``internal_gap``
    (sum of positive vertical gaps between stacked blocks), ``top`` /
    ``bottom``, and ``trailing`` (bottom edge distance below the lowest block).
    """
    if not placements:
        return {
            "content_height": 0.0,
            "internal_gap": 0.0,
            "top": 0.0,
            "bottom": 0.0,
            "trailing": 0.0,
        }
    ordered = sorted(placements, key=lambda b: b.bottom)
    top = float(max(b.top for b in ordered))
    bottom = float(min(b.bottom for b in ordered))
    internal_gap = 0.0
    for a, b in zip(ordered, ordered[1:]):
        # stacked: the upper block's top above the lower block's bottom
        gap = b.bottom - a.top
        if gap > _TOL:
            internal_gap += gap
    return {
        "content_height": round(top - bottom, 2),
        "internal_gap": round(internal_gap, 2),
        "top": round(top, 2),
        "bottom": round(bottom, 2),
        "trailing": round(max(0.0, bottom), 2),
    }


@dataclass(frozen=True)
class ColumnMetrics:
    """One column's packing baseline (per-column, 7G-2 measurement)."""

    left: float
    right: float
    blocks: int
    content_height: float
    page_height: float
    internal_gap: float
    trailing_gap: float

    @property
    def fill_ratio(self) -> float:
        if self.page_height <= 0.0:
            return 0.0
        return round(min(1.0, max(0.0, self.content_height / self.page_height)), 3)

    @property
    def whitespace_ratio(self) -> float:
        return round(1.0 - self.fill_ratio, 3)

    def to_dict(self) -> dict:
        return {
            "left": round(self.left, 2),
            "right": round(self.right, 2),
            "blocks": int(self.blocks),
            "content_height": round(self.content_height, 2),
            "page_height": round(self.page_height, 2),
            "fill_ratio": self.fill_ratio,
            "whitespace_ratio": self.whitespace_ratio,
            "internal_gap_pt": round(self.internal_gap, 2),
            "trailing_gap_pt": round(self.trailing_gap, 2),
        }


def column_packing_metrics(
    column: CollectColumns,
    page_height: float,
) -> ColumnMetrics:
    """One column's packing baseline from its settled placements."""
    band = _band_gaps(column.placements)
    return ColumnMetrics(
        left=column.left,
        right=column.right,
        blocks=len(column.placements),
        content_height=band["content_height"],
        page_height=float(page_height or 0.0),
        internal_gap=band["internal_gap"],
        trailing_gap=band["trailing"],
    )


@dataclass(frozen=True)
class PagePackingMetrics:
    """Per-page packing baseline (all columns aggregated)."""

    page: int
    columns: list

    @property
    def column_count(self) -> int:
        return len(self.columns)

    @property
    def avg_fill_ratio(self) -> float:
        vals = [c.fill_ratio for c in self.columns]
        if not vals:
            return 0.0
        return round(sum(vals) / len(vals), 3)

    @property
    def avg_whitespace_ratio(self) -> float:
        return round(1.0 - self.avg_fill_ratio, 3)

    @property
    def total_internal_gap(self) -> float:
        return round(sum(c.internal_gap for c in self.columns), 2)

    @property
    def avg_trailing_gap(self) -> float:
        vals = [c.trailing_gap for c in self.columns]
        if not vals:
            return 0.0
        return round(sum(vals) / len(vals), 2)

    def to_dict(self) -> dict:
        return {
            "page": int(self.page),
            "columns": self.column_count,
            "avg_fill_ratio": self.avg_fill_ratio,
            "avg_whitespace_ratio": self.avg_whitespace_ratio,
            "internal_gap_pt": self.total_internal_gap,
            "trailing_gap_pt": self.avg_trailing_gap,
            "column_detail": [c.to_dict() for c in self.columns],
        }


def page_packing_metrics_for_placements(
    page_placements,
    *,
    page: int,
    page_height: float,
) -> PagePackingMetrics:
    """Per-page packing baseline from an already-clustered page's placements."""
    cols = page_columns(page_placements)
    return PagePackingMetrics(
        page=int(page),
        columns=[column_packing_metrics(c, page_height) for c in cols],
    )


def document_packing_report(
    plan,
    page_sizes=None,
) -> dict:
    """Document-level V2 packing baseline over a settled plan.

    Reads settled placements (7F-8a), clusters them into columns per page,
    and aggregates the per-column vertical-band fill / gaps into one JSON-safe
    report.  Pure measurement — never re-lays-out, never moves a block, never
    writes PDF geometry.
    """
    sizes = dict(page_sizes or {})
    pages: dict[int, list] = {}
    for blk in placements_from_plan(plan):
        pages.setdefault(blk.page, []).append(blk)

    per_page: list[dict] = []
    total_fill = []
    total_trailing = []
    total_internal = 0.0
    for pno in sorted(pages):
        ph = sizes.get(pno)
        if not ph or ph <= 0.0:
            ph = 792.0
        mp = page_packing_metrics_for_placements(
            pages[pno], page=int(pno), page_height=ph
        )
        per_page.append(mp.to_dict())
        total_fill.append(mp.avg_fill_ratio)
        total_trailing.append(mp.avg_trailing_gap)
        total_internal += mp.total_internal_gap

    ncols = sum(int(p["columns"]) for p in per_page)
    return {
        "pages": len(per_page),
        "columns": ncols,
        "avg_fill_ratio": round(sum(total_fill) / len(total_fill), 3) if total_fill else 0.0,
        "avg_whitespace_ratio": round(
            1.0 - (sum(total_fill) / len(total_fill)) if total_fill else 0.0, 3
        ),
        "avg_trailing_gap_pt": round(sum(total_trailing) / len(total_trailing), 2)
        if total_trailing else 0.0,
        "total_internal_gap_pt": round(total_internal, 2),
        "per_page": per_page,
    }