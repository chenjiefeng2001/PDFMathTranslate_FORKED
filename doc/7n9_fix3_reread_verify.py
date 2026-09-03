"""7N-FIX-3 — geometry-only full-book re-render verification.

The 7N-8 artifacts contain the POST-FIX-2 fixed plan (old +Δy shift
direction).  FIX-3 changed: (a) the renderer baseline anchor, (b) the erase
rect (src_box), and (c) the fixup shift direction (−Δy).  Translations are
unchanged — FIX-3 is geometry-only, so we reconstruct the pre-fixup plan
from the dump, re-run the FIXED fixup, re-render the mono PDF, and re-audit
— no translator/API needed.

Usage:
    python doc/7n9_fix3_reread_verify.py [--out doc/7n9-mp2e-fix3]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from doc.seven_n_helpers import STEM, load, page_sizes_from_document, undo_old_shift  # noqa: E402


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
            print(f"[FIX-3] missing: {p}")
            return 2

    plan = load(plan_path)
    doc = load(doc_path)

    sizes = page_sizes_from_document(doc)
    print(f"[FIX-3] pages: {len(sizes)}")

    pre = undo_old_shift(plan)
    n_shift_old = sum(1 for e in plan if e.get("render_fixup") == "shift_down")
    print(f"[FIX-3] old shift_down entries undone: {n_shift_old}")

    from pdf2zh.v3.render_takeover import fixup_render_plan

    fixed, stats = fixup_render_plan(pre)
    print(f"[FIX-3] re-fixup stats: {json.dumps(stats, ensure_ascii=False)}")

    # direction sanity: every shift_down must move DOWN (v3 y-up: −Δy)
    bad_dir = 0
    for e in fixed:
        if e.get("render_fixup") != "shift_down":
            continue
        src = e.get("src_box")
        dst = e.get("dst_box")
        if float(dst[3]) - float(src[3]) >= 0:
            bad_dir += 1
    print(f"[FIX-3] shift_down blocks with non-negative Δy: {bad_dir} (must be 0)")

    # decoupled invariant: first_cmd_y == dst_box.y1 after shift
    decoupled = 0
    for e in fixed:
        if e.get("render_fixup") != "shift_down":
            continue
        cmds = (e.get("render_payload") or {}).get("commands") or []
        if not cmds:
            continue
        first_y = float(cmds[0].get("y") or 0.0)
        if abs(first_y - float((e.get("dst_box") or [0, 0, 0, 0])[3])) > 0.5:
            decoupled += 1
    print(f"[FIX-3] decoupled after re-fixup: {decoupled} (must be 0)")

    from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

    out_magic = os.path.join(args.out, "output-magicpdf", "magicpdf")
    os.makedirs(out_magic, exist_ok=True)
    plan_out = os.path.join(out_magic, f"{STEM}_render_plan.json")
    with open(plan_out, "w", encoding="utf-8") as fh:
        json.dump(fixed, fh, ensure_ascii=False)
    print(f"[FIX-3] wrote {plan_out}")

    mono = os.path.join(out_magic, f"{STEM}_mono.pdf")
    _, render_stats = render_plan_to_pdf(
        fixed,
        page_sizes=sizes,
        output_path=mono,
        source_pdf=src_pdf,
    )
    print(
        f"[FIX-3] mono rendered: pages={render_stats['pages']} blocks={render_stats['blocks']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
