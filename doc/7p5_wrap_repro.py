"""为什么 p25_1 没有缩到能装下？—— 实测，不靠推测。

日志说 ``needs 2 lines, only 1 fit in 15.0pt``。按 ``_WRAP_SHRINK_STEPS`` 最低 0.55
手算，7 个字符在 67pt 宽的框里、字号缩到 9.16pt 应该只需 1 行、``_stack_height`` 只有
7.79pt ≤ 15pt，理应缩下去。**但它没有。** 这个矛盾必须实测定位，否则修复方案就是猜的。

这里直接驱动真实的 ``_insert_text_wrapped``，逐个缩放档位打印真实行数/行宽/行高。

跑法::

    python doc/7p5_wrap_repro.py
"""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\14977\source\repos\PDFMathTranslate_FORKED")

import pymupdf  # noqa: E402

from pdf2zh.v3 import magicpdf_renderer as R  # noqa: E402

CASES = [
    # (label, text, rect(x0,y0,x1,y1), font_size, fontname)
    ("p25_1", "1.2 一个寓言", (82.0, 428.04, 149.0, 443.04), 16.65, "china-ss"),
    (
        "p36_8",
        "关于N，所使用的核心数量。依赖关系是\\begin{array} { r } "
        "{ C a c h e M i s s = \\frac { N ^ { \\bullet } } { N + 1 0 } } "
        "\\end{array}。性能分析",
        (119.0, 204.06, 456.0, 220.06),
        15.95,
        "china-ss",
    ),
]


class _Rect:
    def __init__(self, b):
        self.x0, self.y0, self.x1, self.y1 = b


def probe(label, text, box, fs, fontname):
    rect = _Rect(box)
    box_w = box[2] - box[0]
    box_h = box[3] - box[1]
    eff = R._resolve_effect_font(text, fontname)
    print("\n" + "=" * 78)
    print(
        f"{label}: {len(text)} chars, box {box_w:.1f}x{box_h:.1f}pt, font {fs}pt, "
        f"font={eff}"
    )
    print("=" * 78)
    width = R._advance_width(text, eff, fs)
    print(f"  whole-string width at {fs}pt : {width:.1f}pt  (box {box_w:.1f}pt)")
    print(f"  -> {'FITS on one line' if width <= box_w else 'WRAPS'}")

    print(
        f"\n  {'scale':>7} {'fontsize':>9} {'lines':>6} {'stack_h':>9} " f"{'fits?':>7}"
    )
    print("  " + "-" * 46)
    for scale in R._WRAP_SHRINK_STEPS:
        if scale >= 1.0:
            continue
        cand = fs * scale
        # 复用真实测量：逐行累加 advance
        # （内部 _layout 是闭包，这里用等价的 token 累加复现）
        lines, cur = [], ""
        for tok in R._wrap_tokens(text):
            if tok == " ":
                if cur and R._advance_width(cur + " " + "", eff, cand) <= box_w:
                    cur += " "
                else:
                    lines.append(cur)
                    cur = ""
                continue
            if cur and R._advance_width(cur + tok, eff, cand) > box_w:
                lines.append(cur)
                cur = tok
            else:
                cur += tok
        if cur:
            lines.append(cur)
        n = max(1, len(lines))
        stack = cand * 0.85 + (n - 1) * cand * 1.4
        ok = stack <= box_h
        print(
            f"  {scale:>7.2f} {cand:>9.2f} {len(lines):>6} {stack:>9.2f} "
            f"{'YES' if ok else 'no':>7}"
        )
        if ok:
            print(f"  -> ladder would stop here (scale {scale})")
            break
    else:
        print("  -> NO scale in the ladder fits -> lines get dropped")

    # 真实路径：真调一次，拿到 stats
    doc = pymupdf.open()
    pg = doc.new_page(width=539, height=665)
    stats: dict = {}
    R._insert_text_wrapped(pg, rect, text, fs, fontname, stats)
    print(f"\n  real _insert_text_wrapped stats: {stats}")
    drawn = pg.get_text().strip().replace("\n", " | ")
    print(f"  drawn text: {drawn[:90]!r}")
    doc.close()


for case in CASES:
    probe(*case)
