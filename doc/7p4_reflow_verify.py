"""验收 (b)：页级纵向重排是否真的降低了正文叠印？

离线跑真实 50 页计划（``doc/7p8-thread4-nocache``），其余环节一律不变，只比较
「开/关 reflow」两个版本的 ``audit_page_geometry``。

关键纪律：**先证明开关真的改变了落笔位置**，再看叠印数。曾经两次把空操作当成否证
（改渲染期参数、缩放不存在的 ``line_step`` 字段），所以这里以
``reflow_shifted_blocks`` 作为前置判据：为 0 就直接报 INCONCLUSIVE。

跑法::

    python doc/7p4_reflow_verify.py doc/7p8-thread4-nocache
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from pdf2zh.v3 import magicpdf_renderer as R  # noqa: E402

BOOK = ROOT / "tests" / "file" / "The Art of Multiprocessor Programming, 2e.pdf"


def _background(pages: dict) -> dict:
    doc = pymupdf.open(str(BOOK))
    out = pymupdf.open()
    bg = {}
    for pno in sorted(pages):
        w, h = pages[pno]
        pg = out.new_page(width=w, height=h)
        if pno < doc.page_count:
            pg.show_pdf_page(pg.rect, doc, pno)
        bg[pno] = R.page_span_snapshot(pg)
    out.close()
    doc.close()
    return bg


def _run(plan, sizes, bg, tag, enabled: bool) -> dict:
    orig = R._reflow_page_entries
    if not enabled:
        R._reflow_page_entries = lambda entries, page_height, stats=None: list(entries)
    try:
        pdf, stats = R.render_plan_to_pdf(plan, page_sizes=sizes, source_pdf=str(BOOK))
    finally:
        R._reflow_page_entries = orig

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    geo = R.audit_page_geometry(doc, ignore=bg)
    doc.close()
    r = {
        "tag": tag,
        "enabled": enabled,
        "overlap_pages": geo["pages_with_overlap"],
        "outside": geo["spans_outside_page"],
        "reflow_shifted_blocks": stats.get("reflow_shifted_blocks", 0),
        "reflow_pages": stats.get("reflow_pages", 0),
        "fit_shrunk": stats.get("fit_shrunk", 0),
        "deep": stats.get("fit_shrunk_deep", 0),
        "best_effort": stats.get("fit_shrunk_best_effort", 0),
        "expanded": stats.get("box_expanded", 0),
        "overflow_preserved": stats.get("wrap_overflow_preserved", 0),
        "clipped": stats.get("wrap_clipped_page", 0),
        "wrap_truncated": stats.get("wrap_truncated", 0),
    }
    print(
        f"  {tag:<14} ovl={r['overlap_pages']:>3} outside={r['outside']:>2} "
        f"shifted={r['reflow_shifted_blocks']:>4} pages={r['reflow_pages']:>3} "
        f"shrunk={r['fit_shrunk']:>3} deep={r['deep']:>2} best={r['best_effort']:>2} "
        f"expand={r['expanded']:>2} clip={r['clipped']:>2} trunc={r['wrap_truncated']:>2}"
    )
    return r


def main(out_dir: str) -> int:
    d = Path(out_dir)
    plan = json.loads(
        next(d.glob("output/**/*_render_plan.json")).read_text(encoding="utf-8")
    )
    pages = sorted({int(b.get("page") or 0) for b in plan})
    with pymupdf.open(str(BOOK)) as doc:
        sizes = {p: [doc[p].rect.width, doc[p].rect.height] for p in pages}
    bg = _background(sizes)

    print(f"plan: {d}  ({len(plan)} blocks, {len(pages)} pages)")
    print()
    off = _run(plan, sizes, bg, "reflow OFF", False)
    on = _run(plan, sizes, bg, "reflow ON", True)

    print()
    print("=" * 84)
    print("VERDICT")
    print("=" * 84)
    bites = on["reflow_shifted_blocks"] > 0
    print(f"  reflow actually moved blocks? {bites}")
    print(
        f"  overlap pages : {off['overlap_pages']} (OFF) -> "
        f"{on['overlap_pages']} (ON)   delta "
        f"{on['overlap_pages'] - off['overlap_pages']:+d}"
    )
    print(f"  out-of-page   : {off['outside']} -> {on['outside']}")
    print(
        f"  content safety: truncated {on['wrap_truncated']}, "
        f"clipped {on['clipped']}, overflow-preserved {on['overflow_preserved']}"
    )
    if not bites:
        print("  -> INCONCLUSIVE：reflow 一个块都没推，本实验什么也没测到。")
    elif on["overlap_pages"] < off["overlap_pages"]:
        print("  -> 成立：页级下推降低了正文叠印。")
    elif on["overlap_pages"] == off["overlap_pages"]:
        print("  -> 无变化：推了块但叠印没降 —— 需要查是哪些重叠不由块间碰撞造成。")
    else:
        print("  -> **变差**：下推本身制造了新的碰撞，需要回退。")
    print("=" * 84)

    (d / "reflow-verify.json").write_text(
        json.dumps({"off": off, "on": on}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"wrote {d / 'reflow-verify.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p8-thread4-nocache")
    )
