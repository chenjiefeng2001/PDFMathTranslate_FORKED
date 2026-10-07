"""P3：把「叠印 11 → 17」归因到字号。

单变量对照：同一份 render plan、同一份译文、同一份背景，只换字号，离线重渲两次。
翻译与网络完全不参与。

为什么不能直接拿产物的 mono PDF 复算
------------------------------------
``audit_page_geometry(ignore=...)`` 要求的是「贴入背景**之后**、绘制译文**之前**」的
span 集合。产物 PDF 已经合成完毕，此时再取 :func:`page_span_snapshot` 会把**刚画好的
译文**也收进忽略集 —— 实测 16 页叠印被算成 **0**（情形 A），而完全不忽略则是 **41**
（情形 B，把「原文 + 覆盖其上的译文」全算成叠印）。两个数都是错的，只是错的方向相反。

所以这里**在渲染过程中**取背景集合，与 ``render_plan_to_pdf`` 内部的做法一致。

跑法::

    python doc/7p3_overlap_attribution.py doc/7p8-thread4-nocache
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

from pdf2zh.v3.magicpdf_renderer import (
    audit_page_geometry,
    render_plan_to_pdf,
)  # noqa: E402

BOOK = ROOT / "tests" / "file" / "The Art of Multiprocessor Programming, 2e.pdf"

#: 校准前后的实测众数字号（doc/7p5_audit_report.md §2）。
OLD_MODAL = 7.65
NEW_MODAL = 9.96


def _scaled(plan: list[dict], ratio: float) -> list[dict]:
    out = []
    for b in plan:
        c = dict(b)
        fs = c.get("font_size")
        if fs:
            try:
                c["font_size"] = round(float(fs) * ratio, 2)
            except (TypeError, ValueError):
                pass
        out.append(c)
    return out


def _render_with_background(sizes: dict):
    """渲染一份"只有背景"的文档，返回各页背景 span 集合。

    复刻 ``render_plan_to_pdf`` 内部 ``show_pdf_page`` 之后、落笔之前的那一步。

    必须与真实重渲**用同一个 ``source_pdf``**：漏掉它时公式/代码块不会走
    ``preserve_float``，而是把 LaTeX/原文当纯文本画出来 —— 叠印数因此虚高
    （实测 16 页虚报成 23~26 页）。这个坑我踩过一次，结论已作废。
    """
    doc = pymupdf.open(str(BOOK))
    out = pymupdf.open()
    bg = {}
    from pdf2zh.v3.magicpdf_renderer import page_span_snapshot

    for pno in sorted(sizes):
        w, h = sizes[pno]
        pg = out.new_page(width=w, height=h)
        if pno < doc.page_count:
            pg.show_pdf_page(pg.rect, doc, pno)
        bg[pno] = page_span_snapshot(pg)
    out.close()
    doc.close()
    return bg


def _run(plan: list[dict], sizes: dict, bg: dict, tag: str) -> dict:
    pdf_bytes, stats = render_plan_to_pdf(plan, page_sizes=sizes, source_pdf=str(BOOK))
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    geo = audit_page_geometry(doc, ignore=bg)
    result = {
        "tag": tag,
        "pages_with_overlap": geo["pages_with_overlap"],
        "spans_outside_page": geo["spans_outside_page"],
        "overlap_examples": geo["overlap_examples"][:10],
        "renderer_overlap": stats.get("pages_with_overlap"),
        "renderer_outside": stats.get("spans_outside_page"),
        "fit_shrunk": stats.get("fit_shrunk", 0),
        "fit_shrunk_deep": stats.get("fit_shrunk_deep", 0),
        "box_expanded": stats.get("box_expanded", 0),
        "wrap_overflow_preserved": stats.get("wrap_overflow_preserved", 0),
        "wrap_clipped_page": stats.get("wrap_clipped_page", 0),
    }
    doc.close()
    print(
        f"  [{tag}] overlap pages={result['pages_with_overlap']}  "
        f"outside spans={result['spans_outside_page']}  "
        f"fit_shrunk={result['fit_shrunk']}  deep={result['fit_shrunk_deep']}  "
        f"expanded={result['box_expanded']}"
    )
    return result


def main(out_dir: str) -> int:
    d = Path(out_dir)
    plan = json.loads(
        next(d.glob("output/**/*_render_plan.json")).read_text(encoding="utf-8")
    )
    pages = sorted({int(b.get("page") or 0) for b in plan})
    with pymupdf.open(str(BOOK)) as doc:
        sizes = {p: [doc[p].rect.width, doc[p].rect.height] for p in pages}

    ratio = OLD_MODAL / NEW_MODAL
    dist = Counter(round(float(b.get("font_size") or 0), 2) for b in plan)
    print(f"plan      : {d}  ({len(plan)} blocks, {len(pages)} pages)")
    print(f"old modal : {OLD_MODAL}pt    new modal: {NEW_MODAL}pt    ratio {ratio:.4f}")
    print(f"new dist  : {dist.most_common(5)}")
    print(
        f"old dist  : " f"{[(round(k * ratio, 2), v) for k, v in dist.most_common(5)]}"
    )

    bg = _render_with_background(sizes)
    print(f"background spans per page: {[len(bg[p]) for p in pages[:6]]} ...")

    print("\n" + "=" * 78)
    print("SINGLE-VARIABLE A/B — identical plan, identical translations, size only")
    print("=" * 78)
    old = _run(_scaled(plan, ratio), sizes, bg, f"old x{ratio:.4f} (7.65pt)")
    new = _run(plan, sizes, bg, "new (9.96pt)")

    print()
    print("=" * 78)
    delta = new["pages_with_overlap"] - old["pages_with_overlap"]
    print(
        f"overlap pages: old {old['pages_with_overlap']} -> new "
        f"{new['pages_with_overlap']}  (delta {delta:+d})"
    )
    print(
        f"renderer agrees: old {old['renderer_overlap']}  new {new['renderer_overlap']}"
    )
    if abs(delta) <= 1:
        print("VERDICT: font size is NOT the cause of the 11 -> 17 growth.")
        print("         The growth must come from the other changes (P0 reparse, P2).")
    else:
        print("VERDICT: font size IS a contributor.")
        for ex in new["overlap_examples"][:8]:
            print(f"     page {ex.get('page')}: {ex.get('overlaps')} overlaps")
    print("=" * 78)

    (d / "overlap-attribution.json").write_text(
        json.dumps(
            {"ratio": ratio, "old": old, "new": new}, indent=1, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    print(f"wrote {d / 'overlap-attribution.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p8-thread4-nocache")
    )
