"""对照实验：同一语料/同一模型/同一页范围，比较修复前后的产物。

数据来自两次**真跑**（同一 commit 系列的 HEAD vs 7P0 记录的那次）：

- BEFORE  ``doc/7p0-load50/``        —— 修复前（7P0 那轮，89 min）
- AFTER   本 run 目录（``--out``）    —— 修复后

⚠ 一个必须先说清的前提
----------------------
修复后的这次**整篇退到了 legacy 内核**，因为 P0 的合并页检测拦下了第 7 页那个
目录折叠（日志：``page 6: 327 lines, 2.03pt/line, implies 5.83 page(s)``）。
所以 AFTER 的产物是 **legacy 渲染**的，不是 magicpdf 渲染的。

因此这个对照**不能**直接回答"magicpdf 修好之后好多少"，它回答的是：
「折叠被拦住之后，工具选择降级交付，与此前直接交付一页叠影相比如何」。
这本身就是 P0 那个降级决策必须付的代价，需要如实量化。

用法::

    python doc/7p2_compare_runs.py --after <run-dir>
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 7P0 那次（修复前）的产物。仓库里只提交了小体积证据，成品 PDF 仍在临时目录，
#: 所以两个位置都找；都找不到时几何指标会是 None —— 报告里必须显式说明「未测到」，
#: 而不是让 0 和 None 混为一谈。
CJK = re.compile(r"[\u4e00-\u9fff]")


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _plan(run: Path) -> list[dict]:
    """render_plan.json —— **只有 magicpdf 路径会产出**。

    降级到 legacy 后这个文件不存在，块级指标全部为空。绝不能把「没有产物」
    报成 0 个块：那是把"没测到"说成"测到是 0"。
    """
    f = run / "render_plan.json"
    if f.is_file():
        return _load(f)
    cands = sorted((run / "output" / "magicpdf").glob("*_render_plan.json"))
    return _load(cands[0]) if cands else []


def _mono(run: Path) -> Path | None:
    """成品 PDF。magicpdf 落在 ``output/magicpdf/``，legacy 直接落在 ``output/``。

    降级那次两者都找得到，所以必须**两个位置都查**——只查 magicpdf 子目录会让
    降级后的运行看起来"没有产物"。
    """
    f = run / "mono.pdf"
    if f.is_file():
        return f
    # magicpdf 命名 ``<book>_mono.pdf``，legacy 命名 ``<book>-mono.pdf`` —— 下划线
    # vs 连字符。只匹配其中一种会让另一条路径看起来"没有产物"（实测踩过：降级那次
    # glob ``*_mono.pdf`` 返回空，于是几何指标整排 None，还以为产物丢了）。
    for pat in (
        "output/*-mono.pdf",
        "output/*_mono.pdf",
        "output/**/*-mono.pdf",
        "output/**/*_mono.pdf",
    ):
        cands = sorted(run.glob(pat))
        if cands:
            return cands[0]
    return None


def _log(run: Path) -> str:
    f = run / "run.log"
    return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""


def metrics(run: Path, label: str) -> dict:
    import pymupdf

    blocks = _plan(run)
    out: dict = {"label": label, "run": str(run)}

    # --- elapsed
    s = run / "run-summary.json"
    if s.is_file():
        out["elapsed_s"] = _load(s).get("elapsed_s")

    # --- engine actually used
    log = _log(run)
    degraded = "并进了同一" in log or "merged source pages" in log
    out["degraded_to_legacy"] = degraded
    out["engine"] = "legacy (degraded)" if degraded else "magicpdf"

    # --- block distribution / page collapse
    per_page = Counter(b["page"] for b in blocks)
    out["blocks"] = len(blocks)
    out["pages_with_blocks"] = len(per_page)
    out["max_blocks_on_one_page"] = max(per_page.values()) if per_page else 0
    top = per_page.most_common(1)[0] if per_page else (None, 0)
    out["worst_page"] = {"page": top[0], "blocks": top[1]}

    # --- translation completeness
    tr = [b for b in blocks if (b.get("translated") or "").strip()]
    empty_with_src = [
        b
        for b in blocks
        if not (b.get("translated") or "").strip() and (b.get("text") or "").strip()
    ]
    out["blocks_translated"] = len(tr)
    out["coverage"] = round(len(tr) / len(blocks), 4) if blocks else None
    out["empty_translation_with_source"] = len(empty_with_src)
    out["cjk_ratio"] = (
        round(
            len(CJK.findall("".join(b["translated"] for b in tr)))
            / max(sum(len(b["translated"]) for b in tr), 1),
            3,
        )
        if tr
        else None
    )

    # --- geometry (independent recompute on the rendered PDF)
    mono = _mono(run)
    if mono and mono.is_file():
        from pdf2zh.v3.magicpdf_renderer import audit_page_geometry

        doc = pymupdf.open(str(mono))
        # ⚠ legacy 路径会把译好的 50 页**嫁接回原 562 页文档**，所以产物是全书
        # 页数，而只有前 50 页含译文（实测 pages_with_cjk=47）。拿全书页数做指标
        # 等于把 512 页未翻译的原文算进来。必须只看被翻译的那段。
        limit = min(len(doc), _page_limit(run))
        sub = _slice_doc(doc, limit)
        a = audit_page_geometry(sub)  # 不忽略背景（上界）
        b_ = audit_page_geometry(sub, ignore=_bg(run, sub))
        out["mono_pages_total"] = len(doc)
        out["mono_pages_measured"] = limit
        out["pages_with_overlap_noignore"] = a["pages_with_overlap"]
        out["pages_with_overlap"] = b_["pages_with_overlap"]
        out["spans_outside_page"] = b_["spans_outside_page"]
        out["pages_with_cjk"] = sum(
            1 for p in range(limit) if CJK.search(sub[p].get_text("text") or "")
        )
        doc.close()
    return out


def _page_limit(run: Path) -> int:
    """被翻译的页数（默认 50）。"""
    s = run / "run-summary.json"
    if s.is_file():
        d = _load(s)
        pages = str(d.get("pages") or "")
        if "-" in pages:
            try:
                return int(pages.split("-")[-1])
            except ValueError:
                pass
    return 50


def _slice_doc(doc, limit: int):
    """只保留前 ``limit`` 页，产出可独立传给 audit 的文档。"""
    import pymupdf

    if len(doc) <= limit:
        return doc
    out = pymupdf.open()
    out.insert_pdf(doc, from_page=0, to_page=limit - 1)
    return out


def _bg(run: Path, doc) -> dict:
    """重建渲染器的背景忽略集。

    必须**如实复现**渲染器的做法：把源页 ``show_pdf_page`` 贴进一张空白页，再对该页
    取 :func:`page_span_snapshot`。不能直接读源 PDF 的 span —— ``show_pdf_page``
    会对坐标做变换/取整，而 ``_span_key`` 是按量化后的 bbox 建键的，于是键对不上、
    一个背景 span 都排除不掉。实测这正是我先前把 41 页误当叠印的同一个坑：
    近似复现得到 41，如实复现得到 11。
    """
    import pymupdf

    from pdf2zh.v3.magicpdf_renderer import page_span_snapshot

    src_path = ROOT / "tests" / "file" / "The Art of Multiprocessor Programming, 2e.pdf"
    src = pymupdf.open(str(src_path))
    ignore = {}
    for p in range(min(len(doc), len(src))):
        blank = doc.new_page(width=doc[p].rect.width, height=doc[p].rect.height)
        blank.show_pdf_page(blank.rect, src, p)
        ignore[p] = page_span_snapshot(blank)
        doc.delete_page(doc.page_count - 1)
    src.close()
    return ignore


_BEFORE_CANDIDATES = [
    ROOT / "doc" / "7p0-load50",
    Path(os.environ.get("TEMP", "/tmp")) / "opencode" / "load50",
]
#: 优先挑**同时有日志和成品 PDF** 的那个：只要日志没有 PDF，几何指标会整排 None，
#: 而提交进仓库的小证据目录恰好就是这样（7P0 只提了 run-config/run.log/analysis/
#: render_plan，成品 PDF 留在临时目录）。
BEFORE = next(
    (
        d
        for d in _BEFORE_CANDIDATES
        if d.is_dir() and (d / "run.log").is_file() and _mono(d) is not None
    ),
    _BEFORE_CANDIDATES[0],
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", required=True)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    after = metrics(Path(args.after), "AFTER (HEAD)")
    before = metrics(BEFORE, "BEFORE (7P0)")
    print("=" * 74)
    print("WARNING: the AFTER run DEGRADED to the legacy kernel.")
    print("P0's merged-page detector caught mp2e's 8-page TOC collapsed onto one")
    print("render page, and the existing degrade chain routed the whole file to")
    print("legacy. So the two runs are NOT the same renderer, and magicpdf's")
    print("post-fix quality is NOT measured here -- only what the degrade costs")
    print("or saves versus shipping a page of stacked ink.")
    print("=" * 74)
    print()

    keys = [
        ("elapsed_s", "wall clock (s)"),
        ("engine", "engine actually used"),
        ("blocks", "render-plan blocks"),
        ("max_blocks_on_one_page", "worst page: blocks"),
        ("coverage", "translation coverage"),
        ("empty_translation_with_source", "empty w/ source"),
        ("pages_with_overlap", "overlap pages (renderer口径)"),
        ("pages_with_overlap_noignore", "overlap pages (no-ignore)"),
        ("spans_outside_page", "spans outside page"),
        ("pages_with_cjk", "pages containing CJK"),
        ("cjk_ratio", "CJK share of output"),
    ]
    w = 30
    print(f"{'metric':<{w}} {'BEFORE':>18} {'AFTER':>18}")
    print("-" * (w + 40))
    for k, name in keys:
        b = before.get(k)
        a = after.get(k)
        fb = f"{b:.3f}" if isinstance(b, float) else str(b)
        fa = f"{a:.3f}" if isinstance(a, float) else str(a)
        mark = ""
        if isinstance(b, (int, float)) and isinstance(a, (int, float)) and b:
            d = (a - b) / abs(b) * 100
            if abs(d) >= 1:
                mark = f"  ({d:+.0f}%)"
        print(f"{name:<{w}} {fb:>18} {fa:>18}{mark}")

    payload = {
        "before": before,
        "after": after,
        "note": "AFTER degraded to legacy because P0 caught the collapsed TOC page",
    }
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
