"""Structural fidelity metrics — Commit 7D.

Computes the renderer-independent fidelity report from two *normalized*
extracted PDFs::

    compute_report(source_doc, output_doc) -> dict[str, float]

Every key in the returned dict is a single, individually-readable metric.  The
semantic-aware checks (list indentation, TOC columns/levels, preserved geometry,
outline destinations) live here as self-contained heuristics — this evaluator
deliberately does *not* import the semantic detectors, so it stays decoupled.
"""

from __future__ import annotations

import re

# distance/numeric tolerances (points) — kept generous because real layouts
# reflow; these catch true regressions (a column moved by a full indent) but
# ignore sub-point float noise.
_X_TOL = 10.0          # horizontal column tolerance
_OVERFLOW_TOL = 1.0    # a line may sit off-page by at most this before it counts
_MATCH_TOL2 = 250.0 ** 2  # squared Euclidean centre distance for line matching

_MARKER_RE = re.compile(r"^(?:[•·▪‣●○◦‣]|(?:[0-9]+|[a-z]|[ivxlcdmIVXLCDM]{1,4})[.)])$")
_DOT_LEADER_RE = re.compile(r"\.{2,}")
_NUM_RE = re.compile(r"^\d+$")
# mono-spaced families treated as "code-ish" for the preserved-geometry check
_MONO_FONTS = {"cour", "consolas", "menlo", "monaco"}


# -- low-level helpers ------------------------------------------------------

def _cx(bbox):
    return (bbox[0] + bbox[2]) / 2.0


def _cy(bbox):
    return (bbox[1] + bbox[3]) / 2.0


def _clamp01(v):
    return max(0.0, min(1.0, float(v)))


def _col_acc(pairs, tol=_X_TOL) -> float:
    """Mean per-pair closeness ``1 - |src-out|/tol`` (0..1). Empty -> 1.0."""
    if not pairs:
        return 1.0
    return sum(_clamp01(1.0 - abs(s - o) / tol) for s, o in pairs) / float(len(pairs))


def _match_lines(src_lines, out_lines):
    """Greedy nearest-centre line matching (within _MATCH_TOL2)."""
    pairs = []
    used = set()
    for sl in src_lines:
        scx, scy = _cx(sl["bbox"]), _cy(sl["bbox"])
        best, best_i, best_d = None, -1, None
        for i, ol in enumerate(out_lines):
            if i in used:
                continue
            dx = scx - _cx(ol["bbox"])
            dy = scy - _cy(ol["bbox"])
            d = dx * dx + dy * dy
            if best_d is None or d < best_d:
                best, best_i, best_d = ol, i, d
        if best is not None and best_d < _MATCH_TOL2:
            pairs.append((sl, best))
            used.add(best_i)
    return pairs


def _group_words(words):
    """Cluster words into reading-order lines by vertical centre."""
    groups = []
    for w in sorted(words, key=lambda w: (w["bbox"][1], w["bbox"][0])):
        if groups and abs(w["bbox"][1] - groups[-1]["y0"]) < 3:
            groups[-1]["words"].append(w)
        else:
            groups.append({"y0": w["bbox"][1], "words": [w]})
    for g in groups:
        g["words"].sort(key=lambda w: w["bbox"][0])
    return groups


def _line_word_features(words, page_width: float):
    """Derive list / TOC structural features for a page's words.

    Returns a list of feature dicts aligned with reading-order lines:

    - ``is_item`` / ``marker_x`` / ``content_x``  — list marker lines
    - ``is_cont`` / ``cont_x``                   — a wrapped continuation line
    - ``is_toc`` / ``title_x`` / ``page_x`` /
      ``page_number``                            — TOC entries
    """
    feats = []
    groups = _group_words(words)
    for i, g in enumerate(groups):
        ws = g["words"]
        if not ws:
            continue
        first, last = ws[0], ws[-1]
        first_x, last_x = first["bbox"][0], last["bbox"][0]
        line_text = " ".join(w["text"] for w in ws)

        is_item = bool(_MARKER_RE.fullmatch(first["text"]))
        content_x = None
        if is_item:
            content = next(
                (w for w in ws[1:] if not _MARKER_RE.fullmatch(w["text"])), None
            )
            content_x = content["bbox"][0] if content else first["bbox"][2]

        is_cont = (not is_item) and i > 0 and feats and feats[-1].get("is_item")
        cont_x = first_x if is_cont else None

        is_toc = False
        title_x = page_x = page_number = None
        has_leader = bool(_DOT_LEADER_RE.search(line_text))
        last_numeric = bool(last and _NUM_RE.fullmatch(last["text"]))
        if has_leader and last_numeric:
            is_toc = True
            title_x = first_x
            page_x = last_x
            page_number = last["text"]
        elif last_numeric and first_x < 0.3 * page_width and last_x > 0.6 * page_width:
            # right-aligned empty-column page number, no leader
            is_toc = True
            title_x = first_x
            page_x = last_x
            page_number = last["text"]

        feats.append(
            {
                "is_item": is_item,
                "marker_x": first_x if is_item else None,
                "content_x": content_x,
                "is_cont": is_cont,
                "cont_x": cont_x,
                "is_toc": is_toc,
                "title_x": title_x,
                "page_x": page_x,
                "page_number": page_number,
                "title": line_text if is_toc else None,
            }
        )
    return feats


def _level_ranks(x0s):
    """Map each indent to a stable rank (0-based) = pseudo TOC level."""
    seen = []
    ranks = []
    for x in x0s:
        if x not in seen:
            seen.append(x)
        ranks.append(seen.index(x))
    return ranks


# -- metric families --------------------------------------------------------

def _text_exactness(src_doc, out_doc):
    """Fraction of source words (per page) also present in the output page.

    1.0 for intact text (e.g. preserved code); lower once translation rewords.
    """
    rows = []
    for sp, op in zip(src_doc["pages"], out_doc["pages"]):
        outs = set(w["text"] for w in op["words"])
        sw = [w["text"] for w in sp["words"]]
        if not sw:
            continue
        rows.append(sum(1 for t in sw if t in outs) / float(len(sw)))
    return round(sum(rows) / len(rows), 4) if rows else 1.0


def _geometry_and_style(src_doc, out_doc):
    """bbox mean/max centre delta + font/bold/italic accuracy over matched lines."""
    all_deltas = []
    font_ok = []
    bold_pairs = {"hit": 0, "den": 0}
    italic_pairs = {"hit": 0, "den": 0}
    for sp, op in zip(src_doc["pages"], out_doc["pages"]):
        pairs = _match_lines(sp["lines"], op["lines"])
        for sl, ol in pairs:
            dx = _cx(sl["bbox"]) - _cx(ol["bbox"])
            dy = _cy(sl["bbox"]) - _cy(ol["bbox"])
            all_deltas.append((dx * dx + dy * dy) ** 0.5)
            font_ok.append(1 if sl["font"] == ol["font"] else 0)
            if sl["bold"]:
                bold_pairs["den"] += 1
                bold_pairs["hit"] += 1 if ol["bold"] else 0
            if sl["italic"]:
                italic_pairs["den"] += 1
                italic_pairs["hit"] += 1 if ol["italic"] else 0
    mean = (sum(all_deltas) / len(all_deltas)) if all_deltas else 0.0
    _max = max(all_deltas) if all_deltas else 0.0
    return {
        "bbox_mean_delta": round(mean, 2),
        "bbox_max_delta": round(_max, 2),
        "font_match_rate": round((sum(font_ok) / len(font_ok)) if font_ok else 1.0, 4),
        "bold_accuracy": round((bold_pairs["hit"] / bold_pairs["den"]) if bold_pairs["den"] else 1.0, 4),
        "italic_accuracy": round((italic_pairs["hit"] / italic_pairs["den"]) if italic_pairs["den"] else 1.0, 4),
        "_matched_lines": len(all_deltas),
    }


def _list_toc_metrics(src_doc, out_doc):
    """List content/continuation indentation + TOC columns/levels/number."""
    content_pairs, cont_pairs = [], []
    toc_title_pairs, toc_page_pairs, toc_num_equal, toc_level_eq, toc_level_den = (
        [], [], 0, 0, 0
    )
    for sp, op in zip(src_doc["pages"], out_doc["pages"]):
        sf = _line_word_features(sp["words"], sp["width"])
        of = _line_word_features(op["words"], op["width"])

        # list content_x: first item per line, matched by index among items
        sitems = [f["content_x"] for f in sf if f["is_item"] and f["content_x"] is not None]
        oitems = [f["content_x"] for f in of if f["is_item"] and f["content_x"] is not None]
        content_pairs.extend(zip(sitems, oitems))

        sconts = [f["cont_x"] for f in sf if f["is_cont"] and f["cont_x"] is not None]
        oconts = [f["cont_x"] for f in of if f["is_cont"] and f["cont_x"] is not None]
        cont_pairs.extend(zip(sconts, oconts))

        # TOC entries
        sentries = [f for f in sf if f["is_toc"]]
        oentries = [f for f in of if f["is_toc"]]
        if sentries and oentries:
            s_title_x = [f["title_x"] for f in sentries]
            o_title_x = [f["title_x"] for f in oentries]
            s_levels = _level_ranks(s_title_x)
            o_levels = _level_ranks(o_title_x)
            for k in range(min(len(sentries), len(oentries))):
                toc_title_pairs.append((sentries[k]["title_x"], oentries[k]["title_x"]))
                toc_page_pairs.append((sentries[k]["page_x"], oentries[k]["page_x"]))
                toc_num_equal += int(sentries[k]["page_number"] == oentries[k]["page_number"])
                if k < len(s_levels) and k < len(o_levels):
                    toc_level_eq += int(s_levels[k] == o_levels[k])
                    toc_level_den += 1

    return {
        "list_content_x_accuracy": round(_col_acc(content_pairs), 4),
        "list_continuation_x_accuracy": round(_col_acc(cont_pairs), 4),
        "toc_title_x_accuracy": round(_col_acc(toc_title_pairs), 4),
        "toc_page_x_accuracy": round(_col_acc(toc_page_pairs), 4),
        "toc_page_number_accuracy": round((toc_num_equal / len(toc_title_pairs)) if toc_title_pairs else 1.0, 4),
        "toc_level_accuracy": round((toc_level_eq / toc_level_den) if toc_level_den else 1.0, 4),
    }


def _outline_metrics(src_doc, out_doc):
    """Fraction of source outline destinations preserved in the output outline.

    An entry is preserved when an output outline entry has the same canonical
    title and the same page number (bookmark targets survive translation).
    """
    outs = {(e["title"], e["page"]) for e in out_doc.get("outline", [])}
    src = src_doc.get("outline", [])
    hits = sum(1 for e in src if (e["title"], e["page"]) in outs)
    return round((hits / len(src)) if src else 1.0, 4)


def _overflow_count(out_doc):
    """Count output lines that fall off the page horizontally (structural tip)."""
    total = 0
    for pg in out_doc["pages"]:
        w = pg["width"]
        for ln in pg["lines"]:
            if ln["bbox"][2] > w + _OVERFLOW_TOL or ln["bbox"][0] < -_OVERFLOW_TOL:
                total += 1
    return total


def _code_preserved_bbox(src_doc, out_doc):
    """Among code-like source lines (monospaced font, or a verbatim run), the
    fraction whose output keeps the same text **and** near-identical bbox.

    This is the Code → PreservedRegion structural check: a preserved code region
    must not reflow (same geometry), while translated prose lines are excluded by
    requiring matching verbatim text.
    """
    den = 0
    hits = 0
    for sp, op in zip(src_doc["pages"], out_doc["pages"]):
        out_texts = {ln["text"] for ln in op["lines"]}
        mono = [ln for ln in sp["lines"] if ln["font"] in _MONO_FONTS]
        verbatim_runs = []
        run = []
        for ln in sp["lines"]:
            if ln["text"] in out_texts:
                run.append(ln)
            else:
                if len(run) >= 2:
                    verbatim_runs.extend(run)
                run = []
        if len(run) >= 2:
            verbatim_runs.extend(run)
        for sl in mono:
            if sl["text"] not in out_texts:
                continue
            den += 1
            for ol in op["lines"]:
                if ol["text"] != sl["text"]:
                    continue
                dcx = _cx(sl["bbox"]) - _cx(ol["bbox"])
                dcy = _cy(sl["bbox"]) - _cy(ol["bbox"])
                if abs(dcx) <= _X_TOL and abs(dcy) <= _X_TOL:
                    hits += 1
                    break
    return round((hits / den) if den else 1.0, 4)


def compute_report(source_doc: dict, output_doc: dict) -> dict:
    """Compute the full structural fidelity report (JSON-safe, flat metrics).

    Args:
        source_doc: normalized extraction of the source PDF.
        output_doc: normalized extraction of the output PDF.

    Returns:
        Flat dict of metric -> float.  Helper ``_``-prefixed keys give raw
        diagnostic counts (dropped when serialized to baseline).
    """
    g = _geometry_and_style(source_doc, output_doc)
    n = g.pop("_matched_lines")  # not a fidelity metric itself
    lst = _list_toc_metrics(source_doc, output_doc)
    return {
        "text_exactness": _text_exactness(source_doc, output_doc),
        "bbox_mean_delta": g["bbox_mean_delta"],
        "bbox_max_delta": g["bbox_max_delta"],
        "font_match_rate": g["font_match_rate"],
        "bold_accuracy": g["bold_accuracy"],
        "italic_accuracy": g["italic_accuracy"],
        "list_content_x_accuracy": lst["list_content_x_accuracy"],
        "list_continuation_x_accuracy": lst["list_continuation_x_accuracy"],
        "toc_title_x_accuracy": lst["toc_title_x_accuracy"],
        "toc_page_x_accuracy": lst["toc_page_x_accuracy"],
        "toc_page_number_accuracy": lst["toc_page_number_accuracy"],
        "toc_level_accuracy": lst["toc_level_accuracy"],
        "outline_destination_accuracy": _outline_metrics(source_doc, output_doc),
        "overflow_count": _overflow_count(output_doc),
        "code_preserved_bbox": _code_preserved_bbox(source_doc, output_doc),
        "_matched_lines": n,
    }