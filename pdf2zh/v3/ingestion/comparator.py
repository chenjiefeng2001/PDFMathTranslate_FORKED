"""Comparator — the ``INGESTION_DIFF`` between two ingestion backends.

Dual-track ingestion (existing pdfminer backend vs Marker) is most valuable
when the two canonical IRs are compared automatically: page count, block
counts, text coverage, paragraph split/merge, table/figure detection,
header/footer and reading structure.  The output mirrors the plan's
diagnosis shape::

    page 442
      Existing: 12 text blocks, 1 list, 0 table
      Marker:   11 text blocks, 1 list, 1 table
      Divergence:
        block p442_b8 / p442_b9 merged into one block
      Severity: MEDIUM

Severity vocabulary matches trace_rules: HIGH / MEDIUM / LOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pdf2zh.v3.ingestion.ir import (
    KIND_FIGURE,
    KIND_HEADER,
    KIND_FOOTER,
    KIND_TABLE,
    TEXT_KINDS,
    IngestDocument,
)

SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"

_SEVERITY_ORDER = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}

#: 结构化大块类别 —— 缺失/多出单独计 divergence（这些不参与逐字文本比对）。
_STRUCTURAL_KINDS = frozenset({KIND_TABLE, KIND_FIGURE, KIND_HEADER, KIND_FOOTER})

#: 文本承载类别（页面文本比对用，与 ir.TEXT_KINDS 一致）。
TEXT_CMP_KINDS = frozenset(TEXT_KINDS)


@dataclass
class DiffItem:
    """One divergence on one page."""

    page_no: int
    kind: str  # e.g. "merged", "split", "table_detection", "block_count"
    severity: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page_no,
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "detail": dict(self.detail),
        }


@dataclass
class PageDiff:
    page_no: int
    blocks_a: int = 0
    blocks_b: int = 0
    kinds_a: Dict[str, int] = field(default_factory=dict)
    kinds_b: Dict[str, int] = field(default_factory=dict)
    items: List[DiffItem] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _text_blocks(doc: IngestDocument, page_no: int) -> List[Tuple[str, str]]:
    """(block_id, normalized text) for text-bearing blocks in reading order."""
    out = []
    for b in doc.page_blocks(page_no):
        t = _norm(b.text)
        if b.block_type in TEXT_CMP_KINDS and t:
            out.append((b.block_id, t))
    return out


class IngestionDiff:
    """Aggregated result of comparing two ingestion IRs."""

    def __init__(self, backend_a: str, backend_b: str) -> None:
        self.backend_a = backend_a
        self.backend_b = backend_b
        self.pages: List[PageDiff] = []
        self._global_items: List[DiffItem] = []

    # ── collection ───────────────────────────────────────────────────

    def page(self, page_no: int) -> PageDiff:
        for p in self.pages:
            if p.page_no == page_no:
                return p
        p = PageDiff(page_no=page_no)
        self.pages.append(p)
        return p

    def add(self, item: DiffItem) -> None:
        if item.page_no is None or item.page_no < 0:
            self._global_items.append(item)
        else:
            self.page(item.page_no).items.append(item)

    @property
    def items(self) -> List[DiffItem]:
        return self._global_items + [i for p in self.pages for i in p.items]

    @property
    def max_severity(self) -> Optional[str]:
        worst = None
        for i in self.items:
            if worst is None or _SEVERITY_ORDER.get(
                i.severity, 9
            ) < _SEVERITY_ORDER.get(worst, 9):
                worst = i.severity
        return worst

    # ── output ───────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend_a": self.backend_a,
            "backend_b": self.backend_b,
            "summary": self.summary(),
            "pages": [
                {
                    "page": p.page_no,
                    "blocks_a": p.blocks_a,
                    "blocks_b": p.blocks_b,
                    "kinds_a": p.kinds_a,
                    "kinds_b": p.kinds_b,
                    "items": [i.to_dict() for i in p.items],
                }
                for p in self.pages
            ],
            "items": [i.to_dict() for i in self.items],
        }

    def to_json(self, indent: int = 1) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def summary(self) -> Dict[str, Any]:
        sev_counts: Dict[str, int] = {}
        for i in self.items:
            sev_counts[i.severity] = sev_counts.get(i.severity, 0) + 1
        return {
            "pages_compared": len(self.pages),
            "item_count": len(self.items),
            "by_severity": sev_counts,
            "max_severity": self.max_severity or "PASS",
        }

    def render_text(self) -> str:
        lines = ["INGESTION_DIFF"]
        lines.append(f"  backend A: {self.backend_a}")
        lines.append(f"  backend B: {self.backend_b}")
        for p in self.pages:
            lines.append(f"page {p.page_no}")
            lines.append(
                f"  A: {p.blocks_a} blocks | B: {p.blocks_b} blocks "
                f"(A {_compact_kinds(p.kinds_a)} | B {_compact_kinds(p.kinds_b)})"
            )
            if not p.items:
                lines.append("  match: structure aligned")
                continue
            lines.append("  Divergence:")
            for it in sorted(
                p.items, key=lambda i: (_SEVERITY_ORDER.get(i.severity, 9), i.kind)
            ):
                lines.append(f"    [{it.severity}] {it.kind}: {it.message}")
        s = self.summary()
        lines.append(f"Severity: {s['max_severity']} (items={s['item_count']})")
        return "\n".join(lines)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        s = self.summary()
        return f"IngestionDiff(pages={s['pages_compared']}, max={s['max_severity']})"


def _compact_kinds(kinds: Dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "empty"


def _severity_for_kind(kind: str) -> str:
    if kind in (KIND_TABLE, KIND_FIGURE):
        return SEVERITY_MEDIUM
    if kind in (KIND_HEADER, KIND_FOOTER):
        return SEVERITY_LOW
    return SEVERITY_LOW


def _match_runs(left: List[Tuple[str, str]], right_text: str) -> Optional[List[str]]:
    """Find a contiguous run of ``left`` blocks whose text joins to ``right_text``.

    Used both directions for split/merge detection (whitespace-joined).
    """
    for i in range(len(left)):
        acc = left[i][1]
        run = [left[i][0]]
        if acc == right_text:
            return run
        for j in range(i + 1, len(left)):
            acc = acc + " " + left[j][1]
            run.append(left[j][0])
            if acc == right_text:
                return run
            if len(acc) > len(right_text) + 64:
                break
    return None


def compare(a: IngestDocument, b: IngestDocument) -> IngestionDiff:
    """Compare two canonical ingestion IRs and produce an IngestionDiff.

    Pages are aligned by order (both are 0-based page lists).  Divergence
    items follow the plan vocabulary: page_count mismatch (HIGH), structural
    kind (table/figure/header/footer) count drift, and paragraph-level
    merges/splits detected by exact normalized-text runs.
    """
    diff = IngestionDiff(
        backend_a=a.source_backend or "backend_a",
        backend_b=b.source_backend or "backend_b",
    )
    a_pages, b_pages = a.pages(), b.pages()

    if len(a_pages) != len(b_pages):
        diff.add(
            DiffItem(
                page_no=-1,
                kind="page_count",
                severity=SEVERITY_HIGH,
                message=f"page count differs: {a.source_backend} has {len(a_pages)} pages, "
                f"{b.source_backend} has {len(b_pages)}",
                detail={"a": len(a_pages), "b": len(b_pages)},
            )
        )

    n = max(len(a_pages), len(b_pages))
    for pno in range(n):
        pa = a_pages[pno] if pno < len(a_pages) else None
        pb = b_pages[pno] if pno < len(b_pages) else None
        if pa is None or pb is None:
            continue
        pd = diff.page(pno)
        ba = a.page_blocks(pa.page_no)
        bb = b.page_blocks(pb.page_no)
        pd.blocks_a, pd.blocks_b = len(ba), len(bb)
        pd.kinds_a, pd.kinds_b = a.kinds_histogram(pa.page_no), b.kinds_histogram(
            pb.page_no
        )

        # 1) structural kind drift (table/figure/header/footer presence)
        for kind in sorted(_STRUCTURAL_KINDS):
            ca = pd.kinds_a.get(kind, 0)
            cb = pd.kinds_b.get(kind, 0)
            if ca != cb:
                diff.add(
                    DiffItem(
                        page_no=pno,
                        kind=f"{kind}_detection",
                        severity=_severity_for_kind(kind),
                        message=f"{kind} count {ca} -> {cb} "
                        f"(A {_names_for(ba, kind)}, B {_names_for(bb, kind)})",
                        detail={"a": ca, "b": cb},
                    )
                )

        # 2) total block count drift (non-structural kinds)
        if pd.blocks_a != pd.blocks_b:
            diff.add(
                DiffItem(
                    page_no=pno,
                    kind="block_count",
                    severity=SEVERITY_LOW,
                    message=f"block count differs: A {pd.blocks_a} vs B {pd.blocks_b}",
                    detail={"a": pd.blocks_a, "b": pd.blocks_b},
                )
            )

        # 3) paragraph-level merge/split via normalized text runs
        la, lb = _text_blocks(a, pa.page_no), _text_blocks(b, pb.page_no)
        matched_a: set = set()
        matched_b: set = set()
        # exact matches first
        counts_b: Dict[str, List[str]] = {}
        for bid, t in lb:
            counts_b.setdefault(t, []).append(bid)
        for aid, t in la:
            hits = counts_b.get(t)
            if hits:
                matched_a.add(aid)
                matched_b.add(hits.pop(0))
        # leftover runs
        la_left = [(i, t) for i, t in la if i not in matched_a]
        lb_left = [(i, t) for i, t in lb if i not in matched_b]
        # merged in B: one B block == several consecutive leftover A blocks
        still_a = list(la_left)
        for bid_b, tb in list(lb_left):
            run = _match_runs(still_a, tb)
            if run:
                for rid in run:
                    matched_a.add(rid)
                    still_a = [(i, t) for i, t in still_a if i != rid]
                matched_b.add(bid_b)
                diff.add(
                    DiffItem(
                        page_no=pno,
                        kind="merged",
                        severity=SEVERITY_MEDIUM,
                        message=f"{' / '.join(run)} merged into one block "
                        f"({a.source_backend} {len(run)} blocks -> {b.source_backend} 1)",
                        detail={"a_blocks": run, "b_block": bid_b},
                    )
                )
        # split in B: one A block == several consecutive leftover B blocks
        still_b = list(lb_left)
        for bid_a, ta in list(la_left):
            run = _match_runs(still_b, ta)
            if run:
                for rid in run:
                    matched_b.add(rid)
                    still_b = [(i, t) for i, t in still_b if i != rid]
                matched_a.add(bid_a)
                diff.add(
                    DiffItem(
                        page_no=pno,
                        kind="split",
                        severity=SEVERITY_MEDIUM,
                        message=f"{bid_a} split into {' / '.join(run)} "
                        f"({a.source_backend} 1 block -> {b.source_backend} {len(run)})",
                        detail={"a_block": bid_a, "b_blocks": run},
                    )
                )
        unmatched_a = len(la) - len(matched_a)
        unmatched_b = len(lb) - len(matched_b)
        if unmatched_a or unmatched_b:
            diff.add(
                DiffItem(
                    page_no=pno,
                    kind="text_unmatched",
                    severity=SEVERITY_LOW,
                    message=f"{unmatched_a} text blocks unmatched in A, "
                    f"{unmatched_b} in B",
                    detail={"a": unmatched_a, "b": unmatched_b},
                )
            )
    return diff


def _names_for(blocks: List[Any], kind: str) -> str:
    names = [b.block_id for b in blocks if b.block_type == kind][:8]
    return ", ".join(names) if names else "-"


__all__ = [
    "DiffItem",
    "PageDiff",
    "IngestionDiff",
    "compare",
    "SEVERITY_HIGH",
    "SEVERITY_MEDIUM",
    "SEVERITY_LOW",
]
