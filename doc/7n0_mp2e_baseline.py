"""7N-0 — MP 2e recurring-defect baseline (evidence-only; no production change).

Question: with the 7I-5C re-wrap SHRINK fix landed and v1.9.16 frozen, why does
``tests/file/The Art of Multiprocessor Programming, 2e.pdf`` still reproduce the
old behavior (blocks laid out past their box / clipped / drifted)?

Method (matches the 7N plan):
  - identity translation, production parser/model/plan/renderer chain;
  - per-block checkpoint: source / parser / model / layout / emit;
  - 7N-3 instrumentation: which render path drew the block, and whether the
    7I-5C executor (adaptive_layout WRAP→SHRINK re-wrap) was actually hit;
  - classification per the 7N decision tree:
      FIX_HIT          — adaptive recovery ran (fix in play)
      FIX_BYPASSED     — flow payload missing/degraded → renderer fallback
      PRESERVE_REGION  — fixup keep_overflow (alternate path)
      SHIFT_DOWN       — fixup moved the box instead of adaptive recovery
      LEGACY_FALLBACK  — layout_ok=False / no commands → _insert_text_wrapped
      TOC_CHANNEL / PRESERVE / EMPTY — other channels
  - ``--expand`` probe: deterministic pseudo-CJK suffix per block simulating
    translation expansion, to observe the fixup vs adaptive interplay that the
    real Chinese output would hit.  Probe only — never production semantics.

Usage:
    python doc/7n0_mp2e_baseline.py [--out doc/7n0-mp2e] [--expand]
Output: <out>/summary.json, <out>/checkpoints.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BOOK = "tests/file/The Art of Multiprocessor Programming, 2e.pdf"
# 7I-4/7I-5 sample manifest (same pages the corpus baseline froze), so any
# comparison against 7I-4-4 / 7I-5C evidence is apples-to-apples.
PAGES = [0, 5, 8, 12, 20, 40, 80, 120, 200, 300, 400, 500, 550]

_EXPANSION_SUFFIX = (
    "（中文译文扩展探针：用于在恒等翻译之外模拟真实翻译的体积增长，"
    "触发 fixup 与 adaptive 恢复路径的交互，仅取证用。）"
)

_DECISIONS_HIT = {"wrap", "shrink", "clip", "preserve_overflow"}


def _classify(block: dict) -> str:
    """7N-3 per-block classification (which path actually drew this block)."""
    payload = block.get("render_payload") or {}
    fixup = block.get("render_fixup")
    pkind = payload.get("kind")

    if block.get("kind") == "toc":
        return "TOC_CHANNEL"
    if block.get("kind") not in ("paragraph", "flow"):
        return "PRESERVE_OR_OTHER"
    if not (block.get("source_text") or "").strip():
        return "EMPTY"
    has_cmds = bool(payload.get("commands") or [])
    if pkind != "flow" or not has_cmds:
        # renderer observable legacy fallback: payload_kind==flow but no
        # settled commands -> _insert_text_wrapped draws it (dst_box read).
        return "LEGACY_FALLBACK"
    # NB: layout_ok=False (terminal CLIP) still draws via the flow path —
    # the renderer only falls back when commands are EMPTY.
    if fixup == "keep_overflow":
        return "PRESERVE_REGION"  # fixup marked overflow, adaptive not in play
    if fixup == "shift_down":
        return "SHIFT_DOWN"  # fixup moved the box instead of adaptive recovery
    rec = payload.get("recovery") or {}
    if rec.get("decision") in _DECISIONS_HIT:
        return "FIX_HIT"
    if payload.get("overflow"):
        # overflow but no recovery record: lay_out default policy ran without
        # the executor ladder — needs eyes (7N-4 alternate-path candidate).
        return "OVERFLOW_NO_RECOVERY"
    return "CLEAN"


def _checkpoint_row(block: dict) -> str:
    payload = block.get("render_payload") or {}
    rec = payload.get("recovery") or {}
    steps = "->".join(rec.get("steps") or [])
    trace = payload.get("trace") or []
    trace_s = ";".join(f"{t.get('decision')}@{t.get('font_size')}" for t in trace)

    def _t(s, n=26):
        s = " ".join(str(s or "").split())
        return (s[: n - 1] + "…") if len(s) > n else s

    return (
        f"| {block.get('block_id')} | {_t(block.get('source_text'))} "
        f"| {_t(block.get('translated_text'))} | {payload.get('layout_ok')} "
        f"| {payload.get('overflow')} | {rec.get('decision') or '-'} | {steps or '-'} "
        f"| {trace_s or '-'} | {payload.get('font_size') or '-'} "
        f"| {block.get('classification')} | {block.get('render_fixup') or '-'} "
        f"| {block.get('render_object_type') or '-'} |"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="doc/7n0-mp2e")
    ap.add_argument(
        "--expand",
        action="store_true",
        help="append a deterministic pseudo-CJK suffix per block (expansion probe)",
    )
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    from pdf2zh.cid_recovery import extract_pages_recovering as extract_pages
    from pdf2zh.v3.document_model import (
        build_document_model,
        render_plan_from_model,
        translate_document,
    )
    from pdf2zh.v3.render_takeover import fixup_render_plan
    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
    from dual_forensics.snapshot import identity

    def translate_fn(s: str) -> str:
        out = identity(s)
        return out + _EXPANSION_SUFFIX if args.expand else out

    lt = list(extract_pages(BOOK, page_numbers=PAGES))
    model = build_document_model(lt)
    for k, p in enumerate(model.pages):
        if k < len(PAGES):
            p.page_num = PAGES[k]
    translate_document(model, translate_fn)
    plan = render_plan_from_model(model)
    fixed_plan, fixup_stats = fixup_render_plan(plan)
    page_sizes = {
        p.page_num: [float(p.width) or 612.0, float(p.height) or 792.0]
        for p in model.pages
    }
    _, render_stats = render_plan_to_pdf(
        fixed_plan, page_sizes=page_sizes, provenance=True
    )
    prov_by_id = {
        r["source_node_id"]: r for r in (render_stats.get("provenance") or [])
    }
    plan_by_id = {e["block_id"]: e for e in fixed_plan}

    blocks = []
    for page in model.pages:
        pno = page.page_num
        for i, b in enumerate(page.blocks):
            bid = f"p{pno}_{i}"
            entry = plan_by_id.get(bid) or {}
            prov = prov_by_id.get(bid) or {}
            payload = dict(entry.get("render_payload") or {})
            blocks.append(
                {
                    "block_id": bid,
                    "page": pno,
                    "kind": b.kind,
                    "source_text": b.text,
                    "translated_text": (b.metadata or {}).get("translated"),
                    "classification": None,
                    "render_fixup": entry.get("render_fixup"),
                    "render_path": entry.get("render_path"),
                    "render_object_type": prov.get("object_type"),
                    "render_payload": payload,
                }
            )

    for blk in blocks:
        blk["classification"] = _classify(blk)

    # ── aggregate ────────────────────────────────────────────────────────
    cls = Counter(b["classification"] for b in blocks)
    decisions = Counter()
    steps_hist = Counter()
    trace_hist = Counter()
    for b in blocks:
        payload = b["render_payload"] or {}
        rec = payload.get("recovery") or {}
        if rec.get("decision"):
            decisions[rec["decision"]] += 1
        if rec.get("steps"):
            steps_hist["->".join(rec["steps"])] += 1
        tr = payload.get("trace") or []
        if tr:
            trace_hist[
                "->".join(f"{t.get('decision')}@{t.get('font_size')}" for t in tr)
            ] += 1
    per_page = {}
    for b in blocks:
        per_page.setdefault(str(b["page"]), Counter())["blocks"] += 1
        per_page[str(b["page"])][b["classification"]] += 1

    summary = {
        "schema": "7n0-mp2e-baseline-v1",
        "book": BOOK,
        "pages": PAGES,
        "expand_probe": bool(args.expand),
        "blocks": len(blocks),
        "classification": dict(cls),
        "recovery_decisions": dict(decisions),
        "recovery_steps_histogram": dict(steps_hist),
        "recovery_trace_histogram": dict(trace_hist),
        "fixup_stats": {k: v for k, v in fixup_stats.items() if k != "fixed"},
        "fixup_fixed_detail": fixup_stats.get("fixed") or [],
        "render_stats": {k: v for k, v in render_stats.items() if k != "provenance"},
        "per_page": {
            k: dict(v) for k, v in sorted(per_page.items(), key=lambda kv: int(kv[0]))
        },
        "bad_blocks": [
            {
                "block_id": b["block_id"],
                "class": b["classification"],
                "fixup": b["render_fixup"],
                "overflow": (b["render_payload"] or {}).get("overflow"),
                "recovery": (b["render_payload"] or {}).get("recovery"),
                "trace": (b["render_payload"] or {}).get("trace"),
                "src_box": [
                    round(v, 1) for v in ((b["render_payload"] or {}).get("bbox") or [])
                ],
            }
            for b in blocks
            if b["classification"]
            in (
                "PRESERVE_REGION",
                "SHIFT_DOWN",
                "LEGACY_FALLBACK",
                "OVERFLOW_NO_RECOVERY",
            )
        ],
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    # ── report ───────────────────────────────────────────────────────────
    L = []
    L.append("# 7N-0 — MP 2e Baseline (evidence-only)")
    L.append("")
    L.append(f"- book: `{BOOK}`")
    L.append(f"- pages: {PAGES}")
    L.append(f"- expand probe: {bool(args.expand)}")
    L.append(f"- blocks: {len(blocks)}")
    L.append("")
    L.append("## 7N-3 classification (which path drew each block)")
    L.append("")
    L.append("| class | blocks |")
    L.append("|---|---|")
    for k, v in sorted(cls.items()):
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append("## Recovery decisions / steps / traces (blocks entering adaptive_layout)")
    L.append("")
    L.append(f"- decisions: {dict(decisions)}")
    L.append(f"- steps: {dict(steps_hist)}")
    L.append(f"- traces: {dict(trace_hist)}")
    L.append("")
    L.append("## fixup_render_plan stats (alternate-path signals)")
    L.append("")
    L.append(
        f"- preserved={fixup_stats['preserved']} shifted={fixup_stats['shifted']} "
        f"overflowed={fixup_stats['overflowed']}"
    )
    L.append("")
    L.append("## render path counters")
    L.append("")
    L.append(
        f"- flow_layout_used={render_stats.get('flow_layout_used', 0)} "
        f"flow_legacy_fallback={render_stats.get('flow_legacy_fallback', 0)} "
        f"flow_overflow={render_stats.get('flow_overflow', 0)}"
    )
    L.append("")
    L.append("## Per-page classification")
    L.append("")
    L.append("| page | blocks | classes |")
    L.append("|---|---|---|")
    for k in sorted(per_page, key=lambda x: int(x)):
        c = dict(per_page[k])
        n = c.pop("blocks", 0)
        L.append(f"| {k} | {n} | {c} |")
    L.append("")
    L.append("## Per-block checkpoints (suspicious classes first)")
    L.append("")
    L.append(
        "| block | source | translated | layout_ok | overflow | decision | steps "
        "| trace | font | class | fixup | drawn-as |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    order = {
        "PRESERVE_REGION": 0,
        "SHIFT_DOWN": 1,
        "LEGACY_FALLBACK": 2,
        "OVERFLOW_NO_RECOVERY": 3,
        "FIX_HIT": 4,
    }
    for blk in sorted(
        blocks, key=lambda b: (order.get(b["classification"], 9), b["block_id"])
    ):
        L.append(_checkpoint_row(blk))
    with open(os.path.join(args.out, "checkpoints.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")

    print("classification:", dict(cls))
    print("recovery decisions:", dict(decisions))
    print("steps:", dict(steps_hist))
    print("fixup:", {k: fixup_stats[k] for k in ("preserved", "shifted", "overflowed")})
    print(
        "render:",
        {
            k: render_stats.get(k, 0)
            for k in ("flow_layout_used", "flow_legacy_fallback", "flow_overflow")
        },
    )
    print("wrote", os.path.join(args.out, "summary.json"), "and checkpoints.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
