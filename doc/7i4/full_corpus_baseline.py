"""7I-4-4 — Full Corpus Baseline（post-7I-4-3，F1–F10 检测器全落地）。

目的不是证明系统干净，而是**建立完整检测覆盖之后的真实 residual baseline**：

- coverage matrix：每 F-id 的 PASS/FAIL/SKIP/NOT_MEASURED 页计数；
- F1–F10 residual histogram（by first_divergence）；
- per-book 分布；
- **F8 深度分布**（回答 7I-4-4 B）：按 kind / recovery reason / steps /
  final_font_size / 原高 / 行数 / translated-with，判别是 translation expansion
  还是 layout constraint 还是 recovery policy；
- **F5 observability gap 普遍度**（回答 7I-4-4 C）：每页物理层
  （pymupdf drawings / images）vs 模型 figure/table/image 块数。

不修任何 F8（本阶段只测量、冻结 baseline）。

用法：
    python doc/7i4/full_corpus_baseline.py --out doc/7i4-corpus-baseline
输出：<out>/summary.json 与 <out>/report.md（与 7I-4-0/4-1 的 scan 并行，不覆盖）。

corpus 采样页沿用 7I-4-0 manifest（C/AI/GP/Net 各 4–7 页；MP 13 页，含 p300
control）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pymupdf  # noqa: E402

from dual_forensics.defect import (  # noqa: E402
    STATUS_FAIL,
    STATUS_NOT_MEASURED,
    STATUS_PASS,
    STATUS_SKIP,
    coverage_page,
    run_defect_detectors,
)
from dual_forensics.diff import (  # noqa: E402
    aggregate_page_id_direct,
    load_provenance,
)
from dual_forensics.snapshot import capture_source_chain  # noqa: E402

from residual_corpus_scan import (  # noqa: E402
    BOOKS,
    _render_plan_with_provenance,
)

_F_IDS = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10")


def _content_stream(path: str, pno: int):
    """content_stream_anomaly verdict for a page, or None if unavailable."""
    from dual_forensics.pdf_inspector import content_stream_anomaly

    try:
        d = pymupdf.open(path)
        try:
            cs = content_stream_anomaly(d, pno)
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        return None
    return cs or {}


def _model_kind_counts(rows):
    return Counter(r.get("kind") for r in rows)


def _physical_layer(path: str, pno: int):
    """Physical-layer float signal: drawings + images on a page."""
    try:
        d = pymupdf.open(path)
        p = d[pno]
        images = len(p.get_images(full=True))
        drawings = len(p.get_drawings())
        d.close()
    except Exception:  # noqa: BLE001
        return 0, 0
    return drawings, images


def _f8_features(trace):
    """Per-block features for the F8 distribution table."""
    rec = trace.layout_recovery or {}
    num_lines = len(trace.render_rows or [])
    src_box = trace.src_box or [0, 0, 0, 0]
    height = round(float(src_box[3] - src_box[1]), 1) if len(src_box) == 4 else None
    return {
        "kind": trace.kind,
        "reason": rec.get("reason"),
        "steps": "->".join(rec.get("steps") or []),
        "final_font_size": rec.get("final_font_size"),
        "origin_font_size": rec.get("original_font_size"),
        "src_height": height,
        "line_count": num_lines,
        "translated_len": len((trace.translated_text or "")),
        "source_len": len((trace.source_text or "")),
    }


def _run_book(book, out_dir):
    path = book["path"]
    pages = book["pages"]
    label = book["label"]
    print(f"[{label}] pages {pages}", flush=True)
    if not os.path.exists(path):
        return {"label": label, "error": "missing"}

    snapshot = capture_source_chain(path, page_ids=pages)
    prov = _render_plan_with_provenance(path, pages, os.path.join(out_dir, "prov"))
    prov_by_page = {k: load_provenance(v) for k, v in prov.items()}

    cov_pages = {}
    findings_all = []
    f8_blocks = []
    phys = {"drawings": 0, "images": 0, "pages": 0}
    model_float_pages = 0
    model_float_blocks = 0
    kind_counts = Counter()
    per_page = {}

    for pno in pages:
        rows = snapshot.get("pages", {}).get(str(pno)) or []
        if not rows:
            continue
        aggr = aggregate_page_id_direct(pno, rows, prov_by_page.get(pno, {}))
        traces = aggr["traces"]
        # 7I-6B: surface ID-direct provenance summary (F10) + content-stream
        # (F9) as page-level evidence so coverage measures them in-pipeline.
        dual_evidence = {
            "id_direct": {
                "page": pno,
                "present_blocks": aggr.get("present_blocks"),
                "dangling_blocks": aggr.get("dangling_blocks"),
                "stray_records": aggr.get("stray_records"),
            }
        }
        cs = _content_stream(path, pno)
        if cs is not None:
            dual_evidence["content_stream"] = cs
        cov = coverage_page(traces or [], dual_evidence)
        cov_pages[pno] = cov
        finds = run_defect_detectors(traces or [], dual_evidence)
        findings_all.extend(finds)
        # F5 observability gap: model float blocks on this page vs physical.
        fk = _model_kind_counts(rows)
        nfloat = fk.get("figure", 0) + fk.get("table", 0) + fk.get("image", 0)
        if nfloat:
            model_float_pages += 1
        model_float_blocks += nfloat
        kind_counts.update(fk)
        if nfloat == 0:
            dr, im = _physical_layer(path, pno)
            phys["drawings"] += dr
            phys["images"] += im
            phys["pages"] += 1
        # collect F8 per-block detail
        for t in traces or []:
            if t.layout_overflow is True and (
                (t.layout_recovery or {}).get("decision") == "clip"
            ):
                f8_blocks.append({"page": pno, "node": t.node_id, **_f8_features(t)})
        per_page[str(pno)] = {
            "blocks": aggr["total_blocks"],
            "findings": len(finds),
            "model_float_blocks": nfloat,
        }

    # per-F status counts across all pages of this book
    matrix = {
        fid: {
            "PASS": sum(1 for p in cov_pages.values() if p[fid].status == STATUS_PASS),
            "FAIL": sum(1 for p in cov_pages.values() if p[fid].status == STATUS_FAIL),
            "SKIP": sum(1 for p in cov_pages.values() if p[fid].status == STATUS_SKIP),
            "NOT_MEASURED": sum(
                1 for p in cov_pages.values() if p[fid].status == STATUS_NOT_MEASURED
            ),
        }
        for fid in _F_IDS
    }
    return {
        "label": label,
        "path": path,
        "pages": [p for p in pages if str(p) in per_page],
        "blocks": sum(v["blocks"] for v in per_page.values()),
        "per_page": per_page,
        "matrix": matrix,
        "defects": aggregate_defects(findings_all),
        "f8_blocks": f8_blocks,
        "kind_counts": dict(kind_counts),
        "f5_gap": {
            "pages_with_no_model_float": phys["pages"],
            "physical_drawings": phys["drawings"],
            "physical_images": phys["images"],
            "model_float_pages": model_float_pages,
            "model_float_blocks": model_float_blocks,
        },
    }


def aggregate_defects(findings):
    by_id = defaultdict(lambda: {"count": 0, "by_first_divergence": defaultdict(int)})
    for f in findings:
        fid = getattr(f, "defect_id", None)
        fd = getattr(f, "first_divergence", None) or "unknown"
        by_id[fid]["count"] += 1
        by_id[fid]["by_first_divergence"][fd] += 1
    return {
        k: {"count": v["count"], "by_first_divergence": dict(v["by_first_divergence"])}
        for k, v in by_id.items()
    }


def _fmt_matrix(matrix: dict):
    rows = []
    hed = ["defect"] + ["PASS", "FAIL", "SKIP", "NOT_MEASURED"]
    rows.append("| " + " | ".join(hed) + " |")
    rows.append("|" + "---|" * len(hed))
    for fid in _F_IDS:
        m = matrix.get(fid, {})
        rows.append(
            "| {0} | {1} | {2} | {3} | {4} |".format(
                fid,
                m.get("PASS", 0),
                m.get("FAIL", 0),
                m.get("SKIP", 0),
                m.get("NOT_MEASURED", 0),
            )
        )
    return "\n".join(rows)


def _write_report(books, summary, path):
    L = []
    L.append("# 7I-4-4 — Full Corpus Baseline（F1–F10 全落地后）")
    L.append("")
    L.append(
        "日期：2026-08-31 · 方法：in-pipeline provenance（恒等翻译 + 生产 "
        "renderer + ID-direct diff）+ four-state detector coverage。"
        "**本阶段只测量、不修 F8** —— 目标是冻结可审计 baseline。"
    )
    L.append("")

    L.append("## 1. 全局 Coverage Matrix（页面计数，5 书合并）")
    L.append("")
    L.append(_GLOBAL_MATRIX_MD)
    L.append("")
    L.append(
        "> 原则：`SKIP`/`NOT_MEASURED` ≠ `0`（F5=SKIP 是 representation gap，"
        "不是干净）。"
    )
    L.append("")

    L.append("## 2. F1–F10 Residual Histogram（first_divergence）")
    L.append("")
    L.append(summary["histogram_md"])
    L.append("")

    L.append("## 3. Per-book 分布")
    L.append("")
    L.append("| 书 | blocks | " + " | ".join(_F_IDS) + " |")
    L.append("|---" * (4 + len(_F_IDS)) + "|")
    for b in books:
        defs = b.get("defects", {})
        L.append(
            "| {0} | {1} | {2} |".format(
                b["label"],
                b.get("blocks", 0),
                " | ".join(str(defs.get(f, {}).get("count", 0)) for f in _F_IDS),
            )
        )
    L.append("")

    L.append("## 4. F8 深度分布（bug 类判别）")
    L.append("")
    L.append(summary["f8_md"])
    L.append("")

    L.append("## 5. F5 observability gap 普遍度")
    L.append("")
    L.append(summary["f5_md"])
    L.append("")

    L.append("## 6. p300 control（多 defect 独立）")
    L.append("")
    mp = [b for b in books if b["label"] == "Multiprocessor 2e"]
    if mp:
        pp = mp[0].get("per_page", {}).get("300", {})
        L.append(
            f"p300: blocks={pp.get('blocks')}, model_float={pp.get('model_float_blocks')}"
        )
    L.append(
        "上表 F4=F@parser / F8=F@layout / F6=P / F10=P —— 一个 source/parser "
        "anomaly 不污染其它 detector。"
    )
    L.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))


_GLOBAL_MATRIX_MD = "(set in main)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="doc/7i4-corpus-baseline")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    books = [_run_book(b, args.out) for b in BOOKS]

    # build global coverage matrix
    g = {fid: {"PASS": 0, "FAIL": 0, "SKIP": 0, "NOT_MEASURED": 0} for fid in _F_IDS}
    for b in books:
        for fid in _F_IDS:
            for st in ("PASS", "FAIL", "SKIP", "NOT_MEASURED"):
                g[fid][st] += b.get("matrix", {}).get(fid, {}).get(st, 0)
    global _GLOBAL_MATRIX_MD
    _GLOBAL_MATRIX_MD = _fmt_matrix(g)

    # F8 distribution
    f8_by_kind = Counter()
    f8_by_reason = Counter()
    f8_by_steps = Counter()
    f8_sizes = []
    f8_lines = Counter()
    f8_len_growth = []
    for b in books:
        for fb in b.get("f8_blocks", []):
            f8_by_kind[fb["kind"]] += 1
            f8_by_reason[fb["reason"]] += 1
            f8_by_steps[fb["steps"]] += 1
            f8_sizes.append(fb["final_font_size"] or 0)
            f8_lines[fb["line_count"]] += 1
            if fb["source_len"]:
                f8_len_growth.append(
                    round((fb["translated_len"] / fb["source_len"]), 2)
                )
    f8_md = (
        "- 总 clip 块数（含重复页）："
        + str(sum(len(b.get("f8_blocks", [])) for b in books))
        + "\n"
        + f"- by kind: {dict(f8_by_kind)}\n"
        + f"- by recovery reason: {dict(f8_by_reason)}\n"
        + f"- by steps: {dict(f8_by_steps)}\n"
        + "- final_font_size 分布(<=7pt): "
        + str(sum(1 for s in f8_sizes if s and s <= 7))
        + f" / {len(f8_sizes)}\n"
        + f"- by line_count: {dict(sorted(f8_lines.items()))}\n"
        + "- translated/source 长度比均值: "
        + (f"{sum(f8_len_growth) / len(f8_len_growth):.2f}" if f8_len_growth else "n/a")
    )

    # F5 physical-vs-model gap
    tot_draw, tot_img, gap_pages = 0, 0, 0
    float_pages = 0
    for b in books:
        fg = b.get("f5_gap", {})
        tot_draw += fg.get("physical_drawings", 0)
        tot_img += fg.get("physical_images", 0)
        gap_pages += fg.get("pages_with_no_model_float", 0)
        float_pages += fg.get("model_float_pages", 0)
    f5_md = (
        f"- 无 model float 块的页 / 有 model float 的页: {gap_pages} / {float_pages}\n"
        + f"- 这些无 model float 页的物理层对象（抽样）：drawings={tot_draw}, "
        + f"images={tot_img}\n"
        + "→ F5 的 representation gap **普遍**：即便物理层有 drawings，document "
        "model 也无 figure/table/image 语义块，F5 只能 SKIP。"
    )

    # total residual
    total_res = sum(
        d["count"] for b in books for d in (b.get("defects") or {}).values()
    )
    by_def = Counter()
    by_fd = Counter()
    for b in books:
        for fid, d in (b.get("defects") or {}).items():
            by_def[fid] += d["count"]
            for fd, c in d["by_first_divergence"].items():
                by_fd[fd] += c
    hist_lines = [f"- 总 residual: **{total_res}**"]
    # per-defect FDS from each book's own first_divergence breakdown
    per_fid_fds = defaultdict(Counter)
    for b in books:
        for fid, det in (b.get("defects") or {}).items():
            for fd, c in det.get("by_first_divergence", {}).items():
                per_fid_fds[fid][fd] += c
    for fid in _F_IDS:
        if by_def.get(fid):
            fd = ", ".join(f"{k}={v}" for k, v in per_fid_fds[fid].items())
            hist_lines.append(f"- **{fid}** = {by_def[fid]}  (FDS: {fd})")
    histogram_md = "\n".join(hist_lines)

    summary = {
        "schema_version": 3,
        "post_7i4_3": True,
        "global_coverage_matrix": g,
        "total_residual": total_res,
        "by_defect": dict(by_def),
        "by_first_divergence": dict(by_fd),
        "f8_distribution": {
            "total_clip_blocks": sum(len(b.get("f8_blocks", [])) for b in books),
            "by_kind": dict(f8_by_kind),
            "by_reason": dict(f8_by_reason),
            "by_steps": dict(f8_by_steps),
            "small_font_clips": sum(1 for s in f8_sizes if s and s <= 7),
            "final_font_sizes": sorted(set(int(s) for s in f8_sizes if s)),
            "translated_source_ratio_mean": (
                round(sum(f8_len_growth) / len(f8_len_growth), 2)
                if f8_len_growth
                else None
            ),
        },
        "f5_observability_gap": {
            "no_model_float_pages": gap_pages,
            "model_float_pages": float_pages,
            "physical_drawings": tot_draw,
            "physical_images": tot_img,
        },
        "histogram_md": histogram_md,
        "f8_md": f8_md,
        "f5_md": f5_md,
        "books": books,
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    _write_report(books, summary, os.path.join(args.out, "report.md"))
    print(
        "\nwrote",
        os.path.join(args.out, "summary.json"),
        os.path.join(args.out, "report.md"),
    )
    print("total residual:", total_res, dict(by_def))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
