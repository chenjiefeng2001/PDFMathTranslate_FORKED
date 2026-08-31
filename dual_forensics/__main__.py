"""CLI: ``python -m dual_forensics --source ... --dual ... --page N ...``.

Per requested source page, snapshot the source chain (parser/model/translation/
layout), inspect the matching page of the *rendered* Dual PDF (the alternating
origin/translation pages ``2N`` / ``2N+1``), diff, run defect detectors, and
write ``forensic-report/`` as described in :mod:`.report`.
"""

from __future__ import annotations

import argparse
import logging
from typing import Dict, List

import pymupdf

from dual_forensics import __version__
from dual_forensics.diff import aggregate_page, aggregate_page_id_direct, load_provenance
from dual_forensics.defect import run_defect_detectors
from dual_forensics.pdf_inspector import inspect_page
from dual_forensics.report import build_summary, write_report_tree
from dual_forensics.snapshot import capture_source_chain

log = logging.getLogger("dual_forensics")


class _AppendPages(argparse.Action):
    """Accumulate page numbers across repeated ``--page`` flags/lists."""

    def __call__(self, parser, namespace, values, option_string=None):
        seen = list(getattr(namespace, self.dest, None) or [])
        seen.extend(int(v) for v in values)
        setattr(namespace, self.dest, seen)


def _dual_page_numbers(src_page: int, dual_count: int) -> Dict[str, int]:
    """Default dual mapping: alternating origin(2N)/translation(2N+1)."""
    even, odd = 2 * src_page, 2 * src_page + 1
    if even < dual_count and (even + 1) < dual_count:
        return {"origin": even, "translation": odd}
    if even < dual_count:
        return {"origin": even, "translation": even}
    return {}


def _render_plan_with_provenance(source, page_ids, out_dir):
    """Render the source pages' plan (identity translation) with provenance.

    Uses the production renderer (`render_plan_to_pdf`) with ``provenance=True``
    so each block carries ``source_node_id → render_object_ref``.  Returns
    ``{page_num: [provenance records]``.  Only reachable when
    ``--use-provenance-render`` is set (requires no translator — identity).
    """
    from pdf2zh.v3.document_model import (
        build_document_model,
        render_plan_from_model,
        translate_document,
    )
    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
    from pdfminer.high_level import extract_pages

    from dual_forensics.snapshot import identity

    lt = list(extract_pages(source, page_numbers=sorted(page_ids) if page_ids else None))
    model = build_document_model(lt)
    wanted = sorted(page_ids) if page_ids else None
    if wanted:
        for k, p in enumerate(model.pages):
            if k < len(wanted):
                p.page_num = wanted[k]
    translate_document(model, identity)
    plan = render_plan_from_model(model)
    sizes = {p.page_num: [float(p.width) or 612.0, float(p.height) or 792.0] for p in model.pages}
    _, stats = render_plan_to_pdf(plan, page_sizes=sizes, provenance=True)
    by_page: Dict[int, List] = {}
    for r in stats.get("provenance") or []:
        by_page.setdefault(int(r["page"]), []).append(r)
    return by_page


def _run_pages(source, dual, pages: List[int], out: str, prov_render: bool = False) -> int:
    src_doc = pymupdf.open(source)
    dual_doc = pymupdf.open(dual)
    try:
        src_snapshot = capture_source_chain(source, page_ids=pages if pages else None)
        if src_snapshot["errors"]:
            log.error("source chain errors: %s", src_snapshot["errors"])
    finally:
        src_doc.close()

    all_pages: Dict[int, Dict] = {}
    findings: List[dict] = []
    prov_by_page: Dict[int, dict] = {}
    if prov_render:
        try:
            _prov = _render_plan_with_provenance(source, pages, out)
            prov_by_page = {k: load_provenance(v) for k, v in _prov.items()}
        except Exception as exc:  # noqa: BLE001
            log.error("provenance render failed: %s", exc)

    for pno in sorted(src_snapshot["pages"].keys()):
        pno = int(pno)
        rows = src_snapshot["pages"][str(pno)]
        # After capture, recompute the block-derived plan evidence from the
        # source snapshot (we already have it).  Inspect the dual translation page.
        mp = _dual_page_numbers(pno, dual_doc.page_count)
        dual_evidence = {}
        if "translation" in mp:
            page = dual_doc[mp["translation"]]
            dual_evidence = inspect_page(dual_doc, mp["translation"], page.rect.height)
        else:
            dual_evidence = {
                "note": f"no dual translation page for source p{pno}",
                "text_runs": [],
                "drawings": [],
                "content_stream": {},
            }
        # Split source evidence into stage files (same rows re-partitioned).
        source_stage = [
            {
                "node_id": e["node_id"],
                "index": e["primitive_index"],
                **e.get("parser", {}),
                "translation_status": e.get("translation", {}).get(
                    "translation_status"
                ),
            }
            for e in rows
        ]
        model_stage = [{"node_id": e["node_id"], **e.get("model", {})} for e in rows]
        translation_stage = [
            {"node_id": e["node_id"], **e.get("translation", {})} for e in rows
        ]
        layout_stage = [
            {"node_id": e["node_id"], "kind": e.get("kind"), **e.get("layout", {})}
            for e in rows
        ]

        if prov_by_page.get(pno):
            aggr = aggregate_page_id_direct(pno, rows, prov_by_page[pno])
            dual_evidence["id_direct"] = True
        else:
            aggr = aggregate_page(pno, rows, dual_evidence.get("text_runs") or [])
        traces = aggr["traces"]  # list of Trace
        finds = run_defect_detectors(traces, dual_evidence)
        # Page-level renderer signal: MuPDF emitter failure → one F9/F10 page
        # finding (not one per block).  The malformed float is emitted once; a
        # per-block cascade would inflate the count 100x.
        cs = dual_evidence.get("content_stream") or {}
        if cs.get("anomaly"):
            finds.append(
                {
                    "defect_id": "F9",
                    "name": "text layer vs visual layer mismatch",
                    "suspect_layer": "renderer",
                    "node_id": f"p{pno}_page",
                    "page": pno,
                    "evidence": {"mupdf_syntax_error": cs.get("sample")},
                    "first_divergence": "render",
                    "stage_verdicts": {
                        "source": "PASS",
                        "parser": "PASS",
                        "model": "PASS",
                        "translation": "PASS",
                        "layout": "PASS",
                        "render": "FAIL",
                    },
                    "note": "renderer emitted a malformed float into the page stream",
                }
            )
        # Blocks planned to render but with no matched run: F8 truncation / F10
        # draw-lost *candidates*.  The dual odd page re-lays-out content at new
        # coordinates, so a missing geometry match is usually a match-gap, not a
        # real drop — mark them UNCERTAIN, never a confirmed renderer verdict.
        for nid in aggr["dangling_blocks"]:
            finds.append(
                {
                    "defect_id": "F10",
                    "name": "XObject / draw object lost or drifted",
                    "suspect_layer": "renderer / object preservation",
                    "node_id": nid,
                    "page": pno,
                    "evidence": {
                        "dangling": True,
                        "node_id": nid,
                        "confidence": "uncertain",
                    },
                    "first_divergence": "render",
                    "stage_verdicts": {
                        "source": "PASS",
                        "parser": "PASS",
                        "model": "PASS",
                        "translation": "PASS",
                        "layout": "PASS",
                        "render": "FAIL",
                    },
                    "note": "planned block had no matched rendered run at source coords "
                    "(UNCERTAIN: likely a match-gap, dual re-layout moves text)",
                }
            )

        # diff.json = {…, traces, defects}
        aggr_dict = dict(aggr)
        aggr_dict["traces"] = [
            {
                **t.__dict__,
                "render_rows": t.render_rows,
                "rendered_text": t.rendered_text,
            }
            for t in traces
        ]
        aggr_dict["defects"] = [
            f.to_dict() if hasattr(f, "to_dict") else f for f in finds
        ]

        all_pages[pno] = {
            "source": source_stage,
            "model": model_stage,
            "translation": translation_stage,
            "layout": layout_stage,
            "render": dual_evidence,
            "diff": aggr_dict,
        }
        findings.extend(f.to_dict() if hasattr(f, "to_dict") else f for f in finds)

    summary = build_summary(
        [{"path": source, "pages_analysed": sorted(all_pages)}],
        findings,
    )
    write_report_tree(
        out,
        all_pages,
        summary,
        {
            "source": source,
            "dual": dual,
            "version": __version__,
            "pages": sorted(all_pages),
        },
    )
    print(
        f"wrote forensic report to {out}/  over {len(all_pages)} pages; "
        f"{len(findings)} defect findings."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dual-forensics",
        description="7H-1 Dual Fidelity Forensics: where a Dual-PDF defect first appears",
    )
    parser.add_argument("--source", required=True, help="original source PDF")
    parser.add_argument("--dual", required=True, help="generated dual PDF")
    parser.add_argument(
        "--page",
        type=int,
        nargs="+",
        default=[],
        action=_AppendPages,
        metavar="N",
        help="0-based source page number(s)",
    )
    parser.add_argument("--out", default="forensic-report")
    parser.add_argument(
        "--use-provenance-render",
        dest="prov_render",
        action="store_true",
        help="Render source pages with provenance and diff ID-directly "
        "(7H-2A) instead of geometry-matching the external dual",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return _run_pages(args.source, args.dual, args.page, args.out, args.prov_render)


if __name__ == "__main__":
    raise SystemExit(main())
