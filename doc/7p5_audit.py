"""50 页翻译产物审计 —— 字号校准落地后的端到端对账。

跑法::

    python doc/7p5_audit.py doc/7p5-audit50

回答四个问题，每一个都用产物而不是日志自述来判：

1. **字号落点**：render_plan 的块字号相对源 PDF 真实字号的误差，并与上一次同语料运行
   ``doc/7p0-load50/`` 用**同一把尺子**对比（否则 7.3% 没法判断好坏）。
2. **内容丢失**：这是「不溢出、不静默裁字」的直接检验。日志里的
   ``line(s) dropped`` 只是自述，真正的判据是
   ``len(render_payload.lines)`` 里的落笔文本 vs ``translated`` 的完整译文 ——
   两者不等就是真的丢了字。
3. **几何**：越界 / 叠印页 / fixup / overflow。
4. **校准覆盖率**：哪几页没校准、为什么。

渲染计划里译文在 ``translated`` 字段（``text`` 是**原文**）；``render_payload.lines``
是真正画出去的每一行。用错字段会得出"0 块含中文"的荒谬结论。
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

BOOK = ROOT / "tests" / "file" / "The Art of Multiprocessor Programming, 2e.pdf"
CJK = re.compile(r"[\u4e00-\u9fff]")
NONSPACE = re.compile(r"\S")


def norm_nospace(s: str) -> str:
    return "".join(NONSPACE.findall(s))


def longest_common_substring(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组 DP）。块都很短，O(n*m) 足够。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def source_modal(pno: int, _cache={}) -> float:
    if pno in _cache:
        return _cache[pno]
    sizes: Counter = Counter()
    with pymupdf.open(str(BOOK)) as doc:
        for blk in doc[pno].get_text("dict").get("blocks", []):
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if len((sp.get("text") or "").strip()) >= 8:
                        sizes[round(float(sp.get("size") or 0), 2)] += 1
    sizes.pop(0.0, None)
    _cache[pno] = sizes.most_common(1)[0][0] if sizes else 0.0
    return _cache[pno]


def _plan_font_err(plan: list[dict]) -> tuple[float, int, dict]:
    """块字号众数 vs 源页众数字号 —— 每次运行同一把尺子。"""
    per_page: dict[int, Counter] = {}
    for b in plan:
        v = b.get("font_size")
        if v:
            per_page.setdefault(int(b["page"]), Counter())[round(float(v), 2)] += 1
    errs = []
    for pno, c in per_page.items():
        truth = source_modal(pno)
        got = c.most_common(1)[0][0]
        if truth > 0:
            errs.append(abs((got / truth - 1) * 100))
    return (
        (sum(errs) / len(errs) if errs else 0.0),
        len(errs),
        {
            k: v
            for k, v in Counter(
                round(float(b.get("font_size") or 0), 2) for b in plan
            ).most_common(8)
        },
    )


def _drawn_text(payload: dict) -> str:
    """真正画出去的文本（``render_payload.lines``）。"""
    parts = []
    for ln in payload.get("lines") or []:
        if isinstance(ln, dict):
            parts.append(str(ln.get("text") or ""))
        else:
            parts.append(str(ln))
    if parts:
        return "".join(parts)
    for cmd in payload.get("commands") or []:
        if isinstance(cmd, dict) and cmd.get("kind") == "flow-text":
            parts.append(str(cmd.get("text") or ""))
    return "".join(parts)


def main(out_dir: str) -> int:
    d = Path(out_dir)
    plan_f = next(d.glob("output/**/*_render_plan.json"))
    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    log = (d / "run.log").read_text(encoding="utf-8", errors="replace")
    pdf_f = next(d.glob("output/**/*_mono.pdf"))
    doc = pymupdf.open(str(pdf_f))
    page_text = {i: norm_nospace(doc[i].get_text()) for i in range(doc.page_count)}
    doc.close()
    prev_p = ROOT / "doc" / "7p0-load50" / "render_plan.json"
    prev = json.loads(prev_p.read_text(encoding="utf-8")) if prev_p.is_file() else None

    print(f"run  : {d}")
    print(f"plan : {plan_f.name}  ({len(plan)} blocks)")
    print(f"prev : {'doc/7p0-load50/render_plan.json' if prev else 'n/a'}")

    # ------------------------------------------------------------- 1. 字号
    print("\n" + "=" * 78)
    print("1. FONT SIZE  (block-modal vs source-modal, same ruler both runs)")
    print("=" * 78)
    cur_err, n_pg, cur_dist = _plan_font_err(plan)
    prev_err = None
    print(f"   this run : mean |err| = {cur_err:.1f}%  over {n_pg} pages")
    print(f"   dist     : {cur_dist}")
    if prev:
        p_err, p_n, p_dist = _plan_font_err(prev)
        prev_err = p_err
        print(f"   prev run : mean |err| = {p_err:.1f}%  over {p_n} pages")
        print(f"   dist     : {p_dist}")
        prev_err = p_err
        verdict = "BETTER" if cur_err < p_err else "WORSE"
        print(f"   -> {verdict}  ({p_err:.1f}% -> {cur_err:.1f}%)")

    # ------------------------------------------------------------- 2. 丢字
    print("\n" + "=" * 78)
    print("2. CONTENT LOSS  (drawn text vs full translation)")
    print("=" * 78)
    lost = []
    by_design = []
    for b in plan:
        want = norm_nospace(str(b.get("translated") or ""))
        if len(want) < 4:
            continue
        pno = int(b["page"])
        # 权威判据 = 渲染后的 PDF 文本。反推 render_payload 的内部结构来做这件事
        # 只会踩坑：translate_refit 的文本在 commands 里、toc 走 toc_commands、
        # shift_down 干脆没有 payload —— 同一份计划里就有三种形态。更糟的是，一旦
        # 按"payload 没数据就跳过"来写，恰好会把真正丢字的块全跳过。
        page = page_text.get(pno, "")
        if want in page:
            continue
        # PDF 文本提取对破折号/项目符号不回环："——M.H." 画出来是 "··M.H."，提取回来
        # 前缀变成替换字符。只做全串相等会满屏假警报（实测 23 条里 21 条是这类）。
        # 用最长公共子串占全长的比例来区分「提取差异」与「真丢字」。
        lcs = longest_common_substring(want, page)
        if lcs >= max(4, int(len(want) * 0.6)):
            continue
        path = str(b.get("render_path") or "")
        # TOC 按 toc_commands 渲染（点引线 + 页码）、preserve_float 是公式/代码刻意
        # 保留原样 —— 归一化后的文本形态本就与 translated 不同，不算丢字。
        if path in ("overlay", "preserve_float") or b.get("toc_commands"):
            by_design.append((pno, b["block_id"], path))
            continue
        elsewhere = [i for i, t in page_text.items() if want in t]
        lost.append((pno, b["block_id"], path, want[:56], elsewhere))

    print(f"   by-design text-form mismatch (toc/preserve_float) : {len(by_design)}")
    print(f"   REAL content loss                                    : {len(lost)}")
    for pno, bid, path, want, elsewhere in lost:
        print(f"   page {pno:>3} {bid:<10} path={path}")
        print(f"      translated : {want!r}")
        print(f"      found elsewhere in PDF: {elsewhere or 'NOWHERE'}")

    drops = re.findall(
        r"wrapped block needs (\d+) line\(s\) but only (\d+) fit in ([\d.]+)pt; "
        r"(\d+) line\(s\) dropped: '([^']*)'",
        log,
    )
    print(f"   log 'line(s) dropped' warnings: {len(drops)}")
    for need, fit, box, dropped, prev_txt in drops:
        print(
            f"     needs {need}, {fit} fit in {box}pt -> dropped {dropped}"
            f" ({int(dropped) / max(1, int(need)):.0%})  {prev_txt[:50]!r}"
        )

    # ------------------------------------------------------------- 3. 几何
    print("\n" + "=" * 78)
    print("3. GEOMETRY / TRANSLATION COVERAGE")
    print("=" * 78)
    fx = Counter(str(b.get("render_fixup")) for b in plan)
    ov = Counter(str((b.get("render_payload") or {}).get("overflow")) for b in plan)
    rec = Counter(
        str(((b.get("render_payload") or {}).get("recovery") or {}).get("decision"))
        for b in plan
    )
    ok = Counter(str((b.get("render_payload") or {}).get("layout_ok")) for b in plan)
    print(f"   render_fixup        : {dict(fx)}")
    print(f"   payload.overflow    : {dict(ov)}")
    print(f"   recovery.decision   : {dict(rec)}")
    print(f"   payload.layout_ok   : {dict(ok)}")

    cjk_blocks = sum(1 for b in plan if CJK.search(str(b.get("translated") or "")))
    kept = sum(1 for b in plan if not b.get("translated"))
    print(f"   blocks with CJK translation : {cjk_blocks}/{len(plan)}")
    print(f"   blocks kept as original     : {kept}")

    for pat, label in (
        (r"越界 (\d+)", "out-of-bounds"),
        (r"(\d+) 页存在文", "overlap pages"),
    ):
        m = re.search(pat, log)
        if m:
            print(f"   {label:<24}: {m.group(1)}")

    # ------------------------------------------------------------- 4. 校准
    print("\n" + "=" * 78)
    print("4. CALIBRATION COVERAGE")
    print("=" * 78)
    cal = re.search(r"size calibrated from source on (\d+)/(\d+) page", log)
    if cal:
        got, tot = int(cal.group(1)), int(cal.group(2))
        print(f"   calibrated {got}/{tot}; {tot - got} pages fell back to 0.85")

    out = {
        "blocks": len(plan),
        "font_mean_abs_err_pct": round(cur_err, 2),
        "prev_font_mean_abs_err_pct": round(prev_err, 2) if prev else None,
        "font_dist": cur_dist,
        "blocks_with_real_content_loss": len(lost),
        "blocks_by_design_text_form": len(by_design),
        "dropped_line_warnings": len(drops),
        "fixup": dict(fx),
        "recovery": {k: v for k, v in rec.items() if k != "None"},
        "cjk_blocks": cjk_blocks,
    }
    (d / "audit.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nwrote {d / 'audit.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p5-audit50"))
