import sys, io, json, pathlib
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import pymupdf

BASE = pathlib.Path(
    r"C:\Users\14977\AppData\Local\Temp\opencode\load50\output\magicpdf"
)
raw = json.loads(
    (BASE / "The Art of Multiprocessor Programming, 2e_magicpdf.json").read_text(
        encoding="utf-8"
    )
)
doc = json.loads(
    (BASE / "The Art of Multiprocessor Programming, 2e_document.json").read_text(
        encoding="utf-8"
    )
)
plan = json.loads(
    pathlib.Path("doc/7p0-load50/render_plan.json").read_text(encoding="utf-8")
)
SRC = pathlib.Path(
    r"C:\Users\14977\source\repos\PDFMathTranslate_FORKED\tests\file"
    r"\The Art of Multiprocessor Programming, 2e.pdf"
)
src = pymupdf.open(str(SRC))

PAGES = [14, 15, 21]  # 0-based; these showed ratio 0.77
for pno in PAGES:
    print(f"\n{'='*78}\n0-based page {pno}  (PDF page {pno+1})\n{'='*78}")

    # --- ring 0: the source PDF itself
    szs = Counter()
    for b in src[pno].get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                if len((s.get("text") or "").strip()) >= 8:
                    szs[round(s.get("size", 0), 2)] += 1
    src_modal = szs.most_common(1)[0][0] if szs else None
    print(f"ring 0  source PDF span sizes : {szs.most_common(4)}")

    # --- ring 1: raw MinerU output (the *_magicpdf.json dump)
    rp = raw[pno]
    span_sizes, line_bbox_h = Counter(), []
    for b in rp.get("blocks", []):
        for l in b.get("lines", []):
            bb = l.get("bbox") or []
            if len(bb) == 4:
                line_bbox_h.append(round(bb[3] - bb[1], 2))
            for s in l.get("spans", []):
                span_sizes[round(s.get("size", 0), 2)] += 1
    print(f"ring 1  MinerU span sizes     : {span_sizes.most_common(4)}")
    if line_bbox_h:
        print(
            f"ring 1  MinerU line bbox heights: modal {Counter(line_bbox_h).most_common(3)}"
        )

    # --- ring 2: our document.json (after adapter + layout passes)
    dp = doc["pages"][pno]
    meta_fs, meta_line_sizes, dspan = Counter(), Counter(), Counter()
    for b in dp.get("blocks", []):
        md = b.get("metadata") or {}
        if md.get("font_size"):
            meta_fs[round(md["font_size"], 2)] += 1
        for v in md.get("line_sizes") or []:
            meta_line_sizes[round(float(v), 2)] += 1
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                if s.get("size"):
                    dspan[round(s["size"], 2)] += 1
    print(f"ring 2  doc.meta.font_size    : {meta_fs.most_common(4)}")
    print(f"ring 2  doc.meta.line_sizes   : {meta_line_sizes.most_common(4)}")
    print(f"ring 2  doc span sizes        : {dspan.most_common(4)}")

    # --- ring 3: the render plan actually used
    pb = [b for b in plan if b["page"] == pno]
    print(
        f"ring 3  render_plan font_size : {Counter(round(b.get('font_size') or 0,2) for b in pb).most_common(4)}"
    )
    paths = Counter(b.get("render_path") for b in pb)
    fx = Counter(str(b.get("render_fixup")) for b in pb)
    print(f"ring 3  render_path / fixup   : {dict(paths)} / {dict(fx)}")
    payload_fs = Counter()
    for b in pb:
        pf = (b.get("render_payload") or {}).get("font_size")
        if pf:
            payload_fs[round(float(pf), 2)] += 1
    print(f"ring 3  payload.font_size     : {payload_fs.most_common(4)}")

    # does any payload report overflow / a shrink decision?
    ov = Counter()
    shrink = Counter()
    for b in pb:
        pl = b.get("render_payload") or {}
        ov[str(pl.get("overflow"))] += 1
        rec = pl.get("recovery") or {}
        if rec:
            shrink[str(rec.get("decision") or rec.get("policy"))] += 1
    print(f"ring 3  payload.overflow      : {dict(ov)}")
    print(f"ring 3  payload.recovery      : {dict(shrink)}")

src.close()
