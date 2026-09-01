"""``--debug-layout`` analysis entry — Commit 7F-7c.

Opens a PDF with pdfminer (LTChar stream), builds the v3 document model,
translates with an **identity** translator (pure analysis — no translator /
ONNX / renderer involved, mirroring ``--debug-toc`` / ``--debug-list``),
settles the render plan, and writes ``debug/layout.json``:

    {
      "schema_version": 1,
      "diagnostics": [
        {"page": 1, "block_index": 3, "kind": "flow",
         "primitive_kind": "flow", "target": null,
         "source_text": "...", "translated_text": "...",
         "bbox": [...], "resolved_bbox": [...],
         "overflow": false, "recovery": null, "trace": [],
         "anchors": {}, "font_size": 11.0}
      ],
      "summary": {"blocks": 4, "overflow": 1, "recovered": 1,
                  "preserved_overflow": 0},
      "page_flow": {                       <- 7F-8a/8b cross-block analysis
        "placements": [...], "collisions": [...], "overflows": [...],
        "summary": {"blocks": 4, "collision_count": 0,
                     "page_overflow_count": 0, "by_reason": {}}},
      "page_recovery": {                   <- 7F-8c decision contract
        "decisions": [{"page": 1, "block_index": 4, "target": "lower",
                        "collision": {"upper": 3, "lower": 4,
                                       "overlap": 18.5, "required_shift": 18.5,
                                       "bbox_mode": "resolved"},
                        "recovery": {"decision": "shift_down",
                                      "shift_y": 18.5, "reason": "overlap"}}],
        "summary": {"total": 1, "by_decision": {"shift_down": 1}}}
    }

The ``page_flow`` section answers the 7F-8a/8b DoD question — which page /
which block collides with which block and by how many pt — without moving
anything (pure detection).  The ``page_recovery`` section (7F-8c) turns each
collision into a decision contract (``KEEP`` / ``SHIFT_DOWN`` / ``NEXT_PAGE`` /
``PRESERVE_OVERFLOW``), still without executing any movement.  Pure analysis:
never modifies the PDF, never translates (identity), never renders.  The
diagnostics chain is the **settled render plan** read by
:mod:`pdf2zh.semantic.layout.diagnostics` — nothing here re-lays-out.
"""

from __future__ import annotations

import json
import os

__all__ = ["dump_layout_debug"]


def _identity_translate(text: str) -> str:
    return text


def dump_layout_debug(pdf_path: str, out_dir: str | None = None) -> dict:
    """Analyze a PDF's layout pipeline and write ``<out_dir>/layout.json``.

    Returns the payload dict (also written to disk).  Any failure is recorded
    as an empty diagnostics payload — observability never raises.
    """
    payload = {
        "schema_version": 1,
        "diagnostics": [],
        "summary": {
            "blocks": 0,
            "overflow": 0,
            "recovered": 0,
            "preserved_overflow": 0,
        },
    }
    try:
        from pdfminer.high_level import extract_pages

        from pdf2zh.semantic.layout.diagnostics import (
            collect_layout_diagnostics,
            summarize_diagnostics,
        )
        from pdf2zh.v3.document_model import (
            build_document_model,
            render_plan_from_model,
            translate_document,
        )

        from pdf2zh.semantic.layout.page_flow import build_page_flow_report
        from pdf2zh.semantic.layout.page_recovery import (
            decide_page_recovery,
            decision_summary,
        )

        ltpages = list(extract_pages(pdf_path))
        model = build_document_model(ltpages)
        translate_document(model, _identity_translate)
        plan = render_plan_from_model(model)
        diags = collect_layout_diagnostics(plan)
        # 7F-8a/8b: cross-block vertical collisions + page-boundary overflows;
        # 7F-8c: decision contract per collision — detection + decision only,
        # nothing moves, the plan is never mutated.
        page_sizes = {}
        for lp in ltpages:
            try:
                ph = float(getattr(lp, "height", 0.0) or 0.0)
            except (TypeError, ValueError):
                ph = 0.0
            if ph > 0.0:
                page_sizes[int(getattr(lp, "pageid", 0))] = ph
        flow_report = build_page_flow_report(plan, page_sizes=page_sizes)
        decisions = decide_page_recovery(plan, page_sizes=page_sizes)
        payload = {
            "schema_version": 1,
            "diagnostics": [d.to_dict() for d in diags],
            "summary": summarize_diagnostics(diags),
            "page_flow": flow_report.to_dict(),
            "page_recovery": {
                "decisions": [d.to_dict() for d in decisions],
                "summary": decision_summary(decisions),
            },
        }
    except Exception as exc:  # noqa: BLE001 -- 分析失败写空载荷，不阻断
        payload["error"] = str(exc)[:200]

    out_dir = out_dir or "debug"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "layout.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return payload
