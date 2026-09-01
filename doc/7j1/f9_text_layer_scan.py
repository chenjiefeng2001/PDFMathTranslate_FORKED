# -*- coding: utf-8 -*-
"""7J-1C — F9-class passthrough text-layer corruption scope scan (evidence-only).

After 7I, real-translation artifacts (dual/mono per book) still show a text-
layer corruption on passthrough (non-translated) content:

    content stream Tj  →  correct ASCII glyph bytes
    visual render      →  correct glyphs (ink present)
    extracted text     →  GBK-mojibake (dual) or NUL (mono)   ← text layer broken

This is a ToUnicode/CMap fidelity defect of class F9 (text-vs-visual font /
text consistency). This scan quantifies the affected pages and characters
across all four real-translation books without writing any PDF.

Signals measured on the mono (rendered) output:
    S1  NUL bytes in extracted text (unrecoverable deltas)
    S2  double-byte mojibake characters in known-ASCII regions
    S3  CJK delta between dual-translated half and mono (translation loss)
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pymupdf  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

BOOKS = [
    "AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5",
    "Game Physics David H. Eberly z-library.sk 1lib.sk z-lib.sk",
    "Large-Scale C Volume I_ Process and Architecture -- јohn Lakos -- 2020 _2c3bdba4",
    "Networking and Online Games Understanding and Engineering Multiplayer I_1eed56a6",
]

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _cjk(t: str) -> int:
    return len(CJK_RE.findall(t))


def main() -> int:
    out_dir = ROOT / "doc" / "7j1"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict = {}

    for name in BOOKS:
        src = pymupdf.open(ROOT / "pdf2zh_files" / f"{name}.pdf")
        dual = pymupdf.open(ROOT / "pdf2zh_files" / f"{name}-dual.pdf")
        mono = pymupdf.open(ROOT / "pdf2zh_files" / f"{name}-mono.pdf")
        n = min(len(src), len(mono), len(dual) // 2)

        nul_pages = []
        nul_chars = 0
        gbk_ascii_pages = []
        cjk_delta_pages = []

        for k in range(n):
            m_txt = mono[k].get_text()
            d_tr = dual[2 * k + 1].get_text()

            # S1: NUL bytes = renderer emitted wrong ToUnicode for passthrough text
            nul = m_txt.count("\x00")
            if nul:
                nul_pages.append({"page": k + 1, "nul": nul})
                nul_chars += nul

            # S2: mojibake: a run that should be ASCII but extracted as
            # double-byte junk (surrogate / GBK-in-unicode range)
            moji = re.findall(r"[\u4e00-\u9fff]\u0000|[\U00010000-\U0010FFFF]", m_txt)
            if moji:
                gbk_ascii_pages.append({"page": k + 1, "moji": len(moji)})

            # S3: translation loss — CJK present in dual-translated half but
            # missing from mono render
            d_cjk = _cjk(d_tr)
            m_cjk = _cjk(m_txt)
            if d_cjk != m_cjk:
                cjk_delta_pages.append(
                    {"page": k + 1, "dual_tr_cjk": d_cjk, "mono_cjk": m_cjk}
                )

        results[name] = {
            "pages": n,
            "pages_with_nul": len(nul_pages),
            "total_nul_chars": nul_chars,
            "nul_samples": nul_pages[:6],
            "pages_with_mojibake": len(gbk_ascii_pages),
            "mojibake_samples": gbk_ascii_pages[:6],
            "pages_with_cjk_delta": len(cjk_delta_pages),
            "cjk_delta_samples": cjk_delta_pages[:6],
        }
        print(f"[{name[:44]}] pages={n} NUL={len(nul_pages)} "
              f"moji={len(gbk_ascii_pages)} cjk_delta={len(cjk_delta_pages)}")
        src.close()
        dual.close()
        mono.close()

    with (out_dir / "f9_text_layer_scan.json").open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nwrote {out_dir / 'f9_text_layer_scan.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())