"""7N-8 — Full-book post-FIX qualification (evidence-only; zero prod change).

Consumes the artifacts of a REAL production run produced by
``doc/7n_real_mp2e.py run --engine magicpdf`` and emits:

- ``7n-postfix-audit.json``  — machine audit, per page + per block
- ``7n-postfix-audit.md``    — human-readable qualification report
- ``defect-ledger.csv``      — final defect ledger (Phase 7)

Phases implemented (per 7N-8 plan):
  8A  full-book machine audit baseline (all 562 pages)
  8B  page grading A/B/C/D + suspicious-page inspection queue
  8C  p442_4 forensic (Q1-Q3)  — included in grading + dedicated section
  8D  FIX-2 regression qualification (shift/command/alias checks)

Read-only: never imports the forensic stack, never modifies production code.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import unicodedata
from collections import Counter

try:
    import pymupdf  # optional: enables the 8B mono-PDF visual cross-check
except Exception:  # noqa: BLE001
    pymupdf = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STEM = "The Art of Multiprocessor Programming, 2e"
TOKEN_RE = re.compile(r"<b\d+>|</b\d+>|<[a-z]+_\d+>")

# Page-grading thresholds (7N-8B)
LARGE_SHIFT_PT = 60.0  # shift_down delta larger than this => suspicious
BBOX_JUMP_PT = 100.0  # dst_y1 - src_y1 jump larger than this
MAX_CANDIDATE_PAGES = 40


def _load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ────────────────────────────────────────────────────────────────────────────
# Per-block audit
# ────────────────────────────────────────────────────────────────────────────


def audit_block(e: dict) -> dict:
    """Audit one render-plan entry; returns an audit record dict."""
    rp = e.get("render_payload") or {}
    rec = rp.get("recovery") or {}
    src = list(e.get("src_box") or [0, 0, 0, 0])
    dst = list(e.get("dst_box") or [0, 0, 0, 0])
    cmds = rp.get("commands") or []
    text = (e.get("text") or "").strip()
    translated = (e.get("translated") or "").strip()

    a = {
        "block_id": e.get("block_id"),
        "page": int(e.get("page") or 0),
        "kind": e.get("kind"),
        "render_path": e.get("render_path"),
        "fixup": e.get("render_fixup"),
        "src_box": [round(v, 2) for v in src],
        "dst_box": [round(v, 2) for v in dst],
        "font_size": e.get("font_size"),
        "text": text[:120],
        "translated": translated[:120],
        "n_commands": len(cmds),
        "recovery_decision": rec.get("decision"),
        "recovery_steps": rec.get("steps") or [],
        "final_font": rec.get("final_font_size"),
        "rp_overflow": rp.get("overflow"),
        "overflowed": bool(e.get("overflowed")),
        "layout_ok": rp.get("layout_ok"),
        "flags": [],
    }
    flags: list[str] = a["flags"]

    # token leakage (<b1>, </b2>, <x_3> ...)
    if TOKEN_RE.findall(translated):
        flags.append("token_leak")

    # empty translation on a translate-path block that had source text
    if (
        text
        and not translated
        and e.get("render_path") in ("translate_refit", "shift_down")
    ):
        flags.append("empty_translation")

    # recovery classification
    steps = rec.get("steps") or []
    if rec.get("decision") == "clip":
        flags.append("clip")

    # 1-line collapse: recovery went SHRINK and final render collapsed to
    # 1 line while the source had >= 3 lines and overflow persists.
    src_line_count = max(1, len((e.get("text") or "").splitlines()))
    final_lines = len(rp.get("lines") or [])
    if (
        "SHRINK" in steps
        and final_lines <= 1
        and src_line_count >= 3
        and len(translated) >= 20
    ):
        flags.append("1line_collapse")

    # shift metrics
    shift = round(float(dst[3]) - float(src[3]), 2)  # y-up: y1 delta
    if e.get("render_fixup") == "shift_down":
        a["shift"] = shift
        if abs(shift) > LARGE_SHIFT_PT:
            flags.append("large_shift")
        # bbox jump = same as shift for shift_down; keep separate flag name
        # for non-shift paths where dst moved without shift_down fixup.
        if e.get("render_fixup") != "shift_down" and abs(shift) > BBOX_JUMP_PT:
            flags.append("bbox_anomaly")
    else:
        a["shift"] = 0.0
        if abs(shift) > BBOX_JUMP_PT:
            flags.append("bbox_anomaly")

    # MECH-2: shift_down with settled commands — check anchoring
    if e.get("render_fixup") == "shift_down" and cmds:
        first_y = float(cmds[0].get("y") or 0.0)
        a["first_cmd_y"] = round(first_y, 2)
        a["decoupled"] = abs(first_y - float(dst[3])) > 0.5
        if a["decoupled"]:
            flags.append("mech2_decoupled")
    else:
        a["decoupled"] = None

    # residual overflow after recovery (potential CLIP manifestation)
    if rp.get("overflow") is True and rec.get("decision") in ("clip", None):
        flags.append("residual_overflow")

    # suspicious bbox: dst box degenerate or out of page (y-up, page ~792)
    bw = float(dst[2]) - float(dst[0])
    bh = float(dst[3]) - float(dst[1])
    if bw <= 1.0 or bh < 2.0 or float(dst[1]) < -1.0 or float(dst[3]) > 800.0:
        flags.append("suspicious_bbox")

    # severity for the ledger
    if flags:
        a["grade_flag"] = "C"
    else:
        a["grade_flag"] = ""
    return a


# ────────────────────────────────────────────────────────────────────────────
# FIX-2 side-effect checks (Phase 6 / 8D)
# ────────────────────────────────────────────────────────────────────────────


def fix2_checks(plan: list[dict]) -> dict:
    """FIX-2 regression qualification over the full plan."""
    shifted = [e for e in plan if e.get("render_fixup") == "shift_down"]
    with_cmds = []
    decoupled = 0
    alias_mismatch = 0
    double_shift_suspect = 0
    x_changed = 0
    font_changed = 0
    for e in shifted:
        rp = e.get("render_payload") or {}
        cmds = rp.get("commands") or []
        src = list(e.get("src_box") or [0, 0, 0, 0])
        dst = list(e.get("dst_box") or [0, 0, 0, 0])
        fs = float(e.get("font_size") or 0.0)
        if cmds:
            first_y = float(cmds[0].get("y") or 0.0)
            dec = abs(first_y - float(dst[3])) > 0.5
            with_cmds.append(
                {
                    "block_id": e.get("block_id"),
                    "page": e.get("page"),
                    "dst_box_y1": round(float(dst[3]), 2),
                    "first_cmd_y": round(first_y, 2),
                    "decoupled": dec,
                }
            )
            if dec:
                decoupled += 1
            # FIX-2 invariant: x untouched, fontsize untouched, shift exactly
            # once => first_cmd_y - src_y1 == dst_y1 - src_y1 == shift
            delta_box = float(dst[3]) - float(src[3])
            delta_cmd = first_y - float(src[3])
            if abs(delta_cmd - delta_box) > 1.0:
                double_shift_suspect += 1
            # settled font baseline: recovery-adjusted size when SHRINK ran,
            # else the entry font. Co-shift must NOT alter either (only y).
            rec = rp.get("recovery") or {}
            settled_font = float(
                rec.get("final_font_size") or e.get("font_size") or 0.0
            )
            for c in cmds:
                # x must equal src x0 (rounded); width/shape unchanged
                if abs(float(c.get("x") or 0.0) - float(src[0])) > 1.0 and not c.get(
                    "is_last"
                ):
                    x_changed += 1
                    break
                cfs = float(c.get("font_size") or 0.0)
                if cfs > 0 and settled_font > 0 and abs(cfs - settled_font) > 0.05:
                    font_changed += 1
                    break
        # alias value-consistency (JSON dump loses dict identity): compare
        # payload.commands vs list_items/toc_commands command sets by value.
        li = e.get("list_items") or {}
        tc = e.get("toc_commands") or {}
        for alias in (li, tc):
            if isinstance(alias, dict) and alias.get("commands"):
                v1 = json.dumps(cmds, sort_keys=True, ensure_ascii=False)
                v2 = json.dumps(alias["commands"], sort_keys=True, ensure_ascii=False)
                if v1 != v2:
                    alias_mismatch += 1
    return {
        "shift_down_total": len(shifted),
        "shifted_with_commands": len(with_cmds),
        "decoupled": decoupled,
        "double_shift_suspect": double_shift_suspect,
        "x_changed": x_changed,
        "font_changed": font_changed,
        "alias_value_mismatch": alias_mismatch,
        "detail": with_cmds,
    }


# ────────────────────────────────────────────────────────────────────────────
# p442_4 forensic (Phase 4 / 8C)
# ────────────────────────────────────────────────────────────────────────────


def forensic_packet(plan: list[dict], doc: dict, block_id: str) -> dict:
    e = next((x for x in plan if x.get("block_id") == block_id), None)
    if e is None:
        return {"block_id": block_id, "found": False}
    rp = e.get("render_payload") or {}
    rec = rp.get("recovery") or {}

    # doc block (source IR) for the same page/geometry
    page_no = int(e.get("page") or 0)
    src_box = list(e.get("src_box") or [0, 0, 0, 0])
    doc_block = None
    for pg in doc.get("pages", []):
        if int(pg.get("page") or 0) != page_no:
            continue
        for b in pg.get("blocks", []):
            if (
                abs(float(b.get("x0", 0)) - float(src_box[0])) < 2
                and abs(float(b.get("y0", 0)) - float(src_box[1])) < 2
            ):
                doc_block = b
                break
        if doc_block:
            break

    src_line_count = len((e.get("text") or "").splitlines())
    final_lines = list(rp.get("lines") or [])
    q1_visual = None  # filled by 8B visual pass; telemetry-only here
    packet = {
        "block_id": block_id,
        "found": True,
        "page": page_no,
        "kind": e.get("kind"),
        "source_text": e.get("text"),
        "translated_text": e.get("translated"),
        "src_box": src_box,
        "dst_box": e.get("dst_box"),
        "font_size": e.get("font_size"),
        "recovery": rec,
        "trace": rp.get("trace"),
        "src_line_count": src_line_count,
        "final_line_count": len(final_lines),
        "final_lines": final_lines[:10],
        "commands": rp.get("commands"),
        "rp_overflow": rp.get("overflow"),
        "layout_ok": rp.get("layout_ok"),
        "doc_block_found": doc_block is not None,
        "doc_block_text": (doc_block or {}).get("text"),
        "doc_block_n_lines": len((doc_block or {}).get("lines") or []),
        "q1_visual_defect": q1_visual,
    }
    # First-divergence reasoning:
    #   WRAP->SHRINK->CLIP with final 1 line => layout-stage line collapse;
    #   if commands exist with per-line y's, render layer re-flows.
    steps = rec.get("steps") or []
    if steps[-1:] == ["CLIP"] and len(final_lines) <= 1 and src_line_count >= 2:
        packet["first_divergence"] = "Stage-3 (adaptive layout CLIP policy)"
    elif "SHRINK" in steps and len(final_lines) <= 1:
        packet["first_divergence"] = "Stage-3 (SHRINK line-collapse, no CLIP)"
    else:
        packet["first_divergence"] = "undetermined"
    return packet


# ────────────────────────────────────────────────────────────────────────────
# 8B: mono-PDF visual cross-check (page-index == plan page, 0-based)
# ────────────────────────────────────────────────────────────────────────────


def mono_visual_check(mono_path: str, blocks: list[dict], max_pages: int = 30) -> dict:
    """Open the rendered mono PDF and verify that translated CJK spans
    actually landed at the plan's command coordinates (y-up → y-down flip:
    fitz_y = page_height - plan_y).  Flags:

    - ``visual_missing``: no CJK span found at/near the expected command site
    - ``visual_overlap``: translation span intersects a foreign (English)
      span by >10% of the translation area

    Only translate-path blocks are checked; at most ``max_pages`` flagged pages.
    """
    result: dict = {
        "available": pymupdf is not None,
        "checked_blocks": 0,
        "visual_missing": [],
        "visual_overlap": [],
    }
    if pymupdf is None:
        return result
    doc = pymupdf.open(mono_path)
    target_pages = sorted({b["page"] for b in blocks if b["flags"]})[:max_pages]
    page_cache: dict[int, object] = {}
    for pno in target_pages:
        idx = pno  # plan page is 0-based mono index (verified: plan p442 == mono[442])
        if idx >= len(doc):
            continue
        pg = doc[idx]
        h = float(pg.rect.height)
        d = pg.get_text("rawdict")
        spans = []
        for blk in d["blocks"]:
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    txt = "".join(ch["c"] for ch in sp.get("chars", []))
                    if txt.strip():
                        spans.append((txt, sp["bbox"]))
        page_cache[pno] = spans
    # now audit flagged blocks on those pages
    for b in blocks:
        if not b["flags"] or b["page"] not in page_cache:
            continue
        if not (b.get("translated") or ""):
            continue
        result["checked_blocks"] += 1
        spans = page_cache[b["page"]]
        h = None
        # expected site: first command y (v3 box-top anchor), else dst_box y1.
        # 7N-FIX-3: the renderer converts box-top → baseline
        # (baseline = (h - y) + 0.85 * font_size), so the span is expected
        # 0.85em BELOW the box top, not at it.
        exp_y_up = b.get("first_cmd_y")
        if exp_y_up is None:
            exp_y_up = b["dst_box"][3]
        exp_x = b["src_box"][0]
        pg = doc[b["page"]]
        h = float(pg.rect.height)
        expect_y = h - float(exp_y_up) + 0.85 * float(b.get("font_size") or 0.0)

        def _cjk(s: str) -> bool:
            return any("\u4e00" <= c <= "\u9fff" for c in s)

        near = [
            (t, bb)
            for (t, bb) in spans
            if _cjk(t)
            and abs(float(bb[0]) - float(exp_x)) < 15
            and -8 <= (float(bb[3]) - expect_y) <= 8
        ]
        if not near:
            result["visual_missing"].append(
                {
                    "block_id": b["block_id"],
                    "page": b["page"],
                    "expected": [round(float(exp_x), 1), round(expect_y, 1)],
                    "translated": b["translated"][:40],
                }
            )
            continue
        # overlap against non-CJK (foreign/English) spans
        t, bb = near[0]
        tr = pymupdf.Rect(bb[0], bb[1], bb[2], bb[3])
        for ot, obb in spans:
            if _cjk(ot) or not ot.strip():
                continue
            orect = pymupdf.Rect(obb[0], obb[1], obb[2], obb[3])
            inter = tr & orect
            if not inter.is_empty and inter.get_area() > 0.10 * tr.get_area():
                result["visual_overlap"].append(
                    {
                        "block_id": b["block_id"],
                        "page": b["page"],
                        "translation": t[:30],
                        "collides_with": ot[:30],
                        "overlap_pct": round(
                            100 * inter.get_area() / max(1e-6, tr.get_area()), 1
                        ),
                    }
                )
                break
    return result


# ────────────────────────────────────────────────────────────────────────────
# 8E: MECH-3 sweep — shift_down landing zones vs preserved blocks
# ────────────────────────────────────────────────────────────────────────────


def mech3_sweep(plan: list[dict], mono_path: str) -> dict:
    """Detect shift_down blocks whose landing (dst) box overlaps a preserved
    (preserve_float / kind in _PRESERVE_KINDS) block's final box, then verify
    against the rendered mono PDF whether actual glyph ink collides.

    Returns counts + per-hit verdicts (plan_overlap_pct / ink_overlap_pct).
    """
    preserve_kinds = {"figure", "image", "table", "formula", "formula_inline", "code"}
    by_page: dict[int, list[dict]] = {}
    for e in plan:
        by_page.setdefault(int(e.get("page") or 0), []).append(e)

    def _inter(a, b) -> float:
        x0 = max(a[0], b[0])
        x1 = min(a[2], b[2])
        y0 = max(a[1], b[1])
        y1 = min(a[3], b[3])
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)

    def _area(b) -> float:
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

    hits = []
    for e in plan:
        if e.get("render_fixup") != "shift_down":
            continue
        dst = list(e.get("dst_box") or [0, 0, 0, 0])
        if _area(dst) <= 0:
            continue
        for o in by_page.get(int(e.get("page") or 0), []):
            if o is e or o.get("render_fixup") != "preserve":
                continue
            ok = o.get("kind")
            if ok not in preserve_kinds:
                continue
            ob = list(o.get("dst_box") or [0, 0, 0, 0])
            ov = _inter(dst, ob)
            if ov <= 0:
                continue
            frac = ov / max(1e-6, _area(dst))
            if frac > 0.2:
                hits.append(
                    {
                        "block_id": e.get("block_id"),
                        "page": e.get("page"),
                        "plan_overlap_pct": round(100 * frac, 1),
                        "into": o.get("block_id"),
                        "into_kind": ok,
                    }
                )

    # ink verification on the rendered mono PDF
    doc = None
    if pymupdf is not None:
        try:
            doc = pymupdf.open(mono_path)
        except Exception:  # noqa: BLE001
            doc = None

    def _cjk(s: str) -> bool:
        return any("\u4e00" <= c <= "\u9fff" for c in s)

    page_spans: dict[int, list] = {}

    def _spans(pno: int):
        if pno not in page_spans:
            if doc is None or pno >= len(doc):
                page_spans[pno] = []
                return page_spans[pno]
            d = doc[pno].get_text("rawdict")
            out = []
            for b in d["blocks"]:
                for ln in b.get("lines", []):
                    for sp in ln.get("spans", []):
                        txt = "".join(ch["c"] for ch in sp.get("chars", []))
                        if txt.strip():
                            out.append((txt, sp["bbox"]))
            page_spans[pno] = out
        return page_spans[pno]

    verified = 0
    real_collisions = 0
    for h in hits:
        if doc is None:
            h["ink_overlap_pct"] = None
            continue
        e = next((x for x in plan if x.get("block_id") == h["block_id"]), None)
        if e is None:
            continue
        pno = int(e.get("page") or 0)
        if pno >= len(doc):
            continue
        pg_h = float(doc[pno].rect.height)
        cmds = (e.get("render_payload") or {}).get("commands") or []
        cmd_y = (
            float(cmds[0]["y"])
            if cmds
            else float((e.get("dst_box") or [0, 0, 0, 0])[3])
        )
        # 7N-FIX-3: rendered baseline = (h - cmd_y) + 0.85 * settled font size
        baseline_off = 0.85 * float(cmds[0].get("font_size") or 0.0) if cmds else 0.0
        trs = [
            (t, bb)
            for (t, bb) in _spans(pno)
            if _cjk(t)
            and abs(float(bb[3]) - (pg_h - cmd_y + baseline_off)) < 12
            and abs(float(bb[0]) - float((e.get("src_box") or [0, 0, 0, 0])[0])) < 20
        ]
        if not trs:
            h["ink_overlap_pct"] = None  # translation not found at landing
            continue
        verified += 1
        t, tb = trs[0]
        tr = pymupdf.Rect(tb[0], tb[1], tb[2], tb[3])
        ob = list(
            next(x for x in plan if x.get("block_id") == h["into"]).get("dst_box")
        )
        ink = 0.0
        for ot, obb in _spans(pno):
            if _cjk(ot) or not ot.strip():
                continue
            if _inter(list(obb), ob) <= 0:
                continue
            r = pymupdf.Rect(obb[0], obb[1], obb[2], obb[3])
            ir = tr & r
            if not ir.is_empty:
                ink += ir.get_area()
        h["ink_overlap_pct"] = round(100 * ink / max(1e-6, tr.get_area()), 1)
        if h["ink_overlap_pct"] > 5.0:
            real_collisions += 1
            h["verdict"] = "REAL_COLLISION"
        else:
            h["verdict"] = "plan_only"
    return {
        "plan_overlap_hits": len(hits),
        "ink_verified": verified,
        "real_collisions": real_collisions,
        "detail": hits,
    }


# ────────────────────────────────────────────────────────────────────────────
# Page grading (Phase 2 / 8B)
# ────────────────────────────────────────────────────────────────────────────

DEFECT_FLAGS = {
    "clip",
    "mech2_decoupled",
    "empty_translation",
    "token_leak",
    "1line_collapse",
}
SUSPICIOUS_FLAGS = {
    "large_shift",
    "bbox_anomaly",
    "suspicious_bbox",
    "residual_overflow",
}


def grade_pages(blocks: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Grade each page A/B/C/D; returns (grades, suspicious_queue, d_pages)."""
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        by_page.setdefault(b["page"], []).append(b)

    grades = []
    suspicious = []
    confirmed = []
    for pno in sorted(by_page):
        bl = by_page[pno]
        flags: Counter = Counter()
        for b in bl:
            for f in b["flags"]:
                flags[f] += 1
        defect = {f: flags[f] for f in DEFECT_FLAGS if flags[f]}
        sus = {f: flags[f] for f in SUSPICIOUS_FLAGS if flags[f]}
        recovered = sum(1 for b in bl if b["recovery_decision"] == "shrink") + sum(
            1 for b in bl if b["fixup"] == "shift_down"
        )
        if defect:
            grade = "D"
        elif sus:
            grade = "C"
        elif recovered:
            grade = "B"
        else:
            grade = "A"
        g = {
            "page": pno,
            "grade": grade,
            "blocks": len(bl),
            "defect_flags": defect,
            "suspicious_flags": sus,
            "recovery_blocks": recovered,
        }
        grades.append(g)
        if grade == "C":
            suspicious.append(g)
        elif grade == "D":
            confirmed.append(g)
    return grades, suspicious, confirmed


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


def cmd_audit(args) -> int:
    out = args.out
    magic_dir = os.path.join(out, "output-magicpdf", "magicpdf")
    plan_path = os.path.join(magic_dir, f"{STEM}_render_plan.json")
    doc_path = os.path.join(magic_dir, f"{STEM}_document.json")
    mono_path = os.path.join(magic_dir, f"{STEM}_mono.pdf")
    for p in (plan_path, doc_path, mono_path):
        if not os.path.exists(p):
            print(f"[7N-8] missing artifact: {p}")
            return 2

    plan = _load_json(plan_path)
    doc = _load_json(doc_path)

    # ── 8A: per-block audit ─────────────────────────────────────────────
    blocks = [audit_block(e) for e in plan]

    # doc model cross-check
    doc_pages = len(doc.get("pages", []))
    doc_blocks = 0
    doc_translated = 0
    for pg in doc.get("pages", []):
        for b in pg.get("blocks", []):
            doc_blocks += 1
            if (b.get("metadata") or {}).get("translated"):
                doc_translated += 1

    decisions = Counter(
        b["recovery_decision"] for b in blocks if b["recovery_decision"]
    )
    steps_hist = Counter(
        "->".join(b["recovery_steps"]) for b in blocks if b["recovery_steps"]
    )
    fixups = Counter(b["fixup"] for b in blocks if b["fixup"])
    paths = Counter(b["render_path"] for b in blocks)

    # ── 8B: page grading ────────────────────────────────────────────────
    grades, suspicious, confirmed = grade_pages(blocks)
    grade_hist = Counter(g["grade"] for g in grades)

    # ── 8C: p442_4 forensic ─────────────────────────────────────────────
    target_blocks = sorted({b["block_id"] for b in blocks if "clip" in b["flags"]})
    target_blocks.append("p442_4")
    forensics = {}
    for bid in dict.fromkeys(target_blocks):
        forensics[bid] = forensic_packet(plan, doc, bid)

    # ── 8D: FIX-2 side-effect checks ────────────────────────────────────
    fix2 = fix2_checks(plan)

    # ── 8B: mono-PDF visual cross-check ─────────────────────────────────
    visual = mono_visual_check(mono_path, blocks)

    # ── 8E: MECH-3 sweep (shift lands inside preserved blocks) ─────────
    mech3 = mech3_sweep(plan, mono_path)

    # ── assembly ────────────────────────────────────────────────────────
    n_translated = sum(1 for b in blocks if b["translated"])
    summary = {
        "schema": "7n-postfix-audit-v1",
        "run_dir": os.path.abspath(out),
        "total_pages": doc_pages,
        "total_blocks": len(blocks),
        "translated_blocks": n_translated,
        "render_paths": dict(paths),
        "fixup_counts": dict(fixups),
        "recovery_decisions": dict(decisions),
        "recovery_steps": dict(steps_hist),
        "page_grades": dict(grade_hist),
        "grade_D_pages": [g["page"] for g in confirmed],
        "grade_C_pages": [g["page"] for g in suspicious],
        "suspicious_queue": [
            {
                "page": g["page"],
                "severity": "HIGH" if g["defect_flags"] else "MEDIUM",
                "flags": {**g["defect_flags"], **g["suspicious_flags"]},
                "reason": "; ".join(
                    f"{k}x{v}"
                    for k, v in {**g["defect_flags"], **g["suspicious_flags"]}.items()
                ),
            }
            for g in (confirmed + suspicious)[:MAX_CANDIDATE_PAGES]
        ],
        "flag_counts": dict(Counter(f for b in blocks for f in b["flags"])),
        "doc_pages": doc_pages,
        "doc_blocks": doc_blocks,
        "doc_blocks_with_translated": doc_translated,
        "fix2": {k: v for k, v in fix2.items() if k != "detail"},
        "fix2_detail": fix2["detail"],
        "visual_check": {
            k: v
            for k, v in visual.items()
            if k not in ("visual_missing", "visual_overlap")
        },
        "visual_missing": visual["visual_missing"],
        "visual_overlap": visual["visual_overlap"],
        "mech3": {k: v for k, v in mech3.items() if k != "detail"},
        "mech3_detail": mech3["detail"],
        "forensics": forensics,
        "mono_pdf": mono_path,
    }
    audit_json = os.path.join(out, "7n-postfix-audit.json")
    with open(audit_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    # per-page table
    pages_json = os.path.join(out, "7n-postfix-pages.json")
    with open(pages_json, "w", encoding="utf-8") as fh:
        json.dump(grades, fh, ensure_ascii=False, indent=1)

    # ── defect ledger CSV ───────────────────────────────────────────────
    ledger_csv = os.path.join(out, "defect-ledger.csv")
    with open(ledger_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "page",
                "block",
                "symptom",
                "first_divergence",
                "severity",
                "reproducible",
                "action",
            ]
        )
        for bid, pk in forensics.items():
            if not pk.get("found"):
                continue
            sym = ",".join(pk.get("recovery", {}).get("steps") or []) or pk.get(
                "kind", ""
            )
            w.writerow(
                [
                    pk.get("page"),
                    bid,
                    sym,
                    pk.get("first_divergence", ""),
                    "HIGH",
                    "Yes",
                    "investigate",
                ]
            )
        for g in confirmed:
            for b in blocks:
                if b["page"] == g["page"] and b["flags"]:
                    w.writerow(
                        [
                            g["page"],
                            b["block_id"],
                            ",".join(b["flags"]),
                            "per-block",
                            "HIGH",
                            "Yes",
                            "investigate",
                        ]
                    )

    # ── markdown report ─────────────────────────────────────────────────
    md = []
    md.append("# 7N-8 — Full-book post-FIX machine audit\n")
    md.append(f"- run dir: `{os.path.abspath(out)}`")
    md.append(
        f"- total pages: **{doc_pages}** (plan blocks: {len(blocks)}, translated: {n_translated})"
    )
    md.append(f"- render paths: `{dict(paths)}`")
    md.append(f"- fixups: `{dict(fixups)}`")
    md.append(
        f"- recovery decisions: `{dict(decisions)}` steps: `{dict(steps_hist)}`\n"
    )
    md.append("## Page grading (A/B/C/D)\n")
    md.append("| Grade | Pages |")
    md.append("|---|---|")
    for k in "ABCD":
        md.append(f"| {k} | {grade_hist.get(k, 0)} |")
    md.append("")
    if confirmed:
        md.append(
            f"### D — confirmed defect candidates: {', '.join(map(str, [g['page'] for g in confirmed]))}\n"
        )
    if suspicious:
        md.append("### C — suspicious queue (visual inspection needed)\n")
        md.append("| Page | Severity | Flags |")
        md.append("|---|---|---|")
        for s in summary["suspicious_queue"]:
            if s["severity"] == "MEDIUM":
                md.append(f"| {s['page']} | {s['severity']} | {s['reason']} |")
        md.append("")
    md.append("## FIX-2 regression qualification (8D)\n")
    md.append(f"- shift_down total: **{fix2['shift_down_total']}**")
    md.append(f"- shifted with settled commands: **{fix2['shifted_with_commands']}**")
    md.append(f"- **decoupled: {fix2['decoupled']}** (must be 0)")
    md.append(f"- double-shift suspects: {fix2['double_shift_suspect']} (must be 0)")
    md.append(
        f"- x-changed: {fix2['x_changed']} / font-changed: {fix2['font_changed']} (must be 0)"
    )
    md.append(f"- alias value mismatches: {fix2['alias_value_mismatch']} (must be 0)\n")
    md.append("## Mono-PDF visual cross-check (8B)\n")
    md.append(
        f"- available: {visual['available']}; flagged blocks checked: {visual['checked_blocks']}"
    )
    md.append(
        f"- visual_missing: **{len(visual['visual_missing'])}** (translation not found at command site)"
    )
    md.append(
        f"- visual_overlap: **{len(visual['visual_overlap'])}** (translation bbox intersects foreign span >10%)\n"
    )
    for v in visual["visual_missing"][:20]:
        md.append(
            f"  - MISSING {v['block_id']} p{v['page']} expected~{v['expected']} text=`{v['translated']}`"
        )
    for v in visual["visual_overlap"][:20]:
        md.append(
            f"  - OVERLAP {v['block_id']} p{v['page']} `{v['translation']}` × `{v['collides_with']}` {v['overlap_pct']}%"
        )
    md.append("")
    md.append("## MECH-3 sweep (8E): shift_down landing vs preserved blocks\n")
    md.append(
        f"- plan-level landing overlaps (>20% of landing box): **{mech3['plan_overlap_hits']}**"
    )
    md.append(f"- ink-verified on mono PDF: {mech3['ink_verified']}")
    md.append(
        f"- **real glyph collisions: {mech3['real_collisions']}** (must be 0 to close MECH-3 as benign)\n"
    )
    for h in mech3["detail"]:
        if h.get("verdict") == "REAL_COLLISION":
            md.append(
                f"  - REAL {h['block_id']} p{h['page']} into {h['into']} ({h['into_kind']}) ink={h['ink_overlap_pct']}%"
            )
    md.append("")
    md.append("## Forensic packets (8C)\n")
    for bid, pk in forensics.items():
        if not pk.get("found"):
            continue
        md.append(f"### {bid} (page {pk.get('page')})\n")
        md.append(
            f"- source ({pk.get('src_line_count')} src lines): `{(pk.get('source_text') or '')[:100]}`"
        )
        md.append(
            f"- translated ({pk.get('final_line_count')} final lines): `{(pk.get('translated_text') or '')[:100]}`"
        )
        md.append(f"- trace: `{json.dumps(pk.get('trace'))}`")
        md.append(f"- first_divergence: **{pk.get('first_divergence')}**")
        md.append(
            f"- rp_overflow={pk.get('rp_overflow')} layout_ok={pk.get('layout_ok')}\n"
        )
    md_path = os.path.join(out, "7n-postfix-audit.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))

    # console summary
    print(f"[7N-8] pages={doc_pages} blocks={len(blocks)} translated={n_translated}")
    print(f"[7N-8] grades: {dict(grade_hist)}")
    print(f"[7N-8] flags: {summary['flag_counts']}")
    print(
        f"[7N-8] FIX-2: decoupled={fix2['decoupled']} "
        f"double_shift={fix2['double_shift_suspect']} "
        f"alias_mismatch={fix2['alias_value_mismatch']}"
    )
    print(
        f"[7N-8] visual: checked={visual['checked_blocks']} "
        f"missing={len(visual['visual_missing'])} "
        f"overlap={len(visual['visual_overlap'])}"
    )
    print(
        f"[7N-8] MECH-3: plan_hits={mech3['plan_overlap_hits']} "
        f"ink_verified={mech3['ink_verified']} "
        f"real_collisions={mech3['real_collisions']}"
    )
    for bid, pk in forensics.items():
        if pk.get("found"):
            print(
                f"[7N-8] forensic {bid}: first_divergence={pk.get('first_divergence')}"
            )
    print(f"[7N-8] wrote {audit_json}")
    print(f"[7N-8] wrote {md_path}")
    print(f"[7N-8] wrote {ledger_csv}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="full-book machine audit (8A–8D)")
    a.add_argument("--out", default="doc/7n8-mp2e")
    a.set_defaults(func=cmd_audit)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
