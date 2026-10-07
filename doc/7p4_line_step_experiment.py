"""决定性实验：把 flow 块的 ``line_step`` 换算成源的行距，叠印会不会消失。

上一版实验（渲染期改参数）**无效**，因为 flow 的 y 在布局阶段就写进了
``render_payload.commands``。这一版改在真正的杠杆上：

    document_model.py:273  build_block_flow_payload(block)
      -> flow_sidechannel.py:125  line_step = -(size * line_height)
                                 DEFAULT_LINE_HEIGHT = 1.4

``line_step`` 是**绝对 pt**，随块字号定版。把它按 ``目标行距 / 1.4`` 缩放，就等于把
每行的纵向步长从 1.4× 换成目标倍数，且完全复用真实管线的其余部分。

实测依据（mp2e page 15）：
- 源 PDF 真实行距 median 12.1pt / 字 9.96pt → **1.21×**
- 渲染器落笔行距 median 13.2pt / 字 9.46pt → **1.40×**

跑法::

    python doc/7p4_line_step_experiment.py doc/7p8-thread4-nocache
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

CURRENT = 1.4
SOURCE = 1.21


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


def _rescale(plan: list[dict], ratio: float) -> tuple[list[dict], int]:
    """把 flow 块的逐行 y 偏移按 ``ratio`` 缩放，返回 (新计划, 改动块数)。

    flow 路径的每一行 y 是**布局阶段算好的绝对值**（实测 p4_3：delta -11.16pt /
    字号 7.97pt = **1.400**，正是硬编码的 DEFAULT_LINE_HEIGHT）。计划里**没有**
    ``line_step`` 字段 —— 我第一版去找它、找不到、于是 rescale 成了空操作，
    却把 delta +0 当成了否证（同一个坑踩了第三次）。

    正确做法：以**首行 y 为锚点**，只缩放后续行的偏移，首行不动。
    """
    out = []
    touched = 0
    for b in plan:
        c = dict(b)
        pl = dict(c.get("render_payload") or {})
        cmds = [dict(x) for x in (pl.get("commands") or [])]
        ys = [x.get("y") for x in cmds]
        if len(cmds) >= 2 and all(isinstance(y, (int, float)) for y in ys):
            anchor = ys[0]
            moved = False
            for x in cmds:
                x["y"] = anchor + (x["y"] - anchor) * ratio
                moved = True
            if moved:
                pl["commands"] = cmds
                touched += 1
        c["render_payload"] = pl
        out.append(c)
    return out, touched


def _run(plan, sizes, bg, tag, lh, touched: int = 0) -> dict:
    pdf, stats = R.render_plan_to_pdf(plan, page_sizes=sizes, source_pdf=str(BOOK))
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    geo = R.audit_page_geometry(doc, ignore=bg)
    doc.close()
    r = {
        "tag": tag,
        "line_height": lh,
        "blocks_touched": touched,
        "overlap_pages": geo["pages_with_overlap"],
        "outside": geo["spans_outside_page"],
        "fit_shrunk": stats.get("fit_shrunk", 0),
        "deep": stats.get("fit_shrunk_deep", 0),
        "best_effort": stats.get("fit_shrunk_best_effort", 0),
        "expanded": stats.get("box_expanded", 0),
        "overflow_preserved": stats.get("wrap_overflow_preserved", 0),
        "clipped": stats.get("wrap_clipped_page", 0),
    }
    print(
        f"  {tag:<26} {touched:>8} ovl={r['overlap_pages']:>3}  outside={r['outside']:>2}  "
        f"shrunk={r['fit_shrunk']:>3} deep={r['deep']:>2} best={r['best_effort']:>2} "
        f"expand={r['expanded']:>2} clip={r['clipped']:>2}"
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

    multi = [
        b
        for b in plan
        if (b.get("render_payload") or {}).get("kind") == "flow"
        and len((b["render_payload"].get("commands") or [])) >= 2
    ]
    print(f"plan      : {d}  ({len(plan)} blocks, {len(pages)} pages)")
    print(f"multi-line flow blocks (the ones that can drift): {len(multi)}")
    print(f"current   : {CURRENT}x   source (measured): {SOURCE}x")
    print()
    print(f"{'variant':<26} {'touched':>8} {'ovl':>5}  rest")
    print("-" * 84)

    rows = []
    rows.append(_run(plan, sizes, bg, "baseline 1.40x", CURRENT, 0))
    for lh in (1.30, 1.21, 1.10):
        scaled, touched = _rescale(plan, lh / CURRENT)
        rows.append(_run(scaled, sizes, bg, f"rescaled to {lh:.2f}x", lh, touched))
        if touched == 0:
            print("     !! rescale touched 0 blocks -> this variant proves NOTHING")

    base = rows[0]["overlap_pages"]
    src = next(r for r in rows if abs(r["line_height"] - SOURCE) < 1e-9)
    bites = src.get("blocks_touched", 0) > 0
    print(f"  rescale actually touched blocks? {bites}")
    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    print(f"  baseline {CURRENT}x        : {base} overlap pages")
    print(f"  rescaled {SOURCE}x (source): {src['overlap_pages']} overlap pages")
    print(f"  delta                    : {src['overlap_pages'] - base:+d}")
    if not bites:
        print("  -> INCONCLUSIVE：缩放没改到任何块，本实验什么也没测到。")
    elif src["overlap_pages"] < base:
        print("  -> 假设成立：行距 1.4 > 源的 1.21 是正文叠印的主因。")
        print("     修复点集中在一处：`v3/flow_sidechannel.DEFAULT_LINE_HEIGHT = 1.4`")
        print("     （flow 块的 line_step 由它算出），外加 wrapped 路径的 1.4 与")
        print("     `translation/layout_request.py` 的 1.2 —— 三处目前互不自洽。")
    else:
        print("  -> 假设不成立：降行距并不能减少叠印，正文叠印另有原因。")
    print("=" * 78)

    (d / "line-step-experiment.json").write_text(
        json.dumps({"current": CURRENT, "source": SOURCE, "rows": rows}, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {d / 'line-step-experiment.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p8-thread4-nocache")
    )
