"""把两处丢字钉死，并把 46 条"尾部缺失"分清真伪。

关键区分：TOC 块的译文是按 ``toc_commands``（点引线 + 页码）渲染的，归一化后的文本
形态与 ``translated`` 本来就不一样，不能算丢字。而 flow 块的 ``translated`` 必须在
PDF 里找得到。

跑法::

    python doc/7p5_loss_detail.py doc/7p5-audit50
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

NONSPACE = re.compile(r"\S")


def norm(s: str) -> str:
    return "".join(NONSPACE.findall(s))


def main(out_dir: str) -> int:
    d = Path(out_dir)
    plan = json.loads(
        next(d.glob("output/**/*_render_plan.json")).read_text(encoding="utf-8")
    )
    pdf_f = next(d.glob("output/**/*_mono.pdf"))
    by_id = {b["block_id"]: b for b in plan}

    # ---------------------------------------------------------- 1. 两条丢字
    print("=" * 78)
    print("1. THE TWO DROPPED-LINE WARNINGS, IN FULL")
    print("=" * 78)
    for bid in ("p25_1", "p36_8"):
        b = by_id.get(bid)
        if not b:
            print(f"{bid}: not found")
            continue
        pl = b.get("render_payload") or {}
        print(
            f"\n--- {bid}  page={b['page']}  kind={b['kind']}  "
            f"font_size={b['font_size']}  fixup={b['render_fixup']}  "
            f"path={b['render_path']}"
        )
        print(f"  src_box  : {b['src_box']}")
        print(f"  dst_box  : {b['dst_box']}")
        print(f"  translated: {b['translated']!r}")
        print(
            f"  payload.kind={pl.get('kind')} overflow={pl.get('overflow')} "
            f"layout_ok={pl.get('layout_ok')} policy={pl.get('policy')}"
        )
        print(f"  payload.font_size={pl.get('font_size')} bbox={pl.get('bbox')}")
        print(f"  recovery : {pl.get('recovery')}")
        print(f"  lines ({len(pl.get('lines') or [])}) : {pl.get('lines')}")
        for c in pl.get("commands") or []:
            print(f"  cmd      : {str(c)[:150]}")
        print(f"  trace    : {str(pl.get('trace'))[:300]}")

    # ---------------------------------------------------------- 2. 全 PDF 搜索
    print("\n" + "=" * 78)
    print("2. SEARCH THE WHOLE RENDERED PDF FOR THOSE TWO TRANSLATIONS")
    print("=" * 78)
    doc = pymupdf.open(str(pdf_f))
    all_text = {i: norm(doc[i].get_text()) for i in range(doc.page_count)}
    for bid, probe in (("p25_1", "1.2一个寓言"), ("p36_8", "关于N，所使用的核心数量")):
        want = norm(str(by_id[bid].get("translated") or ""))
        pno = int(by_id[bid]["page"])
        found = [i for i, t in all_text.items() if probe in t]
        whole = [i for i, t in all_text.items() if want and want in t]
        print(f"\n{bid}: page={pno}  translated={want[:60]!r}")
        print(f"  probe {probe!r} found on pages: {found}")
        print(f"  full translation found on pages: {whole}")

    # ---------------------------------------------------------- 3. 尾部缺失分类
    print("\n" + "=" * 78)
    print("3. CLASSIFYING THE 'TAIL ABSENT' BLOCKS")
    print("=" * 78)
    by_kind: Counter = Counter()
    toc_like = []
    real = []
    for b in plan:
        want = norm(str(b.get("translated") or ""))
        if len(want) < 8:
            continue
        if want[-8:] in all_text.get(int(b["page"]), ""):
            continue
        pl = b.get("render_payload") or {}
        is_toc = (
            bool(b.get("toc_commands") or b.get("toc_entries"))
            or b.get("kind") == "toc"
        )
        by_kind[("toc" if is_toc else "flow", b.get("render_path"))] += 1
        (toc_like if is_toc else real).append(b)
    print(
        f"  toc-like  : {len(toc_like)}  (rendered via toc_commands; text form differs by design)"
    )
    print(f"  flow-like : {len(real)}")
    print(f"  breakdown : {dict(by_kind)}")
    if real:
        print("\n  flow blocks whose tail is genuinely absent:")
        for b in real[:20]:
            pl = b.get("render_payload") or {}
            print(
                f"    page {b['page']:>3} {b['block_id']:<10} kind={b['kind']:<10} "
                f"path={b['render_path']:<16} fixup={b['render_fixup']:<11} "
                f"overflow={pl.get('overflow')} lines={len(pl.get('lines') or [])}"
            )
            print(f"      translated={str(b.get('translated'))[:70]!r}")

    # 渲染路径分布
    print("\n" + "=" * 78)
    print("4. RENDER PATH / FIXUP DISTRIBUTION")
    print("=" * 78)
    print(f"  render_path : {dict(Counter(str(b.get('render_path')) for b in plan))}")
    print(f"  render_fixup: {dict(Counter(str(b.get('render_fixup')) for b in plan))}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p5-audit50"))
