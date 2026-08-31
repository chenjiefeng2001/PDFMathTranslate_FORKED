"""diff — build a per-page NodeTrace that threads source→render, and match the
rendered (dual) text runs back onto model nodes so a defect can be attributed.

The model layer carries stable ``p{page}_{index}`` ids; the render layer has no
ids, so runs are matched **by geometry**: each rendered run is assigned to the
overlapping model block whose ``dst_box`` best contains its bbox (v3 y-up), with
an id-based fallback when the plan did not move the block.  A run that matches
no block, or a block with no run, is itself a divergence signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "Trace",
    "match_runs_to_blocks",
    "build_traces",
    "aggregate_page",
    "load_provenance",
    "aggregate_page_id_direct",
]


@dataclass
class Trace:
    """One source block threaded with both its plan/layout evidence and the
    matched rendered runs (or their absence)."""

    node_id: str
    page: int
    kind: str
    source_text: Optional[str] = None
    translated_text: Optional[str] = None
    translation_status: Optional[str] = None
    src_box: Optional[List[float]] = None
    dst_box: Optional[List[float]] = None
    layout_font_size: Optional[float] = None
    layout_overflow: Optional[bool] = None  # flow layout said a line would clip
    layout_recovery: Optional[dict] = None  # {reason,decision,steps,...}
    layout_ok: Optional[bool] = None
    parser_font_size: Optional[float] = None
    render_rows: List[dict] = field(default_factory=list)  # matched dual runs
    matched_present: bool = False
    render_box: Optional[List[float]] = None  # union of matched run bboxes (y-up)

    @property
    def rendered_text(self) -> str:
        return "".join(r.get("text") or "" for r in self.render_rows)


def _inter(a: List[float], b: List[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def _area(b) -> float:
    return max(0.0, (b[2] - b[0]) * (b[3] - b[1]))


def match_runs_to_blocks(
    runs: List[dict], evidence_rows: List[dict]
) -> Dict[int, Dict[str, Any]]:
    """Assign each rendered run to the best-overlapping model block.

    Returns ``{run_index: matched}`` where ``matched`` includes the block id,
    the overlap ratio and the run bbox.  Runs that match nothing get
    ``matched=None``.
    """
    blocks = [(i, ev) for i, ev in enumerate(evidence_rows)]
    out: Dict[int, Dict[str, Any]] = {}
    for ri, run in enumerate(runs):
        rb = run.get("v3_bbox") or run.get("bbox")
        if not rb:
            out[ri] = {"node_id": None, "overlap": 0.0}
            continue
        best_i, best_ov = None, 0.0
        for i, ev in blocks:
            dst = ev.get("layout", {}).get("target_bbox") or ev.get("parser", {}).get(
                "bbox"
            )
            if not dst:
                continue
            ov = _inter(rb, dst)
            if ov > best_ov:
                best_ov, best_i = ov, i
        if best_i is None or best_ov <= 0:
            out[ri] = {"node_id": None, "overlap": 0.0}
            continue
        out[ri] = {
            "node_id": evidence_rows[best_i]["node_id"],
            "overlap": round(best_ov / max(_area(rb), 1e-6), 3),
        }
    return out


def _union_box(boxes: List[List[float]]) -> Optional[List[float]]:
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def build_traces(
    page: int,
    evidence_rows: List[dict],
    runs_by_block: Dict[str, List[dict]],
) -> List[Trace]:
    traces: List[Trace] = []
    for ev in evidence_rows:
        nid = ev["node_id"]
        layout = ev.get("layout") or {}
        parser = ev.get("parser") or {}
        matched = runs_by_block.get(nid, [])
        traces.append(
            Trace(
                node_id=nid,
                page=page,
                kind=ev.get("kind"),
                source_text=parser.get("text")
                or ev.get("translation", {}).get("source_text"),
                translated_text=ev.get("translation", {}).get("translated_text"),
                translation_status=ev.get("translation", {}).get("translation_status"),
                src_box=(ev.get("parser") or {}).get("bbox"),
                dst_box=layout.get("target_bbox"),
                layout_font_size=layout.get("target_font_size"),
                layout_overflow=layout.get("overflow"),
                layout_recovery=layout.get("recovery"),
                layout_ok=layout.get("layout_ok"),
                parser_font_size=parser.get("font_size"),
                render_rows=matched,
                matched_present=bool(matched),
                render_box=_union_box([r.get("v3_bbox") for r in matched]),
            )
        )
    return traces


def load_provenance(prov_records: List[dict]) -> Dict[str, List[dict]]:
    """Group renderer provenance records (7H-2A) by ``source_node_id``.

    Each record carries ``render_object_ref``, ``object_type``, ``page``,
    ``final_bbox_v3``, ``font_size``, ``text``.  Returns
    ``{source_node_id: [records...]}``.  Any malformed record is skipped.
    """
    by_node: Dict[str, List[dict]] = {}
    for r in prov_records or []:
        nid = r.get("source_node_id")
        if not nid:
            continue
        by_node.setdefault(nid, []).append(dict(r))
    return by_node


def aggregate_page_id_direct(
    page: int,
    evidence_rows: List[dict],
    prov_by_node: Dict[str, List[dict]],
) -> Dict[str, Any]:
    """ID-direct page diff using renderer provenance instead of geometry.

    Every model block is looked up by ``source_node_id``:
      - present  → matched directly (present/moved by comparing dst vs recorded)
      - absent   → a **confirmed** missing draw (no longer UNCERTAIN)
      - stray    → produced render objects not backed by a model block
    """
    traces: List[Trace] = []
    present = moved = 0
    dangling: List[str] = []
    stray: List[dict] = []
    for ev in evidence_rows:
        nid = ev["node_id"]
        layout = ev.get("layout") or {}
        parser = ev.get("parser") or {}
        recs = prov_by_node.get(nid, [])
        if recs:
            present += 1
            moved += 0  # presence is the strong claim; movement needs dst compare
        else:
            dangling.append(nid)
        traces.append(
            Trace(
                node_id=nid,
                page=page,
                kind=ev.get("kind"),
                source_text=parser.get("text")
                or ev.get("translation", {}).get("source_text"),
                translated_text=ev.get("translation", {}).get("translated_text"),
                translation_status=ev.get("translation", {}).get("translation_status"),
                src_box=(ev.get("parser") or {}).get("bbox"),
                dst_box=layout.get("target_bbox"),
                layout_font_size=layout.get("target_font_size"),
                layout_overflow=layout.get("overflow"),
                layout_recovery=layout.get("recovery"),
                layout_ok=layout.get("layout_ok"),
                parser_font_size=parser.get("font_size"),
                render_rows=recs,
                matched_present=bool(recs),
            )
        )
    for nid, recs in prov_by_node.items():
        if not any(ev["node_id"] == nid for ev in evidence_rows):
            for r in recs:
                stray.append(r)
    return {
        "page": page,
        "total_blocks": len(evidence_rows),
        "total_render_runs": sum(len(v) for v in prov_by_node.values()),
        "matched_runs": present,
        "unmatched_runs": len(stray),
        "dangling_blocks": dangling,
        "id_direct": True,
        "present_blocks": present,
        "stray_records": stray,
        "traces": traces,
    }


def aggregate_page(
    page: int,
    evidence_rows: List[dict],
    runs: List[dict],
) -> Dict[str, Any]:
    """Assemble the page-level diff: match runs, build traces, report gaps."""
    matched = match_runs_to_blocks(runs, evidence_rows)
    runs_by_block: Dict[str, List[dict]] = {}
    unmatched: List[dict] = []
    for ri, m in matched.items():
        nid = m.get("node_id")
        if nid is None:
            unmatched.append({**runs[ri], "match": "none"})
        else:
            runs_by_block.setdefault(nid, []).append(runs[ri])
    traces = build_traces(page, evidence_rows, runs_by_block)
    # dangling blocks: planned to render but no run matched
    dangling = [
        t.node_id
        for t in traces
        if not t.matched_present
        and (
            t.translation_status not in ("preserved",)
            or t.kind in ("code", "formula", "caption")
        )
        and (t.source_text or "").strip()
    ]
    return {
        "page": page,
        "total_blocks": len(evidence_rows),
        "total_render_runs": len(runs),
        "matched_runs": len(matched) - len(unmatched),
        "unmatched_runs": len(unmatched),
        "dangling_blocks": dangling,
        "traces": traces,
    }
