# -*- coding: utf-8 -*-
"""7J-3C — CID + ToUnicode forensics for the Case B NUL units.

For each NUL site, parse the page content stream text runs (tracking
Tf/Td/Tm), locate the run at the NUL's origin, and dump:
  - the run's hex codes (CIDs)
  - the used font's /ToUnicode mapping for those CIDs (U+0000? absent? real?)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "doc" / "7j3"))

from proof_case_a import parse_tounicode_cmap  # noqa: E402

SITES = [
    (
        "ai",
        r"pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-mono.pdf",
        156,
        (302.93, 543.76),
    ),
    (
        "gp",
        r"pdf2zh_files/Game Physics David H. Eberly z-library.sk 1lib.sk z-lib.sk-mono.pdf",
        36,
        (234.53, 431.58),
    ),
    (
        "lsc",
        r"pdf2zh_files/Large-Scale C Volume I_ Process and Architecture -- јohn Lakos -- 2020 _2c3bdba4-mono.pdf",
        907,
        (274.69, 585.54),
    ),
]


def walk_text_runs(raw: bytes):
    """Yield (font, size, x, y, codes) for every text-showing run."""
    font = None
    size = 0.0
    pos = (0.0, 0.0)
    tokens = re.finditer(
        rb"/(\w+)\s+([\d.]+)\s+Tf"
        rb"|(-?[\d.]+)\s+(-?[\d.]+)\s+Td"
        rb"|([\d.]+)\s+0\s+0\s+([\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+Tm"
        rb"|<([0-9a-fA-F]+)>\s*Tj"
        rb"|\[(.*?)\]\s*TJ"
        rb"|\(([^)]*)\)\s*Tj",
        raw,
    )
    for m in tokens:
        if m.group(1):
            font, size = m.group(1).decode(), float(m.group(2))
        elif m.group(3):
            pos = (float(m.group(3)), float(m.group(4)))
        elif m.group(5):
            pos = (float(m.group(7)), float(m.group(8)))
        elif m.group(9):
            code = int(m.group(9), 16)
            yield font, size, pos, [code]
        elif m.group(10):
            codes = [int(x, 16) for x in re.findall(rb"<([0-9a-fA-F]+)>", m.group(10))]
            yield font, size, pos, codes
        elif m.group(11):
            yield font, size, pos, list(m.group(11))


def to_unicode_for_font(doc, font_resource_name: str, page) -> dict[int, int]:
    """ToUnicode cmap of the font resource used on the page."""
    for f in page.get_fonts():
        if f[4] == font_resource_name:  # resource name, e.g. 'noto'
            t_type, t_val = doc.xref_get_key(f[0], "ToUnicode")
            if t_type != "xref":
                return {}
            t_xref = int(re.search(r"\d+", t_val).group())
            return parse_tounicode_cmap(doc.xref_stream(t_xref))
    return {}


def main() -> int:
    for label, path, pno, (x0, y0) in SITES:
        doc = pymupdf.open(ROOT / path)
        page = doc[pno]
        raw = page.read_contents()
        found = False
        for font, size, pos, codes in walk_text_runs(raw):
            # runs near the target origin (y tolerance ~1.0, x within run)
            if abs(pos[1] - y0) < 1.2:
                print(
                    f"[{label} p{pno + 1}] font={font} size={size:.1f} pos=({pos[0]:.1f},{pos[1]:.1f}) "
                    f"ncodes={len(codes)} codes={[hex(c) for c in codes]}"
                )
                tu = to_unicode_for_font(doc, font, page)
                if not tu:
                    print(f"    no ToUnicode for font {font}")
                else:
                    for c in codes[:40]:
                        v = tu.get(c)
                        print(
                            f"    CID {c:#04x} -> unicode {('U+%04X' % v) + ' ' + chr(v) if v is not None else 'ABSENT'}"
                        )
                found = True
        if not found:
            print(f"[{label} p{pno + 1}] no run near ({x0:.1f},{y0:.1f})")
        doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
