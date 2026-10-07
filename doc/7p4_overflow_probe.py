"""叠印的机制假设：文字被画出了自己的框，撞进邻块。

``doc/7p4_overlap_pairs.py`` 抽出 page 15 的重叠对后发现：压在一起的是**相邻两个正文块**
（字号 9.96 与 9.46），纵向只错开 2.3pt。两块在该页都走 ``translate_refit``，没有
``shift_down``。

于是有三种可能，必须分清：

A. MinerU 给的 ``src_box``/``dst_box`` 本来就重叠 —— 解析侧的问题，P1/P2 无关；
B. 文字在框内正常，但**行距被压缩**导致相邻行相接 —— 渲染侧行高问题；
C. 文字**画出了框底**，撞进下一个块 —— 这正是 P1 的 ``box_expanded`` /
   ``wrap_overflow_preserved`` 会做的事（它们故意越框以避免丢字）。

本脚本对每一页逐块测量：``dst_box`` 的纵向范围 vs 该块**实际落笔**的纵向范围。
超出量 > 0 即 C。

跑法::

    python doc/7p4_overflow_probe.py doc/7p8-thread4-nocache [page ...]
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

from pdf2zh.v3.magicpdf_renderer import (  # noqa: E402
    _span_key,
    page_span_snapshot,
    render_plan_to_pdf,
)

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
        bg[pno] = page_span_snapshot(pg)
    out.close()
    doc.close()
    return bg


def main(out_dir: str, wanted: list[int]) -> int:
    d = Path(out_dir)
    plan = json.loads(
        next(d.glob("output/**/*_render_plan.json")).read_text(encoding="utf-8")
    )
    pages = sorted({int(b.get("page") or 0) for b in plan})
    with pymupdf.open(str(BOOK)) as doc:
        sizes = {p: [doc[p].rect.width, doc[p].rect.height] for p in pages}
    bg = _background(sizes)

    pdf_bytes, stats = render_plan_to_pdf(plan, page_sizes=sizes, source_pdf=str(BOOK))
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    print(
        "renderer metrics:",
        {
            k: v
            for k, v in stats.items()
            if k
            in (
                "fit_shrunk",
                "fit_shrunk_deep",
                "fit_shrunk_best_effort",
                "box_expanded",
                "wrap_overflow_preserved",
                "wrap_clipped_page",
            )
        },
    )
    print()

    # A) 计划里相邻块的 dst_box 本身是否重叠
    print("=" * 92)
    print("A) do neighbouring dst_boxes overlap in the PLAN itself?")
    print("=" * 92)
    by_page: dict[int, list[dict]] = {}
    for b in plan:
        by_page.setdefault(int(b.get("page") or 0), []).append(b)
    plan_overlap_pages = 0
    for pno, items in by_page.items():
        items = sorted(
            items, key=lambda b: float((b.get("dst_box") or [0, 0, 0, 0])[1])
        )
        hit = 0
        for a, b in zip(items, items[1:]):
            ab, bb = a.get("dst_box") or [], b.get("dst_box") or []
            if len(ab) == 4 and len(bb) == 4:
                if ab[1] < bb[3] - 1 and bb[1] < ab[3] - 1:
                    hit += 1
        if hit:
            plan_overlap_pages += 1
            if not wanted or pno in wanted:
                print(f"  page {pno}: {hit} neighbouring dst_box pairs overlap")
    print(f"  -> pages whose plan already has overlapping boxes: {plan_overlap_pages}")
    print()

    # C) 实际落笔是否越出 dst_box
    print("=" * 92)
    print("C) does drawn text escape its own dst_box?")
    print("=" * 92)
    escapes = []
    for pno in range(doc.page_count):
        background = bg.get(pno) or set()
        spans = []
        for blk in doc[pno].get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if (sp.get("text") or "").strip() and _span_key(
                        sp
                    ) not in background:
                        spans.append(sp)
        if not spans:
            continue
        items = by_page.get(pno, [])
        for b in items:
            db = b.get("dst_box") or []
            if len(db) != 4:
                continue
            x0, y0, x1, y1 = [float(v) for v in db]
            # 该块的文本若出现在 span 里，就用这些 span 的范围
            key = str(b.get("translated") or b.get("text") or "")[:8]
            if not key:
                continue
            mine = [
                s
                for s in spans
                if key[:6] in "".join(x["text"] for x in spans)[:0]
                or key[:6] in "".join(spans_text(spans))
            ]
            if len(mine) < 2:
                continue
            top = min(s["bbox"][1] for s in mine)
            bot = max(s["bbox"][3] for s in mine)
            if bot > y1 + 1.5 or top < y0 - 1.5:
                escapes.append(
                    (
                        pno,
                        b.get("block_id"),
                        y0,
                        y1,
                        top,
                        bot,
                        str(b.get("render_path")),
                    )
                )
    print(f"  blocks whose drawn text escapes dst_box: {len(escapes)}")
    for row in escapes[:15]:
        print(
            f"    page {row[0]:>3} {str(row[1]):<9} box=[{row[2]:.1f},{row[3]:.1f}] "
            f"drawn=[{row[4]:.1f},{row[5]:.1f}] overflow_bottom={row[5] - row[3]:+.1f} "
            f"path={row[6]}"
        )
    print()
    print("=" * 92)
    print("VERDICT")
    print("=" * 92)
    if plan_overlap_pages and not escapes:
        print("  -> A: the boxes already overlap in the plan (parse-side).")
    elif escapes:
        print(f"  -> C: {len(escapes)} block(s) draw outside their own box.")
    else:
        print("  -> B: boxes do not overlap and nothing escapes; it is line spacing.")
    print("=" * 92)
    doc.close()
    return 0


def spans_text(spans) -> str:
    return "".join(s["text"] for s in spans)


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(
        main(args[0] if args else "doc/7p8-thread4-nocache", [int(a) for a in args[1:]])
    )
