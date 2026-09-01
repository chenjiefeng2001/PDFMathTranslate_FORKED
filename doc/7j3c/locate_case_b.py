# -*- coding: utf-8 -*-
"""7J-3C — locate Case B code-point-loss sites in the artifacts.

For each known site (AI p157 ``OBJECTxN —\\x00 Rn``, GP p37 ``Ana¨\\x00s``,
LSC p908 ``\\x00 2``) find the span in mono/dual, then classify the loss:

  - content-stream CIDs around the NUL position
  - ToUnicode entries for those CIDs (mapped to U+0000? absent?)
  - embedded glyph coverage for the CID (glyph exists or not)

Output feeds the first-divergence decision (translation vs layout vs emitter).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "doc" / "7j3"))


BOOKS = {
    "ai": r"pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-{kind}.pdf",
    "gp": r"pdf2zh_files/Game Physics David H. Eberly z-library.sk 1lib.sk z-lib.sk-{kind}.pdf",
    "lsc": r"pdf2zh_files/Large-Scale C Volume I_ Process and Architecture -- јohn Lakos -- 2020 _2c3bdba4-{kind}.pdf",
}

SITES = [
    ("ai", "mono", 156, "OBJECT"),
    ("gp", "mono", 36, "\x00"),
    ("lsc", "mono", 907, "\x00"),
]


def parse_hex_strings(raw: bytes) -> list[int]:
    """All 1- and 2-byte hex codes in a content stream, in order."""
    out = []
    for m in re.finditer(rb"<([0-9a-fA-F]+)>", raw):
        h = m.group(1).decode()
        code = int(h, 16)
        out.append(code)
    return out


def dump_site(book: str, kind: str, pno: int, want: str) -> None:
    path = ROOT / BOOKS[book].format(kind=kind)
    if not path.exists():
        print(f"  ! missing {path.name}")
        return
    doc = pymupdf.open(str(path))
    page = doc[pno]
    text = page.get_text()
    nul_positions = [i for i, ch in enumerate(text) if ch == "\x00"]
    print(f"\n  [{book} {kind} p{pno + 1}] NUL count={len(nul_positions)}")

    d = page.get_text("rawdict")
    for block in d["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                s = "".join(ch["c"] for ch in span.get("chars", []))
                if "\x00" in s and (want == "\x00" or want in s):
                    print(
                        f"    span font={span['font']!r} size={span['size']:.1f} text={s[:70]!r}"
                    )
                    # which char indexes are NUL
                    nul_idx = [i for i, ch in enumerate(s) if ch == "\x00"]
                    print(f"      nul char offsets in span: {nul_idx}")
                    # content stream codes for this span region: match by origin y
                    if span["chars"]:
                        origin = span["chars"][0]["origin"]
                        print(f"      origin={origin}")

    # content stream codes near the span y
    raw = page.read_contents()
    codes = parse_hex_strings(raw)
    print(
        f"      content-stream hex codes on page: {len(codes)} (first 40: {[hex(c) for c in codes[:40]]})"
    )
    doc.close()


def main() -> int:
    for book, kind, pno, want in SITES:
        dump_site(book, kind, pno, want)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
