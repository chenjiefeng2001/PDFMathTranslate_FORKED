"""7K-1C — build the synthetic annotation corpus for 7K-1.

One 6-page PDF containing 25 annotation cases across the required
categories, plus ``annotation_corpus.json`` recording each case's
expected contract (type / page / target text / bbox tolerance).

Categories covered (per 7K-1 spec):
  * highlight / underline / link / text(comment) annotations
  * multi-annotation on one page, cross-line, cross-page
  * annotations over CJK text, over Latin text
  * annotations overlapping a figure/table
  * negative-control page with no annotations

Evidence-only: this script does NOT touch the production pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "doc" / "7k1"

# Built-in pymupdf fonts only -> self-contained corpus, no host font paths.
LATIN_FONT = "helv"  # base-14 Helvetica
CJK_FONT = "china-s"  # built-in CJK (STSong-Light via Adobe CMaps)

CASES: list[dict] = []


def reg(cid: str, kind: str, page: int, target: str, tol: float = 2.0) -> None:
    CASES.append(
        {
            "annotation_id": cid,
            "type": kind,
            "page": page,
            "target": target,
            "expected": {
                "present": True,
                "type": kind,
                "page": page,
                "target": target,
                "bbox_tolerance": tol,
            },
        }
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pdf = OUT / "annotation_corpus.pdf"
    doc = pymupdf.open()

    # ── page 1: Latin text, basic highlight/underline/text/link ──────────
    page = doc.new_page()
    page.insert_text(
        (72, 90),
        "The quick brown fox jumps over the lazy dog.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    page.insert_text(
        (72, 120),
        "Annotation preservation is the question under study.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    page.insert_text(
        (72, 150),
        "Visit the documentation page for details.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    r = pymupdf.Rect(72, 84, 330, 96)
    page.add_highlight_annot(r)
    reg("A01", "highlight", 1, "The quick brown fox", 2.0)
    r = pymupdf.Rect(72, 114, 400, 126)
    page.add_underline_annot(r)
    reg("A02", "underline", 1, "Annotation preservation", 2.0)
    r = pymupdf.Rect(72, 144, 310, 156)
    page.insert_link(
        {"kind": pymupdf.LINK_URI, "from": r, "uri": "https://example.com/docs"}
    )
    reg("A03", "link", 1, "Visit the documentation page", 2.0)
    r = pymupdf.Rect(420, 84, 540, 160)
    page.add_text_annot(r, "Reader note: check section 3.")
    reg("A04", "text", 1, "Reader note: check section 3.", 4.0)
    r = pymupdf.Rect(72, 114, 260, 126)
    page.add_underline_annot(r)
    reg("A05", "underline", 1, "Annotation preservation is", 2.0)

    # ── page 2: CJK text with annotations ────────────────────────────────
    page = doc.new_page()
    page.insert_text(
        (72, 90),
        "这是一个用于验证标注保真度的中文测试页面。",
        fontname=CJK_FONT,
        fontsize=12,
    )
    page.insert_text(
        (72, 120), "标注内容应当与原文保持一致。", fontname=CJK_FONT, fontsize=12
    )
    page.insert_text(
        (72, 150), "第二行中文用于跨行高亮测试。", fontname=CJK_FONT, fontsize=12
    )
    r = pymupdf.Rect(72, 84, 420, 96)
    page.add_highlight_annot(r)
    reg("B01", "highlight", 2, "这是一个用于验证标注保真度的", 2.0)
    r = pymupdf.Rect(72, 114, 300, 126)
    page.add_underline_annot(r)
    reg("B02", "underline", 2, "标注内容应当", 2.0)
    r = pymupdf.Rect(72, 144, 360, 156)
    page.insert_link(
        {"kind": pymupdf.LINK_URI, "from": r, "uri": "https://example.com/zh"}
    )
    reg("B03", "link", 2, "第二行中文", 2.0)
    r = pymupdf.Rect(430, 84, 545, 150)
    page.add_text_annot(r, "中文注释：请核对翻译")
    reg("B04", "text", 2, "中文注释：请核对翻译", 4.0)
    r = pymupdf.Rect(72, 114, 300, 126)
    page.add_highlight_annot(r)
    reg("B05", "highlight", 2, "标注内容应当", 2.0)
    # mixed CJK + Latin on one line
    page.insert_text(
        (72, 180), "Mixed line: alpha 中文与Latin共存。", fontname=CJK_FONT, fontsize=12
    )
    r = pymupdf.Rect(72, 174, 400, 186)
    page.add_highlight_annot(r)
    reg("B06", "highlight", 2, "Mixed line: alpha 中文与Latin共存", 2.0)

    # ── page 3: cross-line / multi-annotation same region ────────────────
    page = doc.new_page()
    page.insert_text(
        (72, 90),
        "Line one of a long paragraph that continues",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    page.insert_text(
        (72, 120),
        "across multiple lines for a cross-line test.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    page.insert_text(
        (72, 150),
        "This line holds two separate annotations.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    # cross-line highlight: two rects joined logically
    r1 = pymupdf.Rect(72, 84, 400, 96)
    r2 = pymupdf.Rect(72, 114, 300, 126)
    page.add_highlight_annot(r1)
    page.add_highlight_annot(r2)
    reg("C01", "highlight", 3, "Line one ... across multiple lines", 2.0)
    # two annotations on the same text region
    r = pymupdf.Rect(72, 144, 400, 156)
    page.add_highlight_annot(r)
    reg("C02", "highlight", 3, "This line holds two separate", 2.0)
    r = pymupdf.Rect(72, 144, 400, 156)
    page.add_underline_annot(r)
    reg("C03", "underline", 3, "This line holds two separate", 2.0)
    # internal GOTO link -> cross-page reference (page 3 -> page 1)
    r = pymupdf.Rect(72, 174, 360, 186)
    page.insert_link({"kind": pymupdf.LINK_GOTO, "from": r, "page": 0})
    reg("C04", "link_goto", 3, "internal goto to page 1", 2.0)

    # ── page 4: annotations near a figure/table ──────────────────────────
    page = doc.new_page()
    page.insert_text(
        (72, 80), "Figure 1: sample chart caption.", fontname=LATIN_FONT, fontsize=12
    )
    page.draw_rect(pymupdf.Rect(72, 100, 360, 260), color=(0, 0, 0), width=1)
    page.draw_rect(
        pymupdf.Rect(100, 120, 180, 200), color=(0.2, 0.4, 0.8), width=0, fill=True
    )
    page.insert_text(
        (72, 300),
        "Text that overlaps the figure region below.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    # annotation overlapping the drawn figure
    r = pymupdf.Rect(90, 110, 190, 210)
    page.add_highlight_annot(r)
    reg("D01", "highlight", 4, "figure overlap", 2.0)
    r = pymupdf.Rect(72, 74, 330, 86)
    page.add_underline_annot(r)
    reg("D02", "underline", 4, "Figure 1: sample chart caption.", 2.0)
    r = pymupdf.Rect(72, 294, 400, 306)
    page.add_highlight_annot(r)
    reg("D03", "highlight", 4, "Text that overlaps the figure region", 2.0)
    r = pymupdf.Rect(410, 110, 520, 220)
    page.add_text_annot(r, "Annotation beside a figure")
    reg("D04", "text", 4, "Annotation beside a figure", 4.0)

    # ── page 5: symbols + internal link + cross-page reference ───────────
    page = doc.new_page()
    page.insert_text(
        (72, 90),
        "Formula symbols: alpha beta gamma delta",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    page.insert_text(
        (72, 120),
        "See page 3 for the cross-line example.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    r = pymupdf.Rect(72, 84, 400, 96)
    page.add_highlight_annot(r)
    reg("E01", "highlight", 5, "Formula symbols: alpha beta", 2.0)
    r = pymupdf.Rect(72, 114, 400, 126)
    page.insert_link(
        {"kind": pymupdf.LINK_URI, "from": r, "uri": "https://example.com/internal"}
    )
    reg("E02", "link", 5, "See page 3 for the cross-line", 2.0)
    r = pymupdf.Rect(72, 84, 200, 96)
    page.add_underline_annot(r)
    reg("E03", "underline", 5, "Formula symbols", 2.0)
    r = pymupdf.Rect(72, 84, 400, 96)
    page.add_text_annot(r, "Symbol line annotation")
    reg("E04", "text", 5, "Symbol line annotation", 4.0)

    # ── page 6: negative control (no annotations) ────────────────────────
    page = doc.new_page()
    page.insert_text(
        (72, 90),
        "This page intentionally carries no annotations.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    page.insert_text(
        (72, 120),
        "It is the negative control for the corpus.",
        fontname=LATIN_FONT,
        fontsize=12,
    )
    reg("F00", "none", 6, "negative control", 0.0)

    doc.save(str(pdf))
    doc.close()

    # verify source-side inventory
    verify = pymupdf.open(str(pdf))
    inv = {}
    total = 0
    for pno, pg in enumerate(verify, start=1):
        anns = list(pg.annots() or [])
        links = list(pg.get_links())
        inv[pno] = {
            "annots": [a.type[1] for a in anns],
            "links": [lk["kind"] for lk in links],
        }
        total += len(anns) + len(links)
    verify.close()

    corpus = {
        "corpus": "7k1-annotation-evidence",
        "source_pdf": str(pdf),
        "pages": 6,
        "cases": CASES,
        "source_inventory": inv,
        "source_annotation_count": total,
        "note": (
            "expected.present=True records the SOURCE contract. The current "
            "pipeline is expected to fail it (deliberate /Annots stripping in "
            "babeldoc fix_null_xref); that divergence is the 7K-1 finding."
        ),
    }
    (OUT / "annotation_corpus.json").write_text(
        json.dumps(corpus, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"built {pdf.name}: pages=6 annots={total}")
    print(json.dumps(inv, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
