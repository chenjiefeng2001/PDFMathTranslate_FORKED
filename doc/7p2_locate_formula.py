"""P2 定位：LaTeX 公式是怎么混进标题文本的？

现象：mp2e page 36（0 基）的标题块 p36_8，``kind=heading``，``translated`` 有 86 字符，
其中含 ``\\begin{array} ... \\end{array}``。16pt 高的框装不下，缩到 5.58pt 才勉强放下
（见 doc/7p5_audit_report.md §6 的 P2）。

要分清三种可能，因为修法完全不同：

- **A. MinerU 本来就把公式内联进这一块的 text** —— 那么必须回到解析侧，接受它内联，
  在 canonical 层拆；
- **B. MinerU 给出了独立的公式块，但 adapter 合并了** —— 修合并逻辑；
- **C. 公式在独立块里，只是 translate 阶段把它拼到了标题的译文里** —— 修翻译装配。

跑法::

    python doc/7p2_locate_formula.py doc/7p6-p1-verify50 36
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent

out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "doc/7p6-p1-verify50")
pno = int(sys.argv[2]) if len(sys.argv) > 2 else 36

raw = json.loads(
    next(out_dir.glob("output/**/*_magicpdf.json")).read_text(encoding="utf-8")
)
doc = json.loads(
    next(out_dir.glob("output/**/*_document.json")).read_text(encoding="utf-8")
)
plan = json.loads(
    next(out_dir.glob("output/**/*_render_plan.json")).read_text(encoding="utf-8")
)

print(
    f"=== MinerU raw blocks on page {pno} (the parse output, before any of our code) ==="
)
page_raw = raw[pno] if isinstance(raw, list) else raw["pages"][pno]
for i, b in enumerate(page_raw.get("blocks", [])):
    txt = str(b.get("text") or "")
    has_tex = "\\" in txt
    print(
        f"  [{i}] type={b.get('type')!r} cls={b.get('cls')!r} "
        f"bbox={[round(v, 1) for v in (b.get('bbox') or [])]}"
    )
    print(f"       text   = {txt[:96]!r}{'   <<< HAS LATEX' if has_tex else ''}")
    if b.get("latex") or b.get("img"):
        print(f"       latex  = {str(b.get('latex'))[:70]!r}")
        print(f"       img    = {str(b.get('img'))[:70]!r}")

print()
print(f"=== our document model blocks on page {pno} ===")
dp = doc["pages"][pno]
for b in dp.get("blocks", []):
    md = b.get("metadata") or {}
    txt = str(b.get("text") or "")
    print(
        f"  {b.get('id')!r:<10} kind={b.get('kind')!r:<10} "
        f"fs={md.get('font_size')} bbox={[round(v, 1) for v in (b.get('bbox') or [])]}"
    )
    print(f"       text = {txt[:96]!r}")

print()
print(f"=== render plan entries on page {pno} ===")
for b in plan:
    if int(b["page"]) != pno:
        continue
    print(
        f"  {b['block_id']:<10} kind={b.get('kind'):<10} fs={b.get('font_size')} "
        f"path={b.get('render_path')} fixup={b.get('render_fixup')}"
    )
    print(f"       translated = {str(b.get('translated'))[:100]!r}")
