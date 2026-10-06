"""真实翻译负载的独立度量（不依赖 7n9 那套空跑的审计）。

为什么自己写
------------
``doc/7n9-mp2e-fix3/audit/summary.json`` 报的是 ``qualification: PASS``，但同一
份文件里 ``pages: 562`` 而 ``page_grades`` 全 0、``total_events: 11467`` 而
``rule_results: 0``、``rules: []``。也就是说**没有一条规则被执行过**，那个 PASS
不代表任何质量结论。本脚本因此不复用它的判定，只用产物（render_plan /
document.json / 输出 PDF）重新算。

度量项
------
parse       页/块分布、layout split、公式数
translate   覆盖率、空译文、与原文相同的「未翻译」残留、英文残留率、CJK 率
render      越界字形、**独立复算**的 span 叠印数（不采信渲染器自报）
loss        render_plan 里的译文 vs 输出 PDF 里真实存在的文本，量化丢失

用法:
    python doc/7p0_real_load_analyze.py --run <run-dir>
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# translate
# --------------------------------------------------------------------------
_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def analyze_translation(blocks: list[dict]) -> dict:
    total = len(blocks)
    tr = [b for b in blocks if b.get("translated")]
    src_chars = sum(len(b.get("text") or "") for b in blocks)
    out_chars = sum(len(b.get("translated") or "") for b in blocks)

    empty = [b for b in blocks if not (b.get("translated") or "").strip()]
    #: 真正要挑的「空译文」必须**原文非空** —— 图/占位块原文也空，把它们算成
    #: 漏译会虚报 36 个（实测修正前 empty_translation=36，加了这个条件后为 0）。
    empty_with_src = [
        b
        for b in blocks
        if not (b.get("translated") or "").strip() and (b.get("text") or "").strip()
    ]
    identical = [
        b
        for b in blocks
        if (b.get("translated") or "").strip()
        and (b.get("translated") or "").strip() == (b.get("text") or "").strip()
    ]
    #: 身份翻译里要区分「本就不该译」和「真的漏译」：公式/代码/纯标识符属于前者。
    ident_legit = [
        b
        for b in identical
        if b.get("kind") in ("code", "formula")
        or not re.search(r"[a-z]{4,}\s+[a-z]{4,}", (b.get("text") or ""))
    ]
    ident_suspect = [b for b in identical if b not in ident_legit]
    latin_only = [
        b
        for b in tr
        if not _CJK.search(b["translated"])
        and _LATIN.search(b["translated"])
        and len(b["translated"].strip()) > 12
    ]
    residue = []
    for b in tr:
        t = b["translated"]
        if not t.strip():
            continue
        lat = len(_LATIN.findall(t))
        cjk = len(_CJK.findall(t))
        # 正常中文译文里 ASCII 主要是术语/数字；纯英文残留会 lat >> cjk
        if lat > 0 and cjk == 0 and lat / max(len(t), 1) > 0.6:
            residue.append(b)
        elif cjk and lat / max(cjk, 1) > 3.0:
            residue.append(b)

    by_kind = Counter(b.get("kind") for b in blocks)
    return {
        "blocks_total": total,
        "blocks_translated": len(tr),
        "coverage": round(len(tr) / total, 4) if total else None,
        "empty_translation": len(empty),
        "empty_translation_with_source": len(empty_with_src),
        "identical_to_source": len(identical),
        "identical_legit_untranslatable": len(ident_legit),
        "identical_suspect_miss": len(ident_suspect),
        "latin_only_output": len(latin_only),
        "suspicious_residue": len(residue),
        "src_chars": src_chars,
        "out_chars": out_chars,
        "out_over_src": round(out_chars / src_chars, 3) if src_chars else None,
        "by_kind": dict(by_kind),
        "identical_by_kind": dict(Counter(b.get("kind") for b in identical)),
        "samples": {
            "empty": [b["block_id"] for b in empty[:8]],
            "empty_with_source": [
                {"id": b["block_id"], "text": (b.get("text") or "")[:70]}
                for b in empty_with_src[:8]
            ],
            "identical": [
                {
                    "id": b["block_id"],
                    "kind": b.get("kind"),
                    "text": (b.get("text") or "")[:70],
                }
                for b in identical[:5]
            ],
            "identical_suspect": [
                {
                    "id": b["block_id"],
                    "kind": b.get("kind"),
                    "text": (b.get("text") or "")[:70],
                }
                for b in ident_suspect[:8]
            ],
            "residue": [
                {"id": b["block_id"], "out": (b.get("translated") or "")[:90]}
                for b in residue[:5]
            ],
            "latin_only": [
                {"id": b["block_id"], "out": (b.get("translated") or "")[:90]}
                for b in latin_only[:5]
            ],
        },
    }


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------
def analyze_parse(doc: dict, blocks: list[dict]) -> dict:
    pages = sorted({b["page"] for b in blocks})
    per_page = Counter(b["page"] for b in blocks)
    kinds = Counter(b.get("kind") for b in blocks)
    paths = Counter(b.get("render_path") for b in blocks)
    fixups = Counter(str(b.get("render_fixup")) for b in blocks)
    # layout split：MinerU 侧把一个逻辑块拆成多个 layout block 的痕迹
    splits = doc.get("layout_splits")
    return {
        "pages_with_blocks": len(pages),
        "page_range": [pages[0], pages[-1]] if pages else None,
        "blocks": len(blocks),
        "blocks_per_page_mean": round(len(blocks) / len(pages), 2) if pages else None,
        "blocks_per_page_max": max(per_page.values()) if per_page else None,
        "by_kind": dict(kinds),
        "by_render_path": dict(paths),
        "by_render_fixup": dict(fixups),
        "layout_splits": len(splits) if isinstance(splits, list) else splits,
        "per_page": {str(p): per_page[p] for p in pages},
    }


# --------------------------------------------------------------------------
# render —— 独立复算，不采信渲染器自报
# --------------------------------------------------------------------------
def _span_overlaps(pymupdf, path: Path, area_frac: float = 0.30):
    """逐页统计文本 span 两两压盖。

    只在**同一绘制序列的相邻 span** 之间判重叠，并要求交集面积超过较小 span 的
    area_frac —— 否则行距、基线偏移、上下标都会误报。返回 {page: count}。
    """
    doc = pymupdf.open(str(path))
    per_page = {}
    worst = []
    for pno in range(len(doc)):
        page = doc[pno]
        d = page.get_text("dict")
        spans = []
        for blk in d.get("blocks", []):
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if not sp.get("text", "").strip():
                        continue
                    spans.append(sp)
        hits = 0
        pairs = []
        # 按 y 排序后只看邻近行，O(n·k) 而不是 O(n^2)
        spans.sort(key=lambda s: (round(s["bbox"][1], 1), s["bbox"][0]))
        for i, a in enumerate(spans):
            ax0, ay0, ax1, ay1 = a["bbox"]
            aa = max((ax1 - ax0) * (ay1 - ay0), 1e-6)
            for b in spans[i + 1 : i + 12]:
                bx0, by0, bx1, by1 = b["bbox"]
                if by0 > ay1 + 2:  # 已按 y 排序，后面的更靠下
                    break
                ix = min(ax1, bx1) - max(ax0, bx0)
                iy = min(ay1, by1) - max(ay0, by0)
                if ix <= 0.4 or iy <= 0.4:
                    continue
                ba = max((bx1 - bx0) * (by1 - by0), 1e-6)
                inter = ix * iy
                if inter / min(aa, ba) > area_frac:
                    hits += 1
                    if len(pairs) < 4:
                        pairs.append(
                            {
                                "a": a["text"][:40],
                                "b": b["text"][:40],
                                "frac": round(inter / min(aa, ba), 2),
                            }
                        )
        if hits:
            per_page[pno + 1] = hits
            worst.append({"page": pno + 1, "overlaps": hits, "examples": pairs})
    doc.close()
    return per_page, worst


def _out_of_page(pymupdf, path: Path):
    doc = pymupdf.open(str(path))
    bad = []
    for pno in range(len(doc)):
        rect = doc[pno].rect
        for blk in doc[pno].get_text("dict").get("blocks", []):
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if not sp.get("text", "").strip():
                        continue
                    x0, y0, x1, y1 = sp["bbox"]
                    if (
                        x0 < rect.x0 - 1
                        or y0 < rect.y0 - 1
                        or x1 > rect.x1 + 1
                        or y1 > rect.y1 + 1
                    ):
                        bad.append(
                            {
                                "page": pno + 1,
                                "text": sp["text"][:40],
                                "bbox": [round(v, 1) for v in sp["bbox"]],
                            }
                        )
    n = len(doc)
    doc.close()
    return n, bad


def analyze_render(pymupdf, mono: Path, blocks: list[dict]) -> dict:
    per_page, worst = _span_overlaps(pymupdf, mono)
    npages, oob = _out_of_page(pymupdf, mono)
    # renderer 自报的两条
    wide = []
    return {
        "mono_pages": npages,
        "plan_pages": len({b["page"] for b in blocks}),
        "overlap_pages": len(per_page),
        "overlap_total": sum(per_page.values()),
        "overlap_by_page": {str(k): v for k, v in sorted(per_page.items())},
        "overlap_worst": worst[:6],
        "out_of_page_spans": len(oob),
        "out_of_page_samples": oob[:5],
        "wide_line_drops": wide,
    }


# --------------------------------------------------------------------------
# loss —— 译文是否真的落到 PDF 里
# --------------------------------------------------------------------------
def analyze_loss(pymupdf, mono: Path, blocks: list[dict], sample: int = 400) -> dict:
    """抽样比对：render_plan 的译文 vs 输出 PDF 里真实存在的文本。

    做法与两个已被实测否掉的直觉有关：

    - **不能用「连续 8 字命中」**。PDF 提取顺序不等于块顺序，原文会插在译文中间，
      换行也会切断连续片段 —— 早先这么写得到 missing_rate=0.916，而实测该 PDF 的
      CJK 完全可提取（第 7 页 855 个汉字），是量法错了不是文件错了。改为取多个
      分散的 4-gram，逐个在整页文本里找，命中过半即视为「这段译文在页面上」。
    - **页码要 +1**。render_plan 的 ``page`` 是 0 基（实测范围 0..49），PDF 页码
      是 1 基；不对齐会把整章的判定挪到隔壁页。
    """
    doc = pymupdf.open(str(mono))
    page_text = {}
    for pno in range(len(doc)):
        page_text[pno + 1] = re.sub(r"\s+", "", doc[pno].get_text("text"))
    doc.close()

    def present(txt: str, page0: int) -> bool:
        hay = page_text.get(page0 + 1, "")  # 0 基 -> 1 基
        norm = re.sub(r"\s+", "", txt or "")
        if len(norm) < 8:
            return True
        grams = []
        step = max(len(norm) // 6, 4)
        for i in range(0, len(norm) - 3, step):
            grams.append(norm[i : i + 4])
            if len(grams) >= 6:
                break
        if not grams:
            grams = [norm[:4]]
        hits = sum(1 for g in grams if g in hay)
        return hits * 2 >= len(grams)

    checked = miss = 0
    misses = []
    for b in blocks:
        t = b.get("translated") or ""
        if len(t) < 20:
            continue
        checked += 1
        if not present(t, b["page"]):
            miss += 1
            if len(misses) < 10:
                misses.append(
                    {
                        "id": b["block_id"],
                        "page": b["page"],
                        "len": len(t),
                        "text": t[:80],
                    }
                )
        if checked >= sample:
            break
    return {
        "checked_blocks": checked,
        "missing_from_pdf": miss,
        "missing_rate": round(miss / checked, 4) if checked else None,
        "note": "4-gram majority match; page index converted 0-based -> 1-based",
        "samples": misses,
    }


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="run dir containing output/magicpdf")
    args = ap.parse_args()

    run = Path(args.run)
    mg = run / "output" / "magicpdf"
    plan_f = next(mg.glob("*_render_plan.json"), None)
    doc_f = next(mg.glob("*_document.json"), None)
    mono_f = next(mg.glob("*_mono.pdf"), None)
    if not plan_f or not doc_f or not mono_f:
        raise SystemExit(f"missing artifacts under {mg}")

    import pymupdf

    blocks = _load(plan_f)
    doc = _load(doc_f)
    report = {
        "run_dir": str(run),
        "artifacts": {
            "render_plan": plan_f.name,
            "document": doc_f.name,
            "mono_pdf": mono_f.name,
        },
        "parse": analyze_parse(doc, blocks),
        "translate": analyze_translation(blocks),
        "render": analyze_render(pymupdf, mono_f, blocks),
        "loss": analyze_loss(pymupdf, mono_f, blocks),
    }
    (run / "analysis.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=1, ensure_ascii=False)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
