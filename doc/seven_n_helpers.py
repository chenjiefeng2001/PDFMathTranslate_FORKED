"""Shared helpers for the 7N-series MP2e verification / trace scripts."""

from __future__ import annotations

import json

STEM = "The Art of Multiprocessor Programming, 2e"


def load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def page_sizes_from_document(doc) -> dict:
    """page_no → [width, height] from the document model dump."""
    sizes = {}
    for pg in doc.get("pages", []):
        pno = int(pg.get("page") or 0)
        w = float(pg.get("width") or 0)
        h = float(pg.get("height") or 0)
        if w > 0 and h > 0:
            sizes[pno] = [w, h]
    return sizes


def undo_old_shift(plan):
    """Rebuild the pre-fixup plan: undo the old +Δy shift_down.

    For ``render_fixup == "shift_down"`` entries the old code applied
    ``dst = src + Δ`` with ``Δ = dst[3] - src[3]`` (positive) and co-shifted
    commands by the same Δ.  Reverting yields the plan exactly as
    ``render_plan_from_model`` produced it (keep/preserve/keep_overflow
    entries already satisfy dst == src).
    """
    out = []
    for e in plan:
        e = json.loads(json.dumps(e))  # deep copy
        if e.get("render_fixup") != "shift_down":
            out.append(e)
            continue
        src = list(e.get("src_box") or [0, 0, 0, 0])
        dst = list(e.get("dst_box") or src)
        delta = round(float(dst[3]) - float(src[3]), 2)
        if abs(delta) < 0.5:
            e["render_fixup"] = None
            e.pop("render_fixup", None)
            out.append(e)
            continue
        e["dst_box"] = [
            round(float(dst[0]), 2),
            round(float(dst[1]) - delta, 2),
            round(float(dst[2]), 2),
            round(float(dst[3]) - delta, 2),
        ]
        e.pop("render_fixup", None)
        e.pop("render_path", None)
        e.pop("overflowed", None)
        for key in ("render_payload", "list_items", "toc_commands"):
            obj = e.get(key)
            if isinstance(obj, dict):
                for c in obj.get("commands") or []:
                    if isinstance(c, dict) and isinstance(c.get("y"), (int, float)):
                        c["y"] = round(float(c["y"]) - delta, 2)
        out.append(e)
    return out