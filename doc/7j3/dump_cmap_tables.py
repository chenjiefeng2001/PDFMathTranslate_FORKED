"""Dump every cmap subtable of the embedded Arial fonts in the mono PDF."""

from __future__ import annotations

import re
import struct

import pymupdf

MONO = r"pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-mono.pdf"


def gid_for(sub: bytes, fmt: int, c: int) -> int | None:
    try:
        if fmt == 4:
            seg_count_x2 = struct.unpack_from(">H", sub, 6)[0]
            seg_count = seg_count_x2 // 2
            end_code = struct.unpack_from(f">{seg_count}H", sub, 14)
            start_code = struct.unpack_from(f">{seg_count}H", sub, 16 + seg_count_x2)
            id_delta = struct.unpack_from(f">{seg_count}h", sub, 16 + 2 * seg_count_x2)
            iro_pos = 16 + 3 * seg_count_x2
            id_range_offset = struct.unpack_from(f">{seg_count}H", sub, iro_pos)
            for i in range(seg_count):
                if start_code[i] <= c <= end_code[i]:
                    if id_range_offset[i] == 0:
                        return (c + id_delta[i]) & 0xFFFF
                    abs_off = (
                        iro_pos + 2 * i + id_range_offset[i] + 2 * (c - start_code[i])
                    )
                    if abs_off + 2 > len(sub):
                        return None
                    g = struct.unpack_from(">H", sub, abs_off)[0]
                    if g != 0:
                        g = (g + id_delta[i]) & 0xFFFF
                    return g
            return None
        if fmt == 12:
            n = struct.unpack_from(">I", sub, 12)[0]
            for i in range(n):
                start, end, sg = struct.unpack_from(">III", sub, 16 + 12 * i)
                if start <= c <= end:
                    g = sg + (c - start)
                    return g if g != 0 else None
            return None
        if fmt == 6:
            first = struct.unpack_from(">H", sub, 6)[0]
            count = struct.unpack_from(">H", sub, 8)[0]
            if first <= c < first + count:
                g = struct.unpack_from(">H", sub, 10 + 2 * (c - first))[0]
                return g if g != 0 else None
            return None
        return None
    except Exception:
        return None


def get_ttf(doc: pymupdf.Document, wrapper_xref: int) -> bytes:
    fd = doc.xref_get_key(wrapper_xref, "DescendantFonts")
    fx = int(re.search(r"\d+", fd[1]).group())
    ff = doc.xref_get_key(fx, "FontDescriptor/FontFile2")
    ffx = int(re.search(r"\d+", ff[1]).group())
    return doc.xref_stream(ffx)


def main() -> int:
    doc = pymupdf.open(MONO)
    page = doc[2]
    targets = {f[3]: f[0] for f in page.get_fonts() if "Arial" in f[3]}
    for base, xref in targets.items():
        ttf = get_ttf(doc, xref)
        _, num_tables = struct.unpack_from(">HH", ttf, 4)
        cmap_off = None
        for i in range(num_tables):
            rec = 12 + 16 * i
            if ttf[rec : rec + 4] == b"cmap":
                cmap_off, cmap_len = struct.unpack_from(">II", ttf, rec + 8)
        cmap = ttf[cmap_off : cmap_off + cmap_len]
        _, nrec = struct.unpack_from(">HH", cmap, 0)
        print(f"\n{base} (wrapper xref {xref}) ttf={len(ttf)}B")
        for i in range(nrec):
            platform, encoding, off = struct.unpack_from(">HHI", cmap, 4 + 8 * i)
            sub = cmap[off:]
            fmt = struct.unpack_from(">H", sub, 0)[0]
            gids = {
                hex(c): gid_for(sub, fmt, c)
                for c in (0x54, 0x61, 0x74, 0x79, 0x6C, 0x26)
            }
            print(
                f"  plat={platform} enc={encoding} fmt={fmt}: "
                + " ".join(f"{k}->{v}" for k, v in gids.items())
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
