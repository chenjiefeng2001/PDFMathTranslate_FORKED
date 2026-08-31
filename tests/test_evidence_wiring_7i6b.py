# -*- coding: utf-8 -*-
"""7I-6B — wiring test latch (F10 / F9) + F7 contract freeze.

Per the frozen order (F10 → F9 → F7 contract; F5 untouched):

- F10: now a page-level detector over the ID-direct provenance summary
  (``dual_page[\"id_direct\"]``).  present → PASS, dangling/stray → FAIL,
  no provenance → SKIP.  A block never owed by the plan stays SKIP (not PASS),
  never a fabricated clean 0.
- F9: : now a page-level detector over ``dual_page[\"content_stream\"]``.
  checked+clean → PASS, anomaly → FAIL, no content stream → SKIP.
- F7: contract frozen only — stays NOT_MEASURED and, crucially, identity
  (``translated_text == source_text``) must NOT be treated as F7 FAIL.
- F5: representation gap untouched → SKIP when no float block.
"""

from __future__ import annotations

import pytest

from dual_forensics.defect import (
    STATUS_FAIL,
    STATUS_NOT_MEASURED,
    STATUS_PASS,
    STATUS_SKIP,
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
    return Trace(**base)


def _duals(**kw):
    return kw  # caller passes {"id_direct": ..., "content_stream": ...}


# ── F10 wire: present → PASS, dangling/stray → FAIL, no prov → SKIP ────────


def test_f10_pass_when_all_blocks_present():
    tr = _trace()
    dual = _duals(
        id_direct={
            "page": 0,
            "present_blocks": 3,
            "dangling_blocks": [],
            "stray_records": [],
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F10"].status == STATUS_PASS
    assert cov["F10"].evaluated_nodes == 3
    assert run_defect_detectors([tr], dual) == []


def test_f10_fail_on_dangling_block():
    tr = _trace()
    dual = _duals(
        id_direct={
            "page": 0,
            "present_blocks": 2,
            "dangling_blocks": ["p0_2"],
            "stray_records": [],
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F10"].status == STATUS_FAIL
    find = cov["F10"].findings[0]
    assert find.first_divergence == "render"
    finds = run_defect_detectors([tr], dual)
    assert any(f.defect_id == "F10" for f in finds)


def test_f10_fail_on_stray_record():
    tr = _trace()
    dual = _duals(
        id_direct={
            "page": 0,
            "present_blocks": 2,
            "dangling_blocks": [],
            "stray_records": [{"source_node_id": "ghost", "object_type": "text"}],
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F10"].status == STATUS_FAIL


def test_f10_skip_when_no_provenance():
    # no id_direct summary on the page → nothing measured → SKIP, not PASS.
    tr = _trace()
    cov = coverage_page([tr], _duals())  # no evidence keyed at all
    assert cov["F10"].status == STATUS_SKIP
    assert cov["F10"].evaluated_nodes == 0


def test_f10_skip_when_block_never_owed():
    # no provenance ran for this page: the "missing" can't be a confirmed drop.
    tr = _trace()
    cov = coverage_page([tr], _duals(id_direct={"page": 0}))  # incomplete
    assert cov["F10"].status == STATUS_SKIP


# ── F9 wire: checked+clean → PASS, anomaly → FAIL, no stream → SKIP ────────


def test_f9_pass_when_emitter_clean():
    tr = _trace()
    dual = _duals(
        content_stream={
            "checked": True,
            "anomaly": False,
            "sample": [],
            "source": "mupdf",
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F9"].status == STATUS_PASS
    assert cov["F9"].evaluated_nodes == 1
    assert run_defect_detectors([tr], dual) == []


def test_f9_fail_on_malformed_float():
    tr = _trace()
    dual = _duals(
        content_stream={
            "checked": True,
            "anomaly": True,
            "sample": ["-9.0e"],
            "source": "regex",
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F9"].status == STATUS_FAIL
    find = cov["F9"].findings[0]
    assert find.first_divergence == "render"
    finds = run_defect_detectors([tr], dual)
    assert any(f.defect_id == "F9" for f in finds)


def test_f9_skip_when_no_content_stream():
    tr = _trace()
    cov = coverage_page([tr], _duals())
    assert cov["F9"].status == STATUS_SKIP
    assert cov["F9"].evaluated_nodes == 0


# ── F7 contract freeze: stays NOT_MEASURED; identity is NOT FAIL ───────────


def test_f7_remains_not_measured():
    # detector contract not wired — gated on a real-translation corpus.
    cov = coverage_page([_trace()], _duals())
    assert cov["F7"].status == STATUS_NOT_MEASURED


def test_f7_identity_translation_is_not_fail():
    # CRITICAL invariant: under identity translation translated==source is the
    # null case and must never be counted as an F7 leftover/duplicate finding.
    tr = _trace(source_text="hello world", translated_text="hello world")
    tr.render_rows = [{"text": "hello world"}]
    cov = coverage_page([tr], _duals())
    assert cov["F7"].status == STATUS_NOT_MEASURED
    assert cov["F7"].findings == []
    assert run_defect_detectors([tr], _duals()) == []


# ── F5 untouched: representation gap → SKIP without a float block ──────────


def test_f5_still_skips_without_float_block():
    tr = _trace(kind="paragraph")
    cov = coverage_page([tr], _duals())
    assert cov["F5"].status == STATUS_SKIP


# ── independence ~ wiring doesn't cascade onto other detectors ─────────────


def test_f9_f10_wiring_does_not_cascade_to_f1_f3():
    # a clean page with F9/F10 measured must not manufacture F1/F3 findings.
    tr = _drawn_trace()
    dual = _duals(
        id_direct={"present_blocks": 1, "dangling_blocks": [], "stray_records": []},
        content_stream={"checked": True, "anomaly": False},
    )
    cov = coverage_page([tr], dual)
    assert cov["F1"].status == STATUS_PASS
    assert cov["F3"].status == STATUS_PASS


def _drawn_trace():
    tr = _trace(dst_box=[0, 0, 100, 50], layout_font_size=12.0)
    tr.render_rows = [{"text": "x", "font_size": 12.0, "v3_bbox": [0, 0, 100, 50]}]
    tr.render_box = [0, 0, 100, 50]
    return tr


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__]))
