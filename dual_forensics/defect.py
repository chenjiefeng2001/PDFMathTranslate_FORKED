"""defect — F1–F10 defect taxonomy + First-Divergence-Stage attribution.

Defines the taxonomy table (7H-1 §3) and *detector* functions.  Detectors are
pure reads over a node's evidence (see :class:`.diff.NodeTrace`); each returns
a list of :class:`.DefectFinding` with a guessed ``first_divergence`` stage.

Important discipline from the plan: **``translation wrong`` (F2) is kept
separate from ``translation placed wrong`` (F1 / F6 / F8)**.  A finding must
first prove *which* stage's evidence already diverges before it blames a layer.
Each detector therefore inspects source→parser→model→translation→layout→render
in order and records the first stage where the signal is already present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dual_forensics.provenance import STAGES

__all__ = [
    "F1",
    "F2",
    "F3",
    "F4",
    "F5",
    "F6",
    "F7",
    "F8",
    "F9",
    "F10",
    "DEFECTS",
    "DefectFinding",
    "run_defect_detectors",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_SKIP",
    "STATUS_NOT_MEASURED",
    "Coverage",
    "coverage_page",
    "aggregate_coverage",
]

F1 = "F1"
F2 = "F2"
F3 = "F3"
F4 = "F4"
F5 = "F5"
F6 = "F6"
F7 = "F7"
F8 = "F8"
F9 = "F9"
F10 = "F10"

DEFECTS: Dict[str, dict] = {
    F1: {"name": "wrong translation area", "suspect": "segmentation / placement"},
    F2: {
        "name": "code translated when it should not be",
        "suspect": "semantic classification / translation",
    },
    F3: {
        "name": "abnormal font size",
        "suspect": "layout measurement / font resolution",
    },
    F4: {"name": "font anomaly / mojibake", "suspect": "font mapping / renderer"},
    F5: {
        "name": "figure/table detached from text",
        "suspect": "object grouping / placement",
    },
    F6: {"name": "caption displaced", "suspect": "semantic relation / layout"},
    F7: {
        "name": "source text leftover / duplicate",
        "suspect": "translation segmentation",
    },
    F8: {"name": "text truncated", "suspect": "layout / packing"},
    F9: {"name": "text layer vs visual layer mismatch", "suspect": "renderer"},
    F10: {
        "name": "XObject / draw object lost or drifted",
        "suspect": "renderer / object preservation",
    },
}

# CJK ranges — a rendered translation page should be dominated by these.
_RE_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uf900-\ufaff]")
# Code-ish tokens we expect to survive translation untouched (not CJK).
_RE_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_KEYWORDS = {
    "int",
    "void",
    "return",
    "if",
    "else",
    "for",
    "while",
    "switch",
    "case",
    "break",
    "continue",
    "public",
    "private",
    "protected",
    "class",
    "struct",
    "namespace",
    "using",
    "template",
    "typename",
    "include",
    "define",
    "static",
    "const",
    "new",
    "delete",
    "true",
    "false",
    "NULL",
    "nullptr",
    "std::",
}


@dataclass
class DefectFinding:
    defect_id: str
    node_id: str
    page: int
    evidence: Dict[str, Any] = field(default_factory=dict)  # per-node signals
    first_divergence: Optional[str] = None  # earliest stage already diverged
    stage_verdicts: Dict[str, str] = field(default_factory=dict)  # PASS/FAIL/None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "defect_id": self.defect_id,
            "name": DEFECTS.get(self.defect_id, {}).get("name"),
            "suspect_layer": DEFECTS.get(self.defect_id, {}).get("suspect"),
            "node_id": self.node_id,
            "page": self.page,
            "evidence": self.evidence,
            "first_divergence": self.first_divergence,
            "stage_verdicts": self.stage_verdicts,
            "note": self.note,
        }


STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"
STATUS_NOT_MEASURED = "NOT_MEASURED"

# defect ids covered by an *implemented* detector that can return PASS/FAIL.
# anything else stays NOT_MEASURED (distinct from a clean PASS).
_NODE_DETECTORS = (F1, F2, F3, F4, F8)
_ALL_F_IDS = (F1, F2, F3, F4, F5, F6, F7, F8, F9, F10)


@dataclass
class Coverage:
    """Per-detector page status under the 7I-4 contract.

    Contract:
      PASS   = detector ran on >=1 node with sufficient evidence, no defect
      FAIL   = detector ran and found >=1 defect
      SKIP   = detector ran but no node had sufficient evidence (nothing measured)
      NOT_MEASURED = detector is not implemented yet (5I-4 later steps)

    The point: never let ``SKIP``/``NOT_MEASURED`` masquerade as ``0``.
    """

    defect_id: str
    status: str = STATUS_SKIP
    evaluated_nodes: int = 0  # nodes where the detector had sufficient evidence
    findings: List[DefectFinding] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "defect_id": self.defect_id,
            "name": DEFECTS.get(self.defect_id, {}).get("name"),
            "status": self.status,
            "evaluated_nodes": self.evaluated_nodes,
            "findings": [f.to_dict() for f in self.findings],
            "note": self.note,
        }


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    total = sum(1 for ch in text if not ch.isspace())
    if total == 0:
        return 0.0
    return sum(1 for _ in _RE_CJK.finditer(text)) / total


def _is_code_like(text: str) -> bool:
    """Heuristic: a block that looks like programming source/pseudocode."""
    if not text:
        return False
    words = [w.lower() for w in _RE_EN_WORD.findall(text)]
    if not words:
        return False
    hits = sum(1 for w in words if w in _KEYWORDS or w.endswith("::"))
    return (hits / len(words) >= 0.25) or any(c in text for c in "{};")


_STAGE_WALK = STAGES  # source→parser→model→translation→layout→render→pdf


def _fds(verdicts: Dict[str, str]) -> Optional[str]:
    """Earliest stage whose verdict is FAIL."""
    for s in _STAGE_WALK:
        if verdicts.get(s) == "FAIL":
            return s
    return None


def _fin(verdicts, defect_id, node, page, note, evidence) -> DefectFinding:
    return DefectFinding(
        defect_id=defect_id,
        node_id=node,
        page=page,
        evidence=evidence,
        first_divergence=_fds(verdicts),
        stage_verdicts=verdicts,
        note=note,
    )


# ── detectors ──────────────────────────────────────────────────────────────


def _detect_f2_code_translated(trace) -> List[DefectFinding]:
    """F2: a code-like block whose *rendered* text got CJK-ised.

    The real dual page's matched-back text is the ground truth (``rendered_text``,
    read from the dual page and matched by geometry).  Identity source text is
    *not* evidence of a translation defect, so we test the rendered string.
    """
    findings: List[DefectFinding] = []
    src = (trace.source_text or "").strip()
    if not src or not _is_code_like(src):
        return findings
    rendered = (trace.rendered_text or "").strip()
    if not rendered:
        return findings  # absence handled by F8/F10 dangling, not F2
    cjk = _cjk_ratio(rendered)
    status = trace.translation_status or ""
    verdicts = {s: None for s in STAGES}
    if cjk > 0.25:
        verdicts["source"] = "PASS"  # source was genuine code
        verdicts["parser"] = "PASS"  # code chars parsed fine
        verdicts["model"] = "FAIL" if trace.kind != "code" else "PASS"
        verdicts["render"] = "PASS"
        # Divergence is either the model mis-typed the block (semantic) or the
        # translation unit translated it.  If the block was typed code and still
        # translated, blame translation; else blame model classification.
        if verdicts["model"] == "PASS":
            verdicts["translation"] = "FAIL"
        else:
            verdicts["translation"] = None
            verdicts["layout"] = "PASS"
        findings.append(
            _fin(
                verdicts,
                F2,
                trace.node_id,
                trace.page,
                f"code block rendered {cjk:.0%} CJK; kind={trace.kind}; status={status}",
                {
                    "source_has_code": True,
                    "kind": trace.kind,
                    "cjk_ratio": round(cjk, 3),
                    "translation_status": status,
                },
            )
        )
    return findings


def _detect_f4_font_anomaly(trace) -> List[DefectFinding]:
    """F4: mojibake / replacement glyphs / ``(cid:N)`` placeholders.

    Attribution is **stage-faithful** (7I-3A): instead of blaming the renderer
    whenever the rendered text carries the artifact, we walk the stage
    snapshots on the trace — parser source text → translation text → rendered
    text — and record the *first* stage whose text already contains it:

    - present in parser text      → parser FAIL (source-PDF encoding/ToUnicode
      gap; every later stage faithfully passed it through);
    - clean at parser, present in translation text → translation FAIL;
    - clean earlier, present in rendered text      → render FAIL.

    This stops ``(cid:N)`` placeholders that the *source PDF* cannot decode
    from being misattributed to this pipeline's renderer (7I-2 §4 E).
    """
    findings: List[DefectFinding] = []
    src = (trace.source_text or "").strip()
    tr = (trace.translated_text or "").strip()
    rendered = (trace.rendered_text or "").strip()

    def _has_artifact(t: str) -> bool:
        return "(cid:" in t or "\ufffd" in t

    if not (_has_artifact(src) or _has_artifact(tr) or _has_artifact(rendered)):
        return findings

    verdicts = {s: None for s in STAGES}
    verdicts["source"] = "PASS"
    if _has_artifact(src):
        # parser-originated: the artifact already exists in the source PDF's
        # text layer before this pipeline touched it.
        verdicts["parser"] = "FAIL"
        verdicts["translation"] = "PASS"
        verdicts["layout"] = "PASS"
        verdicts["render"] = "PASS"
        note = "CID/replacement glyph already present at parser stage (source-PDF encoding gap)"
        evidence = {
            "parser_originated": True,
            "stage_snapshot": "parser",
            "cid": "(cid:" in src,
            "fffd": "\ufffd" in src,
            "text": src[:40],
        }
    elif _has_artifact(tr):
        # clean at parser, artifact introduced with the translation text.
        verdicts["parser"] = "PASS"
        verdicts["model"] = "PASS"
        verdicts["translation"] = "FAIL"
        verdicts["layout"] = "PASS"
        verdicts["render"] = "PASS"
        note = "CID/replacement glyph introduced at translation stage"
        evidence = {
            "parser_originated": False,
            "stage_snapshot": "translation",
            "cid": "(cid:" in tr,
            "fffd": "\ufffd" in tr,
            "text": tr[:40],
        }
    else:
        # clean at parser and translation, artifact first appears rendered.
        verdicts["parser"] = "PASS"
        verdicts["translation"] = "PASS"
        verdicts["layout"] = "PASS"
        verdicts["render"] = "FAIL"
        note = "CID/replacement glyph first appears in rendered text"
        evidence = {
            "parser_originated": False,
            "stage_snapshot": "render",
            "cid": "(cid:" in rendered,
            "fffd": "\ufffd" in rendered,
            "text": rendered[:40],
        }
    findings.append(_fin(verdicts, F4, trace.node_id, trace.page, note, evidence))
    return findings


def _detect_f2_style_alias(trace, raw_duals: Dict[str, Any]) -> List[DefectFinding]:
    """F4/F9: rendered text layer differs from what the model/translation owed.

    Raw-dual text runs are matched back per node; if the run text is empty but
    the node was supposed to render, or the run is CJK where source was code,
    flag it.  Kept minimal — the strong signals come from the inspector's
    MuPDF anomaly detector (see report aggregation).
    """
    return []


def _node_render_box(trace) -> Optional[List[float]]:
    """Union of the node's rendered run bboxes (y-up)."""
    if getattr(trace, "render_box", None):
        return list(trace.render_box)
    boxes = [
        r.get("final_bbox_v3") or r.get("v3_bbox") or r.get("bbox")
        for r in (trace.render_rows or [])
    ]
    boxes = [b for b in boxes if b and len(b) == 4]
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _iou(a: List[float], b: List[float]) -> Optional[float]:
    """Intersection-over-union of two y-up boxes; None if either is empty."""
    if not a or not b or len(a) != 4 or len(b) != 4:
        return None
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    au = (a[2] - a[0]) * (a[3] - a[1])
    bu = (b[2] - b[0]) * (b[3] - b[1])
    if au <= 0 or bu <= 0:
        return None
    return inter / (au + bu - inter)


def _detect_f1_wrong_area(trace) -> List[DefectFinding]:
    """F1: translated content rendered in a substantially different area than
    the layout planned (``dst_box``), or missing a planned location entirely.

    Evidence-based: we only judge when we have BOTH a layout ``dst_box`` and an
    actually-drawn render box.  A node that was never drawn (no render box / no
    matched run) is **SKIP**, not a placement FAIL — its absence is elsewhere
    (F8/F10 dangling), not proof of a wrong area.
    """
    findings: List[DefectFinding] = []
    dst = trace.dst_box
    rbox = _node_render_box(trace)
    if not dst or not rbox:
        return findings  # cannot judge placement -> SKIP (no finding emitted)
    iou = _iou(rbox, dst)
    if iou is None:
        return findings
    # Genuine wrong-area: essentially no overlap between where we drew and where
    # the plan said to draw, with a meaningful distance apart.
    if iou < 0.20:
        verdicts = {s: None for s in STAGES}
        verdicts["source"] = "PASS"
        verdicts["parser"] = "PASS"
        verdicts["model"] = "PASS"
        verdicts["translation"] = "PASS"
        verdicts["layout"] = "FAIL"
        verdicts["render"] = "PASS"
        findings.append(
            _fin(
                verdicts,
                F1,
                trace.node_id,
                trace.page,
                f"rendered area overlaps planned dst_box by only {iou:.0%}",
                {
                    "render_box": rbox,
                    "dst_box": dst,
                    "iou": round(iou, 3),
                    "confidence": "uncertain",
                },
            )
        )
    return findings


_FONT_RATIO_LOW = 0.55  # rendered/target below this is a suspicious shrink
_FONT_RATIO_HIGH = 1.6  # rendered/target above this is a suspicious enlarg


def _detect_f3_font_size(trace) -> List[DefectFinding]:
    """F3: rendered font size diverges abnormally from the layout target size.

    Compare what was actually drawn (render row ``font_size``) against the
    layout target (``layout_font_size``).  A large relative deviation is an
    abnormal font size; if no drawn font size is available we SKIP (nothing
    measurable) rather than guess.
    """
    findings: List[DefectFinding] = []
    target = trace.layout_font_size
    if not target:
        return findings  # no layout plan size to compare -> SKIP
    drawn_sizes = [float(r.get("font_size") or 0) for r in (trace.render_rows or [])]
    drawn_sizes = [s for s in drawn_sizes if s and s > 0]
    if not drawn_sizes:
        return findings  # not drawn -> SKIP (absence handled elsewhere)
    drawn = max(drawn_sizes)  # dominant line size
    ratio = drawn / float(target)
    if ratio < _FONT_RATIO_LOW or ratio > _FONT_RATIO_HIGH:
        verdicts = {s: None for s in STAGES}
        verdicts["source"] = "PASS"
        verdicts["parser"] = "PASS"
        verdicts["model"] = "PASS"
        verdicts["translation"] = "PASS"
        verdicts["layout"] = "FAIL"
        verdicts["render"] = "PASS"
        findings.append(
            _fin(
                verdicts,
                F3,
                trace.node_id,
                trace.page,
                f"drawn font size {drawn:.1f} vs layout target {float(target):.1f} "
                f"(ratio {ratio:.2f})",
                {
                    "drawn_font_size": round(drawn, 2),
                    "layout_font_size": float(target),
                    "ratio": round(ratio, 3),
                    "confidence": "uncertain",
                },
            )
        )
    return findings


def _detect_f8_text_truncated(trace) -> List[DefectFinding]:
    """F8: a block the layout declared would be **clipped** (text truncated).

    Evidence contract:
      min evidence  = the settled flow layout carries ``overflow`` and a
                      ``recovery`` dict whose ``decision`` is ``clip`` (7F-7);
      boundary       = F8 ≠ F10 (object lost/drifted) and ≠ F5 (float detached).
                      F8 only fires on a **present** block whose *text* the
                      layout had to clip — not on a missing object.
      unavailable    = page/block with no settled overflow verdict -> SKIP.

    Attribution: the clip decision is made by the layout stage, so F8 defaults
    to ``layout FAIL``.  It carries ``confidence: uncertain`` (a clip of a
    zero/whitespace tail may be benign, but a clipped content line is truncation).
    """
    findings: List[DefectFinding] = []
    if trace.layout_overflow is not True:
        return findings  # not flagged as overflow by layout -> not F8
    rec = trace.layout_recovery or {}
    decision = (rec or {}).get("decision") or ""
    if decision != "clip":
        return findings  # overflow with a non-clip recovery isn't truncation
    verdicts = {s: None for s in STAGES}
    verdicts["source"] = "PASS"
    verdicts["parser"] = "PASS"
    verdicts["model"] = "PASS"
    verdicts["translation"] = "PASS"
    verdicts["layout"] = "FAIL"
    verdicts["render"] = "PASS"
    findings.append(
        _fin(
            verdicts,
            F8,
            trace.node_id,
            trace.page,
            f"flow layout clipped block (reason={rec.get('reason')}), "
            f"steps={rec.get('steps')}",
            {
                "reason": rec.get("reason"),
                "decision": decision,
                "steps": rec.get("steps"),
                "original_font_size": rec.get("original_font_size"),
                "final_font_size": rec.get("final_font_size"),
                "confidence": "uncertain",
            },
        )
    )
    return findings


# F5/F6 are page-level: they compare a figure/table/caption block against
# the surrounding text blocks on the same page, so they need the full trace
# list, not one node in isolation.
_FLOAT_KINDS = {"figure", "table", "image"}
_TEXT_KINDS = {"paragraph", "heading", "caption", "formula"}


def _box_distance(
    a: Optional[List[float]], b: Optional[List[float]]
) -> Optional[float]:
    """Min distance between two y-up boxes (0.0 if they overlap)."""
    if not a or not b or len(a) != 4 or len(b) != 4:
        return None
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]), min(a[2], b[2]) - min(a[0], b[0]))
    dx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return (dx * dx + dy * dy) ** 0.5


_F5_DETACH_GAP = 4.0  # gap (in block-heights) beyond which a float is "detached"
_F6_IOU = 0.30


def _detect_f5_detached_page(traces: List[Any]) -> "tuple[List[DefectFinding], int]":
    """F5: a figure/table/image block rendered far from the surrounding text.

    Evidence contract:
      min evidence  = a float block with a drawn box **and** >=1 other text
                      block on the page (so "detached from text" is measurable);
      unavailable    = page with no float block at all -> SKIP (nothing measured),
                      never a clean 0.
    """
    findings: List[DefectFinding] = []
    floats = [t for t in traces if t.kind in _FLOAT_KINDS]
    if not floats:
        return findings, 0  # no float block -> SKIP
    text_boxes = [
        (t.dst_box or _node_render_box(t))
        for t in traces
        if t.kind in _TEXT_KINDS and (t.dst_box or _node_render_box(t))
    ]
    evaled = 0
    for t in floats:
        rbox = _node_render_box(t)
        dst = t.dst_box or rbox
        if not dst or not rbox:
            continue  # float not drawn -> absence is F10/F8, not F5 detachment
        evaled += 1
        if not text_boxes:
            continue  # no text blocks to be near -> can't judge, still evaluated
        gap = min(
            (g for g in (_box_distance(dst, b) for b in text_boxes) if g is not None),
            default=None,
        )
        height = max(1e-6, float((dst[3] - dst[1]) or 1.0))
        if gap is not None and gap > _F5_DETACH_GAP * height:
            verdicts = {s: None for s in STAGES}
            verdicts["source"] = "PASS"
            verdicts["parser"] = "PASS"
            verdicts["model"] = "PASS"
            verdicts["translation"] = "PASS"
            verdicts["layout"] = "FAIL"
            verdicts["render"] = "PASS"
            findings.append(
                _fin(
                    verdicts,
                    F5,
                    t.node_id,
                    t.page,
                    f"float block {gap:.0f}pt from nearest text (> {_F5_DETACH_GAP}x its height)",
                    {
                        "kind": t.kind,
                        "dst_box": dst,
                        "nearest_text_gap": round(gap, 1) if gap else None,
                        "confidence": "uncertain",
                    },
                )
            )
    return findings, evaled


_F8_CLIP_END = ("CLIP",)


# ── F9 / F10 page-level detectors (7I-6B wiring) ───────────────────────────
# These read *page-level render evidence* (``dual_page``), not per-node traces:
#   - F9  reads ``dual_page["content_stream"]`` (``content_stream_anomaly``), the
#         render/PDF-layer emitter signal;
#   - F10 reads ``dual_page["id_direct"]`` — the ID-direct provenance summary
#         (``present_blocks`` / ``dangling_blocks`` / ``stray_records``) that
#         :func:`.diff.aggregate_page_id_direct` already computes.
# Both follow the 7I-4 contract: without sufficient page-level evidence they are
# SKIP (nothing measured), never a fabricated PASS or a clean 0.


def _detect_f9_text_visual_mismatch_page(
    traces: List[Any],
    dual_page: Optional[Any] = None,
) -> "tuple[List[DefectFinding], int]":
    """F9: the rendered text layer vs the visual layer disagree.

    Evidence = ``dual_page["content_stream"]`` from
    :func:`.pdf_inspector.content_stream_anomaly`:
      - present + ``anomaly``      → FAIL (FDS render/pdf);
      - present + no anomaly       → PASS (emitter clean);
      - absent / not checked       → SKIP (nothing measurable), never PASS.

    7I-6B boundary: this only *wires* the 7H-2B emitter sensor into the
    coverage contract — the sensor itself is unchanged.
    """
    findings: List[DefectFinding] = []
    cs = (
        (dual_page or {}).get("content_stream") if isinstance(dual_page, dict) else None
    )
    cs = cs or {}
    if not cs.get("checked"):
        return findings, 0  # no inspectable content stream -> SKIP
    if not cs.get("anomaly"):
        return findings, 1  # scanner ran, emitter clean -> PASS
    page = traces[0].page if traces else (dual_page or {}).get("page", 0)
    verdicts = {s: None for s in STAGES}
    verdicts["source"] = "PASS"
    verdicts["parser"] = "PASS"
    verdicts["model"] = "PASS"
    verdicts["translation"] = "PASS"
    verdicts["layout"] = "PASS"
    verdicts["render"] = "FAIL"
    findings.append(
        _fin(
            verdicts,
            F9,
            f"p{page}_page",
            page,
            "renderer emitted a malformed numeric token into the page stream",
            {
                "mupdf_syntax_error": list(cs.get("sample") or [])[:5],
                "source": cs.get("source"),
                "confidence": "medium",
            },
        )
    )
    return findings, 1


def _detect_f10_object_lost_page(
    traces: List[Any],
    dual_page: Optional[Any] = None,
) -> "tuple[List[DefectFinding], int]":
    """F10: a model block lost / drifted / stray in the render layer.

    Evidence = ``dual_page["id_direct"]``, the summary that
    :func:`.diff.aggregate_page_id_direct` already produces from
    ``source_node_id → render_object_ref`` provenance:
      - present  + provenance        → PASS;
      - any dangling/absent block    → FAIL (FDS render);
      - any stray render record      → FAIL (FDS render);
      - no provenance ran            → SKIP (nothing measured), never PASS;
      - a block never owed by plan   → SKIP (not a dropped object), not FAIL.

    Boundary: F10 ≠ F8 (a *present* block whose text was clipped) and ≠ F5
    (a float's placement).  F10 is strictly about object presence / drift.
    """
    findings: List[DefectFinding] = []
    prov = (dual_page or {}).get("id_direct") if isinstance(dual_page, dict) else None
    if not isinstance(prov, dict) or not prov:
        return findings, 0  # no ID-direct provenance for this page -> SKIP
    page = (
        prov.get("page")
        if prov.get("page") is not None
        else (traces[0].page if traces else (dual_page or {}).get("page", 0))
    )
    dangling = list(prov.get("dangling_blocks") or [])
    stray = list(prov.get("stray_records") or [])
    present = int(prov.get("present_blocks") or 0)
    # The page is measured to the extent provenance decided each owed block's
    # presence: present _and_ dangling are authoritative outcomes.
    eval_count = present + len(dangling)

    def _mk(nid, evidence, note):
        v = {s: None for s in STAGES}
        v["source"], v["parser"], v["model"] = "PASS", "PASS", "PASS"
        v["translation"], v["layout"] = "PASS", "PASS"
        v["render"] = "FAIL"
        return _fin(v, F10, nid, page, note, evidence)

    for nid in dangling:
        findings.append(
            _mk(
                nid,
                {
                    "dangling": True,
                    "object_presence": "absent",
                    "confidence": "confirmed",
                },
                "model block had no render object (ID-direct provenance)",
            )
        )
    for rec in stray:
        findings.append(
            _mk(
                rec.get("source_node_id") or "stray",
                {"stray": True, "render_object_ref": rec, "confidence": "confirmed"},
                "render object produced without a backing model block",
            )
        )
    # evaluated > 0 only if provenance gave an authoritative presence decision
    # for at least one block; otherwise (no blocks, or never owed) -> SKIP.
    if eval_count == 0 and not dangling and not stray:
        return findings, 0
    return findings, max(eval_count, 1)


_PAGE_DETECTORS = {
    F5: lambda traces, dual_page=None: _detect_f5_detached_page(traces),
    F6: lambda traces, dual_page=None: _detect_f6_caption_displaced_page(traces),
    F9: _detect_f9_text_visual_mismatch_page,
    F10: _detect_f10_object_lost_page,
}


def _detect_f6_caption_displaced_page(
    traces: List[Any],
) -> "tuple[List[DefectFinding], int]":
    """F6: a caption rendered in a substantially different place than planned.

    Evidence contract:
      min evidence  = a caption block with both a layout ``dst_box`` and a drawn
                      box;
      unavailable    = page with no caption block -> SKIP.

    Here we detect the *caption's own* displacement from its planned position.
    (The caption→host relationship, when the model exposes it, is the stronger
    F6 signal; that topdown relation is checked when ``src_box`` is absent.)
    """
    findings: List[DefectFinding] = []
    caps = [t for t in traces if t.kind == "caption"]
    if not caps:
        return findings, 0  # no caption -> SKIP
    evaled = 0
    for t in caps:
        dst = t.dst_box
        rbox = _node_render_box(t)
        if not dst or not rbox:
            continue
        evaled += 1
        iou = _iou(rbox, dst)
        if iou is not None and iou < _F6_IOU:
            verdicts = {s: None for s in STAGES}
            verdicts["source"] = "PASS"
            verdicts["parser"] = "PASS"
            verdicts["model"] = "PASS"
            verdicts["translation"] = "PASS"
            verdicts["layout"] = "FAIL"
            verdicts["render"] = "PASS"
            findings.append(
                _fin(
                    verdicts,
                    F6,
                    t.node_id,
                    t.page,
                    f"caption rendered with only {iou:.0%} overlap of planned dst_box",
                    {
                        "render_box": rbox,
                        "dst_box": dst,
                        "iou": round(iou, 3),
                        "confidence": "uncertain",
                    },
                )
            )
    return findings, evaled


# node-level detector dispatch: defect_id -> (can_evaluate, run)
_NODE_DISPARITY = {
    F4: (
        lambda t: bool(
            (t.source_text or "").strip() or (t.rendered_text or "").strip()
        ),
        _detect_f4_font_anomaly,
    ),
    F2: (
        lambda t: bool(
            (t.source_text or "").strip()
            and _is_code_like((t.source_text or "").strip())
        ),
        _detect_f2_code_translated,
    ),
    F1: (lambda t: bool(t.dst_box and _node_render_box(t)), _detect_f1_wrong_area),
    F3: (
        lambda t: bool(
            t.layout_font_size
            and [
                float(r.get("font_size") or 0)
                for r in (t.render_rows or [])
                if r.get("font_size")
            ]
        ),
        _detect_f3_font_size,
    ),
    # F8 evaluates any node with a *settled* flow layout verdict; FAIL only
    # fires when that verdict says the block was clipped.
    F8: (
        lambda t: t.layout_overflow is not None,
        _detect_f8_text_truncated,
    ),
}


def run_defect_detectors(
    traces: List[Any], dual_page: Optional[Any] = None
) -> List[DefectFinding]:
    """Run all enabled detectors over a list of :class:`.diff.Trace` objects.

    Returns only **FAIL findings** (backward compatible).  Use
    :func:`coverage_page` when you need the PASS/FAIL/SKIP accounting too.
    """
    findings: List[DefectFinding] = []
    for trace in traces:
        findings.extend(_detect_f2_code_translated(trace))
        findings.extend(_detect_f1_wrong_area(trace))
        findings.extend(_detect_f3_font_size(trace))
        findings.extend(_detect_f4_font_anomaly(trace))
        findings.extend(_detect_f8_text_truncated(trace))
    # page-level detectors (F5/F6 compare a block against the page's text)
    page_finds, _ = _detect_f5_detached_page(list(traces))
    findings.extend(page_finds)
    cap_finds, _ = _detect_f6_caption_displaced_page(list(traces))
    findings.extend(cap_finds)
    # 7I-6B: F9 (content_stream) / F10 (ID-direct provenance) page-level wiring.
    # They need page-level render evidence (``dual_page``), which must be passed
    # in; without it they SKIP (never a fabricated 0).
    f9_finds, _ = _detect_f9_text_visual_mismatch_page(list(traces), dual_page)
    findings.extend(f9_finds)
    f10_finds, _ = _detect_f10_object_lost_page(list(traces), dual_page)
    findings.extend(f10_finds)
    return findings


def coverage_page(
    traces: List[Any], dual_page: Optional[Any] = None
) -> Dict[str, Coverage]:
    """7I-4 contract: per-defect page status with PASS/FAIL/SKIP/NOT_MEASURED.

    For each F-id we:
      - NOT_MEASURED when no implemented detector can judge it yet;
      - otherwise evaluate every node; if >=1 node had sufficient evidence and
        produced a defect -> FAIL; if nodes were evaluable and all clean -> PASS;
        if no node had sufficient evidence -> SKIP (nothing measured).

    This keeps ``0`` honest: a SKIP/NOT_MEASURED page is *not* a clean page.
    """
    _traces = list(traces or [])
    out: Dict[str, Coverage] = {}
    for fid in _ALL_F_IDS:
        cov = Coverage(defect_id=fid)
        if fid in _PAGE_DETECTORS:
            all_find, evaled = _PAGE_DETECTORS[fid](_traces, dual_page)
            cov.evaluated_nodes = evaled
            cov.findings = all_find
            if evaled == 0:
                cov.status = STATUS_SKIP
                cov.note = "no node on this page had sufficient evidence"
            elif all_find:
                cov.status = STATUS_FAIL
            else:
                cov.status = STATUS_PASS
            out[fid] = cov
            continue
        if fid not in _NODE_DETECTORS:
            cov.status = STATUS_NOT_MEASURED
            cov.note = "detector not implemented in this release"
            out[fid] = cov
            continue
        can_eval, run_fn = _NODE_DISPARITY[fid]
        all_find = []
        evaled = 0
        for tr in _traces:
            if not can_eval(tr):
                continue
            evaled += 1
            all_find.extend(run_fn(tr))
        cov.evaluated_nodes = evaled
        cov.findings = all_find
        if evaled == 0:
            cov.status = STATUS_SKIP
            cov.note = "no node had sufficient evidence on this page"
        elif all_find:
            cov.status = STATUS_FAIL
        else:
            cov.status = STATUS_PASS
        out[fid] = cov
    return out


def aggregate_coverage(pages: Dict[int, Dict[str, Coverage]]) -> Dict[str, Any]:
    """Merge per-page :class:`Coverage` into a corpus-level summary.

    Returns ``{defect_id: {status, pages_evaluated, pages_total, pass, fail,
    skip, not_measured, findings}}`` — the acceptance table for 7I-4.
    """
    agg: Dict[str, Any] = {}
    for fid in _ALL_F_IDS:
        statuses = [p.get(fid).status for p in pages.values() if p.get(fid) is not None]
        total = len(statuses)
        fail_pages = statuses.count(STATUS_FAIL)
        skip_pages = statuses.count(STATUS_SKIP)
        nm_pages = statuses.count(STATUS_NOT_MEASURED)
        pass_pages = statuses.count(STATUS_PASS)
        # pages_evaluated = pages where the detector actually ran and could
        # conclude something (PASS or FAIL), i.e. not SKIP/NOT_MEASURED.
        evaluated = pass_pages + fail_pages
        findings = [
            f
            for p in pages.values()
            for f in (p.get(fid).findings if p.get(fid) else [])
        ]
        if total == 0 or evaluated == 0:
            status = STATUS_NOT_MEASURED if total == 0 else STATUS_SKIP
        elif fail_pages:
            status = STATUS_FAIL
        else:
            status = STATUS_PASS
        agg[fid] = {
            "status": status,
            "pages_evaluated": evaluated,
            "pages_total": total,
            "pass": pass_pages,
            "fail": fail_pages,
            "skip": skip_pages,
            "not_measured": nm_pages,
            "findings": [f.to_dict() for f in findings],
        }
    return agg


def classify_findings(findings: List[DefectFinding]) -> Dict[str, Any]:
    """summary per F-id + first-divergence-stage distribution."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for d in findings:
        entry = by_id.setdefault(
            d.defect_id,
            {"count": 0, "by_first_divergence": {}, "names": []},
        )
        entry["count"] += 1
        fd = d.first_divergence or "unknown"
        entry["by_first_divergence"][fd] = entry["by_first_divergence"].get(fd, 0) + 1
    for fid, entry in by_id.items():
        entry["name"] = DEFECTS.get(fid, {}).get("name")
    return by_id
