"""Commit 7F-5d — adaptive-TOC golden corpus + regression gate.

The six 7F-5c golden cases become a machine-checkable corpus:

    source PDF → extract → normalize → compute_report
    output PDF → extract → normalize →            ↓
                            compare_reports(baseline, current) → pass

The committed baseline ``tests/baselines/toc_adaptive_7f5d.json`` records the
expected metrics with provenance.  The gate aggregates per-case metrics with
``min`` — any single case regression fails the whole gate — and compares the
result against the baseline, so swapping the ONNX model, fonts or layout
policy can no longer silently break the TOC adaptive contract.
"""

import os

from pdf2zh.semantic.eval import compare_reports, evaluate, load_baseline
from tests.pdf_eval_build import (
    build_toc_adaptive_cjk_output,
    build_toc_adaptive_cjk_source,
    build_toc_adaptive_extreme_output,
    build_toc_adaptive_shrink_output,
    build_toc_adaptive_short,
    build_toc_adaptive_wrap_output,
    build_toc_multiline,
)

BASELINE = os.path.join(
    os.path.dirname(__file__), "baselines", "toc_adaptive_7f5d.json"
)

ADAPTIVE_METRICS = [
    "toc_adaptive_wrap_integrity",
    "toc_adaptive_font_size",
    "toc_adaptive_overflow",
    "toc_page_column_stability",
    "toc_continuation_x_accuracy",
]

#: (name, source builder, output builder) — output mirrors the 7F-5a/5b
#: layout contract geometry as rendered by the golden renderer.
CASES = [
    ("toc_short", build_toc_adaptive_short, build_toc_adaptive_short),
    ("toc_long_wrap", build_toc_adaptive_short, build_toc_adaptive_wrap_output),
    ("toc_long_shrink", build_toc_adaptive_short, build_toc_adaptive_shrink_output),
    (
        "toc_extreme_overflow",
        build_toc_adaptive_short,
        build_toc_adaptive_extreme_output,
    ),
    ("toc_cjk", build_toc_adaptive_cjk_source, build_toc_adaptive_cjk_output),
    ("toc_multiline_continuation", build_toc_multiline, build_toc_multiline),
]


def corpus_metrics(tmp_path):
    """min across cases of every report metric (any case regression fails)."""
    mins = {}
    for name, sb, ob in CASES:
        src = str(tmp_path / f"{name}_src.pdf")
        out = str(tmp_path / f"{name}_out.pdf")
        sb(src)
        ob(out)
        r = evaluate(src, out)["metrics"]
        for key, val in r.items():
            if key.startswith("_"):
                continue
            mins[key] = min(mins.get(key, val), val)
    return mins


def test_corpus_adaptive_metrics_perfect(tmp_path):
    m = corpus_metrics(tmp_path)
    for key in ADAPTIVE_METRICS:
        assert m[key] == 1.0, f"{key} should be 1.0 across the corpus, got {m[key]}"


def test_corpus_gate_passes_against_committed_baseline(tmp_path):
    base = load_baseline(BASELINE)
    res = compare_reports(base, corpus_metrics(tmp_path))
    assert res["status"] == "pass", res["regressions"]
    assert res["regressions"] == []


def test_baseline_file_matches_current(tmp_path):
    """The committed baseline must equal today's corpus metrics.  Regenerate
    it deliberately (via save_baseline) when metrics legitimately change."""
    base = load_baseline(BASELINE)["metrics"]
    cur = corpus_metrics(tmp_path)
    for key, val in base.items():
        assert key in cur, f"baseline metric {key} missing from current report"
        assert (
            abs(cur[key] - val) < 1e-6
        ), f"{key}: baseline {val} != current {cur[key]}"
