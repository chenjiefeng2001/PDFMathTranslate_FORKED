"""7N-TRACE — production trace smoke run on the real MP2e book.

Re-runs fixup + renderer over the 7N-8 pre-fixup plan (same geometry-only
path as ``7n9_fix3_reread_verify.py``) but with a **FlightRecorder**
attached, then feeds the produced JSONL to the trace-audit CLI.  This is
the "real translation is the test data source" loop: every block traverses
plan → fixup → render → raster-classified events with semantic coordinates,
and the invariant engine decides the verdict — no hand-written per-page
tests.

Usage:
    python doc/7n9_trace_smoke.py [--out doc/7n9-mp2e-fix3]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from doc.seven_n_helpers import (  # noqa: E402
    STEM,
    load,
    page_sizes_from_document,
    undo_old_shift,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default="doc/7n8-mp2e")
    ap.add_argument("--out", default="doc/7n9-mp2e-fix3")
    args = ap.parse_args(argv)

    magic = os.path.join(args.src, "output-magicpdf", "magicpdf")
    plan_path = os.path.join(magic, f"{STEM}_render_plan.json")
    doc_path = os.path.join(magic, f"{STEM}_document.json")
    src_pdf = os.path.join("tests", "file", f"{STEM}.pdf")
    for p in (plan_path, doc_path, src_pdf):
        if not os.path.exists(p):
            print(f"[TRACE] missing: {p}")
            return 2

    plan = load(plan_path)
    doc = load(doc_path)
    sizes = page_sizes_from_document(doc)
    print(f"[TRACE] pages: {len(sizes)}  plan entries: {len(plan)}")

    pre = undo_old_shift(plan)
    print(f"[TRACE] pre-fixup plan entries: {len(pre)}")

    from pdf2zh.v3.flight_recorder import FlightRecorder, read_events
    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
    from pdf2zh.v3.render_takeover import fixup_render_plan

    out_magic = os.path.join(args.out, "output-magicpdf", "magicpdf")
    os.makedirs(out_magic, exist_ok=True)
    trace_path = os.path.join(args.out, "trace", "mp2e_events.jsonl")

    rec = FlightRecorder(trace_path, book_id="mp2e", level=1)
    fixed, stats = fixup_render_plan(pre, trace=rec)
    mono = os.path.join(out_magic, f"{STEM}_mono_trace.pdf")
    _, render_stats = render_plan_to_pdf(
        fixed,
        page_sizes=sizes,
        output_path=mono,
        source_pdf=src_pdf,
        trace=rec,
    )
    rec.close()

    events = list(read_events(trace_path))
    print(f"[TRACE] events: {len(events)}  blocks: {render_stats['blocks']}")

    # ── invariant engine verdict (rules over real events) ───────────────
    from pdf2zh.v3.trace_rules import run_rules

    results = run_rules(events)
    from collections import Counter

    by_rule = Counter(r.rule for r in results)
    print(f"[TRACE] rule FAILs: {dict(by_rule) or 'none'}")
    for want in (
        "FLOW_BASELINE_SEMANTICS",
        "FLOW_BASELINE_MISMATCH",
        "SHIFT_DIRECTION",
        "DECOUPLED",
        "ERASE_GEOMETRY",
    ):
        if by_rule.get(want):
            print(f"[TRACE] !! {want} fired on a real run: {by_rule[want]}")

    # ── trace-audit CLI outputs ─────────────────────────────────────────
    from pdf2zh.v3.trace_audit import _run_audit

    audit_dir = os.path.join(args.out, "audit")
    rc = _run_audit(trace_path, pdf=mono, out=audit_dir)
    if rc != 0:
        print(f"[TRACE] trace-audit rc={rc}")
        return rc
    summary = load(os.path.join(audit_dir, "summary.json"))
    print(
        f"[TRACE] audit: qualification={summary.get('qualification')} "
        f"rule_fails={summary.get('rule_results')} "
        f"pages={summary.get('pages')}"
    )
    ledger = os.path.join(audit_dir, "defect-ledger.csv")
    n_rows = sum(1 for _ in open(ledger, encoding="utf-8")) - 1
    print(f"[TRACE] defect-ledger rows: {n_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
