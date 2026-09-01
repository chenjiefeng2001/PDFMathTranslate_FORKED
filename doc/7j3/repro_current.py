"""7J-3B — reproduce the Case A ToUnicode defect with the CURRENT babeldoc.

Simulates the exact font-handling steps BabelDOC 0.6.4 performs at the end of
every translation (subset_fonts then fix_cmap -> reproduce_cmap), on a copy of
the AI source PDF, and inspects the footer text layer.  The full translation
is not needed: the publisher footer is passthrough text whose font objects
flow through the same subset + ToUnicode-rewrite path.

Variants:
  A. native  : current babeldoc reproduce_cmap  (expect broken text layer)
  B. fixed   : reproduce_cmap + CID-space remap  (expect recovered)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "doc" / "7j3"))

from proof_case_a import build_cmap_text, fix_one_font  # noqa: E402

SRC = ROOT / "pdf2zh_files" / "AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5.pdf"
PAGE = 2  # 0-based: source p3

from babeldoc.format.pdf.document_il.backend.pdf_creater import reproduce_cmap  # noqa: E402


def run(variant: str, work: Path) -> dict:
    pdf = pymupdf.open(str(SRC))
    pdf.subset_fonts(fallback=False)
    mid = work / f"subset_{variant}.pdf"
    pdf.save(str(mid), garbage=3, deflate=True)
    pdf.close()

    pdf = pymupdf.open(str(mid))
    if variant == "native":
        pdf = reproduce_cmap(pdf)
    else:
        # fixed: run reproduce_cmap but with CID-space ToUnicode keys
        for page in pdf:
            for f in page.get_fonts():
                if f[1] == "ttf" and "Identity-H" in (f[5] or ""):
                    diag = fix_one_font(pdf, f[0])
                    if "skipped" not in diag:
                        pdf.update_stream(diag["t_xref"], build_cmap_text(diag["fixed"]).encode())
    out = work / f"out_{variant}.pdf"
    pdf.save(str(out), garbage=3, deflate=True)
    pdf.close()

    check = pymupdf.open(str(out))
    text = check[PAGE].get_text()
    nul = text.count("\x00")
    taylor = "Taylor" in text or "taylorandfrancis" in text.lower()
    sample = None
    for line in text.splitlines():
        if "Taylor" in line or "francis" in line.lower() or "\x00" in line:
            sample = line
            break
    check.close()
    return {"nul": nul, "taylor": taylor, "sample": repr(sample[:90]) if sample else None}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="7j3_repro_") as td:
        work = Path(td)
        for variant in ("native", "fixed"):
            res = run(variant, work)
            print(f"variant={variant:7s} NUL={res['nul']:3d} taylor={res['taylor']!s:5s} line={res['sample']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
