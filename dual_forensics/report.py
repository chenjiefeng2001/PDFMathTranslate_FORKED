"""report — write the forensic report tree.

    forensic-report/
    ├── manifest.json
    ├── page-077/
    │   ├── source.json     (parser evidence of the source page)
    │   ├── model.json      (document-model evidence)
    │   ├── translation.json
    │   ├── layout.json
    │   ├── render.json     (dual rendered-PDF evidence + MuPDF anomaly)
    │   └── diff.json       (NodeTrace + defects + first divergence)
    └── summary.json

Pure file writer: takes in-memory per-page dicts and a global summary.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

__all__ = [
    "json_safe",
    "write_report_tree",
    "build_summary",
]


def json_safe(obj: Any) -> Any:
    """Best-effort JSON sanitize (drop None-bearing geometry, tuple→list)."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items() if v is not None or True}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def _dump(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(json_safe(payload), fh, ensure_ascii=False, indent=1)


def build_summary(
    docs: List[Dict[str, Any]], findings: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Global summary.json: per-defect counts + first-divergence-stage histogram."""
    by_id: Dict[str, Dict[str, int]] = {}
    stage_totals: Dict[str, int] = {}
    for f in findings:
        if hasattr(f, "defect_id"):
            fid = f.defect_id
            fd = f.first_divergence
        else:
            fid = f.get("defect_id", "?")
            fd = f.get("first_divergence")
        entry = by_id.setdefault(fid, {"total": 0, "by_first_divergence": {}})
        entry["total"] += 1
        fd = fd or "unknown"
        entry["by_first_divergence"][fd] = entry["by_first_divergence"].get(fd, 0) + 1
        stage_totals[fd] = stage_totals.get(fd, 0) + 1
    return {
        "documents": [
            {"path": d.get("path"), "pages": d.get("pages_analysed")} for d in docs
        ],
        "defects": by_id,
        "first_divergence_stage_histogram": {
            k: stage_totals.get(k, 0)
            for k in [
                "source",
                "parser",
                "model",
                "translation",
                "layout",
                "render",
                "pdf",
            ]
        },
    }


def write_report_tree(
    out_dir: str,
    pages: Dict[int, Dict[str, Any]],  # pno → all per-stage payloads
    summary: Dict[str, Any],
    manifest_meta: Dict[str, Any],
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for pno, payload in sorted(pages.items()):
        pdir = os.path.join(out_dir, f"page-{int(pno):03d}")
        _dump(os.path.join(pdir, "source.json"), payload.get("source", {}))
        _dump(os.path.join(pdir, "model.json"), payload.get("model", {}))
        _dump(os.path.join(pdir, "translation.json"), payload.get("translation", {}))
        _dump(os.path.join(pdir, "layout.json"), payload.get("layout", {}))
        _dump(os.path.join(pdir, "render.json"), payload.get("render", {}))
        _dump(os.path.join(pdir, "diff.json"), payload.get("diff", {}))
    manifest = {
        "schema_version": 1,
        "meta": manifest_meta,
        "pages": {str(p): None for p in sorted(pages)},
        "summary": "summary.json",
    }
    _dump(os.path.join(out_dir, "manifest.json"), manifest)
    _dump(os.path.join(out_dir, "summary.json"), summary)
