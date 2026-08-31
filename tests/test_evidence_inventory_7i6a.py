# -*- coding: utf-8 -*-
"""7I-6A — Evidence Inventory contract tests.

Freezes the evidence-inventory findings for F5 / F7 / F9 / F10:

- F5  = representation gap → the *detector* exists but a page with no float
        block must stay SKIP (never 0, never a fabricated PASS).
- F7  = detector + methodology gap → NOT_MEASURED today; the source-vs-trans
        discrimination is degenerate under identity translation (assert the
        sensor FIELDS exist on Trace, so only wiring+corpus gating remains).
- F9  = wiring gap → the sensor ``pdf_inspector.content_stream_anomaly`` must
        exist (so 7I-6B is a wiring change, not new detection).
- F10 = wiring gap → the ID-direct aggregator must already compute
        present/dangling/stray (so 7I-6B is a wiring change).

These tests lock the *truthful* current state so nobody "fixes" the scorecard
by pretending SKIP/NOT_MEASURED is a clean 0, and so 7I-6B can prove it only
added wiring, not new semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dual_forensics.defect import (
    STATUS_NOT_MEASURED,
    STATUS_SKIP,
    coverage_page,
)
from dual_forensics.diff import Trace

_EDGE = Path(__file__).resolve().parents[1] / "doc" / "7i6" / "evidence_matrix.json"


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
    return Trace(**base)


# ── F5: representation gap → SKIP, detector present ────────────────────────


def test_f5_page_without_float_block_skips_not_zero():
    cov = coverage_page([_trace(kind="paragraph")])
    assert cov["F5"].status == STATUS_SKIP
    # a SKIP page contributes nothing to pages_evaluated (never a clean 0)
    from dual_forensics.defect import aggregate_coverage

    agg = aggregate_coverage({0: cov})
    assert agg["F5"]["pages_evaluated"] == 0
    assert agg["F5"]["status"] == STATUS_SKIP


def test_f5_matrix_records_no_float_blocks_in_corpus():
    d = json.loads(_EDGE.read_text(encoding="utf-8"))
    sig = d["signals"]["F5"]
    assert sig["gap_type"] == "representation"
    assert sig["float_model_blocks"] == 0  # corpus has no float model block
    assert sig["status"] == STATUS_SKIP


# ── F7: detector+methodology gap → NOT_MEASURED, sensor fields on Trace ────


def test_f7_not_measured_and_trace_carries_text_sensors():
    cov = coverage_page([_trace()])
    assert cov["F7"].status == STATUS_NOT_MEASURED
    # the *sensor fields* needed for a real F7 already live on Trace — the gap
    # is a detector + a real-translation corpus, not missing data plumbing.
    t = _trace(source_text="old", translated_text="新")
    t.render_rows = [{"text": "新"}]
    assert t.source_text == "old"
    assert t.translated_text == "新"
    assert t.rendered_text == "新"  # property over render_rows


def test_f7_matrix_is_not_measured():
    d = json.loads(_EDGE.read_text(encoding="utf-8"))
    sig = d["signals"]["F7"]
    assert sig["status"] == STATUS_NOT_MEASURED
    assert sig["gap_type"] == "detector+methodology"


# ── F9: wiring gap → SKIP without content_stream; sensor MUST exist ───────


def test_f9_skip_without_content_stream():
    # 7I-6B wired the content_stream_anomaly sensor into coverage: a page with
    # no dual_page evidence cannot be judged → SKIP, never a fabricated PASS.
    cov = coverage_page([_trace()])
    assert cov["F9"].status == STATUS_SKIP


def test_f9_signal_content_stream_anomaly_exists():
    # the sensor is implemented (7H-2B); 7I-6B only wires it into coverage.
    from dual_forensics.pdf_inspector import content_stream_anomaly

    assert callable(content_stream_anomaly)


def test_f9_matrix_wiring_gap():
    d = json.loads(_EDGE.read_text(encoding="utf-8"))
    sig = d["signals"]["F9"]
    assert sig["gap_type"] == "wiring"
    assert sig["signal_available"] is True
    assert sig["in_pipeline_anomaly_pages"] == 0  # clean emitter since 7H-2B


# ── F10: wiring gap → SKIP without ID-direct prov; prov summary exists ─────


def test_f10_skip_without_provenance():
    # 7I-6B wired the ID-direct provenance into coverage: a page with no
    # dual_page provenance cannot be judged → SKIP, never a fabricated PASS.
    cov = coverage_page([_trace()])
    assert cov["F10"].status == STATUS_SKIP


def test_f10_id_direct_summary_computes_presence():
    from dual_forensics.diff import aggregate_page_id_direct

    rows = [
        {
            "node_id": "p0_0",
            "kind": "paragraph",
            "parser": {"text": "x"},
            "layout": {"target_bbox": [0, 0, 10, 10]},
        },
        {
            "node_id": "p0_1",
            "kind": "paragraph",
            "parser": {"text": "y"},
            "layout": {"target_bbox": [0, 30, 10, 40]},
        },
    ]
    prov = {
        "p0_0": [
            {
                "source_node_id": "p0_0",
                "object_type": "text",
                "final_bbox_v3": [0, 0, 10, 10],
            }
        ]
    }
    out = aggregate_page_id_direct(0, rows, prov)
    # p0_1 has no provenance record → dangling; p0_0 present; no stray
    assert out["dangling_blocks"] == ["p0_1"]
    assert out["stray_records"] == []


def test_f10_matrix_wiring_gap():
    d = json.loads(_EDGE.read_text(encoding="utf-8"))
    sig = d["signals"]["F10"]
    assert sig["gap_type"] == "wiring"
    assert sig["signal_available"] is True
    assert sig["dangling_blocks"] == 0  # corpus: every block present


# ── honest-states principle: SKIP/NOT_MEASURED are never a clean 0 ─────────


def test_none_of_the_four_inventory_defects_reads_as_pass_on_noes():
    cov = coverage_page([_trace(kind="paragraph")])
    # none of F5/F7/F9/F10 may be PASS or FAIL when no/insufficient evidence
    for fid in ("F5", "F7", "F9", "F10"):
        assert cov[fid].status in (STATUS_SKIP, STATUS_NOT_MEASURED)
        assert cov[fid].status != "PASS"
        assert cov[fid].evaluated_nodes == 0


def test_evidence_matrix_and_report_present():
    assert _EDGE.exists()
    assert (Path(__file__).resolve().parents[1] / "doc" / "7i6" / "report.md").exists()
    assert (
        Path(__file__).resolve().parents[1] / "doc" / "7i6" / "evidence_inventory.md"
    ).exists()


if __name__ == "__main__":
    import sys
    import pytest as _pt

    sys.exit(_pt.main([__file__]))
