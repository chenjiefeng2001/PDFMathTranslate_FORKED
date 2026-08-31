"""7I-4-1/4-2 — Detector Coverage Completion: contract + F1/F3/F5/F6 tests.

The contract (the core methodological point of 7I-4) is:

    PASS          = detector ran on >=1 node with sufficient evidence, no defect
    FAIL          = detector ran and found a defect
    SKIP          = detector ran but no node had sufficient evidence
    NOT_MEASURED  = detector not implemented yet

Crucially **SKIP / NOT_MEASURED must never be reported as a clean ``0``**.
These tests pin that down, plus genuine PASS/FAIL behaviour of the
F1 (wrong translation area), F3 (abnormal font size) and page-level
F5 (figure/table detached from text) / F6 (caption displaced) detectors.
"""

from __future__ import annotations

from dual_forensics.defect import (
    STATUS_FAIL,
    STATUS_NOT_MEASURED,
    STATUS_PASS,
    STATUS_SKIP,
    aggregate_coverage,
    coverage_page,
    run_defect_detectors,
)
from dual_forensics.diff import Trace


def _trace(**kw):
    base = dict(
        node_id="p0_0",
        page=0,
        kind="paragraph",
        source_text="some body text",
        translated_text="一些正文",
        translation_status="translated",
    )
    base.update(kw)
    return Trace(
        **base
    )  # ── contract: SKIP/NOT_MEASURED are honest, never a clean 0 ───────────────


def test_not_measured_defects_do_not_read_as_zero():
    # A page with only a plain paragraph has no F1/F3 evidence → SKIP, no
    # float/caption → F5/F6 SKIP, F8 no settled verdict → SKIP, no dual_page
    # (F9/F10 wired but no content-stream/provenance evidence) → SKIP.
    cov = coverage_page([_trace(dst_box=None, layout_font_size=None, kind="paragraph")])
    # no dst_box / no drawn box → F1 cannot judge → SKIP
    assert cov["F1"].status == STATUS_SKIP
    # no layout_font_size / no drawn font → F3 cannot judge → SKIP
    assert cov["F3"].status == STATUS_SKIP
    # no figure/table block → F5 nothing to measure → SKIP
    assert cov["F5"].status == STATUS_SKIP
    # no caption block → F6 nothing to measure → SKIP
    assert cov["F6"].status == STATUS_SKIP
    # no settled flow layout verdict → F8 nothing to measure → SKIP
    assert cov["F8"].status == STATUS_SKIP
    # wired in 7I-6B but a page without dual_page evidence → SKIP (never PASS)
    assert cov["F9"].status == STATUS_SKIP
    assert cov["F10"].status == STATUS_SKIP
    # the only not-implemented detector remains explicitly NOT_MEASURED
    assert cov["F7"].status == STATUS_NOT_MEASURED
    # a SKIP page contributes 0 to "pages_evaluated", so aggregate can't
    # claim coverage it does not have.
    agg = aggregate_coverage({0: cov})
    assert agg["F1"]["pages_evaluated"] == 0
    assert agg["F1"]["status"] == STATUS_SKIP
    assert agg["F5"]["pages_evaluated"] == 0


def test_empty_page_aggregate_not_measured():
    # Entirely no nodes at all → detector coverage must not fabricate a PASS.
    agg = aggregate_coverage({})
    mangd = agg["F1"]
    assert mangd["status"] in (STATUS_NOT_MEASURED, STATUS_SKIP)
    assert mangd["pages_evaluated"] == 0


# ── F1 wrong translation area ──────────────────────────────────────────────


def _drawn_trace(dst, rbox, layout_font_size=12.0):
    rows = [{"text": "x", "font_size": layout_font_size, "v3_bbox": rbox}]
    return _trace(
        dst_box=dst,
        render_box=rbox,
        render_rows=rows,
        layout_font_size=layout_font_size,
    )


def test_f1_pass_when_drawn_box_matches_plan():
    tr = _drawn_trace([0, 0, 100, 50], [0, 0, 100, 50])
    cov = coverage_page([tr])
    assert cov["F1"].status == STATUS_PASS
    # detector actually evaluated the node (has both dst_box and drawn box)
    assert cov["F1"].evaluated_nodes == 1
    assert run_defect_detectors([tr]) == []


def test_f1_fail_on_genuine_wrong_area():
    tr = _drawn_trace([0, 0, 100, 50], [400, 400, 500, 450])  # far away (y-up)
    cov = coverage_page([tr])
    assert cov["F1"].status == STATUS_FAIL
    assert cov["F1"].findings[0].first_divergence == "layout"
    finds = run_defect_detectors([tr])
    assert len(finds) == 1 and finds[0].defect_id == "F1"


def test_f1_skip_when_dst_missing():
    tr = _trace(dst_box=None, render_box=[0, 0, 10, 10], render_rows=[{"text": "x"}])
    cov = coverage_page([tr])
    assert cov["F1"].status == STATUS_SKIP


# ── F3 abnormal font size ──────────────────────────────────────────────────


def test_f3_pass_when_drawn_size_matches_target():
    tr = _drawn_trace([0, 0, 100, 50], [0, 0, 100, 50], layout_font_size=12.0)
    cov = coverage_page([tr])
    assert cov["F3"].status == STATUS_PASS
    assert cov["F3"].evaluated_nodes == 1


def test_f3_fail_on_abnormal_font_ratio():
    tr = _drawn_trace([0, 0, 100, 50], [0, 0, 100, 50], layout_font_size=12.0)
    tr.render_rows = [{"text": "x", "font_size": 48.0}]  # 4x the target
    cov = coverage_page([tr])
    assert cov["F3"].status == STATUS_FAIL
    finds = run_defect_detectors([tr])
    assert len(finds) == 1 and finds[0].defect_id == "F3"


def test_f3_skip_when_no_layout_font_target():
    tr = _trace(layout_font_size=None, render_rows=[{"text": "x", "font_size": 12.0}])
    cov = coverage_page([tr])
    assert cov["F3"].status == STATUS_SKIP


def test_f3_skip_when_nothing_drawn():
    tr = _trace(layout_font_size=12.0, render_rows=[])
    cov = coverage_page([tr])
    assert cov["F3"].status == STATUS_SKIP


# ── aggregate across pages ─────────────────────────────────────────────────


def test_aggregate_counts_only_evaluated_pages():
    p0 = _drawn_trace([0, 0, 100, 50], [0, 0, 100, 50])  # clean F1+F3
    cov = {
        0: coverage_page([p0]),
        1: coverage_page([_trace(dst_box=None, layout_font_size=None)]),
    }
    agg = aggregate_coverage(cov)
    assert agg["F1"]["pages_evaluated"] == 1  # only page 0 could be judged
    assert agg["F1"]["status"] == STATUS_PASS
    assert agg["F1"]["pages_total"] == 2


def test_coverage_to_dict_roundtrip():
    cov = coverage_page([_drawn_trace([0, 0, 10, 10], [0, 0, 10, 10])])["F1"]
    d = cov.to_dict()
    assert d["defect_id"] == "F1" and d["status"] == STATUS_PASS
    assert "findings" in d and "note" in d


# ── F5 figure/table detached from text (page-level) ────────────────────────


def _float_drawn(kind, dst, rbox, node="p0_9", src=None):
    return _trace(
        node_id=node,
        kind=kind,
        dst_box=dst,
        render_box=rbox,
        render_rows=[{"text": "x", "font_size": 12.0, "v3_bbox": rbox}],
        src_box=src or dst,
    )


def test_f5_pass_when_float_adjacent_to_text():
    # figure at y 0..100; paragraph right below it at y 120..140 -> gap 20pt,
    # well within 4x the figure height.  Both blocks present -> PASS.
    fig = _float_drawn("figure", [100, 0, 200, 100], [100, 0, 200, 100])
    par = _trace(
        kind="paragraph",
        dst_box=[100, 120, 200, 140],
        render_rows=[{"text": "t", "font_size": 12.0, "v3_bbox": [100, 120, 200, 140]}],
    )
    cov = coverage_page([fig, par])
    assert cov["F5"].status == STATUS_PASS
    assert cov["F5"].evaluated_nodes == 1
    assert run_defect_detectors([fig, par]) == []


def test_f5_fail_when_float_detached_from_text():
    # figure at y 0..100, nearest text far away at y 600 -> gap >> 4x height.
    fig = _float_drawn("table", [100, 0, 200, 100], [100, 0, 200, 100])
    par = _trace(
        kind="paragraph",
        dst_box=[100, 600, 200, 620],
        render_rows=[{"text": "t", "font_size": 12.0, "v3_bbox": [100, 600, 200, 620]}],
    )
    cov = coverage_page([fig, par])
    assert cov["F5"].status == STATUS_FAIL
    finds = run_defect_detectors([fig, par])
    assert any(f.defect_id == "F5" for f in finds), finds


def test_f5_skip_when_no_float_block():
    par = _trace(
        kind="paragraph",
        dst_box=[0, 0, 100, 50],
        render_rows=[{"text": "t", "v3_bbox": [0, 0, 100, 50]}],
    )
    cov = coverage_page([par])
    assert cov["F5"].status == STATUS_SKIP


def test_f5_skip_when_float_not_drawn():
    # float block exists but was never drawn; absence handled by F10/F8.
    fig = _trace(kind="figure", dst_box=[100, 0, 200, 100], render_rows=[])
    cov = coverage_page([fig])
    assert cov["F5"].status == STATUS_SKIP


# ── F6 caption displaced (page-level) ──────────────────────────────────────


def _caption(dst, rbox, node="p0_9"):
    return _trace(
        node_id=node,
        kind="caption",
        source_text="Figure 1",
        translated_text="图 1",
        dst_box=dst,
        render_box=rbox,
        render_rows=[{"text": "图 1", "font_size": 9.0, "v3_bbox": rbox}],
    )


def test_f6_pass_when_caption_at_planned_position():
    cap = _caption([100, 400, 300, 415], [100, 400, 300, 415])
    cov = coverage_page([cap])
    assert cov["F6"].status == STATUS_PASS
    assert cov["F6"].evaluated_nodes == 1
    assert run_defect_detectors([cap]) == []


def test_f6_fail_when_caption_displaced():
    cap = _caption([100, 400, 300, 415], [100, 100, 300, 115])  # moved up
    cov = coverage_page([cap])
    assert cov["F6"].status == STATUS_FAIL
    finds = run_defect_detectors([cap])
    assert any(f.defect_id == "F6" for f in finds), finds


def test_f6_skip_when_no_caption():
    par = _trace(
        kind="flow",
        dst_box=[0, 0, 100, 50],
        render_rows=[{"text": "t", "v3_bbox": [0, 0, 100, 50]}],
    )
    cov = coverage_page([par])
    assert cov["F6"].status == STATUS_SKIP


def test_f5_f6_do_not_false_positive_on_f4_page():
    # Detector independence: a caption-page that carries an F4 parser anomaly
    # (the p300 case) must NOT cascade a false F5/F6.
    cap = _caption([100, 400, 300, 415], [100, 402, 300, 415])  # correct spot
    cap.source_text = "(cid:129) figure caption"  # parser-originated CID
    cap.translated_text = "(cid:129) figure caption"
    fig = _float_drawn("figure", [100, 0, 300, 350], [100, 0, 300, 350])
    cov = coverage_page([fig, cap])
    assert cov["F5"].status == STATUS_PASS
    assert cov["F6"].status == STATUS_PASS
    assert cov["F4"].status == STATUS_FAIL  # F4 still fires on its own


# ── F8 text truncated (node-level, layout clip verdict) ────────────────────


def _flow(dst, overflow, recovery=None, node="p0_9"):
    return _trace(
        node_id=node,
        kind="flow",
        layout_overflow=overflow,
        layout_recovery=recovery,
        dst_box=dst,
        render_rows=[{"text": "x", "font_size": 10.0, "v3_bbox": dst}],
    )


_CLIP_REC = {
    "reason": "width",
    "decision": "clip",
    "steps": ["SHRINK", "CLIP"],
    "final_font_size": 5.0,
}


def test_f8_pass_when_flow_fits():
    tr = _flow([0, 0, 100, 50], False)
    cov = coverage_page([tr])
    assert cov["F8"].status == STATUS_PASS
    assert cov["F8"].evaluated_nodes == 1
    assert run_defect_detectors([tr]) == []


def test_f8_fail_when_layout_clipped_block():
    tr = _flow([0, 0, 100, 50], True, _CLIP_REC)
    cov = coverage_page([tr])
    assert cov["F8"].status == STATUS_FAIL
    finds = run_defect_detectors([tr])
    assert len(finds) == 1 and finds[0].defect_id == "F8"
    assert finds[0].first_divergence == "layout"


def test_f8_pass_when_overflow_without_clip():
    # overflow=True but recovery decision isn't "clip" (e.g. NEXT_PAGE) → not F8.
    tr = _flow([0, 0, 100, 50], True, {"decision": "next_page"})
    cov = coverage_page([tr])
    assert cov["F8"].status == STATUS_PASS


def test_f8_skip_when_no_layout_verdict():
    tr = _flow([0, 0, 100, 50], None)  # never through flow layout
    cov = coverage_page([tr])
    assert cov["F8"].status == STATUS_SKIP


def test_f8_not_triggered_by_missing_object():
    # F8 boundary: a block that was never drawn (F10/F8 dangling) must NOT fire.
    tr = _flow([0, 0, 100, 50], False, None)
    tr.render_rows = []
    cov = coverage_page([tr])
    assert cov["F8"].evaluated_nodes == 1  # settled layout measured fit
    assert cov["F8"].status == STATUS_PASS


def test_p300_f8_independent_of_f4():
    # The real p300 discriminator: same page can have an F4 parser artifact on
    # one block and a F8 clip on another; and a clean-fitted F4 block must not
    # be double-counted as F8.
    cap = _flow([100, 400, 300, 415], False)  # clean fit
    cap.source_text = "(cid:129) figure caption"  # F4 artifact, but fits fine
    cap.translated_text = "(cid:129) figure caption"
    clipped = _flow([100, 0, 300, 60], True, _CLIP_REC)  # genuinely clipped
    cov = coverage_page([clipped, cap])
    assert cov["F4"].status == STATUS_FAIL  # artifact present → F4
    assert cov["F8"].status == STATUS_FAIL  # a real clip happened → F8
    # independence: F4 doesn't manufacture an F8, and the F4 block itself was
    # measured as clean fit (not clipped).
    f8_find = [f for f in cov["F8"].findings if f.defect_id == "F8"]
    assert len(f8_find) == 1
    assert f8_find[0].node_id == clipped.node_id
