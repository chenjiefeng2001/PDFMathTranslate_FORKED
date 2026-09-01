# -*- coding: utf-8 -*-
"""7J-1 — Real-translation 3-stage E2E residual scan (evidence-only).

Aligns the real translated artifacts (source / dual / mono per book) into
``source -> translated -> rendered`` triples WITHOUT any identity shortcut:

    dual[2k]   = source page k   (verify: near-identical to source[k])
    dual[2k+1] = translated page (the "translated" stage)
    mono[k]    = rendered page   (the "rendered" stage)

Then measures per-page divergence:

    D1  source vs dual-source half   (alignment sanity; pages that break the
                                      alternating assumption are flagged)
    D2  translated vs rendered       (CJK char delta, text-char delta,
                                      missing/extra blocks => F7/F10 candidates)

No PDF is written; no production code is touched. Evidence-only scan.
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pymupdf  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
try:
    pymupdf.TOOLS.mupdf_display_errors(False)  # suppress C-level parser spam
except Exception:  # noqa: BLE001
    pass

BOOKS = [
    "AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5",
    "Game Physics David H. Eberly z-library.sk 1lib.sk z-lib.sk",
    "Large-Scale C Volume I_ Process and Architecture -- јohn Lakos -- 2020 _2c3bdba4",
    "Networking and Online Games Understanding and Engineering Multiplayer I_1eed56a6",
]

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _cjk(t: str) -> int:
    return len(CJK_RE.findall(t))


def _words(t: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+", t))


def main() -> int:
    out = ROOT / "doc" / "7j1"
    out.mkdir(parents=True, exist_ok=True)
    summary: dict = {}

    for name in BOOKS:
        src_p = ROOT / "pdf2zh_files" / f"{name}.pdf"
        dual_p = ROOT / "pdf2zh_files" / f"{name}-dual.pdf"
        mono_p = ROOT / "pdf2zh_files" / f"{name}-mono.pdf"
        if not (src_p.exists() and dual_p.exists() and mono_p.exists()):
            print(f"[skip] missing artifacts for {name}")
            continue

        src = pymupdf.open(src_p)
        dual = pymupdf.open(dual_p)
        mono = pymupdf.open(mono_p)
        n = min(len(src), len(mono), len(dual) // 2)

        rows = []
        for k in range(n):
            s_txt = src[k].get_text()
            d_src_txt = dual[2 * k].get_text()
            d_tr_txt = dual[2 * k + 1].get_text()
            m_txt = mono[k].get_text()

            # D1: is the dual's source half really the source?
            align_ratio = min(len(d_src_txt), len(s_txt)) / max(
                1, max(len(d_src_txt), len(s_txt))
            )
            # D2: translated (dual odd) vs rendered (mono)
            dc = _cjk(d_tr_txt)
            mc = _cjk(m_txt)
            dlen = len(d_tr_txt)
            mlen = len(m_txt)
            rows.append(
                {
                    "src_page": k + 1,
                    "align_ratio": round(align_ratio, 4),
                    "dual_tr_cjk": dc,
                    "mono_cjk": mc,
                    "dual_tr_chars": dlen,
                    "mono_chars": mlen,
                    "cjk_delta": dc - mc,
                    "char_delta": dlen - mlen,
                }
            )

        # Roll up: alignment failures + D2 divergence distribution
        bad_align = [r for r in rows if r["align_ratio"] < 0.6]
        cjk_delta_nonzero = [r for r in rows if abs(r["cjk_delta"]) > 0]
        char_delta_large = [
            r for r in rows if abs(r["char_delta"]) > max(50, 0.15 * r["mono_chars"])
        ]
        summary[name] = {
            "pages_aligned": len(rows),
            "bad_alignment": len(bad_align),
            "bad_alignment_pages": [r["src_page"] for r in bad_align][:20],
            "cjk_delta_nonzero": len(cjk_delta_nonzero),
            "cjk_delta_samples": [
                {k_: r[k_] for k_ in ("src_page", "dual_tr_cjk", "mono_cjk", "cjk_delta")}
                for r in cjk_delta_nonzero[:12]
            ],
            "char_delta_large": len(char_delta_large),
            "char_delta_samples": [
                {k_: r[k_] for k_ in ("src_page", "dual_tr_chars", "mono_chars", "char_delta")}
                for r in char_delta_large[:12]
            ],
        }
        print(f"[{name[:44]}] aligned={len(rows)} bad_align={len(bad_align)} "
              f"cjk_delta>0={len(cjk_delta_nonzero)} char_delta_large={len(char_delta_large)}")
        src.close()
        dual.close()
        mono.close()

    with (out / "real_translation_scan.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nwrote {out / 'real_translation_scan.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())