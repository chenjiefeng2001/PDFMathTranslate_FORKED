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

import math
import re

# distance/numeric tolerances (points) — kept generous because real layouts
# reflow; these catch true regressions (a column moved by a full indent) but
# ignore sub-point float noise.
_X_TOL = 10.0  # horizontal column tolerance
_OVERFLOW_TOL = 1.0  # a line may sit off-page by at most this before it counts
_MATCH_TOL2 = 250.0**2  # squared Euclidean centre distance for line matching
_TOC_CONT_TOL = (
    30.0  # continuation line may drift from title_x by this (title_x+size anchor)
)
# ── 7F-5d adaptive-TOC constants ────────────────────────────────────────────
_PAGE_COL_EPS = 2.0  # strict page-column stability epsilon (page_x must not move)
_FONT_SIZE_TOL = 2.0  # font size may not drift without reason (points)
_SHRINK_EVIDENCE_TOL = 0.5  # shrink must reduce the size by at least this
_WORD_GAP_EM = 0.25  # inter-word gap estimate used for re-wrap simulation

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
    last_toc_x = None
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
        dot_words = [w for w in ws if re.fullmatch(r"\.+", w["text"])]
        has_leader = has_leader or bool(dot_words)
        leader_start = leader_end = title_end = None
        last_numeric = bool(last and _NUM_RE.fullmatch(last["text"]))
        if has_leader and last_numeric:
            is_toc = True
            title_x = first_x
            page_x = last_x
            page_number = last["text"]
            if dot_words:
                first_dot = ws.index(dot_words[0])
                title_end = max((w["bbox"][2] for w in ws[:first_dot]), default=first_x)
                leader_start = min(w["bbox"][0] for w in dot_words)
                leader_end = max(w["bbox"][2] for w in dot_words)
        elif last_numeric and first_x < 0.3 * page_width and last_x > 0.6 * page_width:
            # right-aligned empty-column page number, no leader
            is_toc = True
            title_x = first_x
            page_x = last_x
            page_number = last["text"]

        # TOC continuation: a line right after a TOC entry (or another TOC
        # continuation) anchored near the entry's title_x — the wrapped
        # follow-on lines of a multi-line entry, never a new entry itself.
        is_toc_cont = False
        toc_cont_x = None
        if (
            not is_item
            and not is_toc
            and last_toc_x is not None
            and abs(first_x - last_toc_x) <= _TOC_CONT_TOL
        ):
            is_toc_cont = True
            toc_cont_x = first_x
        if is_toc:
            last_toc_x = title_x
        # (continuations keep anchoring to the entry's title_x automatically)

        feats.append(
            {
                "y0": float(g["y0"]),
                "_words": list(ws),
                "is_item": is_item,
                "marker_x": first_x if is_item else None,
                "marker_text": first["text"] if is_item else None,
                "content_x": content_x,
                "is_cont": is_cont,
                "cont_x": cont_x,
                "is_toc": is_toc,
                "title_x": title_x,
                "page_x": page_x,
                "page_number": page_number,
                "title": line_text if is_toc else None,
                "has_leader": has_leader,
                "title_end": title_end,
                "leader_start": leader_start,
                "leader_end": leader_end,
                "is_toc_cont": is_toc_cont,
                "toc_cont_x": toc_cont_x,
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
        "bold_accuracy": round(
            (bold_pairs["hit"] / bold_pairs["den"]) if bold_pairs["den"] else 1.0, 4
        ),
        "italic_accuracy": round(
            (italic_pairs["hit"] / italic_pairs["den"]) if italic_pairs["den"] else 1.0,
            4,
        ),
        "_matched_lines": len(all_deltas),
    }


def _list_wrap_integrity_page(sf, of):
    """Per-page wrap integrity: markers preserved + item count fidelity.

    Catches the classic wrap regressions the spec cares about — two items
    merged into one line, a marker dropped, a continuation promoted to an
    item, or the next item swallowed by the previous one.  ``1.0`` when
    either side has no items.
    """
    s_markers = {f["marker_text"] for f in sf if f["is_item"] and f["marker_text"]}
    o_markers = {f["marker_text"] for f in of if f["is_item"] and f["marker_text"]}
    marker_hit = (len(s_markers & o_markers) / len(s_markers)) if s_markers else 1.0
    n_src = sum(1 for f in sf if f["is_item"])
    n_out = sum(1 for f in of if f["is_item"])
    denom = max(n_src, n_out)
    count_fidelity = (1.0 - abs(n_src - n_out) / denom) if denom else 1.0
    return (marker_hit + count_fidelity) / 2.0


def _list_nested_accuracy_page(sf, of):
    """Per-page nested geometry: content_x level-rank preserved across levels.

    Only pages with >= 2 distinct content_x levels contribute; the rank of
    each item's content column (0, 1, 2, … by first-appearance order) must
    match source vs output — a flattened or re-ordered nesting drops it.
    """
    s_cx = [f["content_x"] for f in sf if f["is_item"] and f["content_x"] is not None]
    o_cx = [f["content_x"] for f in of if f["is_item"] and f["content_x"] is not None]
    if len(set(s_cx)) < 2 or len(set(o_cx)) < 2:
        return 1.0
    s_ranks = _level_ranks(s_cx)
    o_ranks = _level_ranks(o_cx)
    n = min(len(s_ranks), len(o_ranks))
    if not n:
        return 1.0
    return sum(1 for k in range(n) if s_ranks[k] == o_ranks[k]) / float(n)


def _toc_leader_integrity_page(sf, of):
    """Per-page TOC leader integrity (index-aligned entries).

    For source entries that carried a dot leader, the aligned output entry
    must keep its dots strictly between the translated title end and page_x
    (no overlap with the title, no dots past the page column).  For entries
    without a leader the output must **not** invent dots.  ``1.0`` when the
    source page has no TOC entries; ``0.0`` when entries were lost.
    """
    s_toc = [f for f in sf if f["is_toc"]]
    o_toc = [f for f in of if f["is_toc"]]
    if not s_toc:
        return 1.0
    if len(o_toc) < len(s_toc):
        return 0.0  # entries lost — nothing left to verify
    scores = []
    for k, s in enumerate(s_toc):
        o = o_toc[k]
        if not s["has_leader"]:
            scores.append(1.0 if not o["has_leader"] else 0.0)
            continue
        if not o["has_leader"]:
            scores.append(0.0)
            continue
        ok = True
        if (
            o["title_end"] is not None
            and o["leader_start"] is not None
            and o["title_end"] > o["leader_start"] + _X_TOL
        ):
            ok = False  # dots overlap the title
        if (
            o["leader_end"] is not None
            and o["page_x"] is not None
            and o["leader_end"] > o["page_x"] + _X_TOL
        ):
            ok = False  # dots run past the page column
        scores.append(1.0 if ok else 0.0)
    return sum(scores) / float(len(scores))


def _toc_continuation_page(sf, of):
    """Per-page TOC continuation x-fidelity (index-aligned wrapped lines).

    The follow-on lines of a multi-line entry must keep their anchor column
    (``toc_cont_x``) — a continuation dragged to a new column, or lost
    entirely, drops it.  ``1.0`` when neither side has continuations.
    """
    s_cont = [
        f["toc_cont_x"] for f in sf if f["is_toc_cont"] and f["toc_cont_x"] is not None
    ]
    o_cont = [
        f["toc_cont_x"] for f in of if f["is_toc_cont"] and f["toc_cont_x"] is not None
    ]
    if not s_cont:
        return 1.0  # source had no continuation lines to preserve
    if not o_cont:
        return 0.0  # source continuation lost entirely
    n = min(len(s_cont), len(o_cont))
    return _col_acc(list(zip(s_cont[:n], o_cont[:n])))


# -- 7F-5d: adaptive-TOC metrics -------------------------------------------
#
# These four metrics quantify the 7F-5a/5b adaptive-TOC contract on real
# extracted PDFs (source vs output, index-aligned like the other TOC checks):
#
# - ``toc_page_column_stability`` — the page-number column may not move at
#   all (strict epsilon, unlike the graded ``toc_page_x_accuracy``).
# - ``toc_adaptive_wrap_integrity`` — multi-line entries keep every wrapped
#   line anchored at the entry's title column, never duplicate a word, and
#   never lose title text (same-language render).
# - ``toc_adaptive_font_size`` — font size is stable when the entry does not
#   grow, and genuinely shrinks when the title could not fit its rendered
#   line count at the source size (SHRINK really happened).
# - ``toc_adaptive_overflow`` — an entry that cannot fit either shows explicit
#   overflow evidence (title words reaching the page column) or keeps its
#   full title text; a silently truncated title (CLIP) drops it.
#
# None of them imports the semantic detectors — pure extraction heuristics.


def _col_strict(pairs, eps=_PAGE_COL_EPS) -> float:
    """Fraction of pairs within a strict epsilon (column must not move)."""
    if not pairs:
        return 1.0
    return sum(1.0 for s, o in pairs if abs(s - o) <= eps) / float(len(pairs))


def _entry_groups(feats):
    """Group each TOC entry with its immediately following wrapped lines."""
    groups = []
    cur = None
    for f in feats:
        if f["is_toc"]:
            cur = {"entry": f, "conts": []}
            groups.append(cur)
        elif f["is_toc_cont"] and cur is not None:
            cur["conts"].append(f)
        else:
            cur = None
    return groups


def _title_words(feat):
    """Title words of an entry's first line (leader dots / page number out)."""
    ws = feat.get("_words") or []
    if not ws:
        return []
    return [w for w in ws[:-1] if not re.fullmatch(r"\.+", w["text"])]


def _group_title_words(group):
    """All title words of an entry: first line + every wrapped follow-on line."""
    words = list(_title_words(group["entry"]))
    for c in group["conts"]:
        words.extend(c.get("_words") or [])
    return words


def _text_fidelity(src_words, out_words):
    """Preservation score + mode; neutral ``1.0`` for translated text.

    Returns ``(score, mode)``: when the output words are (almost) a subset of
    the source title words — i.e. a same-language render — the score is the
    fraction of source words still present (a deleted / truncated word drops
    it).  When the output is translated (little word overlap) the score is
    neutral ``1.0`` because word-for-word fidelity is unverifiable.
    """
    s = [w["text"] for w in src_words]
    o = [w["text"] for w in out_words]
    if not s:
        return 1.0, "none"
    oset = set(o)
    cov = sum(1 for t in s if t in oset) / float(len(s))
    sset = set(s)
    out_in_src = (sum(1 for t in o if t in sset) / float(len(o))) if o else 1.0
    if out_in_src >= 0.9:
        return cov, "preserved"
    return 1.0, "translated"


def _entry_size(lines, y0, x0):
    """Font size of the text line at (≈y0, ≈x0); ``None`` when not found."""
    for ln in lines:
        b = ln["bbox"]
        if abs(b[1] - y0) <= 3.0 and abs(b[0] - x0) <= _X_TOL:
            return float(ln["size"])
    return None


def _entry_overflows(group) -> bool:
    """True when any title word of the entry crosses the page column."""
    e = group["entry"]
    page_x = float(e["page_x"] or 0.0)
    return any(w["bbox"][2] > page_x - _X_TOL for w in _group_title_words(group))


def _needs_shrink(group, src_size, out_size) -> bool:
    """True when the title could not fit its rendered line count at the source
    font size (word widths measured from the rendered bboxes, scaled) — i.e.
    SHRINK was required to achieve the observed wrap.
    """
    e = group["entry"]
    words = _group_title_words(group)
    if not words or out_size <= 0.0:
        return False
    avail = float(e["page_x"] or 0.0) - float(e["title_x"] or 0.0) - 2.0
    if avail <= 0.0:
        return False
    total = sum(w["bbox"][2] - w["bbox"][0] for w in words)
    total += _WORD_GAP_EM * out_size * max(0, len(words) - 1)
    lines_at_src = math.ceil(total * src_size / out_size / avail)
    return lines_at_src > 1 + len(group["conts"])


def _toc_page_column_stability_page(sf, of):
    """Strict page-number column stability (page_x must never move)."""
    s = [f["page_x"] for f in sf if f["is_toc"] and f["page_x"] is not None]
    o = [f["page_x"] for f in of if f["is_toc"] and f["page_x"] is not None]
    if not s:
        return 1.0
    if not o:
        return 0.0  # entries lost
    n = min(len(s), len(o))
    return _col_strict(list(zip(s[:n], o[:n])))


def _toc_adaptive_wrap_integrity_page(sf, of):
    """Per-page adaptive wrap integrity for TOC entries.

    For every output entry: every wrapped line must start at the entry's
    title anchor column (never dragged to a new column), no whole word may be
    duplicated across the entry's lines (CJK single glyphs are exempt —
    repeated characters are legitimate in Chinese titles), and — when the
    output is a same-language render of the source entry — no title word may
    be lost.  ``1.0`` when the page has no output entries.
    """
    s_groups = _entry_groups(sf)
    o_groups = _entry_groups(of)
    if not o_groups:
        return 1.0
    scores = []
    for k, og in enumerate(o_groups):
        e = og["entry"]
        twords = _title_words(e)
        if not twords:
            scores.append(1.0)
            continue
        anchor = twords[0]["bbox"][0]
        line_xs = [anchor] + [
            c["_words"][0]["bbox"][0] for c in og["conts"] if c.get("_words")
        ]
        anchored = all(abs(x - anchor) <= _TOC_CONT_TOL for x in line_xs)
        # no whole wrapped line may be a duplicate of another (double-render);
        # repeated words within lines are legitimate, so compare lines, not words
        line_texts = [" ".join(w["text"] for w in _title_words(e))]
        line_texts += [" ".join(w["text"] for w in c["_words"]) for c in og["conts"]]
        no_dup_line = len(line_texts) == len(set(line_texts))
        cov, mode = 1.0, "none"
        if k < len(s_groups):
            cov, mode = _text_fidelity(
                _group_title_words(s_groups[k]), _group_title_words(og)
            )
        text_ok = mode != "preserved" or cov >= 1.0
        scores.append(1.0 if (anchored and no_dup_line and text_ok) else 0.0)
    return sum(scores) / float(len(scores)) if scores else 1.0


def _toc_adaptive_font_size_page(sf, of, sp, op):
    """Per-page font-size fidelity for TOC entries.

    Font size may never grow; stays stable (within ``_FONT_SIZE_TOL``) when
    the entry's line count does not grow (no unjustified SHRINK); and must be
    genuinely smaller when the title could not fit its rendered line count at
    the source size (the SHRINK stage really ran).
    """
    s_groups = _entry_groups(sf)
    o_groups = _entry_groups(of)
    if not o_groups:
        return 1.0
    scores = []
    for k, og in enumerate(o_groups):
        e = og["entry"]
        out_size = _entry_size(op["lines"], e["y0"], e["title_x"])
        out_lines_n = 1 + len(og["conts"])
        if k < len(s_groups):
            se = s_groups[k]["entry"]
            src_size = _entry_size(sp["lines"], se["y0"], se["title_x"])
            src_lines_n = 1 + len(s_groups[k]["conts"])
        else:
            src_size, src_lines_n = out_size, out_lines_n
        if out_size is None or src_size is None:
            scores.append(1.0)
            continue
        ok = True
        if out_size > src_size + _FONT_SIZE_TOL:
            ok = False  # font must never increase
        elif out_lines_n == src_lines_n:
            ok = abs(out_size - src_size) <= _FONT_SIZE_TOL  # no unjustified shrink
        elif _needs_shrink(og, src_size, out_size):
            if out_size < src_size - _SHRINK_EVIDENCE_TOL:
                ok = True  # shrink really happened
            else:
                # shrink required but the font kept the source size: only
                # acceptable when the renderer re-wrapped within the width —
                # a naive font-size-only drop overflows the page column
                ok = not _entry_overflows(og)
        scores.append(1.0 if ok else 0.0)
    return sum(scores) / float(len(scores)) if scores else 1.0


def _toc_adaptive_overflow_page(sf, of):
    """Per-page honest-overflow detection for TOC entries.

    An entry that cannot fit is honest when its title words either reach the
    page column (explicit overflow evidence, drawn fully) or keep every word
    of a same-language source title.  A silently truncated title (CLIP —
    words lost, no overflow shown) drops it.
    """
    s_groups = _entry_groups(sf)
    o_groups = _entry_groups(of)
    if not o_groups:
        return 1.0
    scores = []
    for k, og in enumerate(o_groups):
        e = og["entry"]
        title = _title_words(e)
        page_x = float(e["page_x"] or 0.0)
        visible = bool(title) and any(w["bbox"][2] > page_x - _X_TOL for w in title)
        if visible:
            scores.append(1.0)
            continue
        cov, mode = 1.0, "none"
        if k < len(s_groups):
            cov, mode = _text_fidelity(
                _group_title_words(s_groups[k]), _group_title_words(og)
            )
        scores.append(1.0 if mode != "preserved" or cov >= 1.0 else 0.0)
    return sum(scores) / float(len(scores)) if scores else 1.0


def _list_toc_metrics(src_doc, out_doc):
    """List marker/content/continuation + wrap integrity + TOC columns."""
    content_pairs, cont_pairs, marker_pairs = [], [], []
    wrap_ok, nested_ok = [], []
    toc_leader_ok, toc_cont_acc = [], []
    toc_title_pairs, toc_page_pairs, toc_num_equal, toc_level_eq, toc_level_den = (
        [],
        [],
        0,
        0,
        0,
    )
    toc_page_strict, toc_wrap_ok, toc_font_ok, toc_overflow_ok = [], [], [], []
    for sp, op in zip(src_doc["pages"], out_doc["pages"]):
        sf = _line_word_features(sp["words"], sp["width"])
        of = _line_word_features(op["words"], op["width"])

        # list marker_x / content_x / continuation_x, matched by index
        smarkers = [
            f["marker_x"] for f in sf if f["is_item"] and f["marker_x"] is not None
        ]
        omarkers = [
            f["marker_x"] for f in of if f["is_item"] and f["marker_x"] is not None
        ]
        marker_pairs.extend(zip(smarkers, omarkers))

        sitems = [
            f["content_x"] for f in sf if f["is_item"] and f["content_x"] is not None
        ]
        oitems = [
            f["content_x"] for f in of if f["is_item"] and f["content_x"] is not None
        ]
        content_pairs.extend(zip(sitems, oitems))

        sconts = [f["cont_x"] for f in sf if f["is_cont"] and f["cont_x"] is not None]
        oconts = [f["cont_x"] for f in of if f["is_cont"] and f["cont_x"] is not None]
        cont_pairs.extend(zip(sconts, oconts))

        wrap_ok.append(_list_wrap_integrity_page(sf, of))
        nested_ok.append(_list_nested_accuracy_page(sf, of))
        toc_leader_ok.append(_toc_leader_integrity_page(sf, of))
        toc_cont_acc.append(_toc_continuation_page(sf, of))

        # 7F-5d adaptive-TOC metrics
        toc_page_strict.append(_toc_page_column_stability_page(sf, of))
        toc_wrap_ok.append(_toc_adaptive_wrap_integrity_page(sf, of))
        toc_font_ok.append(_toc_adaptive_font_size_page(sf, of, sp, op))
        toc_overflow_ok.append(_toc_adaptive_overflow_page(sf, of))

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
                toc_num_equal += int(
                    sentries[k]["page_number"] == oentries[k]["page_number"]
                )
                if k < len(s_levels) and k < len(o_levels):
                    toc_level_eq += int(s_levels[k] == o_levels[k])
                    toc_level_den += 1

    return {
        "list_marker_x_accuracy": round(_col_acc(marker_pairs), 4),
        "list_content_x_accuracy": round(_col_acc(content_pairs), 4),
        "list_continuation_x_accuracy": round(_col_acc(cont_pairs), 4),
        "list_wrap_integrity": round(
            sum(wrap_ok) / len(wrap_ok) if wrap_ok else 1.0, 4
        ),
        "list_nested_geometry_accuracy": round(
            sum(nested_ok) / len(nested_ok) if nested_ok else 1.0, 4
        ),
        "toc_title_x_accuracy": round(_col_acc(toc_title_pairs), 4),
        "toc_page_x_accuracy": round(_col_acc(toc_page_pairs), 4),
        "toc_page_number_accuracy": round(
            (toc_num_equal / len(toc_title_pairs)) if toc_title_pairs else 1.0, 4
        ),
        "toc_level_accuracy": round(
            (toc_level_eq / toc_level_den) if toc_level_den else 1.0, 4
        ),
        "toc_leader_integrity": round(
            sum(toc_leader_ok) / len(toc_leader_ok) if toc_leader_ok else 1.0, 4
        ),
        "toc_continuation_x_accuracy": round(
            sum(toc_cont_acc) / len(toc_cont_acc) if toc_cont_acc else 1.0, 4
        ),
        # 7F-5d adaptive-TOC metrics
        "toc_page_column_stability": round(
            sum(toc_page_strict) / len(toc_page_strict) if toc_page_strict else 1.0, 4
        ),
        "toc_adaptive_wrap_integrity": round(
            sum(toc_wrap_ok) / len(toc_wrap_ok) if toc_wrap_ok else 1.0, 4
        ),
        "toc_adaptive_font_size": round(
            sum(toc_font_ok) / len(toc_font_ok) if toc_font_ok else 1.0, 4
        ),
        "toc_adaptive_overflow": round(
            sum(toc_overflow_ok) / len(toc_overflow_ok) if toc_overflow_ok else 1.0, 4
        ),
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
        "list_marker_x_accuracy": lst["list_marker_x_accuracy"],
        "list_content_x_accuracy": lst["list_content_x_accuracy"],
        "list_continuation_x_accuracy": lst["list_continuation_x_accuracy"],
        "list_wrap_integrity": lst["list_wrap_integrity"],
        "list_nested_geometry_accuracy": lst["list_nested_geometry_accuracy"],
        "toc_title_x_accuracy": lst["toc_title_x_accuracy"],
        "toc_page_x_accuracy": lst["toc_page_x_accuracy"],
        "toc_page_number_accuracy": lst["toc_page_number_accuracy"],
        "toc_level_accuracy": lst["toc_level_accuracy"],
        "toc_leader_integrity": lst["toc_leader_integrity"],
        "toc_continuation_x_accuracy": lst["toc_continuation_x_accuracy"],
        "toc_page_column_stability": lst["toc_page_column_stability"],
        "toc_adaptive_wrap_integrity": lst["toc_adaptive_wrap_integrity"],
        "toc_adaptive_font_size": lst["toc_adaptive_font_size"],
        "toc_adaptive_overflow": lst["toc_adaptive_overflow"],
        "outline_destination_accuracy": _outline_metrics(source_doc, output_doc),
        "overflow_count": _overflow_count(output_doc),
        "code_preserved_bbox": _code_preserved_bbox(source_doc, output_doc),
        "_matched_lines": n,
    }
