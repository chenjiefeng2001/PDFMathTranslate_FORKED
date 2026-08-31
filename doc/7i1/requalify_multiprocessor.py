"""7I-1-after: requalify "The Art of Multiprocessor Programming, 2e".

The book was excluded from 7H-1 because build_document_model appeared to
infinite-hang. 7I-1 removed that blocker (catastrophic regex backtracking in
_RE_LEADER).  This script runs the same in-pipeline provenance measurement as
7I-0 (identity translation + production renderer + ID-direct diff) on sample
pages and writes doc/7i1-multiprocessor-provenance/{summary.json,report.json}.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dual_forensics.snapshot import capture_source_chain
from dual_forensics.diff import load_provenance, aggregate_page_id_direct
from dual_forensics.defect import run_defect_detectors
from dual_forensics.__main__ import _render_plan_with_provenance
from dual_forensics.pdf_inspector import inspect_page

import pymupdf

BOOK = "tests/file/The Art of Multiprocessor Programming, 2e.pdf"
OUT = "doc/7i1-multiprocessor-provenance"
# Sample: front matter (TOC, roman numerals), body, code, index.
PAGES = [0, 5, 8, 12, 20, 40, 80, 120, 200, 300, 400, 500, 550]


def main():
    os.makedirs(OUT, exist_ok=True)
    src = pymupdf.open(BOOK)
    total_pages = src.page_count
    src.close()
    print(f"source pages: {total_pages}; sample: {PAGES}")

    snapshot = capture_source_chain(BOOK, page_ids=PAGES)
    if snapshot["errors"]:
        print("snapshot errors:", snapshot["errors"])
        return 1

    prov = _render_plan_with_provenance(BOOK, PAGES, OUT)
    prov_by_page = {k: load_provenance(v) for k, v in prov.items()}

    dual_doc = pymupdf.open(BOOK)  # no external dual: MuPDF content-stream check on source pages
    findings = []
    pages_out = {}
    for pno in PAGES:
        rows = snapshot["pages"].get(str(pno)) or []
        page = dual_doc[pno]
        dual_evidence = inspect_page(dual_doc, pno, page.rect.height)
        aggr = aggregate_page_id_direct(pno, rows, prov_by_page.get(pno, {}))
        traces = aggr["traces"]
        finds = run_defect_detectors(traces, dual_evidence)
        cs = dual_evidence.get("content_stream") or {}
        if cs.get("anomaly"):
            finds.append({
                "defect_id": "F9",
                "page": pno,
                "first_divergence": "render",
                "evidence": {"mupdf_syntax_error": cs.get("sample")},
            })
        for nid in aggr["dangling_blocks"]:
            finds.append({
                "defect_id": "F10",
                "page": pno,
                "node_id": nid,
                "first_divergence": "render",
                "evidence": {"dangling": True, "confidence": "uncertain"},
            })
        findings.extend(finds)
        preserved_violation = sum(
            1 for t in traces
            if t.kind in ("code", "formula", "filename", "identifier")
            and (t.translation_status or "") not in ("preserved", "done", "")
        )
        pages_out[str(pno)] = {
            "blocks": aggr["total_blocks"],
            "present": aggr["present_blocks"],
            "dangling": len(aggr["dangling_blocks"]),
            "stray": len(aggr["stray_records"]),
            "findings": len(finds),
            "preserved_violation": preserved_violation,
            "by_kind": {},
        }
        for t in traces:
            k = t.kind or "unknown"
            pages_out[str(pno)]["by_kind"][k] = pages_out[str(pno)]["by_kind"].get(k, 0) + 1
        print(f"page {pno:>3}: blocks={aggr['total_blocks']:>3} present={aggr['present_blocks']:>3} "
              f"dangling={len(aggr['dangling_blocks'])} findings={len(finds)}")

    by_fd = {}
    by_defect = {}
    for f in findings:
        if isinstance(f, dict):
            fd = f.get("first_divergence", "unknown")
            did = f.get("defect_id", "?")
        else:  # DefectFinding dataclass
            fd = getattr(f, "first_divergence", "unknown")
            did = getattr(f, "defect_id", "?")
        by_fd[fd] = by_fd.get(fd, 0) + 1
        by_defect[did] = by_defect.get(did, 0) + 1

    summary = {
        "book": BOOK,
        "total_pages": total_pages,
        "sample_pages": PAGES,
        "model_blocks_total": snapshot.get("model_stats", {}).get("blocks"),
        "defects": {"total": len(findings), "by_first_divergence": by_fd, "by_defect_id": by_defect},
        "pages": pages_out,
    }
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(json.dumps(summary["defects"], ensure_ascii=False))
    print(f"wrote {OUT}/summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
