"""把一页的落笔 span 按 y 排开，和计划的 dst_box 逐行对照。

前面的探针用"文本前缀猜"来归属 span，会把整页 span 误算给每个块（330 个"越框"全是
假的）。这里不猜：直接把该页所有落笔 span 按 y 排序打印，人眼直接看行距与框的关系。

要回答的问题很具体：**行距是不是比源 PDF 的行距大？**
源 PDF 实测 9.96pt 正文的行框只有 10.0~10.1pt（即行距 ≈1.01 倍），而渲染器按
``font_size * 1.4`` 算行高（9.96 -> 13.9pt）。如果确实如此，多行段落的第二行就会落到
比源更靠下的位置，撞上后面的块 —— 这正好解释「字号变大后叠印增加」。

跑法::

    python doc/7p4_line_pitch.py doc/7p8-thread4-nocache 15
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


def main(out_dir: str, pno: int) -> int:
    d = Path(out_dir)
    plan = json.loads(
        next(d.glob("output/**/*_render_plan.json")).read_text(encoding="utf-8")
    )
    pages = sorted({int(b.get("page") or 0) for b in plan})
    with pymupdf.open(str(BOOK)) as doc:
        sizes = {p: [doc[p].rect.width, doc[p].rect.height] for p in pages}
    bg = _background(sizes)

    # 源 PDF 该页的行框（真实行距基准）
    print(f"=== SOURCE page {pno}: line boxes (real pitch) ===")
    with pymupdf.open(str(BOOK)) as doc:
        tops = []
        for blk in doc[pno].get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                bb = line["bbox"]
                sz = max(
                    (
                        round(float(sp.get("size") or 0), 2)
                        for sp in line.get("spans", [])
                    ),
                    default=0.0,
                )
                tops.append((round(bb[1], 1), round(bb[3], 1), sz))
        tops.sort()
        pitches = [
            round(tops[i + 1][0] - tops[i][0], 1)
            for i in range(len(tops) - 1)
            if 0 < tops[i + 1][0] - tops[i][0] < 40
        ]
        for t in tops[:12]:
            print(f"    top={t[0]:>6.1f} bottom={t[1]:>6.1f} size={t[2]:>6.2f}")
        if pitches:
            print(
                f"    real line pitch: min={min(pitches)} median="
                f"{sorted(pitches)[len(pitches) // 2]} max={max(pitches)}"
            )
    print()

    pdf_bytes, stats = render_plan_to_pdf(plan, page_sizes=sizes, source_pdf=str(BOOK))
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")

    print(f"=== DRAWN spans on output page {pno}, sorted by y ===")
    background = bg.get(pno) or set()
    spans = []
    for blk in doc[pno].get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                if (sp.get("text") or "").strip() and _span_key(sp) not in background:
                    spans.append(sp)
    spans.sort(key=lambda s: s["bbox"][1])

    drawn_pitch = []
    for a, b in zip(spans, spans[1:]):
        d = round(b["bbox"][1] - a["bbox"][1], 1)
        if 0 < d < 40:
            drawn_pitch.append(d)
    for s in spans[:26]:
        x0, y0, x1, y1 = [round(v, 1) for v in s["bbox"]]
        print(
            f"    y=[{y0:>6.1f},{y1:>6.1f}] x=[{x0:>6.1f},{x1:>6.1f}] "
            f"size={round(s.get('size', 0), 2):>6.2f} {s['text'][:40]!r}"
        )
    if drawn_pitch:
        print(
            f"    drawn line pitch: min={min(drawn_pitch)} "
            f"median={sorted(drawn_pitch)[len(drawn_pitch) // 2]} "
            f"max={max(drawn_pitch)}"
        )
    print()
    print(f"    renderer line-height rule = font_size * 1.4")
    doc.close()
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(
        main(
            args[0] if args else "doc/7p8-thread4-nocache",
            int(args[1]) if len(args) > 1 else 15,
        )
    )
