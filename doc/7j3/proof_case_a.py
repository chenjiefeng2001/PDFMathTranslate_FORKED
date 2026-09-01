"""7J-3B Case A proof: ToUnicode keys must be content-stream CIDs, not subset GIDs.

Demonstrates on the real mono/dual artifacts (AI book):
  - current /ToUnicode keys are GID space (post-subset renumber)
  - embedded TTF cmap gives authoritative CID -> GID
  - rebuilding ToUnicode with keys = CID recovers "Taylor & Francis" via an
    independent reader (PyMuPDF), while glyphs (content stream) stay untouched.
"""

from __future__ import annotations

import os
import re
import struct
import sys

import pymupdf

MONO = r"pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-mono.pdf"
DUAL = r"pdf2zh_files/AI for Games and Animation A Cognitive Modeling Approach John David Fun_4ca3f7b5-dual.pdf"


def parse_truetype_cmap(data: bytes) -> dict[int, int]:
    """Parse TTF cmap table -> {char_code (CID for Identity-H): glyph_id}.

    Reads the SFNT table directory directly (freetype-py does not expose
    ``get_sfnt_table``), then parses the cmap subtable (formats 4, 6, 12),
    picking the best Unicode subtable.
    """
    if len(data) < 12:
        return {}
    (_, num_tables) = struct.unpack_from(">HH", data, 4)
    cmap_data: bytes | None = None
    for i in range(num_tables):
        rec = 12 + 16 * i
        tag = data[rec : rec + 4]
        if tag == b"cmap":
            offset, length = struct.unpack_from(">II", data, rec + 8)
            if offset + length <= len(data):
                cmap_data = data[offset : offset + length]
            break
    if cmap_data is None:
        return {}
    (_, num_records) = struct.unpack_from(">HH", cmap_data, 0)
    best: tuple[int, bytes] | None = None
    for i in range(num_records):
        platform, encoding, offset = struct.unpack_from(">HHI", cmap_data, 4 + 8 * i)
        if platform == 3 and encoding == 10:
            best = (2, cmap_data[offset:])
            break
        if platform == 3 and encoding == 1:
            if best is None or best[0] < 1:
                best = (1, cmap_data[offset:])
        elif platform == 0:
            if best is None or best[0] < 0:
                best = (0, cmap_data[offset:])
    if best is None:
        return {}
    sub = best[1]
    if len(sub) < 2:
        return {}
    fmt = struct.unpack_from(">H", sub, 0)[0]
    if fmt == 4:
        return _parse_cmap4(sub)
    if fmt == 6:
        return _parse_cmap6(sub)
    if fmt == 12:
        return _parse_cmap12(sub)
    return {}


def _parse_cmap4(sub: bytes) -> dict[int, int]:
    seg_count_x2 = struct.unpack_from(">H", sub, 6)[0]
    seg_count = seg_count_x2 // 2
    end_code = struct.unpack_from(f">{seg_count}H", sub, 14)
    start_code = struct.unpack_from(f">{seg_count}H", sub, 16 + seg_count_x2)
    id_delta = struct.unpack_from(f">{seg_count}h", sub, 16 + 2 * seg_count_x2)
    id_range_off_pos = 16 + 3 * seg_count_x2
    id_range_offset = struct.unpack_from(f">{seg_count}H", sub, id_range_off_pos)
    cmap: dict[int, int] = {}
    for i in range(seg_count):
        for c in range(start_code[i], end_code[i] + 1):
            if id_range_offset[i] == 0:
                gid = (c + id_delta[i]) & 0xFFFF
            else:
                abs_off = id_range_off_pos + 2 * i + id_range_offset[i] + 2 * (c - start_code[i])
                if abs_off + 2 > len(sub):
                    continue
                gid = struct.unpack_from(">H", sub, abs_off)[0]
                if gid != 0:
                    gid = (gid + id_delta[i]) & 0xFFFF
            if gid != 0:
                cmap[c] = gid
    return cmap


def _parse_cmap6(sub: bytes) -> dict[int, int]:
    first = struct.unpack_from(">H", sub, 6)[0]
    count = struct.unpack_from(">H", sub, 8)[0]
    gids = struct.unpack_from(f">{count}H", sub, 10)
    return {first + i: g for i, g in enumerate(gids) if g != 0}


def _parse_cmap12(sub: bytes) -> dict[int, int]:
    n_groups = struct.unpack_from(">I", sub, 12)[0]
    cmap: dict[int, int] = {}
    for i in range(n_groups):
        start, end, start_gid = struct.unpack_from(">III", sub, 16 + 12 * i)
        for c in range(start, end + 1):
            gid = start_gid + (c - start)
            if gid != 0:
                cmap[c] = gid
    return cmap


def parse_tounicode_cmap(data: bytes) -> dict[int, int]:
    cmap: dict[int, int] = {}

    def parse_mapping(text: bytes) -> list[int]:
        return [int(x, 16) for x in re.findall(rb"<([0-9a-fA-F]+)>", text)]

    for m in re.finditer(rb"\s+beginbfrange\s*(?P<r>(<[0-9a-fA-F]+>\s*)+)endbfrange\s+", data):
        vals = parse_mapping(m.group("r"))
        for start, stop, value in zip(vals[::3], vals[1::3], vals[2::3]):
            for c in range(start, stop + 1):
                cmap[c] = value + c - start
    for m in re.finditer(rb"\s+beginbfchar\s*(?P<c>(<[0-9a-fA-F]+>\s*)+)endbfchar", data):
        vals = parse_mapping(m.group("c"))
        for k, v in zip(vals[::2], vals[1::2]):
            cmap[k] = v
    return cmap


def fix_one_font(doc: pymupdf.Document, xref: int) -> dict:
    """Rebuild ToUnicode keys onto the CID space. Returns diagnostics."""
    t_type, t_val = doc.xref_get_key(xref, "ToUnicode")
    f_type, f_val = doc.xref_get_key(xref, "DescendantFonts")
    if t_type != "xref" or f_type != "array":
        return {"skipped": "no ToUnicode/DescendantFonts"}
    t_xref = int(re.search(r"\d+", t_val).group())
    f_xref = int(re.search(r"\d+", f_val).group())
    ff_type, ff_val = doc.xref_get_key(f_xref, "FontDescriptor/FontFile2")
    if ff_type != "xref":
        return {"skipped": "no FontFile2"}
    ff_xref = int(re.search(r"\d+", ff_val).group())
    tu_raw = doc.xref_stream(t_xref)
    ttf = doc.xref_stream(ff_xref)
    cur = parse_tounicode_cmap(tu_raw)
    cid2gid = parse_truetype_cmap(ttf)
    gid2cid: dict[int, int] = {}
    for cid, gid in cid2gid.items():
        gid2cid.setdefault(gid, cid)
    fixed: dict[int, int] = {}
    for k, v in cur.items():
        fixed[gid2cid.get(k, k)] = v
    return {
        "t_xref": t_xref,
        "current": cur,
        "fixed": fixed,
        "gid2cid": gid2cid,
    }


def build_cmap_text(fixed: dict[int, int]) -> str:
    lines: list[str] = []
    items = sorted(fixed.items())
    for i in range(0, len(items), 100):
        block = items[i : i + 100]
        lines.append(f"{len(block)} beginbfchar")
        for k, v in block:
            if v < 0x10000:
                lines.append(f"<{k:04x}><{v:04x}>")
            else:
                v -= 0x10000
                high = 0xD800 + (v >> 10)
                low = 0xDC00 + (v & 0x3FF)
                lines.append(f"<{k:04x}><{high:04x}{low:04x}>")
        lines.append("endbfchar")
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        + "\n".join(lines)
        + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    )


def nul_stats(doc: pymupdf.Document, page_index: int) -> int:
    return doc[page_index].get_text().count("\x00")


def main() -> int:
    out_dir = "doc/7j3/out"
    os.makedirs(out_dir, exist_ok=True)

    for label, pdf_path, page_index, want_line in (
        ("mono", MONO, 2, "Taylor & Francis"),
        ("dual", DUAL, 4, "Taylor & Francis"),
    ):
        print(f"\n===== {label} p{page_index + 1} =====")
        doc = pymupdf.open(pdf_path)
        before = nul_stats(doc, page_index)
        # Iterate every font in the doc, exactly like reproduce_cmap does.
        font_set: dict[int, str] = {}
        for page in doc:
            for f in page.get_fonts():
                if f[1] == "ttf" and "Identity-H" in (f[5] or ""):
                    font_set.setdefault(f[0], f[3])
        changed = 0
        sample: dict[str, str] = {}
        for xref, base in sorted(font_set.items()):
            diag = fix_one_font(doc, xref)
            if "skipped" in diag:
                continue
            cur, fixed, gid2cid = diag["current"], diag["fixed"], diag["gid2cid"]
            if base in ("DQQQAE+Arial Bold", "UMHYOM+Arial Bold", "DGPIWC+Arial Regular", "GTCLZO+Arial Regular"):
                g2c_of_cur = {hex(k): hex(gid2cid[k]) for k in sorted(cur) if k in gid2cid}
                fixed_sample = {hex(k): chr(v) for k, v in list(fixed.items())[:6]}
                sample[base] = (
                    f"cur_keys={[hex(k) for k in sorted(cur)[:6]]} "
                    f"gid2cid_of_cur={g2c_of_cur} "
                    f"fixed_keys={[hex(k) for k in sorted(fixed)[:6]]} "
                    f"fixed_sample={fixed_sample}"
                )
            doc.update_stream(diag["t_xref"], build_cmap_text(fixed).encode())
            changed += 1
        fixed_path = os.path.join(out_dir, f"{label}_fixed.pdf")
        doc.save(fixed_path, garbage=3, deflate=True)
        doc.close()

        for base, s in sample.items():
            print(f"  {base}: {s}")

        check = pymupdf.open(fixed_path)
        after = nul_stats(check, page_index)
        text = check[page_index].get_text()
        ok = want_line in text or "taylorandfrancis" in text.lower()
        print(f"fonts fixed: {changed}  NUL before={before} after={after}  recovered={ok!r}")
        if ok:
            for line in text.splitlines():
                if "Taylor" in line or "francis" in line.lower():
                    print("   line:", repr(line[:120]))
        check.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
