# -*- coding: utf-8 -*-
"""7I-6C — Gap Closure / Corpus Eligibility contract tests.

Freezes the evidence-only findings of the F5/F7 eligibility assessment:

- F5  = representation gap with a DORMANT capability: ``annotate_figures``
        exists and can create ``kind="figure"`` blocks, but is never called
        from the model build path.  F5 must stay SKIP — wiring the capability
        into ``build_document_model`` is a production decision, not a scorecard
        fix (assert the capability is currently unused).
- F7  = NOT_MEASURED, gated on a real-translation HARNESS (not corpus): real
        dual/mono translation artifacts exist, but ``capture_source_chain`` is
        identity-only by design, so no per-block 3-stage triple exists in the
        pipeline.  Identity translation must never be an F7 FAIL.
- The final F1–F10 matrix states are frozen (PASS/FAIL/SKIP/NOT_MEASURED with
  strict semantics: SKIP and NOT_MEASURED are never a clean 0).

These tests lock the truthful boundary so nobody converts a representation gap
or an unwired detector into a PASS to green the scorecard.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from dual_forensics.defect import (
    STATUS_NOT_MEASURED,
    STATUS_SKIP,
    aggregate_coverage,
    coverage_page,
)
from dual_forensics.diff import Trace

_ROOT = Path(__file__).resolve().parents[1]


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


# ── F5: dormant capability — representation gap stays SKIP ──────────────


def test_f5_stays_skip_with_no_float_blocks():
    """A page with only paragraphs must leave F5 SKIP, never PASS/0."""
    cov = coverage_page([_trace()], {})
    assert cov["F5"].status == STATUS_SKIP
    assert cov["F5"].findings == []


def test_f5_dormant_figure_capability_is_unused():
    """annotate_figures must exist (capability) but have no callers (dormant).

    This is the 7I-6C F5 finding: the model CAN express figure blocks, but the
    build path never invokes it.  If this test starts failing because someone
    wired the capability into production, that is a *deliberate* production
    decision — it must come with a real float corpus, not as a scorecard fix.
    """
    src = (_ROOT / "pdf2zh" / "v3" / "figure_understanding.py").read_text(
        encoding="utf-8"
    )
    assert "def annotate_figures" in src

    # scan pdf2zh for callers outside the definition + re-exports
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print('ok')"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0

    grep = subprocess.run(
        ["grep", "-rn", "annotate_figures", "pdf2zh"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = grep.stdout.splitlines()
    calls = [
        ln
        for ln in lines
        if "def annotate_figures" not in ln
        and "__all__" not in ln
        and "annotate_figures," not in ln
        and '"annotate_figures"' not in ln
        and ".pyc" not in ln
    ]
    # the definition docstring/comment line is fine; real callers would show a
    # function-call site like ``annotate_figures(``
    calls = [ln for ln in calls if "annotate_figures(" in ln]
    assert calls == [], f"annotate_figures gained unexpected callers: {calls}"


def test_f5_build_path_emits_no_figure_kind():
    """build_document_model must not currently emit figure/image blocks."""
    src = (_ROOT / "pdf2zh" / "v3" / "document_model.py").read_text(encoding="utf-8")
    # no literal float-kind emission in the model builder
    assert 'kind="figure"' not in src
    assert 'kind="image"' not in src


# ── F7: real-translation harness gate — NOT_MEASURED, identity never FAIL ─


def test_f7_stays_not_measured():
    cov = coverage_page([_trace()], {})
    assert cov["F7"].status == STATUS_NOT_MEASURED


def test_f7_identity_translation_is_not_fail():
    """Invariant: under identity translation F7 is never FAIL, never a finding.

    Even when translated_text == source_text (the degenerate harness mode),
    F7 must not report source leftover — that is an artifact of identity
    translation, not a real F7 defect.
    """
    t = _trace(source_text="hello world", translated_text="hello world")
    cov = coverage_page([t], {})
    assert cov["F7"].status == STATUS_NOT_MEASURED
    assert cov["F7"].findings == []


def test_f7_contract_semantics_frozen():
    """The frozen F7 contract must be documented in the 7I-6A inventory.

    PASS/FAIL require real-translation evidence; SKIP covers insufficient
    evidence / identity; NOT_MEASURED is the current honest state.
    """
    inv = (_ROOT / "doc" / "7i6" / "evidence_inventory.md").read_text(encoding="utf-8")
    assert "NOT_MEASURED" in inv
    assert "F7" in inv
    # identity-translation invariant is recorded
    assert "identity" in inv.lower()


# ── final matrix: strict state semantics ─────────────────────────────────


def test_skip_and_not_measured_are_never_clean_zero():
    """SKIP/NOT_MEASURED cells must not contribute pages_evaluated."""
    cov = coverage_page([_trace()], {})
    # per-page honest state: F5 SKIP (no float object), F7 NOT_MEASURED
    assert cov["F5"].status == STATUS_SKIP
    assert cov["F7"].status == STATUS_NOT_MEASURED
    agg = aggregate_coverage({0: cov})
    # pages without evaluable evidence never count as measured clean
    assert agg["F5"]["pages_evaluated"] == 0
    assert agg["F7"]["pages_evaluated"] == 0


def test_real_translation_artifacts_exist():
    """7I-6C corpus-eligibility fact: real dual/mono outputs exist.

    F7's gate is NOT corpus availability (artifacts exist) — it is harness
    ingestion.  Assert the artifacts are present so nobody re-opens the gate by
    claiming 'no real translation corpus exists'.
    """
    files = sorted((_ROOT / "pdf2zh_files").glob("*-dual.pdf")) + sorted(
        (_ROOT / "pdf2zh_files").glob("*-mono.pdf")
    )
    assert len(files) >= 6, f"expected dual+mono artifacts, found {len(files)}"
    names = {p.name for p in files}
    assert any("dual" in n for n in names)
    assert any("mono" in n for n in names)


def test_final_matrix_documented():
    """The frozen final matrix must exist and record F4's single residual."""
    m = (_ROOT / "doc" / "7i6c" / "final_matrix.md").read_text(encoding="utf-8")
    assert "F4" in m
    assert "NOT_MEASURED" in m
    assert "SKIP" in m
