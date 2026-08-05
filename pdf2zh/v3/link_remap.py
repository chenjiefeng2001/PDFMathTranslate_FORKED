"""V8.5 hyperlink re-anchoring for translated pages.

Pure-logic, dependency-free companion to the legacy pipeline's per-paragraph
geometry records (``_gate_records``). The translation pipeline only replaces
``/Contents`` — it never relocates link ``/Annots`` — so annotation /Rect values
stay at their *source* coordinates while the translated text moves during the
collision-resolved reflow. This module reprojects each annotation rect through
the paragraph-level source → destination bbox mapping recorded by the
TranslationGate, so the hot zone lands on the rendered translation.

Design goals (kept deliberately exportable / testable without PyMuPDF):
  * All rect math is pure: inputs are 4-tuples ``(x0, y0, x1, y1)``.
  * Every function normalizes rect orientation first, so page-space vs.
    render-space y-axis flips cannot silently break matching or projection.
  * ``remap_document_links`` is the only fitz-touching entry point; it is
    fully guarded — a single bad link/record never raises into the mainline.
  * Unknown / unmatched links keep their original rect (conservative no-op).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# Rect as a 4-tuple in page space (PDF: y-up, origin bottom-left).
Rect = Tuple[float, float, float, float]


def normalize_rect(r: Rect) -> Rect:
    """Return a canonical ``(x0, y0, x1, y1)`` with x0<=x1 and y0<=y1.

    Mirrors what MuPDF positions mini & maxi to; absorbing the reference-frame
    ambiguity between pdfminer (y-down origin top-left in some code paths) and
    PDF user space keeps matching/projection orientation-agnostic.
    """
    x0, x1 = (r[0], r[2]) if r[0] <= r[2] else (r[2], r[0])
    y0, y1 = (r[1], r[3]) if r[1] <= r[3] else (r[3], r[1])
    return (x0, y0, x1, y1)


def rect_area(r: Rect) -> float:
    x0, y0, x1, y1 = normalize_rect(r)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def rect_center(r: Rect) -> Tuple[float, float]:
    x0, y0, x1, y1 = normalize_rect(r)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def rect_contains_point(r: Rect, p: Tuple[float, float], tol: float = 0.0) -> bool:
    x0, y0, x1, y1 = normalize_rect(r)
    px, py = p
    return (x0 - tol) <= px <= (x1 + tol) and (y0 - tol) <= py <= (y1 + tol)


def rect_iou(a: Rect, b: Rect) -> float:
    """Intersection-over-union of two rects, in [0, 1]."""
    ax0, ay0, ax1, ay1 = normalize_rect(a)
    bx0, by0, bx1, by1 = normalize_rect(b)
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    union = rect_area(a) + rect_area(b) - inter
    if union <= 0.0:
        return 0.0
    return inter / union


def match_link_to_paragraphs(
    link_rect: Rect,
    src_boxes: Sequence[Rect],
) -> Optional[int]:
    """Pick the source paragraph whose bbox best explains this link rect.

    Primary heuristic: the paragraph box containing the link's center. When no
    box contains the center (links are frequently a word or two inside a wider
    paragraph), fall back to the maximal overlap. Returns ``None`` for links
    that touch no recorded paragraph (e.g. whole-page links / URL stamps) so
    callers can leave them untouched.
    """
    boxes = [normalize_rect(b) for b in src_boxes]
    if not boxes:
        return None
    cx, cy = rect_center(link_rect)
    for i, b in enumerate(boxes):
        if rect_contains_point(b, (cx, cy)):
            return i
    best_i, best_overlap = -1, 0.0
    for i, b in enumerate(boxes):
        ov = rect_iou(link_rect, b)
        if ov > best_overlap:
            best_i, best_overlap = i, ov
    if best_i >= 0 and best_overlap > 0.0:
        return best_i
    return None


def project_rect(link_rect: Rect, src_box: Rect, dst_box: Rect) -> Rect:
    """Affine-project ``link_rect`` from source into destination geometry.

    The link rect is treated as a sub-rect of the source paragraph box and is
    mapped into the translated paragraph box by a translate+scale transform:

        new_x = dst_x0 + (x - src_x0) * (dst_w / src_w)
        new_y = dst_y0 + (y - src_y0) * (dst_h / src_h)

    Degenerate source boxes (zero width/height) degrade gracefully to pure
    translation. The output keeps the caller's y-flip convention by applying
    the same transform to every coordinate (orientation is preserved).
    """
    rx0, ry0, rx1, ry1 = normalize_rect(link_rect)
    sx0, sy0, sx1, sy1 = normalize_rect(src_box)
    dx0, dy0, dx1, dy1 = normalize_rect(dst_box)
    sw = max(1e-9, sx1 - sx0)
    sh = max(1e-9, sy1 - sy0)
    sx = (dx1 - dx0) / sw
    sy = (dy1 - dy0) / sh
    # Apply the same affine to all four coordinates, preserving orientation so
    # the caller's y-convention (y-up vs y-down) is left untouched.
    n0 = (dx0 + (rx0 - sx0) * sx, dy0 + (ry0 - sy0) * sy)
    n1 = (dx0 + (rx1 - sx0) * sx, dy0 + (ry1 - sy0) * sy)
    x0, y0 = min(n0[0], n1[0]), min(n0[1], n1[1])
    x1, y1 = max(n0[0], n1[0]), max(n0[1], n1[1])
    # Preserve the orientation of the *paired* vertical edges from incoming
    # ordering: if the caller dealt y-up, top stays larger.
    if rx1 >= rx0:
        left, right = x0, x1
    else:
        left, right = x1, x0
    if ry1 >= ry0:
        bottom, top = y0, y1
    else:
        bottom, top = y1, y0
    return (left, bottom, right, top)


def compute_link_updates(
    links: Sequence[dict],
    src_boxes: Sequence[Rect],
    dst_boxes: Sequence[Rect],
) -> List[Tuple[dict, Rect]]:
    """Pure mapping links → new rects. Only links with a usable ``from`` rect
    and a matched source paragraph are returned (unchanged links are omitted).

    ``links`` may be fitz link dicts (with a ``from`` key) or any dict with a
    ``("from")`` tuple. Nothing here imports fitz.
    """
    updates: List[Tuple[dict, Rect]] = []
    if not links or not src_boxes or len(src_boxes) != len(dst_boxes):
        return updates
    for link in links:
        f = link.get("from")
        if f is None:
            continue
        try:
            rect = tuple(float(v) for v in f)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            continue
        idx = match_link_to_paragraphs(rect, src_boxes)
        if idx is None:
            continue
        new_rect = project_rect(rect, src_boxes[idx], dst_boxes[idx])
        updates.append((link, new_rect))
    return updates


def records_to_boxes(records: Sequence[dict]) -> Tuple[List[Rect], List[Rect]]:
    """Split extended gate records into (src_boxes, dst_boxes).

    Accepts both the extended schema (explicit ``src_box``/``dst_box`` keys)
    and the older minimal schema by deriving boxes from x/y/width/height.
    """
    src_boxes: List[Rect] = []
    dst_boxes: List[Rect] = []
    for rec in records:
        src = rec.get("src_box")
        dst = rec.get("dst_box")
        if src is not None and dst is not None:
            try:
                src_boxes.append(tuple(float(v) for v in src))  # type: ignore[union-attr]
                dst_boxes.append(tuple(float(v) for v in dst))  # type: ignore[union-attr]
                continue
            except (TypeError, ValueError):
                pass
        x = float(rec.get("x", 0.0))
        y = float(rec.get("y", 0.0))
        w = float(rec.get("width", 0.0))
        h = float(rec.get("height", 0.0))
        # Legacy fallback: no source bounds are known, so assume source box ==
        # the recorded destination geometry (identity mapping -> no-op update).
        src = (x, y - h, x + w, y)
        src_boxes.append(src)
        dst_boxes.append(src)
    return src_boxes, dst_boxes


def remap_document_links(
    doc,
    page_records_map: Dict[int, List[dict]],
    page_offset: int = 0,
    page_shifts: Optional[Dict[int, Tuple[float, float]]] = None,
) -> Dict[str, int]:
    """Guarded, fitz-based entry point: re-anchor links on translated pages.

    Args:
        doc: a PyMuPDF Document whose pages hold translated content.
        page_records_map: page index -> list of extended gate records produced
            by the legacy converter for that page.
        page_offset: added to every key of ``page_records_map`` (used when the
            record keys are pdfminer pageids but the doc page indexes differ,
            e.g. mono merged docs where page ``2*k`` is the original and
            ``2*k+1`` the translated copy).
        page_shifts: optional {page_no: (dx, dy)} translating the gate-record
            cropbox-relative frame into page user space (the space of link
            /Rect values). Applied to both src and dst boxes before matching.
            Pages with ``rotation != 0`` are skipped conservatively.

    Returns a small stats dict (``{"pages": n, "relinked": n, "skipped": n}``).
    Failures are logged and swallowed — relinking never breaks translation.
    """
    stats: Dict[str, int] = {"pages": 0, "relinked": 0, "skipped": 0}
    try:
        page_count = doc.page_count
    except Exception:
        return stats
    for rec_key, records in page_records_map.items():
        page_no = int(rec_key) + page_offset
        if not (0 <= page_no < page_count):
            continue
        try:
            page = doc[page_no]
        except Exception as e:
            log.debug("link_remap: skip page %s: %s", page_no, e)
            continue
        stats["pages"] += 1
        try:
            if getattr(page, "rotation", 0) not in (0, None):
                log.debug("link_remap: skip rotated page %s", page_no)
                stats["skipped"] += 1
                continue
            links = page.get_links()
        except Exception as e:
            log.debug("link_remap: get_links failed on page %s: %s", page_no, e)
            stats["skipped"] += 1
            continue
        if not links:
            continue
        src_boxes, dst_boxes = records_to_boxes(records)
        shift = (page_shifts or {}).get(page_no, (0.0, 0.0))
        dx, dy = shift
        if dx or dy:
            src_boxes = [(b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy) for b in src_boxes]
            dst_boxes = [(b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy) for b in dst_boxes]
        updates = compute_link_updates(links, src_boxes, dst_boxes)
        for link, new_rect in updates:
            try:
                _apply_one_link(page, link, new_rect)
                stats["relinked"] += 1
            except Exception as e:
                log.warning(
                    "link_remap: failed to re-anchor link on page %s: %s",
                    page_no, str(e)[:120],
                )
                stats["skipped"] += 1
    return stats


def _apply_one_link(page, link: dict, new_rect: Rect) -> None:
    """Rewrite a single annotation rect on ``page``.

    Uses update_link when the link dict round-trips; otherwise rebuilds by
    removing and re-inserting, preserving all non-rect fields.
    """
    if hasattr(page, "update_link"):
        try:
            updated = dict(link)
            updated["from"] = fitz_rect(*new_rect)
            page.update_link(updated)
            return
        except Exception:
            pass
    # Rebuild path.
    page.delete_link(link)
    rebuilt = dict(link)
    rebuilt["from"] = fitz_rect(*new_rect)
    page.insert_link(rebuilt)


def fitz_rect(x0: float, y0: float, x1: float, y1: float):
    """Lazy-import fitz.Rect (avoids hard import so the module stays unit-test
    friendly even without PyMuPDF present)."""
    import fitz

    return fitz.Rect(x0, y0, x1, y1)