"""Ingestion invariant rules — ``ingest.*`` flight-recorder events -> FAIL verdicts.

The rules engine (``pdf2zh/v3/trace_rules``) already answers "which layer
first produced the error" across plan → render → raster.  These rules do the
same for the *ingest* layer, so a Marker backend silently emitting
coordinate-less blocks — or a doc ingested without page sizes, whose
coordinates were never normalized into v3 — shows up as
``first_divergence = ingest`` instead of being blamed on the renderer later.

Rules produce :class:`~pdf2zh.v3.trace_rules.RuleResult` (stage ``"ingest"``)
so they can be merged with the plan/raster results and run through
``annotate_first_divergence`` unchanged.  They are intentionally *not* wired
into ``trace_rules.ALL_RULES`` (that set runs over every block of every
translation trace); wire them once ingest events are emitted on the real
translation path (see ``run_ingest_rules``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from pdf2zh.v3.ingestion.base import (
    EVENT_INGEST_BLOCK,
    STAGE_INGEST,
)
from pdf2zh.v3.trace_rules import (
    SEVERITY_DEFECT,
    SEVERITY_SUSPICIOUS,
    RuleResult,
    _fail,
)

#: rule names (the invariant each one guards).
RULE_INGEST_GEOMETRY_DECLARED = "INGEST_GEOMETRY_DECLARED"
RULE_MARKER_GEOMETRY_NORMALIZED = "MARKER_GEOMETRY_NORMALIZED"

#: backends whose coordinates must be normalized into v3 before use.
_SPACE_SENSITIVE_BACKENDS = {"marker"}


def _ingest_records(events: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``trace_id -> facts`` assembled from ``ingest.block`` events."""
    out: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        if ev.get("event") != EVENT_INGEST_BLOCK:
            continue
        pno = int(ev.get("page") or -1)
        bid = ev.get("block_id") or "?"
        tid = ev.get("trace_id") or f"{pno}/{bid}"
        p = ev.get("payload") or {}
        out[tid] = {
            "page": pno,
            "block_id": bid,
            "trace_id": tid,
            "backend": p.get("backend", ""),
            "kind": p.get("kind", ""),
            "text": p.get("text", ""),
            "box": p.get("box"),
            "v3_box": p.get("v3_box"),
        }
    return out


def rule_ingest_geometry_declared(rec: Dict[str, Any]) -> Optional[RuleResult]:
    """Text-bearing blocks must carry a *declared* box (space/origin/unit/
    meaning).

    The MECH-4 lesson at the ingestion boundary: a bare tuple of numbers is
    not geometry.  If a backend ever emits text without declared coordinates
    this fires before the block can poison plan/render geometry.  Generic
    across every backend — the invariant is about the *declaration*, never
    about a specific parser's quality heuristics.
    """
    text = (rec.get("text") or "").strip()
    if not text:
        return None
    box = rec.get("box")
    if not box or not isinstance(box, dict):
        return _fail(
            RULE_INGEST_GEOMETRY_DECLARED,
            SEVERITY_DEFECT,
            rec,
            {"box": box, "reason": "text block has no declared box"},
            action="adapter-fix",
            stage=STAGE_INGEST,
        )
    missing = [k for k in ("space", "origin", "unit", "meaning") if not box.get(k)]
    if missing:
        return _fail(
            RULE_INGEST_GEOMETRY_DECLARED,
            SEVERITY_DEFECT,
            rec,
            {
                "box": box,
                "missing": missing,
                "reason": "box lacks declared " + "/".join(missing),
            },
            action="adapter-fix",
            stage=STAGE_INGEST,
        )
    return None


def rule_marker_geometry_normalized(rec: Dict[str, Any]) -> Optional[RuleResult]:
    """Marker blocks must have been normalized into the v3 canonical frame.

    Marker JSON coordinates live in page-image pixels (top-left, y down);
    they are only comparable to v3 (PDF points, lower-left, y up) after the
    explicit projection in ``adapter.normalize_marker_box``.  A Marker
    ingest without PDF page sizes leaves ``v3_box = None`` — flagged here so
    nobody silently treats marker_image numbers as v3 numbers downstream.
    """
    if rec.get("backend") not in _SPACE_SENSITIVE_BACKENDS:
        return None
    text = (rec.get("text") or "").strip()
    if not text:
        return None
    if rec.get("v3_box") is None:
        return _fail(
            RULE_MARKER_GEOMETRY_NORMALIZED,
            SEVERITY_SUSPICIOUS,
            rec,
            {
                "box_space": (rec.get("box") or {}).get("space"),
                "reason": "marker block never normalized into v3 space",
            },
            action="ingest-with-pdf-page-sizes",
            stage=STAGE_INGEST,
        )
    return None


#: ingest rules in stable execution order.
INGESTION_RULES = [
    rule_ingest_geometry_declared,
    rule_marker_geometry_normalized,
]


def run_ingest_rules(events: Sequence[Dict[str, Any]]) -> List[RuleResult]:
    """Run ingest rules over ``ingest.*`` events (FAIL-only results).

    Merge the result list with plan/render/raster rule results and call
    ``trace_rules.annotate_first_divergence`` once to get a truthful
    ``first_divergence`` (an ingest FAIL correctly outranks plan FAILs).
    """
    results: List[RuleResult] = []
    for tid, rec in sorted(_ingest_records(events).items()):
        for rule in INGESTION_RULES:
            try:
                res = rule(rec)
            except Exception:  # noqa: BLE001 -- a broken rule never kills the audit
                res = None
            if res is not None:
                results.append(res)
    return results


__all__ = [
    "RULE_INGEST_GEOMETRY_DECLARED",
    "RULE_MARKER_GEOMETRY_NORMALIZED",
    "INGESTION_RULES",
    "run_ingest_rules",
]
