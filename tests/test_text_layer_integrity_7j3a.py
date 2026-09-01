# -*- coding: utf-8 -*-
"""7J-3A — F9 text-layer integrity detector contract tests.

Freezes the 7J-3A contract after the 7J-2 forensics found two text-layer
corruption families the F9 detector could not see:

  Case A: /ToUnicode CMap written in a subset-renumbered CID space that
          disagrees with the content-stream CIDs (Taylor & Francis footer
          → NUL in mono / GBK-mojibake in dual; glyphs fine).
  Case B: special code points lost to NUL in translated spans (► / ï / →).

Contract locked here:

  PASS    = sensor ran + content stream clean + text layer clean
  FAIL    = content-stream syntax anomaly, OR text-layer NUL corruption
            *with* translated content present on the page (cross-stage gate)
  SKIP    = no inspectable evidence, OR NUL suspect present but the page has
            no translated text (attribution unavailable → never FAIL)
  NOT_MEASURED = detector not wired (unused here; F9 is wired)

Invariant (7J-3A): NUL/mojibake ≠ automatic corruption — a NUL count alone
must not FAIL a page with no translated content, so exotic fonts with
legitimate control chars cannot be mislabelled.
"""

from __future__ import annotations

from dual_forensics.defect import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    coverage_page,
    run_defect_detectors,
)
from dual_forensics.diff import Trace
from dual_forensics.pdf_inspector import text_layer_integrity


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


def _dual(**kw):
    return {
        "content_stream": {"checked": True, "anomaly": False, "source": "regex"},
        "text_layer": {
            "checked": True,
            "nul_chars": 0,
            "samples": [],
            "corruption_suspect": False,
        },
        **kw,
    }


# ── sensor level ───────────────────────────────────────────────────────────


def test_sensor_counts_nul_and_samples():
    """text_layer_integrity reports NUL chars + context samples on a corrupt page."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Taylor\x00&\x00Francis")
    try:
        r = text_layer_integrity(doc, 0)
        assert r["checked"] is True
        assert r["nul_chars"] == 2
        assert r["corruption_suspect"] is True
        assert len(r["samples"]) == 2
    finally:
        doc.close()


def test_sensor_clean_page():
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "clean text layer")
    try:
        r = text_layer_integrity(doc, 0)
        assert r["checked"] is True
        assert r["nul_chars"] == 0
        assert r["corruption_suspect"] is False
    finally:
        doc.close()


# ── F9 contract ────────────────────────────────────────────────────────────


def test_f9_pass_clean_page():
    tr = _trace()
    cov = coverage_page([tr], _dual())
    assert cov["F9"].status == STATUS_PASS
    assert cov["F9"].evaluated_nodes == 1
    assert run_defect_detectors([tr], _dual()) == []


def test_f9_fail_on_content_stream_anomaly():
    tr = _trace()
    dual = _dual(
        content_stream={
            "checked": True,
            "anomaly": True,
            "sample": ["syntax error: unknown keyword '-1.2e'"],
            "source": "mupdf",
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F9"].status == STATUS_FAIL
    find = cov["F9"].findings[0]
    assert find.first_divergence == "render"
    assert any(f.defect_id == "F9" for f in run_defect_detectors([tr], dual))


def test_f9_fail_on_text_layer_nul_with_translated_content():
    """Case A/B: NUL corruption + translated text on page → FAIL @ render."""
    tr = _trace()  # translated_text set
    dual = _dual(
        text_layer={
            "checked": True,
            "nul_chars": 60,
            "samples": ["...Taylor\x00..."],
            "corruption_suspect": True,
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F9"].status == STATUS_FAIL
    find = cov["F9"].findings[0]
    assert find.first_divergence == "render"
    assert "NUL" in find.note
    assert find.evidence["nul_chars"] == 60
    assert any(f.defect_id == "F9" for f in run_defect_detectors([tr], dual))


def test_f9_skip_on_nul_without_translated_content():
    """7J-3A invariant: NUL alone (no translated text) → SKIP, never FAIL."""
    tr = _trace(translated_text="", translation_status="")
    dual = _dual(
        text_layer={
            "checked": True,
            "nul_chars": 5,
            "samples": ["...\x00..."],
            "corruption_suspect": True,
        }
    )
    cov = coverage_page([tr], dual)
    assert cov["F9"].status == STATUS_SKIP
    assert cov["F9"].evaluated_nodes == 0
    assert run_defect_detectors([tr], dual) == []


def test_f9_skip_when_no_evidence():
    tr = _trace()
    dual = {"content_stream": {"checked": False}, "text_layer": {"checked": False}}
    cov = coverage_page([tr], dual)
    assert cov["F9"].status == STATUS_SKIP
    assert run_defect_detectors([tr], dual) == []


def test_f9_pass_when_nul_sensor_absent_but_stream_clean():
    """Backward compatible: pages inspected before the 7J-3A sensor still
    evaluate from the content-stream evidence alone."""
    tr = _trace()
    dual = {"content_stream": {"checked": True, "anomaly": False, "source": "regex"}}
    cov = coverage_page([tr], dual)
    assert cov["F9"].status == STATUS_PASS
