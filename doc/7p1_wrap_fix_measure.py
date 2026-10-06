"""在 7P0 的真实 50 页产物上，量化「中文换行修复」到底救回了多少内容。

7P0 报告（``doc/7p0_real_load_report.md`` 缺陷 R2）说 p38_2 丢 145/244 字。
本脚本在**同一份 render_plan.json** 上用**修复前 / 修复后**两套换行器各跑一遍，
给出可对比的数字，而不是只报「现在不丢字了」。

用法:
    python doc/7p1_wrap_fix_measure.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLAN = ROOT / "doc" / "7p0-load50" / "render_plan.json"


def _cjk(s: str) -> int:
    return sum(1 for c in (s or "") if ord(c) > 0x2E80)


class _Page:
    def __init__(self) -> None:
        self.drawn: list[tuple[float, str, float | None]] = []
        self.rect = pymupdf.Rect(0.0, 0.0, 595.0, 842.0)

    def insert_text(self, pt, text, fontsize=None, fontname=None):
        self.drawn.append((pt[1], text, fontsize))

    @property
    def text(self) -> str:
        return "".join(t for _, t, _ in self.drawn)


def _legacy_layout(render, rect, text, fs, font):
    """修复前的换行器：只按空格切词。

    刻意从当前实现里**独立重建**，而不是 ``git show`` 旧版本 —— 那样会把旧代码的
    其它行为也一起带进来，测出来的差异就无法归因到「按空格切词」这一处。
    """
    page = _Page()
    stats = {"blocks": 0, "glyphs": 0}
    effective = font or "helv"
    if effective == "china-ss" and all(ord(c) < 0x2E80 for c in text):
        effective = "helv"
    x0, y0, x1, y1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
    max_w = max(0.1, x1 - x0)

    def width(s: str) -> float:
        if effective in ("helv", "cour"):
            return pymupdf.get_text_length(s, fontsize=fs, fontname=effective)
        return len(s) * fs

    lines: list[str] = []
    cur = ""
    for tok in text.split(" "):  # <-- 旧实现只认空格
        sep = " " if cur else ""
        trial = f"{cur}{sep}{tok}"
        if cur and width(trial) > max_w:
            lines.append(cur)
            cur = tok
        else:
            cur = trial
    if cur:
        lines.append(cur)

    from pdf2zh.v3.magicpdf_renderer import _draw_line

    line_h = fs * 1.4
    y = y0 + fs * 0.85
    drawn = 0
    for line in lines:
        if drawn and y > y1 + 1e-6:
            break
        _draw_line(page, line, x0, y, effective, fs, stats, "legacy")
        drawn += 1
        y += line_h
    return page, stats


def _fixed_layout(render, rect, text, fs, font):
    page = _Page()
    stats = {"blocks": 0, "glyphs": 0}
    render._insert_text_wrapped(page, rect, text, fs, font, stats)
    return page, stats


def main() -> int:
    if not PLAN.is_file():
        print(f"missing evidence: {PLAN}")
        return 2
    blocks = json.loads(PLAN.read_text(encoding="utf-8"))
    import pdf2zh.v3.magicpdf_renderer as R

    risky = [
        b
        for b in blocks
        if b.get("render_path") in ("translate_refit", "shift_down")
        and (b.get("translated") or "").strip()
        and _cjk(b["translated"]) > _cjk(b.get("text") or "")
    ]

    tot_before = tot_after = 0
    worst_before: list[tuple[str, int, int]] = []
    lossy_after: list[str] = []
    for b in risky:
        text = b["translated"]
        fs = b.get("font_size") or 9.0
        rect = pymupdf.Rect(*b["src_box"])
        before, _ = _legacy_layout(R, rect, text, fs, "china-ss")
        after, _ = _fixed_layout(R, rect, text, fs, "china-ss")
        nb, na = len(before.text), len(after.text)
        tot_before += nb
        tot_after += na
        if nb < len(text):
            worst_before.append((b["block_id"], len(text) - nb, len(text)))
        # 行尾空格在换行处被取代，不算内容丢失
        if after.text.replace(" ", "") != text.replace(" ", ""):
            lossy_after.append(b["block_id"])

    total = sum(len(b["translated"]) for b in risky)
    print(f"risky blocks (CJK widened)      : {len(risky)}")
    print(f"translated chars in those blocks: {total}")
    print()
    print(
        f"BEFORE  chars landed            : {tot_before}  ({tot_before/total*100:.1f}%)"
    )
    print(
        f"AFTER   chars landed            : {tot_after}  ({tot_after/total*100:.1f}%)"
    )
    print(f"recovered                      : {tot_after - tot_before} chars")
    print()
    worst_before.sort(key=lambda x: -x[1] / x[2] if x[2] else 0)
    print("worst offenders BEFORE (block, lost/total):")
    for bid, lost, tot in worst_before[:8]:
        print(f"   {bid:>10}  {lost:>4}/{tot:<4} ({lost/tot*100:.1f}%)")
    print()
    print(f"blocks still losing real content AFTER: {len(lossy_after)}")
    if lossy_after:
        print("  ", lossy_after[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
