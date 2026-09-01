"""Inspect the fresh E2E output: footer ToUnicode key space + extraction.

Answers:
  1. Is the fresh mono's footer text layer broken? (NUL / mojibake)
  2. If broken, is the ToUnicode key space GID (mismatched) or CID?
  3. Does the CID-space remap (fix_one_font) repair extraction?
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "doc" / "7j3"))

from proof_case_a import (
    build_cmap_text,
    fix_one_font,
    parse_tounicode_cmap,
)  # noqa: E402

OUT = ROOT / "doc" / "7j3" / "out_e2e"


def font_usage(page) -> dict[int, str]:
    return {f[0]: f[3] for f in page.get_fonts() if "Identity-H" in (f[5] or "")}


def main() -> int:
    monos = sorted(OUT.glob("*mono.pdf"))
    if not monos:
        print("no mono output yet")
        return 1
    pdf_path = monos[0]
    print(f"=== {pdf_path.name} ===")
    doc = pymupdf.open(str(pdf_path))
    # find a page with footer NULs
    target_page = None
    for pno in range(min(8, len(doc))):
        txt = doc[pno].get_text()
        if "\x00" in txt or "呡" in txt:
            target_page = pno
            break
    if target_page is None:
        target_page = 0
        print("no NUL footer page found in first 8 pages; using page 1")

    text = doc[target_page].get_text()
    nul = text.count("\x00")
    print(f"page {target_page + 1}: NUL={nul}")
    for line in text.splitlines():
        if "\x00" in line or "Taylor" in line or "francis" in line.lower():
            print("   line:", repr(line[:100]))

    # which fonts are used on this page, and their ToUnicode key spaces
    page = doc[target_page]
    for f in page.get_fonts():
        if f[1] == "ttf":
            xref = f[0]
            t_type, t_val = doc.xref_get_key(xref, "ToUnicode")
            if t_type != "xref":
                continue
            t_xref = int(re.search(r"\d+", t_val).group())
            cur = parse_tounicode_cmap(doc.xref_stream(t_xref))
            sample = {hex(k): chr(v) for k, v in list(cur.items())[:5]}
            print(f"  font={f[3]!r} xref={xref} keys={len(cur)} sample={sample}")

    # apply fix and re-check
    changed = 0
    for pno in range(len(doc)):
        for f in doc[pno].get_fonts():
            if f[1] == "ttf" and "Identity-H" in (f[5] or ""):
                diag = fix_one_font(doc, f[0])
                if "skipped" not in diag:
                    doc.update_stream(
                        diag["t_xref"], build_cmap_text(diag["fixed"]).encode()
                    )
                    changed += 1
    fixed_path = OUT / f"{pdf_path.stem}_cidspace.pdf"
    doc.save(str(fixed_path), garbage=3, deflate=True)
    doc.close()

    check = pymupdf.open(str(fixed_path))
    text2 = check[target_page].get_text()
    print(f"after fix: NUL={text2.count(chr(0))} fonts_changed={changed}")
    for line in text2.splitlines():
        if "Taylor" in line or "francis" in line.lower():
            print("   line:", repr(line[:110]))
    check.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
