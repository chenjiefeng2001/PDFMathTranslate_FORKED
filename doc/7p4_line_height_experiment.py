"""离线验证：正文叠印的根因是不是「行距 1.4 > 源的 1.21」？

已测得的事实（``doc/7p4_line_pitch.py``，mp2e page 15）：

- 源 PDF 真实行距 median **12.1pt**（9.96pt 字 → 约 **1.21x**）；
- 渲染器按 ``font_size * 1.4`` 落笔，实测 median **13.2pt**（1.40x）。

也就是说每行比源多用 9~15% 纵向空间，段落越往下漂得越远，最终压进邻块。这正好解释
「字号校准之后叠印从 11 页涨到 16 页」：旧字号 7.65 x 1.4 = 10.7pt < 源的 12.1pt，
反而比源更松；新字号 9.96 x 1.4 = 13.9pt > 12.1pt，开始漂。

本脚本**不改任何产品代码**，只在进程内把行距系数换掉再重渲一次，看叠印页数怎么动。
这是验证假设，不是交付实现 —— 结果会直接决定下一步该改哪几处。

跑法::

    python doc/7p4_line_height_experiment.py doc/7p8-thread4-nocache
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

from pdf2zh.semantic.renderer import flow as flow_mod  # noqa: E402
from pdf2zh.v3 import magicpdf_renderer as R  # noqa: E402

BOOK = ROOT / "tests" / "file" / "The Art of Multiprocessor Programming, 2e.pdf"

#: 源的实测行距（page 15，9.96pt 字，median 12.1pt）。
SOURCE_RATIO = 1.21


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


def render_with(plan, sizes, bg, line_height: float) -> dict:
    """把行距系数换成 ``line_height`` 后重渲，返回叠印统计。"""
    orig_init = flow_mod.FlowTextRenderer.__init__
    orig_render = flow_mod.render_flow_text

    def patched_init(self, lh: float = line_height):  # noqa: N803
        orig_init(self, lh)

    def patched_render(*args, **kwargs):
        kwargs.setdefault("line_height", line_height)
        return orig_render(*args, **kwargs)

    flow_mod.FlowTextRenderer.__init__ = patched_init
    flow_mod.render_flow_text = patched_render
    try:
        pdf_bytes, stats = R.render_plan_to_pdf(
            plan, page_sizes=sizes, source_pdf=str(BOOK)
        )
    finally:
        flow_mod.FlowTextRenderer.__init__ = orig_init
        flow_mod.render_flow_text = orig_render

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    geo = R.audit_page_geometry(doc, ignore=bg)
    doc.close()
    return {
        "line_height": line_height,
        "overlap_pages": geo["pages_with_overlap"],
        "outside": geo["spans_outside_page"],
        "fit_shrunk": stats.get("fit_shrunk", 0),
        "deep": stats.get("fit_shrunk_deep", 0),
        "expanded": stats.get("box_expanded", 0),
        "clipped": stats.get("wrap_clipped_page", 0),
    }


def _patch_bites(plan, sizes, pno: int = 15) -> bool:
    """自检：把行距改成两个不同的值，落笔行距是否真的变了？

    没变就说明补丁是空操作，本实验的 delta 毫无意义。
    """
    pitches = []
    for lh in (1.40, 1.05):
        orig = flow_mod.FlowTextRenderer.__init__
        orig_r = flow_mod.render_flow_text

        def pi(self, x: float = lh):  # noqa: N803
            orig(self, x)

        def pr(*a, **k):
            k.setdefault("line_height", lh)
            return orig_r(*a, **k)

        flow_mod.FlowTextRenderer.__init__ = pi
        flow_mod.render_flow_text = pr
        try:
            pdf, _ = R.render_plan_to_pdf(plan, page_sizes=sizes, source_pdf=str(BOOK))
        finally:
            flow_mod.FlowTextRenderer.__init__ = orig
            flow_mod.render_flow_text = orig_r
        d = pymupdf.open(stream=pdf, filetype="pdf")
        spans = []
        for blk in d[pno].get_text("dict")["blocks"]:
            if blk.get("type") != 0:
                continue
            for line in blk["lines"]:
                for sp in line["spans"]:
                    if (sp.get("text") or "").strip():
                        spans.append(sp)
        spans.sort(key=lambda s: s["bbox"][1])
        gaps = [
            round(b["bbox"][1] - a["bbox"][1], 2)
            for a, b in zip(spans, spans[1:])
            if 0 < b["bbox"][1] - a["bbox"][1] < 40
        ]
        pitches.append(sorted(gaps)[len(gaps) // 2] if gaps else 0.0)
        d.close()
    return abs(pitches[0] - pitches[1]) > 0.5


def main(out_dir: str) -> int:
    d = Path(out_dir)
    plan = json.loads(
        next(d.glob("output/**/*_render_plan.json")).read_text(encoding="utf-8")
    )
    pages = sorted({int(b.get("page") or 0) for b in plan})
    with pymupdf.open(str(BOOK)) as doc:
        sizes = {p: [doc[p].rect.width, doc[p].rect.height] for p in pages}
    bg = _background(sizes)

    print(f"source line ratio (measured, page 15): {SOURCE_RATIO}")
    print("renderer default                     : 1.40")
    print()
    print(
        f"{'line_height':>11} {'ovl pages':>10} {'outside':>8} "
        f"{'shrunk':>7} {'deep':>5} {'expand':>7} {'clipped':>8}"
    )
    print("-" * 62)
    rows = []
    for lh in (1.40, 1.30, 1.21, 1.10):
        r = render_with(plan, sizes, bg, lh)
        rows.append(r)
        print(
            f"{r['line_height']:>11.2f} {r['overlap_pages']:>10} "
            f"{r['outside']:>8} {r['fit_shrunk']:>7} {r['deep']:>5} "
            f"{r['expanded']:>7} {r['clipped']:>8}"
        )

    base = rows[0]["overlap_pages"]
    src = next(r for r in rows if abs(r["line_height"] - SOURCE_RATIO) < 1e-9)
    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    print(f"  line_height 1.40 (current)  : {base} overlap pages")
    print(
        f"  line_height {SOURCE_RATIO} (source) : {src['overlap_pages']} overlap pages"
    )
    print(f"  delta                        : {src['overlap_pages'] - base:+d}")
    # **先证明这个实验有效**，再解读结果。
    #
    #: 我第一版就是在这里翻的车：delta 恰好 +0，我据此写了「假设不成立」。但那个 0 是
    #: 因为**补丁根本没生效** —— flow 路径每一行的 y 由布局阶段算好后写进
    #: ``render_payload.commands``，渲染期只是照着画。实测把行距从 1.40 改成 1.21 /
    #: 1.05，落笔行距恒为 13.2pt（比值 1.395），纹丝不动。
    #:
    #: 所以下面必须先量「落笔行距是否真的变了」。没变就**不能**下任何结论。
    bites = _patch_bites(plan, sizes)
    print(f"  patch actually changes the drawn pitch? {bites}")
    if not bites:
        print()
        print(
            "  -> INCONCLUSIVE。补丁是空操作：行距在布局阶段就已固化，本实验什么也没测到。"
        )
        print("     要真正验证这个假设，必须从**布局阶段**重跑（parse -> layout），")
        print("     而不是改渲染期参数。")
    elif src["overlap_pages"] < base:
        print("  -> 假设成立：行距大于源的行距是正文叠印的主因。")
        print(
            "     修复面：`semantic/renderer/flow.py`(1.4 默认) + `v3/magicpdf_renderer`"
        )
        print("     的 `_stack_height` / `_insert_text_wrapped`（同样是 1.4），以及")
        print("     `translation/layout_request.py`(1.2) 与之不一致的问题。")
    else:
        print("  -> 假设**不成立**：降行距并不能减少叠印，正文叠印另有原因。")
    print("=" * 78)

    (d / "line-height-experiment.json").write_text(
        json.dumps(
            {"source_ratio": SOURCE_RATIO, "rows": rows, "patch_bites": bites},
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"wrote {d / 'line-height-experiment.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p8-thread4-nocache")
    )


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p8-thread4-nocache")
    )
