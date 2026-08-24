"""Module: V8.4 Mainline Relayout Gate — legacy write-back safety.

Before a V4-relayouted result is written back into the *legacy* PDF object
stream, the MainlineRelayoutGate re-verifies the plan through the same
Constraint Layout + Collision Detection pipeline the V4 runtime uses. When
the translated text grew, blocks are resized with adaptive typography and the
layout is re-solved until the overlap rate falls below the threshold (with a
bounded number of passes). Only then is the write-back allowed.

This mirrors the roadmap's migration-closure rule: *the legacy path stays the
fallback*, but it may never silently corrupt the PDF layout.

Usage::

    from pdf2zh.v3.mainline_gate import MainlineRelayoutGate
    gate = MainlineRelayoutGate()
    result = gate.run(blocks, translations)
    if result.writeback_allowed:
        legacy_apply(result.blocks)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.constraint_graph import (
    ConstraintGraph,
    ConstraintRelation,
    ConstraintSolver,
)
from pdf2zh.v3.typography import AdaptiveTypography
from pdf2zh.v3.visual_tree import BoundingBox

logger = logging.getLogger(__name__)


@dataclass
class GateBlock:
    """One layout block entering / leaving the gate."""

    node_id: str
    text: str
    x: float = 0.0
    y: float = 0.0
    width: float = 400.0
    height: float = 20.0
    page: int = 0
    font_size: float = 12.0
    node_type: str = "paragraph"
    translated: str = ""

    @property
    def bbox(self) -> BoundingBox:
        return BoundingBox(self.x, self.y, self.width, self.height)

    @property
    def translated_text(self) -> str:
        return self.translated or self.text

    def to_dict(self) -> dict:
        """Faithful round-trip (keys match the GateBlock fields)."""
        return {
            "node_id": self.node_id,
            "page": self.page,
            "text": self.text,
            "translated": self.translated,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "font_size": self.font_size,
            "node_type": self.node_type,
        }


@dataclass
class GatedResult:
    """Gate verdict + the (possibly re-solved) blocks."""

    blocks: List[GateBlock] = field(default_factory=list)
    overlap_rate: float = 0.0
    passes: int = 0
    relayout_needed: bool = False
    writeback_allowed: bool = True
    issues: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "overlap_rate": round(self.overlap_rate, 4),
            "passes": self.passes,
            "relayout_needed": self.relayout_needed,
            "writeback_allowed": self.writeback_allowed,
            "issues": self.issues,
            "blocks": [b.to_dict() for b in self.blocks],
        }


def _blocks_overlap_rate(blocks: Sequence[GateBlock]) -> float:
    if not blocks:
        return 0.0
    by_page: Dict[int, List[GateBlock]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(b)
    overlapping = 0
    total = 0
    for page_blocks in by_page.values():
        total += len(page_blocks)
        for i in range(len(page_blocks)):
            for j in range(i + 1, len(page_blocks)):
                a, b = page_blocks[i], page_blocks[j]
                ox = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
                oy = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
                if ox > 1e-6 and oy > 1e-6:
                    overlapping += 1
                    break
    return overlapping / total if total else 0.0


class MainlineRelayoutGate:
    """Re-solve the translated layout before legacy write-back.

    Parameters:
        page_width / page_height: page geometry.
        margin: bottom safe margin (no block may overflow past it).
        threshold: maximum allowed overlap rate (fraction of overlapping
            blocks) before a new relayout pass is triggered.
        max_passes: bounded relayout iterations.
        typography: injectable adaptive typography (tests / custom sizing).
    """

    def __init__(
        self,
        page_width: float = 612.0,
        page_height: float = 792.0,
        margin: float = 50.0,
        threshold: float = 0.0,
        max_passes: int = 3,
        typography: Optional[AdaptiveTypography] = None,
    ) -> None:
        self.page_width = page_width
        self.page_height = page_height
        self.margin = margin
        self.threshold = threshold
        self.max_passes = max_passes
        self.typography = typography or AdaptiveTypography(
            container_width=page_width - 2 * 50.0, font_size=12.0
        )

    # ── Helpers ───────────────────────────────────────────────────────

    def _apply_translations(
        self, blocks: List[GateBlock], translations: Dict[str, str]
    ) -> List[GateBlock]:
        for b in blocks:
            b.translated = translations.get(b.node_id, b.translated)
        return blocks

    def _resize_by_translation(self, blocks: List[GateBlock]) -> List[GateBlock]:
        """Grow block heights to the adaptive typeset height of the text."""
        for b in blocks:
            m = self.typography.metrics(
                b.translated_text,
                source=b.text,
                font_size=b.font_size,
                container_width=b.width,
            )
            b.height = max(b.height, m.block_height)
            if m.expansion_ratio > 1.0:
                b.width = min(
                    self.page_width - 2 * 50.0, max(b.width, m.estimated_width + 4.0)
                )
        return blocks

    # ── Core ──────────────────────────────────────────────────────────

    def run(
        self,
        blocks: Sequence[GateBlock],
        translations: Optional[Dict[str, str]] = None,
        order: Optional[List[Tuple[str, str]]] = None,
    ) -> GatedResult:
        work = [GateBlock(**b.to_dict()) for b in blocks]
        if translations:
            work = self._apply_translations(work, translations)
        work = self._resize_by_translation(work)

        passes = 0
        relayout_needed = False
        rate = _blocks_overlap_rate(work)
        while rate > self.threshold and passes < self.max_passes:
            passes += 1
            relayout_needed = True
            work = self._relayout(work, order)
            rate = _blocks_overlap_rate(work)
            if rate <= self.threshold:
                break

        issues: List[str] = []
        if rate > self.threshold:
            issues.append(
                f"overlap rate {rate:.3f} above threshold "
                f"{self.threshold:.3f} after {passes} passes"
            )
        overflow = [
            b.node_id for b in work if b.y + b.height > self.page_height - self.margin
        ]
        if overflow:
            issues.append(f"blocks overflow the page: {overflow}")
        return GatedResult(
            blocks=work,
            overlap_rate=rate,
            passes=passes,
            relayout_needed=relayout_needed,
            writeback_allowed=rate <= self.threshold and not overflow,
            issues=issues,
        )

    def _relayout(
        self, blocks: List[GateBlock], order: Optional[List[Tuple[str, str]]]
    ) -> List[GateBlock]:
        """Re-solve vertical positions with the Kiwi constraint engine."""
        for page in sorted({b.page for b in blocks}):
            page_blocks = [b for b in blocks if b.page == page]
            cg = ConstraintGraph()
            for b in page_blocks:
                cg.add_node(
                    b.node_id,
                    b.node_type,
                    bbox=BoundingBox(b.x, b.y, b.width, b.height),
                    page_num=b.page,
                )
            used: set = set()
            for src, tgt in order or []:
                if src in cg._nodes and tgt in cg._nodes and (src, tgt) not in used:
                    used.add((src, tgt))
                    cg.add_edge(
                        src,
                        tgt,
                        ConstraintRelation.MUST_BELOW,
                        priority="hard",
                        gap=4.0,
                    )
            order_by_y = sorted(page_blocks, key=lambda b: b.y)
            for prev, nxt in zip(order_by_y, order_by_y[1:]):
                if (prev.node_id, nxt.node_id) not in used:
                    cg.add_edge(
                        prev.node_id,
                        nxt.node_id,
                        ConstraintRelation.MUST_BELOW,
                        priority="soft",
                        gap=4.0,
                    )
            solver = ConstraintSolver(cg, self.page_width, self.page_height)
            solver.solve(engine="auto")
            for b in page_blocks:
                n = cg.get_node(b.node_id)
                if n is not None and n.resolved_bbox is not None:
                    b.y = n.resolved_bbox.y
        return blocks


__all__ = ["GateBlock", "GatedResult", "MainlineRelayoutGate"]
