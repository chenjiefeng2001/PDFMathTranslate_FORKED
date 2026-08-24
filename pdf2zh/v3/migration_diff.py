"""Module: V8.1 Migration Diff Harness + 阶段零 IR 快照基线.

Implements the migration-closure deliverables from the roadmap v3.0:

  * MigrationDiffHarness — regression baseline between the legacy pipeline
    (``LTChar -> obj_patch``) and the V4 pipeline. A V4 migration is only
    *green* when it is not worse than legacy on every observable metric:
    page count, text similarity, bbox displacement, overlap rate.
  * IR snapshot baseline — the 阶段零 requirement that the Document IR be
    serializable to a stable JSON schema (``paragraphs`` / ``captions`` /
    ``tables`` / ...), so a corpus snapshot can be diffed across runs.
  * SyntheticCorpus — a deterministic 100-document corpus generator so the
    "100 份 PDF 测试集" acceptance criterion is testable headlessly.

Pure-Python; no PDFs required.

Usage::

    from pdf2zh.v3.migration_diff import MigrationDiffHarness, snapshot_ir
    report = MigrationDiffHarness().compute(legacy_blocks, v4_blocks)
    print(report.summary(), report.passed)
    json_text = snapshot_ir(ir, title="doc")
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class BlockRecord:
    """Normalized layout block shared by both engines."""

    node_id: str
    page: int
    text: str
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.height

    def to_dict(self) -> dict:
        return {
            "id": self.node_id,
            "page": self.page,
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "w": self.width,
            "h": self.height,
        }


def _item_page(item: Any, fallback: int) -> int:
    if isinstance(item, dict):
        page = item.get("page_num", item.get("page", fallback))
    else:
        page = getattr(item, "page_num", None)
        if page is None:
            page = getattr(item, "page", fallback)
    return int(page if page is not None else fallback)


def normalize_block(item: Any, fallback_page: int = 0) -> Optional[BlockRecord]:
    """Adapt a legacy or V4 block (dict / object / tuple bbox) to BlockRecord.

    Tuples are accepted as ``(text, bbox)``, ``(text, bbox, page)`` or
    ``(text, bbox, page, node_id)`` — the compact form used by SyntheticCorpus.
    """
    if isinstance(item, (tuple, list)):
        parts = list(item)
        text = str(parts[0]) if parts else ""
        bb = parts[1] if len(parts) > 1 else None
        page = int(parts[2]) if len(parts) > 2 else fallback_page
        node_id = str(parts[3]) if len(parts) > 3 else f"anon_{len(text)}"
    elif isinstance(item, dict):
        text = str(item.get("text", ""))
        node_id = item.get("id") or f"anon_{len(text)}"
        page = _item_page(item, fallback_page)
        bb = item.get("bbox")
        if bb is None:
            bb = (
                item.get("x0", 0.0),
                item.get("y0", 0.0),
                item.get("x1", 0.0),
                item.get("y1", 0.0),
            )
    else:
        text = str(getattr(item, "text", ""))
        node_id = getattr(item, "id", None) or f"anon_{len(text)}"
        page = _item_page(item, fallback_page)
        bb = getattr(item, "bbox", None)
    if isinstance(bb, (tuple, list)):
        if len(bb) == 4:
            x0, y0, x1, y1 = (float(v) for v in bb)
            x, y, w, h = x0, y0, x1 - x0, y1 - y0
        else:
            x, y, w, h = (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
    elif hasattr(bb, "x"):
        x, y, w, h = (bb.x, bb.y, bb.width, bb.height)
    else:
        x = y = w = h = 0.0
    return BlockRecord(str(node_id), page, text, x, y, w, h)


# ── Metrics ──────────────────────────────────────────────────────────


def dice_similarity(a: str, b: str) -> float:
    """Token-level Dice coefficient in [0, 1]."""
    if not a and not b:
        return 1.0
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta and not tb:
        return 1.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


def _overlap_area(a: BlockRecord, b: BlockRecord) -> float:
    ox = max(0.0, min(a.x1, b.x1) - max(a.x, b.x))
    oy = max(0.0, min(a.y1, b.y1) - max(a.y, b.y))
    return ox * oy


def overlap_rate(blocks: Sequence[BlockRecord]) -> float:
    """Fraction of blocks (per page) that overlap another block."""
    if not blocks:
        return 0.0
    by_page: Dict[int, List[BlockRecord]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(b)
    overlapping = 0
    total = 0
    for page_blocks in by_page.values():
        total += len(page_blocks)
        for i in range(len(page_blocks)):
            for j in range(i + 1, len(page_blocks)):
                if _overlap_area(page_blocks[i], page_blocks[j]) > 1e-6:
                    overlapping += 1
                    break
    return overlapping / total if total else 0.0


# ── Report ───────────────────────────────────────────────────────────


@dataclass
class MigrationDiffReport:
    """One-shot comparison of two pipeline outputs."""

    pages_legacy: int = 0
    pages_v4: int = 0
    text_similarity: float = 1.0
    node_match_ratio: float = 1.0
    bbox_displacement: float = 0.0
    overlap_rate_legacy: float = 0.0
    overlap_rate_v4: float = 0.0
    regressions: List[str] = field(default_factory=list)
    per_page: Dict[int, dict] = field(default_factory=dict)
    thresholds: Dict[str, float] = field(default_factory=dict)

    @property
    def page_diff(self) -> int:
        return abs(self.pages_v4 - self.pages_legacy)

    @property
    def passed(self) -> bool:
        return len(self.regressions) == 0

    def to_dict(self) -> dict:
        return {
            "pages_legacy": self.pages_legacy,
            "pages_v4": self.pages_v4,
            "page_diff": self.page_diff,
            "text_similarity": round(self.text_similarity, 4),
            "node_match_ratio": round(self.node_match_ratio, 4),
            "bbox_displacement": round(self.bbox_displacement, 2),
            "overlap_rate_legacy": round(self.overlap_rate_legacy, 4),
            "overlap_rate_v4": round(self.overlap_rate_v4, 4),
            "regressions": list(self.regressions),
            "per_page": self.per_page,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "REGRESSION"
        return (
            f"[{verdict}] pages {self.pages_legacy}→{self.pages_v4} "
            f"sim={self.text_similarity:.3f} match={self.node_match_ratio:.3f} "
            f"bboxΔ={self.bbox_displacement:.2f} "
            f"overlap {self.overlap_rate_legacy:.3f}→{self.overlap_rate_v4:.3f}"
        )


class MigrationDiffHarness:
    """V8.1 regression baseline: legacy output vs V4 output.

    Thresholds bound the *acceptable* degradation of the V4 pipeline relative
    to legacy before the migration is flagged as a regression.
    """

    DEFAULT_THRESHOLDS = {
        "page_diff": 1,  # V4 must not add/remove > 1 page
        "text_similarity_drop": 0.15,  # dice drop vs legacy
        "node_match_ratio": 0.90,  # matched node ids >= 90%
        "bbox_displacement": 40.0,  # mean drift in points
        "overlap_rate_delta": 0.10,  # overlap-rate increase <= 10 points
    }

    def __init__(self, thresholds: Optional[dict] = None) -> None:
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **(thresholds or {})}

    def compute(
        self,
        legacy_blocks: Sequence[Any],
        v4_blocks: Sequence[Any],
        pages_legacy: Optional[int] = None,
        pages_v4: Optional[int] = None,
    ) -> MigrationDiffReport:
        legacy = [
            b for b in (normalize_block(b, 0) for b in legacy_blocks) if b is not None
        ]
        v4 = [b for b in (normalize_block(b, 0) for b in v4_blocks) if b is not None]
        n_legacy = max({b.page for b in legacy}, default=-1) + 1
        n_v4 = max({b.page for b in v4}, default=-1) + 1
        pages_legacy = pages_legacy if pages_legacy is not None else n_legacy
        pages_v4 = pages_v4 if pages_v4 is not None else n_v4

        legacy_map = {b.node_id: b for b in legacy}
        v4_map = {b.node_id: b for b in v4}
        matched = len(set(legacy_map) & set(v4_map))
        node_match = matched / max(1, len(legacy_map))

        # text similarity over matched ids (fallback: whole-document dice)
        sims = []
        for nid, lb in legacy_map.items():
            vb = v4_map.get(nid)
            if vb is not None:
                sims.append(dice_similarity(lb.text, vb.text))
        if not sims:
            sims = [
                dice_similarity(
                    " ".join(b.text for b in legacy), " ".join(b.text for b in v4)
                )
            ]
        text_sim = sum(sims) / len(sims)

        # bbox displacement on matched ids
        displacements = [
            math.hypot(v4_map[nid].x - lb.x, v4_map[nid].y - lb.y)
            for nid, lb in legacy_map.items()
            if nid in v4_map
        ]
        bbox_disp = sum(displacements) / len(displacements) if displacements else 0.0

        ol_legacy = overlap_rate(legacy)
        ol_v4 = overlap_rate(v4)

        regressions: List[str] = []
        t = self.thresholds
        if abs(pages_v4 - pages_legacy) > t["page_diff"]:
            regressions.append(f"page count drift {pages_legacy}→{pages_v4}")
        if node_match < t["node_match_ratio"]:
            regressions.append(
                f"node match {node_match:.3f} < " f"{t['node_match_ratio']:.3f}"
            )
        if text_sim < 1.0 - t["text_similarity_drop"]:
            regressions.append(
                f"text similarity {text_sim:.3f} below "
                f"{1.0 - t['text_similarity_drop']:.3f}"
            )
        if bbox_disp > t["bbox_displacement"]:
            regressions.append(
                f"bbox displacement {bbox_disp:.1f} > " f"{t['bbox_displacement']:.1f}"
            )
        if ol_v4 - ol_legacy > t["overlap_rate_delta"]:
            regressions.append(f"overlap rate rose {ol_legacy:.3f}→{ol_v4:.3f}")

        return MigrationDiffReport(
            pages_legacy=pages_legacy,
            pages_v4=pages_v4,
            text_similarity=text_sim,
            node_match_ratio=node_match,
            bbox_displacement=bbox_disp,
            overlap_rate_legacy=ol_legacy,
            overlap_rate_v4=ol_v4,
            regressions=regressions,
            thresholds=dict(t),
        )


# ── 阶段零 IR snapshot baseline ─────────────────────────────────────


_IR_BUCKET_KEYS = (
    "paragraphs",
    "captions",
    "tables",
    "headings",
    "formulas",
    "references",
    "others",
)


def snapshot_ir(ir: Any, title: str = "", include_geometry: bool = True) -> dict:
    """Stable JSON-able snapshot of a DocumentIR grouped by semantic role.

    Produces the ``paragraphs`` / ``captions`` / ``tables`` ... buckets from
    the roadmap 阶段零 so corpus snapshots can be diffed across runs. Every
    bucket key is always present (empty lists included) so two runs of the
    same corpus diff cleanly even when a role disappears.
    """
    nodes = getattr(ir, "nodes", None)
    nodes = list(nodes()) if callable(nodes) else list(nodes or [])
    buckets: Dict[str, List[dict]] = {k: [] for k in _IR_BUCKET_KEYS}
    for n in nodes:
        role = getattr(n, "semantic", None)
        if role is None:
            continue
        name = getattr(role, "value", None) or str(role)
        if name in ("document", "section"):
            continue
        entry = {
            "id": getattr(n, "id", ""),
            "text": getattr(n, "text", ""),
            "role": name,
            "reading": getattr(getattr(n, "reading", None), "value", ""),
            "page": getattr(n, "page_num", 0),
        }
        bb = getattr(n, "bbox", None)
        if include_geometry and bb is not None:
            if hasattr(bb, "x"):
                entry["bbox"] = (
                    round(bb.x, 1),
                    round(bb.y, 1),
                    round(bb.width, 1),
                    round(bb.height, 1),
                )
            else:
                entry["bbox"] = tuple(round(float(v), 1) for v in bb)
        if name in ("body_text", "paragraph"):
            key = "paragraphs"
        elif name in ("caption",):
            key = "captions"
        elif name in ("table",):
            key = "tables"
        elif name in ("heading",):
            key = "headings"
        elif name in ("formula", "formula_inline"):
            key = "formulas"
        elif name in ("reference", "citation", "bibliography"):
            key = "references"
        else:
            key = "others"
        buckets.setdefault(key, []).append(entry)

    return {
        "schema": "pdf2zh.v3.ir-snapshot",
        "version": 1,
        "title": title,
        "node_count": len(nodes),
        **{
            k: sorted(v, key=lambda e: (e["page"], e["id"]))
            for k, v in sorted(buckets.items())
        },
    }


class SyntheticCorpus:
    """Deterministic corpus of synthetic documents for headless IR baselines.

    Covers the three layout families from 阶段零: double-column papers,
    textbooks (formula-heavy) and figure-heavy documents. The generated IR
    snapshots are stable for a given seed, which makes golden-file diffing
    across engine versions possible without shipping real PDFs.
    """

    TEMPLATES = ("paper_two_column", "textbook", "figure_heavy")

    def __init__(self, count: int = 100, seed: int = 42) -> None:
        self.count = count
        self.rng = random.Random(seed)

    def make_document_graph(self, index: int, title: str = ""):
        """Build one DocumentGraph-like object (see pdf2zh.v3.graph)."""
        from pdf2zh.v3.graph import (
            DocumentGraph,
            DocumentNode,
            Edge,
            EdgeType,
            NodeType,
        )

        template = self.TEMPLATES[index % len(self.TEMPLATES)]
        g = DocumentGraph()
        g.add_node(
            DocumentNode(
                id="page_0", node_type=NodeType.PAGE, bbox=(0, 0, 612, 792), page_num=0
            )
        )
        if template == "paper_two_column":
            rows = [
                ("title", "A Two-Column Study", NodeType.HEADING, 72, 60),
                ("abs", "We study a new method.", NodeType.PARAGRAPH, 72, 90),
                ("c1", "Left column paragraph one.", NodeType.PARAGRAPH, 72, 130),
                ("c2", "Right column paragraph one.", NodeType.PARAGRAPH, 340, 130),
                ("fig", "Figure 1: Overview.", NodeType.CAPTION, 72, 300),
                ("ref", "References go here.", NodeType.REFERENCE, 72, 500),
            ]
        elif template == "textbook":
            rows = [
                ("t", "Chapter 1", NodeType.HEADING, 72, 60),
                ("p1", "Definition and theorem.", NodeType.PARAGRAPH, 72, 100),
                ("f", "E = mc^2", NodeType.FORMULA, 72, 170),
                ("c", "Caption of the example.", NodeType.CAPTION, 72, 220),
                ("p2", "Proof sketch.", NodeType.PARAGRAPH, 72, 260),
                ("l", "List item A, list item B.", NodeType.LIST, 72, 320),
            ]
        else:
            rows = [
                ("t", "Figure Heavy Document", NodeType.HEADING, 72, 60),
                ("fig1", "Figure 1", NodeType.FIGURE, 72, 100),
                ("cap1", "Fig 1 caption.", NodeType.CAPTION, 72, 260),
                ("tab", "Table 1", NodeType.TABLE, 72, 320),
                ("cap2", "Tab 1 caption.", NodeType.CAPTION, 72, 420),
                ("p", "Body text after the figure.", NodeType.PARAGRAPH, 72, 470),
            ]
        for i, (nid, text, ntype, x, y) in enumerate(rows):
            g.add_node(
                DocumentNode(
                    id=nid, node_type=ntype, text=text, bbox=(x, y, 200, 20), page_num=0
                )
            )
            g.add_edge(Edge("page_0", nid, EdgeType.CONTAINS))
            if i > 0:
                g.add_edge(Edge(rows[i - 1][0], nid, EdgeType.FOLLOWS))
        return g

    def snapshot(self, index: int, title: str = "") -> dict:
        """IR snapshot for corpus document ``index`` (deterministic)."""
        from pdf2zh.v3.document_ir import IRBuilder

        g = self.make_document_graph(index, title)
        title = title or f"synthetic_{index:03d}"
        ir = IRBuilder(title=title, source_lang="en", target_lang="zh-cn").build(g)
        return snapshot_ir(ir, title=ir.title)

    def run(self, title_prefix: str = "corpus") -> List[dict]:
        """Build the full corpus snapshot set (P4 acceptance artifact)."""
        return [self.snapshot(i, f"{title_prefix}_{i:03d}") for i in range(self.count)]


__all__ = [
    "BlockRecord",
    "normalize_block",
    "dice_similarity",
    "overlap_rate",
    "MigrationDiffReport",
    "MigrationDiffHarness",
    "snapshot_ir",
    "SyntheticCorpus",
]
