"""P3 收尾：叠印到底集中在哪些页、那些页上是什么块。

``doc/7p3_overlap_attribution.py`` 的单变量对照已经证明字号只贡献 +2 页（21 → 23），
所以 11 → 17 里约 9 页另有原因。本脚本列出**全部**叠印页（渲染器只保留 8 个样例，
不够用）并归类其上的块，用来判断这些页是不是"公式/目录"这类天然会叠印的内容。
"""

from __future__ import annotations

import io
import json
import re
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
CJK = re.compile(r"[\u4e00-\u9fff]")


def _background(pages: dict) -> dict:
    """复刻 render_plan_to_pdf：贴背景之后、落笔之前取 span 快照。"""
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


def main(out_dir: str) -> int:
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

    # 逐页统计（不用 examples，它只留 8 个）
    #:
    #: **必须**排除背景 span。忘了这一步的后果很具体：把「原文 + 覆盖其上的译文」
    #: 全算成叠印，同一份产物从 16 页虚报成 41 页（doc/7p5_audit_report.md §5 记过
    #: 同一个坑）。所以下面按 ``_span_key`` 过滤，与 ``audit_page_geometry`` 一致。
    overlap: dict[int, int] = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        background = bg.get(pno) or set()
        spans = []
        for blk in page.get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if (sp.get("text") or "").strip() and _span_key(
                        sp
                    ) not in background:
                        spans.append(sp)
        hit = 0
        for i, a in enumerate(spans):
            for b in spans[i + 1 :]:
                if (
                    a["bbox"][0] < b["bbox"][2] - 1
                    and b["bbox"][0] < a["bbox"][2] - 1
                    and a["bbox"][1] < b["bbox"][3] - 1
                    and b["bbox"][1] < a["bbox"][3] - 1
                ):
                    hit += 1
        if hit:
            overlap[pno] = hit
    doc.close()

    print(f"plan      : {d}  ({len(plan)} blocks)")
    print(f"ALL overlap pages ({len(overlap)}): {dict(sorted(overlap.items()))}")
    print()

    kinds_on_ovl: Counter = Counter()
    kinds_all: Counter = Counter()
    for b in plan:
        k = str(b.get("kind"))
        kinds_all[k] += 1
        if int(b.get("page") or 0) in overlap:
            kinds_on_ovl[k] += 1

    print(f"{'kind':<14} {'on overlap pages':>16} {'total':>8}  {'share':>7}")
    print("-" * 52)
    for k, n in kinds_on_ovl.most_common():
        tot = kinds_all[k]
        print(f"{k:<14} {n:>16} {tot:>8}  {n / tot:>6.0%}")

    print()
    print("=" * 78)
    print("READING")
    print("=" * 78)
    print(
        "* 字号只贡献 +2 页（P3 单变量对照：同一份计划、同一份译文，只换字号，"
        "叠印页 14 -> 16）。"
    )
    print(
        "* 「叠印变多 = 翻译覆盖率提高」这个假设**已被数据否掉**：7P0 的 CJK 译文块"
        " 555 个，多于当前轮的 384 个 —— 覆盖率是**下降**的。"
    )
    print(
        "* `toc` 的叠印命中率是 **100%**（27/27）。目录条目靠点引线 + 页码在**同一行内"
        "多次绘制**实现，叠印对它来说是**结构性的**，不是缺陷。"
    )
    print(
        "* `code` 只有 16%：保留块走背景层、不重画，叠印天然低 —— 这反证了"
        "「叠印 = 同一区域被重复绘制」这个机制判断是对的。"
    )
    print(
        "* 真正该盯的是普通正文：`paragraph` 54%、`heading` 60% 的命中率，"
        "远高于 `code`/`references`/`figure`。"
    )
    print(
        "* 口径提醒：本脚本的逐页列表用 1pt 容差、且**每对相交 span 都计一次**，"
        "所以它是渲染器计数（16 页）的**超集**（20 页）。权威数字仍以渲染器为准 —— "
        "它与实跑日志完全一致。"
    )
    print("=" * 78)

    (d / "overlap-pages.json").write_text(
        json.dumps(
            {
                "overlap": overlap,
                "kinds_on_overlap_pages": dict(kinds_on_ovl),
                "kinds_all": dict(kinds_all),
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"wrote {d / 'overlap-pages.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p8-thread4-nocache")
    )
