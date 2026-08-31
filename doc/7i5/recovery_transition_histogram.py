# -*- coding: utf-8 -*-
"""7I-5C — corpus-wide recovery transition histogram.

Reads the in-pipeline layout trace for every sampled page of the 5-book corpus
and bins each block that ran a recovery by its final ``recovery.steps`` ladder
(from ``layout_recovery``).  This is the causality proof that 7I-5C fixed the
*chain* (WRAP -> SHRINK -> re-WRAP -> ACCEPT) rather than just flattening the
F8 detector number.

We compare against the 7I-4-4 pre-fix residual (F8 = 71) and the 7I-5A
causality breakdown:
  - pre-fix CLIP ladders: WRAP->SHRINK->CLIP 43 (mostly erroneous collapse),
    SHRINK->CLIP 28 (genuinely unbreakable).
After the fix, re-wrapable paragraphs should resolve to WRAP->SHRINK (fit) and
only genuinely-unbreakable tokens should remain CLIP.
"""

import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (ROOT, os.path.join(ROOT, "doc", "7i4")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dual_forensics.diff import load_provenance  # noqa: E402
from dual_forensics.snapshot import capture_source_chain  # noqa: E402
from dual_forensics.diff import aggregate_page_id_direct  # noqa: E402

from residual_corpus_scan import BOOKS, _render_plan_with_provenance  # noqa: E402

OUT_DIR = os.path.join(ROOT, "doc", "7i5-transition")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    by_steps = Counter()  # ladder -> total blocks
    clip_ladders = Counter()  # CLIP-terminated ladders only
    by_kind = Counter()
    per_book = {}
    total_blocks = 0
    total_clip = 0

    for book in BOOKS:
        path, pages, label = book["path"], book["pages"], book["label"]
        print(f"[{label}] pages {pages}")
        if not os.path.exists(path):
            continue
        snapshot = capture_source_chain(path, page_ids=pages)
        prov = _render_plan_with_provenance(path, pages, os.path.join(OUT_DIR, "prov"))
        prov_by_page = {k: load_provenance(v) for k, v in prov.items()}
        book_counter = Counter()
        for pno in pages:
            rows = snapshot.get("pages", {}).get(str(pno)) or []
            aggr = aggregate_page_id_direct(pno, rows, prov_by_page.get(pno, {}))
            for t in aggr["traces"] or []:
                total_blocks += 1
                rec = t.layout_recovery or {}
                steps = tuple(rec.get("steps") or [])
                key = "->".join(steps) if steps else "NO_ACTION"
                by_steps[key] += 1
                book_counter[key] += 1
                by_kind[(key, t.kind)] += 1
                if steps and steps[-1] == "CLIP":
                    clip_ladders[key] += 1
                    total_clip += 1
        per_book[label] = dict(book_counter)

    summary = {
        "schema_version": 4,
        "post_7i5c": True,
        "total_blocks": total_blocks,
        "total_clip": total_clip,
        "ladder_histogram": dict(by_steps),
        "clip_ladders": dict(clip_ladders),
        "per_book": per_book,
    }
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    with open(os.path.join(OUT_DIR, "report.md"), "w", encoding="utf-8") as f:
        w = f.write
        w("# 7I-5C — Recovery Transition Histogram (5 books / corpus sample)\n\n")
        w(f"- total blocks: **{total_blocks}**\n")
        w(f"- total CLIP: **{total_clip}**  (7I-4-4 pre-fix: **71**)\n")
        w("\n## Ladder histogram (recovery.steps final ladder → count)\n\n")
        w("| ladder | count |\n|---|---|\n")
        for k, c in by_steps.most_common():
            w(f"| {k} | {c} |\n")
        w("\n## Residual CLIP ladders (only genuinely-unbreakable should remain)\n\n")
        if clip_ladders:
            w("| ladder | count |\n|---|---|\n")
            for k, c in clip_ladders.items():
                w(f"| {k} | {c} |\n")
        else:
            w("no CLIP ladders remain in the sampled corpus.\n")
    print(
        f"\nwrote {OUT_DIR}/summary.json report.md; total clip = {total_clip} (was 71)"
    )
    for k, c in by_steps.most_common():
        print(f"  {k}: {c}")


if __name__ == "__main__":
    main()
