"""正文叠印到底是什么？把重叠的 span 对原样打出来。

P3 已把叠印归因收窄：字号只贡献 +2 页，`toc` 的 100% 是结构性的，剩下真正没归因的是
``paragraph`` 54% / ``heading`` 60% 那一档。这里不改任何代码，只把**每一对重叠的
span** 连同文本、bbox、字体一起打出来，好判断它是真实重影（读者看到鬼影）还是良性重叠。

必须排除背景 span —— 否则会把「原文 + 覆盖其上的译文」全算成叠印（实测同一份产物
16 页虚报成 41 页）。这里按 ``_span_key`` 过滤，与渲染器一致。

跑法::

    python doc/7p4_overlap_pairs.py doc/7p8-thread4-nocache [page ...]
"""

from __future__ import annotations

import io
import json
import sys
from collections import Counter
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

    plan_by_page: dict[int, list[dict]] = {}
    for b in plan:
        plan_by_page.setdefault(int(b.get("page") or 0), []).append(b)

    summary = Counter()
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
        pairs = []
        for i, a in enumerate(spans):
            for b in spans[i + 1 :]:
                ax0, ay0, ax1, ay1 = a["bbox"]
                bx0, by0, bx1, by1 = b["bbox"]
                if ax0 < bx1 - 1 and bx0 < ax1 - 1 and ay0 < by1 - 1 and by0 < ay1 - 1:
                    pairs.append(
                        (
                            a,
                            b,
                            (min(ax1, bx1) - max(ax0, bx0)),
                            (min(ay1, by1) - max(ay0, by0)),
                        )
                    )
        if not pairs:
            continue
        if wanted and pno not in wanted:
            continue
        print("=" * 90)
        print(f"PAGE {pno}  ({len(pairs)} overlapping pairs)")
        print("=" * 90)
        blocks_here = plan_by_page.get(pno, [])
        for a, b, ox, oy in pairs[:12]:
            print(
                f"  overlap area {ox:.1f}x{oy:.1f}pt\n"
                f"    A font={a.get('font')!r} size={round(a.get('size', 0), 2)} "
                f"bbox={[round(v, 1) for v in a['bbox']]}\n"
                f"      {a['text'][:72]!r}\n"
                f"    B font={b.get('font')!r} size={round(b.get('size', 0), 2)} "
                f"bbox={[round(v, 1) for v in b['bbox']]}\n"
                f"      {b['text'][:72]!r}"
            )
            summary[("same_font", a.get("font") == b.get("font"))] += 1
            summary[("both_cjk", _cjk(a["text"]) and _cjk(b["text"]))] += 1
        # 这页上的块都走什么 render 路径
        print(
            f"    render paths on this page: "
            f"{dict(Counter(str(x.get('render_path')) for x in blocks_here))}"
        )
        print()

    print("=" * 90)
    print("SUMMARY over printed pairs:")
    print(f"  same font        : {summary['same_font']}")
    print(f"  both CJK         : {summary['both_cjk']}")
    print("=" * 90)
    doc.close()
    return 0


def _cjk(t: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in t)


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(
        main(
            args[0] if args else "doc/7p8-thread4-nocache",
            [int(a) for a in args[1:]],
        )
    )
