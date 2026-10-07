"""P3 追问：字号只贡献 +2，那剩下的 11 → 17 是什么？

``doc/7p3_overlap_attribution.py`` 的单变量对照已经给出：同一份计划只换字号，叠印页
21 → 23（**delta +2**）。而 7P0 基线是 11 页。所以 11 → 17 里的**约 9 页不是字号造成的**。

本脚本做第二组单变量对照：把 P0 造成的**块数差异**隔离出来。7P0 基线有 621 块、
137 个 toc 条目（目录被折叠成孤块）；7P5 之后是 450 块、27 个 toc 条目（P4 重解析把
page 6 的 201 个重叠块换成了正常条目）。既然 P0/P4 恰恰是为了**消除**叠印而做的，
这里要验证：叠印变多，是不是因为原先"被折叠、因而大量保留原文未翻译"的目录页不再保留，
于是译文真的画上去了 —— 换句话说，**叠印变多可能是翻译覆盖率提高的副作用，而不是
排版退化**。

判据：对每轮统计「含叠印的页里，译文 vs 保留原文的块数」，以及「CJK 译文块总数」。
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
CJK = re.compile(r"[\u4e00-\u9fff]")

RUNS = [
    ("7P0 baseline", "doc/7p0-load50"),
    ("7P5 font calib", "doc/7p5-audit50"),
    ("7P7 after P1+P2", "doc/7p7-p2-verify50"),
    ("7P8 no-cache", "doc/7p8-thread4-nocache"),
]


def load(d: str):
    fs = Path(d).glob("**/*_render_plan.json")
    fs = list(fs) or list(Path(d).glob("render_plan.json"))
    if not fs:
        return None, None
    plan = json.loads(fs[0].read_text(encoding="utf-8"))
    log = None
    try:
        log = Path(d, "run.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return plan, log


def overlap_pages(log: str | None) -> int | None:
    if not log:
        return None
    m = re.search(r"([0-9]+) 页存在文", log)
    return int(m.group(1)) if m else None


def main() -> int:
    print(
        f"{'run':<18} {'blocks':>7} {'ovl':>4} {'CJK':>5} {'kept':>5} "
        f"{'ovl_pages_w/ CJK':>17}  {'ovl share':>10}"
    )
    print("-" * 92)
    for tag, d in RUNS:
        plan, log = load(d)
        if plan is None:
            print(f"{tag:<18} (missing)")
            continue
        ovl = overlap_pages(log)
        cjk = sum(1 for b in plan if CJK.search(str(b.get("translated") or "")))
        kept = sum(1 for b in plan if not (b.get("translated") or "").strip())
        # 含叠印的页（只知总数，逐页明细从 renderer 的 examples 里再取）
        ex = []
        if log:
            m = re.search(r"样例页 (\[[^\]]*\])", log)
            if m:
                try:
                    ex = json.loads(m.group(1).replace("'", '"'))
                except Exception:  # noqa: BLE001
                    ex = []
        ex_pages = {e["page"] for e in ex}
        cjk_in_ovl = sum(
            1
            for b in plan
            if int(b.get("page") or 0) in ex_pages
            and CJK.search(str(b.get("translated") or ""))
        )
        share = f"{cjk_in_ovl}/{len(ex_pages)}" if ex_pages else "?"
        print(
            f"{tag:<18} {len(plan):>7} {str(ovl):>4} {cjk:>5} {kept:>5} "
            f"{share:>17}  {'' if not ex_pages else '':<10}"
        )

    print()
    print("=" * 92)
    print("READING")
    print("=" * 92)
    print("* 7P0 基线 621 块里 137 个 toc 条目 —— 那是目录被折叠后的孤块，P4 重解析")
    print("  已把它换成 27 个正常条目。这批块在 7P0 里大多是**保留原文**（未翻译）。")
    print("* 若叠印集中在这些目录页上，那么叠印上升 = **翻译覆盖率提高**，")
    print("  而不是排版退化：原先没被翻译、也就没有被绘制，自然不会叠印。")
    print("* 字号只贡献 +2 页（P3 单变量对照实测 21 -> 23）。")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
