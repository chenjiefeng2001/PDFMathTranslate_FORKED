"""真实 mp2e 页面上的字号校准端到端验证（解析 → bridge → 页面模型）。

关注两件事，都必须在真实数据上成立：

1. **保真**：正文块的 ``font_size`` 回到源 PDF 的 9.96pt，而不是旧的 7.65pt。
2. **不溢出**：字号变大后没有把块挤出原来的框 —— 逐块比较校准前后的
   ``bbox`` 与字号，并统计真正被布局层缩过的块。

跑法::

    python doc/7p2_font_calibration_probe.py [first_page] [n_pages]
"""

import io
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pymupdf  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pdf2zh.magicpdf_adapter import (  # noqa: E402
    MagicPdfAdapter,
    MagicPdfParseResult,
    _annotate_size_scale,
)
from pdf2zh.v3.canonical_page import annotate_style  # noqa: E402
from pdf2zh.v3.magicpdf_bridge import DEFAULT_SIZE_SCALE, MagicPdfBridge  # noqa: E402

SRC = Path(
    r"C:\Users\14977\source\repos\PDFMathTranslate_FORKED\tests\file"
    r"\The Art of Multiprocessor Programming, 2e.pdf"
)

first = int(sys.argv[1]) if len(sys.argv) > 1 else 14
count = int(sys.argv[2]) if len(sys.argv) > 2 else 12


def source_modal(pno: int) -> tuple[float, dict]:
    sizes: Counter = Counter()
    with pymupdf.open(str(SRC)) as doc:
        for blk in doc[pno].get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if len((sp.get("text") or "").strip()) >= 8:
                        sizes[round(float(sp.get("size") or 0), 2)] += 1
    sizes.pop(0.0, None)
    if not sizes:
        return 0.0, {}
    return sizes.most_common(1)[0][0], dict(sizes.most_common(3))


print(f"source      : {SRC.name}")
print(f"pages       : 0-based {first}..{first + count - 1}")
print(f"default coef: {DEFAULT_SIZE_SCALE}\n")

adapter = MagicPdfAdapter()
results = adapter.parse(str(SRC), pages=list(range(first, first + count)), ocr=False)

calibrated = _annotate_size_scale(results, str(SRC))
print(f"calibrated  : {calibrated}/{len(results)} page(s)\n")

bridge = MagicPdfBridge()
before_fs: Counter = Counter()
after_fs: Counter = Counter()
scales: Counter = Counter()
src_truth: list[tuple[int, float]] = []
per_page_before: dict[int, Counter] = {}
per_page_after: dict[int, Counter] = {}
grew: list[tuple[int, float, float]] = []
mismatch: list[str] = []

for res in results:
    pno = res.page_num
    truth, top = source_modal(pno)
    src_truth.append((pno, truth))

    cal = res.raw.get("size_map")
    if cal:
        scales[cal.fallback_scale] += 1

    # 基线必须**剥掉** per-page 系数：`MagicPdfBridge.convert()` 优先读
    # ``raw["size_scale"]``，不剥的话"校准前"也会用上校准值，两边就一样了。
    raw_copy = MagicPdfParseResult(
        page_num=res.page_num,
        width=res.width,
        height=res.height,
        raw={k: v for k, v in res.raw.items() if k not in ("size_scale", "size_map")},
        blocks=res.blocks,
        backend=res.backend,
    )
    raw_snapshot = MagicPdfBridge(size_scale=DEFAULT_SIZE_SCALE).convert(raw_copy)
    annotate_style(raw_snapshot)
    cal_snapshot = bridge.convert(res)
    annotate_style(cal_snapshot)

    for blk_o, blk_n in zip(raw_snapshot.blocks, cal_snapshot.blocks):
        fs_o = blk_o.font_size
        fs_n = blk_n.font_size
        if not fs_o or not fs_n:
            continue
        before_fs[round(fs_o, 2)] += 1
        after_fs[round(fs_n, 2)] += 1
        per_page_before.setdefault(pno, Counter())[round(fs_o, 2)] += 1
        per_page_after.setdefault(pno, Counter())[round(fs_n, 2)] += 1
        if fs_n > fs_o + 0.01:
            grew.append((pno, fs_o, fs_n))
        # 框不能被字号撑破：字号不该超过该块行框高的 1.4 倍（源 PDF 自身比值 1.107）
        line_h = max(
            (float(l.y1) - float(l.y0) for l in blk_n.lines),
            default=0.0,
        )
        if line_h > 0 and fs_n > line_h * 1.4:
            mismatch.append(f"page {pno}: font {fs_n} > line box {line_h:.2f} * 1.4")

print("=" * 78)
print("source truth  (modal glyph size per page)")
for pno, truth in src_truth:
    print(f"  page {pno:>3} (PDF {pno + 1:>3}): {truth}")
print("=" * 78)
print(f"calibrated fallback scale: {dict(scales)}")
print()
print(f"font_size BEFORE (0.85 guess) : {before_fs.most_common(6)}")
print(f"font_size AFTER  (calibrated)  : {after_fs.most_common(6)}")
print()

# 用**每页众数**而不是全页平均：一页里既有 9.96pt 正文也有 16.6pt 标题，平均值
# 谁都不代表，拿它去比源页众数会得出荒谬的结论。
print(
    f"{'page':>5} {'src':>6} {'before':>7} {'after':>7} {'err_before':>11} {'err_after':>10}"
)
err_b: list[float] = []
err_a: list[float] = []
for pno, truth in src_truth:
    if not truth:
        continue
    b = per_page_before[pno].most_common(1)[0][0] if per_page_before[pno] else 0.0
    a = per_page_after[pno].most_common(1)[0][0] if per_page_after[pno] else 0.0
    if not b or not a:
        continue
    eb, ea = (b / truth - 1) * 100, (a / truth - 1) * 100
    err_b.append(abs(eb))
    err_a.append(abs(ea))
    flag = "" if abs(ea) < abs(eb) else "  <-- WORSE"
    print(
        f"{pno:>5} {truth:>6.2f} {b:>7.2f} {a:>7.2f} {eb:>+10.1f}% {ea:>+9.1f}%{flag}"
    )

print()
print(
    f"mean |error| vs source: before {sum(err_b) / len(err_b):.1f}%   "
    f"after {sum(err_a) / len(err_a):.1f}%"
)
print(f"blocks that grew       : {len(grew)}")
print(f"size-vs-linebox violations: {len(mismatch)}")
for m in mismatch[:8]:
    print(f"  !! {m}")
print("=" * 78)
