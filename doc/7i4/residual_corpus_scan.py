"""7I-4-0 — Expanded Residual Corpus Scan（post-7I-3）。

在 7I-3（stage-aware FDS attribution + font-aware CID recovery）落地后的真实
Dual corpus 上重跑 in-pipeline provenance 测量，并**把 undefined CID 单独分类**：

    every undefined (font, cid) at the parser
        ├─ recover_unicode == a Unicode  → recoverable / recovered_unicode
        └─ recover_unicode == None      → unrecoverable / preserved placeholder

对每个样本页输出：
  - 缺陷分布（F1..F10）+ first-divergence-stage + anomaly 直方图（parser/
    translation/layout/render）；
  - CID artifacts（recoverable / unrecoverable / recovered Unicode /
    preserved placeholder）＋典型例。

Corpus（5 书，页集合沿用 7I-0 manifest，Multiprocessor 沿用 7I-1/3 样本）：

    C 书（Large-Scale C I）    [62, 65, 69, 75, 185, 186, 187]
    AI for Games              [0, 10, 20, 30, 40]
    Game Physics              [0, 15, 30, 45]
    Networking                [0, 12, 24, 36, 48]
    Multiprocessor 2e         [0, 5, 8, 12, 20, 40, 80, 120, 200, 300, 400, 500, 550]

用法：
    python doc/7i4/residual_corpus_scan.py            # 全 5 书
    python doc/7i4/residual_corpus_scan.py --book GP  # 只看某一书
    python doc/7i4/residual_corpus_scan.py --out doc/7i4-corpus-scan
输出：<out>/summary.json 与 <out>/report.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pymupdf  # noqa: E402

from pdfminer.pdfdocument import PDFDocument  # noqa: E402
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager  # noqa: E402
from pdfminer.pdfpage import PDFPage  # noqa: E402
from pdfminer.pdfparser import PDFParser as PDFMinerParser  # noqa: E402

from pdf2zh.cid_recovery import recover_unicode  # noqa: E402
from pdf2zh.converter import PDFConverterEx  # noqa: E402
from dual_forensics.__main__ import _render_plan_with_provenance  # noqa: E402
from dual_forensics.defect import (  # noqa: E402
    STATUS_NOT_MEASURED,
    aggregate_coverage,
    coverage_page,
    run_defect_detectors,
)
from dual_forensics.diff import aggregate_page_id_direct, load_provenance  # noqa: E402
from dual_forensics.pdf_inspector import inspect_page  # noqa: E402
from dual_forensics.snapshot import capture_source_chain  # noqa: E402

BOOKS = [
    {
        "label": "C book",
        "path": "pdf2zh_files/Large-Scale C Volume I_ Process and Architecture -- "
        "јohn Lakos -- 2020 _2c3bdba4.pdf",
        "pages": [62, 65, 69, 75, 185, 186, 187],
    },
    {
        "label": "AI for Games",
        "path": "pdf2zh_files/AI for Games and Animation A Cognitive Modeling "
        "Approach John David Fun_4ca3f7b5.pdf",
        "pages": [0, 10, 20, 30, 40],
    },
    {
        "label": "Game Physics",
        "path": "pdf2zh_files/Game Physics David H. Eberly z-library.sk "
        "1lib.sk z-lib.sk.pdf",
        "pages": [0, 15, 30, 45],
    },
    {
        "label": "Networking",
        "path": "pdf2zh_files/Networking and Online Games Understanding and "
        "Engineering Multiplayer I_1eed56a6.pdf",
        "pages": [0, 12, 24, 36, 48],
    },
    {
        "label": "Multiprocessor 2e",
        "path": "tests/file/The Art of Multiprocessor Programming, 2e.pdf",
        "pages": [0, 5, 8, 12, 20, 40, 80, 120, 200, 300, 400, 500, 550],
    },
]

DIV_STAGES = ["source", "parser", "model", "translation", "layout", "render", "pdf"]


class _CIDProbe(PDFConverterEx):
    """Production converter that additionally records every undefined char.

    For each (font, cid) pdfminer cannot decode we capture the recovery
    decision — recovered Unicode or None (kept placeholder) — without changing
    pipeline semantics.
    """

    def __init__(self, rsrcmgr):
        super().__init__(rsrcmgr)
        self.records = []  # [(basefont, cid, recovered_unicode_or_None)]
        self.page_records = {}

    def handle_undefined_char(self, font, cid: int) -> str:
        recovered = recover_unicode(font, cid)
        self.records.append((getattr(font, "basefont", None), cid, recovered))
        return recovered if recovered is not None else "(cid:%d)" % cid


def _scan_with_cid_probe(path: str, page_ids):
    """Enumerate undefined (font, cid) → recovery outcome per requested page."""
    out = {p: [] for p in page_ids}
    with open(path, "rb") as fp:
        parser = PDFMinerParser(fp)
        doc = PDFDocument(parser)
        rsrcmgr = PDFResourceManager()
        probe = _CIDProbe(rsrcmgr)
        interp = PDFPageInterpreter(rsrcmgr, probe)
        pages = list(PDFPage.create_pages(doc))
        for pno in page_ids:
            if pno >= len(pages):
                continue
            page = pages[pno]
            page.pageno = pno
            interp.process_page(page)
            out[pno] = list(probe.records)
            probe.records = []
    return out


def _cid_summary(page_cid: dict):
    recovered = []
    unrecovered = []
    for font, cid, uni in sum(page_cid.values(), []):
        if uni is not None:
            recovered.append((font, cid, uni))
        else:
            unrecovered.append((font, cid))
    return {
        "undefined_total": len(recovered) + len(unrecovered),
        "recovered_unicode": len(recovered),
        "preserved_placeholder": len(unrecovered),
        "recoverable": {
            "count": len(recovered),
            "examples": [list(x) for x in recovered[:8]],
        },
        "unrecoverable": {
            "count": len(unrecovered),
            "examples": [list(x) for x in unrecovered[:8]],
        },
    }


def _run_book(book, out_dir):
    path = book["path"]
    pages = book["pages"]
    label = book["label"]
    print(f"[{label}@{path}] pages {pages}", flush=True)
    if not os.path.exists(path):
        return {"label": label, "path": path, "pages": pages, "error": "missing"}

    snapshot = capture_source_chain(path, page_ids=pages)
    prov = _render_plan_with_provenance(path, pages, os.path.join(out_dir, "prov"))
    prov_by_page = {k: load_provenance(v) for k, v in prov.items()}
    page_cid = _scan_with_cid_probe(path, pages)

    src_doc = pymupdf.open(path)
    findings = []
    books_rows = 0
    present = 0
    dangling_all = []
    stray = 0
    preserved_violation = 0
    per_page = {}
    coverage_by_page = {}
    for pno in pages:
        rows = snapshot.get("pages", {}).get(str(pno)) or []
        if not rows:
            continue
        page = src_doc[pno]
        dual_evidence = inspect_page(src_doc, pno, page.rect.height)
        aggr = aggregate_page_id_direct(pno, rows, prov_by_page.get(pno, {}))
        traces = aggr["traces"]
        finds = run_defect_detectors(traces, dual_evidence)
        cover = coverage_page(traces, dual_evidence)
        coverage_by_page[pno] = cover
        cs = dual_evidence.get("content_stream") or {}
        if cs.get("anomaly"):
            finds.append(
                {
                    "defect_id": "F9",
                    "page": pno,
                    "first_divergence": "render",
                    "evidence": {"mupdf_syntax_error": cs.get("sample")},
                }
            )
        for nid in aggr["dangling_blocks"]:
            finds.append(
                {
                    "defect_id": "F10",
                    "page": pno,
                    "node_id": nid,
                    "first_divergence": "render",
                    "evidence": {"dangling": True, "confidence": "uncertain"},
                }
            )
        findings.extend(finds)
        books_rows += aggr["total_blocks"]
        present += aggr["present_blocks"]
        dangling_all.extend(aggr["dangling_blocks"])
        stray += len(aggr["stray_records"])
        preserved_violation += sum(
            1
            for t in traces
            if t.kind in ("code", "formula", "filename", "identifier")
            and (t.translation_status or "") not in ("preserved", "done", "")
        )
        per_page[str(pno)] = {"blocks": aggr["total_blocks"], "findings": len(finds)}
    src_doc.close()

    cov = aggregate_coverage(coverage_by_page)

    by_defect = {}
    by_fd = {}
    anomalies = {k: 0 for k in ("parser", "translation", "layout", "render")}
    for f in findings:
        if isinstance(f, dict):
            did = f.get("defect_id", "?")
            fd = f.get("first_divergence", "unknown")
        else:  # DefectFinding dataclass
            did = getattr(f, "defect_id", "?")
            fd = getattr(f, "first_divergence", "unknown") or "unknown"
        by_defect[did] = by_defect.get(did, 0) + 1
        by_fd[fd] = by_fd.get(fd, 0) + 1
        if fd in anomalies:
            anomalies[fd] += 1

    return {
        "label": label,
        "path": path,
        "pages": pages,
        "blocks": books_rows,
        "present": present,
        "dangling": len(dangling_all),
        "stray": stray,
        "preserved_violation": preserved_violation,
        "defects": by_defect,
        "by_first_divergence": by_fd,
        "anomalies": anomalies,
        "cid": _cid_summary(page_cid),
        "per_page": per_page,
        "detector_coverage": cov,
    }


def _fmt_row(headers, row):
    return " | ".join(str(row.get(h, "-")) for h in headers)


def _write_report(books, summary, path):
    lines = []
    lines.append("# 7I-4-0 — Expanded Residual Corpus Scan（post-7I-3）")
    lines.append("")
    lines.append(
        "日期：2026-08-31 · 方法：in-pipeline provenance（恒等翻译 + 生产 "
        "renderer + ID-direct diff + run_defect_detectors）+ font-aware CID "
        "recovery 分类（7I-3B）。"
    )
    lines.append("")

    lines.append("## 1. Defect & FDS 分布（按书）")
    lines.append("")
    hed = [
        "书",
        "块",
        "present",
        "dangling",
        "stray",
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
        "F6",
        "F8",
        "F9",
        "F10",
        "preserved_v",
    ]
    lines.append("| " + " | ".join(hed) + " |")
    lines.append("|" + "---|" * len(hed))
    for b in books:
        row = {
            "书": b["label"],
            "块": b.get("blocks", 0),
            "present": b.get("present", 0),
            "dangling": b.get("dangling", 0),
            "stray": b.get("stray", 0),
        }
        f = b.get("defects", {})
        row.update({f"F{i}": f.get(f"F{i}", 0) for i in (1, 2, 3, 4, 5, 6, 8, 9, 10)})
        row["preserved_v"] = b.get("preserved_violation", 0)
        lines.append("| " + _fmt_row(hed, row) + " |")

    lines.append("")
    lines.append("## 2. FDS 直方图")
    lines.append("")
    for b in books:
        fd = b.get("by_first_divergence", {})
        parts = ", ".join(f"{k}={fd.get(k, 0)}" for k in DIV_STAGES if fd.get(k))
        lines.append(f"- **{b['label']}**: {parts or '全部 0'}")
    lines.append("")

    lines.append("## 3. CID artifacts（undefined CID 分类）")
    lines.append("")
    hed2 = ["书", "undefined", "recovered Unicode", "preserved placeholder"]
    lines.append("| " + " | ".join(hed2) + " |")
    lines.append("|" + "---|" * len(hed2))
    for b in books:
        c = b.get("cid", {})
        lines.append(
            "| {0} | {1} | {2} | {3} |".format(
                b["label"],
                c.get("undefined_total", 0),
                c.get("recovered_unicode", 0),
                c.get("preserved_placeholder", 0),
            )
        )
    lines.append("")
    for b in books:
        c = b.get("cid", {})
        rec = c.get("recoverable", {}).get("examples", [])
        unr = c.get("unrecoverable", {}).get("examples", [])
        lines.append(f"### {b['label']}")
        if rec:
            lines.append(
                "- recoverable 例子："
                + "; ".join(f"`{f} {c2} → {u!r}`" for f, c2, u in rec)
            )
        if unr:
            lines.append(
                "- unrecoverable 例子：" + "; ".join(f"`{f} {c2}`" for f, c2 in unr)
            )
        lines.append("")

    # 7I-4-1 — detector coverage acceptance table (PASS/FAIL/SKIP/NOT_MEASURED).
    lines.append("## 4. Detector Coverage（7I-4 contract）")
    lines.append("")
    lines.append(
        "每格：`状态 已评测页面/总页面`（pass/fail/skip/not_measured）。"
        "SKIP/NOT_MEASURED **不等于 0**——表示该 defect 未被能力覆盖。"
    )
    lines.append("")
    hed3 = ["书"] + [f"F{i}" for i in (1, 2, 3, 4, 5, 6, 8, 9, 10)]
    lines.append("| " + " | ".join(hed3) + " |")
    lines.append("|" + "---|" * len(hed3))
    for b in books:
        cov = b.get("detector_coverage", {})
        row = {"书": b["label"]}
        for fid in ("F1", "F2", "F3", "F4", "F5", "F6", "F8", "F9", "F10"):
            c = cov.get(fid, {})
            st = c.get("status", STATUS_NOT_MEASURED)
            row[fid] = f"{st} {c.get('pages_evaluated', 0)}/{c.get('pages_total', 0)}"
        lines.append("| " + _fmt_row(hed3, row) + " |")
    lines.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=None, help="label filter, e.g. GP/C/AI/Net/MP")
    ap.add_argument("--out", default="doc/7i4-corpus-scan")
    args = ap.parse_args()

    books = BOOKS
    if args.book:

        def match(b):
            lb = b["label"].lower()
            return (
                args.book.lower() in lb
                or {
                    "c": "c book",
                    "gp": "game physics",
                    "ai": "ai for games",
                    "net": "networking",
                    "mp": "multiprocessor",
                }.get(args.book.lower())
                == lb
            )

        books = [b for b in BOOKS if match(b)]

    os.makedirs(args.out, exist_ok=True)
    results = [_run_book(b, args.out) for b in books]
    summary = {
        "schema_version": 2,
        "post_7i3": True,
        "books": results,
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    _write_report(results, summary, os.path.join(args.out, "report.md"))
    print(
        "\nwrote",
        os.path.join(args.out, "summary.json"),
        os.path.join(args.out, "report.md"),
    )
    # console one-liner
    for b in results:
        c = b.get("cid", {})
        print(
            f"{b['label']:>14s}: defects={b.get('defects', {})} "
            f"FDS={b.get('by_first_divergence', {})} "
            f"cid={c.get('undefined_total', 0)}(rec={c.get('recovered_unicode', 0)}/"
            f"keep={c.get('preserved_placeholder', 0)})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
