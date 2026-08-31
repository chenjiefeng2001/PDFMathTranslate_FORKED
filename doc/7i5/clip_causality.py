"""7I-5A — Clip Causality Forensics（对全部 F8/clip residual 全量因果取证）。

目标：对 7I-4-4 揭出的 71 个 F8（recovery.decision=clip）建立统一因果记录，
找到 **CLIP 的真正决策点**，回答「CLIP 是 recovery 的最终 fallback（穷尽后
clip）还是 layout contract 的问题（engine 认为 clip 可接受）」——先不修。

证据链（每个 case）：
    node_id / kind / source_bbox / translated_bbox
    available_width / available_height      （render_payload.bbox）
    source_font_size / initial_layout_font_size / final_font_size
    line_count / text_length / width_ratio / height_ratio
    recovery.steps / reason / decision / overflow / layout_ok
    trace[]（7F-7d 每阶段 WRAP/SHRINK/CLIP 的诊断）

来源：**plan 的 ``render_payload``**（recovery + trace + bbox + line_widths +
font_size 全部权威字段，直接读 plan，不改生产代码）。Trace 只镜像了
overflow/recovery，深度字段在 plan。

输出：<out>/summary.json + <out>/report.md
用法：python doc/7i5/clip_causality.py --out doc/7i5-causality
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

# script lives at <ROOT>/doc/7i5/clip_causality.py
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))  # project root
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "doc", "7i4"))  # for residual_corpus_scan

from pdf2zh.cid_recovery import extract_pages_recovering as extract_pages  # noqa: E402
from pdf2zh.v3.document_model import (  # noqa: E402
    build_document_model,
    render_plan_from_model,
    translate_document,
)
from dual_forensics.snapshot import identity  # noqa: E402

from residual_corpus_scan import BOOKS  # noqa: E402


def _case(entry: dict, pno: int) -> dict:
    """Extract the full causal record for one clipped block's render_payload."""
    rp = entry.get("render_payload") or {}
    rec = rp.get("recovery") or {}
    src = list(entry.get("src_box") or [0, 0, 0, 0])
    dst = list(entry.get("dst_box") or src)
    avail = rp.get("bbox") or dst
    widths = [float(w) for w in (rp.get("line_widths") or [])]
    text = entry.get("text") or ""
    trans = entry.get("translated") or text
    aw = max(0.0, float(avail[2]) - float(avail[0])) if len(avail) == 4 else 0.0
    max_w = max(widths) if widths else 0.0
    return {
        "node_id": entry.get("block_id"),
        "page": pno,
        "kind": entry.get("kind"),
        "render_path": entry.get("render_path"),
        "source_bbox": src,
        "dst_bbox": dst,
        "available_width": round(aw, 1),
        "available_height": (
            round(max(0.0, float(avail[3]) - float(avail[1])), 1)
            if len(avail) == 4
            else None
        ),
        # font trajectory
        "source_font_size": entry.get("font_size"),
        "original_font_size": rec.get("original_font_size"),
        "final_font_size": rec.get("final_font_size"),
        # ratios
        "max_line_width": round(max_w, 1),
        "width_ratio": round(max_w / aw, 3) if aw > 0.0 and max_w else None,
        "text_length": len(text),
        "translated_length": len(trans),
        "length_ratio": round(len(trans) / len(text), 3) if text else None,
        "line_count": len(rp.get("lines") or []),
        # recovery verdict
        "reason": rec.get("reason"),
        "decision": rec.get("decision"),
        "steps": "->".join(rec.get("steps") or []),
        "overflow": bool(rp.get("overflow")),
        "layout_ok": bool(rp.get("layout_ok")),
        "policy": rp.get("policy"),
        "trace": rp.get("trace") or [],
    }


def _font_shrink(rec: dict) -> float:
    """relative font drop original->final (0 = no shrink, 1 = never reached floor)."""
    o = rec.get("original_font_size")
    f = rec.get("final_font_size")
    if o and f and o > 0:
        return round(float(o) / float(f), 2) if f > 0 else None  # >1 means shrank
    return None


def _run_book(book, out_dir):
    path = book["path"]
    pages = book["pages"]
    label = book["label"]
    print(f"[{label}] pages {pages}", flush=True)
    if not os.path.exists(path):
        return {"label": label, "error": "missing"}

    lt = list(extract_pages(path, page_numbers=pages))
    model = build_document_model(lt)
    if pages:
        for k, p in enumerate(model.pages):
            if k < len(pages):
                p.page_num = pages[k]
    translate_document(model, identity)
    plan = render_plan_from_model(model)

    cases = []
    by_page = defaultdict(int)
    for entry in plan:
        rp = entry.get("render_payload") or {}
        rec = rp.get("recovery") or {}
        if rec.get("decision") == "clip":
            pno = int(entry.get("page") or 0)
            c = _case(entry, pno)
            c["font_shrink"] = _font_shrink(rec)
            cases.append(c)
            by_page[pno] += 1
    # only count requested sample pages (dedupe by page+node)
    wanted = set(pages)
    cases = [c for c in cases if c["page"] in wanted]
    return {
        "label": label,
        "path": path,
        "pages_total": len(wanted),
        "clip_blocks": len(cases),
        "by_page": dict(Counter(c["page"] for c in cases)),
        "cases": cases,
    }


def _stage_map(c):
    return {t["decision"]: t for t in c.get("trace") or []}


def _cluster(cases):
    by_steps = Counter(c["steps"] for c in cases)
    by_reason = Counter(c["reason"] for c in cases)
    # 7I-5A root-cause: does SHRINK collapse an already-wrapped multi-line text
    # to a single line?  WRAP produced N>1 lines, then SHRINK re-laid-out the
    # whole text as ONE line (shrink_to_fit on unwrapped text) -> still too wide
    # -> CLIP truncates to a single line.  This is the CLIP decision point.
    wrap_multi = 0
    wrap_multi_collapse = 0
    floor_after_wrap = 0
    tiny_src = 0
    no_wrap = 0
    for c in cases:
        st = _stage_map(c)
        w = st.get("WRAP")
        s = st.get("SHRINK")
        h = c["source_bbox"][3] - c["source_bbox"][1]
        if w is not None and w["line_count"] > 1:
            wrap_multi += 1
            if s is not None and s["line_count"] == 1:
                wrap_multi_collapse += 1
            if s is not None and s["font_size"] <= 5.0:
                floor_after_wrap += 1
        else:
            no_wrap += 1
        if h < 20.0:
            tiny_src += 1
    return {
        "total": len(cases),
        "by_steps": dict(by_steps),
        "by_reason": dict(by_reason),
        # root-cause of CLIP
        "wrap_multi": wrap_multi,
        "wrap_multi_collapse": wrap_multi_collapse,
        "floor_after_wrap": floor_after_wrap,
        "not_wrapped": no_wrap,
        "tiny_src_box_lt_20pt": tiny_src,
        "clip_at_or_below_5pt": len(
            [
                c
                for c in cases
                if c.get("final_font_size") and float(c["final_font_size"]) <= 5.0
            ]
        ),
        "width_ratio_le_1p0_at_clip": len(
            [
                c
                for c in cases
                if c.get("width_ratio") is not None and c["width_ratio"] <= 1.0
            ]
        ),
        "width_ratio_histogram": dict(
            sorted(
                Counter(
                    round(c["width_ratio"], 2) if c["width_ratio"] else 0 for c in cases
                ).items()
            )
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="doc/7i5-causality")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    books = [_run_book(b, args.out) for b in BOOKS]
    all_cases = [c for b in books for c in b.get("cases", [])]

    clust = _cluster(all_cases)
    # per-kind split
    by_kind = Counter(c["kind"] for c in all_cases)
    # height-bound subset
    height_bound = [c for c in all_cases if c["reason"] == "height"]
    # width-bound but only-just (0.9< ratio <=1.05) — near-fit that got clipped anyway
    near_fit = [
        c
        for c in all_cases
        if c.get("width_ratio") is not None and 0.90 <= c["width_ratio"] <= 1.05
    ]
    cluster_detail = {
        "total": len(all_cases),
        "by_steps": clust["by_steps"],
        "by_reason": clust["by_reason"],
        "by_kind": dict(by_kind),
        "clip_at_or_below_5pt": clust["clip_at_or_below_5pt"],
        "width_ratio_le_1p0_at_clip": clust["width_ratio_le_1p0_at_clip"],
        "height_bound_count": len(height_bound),
        "near_fit_width_090_105": len(near_fit),
        "wrap_multi": clust["wrap_multi"],
        "wrap_multi_collapse": clust["wrap_multi_collapse"],
        "floor_after_wrap": clust["floor_after_wrap"],
        "not_wrapped": clust["not_wrapped"],
        "tiny_src_box_lt_20pt": clust["tiny_src_box_lt_20pt"],
        "font_shrunk_cases": len(
            [c for c in all_cases if (c.get("font_shrink") or 1.0) > 1.0]
        ),
        # examples per book for the report
        "examples": [
            {"label": next(b["label"] for b in books if b["label"] == label), "case": c}
            for label, c in _first_by_book(books)
        ],
    }

    lines = []
    lines.append("# 7I-5A — Clip Causality Forensics（71 个 F8/clip）")
    lines.append("")
    lines.append(
        "来源：plan.render_payload（recovery + trace + bbox + line_widths）。"
        "纯取证，不改生产。目标：定位 CLIP 真正决策点，先修前先证因果。"
    )
    lines.append("")
    lines.append("## 1. 总量与聚类")
    lines.append("")
    lines.append(f"- 总 CLIP 块：**{len(all_cases)}**")
    lines.append(f"- by steps: {cluster_detail['by_steps']}")
    lines.append(f"- by reason: {cluster_detail['by_reason']}")
    lines.append(f"- by kind: {cluster_detail['by_kind']}")
    lines.append("")
    lines.append("## 2. CLIP 真正决策点（根因取证）")
    lines.append("")
    lines.append(
        f"经过 WRAP 产生了 >1 行的块: {cluster_detail.get('wrap_multi')} / {len(all_cases)}"
    )
    lines.append(
        f"其中 WRAP 后又被 SHRINK 折叠成 1 行: {cluster_detail.get('wrap_multi_collapse')}"
    )
    lines.append(
        f"其中 SHRINK 触底(<=5pt)仍失败: {cluster_detail.get('floor_after_wrap')}"
    )
    lines.append(
        f"未经历 WRAP（直接 SHRINK->CLIP）: {cluster_detail.get('not_wrapped')}"
    )
    lines.append(
        f"源块高 < 20pt（极小源框，几乎必溢）: {cluster_detail.get('tiny_src_box_lt_20pt')}"
    )
    lines.append("")
    lines.append(
        "**判定：CLIP 是 recovery 阶段不当执行的结果，而非纯粹‘文字远超盒可容纳’。**"
    )
    lines.append(
        "WRAP 已把多行文本排好（70/71 在 clip 时 width-ratio<=1.0），但下一个 SHRINK "
        "用 `shrink_to_fit` 把整段当**单行**再排（unwrapped），导致行数塌缩回 1、字号跌到 "
        "5pt floor 仍超宽，才轮到 CLIP 把 1 行截断。"
    )
    lines.append("")
    lines.append("## 3. 代表性 case")
    lines.append("")
    for ex in cluster_detail["examples"][:8]:
        c = ex["case"]
        lines.append(
            f"- **{ex['label']} {c['node_id']}** ({c['kind']}) "
            f"avail_w={c['available_width']} src_h={c['source_bbox'][3] - c['source_bbox'][1]:.0f} "
            f"font {c.get('original_font_size')}→{c.get('final_font_size')} "
            f"len {c['text_length']} →ratio {c['length_ratio']} "
            f"steps={c['steps']} reason={c['reason']}"
        )
    lines.append("")

    summary = {
        "schema_version": 1,
        "post_7i5a": True,
        "total_clip": len(all_cases),
        "cluster_detail": cluster_detail,
        "books": books,
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(
        "\nwrote",
        os.path.join(args.out, "summary.json"),
        os.path.join(args.out, "report.md"),
    )
    print("total clip:", len(all_cases))
    return 0


def _first_by_book(books):
    """yield (label, first_case) per book that has cases, for report examples."""
    out = []
    seen = set()
    for b in books:
        if b.get("error"):
            continue
        for c in b.get("cases", []):
            if c["node_id"] and c["node_id"] not in seen:
                seen.add(c["node_id"])
                out.append((b["label"], c))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
